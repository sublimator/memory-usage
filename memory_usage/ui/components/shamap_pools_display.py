"""
SHAMap pool / TreeNodeCache lock diagnostics.

Renders the ``tagged_pointer_pools`` and ``treenode_cache_locks`` blocks
emitted by patched rippled builds. Quietly shows 'Waiting for data...'
on builds that don't emit them — neither key is part of stock rippled.
"""

from typing import Any, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


def _to_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _format_bytes(n_bytes: int) -> str:
    if n_bytes >= 1024**3:
        return f"{n_bytes / (1024**3):.2f} GB"
    if n_bytes >= 1024**2:
        return f"{n_bytes / (1024**2):.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes} B"


def _format_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n}"


class SHAMapPoolsDisplay(VerticalScroll):
    """TaggedPointer pool totals + TreeNodeCache lock contention, if emitted."""

    def __init__(self):
        super().__init__()
        self.border_title = "SHAMap Pools & Locks"
        self._content = Static("Waiting for data...")

    def compose(self) -> ComposeResult:
        yield self._content

    def update_counts(self, counts: Optional[Dict[str, Any]]):
        if not counts:
            self._content.update(Text("Waiting for data...", style="dim"))
            return

        if "result" in counts:
            counts = counts["result"]

        pools = counts.get("tagged_pointer_pools")
        locks = counts.get("treenode_cache_locks")
        if not pools and not locks:
            self._content.update(
                Text(
                    "Not emitted by this rippled build — requires patched get_counts.",
                    style="dim",
                )
            )
            return

        self._content.update(self._format(pools, locks))

    def _format(
        self,
        pools: Optional[Dict[str, Any]],
        locks: Optional[Dict[str, Any]],
    ) -> Table:
        # expand=False lets the table hug its content — the previous layout
        # stretched to panel width and left a wide gap between labels and
        # values. pad_edge=False drops the 1-char gutters rich adds by default.
        table = Table(show_header=False, box=None, expand=False, pad_edge=False)
        table.add_column(style="yellow", no_wrap=True)
        table.add_column(justify="right", style="green", no_wrap=True)

        if pools:
            total = pools.get("_total") or {}
            current_b = _to_int(total.get("current_bytes"))
            peak_b = _to_int(total.get("peak_bytes"))
            wasted_b = _to_int(total.get("cached_wasted_bytes"))
            cum_allocs = _to_int(total.get("cumulative_allocs"))
            wasted_pct = (wasted_b / peak_b * 100) if peak_b > 0 else 0

            table.add_row("[bold magenta]TaggedPointer pools[/bold magenta]", "")
            table.add_row(
                "  Current / Peak",
                f"{_format_bytes(current_b)} / {_format_bytes(peak_b)}",
            )
            table.add_row(
                "  Cached (peak-cur)",
                f"{_format_bytes(wasted_b)} ({wasted_pct:.1f}%)",
            )
            table.add_row("  Lifetime allocs", _format_count(cum_allocs))

            # Top slots by current byte usage. Compact: bytes + chunk counts
            # on the same line so we don't need a third column.
            slot_rows = []
            for key, entry in pools.items():
                if not key.endswith("_slot") or not isinstance(entry, dict):
                    continue
                current = _to_int(entry.get("current"))
                peak = _to_int(entry.get("peak"))
                chunk_bytes = _to_int(entry.get("chunk_bytes"))
                if current == 0 and peak == 0:
                    continue
                cur_bytes = current * chunk_bytes
                slot_rows.append((key.removesuffix("_slot"), cur_bytes, current, peak))
            slot_rows.sort(key=lambda r: r[1], reverse=True)

            if slot_rows:
                table.add_row("", "")
                table.add_row("[bold magenta]All slots[/bold magenta]", "bytes  chunks")
                for slot, cur_bytes, current, peak in slot_rows:
                    chunks = (
                        f"{_format_count(current)}"
                        if current == peak
                        else f"{_format_count(current)}/{_format_count(peak)}"
                    )
                    table.add_row(
                        f"  slot {slot}",
                        f"{_format_bytes(cur_bytes)}  [dim]{chunks}[/dim]",
                    )

        if locks:
            if pools:
                table.add_row("", "")
            held_ms = _to_int(locks.get("held_ms"))
            acquires = _to_int(locks.get("acquires"))
            mean_ns = _to_int(locks.get("mean_ns"))
            table.add_row("[bold magenta]TreeNodeCache locks[/bold magenta]", "")
            table.add_row("  Held / Acquires", f"{held_ms:,} ms / {_format_count(acquires)}")
            table.add_row("  Mean hold", f"{mean_ns:,} ns")

        return table
