"""
Counts display widget for showing get_counts diagnostics
"""

from collections import deque
from typing import Any, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

# Rolling window for the up-count-majority trend detector. At ~2-4s between
# server_info polls, 30 samples is ~60-120s of recent direction — long
# enough that one bounce doesn't flip the arrow, short enough that the
# reading reflects the last minute or two rather than all-time drift.
_TREND_WINDOW = 30
# A step-delta counts as a 'move' only when its magnitude exceeds this
# fraction of the observed range, otherwise the sample is considered flat.
# Keeps noisy counters (cache sizes that wiggle ±1) from tipping either way.
_TREND_NOISE = 0.0  # 0 = any non-zero delta; raise if needed
# Fraction of the window that must agree for the arrow to show.
_TREND_MAJORITY = 0.6


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
                    # Rolling window of step-direction signs (+1/0/-1) — used
                    # by _trend_marker to compute 'went up more often than
                    # down' over the last ~_TREND_WINDOW samples.
                    "deltas": deque(maxlen=_TREND_WINDOW),
                    # Latched trend — once we've shown '↑' or '↓' it stays
                    # until the *opposite* direction crosses the majority
                    # threshold. Prevents the arrow flickering on/off as
                    # the window oscillates around 60%.
                    "trend": None,  # 'up' | 'down' | None (never decided)
                }
                continue
            delta = value - entry["last"]
            if delta > 0:
                entry["deltas"].append(1)
            elif delta < 0:
                entry["deltas"].append(-1)
                entry["ever_decreased"] = True
            else:
                entry["deltas"].append(0)
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
        """Recent-direction arrow with latching.

        Tallies up vs down step-deltas in the rolling _TREND_WINDOW. A new
        direction *only replaces* the latched one if it crosses
        _TREND_MAJORITY — so '↑' stays until '↓' actually takes majority,
        and vice versa. Avoids the arrow blinking in/out when a metric
        hovers around the threshold.
        """
        entry = self._history.get(key)
        if entry is None:
            return ""
        deltas = entry["deltas"]
        if len(deltas) < 5:
            return ""  # need a few samples before we commit to a direction

        ups = sum(1 for d in deltas if d > 0)
        downs = sum(1 for d in deltas if d < 0)
        moves = ups + downs
        if moves > 0:
            if ups / moves >= _TREND_MAJORITY:
                entry["trend"] = "up"
            elif downs / moves >= _TREND_MAJORITY:
                entry["trend"] = "down"
            # else: keep whatever was latched last — don't blank it out

        trend = entry.get("trend")
        if trend == "up":
            return "[bold red]↑[/bold red]"
        if trend == "down":
            return "[bold green]↓[/bold green]"
        return ""

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
        """Format counts data into a min/cur/max table with inline markers."""
        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=None,
            expand=True,
            # Subtle zebra stripes so the eye can track across a wide row
            # without losing its place in a dense panel.
            row_styles=["", "on grey15"],
        )
        table.add_column("Metric", style="yellow", ratio=3)
        table.add_column("Min", justify="right", style="dim", ratio=1)
        table.add_column("Cur", justify="right", style="green", ratio=1)
        table.add_column("Max", justify="right", style="dim", ratio=1)

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
            table.add_row(f"[bold]{section}[/bold]", "", "", "", style="bold magenta")

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

                # Two inline markers: recent-window trend arrow, then '++'
                # for strict all-time monotonic. Both are space-prefixed so
                # they only appear when relevant — keeps rows clean when the
                # metric is boring.
                trend = self._trend_marker(key)
                monotonic = (
                    " [bold red]++[/bold red]" if self._is_monotonic_growing(key, value) else ""
                )
                name_markers = f"{' ' + trend if trend else ''}{monotonic}"
                table.add_row(
                    f"  {display_name}{name_markers}",
                    min_str,
                    cur_str,
                    max_str,
                )

            # Add spacing between sections
            table.add_row("", "", "", "")

        return table
