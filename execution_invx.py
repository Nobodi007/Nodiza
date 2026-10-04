"""
execution_invx.py — order placement on InnovestX for Crypto Mall (roadmap Phase 10, tiny size).

Endpoints, fields and error codes follow the InnovestX Open API docs:
  POST /api/v1/digital-asset/order/send            (Trading permission)
  POST /api/v1/digital-asset/order/cancel          (Trading)
  POST /api/v1/digital-asset/order/history/inquiry (Read/Trading)   <- order status by clientOrderId
  GET  /api/v1/digital-asset/order/open/inquiry    (Read/Trading)
  GET  /api/v1/digital-asset/account/balance/inquiry
  GET  /api/v1/digital-asset/symbols               (quantity / price increments)
Things the docs leave unclear are marked "UNVERIFIED" (checked on the first tiny live order).

HOW AN ORDER IS SENT
  * Default order = a marketable LIMIT (limit price = reference price +/- INVX_SLIPPAGE_PCT, default 0.3%),
    so a thin book can never fill you far from the price you saw. Set INVX_ORDER_TYPE=MARKET for market orders.
  * The send reply only contains an orderId, so the fill is read back by our own clientOrderId.
  * timeInForce can only be GTC, so a leftover (unfilled) part would stay on the book: we cancel it.

SAFETY MODEL — a real order needs ALL of these, otherwise the call is a DRY-RUN (nothing is sent):
  1. EXEC_ENABLED=1
  2. INVX_ALLOW_LIVE=1                (second switch just for real money)
  3. INNOVESTX_API_KEY / SECRET set   (the key needs Trading permission; Withdraw/Deposit should stay OFF)
  4. kill switch OFF, symbol allow-listed, order <= INVX_MAX_ORDER_THB, day total <= INVX_MAX_DAILY_THB
  * the order is written to the DB BEFORE it is sent (write-ahead), with our own client order id
  * a timeout / 5xx / unreadable reply is UNKNOWN, not FAILED: we look the order up by client id;
    unresolved stays "UNKNOWN" and still counts against the daily limit
  * after a fill, the THB/coin balance change on the exchange is compared with what the fill says

Env / Secrets (optional unless noted):
  INVX_MAX_ORDER_THB (default 300)   INVX_MAX_DAILY_THB (default 1000)   INVX_SLIPPAGE_PCT (default 0.3)
  INVX_ORDER_TYPE (LIMIT|MARKET, default LIMIT)
  INVX_ORDER_PATH / INVX_CANCEL_PATH / INVX_HISTORY_PATH / INVX_OPEN_PATH / INVX_BALANCE_PATH (override paths)
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from urllib.parse import urlsplit

import requests

import portfolio
from exchanges import InnovestXExchange, _secret
from execution import (ApiError, ExecutionError, StatusUnknown, _d, _fmt, _insert, _update,
                       exec_enabled, init_exec_db, is_killed)

ALLOWED_SYMBOLS = ["BTCTHB", "ETHTHB"]
ALLOWED_HOSTS = {"api.innovestxonline.com"}
LIVE_MODE, DRY_MODE = "invx-live", "invx-dry"
API = "/api/v1/digital-asset"
PATHS = {"ORDER": API + "/order/send", "CANCEL": API + "/order/cancel",
         "HISTORY": API + "/order/history/inquiry", "OPEN": API + "/order/open/inquiry",
         "BALANCE": API + "/account/balance/inquiry", "SYMBOLS": API + "/symbols"}
_sleep = time.sleep                                   # tests replace this


# --- limits ----------------------------------------------------------------------
def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(_secret(name) or default)
    except Exception:
        return Decimal(default)


def max_order_thb() -> Decimal:
    return _env_decimal("INVX_MAX_ORDER_THB", "300")


def max_daily_thb() -> Decimal:
    return _env_decimal("INVX_MAX_DAILY_THB", "1000")


def slippage_pct() -> Decimal:
    return min(max(_env_decimal("INVX_SLIPPAGE_PCT", "0.3"), Decimal("0")), Decimal("5"))


def order_type() -> str:
    return "MARKET" if _secret("INVX_ORDER_TYPE").upper() == "MARKET" else "LIMIT"


def allow_live() -> bool:
    return _secret("INVX_ALLOW_LIVE").lower() in ("1", "true", "yes", "on")


def daily_notional_thb() -> Decimal:
    """THB value of today's real orders that were accepted or are unresolved (not REJECTED/ERROR)."""
    init_exec_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    with portfolio.db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(notional_quote), 0) AS s FROM exec_orders "
            "WHERE mode = ? AND status NOT IN ('REJECTED', 'ERROR') AND created_at LIKE ?",
            (LIVE_MODE, today)).fetchone()
    return Decimal(str(row["s"]))


def new_client_id() -> int:
    """clientOrderId is a Long Integer in the docs: milliseconds * 1000 + random, unique and < 2^63."""
    return int(time.time() * 1000) * 1000 + secrets.randbelow(1000)


# --- adapter ---------------------------------------------------------------------
@dataclass
class Rules:
    qty_inc: Decimal
    price_inc: Decimal


class InnovestXExec:
    name = "InnovestX"
    timeout = 8

    def __init__(self):
        self.base = (_secret("INNOVESTX_BASE_URL") or InnovestXExchange.DEFAULT_BASE).rstrip("/")
        if urlsplit(self.base).hostname not in ALLOWED_HOSTS:
            raise ExecutionError("Refusing to connect: execution only talks to api.innovestxonline.com.")
        self._rules: dict[str, tuple[float, Rules]] = {}

    def configured(self) -> bool:
        return bool(_secret("INNOVESTX_API_KEY") and _secret("INNOVESTX_API_SECRET"))

    @staticmethod
    def path(kind: str) -> str:
        return _secret(f"INVX_{kind}_PATH") or PATHS[kind]

    def _signed_headers(self, method: str, url: str, body_str: str) -> dict:
        """Signature per the docs: HMAC-SHA256(secret, apikey+VERB+host+path+query+content-type+uid+ts+body)."""
        key, secret = _secret("INNOVESTX_API_KEY"), _secret("INNOVESTX_API_SECRET")
        if not key or not secret:
            raise ExecutionError("INNOVESTX_API_KEY / INNOVESTX_API_SECRET are not set.")
        parts = urlsplit(url)
        uid, ts, ctype = str(uuid.uuid4()), str(int(time.time() * 1000)), "application/json"
        query = f"?{parts.query}" if parts.query else ""
        to_sign = key + method + parts.netloc.lower() + parts.path + query + ctype + uid + ts + body_str
        sig = hmac.new(secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        return {"Content-Type": ctype, "X-INVX-APIKEY": key, "X-INVX-SIGNATURE": sig,
                "X-INVX-REQUEST-UID": uid, "X-INVX-TIMESTAMP": ts}

    def _call(self, path: str, body: dict | None = None, method: str = "POST", side_effect: bool = False) -> dict:
        """side_effect=True: an unclear reply raises StatusUnknown (the order may exist) instead of a plain error."""
        url = self.base + path
        body_str = "" if body is None else json.dumps(body, separators=(",", ":"))   # sign exactly what we send
        headers = self._signed_headers(method, url, body_str)
        try:
            r = requests.request(method, url, data=body_str.encode() if body_str else None,
                                 headers=headers, timeout=self.timeout)
        except Exception as e:
            if side_effect:
                raise StatusUnknown(f"no response ({type(e).__name__})") from e
            raise ExecutionError(f"request failed ({type(e).__name__})") from e
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code >= 500 or not isinstance(data, dict):
            if side_effect:
                raise StatusUnknown(f"server reply unclear (HTTP {r.status_code})")
            raise ExecutionError(f"exchange server error or unreadable reply (HTTP {r.status_code})")
        code = str(data.get("code", data.get("status", "")))
        if not r.ok or code != "0000":
            hint = {"4003": " (IP not whitelisted for this key)",
                    "4004": " (this API key has no Trading permission)",
                    "4019": " (insufficient balance)"}.get(code, "")
            detail = (data.get("data") or {}).get("detail") if isinstance(data.get("data"), dict) else ""
            raise ApiError(r.status_code, code, f"{data.get('message')}{f' / {detail}' if detail else ''}{hint}")
        return data

    # --- reads
    def rules(self, symbol: str) -> Rules:
        hit = self._rules.get(symbol)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        data = self._call(self.path("SYMBOLS"), method="GET")
        for row in data.get("data") or []:
            if row.get("symbol") == symbol:
                rules = Rules(_d(row.get("quantityIncrement", "0")), _d(row.get("priceIncrement", "0")))
                self._rules[symbol] = (time.time(), rules)
                return rules
        raise ExecutionError(f"{symbol} was not found in the exchange's symbol list.")

    def balances(self) -> dict[str, Decimal]:
        """Available = amount - hold. UNVERIFIED: the docs don't say whether `amount` includes `hold`."""
        out = {}
        for r in self._call(self.path("BALANCE"), method="GET").get("data") or []:
            if r.get("product"):
                out[str(r["product"]).upper()] = _d(r.get("amount", "0")) - _d(r.get("hold", "0"))
        return out

    def get_order(self, symbol: str, client_id: int) -> dict | None:
        """Latest known row for our client order id (history first, then open orders). None = not visible."""
        rows = []
        try:
            data = self._call(self.path("HISTORY"), {"symbol": symbol, "clientOrderId": int(client_id), "depth": 10})
            rows = [r for r in data.get("data") or [] if str(r.get("clientOrderId")) == str(client_id)]
        except ApiError as e:
            if e.code not in ("4041", "4001"):
                raise
        if not rows:
            data = self._call(self.path("OPEN"), method="GET")
            rows = [r for r in data.get("data") or [] if str(r.get("clientOrderId")) == str(client_id)]
        return max(rows, key=lambda r: str(r.get("receiveDateTime", ""))) if rows else None

    # --- writes
    def new_order(self, payload: dict) -> dict:
        return self._call(self.path("ORDER"), payload, side_effect=True)

    def cancel_order(self, client_id: int) -> dict:
        return self._call(self.path("CANCEL"), {"clientOrderId": int(client_id), "orderId": None}, side_effect=True)


# --- payload / response parsing (field names from the docs) --------------------------
def _round_to(x: Decimal, inc: Decimal, mode) -> Decimal:
    return x if inc <= 0 else (x / inc).to_integral_value(rounding=mode) * inc


def build_order_payload(symbol: str, side: str, quantity: Decimal, client_id: int,
                        limit_price: Decimal | None = None) -> dict:
    p = {"symbol": symbol, "timeInForce": 1, "side": 0 if side == "BUY" else 1,
         "quantity": float(quantity), "orderType": 2 if limit_price is not None else 1,
         "clientOrderId": int(client_id)}
    if limit_price is not None:
        p["limitPrice"] = float(limit_price)
    return p


_STATES = {"0": "UNKNOWN", "UNKNOWN": "UNKNOWN", "1": "WORKING", "WORKING": "WORKING",
           "2": "REJECTED", "REJECTED": "REJECTED", "3": "CANCELED", "CANCELED": "CANCELED",
           "CANCELLED": "CANCELED", "4": "EXPIRED", "EXPIRED": "EXPIRED",
           "5": "FULLYEXECUTED", "FULLYEXECUTED": "FULLYEXECUTED"}


def parse_order_row(row: dict) -> dict:
    """The docs list numeric states but their examples show names ("Working", "FullyExecuted"): accept both.
    Returns status (FILLED | PARTIALLY_FILLED | WORKING | CANCELED | EXPIRED | REJECTED | UNKNOWN), working,
    order_id, executed_qty, quote_qty, note."""
    raw = str(row.get("orderState", "")).strip().upper().replace(" ", "").replace("_", "")
    state = _STATES.get(raw, "UNKNOWN")
    executed = _d(row.get("quantityExecuted", "0") or "0")
    avg = _d(row.get("avgPrice", "0") or "0")
    status = {"FULLYEXECUTED": "FILLED", "WORKING": "WORKING", "CANCELED": "CANCELED", "EXPIRED": "EXPIRED",
              "REJECTED": "REJECTED"}.get(state, "UNKNOWN")
    if status in ("WORKING", "CANCELED", "EXPIRED") and executed > 0:
        status = "PARTIALLY_FILLED"
    return {"status": status, "working": state == "WORKING", "order_id": str(row.get("orderId", "")),
            "executed_qty": executed, "quote_qty": executed * avg,
            "note": str(row.get("rejectReason") or row.get("cancelReason") or "")}


# --- order flow --------------------------------------------------------------------
@dataclass
class InvxResult:
    mode: str
    status: str
    symbol: str
    side: str
    client_order_id: str = ""
    order_id: str = ""
    executed_qty: Decimal = Decimal(0)
    quote_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    reconcile: str = ""
    note: str = ""
    row_id: int = 0
    warnings: list = field(default_factory=list)


def status() -> dict:
    """What the UI should show before anyone presses a button."""
    ad = InnovestXExec()
    return {"enabled": exec_enabled(), "allow_live": allow_live(), "configured": ad.configured(),
            "killed": is_killed(), "order_type": order_type(), "slippage_pct": slippage_pct(),
            "max_order": max_order_thb(), "max_daily": max_daily_thb(), "used_today": daily_notional_thb()}


def _live_blockers(ad: InnovestXExec) -> list[str]:
    out = []
    if not exec_enabled():
        out.append("EXEC_ENABLED is off")
    if not allow_live():
        out.append("INVX_ALLOW_LIVE is off")
    if not ad.configured():
        out.append("API keys are not set")
    return out


def reconcile(side: str, base: str, before: dict, after: dict, executed: Decimal, quote_qty: Decimal) -> str:
    """Compare the exchange's balance change with the fill. Fees are not modelled yet, so the
    tolerance is 1% of the order value; anything bigger is flagged."""
    sign = 1 if side == "BUY" else -1
    want = {base: sign * executed, "THB": -sign * quote_qty}
    problems = []
    for asset, w in want.items():
        got = after.get(asset, Decimal(0)) - before.get(asset, Decimal(0))
        tol = max(Decimal("0.00000001"), abs(w) * Decimal("0.01"))
        if abs(got - w) > tol:
            problems.append(f"{asset}: expected ~{_fmt(w)}, exchange shows {_fmt(got)}")
    return "MISMATCH: " + "; ".join(problems) if problems else "OK"


def place_order(symbol: str, side: str, quantity, ref_price, adapter: InnovestXExec | None = None) -> InvxResult:
    """Place an order for `quantity` coins. `ref_price` = the app's current ask (BUY) / bid (SELL): it sizes
    the order against the THB limits and sets the limit price. Dry-run unless every live gate is open."""
    init_exec_db()
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ExecutionError("side must be BUY or SELL")
    if symbol not in ALLOWED_SYMBOLS:
        raise ExecutionError(f"Symbol not allowed: {symbol}")
    qty, price = _d(quantity), _d(ref_price)
    if qty <= 0 or price <= 0:
        raise ExecutionError("Quantity and reference price must be greater than zero.")
    if is_killed():
        raise ExecutionError("Kill switch is ON: no orders can be placed.")
    ad = adapter or InnovestXExec()
    blockers = _live_blockers(ad)
    live = not blockers
    base = symbol[:-3]
    notes = []

    rules = None                                           # round to the exchange's increments
    if ad.configured():
        try:
            rules = ad.rules(symbol)
        except ExecutionError as e:
            if live:
                raise ExecutionError(f"Cannot read the symbol rules: {e}") from e
            notes.append(f"rules not checked ({e})")
    else:
        notes.append("increments not checked (no keys)")
    if rules:
        qty = _round_to(qty, rules.qty_inc, ROUND_DOWN)
        if qty <= 0:
            raise ExecutionError(f"Quantity is below the minimum increment {_fmt(rules.qty_inc)} {base}.")
    limit = None
    if order_type() == "LIMIT":
        tol = slippage_pct() / 100
        limit = price * (1 + tol) if side == "BUY" else price * (1 - tol)
        limit = _round_to(limit, rules.price_inc, ROUND_DOWN if side == "BUY" else ROUND_UP) if rules else limit.quantize(Decimal("0.01"))

    notional = qty * price
    if notional > max_order_thb():
        raise ExecutionError(f"Order value ฿{_fmt(notional.quantize(Decimal('0.01')))} exceeds the per-order "
                             f"limit ฿{_fmt(max_order_thb())}.")
    if live and daily_notional_thb() + notional > max_daily_thb():
        raise ExecutionError(f"This order would pass the daily limit of ฿{_fmt(max_daily_thb())} "
                             f"(used today: ฿{_fmt(daily_notional_thb().quantize(Decimal('0.01')))}).")
    requested = f"{side.lower()} {_fmt(qty)} {base} @~{_fmt(price)}"
    cid = new_client_id()
    payload = build_order_payload(symbol, side, qty, cid, limit)
    result = InvxResult(mode=LIVE_MODE if live else DRY_MODE, status="", symbol=symbol, side=side,
                        client_order_id=str(cid))

    if not live:                                           # ---- dry-run: nothing leaves this machine
        result.status = "DRY_RUN"
        result.note = ("Not sent: " + "; ".join(blockers) + f". Payload: {json.dumps(payload)}"
                       + (f" ({'; '.join(notes)})" if notes else ""))
        result.row_id = _insert(mode=DRY_MODE, symbol=symbol, side=side, requested=requested,
                                notional_quote=float(notional), client_order_id=str(cid),
                                status=result.status, note=result.note)
        return result

    before = ad.balances()                                 # ---- live
    need_asset, need = ("THB", qty * (limit or price)) if side == "BUY" else (base, qty)
    if before.get(need_asset, Decimal(0)) < need:
        raise ExecutionError(f"Not enough {need_asset} on InnovestX "
                             f"(have {_fmt(before.get(need_asset, Decimal(0)))}, need {_fmt(need)}).")
    result.row_id = _insert(mode=LIVE_MODE, symbol=symbol, side=side, requested=requested,   # write-ahead
                            notional_quote=float(notional), client_order_id=str(cid), status="SENDING")
    parsed = None
    try:
        sent = ad.new_order(payload)
        result.order_id = str((sent.get("data") or {}).get("orderId", ""))
        parsed = _settle(ad, symbol, cid, result)
        if parsed is None:
            result.status = "UNKNOWN"
            result.note = "Order was accepted but its status is not visible yet. Check InnovestX before trying again."
    except StatusUnknown as e:
        result.warnings.append(f"Unclear reply ({e}); checking the order by its client id.")
        parsed = _settle(ad, symbol, cid, result)
        if parsed is None:
            result.status = "UNKNOWN"
            result.note = "Could not confirm whether the order exists. Check InnovestX before trying again."
    except ApiError as e:
        result.status, result.note = "REJECTED", str(e)
    except Exception as e:
        _update(result.row_id, status="ERROR", note=f"{type(e).__name__}: {e}")
        raise
    if parsed is not None:
        result.status = parsed["status"]
        result.order_id = parsed["order_id"] or result.order_id
        result.executed_qty, result.quote_qty = parsed["executed_qty"], parsed["quote_qty"]
        result.note = parsed["note"] or result.note
        if result.executed_qty > 0 and result.quote_qty > 0:
            result.avg_price = result.quote_qty / result.executed_qty
        if result.executed_qty > 0:
            try:
                result.reconcile = reconcile(side, base, before, ad.balances(), result.executed_qty, result.quote_qty)
            except ExecutionError as e:
                result.reconcile = f"SKIPPED: could not read balances ({e})"
    _update(result.row_id, status=result.status, order_id=result.order_id,
            executed_qty=float(result.executed_qty), quote_qty=float(result.quote_qty),
            avg_price=float(result.avg_price), reconcile=result.reconcile,
            note=" | ".join(x for x in [result.note, *result.warnings] if x))
    return result


def _poll(ad: InnovestXExec, symbol: str, cid: int, tries: int = 3):
    """Read our order back by client id. Returns the last parsed row (maybe still working) or None."""
    last = None
    for i in range(tries):
        if i:
            _sleep(float(i))
        try:
            row = ad.get_order(symbol, cid)
        except ExecutionError:
            row = None
        if row:
            last = parse_order_row(row)
            if not last["working"] and last["status"] != "UNKNOWN":
                return last
    return last


def _settle(ad: InnovestXExec, symbol: str, cid: int, result: InvxResult):
    """Poll until the order is final. GTC is the only time-in-force, so anything still working is cancelled."""
    parsed = _poll(ad, symbol, cid)
    if parsed is None or not parsed["working"]:
        return parsed
    try:
        ad.cancel_order(cid)
        result.warnings.append("Part of the order was still open, so it was cancelled.")
    except (ExecutionError, StatusUnknown) as e:
        result.warnings.append(f"Order is still open and the cancel failed ({e}). Cancel it on InnovestX now.")
        return parsed
    return _poll(ad, symbol, cid) or parsed
