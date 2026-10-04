"""
₿ Crypto Mall — V0.3 Smart Routing (mock).
Run with:  streamlit run app.py
"""
import math

import pandas as pd
import streamlit as st

import portfolio
from exchanges import build_mock_exchanges

st.set_page_config(page_title="Crypto Mall", page_icon="₿", layout="wide")
portfolio.init_db()

EXCHANGES = build_mock_exchanges()
SYMBOLS = ["BTC/THB", "ETH/THB"]

# Streamlit re-runs this whole file on every click, so prices live in
# session_state and only change when you press "Refresh prices".
if "quotes" not in st.session_state:
    st.session_state.quotes = {}

with st.sidebar:
    st.header("₿ Crypto Mall")
    symbol = st.selectbox("Pair", SYMBOLS)
    if st.button("Refresh prices"):
        st.session_state.quotes = {}
    st.divider()
    allow_reset = st.checkbox("I want to reset the wallet")
    if st.button("Reset to ฿1,000,000", disabled=not allow_reset):
        portfolio.reset_wallet()
        st.session_state.flash = "Wallet reset. History cleared."
        st.rerun()

for s in SYMBOLS:
    if s not in st.session_state.quotes:
        st.session_state.quotes[s] = [ex.get_price(s) for ex in EXCHANGES]

quotes = st.session_state.quotes[symbol]
by_name = {q.exchange: q for q in quotes}
coin = symbol.split("/")[0]
balances = portfolio.get_balances()

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))

# --- Header + wallet --------------------------------------------------------
st.title("₿ Crypto Mall")
st.caption("One place. Multiple exchanges.  Mock mode: simulated prices, wallet and orders. "
           "No real money and no real exchange connection.")

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
            "Buy cost after fee": [f"฿{q.effective_buy_price:,.0f}" for q in quotes],
            "Sell proceeds after fee": [f"฿{q.effective_sell_price:,.0f}" for q in quotes],
        }),
        hide_index=True, width="stretch",
    )

    st.subheader("Smart Order Routing (Mock)")
    st.caption("ระบบเลือก Exchange ที่ให้ผลลัพธ์สุทธิดีที่สุดจากราคาและค่าธรรมเนียมที่มีอยู่ใน Mock quotes")

    route_buy_col, route_sell_col = st.columns(2)
    with route_buy_col:
        route_thb = st.number_input(
            "Auto Buy amount (THB)", min_value=0.0, value=50_000.0,
            step=1_000.0, key=f"route_buy_thb_{symbol}"
        )
        route_coins, route_fee = best_buy.buy(route_thb) if route_thb > 0 else (0.0, 0.0)
        st.write(f"Best route: **{best_buy.exchange}**")
        st.caption(f"ประมาณ {route_coins:.8f} {coin} หลังหักค่าธรรมเนียม")
        if st.button(f"Auto Buy {coin}", type="primary", disabled=not (0 < route_thb <= balances["THB"]), key=f"auto_buy_{symbol}"):
            try:
                received = portfolio.buy(best_buy, route_thb)
                st.session_state.flash = f"Auto-routed buy: {received:.8f} {coin} via {best_buy.exchange}."
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
        route_proceeds, route_sell_fee = best_sell.sell(route_sell_amount) if route_sell_amount > 0 else (0.0, 0.0)
        st.write(f"Best route: **{best_sell.exchange}**")
        st.caption(f"ประมาณรับ ฿{route_proceeds:,.2f} หลังหักค่าธรรมเนียม")
        if st.button(f"Auto Sell {coin}", type="primary", disabled=not (0 < route_sell_amount <= route_held + 1e-8), key=f"auto_sell_{symbol}"):
            try:
                received_thb = portfolio.sell(best_sell, route_sell_amount)
                st.session_state.flash = f"Auto-routed sell via {best_sell.exchange}: ฿{received_thb:,.2f}."
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
        if st.button(f"Confirm buy {coin}", type="primary", disabled=not enough):
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
        if st.button(f"Confirm sell {coin}", type="primary", disabled=not can_sell):
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
    st.caption("Coins are valued at the best bid across the three exchanges, before fees.")

# --- History ----------------------------------------------------------------
with history_tab:
    orders = portfolio.get_orders()
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
                "Status": [o["status"] for o in orders],
            }),
            hide_index=True, width="stretch",
        )
    else:
        st.caption("No orders yet. Place a mock order on the Trade tab.")
