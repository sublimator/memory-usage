"""
Main dashboard application using dependency injection
"""

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer
from textual.binding import Binding
from datetime import datetime
import asyncio
from typing import TYPE_CHECKING
from dependency_injector.wiring import Provide, inject

if TYPE_CHECKING:
    from ..services.logging_service import LoggingService

from .components import StatusBar, MonitorLogViewer, ProcessOutputViewer
from ..container import Container
from ..managers import StateManager, ProcessManager, WebSocketManager
from ..services import MonitoringService
from ..config import Config


class MemoryMonitorDashboard(App):
    """Main dashboard application with proper DI"""
    
    CSS = """
    Screen {
        background: $surface;
    }
    
    #status-bar-container {
        dock: top;
        height: 3;
        width: 100%;
    }
    
    #status-bar {
        height: 3;
        background: $panel;
        border: solid $primary;
        padding: 0 1;
        width: 100%;
        layout: horizontal;
    }
    
    .status-text {
        width: 1fr;
        content-align: left middle;
        color: $text;
        text-style: bold;
    }
    
    .memory-text {
        width: 1fr;
        content-align: center middle;
        color: $success;
    }
    
    .complete-ledgers {
        width: 2fr;
        content-align: center middle;
        color: $warning;
    }
    
    .current-ledger {
        width: 1fr;
        content-align: right middle;
        color: $accent;
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
    
    @inject
    def __init__(
        self,
        config: Config = Provide[Container.config],
        state_manager: StateManager = Provide[Container.state_manager],
        process_manager: ProcessManager = Provide[Container.process_manager],
        websocket_manager: WebSocketManager = Provide[Container.websocket_manager],
        monitoring_service: MonitoringService = Provide[Container.monitoring_service],
        logging_service: "LoggingService" = Provide[Container.logging_service]
    ):
        super().__init__()
        self.config = config
        self.state_manager = state_manager
        self.process_manager = process_manager
        self.websocket_manager = websocket_manager
        self.monitoring_service = monitoring_service
        self.logging_service = logging_service
        
        self.title = "Xahaud Memory Monitor Dashboard"
        self.sub_title = "Real-time memory monitoring"
        
        # UI components
        self.status_bar = None
        self.monitor_log = None
        self.process_output = None
        
        # Background task
        self.monitoring_task = None
    
    def compose(self) -> ComposeResult:
        """Create the layout"""
        yield Header()
        
        # Status bar
        self.status_bar = StatusBar(self.state_manager)
        yield self.status_bar
        
        with Vertical(id="main-container"):
            # Top half - Memory monitor logs
            with VerticalScroll(id="monitor-log"):
                self.monitor_log = MonitorLogViewer()
                yield self.monitor_log
            
            # Bottom half - Rippled output
            with VerticalScroll(id="rippled-output"):
                self.process_output = ProcessOutputViewer()
                yield self.process_output
        
        yield Footer()
    
    def on_mount(self) -> None:
        """Called when the app is mounted"""
        # Set up logging to capture monitor logs
        self._setup_logging()
        
        # Set up process output capture
        self._setup_process_output()
        
        # Initial log messages
        self._log_startup_info()
        
        # Start update loops
        self.set_interval(0.1, self._process_queues)
        self.set_interval(1.0, self._update_memory_stats)
        
        # Start the test
        self.run_worker(self._start_monitoring, exclusive=True)
    
    def _setup_logging(self):
        """Set up logging to capture to the monitor log"""
        from ..services.logging_service import LogLevel
        
        def log_handler(message: str, level: LogLevel):
            """Handle log messages from the logging service"""
            style_map = {
                LogLevel.ERROR: "red",
                LogLevel.WARNING: "yellow",
                LogLevel.INFO: None,
                LogLevel.DEBUG: "dim",
                LogLevel.CRITICAL: "bold red"
            }
            style = style_map.get(level, None)
            self.monitor_log.queue_message(message, style)
        
        # Add handler to logging service
        self.logging_service.add_handler(log_handler)
    
    def _setup_process_output(self):
        """Set up process output capture"""
        # Subscribe to process output
        def on_stdout(line: str):
            self.process_output.queue_stdout(line)
        
        def on_stderr(line: str):
            self.process_output.queue_stderr(line)
        
        # Hook these up to the process manager
        self.process_manager.subscribe_stdout(on_stdout)
        self.process_manager.subscribe_stderr(on_stderr)
    
    def _log_startup_info(self):
        """Log initial startup information"""
        self.monitor_log.queue_message(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.monitor_log.queue_message(f"Config: {self.config.rippled_config_path}")
        self.monitor_log.queue_message(f"WebSocket: {self.config.websocket_url}")
        self.monitor_log.queue_message(f"Mode: {'standalone' if self.config.standalone_mode else 'network'}")
        self.monitor_log.queue_message(f"API Version: {self.config.api_version}")
        self.monitor_log.queue_message("")
        
        self.process_output.queue_message("Waiting to start rippled process...")
        self.process_output.queue_message("")
    
    def _process_queues(self):
        """Process all message queues"""
        self.monitor_log.process_queue()
        self.process_output.process_queue()
    
    async def _update_memory_stats(self):
        """Update memory statistics"""
        # Get memory stats from process manager
        memory_stats = self.process_manager.get_memory_stats()
        if memory_stats:
            await self.state_manager.update_memory_stats(
                memory_stats.get('rss', 0),
                memory_stats.get('percent', 0),
                memory_stats.get('num_threads', 0)
            )
    
    async def _start_monitoring(self):
        """Start the monitoring process"""
        try:
            # Find binaries
            binaries = self.process_manager.find_binaries()
            if not binaries:
                self.logging_service.error("No binaries found!")
                await self.state_manager.update_status("Error: No binaries found")
                return
            
            # Start monitoring
            await self.monitoring_service.start_monitoring(binaries)
            
        except Exception as e:
            self.logging_service.error(f"Error during monitoring: {e}")
            await self.state_manager.update_status(f"Error: {str(e)}")
    
    def action_clear(self) -> None:
        """Clear both logs"""
        self.monitor_log.clear()
        self.process_output.clear()
        
        # Re-add titles
        self.monitor_log._initialized = False
        self.process_output._initialized = False
        self.monitor_log.on_mount()
        self.process_output.on_mount()
    
    async def action_pause(self) -> None:
        """Pause/resume updates"""
        is_paused = await self.state_manager.toggle_pause()
        status = "PAUSED" if is_paused else "RESUMED"
        self.monitor_log.queue_message(f">>> {status} <<<", "bold yellow")
    
    async def action_stop_process(self) -> None:
        """Stop the current rippled process"""
        self.monitor_log.queue_message("Stopping rippled process...", "yellow")
        await self.monitoring_service.stop_monitoring()
        await self.state_manager.update_status("Stopped by user")