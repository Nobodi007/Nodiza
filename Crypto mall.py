"""
₿ Crypto Mall — V0.4 Live market data + Smart Routing (paper).
Run with:  streamlit run app.py
"""
import math
import time

import pandas as pd
import streamlit as st

import portfolio
from exchanges import build_exchanges, fetch_quotes

st.set_page_config(page_title="Crypto Mall", page_icon="₿", layout="wide")
portfolio.init_db()

SYMBOLS = ["BTC/THB", "ETH/THB"]
FEE_SOURCE_LABEL = {
    "live": "Live (from exchange API)",
    "env": "Your setting (.env / Secrets)",
    "default": "Built-in default",
    "placeholder": "Built-in default",
    "mock": "Simulated",
}

# Streamlit re-runs this whole file on every click, so prices live in
# session_state and only change when you press "Refresh prices".
if "quotes" not in st.session_state:
    st.session_state.quotes = {}

with st.sidebar:
    st.header("₿ Crypto Mall")
    mode = st.radio("Data source", ["Mock", "Live data"], horizontal=True,
                    help="Live data = real order books, paper money only.")
    if st.session_state.get("mode") != mode:      # switched mode -> drop old prices
        st.session_state.mode = mode
        st.session_state.quotes = {}
    symbol = st.selectbox("Pair", SYMBOLS)
    if st.button("Refresh prices"):
        st.session_state.quotes = {}
    st.divider()
    allow_reset = st.checkbox("I want to reset the wallet")
    if st.button("Reset to ฿1,000,000", disabled=not allow_reset):
        portfolio.reset_wallet()
        st.session_state.flash = "Wallet reset. History cleared."
        st.rerun()

EXCHANGES = build_exchanges(mode)

for s in SYMBOLS:
    if s not in st.session_state.quotes:
        st.session_state.quotes[s] = fetch_quotes(EXCHANGES, s)

STALE_AFTER = 30  # seconds; live quotes older than this can't be traded
quote_age = time.time() - min(q.ts for q in st.session_state.quotes[symbol])
stale = mode == "Live data" and quote_age > STALE_AFTER

# Read errors from the cached quotes, not from EXCHANGES: those objects are rebuilt on every rerun,
# so their last_error would be empty after any click while the prices on screen are still mock.
# getattr: quotes cached in session_state before an update may lack the newer fields.
errors = sorted({f"{q.exchange.replace(' (mock fallback)', '')}: {getattr(q, 'error', '')}"
                 for qs in st.session_state.quotes.values() for q in qs if getattr(q, "error", "")})

quotes = st.session_state.quotes[symbol]
by_name = {q.exchange: q for q in quotes}
coin = symbol.split("/")[0]
balances = portfolio.get_balances()

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))

# --- Header + wallet --------------------------------------------------------
st.title("₿ Crypto Mall")
if mode == "Mock":
    st.caption("Mock mode: simulated prices, wallet and orders. No real money, no real exchange connection.")
else:
    st.caption("Live data: real order books, paper money only. No orders are sent to any exchange.")
    if errors:
        st.warning("Some exchanges fell back to mock prices:\n\n" + "\n\n".join(errors))
    st.caption(f"Prices fetched {quote_age:.0f}s ago.")
    if stale:
        st.error(f"Prices are older than {STALE_AFTER}s. Press 'Refresh prices' before placing an order.")

best_bids = {a: max(q.bid for q in st.session_state.quotes[f"{a}/THB"]) for a in ("BTC", "ETH")}
total_value = balances["THB"] + sum(balances[a] * best_bids[a] for a in best_bids)

w1, w2, w3, w4 = st.columns(4)
w1.metric("Cash (THB)", f"฿{balances['THB']:,.2f}")
w2.metric("BTC", f"{balances['BTC']:.8f}")
w3.metric("ETH", f"{balances['ETH']:.8f}")
w4.metric("Estimated total", f"฿{total_value:,.0f}", f"{total_value - portfolio.START_THB:+,.0f} vs start")

trade_tab, portfolio_tab, history_tab = st.tabs(["Trade", "Portfolio", "Order history"])

# --- Trade ------------------------------------------------------------------
with trade_tab:
    # Top-of-book leaders are useful reference points; routing below is sized.
    best_buy = min(quotes, key=lambda q: q.effective_buy_price)
    best_sell = max(quotes, key=lambda q: q.effective_sell_price)
    c1, c2 = st.columns(2)
    c1.metric("Cheapest to buy (after fee)", best_buy.exchange,
              f"฿{best_buy.effective_buy_price:,.0f} per {coin}", delta_color="off")
    c2.metric("Best to sell (after fee)", best_sell.exchange,
              f"฿{best_sell.effective_sell_price:,.0f} per {coin}", delta_color="off")

    st.dataframe(
        pd.DataFrame({
            "Exchange": [q.exchange for q in quotes],
            "Bid (you sell at)": [f"฿{q.bid:,.0f}" for q in quotes],
            "Ask (you buy at)": [f"฿{q.ask:,.0f}" for q in quotes],
            "Spread": [f"฿{q.spread:,.0f} ({q.spread_pct:.2%})" for q in quotes],
            "Fee": [f"{q.fee_rate:.2%}" for q in quotes],
            "Fee source": [FEE_SOURCE_LABEL.get(getattr(q, "fee_source", "mock"), "-") for q in quotes],
            "Buy cost after fee": [f"฿{q.effective_buy_price:,.0f}" for q in quotes],
            "Sell proceeds after fee": [f"฿{q.effective_sell_price:,.0f}" for q in quotes],
        }),
        hide_index=True, width="stretch",
    )
    if mode == "Live data":
        fee_notes = [f"**{q.exchange}**: {q.fee_note}" for q in quotes if getattr(q, "fee_note", "")]
        if fee_notes:
            st.caption("Fee details  \n" + "  \n".join(fee_notes))

    with st.expander(f"Order Book & Liquidity — {symbol}", expanded=True):
        depth_exchange = st.selectbox("View depth", [q.exchange for q in quotes], key=f"depth_{symbol}")
        depth_q = by_name[depth_exchange]
        depth_rows = ([{"Side": "ASK", "Price (THB)": px, "Quantity": qty} for px, qty in depth_q.asks]
                      + [{"Side": "BID", "Price (THB)": px, "Quantity": qty} for px, qty in depth_q.bids])
        st.dataframe(pd.DataFrame(depth_rows), hide_index=True, width="stretch")
        if mode == "Mock":
            st.caption("Order Book เป็นข้อมูลจำลอง 5 ระดับราคา; การซื้อขายจะไล่กินสภาพคล่องตามระดับราคาและบันทึกราคาเฉลี่ยถ่วงน้ำหนัก")
        else:
            st.caption("Order Book จริงจากแต่ละ exchange (จำนวนระดับราคาขึ้นกับที่เขาส่งมา) ยกเว้นแถวที่ติดป้าย (mock fallback) "
                       "ซึ่งเป็นข้อมูลจำลอง; การซื้อขายเป็นเงินกระดาษ ไล่กินสภาพคล่องตามระดับราคาและบันทึกราคาเฉลี่ยถ่วงน้ำหนัก")

    if mode == "Mock":
        st.subheader("Smart Order Routing (Mock)")
        st.caption("ระบบเปรียบเทียบผลลัพธ์ของคำสั่งขนาดที่ระบุ โดยรวม depth และค่าธรรมเนียมจากสมุดคำสั่งจำลอง")
    else:
        st.subheader("Smart Order Routing (Live data, paper)")
        st.caption("ระบบเปรียบเทียบผลลัพธ์ของคำสั่งขนาดที่ระบุ โดยรวม depth และค่าธรรมเนียมจากสมุดคำสั่งจริง "
                   "แต่เป็นเงินกระดาษ ไม่มีคำสั่งถูกส่งไปที่ exchange จริง")

    def allocate_buy(thb_amount):
        # Greedy allocation across every venue's ask levels, best fee-adjusted
        # marginal cost first. Aggregate cash per venue; quote.buy() then walks
        # that venue's depth in price order during execution.
        chunks = []
        for q in quotes:
            levels = q.asks or [(q.ask, float("inf"))]
            for price, qty in levels:
                cash_capacity = qty * price / (1 - q.fee_rate)
                chunks.append((price / (1 - q.fee_rate), q, cash_capacity))
        chunks.sort(key=lambda x: x[0])
        remaining = thb_amount
        allocation = {}
        for _, q, capacity in chunks:
            take = min(remaining, capacity)
            if take > 1e-8:
                allocation[q.exchange] = allocation.get(q.exchange, 0.0) + take
                remaining -= take
            if remaining <= 1e-7:
                break
        if remaining > 1e-5:
            return None
        legs = [(by_name[name], amount) for name, amount in allocation.items()]
        try:
            results = [(q, amount, *q.buy(amount)) for q, amount in legs]
        except ValueError:
            return None
        return legs, results, sum(row[2] for row in results), sum(row[3] for row in results)

    def allocate_sell(coin_amount):
        chunks = []
        for q in quotes:
            levels = q.bids or [(q.bid, float("inf"))]
            for price, qty in levels:
                chunks.append((price * (1 - q.fee_rate), q, qty))
        chunks.sort(key=lambda x: x[0], reverse=True)
        remaining = coin_amount
        allocation = {}
        for _, q, capacity in chunks:
            take = min(remaining, capacity)
            if take > 1e-12:
                allocation[q.exchange] = allocation.get(q.exchange, 0.0) + take
                remaining -= take
            if remaining <= 1e-10:
                break
        if remaining > 1e-8:
            return None
        legs = [(by_name[name], amount) for name, amount in allocation.items()]
        try:
            results = [(q, amount, *q.sell(amount)) for q, amount in legs]
        except ValueError:
            return None
        return legs, results, sum(row[2] for row in results), sum(row[3] for row in results)

    route_buy_col, route_sell_col = st.columns(2)
    with route_buy_col:
        route_thb = st.number_input(
            "Auto Buy amount (THB)", min_value=0.0, value=50_000.0,
            step=1_000.0, key=f"route_buy_thb_{symbol}"
        )
        buy_route = allocate_buy(route_thb) if route_thb > 0 else None
        if buy_route:
            buy_legs, buy_results, route_coins, route_fee = buy_route
            st.write(f"Split route for ฿{route_thb:,.2f}: **{len(buy_legs)} exchange(s)**")
            st.dataframe(pd.DataFrame([{"Exchange": q.exchange, "Spend (THB)": amount, "Estimated coins": coins}
                                       for q, amount, coins, _ in buy_results]), hide_index=True, width="stretch")
            buy_avg = route_thb / route_coins if route_coins else 0.0
            single_buy = []
            for q in quotes:
                try:
                    one_coins, one_fee = q.buy(route_thb)
                    single_buy.append((one_coins, q, one_fee))
                except ValueError:
                    pass
            buy_single_best = max(single_buy, key=lambda row: row[0]) if single_buy else None
            st.caption(f"ประมาณได้รับ {route_coins:.8f} {coin} หลังหักค่าธรรมเนียม | Fee รวม ฿{route_fee:,.2f}")
            st.metric("Execution average buy price", f"฿{buy_avg:,.2f}/{coin}")
            if buy_single_best:
                gain = route_coins - buy_single_best[0]
                st.caption(f"เทียบกับกระดานเดียวที่ดีที่สุด: {gain:+.8f} {coin} ({gain / buy_single_best[0] * 100:+.3f}%)")
        else:
            buy_legs = []
            st.warning("สภาพคล่อง Ask รวมของทุก Exchange ไม่เพียงพอสำหรับยอดนี้")
        if st.button(f"Auto Buy {coin}", type="primary", disabled=stale or not (buy_route and 0 < route_thb <= balances["THB"]), key=f"auto_buy_{symbol}"):
            try:
                baseline = (buy_single_best[1].exchange, buy_single_best[0]) if buy_route and buy_single_best else None
                received = portfolio.buy_split(buy_legs, baseline)
                st.session_state.flash = f"Split-routed buy: {received:.8f} {coin} across {len(buy_legs)} exchange(s)."
                st.rerun()
            except portfolio.OrderError as e:
                st.error(str(e))

    with route_sell_col:
        route_held = balances[coin]
        route_sell_amount = st.number_input(
            f"Auto Sell amount ({coin})", min_value=0.0,
            value=math.floor(route_held * 1e8) / 1e8,
            step=0.001, format="%.8f", key=f"route_sell_amount_{symbol}"
        )
        sell_route = allocate_sell(route_sell_amount) if route_sell_amount > 0 else None
        if sell_route:
            sell_legs, sell_results, route_proceeds, route_sell_fee = sell_route
            st.write(f"Split route for {route_sell_amount:.8f} {coin}: **{len(sell_legs)} exchange(s)**")
            st.dataframe(pd.DataFrame([{"Exchange": q.exchange, "Coins": amount, "Estimated THB": proceeds}
                                       for q, amount, proceeds, _ in sell_results]), hide_index=True, width="stretch")
            sell_avg = route_proceeds / route_sell_amount if route_sell_amount else 0.0
            single_sell = []
            for q in quotes:
                try:
                    one_thb, one_fee = q.sell(route_sell_amount)
                    single_sell.append((one_thb, q, one_fee))
                except ValueError:
                    pass
            sell_single_best = max(single_sell, key=lambda row: row[0]) if single_sell else None
            st.caption(f"ประมาณรับ ฿{route_proceeds:,.2f} หลังหักค่าธรรมเนียม | Fee รวม ฿{route_sell_fee:,.2f}")
            st.metric("Execution average sell price", f"฿{sell_avg:,.2f}/{coin}")
            if sell_single_best:
                improvement = route_proceeds - sell_single_best[0]
                st.caption(f"เทียบกับกระดานเดียวที่ดีที่สุด: {improvement:+,.2f} THB ({improvement / sell_single_best[0] * 100:+.3f}%)")
        else:
            sell_legs = []
            st.warning("สภาพคล่อง Bid รวมของทุก Exchange ไม่เพียงพอสำหรับจำนวนนี้")
        if st.button(f"Auto Sell {coin}", type="primary", disabled=stale or not (sell_route and 0 < route_sell_amount <= route_held + 1e-8), key=f"auto_sell_{symbol}"):
            try:
                baseline = (sell_single_best[1].exchange, sell_single_best[0]) if sell_route and sell_single_best else None
                received_thb = portfolio.sell_split(sell_legs, baseline)
                st.session_state.flash = f"Split-routed sell across {len(sell_legs)} exchange(s): ฿{received_thb:,.2f}."
                st.rerun()
            except portfolio.OrderError as e:
                st.error(str(e))

    st.divider()
    st.subheader(f"Manual order: {symbol}")
    buy_col, sell_col = st.columns(2)

    with buy_col:
        st.markdown("**Buy**")
        names = list(by_name)
        buy_ex = st.selectbox("Exchange", names, index=names.index(best_buy.exchange), key=f"buy_ex_{symbol}")
        thb = st.number_input("Amount to spend (THB)", min_value=0.0, value=50_000.0, step=1_000.0)
        got, fee = by_name[buy_ex].buy(thb) if thb > 0 else (0.0, 0.0)
        st.write(f"You receive about **{got:.8f} {coin}**, fee **฿{fee:,.2f}**.")
        enough = 0 < thb <= balances["THB"]
        if thb > balances["THB"]:
            st.warning(f"Not enough THB. You have ฿{balances['THB']:,.2f}.")
        if st.button(f"Confirm buy {coin}", type="primary", disabled=stale or not enough):
            try:
                coins = portfolio.buy(by_name[buy_ex], thb)
                st.session_state.flash = f"Bought {coins:.8f} {coin} on {buy_ex}."
                st.rerun()
            except portfolio.OrderError as e:
                st.error(str(e))

    with sell_col:
        st.markdown("**Sell**")
        held = balances[coin]
        st.caption(f"You hold {held:.8f} {coin}")
        sell_ex = st.selectbox("Exchange", names, index=names.index(best_sell.exchange), key=f"sell_ex_{symbol}")
        default_sell = math.floor(held * 1e8) / 1e8
        amount = st.number_input(f"Amount to sell ({coin})", min_value=0.0, value=default_sell,
                                 step=0.001, format="%.8f")
        back, fee = by_name[sell_ex].sell(amount) if amount > 0 else (0.0, 0.0)
        st.write(f"You receive about **฿{back:,.2f}**, fee **฿{fee:,.2f}**.")
        if amount > held + 1e-8:
            st.warning(f"Not enough {coin}.")
        can_sell = 0 < amount <= held + 1e-8
        if st.button(f"Confirm sell {coin}", type="primary", disabled=stale or not can_sell):
            try:
                thb_back = portfolio.sell(by_name[sell_ex], amount)
                st.session_state.flash = f"Sold {amount:.8f} {coin} on {sell_ex} for ฿{thb_back:,.2f}."
                st.rerun()
            except portfolio.OrderError as e:
                st.error(str(e))

# --- Portfolio --------------------------------------------------------------
with portfolio_tab:
    rows = [("THB", balances["THB"], balances["THB"])]
    rows += [(a, balances[a], balances[a] * best_bids[a]) for a in ("BTC", "ETH")]
    st.dataframe(
        pd.DataFrame({
            "Asset": [r[0] for r in rows],
            "Amount": [f"฿{r[1]:,.2f}" if r[0] == "THB" else f"{r[1]:.8f}" for r in rows],
            "Estimated value": [f"฿{r[2]:,.2f}" for r in rows],
            "Share": [f"{r[2] / total_value:.1%}" if total_value > 0 else "0.0%" for r in rows],
        }),
        hide_index=True, width="stretch",
    )
    st.caption("Coins are valued at the best bid across the exchanges shown, before fees.")

# --- History ----------------------------------------------------------------
with history_tab:
    orders = portfolio.get_orders()

    def slippage_pct(o):
        """Cost vs top of book, in %. Positive = worse than the best quote shown."""
        if not o["ref_price"]:
            return None
        diff = o["price"] / o["ref_price"] - 1
        return diff if o["side"] == "Buy" else -diff

    slips = [s for s in map(slippage_pct, orders) if s is not None]
    lats = [o["latency_ms"] for o in orders if o["latency_ms"]]
    if slips:
        m1, m2, m3 = st.columns(3)
        m1.metric("Avg slippage vs top of book", f"{sum(slips) / len(slips):.4%}")
        m2.metric("Worst slippage", f"{max(slips):.4%}")
        m3.metric("Avg quote latency", f"{sum(lats) / len(lats):.0f} ms" if lats else "n/a (mock)")
    rs = portfolio.get_route_stats(live=(mode == "Live data"))
    st.subheader("Smart routing vs best single exchange")
    if rs["compared"]:
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Routed orders compared", rs["compared"])
        r2.metric("Split beat single", f"{rs['wins']}/{rs['compared']}")
        r3.metric("Total gained", f"฿{rs['total_gain_thb']:,.2f}")
        r4.metric("Avg improvement", f"{rs['avg_gain_pct']:.4%}")
        st.caption(f"Counts only {mode} orders. Baseline = the best single exchange for the same order at the same moment."
                   + (f" {rs['split_only']} order(s) had no single exchange with enough liquidity." if rs["split_only"] else ""))
    else:
        st.caption(f"No Auto Buy / Auto Sell orders in {mode} mode yet.")
    st.subheader("Order history")
    if orders:
        st.dataframe(
            pd.DataFrame({
                "Time": [o["created_at"] for o in orders],
                "Pair": [o["symbol"] for o in orders],
                "Side": [o["side"] for o in orders],
                "Exchange": [o["exchange"] for o in orders],
                "Coins": [f"{o['coins']:.8f}" for o in orders],
                "Price": [f"฿{o['price']:,.0f}" for o in orders],
                "Fee": [f"฿{o['fee_thb']:,.2f}" for o in orders],
                "THB spent / received": [f"฿{o['total_thb']:,.2f}" for o in orders],
                "Slippage": [f"{s:.4%}" if (s := slippage_pct(o)) is not None else "-" for o in orders],
                "Latency": [f"{o['latency_ms']:.0f} ms" if o["latency_ms"] else "-" for o in orders],
                "Status": [o["status"] for o in orders],
            }),
            hide_index=True, width="stretch",
        )
    else:
        st.caption(f"No orders yet. Place a {'mock' if mode == 'Mock' else 'paper'} order on the Trade tab.")
