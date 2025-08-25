# message_handlers.py
from typing import Callable, Dict, Any
from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
import logging
import ui_helpers as H
MessageContext = Any

Handler = Callable[[Any, Any], None]  # ctx is just Any now

_registry: Dict[int, Handler] = {}

def register(payload_cls_or_id):
    """Decorator to register a handler by proto class or numeric id."""
    if isinstance(payload_cls_or_id, int):
        pt = int(payload_cls_or_id)
    else:
        pt = payload_cls_or_id().payloadType
    def _wrap(fn: Handler):
        _registry[pt] = fn
        return fn
    return _wrap

def dispatch_message(client, raw_message, ctx: Any):
    pt = raw_message.payloadType
    handler = _registry.get(pt)
    # some payloads we register by number (2101, 2103, 2142)
    if not handler and isinstance(pt, int):
        handler = _registry.get(int(pt))

    if not handler:
        # ignore frequent keepalives silently
        if pt in {ProtoOAAccountLogoutRes().payloadType, ProtoHeartbeatEvent().payloadType}:
            return
        print(f"⚠️ Unhandled message — payloadType: {pt}")
        return

    # For 2142 we parse manually below; others use Protobuf.extract
    if pt == 2142:
        res = ProtoOAErrorRes()
        try:
            res.ParseFromString(raw_message.payload)
        except Exception as e:
            print(f"❌ Failed to parse ProtoOAErrorRes: {e}")
            print(f"Payload: {raw_message.payload}")
            return
        return handler(res, ctx)

    # normal path
    decoded = Protobuf.extract(raw_message)
    return handler(decoded, ctx)

# -------------------- Handlers (one per old elif) --------------------

@register(ProtoOASubscribeSpotsRes)
def on_subscribe_spots(res: ProtoOASubscribeSpotsRes, ctx: MessageContext):
    print(f"✅ Spot subscription confirmed: {res}")
    ctx.receivedSpotConfirmations += 1
    if ctx.receivedSpotConfirmations >= ctx.expectedSpotSubscriptions:
        print("✅ All spot subscriptions confirmed. Starting price board loop.")
        ctx.reactor.callLater(0.5, ctx.printUpdatedPriceBoard)

@register(ProtoOASymbolsListRes)
def on_symbols_list(res: ProtoOASymbolsListRes, ctx: MessageContext):
    print(f"📈 Received {len(res.symbol)} symbols:")

    for s in res.symbol:
        ctx.symbolIdToPips[s.symbolId] = getattr(s, "pipsPosition", 5)
        ctx.symbolIdToDetails[s.symbolId] = {
            "name": s.symbolName,
            "pips": getattr(s, "pipsPosition", 5),
            "contractSize": getattr(s, "contractSize", 1.0),
            "assetClass": getattr(s, "assetClassName", "Unknown"),
        }
        ctx.symbolIdToName[s.symbolId] = s.symbolName

    open_position_symbols = {p.tradeData.symbolId for p in ctx.positionsById.values()}
    new_to_sub = {sid for sid in open_position_symbols if sid not in ctx.subscribedSymbols}
    ctx.receivedSpotConfirmations = 0
    ctx.expectedSpotSubscriptions = len(new_to_sub)

    for sid in new_to_sub:
        ctx.sendProtoOASubscribeSpotsReq(sid)

    def fetch_missing_ticks():
        for sid in list(ctx.subscribedSymbols):
            if sid not in ctx.symbolIdToPrice:
                ctx.sendProtoOAGetTickDataReq(1, "BID", sid)

    ctx.reactor.callLater(0.5, fetch_missing_ticks)
    ctx.reactor.callLater(1.0, ctx.printUpdatedPriceBoard)
    ctx.returnToMenu()

# @register(ProtoOASpotEvent)
# def on_spot(res: ProtoOASpotEvent, ctx: MessageContext):
#     try:
#         symbolId = res.symbolId
#         pips = ctx.symbolIdToPips.get(symbolId, 5)
#         bid = res.bid / (10 ** pips)
#         ask = res.ask / (10 ** pips)
#         ctx.symbolIdToPrice[symbolId] = (bid, ask)
#         if ctx.liveViewerActive:
#             ctx.printLivePnLTable()
#     except Exception as e:
#         print(f"❌ Failed to parse SpotEvent: {e}")


# @register(ProtoOASpotEvent)
# def on_spot(res: ProtoOASpotEvent, ctx: MessageContext):
#     try:
#         sid  = res.symbolId
#         pips = ctx.symbolIdToPips.get(sid, 5)
#         bid  = res.bid / (10 ** pips)
#         ask  = res.ask / (10 ** pips)
# 
#         ctx.symbolIdToPrice[sid] = (bid, ask)
# 
#         # 🔥 keep sort and TOTAL in sync with the live tick
# #         ctx.update_pnl_cache_for_symbol(sid)
# 
#         if ctx.liveViewerActive:
#             ctx.request_render()
#     except Exception as e:
#         print(f"❌ Failed to parse SpotEvent: {e}")


# message_handlers.py

@register(ProtoOASpotEvent)
def on_spot(res: ProtoOASpotEvent, ctx):
    try:
        sid  = res.symbolId
        pips = ctx.symbolIdToPips.get(sid, 5)

        prev_bid, prev_ask = ctx.symbolIdToPrice.get(sid, (None, None))

        bid = (res.bid / (10 ** pips)) if res.bid else None
        ask = (res.ask / (10 ** pips)) if res.ask else None

        # keep last non-zero values
        bid = bid if (bid and bid != 0.0) else prev_bid
        ask = ask if (ask and ask != 0.0) else prev_ask

        # only store if we have at least one side
        if bid is not None or ask is not None:
            if bid is None: bid = ask
            if ask is None: ask = bid
            ctx.symbolIdToPrice[sid] = (bid, ask)

        if ctx.liveViewerActive:
            ctx.request_render()
    except Exception as e:
        print(f"❌ Failed to parse SpotEvent: {e}")


@register(ProtoOAAssetListRes)
def on_asset_list(res: ProtoOAAssetListRes, ctx: MessageContext):
    print(f"📊 Received {len(res.asset)} assets:")
    for asset in res.asset[:5]:
        print(f" - {asset.name} ({asset.assetId})")
    ctx.returnToMenu()

@register(ProtoOAVersionRes)
def on_version(res: ProtoOAVersionRes, ctx: MessageContext):
    print(f"🔧 Version Info: {res.version}")
    ctx.returnToMenu()

@register(2101)
def on_2101(decoded_any, ctx: MessageContext):
    """Debug dump for payloadType 2101 (kept as-is)."""
    try:
        res = decoded_any
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

@register(ProtoOAAssetClassListRes)
def on_asset_class_list(res: ProtoOAAssetClassListRes, ctx: MessageContext):
    print(f"🏷️ Asset Classes: {len(res.assetClass)} found.")
    ctx.returnToMenu()

@register(ProtoOASymbolCategoryListRes)
def on_symbol_category_list(res: ProtoOASymbolCategoryListRes, ctx: MessageContext):
    print(f"🗂️ Symbol Categories: {len(res.category)}")
    ctx.returnToMenu()

@register(ProtoOAGetAccountListByAccessTokenRes)
def on_account_list(res: ProtoOAGetAccountListByAccessTokenRes, ctx: MessageContext):
    # keep your existing side-effects
    def _on_received():
        # reuse your existing helper
        apiAccountIds = [acc.ctidTraderAccountId for acc in res.ctidTraderAccount]
        valid = list(set(ctx.envAccountIds) & set(apiAccountIds))
        if valid:
            print("✅ Valid accounts from .env matched the API response:")
            for accId in valid:
                print(f" - {accId}")
                if accId not in ctx.authorizedAccounts:
                    ctx.fetchTraderInfo(accId)
                    ctx.reactor.callLater(0, ctx.returnToMenu)
        else:
            print("⚠️ None of the ACCOUNT_IDS from .env matched available accounts.")
            print("Use menu option 2 to manually authorize one.")
            ctx.returnToMenu()

    # Also mirror your onAccountListReceived logic (metadata + printing)
    ctx.availableAccounts[:] = [acc.ctidTraderAccountId for acc in res.ctidTraderAccount]
    for acc in res.ctidTraderAccount:
        acc_id = acc.ctidTraderAccountId
        currency = getattr(acc, "depositCurrency", "?")
        broker = getattr(acc, "brokerName", "?")
        is_live = "Live" if getattr(acc, "isLive", False) else "Demo"
        ctx.accountMetadata[acc_id] = {"currency": currency, "broker": broker, "isLive": is_live}
        print(f" - ID: {acc_id}, Type: {is_live}, Broker: {broker}, Currency: {currency}")

    _on_received()

@register(ProtoOAReconcileRes)
def on_reconcile(res: ProtoOAReconcileRes, ctx: MessageContext):
    accountId = res.ctidTraderAccountId

    if ctx.showStartupOutput:
        print("🧾 Full Reconciliation Response:")
        print(res)

    if accountId in ctx.pendingReconciliations:
        ctx.pendingReconciliations.discard(accountId)

    new_positions = {p.positionId: p for p in getattr(res, "position", [])}
    old_pos_ids = set(ctx.positionsById.keys())
    new_pos_ids = set(new_positions.keys())
    added_ids   = new_pos_ids - old_pos_ids

    if ctx.liveViewerActive:
        added_symbols = {new_positions[pid].tradeData.symbolId for pid in added_ids}
        for sid in added_symbols:
            if sid not in ctx.subscribedSymbols:
                ctx.sendProtoOASubscribeSpotsReq(sid)

        remaining_symbols = {p.tradeData.symbolId for p in new_positions.values()}
        for sid in list(ctx.subscribedSymbols):
            if sid not in remaining_symbols:
                try:
                    ctx.sendProtoOAUnsubscribeSpotsReq(sid)
                finally:
                    ctx.subscribedSymbols.discard(sid)

    ctx.positionsById.clear()
    ctx.positionsById.update(new_positions)
    H.mark_positions_dirty()

    if ctx.liveViewerActive:
        ctx.sendProtoOAGetPositionUnrealizedPnLReq()
        ctx.printLivePnLTable()

    if res.order:
        for order in res.order:
            try:
                order_id = getattr(order, "orderId", "N/A")
                symbol_id = getattr(order, "symbolId", None)
                symbol_name = ctx.symbolIdToName.get(symbol_id, f"ID:{symbol_id}" if symbol_id else "UNKNOWN")
                status = ProtoOAOrderStatus.Name(order.orderStatus) if hasattr(order, "orderStatus") else "UNKNOWN"
                print(f" - Order ID: {order_id}, Symbol: {symbol_name}, Status: {status}")
            except Exception as e:
                print(f"❌ Error displaying order: {e}\n{order}")
    else:
        print("📦 No active orders.")

    ctx.reactor.callLater(0.5, ctx.sendProtoOATraderReq, accountId)

@register(ProtoOAGetTrendbarsRes)
def on_trendbars(res: ProtoOAGetTrendbarsRes, ctx: MessageContext):
    print(f"📉 {len(res.trendbar)} trendbars received.")
    ctx.returnToMenu()

@register(ProtoOAGetTickDataRes)
def on_tickdata(res: ProtoOAGetTickDataRes, ctx: MessageContext):
    try:
        symbolId = getattr(res, "symbolId", None)
        if symbolId is None:
            print("⚠️ TickDataRes missing symbolId; ignoring this response")
            return

        symbolName = ctx.symbolIdToName.get(symbolId, f"ID:{symbolId}")
        pips = ctx.symbolIdToPips.get(symbolId, 5)

        ticks = list(getattr(res, "tickData", []))
        if not ticks:
            print(f"⚠️ No tick data for {symbolName}")
            return

        latest = max(ticks, key=lambda x: getattr(x, "timestamp", 0))
        raw_bid = getattr(latest, "bid", 0)
        raw_ask = getattr(latest, "ask", 0)

        bid = (raw_bid / (10 ** pips)) if raw_bid else None
        ask = (raw_ask / (10 ** pips)) if raw_ask else None

        if bid is None and ask is None:
            print(f"⚠️ Latest tick has no bid/ask for {symbolName}")
            return
        if bid is None: bid = ask
        if ask is None: ask = bid

        ctx.symbolIdToPrice[symbolId] = (bid, ask)
        print(f"📊 {symbolName} — Tick Price Fallback — Bid: {bid}, Ask: {ask}")

        if ctx.liveViewerActive:
            ctx.update_pnl_cache_for_symbol(symbolId)
            ctx.request_render()
    except Exception as e:
        logging.error("TickData handler error: %s", e)

@register(ProtoOAExecutionEvent)
def on_execution(res: ProtoOAExecutionEvent, ctx: MessageContext):
    try:
        exec_type = res.executionType
        print(f"📥 Execution Event: {ProtoOAExecutionType.Name(exec_type)} for Order ID {getattr(res,'orderId','N/A')}")

        if exec_type == ProtoOAExecutionType.ORDER_FILLED:
            ctx.print_order_filled_event(res)

            if hasattr(res, "position") and res.HasField("position"):
                ctx.add_position(res.position)
                ctx.sendProtoOAGetPositionUnrealizedPnLReq()
                ctx.printLivePnLTable()
            else:
                ctx.runWhenReady(ctx.sendProtoOAReconcileReq, ctx.currentAccountId)
                if hasattr(res, "orderId"):
                    ctx.runWhenReady(ctx.sendProtoOAOrderDetailsReq, res.orderId)
            return

        pos_id = res.positionId if res.HasField("positionId") else None

        close_like = {
            ProtoOAExecutionType.CLOSE_POSITION,
            ProtoOAExecutionType.ORDER_CANCEL,
            ProtoOAExecutionType.DEAL_CANCEL,
        }

        if exec_type in close_like and pos_id:
            print(f"🗑 Removing position {pos_id} due to {ProtoOAExecutionType.Name(exec_type)}")
            ctx.remove_position(pos_id)
        else:
            ctx.runWhenReady(ctx.sendProtoOAReconcileReq, ctx.currentAccountId)

    except Exception as e:
        ctx.log_exec_event_error(res, e)

@register(2103)
def on_2103(decoded_any, ctx: MessageContext):
    try:
        print("📩 Possibly Auth/Execution Response:", decoded_any)
    except Exception:
        print("⚠️ Could not decode payloadType 2103")


# message_handlers.py
@register(ProtoOATraderRes)
def on_trader(res: ProtoOATraderRes, ctx: MessageContext):
    trader = res.trader
    accountId = trader.ctidTraderAccountId

    ctx.accountTraderInfo[accountId] = trader

    if (ctx.currentAccountId is None
        and accountId in ctx.authorizedAccounts
        and accountId not in ctx.pendingReconciliations):
        ctx.set_current_account_id(accountId)   # <— instead of assigning ctx.currentAccountId
        print(f"✅ currentAccountId is now set to: {accountId}")

    print(f"\n💰 Account {accountId}:\n - Balance: {trader.balance / 100:.2f}")

    if len(ctx.accountTraderInfo) == len(ctx.availableAccounts):
        ctx.promptUserToSelectAccount()
        ctx.returnToMenu()


@register(ProtoOADealOffsetListRes)
def on_deal_offset_list(res: ProtoOADealOffsetListRes, ctx: MessageContext):
    print(f"🧾 Deal Offsets: {len(res.offset)} entries.")
    ctx.returnToMenu()

@register(ProtoOAGetPositionUnrealizedPnLRes)
def on_unrealized(res: ProtoOAGetPositionUnrealizedPnLRes, ctx: MessageContext):
    money_digits = getattr(res, "moneyDigits", 2)
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
        return

    total_net_pnl = 0.0
    for pnl in unrealized_list:
        try:
            net_usd = pnl.netUnrealizedPnL / (10 ** money_digits)
            gross_usd = pnl.grossUnrealizedPnL / (10 ** money_digits)
            total_net_pnl += net_usd

            pid = pnl.positionId
            prev = ctx.positionPnLById.get(pid)
            ctx.positionPnLById[pid] = net_usd
            if prev != net_usd:
                H.mark_positions_dirty()

            pos = ctx.positionsById.get(pid)
            if pos:
                symbol_id = pos.tradeData.symbolId
                _ = ctx.symbolIdToName.get(symbol_id, f"ID:{symbol_id}")

            # SL check (account currency max loss)
            sl_val = ctx.slByPositionId.get(pid)
            if sl_val is not None and net_usd <= -abs(sl_val):
                if pos:
                    volume_units = pos.tradeData.volume
                    ctx.slByPositionId.pop(pid, None)
                    ctx.reactor.callLater(0, ctx.sendProtoOAClosePositionReq, pid, volume_units / 100)
                    msg = f"SL hit on {pid}: closing at net {net_usd:.2f}"
                    ctx.error_messages.append(msg)
                    if len(ctx.error_messages) > 6:
                        ctx.error_messages.pop(0)

        except Exception as e:
            print(f"❌ Error storing/displaying PnL for position {getattr(pnl, 'positionId', '?')}: {e}")

    ctx.request_render()

@register(ProtoOAOrderDetailsRes)
def on_order_details(res: ProtoOAOrderDetailsRes, ctx: MessageContext):
    print(f"📄 Order Details - ID: {res.order.orderId}, Status: {res.order.orderStatus}")
    ctx.returnToMenu()

@register(ProtoOAOrderListByPositionIdRes)
def on_order_list_by_pos(res: ProtoOAOrderListByPositionIdRes, ctx: MessageContext):
    print(f"📋 Orders in Position: {len(res.order)}")
    ctx.returnToMenu()

@register(2142)  # ProtoOAErrorRes
def on_error_res(res: ProtoOAErrorRes, ctx: MessageContext):
    print(f"❌ ERROR: {res.errorCode} — {res.description}")
    if res.errorCode in ["ACCOUNT_NOT_AUTHORIZED", "CH_CTID_TRADER_ACCOUNT_NOT_FOUND"]:
        print("🚫 Account authorization failed — please check ACCOUNT_ID in .env or use option 1 to authorize.")
        # If you want to stop the reactor, do it in main where you own reactor lifecycle.
    ctx.returnToMenu()
