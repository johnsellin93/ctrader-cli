
# ui_helpers.py
from typing import Dict, Tuple, List, Optional
from contextlib import contextmanager
from rich.table import Table
from rich.console import Group
from rich import box
import sys, os
import datetime as dt

# Wired from main via init_ordering()
positionsById: Dict[int, object] = {}
positionPnLById: Dict[int, float] = {}

# Cached sort
_positions_sorted_cache: List[Tuple[int, object]] = []
_positions_sorted_dirty: bool = True

_TRADE_SIDE = {1: "BUY", 2: "SELL"}
def trade_side_name(value) -> str:
    try:
        return _TRADE_SIDE.get(int(value), str(value))
    except Exception:
        return str(value)

def init_ordering(positions_ref: Dict[int, object], pnl_ref: Dict[int, float]) -> None:
    global positionsById, positionPnLById
    positionsById = positions_ref
    positionPnLById = pnl_ref

def mark_positions_dirty() -> None:
    global _positions_sorted_dirty
    _positions_sorted_dirty = True

def _rebuild_sorted_cache() -> None:
    global _positions_sorted_cache, _positions_sorted_dirty
    _positions_sorted_cache = sorted(
        positionsById.items(),
        key=lambda item: positionPnLById.get(item[0], 0.0),
        reverse=True,
    )
    _positions_sorted_dirty = False

def ordered_positions() -> List[Tuple[int, object]]:
    if _positions_sorted_dirty:
        _rebuild_sorted_cache()
    return _positions_sorted_cache

def safe_current_selection(selected_index: int) -> Optional[Tuple[int, object]]:
    ops = ordered_positions()
    if not ops:
        return None
    i = max(0, min(selected_index, len(ops) - 1))
    return ops[i]

def colorize_number(amount: Optional[float]) -> str:
    if amount is None:
        return "[N/A]"
    formatted = f"{amount:.2f}"
    padded = f"{formatted:>10}"
    if amount > 0:
        return f"\033[92m{padded}\033[0m"
    if amount < 0:
        return f"\033[91m{padded}\033[0m"
    return padded

def colorize(amount: float) -> str:
    if amount > 0:
        return f"\033[92m${amount:.2f}\033[0m"
    if amount < 0:
        return f"\033[91m${amount:.2f}\033[0m"
    return f"${amount:.2f}"

def format_lots(volume_units: int, with_suffix: bool = True) -> str:
    # Your app uses centi-lots: 100 units = 1 lot
    lots = volume_units / 10000000
    s = f"{lots:,.2f}"
    return f"{s} Lots" if with_suffix else s

@contextmanager
def suppress_stdout(active: bool, to_file: bool = True, logfile_path: str = "live_pnl_stdout.log"):
    """
    Suppress stdout/stderr while the live viewer is active.
    Use: with H.suppress_stdout(liveViewerActive): ...
    """
    if to_file and active:
        with open(logfile_path, "a") as logfile:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = logfile
            sys.stderr = logfile
            try:
                yield
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
    else:
        with open(os.devnull, "w") as fnull:
            old_stdout = sys.stdout
            sys.stdout = fnull
            try:
                yield
            finally:
                sys.stdout = old_stdout

def buildLivePnLTable(
    console_height: int,
    positions_sorted: List[Tuple[int, object]],
    selected_index: int,
    view_offset: int,
    symbolIdToName: Dict[int, str],
    symbolIdToDetails: Dict[int, dict],
    symbolIdToPrice: Dict[int, Tuple[Optional[float], Optional[float]]],
    positionPnLById_map: Dict[int, float],
    error_messages: List[str],
):
    """
    Build and return (rich.Table, msg, selected_index, view_offset).
    Pure: no module globals.
    """

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
    table.add_column("Market",       width=10, no_wrap=True)
    table.add_column("PnL",          justify="right")

    n = len(positions_sorted)
    max_rows = max(1, console_height - 8)

    if n == 0:
        selected_index = 0
        view_offset = 0
    else:
        if selected_index >= n:
            selected_index = n - 1
        if selected_index < view_offset:
            view_offset = selected_index
        elif selected_index >= view_offset + max_rows:
            view_offset = max(0, selected_index - max_rows + 1)

    visible = positions_sorted[view_offset : view_offset + max_rows]
    total_pnl = 0.0
    now_utc = dt.datetime.now(dt.timezone.utc)

    for global_idx, (posId, pos) in enumerate(visible, start=view_offset):
        try:
            is_selected = global_idx == selected_index
            pos_id_label = f"    ⮞ {posId}" if is_selected else str(posId)
            row_style = "bold on blue" if is_selected else ""

            symbol_id   = pos.tradeData.symbolId
            symbol_name = symbolIdToName.get(symbol_id, f"ID:{symbol_id}")

            side_raw = trade_side_name(pos.tradeData.tradeSide)
            side = "[green]BUY[/green]" if side_raw == "BUY" else "[red]SELL[/red]"

            opened_at_utc = dt.datetime.fromtimestamp(pos.tradeData.openTimestamp / 1000, tz=dt.timezone.utc)
            held_diff     = now_utc - opened_at_utc
            hrs, mins     = divmod(int(held_diff.total_seconds() // 60), 60)
            held_for      = f"{hrs}h {mins}m" if hrs else f"{mins}m"

            volume_lots   = pos.tradeData.volume / 100.0
            contract_size = symbolIdToDetails.get(symbol_id, {}).get("contractSize", 100000) or 100000

            entry_price   = pos.price
            bid, ask      = symbolIdToPrice.get(symbol_id, (None, None))
            market_price  = ask if side_raw == "BUY" else bid
            market_price_s= f"{market_price}" if market_price is not None else "[pending]"

            pnl_cached = positionPnLById_map.get(posId)
            if isinstance(pnl_cached, (int, float)):
                pnl_val = pnl_cached
            elif market_price is not None:
                delta   = (market_price - entry_price) if side_raw == "BUY" else (entry_price - market_price)
                pnl_val = delta * volume_lots * contract_size
            else:
                pnl_val = None

            pnl_s = colorize_number(pnl_val)
            if pnl_val is not None:
                total_pnl += pnl_val

            table.add_row(
                pos_id_label,
                symbol_name,
                side,
                held_for,
                format_lots(pos.tradeData.volume, with_suffix=False),
                f"{entry_price}",
                market_price_s,
                pnl_s,
                style=row_style,
            )

        except Exception as e:
            table.add_row(str(posId), "[Error]", "", "", "", "", "", str(e))
            error_messages.append(str(e))
            if len(error_messages) > 3:
                error_messages.pop(0)

    table.add_section()
    table.add_row("", "", "", "", "", "", "[bold]TOTAL[/bold]", colorize_number(total_pnl))
    return table, "\n".join(error_messages[-3:]), selected_index, view_offset

def buildLivePnLView(
    console_height: int,
    positions_sorted: List[Tuple[int, object]],
    selected_index: int,
    view_offset: int,
    symbolIdToName: Dict[int, str],
    symbolIdToDetails: Dict[int, dict],
    symbolIdToPrice: Dict[int, Tuple[Optional[float], Optional[float]]],
    positionPnLById: Dict[int, float],
    error_messages: List[str],
):
    table, msg, selected_index, view_offset = buildLivePnLTable(
        console_height,
        positions_sorted,
        selected_index,
        view_offset,
        symbolIdToName,
        symbolIdToDetails,
        symbolIdToPrice,
        positionPnLById,
        error_messages,
    )
    pieces = [table]
    if msg:
        pieces.append(f"[red]⚠ {msg}[/red]")
    pieces.append("[dim]🔴  q → quit ⏎[/dim]")
    pieces.append("[dim]↕️  j / k → Navigate[/dim]")
    pieces.append("[dim]❌  x → Exit selected position[/dim]")
    return Group(*pieces), selected_index, view_offset

def displayPosition(
    pos,
    symbolIdToName: Dict[int, str],
    symbolIdToPrice: Dict[int, Tuple[Optional[float], Optional[float]]],
    positionPnLById_map: Dict[int, float],
) -> None:
    """Pretty-print a single position (pure UI)."""
    try:
        symbolId = pos.tradeData.symbolId
        volumeLots = pos.tradeData.volume / 100.0
        side = trade_side_name(pos.tradeData.tradeSide)

        symbolName = symbolIdToName.get(symbolId, f"ID:{symbolId}")
        openPrice = pos.price
        marginUsed = pos.usedMargin / 100.0
        openTime = dt.datetime.utcfromtimestamp(pos.tradeData.openTimestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')

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
        elif pos.positionId in positionPnLById_map:
            pnl = positionPnLById_map[pos.positionId]
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
