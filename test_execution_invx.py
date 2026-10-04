"""Run with:  pytest -q   (from the project folder)"""
import hashlib
import hmac
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import execution
import execution_invx as ix
import portfolio


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "DB_PATH", tmp_path / "t.db")
    portfolio.init_db()
    monkeypatch.setattr(ix, "_sleep", lambda s: None)
    for k in ("EXEC_ENABLED", "INVX_ALLOW_LIVE", "INVX_MAX_ORDER_THB", "INVX_MAX_DAILY_THB", "INVX_ORDER_TYPE",
              "INVX_SLIPPAGE_PCT", "INNOVESTX_API_KEY", "INNOVESTX_API_SECRET", "INNOVESTX_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def open_gates(monkeypatch):
    for k, v in {"EXEC_ENABLED": "1", "INVX_ALLOW_LIVE": "1",
                 "INNOVESTX_API_KEY": "k", "INNOVESTX_API_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


def row(state="FullyExecuted", qty="0.001", avg="100000", cid=1):
    return {"orderId": 9, "clientOrderId": cid, "orderState": state, "quantityExecuted": qty, "avgPrice": avg,
            "receiveDateTime": "2026-10-05T00:00:00.000Z"}


class FakeAd(ix.InnovestXExec):
    """Scripted exchange: get_order pops from `rows` (last one repeats)."""
    def __init__(self, send=None, rows=None, bal=None, cancel=None):
        super().__init__()
        self.sent, self.cancelled = [], []
        self._send, self._rows, self._cancel = send, list(rows or []), cancel
        self.bal = list(bal or [{"THB": Decimal(10000), "BTC": Decimal(0)}])

    def rules(self, symbol):
        return ix.Rules(Decimal("0.00001"), Decimal("0.01"))

    def balances(self):
        return self.bal.pop(0) if len(self.bal) > 1 else self.bal[0]

    def new_order(self, payload):
        self.sent.append(payload)
        if isinstance(self._send, Exception):
            raise self._send
        return self._send or {"code": "0000", "data": {"orderId": 9}}

    def get_order(self, symbol, cid):
        r = self._rows.pop(0) if len(self._rows) > 1 else (self._rows[0] if self._rows else None)
        if isinstance(r, Exception):
            raise r
        return r

    def cancel_order(self, cid):
        self.cancelled.append(cid)
        if isinstance(self._cancel, Exception):
            raise self._cancel
        return {"code": "0000"}


FILLED_BAL = [{"THB": Decimal(10000), "BTC": Decimal(0)}, {"THB": Decimal(9900), "BTC": Decimal("0.001")}]


# --- dry-run and gates ---------------------------------------------------------
def test_dry_run_by_default_sends_nothing():
    ad = FakeAd(rows=[row()])
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "DRY_RUN" and ad.sent == []
    assert "INVX_ALLOW_LIVE is off" in r.note and '"orderType":' in r.note.replace(" ", "")
    assert execution.get_exec_orders()[0]["mode"] == "invx-dry"


@pytest.mark.parametrize("missing", ["EXEC_ENABLED", "INVX_ALLOW_LIVE", "INNOVESTX_API_KEY"])
def test_every_gate_must_be_open(monkeypatch, missing):
    open_gates(monkeypatch)
    monkeypatch.delenv(missing)
    ad = FakeAd(rows=[row()])
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).status == "DRY_RUN"
    assert ad.sent == []


# --- payload ---------------------------------------------------------------------
def test_payload_is_a_marketable_limit_with_integer_client_id(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row()], bal=FILLED_BAL)
    ix.place_order("BTCTHB", "BUY", "0.0012345", "100000", ad)
    p = ad.sent[0]
    assert p["orderType"] == 2 and p["side"] == 0 and p["timeInForce"] == 1
    assert p["quantity"] == 0.00123                        # rounded DOWN to the 0.00001 increment
    assert p["limitPrice"] == 100300.0                     # +0.3%, rounded down to 0.01
    assert isinstance(p["clientOrderId"], int) and p["clientOrderId"] < 2 ** 63


def test_sell_limit_is_below_reference_and_market_has_no_limit_price(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row()], bal=[{"THB": Decimal(0), "BTC": Decimal(1)}])
    ix.place_order("BTCTHB", "SELL", "0.001", "100000", ad)
    assert ad.sent[0]["side"] == 1 and ad.sent[0]["limitPrice"] == 99700.0
    monkeypatch.setenv("INVX_ORDER_TYPE", "MARKET")
    ad2 = FakeAd(rows=[row()], bal=[{"THB": Decimal(0), "BTC": Decimal(1)}])
    ix.place_order("BTCTHB", "SELL", "0.001", "100000", ad2)
    assert ad2.sent[0]["orderType"] == 1 and "limitPrice" not in ad2.sent[0]


def test_quantity_below_increment_is_refused(monkeypatch):
    open_gates(monkeypatch)
    with pytest.raises(execution.ExecutionError, match="minimum increment"):
        ix.place_order("BTCTHB", "BUY", "0.000001", "100000", FakeAd(rows=[row()]))


# --- live flow -------------------------------------------------------------------
def test_live_fill_and_reconcile_ok(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row()], bal=FILLED_BAL)
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "FILLED" and r.reconcile == "OK" and len(ad.sent) == 1 and ad.cancelled == []
    assert r.avg_price == Decimal(100000) and r.order_id == "9"
    assert execution.get_exec_orders()[0]["status"] == "FILLED"


def test_reconcile_flags_mismatch(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row()], bal=[{"THB": Decimal(10000), "BTC": Decimal(0)}] * 2)
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).reconcile.startswith("MISMATCH")


def test_leftover_working_order_is_cancelled(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row("Working", "0", "0"), row("Working", "0", "0"), row("Working", "0", "0"),
                      row("Canceled", "0", "0")])
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert len(ad.cancelled) == 1 and r.status == "CANCELED"


def test_partial_fill_then_cancel_reports_partial(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row("Working", "0.0004"), row("Working", "0.0004"), row("Working", "0.0004"),
                      row("Canceled", "0.0004")],
                bal=[{"THB": Decimal(10000), "BTC": Decimal(0)}, {"THB": Decimal(9960), "BTC": Decimal("0.0004")}])
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "PARTIALLY_FILLED" and r.executed_qty == Decimal("0.0004") and r.reconcile == "OK"


def test_cancel_failure_is_flagged_loudly(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row("Working", "0", "0")], cancel=execution.StatusUnknown("timeout"))
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "WORKING" and any("cancel failed" in w for w in r.warnings)
    assert ix.daily_notional_thb() == Decimal(100)


def test_per_order_and_daily_limits(monkeypatch):
    open_gates(monkeypatch)
    with pytest.raises(execution.ExecutionError, match="per-order"):
        ix.place_order("BTCTHB", "BUY", "0.01", "100000", FakeAd(rows=[row()]))          # 1000 > 300
    monkeypatch.setenv("INVX_MAX_DAILY_THB", "350")
    ix.place_order("BTCTHB", "BUY", "0.002", "100000", FakeAd(rows=[row()], bal=FILLED_BAL))
    with pytest.raises(execution.ExecutionError, match="daily"):
        ix.place_order("BTCTHB", "BUY", "0.002", "100000", FakeAd(rows=[row()]))


def test_kill_switch_and_symbol_allowlist():
    with pytest.raises(execution.ExecutionError, match="not allowed"):
        ix.place_order("DOGETHB", "BUY", "1", "1")
    execution.set_kill(True)
    with pytest.raises(execution.ExecutionError, match="Kill switch"):
        ix.place_order("BTCTHB", "BUY", "0.001", "100000", FakeAd())


def test_insufficient_balance_blocks_before_sending(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(rows=[row()], bal=[{"THB": Decimal(10), "BTC": Decimal(0)}])
    with pytest.raises(execution.ExecutionError, match="Not enough THB"):
        ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert ad.sent == []


def test_unknown_reply_is_resolved_by_client_id(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(send=execution.StatusUnknown("timeout"), rows=[row()], bal=FILLED_BAL)
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "FILLED" and r.warnings


def test_unresolved_stays_unknown_and_counts_against_daily(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(send=execution.StatusUnknown("timeout"), rows=[None])
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).status == "UNKNOWN"
    assert ix.daily_notional_thb() == Decimal(100)


def test_accepted_but_not_visible_is_unknown(monkeypatch):
    open_gates(monkeypatch)
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", FakeAd(rows=[None])).status == "UNKNOWN"


def test_rejected_does_not_count_against_daily(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(send=execution.ApiError(400, "4019", "Insufficient Balance"), rows=[None])
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).status == "REJECTED"
    assert ix.daily_notional_thb() == 0


# --- parsing ---------------------------------------------------------------------
def test_order_states_accept_names_and_numbers():
    for raw, want in [("FullyExecuted", "FILLED"), ("Fully Executed", "FILLED"), (5, "FILLED"), ("Working", "WORKING"),
                      (1, "WORKING"), ("Rejected", "REJECTED"), (2, "REJECTED"), ("Canceled", "CANCELED"),
                      ("Expired", "EXPIRED"), ("???", "UNKNOWN")]:
        assert ix.parse_order_row({"orderState": raw, "quantityExecuted": "0"})["status"] == want
    assert ix.parse_order_row({"orderState": "Working", "quantityExecuted": "0.1"})["status"] == "PARTIALLY_FILLED"


# --- adapter / HTTP --------------------------------------------------------------
class Resp:
    def __init__(self, status=200, data=None, raw=None):
        self.status_code, self._d, self._raw = status, data, raw
        self.ok = status < 400

    def json(self):
        if self._raw is not None:
            raise ValueError("bad json")
        return self._d


def capture(monkeypatch, resp):
    calls = []

    def fake(method, url, data=None, headers=None, timeout=None):
        calls.append({"method": method, "url": url, "data": data, "headers": headers})
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(ix.requests, "request", fake)
    return calls


def test_signature_matches_the_documented_scheme_for_post_and_get(monkeypatch):
    open_gates(monkeypatch)
    ad = ix.InnovestXExec()
    calls = capture(monkeypatch, Resp(200, {"code": "0000", "data": []}))
    ad._call("/api/v1/digital-asset/order/send", {"symbol": "BTCTHB", "side": 0})
    ad._call("/api/v1/digital-asset/account/balance/inquiry", method="GET")
    for c in calls:
        h, body = c["headers"], (c["data"] or b"").decode()
        path = c["url"].split("api.innovestxonline.com")[1]
        to_sign = ("k" + c["method"] + "api.innovestxonline.com" + path + "application/json"
                   + h["X-INVX-REQUEST-UID"] + h["X-INVX-TIMESTAMP"] + body)
        assert h["X-INVX-SIGNATURE"] == hmac.new(b"s", to_sign.encode(), hashlib.sha256).hexdigest()
        assert h["X-INVX-APIKEY"] == "k"
    assert calls[1]["data"] is None                           # GET: empty body, not "{}"
    assert json.loads(calls[0]["data"]) == {"symbol": "BTCTHB", "side": 0}


def test_http_errors_on_send_are_classified(monkeypatch):
    open_gates(monkeypatch)
    ad = ix.InnovestXExec()
    capture(monkeypatch, Resp(500, {"code": "9100"}))
    with pytest.raises(execution.StatusUnknown):
        ad.new_order({})
    capture(monkeypatch, Resp(200, raw="<html>"))
    with pytest.raises(execution.StatusUnknown):               # unreadable reply: the order may exist
        ad.new_order({})
    capture(monkeypatch, ConnectionError("down"))
    with pytest.raises(execution.StatusUnknown):
        ad.new_order({})
    capture(monkeypatch, Resp(403, {"code": "4004", "message": "Permission Denied"}))
    with pytest.raises(execution.ApiError, match="no Trading permission"):
        ad.new_order({})
    capture(monkeypatch, Resp(400, {"code": "4019", "message": "Insufficient Balance"}))
    with pytest.raises(execution.ApiError, match="4019"):
        ad.new_order({})


def test_balance_is_amount_minus_hold(monkeypatch):
    open_gates(monkeypatch)
    capture(monkeypatch, Resp(200, {"code": "0000", "data": [
        {"product": "THB", "amount": "1000.00", "hold": "250.00"}, {"product": "btc", "amount": "0.5", "hold": "0"}]}))
    assert ix.InnovestXExec().balances() == {"THB": Decimal("750.00"), "BTC": Decimal("0.5")}


def test_get_order_falls_back_to_open_orders(monkeypatch):
    open_gates(monkeypatch)
    ad = ix.InnovestXExec()
    seq = [Resp(200, {"code": "0000", "data": []}),
           Resp(200, {"code": "0000", "data": [{"clientOrderId": 5, "orderState": "Working"}]})]
    monkeypatch.setattr(ix.requests, "request", lambda *a, **k: seq.pop(0))
    assert ad.get_order("BTCTHB", 5)["orderState"] == "Working"


def test_adapter_refuses_other_hosts(monkeypatch):
    monkeypatch.setenv("INNOVESTX_BASE_URL", "https://evil.example.com")
    with pytest.raises(execution.ExecutionError, match="Refusing"):
        ix.InnovestXExec()
