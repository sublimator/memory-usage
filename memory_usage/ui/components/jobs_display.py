"""
Jobs display widget for showing job_types data
"""

from typing import Any, Dict, List, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


class JobsDisplay(VerticalScroll):
    """Display job types information in a formatted way"""

    def __init__(self):
        super().__init__()
        self.border_title = "Job Types"
        self._content = Static("Waiting for data...")

    def compose(self) -> ComposeResult:
        """Compose the widget"""
        yield self._content

    def update_jobs(self, job_types: Optional[List[Dict[str, Any]]]):
        """Update the jobs display with new data"""
        if not job_types:
            self._content.update(Text("No job data available", style="dim"))
            return

        # Create a formatted display
        content = self._format_jobs(job_types)
        self._content.update(content)

    def _format_jobs(self, job_types: List[Dict[str, Any]]) -> Table:
        """Format job types data into a nice table"""
        table = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        table.add_column("Job Type", style="yellow", width=None, ratio=3)
        table.add_column("In Prog", justify="center", style="magenta", width=None, ratio=1)
        table.add_column("Per Sec", justify="right", style="green", width=None, ratio=1)
        table.add_column("Avg Time", justify="right", style="blue", width=None, ratio=1)
        table.add_column("Peak Time", justify="right", style="red", width=None, ratio=1)

        # Sort jobs by name for consistent display
        sorted_jobs = sorted(job_types, key=lambda x: x.get("job_type", ""))

        for job in sorted_jobs:
            job_type = job.get("job_type", "Unknown")
            in_progress = str(job.get("in_progress", ""))
            per_second = str(job.get("per_second", ""))
            avg_time = str(job.get("avg_time", ""))
            peak_time = str(job.get("peak_time", ""))

            # Only show non-empty values
            table.add_row(
                job_type,
                in_progress if in_progress else "-",
                per_second if per_second else "-",
                avg_time if avg_time else "-",
                peak_time if peak_time else "-",
            )

        return table
