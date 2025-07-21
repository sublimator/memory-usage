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


class RippledOutputCapture:
    """Handles capturing output from rippled process."""
    
    def __init__(self, process: subprocess.Popen, stdout_queue: queue.Queue, stderr_queue: queue.Queue):
        self.process = process
        self.stdout_queue = stdout_queue
        self.stderr_queue = stderr_queue
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
    
    def _capture_loop(self):
        """Capture output from process streams."""
        while not self.stop_event.is_set() and self.process.poll() is None:
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
                    line = stream.readline()
                    if line:
                        line = line.strip()
                        if stream == self.process.stdout:
                            self.stdout_queue.put(line)
                        else:
                            self.stderr_queue.put(line)
                            
            except Exception:
                break
    
    def stop(self):
        """Stop capturing output."""
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)


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
        
        # Process management
        self.process: Optional[subprocess.Popen] = None
        self.output_capture: Optional[RippledOutputCapture] = None
        self.monitoring = False
        
        # WebSocket client
        self.client: Optional[AsyncWebsocketClient] = None
        
        # Current state
        self.complete_ledgers = "empty"
        self.current_ledger = "N/A"
        self.process_pid = None
        
        # Output queues
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()
    
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
    
    def process_output_queues(self) -> None:
        """Process output from rippled."""
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
        if self.process and self.process.poll() is None:
            try:
                proc = psutil.Process(self.process.pid)
                memory_info = proc.memory_info()
                memory_mb = memory_info.rss / (1024 * 1024)
                memory_percent = proc.memory_percent()
                
                self.memory_text.update(f"Memory: {memory_mb:.1f}MB ({memory_percent:.1f}%)")
                
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.memory_text.update("Memory: N/A")
    
    async def start_test(self) -> None:
        """Start the memory monitoring test."""
        # Find binaries
        binaries = self.find_binaries()
        if not binaries:
            self.monitor_log.write("[red]No rippled binaries found![/red]")
            self.status_text.update("Status: Error - No binaries")
            return
        
        # For now, test first binary
        binary = binaries[0]
        binary_name = Path(binary).name
        
        self.monitor_log.write(f"[bold cyan]Starting test for {binary_name}[/bold cyan]")
        
        # Start process
        await self.start_rippled(binary, binary_name)
        
        # Connect to WebSocket
        await self.connect_websocket()
        
        # Start monitoring
        if self.client:
            self.run_worker(self.monitor_server_info, exclusive=False)
    
    async def start_rippled(self, binary_path: str, binary_name: str) -> None:
        """Start the rippled process."""
        cmd = [binary_path, "--conf", self.config.rippled_config_path]
        
        if self.config.standalone_mode:
            cmd.append("--standalone")
        else:
            cmd.append("--net")
        
        self.monitor_log.write(f"Starting: {' '.join(cmd)}")
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                universal_newlines=True,
                preexec_fn=None
            )
            
            self.process_pid = self.process.pid
            self.status_text.update(f"Status: Running (PID: {self.process_pid})")
            self.monitor_log.write(f"[green]Started {binary_name} with PID: {self.process_pid}[/green]")
            
            # Start output capture
            self.output_capture = RippledOutputCapture(
                self.process, self.stdout_queue, self.stderr_queue
            )
            
            self.monitoring = True
            
        except Exception as e:
            self.monitor_log.write(f"[red]Failed to start: {e}[/red]")
            self.status_text.update("Status: Failed to start")
    
    async def connect_websocket(self) -> None:
        """Connect to rippled websocket."""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                self.client = AsyncWebsocketClient(self.config.websocket_url)
                await self.client.open()
                self.monitor_log.write(f"[green]Connected to WebSocket at {self.config.websocket_url}[/green]")
                return
            except Exception as e:
                self.monitor_log.write(f"[yellow]WebSocket attempt {attempt + 1} failed: {e}[/yellow]")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    self.monitor_log.write("[red]Failed to connect to WebSocket[/red]")
    
    async def monitor_server_info(self) -> None:
        """Monitor server info for ledger status."""
        while self.monitoring and self.client:
            try:
                request = ServerInfo(api_version=self.config.api_version)
                response = await self.client.request(request)
                
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
                    
            except Exception as e:
                pass  # Silently ignore to avoid log spam
            
            await asyncio.sleep(2)
    
    def parse_ledger_count(self, ledgers: str) -> int:
        """Parse ledger range string to get total count."""
        if not ledgers or ledgers == 'empty':
            return 0
        
        total = 0
        try:
            for range_str in ledgers.split(','):
                if '-' in range_str:
                    parts = range_str.strip().split('-')
                    if len(parts) == 2:
                        start = int(parts[0])
                        end = int(parts[1])
                        total += (end - start + 1)
        except:
            return 0
        
        return total
    
    def format_ledgers(self, ledgers: str, max_len: int = 40) -> str:
        """Format ledger string for display."""
        if len(ledgers) <= max_len:
            return ledgers
        
        ranges = ledgers.split(',')
        if len(ranges) > 2:
            return f"{ranges[0]}...{ranges[-1]}"
        return ledgers
    
    def find_binaries(self) -> List[str]:
        """Find rippled binaries."""
        if self.config.specified_binaries:
            return self.config.specified_binaries
        
        build_path = Path(self.config.build_dir)
        if not build_path.exists():
            return []
        
        binaries = []
        for file_path in build_path.glob("rippled-*"):
            if file_path.is_file() and file_path.stat().st_mode & 0o111:
                binaries.append(str(file_path))
        
        return sorted(binaries)
    
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
        self.monitor_log.write(f"[bold yellow]>>> {status} <<<[/bold yellow]")
    
    def action_stop_process(self) -> None:
        """Stop the current rippled process."""
        if self.process:
            self.monitor_log.write("[yellow]Stopping rippled process...[/yellow]")
            self.monitoring = False
            
            if self.output_capture:
                self.output_capture.stop()
            
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            
            self.status_text.update("Status: Stopped by user")
            self.monitor_log.write("[green]Process stopped[/green]")