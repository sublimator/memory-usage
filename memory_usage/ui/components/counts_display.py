"""
Counts display widget for showing get_counts diagnostics
"""

from collections import deque
from typing import Any, Deque, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

# Sparkline rendering: 8-level block chars + dashed for flat series. SPARK_WIDTH
# bounds the per-metric ring buffer; at ~2-4s between updates that's ~20-40s of
# history — enough shape to spot a growth trend at a glance.
_SPARK_CHARS = "▁▂▃▄▅▆▇█"
SPARK_WIDTH = 10


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
                samples: Deque[float] = deque(maxlen=SPARK_WIDTH)
                samples.append(float(value))
                self._history[key] = {
                    "first": value,
                    "min": value,
                    "max": value,
                    "last": value,
                    "ever_decreased": False,
                    "samples": samples,
                }
                continue
            if value < entry["last"]:
                entry["ever_decreased"] = True
            entry["min"] = min(entry["min"], value)
            entry["max"] = max(entry["max"], value)
            entry["last"] = value
            entry["samples"].append(float(value))

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
        return value > entry["first"]

    @staticmethod
    def _sparkline(samples) -> str:
        """Render a list of numeric samples as an 8-level unicode sparkline.

        Returns '' for fewer than 2 samples. A flat series renders as dashes
        so the eye doesn't see fake motion where there is none.
        """
        if len(samples) < 2:
            return ""
        lo = min(samples)
        hi = max(samples)
        span = hi - lo
        if span == 0:
            return "─" * len(samples)
        scale = len(_SPARK_CHARS) - 1
        return "".join(_SPARK_CHARS[int((s - lo) / span * scale)] for s in samples)

    def _trend_delta(self, key: str):
        """Return (delta_value, samples_list) for the observed window."""
        entry = self._history.get(key)
        if entry is None:
            return None, []
        samples = list(entry["samples"])
        if len(samples) < 2:
            return None, samples
        return samples[-1] - samples[0], samples

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
        """Format counts data into a nice table with min/cur/max/trend/Δ columns."""
        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Metric", style="yellow", ratio=3)
        table.add_column("Min", justify="right", style="dim", ratio=1)
        table.add_column("Cur", justify="right", style="green", ratio=1)
        table.add_column("Max", justify="right", style="dim", ratio=1)
        # Fixed-width so 10 block chars + border always fit cleanly.
        table.add_column("Trend", justify="left", width=SPARK_WIDTH, no_wrap=True)
        table.add_column("Δ", justify="right", ratio=1)

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
            table.add_row(f"[bold]{section}[/bold]", "", "", "", "", "", style="bold magenta")

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
                # Eyeball marker: value has only ever grown since we started
                # observing. Redundant-ish with the min/max columns but quick
                # to spot at a glance.
                marker = (
                    " [bold red]++[/bold red]" if self._is_monotonic_growing(key, value) else ""
                )

                # Sparkline over the last SPARK_WIDTH samples + Δ from the
                # oldest sample in that window. Coloured so up (red) = concern,
                # down (green) = reclaiming.
                delta, samples = self._trend_delta(key)
                spark = self._sparkline(samples)
                if delta is None or delta == 0:
                    delta_str = ""
                elif delta > 0:
                    delta_str = f"[red]+{self._format_value(delta, suffix)}[/red]"
                else:
                    delta_str = f"[green]{self._format_value(delta, suffix)}[/green]"
                table.add_row(
                    f"  {display_name}{marker}",
                    min_str,
                    cur_str,
                    max_str,
                    spark,
                    delta_str,
                )

            # Add spacing between sections
            table.add_row("", "", "", "", "", "")

        return table
