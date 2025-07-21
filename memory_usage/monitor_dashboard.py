#!/usr/bin/env python3
"""
Clean Textual dashboard for Xahaud Memory Monitor
"""

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Static, RichLog
from textual.binding import Binding
from rich.text import Text
import asyncio
from datetime import datetime
from pathlib import Path
import subprocess
import threading
import queue
from typing import Optional, Dict, List
import psutil
import select

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import ServerInfo

from .config import Config
from .monitoring import MemoryMonitor, RippledProcess


class MonitorDashboard(App):
    """Clean dashboard for monitoring xahaud memory usage."""
    
    CSS = """
    Screen {
        background: $surface;
    }
    
    #status-bar {
        dock: top;
        height: 3;
        background: $panel;
        border: solid $primary;
        padding: 0 1;
    }
    
    #main-container {
        height: 100%;
        width: 100%;
    }
    
    #monitor-log {
        height: 50%;
        border: solid $primary;
        padding: 1;
    }
    
    #rippled-output {
        height: 50%;
        border: solid $secondary;
        padding: 1;
    }
    
    RichLog {
        background: $surface;
        color: $text;
        scrollbar-size: 1 1;
    }
    """
    
    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("c", "clear", "Clear logs"),
        Binding("space", "pause", "Pause/Resume"),
        Binding("s", "stop_process", "Stop rippled"),
    ]
    
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.title = "Xahaud Memory Monitor Dashboard"
        self.sub_title = "Real-time memory monitoring"
        self.paused = False
        
        # Create monitor instance
        self.monitor = MemoryMonitor(config)
        
        # Process management
        self.monitoring = False
        
        # WebSocket client
        self.client: Optional[AsyncWebsocketClient] = None
        
        # Current state
        self.complete_ledgers = "empty"
        self.current_ledger = "N/A"
        self.process_pid = None
        
        # Output queues
        self.monitor_queue = queue.Queue()
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()
        
        # Background task handle
        self.test_task = None
    
    def compose(self) -> ComposeResult:
        """Create the layout."""
        yield Header()
        
        # Status bar
        with Horizontal(id="status-bar"):
            yield Static("Status: Initializing...", id="status-text")
            yield Static("Memory: N/A", id="memory-text") 
            yield Static("Complete Ledgers: empty", id="complete-ledgers")
            yield Static("Current: N/A", id="current-ledger")
        
        with Vertical(id="main-container"):
            # Top half - Memory monitor logs
            with VerticalScroll(id="monitor-log"):
                yield RichLog(
                    highlight=True, 
                    markup=True,
                    wrap=True,
                    auto_scroll=True,
                    id="monitor-richlog"
                )
            
            # Bottom half - Rippled output
            with VerticalScroll(id="rippled-output"):
                yield RichLog(
                    highlight=True,
                    markup=True,
                    wrap=True,
                    auto_scroll=True,
                    id="rippled-richlog"
                )
                
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when the app is mounted."""
        # Get widget references
        self.monitor_log = self.query_one("#monitor-richlog", RichLog)
        self.rippled_log = self.query_one("#rippled-richlog", RichLog)
        self.status_text = self.query_one("#status-text", Static)
        self.memory_text = self.query_one("#memory-text", Static)
        self.complete_ledgers_text = self.query_one("#complete-ledgers", Static)
        self.current_ledger_text = self.query_one("#current-ledger", Static)
        
        # Initial messages
        self.monitor_log.write(Text("Memory Monitor Log", style="bold cyan"))
        self.monitor_log.write("-" * 60)
        self.monitor_log.write(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.monitor_log.write(f"Config: {self.config.rippled_config_path}")
        self.monitor_log.write(f"WebSocket: {self.config.websocket_url}")
        self.monitor_log.write(f"Mode: {'standalone' if self.config.standalone_mode else 'network'}")
        self.monitor_log.write(f"API Version: {self.config.api_version}")
        self.monitor_log.write("")
        
        self.rippled_log.write(Text("Rippled Output", style="bold green"))
        self.rippled_log.write("-" * 60)
        self.rippled_log.write("Waiting to start rippled process...")
        self.rippled_log.write("")
        
        # Start update loops
        self.set_interval(0.1, self.process_output_queues)
        self.set_interval(1.0, self.update_stats)
        
        # Start the test
        self.run_worker(self.start_test, exclusive=True)
    
    def log_to_monitor(self, message: str, style: str = None):
        """Add a message to the monitor log queue."""
        self.monitor_queue.put((message, style))
    
    def process_output_queues(self) -> None:
        """Process output from all queues."""
        # Process monitor log queue
        count = 0
        while not self.monitor_queue.empty() and count < 10:
            try:
                message, style = self.monitor_queue.get_nowait()
                if not self.paused:
                    if style:
                        self.monitor_log.write(f"[{style}]{message}[/{style}]")
                    else:
                        self.monitor_log.write(message)
                count += 1
            except queue.Empty:
                break
        
        # Process stdout
        count = 0
        while not self.stdout_queue.empty() and count < 10:  # Limit per update
            try:
                line = self.stdout_queue.get_nowait()
                if not self.paused:
                    self.rippled_log.write(f"[cyan][stdout][/cyan] {line}")
                count += 1
            except queue.Empty:
                break
        
        # Process stderr
        count = 0
        while not self.stderr_queue.empty() and count < 10:
            try:
                line = self.stderr_queue.get_nowait()
                if not self.paused:
                    self.rippled_log.write(f"[yellow][stderr][/yellow] {line}")
                count += 1
            except queue.Empty:
                break
    
    def update_stats(self) -> None:
        """Update memory and process stats."""
        if self.monitor.current_process and self.monitor.current_process.is_alive():
            try:
                memory_stats = self.monitor.current_process.get_memory_usage()
                if memory_stats:
                    memory_mb = memory_stats.get('rss', 0)
                    memory_percent = memory_stats.get('percent', 0)
                    self.memory_text.update(f"Memory: {memory_mb:.1f}MB ({memory_percent:.1f}%)")
                else:
                    self.memory_text.update("Memory: N/A")
            except Exception:
                self.memory_text.update("Memory: N/A")
        else:
            self.memory_text.update("Memory: N/A")
    
    async def start_test(self) -> None:
        """Start the memory monitoring test."""
        # Set up custom logging handler to capture logs
        import logging
        
        class QueueHandler(logging.Handler):
            def __init__(self, queue):
                super().__init__()
                self.queue = queue
                
            def emit(self, record):
                msg = self.format(record)
                # Map log levels to rich styles
                style_map = {
                    logging.ERROR: "red",
                    logging.WARNING: "yellow",
                    logging.INFO: None,
                    logging.DEBUG: "dim"
                }
                style = style_map.get(record.levelno, None)
                self.queue.put((msg, style))
        
        # Add queue handler to monitor logger
        handler = QueueHandler(self.monitor_queue)
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.monitor.logger.addHandler(handler)
        
        # Capture output from RippledProcess
        original_capture = RippledProcess._capture_output_thread
        
        def capture_with_queues(self):
            """Modified capture that also puts output in our queues"""
            while not self.stop_output_capture.is_set() and self.process:
                try:
                    streams = []
                    if self.process.stdout:
                        streams.append(self.process.stdout)
                    if self.process.stderr:
                        streams.append(self.process.stderr)
                    
                    if not streams:
                        break
                    
                    readable, _, _ = select.select(streams, [], [], 0.1)
                    
                    for stream in readable:
                        if stream == self.process.stdout:
                            line = stream.readline()
                            if line:
                                line = line.strip()
                                self.stdout_buffer.append(line)
                                if len(self.stdout_buffer) > 100:
                                    self.stdout_buffer.pop(0)
                                # Add to dashboard queue
                                dashboard.stdout_queue.put(line)
                        elif stream == self.process.stderr:
                            line = stream.readline()
                            if line:
                                line = line.strip()
                                self.stderr_buffer.append(line)
                                if len(self.stderr_buffer) > 100:
                                    self.stderr_buffer.pop(0)
                                # Add to dashboard queue
                                dashboard.stderr_queue.put(line)
                    
                except Exception as e:
                    if not self.stop_output_capture.is_set():
                        self.logger.debug(f"Error in output capture thread: {e}")
                    break
        
        # Monkey patch the capture method
        dashboard = self
        RippledProcess._capture_output_thread = capture_with_queues
        
        # Set up WebSocket monitoring coroutine
        async def monitor_websocket():
            """Monitor WebSocket for server info updates"""
            while self.monitoring:
                if self.monitor.client:
                    try:
                        request = ServerInfo(api_version=self.config.api_version)
                        response = await self.monitor.client.request(request)
                        
                        if response.is_successful():
                            info = response.result.get('info', {})
                            
                            # Update complete ledgers
                            complete_ledgers = info.get('complete_ledgers', 'empty')
                            if complete_ledgers != self.complete_ledgers:
                                self.complete_ledgers = complete_ledgers
                                
                                if complete_ledgers and complete_ledgers != 'empty':
                                    # Parse ledger count
                                    ledger_count = self.parse_ledger_count(complete_ledgers)
                                    if ledger_count > 0:
                                        self.complete_ledgers_text.update(
                                            f"Complete Ledgers: {self.format_ledgers(complete_ledgers)} ({ledger_count})"
                                        )
                                    else:
                                        self.complete_ledgers_text.update(f"Complete Ledgers: {complete_ledgers}")
                                else:
                                    self.complete_ledgers_text.update("Complete Ledgers: empty")
                            
                            # Update current ledger
                            current = info.get('validated_ledger', {}).get('seq', 'N/A')
                            self.current_ledger_text.update(f"Current: {current}")
                            
                    except Exception:
                        pass  # Silently ignore to avoid log spam
                
                await asyncio.sleep(2)
        
        # Start monitoring
        self.monitoring = True
        
        # Update status
        self.status_text.update("Status: Running tests...")
        
        # Create tasks
        monitor_task = asyncio.create_task(self.monitor.run_all_tests())
        websocket_task = asyncio.create_task(monitor_websocket())
        
        try:
            # Wait for monitor to complete
            await monitor_task
            self.status_text.update("Status: All tests completed")
        except Exception as e:
            self.log_to_monitor(f"Error during tests: {e}", "red")
            self.status_text.update("Status: Error during tests")
        finally:
            self.monitoring = False
            websocket_task.cancel()
            try:
                await websocket_task
            except asyncio.CancelledError:
                pass
    
    def parse_ledger_count(self, ledgers: str) -> int:
        """Parse ledger range string to get total count."""
        from .cli import parse_ledger_ranges
        return parse_ledger_ranges(ledgers)
    
    def format_ledgers(self, ledgers: str, max_len: int = 40) -> str:
        """Format ledger string for display."""
        from .cli import format_ledger_ranges
        return format_ledger_ranges(ledgers, max_len)
    
    def action_clear(self) -> None:
        """Clear both logs."""
        self.monitor_log.clear()
        self.rippled_log.clear()
        
        self.monitor_log.write(Text("Memory Monitor Log", style="bold cyan"))
        self.monitor_log.write("-" * 60)
        
        self.rippled_log.write(Text("Rippled Output", style="bold green"))
        self.rippled_log.write("-" * 60)
    
    def action_pause(self) -> None:
        """Pause/resume updates."""
        self.paused = not self.paused
        status = "PAUSED" if self.paused else "RESUMED"
        self.log_to_monitor(f">>> {status} <<<", "bold yellow")
    
    def action_stop_process(self) -> None:
        """Stop the current rippled process."""
        if self.monitor.current_process:
            self.log_to_monitor("Stopping rippled process...", "yellow")
            self.monitor.shutdown_event.set()
            self.status_text.update("Status: Stopping...")