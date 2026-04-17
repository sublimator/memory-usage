"""
Heap sample widget — renders the latest macOS ``heap(1)`` top-classes view.

Driven by ``ApplicationState.heap_sample``, which MonitoringService
populates when ``--heap-every-ledger`` is enabled. The widget also tracks
per-class first/last/delta across all samples it receives during the
session so the monotonic grower can surface inline without leaving the
dashboard.
"""

from typing import Any, Dict, List, Optional, Tuple

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class HeapDisplay(VerticalScroll):
    """Live heap sample + inline monotonic-grower tracking."""

    def __init__(self, top_n: int = 50) -> None:
        super().__init__()
        self.border_title = "Heap (macOS)"
        self.top_n = top_n
        self._content = Static(
            Text(
                "No heap sample yet. Run with --heap-every-ledger N to enable.",
                style="dim",
            )
        )
        # (class, binary) -> {first_bytes, last_bytes, ever_decreased, samples}
        self._history: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield self._content

    def reset_history(self) -> None:
        self._history.clear()

    def update_sample(self, sample: Optional[Dict[str, Any]]) -> None:
        """Accept a heap_sample dict; re-render the table.

        Extends per-class history so later samples can decide whether
        a row is monotonic. No-op on malformed / error samples.
        """
        if not isinstance(sample, dict):
            self._content.update(Text("No heap sample yet.", style="dim"))
            return
        if not sample.get("ok"):
            err = sample.get("error") or "unknown error"
            self._content.update(Text(f"heap failed: {err}", style="red"))
            return

        rows = sample.get("top") or []
        self._record_history(rows)
        self._content.update(self._build_table(sample, rows))

    def _record_history(self, rows: List[Dict[str, Any]]) -> None:
        """Track first/last/ever_decreased per (class, binary)."""
        for r in rows:
            cls = r.get("class") or ""
            binary = r.get("binary") or ""
            byt = int(r.get("bytes") or 0)
            key = (cls, binary)
            entry = self._history.get(key)
            if entry is None:
                self._history[key] = {
                    "first_bytes": byt,
                    "last_bytes": byt,
                    "ever_decreased": False,
                    "samples": 1,
                }
                continue
            if byt < entry["last_bytes"]:
                entry["ever_decreased"] = True
            entry["last_bytes"] = byt
            entry["samples"] += 1

    def _build_table(self, sample: Dict[str, Any], rows: List[Dict[str, Any]]) -> Table:
        mode = sample.get("mode") or "class"
        pid = sample.get("pid")
        total_mb = (sample.get("total_bytes") or 0) / (1024 * 1024)
        row_count = sample.get("row_count") or 0
        duration_ms = sample.get("duration_ms") or 0
        lines_scanned = sample.get("lines_scanned") or 0
        lines_matched = sample.get("lines_matched") or 0
        unmatched = sample.get("unmatched_numeric") or 0
        header = (
            f"pid {pid}  mode={mode}  {row_count:,} classes  "
            f"{total_mb:,.1f} MB  ({duration_ms} ms)  "
            f"matched {lines_matched}/{lines_scanned}"
        )
        if unmatched:
            header += f"  [yellow]{unmatched} numeric skipped[/yellow]"
        if mode == "alloc-site":
            header += "  [yellow]MallocStackLogging active[/yellow]"

        # Rich markup in a Table's title is opt-in via caption/title; simpler
        # to put the header as a dim first row.
        table = Table(
            title=header,
            title_style="bold cyan",
            header_style="bold cyan",
            box=None,
            expand=True,
            row_styles=["", "on grey15"],
        )
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("↑↑", justify="center", width=3)
        table.add_column("Class", style="yellow", ratio=5, no_wrap=False)
        table.add_column("Count", justify="right", style="dim", ratio=1)
        table.add_column("MB", justify="right", style="green", ratio=1)
        table.add_column("Δ MB", justify="right", ratio=1)

        for i, r in enumerate(rows[: self.top_n], 1):
            cls = r.get("class") or ""
            binary = r.get("binary") or ""
            byt = int(r.get("bytes") or 0)
            entry = self._history.get((cls, binary), {})
            first = entry.get("first_bytes", byt)
            delta = byt - first
            mono_mark = ""
            if (
                entry.get("samples", 0) >= 2
                and not entry.get("ever_decreased", False)
                and delta > 0
            ):
                mono_mark = "[bold red]↑↑[/bold red]"
            style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
            table.add_row(
                str(i),
                mono_mark,
                cls,
                f"{int(r.get('count') or 0):,}",
                f"{byt / (1024 * 1024):,.2f}",
                f"[{style}]{delta / (1024 * 1024):+,.2f}[/{style}]",
            )
        return table
