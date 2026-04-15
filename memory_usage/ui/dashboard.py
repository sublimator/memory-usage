"""
Main dashboard application using dependency injection
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from dependency_injector.wiring import Provide, inject
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header
from textual.worker import Worker

if TYPE_CHECKING:
    from ..services.logging_service import LoggingService

from ..config import Config
from ..container import Container
from ..managers import ProcessManager, StateManager, WebSocketManager
from ..managers.state_manager import ApplicationState
from ..services import MonitoringService
from .components import (
    CatalogueStatusDisplay,
    CountsDisplay,
    JobsDisplay,
    MemoryBreakdownDisplay,
    MemoryGraph,
    MonitorLogViewer,
    ProcessOutputViewer,
    StatusBar,
)


class MemoryMonitorDashboard(App):
    """Main dashboard application with proper DI"""

    # UI components — assigned in compose(), declared here for type narrowing
    status_bar: StatusBar
    monitor_log: MonitorLogViewer
    process_output: ProcessOutputViewer
    counts_display: CountsDisplay
    jobs_display: JobsDisplay
    catalogue_display: CatalogueStatusDisplay
    memory_breakdown_display: MemoryBreakdownDisplay
    memory_graph: MemoryGraph

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
        width: 1.5fr;
        content-align: center middle;
        color: $success;
    }

    .ledgers-text {
        width: 3fr;
        content-align: center middle;
        color: $warning;
    }

    .timing-text {
        width: 2fr;
        content-align: right middle;
        color: $primary;
        text-style: italic;
    }

    #main-container {
        height: 100%;
        width: 100%;
        layout: horizontal;
    }

    #left-panel {
        width: 2fr;
        height: 100%;
        layout: vertical;
    }

    #monitor-log {
        height: 1fr;
        border: solid $primary;
        padding: 1;
    }

    #rippled-output {
        height: 1fr;
        border: solid $secondary;
        padding: 1;
    }

    #right-panel {
        width: 1fr;
        height: 100%;
        layout: vertical;
    }

    CountsDisplay {
        height: 2fr;
        border: solid $accent;
        padding: 1;
        background: $surface;
    }

    JobsDisplay {
        height: 1fr;
        border: solid $warning;
        padding: 1;
        background: $surface;
    }

    CatalogueStatusDisplay {
        height: 1fr;
        border: solid $secondary;
        padding: 1;
        background: $surface;
    }

    MemoryBreakdownDisplay {
        height: 2fr;
        border: solid $success;
        padding: 1;
        background: $surface;
    }

    #memory-graph {
        dock: bottom;
        height: 17;
        background: $panel;
        border: solid $primary;
        padding: 1;
        layout: horizontal;
    }

    .graph-controls {
        height: 1;
        dock: top;
        layout: horizontal;
    }

    .graph-label {
        width: auto;
        content-align: left middle;
        color: $text;
        margin-right: 1;
    }

    .time-button {
        width: auto;
        min-width: 5;
        height: 1;
        margin: 0 1;
        background: $surface;
        color: $text-disabled;
    }

    .time-button.active {
        background: $primary;
        color: $text;
    }

    .graph-spacer {
        width: 1fr;
    }

    .graph-value {
        width: auto;
        content-align: right middle;
        color: $success;
        text-style: bold;
    }

    .graph-area {
        height: 10;
        layout: horizontal;
        padding: 0;
    }

    #graph-display {
        width: 1fr;
        height: 100%;
        color: $success;
        content-align: left middle;
    }

    .time-scale {
        height: 1;
        dock: bottom;
        layout: horizontal;
    }

    .time-spacer {
        width: 8;
        content-align: left middle;
    }

    .time-marker {
        width: auto;
        content-align: left middle;
        color: $text-disabled;
        text-style: italic;
    }

    .time-marker-center {
        width: 1fr;
        content-align: center middle;
        color: $text-disabled;
        text-style: italic;
    }

    .time-marker-right {
        width: auto;
        content-align: right middle;
        color: $text-disabled;
        text-style: italic;
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
        logging_service: "LoggingService" = Provide[Container.logging_service],
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

        # Background tasks
        self.monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_worker: Optional[Worker] = None

    def compose(self) -> ComposeResult:
        """Create the layout"""
        yield Header()

        # Status bar
        self.status_bar = StatusBar(self.state_manager)
        yield self.status_bar

        with Horizontal(id="main-container"):
            # Left 2/3 - Vertical split for monitor logs and rippled output
            with Vertical(id="left-panel"):
                with VerticalScroll(id="monitor-log"):
                    self.monitor_log = MonitorLogViewer()
                    yield self.monitor_log

                with VerticalScroll(id="rippled-output"):
                    self.process_output = ProcessOutputViewer()
                    yield self.process_output

            # Right 1/3 - Counts, jobs, and catalogue
            with Vertical(id="right-panel"):
                self.counts_display = CountsDisplay()
                yield self.counts_display

                self.jobs_display = JobsDisplay()
                yield self.jobs_display

                self.catalogue_display = CatalogueStatusDisplay()
                yield self.catalogue_display

                self.memory_breakdown_display = MemoryBreakdownDisplay()
                yield self.memory_breakdown_display

        # Memory graph at the bottom
        self.memory_graph = MemoryGraph()
        yield self.memory_graph

        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is mounted"""
        # Set up logging to capture monitor logs
        self._setup_logging()

        # Set up process output capture
        self._setup_process_output()

        # Set up state observer for jobs display
        self._setup_state_observer()

        # Initial log messages
        self._log_startup_info()

        # Start update loops
        self.set_interval(0.1, self._process_queues)
        self.set_interval(1.0, self._update_memory_stats)

        # Start the test (save worker so we can cancel it on quit)
        self._monitoring_worker = self.run_worker(self._start_monitoring, exclusive=True)

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
                LogLevel.CRITICAL: "bold red",
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

    def _setup_state_observer(self):
        """Set up state observer for jobs, counts, and catalogue display"""
        self._last_pid = None  # Track last seen PID to detect process starts

        def on_state_change(state: ApplicationState):
            if state.job_types and self.jobs_display:
                self.jobs_display.update_jobs(state.job_types)
            if state.counts and self.counts_display:
                self.counts_display.update_counts(state.counts)
            if state.catalogue_status and self.catalogue_display:
                self.catalogue_display.update_catalogue_status(state.catalogue_status)
            if state.memory_breakdown and self.memory_breakdown_display:
                self.memory_breakdown_display.update_breakdown(state.memory_breakdown)

            # Detect new process start
            if state.current_pid and state.current_pid != self._last_pid:
                self._last_pid = state.current_pid
                # Display log file path in process output viewer
                log_path = self.process_manager.get_log_file_path()
                if log_path:
                    self.process_output.queue_info("")
                    self.process_output.queue_info(f"Process output logging to: {log_path}")
                    self.process_output.queue_info("")

        self.state_manager.subscribe(on_state_change)

    def _log_startup_info(self):
        """Log initial startup information"""
        self.monitor_log.queue_message(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.monitor_log.queue_message(f"Config: {self.config.rippled_config_path}")
        self.monitor_log.queue_message(f"WebSocket: {self.config.websocket_url}")

        if self.config.attach_mode:
            self.monitor_log.queue_message(
                f"Mode: ATTACH (PID: {self.config.attach_pid})", "bold cyan"
            )
            self.monitor_log.queue_message(f"Binary: {self.config.attach_binary_name}")
            if self.config.attach_debug_logfile:
                self.monitor_log.queue_message(f"Debug log: {self.config.attach_debug_logfile}")
        else:
            self.monitor_log.queue_message(
                f"Mode: {'standalone' if self.config.standalone_mode else 'network'}"
            )

        self.monitor_log.queue_message(f"API Version: {self.config.api_version}")
        self.monitor_log.queue_message("")

        if self.config.attach_mode:
            if self.config.attach_debug_logfile:
                self.process_output.queue_message(
                    f"Tailing debug log: {self.config.attach_debug_logfile}"
                )
            else:
                self.process_output.queue_message(
                    "Attach mode: No debug log file configured, cannot tail output"
                )
            self.process_output.queue_message("")
        else:
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
            memory_mb = memory_stats.get("rss", 0)
            await self.state_manager.update_memory_stats(
                memory_mb, memory_stats.get("percent", 0), memory_stats.get("num_threads", 0)
            )

            # Update memory graph
            if self.memory_graph and memory_mb > 0:
                self.memory_graph.update_memory(memory_mb)

    async def _start_monitoring(self):
        """Start the monitoring process"""
        try:
            # Check if we're in attach mode
            if self.config.attach_mode:
                # Attach mode - connect to existing process
                self.logging_service.info(
                    f"Attach mode: connecting to PID {self.config.attach_pid}"
                )
                await self.monitoring_service.start_attach_monitoring(
                    pid=self.config.attach_pid,
                    name=self.config.attach_binary_name,
                    binary_path=self.config.attach_binary_path,
                )
            else:
                # Normal mode - find and start binaries
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
        """Stop the current rippled process (or detach if in attach mode)"""
        if self.config.attach_mode:
            self.monitor_log.queue_message("Detaching from process...", "yellow")
            await self.monitoring_service.stop_monitoring()
            await self.state_manager.update_status("Detached by user")
        else:
            self.monitor_log.queue_message("Stopping rippled process...", "yellow")
            await self.monitoring_service.stop_monitoring()
            await self.state_manager.update_status("Stopped by user")

    async def action_quit(self) -> None:
        """Quit the application with proper cleanup.

        Cap cleanup at a short timeout, cancel the monitoring worker, and
        ask the monitoring service not to wait on the child — otherwise
        asyncio's interpreter-teardown step would block on the executor
        thread still inside ``subprocess.wait(timeout=10)``.
        """
        if self._monitoring_worker is not None:
            self._monitoring_worker.cancel()

        try:
            await asyncio.wait_for(
                self.monitoring_service.stop_monitoring(wait_for_process=False),
                timeout=1.5,
            )
        except (asyncio.TimeoutError, Exception):
            pass  # Best effort — OS will reap anything left
        self.exit()
