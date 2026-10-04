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
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Quote:
    """One exchange's price for one trading pair."""
    exchange: str
    symbol: str
    bid: float       # highest price buyers pay  -> the price YOU get when you SELL
    ask: float       # lowest price sellers want -> the price YOU pay when you BUY
    fee_rate: float  # 0.0025 means 0.25%

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
        """Spend `thb`. Returns (coins received, fee in THB)."""
        fee = thb * self.fee_rate
        return (thb - fee) / self.ask, fee

    def sell(self, coins: float) -> tuple[float, float]:
        """Sell `coins`. Returns (THB received after fee, fee in THB)."""
        gross = coins * self.bid
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
        return Quote(self.name, symbol, bid=mid - half, ask=mid + half, fee_rate=self.fee_rate)


def build_mock_exchanges() -> list[Exchange]:
    """Three fake exchanges, each better at something different."""
    return [
        MockExchange("Exchange A", fee_rate=0.0025, spread_pct=0.0010, price_bias=0.0),
        MockExchange("Exchange B", fee_rate=0.0015, spread_pct=0.0020, price_bias=-0.0015),
        MockExchange("Exchange C", fee_rate=0.0030, spread_pct=0.0005, price_bias=0.0025),
    ]
