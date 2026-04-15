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
    return f"{n:,}"


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
        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Metric", style="yellow", ratio=3)
        table.add_column("Value", justify="right", style="green", ratio=2)
        table.add_column("%", justify="right", style="cyan", ratio=1)

        if pools:
            total = pools.get("_total") or {}
            current_b = _to_int(total.get("current_bytes"))
            peak_b = _to_int(total.get("peak_bytes"))
            wasted_b = _to_int(total.get("cached_wasted_bytes"))
            cum_allocs = _to_int(total.get("cumulative_allocs"))

            def pct_of_peak(v: int) -> str:
                return f"{(v / peak_b * 100):.1f}%" if peak_b > 0 else "-"

            table.add_row("[bold]TaggedPointer pools[/bold]", "", "", style="bold magenta")
            table.add_row("  Current", _format_bytes(current_b), pct_of_peak(current_b))
            table.add_row("  Peak (high-water)", _format_bytes(peak_b), "100%" if peak_b else "-")
            table.add_row("  Cached / unreturned", _format_bytes(wasted_b), pct_of_peak(wasted_b))
            table.add_row("  Lifetime allocs", _format_count(cum_allocs), "")

            # Top non-empty slots by current usage, to spot uneven churn.
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
                table.add_row("", "", "")
                table.add_row("[bold]Top slots[/bold]", "cur / peak", "", style="bold magenta")
                for slot, cur_bytes, current, peak in slot_rows[:6]:
                    table.add_row(
                        f"  slot {slot}",
                        _format_bytes(cur_bytes),
                        f"{_format_count(current)} / {_format_count(peak)}",
                    )

        if locks:
            if pools:
                table.add_row("", "", "")
            held_ms = _to_int(locks.get("held_ms"))
            acquires = _to_int(locks.get("acquires"))
            mean_ns = _to_int(locks.get("mean_ns"))
            table.add_row("[bold]TreeNodeCache locks[/bold]", "", "", style="bold magenta")
            table.add_row("  Total held", f"{held_ms:,} ms", "")
            table.add_row("  Acquires", _format_count(acquires), "")
            table.add_row("  Mean hold", f"{mean_ns:,} ns", "")

        return table
