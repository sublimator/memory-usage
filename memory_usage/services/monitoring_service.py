"""
Main monitoring service that orchestrates the memory monitoring process
"""

import asyncio
import platform
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

import psutil

from ..models.memory_models import BinaryTestResult, MemorySnapshot, SystemInfo, TestConfiguration
from ..utils.formatters import format_duration, format_ledger_ranges
from ..utils.heap_sampler import is_supported as heap_supported
from ..utils.heap_sampler import take_heap_sample
from ..utils.memory_breakdown import MemoryBreakdown
from ..utils.parsers import parse_ledger_ranges
from ..utils.session_store import SessionStore

# Type hints only - these are injected via DI
if TYPE_CHECKING:
    from ..config import Config
    from ..managers.process_manager import ProcessManager
    from ..managers.state_manager import StateManager
    from ..managers.websocket_manager import WebSocketManager
    from .logging_service import LoggingService


# Number of ledger closes to wait before considering synced
LEDGER_CLOSES_FOR_SYNC = 1


class MonitoringService:
    """Orchestrates the monitoring process"""

    def __init__(
        self,
        config: "Config",
        state_manager: "StateManager",
        process_manager: "ProcessManager",
        websocket_manager: "WebSocketManager",
        logging_service: "LoggingService",
    ):
        self.config = config
        self.state_manager = state_manager
        self.process_manager = process_manager
        self.websocket_manager = websocket_manager
        self.logger = logging_service

        # Monitoring state
        self._monitoring = False
        self._shutdown_event = asyncio.Event()
        self._current_result: Optional[BinaryTestResult] = None
        self._test_start_time: Optional[datetime] = None
        self._monitoring_start_time: Optional[datetime] = None

        # Test tracking
        self.test_run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.total_txns = 0
        self.complete_ledgers = "empty"
        self.ledger_close_count = 0

        # Per-snapshot deltas and peaks (reset on each new binary)
        self._last_snapshot_rss_mb: Optional[float] = None
        self._last_snapshot_anon_mb: Optional[float] = None
        self._last_snapshot_time: Optional[datetime] = None
        # ledger_time from the previous ledgerClosed — used to compute tps
        # from the actual inter-close interval rather than wall-clock arrival
        # time (which includes websocket delivery jitter).
        self._last_ledger_close_time: Optional[int] = None
        self._peak_rss_mb: float = 0.0
        self._peak_anon_mb: float = 0.0

        # Diagnostic data tracking
        self.latest_counts: Optional[Dict[str, Any]] = None
        self.latest_job_types: Optional[List[Dict[str, Any]]] = None
        self.latest_catalogue_status: Optional[Dict[str, Any]] = None
        # Memory breakdown (refreshed on its own cadence so the potentially
        # expensive smaps/vmmap parse doesn't block the event loop every
        # ledger close). None until the first refresh task tick completes.
        self.latest_breakdown: Optional[MemoryBreakdown] = None
        # Most recent macOS heap(1) sample. Populated asynchronously when
        # --heap-every-ledger is enabled; stays None otherwise. The "sticky
        # latest" model means every snapshot after a sample carries the
        # same heap picture until a new sample lands, ~matching how
        # memory_breakdown works.
        self.latest_heap_sample: Optional[Dict[str, Any]] = None
        self._heap_sampling_in_flight: bool = False

        # Ensure the output root exists — per-binary session dirs are created
        # lazily inside SessionStore when we know pid + create_time.
        self.output_root: Path = Path(self.config.output_dir)
        self.output_root.mkdir(exist_ok=True)
        self.logger.info(f"Results root: {self.output_root}")

        # SessionStore for the current binary (opened in _open_session, closed
        # in _finalize_binary_result). None before/between binaries.
        self.session_store: Optional[SessionStore] = None
        self.session_n: int = 0
        # Stats scoped to the current *session*, not the whole binary — a
        # reattach starts fresh counts. Written into the meta.json session
        # entry on finalize.
        self._session_peak_rss_mb: float = 0.0
        self._session_total_txns: int = 0
        self._session_total_ledgers: int = 0

        # Replay hook for UI hydration — Dashboard registers this at mount.
        # Invoked once per session_open with (events, meta) so the UI can
        # seed its memory graph / counts history and compute cumulative
        # baselines (Total / Test time) before live updates resume.
        self._hydrate_callback: Optional[
            Callable[[List[Dict[str, Any]], Dict[str, Any]], Awaitable[None]]
        ] = None
        # Reset hook — fires between --reattach incarnations so widgets
        # (memory graph points, counts trend history, heap history, status
        # bar baselines) all look like a fresh launch instead of carrying
        # the dead process's state into the new pid's session.
        self._reset_callback: Optional[Callable[[], Awaitable[None]]] = None

        # Initialize system info and test config (must be non-None for BinaryTestResult)
        self.system_info: SystemInfo = self._build_system_info()
        self.test_config: TestConfiguration = self._build_test_config()

        # Register WebSocket message handler
        self.websocket_manager.add_message_handler(self._handle_websocket_message)

    def set_hydrate_callback(
        self,
        callback: Callable[[List[Dict[str, Any]], Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Register a UI hydration hook.

        Called with ``(events, meta)`` when reattaching to an existing session
        dir, before the new session's events start flowing.
        """
        self._hydrate_callback = callback

    def set_reset_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a UI reset hook fired between --reattach incarnations."""
        self._reset_callback = callback

    async def start_monitoring(self, binaries: List[str]):
        """Start monitoring all binaries"""
        self._monitoring = True

        # Update state
        await self.state_manager.update_test_progress(0, len(binaries))
        await self.state_manager.update_status("Starting tests...")

        # Test each binary
        for i, binary_path in enumerate(binaries):
            self.logger.info("binary path = " + binary_path)
            if self._shutdown_event.is_set():
                break

            binary_name = Path(binary_path).name
            self.logger.info(f"Starting test {i + 1}/{len(binaries)}: {binary_name}")

            await self.state_manager.update_test_progress(i, len(binaries))
            await self.state_manager.update_status(f"Testing {binary_name}...")

            try:
                await self._test_binary(binary_path, binary_name)
            except Exception as e:
                self.logger.error(f"Error testing {binary_name}: {e}", exc_info=True)

            # Wait between tests
            if i < len(binaries) - 1 and not self._shutdown_event.is_set():
                self.logger.info("Waiting 10 seconds before next test...")
                await asyncio.sleep(10)

        # Update final state
        await self.state_manager.update_test_progress(len(binaries), len(binaries))
        await self.state_manager.update_status("All tests completed")
        self._monitoring = False

    async def start_attach_monitoring(self, pid: int, name: str, binary_path: str):
        """Start monitoring by attaching to an existing process.

        With ``config.reattach_on_death`` set, this is an outer loop: when
        the current incarnation exits (process crash, restart, SIGTERM),
        we poll for another rippled at the same ``binary_path`` and
        attach to that one. Each incarnation opens a fresh session dir
        keyed on the new (pid, create_time) tuple — history across
        restarts lives in sibling dirs, not a single stream.
        """
        self._monitoring = True

        await self.state_manager.update_test_progress(0, 1)
        await self.state_manager.update_status(f"Attaching to {name}...")
        self.logger.info(f"Attaching to process {name} (PID: {pid})")

        original_binary_path = binary_path
        incarnation = 0
        while self._monitoring and not self._shutdown_event.is_set():
            incarnation += 1
            try:
                await self._attach_and_monitor(pid, name, binary_path)
            except Exception as e:
                self.logger.error(f"Error monitoring {name}: {e}", exc_info=True)

            if not self.config.reattach_on_death:
                break
            if self._shutdown_event.is_set():
                break

            # Wait for a matching rippled to come back. Cleanup already ran
            # in _attach_and_monitor's finally — fresh state on next cycle.
            await self.state_manager.update_status(
                f"Waiting for {Path(original_binary_path).name} to return…"
            )
            self.logger.info(
                f"Incarnation #{incarnation} ended; waiting for a matching "
                f"rippled at {original_binary_path}"
            )
            next_proc = await self._wait_for_matching_process(original_binary_path)
            if next_proc is None:
                # shutdown requested during wait
                break
            pid, name, binary_path = next_proc
            self.logger.info(f"Reattaching to PID {pid} (new session)")
            # Wipe UI state before next incarnation so the dashboard reads
            # as a cold launch — no carry-over trend arrows, graph shape,
            # or baseline clocks from the dead process.
            if self._reset_callback is not None:
                try:
                    await self._reset_callback()
                except Exception as e:
                    self.logger.warning(f"UI reset callback error: {e}")
            await self.state_manager.update_status(f"Reattaching to {name} (PID {pid})")

        await self.state_manager.update_test_progress(1, 1)
        await self.state_manager.update_status("Monitoring completed")
        self._monitoring = False

    async def _wait_for_matching_process(self, binary_path: str) -> Optional[tuple]:
        """Poll every 1s until a rippled at ``binary_path`` appears.

        Returns (pid, name, binary_path) on match, or None if shutdown
        was requested. If multiple matches exist (shouldn't happen often
        — user usually kills before restart), picks the newest by
        create_time.
        """
        from ..utils.process_discovery import find_rippled_processes

        while not self._shutdown_event.is_set():
            candidates = [p for p in find_rippled_processes() if p.binary_path == binary_path]
            if candidates:
                if len(candidates) == 1:
                    p = candidates[0]
                else:
                    # Pick newest — most likely the one the user just relaunched.
                    p = max(candidates, key=lambda c: self._get_process_create_time(c.pid))
                return (p.pid, p.name, p.binary_path)
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        return None

    async def _attach_and_monitor(self, pid: int, name: str, binary_path: str):
        """Attach to an existing process and monitor it"""
        # Initialize result tracking
        self._initialize_binary_result(binary_path, name)
        self._test_start_time = datetime.now()
        tick_task = asyncio.create_task(self._tick_timer())
        breakdown_task = asyncio.create_task(self._refresh_breakdown_periodically())

        try:
            # Attach to the process
            process = await self.process_manager.attach_to_process(pid, name, binary_path)
            await self.state_manager.update_process_info(name, process.pid)

            # Open (or reopen) the session dir keyed on (pid, create_time).
            # Hydrate runs *before* we start writing new events so the replay
            # only contains prior sessions' snapshots, not this attach's own.
            create_time = self._get_process_create_time(pid)
            await self._open_session(
                binary_path=binary_path,
                binary_name=name,
                pid=pid,
                create_time=create_time,
                mode="attach",
            )

            # Connect to WebSocket
            await self.websocket_manager.connect()

            # Subscribe to ledger events
            await self.websocket_manager.subscribe_to_streams(["ledger"])

            # In attach mode, we assume the process is already synced
            # Just do a quick check to get initial state
            server_info = await self.websocket_manager.get_server_info()
            if server_info:
                self._ingest_server_state_fields(server_info)
                complete_ledgers = server_info.get("complete_ledgers", "empty")
                self.complete_ledgers = complete_ledgers
                if complete_ledgers != "empty":
                    self.logger.info(f"Process already synced: {complete_ledgers}")
                    self._monitoring_start_time = datetime.now()
                    # Set ledger close count to 1 to skip polling
                    self.ledger_close_count = 1
                else:
                    self.logger.info("Process not yet synced, will wait for ledgers...")

            # Run polling phase (will be quick if already synced)
            await self._polling_phase()

            # Run monitoring phase
            if self._monitoring and not self._shutdown_event.is_set():
                await self._monitoring_phase()

            # Finalize results
            self._finalize_binary_result("completed")

        except Exception as e:
            self.logger.error(f"Error during {name} monitoring: {e}")
            self._finalize_binary_result("error", str(e))

        finally:
            tick_task.cancel()
            breakdown_task.cancel()
            self._close_session()

            # Cleanup (detach, don't stop)
            await self.process_manager.stop_current()
            await self.websocket_manager.unsubscribe_from_streams(["ledger"])
            await self.state_manager.update_process_info(None, None)

    def _ingest_server_state_fields(self, server_info: dict) -> None:
        """Extract ``server_state`` + ledger ages from a server_info dict.

        rippled only emits ``closed_ledger`` when there's no validated ledger
        yet (early sync / disconnected). Once it has one, ``validated_ledger``
        is present and ``closed_ledger`` is omitted — so downstream consumers
        should treat them as mutually exclusive fallbacks.
        """
        state = self.state_manager.state

        server_state = server_info.get("server_state")
        state.server_state = server_state if isinstance(server_state, str) else None

        validated = server_info.get("validated_ledger") or {}
        if isinstance(validated, dict) and validated:
            age = validated.get("age")
            state.validated_age_s = int(age) if isinstance(age, (int, float)) else None
        else:
            state.validated_age_s = None

        closed = server_info.get("closed_ledger") or {}
        if isinstance(closed, dict) and closed:
            seq = closed.get("seq")
            age = closed.get("age")
            state.closed_ledger_seq = int(seq) if isinstance(seq, (int, float)) else None
            state.closed_ledger_age_s = int(age) if isinstance(age, (int, float)) else None
        else:
            state.closed_ledger_seq = None
            state.closed_ledger_age_s = None

        uptime = server_info.get("uptime")
        state.rippled_uptime_s = int(uptime) if isinstance(uptime, (int, float)) else None

    async def stop_monitoring(self, wait_for_process: bool = True):
        """Stop monitoring.

        Pass ``wait_for_process=False`` from the quit path so the child
        gets SIGTERM and we return immediately — avoids a cleanup hang
        while python waits for the executor thread doing ``process.wait``.
        """
        self.logger.info("Stopping monitoring...")
        self._shutdown_event.set()
        await self.process_manager.stop_current(wait=wait_for_process)
        await self.websocket_manager.disconnect()

    async def _refresh_breakdown_periodically(self):
        """Refresh the memory breakdown cache off the event loop.

        smaps parsing (Linux) and vmmap (macOS) can take hundreds of ms — and
        on macOS vmmap briefly suspends the target via task_for_pid, which
        measurably slows network sync. Policy:

        - During the polling (sync) phase, skip vmmap entirely and run fast
          psutil-only aggregates every second so the dashboard fills in
          promptly while the process is warming up.
        - Once the monitoring phase begins, honor the configured interval
          (default 15s) and allow vmmap (if --vmmap not disabled) for
          per-file detail.
        - If a refresh returns supported=False (e.g. process not spawned yet,
          or pid gone), retry after 1s rather than waiting a full interval.
        """
        interval = max(1, self.config.breakdown_interval_seconds)
        try:
            while not self._shutdown_event.is_set():
                # task_for_pid stall hurts rippled during peer catchup.
                allow_vmmap = self._monitoring_start_time is not None
                try:
                    breakdown = await asyncio.to_thread(
                        self.process_manager.get_memory_breakdown,
                        5,
                        allow_vmmap,
                    )
                    self.latest_breakdown = breakdown
                    if breakdown.supported:
                        self.state_manager.state.memory_breakdown = breakdown.to_dict()
                        await self.state_manager._notify_observers()
                except Exception as e:
                    self.logger.debug(f"breakdown refresh failed: {e}")
                    breakdown = None

                # Fast tick while we don't have usable data or are still
                # syncing; full interval once we're in steady-state monitoring.
                if breakdown is None or not breakdown.supported or not allow_vmmap:
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def _tick_timer(self):
        """Push elapsed/test timing to state every second.

        Runs from process start through finalization so the dashboard's Total
        clock advances even while we're still trying to connect, before the
        polling loop kicks in.
        """
        try:
            while not self._shutdown_event.is_set() and self._test_start_time:
                live_elapsed = (datetime.now() - self._test_start_time).total_seconds()
                # Add the prior-sessions baseline so Total keeps counting
                # across reattaches. Baseline is 0 until hydrated.
                elapsed = self.state_manager.state.prior_elapsed_seconds + live_elapsed
                monitoring_elapsed: Optional[float] = None
                if self._monitoring_start_time:
                    live_monitoring = (datetime.now() - self._monitoring_start_time).total_seconds()
                    monitoring_elapsed = (
                        self.state_manager.state.prior_monitoring_seconds + live_monitoring
                    )
                await self.state_manager.update_timing(elapsed, monitoring_elapsed)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _test_binary(self, binary_path: str, binary_name: str):
        """Test a single binary"""
        # Initialize result tracking
        self._initialize_binary_result(binary_path, binary_name)
        self._test_start_time = datetime.now()
        tick_task = asyncio.create_task(self._tick_timer())
        breakdown_task = asyncio.create_task(self._refresh_breakdown_periodically())

        try:
            # Start the process
            process = await self.process_manager.start_process(binary_path, binary_name)
            await self.state_manager.update_process_info(binary_name, process.pid)

            # Open the session dir (pid is fresh, so this is a new dir unless
            # the OS recycled the pid within the same second — vanishingly
            # unlikely, but the create_time in the key makes it unambiguous).
            create_time = self._get_process_create_time(process.pid) if process.pid else 0.0
            await self._open_session(
                binary_path=binary_path,
                binary_name=binary_name,
                pid=process.pid or 0,
                create_time=create_time,
                mode="spawn",
            )

            # Connect to WebSocket
            await self.websocket_manager.connect()

            # Subscribe to ledger events
            await self.websocket_manager.subscribe_to_streams(["ledger"])

            # Run polling phase
            await self._polling_phase()

            # Run monitoring phase
            if self._monitoring and not self._shutdown_event.is_set():
                await self._monitoring_phase()

            # Finalize results
            self._finalize_binary_result("completed")

        except Exception as e:
            self.logger.error(f"Error during {binary_name} test: {e}")
            self._finalize_binary_result("error", str(e))

        finally:
            tick_task.cancel()
            breakdown_task.cancel()
            self._close_session()

            # Cleanup
            await self.process_manager.stop_current()
            await self.websocket_manager.unsubscribe_from_streams(["ledger"])
            await self.state_manager.update_process_info(None, None)

    async def _polling_phase(self):
        """Poll until ledgers are available"""
        self.logger.info("Starting polling phase...")

        consecutive_failures = 0
        max_consecutive_failures = 3

        while self._monitoring and not self._shutdown_event.is_set():
            # Check if process is still alive
            if not self.process_manager.is_process_alive():
                self.logger.error("Process crashed during polling!")
                self._finalize_binary_result("crashed", "Process crashed during polling")
                break

            # Check server info, get counts, and catalogue status in parallel
            server_info_task = self.websocket_manager.get_server_info()
            counts_task = self.websocket_manager.get_counts()
            catalogue_task = self.websocket_manager.get_catalogue_status()

            server_info, counts, catalogue_status = await asyncio.gather(
                server_info_task, counts_task, catalogue_task, return_exceptions=True
            )

            if isinstance(server_info, dict):
                self._ingest_server_state_fields(server_info)
                complete_ledgers = server_info.get("complete_ledgers", "empty")
                self.complete_ledgers = complete_ledgers  # Update tracked value

                # Extract and store job types
                if "load" in server_info and "job_types" in server_info["load"]:
                    self.latest_job_types = server_info["load"]["job_types"]
                    self.state_manager.state.job_types = self.latest_job_types
                    await self.state_manager._notify_observers()

                # Always update ledger info even during polling
                ledger_count = parse_ledger_ranges(complete_ledgers)
                await self.state_manager.update_ledger_info(
                    complete_ledgers,
                    str(server_info.get("validated_ledger", {}).get("seq", "N/A")),
                    ledger_count,
                )

                # Check if we've received enough ledger closes to consider ourselves synced
                if self.ledger_close_count >= LEDGER_CLOSES_FOR_SYNC:
                    # We're synced!
                    self.logger.info(
                        f"Received {self.ledger_close_count} ledger close(s), transitioning to monitoring phase"
                    )

                    if ledger_count > 0:
                        self.logger.info(
                            f"Ledgers available: {format_ledger_ranges(complete_ledgers)} (total: {ledger_count} ledgers)"
                        )

                    self._monitoring_start_time = datetime.now()
                    self.logger.info(
                        f"Starting {self.config.test_duration_minutes} minute monitoring period from now"
                    )

                    # Freeze the sync duration at the final value
                    final_sync_duration = (
                        self._monitoring_start_time - self._test_start_time
                    ).total_seconds()
                    self.state_manager.state.sync_duration_seconds = final_sync_duration

                    self.logger.info(f"Sync complete after {format_duration(final_sync_duration)}")

                    consecutive_failures = 0
                    break

                consecutive_failures = 0  # Reset on successful connection
            else:
                # server_info failed - real connection health signal
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(
                        f"Failed to connect to websocket {consecutive_failures} times in a row"
                    )
                    self._finalize_binary_result(
                        "error", f"Websocket connection failed {consecutive_failures} times"
                    )
                    self._monitoring = False
                    break

            # Store counts if available (optional)
            if isinstance(counts, dict):
                self.latest_counts = counts
                self.state_manager.state.counts = counts
                await self.state_manager._notify_observers()

            # Store catalogue status if available (Xahau-only — absent on upstream rippled)
            if isinstance(catalogue_status, dict):
                self.latest_catalogue_status = catalogue_status
                self.state_manager.state.catalogue_status = catalogue_status
                await self.state_manager._notify_observers()

            # Calculate elapsed time
            poll_elapsed = (
                (datetime.now() - self._test_start_time).total_seconds()
                if self._test_start_time
                else 0
            )

            # Create memory snapshot during polling
            snapshot = self._create_memory_snapshot()

            # Log memory usage during polling
            self.logger.info(
                f"No ledgers available yet, continuing to poll... (elapsed: {format_duration(poll_elapsed)})"
            )
            self.logger.info(
                f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Threads: {snapshot.num_threads}"
            )

            # Update UI state. Timing is owned by _tick_timer (which folds
            # in the prior-sessions baseline), so don't call update_timing
            # here — it would clobber the cumulative Total on a reattach.
            await self.state_manager.update_memory_stats(
                snapshot.rss_mb, snapshot.memory_percent, snapshot.num_threads
            )
            # Running sync_duration during polling — mirrors elapsed from
            # this session's start. Safe because it's only ever shown while
            # syncing; finalize writes the real value to meta.json.
            self.state_manager.state.sync_duration_seconds = poll_elapsed
            await self.state_manager._notify_observers()

            await asyncio.sleep(self.config.poll_interval)

    async def _monitoring_phase(self):
        """Monitor during the test duration"""
        self.logger.info("Starting monitoring phase...")

        # Calculate end time based on monitoring start (not test start)
        end_time = self._monitoring_start_time + timedelta(
            minutes=self.config.test_duration_minutes
        )

        # Track time for periodic updates
        last_ledger_update = datetime.now()
        ledger_update_interval = 2  # seconds

        while datetime.now() < end_time and not self._shutdown_event.is_set():
            # Check if process is still alive
            if not self.process_manager.is_process_alive():
                self.logger.error("Process crashed during monitoring!")
                self._finalize_binary_result("crashed", "Process crashed during monitoring")
                break

            # Update complete_ledgers and diagnostics periodically
            if (datetime.now() - last_ledger_update).total_seconds() >= ledger_update_interval:
                # Call server_info, get_counts, and catalogue_status in parallel
                server_info_task = self.websocket_manager.get_server_info()
                counts_task = self.websocket_manager.get_counts()
                catalogue_task = self.websocket_manager.get_catalogue_status()

                server_info, counts, catalogue_status = await asyncio.gather(
                    server_info_task, counts_task, catalogue_task, return_exceptions=True
                )

                # Handle server_info
                if isinstance(server_info, dict):
                    self._ingest_server_state_fields(server_info)
                    complete_ledgers = server_info.get("complete_ledgers", "empty")

                    # Extract and store job types
                    if "load" in server_info and "job_types" in server_info["load"]:
                        self.latest_job_types = server_info["load"]["job_types"]
                        self.state_manager.state.job_types = self.latest_job_types
                        await self.state_manager._notify_observers()

                    if complete_ledgers:
                        self.complete_ledgers = complete_ledgers  # Update tracked value
                        ledger_count = parse_ledger_ranges(complete_ledgers)
                        await self.state_manager.update_ledger_info(
                            complete_ledgers,
                            str(server_info.get("validated_ledger", {}).get("seq", "N/A")),
                            ledger_count,
                        )

                # Handle counts
                if isinstance(counts, dict):
                    self.latest_counts = counts
                    self.state_manager.state.counts = counts
                    await self.state_manager._notify_observers()

                # Handle catalogue status
                if isinstance(catalogue_status, dict):
                    self.latest_catalogue_status = catalogue_status
                    self.state_manager.state.catalogue_status = catalogue_status
                    await self.state_manager._notify_observers()

                last_ledger_update = datetime.now()

            # Timing updates come exclusively from _tick_timer (which applies
            # the cumulative baseline). Ledger-close events still fire
            # snapshots + memory stats via _handle_websocket_message.

            await asyncio.sleep(1)

    async def _handle_websocket_message(self, message: dict):
        """Handle incoming WebSocket messages"""
        if message.get("type") == "ledgerClosed":
            ledger_index = message.get("ledger_index")

            # Increment ledger close count
            self.ledger_close_count += 1

            # Capture sync start on first ledger close after monitoring starts
            # This ensures we only track ledgers we actually process
            if (
                self._monitoring_start_time
                and self.state_manager.state.sync_start_ledger is None
                and ledger_index
            ):
                self.state_manager.state.sync_start_ledger = ledger_index
                await self.state_manager._notify_observers()

                # Log with validated_ledgers info if available
                validated_ledgers = message.get("validated_ledgers", "not provided")
                self.logger.info(
                    f"Monitoring phase - tracking from ledger: {ledger_index} (validated_ledgers: {validated_ledgers})"
                )

            # Create snapshot for ledger close. ledger_time is rippled's
            # close time (seconds, rippled epoch) — the delta between
            # consecutive values is the authoritative inter-close interval.
            ledger_time = message.get("ledger_time")
            self._create_memory_snapshot(
                ledger_index=ledger_index,
                ledger_hash=message.get("ledger_hash"),
                transaction_count=message.get("txn_count", 0),
                ledger_close_time=int(ledger_time)
                if isinstance(ledger_time, (int, float))
                else None,
            )

            # Fire-and-forget heap sample on every Nth ledger once the node
            # is synced. `server_state == "full"` keeps us out of heap's way
            # during peer catchup; the in-flight gate means we never stack
            # two samples even if N is small relative to heap runtime.
            self._maybe_schedule_heap_sample(ledger_index)

    def _maybe_schedule_heap_sample(self, ledger_index: Optional[int]) -> None:
        every = self.config.heap_every_ledger
        if (
            every <= 0
            or ledger_index is None
            or ledger_index % every != 0
            or self._heap_sampling_in_flight
            or self.state_manager.state.server_state != "full"
            or not heap_supported()
        ):
            return
        proc = self.process_manager.get_current_process()
        if proc is None or proc.pid is None:
            return
        self._heap_sampling_in_flight = True
        pid = proc.pid
        # Stash raw heap(1) output next to events.jsonl so heap-trend can
        # re-parse or diff arbitrary samples without re-invoking heap on
        # a target that's long since moved on. Keyed by ledger because
        # that's how the downstream analysis rolls up.
        raw_path: Optional[Path] = None
        if self.session_store is not None:
            raw_path = self.session_store.dir / "heap_samples" / f"{ledger_index}.txt"
        asyncio.create_task(self._run_heap_sample(pid, raw_path))

    async def _run_heap_sample(self, pid: int, raw_path: Optional[Path]) -> None:
        """Run heap(1) off the event loop; publish result to state."""
        try:
            sample = await asyncio.to_thread(take_heap_sample, pid, 50, 30.0, raw_path)
            if sample.get("ok"):
                self.latest_heap_sample = sample
                self.state_manager.state.heap_sample = sample
                await self.state_manager._notify_observers()
                total_mb = (sample.get("total_bytes") or 0) / (1024 * 1024)
                lines_scanned = sample.get("lines_scanned") or 0
                lines_matched = sample.get("lines_matched") or 0
                unmatched = sample.get("unmatched_numeric") or 0
                extra = ""
                if unmatched > 0:
                    extra = f" [warn: {unmatched} numeric lines didn't match parser]"
                self.logger.info(
                    f"heap: {sample.get('row_count'):,} classes, {total_mb:,.1f} MB "
                    f"({sample.get('duration_ms')} ms, matched {lines_matched}/{lines_scanned}){extra}"
                )
            else:
                self.logger.warning(f"heap sample failed: {sample.get('error')}")
        except Exception as e:
            self.logger.error(f"heap sampling error: {e}")
        finally:
            self._heap_sampling_in_flight = False

    @staticmethod
    def _get_process_create_time(pid: int) -> float:
        """psutil create_time (epoch seconds, subsecond precision).

        Used as the second half of the session identifier. Returns 0.0 if we
        can't read it — caller still gets a usable dir name, just without
        the protection against pid-reuse collisions.
        """
        try:
            return psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    async def _open_session(
        self,
        binary_path: str,
        binary_name: str,
        pid: int,
        create_time: float,
        mode: str,
    ) -> None:
        """Resolve the session dir, hydrate prior events, write session_start.

        ``mode`` is 'spawn' or 'attach'. Attach on an existing dir means
        reattach — we replay all prior events through the hydrate callback
        before appending anything new, so the dashboard boots with the full
        history already on screen.
        """
        store = SessionStore(
            output_root=self.output_root,
            binary_name=binary_name,
            pid=pid,
            create_time=create_time,
        )

        # --fresh: shove the existing dir aside so hydration is skipped and
        # a clean store is opened. The backup preserves the old JSONL in
        # case the user wants it later.
        if self.config.fresh_session and store.dir.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = store.dir.with_name(f"{store.dir.name}.bak-{ts}")
            try:
                store.dir.rename(backup)
                self.logger.info(f"--fresh: moved prior session dir to {backup}")
            except OSError as e:
                self.logger.warning(f"--fresh: could not move {store.dir}: {e}")

        # If this (pid, create_time) has been seen before, replay prior
        # events into the UI before the new session writes more.
        if store.exists():
            try:
                events = list(store.iter_events())
                meta = store.load_meta() or {}
                if events:
                    # Service-side counters: restore from the latest snapshot
                    # so cumulative_transactions keeps counting across
                    # reattaches (otherwise _initialize_binary_result's zero
                    # would appear in the next snapshot as a regression).
                    last_snapshot: Optional[Dict[str, Any]] = None
                    for ev in events:
                        if ev.get("event") == "snapshot":
                            last_snapshot = ev
                    if last_snapshot is not None:
                        cum = last_snapshot.get("cumulative_transactions")
                        if isinstance(cum, (int, float)):
                            self.total_txns = int(cum)

                    if self._hydrate_callback is not None:
                        self.logger.info(
                            f"Hydrating UI from {len(events)} prior event(s) in {store.dir}"
                        )
                        await self._hydrate_callback(events, meta)
            except Exception as e:
                self.logger.warning(f"Hydration skipped: {e}")

        store.open_for_append()

        # Initialize meta.json on first session; subsequent sessions just
        # append their entry.
        existing_meta = store.load_meta()
        if existing_meta is None:
            binary_size_mb = 0.0
            try:
                if Path(binary_path).exists():
                    binary_size_mb = Path(binary_path).stat().st_size / (1024 * 1024)
            except OSError:
                pass
            store.save_meta(
                {
                    "binary_name": binary_name,
                    "binary_path": binary_path,
                    "binary_size_mb": binary_size_mb,
                    "pid": pid,
                    "create_time": create_time,
                    "first_seen": datetime.now().isoformat(),
                    "system_info": self.system_info.model_dump(),
                    "test_configuration": self.test_config.model_dump(),
                    "sessions": [],
                }
            )

        session_n = store.next_session_number()
        session_entry: Dict[str, Any] = {
            "n": session_n,
            "mode": mode,
            "start": datetime.now().isoformat(),
            "end": None,
            "status": "running",
            "reason": None,
            "peak_rss_mb": 0.0,
            "total_transactions": 0,
            "total_ledgers": 0,
        }
        store.append_session(session_entry)
        store.write_session_start(session_n, mode)

        self.session_store = store
        self.session_n = session_n
        self._session_peak_rss_mb = 0.0
        self._session_total_txns = 0
        self._session_total_ledgers = 0

        self.logger.info(f"Session #{session_n} ({mode}) opened at {store.dir}")

    def _close_session(self) -> None:
        """Best-effort session_end + meta summary flush.

        Intentionally does NOT compute or persist session durations: those
        are derived from the events.jsonl timestamps at hydration time, so
        they survive Ctrl+C / SIGKILL / monitor crash — anything that stops
        us before this method runs. meta.json is a convenience cache; the
        jsonl is the source of truth.
        """
        store = self.session_store
        if store is None:
            return
        try:
            status = self._current_result.status if self._current_result else "completed"
            reason = self._current_result.error_message if self._current_result else None
            if status == "running":
                status = "interrupted"
            store.write_session_end(self.session_n, status, reason)
            store.update_session(
                self.session_n,
                {
                    "end": datetime.now().isoformat(),
                    "status": status,
                    "reason": reason,
                    "peak_rss_mb": self._session_peak_rss_mb,
                    "total_transactions": self._session_total_txns,
                    "total_ledgers": self._session_total_ledgers,
                },
            )
            meta = store.load_meta() or {}
            summary = meta.setdefault("summary", {})
            prev_peak = summary.get("peak_rss_mb") or 0.0
            summary["peak_rss_mb"] = max(float(prev_peak), self._session_peak_rss_mb)
            if self._current_result and self._current_result.final_memory_rss_mb:
                summary["final_rss_mb"] = self._current_result.final_memory_rss_mb
            store.save_meta(meta)
        except Exception as e:
            self.logger.error(f"Error closing session: {e}")
        finally:
            store.close()
            self.session_store = None

    def _initialize_binary_result(self, binary_path: str, binary_name: str):
        """Initialize result tracking for current binary"""
        binary_path_obj = Path(binary_path)
        binary_size_mb = (
            binary_path_obj.stat().st_size / (1024 * 1024) if binary_path_obj.exists() else 0
        )

        self._current_result = BinaryTestResult(
            test_run_id=self.test_run_timestamp,
            binary_name=binary_name,
            binary_path=binary_path,
            binary_size_mb=binary_size_mb,
            start_time=datetime.now().isoformat(),
            end_time="",  # Will be set later
            configured_duration_seconds=self.config.test_duration_minutes * 60,
            actual_duration_seconds=0,  # Will be calculated
            status="running",
            test_configuration=self.test_config,
            system_info=self.system_info,
        )

        # Reset counters for this binary
        self.total_txns = 0
        self.complete_ledgers = "empty"
        self.ledger_close_count = 0
        self.latest_counts = None
        self.latest_job_types = None
        self.latest_catalogue_status = None
        self._last_snapshot_rss_mb = None
        self._last_snapshot_anon_mb = None
        self._last_snapshot_time = None
        self._last_ledger_close_time = None
        self._peak_rss_mb = 0.0
        self._peak_anon_mb = 0.0
        self.latest_breakdown = None
        self.latest_heap_sample = None
        self._heap_sampling_in_flight = False

        self.logger.info(f"Initialized result tracking for {binary_name}")

    def _create_memory_snapshot(
        self,
        ledger_index: Optional[int] = None,
        ledger_hash: Optional[str] = None,
        transaction_count: Optional[int] = None,
        ledger_close_time: Optional[int] = None,
    ) -> MemorySnapshot:
        """Create a memory snapshot"""
        # Calculate elapsed time
        elapsed = (
            (datetime.now() - self._test_start_time).total_seconds() if self._test_start_time else 0
        )

        # Calculate monitoring elapsed if we're in monitoring phase
        monitoring_elapsed = None
        if self._monitoring_start_time:
            monitoring_elapsed = (datetime.now() - self._monitoring_start_time).total_seconds()

        # Get memory stats
        memory_stats = self.process_manager.get_memory_stats()

        # Read the latest breakdown from the periodic refresh task (see
        # _refresh_breakdown_periodically). Calling get_memory_breakdown inline
        # would block the event loop on smaps/vmmap parsing every snapshot.
        breakdown = self.latest_breakdown
        breakdown_dict = (
            breakdown.to_dict() if breakdown is not None and breakdown.supported else None
        )

        # Update transaction count
        if transaction_count:
            self.total_txns += transaction_count
            self.ledger_close_count += 1

        # Calculate ledger count
        ledger_count = parse_ledger_ranges(self.complete_ledgers)

        # Snapshot carries the full set of values needed to repaint the
        # dashboard on reattach — anything shown in the status bar / side
        # panels that otherwise lives only in ApplicationState.
        s = self.state_manager.state
        snapshot = MemorySnapshot(
            timestamp=datetime.now().isoformat(),
            elapsed_seconds=elapsed,
            monitoring_elapsed_seconds=monitoring_elapsed,
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            transaction_count=transaction_count,
            cumulative_transactions=self.total_txns,
            rss_mb=memory_stats.get("rss", 0),
            vms_mb=memory_stats.get("vms", 0),
            memory_percent=memory_stats.get("percent", 0),
            num_threads=memory_stats.get("num_threads", 0),
            complete_ledgers=self.complete_ledgers,
            ledger_count=ledger_count,
            counts=self.latest_counts,
            job_types=self.latest_job_types,
            memory_breakdown=breakdown_dict,
            catalogue_status=self.latest_catalogue_status,
            heap_sample=self.latest_heap_sample,
            sync_start_ledger=s.sync_start_ledger,
            server_state=s.server_state,
            validated_age_s=s.validated_age_s,
            closed_ledger_seq=s.closed_ledger_seq,
            closed_ledger_age_s=s.closed_ledger_age_s,
            rippled_uptime_s=s.rippled_uptime_s,
        )

        # Persist to events.jsonl — crash-resilient source of truth. The
        # in-memory snapshot list was dropped; summaries still update below.
        if self.session_store is not None:
            try:
                self.session_store.write_snapshot(snapshot.model_dump(mode="json"))
            except Exception as e:
                self.logger.error(f"Failed to write snapshot event: {e}")

        # Track per-session peak/totals for the meta.json session entry.
        self._session_peak_rss_mb = max(self._session_peak_rss_mb, snapshot.rss_mb)
        if transaction_count:
            self._session_total_txns += transaction_count
        if ledger_index is not None:
            self._session_total_ledgers += 1

        # Update peaks and compute deltas from last snapshot (used for log)
        rss_mb = snapshot.rss_mb
        now = datetime.now()
        rss_delta_str = ""
        since_last_str = ""
        tps_str = ""
        if self._last_snapshot_rss_mb is not None:
            rss_delta = rss_mb - self._last_snapshot_rss_mb
            rss_delta_str = f" Δ{rss_delta:+.1f}"

        # Interval for tps: prefer the gap between consecutive ledger close
        # times (authoritative, no websocket jitter). Fall back to wall-clock
        # between snapshots when ledger_close_time isn't available (polling
        # snapshots, first ledger after a restart, etc).
        interval_s: Optional[float] = None
        if ledger_close_time is not None and self._last_ledger_close_time is not None:
            interval_s = float(ledger_close_time - self._last_ledger_close_time)
        elif self._last_snapshot_time is not None:
            interval_s = (now - self._last_snapshot_time).total_seconds()

        if interval_s is not None:
            since_last_str = f" +{interval_s:.1f}s"
            if transaction_count and interval_s > 0:
                tps_str = f", {transaction_count / interval_s:.1f} tps"
        self._peak_rss_mb = max(self._peak_rss_mb, rss_mb)

        # Breakdown info (Linux populates all; macOS gives anon via uss when
        # running as root, otherwise anonymous_mb is None and we skip the line)
        anon_str = ""
        if breakdown is not None and breakdown.supported and breakdown.anonymous_mb is not None:
            anon_mb = breakdown.anonymous_mb
            mmap_mb = breakdown.nodestore_mb + breakdown.other_file_mb
            self._peak_anon_mb = max(self._peak_anon_mb, anon_mb)
            anon_delta = ""
            if self._last_snapshot_anon_mb is not None:
                d = anon_mb - self._last_snapshot_anon_mb
                anon_delta = f" Δ{d:+.1f}"
            anon_str = f" | anon {anon_mb:.0f}MB{anon_delta}, mmap {mmap_mb:.0f}MB"
            self._last_snapshot_anon_mb = anon_mb

        # Log ledger close if this is from a ledger event
        if ledger_index:
            range_info = (
                f"{format_ledger_ranges(self.complete_ledgers)} ({ledger_count} ledgers)"
                if ledger_count > 0
                else self.complete_ledgers
            )
            self.logger.info(
                f"Ledger {ledger_index}: {transaction_count} txns{tps_str} "
                f"(total {self.total_txns},{since_last_str} since prev) | {range_info}"
            )
            self.logger.info(
                f"Memory: {rss_mb:.1f}MB{rss_delta_str} ({snapshot.memory_percent:.1f}%), "
                f"peak {self._peak_rss_mb:.0f}MB{anon_str} | "
                f"{snapshot.num_threads}t, test {format_duration(snapshot.monitoring_elapsed_seconds or 0)}"
            )

        # Record tracking values for next call
        self._last_snapshot_rss_mb = rss_mb
        self._last_snapshot_time = now
        if ledger_close_time is not None:
            self._last_ledger_close_time = ledger_close_time

        return snapshot

    def _finalize_binary_result(self, status: str, error_message: Optional[str] = None):
        """Finalize the binary test result"""
        if not self._current_result:
            return

        end_time = datetime.now()
        self._current_result.end_time = end_time.isoformat()
        self._current_result.status = status
        self._current_result.error_message = error_message

        # Calculate durations
        if self._test_start_time:
            self._current_result.actual_duration_seconds = (
                end_time - self._test_start_time
            ).total_seconds()

            if self._monitoring_start_time:
                self._current_result.sync_duration_seconds = (
                    self._monitoring_start_time - self._test_start_time
                ).total_seconds()
                self._current_result.monitoring_duration_seconds = (
                    end_time - self._monitoring_start_time
                ).total_seconds()
                self._current_result.sync_time = self._monitoring_start_time.isoformat()

        # Get process exit code if available
        process = self.process_manager.get_current_process()
        if process and hasattr(process, "process") and process.process:
            self._current_result.exit_code = process.process.poll()

        # Memory statistics come from in-flight tracking (the full time series
        # is in events.jsonl, not in memory). final/peak are what we've seen
        # this session; average isn't cheap without scanning JSONL, so skip.
        if self._last_snapshot_rss_mb is not None:
            self._current_result.final_memory_rss_mb = self._last_snapshot_rss_mb
        if self._peak_rss_mb > 0:
            self._current_result.peak_memory_rss_mb = self._peak_rss_mb

        # Total transactions and ledgers
        self._current_result.total_transactions = self.total_txns
        self._current_result.total_ledgers = self.ledger_close_count
        self._current_result.final_complete_ledgers = self.complete_ledgers

        # Capture process output tails
        if process and hasattr(process, "stdout_buffer"):
            self._current_result.stdout_tail = process.stdout_buffer[-100:]
            self._current_result.stderr_tail = process.stderr_buffer[-100:]

        # Log completion
        self.logger.info(f"Test completed for {self._current_result.binary_name}")
        self.logger.info(f"  Status: {status}")
        self.logger.info(
            f"  Total time: {format_duration(self._current_result.actual_duration_seconds)}"
        )
        if self._current_result.sync_duration_seconds:
            self.logger.info(
                f"  Syncing time: {format_duration(self._current_result.sync_duration_seconds)}"
            )
        if self._current_result.monitoring_duration_seconds:
            self.logger.info(
                f"  Monitoring time: {format_duration(self._current_result.monitoring_duration_seconds)}"
            )
        if self._current_result.peak_memory_rss_mb:
            self.logger.info(f"  Peak memory: {self._current_result.peak_memory_rss_mb:.1f}MB")
        self.logger.info(f"  Total transactions: {self._current_result.total_transactions}")
        self.logger.info(f"  Total ledgers: {self._current_result.total_ledgers}")

    def _build_system_info(self) -> SystemInfo:
        """Gather system information"""
        return SystemInfo(
            platform=platform.system().lower(),
            platform_version=platform.platform(),
            hostname=socket.gethostname(),
            cpu_count=psutil.cpu_count() or 0,
            total_memory_gb=psutil.virtual_memory().total / (1024**3),
            python_version=sys.version.split()[0],
        )

    def _build_test_config(self) -> TestConfiguration:
        """Initialize test configuration"""
        return TestConfiguration(
            test_duration_minutes=self.config.test_duration_minutes,
            poll_interval_seconds=self.config.poll_interval,
            websocket_url=self.config.websocket_url,
            rippled_config_path=self.config.rippled_config_path,
            script_version="1.0.0",  # TODO: Could read from git
        )
