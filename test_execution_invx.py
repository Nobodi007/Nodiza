"""Run with:  pytest -q   (from the project folder)"""
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
    for k in ("EXEC_ENABLED", "INVX_ALLOW_LIVE", "INVX_ORDER_PATH", "INVX_ORDER_STATUS_PATH", "INVX_BALANCE_PATH",
              "INVX_MAX_ORDER_THB", "INVX_MAX_DAILY_THB", "INNOVESTX_API_KEY", "INNOVESTX_API_SECRET"):
        monkeypatch.delenv(k, raising=False)


def open_gates(monkeypatch):
    for k, v in {"EXEC_ENABLED": "1", "INVX_ALLOW_LIVE": "1", "INVX_ORDER_PATH": "/x/order",
                 "INVX_ORDER_STATUS_PATH": "/x/status", "INNOVESTX_API_KEY": "k", "INNOVESTX_API_SECRET": "s"}.items():
        monkeypatch.setenv(k, v)


class FakeAd(ix.InnovestXExec):
    def __init__(self, order=None, bal=None, status=None):
        super().__init__()
        self.sent, self._order, self._status = [], order, status
        self.bal = bal or [{"THB": Decimal(10000), "BTC": Decimal(0)}]

    def balances(self):
        return self.bal.pop(0) if len(self.bal) > 1 else self.bal[0]

    def new_order(self, payload):
        self.sent.append(payload)
        if isinstance(self._order, Exception):
            raise self._order
        return self._order

    def get_order(self, symbol, cid):
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def filled(qty="0.001", amt="100"):
    return {"code": "0000", "data": {"status": "FILLED", "orderId": "9", "executedQuantity": qty, "executedAmount": amt}}


def test_dry_run_by_default_sends_nothing():
    ad = FakeAd(order=filled())
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "DRY_RUN" and ad.sent == []
    assert "INVX_ALLOW_LIVE is off" in r.note
    assert execution.get_exec_orders()[0]["mode"] == "invx-dry"


@pytest.mark.parametrize("missing", ["EXEC_ENABLED", "INVX_ALLOW_LIVE", "INVX_ORDER_PATH", "INNOVESTX_API_KEY"])
def test_every_gate_must_be_open(monkeypatch, missing):
    open_gates(monkeypatch)
    monkeypatch.delenv(missing)
    ad = FakeAd(order=filled())
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).status == "DRY_RUN"
    assert ad.sent == []


def test_live_fill_and_reconcile_ok(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=filled(), bal=[{"THB": Decimal(10000), "BTC": Decimal(0)},
                                     {"THB": Decimal(9900), "BTC": Decimal("0.001")}])
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "FILLED" and r.reconcile == "OK" and len(ad.sent) == 1
    assert r.avg_price == Decimal(100000)


def test_reconcile_flags_mismatch(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=filled(), bal=[{"THB": Decimal(10000), "BTC": Decimal(0)},
                                     {"THB": Decimal(10000), "BTC": Decimal(0)}])
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).reconcile.startswith("MISMATCH")


def test_per_order_and_daily_limits(monkeypatch):
    open_gates(monkeypatch)
    with pytest.raises(execution.ExecutionError, match="per-order"):
        ix.place_order("BTCTHB", "BUY", "0.01", "100000", FakeAd(order=filled()))     # ฿1000 > ฿300
    monkeypatch.setenv("INVX_MAX_DAILY_THB", "350")
    ix.place_order("BTCTHB", "BUY", "0.002", "100000", FakeAd(order=filled()))        # ฿200
    with pytest.raises(execution.ExecutionError, match="daily"):
        ix.place_order("BTCTHB", "BUY", "0.002", "100000", FakeAd(order=filled()))


def test_kill_switch_and_symbol_allowlist(monkeypatch):
    with pytest.raises(execution.ExecutionError, match="not allowed"):
        ix.place_order("DOGETHB", "BUY", "1", "1")
    execution.set_kill(True)
    with pytest.raises(execution.ExecutionError, match="Kill switch"):
        ix.place_order("BTCTHB", "BUY", "0.001", "100000", FakeAd())


def test_insufficient_balance_blocks_before_sending(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=filled(), bal=[{"THB": Decimal(10), "BTC": Decimal(0)}])
    with pytest.raises(execution.ExecutionError, match="Not enough THB"):
        ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert ad.sent == []


def test_unknown_reply_is_resolved_by_client_id(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=execution.StatusUnknown("timeout"), status=filled())
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "FILLED" and r.warnings


def test_unresolved_stays_unknown_and_counts_against_daily(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=execution.StatusUnknown("timeout"), status=execution.ExecutionError("nope"))
    r = ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad)
    assert r.status == "UNKNOWN"
    assert ix.daily_notional_thb() == Decimal(100)


def test_rejected_does_not_count_against_daily(monkeypatch):
    open_gates(monkeypatch)
    ad = FakeAd(order=execution.ApiError(200, "1234", "bad"))
    assert ix.place_order("BTCTHB", "BUY", "0.001", "100000", ad).status == "REJECTED"
    assert ix.daily_notional_thb() == 0


def test_adapter_refuses_other_hosts(monkeypatch):
    monkeypatch.setenv("INNOVESTX_BASE_URL", "https://evil.example.com")
    with pytest.raises(execution.ExecutionError, match="Refusing"):
        ix.InnovestXExec()
