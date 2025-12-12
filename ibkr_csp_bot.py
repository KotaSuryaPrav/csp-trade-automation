"""
IBKR Cash-Secured Put Bot — December CSP Protocol

Requirements:
  pip install ib_insync

Runtime prerequisites:
  - Trader Workstation (TWS) or IB Gateway running
  - API enabled (socket) and port open (paper: 7497 commonly, live: 7496 commonly)
  - Market data subscriptions recommended for US options (bid/ask/modelGreeks)

Universe:
  - Provide a file sp500.txt with one symbol per line (AAPL, MSFT, ...)

Disclaimer:
  - Trading automation is risky. Use paper trading first.
"""

import os
import json
import math
import logging
import argparse
import time
import json
import re

from pathlib import Path
from urllib.request import Request, urlopen
from dataclasses import dataclass
from datetime import datetime, date, time
from typing import Dict, Any, List, Optional, Tuple

from zoneinfo import ZoneInfo
from ib_insync import IB, Stock, Option, LimitOrder, util #pip install ib_insync


# =========================
# CONFIG
# =========================

@dataclass(frozen=True)
class BotConfig:
    # --- Connection ---
    host: str = "127.0.0.1"
    port: int = 7497          # 7497 paper, 7496 live (commonly)
    client_id: int = 7

    # --- Account / Portfolio ---
    # Fallback if accountSummary fails
    portfolio_value_usd_fallback: float = 50_000.0

    # --- Universe ---
    universe_file: str = "sp500.txt"   # one symbol per line
    max_underlyings_to_scan: int = 10  # throttle scanning load

    max_expiries_per_symbol = 1
    max_strikes_per_expiry = 5

    # Universe selection
    universe: str = "SP500"  # "SP500" (later you can add "NIFTY100")

    # SP500 retrieval (no local file needed)
    sp500_source: str = "wikipedia"  # keep as wikipedia for exact constituents
    sp500_cache_file: str = "universe_cache_sp500.json"
    sp500_cache_ttl_seconds: int = 24 * 60 * 60  # refresh daily

    # --- Strategy parameters ---
    min_dte: int = 5
    max_dte: int = 15

    delta_min: float = 0.05
    delta_target_max: float = 0.15
    delta_absolute_max: float = 0.25
    delta_alert: float = 0.30

    required_roc_per_dte: float = 0.01  # 1% per day

    position_min_pct: float = 0.02      # 2% of portfolio collateral per CSP
    position_max_pct: float = 0.05      # 5% of portfolio collateral per CSP
    max_notional_per_ticker_pct: float = 0.05
    max_total_csp_collateral_pct: float = 0.60
    max_margin_usage_pct: float = 0.75
    min_liquidity_buffer_pct: float = 0.25

    profit_take_pct: float = 0.75       # 75% premium capture
    min_dte_for_profit_take: int = 2

    itm_roll_dte: int = 3
    hard_loss_pct_of_collateral: float = 0.03
    daily_loss_stop_pct: float = 0.02

    # --- US market hours (ET) ---
    exchange_tz: str = "America/New_York"
    trade_start: time = time(9, 35)     # avoid first minutes
    trade_end: time = time(15, 45)      # avoid last 15 mins (close at 16:00)

    # --- Testing controls ---
    test_mode: bool = True             # set True for paper testing anytime
    loop_mode: bool = True             # keep running
    loop_interval_seconds: int = 60    # run scan/monitor every 60s

    dry_run: bool = True                # NO ORDERS placed
    dry_run_save_simulated_positions: bool = False  # keep False: no JSON writes

    # --- Greeks fallback if modelGreeks missing ---
    risk_free_rate: float = 0.04
    default_iv: float = 0.25

    # --- Execution controls ---
    max_new_trades_per_run: int = 3
    require_bid_ask: bool = False
    max_spread_pct: float = 0.35        # (ask-bid)/mid

    # --- Market data controls ---
    use_snapshot_data = True
    allow_delayed_data = True

    # --- State persistence ---
    open_positions_file: str = "csp_open_positions.json"
    daily_pnl_file: str = "csp_daily_pnl.json"

    # Greeks
    use_ibkr_greeks = False
    default_iv = 0.30   # fixed IV for BS delta approximation


CFG = BotConfig()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# =========================
# JSON STATE
# =========================

def load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

def today_key(tz: ZoneInfo) -> str:
    return datetime.now(tz).date().isoformat()

def get_daily_realized_pnl(tz: ZoneInfo) -> float:
    data = load_json(CFG.daily_pnl_file, {})
    return float(data.get(today_key(tz), 0.0))

def add_daily_realized_pnl(tz: ZoneInfo, delta_pnl: float) -> None:
    data = load_json(CFG.daily_pnl_file, {})
    k = today_key(tz)
    data[k] = float(data.get(k, 0.0) + delta_pnl)
    save_json(CFG.daily_pnl_file, data)


# =========================
# HOURS / TIME
# =========================

def in_trading_window(cfg: BotConfig) -> bool:
    #test mode active
    if cfg.test_mode:
        return True

    tz = ZoneInfo(cfg.exchange_tz)
    now_dt = datetime.now(tz)

    if now_dt.weekday() >= 5:
        return False

    now_t = now_dt.time()
    return cfg.trade_start <= now_t <= cfg.trade_end


# =========================
# MATH HELPERS
# =========================

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def approx_put_delta_bs(spot: float, strike: float, dte: int, iv: float, r: float) -> float:
    if dte <= 0 or spot <= 0 or strike <= 0 or iv <= 0:
        return -0.5
    T = dte / 365.0
    sigma = iv
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) - 1.0  # put delta

def dte_from_yyyymmdd(exp: str, tz: ZoneInfo) -> int:
    exp_date = datetime.strptime(exp, "%Y%m%d").date()
    return (exp_date - datetime.now(tz).date()).days


# =========================
# UNIVERSE
# =========================

def load_universe_symbols(cfg: BotConfig) -> List[str]:
    if cfg.universe == "SP500":
        return load_sp500_universe(cfg)
    raise ValueError(f"Unsupported universe: {cfg.universe}")

import re
import time
from pathlib import Path

def _ibkr_symbol_normalize(sym: str) -> str:
    """
    IBKR typically expects class shares with a space instead of dot:
      BRK.B -> BRK B
      BF.B  -> BF B
    """
    s = sym.strip().upper().replace(".", " ")
    s = re.sub(r"\s+", " ", s)
    return s


def _read_cache(cache_path: Path, max_age_seconds: int, max_rows: int) -> list:
    if not cache_path.exists():
        return []
    try:
        obj = json.loads(cache_path.read_text())
        fetched_at = float(obj.get("fetched_at", 0))
        symbols = obj.get("symbols", [])
        if not isinstance(symbols, list):
            return []
        # allow stale cache usage if needed (handled by caller)
        syms = [_ibkr_symbol_normalize(s) for s in symbols]
        syms = [s for s in syms if s]
        # If still fresh, return immediately in caller
        return syms[:max_rows]
    except Exception:
        return []

def load_sp500_universe(cfg) -> List[str]:
    """
    Fetch exact S&P 500 constituents from Wikipedia but in a 403-resistant way:
    - Use a browser-like User-Agent
    - Parse HTML string via pandas.read_html
    - Cache results
    - If fetch fails, fall back to cache (even if stale)
    """
    cache_path = Path(cfg.sp500_cache_file)

    # 1) If cache exists and is fresh, use it
    if cache_path.exists():
        try:
            obj = json.loads(cache_path.read_text())
            fetched_at = float(obj.get("fetched_at", 0))
            if time.time() - fetched_at < cfg.sp500_cache_ttl_seconds:
                syms = [_ibkr_symbol_normalize(s) for s in obj.get("symbols", [])]
                syms = [s for s in syms if s]
                logging.info(f"S&P500 universe loaded from cache: {len(syms)} symbols")
                return syms[: cfg.max_underlyings_to_scan]
        except Exception:
            pass  # continue to fetch

    # 2) Fetch HTML with headers (avoid 403)
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        import pandas as pd
        tables = pd.read_html(html, header=0)
        df = tables[0]
        raw = df["Symbol"].astype(str).tolist()
        symbols = [_ibkr_symbol_normalize(s) for s in raw]
        symbols = [s for s in symbols if s]
        symbols = list(dict.fromkeys(symbols))  # de-dupe

        # Write cache
        try:
            cache_path.write_text(json.dumps({"fetched_at": time.time(), "symbols": symbols}, indent=2))
        except Exception:
            pass

        logging.info(f"S&P500 universe loaded from Wikipedia (fetched): {len(symbols)} symbols")
        return symbols[: cfg.max_underlyings_to_scan]

    except Exception as e:
        logging.error(f"Failed to fetch S&P500 from Wikipedia: {e}. Falling back to cached universe if available.")
        # 3) Fallback to cache (even if stale)
        cached = _read_cache(cache_path, cfg.sp500_cache_ttl_seconds, cfg.max_underlyings_to_scan)
        if cached:
            logging.warning(f"Using STALE cached S&P500 universe: {len(cached)} symbols")
            return cached[: cfg.max_underlyings_to_scan]

        # 4) As a last resort, return a small hardcoded set so the bot doesn't die
        logging.warning("No cache available. Using minimal fallback universe.")
        return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"]


# =========================
# BOT
# =========================

class IbkrCspBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.exchange_tz)
        self.ib = IB()

    # ---------- connection ----------
    def connect(self) -> None:
        self.ib.RequestTimeout = 30
        self.ib.connect(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=30)
        logging.info(f"Connected to IBKR (host={self.cfg.host}, port={self.cfg.port}, clientId={self.cfg.client_id}).")

    def disconnect(self) -> None:
        self.ib.disconnect()
        logging.info("Disconnected.")

    # ---------- portfolio value (USD) ----------
    def get_portfolio_value_usd(self) -> float:
        """
        Auto-read NetLiquidation in USD from IBKR accountSummary.
        Safe fallback to configured value if not available.
        """
        try:
            rows = self.ib.accountSummary()
            for r in rows:
                if r.tag == "NetLiquidation" and r.currency == "USD":
                    v = float(r.value)
                    if v > 0:
                        logging.info(f"Portfolio NetLiquidation (USD) from IBKR: {v:.2f}")
                        return v

            # fallback: any NetLiquidation if USD not found
            for r in rows:
                if r.tag == "NetLiquidation" and r.value:
                    v = float(r.value)
                    if v > 0:
                        logging.warning(f"NetLiquidation USD not found; using NetLiquidation {r.currency}: {v:.2f}")
                        return v

        except Exception as e:
            logging.warning(f"AccountSummary read failed; fallback to config. Error: {e}")

        logging.warning(f"Falling back to portfolio_value_usd_fallback={self.cfg.portfolio_value_usd_fallback:.2f}")
        return float(self.cfg.portfolio_value_usd_fallback)

    # ---------- state ----------
    def current_open_positions(self) -> List[Dict[str, Any]]:
        return load_json(self.cfg.open_positions_file, [])

    def total_csp_collateral(self, positions: List[Dict[str, Any]]) -> float:
        return sum(float(p["collateral"]) for p in positions if p.get("status") == "OPEN")

    def notional_by_ticker(self, positions: List[Dict[str, Any]]) -> Dict[str, float]:
        d: Dict[str, float] = {}
        for p in positions:
            if p.get("status") != "OPEN":
                continue
            u = p["underlying"]
            d[u] = d.get(u, 0.0) + float(p["collateral"])
        return d

    # ---------- market data helpers ----------
    def get_stock_price(self, sym: str) -> Optional[float]:
        stk = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(stk)
    
        # Use snapshot to reduce subscription issues
        t = self.ib.reqMktData(stk, "", snapshot=True, regulatorySnapshot=False)
        self.ib.sleep(1.2)
    
        # Try multiple fields in order of preference
        px = None
        if t.last and t.last > 0:
            px = t.last
        elif t.close and t.close > 0:
            px = t.close
        elif t.marketPrice() and t.marketPrice() > 0:
            px = t.marketPrice()
    
        # No need to cancel snapshot
        if not px or (isinstance(px, float) and (math.isnan(px) or px <= 0)):
            return None
        return float(px)

    def get_tickers(self, contracts: List) -> Dict[int, Any]:
        if not contracts:
            return {}
        ticks = self.ib.reqTickers(*contracts)
        out = {}
        for t in ticks:
            if t.contract and t.contract.conId:
                out[int(t.contract.conId)] = t
        return out

    # ---------- option chain discovery ----------
    def get_option_chain_params(self, sym: str):
        stk = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(stk)

        params = self.ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
        if not params:
            raise RuntimeError(f"No option chain params for {sym}")
        # Usually first is fine for SMART
        return stk, params[0]

    # ---------- candidate builder for one underlying ----------
    def build_put_candidates_for_symbol(self, sym: str) -> List[Dict[str, Any]]:
        spot = self.get_stock_price(sym)
        if not spot:
            return []

        try:
            _, p = self.get_option_chain_params(sym)
        except Exception as e:
            logging.info(f"{sym}: chain params unavailable: {e}")
            return []

        # Expirations within 5–15 DTE
        valid_exps: List[Tuple[str, int]] = []
        for exp in sorted(p.expirations):
            dte = dte_from_yyyymmdd(exp, self.tz)
            if self.cfg.min_dte <= dte <= self.cfg.max_dte:
                valid_exps.append((exp, dte))

        if not valid_exps:
            return []

        # OTM put strikes: strike < spot
        strikes = sorted([float(k) for k in p.strikes if float(k) < spot])
        if not strikes:
            return []

        # throttle: take nearest 40 OTM strikes (closest to spot)
        strikes = strikes[-40:]

        # Build option contracts
        contracts = []
        meta = []
        for exp, dte in valid_exps:
            for strike in strikes:
                opt = Option(sym, exp, strike, "P", "SMART", tradingClass=sym)
                contracts.append(opt)
                meta.append((exp, dte, strike))

        if not contracts:
            return []

        self.ib.qualifyContracts(*contracts)
        tick_by_conid = self.get_tickers(contracts)

        candidates = []
        multiplier = 100  # US equity options

        for opt, (exp, dte, strike) in zip(contracts, meta):
            t = tick_by_conid.get(int(opt.conId))
            if not t:
                continue

            bid = t.bid
            ask = t.ask
            last = t.last

            if self.cfg.require_bid_ask and (not bid or not ask or bid <= 0 or ask <= 0):
                continue

            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                spread_pct = (ask - bid) / mid if mid > 0 else 999.0
                if spread_pct > self.cfg.max_spread_pct:
                    continue
            elif last and last > 0:
                mid = float(last)
            else:
                continue

            # Delta: prefer IB modelGreeks
            if t.modelGreeks and t.modelGreeks.delta is not None:
                delta = float(t.modelGreeks.delta)
            else:
                delta = approx_put_delta_bs(
                    spot=float(spot),
                    strike=float(strike),
                    dte=int(dte),
                    iv=self.cfg.default_iv,
                    r=self.cfg.risk_free_rate,
                )

            abs_delta = abs(delta)
            if abs_delta < self.cfg.delta_min or abs_delta > self.cfg.delta_absolute_max:
                continue

            collateral = float(strike) * multiplier
            premium_per_contract = float(mid) * multiplier
            premium_pct = premium_per_contract / collateral if collateral > 0 else 0.0
            roc_per_dte = premium_pct / dte if dte > 0 else 0.0

            if roc_per_dte < self.cfg.required_roc_per_dte:
                continue

            candidates.append({
                "underlying": sym,
                "spot": float(spot),
                "expiry": exp,
                "dte": int(dte),
                "strike": float(strike),
                "conId": int(opt.conId),
                "mid": float(mid),
                "bid": float(bid) if bid else None,
                "ask": float(ask) if ask else None,
                "delta": float(delta),
                "abs_delta": float(abs_delta),
                "multiplier": int(multiplier),
                "collateral": float(collateral),
                "premium_per_contract": float(premium_per_contract),
                "premium_pct": float(premium_pct),
                "roc_per_dte": float(roc_per_dte),
            })

        candidates.sort(key=lambda x: (-x["roc_per_dte"], x["abs_delta"]))
        return candidates

    # ---------- risk gating ----------
    def can_open_new_trades(self, portfolio_value: float, positions: List[Dict[str, Any]]) -> bool:
        realized_today = get_daily_realized_pnl(self.tz)
        if realized_today <= -self.cfg.daily_loss_stop_pct * portfolio_value:
            logging.warning("Daily loss stop hit: no new trades.")
            return False

        used = self.total_csp_collateral(positions)
        usage = used / portfolio_value if portfolio_value > 0 else 1.0
        buffer = 1.0 - usage

        if usage > self.cfg.max_margin_usage_pct:
            logging.warning("Usage > 75%: no new trades.")
            return False
        if buffer < self.cfg.min_liquidity_buffer_pct:
            logging.warning("Liquidity buffer < 25%: no new trades.")
            return False
        return True

    def passes_position_limits(self, cand: Dict[str, Any], portfolio_value: float, positions: List[Dict[str, Any]]) -> bool:
        collateral = float(cand["collateral"])
        pct = collateral / portfolio_value if portfolio_value > 0 else 999.0

        # 2–5% per position
        if pct < self.cfg.position_min_pct or pct > self.cfg.position_max_pct:
            return False

        # <=5% notional per ticker
        by_ticker = self.notional_by_ticker(positions)
        if (by_ticker.get(cand["underlying"], 0.0) + collateral) > self.cfg.max_notional_per_ticker_pct * portfolio_value:
            return False

        # <=60% total CSP collateral
        total_after = self.total_csp_collateral(positions) + collateral
        if total_after > self.cfg.max_total_csp_collateral_pct * portfolio_value:
            return False

        # usage <=75%, buffer >=25%
        usage_after = total_after / portfolio_value
        buffer_after = 1.0 - usage_after
        if usage_after > self.cfg.max_margin_usage_pct:
            return False
        if buffer_after < self.cfg.min_liquidity_buffer_pct:
            return False

        return True

    # ---------- execution ----------
    def place_short_put(self, cand: Dict[str, Any], qty_contracts: int = 1) -> Optional[str]:
        sym = cand["underlying"]
        limit_price = round(float(cand["mid"]), 2)

        if self.cfg.dry_run:
            fake_id = f"DRYRUN_SELL_{sym}_{cand['expiry']}_{cand['strike']}_{datetime.now(self.tz).strftime('%H%M%S')}"
            logging.info(
                f"[DRY-RUN] WOULD SELL {qty_contracts}x {sym} {cand['expiry']} {cand['strike']}P @ {limit_price} | "
                f"spot={cand['spot']:.2f} delta={cand['delta']:.3f} roc/dte={cand['roc_per_dte']:.4f} "
                f"collateral={cand['collateral']:.2f}"
            )
            return fake_id

        # live order path
        opt = Option(sym, cand["expiry"], float(cand["strike"]), "P", "SMART", tradingClass=sym)
        self.ib.qualifyContracts(opt)

        order = LimitOrder("SELL", qty_contracts, limit_price)
        trade = self.ib.placeOrder(opt, order)

        self.ib.sleep(0.8)
        oid = str(trade.order.orderId) if trade and trade.order else None
        logging.info(f"SELL {sym} {cand['expiry']} {cand['strike']}P @ {limit_price} (orderId={oid})")
        return oid

    def close_short_put(self, pos: Dict[str, Any], limit_price: float) -> Optional[str]:
        sym = pos["underlying"]
        expiry = pos["expiry"]
        strike = float(pos["strike"])
        qty = int(pos["qty"])
        limit_price = round(float(limit_price), 2)

        if self.cfg.dry_run:
            fake_id = f"DRYRUN_BUY_{sym}_{expiry}_{strike}_{datetime.now(self.tz).strftime('%H%M%S')}"
            logging.info(
                f"[DRY-RUN] WOULD BUY to close {qty}x {sym} {expiry} {strike}P @ {limit_price} | "
                f"entry_mid={pos.get('entry_mid')} last_mid={pos.get('last_mid')}"
            )
            return fake_id

        # live order path
        opt = Option(sym, expiry, strike, "P", "SMART", tradingClass=sym)
        self.ib.qualifyContracts(opt)

        order = LimitOrder("BUY", qty, limit_price)
        trade = self.ib.placeOrder(opt, order)

        self.ib.sleep(0.8)
        oid = str(trade.order.orderId) if trade and trade.order else None
        logging.info(f"BUY to close {sym} {expiry} {strike}P @ {limit_price} (orderId={oid})")
        return oid

    # ---------- quote for option + delta ----------
    def get_option_market(self, sym: str, expiry: str, strike: float) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        Returns (mid, bid, ask, delta, spot)
        """
        opt = Option(sym, expiry, strike, "P", "SMART", tradingClass=sym)
        stk = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(opt, stk)

        ticks = self.ib.reqTickers(opt, stk)
        self.ib.sleep(0.4)

        opt_t = next((t for t in ticks if t.contract and t.contract.conId == opt.conId), None)
        stk_t = next((t for t in ticks if t.contract and t.contract.conId == stk.conId), None)
        if not opt_t or not stk_t:
            return None, None, None, None, None

        bid, ask, last = opt_t.bid, opt_t.ask, opt_t.last
        mid = None
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        elif last and last > 0:
            mid = float(last)

        spot = stk_t.marketPrice()
        dte = dte_from_yyyymmdd(expiry, self.tz)

        if opt_t.modelGreeks and opt_t.modelGreeks.delta is not None:
            delta = float(opt_t.modelGreeks.delta)
        else:
            delta = approx_put_delta_bs(
                spot=float(spot) if spot else 0.0,
                strike=float(strike),
                dte=max(dte, 1),
                iv=self.cfg.default_iv,
                r=self.cfg.risk_free_rate,
            )

        return (float(mid) if mid else None,
                float(bid) if bid else None,
                float(ask) if ask else None,
                float(delta) if delta else None,
                float(spot) if spot else None)

    # ---------- entry scan ----------
    def run_entry_scan(self) -> None:
        if not in_trading_window(self.cfg):
            logging.info("Outside US trading window; skipping entries.")
            return

        portfolio_value = self.get_portfolio_value_usd()
        positions = self.current_open_positions()

        if not self.can_open_new_trades(portfolio_value, positions):
            return

        symbols = load_universe_symbols(self.cfg)
        all_candidates: List[Dict[str, Any]] = []

        for sym in symbols:
            cands = self.build_put_candidates_for_symbol(sym)
            if cands:
                all_candidates.extend(cands)

        all_candidates.sort(key=lambda x: (-x["roc_per_dte"], x["abs_delta"]))

        opened = 0
        for cand in all_candidates:
            if opened >= self.cfg.max_new_trades_per_run:
                break
            if not self.passes_position_limits(cand, portfolio_value, positions):
                continue

            oid = self.place_short_put(cand, qty_contracts=1)
            if not oid:
                continue

            pos = {
                "status": "OPEN",
                "orderId": oid,
                "opened_at": datetime.now(self.tz).isoformat(),
                "underlying": cand["underlying"],
                "expiry": cand["expiry"],
                "strike": cand["strike"],
                "qty": 1,
                "entry_mid": cand["mid"],
                "entry_delta": cand["delta"],
                "collateral": cand["collateral"],
                "premium_per_contract": cand["premium_per_contract"],
                "roc_per_dte": cand["roc_per_dte"],
            }
            if self.cfg.dry_run and not self.cfg.dry_run_save_simulated_positions:
                logging.info("[DRY-RUN] Not saving simulated OPEN position to JSON.")
            else:
                positions.append(pos)
                save_json(self.cfg.open_positions_file, positions)

            opened += 1
            logging.info(f"Entry scan complete. New positions opened: {opened}")

    # ---------- rolling ----------
    def try_roll_out_and_down(self, pos: Dict[str, Any], current_mid: float) -> bool:
        """
        Roll rule:
          - ITM with <=3 DTE
          - roll OUT (later expiry) and DOWN (lower strike)
          - net credit only (new premium > buyback cost)
          - never increase strike
        """
        sym = pos["underlying"]
        old_strike = float(pos["strike"])
        old_expiry = pos["expiry"]
        qty = int(pos["qty"])

        buyback_cost = current_mid * 100 * qty

        candidates = self.build_put_candidates_for_symbol(sym)
        roll_cands = [c for c in candidates if c["expiry"] > old_expiry and c["strike"] < old_strike]
        if not roll_cands:
            logging.warning(f"{sym}: no roll candidates (out+down) found.")
            return False

        newc = roll_cands[0]
        new_credit = newc["mid"] * 100 * qty

        if new_credit <= buyback_cost:
            logging.warning(f"{sym}: roll rejected (would be debit).")
            return False

        # Execute: close old then open new
        self.close_short_put(pos, limit_price=current_mid)
        self.place_short_put(newc, qty_contracts=qty)

        logging.info(
            f"ROLLED {sym}: {old_expiry} {old_strike}P -> {newc['expiry']} {newc['strike']}P "
            f"netCredit≈{new_credit - buyback_cost:.2f}"
        )
        return True

    # ---------- monitor & manage ----------
    def run_monitor(self) -> None:
        positions = self.current_open_positions()
        if not positions:
            logging.info("No saved positions.")
            return

        updated: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("status") != "OPEN":
                updated.append(pos)
                continue

            sym = pos["underlying"]
            expiry = pos["expiry"]
            strike = float(pos["strike"])
            dte = dte_from_yyyymmdd(expiry, self.tz)

            mid, bid, ask, delta, spot = self.get_option_market(sym, expiry, strike)
            if mid is None:
                updated.append(pos)
                continue

            entry_mid = float(pos["entry_mid"])
            qty = int(pos["qty"])
            collateral = float(pos["collateral"])

            # 75% profit capture: current premium <= 25% of entry premium, with >=2 DTE
            if dte >= self.cfg.min_dte_for_profit_take and mid <= (1.0 - self.cfg.profit_take_pct) * entry_mid:
                realized_pnl = (entry_mid - mid) * 100 * qty
                self.close_short_put(pos, limit_price=mid)
                add_daily_realized_pnl(self.tz, realized_pnl)

                pos["status"] = "CLOSED_TP"
                pos["closed_at"] = datetime.now(self.tz).isoformat()
                pos["exit_mid"] = mid
                pos["realized_pnl"] = realized_pnl
                updated.append(pos)
                continue

            # Delta alert
            if delta is not None and abs(delta) > self.cfg.delta_alert:
                logging.warning(f"DELTA ALERT {sym} {expiry} {strike}P: delta={delta:.3f}")

            # Hard loss: unrealized loss > 3% of collateral
            unreal_pnl = (entry_mid - mid) * 100 * qty
            unreal_loss = -min(unreal_pnl, 0.0)
            if unreal_loss > self.cfg.hard_loss_pct_of_collateral * collateral:
                self.close_short_put(pos, limit_price=mid)
                add_daily_realized_pnl(self.tz, unreal_pnl)

                pos["status"] = "CLOSED_HARD"
                pos["closed_at"] = datetime.now(self.tz).isoformat()
                pos["exit_mid"] = mid
                pos["realized_pnl"] = unreal_pnl
                updated.append(pos)
                continue

            # Roll: ITM with <=3 DTE
            # ITM for put: spot < strike
            if dte <= self.cfg.itm_roll_dte:
                if spot is None:
                    spot = self.get_stock_price(sym)
                if spot is not None and float(spot) < strike:
                    rolled = self.try_roll_out_and_down(pos, current_mid=mid)
                    if rolled:
                        pos["status"] = "ROLLED"
                        pos["rolled_at"] = datetime.now(self.tz).isoformat()
                        updated.append(pos)
                        continue

            # keep open + update last seen
            pos["last_mid"] = mid
            pos["last_delta"] = delta
            pos["last_spot"] = spot
            pos["last_checked"] = datetime.now(self.tz).isoformat()
            updated.append(pos)

        save_json(self.cfg.open_positions_file, updated)
        logging.info("Monitoring pass complete.")


def parse_args():
    p = argparse.ArgumentParser(description="IBKR CSP Bot")

    # Core runtime modes
    p.add_argument("--test-mode", action="store_true",
                   help="Ignore US trading window checks (useful for testing).")
    p.add_argument("--loop", dest="loop_mode", action="store_true",
                   help="Run continuously (scan + monitor) every interval seconds.")
    p.add_argument("--interval", type=int, default=None,
                   help="Loop interval seconds (default from config).")

    p.add_argument("--dry-run", action="store_true",
                   help="Do not place orders; print what would be done.")
    p.add_argument("--dry-run-save", action="store_true",
                   help="In dry-run, also save simulated positions to JSON (so monitor/roll/close logic can be tested).")

    # Convenience: override connection quickly
    p.add_argument("--host", type=str, default=None, help="IBKR API host (default from config).")
    p.add_argument("--port", type=int, default=None, help="IBKR API port (paper often 7497, live often 7496).")
    p.add_argument("--client-id", type=int, default=None, help="IBKR API clientId (default from config).")

    return p.parse_args()


def apply_cli_overrides(cfg: BotConfig, args) -> BotConfig:
    """
    Return a new BotConfig with CLI overrides applied.
    BotConfig is frozen, so we create a new one via dataclasses.replace.
    """
    from dataclasses import replace

    new_cfg = cfg

    if args.test_mode:
        new_cfg = replace(new_cfg, test_mode=True)

    if args.loop_mode:
        new_cfg = replace(new_cfg, loop_mode=True)

    if args.interval is not None:
        new_cfg = replace(new_cfg, loop_interval_seconds=int(args.interval))

    if args.dry_run:
        new_cfg = replace(new_cfg, dry_run=True)

    if args.dry_run_save:
        new_cfg = replace(new_cfg, dry_run_save_simulated_positions=True)

    if args.host is not None:
        new_cfg = replace(new_cfg, host=args.host)

    if args.port is not None:
        new_cfg = replace(new_cfg, port=int(args.port))

    if args.client_id is not None:
        new_cfg = replace(new_cfg, client_id=int(args.client_id))

    return new_cfg

def main(cfg: BotConfig):
    bot = IbkrCspBot(cfg)
    bot.connect()
    try:
        if cfg.loop_mode:
            logging.info(f"Loop mode ON. Interval={cfg.loop_interval_seconds}s. "
                         f"test_mode={cfg.test_mode} dry_run={cfg.dry_run}")
            while True:
                try:
                    bot.run_entry_scan()
                    bot.run_monitor()
                except Exception as e:
                    logging.exception(f"Loop iteration error: {e}")

                _time.sleep(cfg.loop_interval_seconds)
        else:
            bot.run_entry_scan()
            bot.run_monitor()
    finally:
        bot.disconnect()


if __name__ == "__main__":
    util.startLoop()

    args = parse_args()
    cfg = apply_cli_overrides(CFG, args)

    main(cfg)