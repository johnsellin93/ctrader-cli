
#!/usr/bin/env python
import traceback
from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints

from types import SimpleNamespace  # (you already have this import)
from ctrader_open_api.endpoints import EndPoints
from re import sub
import select
import logging
import termios, tty
import pyautogui
import time
from prompt_toolkit.shortcuts import radiolist_dialog
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor
from inputimeout import inputimeout, TimeoutOccurred
from datetime import datetime, timezone, timedelta
import datetime
import calendar
from dotenv import load_dotenv
import os
import uuid
from rich.table import Table
from rich.live import Live
from rich.console import Console
from rich.console import Group
from rich import box
import sys
import contextlib
import threading
from colorama import Fore, Style
from graceful_shutdown import ShutdownManager
import ui_helpers as H
from message_handlers import dispatch_message

console = Console(emoji=False)
live = None
accountMetadata = {}
pendingReconciliations = set()
symbolIdToName = {}
symbolIdToPrice = {}  # Symbol ID -> (bid, ask)
symbolIdToPips = {}  # Symbol ID -> pipsPosition
subscribedSymbols = set()
expectedSpotSubscriptions = 0
receivedSpotConfirmations = 0
positionsById = {}
positionPnLById = {}
showStartupOutput = False
liveViewerActive = False
symbolIdToDetails = {}
currentAccountId = None
selected_position_index = 0
error_messages = []
view_offset = 0
#

RENDER_MIN_INTERVAL = 0.02
_last_render = 0.0
_render_pending = False

#

# ---- cached sort for positions (to avoid re-sorting on every keypress) ----
menuScheduled = False
slByPositionId = {}            # positionId -> SL in account currency
slInput = {                    # inline input state
    "mode": "idle",            # idle | armed | typing
    "positionId": None,
    "buffer": ""
}
H.init_ordering(positionsById, positionPnLById)

# Configure logging
logging.basicConfig(
    filename="close_position_errors.log",
    filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)



if __name__ == "__main__":
    load_dotenv()
    accountIdsEnv = os.getenv("ACCOUNT_IDS", "")
    envAccountIds = [int(acc.strip()) for acc in accountIdsEnv.split(",") if acc.strip().isdigit()]

    while True:
        hostType = input("Host (Live/Demo): ").strip().lower()
        if hostType in ["live", "demo"]:
            break
        print(f"{hostType} is not a valid host type.")

    appClientId = os.getenv("CLIENT_ID")
    appClientSecret = os.getenv("CLIENT_SECRET")
    accessToken = os.getenv("ACCESS_TOKEN")

    client = Client(EndPoints.PROTOBUF_LIVE_HOST if hostType.lower() == "live" else EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, TcpProtocol)

    def _stop_live_ui():
        global liveViewerActive, live
        try:
            liveViewerActive = False
        except Exception:
            pass
        try:
            if live:
                live.stop()
        except Exception:
            pass

    # main.py
    def get_account_ccy() -> str:
        ccy = accountMetadata.get(currentAccountId, {}).get("currency")
        # treat unknowns as missing and fall back to USD
        if not ccy or ccy in {"?", "UNKNOWN", "N/A", ""}:
            return "USD"
        return ccy

    # Create and wire the shutdown manager
    shutdown = ShutdownManager(
        reactor=reactor,
        client=client,
        get_subscribed_symbols=lambda: list(subscribedSymbols),
        unsubscribe_symbol=lambda sid: sendProtoOAUnsubscribeSpotsReq(sid),
        account_logout=lambda: sendProtoOAAccountLogoutReq(),
        stop_live_ui=_stop_live_ui,
    )
    shutdown.install_signal_handlers()
    
    # Ensure Twisted calls our cleanup on reactor shutdown as well
    reactor.addSystemEventTrigger(
        'before', 'shutdown',
        lambda: shutdown.cleanup(reason='reactor-shutdown')
    )

    def returnToMenu():
        global menuScheduled
        if liveViewerActive:
            # The live viewer owns stdin; don’t start the menu now.
            return
        menuScheduled = False
        reactor.callLater(0, executeUserCommand)


    def refreshSpotPrices():
        if not isAccountInitialized(currentAccountId):
            return
        for symbolId in subscribedSymbols.copy():
            sendProtoOASubscribeSpotsReq(symbolId)
        reactor.callLater(15, refreshSpotPrices)

    def connected(client):
        print("\nConnected")
        request = ProtoOAApplicationAuthReq()
        request.clientId = appClientId
        request.clientSecret = appClientSecret

        def onAppAuthSuccess(_):
            print("✅ Application authorized")
#             print("📥 Fetching available accounts from access token...")
            sendProtoOAGetAccountListByAccessTokenReq()

        deferred = client.send(request)
        deferred.addCallback(onAppAuthSuccess)
        deferred.addErrback(onError)

    def disconnected(client, reason):
        print(f"🔌 Disconnected: {reason}")
        if shutdown.shutting_down:
            return
        print("🔁 Attempting reconnect in 5s...")
        reactor.callLater(5, client.startService)


    def promptUserToSelectAccount():
        print("\n👉 Select the account you want to activate:")
        for idx, accId in enumerate(availableAccounts, 1):
            trader = accountTraderInfo.get(accId)
            meta = accountMetadata.get(accId, {})
            is_live = meta.get("isLive", "?")
            broker = meta.get("broker", "?")
            currency = meta.get("currency", "?")
            if trader:
                print(f" {idx}. {accId} — [{is_live}] Equity: {trader.equity / 100:.2f}, Free Margin: {trader.freeMargin / 100:.2f}, Broker: {broker}, Currency: {currency}")
            else:
                print(f" {idx}. {accId} — [{is_live}], Broker: {broker}, Currency: {currency}")

        while True:
            try:
                choice = int(input("Enter number of account to activate: ").strip())
                if 1 <= choice <= len(availableAccounts):
                    selectedAccountId = availableAccounts[choice - 1]
                    setAccount(selectedAccountId)
                    break
                else:
                    print("Invalid choice. Try again.")
            except ValueError:
                print("Enter a number.")

    #

    def _request_render():
        global _render_pending
        if _render_pending:
            return
        _render_pending = True
        reactor.callLater(0, _do_render)
    
    def _do_render():
        global _render_pending
        _render_pending = False
        printLivePnLTable()

    def printLivePnLTable():
        global live, selected_position_index, view_offset, _last_render 
        if not live:
            return

        prompt_line = ""
        if slInput["mode"] == "armed":
            pid = slInput["positionId"]
            prompt_line = f"SL for Position {pid} [{get_account_ccy()}]: (type a number, Enter=save, Esc=cancel) — j/k moves target"
        elif slInput["mode"] == "typing":
            pid = slInput["positionId"]
            prompt_line = f"SL for Position {pid} [{get_account_ccy()}]: {slInput['buffer']}_  (Enter=save, Esc=cancel, ⌫=backspace)"
        view, selected_position_index, view_offset = H.buildLivePnLView(
            console_height=console.size.height,
            positions_sorted=H.ordered_positions(),
            selected_index=selected_position_index,
            view_offset=view_offset,
            symbolIdToName=symbolIdToName,
            symbolIdToDetails=symbolIdToDetails,
            symbolIdToPrice=symbolIdToPrice,
            positionPnLById=positionPnLById,
            error_messages=error_messages,
            slByPositionId=slByPositionId,              
            account_currency=get_account_ccy(),            
            footer_prompt=prompt_line,   # <- fix
        )
#         live.update(view)
        live.update(view, refresh=True)   # instead of just live.update(view)


    def _update_pnl_cache_for_symbol(symbol_id: int):
        bid, ask = symbolIdToPrice.get(symbol_id, (None, None))
        if bid is None or ask is None:
            return
        contract_size = (symbolIdToDetails.get(symbol_id, {}) or {}).get("contractSize", 100000) or 100000
        for pos_id, pos in positionsById.items():
            if pos.tradeData.symbolId != symbol_id:
                continue
            side = H.trade_side_name(pos.tradeData.tradeSide)
            entry = pos.price
            lots = pos.tradeData.volume / 100.0
            mkt = bid if side == "BUY" else ask
            positionPnLById[pos_id] = (mkt - entry if side == "BUY" else entry - mkt) * lots * contract_size
        H.mark_positions_dirty()


    def add_position(pos):
        global selected_position_index, view_offset
        pos_id = pos.positionId
        slByPositionId.setdefault(pos_id, None) 
        positionsById[pos_id] = pos
        sendProtoOASubscribeSpotsReq(pos.tradeData.symbolId)
        H.mark_positions_dirty()
        sendProtoOAGetPositionUnrealizedPnLReq()  # get real PnL 
    
        ops = H.ordered_positions()
        total = len(ops)
        if total == 1:
            selected_position_index = 0
            view_offset = 0
        elif selected_position_index >= total:
            selected_position_index = total - 1
    
        reactor.callFromThread(printLivePnLTable)

    
    def log_exec_event_error(res, exc: Exception):
        """Log errors from ProtoOAExecutionEvent handling to a file for debugging."""
        try:
            with open("exec_event_errors.log", "a") as f:
                f.write("\n" + "="*60 + "\n")
                f.write("⚠️ Error handling ProtoOAExecutionEvent\n")
                try:
                    etype = ProtoOAExecutionType.Name(res.executionType)
                except Exception:
                    etype = getattr(res, "executionType", "???")
                f.write(f"ExecutionType: {etype}\n")
                f.write(f"OrderId: {getattr(res, 'orderId', 'N/A')}\n")
                f.write(f"PositionId: {getattr(res, 'positionId', 'N/A')}\n\n")
                traceback.print_exc(file=f)
        except Exception as logfail:
            # last resort: print to stderr so it’s not lost
            print(f"❌ Failed to log exec_event error: {logfail}")


    accountTraderInfo = {}  # To store info like balance for each account
    availableAccounts = []  # Fetched account IDs


    authorizedAccounts = set()
    authInProgress = set()

    def fetchTraderInfo(accountId):
        print(f"🔍 Starting auth flow for account: {accountId}")

        # If fully authorized already
        if accountId in authorizedAccounts:
            print(f"✅ Already authorized: {accountId}")
            sendProtoOAReconcileReq(accountId)
            return

        # If auth is already in progress, don’t do it twice
        if accountId in authInProgress:
            print(f"⏳ Auth already in progress for {accountId}")
            return

        authInProgress.add(accountId)

        def onAuthSuccess(_):
            print(f"✅ Account {accountId} authorized successfully")
            authorizedAccounts.add(accountId)
            pendingReconciliations.add(accountId)
            reactor.callLater(0.5, sendProtoOAReconcileReq, accountId)

        request = ProtoOAAccountAuthReq()
        request.ctidTraderAccountId = accountId
        request.accessToken = accessToken

        deferred = client.send(request)
        deferred.addCallback(onAuthSuccess)
        deferred.addErrback(onError)


    def onAccountListReceived(res):
        global availableAccounts, accountMetadata
        availableAccounts = [acc.ctidTraderAccountId for acc in res.ctidTraderAccount]
        
#         print("Available accounts:")
        for acc in res.ctidTraderAccount:
            acc_id = acc.ctidTraderAccountId
            currency = getattr(acc, "depositCurrency", "?")
            broker = getattr(acc, "brokerName", "?")
            is_live = "Live" if getattr(acc, "isLive", False) else "Demo"
            
            # Save metadata for later use
            accountMetadata[acc_id] = {
                "currency": currency,
                "broker": broker,
                "isLive": is_live
            }
    
            print(f" - ID: {acc_id}, Type: {is_live}, Broker: {broker}, Currency: {currency}")
        
        returnToMenu()


    def onError(failure): # Call back for errors
        print("Message Error: ", failure)
        reactor.callLater(3, callable=executeUserCommand)

    def showHelp():
        print("Commands (Parameters with an * are required), ignore the description inside ()")
        print("setAccount(For all subsequent requests this account will be used) *accountId")
        print("ProtoOAVersionReq clientMsgId")
        print("ProtoOAGetAccountListByAccessTokenReq clientMsgId")
        print("ProtoOAAssetListReq clientMsgId")
        print("ProtoOAAssetClassListReq clientMsgId")
        print("ProtoOASymbolCategoryListReq clientMsgId")
        print("ProtoOASymbolsListReq includeArchivedSymbols(True/False) clientMsgId")
        print("ProtoOATraderReq clientMsgId")
        print("ProtoOASubscribeSpotsReq *symbolId *timeInSeconds(Unsubscribes after this time) subscribeToSpotTimestamp(True/False) clientMsgId")
        print("ProtoOAReconcileReq clientMsgId")
        print("ProtoOAGetTrendbarsReq *weeks *period *symbolId clientMsgId")
        print("ProtoOAGetTickDataReq *days *type *symbolId clientMsgId")
        print("NewMarketOrder *symbolId *tradeSide *volume clientMsgId")
        print("NewLimitOrder *symbolId *tradeSide *volume *price clientMsgId")
        print("NewStopOrder *symbolId *tradeSide *volume *price clientMsgId")
        print("ClosePosition *positionId *volume clientMsgId")
        print("CancelOrder *orderId clientMsgId")
        print("DealOffsetList *dealId clientMsgId")
        print("GetPositionUnrealizedPnL clientMsgId")
        print("OrderDetails clientMsgId")
        print("OrderListByPositionId *positionId fromTimestamp toTimestamp clientMsgId")

        reactor.callLater(3, callable=executeUserCommand)


    def setAccount(accountId):
        global currentAccountId
        if currentAccountId is not None:
            sendProtoOAAccountLogoutReq()
        currentAccountId = int(accountId)
        fetchTraderInfo(currentAccountId)


    def sendProtoOAVersionReq(clientMsgId = None):
        request = ProtoOAVersionReq()
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOAGetAccountListByAccessTokenReq(clientMsgId = None):
        request = ProtoOAGetAccountListByAccessTokenReq()
        request.accessToken = accessToken
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOAAccountLogoutReq(clientMsgId = None):
        request = ProtoOAAccountLogoutReq()
        request.ctidTraderAccountId = currentAccountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)



    def sendProtoOAAccountAuthReq(clientMsgId = None):
        request = ProtoOAAccountAuthReq()
        request.ctidTraderAccountId = currentAccountId
        request.accessToken = accessToken

        def onAccountAuthSuccess(_):
            print("✅ Account authorization successful")
#             sendProtoOAReconcileReq()  # <-- This is essential!
            sendProtoOAReconcileReq(currentAccountId)

        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addCallback(onAccountAuthSuccess)
        deferred.addErrback(onError)


    def sendProtoOAAssetListReq(clientMsgId = None):
        global client
        print("📤 Requesting asset list...")
        request = ProtoOAAssetListReq()
        request.ctidTraderAccountId = currentAccountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)


    def sendProtoOAAssetClassListReq(clientMsgId = None):
        global client
        request = ProtoOAAssetClassListReq()
        request.ctidTraderAccountId = currentAccountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOASymbolCategoryListReq(clientMsgId = None):
        global client
        request = ProtoOASymbolCategoryListReq()
        request.ctidTraderAccountId = currentAccountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def isAccountInitialized(accountId):
        return (
            accountId in authorizedAccounts and
            accountId not in pendingReconciliations
        )

    def sendProtoOASymbolsListReq(includeArchivedSymbols=False, clientMsgId=None):
        if not isAccountInitialized(currentAccountId):
            print(f"⛔ Cannot fetch symbols yet — account {currentAccountId} is not authorized or still reconciling.")
            returnToMenu()
            return

        print("📤 Requesting symbols list...")
        request = ProtoOASymbolsListReq()
        request.ctidTraderAccountId = currentAccountId
        request.includeArchivedSymbols = bool(includeArchivedSymbols)
        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)


    def sendProtoOATraderReq(accountId, clientMsgId = None):

        if accountId not in authorizedAccounts:
            print(f"⛔ Cannot request trader info: account {accountId} not authorized.")
            return
        if accountId in pendingReconciliations:
            print(f"⏳ Cannot request trader info: reconciliation still pending for account {accountId}.")
            return
        print(f"📤 Requesting trader info for account: {accountId}")
        request = ProtoOATraderReq()
        request.ctidTraderAccountId = accountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)



    def sendProtoOAUnsubscribeSpotsReq(symbolId, clientMsgId = None):
        global client
        request = ProtoOAUnsubscribeSpotsReq()
        request.ctidTraderAccountId = currentAccountId
        request.symbolId.append(int(symbolId))
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)


    def sendProtoOASubscribeSpotsReq(symbolId, timeInSeconds=None, subscribeToSpotTimestamp=False, clientMsgId=None):
        global client
    
        symbolId = int(symbolId)

        if symbolId in subscribedSymbols:
            return  # Already subscribed — skip
        subscribedSymbols.add(symbolId)
        request = ProtoOASubscribeSpotsReq()
        request.ctidTraderAccountId = currentAccountId
        request.symbolId.append(symbolId)
        request.subscribeToSpotTimestamp = subscribeToSpotTimestamp
    
        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)


    def sendProtoOAReconcileReq(accountId, clientMsgId = None):
        print(f"🔄 Sending reconcile for {accountId}")
        global client
        request = ProtoOAReconcileReq()
        request.ctidTraderAccountId = accountId
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)


    def startPositionPolling(interval=5.0):
        if not liveViewerActive:
            return  # Don't poll if viewer is off
        if currentAccountId in authorizedAccounts:
            sendProtoOAReconcileReq(currentAccountId)
        reactor.callLater(interval, startPositionPolling, interval)

    def sendProtoOAGetTrendbarsReq(weeks, period, symbolId, clientMsgId = None):
        global client
        request = ProtoOAGetTrendbarsReq()
        request.ctidTraderAccountId = currentAccountId
        request.period = ProtoOATrendbarPeriod.Value(period)
        request.fromTimestamp = int(calendar.timegm((datetime.datetime.utcnow() - datetime.timedelta(weeks=int(weeks))).utctimetuple())) * 1000
        request.toTimestamp = int(calendar.timegm(datetime.datetime.utcnow().utctimetuple())) * 1000
        request.symbolId = int(symbolId)
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOAGetTickDataReq(days, quoteType, symbolId, clientMsgId = None):
        global client
        request = ProtoOAGetTickDataReq()
        request.ctidTraderAccountId = currentAccountId
        request.type = ProtoOAQuoteType.Value(quoteType.upper())
        request.fromTimestamp = int(calendar.timegm((datetime.datetime.utcnow() - datetime.timedelta(days=int(days))).utctimetuple())) * 1000
        request.toTimestamp = int(calendar.timegm(datetime.datetime.utcnow().utctimetuple())) * 1000
        request.symbolId = int(symbolId)
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOANewOrderReq(symbolId, orderType, tradeSide, volume, price = None, clientMsgId = None):
        global client
        request = ProtoOANewOrderReq()
        request.ctidTraderAccountId = currentAccountId
        request.symbolId = int(symbolId)
        request.orderType = ProtoOAOrderType.Value(orderType.upper())
        request.tradeSide = ProtoOATradeSide.Value(tradeSide.upper())
        request.volume = int(volume) * 100
        if request.orderType == ProtoOAOrderType.LIMIT:
            request.limitPrice = float(price)
        elif request.orderType == ProtoOAOrderType.STOP:
            request.stopPrice = float(price)
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendNewMarketOrder(symbolId, tradeSide, volume, clientMsgId = None):
        global client
        sendProtoOANewOrderReq(symbolId, "MARKET", tradeSide, volume, clientMsgId = clientMsgId)

    def sendNewLimitOrder(symbolId, tradeSide, volume, price, clientMsgId = None):
        global client
        sendProtoOANewOrderReq(symbolId, "LIMIT", tradeSide, volume, price, clientMsgId)

    def sendNewStopOrder(symbolId, tradeSide, volume, price, clientMsgId = None):
        global client
        sendProtoOANewOrderReq(symbolId, "STOP", tradeSide, volume, price, clientMsgId)

    def sendProtoOAClosePositionReq(positionId, volume, clientMsgId = None):
        global client
        request = ProtoOAClosePositionReq()
        request.ctidTraderAccountId = currentAccountId
        request.positionId = int(positionId)
        # convert lots -> centi-lots with rounding, not truncation
        request.volume = int(round(float(volume) * 100))
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOACancelOrderReq(orderId, clientMsgId = None):
        global client
        request = ProtoOACancelOrderReq()
        request.ctidTraderAccountId = currentAccountId
        request.orderId = int(orderId)
        deferred = client.send(request, clientMsgId = clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOADealOffsetListReq(dealId, clientMsgId=None):
        global client
        request = ProtoOADealOffsetListReq()
        request.ctidTraderAccountId = currentAccountId
        request.dealId = int(dealId)
        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)

    def waitUntilAllPositionPrices(callback, max_wait=1.0, check_interval=0.1):
        symbolIds = {pos.tradeData.symbolId for pos in positionsById.values()}
        attempts = int(max_wait / check_interval)

        def check(remaining):
            missing = [sid for sid in symbolIds if sid not in symbolIdToPrice]
            if not missing:
                callback()
            elif remaining <= 0:
                print("⚠️ Timeout waiting for all spot prices.")
                callback()
            else:
                reactor.callLater(check_interval, check, remaining - 1)

        check(attempts)


    def remove_position(pos_id):
        global selected_position_index, view_offset
    
        if pos_id in positionsById:
            # get symbol for potential unsubscribe
            symbol_id = positionsById[pos_id].tradeData.symbolId
    
            positionsById.pop(pos_id, None)
            positionPnLById.pop(pos_id, None)

    
            # NEW: if no positions left for this symbol, unsubscribe
            still_used = any(p.tradeData.symbolId == symbol_id for p in positionsById.values())
            if not still_used:
                try:
                    sendProtoOAUnsubscribeSpotsReq(symbol_id)
                    subscribedSymbols.discard(symbol_id)
                except Exception:
                    pass  # best-effort
    
            H.mark_positions_dirty()
    
            # selection / viewport housekeeping (unchanged)
            total = len(H.ordered_positions())
            if total == 0:
                selected_position_index = 0
                view_offset = 0
            elif selected_position_index >= total:
                selected_position_index = total - 1
    
            term_height = console.size.height
            max_rows = term_height - 8
            if selected_position_index < view_offset:
                view_offset = selected_position_index
            elif selected_position_index >= view_offset + max_rows:
                view_offset = max(0, selected_position_index - max_rows + 1)
    
            printLivePnLTable()
    #
    
    def listen_for_keys() -> None:
        global selected_position_index, liveViewerActive, slInput, slByPositionId
    
        # Prefer the controlling TTY so we don't compete with input()/inputimeout()
        try:
            tty_in = open('/dev/tty', 'rb', buffering=0)
        except Exception:
            tty_in = sys.stdin  # fallback if /dev/tty is unavailable (e.g., some containers)
    
        fd = tty_in.fileno()
        old_settings = termios.tcgetattr(fd)
        shutdown.set_tty_old_settings(old_settings)
    
        try:
            tty.setcbreak(fd)
    
            def move_selection(delta: int) -> None:
                global selected_position_index, view_offset
                ops = H.ordered_positions()
                n = len(ops)
                if n == 0:
                    return
                selected_position_index = (selected_position_index + delta) % n
                term_height = console.size.height
                max_rows = term_height - 8
                if selected_position_index < view_offset:
                    view_offset = selected_position_index
                elif selected_position_index >= view_offset + max_rows:
                    view_offset = selected_position_index - max_rows + 1
                reactor.callFromThread(printLivePnLTable)
    
            while liveViewerActive:
                try:
                    r, _, _ = select.select([tty_in], [], [], 0.1)  # 100ms poll
                    if not r:
                        continue
                    b = tty_in.read(1)
                    if not b:
                        continue
                    key = b.decode('utf-8', errors='ignore')
    
                    # ----------------- SL input state machine -----------------
                    if slInput["mode"] == "armed":
                        if key == "j":
                            move_selection(+1); continue
                        if key == "k":
                            move_selection(-1); continue
                        if key == "\x1b":  # Esc
                            slInput.update({"mode": "idle", "positionId": None, "buffer": ""})
                            reactor.callFromThread(printLivePnLTable); continue
                        if key in "0123456789.-":
                            sel = H.safe_current_selection(selected_position_index)
                            if not sel: 
                                continue
                            pid, _ = sel
                            slInput.update({"mode": "typing", "positionId": pid, "buffer": key})
                            reactor.callFromThread(printLivePnLTable); continue
                        # ignore others; fall through to normal keys
    
                    elif slInput["mode"] == "typing":
                        if key in ("\r", "\n"):  # Enter -> save (handle CR and LF)
                            try:
                                val = float(slInput["buffer"].strip())
                                slByPositionId[slInput["positionId"]] = abs(val)
                            except Exception:
                                pass
                            slInput.update({"mode": "idle", "positionId": None, "buffer": ""})
                            reactor.callFromThread(printLivePnLTable); continue
                        if key == "\x1b":  # Esc -> cancel
                            slInput.update({"mode": "idle", "positionId": None, "buffer": ""})
                            reactor.callFromThread(printLivePnLTable); continue
                        if key == "\x7f":  # Backspace
                            slInput["buffer"] = slInput["buffer"][:-1]
                            reactor.callFromThread(printLivePnLTable); continue
                        if key in "0123456789.-":
                            slInput["buffer"] += key
                            reactor.callFromThread(printLivePnLTable); continue
                        # while typing we ignore j/k etc, to avoid moving target
    
                    # start SL input
                    if key == "y" and slInput["mode"] == "idle":
                        sel = H.safe_current_selection(selected_position_index)
                        if sel:
                            pid, _ = sel
                            slInput.update({"mode": "armed", "positionId": pid, "buffer": ""})
                            reactor.callFromThread(printLivePnLTable)
                        continue
                    # -----------------------------------------------------------
    
                    # -------- normal hotkeys --------
                    if key == "q":
                        liveViewerActive = False
                        reactor.callFromThread(getattr(live, "stop", lambda: None))
                        print("👋 Exiting Live PnL Viewer...")
                        reactor.callLater(0.5, executeUserCommand)
                        break
                    elif key == "j":
                        move_selection(+1)
                    elif key == "k":
                        move_selection(-1)
                    elif key == "x":
                        sel = H.safe_current_selection(selected_position_index)
                        if not sel:
                            continue
                        pos_id, pos = sel
                        volume_units = pos.tradeData.volume
                        reactor.callFromThread(sendProtoOAClosePositionReq, pos_id, volume_units / 100)
                        reactor.callFromThread(remove_position, pos_id)
                        reactor.callLater(2.0, lambda: runWhenReady(sendProtoOAReconcileReq, currentAccountId))
                    elif key == "\r":
                        sel = H.safe_current_selection(selected_position_index)
                        if not sel:
                            continue
                        pos_id, pos = sel
                        # show details...
    
                except Exception as e:
                    logging.error("Key thread error: %s\n%s", e, traceback.format_exc())
                    # keep the loop alive
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            finally:
                shutdown.clear_tty_old_settings()
                try:
                    if tty_in is not sys.stdin:
                        tty_in.close()
                except Exception:
                    pass
    
    def choosePositionFromLiveList():
        choices = []
        for posId, pos in positionsById.items():
            symbol_id = pos.tradeData.symbolId
            symbol_name = symbolIdToName.get(symbol_id, f"ID:{symbol_id}")
            choices.append((posId, f"{posId} — {symbol_name}"))
    
        result = radiolist_dialog(
            title="🎯 Select Position",
            text="Use ↑ ↓ to move, [Enter] to select, [Esc] to cancel",
            values=choices,
        ).run()
    
        if result:
            print(f"\n✅ You selected position: {result}")
            volume_units = positionsById[result].tradeData.volume
            runWhenReady(sendProtoOAClosePositionReq, result, volume_units / 100)
        else:
            print("\n❌ No selection made.")


    def startPnLUpdateLoop(interval=0.5):
        if not currentAccountId or currentAccountId not in authorizedAccounts:
            print("⚠️ Cannot start PnL loop – account not ready.")
            return
        if not liveViewerActive:
            return
        sendProtoOAGetPositionUnrealizedPnLReq()
        reactor.callLater(interval, startPnLUpdateLoop, interval)



    def sendProtoOAGetPositionUnrealizedPnLReq(clientMsgId=None):
        global client

        request = ProtoOAGetPositionUnrealizedPnLReq()
        request.ctidTraderAccountId = currentAccountId

#         print("📤 Sending Unrealized PnL request (no position IDs needed)...")

        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)

    def sendProtoOAOrderDetailsReq(orderId, clientMsgId=None):
        global client
        request = ProtoOAOrderDetailsReq()
        request.ctidTraderAccountId = currentAccountId
        request.orderId = int(orderId)
        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)


    def sendProtoOAOrderListByPositionIdReq(positionId, fromTimestamp=None, toTimestamp=None, clientMsgId=None):
        global client
        request = ProtoOAOrderListByPositionIdReq()
        request.ctidTraderAccountId = currentAccountId
        request.positionId = int(positionId)

        # Default to full range if not provided
        if fromTimestamp is None:
            fromTimestamp = 0
        if toTimestamp is None:
            toTimestamp = int(calendar.timegm(datetime.datetime.utcnow().utctimetuple())) * 1000

        request.fromTimestamp = int(fromTimestamp)
        request.toTimestamp = int(toTimestamp)

        deferred = client.send(request, clientMsgId=clientMsgId)
        deferred.addErrback(onError)


    tickFetchQueue = set()

    def subscribeToSymbolsFromOpenPositions(duration=None):
        seen = set()
        for pos in positionsById.values():
            sid = pos.tradeData.symbolId
            if sid not in seen:
                seen.add(sid)
                sendProtoOASubscribeSpotsReq(sid, timeInSeconds=duration)
    
        # Optionally kick off a one-shot tick fallback for any missing prices
        def fetch_missing_ticks():
            for sid in seen:
                if sid not in symbolIdToPrice:
                    sendProtoOAGetTickDataReq(1, "BID", sid)
        reactor.callLater(0.5, fetch_missing_ticks)

    def launchLivePnLViewer():
        global liveViewerActive, live, selected_position_index, view_offset
        liveViewerActive = True
        startPositionPolling(5.0)
#         reactor.callLater(10.0)
    
        print("🔃 Subscribing to spot prices for open positions...")
        subscribeToSymbolsFromOpenPositions()
    
        view, selected_position_index, view_offset = H.buildLivePnLView(
            console_height=console.size.height,
            positions_sorted=H.ordered_positions(),
            selected_index=selected_position_index,
            view_offset=view_offset,
            symbolIdToName=symbolIdToName,
            symbolIdToDetails=symbolIdToDetails,
            symbolIdToPrice=symbolIdToPrice,
            positionPnLById=positionPnLById,
            error_messages=error_messages,
            slByPositionId=slByPositionId,
            account_currency=get_account_ccy(),
        )
        live = Live(view, refresh_per_second=20, screen=True, console=console, auto_refresh=False)
        live.start()
    
        sendProtoOAGetPositionUnrealizedPnLReq()
        startPnLUpdateLoop(0.3)
        threading.Thread(target=listen_for_keys, daemon=True).start()

    def printUpdatedPriceBoard():
        print("\n📊 Updated Spot Prices:")
        missing = []
    
        # show only the symbols we're actually subscribed to
        for symbolId in sorted(subscribedSymbols):
            name = symbolIdToName.get(symbolId, f"ID:{symbolId}")
            bid_ask = symbolIdToPrice.get(symbolId)
            if bid_ask:
                bid, ask = bid_ask
                if bid == 0 or ask == 0:
                    print(f" - {name} (ID: {symbolId}) — ⚠️ Price: 0.0 — retrying...")
                    missing.append(symbolId)
                else:
                    print(f" - {name} (ID: {symbolId}) — Bid: {bid}, Ask: {ask}")
            else:
                print(f" - {name} (ID: {symbolId}) — Price: [pending]")
                missing.append(symbolId)
    
        if missing:
            print(f"\n⏳ Retrying {len(missing)} missing prices...")
            for i, sid in enumerate(missing):
                reactor.callLater(i * 0.05, sendProtoOAGetTickDataReq, 1, "BID", sid)
            reactor.callLater(3, printUpdatedPriceBoard)
        else:
            return None

    menu = {
        "1": ("List Accounts", sendProtoOAGetAccountListByAccessTokenReq),
        "2": ("Set Account", setAccount),
        "3": ("Version Info", sendProtoOAVersionReq),
        "4": ("List Assets", sendProtoOAAssetListReq),
        "5": ("List Asset Classes", sendProtoOAAssetClassListReq),
        "6": ("List Symbol Categories", sendProtoOASymbolCategoryListReq),
        "7": ("Show Price Board", printUpdatedPriceBoard),  # <-- new label & function
        "8": ("Trader Info", sendProtoOATraderReq),
        "9": ("Subscribe to Spot", sendProtoOASubscribeSpotsReq),
        "10": ("Reconcile (Show Positions)", lambda: sendProtoOAReconcileReq(currentAccountId)),
        "11": ("Get Trendbars", sendProtoOAGetTrendbarsReq),
        "12": ("Get Tick Data", sendProtoOAGetTickDataReq),
        "13": ("New Market Order", sendNewMarketOrder),
        "14": ("New Limit Order", sendNewLimitOrder),
        "15": ("New Stop Order", sendNewStopOrder),
        "16": ("Close Position", sendProtoOAClosePositionReq),
        "17": ("Cancel Order", sendProtoOACancelOrderReq),
        "18": ("Deal Offset List", sendProtoOADealOffsetListReq),
        "19": (
            "Unrealized PnL (Live Viewer)",
            lambda: reactor.callLater(1.0, runWhenReady, launchLivePnLViewer)
        ),
        "20": ("Order Details", sendProtoOAOrderDetailsReq),
        "21": ("Orders by Position ID", sendProtoOAOrderListByPositionIdReq),
        "22": ("Help", showHelp),
    }
    commands = {v[0].replace(" ", ""): v[1] for v in menu.values()}



def ensureAccountSet():
    if not currentAccountId:
        print("⚠️ Please set a valid account first using option 2.")
        return False
    if not isAccountReady(currentAccountId):
        print(f"⚠️ Account {currentAccountId} is not fully ready yet. Please wait for trader info.")
        return False
    return True

def isAccountReady(accountId):
    return (
        accountId in authorizedAccounts
        and accountId not in pendingReconciliations
        and accountId in accountTraderInfo
    )



def waitUntilAccountReady(accountId, callback, interval=0.5):
    if isAccountReady(accountId):
        print(f"✅ Account {accountId} is now ready.")
        callback()
    else:
        print(f"⏳ Waiting for account {accountId} to be ready...")
        reactor.callLater(interval, waitUntilAccountReady, accountId, callback, interval)


def runWhenReady(fn, *args, **kwargs):
    def call():
        fn(*args, **kwargs)
    waitUntilAccountReady(currentAccountId, call)


def set_current_account_id(val: int) -> None:
    global currentAccountId
    currentAccountId = val




ctx = SimpleNamespace(
    set_current_account_id=set_current_account_id,
    request_render=_request_render,
    update_pnl_cache_for_symbol=_update_pnl_cache_for_symbol,
    # shared state
    accountMetadata=accountMetadata,
    pendingReconciliations=pendingReconciliations,
    symbolIdToName=symbolIdToName,
    symbolIdToPrice=symbolIdToPrice,
    symbolIdToPips=symbolIdToPips,
    subscribedSymbols=subscribedSymbols,
    expectedSpotSubscriptions=expectedSpotSubscriptions,
    receivedSpotConfirmations=receivedSpotConfirmations,
    positionsById=positionsById,
    positionPnLById=positionPnLById,
    showStartupOutput=showStartupOutput,
    liveViewerActive=liveViewerActive,
    symbolIdToDetails=symbolIdToDetails,
    currentAccountId=currentAccountId,
    selected_position_index=selected_position_index,
    error_messages=error_messages,
    view_offset=view_offset,
    slByPositionId=slByPositionId,
    accountTraderInfo=accountTraderInfo,
    availableAccounts=availableAccounts,
    authorizedAccounts=authorizedAccounts,
    authInProgress=authInProgress,
    envAccountIds=envAccountIds,

    # functions used by handlers
    printLivePnLTable=printLivePnLTable,
    printUpdatedPriceBoard=printUpdatedPriceBoard,
    returnToMenu=returnToMenu,
    runWhenReady=runWhenReady,
    isAccountInitialized=isAccountInitialized,
    remove_position=remove_position,
    add_position=add_position,
    log_exec_event_error=log_exec_event_error,
    get_account_ccy=get_account_ccy,

    sendProtoOASubscribeSpotsReq=sendProtoOASubscribeSpotsReq,
    sendProtoOAUnsubscribeSpotsReq=sendProtoOAUnsubscribeSpotsReq,
    sendProtoOAGetTickDataReq=sendProtoOAGetTickDataReq,
    sendProtoOAGetPositionUnrealizedPnLReq=sendProtoOAGetPositionUnrealizedPnLReq,
    sendProtoOAReconcileReq=sendProtoOAReconcileReq,
    sendProtoOATraderReq=sendProtoOATraderReq,
    sendProtoOAOrderDetailsReq=sendProtoOAOrderDetailsReq,
    sendProtoOAClosePositionReq=sendProtoOAClosePositionReq,
    sendProtoOAGetAccountListByAccessTokenReq=sendProtoOAGetAccountListByAccessTokenReq,
    fetchTraderInfo=fetchTraderInfo,
    setAccount=setAccount,
    promptUserToSelectAccount=promptUserToSelectAccount,

    reactor=reactor,

    # minimal stub used by on_execution handler
    print_order_filled_event=lambda res: print(f"🟢 Order filled: {getattr(res, 'orderId', '?')}")
)


def onMessageReceived(client, message):
    if liveViewerActive:
        with H.suppress_stdout(liveViewerActive):
            dispatch_message(client, message, ctx)
    else:
        dispatch_message(client, message, ctx)


def executeUserCommand():
    global menuScheduled
    if liveViewerActive:
        # Safety: never prompt while viewer is active
        menuScheduled = False
        return
    if menuScheduled:
        return  # 👈 Prevent multiple overlapping menu renderings
    menuScheduled = True
    print(f"📌 Active Account ID: {currentAccountId}")
    print("\nMenu Options:")
    for key, (desc, _) in sorted(menu.items(), key=lambda x: int(x[0])):
        print(f" {key}. {desc}")
    print("Or type command name directly (e.g. help, NewMarketOrder, etc.)")

    try:
        userInput = inputimeout("Select option or type command: ", timeout=20).strip()
    except TimeoutOccurred:
        print("⏱️ Timeout – no input detected.")
        menuScheduled = False
        reactor.callLater(3, executeUserCommand)
        return
    menuScheduled = False

    if userInput not in ["1", "2"] and not ensureAccountSet():
        returnToMenu()
        return

    # If it's a menu number
    if userInput in menu:
        desc, func = menu[userInput]
        try:
            if desc == "Set Account":
                if not availableAccounts:
                    print("⚠️ No accounts available. Use option 1 to fetch them first.")
                    returnToMenu()
                    return

                print("\n👉 Select the account you want to activate:")
                for idx, accId in enumerate(availableAccounts, 1):
                    trader = accountTraderInfo.get(accId)
                    if trader:
                        print(f" {idx}. {accId} — Equity: {trader.equity / 100:.2f}, Free Margin: {trader.freeMargin / 100:.2f}")
                    else:
                        print(f" {idx}. {accId}")

                while True:
                    try:
                        choice = int(input("Enter number of account to activate: ").strip())
                        if 1 <= choice <= len(availableAccounts):
                            selectedAccountId = availableAccounts[choice - 1]
                            setAccount(selectedAccountId)
                            break
                        else:
                            print("Invalid choice. Try again.")
                    except ValueError:
                        print("Enter a valid number.")

            elif desc == "Subscribe to Spot":
                symbolId = input("Symbol ID: ")
                seconds = input("Time in seconds: ")
                func(symbolId, seconds)
                runWhenReady(func, symbolId, seconds)


            elif desc == "Show Price Board":
                def fetchSymbolsAndThenShowBoard():
                    def afterSymbols():
                        print("⏳ Waiting for initial prices...")
                        reactor.callLater(3, func)  # func is printUpdatedPriceBoard

                    print("📥 Fetching symbol list...")
                    def symbolsCallback():
                        runWhenReady(afterSymbols)  # Wait for account & spot subs

                    runWhenReady(lambda: sendProtoOASymbolsListReq(False))
                    reactor.callLater(1.5, symbolsCallback)  # give time for subs to dispatch

                runWhenReady(fetchSymbolsAndThenShowBoard)


            elif desc == "Get Trendbars":
                weeks = input("Weeks: ")
                period = input("Period (e.g., M1): ")
                symbolId = input("Symbol ID: ")
                runWhenReady(func, weeks, period, symbolId)

            elif desc == "Get Tick Data":
                days = int(input("Days: "))
                tickType = input("Type (BID/ASK/BOTH): ")
                symbolId = int(input("Symbol ID: "))
                runWhenReady(func, days, tickType, symbolId)

            elif desc == "New Market Order":
                symbolId = input("Symbol ID: ")
                side = input("Side (BUY/SELL): ")
                volume = input("Volume: ")
                runWhenReady(func, symbolId, side, volume)

            elif desc == "New Limit Order" or desc == "New Stop Order":
                symbolId = input("Symbol ID: ")
                side = input("Side (BUY/SELL): ")
                volume = input("Volume: ")
                price = input("Price: ")
                runWhenReady(func, symbolId, side, volume, price)

            elif desc == "Close Position":
                positionId = input("Position ID: ")
                volume = input("Volume: ")
                runWhenReady(func, positionId, volume)

            elif desc == "Cancel Order":
                orderId = input("Order ID: ")
                runWhenReady(func, orderId)

            elif desc == "Deal Offset List":
                dealId = input("Deal ID: ")
                runWhenReady(func, dealId)

            elif desc == "Order Details":
                orderId = input("Order ID: ")
                runWhenReady(func, orderId)

            elif desc == "Orders by Position ID":
                positionId = input("Position ID: ")
                fromTs = input("From Timestamp (or press Enter): ") or None
                toTs = input("To Timestamp (or press Enter): ") or None
                runWhenReady(func, positionId, fromTs, toTs)


            else:
                def run():
                    if desc == "Trader Info":
                        func(currentAccountId)
                    else:
                        func()
                waitUntilAccountReady(currentAccountId, run)
        except Exception as e:
            print(f"❌ Error executing {desc}: {e}")

    # Else if it's a typed command
    elif userInput in commands:
        try:
            if not ensureAccountSet():
                returnToMenu()
                return
            raw = input("Enter parameters (separated by spaces): ").strip()
            args = raw.split() if raw else []
            commands[userInput](*args)
        except Exception as e:
            print(f"❌ Error: {e}")
    else:
        print("❌ Invalid input")
    if not liveViewerActive:
        reactor.callLater(3, executeUserCommand)

# Setting optional client callbacks
client.setConnectedCallback(connected)
client.setDisconnectedCallback(disconnected)
client.setMessageReceivedCallback(onMessageReceived)
# Starting the client service
client.startService()
reactor.run()
