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
import hashlib
import hmac
import json
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
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
    fee_source: str = "mock"   # where fee_rate came from: "live" | "env" | "default" | "placeholder" | "mock"
    fee_note: str = ""         # human-readable detail about the fee (shown in the app)
    error: str = ""            # why a live venue fell back to a mock quote (empty otherwise)

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
_FEE_CACHE: dict[tuple[str, str], tuple] = {}   # (venue, symbol) -> (ts, rate | None, note, ttl)
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

    fee_source = "default"   # "default" | "env" | "live": where the fee used last came from
    fee_note = ""

    def _fee_rate(self, symbol: str) -> float:
        """Fee used for this quote. <NAME>_FEE_RATE (e.g. BITKUB_FEE_RATE=0.0025, BINANCE_TH_FEE_RATE,
        MAXBIT_FEE_RATE) overrides the built-in value, as a fraction (0.0025 = 0.25%), so you can put your
        own account's tier fee in .env / Secrets without editing code. Adapters that can look up their
        real fee (InnovestX) override this method."""
        var = self.name.upper().replace(" ", "_") + "_FEE_RATE"
        raw = _secret(var)
        if raw:
            try:
                rate = float(raw)
                if 0 <= rate <= 0.02:
                    self.fee_source, self.fee_note = "env", f"{var}={raw}"
                    return rate
                self.fee_note = f"{var}={raw} ignored (must be a fraction between 0 and 0.02, e.g. 0.0025)"
            except ValueError:
                self.fee_note = f"{var}={raw!r} ignored (not a number)"
        else:
            self.fee_note = ""
        self.fee_source = "default"
        return self.fee_rate

    def _fetch(self, url: str, params: dict):
        """Default: plain GET. Adapters needing POST/signing override this."""
        return requests.get(url, params=params, headers=self._headers(), timeout=self.timeout)

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
            r = self._fetch(url, params)
            latency_ms = (time.perf_counter() - t0) * 1000
            if not r.ok:                       # show the exchange's own error message
                raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
            raw_asks, raw_bids = self._parse(r.json())
            asks = sorted(self._levels(raw_asks), key=lambda x: x[0])
            bids = sorted(self._levels(raw_bids), key=lambda x: -x[0])
            if not asks or not bids:
                raise ValueError("empty order book")
            self.last_error = None
            fee = self._fee_rate(symbol)          # sets self.fee_source / self.fee_note
            quote = Quote(self.name, symbol, bid=bids[0][0], ask=asks[0][0],
                         fee_rate=fee, asks=asks, bids=bids, live=True,
                         latency_ms=latency_ms, fee_source=self.fee_source, fee_note=self.fee_note)
            with _CACHE_LOCK:
                _CACHE[key] = (time.time(), quote)
            return quote
        except Exception as e:                             # network, JSON, parsing...
            self.last_error = f"{type(e).__name__}: {e} [{url}]{getattr(self, 'debug_note', '')}"
            if self.fallback is None:
                raise
            q = self.fallback.get_price(symbol)
            q.exchange = f"{self.name} (mock fallback)"
            q.fee_source, q.fee_note, q.error = "mock", "simulated fallback fee", self.last_error or ""
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


class InnovestXExchange(RealExchange):
    """InnovestX digital-asset Open API (https://api-docs.innovestxonline.com/).

    POST /api/v1/digital-asset/orderbook/lvl2 with JSON {"symbol": "BTCTHB", "depth": N}.
    Auth: X-INVX-APIKEY + X-INVX-SIGNATURE = HMAC-SHA256(secret, apikey+VERB+host+path+query+
    content-type+request-uid+timestamp+body). Needs a key with Read (or Trading) permission, and
    the machine's IP must be on the key's whitelist (error 4003 otherwise).

    Env / Secrets: INNOVESTX_API_KEY, INNOVESTX_API_SECRET.
    Optional: INNOVESTX_BASE_URL (default https://api.innovestxonline.com), INNOVESTX_DEPTH_PATH.
    The secret only signs the request; it is never sent."""
    name = "InnovestX"
    fee_rate = 0.0025          # placeholder: check InnovestX's real fee (POST /symbol/fee/tier)
    DEFAULT_BASE = "https://api.innovestxonline.com"
    DEFAULT_PATH = "/api/v1/digital-asset/orderbook/lvl2"
    DEPTH = 100
    FEE_PATH = "/api/v1/digital-asset/symbol/fee/tier"
    FEE_TTL = 3600           # real fee changes rarely (tiers): look it up once an hour
    FEE_FAIL_TTL = 300       # after a failed lookup, retry in 5 min instead of on every refresh
    MAX_PLAUSIBLE_FEE = 0.02  # a parsed fee above 2% is treated as a misread, not used
    fee_source = "placeholder"   # "live" | "env" | "placeholder": the app can show this
    fee_note = ""                # why/how the fee was chosen (diagnostics)

    def configured(self):
        return bool(_secret("INNOVESTX_API_KEY") and _secret("INNOVESTX_API_SECRET"))

    def _base_url(self):
        return (_secret("INNOVESTX_BASE_URL") or self.DEFAULT_BASE).rstrip("/")

    @staticmethod
    def _parse_fee(data):
        """POST /symbol/fee/tier -> (rate as fraction, raw feeAmount). Only Percentage fees are used.
        UNVERIFIED: the docs don't say whether a percentage comes as 0.25 (percent) or 0.0025
        (fraction). Values >= 0.05 are read as percent, smaller ones as a fraction; anything that
        ends up above MAX_PLAUSIBLE_FEE is rejected. Set INNOVESTX_FEE_RATE to override."""
        if str(data.get("code")) != "0000":
            raise ValueError(f"fee API {data.get('code')}: {data.get('message')}")
        d = data.get("data")
        rows = d if isinstance(d, list) else [d]
        pct = [r for r in rows if isinstance(r, dict) and str(r.get("feeType", "")).lower() == "percentage"]
        if not pct:
            raise ValueError(f"no percentage fee (feeType={[r.get('feeType') for r in rows if isinstance(r, dict)]})")
        raw = max(float(r["feeAmount"]) for r in pct)      # several order types: take the highest
        rate = raw / 100 if raw >= 0.05 else raw
        if not 0 <= rate <= InnovestXExchange.MAX_PLAUSIBLE_FEE:
            raise ValueError(f"implausible fee value {raw}")
        return rate, raw

    def _fee_rate(self, symbol):
        override = _secret("INNOVESTX_FEE_RATE")
        if override:
            try:
                rate = float(override)
                self.fee_source, self.fee_note = "env", f"INNOVESTX_FEE_RATE={override}"
                return rate
            except ValueError:
                pass
        sym = f"{symbol.split('/')[0].upper()}THB"
        key = (self.name, sym)
        with _CACHE_LOCK:
            hit = _FEE_CACHE.get(key)
        if not hit or time.time() - hit[0] >= hit[3]:
            try:
                r = self._fetch(self._base_url() + self.FEE_PATH, {"symbol": sym})
                if not r.ok:
                    raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
                rate, raw = self._parse_fee(r.json())
                hit = (time.time(), rate, f"live feeAmount={raw} -> {rate:.4%}", self.FEE_TTL)
            except Exception as e:                      # never let a fee lookup break the quote
                hit = (time.time(), None, f"placeholder {self.fee_rate:.2%} ({type(e).__name__}: {e})", self.FEE_FAIL_TTL)
            with _CACHE_LOCK:
                _FEE_CACHE[key] = hit
        self.fee_source = "live" if hit[1] is not None else "placeholder"
        self.fee_note = hit[2]
        return hit[1] if hit[1] is not None else self.fee_rate

    def _params(self, symbol):
        base_url = self._base_url()
        path = _secret("INNOVESTX_DEPTH_PATH") or self.DEFAULT_PATH
        return base_url + path, {"symbol": f"{symbol.split('/')[0].upper()}THB", "depth": self.DEPTH}

    def _fetch(self, url, body):
        key, secret = _secret("INNOVESTX_API_KEY"), _secret("INNOVESTX_API_SECRET")
        if not key or not secret:
            raise RuntimeError("INNOVESTX_API_KEY and INNOVESTX_API_SECRET must both be set")
        self.debug_note = f" [key length sent: {len(key)}]"
        body_str = json.dumps(body, separators=(",", ":"))          # sign exactly what we send
        parts = urlsplit(url)
        uid, ts, ctype = str(uuid.uuid4()), str(int(time.time() * 1000)), "application/json"
        query = f"?{parts.query}" if parts.query else ""
        to_sign = key + "POST" + parts.netloc.lower() + parts.path + query + ctype + uid + ts + body_str
        sig = hmac.new(secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {"Content-Type": ctype, "X-INVX-APIKEY": key, "X-INVX-SIGNATURE": sig,
                   "X-INVX-REQUEST-UID": uid, "X-INVX-TIMESTAMP": ts}
        return requests.post(url, data=body_str, headers=headers, timeout=self.timeout)

    def _parse(self, data):
        if str(data.get("code")) != "0000":
            hint = " (IP not whitelisted for this key)" if str(data.get("code")) == "4003" else ""
            raise ValueError(f"InnovestX {data.get('code')}: {data.get('message')}{hint}")
        rows = [r for r in data.get("data", []) if r.get("actionType", 0) != 2]   # 2 = deletion
        bids = [[r["price"], r["quantity"]] for r in rows if r.get("side") == 0]   # 0 = buy side
        asks = [[r["price"], r["quantity"]] for r in rows if r.get("side") == 1]   # 1 = sell side
        return asks, bids


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
