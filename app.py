from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional, Tuple

import pandas as pd
import streamlit as st
from dotenv import load_dotenv


def ensure_thread_event_loop() -> None:
    """Ensure the Streamlit script thread has an asyncio event loop."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


ensure_thread_event_loop()
from ib_insync import IB, Contract, ContractDetails, Future, Order, Trade, Ticker, util


# --- Constants ---
APP_TITLE = "MES Bracket Trader (IBKR)"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7497  # paper
DEFAULT_CLIENT_ID = 19

RISK_OFFSET = 0.010
REWARD_OFFSET = 0.020


# --- UI / Logging helpers ---
def init_logging() -> logging.Logger:
    logger = logging.getLogger("mes_bracket_trader")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = init_logging()


def log(msg: str) -> None:
    LOGGER.info(msg)
    st.session_state.logs.append(f"{datetime.now().strftime('%H:%M:%S')}  {msg}")


def ensure_session_defaults() -> None:
    if "ib" not in st.session_state:
        st.session_state.ib = IB()
    st.session_state.setdefault("connected", False)
    st.session_state.setdefault("host", DEFAULT_HOST)
    st.session_state.setdefault("port", DEFAULT_PORT)
    st.session_state.setdefault("client_id", DEFAULT_CLIENT_ID)
    st.session_state.setdefault("logs", [])
    st.session_state.setdefault("recent_fills", [])  # list[dict]
    st.session_state.setdefault("current_contract", None)  # Contract
    st.session_state.setdefault("contract_month_str", "")
    st.session_state.setdefault("last_price", None)  # float
    st.session_state.setdefault("pending_order", None)  # dict or None
    st.session_state.setdefault("submitting", False)
    st.session_state.setdefault("last_order_ids", ())  # tuple[int, int, int]
    st.session_state.setdefault("children_adjusted", False)
    st.session_state.setdefault("live_confirmed", False)
    st.session_state.setdefault("order_qty", 1)
    st.session_state.setdefault("ticker_subscription", None)
    st.session_state.setdefault("ticker_listener", None)


# --- IB helpers ---
def get_front_month_mes(ib: IB) -> Future:
    """Return nearest non-expired MES contract as a fully qualified Future.

    Picks the earliest lastTradeDateOrContractMonth strictly in the future.
    """
    details: list[ContractDetails] = ib.reqContractDetails(
        Future(symbol="MES", exchange="CME", currency="USD")
    )
    now = datetime.now(timezone.utc)

    def parse_month(s: str) -> datetime:
        # IB may return YYYYMM or YYYYMMDD; normalize to YYYYMM01 if only month.
        if len(s) == 6:
            dt = datetime.strptime(s + "01", "%Y%m%d")
        else:
            dt = datetime.strptime(s, "%Y%m%d")
        return dt.replace(tzinfo=timezone.utc)

    future_details = [
        (cd, parse_month(cd.contract.lastTradeDateOrContractMonth)) for cd in details
        if cd.contract.lastTradeDateOrContractMonth
    ]
    future_details = [x for x in future_details if x[1] > now]
    if not future_details:
        raise RuntimeError("No non-expired MES contracts found.")
    future_details.sort(key=lambda x: x[1])
    best_cd = future_details[0][0]

    # Build and qualify the picked contract
    picked = Future(
        symbol="MES",
        lastTradeDateOrContractMonth=best_cd.contract.lastTradeDateOrContractMonth,
        exchange="CME",
        currency="USD",
    )
    qualified = ib.qualifyContracts(picked)[0]
    return qualified


def _market_price_from_ticker(t: Ticker) -> Optional[float]:
    # Heuristic for a usable last price
    for v in [t.last, t.marketPrice(), t.midpoint(), t.close]:
        if v and v > 0:
            return float(v)
    return None


def refresh_last_price(ib: IB, contract: Contract) -> Optional[float]:
    try:
        if hasattr(ib, "loop") and hasattr(ib, "reqTickersAsync"):
            future = asyncio.run_coroutine_threadsafe(ib.reqTickersAsync(contract), ib.loop)
            tickers = future.result(timeout=2.0)
        else:
            tickers = ib.reqTickers(contract)
        if tickers:
            price = _market_price_from_ticker(tickers[0])
            st.session_state.last_price = price
            return price
    except FuturesTimeoutError:
        log("Market data request timed out; using last known price.")
    except Exception as e:  # noqa: BLE001
        log(f"Market data error: {e}")
    return st.session_state.last_price


def _stop_market_data_stream(ib: IB) -> None:
    ticker = st.session_state.get("ticker_subscription")
    listener = st.session_state.get("ticker_listener")
    if ticker is not None:
        try:
            if listener:
                ticker.updateEvent -= listener
        except Exception:
            pass
        try:
            ib.cancelMktData(ticker.contract)
        except Exception:
            pass
    st.session_state.ticker_subscription = None
    st.session_state.ticker_listener = None


def _start_market_data_stream(ib: IB, contract: Contract) -> None:
    _stop_market_data_stream(ib)
    try:
        ticker = ib.reqMktData(contract, "", False, False)
    except Exception as e:  # noqa: BLE001
        log(f"Market data subscription error: {e}")
        return

    def on_ticker_update(_: Ticker) -> None:
        price = _market_price_from_ticker(ticker)
        if price:
            st.session_state.last_price = price

    ticker.updateEvent += on_ticker_update
    st.session_state.ticker_subscription = ticker
    st.session_state.ticker_listener = on_ticker_update


def build_bracket(
    parent_action: str,
    entry_type: Literal["MKT", "LMT"],
    entry_price: Optional[float],
    qty: int,
    tif: str,
    risk: float = RISK_OFFSET,
    reward: float = REWARD_OFFSET,
) -> Tuple[Order, Order, Order]:
    """Create parent + (tp, sl) orders with transmit flags per bracket wiring.

    For MKT entries, initial child prices are computed from the latest known
    market price (st.session_state['last_price']). Children will be adjusted
    to the actual average fill price upon first parent fill event.
    """
    assert parent_action in ("BUY", "SELL")
    assert entry_type in ("MKT", "LMT")
    assert qty > 0
    assert tif in ("DAY", "GTC")

    base_price: Optional[float]
    if entry_type == "LMT" and entry_price is not None:
        base_price = float(entry_price)
    else:
        base_price = st.session_state.get("last_price")

    parent = Order()
    parent.action = parent_action
    parent.totalQuantity = qty
    parent.tif = tif
    parent.orderType = "MKT" if entry_type == "MKT" else "LMT"
    if parent.orderType == "LMT":
        parent.lmtPrice = float(entry_price)  # validated upstream
    parent.transmit = False

    # Child orders
    tp = Order()
    sl = Order()

    if parent_action == "BUY":
        tp.action = "SELL"
        sl.action = "SELL"
        if base_price is not None:
            tp_price = base_price + reward
            sl_price = base_price - risk
        else:
            tp_price = None  # will be set on fill adjustment
            sl_price = None
    else:  # SELL
        tp.action = "BUY"
        sl.action = "BUY"
        if base_price is not None:
            tp_price = base_price - reward
            sl_price = base_price + risk
        else:
            tp_price = None
            sl_price = None

    # Take profit as LMT, Stop as STP
    tp.orderType = "LMT"
    tp.totalQuantity = qty
    tp.tif = tif
    if tp_price is not None:
        tp.lmtPrice = float(tp_price)
    tp.transmit = False

    sl.orderType = "STP"
    sl.totalQuantity = qty
    sl.tif = tif
    if sl_price is not None:
        sl.auxPrice = float(sl_price)
    sl.outsideRth = False
    sl.transmit = True  # last child transmits

    return parent, tp, sl


# --- Trading orchestration ---
@dataclass
class PlacedBracket:
    parent_trade: Trade
    tp_trade: Trade
    sl_trade: Trade


def place_bracket_trade(
    ib: IB,
    contract: Contract,
    side: Literal["BUY", "SELL"],
    entry_type: Literal["MKT", "LMT"],
    entry_price: Optional[float],
    qty: int,
    tif: Literal["DAY", "GTC"],
) -> PlacedBracket:
    parent, tp, sl = build_bracket(
        parent_action=side,
        entry_type=entry_type,
        entry_price=entry_price,
        qty=qty,
        tif=tif,
    )

    # Place parent first to obtain orderId
    parent_trade = ib.placeOrder(contract, parent)
    parent_id = parent_trade.order.orderId

    # Wire children to parent and place
    tp.parentId = parent_id
    sl.parentId = parent_id

    tp_trade = ib.placeOrder(contract, tp)
    sl_trade = ib.placeOrder(contract, sl)

    st.session_state.last_order_ids = (parent_id, tp_trade.order.orderId, sl_trade.order.orderId)
    log(f"Placed bracket: parent={parent_id}, tp={tp_trade.order.orderId}, sl={sl_trade.order.orderId}")

    # Attach fill handlers to collect recent fills
    def on_fill(trade: Trade, fill):  # fill: ib_insync.objects.Fill
        try:
            d = {
                "time": fill.time.strftime("%H:%M:%S") if getattr(fill, "time", None) else datetime.now().strftime("%H:%M:%S"),
                "side": trade.order.action,
                "qty": fill.execution.shares if getattr(fill, "execution", None) else trade.order.totalQuantity,
                "avgFillPrice": fill.execution.avgPrice if getattr(fill, "execution", None) else trade.orderStatus.avgFillPrice,
                "orderId": trade.order.orderId,
            }
            # Include realized PnL if available
            cr = getattr(fill, "commissionReport", None)
            if cr is not None and getattr(cr, "realizedPNL", None) is not None:
                d["realizedPnL"] = cr.realizedPNL
            st.session_state.recent_fills.insert(0, d)
            st.session_state.recent_fills = st.session_state.recent_fills[:20]
        except Exception as e:  # noqa: BLE001
            log(f"Fill handler error: {e}")

    for trade in (parent_trade, tp_trade, sl_trade):
        if hasattr(trade, "fillEvent"):
            trade.fillEvent += on_fill

    # Order status logging
    def on_status(tr: Trade):
        try:
            os = tr.orderStatus
            if os is not None:
                msg = (
                    f"Order {tr.order.orderId} status={os.status} filled={os.filled}/{tr.order.totalQuantity}"
                )
                if os.avgFillPrice:
                    msg += f" avg={os.avgFillPrice:.5f}"
                log(msg)
        except Exception as e:  # noqa: BLE001
            log(f"Status handler error: {e}")

    for trade in (parent_trade, tp_trade, sl_trade):
        if hasattr(trade, "statusEvent"):
            trade.statusEvent += on_status

    # Adjust children around actual fill price (once) if needed
    def adjust_children_on_parent_first_fill(trade: Trade, fill):
        try:
            if st.session_state.children_adjusted:
                return
            avg = trade.orderStatus.avgFillPrice or (fill.execution.avgPrice if getattr(fill, "execution", None) else None)
            if not avg or avg <= 0:
                return

            # If LMT and avg equals limit within epsilon, keep as-is
            if parent.orderType == "LMT" and parent.lmtPrice:
                if abs(float(parent.lmtPrice) - float(avg)) < 1e-6:
                    st.session_state.children_adjusted = True
                    return

            # Cancel previous children
            try:
                ib.cancelOrder(tp_trade.order)
                ib.cancelOrder(sl_trade.order)
            except Exception:
                pass

            # Rebuild children around actual avg fill price
            if side == "BUY":
                new_tp_price = float(avg) + REWARD_OFFSET
                new_sl_price = float(avg) - RISK_OFFSET
                new_tp_action = "SELL"
                new_sl_action = "SELL"
            else:
                new_tp_price = float(avg) - REWARD_OFFSET
                new_sl_price = float(avg) + RISK_OFFSET
                new_tp_action = "BUY"
                new_sl_action = "BUY"

            new_tp = Order(
                action=new_tp_action,
                orderType="LMT",
                totalQuantity=qty,
                tif=tif,
                lmtPrice=new_tp_price,
                parentId=parent_id,
                transmit=False,
            )
            new_sl = Order(
                action=new_sl_action,
                orderType="STP",
                totalQuantity=qty,
                tif=tif,
                auxPrice=new_sl_price,
                parentId=parent_id,
                outsideRth=False,
                transmit=True,
            )

            new_tp_trade = ib.placeOrder(contract, new_tp)
            new_sl_trade = ib.placeOrder(contract, new_sl)
            log(
                f"Adjusted children around avgFillPrice={avg:.5f}: tp={new_tp_trade.order.orderId} @ {new_tp_price:.5f}, sl={new_sl_trade.order.orderId} @ {new_sl_price:.5f}"
            )
            st.session_state.children_adjusted = True
        except Exception as e:  # noqa: BLE001
            log(f"Adjustment error: {e}")

    parent_trade.fillEvent += adjust_children_on_parent_first_fill

    return PlacedBracket(parent_trade, tp_trade, sl_trade)


# --- Streamlit UI ---
def render_connection_panel() -> None:
    st.subheader("Connection")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.session_state.host = st.text_input("Host", value=str(st.session_state.host))
    with col2:
        st.session_state.port = int(
            st.number_input("Port", value=int(st.session_state.port), step=1, min_value=1)
        )
    with col3:
        st.session_state.client_id = int(
            st.number_input("Client ID", value=int(st.session_state.client_id), step=1, min_value=0)
        )

    is_live = st.session_state.port == 7496
    indicator = "LIVE" if is_live else "PAPER"
    color = "red" if is_live else "green"
    st.markdown(f"**Mode:** <span style='color:{color}'>{indicator}</span>", unsafe_allow_html=True)

    if is_live:
        st.session_state.live_confirmed = st.checkbox("I understand I am trading LIVE.")
    else:
        st.session_state.live_confirmed = True

    c1, c2 = st.columns(2)
    with c1:
        if not st.session_state.connected:
            if st.button("Connect", type="primary"):
                try:
                    ib: IB = st.session_state.ib
                    log(
                        f"Connecting to {st.session_state.host}:{st.session_state.port} (clientId={st.session_state.client_id})"
                    )
                    ib.connect(
                        host=st.session_state.host,
                        port=st.session_state.port,
                        clientId=st.session_state.client_id,
                        timeout=5,
                    )
                    st.session_state.connected = ib.isConnected()
                    st.session_state.children_adjusted = False
                    if st.session_state.connected:
                        # Resolve front month and subscribe to quotes
                        contract = get_front_month_mes(ib)
                        st.session_state.current_contract = contract
                        st.session_state.contract_month_str = contract.lastTradeDateOrContractMonth
                        _start_market_data_stream(ib, contract)
                        refresh_last_price(ib, contract)
                        log(
                            f"Connected. Selected contract: MES {st.session_state.contract_month_str} CME USD (conId={contract.conId})"
                        )
                except Exception as e:  # noqa: BLE001
                    st.session_state.connected = False
                    log(f"Connect error: {e}")
        else:
            st.write("Connected.")

    with c2:
        if st.session_state.connected and st.button("Disconnect"):
            try:
                _stop_market_data_stream(st.session_state.ib)
                st.session_state.ib.disconnect()
            finally:
                st.session_state.connected = False
                st.session_state.current_contract = None
                st.session_state.contract_month_str = ""
                st.session_state.last_price = None
                st.session_state.live_confirmed = False
                log("Disconnected.")


def render_order_panel() -> None:
    st.subheader("Order")

    if st.session_state.current_contract:
        st.write(
            f"Instrument: MES {st.session_state.contract_month_str} CME USD"
        )
    if st.session_state.connected and st.session_state.current_contract:
        price = st.session_state.get("last_price")
        if price:
            st.caption(f"Last price: {price:.5f}")
        else:
            st.caption("Last price: waiting for market data…")
            if st.button("Request Price Snapshot"):
                fresh = refresh_last_price(st.session_state.ib, st.session_state.current_contract)
                if fresh:
                    st.success(f"Snapshot price: {fresh:.5f}")

    # Inputs
    entry_price_input = st.text_input("Price (optional)", value="", placeholder="Leave blank for market")
    entry_price: Optional[float]
    if entry_price_input.strip() == "":
        entry_price = None
    else:
        try:
            entry_price = float(entry_price_input)
            if entry_price <= 0:
                st.error("Price must be positive if provided.")
                entry_price = None
        except ValueError:
            st.error("Invalid price. Use a numeric value.")
            entry_price = None

    qty = int(
        st.number_input(
            "Quantity",
            min_value=1,
            step=1,
            key="order_qty",
        )
    )

    tif = st.radio("TIF", ("DAY", "GTC"), index=0, horizontal=True)

    colb, cols = st.columns(2)
    disable_trade = not (
        st.session_state.connected and st.session_state.current_contract and st.session_state.live_confirmed
    )
    with colb:
        buy_clicked = st.button("BUY", disabled=disable_trade)
    with cols:
        sell_clicked = st.button("SELL", disabled=disable_trade)

    # Capture intent and show confirmation
    if buy_clicked or sell_clicked:
        if entry_price_input.strip() and entry_price is None:
            return  # invalid price
        side = "BUY" if buy_clicked else "SELL"
        entry_type: Literal["MKT", "LMT"] = "LMT" if entry_price is not None else "MKT"

        # Preview prices based on current info
        base = entry_price if entry_type == "LMT" else st.session_state.get("last_price")
        if base is None:
            st.warning("No market price available yet; children will be adjusted on fill.")
        if side == "BUY":
            tp_p = (base + REWARD_OFFSET) if base is not None else None
            sl_p = (base - RISK_OFFSET) if base is not None else None
        else:
            tp_p = (base - REWARD_OFFSET) if base is not None else None
            sl_p = (base + RISK_OFFSET) if base is not None else None

        st.session_state.pending_order = {
            "side": side,
            "entry_type": entry_type,
            "entry_price": entry_price,
            "qty": qty,
            "tif": tif,
            "tp": tp_p,
            "sl": sl_p,
        }
        log(
            f"Order intent captured: {side} {entry_type}"
            + (f" @ {entry_price:.5f}" if entry_price is not None else "")
            + f", qty={qty}, tif={tif}"
        )

    if st.session_state.pending_order is not None:
        po = st.session_state.pending_order
        with st.expander("Confirm Order", expanded=True):
            st.write(
                f"Side: {po['side']}  |  Entry: {po['entry_type']}"
                + (f" @ {po['entry_price']:.5f}" if po["entry_type"] == "LMT" and po["entry_price"] else "")
            )
            st.write(
                "TP / SL: "
                + (f"{po['tp']:.5f}" if po["tp"] is not None else "(pending)")
                + "  /  "
                + (f"{po['sl']:.5f}" if po["sl"] is not None else "(pending)")
            )
            st.write(f"TIF: {po['tif']}")
            if st.session_state.current_contract:
                st.write(f"Contract: MES {st.session_state.contract_month_str}")
            confirm_disabled = st.session_state.submitting or not st.session_state.live_confirmed
            if st.button("Send Order", type="primary", disabled=confirm_disabled):
                try:
                    st.session_state.submitting = True
                    st.session_state.children_adjusted = False
                    contract_label = (
                        f"MES {st.session_state.contract_month_str}"
                        if st.session_state.contract_month_str
                        else "Unknown contract"
                    )
                    log(
                        f"Submitting {po['side']} {po['entry_type']} order qty={po['qty']} tif={po['tif']} on {contract_label}"
                    )
                    placed = place_bracket_trade(
                        st.session_state.ib,
                        st.session_state.current_contract,
                        po["side"],
                        po["entry_type"],
                        po["entry_price"],
                        po["qty"],
                        po["tif"],
                    )
                    st.success(
                        f"Submitted. Parent/TP/SL IDs: {st.session_state.last_order_ids}"
                    )
                    log(f"Order submission complete: ids={st.session_state.last_order_ids}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Submit error: {e}")
                    log(f"Submit error: {e}")
                finally:
                    st.session_state.submitting = False
                    st.session_state.pending_order = None


def render_status_panel() -> None:
    st.subheader("Status & Logs")
    # Show order IDs
    if st.session_state.last_order_ids:
        st.write(f"Last order IDs: {st.session_state.last_order_ids}")

    # Logs
    log_box = st.empty()
    logs_str = "\n".join(st.session_state.logs[-200:])
    log_box.text_area("Logs", value=logs_str, height=200)

    # Recent fills table
    st.subheader("Recent Fills")
    if st.session_state.recent_fills:
        df = pd.DataFrame(st.session_state.recent_fills)
        st.dataframe(df, use_container_width=True, height=200)
    else:
        st.caption("No fills yet.")


def load_env_defaults() -> None:
    load_dotenv()
    import os

    st.session_state.host = os.getenv("IB_HOST", str(st.session_state.host))
    try:
        st.session_state.port = int(os.getenv("IB_PORT", str(st.session_state.port)))
    except ValueError:
        st.session_state.port = DEFAULT_PORT
    try:
        st.session_state.client_id = int(os.getenv("IB_CLIENT_ID", str(st.session_state.client_id)))
    except ValueError:
        st.session_state.client_id = DEFAULT_CLIENT_ID


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="centered")
    st.title(APP_TITLE)

    ensure_session_defaults()
    # Load .env only on first run
    if not st.session_state.get("_env_loaded", False):
        load_env_defaults()
        st.session_state._env_loaded = True

    render_connection_panel()
    st.divider()
    render_order_panel()
    st.divider()
    render_status_panel()


if __name__ == "__main__":
    # Ensure ib_insync has an event loop
    util.startLoop()
    main()
