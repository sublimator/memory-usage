"""
Memory breakdown widget showing aggregates and per-category RSS.

Emphasizes Anonymous (heap) because that's what moves when a memory
optimization shifts pages out of malloc into file-backed mmap.
"""

from typing import Any, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


def _format_mb(mb: Optional[float]) -> str:
    if mb is None:
        return "-"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


class MemoryBreakdownDisplay(VerticalScroll):
    """Show aggregate RSS split and per-category breakdown."""

    def __init__(self):
        super().__init__()
        self.border_title = "Memory Breakdown"
        self._content = Static("Waiting for data...")

    def compose(self) -> ComposeResult:
        yield self._content

    def update_breakdown(self, breakdown: Optional[Dict[str, Any]]):
        if not breakdown:
            self._content.update(Text("Waiting for data...", style="dim"))
            return

        if not breakdown.get("supported", False):
            self._content.update(
                Text(
                    "Memory breakdown unsupported on this platform.",
                    style="dim",
                )
            )
            return

        self._content.update(self._format(breakdown))

    def _format(self, b: Dict[str, Any]) -> Table:
        total = float(b.get("total_rss_mb") or 0.0)
        anon = b.get("anonymous_mb")
        pss = b.get("pss_mb")
        private_dirty = b.get("private_dirty_mb")
        swap = b.get("swap_mb")
        nodestore = float(b.get("nodestore_mb") or 0.0)
        shared_lib = float(b.get("shared_lib_mb") or 0.0)
        other_file = float(b.get("other_file_mb") or 0.0)
        source = b.get("source") or "?"
        note = b.get("note") or ""

        def pct(v: Optional[float]) -> str:
            if v is None:
                return "-"
            return f"{(v / total * 100):.1f}%" if total > 0 else "-"

        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Metric", style="yellow", ratio=3)
        table.add_column("Value", justify="right", style="green", ratio=2)
        table.add_column("%", justify="right", style="cyan", ratio=1)

        # Aggregates (Anonymous is the one that moves with heap-saving work)
        table.add_row(
            f"[bold]Aggregates[/bold] [dim]({source})[/dim]", "", "", style="bold magenta"
        )
        table.add_row("  Total RSS", _format_mb(total), "")
        if anon is not None:
            anon_f = float(anon)
            table.add_row("  Anonymous (heap/stack)", _format_mb(anon_f), pct(anon_f))
        else:
            table.add_row("  Anonymous (heap/stack)", "[dim]unavailable[/dim]", "-")
        if pss is not None:
            table.add_row("  Pss (proportional)", _format_mb(float(pss)), pct(float(pss)))
        if private_dirty is not None:
            table.add_row(
                "  Private dirty", _format_mb(float(private_dirty)), pct(float(private_dirty))
            )
        if swap is not None and float(swap) > 0:
            table.add_row("  Swap", _format_mb(float(swap)), "")
        if note:
            table.add_row(f"  [dim]{note}[/dim]", "", "")

        # Per-category (populated from smaps on Linux; macOS lumps everything
        # non-anon into other_file)
        if nodestore + shared_lib + other_file > 0:
            table.add_row("", "", "")
            table.add_row("[bold]By category[/bold]", "", "", style="bold magenta")
            table.add_row("  Nodestore (mmap)", _format_mb(nodestore), pct(nodestore))
            table.add_row("  Shared libs", _format_mb(shared_lib), pct(shared_lib))
            table.add_row("  Other file-backed", _format_mb(other_file), pct(other_file))

        top_files = b.get("top_files") or []
        if top_files:
            table.add_row("", "", "")
            table.add_row("[bold]Top mmap'd files[/bold]", "", "", style="bold magenta")
            for path, mb in top_files:
                display_path = path if len(path) <= 40 else "..." + path[-37:]
                table.add_row(f"  {display_path}", _format_mb(float(mb)), pct(float(mb)))

        return table
