"""
Main dashboard application using dependency injection
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from dependency_injector.wiring import Provide, inject
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, TabbedContent, TabPane
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
    HeapDisplay,
    JobsDisplay,
    MemoryBreakdownDisplay,
    MemoryGraph,
    MonitorLogViewer,
    ProcessOutputViewer,
    SHAMapPoolsDisplay,
    StatusBar,
)


def _parse_event_ts(t: Optional[str]) -> Optional[datetime]:
    if not t:
        return None
    try:
        # SessionStore writes ``YYYY-MM-DDTHH:MM:SS[.sss]Z``. fromisoformat
        # doesn't accept the trailing Z in older Pythons, so strip it.
        return datetime.fromisoformat(t.rstrip("Z"))
    except (ValueError, TypeError):
        return None


def _derive_timing_from_events(
    events: List[Dict[str, Any]],
) -> tuple[float, float, Optional[float]]:
    """Compute (prior_elapsed, prior_monitoring, original_sync) from events.

    Walks the event stream and reconstructs per-session spans using the
    ``t`` timestamps that every event carries. For each session:

    - Session span = time from session_start → session_end (or the last
      event before the next session_start / end of stream if session_end
      is missing — e.g. the monitor was killed).
    - Sync-complete point = first snapshot whose ``monitoring_elapsed_seconds``
      is not None. Duration from session_start to that point is this
      session's sync time.
    - Monitoring time = session end minus sync-complete point.

    ``original_sync`` is the first session whose sync time exceeded 1s —
    shorter than that means "reattached to an already-synced node" and
    isn't the real cold-boot sync time.
    """
    prior_elapsed = 0.0
    prior_monitoring = 0.0
    original_sync: Optional[float] = None

    current_start: Optional[datetime] = None
    current_synced_at: Optional[datetime] = None
    current_last_t: Optional[datetime] = None

    def close_session(end_at: datetime) -> None:
        nonlocal prior_elapsed, prior_monitoring, original_sync
        if current_start is None:
            return
        prior_elapsed += max(0.0, (end_at - current_start).total_seconds())
        if current_synced_at is not None:
            session_sync = (current_synced_at - current_start).total_seconds()
            if original_sync is None and session_sync > 1.0:
                original_sync = session_sync
            prior_monitoring += max(0.0, (end_at - current_synced_at).total_seconds())

    for ev in events:
        t = _parse_event_ts(ev.get("t"))
        kind = ev.get("event")
        if kind == "session_start":
            if current_start is not None and current_last_t is not None:
                # Previous session lacked a session_end (crash/kill). Use
                # the last event we saw as its end.
                close_session(current_last_t)
            current_start = t
            current_synced_at = None
            current_last_t = t
        elif kind == "session_end":
            if current_start is not None and t is not None:
                close_session(t)
            current_start = None
            current_synced_at = None
            current_last_t = None
        elif kind == "snapshot":
            if t is not None:
                current_last_t = t
            if current_start is not None and current_synced_at is None:
                if ev.get("monitoring_elapsed_seconds") is not None:
                    current_synced_at = t
        else:
            if t is not None:
                current_last_t = t

    # Open session at EOF (monitor still running during this hydrate is not
    # possible, but a prior run that was killed leaves this open). Close it
    # at the last timestamp we saw.
    if current_start is not None and current_last_t is not None:
        close_session(current_last_t)

    return prior_elapsed, prior_monitoring, original_sync


class MemoryMonitorDashboard(App):
    """Main dashboard application with proper DI"""

    # UI components — assigned in compose(), declared here for type narrowing.
    # The Stats tab needs its own widget instances (a widget can only be in
    # one place in the textual tree). Both sets are kept in sync by the
    # state observer in _setup_state_observer.
    status_bar: StatusBar
    monitor_log: MonitorLogViewer
    process_output: ProcessOutputViewer
    memory_graph: MemoryGraph
    # Overview tab diagnostics (compact column on the right)
    counts_display: CountsDisplay
    jobs_display: JobsDisplay
    catalogue_display: CatalogueStatusDisplay
    memory_breakdown_display: MemoryBreakdownDisplay
    shamap_pools_display: SHAMapPoolsDisplay
    # Stats tab — same widgets, more room
    counts_display_stats: CountsDisplay
    jobs_display_stats: JobsDisplay
    catalogue_display_stats: CatalogueStatusDisplay
    memory_breakdown_display_stats: MemoryBreakdownDisplay
    shamap_pools_display_stats: SHAMapPoolsDisplay
    # Heap tab — only populated when --heap-every-ledger is enabled on a
    # macOS host; empty panel with a gentle message otherwise.
    heap_display: HeapDisplay

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

    /* 1fr (not 100%) so main-container shares space with the memory
       graph below it inside the Overview TabPane. dock: bottom stopped
       working cleanly once these two siblings were inside a pane. */
    #main-container {
        height: 1fr;
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
        /* Hidden by default — only relevant on Xahau builds with
           catalogue loading active. Press 'g' to toggle. */
        display: none;
    }

    MemoryBreakdownDisplay {
        height: 2fr;
        border: solid $success;
        padding: 1;
        background: $surface;
    }

    SHAMapPoolsDisplay {
        height: 2fr;
        border: solid $accent;
        padding: 1;
        background: $surface;
    }

    /* Fill whatever height is left after Header + status bar + Footer.
       Without this the TabPane content collapses to 0 because its
       parent has no explicit size to percent from. */
    TabbedContent {
        height: 1fr;
    }

    /* Stats tab: 50/50 horizontal split, no logs/graph competing */
    #stats-container {
        height: 1fr;
        width: 100%;
        layout: horizontal;
    }

    #stats-left {
        width: 1fr;
        height: 100%;
        layout: vertical;
    }

    /* overflow-y: auto makes the column scroll when the stacked panels
       together exceed the tab height, rather than each panel having a
       fixed fraction and wasting space on short panels / forcing scroll
       on long ones. */
    #stats-right {
        width: 1fr;
        height: 100%;
        layout: vertical;
        overflow-y: auto;
    }

    /* Left column: single CountsDisplay fills the full column height. */
    #stats-left CountsDisplay {
        height: 100%;
    }

    /* Right column panels hug their content. min-height keeps even an
       empty panel legible; max-height caps oversized ones so no single
       panel can monopolise the viewport. */
    #stats-right MemoryBreakdownDisplay,
    #stats-right SHAMapPoolsDisplay,
    #stats-right JobsDisplay,
    #stats-right CatalogueStatusDisplay {
        height: auto;
        min-height: 6;
        max-height: 60;
    }

    /* Heap tab: single widget fills the whole pane. */
    HeapDisplay {
        height: 1fr;
        border: solid $accent;
        padding: 1;
        background: $surface;
    }

    /* Fixed 17-row height so it sits at the bottom of the Overview pane
       without docking (which interacted poorly with TabPane). */
    #memory-graph {
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
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("c", "clear", "Clear logs"),
        Binding("space", "pause", "Pause/Resume"),
        Binding("s", "stop_process", "Stop rippled"),
        Binding("g", "toggle_catalogue", "Catalogue"),
        # Numeric shortcuts: 1=Overview, 2=Stats, 3=Heap. priority=True so
        # focused children (log viewers, scroll panes) can't swallow them
        # or bounce focus back mid-switch. show=False keeps the footer
        # uncluttered.
        Binding("1", "show_tab('tab-overview')", "Overview", show=False, priority=True),
        Binding("2", "show_tab('tab-stats')", "Stats", show=False, priority=True),
        Binding("3", "show_tab('tab-heap')", "Heap", show=False, priority=True),
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

        # Status bar (always visible above the tabs)
        self.status_bar = StatusBar(self.state_manager)
        yield self.status_bar

        with TabbedContent(initial="tab-overview"):
            # ─── Overview: logs + compact stats column + memory graph ───
            with TabPane("Overview", id="tab-overview"):
                with Horizontal(id="main-container"):
                    with Vertical(id="left-panel"):
                        with VerticalScroll(id="monitor-log"):
                            self.monitor_log = MonitorLogViewer()
                            yield self.monitor_log

                        with VerticalScroll(id="rippled-output"):
                            self.process_output = ProcessOutputViewer()
                            yield self.process_output

                    with Vertical(id="right-panel"):
                        self.counts_display = CountsDisplay()
                        yield self.counts_display

                        self.jobs_display = JobsDisplay()
                        yield self.jobs_display

                        self.catalogue_display = CatalogueStatusDisplay()
                        yield self.catalogue_display

                        self.memory_breakdown_display = MemoryBreakdownDisplay()
                        yield self.memory_breakdown_display

                        self.shamap_pools_display = SHAMapPoolsDisplay()
                        yield self.shamap_pools_display

                self.memory_graph = MemoryGraph()
                yield self.memory_graph

            # ─── Stats: same widgets, no logs/graph, much more room ───
            with TabPane("Stats", id="tab-stats"):
                with Horizontal(id="stats-container"):
                    with Vertical(id="stats-left"):
                        self.counts_display_stats = CountsDisplay()
                        yield self.counts_display_stats

                    with Vertical(id="stats-right"):
                        self.memory_breakdown_display_stats = MemoryBreakdownDisplay()
                        yield self.memory_breakdown_display_stats

                        self.shamap_pools_display_stats = SHAMapPoolsDisplay()
                        yield self.shamap_pools_display_stats

                        self.jobs_display_stats = JobsDisplay()
                        yield self.jobs_display_stats

                        self.catalogue_display_stats = CatalogueStatusDisplay()
                        yield self.catalogue_display_stats

            # ─── Heap: live heap(1) top-classes + ↑↑ monotonic grower mark ──
            with TabPane("Heap", id="tab-heap"):
                self.heap_display = HeapDisplay()
                yield self.heap_display

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

        # Register UI hydration for reattach — MonitoringService invokes this
        # during _open_session when a prior events.jsonl exists, so widgets
        # are populated before live updates resume.
        self.monitoring_service.set_hydrate_callback(self._hydrate_ui_from_events)
        # Reset hook fires between --reattach incarnations: clear all
        # widget history so the dashboard looks freshly launched when the
        # new process takes over.
        self.monitoring_service.set_reset_callback(self._reset_ui_for_new_incarnation)

        # Start the test (save worker so we can cancel it on quit)
        self._monitoring_worker = self.run_worker(self._start_monitoring, exclusive=True)

    async def _reset_ui_for_new_incarnation(self) -> None:
        """Cold-launch look for the dashboard between --reattach cycles.

        Widget-local histories (memory graph points, counts trend, heap
        sample) are wiped; widgets that derive from ApplicationState
        repaint empty because we both reset the state AND push ``None``
        through their update methods. The observer fires branches only
        when the new value is truthy, so leaving it to state_manager
        alone wouldn't clear stale panels.
        """
        self.memory_graph.reset()
        self.counts_display.reset_history()
        self.counts_display_stats.reset_history()
        self.heap_display.reset_history()
        # Force widgets that render from state fields back to "no data".
        for counts in (self.counts_display, self.counts_display_stats):
            counts.update_counts(None)
        for jobs in (self.jobs_display, self.jobs_display_stats):
            jobs.update_jobs(None)
        for cat in (self.catalogue_display, self.catalogue_display_stats):
            cat.update_catalogue_status(None)
        for bd in (self.memory_breakdown_display, self.memory_breakdown_display_stats):
            bd.update_breakdown(None)
        for pools in (self.shamap_pools_display, self.shamap_pools_display_stats):
            pools.update_counts(None)
        self.heap_display.update_sample(None)
        # Reset the shared state last so the status bar observer fires
        # against the empty ApplicationState (blank memory + Total=0 etc).
        await self.state_manager.reset_state()
        # Process log viewer is informational — leave it (user may want to
        # scroll back to see what the dead process said right before it
        # died). The new process's output tail will append below.
        self.monitor_log.queue_message(">>> Reset for new incarnation <<<", "bold cyan")

    async def _hydrate_ui_from_events(
        self, events: List[Dict[str, Any]], meta: Dict[str, Any]
    ) -> None:
        """Replay prior snapshots + derive baselines from event timestamps.

        Timing is computed from the jsonl stream's ``t`` field, NOT from
        meta.json session entries — durations there only exist if the
        monitor exited cleanly, but events are line-buffered so they
        survive Ctrl+C, SIGKILL, panic, etc. meta is treated as an
        optional cache, nothing more.
        """
        prior_elapsed, prior_monitoring, original_sync = _derive_timing_from_events(events)
        state = self.state_manager.state
        state.prior_elapsed_seconds = prior_elapsed
        state.prior_monitoring_seconds = prior_monitoring
        if original_sync is not None:
            state.original_sync_duration_seconds = original_sync

        snapshots = [e for e in events if e.get("event") == "snapshot"]
        if not snapshots:
            # Still notify observers so the timing baseline lands in the UI.
            await self.state_manager._notify_observers()
            return

        # Memory graph: bulk-load (unix_ts, rss_mb) points.
        points: List[tuple[float, float]] = []
        for s in snapshots:
            ts_str = s.get("timestamp")
            rss = s.get("rss_mb", 0) or 0
            if ts_str and rss > 0:
                try:
                    # Pydantic isoformat() — no Z suffix, but strip just in case.
                    ts = datetime.fromisoformat(ts_str.rstrip("Z")).timestamp()
                    points.append((ts, float(rss)))
                except (ValueError, TypeError):
                    continue
        if points:
            self.memory_graph.hydrate(points)

        # Counts trend: each historical counts dict fed through the display
        # so _TREND_WINDOW has real samples to latch onto. Inefficient (one
        # render per call) but one-off at attach time.
        for s in snapshots:
            counts = s.get("counts")
            if counts:
                self.counts_display.update_counts(counts)
                self.counts_display_stats.update_counts(counts)

        # Heap sample history — replay so the ↑↑ monotonic markers reflect
        # the full series, not just what arrives post-reattach. Most
        # snapshots won't have a heap_sample (feature is opt-in), so the
        # inner work only fires on the subset that does.
        self.heap_display.reset_history()
        last_heap: Optional[Dict[str, Any]] = None
        for s in snapshots:
            hs = s.get("heap_sample")
            if isinstance(hs, dict) and hs.get("ok"):
                self.heap_display.update_sample(hs)
                last_heap = hs
        if last_heap is not None:
            self.state_manager.state.heap_sample = last_heap

        # Latest values -> state, then a single notify so other widgets paint
        # once from the tail of the history. Everything the status bar and
        # side panels read from ApplicationState is repopulated here so
        # reattach is visually indistinguishable from "never left".
        latest = snapshots[-1]
        state = self.state_manager.state
        if latest.get("counts"):
            state.counts = latest["counts"]
        if latest.get("job_types"):
            state.job_types = latest["job_types"]
        if latest.get("memory_breakdown"):
            state.memory_breakdown = latest["memory_breakdown"]
        if latest.get("catalogue_status"):
            state.catalogue_status = latest["catalogue_status"]
        if latest.get("complete_ledgers"):
            state.complete_ledgers = latest["complete_ledgers"]
            state.ledger_count = latest.get("ledger_count", 0) or 0
        rss_mb = latest.get("rss_mb")
        if rss_mb:
            state.current_memory_mb = float(rss_mb)
            state.current_memory_percent = float(latest.get("memory_percent", 0) or 0)
            state.num_threads = int(latest.get("num_threads", 0) or 0)

        # server_info-derived diagnostics — status bar shows these in the
        # [state] prefix, uptime, and ledger-age suffix. Without this block
        # those go blank for ~2-3s after reattach until the next RPC.
        for field in (
            "sync_start_ledger",
            "server_state",
            "validated_age_s",
            "closed_ledger_seq",
            "closed_ledger_age_s",
            "rippled_uptime_s",
        ):
            val = latest.get(field)
            if val is not None:
                setattr(state, field, val)

        await self.state_manager._notify_observers()

        self.monitor_log.queue_message(
            f">>> Hydrated {len(snapshots)} prior snapshot(s) <<<", "bold cyan"
        )

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
            # Both tabs' diagnostic widgets are kept in sync from a single
            # state observer — Stats tab has its own instances because a
            # widget can't appear twice in textual's tree.
            if state.job_types:
                self.jobs_display.update_jobs(state.job_types)
                self.jobs_display_stats.update_jobs(state.job_types)
            if state.counts:
                self.counts_display.update_counts(state.counts)
                self.counts_display_stats.update_counts(state.counts)
                # tagged_pointer_pools / treenode_cache_locks ride on counts
                self.shamap_pools_display.update_counts(state.counts)
                self.shamap_pools_display_stats.update_counts(state.counts)
            if state.catalogue_status:
                self.catalogue_display.update_catalogue_status(state.catalogue_status)
                self.catalogue_display_stats.update_catalogue_status(state.catalogue_status)
            if state.memory_breakdown:
                self.memory_breakdown_display.update_breakdown(state.memory_breakdown)
                self.memory_breakdown_display_stats.update_breakdown(state.memory_breakdown)
            if state.heap_sample:
                self.heap_display.update_sample(state.heap_sample)

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

    def action_show_tab(self, tab_id: str) -> None:
        """Switch to a tab by id (bound to 1/2/3)."""
        from textual.widgets import TabbedContent

        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    def action_toggle_catalogue(self) -> None:
        """Show/hide the Xahau catalogue status panel across both tabs.

        Hidden by default because it only produces useful output on Xahau
        builds that are actively loading a catalogue; on stock rippled and
        on a synced Xahau node it's just 'No catalogue data available'.
        """
        new_display = not self.catalogue_display.display
        self.catalogue_display.display = new_display
        self.catalogue_display_stats.display = new_display

    async def action_quit(self) -> None:
        """Quit the application with proper cleanup.

        Cancel the monitoring worker and *wait* for its ``finally`` block to
        run — that's where _close_session writes meta.json with this
        session's durations. If we exit before the finally completes, Total
        never gets persisted and the next reattach's baseline is 0.
        """
        if self._monitoring_worker is not None:
            self._monitoring_worker.cancel()
            try:
                # Cap at 2s: _close_session is synchronous file I/O (<10ms);
                # the only thing that can drag is process.stop, and we ask
                # that to fire-and-forget via wait_for_process=False below.
                await asyncio.wait_for(self._monitoring_worker.wait(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass

        try:
            await asyncio.wait_for(
                self.monitoring_service.stop_monitoring(wait_for_process=False),
                timeout=1.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass  # Best effort — OS will reap anything left
        self.exit()
