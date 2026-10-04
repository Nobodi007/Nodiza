"""Run with:  pytest -q   (from the project folder)"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import exchanges
import portfolio
from exchanges import BitkubExchange, MockExchange, Quote, build_mock_exchanges, fetch_quotes


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "DB_PATH", tmp_path / "test.db")
    exchanges._CACHE.clear()
    exchanges._FEE_CACHE.clear()
    portfolio.init_db()


def book(fee=0.0, asks=None, bids=None, symbol="BTC/THB", live=False):
    asks = asks or [(100.0, 1.0), (110.0, 1.0)]
    bids = bids or [(99.0, 1.0), (90.0, 1.0)]
    return Quote("X", symbol, bid=bids[0][0], ask=asks[0][0], fee_rate=fee, asks=asks, bids=bids, live=live)


# --- Quote maths ------------------------------------------------------------
def test_buy_takes_fee_first():
    q = Quote("X", "BTC/THB", bid=99, ask=100, fee_rate=0.01)      # no depth -> infinite at ask
    coins, fee = q.buy(1000)
    assert fee == pytest.approx(10)
    assert coins == pytest.approx(990 / 100)


def test_buy_walks_multiple_levels():
    coins, _ = book().buy(150)                                      # 100 THB @100 + 50 THB @110
    assert coins == pytest.approx(1 + 50 / 110)


def test_buy_raises_when_liquidity_runs_out():
    with pytest.raises(ValueError):
        book().buy(10_000)


def test_sell_walks_bid_levels_and_charges_fee():
    net, fee = book(fee=0.01).sell(1.5)                             # 1 @99 + 0.5 @90 = 144
    assert fee == pytest.approx(1.44)
    assert net == pytest.approx(144 - 1.44)


def test_sell_raises_when_liquidity_runs_out():
    with pytest.raises(ValueError):
        book().sell(5)


# --- Wallet / orders --------------------------------------------------------
def test_buy_moves_balances_and_records_order():
    q = book(fee=0.0025, asks=[(100.0, 1000.0)])
    coins = portfolio.buy(q, 10_000)
    bal = portfolio.get_balances()
    assert bal["THB"] == pytest.approx(portfolio.START_THB - 10_000)
    assert bal["BTC"] == pytest.approx(coins)
    order = portfolio.get_orders()[0]
    assert order["status"] == "Filled (mock)" and order["ref_price"] == 100.0


def test_live_quote_is_recorded_as_paper():
    portfolio.buy(book(asks=[(100.0, 1000.0)], live=True), 1_000)
    assert portfolio.get_orders()[0]["status"] == "Filled (paper)"


def test_buy_more_than_wallet_is_rejected_and_changes_nothing():
    with pytest.raises(portfolio.OrderError):
        portfolio.buy(book(asks=[(1.0, 1e12)]), portfolio.START_THB * 2)
    assert portfolio.get_balances()["THB"] == portfolio.START_THB
    assert portfolio.get_orders() == []


def test_sell_more_than_held_is_rejected():
    with pytest.raises(portfolio.OrderError):
        portfolio.sell(book(), 1.0)


def test_split_buy_is_all_or_nothing():
    good = book(asks=[(100.0, 1e6)])
    too_big = book(asks=[(100.0, 1e6)])
    with pytest.raises(portfolio.OrderError):                        # total > wallet
        portfolio.buy_split([(good, 600_000), (too_big, 600_000)])
    assert portfolio.get_balances()["THB"] == portfolio.START_THB
    assert portfolio.get_orders() == []


def test_split_buy_records_one_order_per_leg():
    legs = [(book(asks=[(100.0, 1e6)]), 10_000), (book(asks=[(101.0, 1e6)]), 10_000)]
    portfolio.buy_split(legs)
    assert len(portfolio.get_orders()) == 2
    assert portfolio.get_balances()["THB"] == pytest.approx(portfolio.START_THB - 20_000)


def test_split_sell_roundtrip():
    portfolio.buy(book(asks=[(100.0, 1e6)]), 10_000)
    held = portfolio.get_balances()["BTC"]
    thb = portfolio.sell_split([(book(bids=[(100.0, 1e6)]), held / 2), (book(bids=[(99.0, 1e6)]), held / 2)])
    assert thb > 0 and portfolio.get_balances()["BTC"] == pytest.approx(0, abs=1e-8)


def test_reset_wallet():
    portfolio.buy(book(asks=[(100.0, 1e6)]), 10_000)
    portfolio.reset_wallet()
    assert portfolio.get_balances()["THB"] == portfolio.START_THB and portfolio.get_orders() == []


# --- Live adapters (network mocked) ----------------------------------------
class FakeResp:
    def __init__(self, data): self._d = data
    ok = True
    def raise_for_status(self): pass
    def json(self): return self._d


def test_bitkub_parses_and_sorts_book(monkeypatch):
    data = {"error": 0, "result": {"asks": [[102, 1], [101, 2]], "bids": [[98, 1], [99, 3]]}}
    monkeypatch.setattr(exchanges.requests, "get", lambda *a, **k: FakeResp(data))
    q = BitkubExchange().get_price("BTC/THB")
    assert (q.ask, q.bid, q.live) == (101, 99, True)
    assert q.asks[0][0] < q.asks[1][0] and q.bids[0][0] > q.bids[1][0]


def test_failure_falls_back_to_mock_and_reports_error(monkeypatch):
    def boom(*a, **k): raise ConnectionError("down")
    monkeypatch.setattr(exchanges.requests, "get", boom)
    ex = BitkubExchange(fallback=MockExchange("Bitkub", 0.0025, 0.001, 0.0))
    q = ex.get_price("BTC/THB")
    assert q.live is False and "mock fallback" in q.exchange and "down" in ex.last_error


def test_failure_without_fallback_raises(monkeypatch):
    def boom(*a, **k): raise ConnectionError("down")
    monkeypatch.setattr(exchanges.requests, "get", boom)
    with pytest.raises(ConnectionError):
        BitkubExchange().get_price("BTC/THB")


def test_cache_prevents_second_request(monkeypatch):
    calls = []
    data = {"result": {"asks": [[101, 1]], "bids": [[99, 1]]}}
    monkeypatch.setattr(exchanges.requests, "get", lambda *a, **k: calls.append(1) or FakeResp(data))
    BitkubExchange().get_price("BTC/THB")
    BitkubExchange().get_price("BTC/THB")                           # new object, same cache
    assert len(calls) == 1


def test_fetch_quotes_keeps_exchange_order():
    exs = build_mock_exchanges()
    assert [q.exchange for q in fetch_quotes(exs, "BTC/THB")] == [e.name for e in exs]


# --- Routing statistics -----------------------------------------------------
def test_split_buy_records_gain_vs_single_venue():
    a = book(asks=[(100.0, 1e6)])
    b = book(asks=[(100.0, 1e6)])
    a.exchange, b.exchange = "A", "B"
    # pretend the best single venue would have given 9,900 coins less than the split
    coins = portfolio.buy_split([(a, 10_000), (b, 10_000)], baseline=("A", 190.0))
    assert coins == pytest.approx(200.0)
    s = portfolio.get_route_stats()
    assert s["compared"] == 1 and s["wins"] == 1
    assert s["total_gain_thb"] == pytest.approx(10 * 100.0)          # 10 coins * ~100 THB
    assert s["avg_gain_pct"] == pytest.approx(1000 / 20_000)


def test_split_sell_records_gain_and_loss():
    portfolio.buy(book(asks=[(100.0, 1e6)]), 10_000)
    held = portfolio.get_balances()["BTC"]
    q = book(bids=[(100.0, 1e6)])
    thb = portfolio.sell_split([(q, held)], baseline=("X", 9_000.0))
    assert portfolio.get_route_stats()["total_gain_thb"] == pytest.approx(thb - 9_000.0)


def test_route_without_baseline_is_counted_as_split_only():
    portfolio.buy_split([(book(asks=[(100.0, 1e6)]), 10_000)])
    s = portfolio.get_route_stats()
    assert (s["runs"], s["compared"], s["split_only"], s["wins"]) == (1, 0, 1, 0)


def test_route_stats_filter_by_live_and_reset():
    portfolio.buy_split([(book(asks=[(100.0, 1e6)], live=True), 10_000)], baseline=("X", 1.0))
    portfolio.buy_split([(book(asks=[(100.0, 1e6)], live=False), 10_000)], baseline=("X", 1.0))
    assert portfolio.get_route_stats(live=True)["runs"] == 1
    assert portfolio.get_route_stats(live=False)["runs"] == 1
    assert portfolio.get_route_stats()["runs"] == 2
    portfolio.reset_wallet()
    assert portfolio.get_route_stats()["runs"] == 0


def test_failed_split_logs_no_route_run():
    with pytest.raises(portfolio.OrderError):
        portfolio.buy_split([(book(asks=[(1.0, 1e12)]), portfolio.START_THB * 2)], baseline=("X", 1.0))
    assert portfolio.get_route_stats()["runs"] == 0


# --- Maxbit key handling -------------------------------------------------
def test_innovestx_without_key_falls_back_and_explains(monkeypatch):
    monkeypatch.delenv("MAXBIT_API_KEY", raising=False) if hasattr(monkeypatch, "delenv") else None
    import os
    os.environ.pop("MAXBIT_API_KEY", None)
    ex = exchanges.MaxbitExchange(fallback=MockExchange("Maxbit", 0.0025, 0.001, 0.0))
    q = ex.get_price("BTC/THB")
    assert "mock fallback" in q.exchange and "MAXBIT_API_KEY" in ex.last_error


def test_innovestx_sends_key_header_only(monkeypatch):
    import os
    os.environ["MAXBIT_API_KEY"] = "dummy-test-key"
    seen = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        seen["headers"] = headers
        return FakeResp({"asks": [["101", "1"]], "bids": [["99", "1"]]})
    monkeypatch.setattr(exchanges.requests, "get", fake_get)
    try:
        q = exchanges.MaxbitExchange().get_price("BTC/THB")
    finally:
        os.environ.pop("MAXBIT_API_KEY", None)
    assert q.live and seen["headers"] == {"X-MBX-APIKEY": "dummy-test-key"}


def test_unconfigured_live_venues_are_left_out(monkeypatch):
    import os
    for v in ("MAXBIT_API_KEY", "INNOVESTX_API_KEY", "INNOVESTX_API_SECRET"):
        os.environ.pop(v, None)
    names = [e.name for e in exchanges.build_exchanges("Live data")]
    assert names == ["Bitkub", "Binance TH"]


def _clear_invx():
    import os
    for v in ("INNOVESTX_API_KEY", "INNOVESTX_API_SECRET", "INNOVESTX_BASE_URL", "INNOVESTX_DEPTH_PATH"):
        os.environ.pop(v, None)


def test_innovestx_needs_key_and_secret():
    import os
    _clear_invx()
    try:
        os.environ["INNOVESTX_API_KEY"] = "k"
        assert not exchanges.InnovestXExchange().configured()
        os.environ["INNOVESTX_API_SECRET"] = "s"
        ex = exchanges.InnovestXExchange()
        assert ex.configured()
        url, body = ex._params("BTC/THB")
        assert url == "https://api.innovestxonline.com/api/v1/digital-asset/orderbook/lvl2"
        assert body["symbol"] == "BTCTHB"
    finally:
        _clear_invx()


INVX_BOOK = {"code": "0000", "message": "SUCCESS", "data": [
    {"actionType": 0, "price": "99", "quantity": "1", "side": 0},
    {"actionType": 0, "price": "98", "quantity": "1", "side": 0},
    {"actionType": 0, "price": "101", "quantity": "2", "side": 1},
    {"actionType": 0, "price": "102", "quantity": "1", "side": 1},
    {"actionType": 2, "price": "100", "quantity": "9", "side": 1}]}      # deletion: must be ignored


def test_innovestx_signs_post_and_parses_book(monkeypatch):
    import os, hmac, hashlib, json
    _clear_invx()
    os.environ["INNOVESTX_API_KEY"], os.environ["INNOVESTX_API_SECRET"] = "mykey", "mysecret"
    seen = {}
    def fake_post(url, data=None, headers=None, timeout=None):
        if url.endswith("/orderbook/lvl2"):                          # ignore the fee-tier call
            seen.update(url=url, data=data, headers=headers)
        return FakeResp(INVX_BOOK)
    monkeypatch.setattr(exchanges.requests, "post", fake_post)
    try:
        q = exchanges.InnovestXExchange().get_price("BTC/THB")
    finally:
        _clear_invx()
    assert (q.live, q.bid, q.ask) == (True, 99, 101) and len(q.asks) == 2
    h = seen["headers"]
    assert json.loads(seen["data"]) == {"symbol": "BTCTHB", "depth": 100}
    to_sign = ("mykey" + "POST" + "api.innovestxonline.com" + "/api/v1/digital-asset/orderbook/lvl2" + ""
               + "application/json" + h["X-INVX-REQUEST-UID"] + h["X-INVX-TIMESTAMP"] + seen["data"])
    assert h["X-INVX-SIGNATURE"] == hmac.new(b"mysecret", to_sign.encode(), hashlib.sha256).hexdigest()
    assert "mysecret" not in json.dumps(h)                          # secret signs, never travels


def test_innovestx_error_code_falls_back_with_reason(monkeypatch):
    import os
    _clear_invx()
    os.environ["INNOVESTX_API_KEY"], os.environ["INNOVESTX_API_SECRET"] = "k", "s"
    monkeypatch.setattr(exchanges.requests, "post",
                        lambda *a, **k: FakeResp({"code": "4003", "message": "Invalid IP Whitelist"}))
    try:
        ex = exchanges.InnovestXExchange(fallback=MockExchange("InnovestX", 0.0025, 0.001, 0.0))
        q = ex.get_price("BTC/THB")
    finally:
        _clear_invx()
    assert "mock fallback" in q.exchange and "4003" in ex.last_error and "whitelist" in ex.last_error.lower()


# --- InnovestX live fee --------------------------------------------------
def _invx_post(fee_resp, calls=None):
    def fake_post(url, data=None, headers=None, timeout=None):
        if calls is not None:
            calls.append(url)
        return FakeResp(fee_resp if url.endswith("/symbol/fee/tier") else INVX_BOOK)
    return fake_post


def _with_invx_env():
    import os
    _clear_invx()
    os.environ["INNOVESTX_API_KEY"], os.environ["INNOVESTX_API_SECRET"] = "k", "s"


def test_innovestx_uses_live_percentage_fee(monkeypatch):
    _with_invx_env()
    fee = {"code": "0000", "message": "SUCCESS", "data": {"symbol": "BTCTHB", "feeAmount": "0.25", "feeType": "Percentage"}}
    monkeypatch.setattr(exchanges.requests, "post", _invx_post(fee))
    try:
        ex = exchanges.InnovestXExchange()
        q = ex.get_price("BTC/THB")
    finally:
        _clear_invx()
    assert q.fee_rate == pytest.approx(0.0025) and ex.fee_source == "live"


def test_innovestx_fee_accepts_fraction_form(monkeypatch):
    _with_invx_env()
    fee = {"code": "0000", "data": [{"feeAmount": "0.0015", "feeType": "Percentage"}]}
    monkeypatch.setattr(exchanges.requests, "post", _invx_post(fee))
    try:
        q = exchanges.InnovestXExchange().get_price("BTC/THB")
    finally:
        _clear_invx()
    assert q.fee_rate == pytest.approx(0.0015)


def test_innovestx_flat_or_failed_fee_keeps_placeholder(monkeypatch):
    _with_invx_env()
    fee = {"code": "0000", "data": {"feeAmount": "10", "feeType": "FlatRate"}}
    monkeypatch.setattr(exchanges.requests, "post", _invx_post(fee))
    try:
        ex = exchanges.InnovestXExchange()
        q = ex.get_price("BTC/THB")
    finally:
        _clear_invx()
    assert q.fee_rate == 0.0025 and ex.fee_source == "placeholder" and "FlatRate" in ex.fee_note


def test_innovestx_fee_is_cached(monkeypatch):
    _with_invx_env()
    calls = []
    fee = {"code": "0000", "data": {"feeAmount": "0.25", "feeType": "Percentage"}}
    monkeypatch.setattr(exchanges.requests, "post", _invx_post(fee, calls))
    try:
        exchanges.InnovestXExchange().get_price("BTC/THB")
        exchanges._CACHE.clear()                                    # force a new order-book call
        exchanges.InnovestXExchange().get_price("BTC/THB")
    finally:
        _clear_invx()
    assert sum(c.endswith("/symbol/fee/tier") for c in calls) == 1


def test_innovestx_fee_env_override(monkeypatch):
    import os
    _with_invx_env()
    os.environ["INNOVESTX_FEE_RATE"] = "0.0018"
    calls = []
    monkeypatch.setattr(exchanges.requests, "post", _invx_post({}, calls))
    try:
        q = exchanges.InnovestXExchange().get_price("BTC/THB")
    finally:
        _clear_invx()
        os.environ.pop("INNOVESTX_FEE_RATE", None)
    assert q.fee_rate == 0.0018 and not any(c.endswith("/symbol/fee/tier") for c in calls)


# --- Per-venue fee override ----------------------------------------------
def test_fee_env_override_for_bitkub(monkeypatch):
    import os
    data = {"result": {"asks": [[101, 1]], "bids": [[99, 1]]}}
    monkeypatch.setattr(exchanges.requests, "get", lambda *a, **k: FakeResp(data))
    os.environ["BITKUB_FEE_RATE"] = "0.0020"
    try:
        ex = BitkubExchange()
        q = ex.get_price("BTC/THB")
        assert q.fee_rate == 0.0020 and ex.fee_source == "env"
        os.environ["BITKUB_FEE_RATE"] = "0.25"                       # percent typo: must be rejected
        exchanges._CACHE.clear()
        ex2 = BitkubExchange()
        q2 = ex2.get_price("BTC/THB")
        assert q2.fee_rate == 0.0025 and ex2.fee_source == "default" and "ignored" in ex2.fee_note
    finally:
        os.environ.pop("BITKUB_FEE_RATE", None)


# --- Fee source / error carried on the Quote -----------------------------
def test_live_quote_carries_fee_source(monkeypatch):
    data = {"result": {"asks": [[101, 1]], "bids": [[99, 1]]}}
    monkeypatch.setattr(exchanges.requests, "get", lambda *a, **k: FakeResp(data))
    q = BitkubExchange().get_price("BTC/THB")
    assert q.fee_source == "default" and q.error == ""


def test_fallback_quote_carries_error_and_mock_fee_source(monkeypatch):
    def boom(*a, **k): raise ConnectionError("down")
    monkeypatch.setattr(exchanges.requests, "get", boom)
    q = BitkubExchange(fallback=MockExchange("Bitkub", 0.0025, 0.001, 0.0)).get_price("BTC/THB")
    assert q.fee_source == "mock" and "down" in q.error


def test_mock_quote_defaults_to_mock_fee_source():
    assert build_mock_exchanges()[0].get_price("BTC/THB").fee_source == "mock"
