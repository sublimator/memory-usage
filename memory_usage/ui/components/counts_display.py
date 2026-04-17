"""
Counts display widget for showing get_counts diagnostics
"""

from typing import Any, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class CountsDisplay(VerticalScroll):
    """Display internal diagnostic counts in a formatted way"""

    def __init__(self):
        super().__init__()
        self.border_title = "Internal Diagnostics"
        self._content = Static("Waiting for data...")
        # Per-key history so we can mark monotonically-growing counters with
        # '++'. Stores {first_seen, last_seen, ever_decreased} per metric key.
        self._history: Dict[str, Dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        """Compose the widget"""
        yield self._content

    def reset_history(self) -> None:
        """Clear the monotonic-growth tracking. Call between binaries."""
        self._history.clear()

    def _record_history(self, counts: Dict[str, Any]) -> None:
        for key, value in counts.items():
            if not isinstance(value, (int, float)):
                continue
            entry = self._history.get(key)
            if entry is None:
                self._history[key] = {
                    "first": value,
                    "min": value,
                    "max": value,
                    "last": value,
                    "ever_decreased": False,
                }
                continue
            if value < entry["last"]:
                entry["ever_decreased"] = True
            entry["min"] = min(entry["min"], value)
            entry["max"] = max(entry["max"], value)
            entry["last"] = value

    def _min_max(self, key: str):
        entry = self._history.get(key)
        if entry is None:
            return None, None
        return entry["min"], entry["max"]

    def _is_monotonic_growing(self, key: str, value: Any) -> bool:
        entry = self._history.get(key)
        if entry is None or not isinstance(value, (int, float)):
            return False
        if entry["ever_decreased"]:
            return False
        return bool(value > entry["first"])

    def _trend_marker(self, key: str) -> str:
        """Return an all-time trend arrow (↑/↓/→) or '' if no history yet.

        Based on last vs first observed value — catches creepers that go up
        and down but net up over the observation window. Paired with the
        '++' marker (strict monotonic) for two complementary views.
        """
        entry = self._history.get(key)
        if entry is None:
            return ""
        if entry["last"] > entry["first"]:
            return "[red]↑[/red]"
        if entry["last"] < entry["first"]:
            return "[green]↓[/green]"
        return "[dim]→[/dim]"

    def update_counts(self, counts: Optional[Dict[str, Any]]):
        """Update the counts display with new data"""
        if not counts:
            self._content.update(Text("No data available", style="dim"))
            return

        if "result" in counts:
            counts = counts["result"]

        self._record_history(counts)

        # Create a formatted display
        content = self._format_counts(counts)
        self._content.update(content)

    @staticmethod
    def _format_value(value: Any, suffix: str) -> str:
        if isinstance(value, (int, float)):
            if suffix == "%":
                return f"{value:.3f}%"
            return f"{value:,}{suffix}"
        return f"{value}{suffix}"

    def _format_counts(self, counts: Dict[str, Any]) -> Table:
        """Format counts data into a nice table with min/cur/max/trend columns."""
        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Metric", style="yellow", ratio=3)
        table.add_column("Min", justify="right", style="dim", ratio=1)
        table.add_column("Cur", justify="right", style="green", ratio=1)
        table.add_column("Max", justify="right", style="dim", ratio=1)
        table.add_column("Trend", justify="center", width=3, no_wrap=True)

        # Group related metrics
        sections = {
            "Cache Performance": [
                ("AL_hit_rate", "AL Hit Rate", "%"),
                ("AL_size", "AL Size", ""),
                ("SLE_hit_rate", "SLE Hit Rate", "%"),
                ("ledger_hit_rate", "Ledger Hit Rate", "%"),
                ("treenode_cache_size", "TreeNode Cache", ""),
            ],
            "Database": [
                ("dbKBTotal", "Total KB", " KB"),
                ("dbKBLedger", "Ledger KB", " KB"),
                ("dbKBTransaction", "Transaction KB", " KB"),
            ],
            "Node I/O": [
                ("node_reads_total", "Reads Total", ""),
                ("node_reads_hit", "Reads Hit", ""),
                ("node_writes", "Writes", ""),
                ("node_written_bytes", "Written Bytes", " B"),
            ],
            "System": [
                ("read_threads_running", "Read Threads", ""),
                ("write_load", "Write Load", ""),
                ("uptime", "Uptime", ""),
            ],
            "Objects": [],  # Will be populated dynamically
        }

        # Collect all namespace-qualified class counts (ripple::, xrpl::, etc.)
        object_entries = []
        for key, value in counts.items():
            if "::" in key:
                display_name = key.rsplit("::", 1)[-1]
                object_entries.append((key, display_name, ""))

        object_entries.sort(key=lambda x: x[1])
        sections["Objects"] = object_entries

        for section, metrics in sections.items():
            # Section header (spans the metric column; others stay blank)
            table.add_row(f"[bold]{section}[/bold]", "", "", "", "", style="bold magenta")

            for key, display_name, suffix in metrics:
                if key not in counts:
                    continue
                value = counts[key]
                cur_str = self._format_value(value, suffix)
                mn, mx = self._min_max(key)
                # Numeric values: show the observed range. Non-numeric or
                # never-recorded keys: just the current value.
                if isinstance(value, (int, float)) and mn is not None:
                    min_str = self._format_value(mn, suffix) if mn != value else ""
                    max_str = self._format_value(mx, suffix) if mx != value else ""
                else:
                    min_str = ""
                    max_str = ""
                # Eyeball marker: strict monotonic grower (never decreased).
                marker = (
                    " [bold red]++[/bold red]" if self._is_monotonic_growing(key, value) else ""
                )
                table.add_row(
                    f"  {display_name}{marker}",
                    min_str,
                    cur_str,
                    max_str,
                    self._trend_marker(key),
                )

            # Add spacing between sections
            table.add_row("", "", "", "", "")

        return table
