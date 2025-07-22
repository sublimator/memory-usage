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

    def compose(self) -> ComposeResult:
        """Compose the widget"""
        yield self._content

    def update_counts(self, counts: Optional[Dict[str, Any]]):
        """Update the counts display with new data"""
        if not counts:
            self._content.update(Text("No data available", style="dim"))
            return

        if "result" in counts:
            counts = counts["result"]

        # Create a formatted display
        content = self._format_counts(counts)
        self._content.update(content)

    def _format_counts(self, counts: Dict[str, Any]) -> Table:
        """Format counts data into a nice table"""
        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Metric", style="yellow", width=None, ratio=2)
        table.add_column("Value", justify="right", style="green", width=None, ratio=1)

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

        # Collect all ripple:: entries dynamically
        ripple_objects = []
        for key, value in counts.items():
            if key.startswith("ripple::"):
                # Extract the class name after ripple::
                display_name = key.replace("ripple::", "")
                ripple_objects.append((key, display_name, ""))

        # Sort ripple objects by name for consistent display
        ripple_objects.sort(key=lambda x: x[1])
        sections["Objects"] = ripple_objects

        for section, metrics in sections.items():
            # Add section header
            table.add_row(f"[bold]{section}[/bold]", "", style="bold magenta")

            # Add metrics
            for key, display_name, suffix in metrics:
                if key in counts:
                    value = counts[key]
                    # Format the value
                    if isinstance(value, (int, float)):
                        # Special handling for hit rates (% suffix)
                        if suffix == "%":
                            formatted_value = f"{value:.3f}%"
                        else:
                            formatted_value = f"{value:,}{suffix}"
                    else:
                        formatted_value = f"{value}{suffix}"
                    table.add_row(f"  {display_name}", formatted_value)

            # Add spacing between sections
            table.add_row("", "")

        return table
