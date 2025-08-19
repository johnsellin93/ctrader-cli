
# cTrader OpenAPI CLI

A Python-powered **command-line trading toolkit** for interacting with the [cTrader Open API](https://connect.spotware.com/).
Includes account management, order execution, and a live Unrealized PnL viewer built with [Rich](https://github.com/Textualize/rich).

⚠️ **Note:** This is *not* the official cTrader CLI (which is Windows-only).
This project is a Python-based client for the cTrader Open API.
Currently tested and confirmed to work on **Linux and macOS terminals**.

---

## Features

- 🔑 **Account Management**
  - Login with API credentials (`CLIENT_ID`, `ACCESS_TOKEN`)
  - Automatic reconnect + account reconciliation

- 📊 **Live Unrealized PnL Viewer**
  - Browse open positions in real time using a Rich-powered table and intuitive keybindings (`j/k`, `q`, `x`).
  - Navigation: `j/k` to move, `q` to quit, `x` to close position

- 📥 **Trading Actions**
  - Market, Limit, and Stop orders
  - Close positions & cancel orders
  - Deal offset support

- 🔍 **Market Data**
  - Trendbars & tick data
  - Spot price board
  - Asset and symbol categories

---

Live Unrealized PnL viewer (Rich-powered terminal UI)
![Demo](output.gif)

---


## ⚙️ Setup

```
git clone https://github.com/johnsellin93/ctrader-cli && \
cd ctrader-cli && \
pip install -r requirements.txt

vim .env
# then edit .env with your cTrader API keys

After starting the script:

1. Choose **`2` → Set Account**
   (select the trading account you want to use).
2. Then choose **`19` → Unrealized PnL (Live Viewer)**
   to launch the live PnL dashboard.
```

## Example .env:
```
CLIENT_ID="your-client-id"
CLIENT_SECRET="your-client-secret"
ACCESS_TOKEN="your-access-token"
REFRESH_TOKEN="your-refresh-token"
ACCOUNT_IDS="1234567,7654321"
TOKEN_URL="https://connect.spotware.com/token"
API_BASE_URL="https://api.spotware.com/connect/tradingaccounts"
```

### 🔑 Where to get these values?

- **CLIENT_ID / CLIENT_SECRET** — Create an app via the [cTrader Developer Portal](https://connect.spotware.com/apps)
- **ACCESS_TOKEN / REFRESH_TOKEN** — Obtained after authorizing your app via OAuth2
- **ACCOUNT_IDS** — Your trading account IDs (visible in cTrader once connected)
- **TOKEN_URL / API_BASE_URL** — Use the defaults above unless Spotware changes endpoints



Tech Stack

- Python (3.10+)

- Twisted (async networking)

- Protobuf (cTrader messages)

- Rich (terminal UI)

- prompt_toolkit (CLI input)

## Dependencies
This project has been tested with:

- ctrader-open-api==0.9.2
- pyautogui==0.9.54
- prompt-toolkit==3.0.51
- twisted==24.3.0
- inputimeout==1.0.4
- python-dotenv==1.0.1
- rich==13.7.0
- colorama==0.4.6


##  License & Contributions
Licensed under MIT. Contributions and feature requests are welcome! 🚀
