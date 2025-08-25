
# ui_helpers.py
from typing import Dict, Tuple, List, Optional
from contextlib import contextmanager
from rich.table import Table
from rich.console import Group
from rich import box
import sys, os
import datetime as dt
from rich.text import Text
from rich.markup import escape
from rich.panel import Panel
from rich import box


# ui_helpers.py (top)
BG       = "#0b0f14"   # base
BG_ALT   = "#0d1118"   # zebra
SEL_BG   = "#0b2538"   # selected row
HEAT_POS = "#0f2410"   # subtle green tint
HEAT_NEG = "#241010"   # subtle red tint
GRID     = "grey37"
HEADER   = "#1f2937"

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

def money_symbol(code: str) -> str:
    return {"USD":"$", "EUR":"€", "GBP":"£", "JPY":"¥", "SEK":"kr"}.get((code or "").upper(), "")



def white_cell(s: str) -> Text:
    return Text(escape(str(s)), style="white")

def side_cell(side_raw: str) -> Text:
    name  = "BUY" if side_raw == "BUY" else ("SELL" if side_raw == "SELL" else str(side_raw))
    color = "green" if name == "BUY" else ("red" if name == "SELL" else "white")
    return Text(name, style=color)   # ← no background

def fmt_sl(value: Optional[float], currency_code: str) -> str:
    if value is None: return "[dim]∙[/dim]"
    sym = money_symbol(currency_code)
    s = f"{value:,.0f}{sym}" if sym else f"{value:,.0f}"
    return f"[red]{s}[/red]"

# def fmt_price(px: Optional[float], symbol_id: int, details: Dict[int, dict]) -> str:
#     if px is None:
#         return "[dim]—[/dim]"
#     dp = max(0, details.get(symbol_id, {}).get("pips", 5))
#     return f"{px:.{dp}f}"

# 
# PRICE_WIDTH = 7  # total visible chars for prices (digits + dot)
# 
# def fmt_price(px: Optional[float], symbol_id: int, details: Dict[int, dict], width: int = PRICE_WIDTH) -> str:
#     if px is None:
#         return "[dim]—[/dim]"
# 
#     # precision from symbol (default 5)
#     pips = max(0, details.get(symbol_id, {}).get("pips", 5))
# 
#     # work with absolute, re-attach sign at the end (defensive)
#     sign = "-" if px < 0 else ""
#     s = f"{abs(px):.{pips}f}"
# 
#     # strip trailing zeros and orphan dot
#     if "." in s:
#         s = s.rstrip("0").rstrip(".")
# 
#     # If it fits already (with sign), return
#     if len(sign) + len(s) <= width:
#         return sign + s
# 
#     # Otherwise trim fractional digits to fit the width
#     if "." in s:
#         intpart, frac = s.split(".", 1)
#         keep_frac = max(0, width - len(sign) - len(intpart) - 1)  # minus 1 for dot
#         s = intpart if keep_frac <= 0 else f"{intpart}.{frac[:keep_frac]}"
# 
#         # If the integer part itself is too long (very rare for FX), hard cut
#         if len(sign) + len(s) > width:
#             s = s[: width - len(sign)]
#         return sign + s
# 
#     # No dot and still too long (very large integer) — hard cut (rare)
#     return (sign + s)[:width]



# ui_helpers.py
PRICE_WIDTH = 7  # exact visible chars for prices (includes sign and dot)

def fmt_price(px: Optional[float], symbol_id: int, details: Dict[int, dict], width: int = PRICE_WIDTH) -> str:
    if px is None:
        return "[dim]—[/dim]"

    pips = max(0, details.get(symbol_id, {}).get("pips", 5))
    neg  = px < 0
    s    = f"{abs(px):.{pips}f}"

    # strip trailing zeros and orphan dot
    if "." in s:
        s = s.rstrip("0").rstrip(".")

    # ensure we have a dot if we’ll need to pad
    sign = "-" if neg else ""
    if len(sign) + len(s) < width:
        if "." not in s:
            s += "."
        # pad zeros to exact width
        s += "0" * (width - len(sign) - len(s))
        return sign + s

    # too long -> trim fractional part to fit
    if "." in s:
        intpart, frac = s.split(".", 1)
        keep = max(0, width - len(sign) - len(intpart) - 1)
        s = intpart if keep <= 0 else f"{intpart}.{frac[:keep]}"
        # if still too long (huge int part), hard cut from right
        s = s[: width - len(sign)]
        return sign + s

    # pure integer and too long (rare) -> hard cut
    return (sign + s)[:width]


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
        return "[dim][N/A][/dim]"
    formatted = f"{amount:,.2f}"
    padded = f"{formatted:>10}"
    if amount > 0:
        return f"[green]{padded}[/green]"
    if amount < 0:
        return f"[red]{padded}[/red]"
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


def pnl_heat(pnl: Optional[float]) -> str:
    if pnl is None: return ""
    if pnl > 0:    return "on #061e06"  # a touch darker than before
    if pnl < 0:    return "on #1e0606"
    return ""


def fmt_held_cell(delta: dt.timedelta) -> Text:
    total_min = int(delta.total_seconds() // 60)
    days, rem = divmod(total_min, 1440)
    hrs, mins = divmod(rem, 60)
    if days > 0:
        s = f"{days}d {hrs:02}h"
    elif hrs > 0:
        s = f"{hrs}h {mins:02}m"
    else:
        s = f"{mins}m"
    return white_cell(s)



# def compute_pnl(pos, symbolIdToDetails, symbolIdToPrice, pnl_cache):
#     """Return current PnL (float or None) for a position."""
#     side = trade_side_name(pos.tradeData.tradeSide)
#     symbol_id = pos.tradeData.symbolId
#     entry_price = pos.price
#     bid, ask = symbolIdToPrice.get(symbol_id, (None, None))
#     market_price = ask if side == "BUY" else bid
# 
#     cached = pnl_cache.get(pos.positionId)
#     if isinstance(cached, (int, float)):
#         return cached
#     if market_price is None:
#         return None
# 
#     volume_lots = pos.tradeData.volume / 100.0
#     contract_size = symbolIdToDetails.get(symbol_id, {}).get("contractSize", 100000) or 100000
#     delta = (market_price - entry_price) if side == "BUY" else (entry_price - market_price)
#     return delta * volume_lots * contract_size


def compute_pnl(pos, symbolIdToDetails, symbolIdToPrice, pnl_cache):
    """Return current PnL (float or None) for a position."""
    side = trade_side_name(pos.tradeData.tradeSide)
    symbol_id = pos.tradeData.symbolId
    entry_price = pos.price
    bid, ask = symbolIdToPrice.get(symbol_id, (None, None))
    market_price = ask if side == "BUY" else bid

    cached = pnl_cache.get(pos.positionId)
    if isinstance(cached, (int, float)):
        return cached
    if market_price is None:
        return None

    volume_lots = pos.tradeData.volume / 100.0
    contract_size = symbolIdToDetails.get(symbol_id, {}).get("contractSize", 100000) or 100000
    delta = (market_price - entry_price) if side == "BUY" else (entry_price - market_price)
    return delta * volume_lots * contract_size


# # ui_helpers.py
# def compute_pnl(pos, symbolIdToDetails, symbolIdToPrice, pnl_cache):
#     side = trade_side_name(pos.tradeData.tradeSide)
#     symbol_id = pos.tradeData.symbolId
#     entry_price = pos.price
#     bid, ask = symbolIdToPrice.get(symbol_id, (None, None))
#     market_price = bid if side == "BUY" else ask
# 
#     # ✅ Prefer live market price if we have it
#     if market_price is not None:
#         volume_lots   = pos.tradeData.volume / 100.0
#         contract_size = symbolIdToDetails.get(symbol_id, {}).get("contractSize", 100000) or 100000
#         delta = (market_price - entry_price) if side == "BUY" else (entry_price - market_price)
#         return delta * volume_lots * contract_size
# 
#     # Fallback to cache only when we don't have a tick yet
#     cached = pnl_cache.get(pos.positionId)
#     return cached if isinstance(cached, (int, float)) else None
# 

def choose_row_style(global_idx: int, pnl_val: Optional[float], is_selected: bool) -> str:
    """Pick zebra, heat, and selection styles for a row."""
    row_bg = BG_ALT if (global_idx % 2) else BG
    if pnl_val is not None and not is_selected:
        if pnl_val > 0:   row_bg = HEAT_POS
        elif pnl_val < 0: row_bg = HEAT_NEG
    return f"bold on {SEL_BG}" if is_selected else f"on {row_bg}"


def make_position_row(
    global_idx, selected_index, posId, pos,
    symbolIdToName, symbolIdToDetails, symbolIdToPrice,
    pnl_cache, slByPositionId, account_currency, now_utc
):
    """Build one table row for a position. Returns (cells, row_style, pnl_val)."""
    is_selected = (global_idx == selected_index)
    selector = "▸" if is_selected else ""

    symbol_id = pos.tradeData.symbolId
    symbol_name = symbolIdToName.get(symbol_id, f"ID:{symbol_id}")
    side_raw = trade_side_name(pos.tradeData.tradeSide)

    opened_at_utc = dt.datetime.fromtimestamp(pos.tradeData.openTimestamp / 1000, tz=dt.timezone.utc)
    held_diff = now_utc - opened_at_utc

    pnl_val = compute_pnl(pos, symbolIdToDetails, symbolIdToPrice, pnl_cache)

    entry_cell  = white_cell(fmt_price(pos.price,  symbol_id, symbolIdToDetails))
    bid, ask    = symbolIdToPrice.get(symbol_id, (None, None))
    market_px = bid if side_raw == "BUY" else ask
    market_cell = white_cell(fmt_price(market_px, symbol_id, symbolIdToDetails))

    sl_val = (slByPositionId or {}).get(posId) if slByPositionId else None
    row_style = choose_row_style(global_idx, pnl_val, is_selected)

    cells = [
        selector,
        str(posId),
        white_cell(symbol_name),
        side_cell(side_raw),
        fmt_held_cell(held_diff),
        white_cell(format_lots(pos.tradeData.volume, with_suffix=False)),
        entry_cell,
        market_cell,
        fmt_sl(sl_val, account_currency),
        colorize_number(pnl_val),
    ]
    return cells, row_style, pnl_val



def clamp_viewport(selected_index: int, view_offset: int, n: int, max_rows: int) -> Tuple[int, int]:
    if n == 0:
        return 0, 0
    selected_index = max(0, min(selected_index, n - 1))
    if selected_index < view_offset:
        view_offset = selected_index
    elif selected_index >= view_offset + max_rows:
        view_offset = max(0, selected_index - max_rows + 1)
    return selected_index, view_offset


def make_live_pnl_table() -> Table:
    t = Table(
        box=box.ROUNDED,
        border_style=GRID,
        expand=True,
        padding=(0, 0),
        pad_edge=False,
        header_style=f"bold white on {HEADER}",
        show_edge=True,
        highlight=True,
        style=f"on {BG}",
    )
    t.add_column("", justify="center", no_wrap=True, min_width=2, max_width=2)
    t.add_column("Position ID", justify="left", no_wrap=True, min_width=10, overflow="crop")
    t.add_column("Symbol", justify="left", no_wrap=True, min_width=8, style="white")
    t.add_column("Side", justify="center", no_wrap=True)
    t.add_column("⏱  Held", justify="center", no_wrap=True)
    t.add_column("Lot", justify="center", no_wrap=True)
    t.add_column("📈 Entry", justify="center", no_wrap=True)
    t.add_column("📊 Market", justify="center", no_wrap=True)
    t.add_column("SL", justify="center", no_wrap=True, style="red")
    t.add_column("PnL", justify="center", no_wrap=True, overflow="fold")
    return t

def add_total_row(table: Table, total_pnl: float) -> None:
    table.add_section()
    table.add_row(
        "", "", "", "", "", "", "", "",
        "[bold]TOTAL[/bold]",
        colorize_number(total_pnl),
        style=pnl_heat(total_pnl),
    )

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
    slByPositionId: Dict[int, Optional[float]] = None,
    account_currency: str = "USD",
):
    table = make_live_pnl_table()
    # scroll window
    RESERVED_LINES = 9
    max_rows = max(1, console_height - RESERVED_LINES)
    n = len(positions_sorted)

    selected_index, view_offset = clamp_viewport(selected_index, view_offset, n, max_rows)

    visible = positions_sorted[view_offset:view_offset+max_rows]
    total_pnl = 0.0
    now_utc = dt.datetime.now(dt.timezone.utc)

    for global_idx, (posId, pos) in enumerate(visible, start=view_offset):
        try:
            cells, row_style, pnl_val = make_position_row(
                global_idx, selected_index, posId, pos,
                symbolIdToName, symbolIdToDetails, symbolIdToPrice,
                positionPnLById_map, slByPositionId, account_currency, now_utc
            )
            table.add_row(*cells, style=row_style)
            if pnl_val is not None:
                total_pnl += pnl_val
        except Exception as e:
            table.add_row("", str(posId), "[Error]", "", "", "", "", "", "", str(e))
            error_messages.append(str(e))
            if len(error_messages) > 3: error_messages.pop(0)

    add_total_row(table, total_pnl)

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
    slByPositionId: Dict[int, Optional[float]] = None,     # NEW
    account_currency: str = "USD",                          # NEW
    footer_prompt: str = "", 
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
        slByPositionId=slByPositionId,
        account_currency=account_currency,
    )

    def bg(s: str) -> str:
        return f"[on {BG}]{s}[/]"
    # Always include these two lines to keep height stable
    symbol = money_symbol(account_currency) or account_currency
    loss_label = f"Loss limit ({symbol})"

    header_line = bg("[bold cyan]Live Unrealized PnL[/bold cyan]")
    msg_line    = bg(f"[red]INFO: {msg}[/red]" if msg else " ")
    prompt_line = bg(f"[bold cyan]{footer_prompt}[/bold cyan]" if footer_prompt else " ")

    pieces = [
        header_line,
        table,
        msg_line,            # constant 1 line
        prompt_line,         # constant 1 line
        "[dim]🔴  q → quit [/dim]",
        "[dim]↕️  j / k → Navigate[/dim]",
        "[dim]❌  x → Exit selected position[/dim]",
        f"[dim]🛟 y → Set {loss_label} for selected[/dim]",
    ]
    return Panel(
        Group(*pieces),
        style=f"on {BG}",       # fills the whole panel background
        box=box.SQUARE,         # <- must be a Box, not None
        border_style=BG,        # make border same color as BG to make it invisible
        padding=0,
        expand=True,            # let it fill the screen width
        height=console_height,
    ), selected_index, view_offset

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
