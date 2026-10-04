"""
exec_panel.py — the "Execution (testnet)" tab for Crypto Mall.

Binance TESTNET only (fake money). Without EXEC_ENABLED=1 every order is a DRY-RUN: with keys set,
the exchange validates the format/signature (POST /order/test) but nothing trades.
Call render() inside a Streamlit tab.
"""
import pandas as pd
import streamlit as st

import execution
from execution import ExecutionError

BUY_STEP, SELL_STEP = 10.0, 0.001


def _status_row(s: dict) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mode", "TESTNET (fake money)" if s["enabled"] else "DRY-RUN")
    c2.metric("API keys", "set" if s["configured"] else "not set")
    c3.metric("Per-order limit", f"{s['max_order']:,.0f} USDT")
    c4.metric("Used today", f"{float(s['used_today']):,.2f} / {float(s['max_daily']):,.0f} USDT")


def _kill_switch(killed: bool) -> None:
    if killed:
        st.error("KILL SWITCH IS ON: no orders can be placed.")
        if st.button("Release kill switch"):
            execution.set_kill(False)
            st.rerun()
    elif st.button("Activate kill switch", help="Blocks every order immediately."):
        execution.set_kill(True)
        st.rerun()


def _order_form(s: dict) -> None:
    st.subheader("Place a test order")
    if s["enabled"] and not s["configured"]:
        st.warning("EXEC_ENABLED is on but the testnet keys are not set, so orders will be refused.")
    col1, col2 = st.columns(2)
    sym = col1.selectbox("Symbol", execution.TESTNET_SYMBOLS, key="ex_sym")
    side = col2.radio("Side", ["BUY", "SELL"], horizontal=True, key="ex_side")
    base = sym[:-4]
    if side == "BUY":
        amount = st.number_input("Amount to spend (USDT)", min_value=0.0, value=20.0, step=BUY_STEP, key="ex_buy_amt")
    else:
        amount = st.number_input(f"Amount to sell ({base})", min_value=0.0, value=0.001, step=SELL_STEP,
                                 format="%.8f", key="ex_sell_amt")
    confirmed = True
    if s["enabled"]:
        confirmed = st.checkbox("I confirm: send this order to the Binance testnet", key="ex_confirm")
    label = "Send to testnet" if s["enabled"] else "Run dry-run"
    if st.button(label, type="primary", disabled=s["killed"] or amount <= 0 or not confirmed, key="ex_go"):
        try:
            with st.spinner("Talking to the exchange..."):
                r = execution.place_market_order(sym, side, amount)
        except ExecutionError as e:
            st.error(str(e))
            return
        except Exception as e:                         # network down, unexpected reply, ...
            st.error(f"Unexpected error: {type(e).__name__}: {e}")
            return
        _show_result(r)


def _show_result(r) -> None:
    summary = f"{r.mode} · {r.side} {r.symbol} · **{r.status}**"
    if r.status in ("FILLED", "DRY_RUN_VALIDATED"):
        st.success(summary)
    elif r.status in ("UNKNOWN", "PARTIALLY_FILLED"):
        st.warning(summary)
    elif r.status in ("REJECTED", "DRY_RUN_REJECTED"):
        st.error(summary)
    else:
        st.info(summary)
    if r.executed_qty:
        st.write(f"Executed {r.executed_qty} at average {r.avg_price:,.2f} (quote value {r.quote_qty:,.2f}). "
                 f"Fees: {r.fees or '-'}")
    if r.reconcile:
        (st.error if r.reconcile.startswith("MISMATCH") else st.caption)(f"Reconcile: {r.reconcile}")
    if r.note:
        st.caption(r.note)
    for w in r.warnings:
        st.caption(w)


def _balances_button(s: dict) -> None:
    if st.button("Show testnet balances", disabled=not s["configured"]):
        try:
            bal = execution.BinanceTestnet().balances()
            rows = [(a, v) for a, v in bal.items() if v > 0]
            st.dataframe(pd.DataFrame({"Asset": [a for a, _ in rows], "Free": [str(v) for _, v in rows]}),
                         hide_index=True, width="stretch")
        except Exception as e:
            st.error(f"Could not read balances: {type(e).__name__}: {e}")


def _history() -> None:
    st.subheader("Execution history")
    rows = execution.get_exec_orders(100)
    if not rows:
        st.caption("No execution orders yet.")
        return
    st.dataframe(pd.DataFrame({
        "Time": [r["created_at"] for r in rows],
        "Mode": [r["mode"] for r in rows],
        "Symbol": [r["symbol"] for r in rows],
        "Side": [r["side"] for r in rows],
        "Requested": [r["requested"] for r in rows],
        "Status": [r["status"] for r in rows],
        "Executed": [f"{r['executed_qty']:.8f}" if r["executed_qty"] else "-" for r in rows],
        "Avg price": [f"{r['avg_price']:,.2f}" if r["avg_price"] else "-" for r in rows],
        "Reconcile": [r["reconcile"] or "-" for r in rows],
        "Note": [r["note"] for r in rows],
    }), hide_index=True, width="stretch")
    unknown = [r for r in rows if r["status"] in ("UNKNOWN", "SENDING")]
    if unknown:
        st.warning(f"{len(unknown)} order(s) are UNKNOWN/SENDING: check the exchange before placing more. "
                   "They still count against today's limit.")


def render() -> None:
    st.caption("Binance testnet only. Fake money. Default is dry-run: nothing is traded unless EXEC_ENABLED=1.")
    try:
        s = execution.status()
    except Exception as e:
        st.error(f"Execution module is not ready: {type(e).__name__}: {e}")
        return
    _status_row(s)
    _kill_switch(s["killed"])
    st.divider()
    _order_form(s)
    _balances_button(s)
    st.divider()
    _history()
