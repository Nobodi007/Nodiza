"""
exchanges.py — the "exchange side" of Crypto Mall.

Two ideas live here:

1. Quote      = one exchange's price for one coin at one moment.
2. Exchange   = a common interface (an "adapter"). Every exchange, mock or
                real, must offer the same methods, so the rest of the app
                never needs to know how a specific exchange works.

V0.1 only needs get_price(). Later versions will add get_orderbook(),
get_balance(), place_order() and cancel_order() to the same interface.
"""
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import requests


@dataclass
class Quote:
    """One exchange's price for one trading pair."""
    exchange: str
    symbol: str
    bid: float       # highest price buyers pay  -> the price YOU get when you SELL
    ask: float       # lowest price sellers want -> the price YOU pay when you BUY
    fee_rate: float  # 0.0025 means 0.25%
    asks: list[tuple[float, float]] = field(default_factory=list)  # (price, coin qty)
    bids: list[tuple[float, float]] = field(default_factory=list)
    ts: float = field(default_factory=time.time)  # when the quote was fetched
    live: bool = False                            # True = real market data
    latency_ms: float = 0.0                       # time to fetch this quote

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        return self.spread / self.ask

    @property
    def effective_buy_price(self) -> float:
        """Price per coin you really pay after the fee is taken from your THB."""
        return self.ask / (1 - self.fee_rate)

    @property
    def effective_sell_price(self) -> float:
        """Price per coin you really receive after the fee."""
        return self.bid * (1 - self.fee_rate)

    def buy(self, thb: float) -> tuple[float, float]:
        """Spend THB across ask levels; returns (coins received, fee THB)."""
        fee = thb * self.fee_rate
        budget = thb - fee
        levels = self.asks or [(self.ask, float("inf"))]
        coins = 0.0
        for price, qty in levels:
            take = min(qty, budget / price)
            coins += take
            budget -= take * price
            if budget <= 1e-8:
                break
        if budget > 1e-6:
            raise ValueError("Insufficient ask-side liquidity in mock order book.")
        return coins, fee

    def sell(self, coins: float) -> tuple[float, float]:
        """Sell across bid levels; returns (net THB, fee THB)."""
        remaining = coins
        gross = 0.0
        levels = self.bids or [(self.bid, float("inf"))]
        for price, qty in levels:
            take = min(qty, remaining)
            gross += take * price
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > 1e-8:
            raise ValueError("Insufficient bid-side liquidity in mock order book.")
        fee = gross * self.fee_rate
        return gross - fee, fee


class Exchange(ABC):
    """The common interface every exchange adapter must follow."""
    name: str

    @abstractmethod
    def get_price(self, symbol: str) -> Quote:
        ...


class MockExchange(Exchange):
    """A fake exchange with its own personality (fee, spread, price bias)."""

    # Rough "fair" prices in THB. Mock data only, not real market prices.
    FAIR_PRICE = {"BTC/THB": 3_100_000, "ETH/THB": 110_000}

    def __init__(self, name: str, fee_rate: float, spread_pct: float, price_bias: float):
        self.name = name
        self.fee_rate = fee_rate
        self.spread_pct = spread_pct    # total gap between bid and ask
        self.price_bias = price_bias    # +0.002 = prices 0.2% above fair

    def get_price(self, symbol: str) -> Quote:
        fair = self.FAIR_PRICE[symbol]
        noise = random.uniform(-0.0005, 0.0005)       # small random wobble
        mid = fair * (1 + self.price_bias + noise)
        half = mid * self.spread_pct / 2
        bid, ask = mid - half, mid + half
        # Synthetic depth: 5 price levels, with finite liquidity at each level.
        base_qty = 0.25 if symbol.startswith("BTC") else 8.0
        bids = [(bid * (1 - 0.0004 * i), base_qty * (1 + i * 0.5)) for i in range(5)]
        asks = [(ask * (1 + 0.0004 * i), base_qty * (1 + i * 0.5)) for i in range(5)]
        return Quote(self.name, symbol, bid=bid, ask=ask, fee_rate=self.fee_rate, asks=asks, bids=bids)


def build_mock_exchanges() -> list[Exchange]:
    """Three fake exchanges, each better at something different."""
    return [
        MockExchange("Exchange A", fee_rate=0.0025, spread_pct=0.0010, price_bias=0.0),
        MockExchange("Exchange B", fee_rate=0.0015, spread_pct=0.0020, price_bias=-0.0015),
        MockExchange("Exchange C", fee_rate=0.0030, spread_pct=0.0005, price_bias=0.0025),
    ]


ENV_PATHS = [Path(__file__).with_name(".env"), Path.cwd() / ".env"]


def _load_env() -> None:
    """Read KEY=value lines from a .env file (next to this script, or the folder you ran
    streamlit from) into os.environ. Real environment variables win. Never commit .env."""
    for path in ENV_PATHS:
        if not path.is_file():
            continue
        # utf-8-sig: Windows Notepad often saves a hidden BOM that would break the first key name
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


_load_env()


def _streamlit_secret(name: str) -> str:
    """Streamlit Community Cloud keeps secrets in st.secrets (Settings > Secrets), not in .env."""
    try:
        import streamlit as st
        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def _secret(name: str) -> str:
    """Env var / .env first, then Streamlit Cloud secrets. Surrounding quotes and spaces are stripped."""
    val = os.environ.get(name, "").strip() or _streamlit_secret(name)
    return val.strip("\"' \r\n\t")


_CACHE: dict[tuple[str, str], tuple[float, "Quote"]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = 2.0  # seconds: stops rapid "Refresh" clicks from hammering public APIs


class RealExchange(Exchange):
    """Public order-book adapter. No API key, no trading: market data only.

    Subclasses set: name, fee_rate, and implement _params()/_parse().
    If the request fails, we fall back to a MockExchange so the app keeps working.
    """
    fee_rate: float = 0.0025
    timeout = 4
    is_live = True

    def __init__(self, fallback: "MockExchange | None" = None):
        self.fallback = fallback
        self.last_error: str | None = None

    def _params(self, symbol: str) -> tuple[str, dict]:
        raise NotImplementedError

    def _parse(self, data: dict) -> tuple[list, list]:
        raise NotImplementedError

    def _headers(self) -> dict:
        return {}

    def configured(self) -> bool:
        """False = missing key/URL; build_exchanges() then leaves this venue out instead of faking prices."""
        return True

    @staticmethod
    def _levels(raw) -> list[tuple[float, float]]:
        out = []
        for row in raw:
            if isinstance(row, dict):                      # {"price":..,"amount":..}
                price = row.get("price") or row.get("rate")
                qty = row.get("amount") or row.get("volume") or row.get("qty")
            else:                                          # [price, qty]
                price, qty = row[0], row[1]
            out.append((float(price), float(qty)))
        return out

    def get_price(self, symbol: str) -> Quote:
        key = (self.name, symbol)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            self.last_error = None
            return hit[1]
        url = "?"
        try:
            url, params = self._params(symbol)
            t0 = time.perf_counter()
            r = requests.get(url, params=params, headers=self._headers(), timeout=self.timeout)
            latency_ms = (time.perf_counter() - t0) * 1000
            if not r.ok:                       # show the exchange's own error message
                raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
            raw_asks, raw_bids = self._parse(r.json())
            asks = sorted(self._levels(raw_asks), key=lambda x: x[0])
            bids = sorted(self._levels(raw_bids), key=lambda x: -x[0])
            if not asks or not bids:
                raise ValueError("empty order book")
            self.last_error = None
            quote = Quote(self.name, symbol, bid=bids[0][0], ask=asks[0][0],
                         fee_rate=self.fee_rate, asks=asks, bids=bids, live=True,
                         latency_ms=latency_ms)
            with _CACHE_LOCK:
                _CACHE[key] = (time.time(), quote)
            return quote
        except Exception as e:                             # network, JSON, parsing...
            self.last_error = f"{type(e).__name__}: {e} [GET {url}]{getattr(self, 'debug_note', '')}"
            if self.fallback is None:
                raise
            q = self.fallback.get_price(symbol)
            q.exchange = f"{self.name} (mock fallback)"
            return q


class BitkubExchange(RealExchange):
    name = "Bitkub"
    fee_rate = 0.0025

    def _params(self, symbol):
        base = symbol.split("/")[0].lower()
        return "https://api.bitkub.com/api/v3/market/depth", {"sym": f"{base}_thb", "lmt": 20}

    def _parse(self, data):
        res = data.get("result", data)
        return res["asks"], res["bids"]


class BinanceTHExchange(RealExchange):
    name = "Binance TH"
    fee_rate = 0.0010

    def _params(self, symbol):
        base = symbol.split("/")[0].upper()
        return "https://api.binance.th/api/v1/depth", {"symbol": f"{base}THB", "limit": 20}

    def _parse(self, data):
        return data["asks"], data["bids"]


class MaxbitExchange(RealExchange):
    """Maxbit gateway (Binance-style). Its depth endpoint needs an API-key header, so this adapter
    needs a READ-ONLY Maxbit key in MAXBIT_API_KEY (.env locally, Settings > Secrets on Streamlit Cloud).
    Only the key is sent: no secret, no signature, no trading. Endpoint path is UNVERIFIED."""
    name = "Maxbit"
    fee_rate = 0.0025          # placeholder: check Maxbit's real fee
    KEY_VAR = "MAXBIT_API_KEY"
    BASE = "https://endpoint-gateway.maxbit.com"
    PATH = "/api/v1/depth"

    def configured(self):
        return bool(_secret(self.KEY_VAR))

    def _headers(self):
        key = _secret(self.KEY_VAR)
        if not key:
            raise RuntimeError(f"{self.KEY_VAR} is not set (local: .env file; Streamlit Cloud: app Settings > Secrets)")
        self.debug_note = f" [key length sent: {len(key)}]"   # length only, never the key itself
        return {"X-MBX-APIKEY": key}

    def _params(self, symbol):
        base = symbol.split("/")[0].upper()
        return self.BASE + self.PATH, {"symbol": f"{base}THB", "limit": 20}

    def _parse(self, data):
        return data["asks"], data["bids"]


class InnovestXExchange(MaxbitExchange):
    """InnovestX. I could NOT find its public API docs, so nothing is guessed here:
    set INNOVESTX_API_KEY (read-only key) and INNOVESTX_BASE_URL (its API host, from InnovestX's docs).
    Optional INNOVESTX_DEPTH_PATH (default /api/v1/depth). Assumes a Binance-style depth response."""
    name = "InnovestX"
    fee_rate = 0.0025          # placeholder: check InnovestX's real fee
    KEY_VAR = "INNOVESTX_API_KEY"

    def configured(self):
        return bool(_secret(self.KEY_VAR) and _secret("INNOVESTX_BASE_URL"))

    def _headers(self):
        """Header name via INNOVESTX_HEADER (default X-MBX-APIKEY).
        Use e.g. INNOVESTX_HEADER=Authorization together with INNOVESTX_AUTH_PREFIX=Bearer."""
        key = _secret(self.KEY_VAR)
        if not key:
            raise RuntimeError(f"{self.KEY_VAR} is not set")
        self.debug_note = f" [key length sent: {len(key)}]"
        name = _secret("INNOVESTX_HEADER") or "X-MBX-APIKEY"
        prefix = _secret("INNOVESTX_AUTH_PREFIX")
        return {name: f"{prefix} {key}" if prefix else key}

    def _params(self, symbol):
        """Symbol format via INNOVESTX_SYMBOL_FMT, default {base}THB. Placeholders: {base} {quote}
        (BASE/QUOTE upper case; use {base_l} {quote_l} for lower case). Example: {base}_{quote}"""
        base, quote = symbol.split("/")
        fmt = _secret("INNOVESTX_SYMBOL_FMT") or "{base}{quote}"
        sym = fmt.format(base=base.upper(), quote=quote.upper(), base_l=base.lower(), quote_l=quote.lower())
        base_url = _secret("INNOVESTX_BASE_URL").rstrip("/")
        path = _secret("INNOVESTX_DEPTH_PATH") or "/api/v1/depth"
        return base_url + path, {"symbol": sym, "limit": 20}


def build_exchanges(mode: str = "Mock") -> list[Exchange]:
    """mode: 'Mock' | 'Live data' (real prices, paper money).

    In Live data mode a venue without credentials is left out, never replaced by fake prices.
    (A configured venue that errors at runtime still falls back to a labelled mock quote.)"""
    if mode == "Mock":
        return build_mock_exchanges()
    live = [
        BitkubExchange(fallback=MockExchange("Bitkub", 0.0025, 0.0010, 0.0)),
        BinanceTHExchange(fallback=MockExchange("Binance TH", 0.0010, 0.0010, -0.0010)),
        MaxbitExchange(fallback=MockExchange("Maxbit", 0.0025, 0.0010, 0.0010)),
        InnovestXExchange(fallback=MockExchange("InnovestX", 0.0025, 0.0010, 0.0005)),
    ]
    return [ex for ex in live if ex.configured()]


def fetch_quotes(exchanges: list[Exchange], symbol: str) -> list[Quote]:
    """Ask every exchange for its quote at the same time. Order is preserved."""
    if len(exchanges) <= 1:
        return [ex.get_price(symbol) for ex in exchanges]
    with ThreadPoolExecutor(max_workers=len(exchanges)) as pool:
        return list(pool.map(lambda ex: ex.get_price(symbol), exchanges))
