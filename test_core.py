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
