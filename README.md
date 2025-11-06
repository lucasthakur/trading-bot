# MES Bracket Trader (IBKR)

A minimal Streamlit app to trade the front-month MES (Micro E-mini S&P 500) futures contract on Interactive Brokers (IBKR) with 3-leg bracket orders (parent + take profit + stop).

This app uses ib_insync for async IB API access and supports both Paper and Live (with safety checks). Offsets are absolute per spec.

## Features

- Auto-selects the nearest non-expired MES contract (CME USD)
- Connect/Disconnect to IB TWS/Gateway with configurable host/port/clientId
- Buy/Sell bracket orders with 1:2 risk:reward
  - Risk offset: 0.010
  - Reward offset: 0.020
- Market or Limit parent entry
- Shared TIF: `DAY` or `GTC`
- Streams statuses, logs, order IDs, and recent fills
- Adjusts TP/SL around actual average fill price when parent fills

## Requirements

- Python 3.11+
- IBKR TWS or IB Gateway running with API enabled

## Install

```bash
python -m venv .venv
.\.venv\Scripts\activate  # Windows PowerShell
pip install -r requirements.txt
```

## Environment

Create a `.env` file in the project root (same folder as `app.py`):

```
IB_HOST=127.0.0.1
IB_PORT=7497
IB_CLIENT_ID=1001
```

Defaults are used if variables are missing: host `127.0.0.1`, port `7497` (paper), clientId `19`.

## Run

Start IBKR TWS or IB Gateway and enable the API:

- TWS: File -> Global Configuration -> API -> Settings
  - Enable ActiveX and Socket Clients
  - Set trusted IPs if needed
  - Note the socket port (7497 paper, 7496 live)

Then launch the app:

```bash
streamlit run app.py
```

## Safety & Live Trading

- The app shows a Paper/Live indicator based on port:
  - `7497` = Paper (recommended)
  - `7496` = Live (danger)
- When Live is detected, the app requires an explicit confirmation before orders can be sent.
- You are responsible for your trading and risk. Test in Paper first.

## Known Limitations

- No position monitoring beyond this symbol
- No advanced stop types (simple stop only)
- Offsets are absolute (not ticks) and may not align with the instrument tick size
- Market data may be delayed without appropriate IBKR subscriptions

## Notes

- The app uses ib_insync's async event loop under the hood, but Streamlit pages rerun per interaction. Long-running background tasks are avoided; order placement and updates are handled via ib_insync events and quick API calls.
- If you restart TWS/Gateway, reconnect from the app.
