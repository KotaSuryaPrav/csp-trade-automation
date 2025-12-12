"""
Generic Cash-Secured Put Bot

Supports:
- Broker: Zerodha OR Interactive Brokers (IBKR)
- Universe: NIFTY 100 OR S&P 500

You can extend the CSP logic in the marked sections.
"""

import os
import math
import json
import logging
from datetime import datetime, date
from typing import List, Dict, Any

# === CONFIG FLAGS ====================================================

BROKER = os.environ.get("BROKER", "ZERODHA")   # "ZERODHA" or "IBKR"
UNIVERSE = os.environ.get("UNIVERSE", "NIFTY100")  # "NIFTY100" or "SP500"

IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "1"))

PORTFOLIO_VALUE = 25_00_000  # 25 lakh; change if you switch to USD acct later

# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ---------------------------------------------------------------------
# UNIVERSE PROVIDERS
# ---------------------------------------------------------------------

def get_universe_nifty100() -> List[str]:
    """
    Returns NIFTY 100 symbols suitable for NSE / NFO options.
    Uses nsetools.
    """
    from nsetools import Nse  # local import so IBKR+SP500 can run without it
    nse = Nse()
    index_data = nse.get_index_constituents("nifty100")
    symbols = [symbol.upper() for symbol in index_data.keys()]
    logging.info(f"NIFTY100 universe loaded: {len(symbols)} symbols")
    return symbols


def get_universe_sp500() -> List[str]:
    """
    Returns S&P 500 symbols.
    Implementation options:
      - Hard-coded CSV/text file
      - Download from a reliable source and cache locally
    For now, we show a minimal placeholder list; replace with full list or file read.
    """
    # TODO: replace with full S&P 500 list or file read
    symbols = [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META",
        "NVDA", "BRK.B", "UNH", "JNJ", "XOM",
        # ...
    ]
    logging.info(f"S&P 500 universe loaded (sample): {len(symbols)} symbols")
    return symbols


def load_trade_universe() -> List[str]:
    if UNIVERSE == "NIFTY100":
        return get_universe_nifty100()
    elif UNIVERSE == "SP500":
        return get_universe_sp500()
    else:
        raise ValueError(f"Unsupported UNIVERSE: {UNIVERSE}")


TRADE_UNIVERSE = load_trade_universe()

# ---------------------------------------------------------------------
# BROKER ADAPTER INTERFACE
# ---------------------------------------------------------------------

class BrokerClient:
    """
    Abstract broker client that CSP logic will use.
    You implement this for Zerodha and IBKR separately.
    """

    def get_option_instruments(self) -> List[Dict[str, Any]]:
        """
        Return list of option instruments in your universe.
        Each instrument must minimally include:
          - 'symbol'         : option symbol/ticker
          - 'underlying'     : underlying symbol (stock)
          - 'expiry'         : expiry date (datetime.date or ISO string)
          - 'strike'         : float
          - 'right'          : "P" for put, "C" for call
          - 'currency'       : "INR" or "USD"
          - 'multiplier'     : contract multiplier/lot size
        """
        raise NotImplementedError

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Return quote data dict: { symbol: { 'last_price': float, 'bid': float, 'ask': float } }
        """
        raise NotImplementedError

    def place_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        """
        Place SELL order (short put).
        Return broker order ID.
        """
        raise NotImplementedError

    def close_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        """
        Close existing short put (BUY).
        Return broker order ID.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------
# ZERODHA IMPLEMENTATION
# ---------------------------------------------------------------------

class ZerodhaClient(BrokerClient):
    def __init__(self):
        from kiteconnect import KiteConnect
        api_key = os.environ.get("KITE_API_KEY", "YOUR_API_KEY")
        access_token = os.environ.get("KITE_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

    def get_option_instruments(self) -> List[Dict[str, Any]]:
        logging.info("Fetching NFO options from Zerodha…")
        all_nfo = self.kite.instruments("NFO")  # Zerodha instrument dump

        inst_list = []
        for inst in all_nfo:
            if inst.get("segment") != "NFO-OPT":
                continue
            if inst.get("instrument_type") != "PE":
                continue
            underlying = inst.get("name")
            if underlying not in TRADE_UNIVERSE:
                continue

            inst_list.append({
                "symbol": inst["tradingsymbol"],
                "underlying": underlying,
                "expiry": inst["expiry"],
                "strike": float(inst["strike"]),
                "right": "P",
                "currency": "INR",
                "multiplier": int(inst["lot_size"]),
                "instrument_token": inst["instrument_token"],
            })

        logging.info(f"Zerodha: {len(inst_list)} PE instruments in universe")
        return inst_list

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        `symbols` for options: ["NFO:RELIANCE24JAN3000PE", ...]
        For underlyings: ["NSE:RELIANCE", ...]
        """
        if not symbols:
            return {}
        q = self.kite.quote(symbols)
        quotes = {}
        for full_sym, data in q.items():
            last_price = data.get("last_price")
            depth = data.get("depth", {})
            bid = ask = None
            if depth:
                b = depth.get("buy", [])
                s = depth.get("sell", [])
                if b:
                    bid = b[0].get("price")
                if s:
                    ask = s[0].get("price")
            quotes[full_sym] = {
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
            }
        return quotes

    def place_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        logging.info(f"Zerodha SELL {symbol}, qty={quantity}, price={limit_price}")
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            product=self.kite.PRODUCT_NRML,
            order_type=self.kite.ORDER_TYPE_LIMIT,
            price=limit_price,
            validity=self.kite.VALIDITY_DAY,
        )
        return order_id

    def close_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        logging.info(f"Zerodha BUY to close {symbol}, qty={quantity}, price={limit_price}")
        order_id = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR,
            exchange=self.kite.EXCHANGE_NFO,
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=quantity,
            product=self.kite.PRODUCT_NRML,
            order_type=self.kite.ORDER_TYPE_LIMIT,
            price=limit_price,
            validity=self.kite.VALIDITY_DAY,
        )
        return order_id


# ---------------------------------------------------------------------
# IBKR IMPLEMENTATION (SKELETON)
# ---------------------------------------------------------------------

class IbkrClient(BrokerClient):
    """
    Skeleton using ib_insync / IB API. You’ll need to fill in the actual details
    once your IBKR account + TWS / Gateway is ready.
    """
    def __init__(self):
        # Example: using ib_insync (pip install ib_insync)
        from ib_insync import IB
        self.ib = IB()
        # Adjust host/port/clientId for your TWS/Gateway setup
        self.host = IB_HOST
        self.port = IB_PORT
        self.client_id = IB_CLIENT_ID
        self.ib.connect(self.host, self.port, clientId=self.client_id)

    def get_option_instruments(self) -> List[Dict[str, Any]]:
        """
        For IBKR, you'll likely:
          - loop through TRADE_UNIVERSE (S&P 500 stocks),
          - use IB's reqSecDefOptParams / OptionChain to get puts,
          - filter by expiry/strike.
        We return a placeholder here; you can extend.
        """
        inst_list = []
        # TODO: implement full option-chain retrieval via IBKR
        logging.info("IBKR get_option_instruments: not fully implemented (skeleton).")
        return inst_list

    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        `symbols` will be your IBKR contract identifiers; in a real implementation,
        you’d map them to Contract objects and call reqMktData.
        """
        quotes = {}
        # TODO: implement using ib.reqMktData
        logging.info("IBKR get_quotes: not fully implemented (skeleton).")
        return quotes

    def place_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        # TODO: Map `symbol` → IBKR Option Contract, place SELL order.
        logging.info(f"IBKR SELL {symbol} (skeleton)")
        order_id = "IBKR_ORDER_ID_PLACEHOLDER"
        return order_id

    def close_short_put(self, symbol: str, quantity: int, limit_price: float) -> str:
        logging.info(f"IBKR BUY to close {symbol} (skeleton)")
        order_id = "IBKR_ORDER_ID_PLACEHOLDER"
        return order_id


# ---------------------------------------------------------------------
# CSP LOGIC (DELTA / ROC / POSITION SIZING – SHORTHAND VERSION)
# ---------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def approx_put_delta(spot: float, strike: float, dte: int,
                     iv: float = 0.25, r: float = 0.06) -> float:
    if dte <= 0 or spot <= 0 or iv <= 0:
        return -0.5
    T = dte / 365.0
    sigma = iv
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1) - 1.0  # put delta


def days_to_expiry(expiry) -> int:
    if isinstance(expiry, date):
        expiry_date = expiry
    else:
        expiry_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
    return (expiry_date - date.today()).days


def select_csp_candidates(broker: BrokerClient) -> List[Dict[str, Any]]:
    """
    Generic CSP candidate selection, independent of broker/universe.
    You can paste the full filtering logic from the earlier script here.
    """
    options = broker.get_option_instruments()

    # Example minimal filters:
    candidates = []
    for inst in options:
        if inst["right"] != "P":
            continue
        dte = days_to_expiry(inst["expiry"])
        if dte < 5 or dte > 15:
            continue

        # Build symbols for quoting
        # Zerodha: "NFO:SYMBOL", underlying: "NSE:UNDERLYING"
        # IBKR: you will adjust later; here we just use inst["symbol"]
        if isinstance(broker, ZerodhaClient):
            opt_symbol = f"NFO:{inst['symbol']}"
            ul_symbol = f"NSE:{inst['underlying']}"
        else:
            opt_symbol = inst["symbol"]
            ul_symbol = inst["underlying"]

        quotes = broker.get_quotes([opt_symbol, ul_symbol])
        opt_q = quotes.get(opt_symbol, {})
        ul_q = quotes.get(ul_symbol, {})

        opt_ltp = opt_q.get("last_price")
        spot = ul_q.get("last_price")

        if not opt_ltp or not spot:
            continue

        # Must be OTM put: strike < spot
        if inst["strike"] >= spot:
            continue

        # Approx delta
        delta = approx_put_delta(spot, inst["strike"], dte)
        abs_delta = abs(delta)
        if abs_delta < 0.05 or abs_delta > 0.25:
            continue

        # Rough ROC calculation
        collateral = inst["strike"] * inst["multiplier"]
        premium_per_contract = opt_ltp * inst["multiplier"]
        premium_pct = premium_per_contract / collateral
        roc_per_dte = premium_pct / dte
        if roc_per_dte < 0.01:  # 1% per DTE
            continue

        mid = opt_q.get("last_price")  # or use avg(bid, ask)

        candidates.append({
            "symbol": inst["symbol"],
            "underlying": inst["underlying"],
            "expiry": inst["expiry"],
            "strike": inst["strike"],
            "dte": dte,
            "delta": delta,
            "collateral": collateral,
            "multiplier": inst["multiplier"],
            "mid_price": mid,
            "premium_per_contract": premium_per_contract,
            "roc_per_dte": roc_per_dte,
        })

    candidates.sort(key=lambda x: (-x["roc_per_dte"], abs(x["delta"])))
    logging.info(f"{len(candidates)} CSP candidates after filtering")
    return candidates


def place_csp_trades(broker: BrokerClient):
    """
    Very simple entry: pick top few candidates, size them, and place orders.
    You can transplant your full position sizing logic here.
    """
    candidates = select_csp_candidates(broker)
    max_trades = 3

    for i, c in enumerate(candidates[:max_trades]):
        # Basic position sizing: 2–5% of portfolio by collateral
        pct = c["collateral"] / PORTFOLIO_VALUE
        if pct < 0.02 or pct > 0.05:
            logging.info(f"Skipping {c['symbol']} – position {pct:.2%} out of bounds")
            continue

        qty = c["multiplier"]  # 1 contract
        price = round(c["mid_price"], 2)
        order_id = broker.place_short_put(c["symbol"], qty, price)
        logging.info(f"Placed CSP on {c['symbol']} – order_id={order_id}")


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def get_broker_client() -> BrokerClient:
    if BROKER == "ZERODHA":
        logging.info("Using Zerodha broker client")
        return ZerodhaClient()
    elif BROKER == "IBKR":
        logging.info("Using IBKR broker client")
        return IbkrClient()
    else:
        raise ValueError(f"Unsupported BROKER: {BROKER}")


if __name__ == "__main__":
    broker = get_broker_client()
    place_csp_trades(broker)
    logging.info("CSP pass completed.")
