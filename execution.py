"""
execution.py — order placement for Crypto Mall (roadmap phase: Exchange Testnet).

SAFETY MODEL (read this first)
  * TESTNET ONLY. The adapter refuses any host except testnet.binance.vision.
  * DRY-RUN BY DEFAULT. Nothing is sent to the matching engine unless EXEC_ENABLED=1.
    In dry-run, if keys are set, the order is validated with POST /api/v3/order/test
    (checks signature and parameters without trading).
  * LIMITS: EXEC_MAX_ORDER_QUOTE (default 100) per order, EXEC_MAX_DAILY_QUOTE (default 500) per
    day, both in the quote asset (USDT). A kill switch (stored in the DB) blocks every order.
  * UNKNOWN != FAILED. Binance says a 5xx / timeout does NOT mean the order failed. Every order gets
    our own client order id, is written to the DB BEFORE it is sent, and an unclear reply is resolved
    by querying that id. Unresolved orders stay "UNKNOWN" and still count against the daily limit.
  * RECONCILE: after each fill we compare the balance change on the exchange with what the fill
    says it should be, and flag a mismatch.

Keys (env / .env / Streamlit Secrets):  BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_API_SECRET
The secret only signs requests; it is never sent or stored.
"""
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from urllib.parse import urlencode, urlsplit

import requests

import portfolio
from exchanges import _secret

TESTNET_BASE = "https://testnet.binance.vision"
ALLOWED_HOSTS = {"testnet.binance.vision"}          # this phase: testnet only
TESTNET_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
_sleep = time.sleep                                  # tests replace this


class ExecutionError(Exception):
    """Anything that stops an order before or while it is being placed."""


class ApiError(ExecutionError):
    """The exchange answered with an error (the order was NOT accepted)."""
    def __init__(self, status, code, msg, retry_after=None):
        self.status, self.code, self.msg, self.retry_after = status, code, msg, retry_after
        extra = f" (retry after {retry_after}s)" if retry_after else ""
        super().__init__(f"HTTP {status}, code {code}: {msg}{extra}")


class StatusUnknown(ExecutionError):
    """No clear answer (timeout / 5xx). The order may or may not exist."""


# --- helpers ------------------------------------------------------------------
def _d(x) -> Decimal:
    try:
        return x if isinstance(x, Decimal) else Decimal(str(x))
    except InvalidOperation as e:
        raise ExecutionError(f"Not a number: {x!r}") from e


def _fmt(d: Decimal) -> str:
    return "0" if d == 0 else format(d.normalize(), "f")


def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(_secret(name) or default)
    except InvalidOperation:
        return Decimal(default)


def exec_enabled() -> bool:
    return _secret("EXEC_ENABLED").lower() in ("1", "true", "yes", "on")


def max_order_quote() -> Decimal:
    return _env_decimal("EXEC_MAX_ORDER_QUOTE", "100")


def max_daily_quote() -> Decimal:
    return _env_decimal("EXEC_MAX_DAILY_QUOTE", "500")


# --- database (same SQLite file as the paper wallet) ---------------------------
def init_exec_db() -> None:
    with portfolio.db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS exec_orders (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   side TEXT NOT NULL,
                   requested TEXT NOT NULL,
                   notional_quote REAL NOT NULL,
                   client_order_id TEXT,
                   order_id TEXT,
                   status TEXT NOT NULL,
                   executed_qty REAL NOT NULL DEFAULT 0,
                   quote_qty REAL NOT NULL DEFAULT 0,
                   avg_price REAL NOT NULL DEFAULT 0,
                   fees TEXT NOT NULL DEFAULT '',
                   reconcile TEXT NOT NULL DEFAULT '',
                   note TEXT NOT NULL DEFAULT '')"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS exec_flags (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def set_kill(on: bool) -> None:
    init_exec_db()
    with portfolio.db() as conn:
        conn.execute("INSERT OR REPLACE INTO exec_flags (key, value) VALUES ('kill', ?)", ("1" if on else "0",))


def is_killed() -> bool:
    init_exec_db()
    with portfolio.db() as conn:
        row = conn.execute("SELECT value FROM exec_flags WHERE key = 'kill'").fetchone()
    return bool(row and row["value"] == "1")


def daily_notional() -> Decimal:
    """Quote value of today's testnet orders that were accepted or are unresolved."""
    init_exec_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    with portfolio.db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(notional_quote), 0) AS s FROM exec_orders "
            "WHERE mode = 'testnet' AND status != 'REJECTED' AND created_at LIKE ?", (today,)).fetchone()
    return Decimal(str(row["s"]))


def get_exec_orders(limit: int = 50) -> list[dict]:
    init_exec_db()
    with portfolio.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM exec_orders ORDER BY id DESC LIMIT ?", (limit,))]


def _insert(**row) -> int:
    row.setdefault("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    cols = ", ".join(row)
    with portfolio.db() as conn:
        cur = conn.execute(f"INSERT INTO exec_orders ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid


def _update(row_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with portfolio.db() as conn:
        conn.execute(f"UPDATE exec_orders SET {sets} WHERE id = ?", [*fields.values(), row_id])


# --- exchange adapter -----------------------------------------------------------
@dataclass
class Rules:
    symbol: str
    base: str
    quote: str
    status: str
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal


class BinanceTestnet:
    name = "Binance Testnet"
    timeout = 8
    RECV_WINDOW = 5000                      # Binance recommends 5000 or less

    def __init__(self):
        base = (_secret("BINANCE_TESTNET_BASE") or TESTNET_BASE).rstrip("/")
        if urlsplit(base).hostname not in ALLOWED_HOSTS:
            raise ExecutionError("Refusing to connect: this phase only talks to testnet.binance.vision.")
        self.base = base
        self._rules: dict[str, tuple[float, Rules]] = {}

    def configured(self) -> bool:
        return bool(_secret("BINANCE_TESTNET_API_KEY") and _secret("BINANCE_TESTNET_API_SECRET"))

    def _build(self, path, params, signed):
        params = dict(params or {})
        headers = {}
        if signed:
            key, secret = _secret("BINANCE_TESTNET_API_KEY"), _secret("BINANCE_TESTNET_API_SECRET")
            if not key or not secret:
                raise ExecutionError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET are not set.")
            params["recvWindow"] = self.RECV_WINDOW
            params["timestamp"] = int(time.time() * 1000)
            qs = urlencode(params)
            qs += "&signature=" + hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = key
        else:
            qs = urlencode(params)
        return f"{self.base}{path}" + (f"?{qs}" if qs else ""), headers

    def _call(self, method, path, params=None, signed=False, side_effect=False):
        """side_effect=True: an unclear reply raises StatusUnknown instead of a plain error."""
        url, headers = self._build(path, params, signed)
        try:
            r = requests.request(method, url, headers=headers, timeout=self.timeout)
        except Exception as e:                                   # timeout, connection reset, ...
            if side_effect:
                raise StatusUnknown(f"no response ({type(e).__name__})") from e
            raise ExecutionError(f"request failed ({type(e).__name__})") from e
        try:
            data = r.json()
        except Exception:
            data = {}
        code = data.get("code") if isinstance(data, dict) else None
        if r.status_code >= 500 or code == -1007:
            if side_effect:
                raise StatusUnknown(f"server reply unclear (HTTP {r.status_code}, code {code})")
            raise ExecutionError(f"exchange server error (HTTP {r.status_code})")
        if not r.ok:
            raise ApiError(r.status_code, code, data.get("msg") if isinstance(data, dict) else None,
                           (getattr(r, "headers", None) or {}).get("Retry-After"))
        return data

    # public
    def ticker_price(self, symbol: str) -> Decimal:
        return _d(self._call("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    def symbol_rules(self, symbol: str) -> Rules:
        hit = self._rules.get(symbol)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        info = self._call("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]
        f = {x["filterType"]: x for x in info.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
        rules = Rules(symbol, info["baseAsset"], info["quoteAsset"], info["status"],
                      _d(lot.get("stepSize", "0")), _d(lot.get("minQty", "0")), _d(notional.get("minNotional", "0")))
        self._rules[symbol] = (time.time(), rules)
        return rules

    # signed
    def balances(self) -> dict[str, Decimal]:
        acct = self._call("GET", "/api/v3/account", signed=True)
        return {b["asset"]: _d(b["free"]) for b in acct["balances"]}

    def test_order(self, params: dict) -> None:
        self._call("POST", "/api/v3/order/test", params, signed=True)

    def new_order(self, params: dict) -> dict:
        return self._call("POST", "/api/v3/order", params, signed=True, side_effect=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        return self._call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        return self._call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                          signed=True, side_effect=True)


# --- the order flow ---------------------------------------------------------------
@dataclass
class ExecResult:
    mode: str                     # "dry-run" | "testnet"
    status: str
    symbol: str
    side: str
    client_order_id: str = ""
    order_id: str = ""
    executed_qty: Decimal = Decimal(0)
    quote_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fees: str = ""
    reconcile: str = ""
    note: str = ""
    row_id: int = 0
    warnings: list = field(default_factory=list)


def status() -> dict:
    """Everything the UI needs to show before an order is placed."""
    ad = BinanceTestnet()
    return {"enabled": exec_enabled(), "configured": ad.configured(), "killed": is_killed(),
            "max_order": max_order_quote(), "max_daily": max_daily_quote(), "used_today": daily_notional()}


def reconcile(side, base, quote, executed, quote_qty, fills, before, after) -> str:
    """Compare the exchange's balance change with what the fill says it should be."""
    fees: dict[str, Decimal] = {}
    for fl in fills:
        fees[fl["commissionAsset"]] = fees.get(fl["commissionAsset"], Decimal(0)) + _d(fl["commission"])
    sign = 1 if side == "BUY" else -1
    expect = {base: sign * executed - (fees.get(base, Decimal(0)) if side == "BUY" else Decimal(0)),
              quote: -sign * quote_qty - fees.get(quote, Decimal(0))}
    if side == "SELL":
        expect[base] = -executed
        expect[quote] = quote_qty - fees.get(quote, Decimal(0))
    problems = []
    for asset, want in expect.items():
        got = after.get(asset, Decimal(0)) - before.get(asset, Decimal(0))
        tol = max(Decimal("0.00000001"), abs(want) * Decimal("0.000001"))
        if abs(got - want) > tol:
            problems.append(f"{asset}: expected {_fmt(want)}, exchange shows {_fmt(got)}")
    other = sorted(a for a in fees if a not in (base, quote))
    note = f" (fee paid in {', '.join(other)}: not checked)" if other else ""
    return ("MISMATCH: " + "; ".join(problems) if problems else "OK") + note


def place_market_order(symbol: str, side: str, amount, adapter: BinanceTestnet | None = None) -> ExecResult:
    """BUY: `amount` = quote to spend (e.g. USDT).  SELL: `amount` = base quantity (e.g. BTC)."""
    init_exec_db()
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ExecutionError("side must be BUY or SELL")
    if symbol not in TESTNET_SYMBOLS:
        raise ExecutionError(f"Symbol not allowed in this phase: {symbol}")
    amount = _d(amount)
    if amount <= 0:
        raise ExecutionError("Amount must be greater than zero.")
    if is_killed():
        raise ExecutionError("Kill switch is ON: no orders can be placed.")
    ad = adapter or BinanceTestnet()
    live = exec_enabled()
    mode = "testnet" if live else "dry-run"

    rules = ad.symbol_rules(symbol)
    if rules.status != "TRADING":
        raise ExecutionError(f"{symbol} is not trading (status {rules.status}).")
    params = {"symbol": symbol, "side": side, "type": "MARKET"}
    if side == "BUY":
        notional = amount
        params["quoteOrderQty"] = _fmt(amount)
        requested = f"spend {_fmt(amount)} {rules.quote}"
    else:
        qty = (amount / rules.step).to_integral_value(rounding=ROUND_DOWN) * rules.step if rules.step > 0 else amount
        if qty < rules.min_qty or qty <= 0:
            raise ExecutionError(f"Quantity {_fmt(qty)} is below the minimum lot {_fmt(rules.min_qty)} {rules.base}.")
        notional = qty * ad.ticker_price(symbol)
        params["quantity"] = _fmt(qty)
        requested = f"sell {_fmt(qty)} {rules.base}"
    if notional < rules.min_notional:
        raise ExecutionError(f"Order value {_fmt(notional.quantize(Decimal('0.01')))} is below the minimum "
                             f"notional {_fmt(rules.min_notional)} {rules.quote}.")
    if notional > max_order_quote():
        raise ExecutionError(f"Order value {_fmt(notional.quantize(Decimal('0.01')))} exceeds the per-order limit "
                             f"{_fmt(max_order_quote())} {rules.quote}.")
    if live and daily_notional() + notional > max_daily_quote():
        raise ExecutionError(f"This order would pass the daily limit of {_fmt(max_daily_quote())} {rules.quote} "
                             f"(used today: {_fmt(daily_notional().quantize(Decimal('0.01')))}).")

    result = ExecResult(mode=mode, status="", symbol=symbol, side=side)

    # ---- dry-run: validate only -------------------------------------------------
    if not live:
        if not ad.configured():
            result.status, result.note = "DRY_RUN", "Keys not set: order was not even validated with the exchange."
        else:
            try:
                ad.test_order(params)
                result.status, result.note = "DRY_RUN_VALIDATED", "Exchange accepted the order format and signature. Nothing was traded."
            except ExecutionError as e:
                result.status, result.note = "DRY_RUN_REJECTED", str(e)
        result.row_id = _insert(mode=mode, symbol=symbol, side=side, requested=requested,
                                notional_quote=float(notional), status=result.status, note=result.note)
        return result

    # ---- live (testnet) ---------------------------------------------------------
    if not ad.configured():
        raise ExecutionError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET are not set.")
    before = ad.balances()
    need_asset, need = (rules.quote, amount) if side == "BUY" else (rules.base, _d(params["quantity"]))
    if before.get(need_asset, Decimal(0)) < need:
        raise ExecutionError(f"Not enough {need_asset} on the testnet account "
                             f"(have {_fmt(before.get(need_asset, Decimal(0)))}, need {_fmt(need)}).")
    client_id = "cm-" + uuid.uuid4().hex[:24]
    params["newClientOrderId"] = client_id
    params["newOrderRespType"] = "FULL"
    result.client_order_id = client_id
    # write-ahead: the order exists in our DB before it can exist on the exchange
    result.row_id = _insert(mode=mode, symbol=symbol, side=side, requested=requested, notional_quote=float(notional),
                            client_order_id=client_id, status="SENDING")
    resp = None
    try:
        resp = ad.new_order(params)
    except StatusUnknown as e:
        result.warnings.append(f"Unclear reply ({e}); checking the order by its client id.")
        resp = _resolve(ad, symbol, client_id)
        if resp is None:
            result.status = "UNKNOWN"
            result.note = "Could not confirm whether the order exists. Check the exchange before trying again."
    except ApiError as e:
        result.status, result.note = "REJECTED", str(e)
    except Exception as e:                                          # keep the row honest, then re-raise
        _update(result.row_id, status="ERROR", note=f"{type(e).__name__}: {e}")
        raise
    if resp is not None:
        _fill_from_response(result, resp)
        if result.status in ("FILLED", "PARTIALLY_FILLED") and resp.get("fills") is not None:
            try:
                after = ad.balances()
                result.reconcile = reconcile(side, rules.base, rules.quote, result.executed_qty, result.quote_qty,
                                             resp.get("fills", []), before, after)
            except ExecutionError as e:
                result.reconcile = f"SKIPPED: could not read balances ({e})"
        elif result.status in ("FILLED", "PARTIALLY_FILLED"):
            result.reconcile = "SKIPPED: no fill details in the reply"
    _update(result.row_id, status=result.status, order_id=result.order_id,
            executed_qty=float(result.executed_qty), quote_qty=float(result.quote_qty),
            avg_price=float(result.avg_price), fees=result.fees, reconcile=result.reconcile,
            note=" | ".join(x for x in [result.note, *result.warnings] if x))
    return result


def _resolve(ad: BinanceTestnet, symbol: str, client_id: str, tries: int = 3):
    """Ask the exchange about our order by client id. None = still unclear."""
    for i in range(tries):
        _sleep(1.0 * (i + 1))
        try:
            return ad.get_order(symbol, client_id)
        except ApiError as e:
            if e.code != -2013:                                     # -2013: order does not exist (yet)
                return None
        except ExecutionError:
            pass
    return None


def _fill_from_response(result: ExecResult, resp: dict) -> None:
    result.status = resp.get("status", "UNKNOWN")
    result.order_id = str(resp.get("orderId", ""))
    result.executed_qty = _d(resp.get("executedQty", "0"))
    result.quote_qty = _d(resp.get("cummulativeQuoteQty", "0"))
    if result.executed_qty > 0 and result.quote_qty > 0:
        result.avg_price = result.quote_qty / result.executed_qty
    fees: dict[str, Decimal] = {}
    for fl in resp.get("fills") or []:
        fees[fl["commissionAsset"]] = fees.get(fl["commissionAsset"], Decimal(0)) + _d(fl["commission"])
    result.fees = ", ".join(f"{_fmt(v)} {a}" for a, v in fees.items())
