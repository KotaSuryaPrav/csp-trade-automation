"""
Cash-Secured Put Bot for Zerodha (KiteConnect)

Implements the "December Cash-Secured Put Protocol" on Zerodha using F&O.

Key features:
- Short-dated, OTM, low-delta CSPs on liquid large caps
- Delta filter, ROC/DTE filter, position-sizing & portfolio limits
- Monitors & manages open positions (profit-take, roll, hard stop)
"""

# pip install kiteconnect nsetools

import os
import math
import json
import logging
from datetime import datetime, date, timedelta
from typing import List, Dict, Any

from kiteconnect import KiteConnect  # pip install kiteconnect
from nsetools import Nse


# -----------------------------
# CONFIGURATION
# -----------------------------

API_KEY = os.environ.get("KITE_API_KEY", "YOUR_API_KEY")
API_SECRET = os.environ.get("KITE_API_SECRET", "YOUR_API_SECRET")
ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")

# Your approximate portfolio size (₹)
PORTFOLIO_VALUE = 25_00_000  # example: 25 lakhs

def get_nifty100_universe():
    nse = Nse()
    # 'nifty next 50' etc; here we need 'nifty 100' index symbol
    # In nsetools it is usually "nifty100"
    index_data = nse.get_index_constituents("nifty100")
    # index_data is usually a dict {symbol: {...}, ...}
    return [symbol.upper() for symbol in index_data.keys()]

# Trade universe: liquid, large-cap underlying names you want to sell puts on
TRADE_UNIVERSE = get_nifty100_universe()

# Risk/strategy parameters (from your protocol)
PARAMS = {
    "min_dte": 5,
    "max_dte": 15,
    "delta_min": 0.05,
    "delta_target_max": 0.15,
    "delta_absolute_max": 0.25,
    "delta_alert": 0.30,
    "profit_take_pct": 0.75,  # 75% premium capture
    "min_dte_for_profit_take": 2,
    "itm_roll_dte": 3,
    "required_premium_per_dte": 0.01,   # 1% per DTE
    "position_min_pct": 0.02,           # 2% of portfolio
    "position_max_pct": 0.05,           # 5% of portfolio
    "max_notional_per_ticker_pct": 0.05,
    "max_csp_collateral_pct": 0.60,
    "max_margin_usage_pct": 0.75,
    "min_liquidity_buffer_pct": 0.25,
    "hard_loss_pct_of_collateral": 0.03,
    "daily_loss_stop_pct": 0.02,        # no new trades if hit
    "risk_free_rate": 0.06,             # annualized, for BS (approx)
    "default_iv": 0.25,                 # 25% IV assumption for delta
}

# State persistence files (very simple JSON-based state)
STATE_DIR = "state"
OPEN_POSITIONS_FILE = os.path.join(STATE_DIR, "csp_open_positions.json")
DAILY_PNL_FILE = os.path.join(STATE_DIR, "csp_daily_pnl.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# -----------------------------
# UTILS
# -----------------------------

def load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def today_str() -> str:
    return date.today().isoformat()


def time_in_market_hours() -> bool:
    """
    Trade only after open and avoid last 15 minutes.
    NSE: 09:15 - 15:30
    Use 09:20 - 15:15 for safety.
    """
    now = datetime.now()
    start = now.replace(hour=9, minute=20, second=0, microsecond=0)
    end = now.replace(hour=15, minute=15, second=0, microsecond=0)
    return start <= now <= end


def norm_cdf(x: float) -> float:
    """Standard normal CDF using error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def approx_put_delta(spot: float, strike: float, dte: int,
                     iv: float, r: float) -> float:
    """
    Approximate put delta using Black-Scholes.
    Zerodha doesn't provide greeks; we approximate with a configurable IV.
    """
    if dte <= 0 or spot <= 0 or iv <= 0:
        return -0.5  # fallback

    T = dte / 365.0
    sigma = iv
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    # Put delta = N(d1) - 1
    return norm_cdf(d1) - 1.0


def days_to_expiry(expiry_str: str) -> int:
    """
    Zerodha instrument expiry is date object or string depending on client;
    we handle both conservatively.
    """
    if isinstance(expiry_str, date):
        expiry_date = expiry_str
    else:
        # Try parsing ISO-like dates, adjust if needed
        expiry_date = datetime.strptime(str(expiry_str)[:10], "%Y-%m-%d").date()
    return (expiry_date - date.today()).days


# -----------------------------
# KITE SETUP
# -----------------------------

def get_kite_client() -> KiteConnect:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)
    return kite


# -----------------------------
# PORTFOLIO / RISK STATE
# -----------------------------

def get_current_csp_collateral(open_positions: List[Dict[str, Any]]) -> float:
    return sum(p["collateral"] for p in open_positions)


def get_notional_by_ticker(open_positions: List[Dict[str, Any]]) -> Dict[str, float]:
    res = {}
    for p in open_positions:
        t = p["underlying"]
        res[t] = res.get(t, 0.0) + p["collateral"]
    return res


def get_daily_realized_pnl() -> float:
    data = load_json(DAILY_PNL_FILE, {})
    return float(data.get(today_str(), 0.0))


def update_daily_realized_pnl(delta_pnl: float):
    data = load_json(DAILY_PNL_FILE, {})
    key = today_str()
    data[key] = float(data.get(key, 0.0) + delta_pnl)
    save_json(DAILY_PNL_FILE, data)


# -----------------------------
# CANDIDATE SCAN & SELECTION
# -----------------------------

def fetch_option_instruments_for_universe(kite: KiteConnect) -> List[Dict[str, Any]]:
    """
    Get all NFO options for underlyings in TRADE_UNIVERSE.
    We only care about PE (puts).
    """
    logging.info("Fetching NFO instruments…")
    all_nfo = kite.instruments("NFO")  # List[dict]

    instruments = []
    for inst in all_nfo:
        # Typical keys: instrument_type, segment, name, tradingsymbol, expiry, strike, lot_size, etc.
        if inst.get("segment") != "NFO-OPT":
            continue
        if inst.get("instrument_type") != "PE":
            continue
        if inst.get("name") not in TRADE_UNIVERSE:
            continue
        instruments.append(inst)

    logging.info(f"Filtered {len(instruments)} PE instruments for configured universe.")
    return instruments


def enrich_with_market_data(kite: KiteConnect, instruments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Gets LTP (and optionally depth) for options and underlyings.
    """
    # Build list of instrument tokens
    tokens = [inst["instrument_token"] for inst in instruments]
    # Kite.quote can accept instrument tokens as ints
    quote_data = kite.quote(tokens)

    # Underlying mapping for LTPs
    underlyings = sorted(set(inst["name"] for inst in instruments))
    underlying_symbols = [f"NSE:{u}" for u in underlyings]
    ul_quote_data = kite.quote(underlying_symbols)

    ul_ltp = {}
    for symbol in underlying_symbols:
        try:
            ul_ltp[symbol.split(":")[1]] = ul_quote_data[symbol]["last_price"]
        except KeyError:
            continue

    enriched = []
    for inst in instruments:
        token = inst["instrument_token"]
        q = quote_data.get(token) or quote_data.get(str(token))
        if not q:
            continue

        try:
            opt_ltp = q["last_price"]
        except KeyError:
            continue

        underlying = inst["name"]
        spot = ul_ltp.get(underlying)
        if not spot:
            continue

        # Mid-price approximation from depth
        bid = ask = None
        depth = q.get("depth", {})
        if depth:
            buy_side = depth.get("buy", [])
            sell_side = depth.get("sell", [])
            if buy_side:
                bid = buy_side[0].get("price")
            if sell_side:
                ask = sell_side[0].get("price")
        if bid and ask:
            mid = (bid + ask) / 2
        else:
            mid = opt_ltp

        inst_copy = dict(inst)
        inst_copy["option_ltp"] = opt_ltp
        inst_copy["option_mid"] = mid
        inst_copy["underlying_spot"] = spot
        enriched.append(inst_copy)

    logging.info(f"Enriched {len(enriched)} instruments with market data.")
    return enriched


def filter_and_rank_candidates(enriched_inst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Apply CSP protocol filters and sort by ROC/DTE desc, then delta asc.
    """
    candidates = []

    for inst in enriched_inst:
        strike = float(inst["strike"])
        spot = float(inst["underlying_spot"])
        ltp = float(inst["option_ltp"])
        mid = float(inst["option_mid"])
        lot_size = int(inst["lot_size"])
        dte = days_to_expiry(inst["expiry"])

        # DTE filter
        if dte < PARAMS["min_dte"] or dte > PARAMS["max_dte"]:
            continue

        # Strike must be OTM at entry (put OTM: strike < spot)
        if strike >= spot:
            continue

        # Collateral (cash-secured) = strike * lot_size
        collateral = strike * lot_size

        # Premium per contract and ROC metrics
        premium_per_contract = mid * lot_size
        premium_pct = premium_per_contract / collateral  # total premium / collateral

        # ROC/DTE requirement (1% per DTE)
        roc_per_dte = premium_pct / dte
        if roc_per_dte < PARAMS["required_premium_per_dte"]:
            continue

        # Approximate delta
        delta = approx_put_delta(
            spot=spot,
            strike=strike,
            dte=dte,
            iv=PARAMS["default_iv"],
            r=PARAMS["risk_free_rate"],
        )

        abs_delta = abs(delta)
        if abs_delta < PARAMS["delta_min"]:
            continue
        if abs_delta > PARAMS["delta_absolute_max"]:
            continue

        eligible = {
            "instrument_token": inst["instrument_token"],
            "tradingsymbol": inst["tradingsymbol"],
            "underlying": inst["name"],
            "expiry": inst["expiry"],
            "strike": strike,
            "spot": spot,
            "dte": dte,
            "lot_size": lot_size,
            "mid_price": mid,
            "delta": delta,
            "abs_delta": abs_delta,
            "collateral": collateral,
            "premium_per_contract": premium_per_contract,
            "premium_pct": premium_pct,
            "roc_per_dte": roc_per_dte,
        }
        candidates.append(eligible)

    # Rank: highest ROC/DTE, then lowest delta, then highest spot (as crude liquidity proxy)
    candidates.sort(key=lambda x: (-x["roc_per_dte"], x["abs_delta"], -x["spot"]))
    logging.info(f"{len(candidates)} candidates after filters.")
    return candidates


# -----------------------------
# POSITION SIZING & ENTRY
# -----------------------------

def can_open_new_positions(open_positions: List[Dict[str, Any]]) -> bool:
    daily_pnl = get_daily_realized_pnl()
    if daily_pnl <= -PARAMS["daily_loss_stop_pct"] * PORTFOLIO_VALUE:
        logging.warning("Daily loss limit exceeded. No new trades.")
        return False

    # margin usage & liquidity buffer checks
    current_collateral = get_current_csp_collateral(open_positions)
    margin_usage = current_collateral / PORTFOLIO_VALUE
    liquidity_buffer = 1.0 - margin_usage  # crude approximation

    if margin_usage > PARAMS["max_margin_usage_pct"]:
        logging.warning("Margin usage > allowed. No new trades.")
        return False

    if liquidity_buffer < PARAMS["min_liquidity_buffer_pct"]:
        logging.warning("Liquidity buffer < allowed. No new trades.")
        return False

    return True


def passes_position_limits(candidate, open_positions: List[Dict[str, Any]]) -> bool:
    position_value = candidate["collateral"]
    pct_of_portfolio = position_value / PORTFOLIO_VALUE

    if pct_of_portfolio < PARAMS["position_min_pct"] or pct_of_portfolio > PARAMS["position_max_pct"]:
        logging.info(f"Rejecting {candidate['tradingsymbol']}: position size {pct_of_portfolio:.2%} out of bounds.")
        return False

    # Notional per ticker
    notional_by_ticker = get_notional_by_ticker(open_positions)
    ticker_notional_existing = notional_by_ticker.get(candidate["underlying"], 0.0)
    if (ticker_notional_existing + position_value) > PARAMS["max_notional_per_ticker_pct"] * PORTFOLIO_VALUE:
        logging.info(f"Rejecting {candidate['tradingsymbol']}: notional per ticker limit exceeded.")
        return False

    # Total CSP collateral
    total_collateral = get_current_csp_collateral(open_positions)
    if (total_collateral + position_value) > PARAMS["max_csp_collateral_pct"] * PORTFOLIO_VALUE:
        logging.info(f"Rejecting {candidate['tradingsymbol']}: total CSP collateral > 60% portfolio.")
        return False

    # Margin usage and liquidity buffer (post trade)
    margin_usage_after = (total_collateral + position_value) / PORTFOLIO_VALUE
    liquidity_buffer_after = 1.0 - margin_usage_after

    if margin_usage_after > PARAMS["max_margin_usage_pct"]:
        logging.info(f"Rejecting {candidate['tradingsymbol']}: would exceed 75% margin usage.")
        return False

    if liquidity_buffer_after < PARAMS["min_liquidity_buffer_pct"]:
        logging.info(f"Rejecting {candidate['tradingsymbol']}: would drop liquidity buffer below 25%.")
        return False

    return True


def place_csp_order(kite: KiteConnect, candidate) -> Dict[str, Any]:
    """
    Place SELL PE order using NRML.
    """
    logging.info(f"Placing CSP SELL order: {candidate['tradingsymbol']} @ {candidate['mid_price']:.2f}")
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=candidate["tradingsymbol"],
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=candidate["lot_size"],
            product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=round(candidate["mid_price"], 1),  # adjust tick if needed
            validity=kite.VALIDITY_DAY,
        )
        logging.info(f"Order placed. ID={order_id}")
    except Exception as e:
        logging.error(f"Order placement failed: {e}")
        return {}

    # Store position metadata for monitoring
    open_positions = load_json(OPEN_POSITIONS_FILE, [])
    position = {
        "order_id": order_id,
        "tradingsymbol": candidate["tradingsymbol"],
        "instrument_token": candidate["instrument_token"],
        "underlying": candidate["underlying"],
        "expiry": str(candidate["expiry"]),
        "strike": candidate["strike"],
        "entry_price": candidate["mid_price"],
        "lot_size": candidate["lot_size"],
        "collateral": candidate["collateral"],
        "entry_delta": candidate["delta"],
        "roc_per_dte": candidate["roc_per_dte"],
        "premium_per_contract": candidate["premium_per_contract"],
        "opened_at": datetime.now().isoformat(),
        "status": "OPEN",
    }
    open_positions.append(position)
    save_json(OPEN_POSITIONS_FILE, open_positions)
    return position


def open_new_trades(kite: KiteConnect):
    """
    One-shot routine: scan, filter, and place at most N new CSPs.
    """
    open_positions = load_json(OPEN_POSITIONS_FILE, [])
    if not time_in_market_hours():
        logging.info("Outside allowed trading window. Skipping new entries.")
        return

    if not can_open_new_positions(open_positions):
        return

    instruments = fetch_option_instruments_for_universe(kite)
    enriched = enrich_with_market_data(kite, instruments)
    candidates = filter_and_rank_candidates(enriched)

    for cand in candidates:
        if not passes_position_limits(cand, open_positions):
            continue
        pos = place_csp_order(kite, cand)
        if pos:
            open_positions.append(pos)
            save_json(OPEN_POSITIONS_FILE, open_positions)
        # You can limit max new trades per run, e.g., 3
        if len(open_positions) >= 10:  # safety cap
            break


# -----------------------------
# MONITOR & RISK MANAGEMENT
# -----------------------------

def refresh_option_quote(kite: KiteConnect, tradingsymbol: str, underlying: str) -> Dict[str, float]:
    """
    Get current option price and underlying spot.
    """
    # NFO option by symbol
    opt_quote = kite.quote(f"NFO:{tradingsymbol}")
    ul_quote = kite.quote(f"NSE:{underlying}")

    opt_ltp = opt_quote[f"NFO:{tradingsymbol}"]["last_price"]
    ul_spot = ul_quote[f"NSE:{underlying}"]["last_price"]
    return {"opt_ltp": opt_ltp, "ul_spot": ul_spot}


def close_position(kite: KiteConnect, position: Dict[str, Any], price: float) -> float:
    """
    Close short put by buying back. Returns realized P&L.
    """
    logging.info(f"Closing CSP {position['tradingsymbol']} at {price:.2f}")
    qty = position["lot_size"]
    entry_price = position["entry_price"]

    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NFO,
            tradingsymbol=position["tradingsymbol"],
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=round(price, 1),
            validity=kite.VALIDITY_DAY,
        )
        logging.info(f"Close order placed. ID={order_id}")
    except Exception as e:
        logging.error(f"Failed to close position: {e}")
        return 0.0

    # Realized P&L (short premium collected – buyback)
    pnl_per_unit = entry_price - price
    realized_pnl = pnl_per_unit * qty
    update_daily_realized_pnl(realized_pnl)
    return realized_pnl


def roll_position(kite: KiteConnect, position: Dict[str, Any]) -> None:
    """
    Roll ITM position: close current, sell further expiry and lower strike for net credit.
    This is a simplified template; selection logic mirrors candidate filtering.
    """
    logging.info(f"Attempting roll for {position['tradingsymbol']}")

    # Close current leg at marketish price
    quotes = refresh_option_quote(kite, position["tradingsymbol"], position["underlying"])
    close_price = quotes["opt_ltp"]
    realized_pnl = close_position(kite, position, close_price)
    logging.info(f"Closed for roll. Realized P&L: {realized_pnl:.2f}")

    # Fetch new candidates on same underlying, with lower strike and longer expiry
    instruments = fetch_option_instruments_for_universe(kite)
    enriched = enrich_with_market_data(kite, instruments)
    candidates = filter_and_rank_candidates(enriched)

    ul = position["underlying"]
    orig_strike = position["strike"]
    orig_expiry = position["expiry"]

    roll_candidates = [
        c for c in candidates
        if c["underlying"] == ul
        and c["strike"] < orig_strike   # roll down
        and str(c["expiry"]) > str(orig_expiry)  # roll out
    ]

    if not roll_candidates:
        logging.warning("No suitable roll candidates found. Staying flat.")
        return

    new_leg = roll_candidates[0]
    # Ensure net credit (new premium > buyback cost)
    new_premium = new_leg["premium_per_contract"]
    old_premium_paid = close_price * position["lot_size"]
    net_credit = new_premium - old_premium_paid
    if net_credit <= 0:
        logging.warning("Roll would not yield net credit. Skipping roll per protocol.")
        return

    logging.info(f"Rolling into {new_leg['tradingsymbol']} with net credit {net_credit:.2f}")
    place_csp_order(kite, new_leg)


def monitor_open_positions(kite: KiteConnect):
    """
    Daily/periodic monitoring:
    - Profit-take at 75% capture (>=2 DTE)
    - Delta alert if >0.30
    - Roll ITM with <=3 DTE for net credit only
    - Hard risk: close/roll if loss >3% of collateral
    """
    open_positions = load_json(OPEN_POSITIONS_FILE, [])
    updated_positions = []

    for pos in open_positions:
        if pos.get("status") != "OPEN":
            updated_positions.append(pos)
            continue

        dte = days_to_expiry(pos["expiry"])
        quotes = refresh_option_quote(kite, pos["tradingsymbol"], pos["underlying"])
        opt_ltp = quotes["opt_ltp"]
        spot = quotes["ul_spot"]

        # Approx delta for alert
        delta = approx_put_delta(
            spot=spot,
            strike=pos["strike"],
            dte=max(dte, 1),
            iv=PARAMS["default_iv"],
            r=PARAMS["risk_free_rate"],
        )

        logging.info(f"{pos['tradingsymbol']} DTE={dte}, LTP={opt_ltp:.2f}, spot={spot:.2f}, delta={delta:.3f}")

        entry_price = pos["entry_price"]
        qty = pos["lot_size"]
        collateral = pos["collateral"]

        # Profit-take logic: current premium <= 25% of entry premium (75% captured)
        if dte >= PARAMS["min_dte_for_profit_take"] and opt_ltp <= (1 - PARAMS["profit_take_pct"]) * entry_price:
            logging.info("Profit target reached. Closing position.")
            pnl = close_position(kite, pos, opt_ltp)
            pos["status"] = "CLOSED"
            pos["closed_at"] = datetime.now().isoformat()
            pos["realized_pnl"] = pnl
            updated_positions.append(pos)
            continue

        # Delta alert
        if abs(delta) > PARAMS["delta_alert"]:
            logging.warning(f"Delta alert for {pos['tradingsymbol']}: {delta:.3f} (>0.30)")

        # ITM roll condition
        if dte <= PARAMS["itm_roll_dte"] and spot < pos["strike"]:
            logging.info("ITM with low DTE. Initiating roll.")
            roll_position(kite, pos)
            pos["status"] = "ROLLED"
            pos["rolled_at"] = datetime.now().isoformat()
            updated_positions.append(pos)
            continue

        # Hard risk: unrealized loss >3% of collateral
        current_value = opt_ltp * qty
        entry_value = entry_price * qty
        unrealized_pnl = entry_value - current_value  # short: positive if gain
        unrealized_loss = -min(unrealized_pnl, 0)     # positive loss only

        if unrealized_loss > PARAMS["hard_loss_pct_of_collateral"] * collateral:
            logging.warning("Hard loss threshold breached. Closing/Rolling per protocol.")
            pnl = close_position(kite, pos, opt_ltp)
            pos["status"] = "CLOSED_HARD_STOP"
            pos["closed_at"] = datetime.now().isoformat()
            pos["realized_pnl"] = pnl
            updated_positions.append(pos)
            continue

        # Still open
        updated_positions.append(pos)

    save_json(OPEN_POSITIONS_FILE, updated_positions)


# -----------------------------
# MAIN ENTRY POINT
# -----------------------------

if __name__ == "__main__":
    kite = get_kite_client()

    # 1) Open new trades (respecting all entry / sizing rules)
    open_new_trades(kite)

    # 2) Monitor and manage existing CSPs
    monitor_open_positions(kite)

    logging.info("CSP bot run completed.")
