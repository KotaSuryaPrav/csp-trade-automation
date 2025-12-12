"""
IBKR Cash-Secured Put Bot —  CSP Protocol

Requirements:
  pip install ib_insync pandas lxml

Runtime prerequisites:
  - Trader Workstation (TWS) or IB Gateway running
  - API enabled (socket) and port open (paper: 7497 commonly, live: 7496 commonly)

Notes:
  - Without market data subscriptions, IBKR may return delayed/snapshot-only quotes.
  - This bot supports testing via --dry-run and snapshot quotes.

Disclaimer:
  - Trading automation is risky. Use paper trading first.
"""

import argparse
import json
import logging
import math
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, Option, LimitOrder, util


# =========================
# CONFIG
# =========================

@dataclass(frozen=True)
class BotConfig:
    # --- Connection ---
    host: str = "127.0.0.1"
    port: int = 7497          # 7497 paper, 7496 live
    client_id: int = 7

    # --- Account / Portfolio ---
    portfolio_value_usd_fallback: float = 50_000.0

    # --- Universe ---
    universe: str = "SP500"   # only SP500 implemented here

    # SP500 retrieval (Wikipedia + cache)
    sp500_cache_file: str = "universe_cache_sp500.json"
    sp500_cache_ttl_seconds: int = 24 * 60 * 60  # refresh daily
    max_underlyings_to_scan: int = 10            # throttle load

    # Option chain throttles
    max_expiries_per_symbol: int = 1
    max_strikes_per_expiry: int = 5

    # --- Strategy parameters ---
    min_dte: int = 5
    max_dte: int = 15

    delta_min: float = 0.05
    delta_absolute_max: float = 0.25
    delta_alert: float = 0.30

    required_roc_per_dte: float = 0.01  # 1% per day (note: hard to meet often)

    position_min_pct: float = 0.02
    position_max_pct: float = 0.05
    max_notional_per_ticker_pct: float = 0.05
    max_total_csp_collateral_pct: float = 0.60
    max_margin_usage_pct: float = 0.75
    min_liquidity_buffer_pct: float = 0.25

    profit_take_pct: float = 0.75
    min_dte_for_profit_take: int = 2

    itm_roll_dte: int = 3
    hard_loss_pct_of_collateral: float = 0.03
    daily_loss_stop_pct: float = 0.02

    # --- US market hours (ET) ---
    exchange_tz: str = "America/New_York"
    trade_start: dtime = dtime(9, 35)
    trade_end: dtime = dtime(15, 45)

    # --- Runtime flags (can be overridden by CLI) ---
    test_mode: bool = False
    loop_mode: bool = False
    loop_interval_seconds: int = 60

    dry_run: bool = False
    dry_run_save_simulated_positions: bool = False

    # --- Pricing / Greeks ---
    risk_free_rate: float = 0.04
    default_iv: float = 0.30
    use_ibkr_greeks: bool = False  # if True, uses modelGreeks when available

    # --- Execution controls ---
    max_new_trades_per_run: int = 3
    require_bid_ask: bool = True
    max_spread_pct: float = 0.35

    # --- Market data controls ---
    use_snapshot_data: bool = True       # use snapshot quotes (good for testing)
    snapshot_sleep_seconds: float = 1.2  # allow time for snapshot
    allow_delayed_data: bool = True      # informational only; IBKR controls this

    # --- State persistence ---
    open_positions_file: str = "csp_open_positions.json"
    daily_pnl_file: str = "csp_daily_pnl.json"


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

def get_daily_realized_pnl(cfg: BotConfig, tz: ZoneInfo) -> float:
    data = load_json(cfg.daily_pnl_file, {})
    return float(data.get(today_key(tz), 0.0))

def add_daily_realized_pnl(cfg: BotConfig, tz: ZoneInfo, delta_pnl: float) -> None:
    data = load_json(cfg.daily_pnl_file, {})
    k = today_key(tz)
    data[k] = float(data.get(k, 0.0) + delta_pnl)
    save_json(cfg.daily_pnl_file, data)


# =========================
# HOURS / TIME
# =========================

def in_trading_window(cfg: BotConfig) -> bool:
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
# UNIVERSE (SP500 via Wikipedia + cache)
# =========================

def _ibkr_symbol_normalize(sym: str) -> str:
    s = sym.strip().upper().replace(".", " ")
    s = re.sub(r"\s+", " ", s)
    return s

def _read_cache(cache_path: Path, max_rows: int) -> List[str]:
    if not cache_path.exists():
        return []
    try:
        obj = json.loads(cache_path.read_text())
        symbols = obj.get("symbols", [])
        if not isinstance(symbols, list):
            return []
        syms = [_ibkr_symbol_normalize(s) for s in symbols]
        syms = [s for s in syms if s]
        return syms[:max_rows]
    except Exception:
        return []

def load_sp500_universe(cfg: BotConfig) -> List[str]:
    """
    Fetch S&P 500 constituents from Wikipedia using a browser-like User-Agent.
    Cache results locally and fall back to cache if fetch fails.
    """
    cache_path = Path(cfg.sp500_cache_file)

    # Use fresh cache if available
    if cache_path.exists():
        try:
            obj = json.loads(cache_path.read_text())
            fetched_at = float(obj.get("fetched_at", 0))
            if time.time() - fetched_at < cfg.sp500_cache_ttl_seconds:
                syms = _read_cache(cache_path, cfg.max_underlyings_to_scan)
                if syms:
                    logging.info(f"S&P500 universe loaded from cache: {len(syms)} symbols")
                    return syms
        except Exception:
            pass

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

        try:
            cache_path.write_text(json.dumps({"fetched_at": time.time(), "symbols": symbols}, indent=2))
        except Exception:
            pass

        logging.info(f"S&P500 universe loaded from Wikipedia (fetched): {len(symbols)} symbols")
        return symbols[: cfg.max_underlyings_to_scan]

    except Exception as e:
        logging.error(f"Failed to fetch S&P500 from Wikipedia: {e}. Falling back to cached universe if available.")
        cached = _read_cache(cache_path, cfg.max_underlyings_to_scan)
        if cached:
            logging.warning(f"Using STALE cached S&P500 universe: {len(cached)} symbols")
            return cached
        logging.warning("No cache available. Using minimal fallback universe.")
        return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"][: cfg.max_underlyings_to_scan]

def load_universe_symbols(cfg: BotConfig) -> List[str]:
    if cfg.universe == "SP500":
        return load_sp500_universe(cfg)
    raise ValueError(f"Unsupported universe: {cfg.universe}")


# =========================
# BOT
# =========================

class IbkrCspBot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.exchange_tz)
        self.ib = IB()

    def _install_ib_error_handler(self) -> None:
        """
        Downgrade noisy market data errors in testing.
        10089: needs subscription (delayed may still be available)
        300: follow-on “Can't find EId...” noise after failed reqs
        """
        def handler(reqId, errorCode, errorString, contract):
            if errorCode in (10089, 300):
                logging.warning(f"IBKR warning {errorCode} (reqId={reqId}): {errorString}")
                return
            logging.error(f"IBKR error {errorCode} (reqId={reqId}): {errorString}")

        self.ib.errorEvent += handler

    # ---------- connection ----------
    def connect(self) -> None:
        self.ib.RequestTimeout = 30
        self._install_ib_error_handler()
        self.ib.connect(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=30)
        logging.info(f"Connected to IBKR (host={self.cfg.host}, port={self.cfg.port}, clientId={self.cfg.client_id}).")

    def disconnect(self) -> None:
        self.ib.disconnect()
        logging.info("Disconnected.")

    # ---------- portfolio value (USD) ----------
    def get_portfolio_value_usd(self) -> float:
        try:
            rows = self.ib.accountSummary()
            for r in rows:
                if r.tag == "NetLiquidation" and r.currency == "USD":
                    v = float(r.value)
                    if v > 0:
                        logging.info(f"Portfolio NetLiquidation (USD) from IBKR: {v:.2f}")
                        return v
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
    def _snapshot_or_stream_ticker(self, contract):
        """
        Request snapshot (preferred for testing) or streaming ticker.
        """
        if self.cfg.use_snapshot_data:
            t = self.ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
            self.ib.sleep(self.cfg.snapshot_sleep_seconds)
            return t
        else:
            t = self.ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
            self.ib.sleep(0.8)
            return t

    def get_stock_price(self, sym: str) -> Optional[float]:
        stk = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(stk)

        t = self._snapshot_or_stream_ticker(stk)

        px = None
        if t.last and t.last > 0:
            px = t.last
        elif t.close and t.close > 0:
            px = t.close
        else:
            mp = t.marketPrice()
            if mp and mp > 0:
                px = mp

        if self.cfg.use_snapshot_data is False:
            self.ib.cancelMktData(stk)

        if not px or (isinstance(px, float) and (math.isnan(px) or px <= 0)):
            return None
        return float(px)

    def get_option_quote_mid(self, sym: str, expiry: str, strike: float) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        Returns (mid, bid, ask, delta) for the option.
        Uses snapshot when configured.
        """
        opt = Option(sym, expiry, float(strike), "P", "SMART", tradingClass=sym)
        self.ib.qualifyContracts(opt)

        t = self._snapshot_or_stream_ticker(opt)

        bid, ask, last, close = t.bid, t.ask, t.last, t.close

        mid = None
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        elif last and last > 0:
            mid = float(last)
        elif close and close > 0:
            mid = float(close)

        delta = None
        if self.cfg.use_ibkr_greeks and t.modelGreeks and t.modelGreeks.delta is not None:
            delta = float(t.modelGreeks.delta)

        if self.cfg.use_snapshot_data is False:
            self.ib.cancelMktData(opt)

        return (float(mid) if mid else None,
                float(bid) if bid else None,
                float(ask) if ask else None,
                float(delta) if delta is not None else None)

    # ---------- option chain discovery ----------
    def get_option_chain_params(self, sym: str):
        stk = Stock(sym, "SMART", "USD")
        self.ib.qualifyContracts(stk)
        params = self.ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
        if not params:
            raise RuntimeError(f"No option chain params for {sym}")
        return stk, params[0]

    # ---------- candidate builder ----------
    def build_put_candidates_for_symbol(self, sym: str) -> List[Dict[str, Any]]:
        spot = self.get_stock_price(sym)
        if not spot:
            return []

        try:
            _, p = self.get_option_chain_params(sym)
        except Exception as e:
            logging.info(f"{sym}: chain params unavailable: {e}")
            return []

        # expiries within window, throttled
        exps: List[Tuple[str, int]] = []
        for exp in sorted(p.expirations):
            dte = dte_from_yyyymmdd(exp, self.tz)
            if self.cfg.min_dte <= dte <= self.cfg.max_dte:
                exps.append((exp, dte))

        exps = exps[: self.cfg.max_expiries_per_symbol]
        if not exps:
            return []

        # strikes OTM, throttled closest-to-spot first
        strikes_all = sorted([float(k) for k in p.strikes if float(k) < spot])
        if not strikes_all:
            return []

        # take nearest OTM strikes (closest to spot)
        strikes = strikes_all[-self.cfg.max_strikes_per_expiry :]

        candidates: List[Dict[str, Any]] = []
        multiplier = 100

        for exp, dte in exps:
            for strike in strikes:
                mid, bid, ask, delta_live = self.get_option_quote_mid(sym, exp, strike)
                if mid is None:
                    continue

                # bid/ask constraint (optional)
                if self.cfg.require_bid_ask:
                    if not bid or not ask or bid <= 0 or ask <= 0:
                        continue
                    spread_pct = (ask - bid) / mid if mid > 0 else 999.0
                    if spread_pct > self.cfg.max_spread_pct:
                        continue

                # delta: IB if available else BS approx
                if delta_live is not None:
                    delta = delta_live
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
        realized_today = get_daily_realized_pnl(self.cfg, self.tz)
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

        if pct < self.cfg.position_min_pct or pct > self.cfg.position_max_pct:
            return False

        by_ticker = self.notional_by_ticker(positions)
        if (by_ticker.get(cand["underlying"], 0.0) + collateral) > self.cfg.max_notional_per_ticker_pct * portfolio_value:
            return False

        total_after = self.total_csp_collateral(positions) + collateral
        if total_after > self.cfg.max_total_csp_collateral_pct * portfolio_value:
            return False

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
            logging.info(f"[DRY-RUN] WOULD BUY to close {qty}x {sym} {expiry} {strike}P @ {limit_price}")
            return fake_id

        opt = Option(sym, expiry, strike, "P", "SMART", tradingClass=sym)
        self.ib.qualifyContracts(opt)
        order = LimitOrder("BUY", qty, limit_price)
        trade = self.ib.placeOrder(opt, order)
        self.ib.sleep(0.8)
        oid = str(trade.order.orderId) if trade and trade.order else None
        logging.info(f"BUY to close {sym} {expiry} {strike}P @ {limit_price} (orderId={oid})")
        return oid

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
        if not symbols:
            logging.warning("Universe empty; skipping entry scan.")
            return

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

    # ---------- rolling (kept minimal; uses candidate scan) ----------
    def try_roll_out_and_down(self, pos: Dict[str, Any], current_mid: float) -> bool:
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

            # get current option mid and delta
            mid, bid, ask, delta_live = self.get_option_quote_mid(sym, expiry, strike)
            if mid is None:
                updated.append(pos)
                continue

            spot = self.get_stock_price(sym)
            if spot is None:
                updated.append(pos)
                continue

            # delta calc fallback
            if delta_live is not None:
                delta = delta_live
            else:
                delta = approx_put_delta_bs(
                    spot=float(spot),
                    strike=float(strike),
                    dte=max(dte, 1),
                    iv=self.cfg.default_iv,
                    r=self.cfg.risk_free_rate,
                )

            entry_mid = float(pos["entry_mid"])
            qty = int(pos["qty"])
            collateral = float(pos["collateral"])

            # take profit
            if dte >= self.cfg.min_dte_for_profit_take and mid <= (1.0 - self.cfg.profit_take_pct) * entry_mid:
                realized_pnl = (entry_mid - mid) * 100 * qty
                self.close_short_put(pos, limit_price=mid)
                add_daily_realized_pnl(self.cfg, self.tz, realized_pnl)

                pos["status"] = "CLOSED_TP"
                pos["closed_at"] = datetime.now(self.tz).isoformat()
                pos["exit_mid"] = mid
                pos["realized_pnl"] = realized_pnl
                updated.append(pos)
                continue

            # delta alert
            if abs(delta) > self.cfg.delta_alert:
                logging.warning(f"DELTA ALERT {sym} {expiry} {strike}P: delta={delta:.3f}")

            # hard loss
            unreal_pnl = (entry_mid - mid) * 100 * qty
            unreal_loss = -min(unreal_pnl, 0.0)
            if unreal_loss > self.cfg.hard_loss_pct_of_collateral * collateral:
                self.close_short_put(pos, limit_price=mid)
                add_daily_realized_pnl(self.cfg, self.tz, unreal_pnl)

                pos["status"] = "CLOSED_HARD"
                pos["closed_at"] = datetime.now(self.tz).isoformat()
                pos["exit_mid"] = mid
                pos["realized_pnl"] = unreal_pnl
                updated.append(pos)
                continue

            # roll if ITM and near expiry
            if dte <= self.cfg.itm_roll_dte and float(spot) < strike:
                rolled = self.try_roll_out_and_down(pos, current_mid=mid)
                if rolled:
                    pos["status"] = "ROLLED"
                    pos["rolled_at"] = datetime.now(self.tz).isoformat()
                    updated.append(pos)
                    continue

            pos["last_mid"] = mid
            pos["last_delta"] = delta
            pos["last_spot"] = spot
            pos["last_checked"] = datetime.now(self.tz).isoformat()
            updated.append(pos)

        save_json(self.cfg.open_positions_file, updated)
        logging.info("Monitoring pass complete.")


# =========================
# CLI
# =========================

def parse_args():
    p = argparse.ArgumentParser(description="IBKR CSP Bot")

    p.add_argument("--test-mode", action="store_true", help="Ignore US trading window checks.")
    p.add_argument("--loop", dest="loop_mode", action="store_true", help="Run continuously.")
    p.add_argument("--interval", type=int, default=None, help="Loop interval seconds.")

    p.add_argument("--dry-run", action="store_true", help="Do not place orders; print actions only.")
    p.add_argument("--dry-run-save", action="store_true", help="In dry-run, also save simulated positions to JSON.")

    p.add_argument("--host", type=str, default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--client-id", type=int, default=None)

    # Testing switches for subscriptions/no-subscriptions
    p.add_argument("--snapshot", action="store_true", help="Use snapshot market data requests.")
    p.add_argument("--no-bidask", action="store_true", help="Do not require bid/ask (testing).")
    p.add_argument("--scan", type=int, default=None, help="Max underlyings to scan (override).")

    return p.parse_args()

def apply_cli_overrides(cfg: BotConfig, args) -> BotConfig:
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

    if args.snapshot:
        new_cfg = replace(new_cfg, use_snapshot_data=True)
    if args.no_bidask:
        new_cfg = replace(new_cfg, require_bid_ask=False)
    if args.scan is not None:
        new_cfg = replace(new_cfg, max_underlyings_to_scan=int(args.scan))

    return new_cfg


# =========================
# MAIN
# =========================

def main(cfg: BotConfig):
    bot = IbkrCspBot(cfg)
    bot.connect()
    try:
        if cfg.loop_mode:
            logging.info(
                f"Loop mode ON. Interval={cfg.loop_interval_seconds}s | "
                f"test_mode={cfg.test_mode} dry_run={cfg.dry_run} snapshot={cfg.use_snapshot_data} "
                f"require_bid_ask={cfg.require_bid_ask}"
            )
            while True:
                try:
                    bot.run_entry_scan()
                    bot.run_monitor()
                except Exception as e:
                    logging.exception(f"Loop iteration error: {e}")
                time.sleep(cfg.loop_interval_seconds)
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