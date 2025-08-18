
#!/usr/bin/env python
import traceback
from ctrader_open_api import Client, Protobuf, TcpProtocol, Auth, EndPoints
from ctrader_open_api.endpoints import EndPoints
from re import sub
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


console = Console()
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
# ---- cached sort for positions (to avoid re-sorting on every keypress) ----
positions_sorted_cache = []
_positions_sorted_dirty = True
menuScheduled = False

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

    def returnToMenu():
        global menuScheduled
        menuScheduled = False
        reactor.callLater(0, executeUserCommand)


    def refreshSpotPrices():
        if not isAccountInitialized(currentAccountId):
            return
        for symbolId in subscribedSymbols.copy():
            sendProtoOASubscribeSpotsReq(symbolId)
        reactor.callLater(15, refreshSpotPrices)


    @contextlib.contextmanager
    def suppress_stdout(to_file=True):
        if to_file and liveViewerActive:
            with open("live_pnl_stdout.log", "a") as logfile:
                old_stdout = sys.stdout
                old_stderr = sys.stderr
                sys.stdout = logfile
                sys.stderr = logfile  # Also capture errors
                try:
                    yield
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
        else:
            # Fallback to original suppression (discard output)
            with open(os.devnull, 'w') as fnull:
                old_stdout = sys.stdout
                sys.stdout = fnull
                try:
                    yield
                finally:
                    sys.stdout = old_stdout

    # Somewhere after symbol list or login:
#     sendProtoOASubscribeSpotsReq(symbol.symbolId, timeInSeconds=60)


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


#     def disconnected(client, reason): # Callback for client disconnection
#         print("\nDisconnected: ", reason)


    def disconnected(client, reason):
        print(f"🔌 Disconnected: {reason}")
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


    def colorize_number(amount):
        formatted = f"{amount:.2f}"
        padded = f"{formatted:>10}"  # Right-align without color
        if amount > 0:
            return f"\033[92m{padded}\033[0m"
        elif amount < 0:
            return f"\033[91m{padded}\033[0m"
        return padded


    # ── new helper (put it near the other little utils) ────────────────
    
    def mark_positions_dirty():
        """Call this whenever PnL changes or positions are added/removed."""
        global _positions_sorted_dirty
        _positions_sorted_dirty = True
    
    def _rebuild_sorted_cache():
        global positions_sorted_cache, _positions_sorted_dirty
        positions_sorted_cache = sorted(
            positionsById.items(),
            key=lambda item: positionPnLById.get(item[0], 0.0),
            reverse=True,
        )
        _positions_sorted_dirty = False

#     def ordered_positions() -> list[tuple[int]]:
#         """[(posId, pos)] sorted the same way the table shows them."""
#         return sorted(
#             positionsById.items(),
#             key=lambda item: positionPnLById.get(item[0], 0),
#             reverse=True,
#         )

    def ordered_positions() -> list[tuple[int]]:
        """[(posId, pos)] sorted by current net PnL (cached)."""
        if _positions_sorted_dirty:
            _rebuild_sorted_cache()
        return positions_sorted_cache


    def buildLivePnLView():
        table, msg = buildLivePnLTable()                # ← unpack the tuple ✅
    
        pieces = [table]                                # always show main table
        if msg:                                         # optional error footer
            pieces.append(f"[red]⚠ {msg}[/red]")
        pieces.append("[dim]🔴  q → quit ⏎ → View details[/dim]")
        pieces.append("[dim]↕️  j / k → Navigate[/dim]")
        pieces.append("[dim]❌  x → Exit selected position[/dim]")
    
        return Group(*pieces)                           # hand Rich real renderables


    def format_lots(volume_units: int, with_suffix: bool = True) -> str:
        # cTrader volume is in centi-lots (100 = 1.00 lots)
        lots = volume_units / 100.0
        s = f"{lots:,.2f}"
        return f"{s} Lots" if with_suffix else s

    def buildLivePnLTable():
        """Return (rich.Table, last_3_error_messages) for the live PnL viewer."""
        global liveViewerActive, selected_position_index, view_offset, error_messages
        liveViewerActive = True
    
        table = Table(
            title="📊 Live Unrealized PnL",
            box=box.SQUARE_DOUBLE_HEAD,
            expand=True,
            show_lines=False,
            padding=(0, 1),
        )
        table.add_column("Position ID", justify="right")
        table.add_column("Symbol",       justify="left")
        table.add_column("Side",         justify="center")
        table.add_column("Held For",     justify="center")
        table.add_column("Lot Size",     justify="right")
        table.add_column("Entry Price",  justify="right")
        table.add_column("Market", width=10, no_wrap=True)
        table.add_column("PnL",          justify="right")
    
#         table.add_column("Market", justify="right", no_wrap=True)
        # ── sort positions: biggest net PnL first ────────────────────────────────
#         sorted_positions = sorted(
#             positionsById.items(),
#             key=lambda item: positionPnLById.get(item[0], 0),
#             reverse=True,
        sorted_positions = ordered_positions()
        n = len(sorted_positions)

        term_height = console.size.height
        max_rows = max(1, term_height - 8)

        if n == 0:                        # no rows – show empty table
            selected_position_index = 0
            view_offset = 0
        else:
            # if the row we were on vanished, snap to last visible row
            if selected_position_index >= n:
                selected_position_index = n - 1
        
            # make sure the viewport still includes the cursor
            if selected_position_index < view_offset:
                view_offset = selected_position_index
            elif selected_position_index >= view_offset + max_rows:
                view_offset = max(0, selected_position_index - max_rows + 1)

        
        # NEW ↓ – work out how many rows fit and slice the data
        term_height = console.size.height
        max_rows = max(1, term_height - 8)
        visible = sorted_positions[view_offset : view_offset + max_rows]
        total_pnl = 0.0  
        for global_idx, (posId, pos) in enumerate(visible, start=view_offset):
            try:
                is_selected = global_idx == selected_position_index
                # ── arrow / highlight for the selected row ───────────────────────
                pos_id_label  = f"    ⮞ {posId}" if is_selected else str(posId)
                row_style     = "bold on blue" if is_selected else ""
    
                symbol_id     = pos.tradeData.symbolId
                symbol_name   = symbolIdToName.get(symbol_id, f"ID:{symbol_id}")
                side_raw      = ProtoOATradeSide.Name(pos.tradeData.tradeSide)
                side          = "[green]BUY[/green]" if side_raw == "BUY" else "[red]SELL[/red]"
    
                opened_at_utc = datetime.datetime.fromtimestamp(
                    pos.tradeData.openTimestamp / 1000, tz=timezone.utc
                )
                held_diff     = datetime.datetime.now(timezone.utc) - opened_at_utc
                hrs, mins     = divmod(int(held_diff.total_seconds() // 60), 60)
                held_for      = f"{hrs}h {mins}m" if hrs else f"{mins}m"
    
                # 1) Convert volume to lots correctly
                volume_lots = pos.tradeData.volume / 100.0
                
                # 2) Still use contractSize when turning price delta into money
                contract_size = symbolIdToDetails.get(symbol_id, {}).get("contractSize", 100000) or 100000
    
   

                entry_price   = pos.price
                money_digits  = pos.moneyDigits or symbolIdToDetails.get(symbol_id, {}).get("pips", 2)
                entry_price_s = f"{entry_price}"
                bid, ask      = symbolIdToPrice.get(symbol_id, (None, None))
                market_price  = ask if side_raw == "BUY" else bid
                market_price_s= f"{market_price}" if market_price else "[pending]"

                # ── NEW: live-fallback if cache empty ────────────────────────────
                pnl_cached = positionPnLById.get(posId)
                if isinstance(pnl_cached, (int, float)):
                    pnl_val = pnl_cached
                elif market_price is not None:
                    delta   = (market_price - entry_price) if side_raw == "BUY" else \
                              (entry_price - market_price)
                    pnl_val = delta * volume_lots * contract_size
                else:
                    pnl_val = None
    
                if pnl_val is not None:
                    total_pnl += pnl_val
                    pnl_s = colorize_number(pnl_val)
                else:
                    pnl_s = "[N/A]"
    
                table.add_row(
                    pos_id_label,
                    symbol_name,
                    side,
                    held_for,
                    format_lots(pos.tradeData.volume, with_suffix=False),
                    entry_price_s,
                    market_price_s,
                    pnl_s,
                    style=row_style,
                )
    
            except Exception as e:
                table.add_row(str(posId), "[Error]", "", "", "", "", "", str(e))
                error_messages.append(str(e))
                if len(error_messages) > 3:
                    error_messages.pop(0)
    
        # ── total row ────────────────────────────────────────────────────────────
        table.add_section()
        table.add_row(
            "", "", "", "", "", "", "[bold]TOTAL[/bold]",
            colorize_number(total_pnl),
        )
    
        return table, "\n".join(error_messages[-3:])


    def printLivePnLTable():
        global live
        if live:
            live.update(buildLivePnLView())





    def displayPosition(pos):
        try:
            symbolId = pos.tradeData.symbolId
            volumeLots = pos.tradeData.volume / 100.0
            side = ProtoOATradeSide.Name(pos.tradeData.tradeSide)
            symbolName = symbolIdToName.get(symbolId, f"ID:{symbolId}")
            openPrice = pos.price
            moneyDigits = pos.moneyDigits or 2
            marginUsed = pos.usedMargin / 100.0
            openTime = datetime.datetime.utcfromtimestamp(pos.tradeData.openTimestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')

            bid, ask = symbolIdToPrice.get(symbolId, (None, None))
            marketPrice = ask if side == "BUY" else bid
            pnl = None
            marketPriceLabel = "[waiting]"
            pnlLabel = "[calculating]"


            if marketPrice is not None:
                delta = (marketPrice - openPrice) if side == "BUY" else (openPrice - marketPrice)
                pnl = round(delta * volumeLots * 100000, 2)
                marketPriceLabel = f"{marketPrice}"
                pnlLabel = f"{pnl:.2f}"
            elif pos.positionId in positionPnLById:
                pnl = positionPnLById[pos.positionId]
                pnlLabel = f"{pnl:.2f} (cached)"
                marketPriceLabel = "[unavailable]"

            print(f"\n📌 Position ID: {pos.positionId}")
            print(f"   • Symbol:       {symbolName} (ID: {symbolId})")
            print(f"   • Side:         {side}")
            print(f"   • Volume:       {volumeLots:.2f} lots")
            print(f"   • Entry Price:  {openPrice}")
            print(f"   • Market Price: {marketPriceLabel}")
            print(f"   • PnL:          {pnlLabel}")
            print(f"   • Margin Used:  {marginUsed:.2f}")
            print(f"   • Open Time:    {openTime} UTC")
        except Exception as e:
            print(f"❌ Error displaying position: {e}")

    #

    def add_position(pos):
        global selected_position_index, view_offset
        pos_id = pos.positionId
        positionsById[pos_id] = pos
        sendProtoOASubscribeSpotsReq(pos.tradeData.symbolId)
        mark_positions_dirty()
        sendProtoOAGetPositionUnrealizedPnLReq()  # get real PnL quickly
    
        ops = ordered_positions()
        total = len(ops)
        if total == 1:
            selected_position_index = 0
            view_offset = 0
        elif selected_position_index >= total:
            selected_position_index = total - 1
    
        reactor.callFromThread(printLivePnLTable)

    def handle_message(client, message):
        global currentAccountId
        global receivedSpotConfirmations, expectedSpotSubscriptions
#         print(f"📩 Message received — payloadType: {message.payloadType}, size: {len(message.payload)} bytes")
        if message.payloadType == ProtoOASubscribeSpotsRes().payloadType:
            res = Protobuf.extract(message)
            print(f"✅ Spot subscription confirmed: {res}")
            receivedSpotConfirmations += 1
            # When all subs are confirmed, kick off the board loop
            if receivedSpotConfirmations >= expectedSpotSubscriptions:
                print("✅ All spot subscriptions confirmed. Starting price board loop.")
                reactor.callLater(0.5, printUpdatedPriceBoard)
            return

        elif message.payloadType in [
            ProtoOAAccountLogoutRes().payloadType,
            ProtoHeartbeatEvent().payloadType
        ]:
            return

        elif message.payloadType == ProtoOASymbolsListRes().payloadType:
            res = Protobuf.extract(message)
            print(f"📈 Received {len(res.symbol)} symbols:")
#             expectedSpotSubscriptions = len(res.symbol)
            open_position_symbols = {pos.tradeData.symbolId for pos in positionsById.values()}
            expectedSpotSubscriptions = len(open_position_symbols)
            receivedSpotConfirmations = 0

            open_position_symbols = {pos.tradeData.symbolId for pos in positionsById.values()}
            symbol_ids = [s.symbolId for s in res.symbol]
            symbol_ids.sort(key=lambda sid: 0 if sid in open_position_symbols else 1)

            symbol_by_id = {s.symbolId: s for s in res.symbol}
            
            for symbolId in symbol_ids:
                symbol = symbol_by_id[symbolId]
                # ✅ Store pips position safely
                symbolIdToPips[symbolId] = getattr(symbol, "pipsPosition", 5)

                # ✅ Store all metadata for this symbol
                symbolIdToDetails[symbolId] = {
                    "name": symbol.symbolName,
                    "pips": getattr(symbol, "pipsPosition", 5),
                    "contractSize": getattr(symbol, "contractSize", 1.0),
                    "assetClass": getattr(symbol, "assetClassName", "Unknown")
                }

                # Optional: keep existing mappings for backward compatibility
                symbolIdToName[symbolId] = symbol.symbolName

                bid_ask = symbolIdToPrice.get(symbolId)
                if bid_ask:
                    bid, ask = bid_ask
                    print(f" - {symbol.symbolName} (ID: {symbolId}) — Bid: {bid}, Ask: {ask}")
                else:
                    print(f" - {symbol.symbolName} (ID: {symbolId}) — Price: [pending]")

            # 🔁 Batch subscribe to all spot prices (in chunks)
            def bulkSubscribeSpots(symbolIds):
                for sid in symbolIds:
                    if sid not in subscribedSymbols:
                        sendProtoOASubscribeSpotsReq(sid)

            # ⏳ Spread out spot subscriptions in batches
            batch_size = 50
            all_symbol_ids = symbol_ids  # use sorted list
            for i in range(0, len(all_symbol_ids), batch_size):
                chunk = all_symbol_ids[i:i + batch_size]
                reactor.callLater(i * 0.5, bulkSubscribeSpots, chunk)

            # 🆘 Fallback: fetch tick prices for any that are still missing after a delay
            def fetchMissingTicks():
                for symbolId in symbolIdToName:
                    if symbolId not in symbolIdToPrice:
                        sendProtoOAGetTickDataReq(1, "BID", symbolId)

            reactor.callLater(0.5, fetchMissingTicks)  # slight delay before fallback
            reactor.callLater(1.0, printUpdatedPriceBoard)
            returnToMenu()

        elif message.payloadType == ProtoOASpotEvent().payloadType:
            try:
                res = Protobuf.extract(message)
                symbolId = res.symbolId
                pips = symbolIdToPips.get(symbolId, 5)
                bid = res.bid / (10 ** pips)
                ask = res.ask / (10 ** pips)

                # Update price map
                symbolIdToPrice[symbolId] = (bid, ask)
                for pos in positionsById.values():
                    if pos.tradeData.symbolId != symbolId:
                        continue
                    # Optionally trigger UI updates per-position here if needed

                # Redraw the table if live viewer is active
                if liveViewerActive:
                    printLivePnLTable()

            except Exception as e:
                print(f"❌ Failed to parse SpotEvent: {e}")


        elif message.payloadType == ProtoOAAssetListRes().payloadType:
            res = Protobuf.extract(message)
            print(f"📊 Received {len(res.asset)} assets:")
            for asset in res.asset[:5]:  # Just show a few
                print(f" - {asset.name} ({asset.assetId})")
            returnToMenu()
        elif message.payloadType == ProtoOAVersionRes().payloadType:
            res = Protobuf.extract(message)
            print(f"🔧 Version Info: {res.version}")
            returnToMenu()


        elif message.payloadType == 2101:
            try:
                res = Protobuf.extract(message)
                print("📦 Full decoded 2101 message:\n", res)

                print("\n🔍 Fields in 2101 message (set fields):")
                for field in res.ListFields():
                    print(f" - {field[0].name}: {field[1]}")

                print("\n🧬 All possible fields (even if unset):")
                for descriptor in res.DESCRIPTOR.fields:
                    field_name = descriptor.name
                    if res.HasField(field_name):
                        value = getattr(res, field_name)
                        print(f" - {field_name}: {value}")
                    else:
                        print(f" - {field_name}: [not set]")

            except Exception as e:
                print(f"⚠️ Could not decode payloadType 2101: {e}")
                print("📦 Raw payload (hex):", message.payload.hex())

        elif message.payloadType == ProtoOAAssetClassListRes().payloadType:
            res = Protobuf.extract(message)
            print(f"🏷️ Asset Classes: {len(res.assetClass)} found.")
            returnToMenu()

        elif message.payloadType == ProtoOASymbolCategoryListRes().payloadType:
            res = Protobuf.extract(message)
            print(f"🗂️ Symbol Categories: {len(res.category)}")
            returnToMenu()

        elif message.payloadType == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            res = Protobuf.extract(message)
            onAccountListReceived(res)

            apiAccountIds = [acc.ctidTraderAccountId for acc in res.ctidTraderAccount]
            validAccounts = list(set(envAccountIds) & set(apiAccountIds))

            if validAccounts:
                print("✅ Valid accounts from .env matched the API response:")
                for accId in validAccounts:
                    print(f" - {accId}")
                    if accId not in authorizedAccounts:
                        fetchTraderInfo(accId)  # ✅ Correct function
            else:
                print("⚠️ None of the ACCOUNT_IDS from .env matched available accounts.")
                print("Use menu option 2 to manually authorize one.")
                returnToMenu()
        #

        elif message.payloadType == ProtoOAReconcileRes().payloadType:
            res = Protobuf.extract(message)
            accountId = res.ctidTraderAccountId
        
            if showStartupOutput:
                print("🧾 Full Reconciliation Response:")
                print(res)
        
            if accountId in pendingReconciliations:
                pendingReconciliations.remove(accountId)
        
            # --- build new positions dict from reconcile ---
            new_positions = {p.positionId: p for p in getattr(res, "position", [])}
            old_pos_ids = set(positionsById.keys())
            new_pos_ids = set(new_positions.keys())
            added_ids   = new_pos_ids - old_pos_ids
            removed_ids = old_pos_ids - new_pos_ids
            
            # --- (un)subscribe symbols based on added/removed positions ---
            if liveViewerActive:
                added_symbols = {new_positions[pid].tradeData.symbolId for pid in added_ids}
                for sid in added_symbols:
                    if sid not in subscribedSymbols:
                        sendProtoOASubscribeSpotsReq(sid)
        
        
                # Unsubscribe: symbols no longer referenced by any position
                # (Compute against the updated positionsById)
                # ✅ Use the freshly reconciled set
                remaining_symbols = {p.tradeData.symbolId for p in new_positions.values()}
                for sid in list(subscribedSymbols):
                    if sid not in remaining_symbols:
                        try:
                            sendProtoOAUnsubscribeSpotsReq(sid)
                        finally:
                            subscribedSymbols.discard(sid)
            positionsById.clear()
            positionsById.update(new_positions)
            # --- the order of rows may change; mark cache dirty once ---
            mark_positions_dirty()
        
            # --- refresh PnL + UI if viewer is active ---
            if liveViewerActive:
                sendProtoOAGetPositionUnrealizedPnLReq()
                printLivePnLTable()
        
            # --- optional logging for orders ---
            if res.order:
#                 print(f"📦 Active Orders ({len(res.order)}):")
                for order in res.order:
                    try:
                        order_id = getattr(order, "orderId", "N/A")
                        symbol_id = getattr(order, "symbolId", None)
                        symbol_name = symbolIdToName.get(symbol_id, f"ID:{symbol_id}" if symbol_id else "UNKNOWN")
                        status = ProtoOAOrderStatus.Name(order.orderStatus) if hasattr(order, "orderStatus") else "UNKNOWN"
                        print(f" - Order ID: {order_id}, Symbol: {symbol_name}, Status: {status}")
                    except Exception as e:
                        print(f"❌ Error displaying order: {e}")
                        print(f"Raw order object:\n{order}")
            else:
                print("📦 No active orders.")
        
            reactor.callLater(0.5, sendProtoOATraderReq, accountId)
    
    
    


        elif message.payloadType == ProtoOAGetTrendbarsRes().payloadType:
            res = Protobuf.extract(message)
            print(f"📉 {len(res.trendbar)} trendbars received.")
            returnToMenu()


        elif message.payloadType == ProtoOAGetTickDataRes().payloadType:
            res = Protobuf.extract(message)
            symbolId = res.symbolId
            symbolName = symbolIdToName.get(symbolId, f"ID:{symbolId}")
            pips = symbolIdToPips.get(symbolId, 5)

            if not res.tickData:
                print(f"⚠️ No tick data for {symbolName}")
                return

            latest = sorted(res.tickData, key=lambda x: x.timestamp, reverse=True)[0]
            bid = latest.bid / (10 ** pips) if latest.HasField("bid") else None
            ask = latest.ask / (10 ** pips) if latest.HasField("ask") else None

            # Ensure at least one side is used
            bid = bid or ask or 0
            ask = ask or bid or 0

            symbolIdToPrice[symbolId] = (bid, ask)
            print(f"📊 {symbolName} — Tick Price Fallback — Bid: {bid}, Ask: {ask}")

        #
        elif message.payloadType == ProtoOAExecutionEvent().payloadType:
            res = Protobuf.extract(message)
            exec_type = res.executionType
            print(f"📥 Execution Event: {exec_type} for Order ID {res.orderId}")
        
            if res.HasField("positionId"):
                pos_id = res.positionId
        
                # Close-like events (CLOSE_POSITION, DEAL_CANCEL, etc.)
                close_like = {
                    ProtoOAExecutionType.CLOSE_POSITION,
                    ProtoOAExecutionType.ORDER_CANCEL,
                    ProtoOAExecutionType.DEAL_CANCEL
                }
        
                if exec_type in close_like:
                    print(f"🗑 Removing closed/finished position {pos_id} due to {exec_type}")
                    remove_position(pos_id)  # clamps index & refreshes table
                elif exec_type == ProtoOAExecutionType.ORDER_FILLED:  # new or modified
                    # try to fetch the position details
                    runWhenReady(lambda: sendProtoOAReconcileReq(currentAccountId))
                    # if the broker includes position info in the execution event:
                    if res.HasField("position"):
                        add_position(res.position)
                else:
                    # For modifications or openings — just reconcile
                    runWhenReady(sendProtoOAReconcileReq, currentAccountId)


        elif message.payloadType == 2103:
            try:
                res = Protobuf.extract(message)
                print("📩 Possibly Auth/Execution Response:", res)
            except Exception:
                print("⚠️ Could not decode payloadType 2103")


        elif message.payloadType == ProtoOATraderRes().payloadType:
            res = Protobuf.extract(message)
            trader = res.trader
            if showStartupOutput:
                print("📦 Full trader message:\n", trader)

            accountId = trader.ctidTraderAccountId  # ✅ Assign this BEFORE using it

            print(f"✅ Trader info received for {accountId}")

            accountTraderInfo[accountId] = trader  # Save trader info

            if accountId not in accountTraderInfo:
                accountTraderInfo[accountId] = trader

            if not currentAccountId and accountId in authorizedAccounts and accountId not in pendingReconciliations:
                currentAccountId = accountId
                print(f"✅ currentAccountId is now set to: {currentAccountId}")

            print(f"\n💰 Account {accountId}:")
            print(f" - Balance: {trader.balance / 100:.2f}")

#             if trader.HasField("equity"):
#                 print(f" - Equity: {trader.equity / 100:.2f}")
#             else:
#                 print(" - Equity: [Not available]")
#             print(f" - Margin Free: {trader.freeMargin / 100:.2f}")
#             print(f" - Leverage: {trader.leverage}")

            if len(accountTraderInfo) == len(availableAccounts):
                promptUserToSelectAccount()

        elif message.payloadType == ProtoOADealOffsetListRes().payloadType:
            res = Protobuf.extract(message)
            print(f"🧾 Deal Offsets: {len(res.offset)} entries.")
            returnToMenu()


        elif message.payloadType == ProtoOAGetPositionUnrealizedPnLRes().payloadType:
            res = Protobuf.extract(message)

            # Try both possible protobuf fields
            money_digits = getattr(res, "moneyDigits", 2)  # fallback to 2
            unrealized_list = getattr(res, "positionUnrealizedPnL", None)

            if not unrealized_list:
                print("📦 Full ProtoOAGetPositionUnrealizedPnLRes message (formatted):")
                print(f"moneyDigits: {money_digits}")

                fallback_list = getattr(res, "unrealizedPnL", None) or getattr(res, "unrealisedPnL", None)
                if fallback_list:
                    for pnl in fallback_list:
                        net_usd = pnl.netUnrealizedPnL / (10 ** money_digits)
                        gross_usd = pnl.grossUnrealizedPnL / (10 ** money_digits)
                        print(f" - Position ID: {pnl.positionId:<12} | Gross: ${gross_usd:.2f} | Net: ${net_usd:.2f}")
            else:
                total_net_pnl = 0.0

                for pnl in unrealized_list:
                    try:
                        # Convert raw values to human-readable dollar amounts
                        net_usd = pnl.netUnrealizedPnL / (10 ** money_digits)
                        gross_usd = pnl.grossUnrealizedPnL / (10 ** money_digits)
                        total_net_pnl += net_usd


                        prev = positionPnLById.get(pnl.positionId)
                        positionPnLById[pnl.positionId] = net_usd
                        if prev != net_usd:
                            mark_positions_dirty()

                        # Try to show symbol name
                        pos = positionsById.get(pnl.positionId)
                        if pos:
                            symbol_id = pos.tradeData.symbolId
                            symbol_name = symbolIdToName.get(symbol_id, f"ID:{symbol_id}")
                        else:
                            symbol_name = "[unknown]"

                        def colorize(amount):
                            if amount > 0:
                                return f"\033[92m${amount:.2f}\033[0m"
                            elif amount < 0:
                                return f"\033[91m${amount:.2f}\033[0m"
                            return f"${amount:.2f}"

                        gross_label = colorize(gross_usd)
                        net_label = colorize(net_usd)

#                         print(f"📊 Position {pnl.positionId:<12} | Symbol: {symbol_name:<10} | Gross: {gross_label:>10} | Net: {net_label:>10}")

                    except Exception as e:
                        print(f"❌ Error storing/displaying PnL for position {pnl.positionId}: {e}")

                # ✅ Print the full table ONCE after the loop
                printLivePnLTable()

                # 💰 Show total net
                total_label = f"\033[92m${total_net_pnl:.2f}\033[0m" if total_net_pnl > 0 else (
                              f"\033[91m${total_net_pnl:.2f}\033[0m" if total_net_pnl < 0 else f"${total_net_pnl:.2f}")
#                 print(f"\n💰 Total Net Unrealized PnL: {total_label}")

                # Move cursor up one line and overwrite
                print(f"\033[F\033[K💰 Total Net Unrealized PnL: {total_label}")


        elif message.payloadType == ProtoOAOrderDetailsRes().payloadType:
            res = Protobuf.extract(message)
            print(f"📄 Order Details - ID: {res.order.orderId}, Status: {res.order.orderStatus}")
            returnToMenu()

        elif message.payloadType == ProtoOAOrderListByPositionIdRes().payloadType:
            res = Protobuf.extract(message)
            print(f"📋 Orders in Position: {len(res.order)}")
            returnToMenu()


        elif message.payloadType == 2142:  # ProtoOAErrorRes
            try:
                res = ProtoOAErrorRes()
                res.ParseFromString(message.payload)
                print(f"❌ ERROR: {res.errorCode} — {res.description}")

                # Optional: stop app if account auth failed
                if res.errorCode in ["ACCOUNT_NOT_AUTHORIZED", "CH_CTID_TRADER_ACCOUNT_NOT_FOUND"]:
                    print("🚫 Account authorization failed — please check ACCOUNT_ID in .env or use option 1 to list valid accounts.")
                    reactor.stop()

            except Exception as e:
                print(f"❌ Failed to parse error message: {e}")
                print(f"Payload: {message.payload}")
            returnToMenu()

        else:
            print(f"⚠️ Unhandled message — payloadType: {message.payloadType}")
#             print(f"Raw payload (first 100 bytes): {message.payload[:100]!r}")



    def onMessageReceived(client, message):
        if liveViewerActive:
            with suppress_stdout():
                handle_message(client, message)
        else:
            handle_message(client, message)


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
#             reactor.callLater(1.0, sendProtoOASymbolsListReq)  # <-- THIS

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
            sendProtoOAReconcileReq()  # <-- This is essential!

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

#     def sendProtoOASubscribeSpotsReq(symbolId, timeInSeconds, subscribeToSpotTimestamp	= False, clientMsgId = None):
#         global client
#         request = ProtoOASubscribeSpotsReq()
#         request.ctidTraderAccountId = currentAccountId
#         request.symbolId.append(int(symbolId))
#         request.subscribeToSpotTimestamp = subscribeToSpotTimestamp if type(subscribeToSpotTimestamp) is bool else bool(subscribeToSpotTimestamp)
#         deferred = client.send(request, clientMsgId = clientMsgId)
#         deferred.addErrback(onError)
#         reactor.callLater(int(timeInSeconds), sendProtoOAUnsubscribeSpotsReq, symbolId)

    #

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
        # ✅ Only auto-unsubscribe if timeInSeconds is set
#         if timeInSeconds:
#             def unsubscribeAndRemove():
#                 sendProtoOAUnsubscribeSpotsReq(symbolId)
#                 subscribedSymbols.discard(symbolId)
#                 print(f"🔕 Auto-unsubscribed from {symbolId} after {timeInSeconds}s")
#     
#             reactor.callLater(int(timeInSeconds), unsubscribeAndRemove)


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
        request.volume = int(volume) * 100
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


#     def startPnLUpdateLoop(interval=1.0):
#         """Continuously poll Unrealized PnL every interval seconds"""
#         if not currentAccountId or currentAccountId not in authorizedAccounts:
#             print("⚠️ Cannot start PnL loop – account not ready.")
#             return
#
#         sendProtoOAGetPositionUnrealizedPnLReq()
#         reactor.callLater(interval, startPnLUpdateLoop, interval)


    def subscribeToSymbolsFromOpenPositions(duration=None):
        seen = set()
        for pos in positionsById.values():
            symbolId = pos.tradeData.symbolId
            if symbolId not in seen:
                seen.add(symbolId)
                sendProtoOASubscribeSpotsReq(symbolId, timeInSeconds=duration)


    def unlock_keyboard_caps():
        print("🔓 Sending CAPSLOCK toggle...")
        pyautogui.press('capslock')  # or system-specific code
    

    def launchLivePnLViewer():
        global liveViewerActive, live, selected_position_index
        liveViewerActive = True
        startPositionPolling(5.0)
        reactor.callLater(10.0, unlock_keyboard_caps)
        # start key-listener (it expects *no* positional args)
#         threading.Thread(target=listen_for_keys, daemon=True).start()
    
        print("🔃 Subscribing to spot prices for open positions...")
    
        subscribeToSymbolsFromOpenPositions()
    
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
   
        def startViewer():
            def render():
                global live
                sendProtoOAGetPositionUnrealizedPnLReq()
                reactor.callLater(0.1, printLivePnLTable)
                startPnLUpdateLoop(0.3)
                threading.Thread(target=listen_for_keys, daemon=True).start()
                console.print("\n[dim]🔴 Press 'q' and Enter to exit viewer[/dim]")
        # 🎯 Render function
        def render():
            table, msg = buildLivePnLTable()
            grid = Table.grid(padding=(1, 1))
            grid.add_row(table)
            if msg:
                grid.add_row(f"[red]⚠ {msg}[/red]")
            return grid

        # create a *global* Live instance (no with-block)
        live = Live(render(), refresh_per_second=20, screen=True)
        live.start()

        # fire off background tasks
        sendProtoOAGetPositionUnrealizedPnLReq()
        startPnLUpdateLoop(0.3)
        threading.Thread(target=listen_for_keys, daemon=True).start()
#         console.print("\n[dim]🔴  q → quit j/k → move ⏎ → details[/dim]")


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
    
            # NEW: table order changed
            mark_positions_dirty()
    
            # selection / viewport housekeeping (unchanged)
            total = len(ordered_positions())
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

    def safe_current_selection():
        ops = ordered_positions()
        n = len(ops)
        if n == 0:
            return None
        i = max(0, min(selected_position_index, n - 1))
        return ops[i]
    
    def listen_for_keys() -> None:
        global selected_position_index, liveViewerActive
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
    
            def move_selection(delta: int) -> None:
                global selected_position_index, view_offset
                ops = ordered_positions()
                n = len(ops)
                if n == 0:
                    return  # ← prevents modulo-by-zero when list is briefly empty
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
                    key = sys.stdin.read(1)
                    if key == "q":
                        liveViewerActive = False
                        reactor.callFromThread(live.stop)
                        print("👋 Exiting Live PnL Viewer...")
                        reactor.callLater(0.5, executeUserCommand)
                        break
                    elif key == "j":
                        move_selection(+1)
                    elif key == "k":
                        move_selection(-1)
                    elif key == "x":
                        sel = safe_current_selection()
                        if not sel:
                            continue
                        pos_id, pos = sel
                        contract_size = symbolIdToDetails.get(pos.tradeData.symbolId, {}).get("contractSize", 1) or 1
                        volume_units = pos.tradeData.volume
                        reactor.callFromThread(sendProtoOAClosePositionReq, pos_id, volume_units / 100)
                        reactor.callFromThread(remove_position, pos_id)
                        reactor.callLater(2.0, lambda: runWhenReady(sendProtoOAReconcileReq, currentAccountId))
                    elif key == "\r":
                        sel = safe_current_selection()
                        if not sel:
                            continue
                        pos_id, pos = sel
                        # show details...
                except Exception as e:
                    logging.error("Key thread error: %s\n%s", e, traceback.format_exc())
                    # keep the loop alive
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    
#         def waitForExit():
#             global selectedPositionIndex
#             def check_input():
#                 global liveViewerActive
#                 while liveViewerActive:
#                     user_input = sys.stdin.readline().strip().lower()
#                     if user_input == "q":
#                         liveViewerActive = False
#                         live.stop()
#                         print("👋 Exiting Live PnL Viewer...")
#                         reactor.callLater(0.5, executeUserCommand)
#                         break
#     
#         waitUntilAllPositionPrices(startViewer)
#         waitForExit()
    
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
            sendProtoOAClosePositionReq(result)
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

    def printUpdatedPriceBoard():
        print("\n📊 Updated Spot Prices:")
        missing = []
    
        for symbolId, name in symbolIdToName.items():
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
            # 👇 Add a recurring call
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


def executeUserCommand():
    global menuScheduled
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
