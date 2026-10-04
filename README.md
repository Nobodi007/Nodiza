# ₿ Crypto Mall (prototype)

One place, multiple Thai crypto exchanges: compare order books, route a single order across venues,
and measure execution quality. **Paper trading only. No orders are ever sent to an exchange and no real money moves.**

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
pytest -q          # run the tests
```
In the sidebar choose **Mock** (simulated prices) or **Live data** (real public order books, paper wallet).

## Structure
| File | Role |
|---|---|
| `exchanges.py` | `Quote` (price + depth maths), the `Exchange` interface, `MockExchange`, live adapters (`BitkubExchange`, `BinanceTHExchange`), parallel `fetch_quotes` |
| `portfolio.py` | SQLite mock wallet, orders, atomic split buy/sell, slippage + latency recording |
| `app.py` | Streamlit UI: quotes, smart order routing, manual orders, history |
| `tests/` | pytest suite for fees, depth walking, wallet atomicity, adapter fallback |

## How live data works
Each live adapter calls a public depth endpoint (no API key), normalises it into a `Quote`, and falls back to a mock
quote if the request fails (the UI shows a warning). Quotes older than 30 s cannot be traded. Fee rates in the adapters are
approximate; check each exchange's fee page.

## Roadmap
- [x] Mock exchanges, wallet, order history, order book, smart order routing
- [x] Real market data, paper trading, slippage/latency tracking
- [ ] More exchanges (Orbix, InnovestX, Maxbit)
- [ ] Split-vs-single-venue savings statistics
- [ ] Exchange testnet trading, user accounts, custody/compliance review
