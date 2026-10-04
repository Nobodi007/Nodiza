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
        for asset in ASSETS:
            start = START_THB if asset == "THB" else 0.0
            conn.execute("INSERT OR IGNORE INTO balances (asset, amount) VALUES (?, ?)", (asset, start))


def reset_wallet() -> None:
    """Back to ฿1,000,000 and an empty history."""
    with db() as conn:
        conn.execute("DELETE FROM orders")
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
    conn.execute(
        """INSERT INTO orders (created_at, symbol, side, exchange, coins, price, fee_thb, total_thb, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Filled (mock)')""",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), quote.symbol, side, quote.exchange,
         coins, price, fee, total_thb),
    )


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
