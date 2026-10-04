"""
execution_invx.py — order placement on InnovestX for Crypto Mall (roadmap: real execution, tiny size).

STATUS: SCAFFOLD. The safety model and order flow are finished and tested, but the InnovestX
order endpoints (paths + field names) are NOT verified. Every unverified item is marked
"UNVERIFIED" and is isolated in build_order_payload() / parse_order_response() / the INVX_*_PATH
settings, so confirming them against api-docs.innovestxonline.com means editing those spots only.

SAFETY MODEL — a real order needs ALL of these, otherwise the call is a DRY-RUN
(payload is built and logged, NOTHING is sent):
  1. EXEC_ENABLED=1
  2. INVX_ALLOW_LIVE=1                (second, separate switch just for real money)
  3. INVX_ORDER_PATH is set           (no default on purpose: forces you to confirm it in the docs)
  4. INNOVESTX_API_KEY / SECRET set   (key must have Trading permission + IP whitelisted)
  5. kill switch OFF, symbol allow-listed, order <= INVX_MAX_ORDER_THB, day total <= INVX_MAX_DAILY_THB
Other rules carried over from execution.py:
  * the order is written to the DB BEFORE it is sent (write-ahead), with our own client order id
  * a timeout / 5xx is UNKNOWN, not FAILED: we look the order up by client id; unresolved stays
    "UNKNOWN" and still counts against the daily limit
  * after a fill the THB/coin balance change on the exchange is compared with what the fill says

Env / Secrets (all optional except the keys):
  INVX_MAX_ORDER_THB (default 300)   INVX_MAX_DAILY_THB (default 1000)
  INVX_ORDER_PATH  INVX_ORDER_STATUS_PATH  INVX_BALANCE_PATH  INVX_CANCEL_PATH
"""
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

import requests

import portfolio
from exchanges import InnovestXExchange, _secret
from execution import (ApiError, ExecutionError, StatusUnknown, _d, _fmt, _insert, _update,
                       exec_enabled, init_exec_db, is_killed)

ALLOWED_SYMBOLS = ["BTCTHB", "ETHTHB"]
ALLOWED_HOSTS = {"api.innovestxonline.com"}
LIVE_MODE, DRY_MODE = "invx-live", "invx-dry"
_sleep = time.sleep                                   # tests replace this


# --- limits (THB) --------------------------------------------------------------
def _env_decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(_secret(name) or default)
    except Exception:
        return Decimal(default)


def max_order_thb() -> Decimal:
    return _env_decimal("INVX_MAX_ORDER_THB", "300")


def max_daily_thb() -> Decimal:
    return _env_decimal("INVX_MAX_DAILY_THB", "1000")


def allow_live() -> bool:
    return _secret("INVX_ALLOW_LIVE").lower() in ("1", "true", "yes", "on")


def daily_notional_thb() -> Decimal:
    """THB value of today's real orders that were accepted or are unresolved (not REJECTED)."""
    init_exec_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    with portfolio.db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(notional_quote), 0) AS s FROM exec_orders "
            "WHERE mode = ? AND status NOT IN ('REJECTED', 'ERROR') AND created_at LIKE ?",
            (LIVE_MODE, today)).fetchone()
    return Decimal(str(row["s"]))


# --- adapter ---------------------------------------------------------------------
class InnovestXExec:
    name = "InnovestX"
    timeout = 8

    def __init__(self):
        self.base = (_secret("INNOVESTX_BASE_URL") or InnovestXExchange.DEFAULT_BASE).rstrip("/")
        if urlsplit(self.base).hostname not in ALLOWED_HOSTS:
            raise ExecutionError("Refusing to connect: execution only talks to api.innovestxonline.com.")

    def configured(self) -> bool:
        return bool(_secret("INNOVESTX_API_KEY") and _secret("INNOVESTX_API_SECRET"))

    @staticmethod
    def path(kind: str) -> str:
        return _secret(f"INVX_{kind}_PATH")           # "" = not confirmed yet

    def _signed_headers(self, method: str, url: str, body_str: str) -> dict:
        """Same scheme as InnovestXExchange._fetch (verified there for the depth endpoint)."""
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

    def _call(self, path: str, body: dict, method: str = "POST", side_effect: bool = False) -> dict:
        if not path:
            raise ExecutionError("Endpoint path is not configured (see INVX_*_PATH).")
        url = self.base + path
        body_str = json.dumps(body, separators=(",", ":"))     # sign exactly what we send
        headers = self._signed_headers(method, url, body_str)
        try:
            r = requests.request(method, url, data=body_str, headers=headers, timeout=self.timeout)
        except Exception as e:
            if side_effect:
                raise StatusUnknown(f"no response ({type(e).__name__})") from e
            raise ExecutionError(f"request failed ({type(e).__name__})") from e
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 500:
            if side_effect:
                raise StatusUnknown(f"server reply unclear (HTTP {r.status_code})")
            raise ExecutionError(f"exchange server error (HTTP {r.status_code})")
        code = str(data.get("code")) if isinstance(data, dict) else ""
        if not r.ok or code != "0000":                          # "0000" = success on the other endpoints
            hint = " (IP not whitelisted for this key)" if code == "4003" else ""
            raise ApiError(r.status_code, code, f"{data.get('message') if isinstance(data, dict) else ''}{hint}")
        return data

    def new_order(self, payload: dict) -> dict:
        return self._call(self.path("ORDER"), payload, side_effect=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        return self._call(self.path("ORDER_STATUS"), {"symbol": symbol, "clientOrderId": client_order_id})

    def balances(self) -> dict[str, Decimal]:
        data = self._call(self.path("BALANCE"), {})
        return parse_balances(data)


# --- UNVERIFIED: field names. Confirm against the InnovestX docs, then edit only here. --------
def build_order_payload(symbol: str, side: str, quantity: Decimal, client_id: str) -> dict:
    """UNVERIFIED. `side` 0/1 mirrors the depth endpoint (0 = buy, 1 = sell); the rest are guesses."""
    return {"symbol": symbol, "side": 0 if side == "BUY" else 1, "orderType": _secret("INVX_ORDER_TYPE") or "MARKET",
            "quantity": _fmt(quantity), "clientOrderId": client_id}


def parse_order_response(resp: dict) -> dict:
    """UNVERIFIED. Returns {status, order_id, executed_qty, quote_qty}. status in
    FILLED / PARTIALLY_FILLED / NEW / CANCELED / REJECTED / UNKNOWN."""
    d = resp.get("data") or {}
    d = d[0] if isinstance(d, list) and d else d
    raw = str(d.get("status", d.get("orderStatus", ""))).upper()
    status = {"FILLED": "FILLED", "PARTIALLY_FILLED": "PARTIALLY_FILLED", "NEW": "NEW", "OPEN": "NEW",
              "CANCELED": "CANCELED", "CANCELLED": "CANCELED", "REJECTED": "REJECTED"}.get(raw, "UNKNOWN")
    return {"status": status, "order_id": str(d.get("orderId", "")),
            "executed_qty": _d(d.get("executedQuantity", d.get("executedQty", "0"))),
            "quote_qty": _d(d.get("executedAmount", d.get("cummulativeQuoteQty", "0")))}


def parse_balances(data: dict) -> dict[str, Decimal]:
    """UNVERIFIED. Expects data = [{"asset"/"currency": "BTC", "available": "0.1"}, ...]."""
    rows = data.get("data") or []
    out = {}
    for r in rows if isinstance(rows, list) else []:
        asset = r.get("asset") or r.get("currency") or r.get("symbol")
        if asset:
            out[str(asset).upper()] = _d(r.get("available", r.get("free", "0")))
    return out


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
            "order_path_set": bool(ad.path("ORDER")), "killed": is_killed(),
            "max_order": max_order_thb(), "max_daily": max_daily_thb(), "used_today": daily_notional_thb()}


def _live_blockers(ad: InnovestXExec) -> list[str]:
    out = []
    if not exec_enabled():
        out.append("EXEC_ENABLED is off")
    if not allow_live():
        out.append("INVX_ALLOW_LIVE is off")
    if not ad.path("ORDER"):
        out.append("INVX_ORDER_PATH is not set")
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
    """Place an order for `quantity` coins. `ref_price` = the app's current ask (BUY) / bid (SELL),
    used only to size the order against the THB limits. Dry-run unless every live gate is open."""
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
    notional = qty * price
    base = symbol[:-3]
    if notional > max_order_thb():
        raise ExecutionError(f"Order value ฿{_fmt(notional.quantize(Decimal('0.01')))} exceeds the per-order "
                             f"limit ฿{_fmt(max_order_thb())}.")
    blockers = _live_blockers(ad)
    live = not blockers
    if live and daily_notional_thb() + notional > max_daily_thb():
        raise ExecutionError(f"This order would pass the daily limit of ฿{_fmt(max_daily_thb())} "
                             f"(used today: ฿{_fmt(daily_notional_thb().quantize(Decimal('0.01')))}).")
    requested = f"{side.lower()} {_fmt(qty)} {base} @~{_fmt(price)}"
    client_id = "cm-" + uuid.uuid4().hex[:24]
    payload = build_order_payload(symbol, side, qty, client_id)
    result = InvxResult(mode=LIVE_MODE if live else DRY_MODE, status="", symbol=symbol, side=side,
                        client_order_id=client_id)

    if not live:                                           # ---- dry-run: nothing leaves this machine
        result.status = "DRY_RUN"
        result.note = "Not sent: " + "; ".join(blockers) + f". Payload: {json.dumps(payload)}"
        result.row_id = _insert(mode=DRY_MODE, symbol=symbol, side=side, requested=requested,
                                notional_quote=float(notional), client_order_id=client_id,
                                status=result.status, note=result.note)
        return result

    before = ad.balances()                                 # ---- live
    need_asset, need = ("THB", notional) if side == "BUY" else (base, qty)
    if before.get(need_asset, Decimal(0)) < need:
        raise ExecutionError(f"Not enough {need_asset} on InnovestX "
                             f"(have {_fmt(before.get(need_asset, Decimal(0)))}, need {_fmt(need)}).")
    result.row_id = _insert(mode=LIVE_MODE, symbol=symbol, side=side, requested=requested,   # write-ahead
                            notional_quote=float(notional), client_order_id=client_id, status="SENDING")
    parsed = None
    try:
        parsed = parse_order_response(ad.new_order(payload))
    except StatusUnknown as e:
        result.warnings.append(f"Unclear reply ({e}); checking the order by its client id.")
        parsed = _resolve(ad, symbol, client_id)
        if parsed is None:
            result.status = "UNKNOWN"
            result.note = "Could not confirm whether the order exists. Check InnovestX before trying again."
    except ApiError as e:
        result.status, result.note = "REJECTED", str(e)
    except Exception as e:
        _update(result.row_id, status="ERROR", note=f"{type(e).__name__}: {e}")
        raise
    if parsed is not None:
        result.status, result.order_id = parsed["status"], parsed["order_id"]
        result.executed_qty, result.quote_qty = parsed["executed_qty"], parsed["quote_qty"]
        if result.executed_qty > 0 and result.quote_qty > 0:
            result.avg_price = result.quote_qty / result.executed_qty
        if result.status in ("FILLED", "PARTIALLY_FILLED"):
            try:
                result.reconcile = reconcile(side, base, before, ad.balances(), result.executed_qty, result.quote_qty)
            except ExecutionError as e:
                result.reconcile = f"SKIPPED: could not read balances ({e})"
    _update(result.row_id, status=result.status, order_id=result.order_id,
            executed_qty=float(result.executed_qty), quote_qty=float(result.quote_qty),
            avg_price=float(result.avg_price), reconcile=result.reconcile,
            note=" | ".join(x for x in [result.note, *result.warnings] if x))
    return result


def _resolve(ad: InnovestXExec, symbol: str, client_id: str, tries: int = 3):
    """Ask InnovestX about our order by client id. None = still unclear (stays UNKNOWN)."""
    if not ad.path("ORDER_STATUS"):
        return None
    for i in range(tries):
        _sleep(1.0 * (i + 1))
        try:
            parsed = parse_order_response(ad.get_order(symbol, client_id))
            if parsed["status"] != "UNKNOWN":
                return parsed
        except ExecutionError:
            pass
    return None
