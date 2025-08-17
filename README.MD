
# 📈 cTrader CLI

A Python-powered command-line toolkit for interacting with the **cTrader Open API**.
Includes account management, order execution, and a live Unrealized PnL viewer powered by [Rich](https://github.com/Textualize/rich).

---

## ✨ Features

- 🔑 **Account Management**
  - Login with API credentials (`CLIENT_ID`, `ACCESS_TOKEN`)
  - Automatic reconnect + account reconciliation

- 📊 **Live Unrealized PnL Viewer**
  - Real-time spot price subscriptions
  - Rich-powered terminal table
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

## 🚀 Demo

Live Unrealized PnL viewer (Rich-powered terminal UI)

*(screenshot or GIF here would be 🔥)*

---

## 📋 Prerequisites

- Python **3.10+**
- `pip` (Python package manager)
- cTrader Open API credentials:
  - `CLIENT_ID`
  - `CLIENT_SECRET`
  - `ACCESS_TOKEN`

---

## ⚙️ Setup

```
# 1. Clone the repo
git clone https://github.com/johnsellin93/ctrader-cli
cd ctrader-cli

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# then edit .env with your cTrader API keys
```

## Example .env:
```
CLIENT_ID=""
CLIENT_SECRET=""
ACCESS_TOKEN=""
REFRESH_TOKEN=""
ACCOUNT_IDS=""
TOKEN_URL="https://connect.spotware.com/token"
API_BASE_URL="https://api.spotware.com/connect/tradingaccounts"
```


## CLI Menu
```
Menu Options:
 1. List Accounts
 2. Set Account
 3. Version Info
 4. List Assets
 5. List Asset Classes
 6. List Symbol Categories
 7. Show Price Board
 8. Trader Info
 9. Subscribe to Spot
 10. Reconcile (Show Positions)
 11. Get Trendbars
 12. Get Tick Data
 13. New Market Order
 14. New Limit Order
 15. New Stop Order
 16. Close Position
 17. Cancel Order
 18. Deal Offset List
 19. Unrealized PnL (Live Viewer)
 20. Order Details
 21. Orders by Position ID
 22. Help
 1. List Accounts
 2. Set Account
 3. Version Info
 4. List Assets
 7. Show Price Board
13. New Market Order
16. Close Position
19. Unrealized PnL (Live Viewer)
22. Help
```
