"""
portfolio.py — the mock wallet and order book-keeping for Crypto Mall (V0.2).

Everything is stored in a small SQLite file (crypto_mall.db) next to this
script, so your wallet and history survive restarting the app.

Two tables:
    balances  one row per asset      THB | BTC | ETH
    orders    one row per fill       newest orders are shown first

Rule of thumb: an order either happens completely (balances change AND the
order is saved) or not at all. `db()` makes sure of that with a transaction.
"""
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from exchanges import Quote

DB_PATH = Path(os.environ.get("CRYPTO_MALL_DB", str(Path(__file__).with_name("crypto_mall.db")))).expanduser()
START_THB = 1_000_000.0
ASSETS = ["THB", "BTC", "ETH"]


class OrderError(Exception):
    """Raised when an order cannot be filled (e.g. not enough balance)."""


@contextmanager
def db():
    """Open the database; commit if everything worked, roll back if not."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the tables on first run. Safe to call on every app start."""
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS balances (asset TEXT PRIMARY KEY, amount REAL NOT NULL)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   side TEXT NOT NULL,
                   exchange TEXT NOT NULL,
                   coins REAL NOT NULL,
                   price REAL NOT NULL,
                   fee_thb REAL NOT NULL,
                   total_thb REAL NOT NULL,
                   status TEXT NOT NULL)"""
        )
        # Migration: older databases lack the execution-quality columns.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        for col in ("ref_price", "latency_ms"):
            if col not in existing:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} REAL NOT NULL DEFAULT 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS route_runs (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   created_at TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   side TEXT NOT NULL,
                   venues INTEGER NOT NULL,
                   notional_thb REAL NOT NULL,
                   single_exchange TEXT,
                   gain_thb REAL,
                   live INTEGER NOT NULL DEFAULT 0)"""
        )
        for asset in ASSETS:
            start = START_THB if asset == "THB" else 0.0
            conn.execute("INSERT OR IGNORE INTO balances (asset, amount) VALUES (?, ?)", (asset, start))


def reset_wallet() -> None:
    """Back to ฿1,000,000 and an empty history."""
    with db() as conn:
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM route_runs")
        for asset in ASSETS:
            conn.execute("UPDATE balances SET amount = ? WHERE asset = ?",
                         (START_THB if asset == "THB" else 0.0, asset))


def get_balances() -> dict[str, float]:
    with db() as conn:
        return {r["asset"]: r["amount"] for r in conn.execute("SELECT asset, amount FROM balances")}


def get_orders() -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY id DESC")]


# --- internal helpers -------------------------------------------------------
def _balance(conn, asset: str) -> float:
    return conn.execute("SELECT amount FROM balances WHERE asset = ?", (asset,)).fetchone()["amount"]


def _adjust(conn, asset: str, delta: float) -> None:
    conn.execute("UPDATE balances SET amount = amount + ? WHERE asset = ?", (delta, asset))


def _record(conn, quote: Quote, side: str, coins: float, price: float, fee: float, total_thb: float) -> None:
    ref = quote.ask if side == "Buy" else quote.bid     # top of book when the quote was taken
    conn.execute(
        """INSERT INTO orders (created_at, symbol, side, exchange, coins, price, fee_thb, total_thb, status,
                               ref_price, latency_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), quote.symbol, side, quote.exchange,
         coins, price, fee, total_thb, "Filled (paper)" if quote.live else "Filled (mock)",
         ref, quote.latency_ms),
    )


def _record_route(conn, quotes: list[Quote], side: str, notional: float,
                  single_exchange: str | None, gain_thb: float | None) -> None:
    """Log one split-routed order next to the best single-venue alternative."""
    conn.execute(
        """INSERT INTO route_runs (created_at, symbol, side, venues, notional_thb, single_exchange, gain_thb, live)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), quotes[0].symbol, side, len(quotes),
         notional, single_exchange, gain_thb, int(all(q.live for q in quotes))),
    )


def get_route_runs(live: bool | None = None) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM route_runs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows if live is None or bool(r["live"]) == live]


def get_route_stats(live: bool | None = None) -> dict:
    """Split routing vs the best single exchange, summed over all routed orders.

    live=True -> only runs on real market data; False -> only mock; None -> all.
    Runs with no single-venue alternative (not enough liquidity on any one venue)
    are counted in `split_only` and left out of the comparison.
    """
    runs = get_route_runs(live)
    cmp = [r for r in runs if r["gain_thb"] is not None]
    gain = sum(r["gain_thb"] for r in cmp)
    notional = sum(r["notional_thb"] for r in cmp)
    return {
        "runs": len(runs),
        "compared": len(cmp),
        "split_only": len(runs) - len(cmp),
        "wins": sum(1 for r in cmp if r["gain_thb"] > 1e-9),
        "total_gain_thb": gain,
        "avg_gain_pct": gain / notional if notional else 0.0,
    }


# --- the two things you can do ---------------------------------------------
def buy(quote: Quote, thb: float) -> float:
    """Spend `thb` on this exchange. Returns the coins received."""
    if thb <= 0:
        raise OrderError("Enter an amount greater than zero.")
    try:
        coins, fee = quote.buy(thb)
    except ValueError as e:
        raise OrderError(str(e)) from e
    coins = math.floor(coins * 1e8) / 1e8          # exchanges trade in 8 decimals
    asset = quote.symbol.split("/")[0]
    with db() as conn:
        have = _balance(conn, "THB")
        if thb > have + 1e-9:
            raise OrderError(f"Not enough THB. You have ฿{have:,.2f}.")
        _adjust(conn, "THB", -thb)
        _adjust(conn, asset, coins)
        _record(conn, quote, "Buy", coins, thb * (1 - quote.fee_rate) / coins if coins else quote.ask, fee, thb)
    return coins


def sell(quote: Quote, coins: float) -> float:
    """Sell `coins` on this exchange. Returns the THB received."""
    if coins <= 0:
        raise OrderError("Enter an amount greater than zero.")
    asset = quote.symbol.split("/")[0]
    with db() as conn:
        have = _balance(conn, asset)
        if coins > have and coins - have < 1e-8:   # typed the balance rounded up
            coins = have
        if coins > have:
            raise OrderError(f"Not enough {asset}. You have {have:.8f}.")
        try:
            thb, fee = quote.sell(coins)
        except ValueError as e:
            raise OrderError(str(e)) from e
        _adjust(conn, asset, -coins)
        _adjust(conn, "THB", thb)
        _record(conn, quote, "Sell", coins, (thb + fee) / coins if coins else quote.bid, fee, thb)
    return thb


def buy_split(legs: list[tuple[Quote, float]], baseline: tuple[str, float] | None = None) -> float:
    """Execute multi-exchange buy legs atomically; each leg is (quote, THB).

    baseline = (exchange name, coins) the best single exchange would have given for the same THB.
    """
    legs = [(q, float(amount)) for q, amount in legs if amount > 1e-8]
    if not legs:
        raise OrderError("No executable buy allocation.")
    total_thb = sum(amount for _, amount in legs)
    asset = legs[0][0].symbol.split("/")[0]
    if any(q.symbol.split("/")[0] != asset for q, _ in legs):
        raise OrderError("All split legs must use the same asset.")
    prepared = []
    try:
        for quote, amount in legs:
            coins, fee = quote.buy(amount)
            coins = math.floor(coins * 1e8) / 1e8
            if coins <= 0:
                raise ValueError("Allocation is too small to fill.")
            avg = amount * (1 - quote.fee_rate) / coins
            prepared.append((quote, amount, coins, fee, avg))
    except ValueError as e:
        raise OrderError(str(e)) from e
    with db() as conn:
        have = _balance(conn, "THB")
        if total_thb > have + 1e-9:
            raise OrderError(f"Not enough THB. You have ฿{have:,.2f}.")
        _adjust(conn, "THB", -total_thb)
        total_coins = 0.0
        for quote, amount, coins, fee, avg in prepared:
            _adjust(conn, asset, coins)
            _record(conn, quote, "Buy", coins, avg, fee, amount)
            total_coins += coins
        gain = None
        if baseline:
            single_coins = math.floor(baseline[1] * 1e8) / 1e8      # same rounding as a real fill
            gain = (total_coins - single_coins) * (total_thb / total_coins)   # coins -> THB
        _record_route(conn, [q for q, *_ in prepared], "Buy", total_thb, baseline[0] if baseline else None, gain)
    return total_coins


def sell_split(legs: list[tuple[Quote, float]], baseline: tuple[str, float] | None = None) -> float:
    """Execute multi-exchange sell legs atomically; each leg is (quote, coins).

    baseline = (exchange name, net THB) the best single exchange would have paid for the same coins.
    """
    legs = [(q, float(amount)) for q, amount in legs if amount > 1e-12]
    if not legs:
        raise OrderError("No executable sell allocation.")
    asset = legs[0][0].symbol.split("/")[0]
    if any(q.symbol.split("/")[0] != asset for q, _ in legs):
        raise OrderError("All split legs must use the same asset.")
    prepared = []
    try:
        for quote, amount in legs:
            proceeds, fee = quote.sell(amount)
            prepared.append((quote, amount, proceeds, fee, (proceeds + fee) / amount))
    except ValueError as e:
        raise OrderError(str(e)) from e
    total_coins = sum(amount for _, amount, *_ in prepared)
    with db() as conn:
        have = _balance(conn, asset)
        if total_coins > have + 1e-8:
            raise OrderError(f"Not enough {asset}. You have {have:.8f}.")
        total_thb = 0.0
        for quote, amount, proceeds, fee, avg in prepared:
            _adjust(conn, asset, -amount)
            _adjust(conn, "THB", proceeds)
            _record(conn, quote, "Sell", amount, avg, fee, proceeds)
            total_thb += proceeds
        gain = (total_thb - baseline[1]) if baseline else None
        _record_route(conn, [q for q, *_ in prepared], "Sell", total_thb, baseline[0] if baseline else None, gain)
    return total_thb
