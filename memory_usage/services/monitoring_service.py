"""
Main monitoring service that orchestrates the memory monitoring process
"""

import asyncio
import json
import logging
import platform
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import psutil

from ..models.memory_models import BinaryTestResult, MemorySnapshot, SystemInfo, TestConfiguration
from ..utils.formatters import format_duration, format_ledger_ranges
from ..utils.parsers import parse_ledger_ranges

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
        self.test_output_dir = None
        self.system_info: Optional[SystemInfo] = None
        self.test_config: Optional[TestConfiguration] = None
        self.total_txns = 0
        self.complete_ledgers = "empty"
        self.ledger_close_count = 0

        # Diagnostic data tracking
        self.latest_counts: Optional[Dict[str, Any]] = None
        self.latest_job_types: Optional[List[Dict[str, Any]]] = None
        self.latest_catalogue_status: Optional[Dict[str, Any]] = None

        # Create output directory
        Path(self.config.output_dir).mkdir(exist_ok=True)
        self.test_output_dir = Path(self.config.output_dir) / self.test_run_timestamp
        self.test_output_dir.mkdir(exist_ok=True)
        self.logger.info(f"Test results will be saved to: {self.test_output_dir}")

        # Initialize system info and test config
        self._initialize_system_info()
        self._initialize_test_config()

        # Register WebSocket message handler
        self.websocket_manager.add_message_handler(self._handle_websocket_message)

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
            self.logger.info(f"Starting test {i+1}/{len(binaries)}: {binary_name}")

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

    async def stop_monitoring(self):
        """Stop monitoring"""
        self.logger.info("Stopping monitoring...")
        self._shutdown_event.set()
        await self.process_manager.stop_current()
        await self.websocket_manager.disconnect()

    async def _test_binary(self, binary_path: str, binary_name: str):
        """Test a single binary"""
        # Initialize result tracking
        self._initialize_binary_result(binary_path, binary_name)
        self._test_start_time = datetime.now()

        try:
            # Start the process
            process = await self.process_manager.start_process(binary_path, binary_name)
            await self.state_manager.update_process_info(binary_name, process.pid)

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
            # Save results
            self._save_binary_result()

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
                complete_ledgers = server_info.get("complete_ledgers", "empty")
                self.complete_ledgers = complete_ledgers  # Update tracked value

                # Extract and store job types
                if "load" in server_info and "job_types" in server_info["load"]:
                    self.latest_job_types = server_info["load"]["job_types"]
                    self.state_manager.state.job_types = self.latest_job_types
                    await self.state_manager._notify_observers()

            # Store counts if available
            if isinstance(counts, dict):
                self.latest_counts = counts
                self.state_manager.state.counts = counts
                await self.state_manager._notify_observers()

            # Store catalogue status if available
            if isinstance(catalogue_status, dict):
                self.latest_catalogue_status = catalogue_status
                self.state_manager.state.catalogue_status = catalogue_status
                await self.state_manager._notify_observers()

                # Always update ledger info even during polling
                if complete_ledgers:
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
                # No server info - connection might be failing
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

            # Update UI state
            await self.state_manager.update_memory_stats(
                snapshot.rss_mb, snapshot.memory_percent, snapshot.num_threads
            )
            # During polling, sync_duration is the same as elapsed time
            await self.state_manager.update_timing(poll_elapsed, None)
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

            # Update timing
            elapsed = (
                (datetime.now() - self._test_start_time).total_seconds()
                if self._test_start_time
                else 0
            )
            monitoring_elapsed = (
                (datetime.now() - self._monitoring_start_time).total_seconds()
                if self._monitoring_start_time
                else 0
            )
            await self.state_manager.update_timing(elapsed, monitoring_elapsed)

            # Note: ledger close events will trigger snapshots via _handle_websocket_message
            # which will also update memory stats

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

            # Create snapshot for ledger close
            self._create_memory_snapshot(
                ledger_index=ledger_index,
                ledger_hash=message.get("ledger_hash"),
                transaction_count=message.get("txn_count", 0),
            )

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

        self.logger.info(f"Initialized result tracking for {binary_name}")

    def _create_memory_snapshot(
        self,
        ledger_index: Optional[int] = None,
        ledger_hash: Optional[str] = None,
        transaction_count: Optional[int] = None,
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

        # Update transaction count
        if transaction_count:
            self.total_txns += transaction_count
            self.ledger_close_count += 1

        # Calculate ledger count
        ledger_count = parse_ledger_ranges(self.complete_ledgers)

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
            # Include diagnostic data if available
            counts=self.latest_counts,
            job_types=self.latest_job_types,
        )

        # Add to current result
        if self._current_result:
            self._current_result.snapshots.append(snapshot)

        # Log ledger close if this is from a ledger event
        if ledger_index:
            if ledger_count > 0:
                self.logger.info(
                    f"Ledger closed: {ledger_index}, txns: {transaction_count} (total txns: {self.total_txns}, ranges: {format_ledger_ranges(self.complete_ledgers)} - {ledger_count} ledgers)"
                )
            else:
                self.logger.info(
                    f"Ledger closed: {ledger_index}, txns: {transaction_count} (total txns: {self.total_txns}, ranges: {self.complete_ledgers})"
                )

            self.logger.info(
                f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Total elapsed: {format_duration(snapshot.elapsed_seconds)}, Test elapsed: {format_duration(snapshot.monitoring_elapsed_seconds or 0)}"
            )

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

        # Calculate memory statistics
        if self._current_result.snapshots:
            memory_values = [s.rss_mb for s in self._current_result.snapshots if s.rss_mb > 0]
            if memory_values:
                self._current_result.final_memory_rss_mb = memory_values[-1]
                self._current_result.peak_memory_rss_mb = max(memory_values)
                self._current_result.average_memory_rss_mb = sum(memory_values) / len(memory_values)

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

    def _save_binary_result(self):
        """Save the binary test result to JSON"""
        if not self._current_result:
            return

        output_file = self.test_output_dir / f"{self._current_result.binary_name}.json"

        try:
            with open(output_file, "w") as f:
                f.write(self._current_result.model_dump_json(indent=2))
            self.logger.info(f"Results saved to: {output_file}")
        except Exception as e:
            self.logger.error(f"Error saving results: {e}")

    def _initialize_system_info(self):
        """Gather system information"""
        self.system_info = SystemInfo(
            platform=platform.system().lower(),
            platform_version=platform.platform(),
            hostname=socket.gethostname(),
            cpu_count=psutil.cpu_count(),
            total_memory_gb=psutil.virtual_memory().total / (1024**3),
            python_version=sys.version.split()[0],
        )

    def _initialize_test_config(self):
        """Initialize test configuration"""
        self.test_config = TestConfiguration(
            test_duration_minutes=self.config.test_duration_minutes,
            poll_interval_seconds=self.config.poll_interval,
            websocket_url=self.config.websocket_url,
            rippled_config_path=self.config.rippled_config_path,
            script_version="1.0.0",  # TODO: Could read from git
        )
