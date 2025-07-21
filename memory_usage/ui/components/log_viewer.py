"""
Log viewer components for the dashboard
"""

from textual.widgets import RichLog
from textual.containers import VerticalScroll
from rich.text import Text
import queue
import asyncio
from typing import Optional, Tuple


class QueuedLogViewer(RichLog):
    """Log viewer that processes messages from a queue"""
    
    def __init__(self, title: str, title_style: str = "bold cyan", *args, **kwargs):
        # Enable text selection
        kwargs['auto_scroll'] = kwargs.get('auto_scroll', True)
        super().__init__(*args, **kwargs)
        self.title = title
        self.title_style = title_style
        self.message_queue = queue.Queue()
        self._initialized = False
        # Enable mouse support for selection
        self.can_focus = True
    
    def on_mount(self):
        """Initialize the log when mounted"""
        if not self._initialized:
            self.write(Text(self.title, style=self.title_style))
            self.write("-" * 60)
            self._initialized = True
    
    def queue_message(self, message: str, style: Optional[str] = None):
        """Add a message to the queue"""
        self.message_queue.put((message, style))
    
    def process_queue(self, max_messages: int = 10):
        """Process messages from the queue"""
        count = 0
        while not self.message_queue.empty() and count < max_messages:
            try:
                message, style = self.message_queue.get_nowait()
                if style:
                    self.write(f"[{style}]{message}[/{style}]")
                else:
                    self.write(message)
                count += 1
            except queue.Empty:
                break


class MonitorLogViewer(QueuedLogViewer):
    """Log viewer for monitor output"""
    
    def __init__(self):
        super().__init__(
            title="Memory Monitor Log",
            title_style="bold cyan",
            highlight=True,
            markup=True,
            wrap=True,
            auto_scroll=True,
            id="monitor-richlog"
        )


class ProcessOutputViewer(QueuedLogViewer):
    """Log viewer for process output"""
    
    def __init__(self):
        super().__init__(
            title="Rippled Output",
            title_style="bold green",
            highlight=True,
            markup=True,
            wrap=True,
            auto_scroll=True,
            id="rippled-richlog"
        )
    
    def queue_stdout(self, line: str):
        """Queue a stdout line"""
        self.queue_message(f"[cyan][stdout][/cyan] {line}")
    
    def queue_stderr(self, line: str):
        """Queue a stderr line"""
        self.queue_message(f"[yellow][stderr][/yellow] {line}")