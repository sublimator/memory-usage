"""
Catalogue status display component
"""

from typing import Any, Dict, Optional

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class CatalogueStatusDisplay(Static):
    """Display catalogue loading status"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.catalogue_data: Optional[Dict[str, Any]] = None

    def compose(self) -> ComposeResult:
        """Compose the widget"""
        with VerticalScroll():
            yield Static("Catalogue Status", classes="display-title")
            yield Static("No catalogue data available", id="catalogue-content")

    def update_catalogue_status(self, data: Optional[Dict[str, Any]]):
        """Update catalogue status display"""
        self.catalogue_data = data
        content_widget = self.query_one("#catalogue-content", Static)

        if not data:
            content_widget.update("No catalogue data available")
            return

        # The data IS the result already (from websocket manager)
        result = data

        # Format the display
        lines = []

        # Job status
        job_status = result.get("job_status", "unknown")

        # Handle different response formats
        if job_status == "no job running":
            lines.append("[dim]No active catalogue operation[/dim]")
        elif job_status == "job_in_progress":
            job_type = result.get("job_type", "unknown")
            lines.append(f"[bold yellow]Job:[/bold yellow] {job_type}")

            # Progress info
            percent = result.get("percent_complete", 0)
            current = result.get("current_ledger", 0)
            min_ledger = result.get("min_ledger", 0)
            max_ledger = result.get("max_ledger", 0)

            # Calculate ledgers loaded
            total_ledgers = max_ledger - min_ledger + 1 if max_ledger > min_ledger else 0
            ledgers_loaded = current - min_ledger + 1 if current >= min_ledger else 0

            lines.append(
                f"[bold green]Ledgers Loaded:[/bold green] {ledgers_loaded:,} / {total_ledgers:,}"
            )
            lines.append(f"[bold]Progress:[/bold] {percent}% (at ledger {current:,})")
            lines.append(f"[bold]Range:[/bold] {min_ledger:,} - {max_ledger:,}")

            # Time info
            elapsed = result.get("elapsed_seconds", 0)
            elapsed_min = elapsed // 60
            elapsed_sec = elapsed % 60
            lines.append(f"[bold]Elapsed:[/bold] {elapsed_min}m {elapsed_sec}s")

            remaining = result.get("estimated_time_remaining", "unknown")
            lines.append(f"[bold]Remaining:[/bold] {remaining}")

            # File info
            file_path = result.get("file", "unknown")
            file_name = file_path.split("/")[-1] if "/" in file_path else file_path
            lines.append(f"[bold]File:[/bold] {file_name}")

            file_size = result.get("file_size_human", "unknown")
            lines.append(f"[bold]Size:[/bold] {file_size}")
        else:
            # Unknown job status
            lines.append(f"[dim]Job status: {job_status}[/dim]")

        content_widget.update("\n".join(lines))
