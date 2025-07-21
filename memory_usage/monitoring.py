#!/usr/bin/env python3
"""
Core monitoring functionality for Xahaud Memory Monitor
"""

import asyncio
import json
import logging
import psutil
import platform
import socket
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import signal
import sys
import threading
import select
from dataclasses import dataclass, field

try:
    from xrpl.asyncio.clients import AsyncWebsocketClient
    from xrpl.models.requests import ServerInfo, Subscribe, Unsubscribe
    from xrpl.models import Response
except ImportError as e:
    print('e', e)
    print("Please install xrpl-py: pip install xrpl-py")
    sys.exit(1)

from pydantic import BaseModel

from .config import Config


# Pydantic models for structured data
class MemorySnapshot(BaseModel):
    """Single point-in-time memory measurement"""
    timestamp: str  # ISO format timestamp
    elapsed_seconds: float  # Time since binary started
    monitoring_elapsed_seconds: Optional[float] = None  # Time since sync completed (None during polling)
    ledger_index: Optional[int] = None  # None during polling phase
    ledger_hash: Optional[str] = None  # None during polling phase
    transaction_count: Optional[int] = None  # Transactions in this specific ledger
    cumulative_transactions: int = 0  # Total transactions processed so far
    rss_mb: float = 0.0  # Resident Set Size in MB
    vms_mb: float = 0.0  # Virtual Memory Size in MB
    memory_percent: float = 0.0  # Percentage of system memory
    num_threads: int = 0
    complete_ledgers: str = "empty"  # e.g., "97523597-97524358" or "empty"
    ledger_count: int = 0  # Number of ledgers in range (0 if empty)


class SystemInfo(BaseModel):
    """System information at test start"""
    platform: str  # e.g., "darwin", "linux"
    platform_version: str  # e.g., "Darwin 24.5.0"
    hostname: str
    cpu_count: int
    total_memory_gb: float
    python_version: str


class TestConfiguration(BaseModel):
    """Test run configuration"""
    test_duration_minutes: int
    poll_interval_seconds: int
    websocket_url: str
    rippled_config_path: str
    script_version: str = "1.0.0"  # Could be git hash or version string


class BinaryTestResult(BaseModel):
    """Complete test result for a single binary"""
    # Metadata
    test_run_id: str  # Parent test run ID (timestamp)
    binary_name: str  # e.g., "rippled-compact-exact"
    binary_path: str  # Full path to binary
    binary_size_mb: float  # Size of the binary file
    
    # Timing
    start_time: str  # ISO format when binary started
    sync_time: Optional[str] = None  # When ledgers became available (None if never synced)
    end_time: str  # ISO format when test ended
    configured_duration_seconds: float  # What was requested
    actual_duration_seconds: float  # Total time from start to end
    sync_duration_seconds: Optional[float] = None  # Time to sync (None if never synced)
    monitoring_duration_seconds: Optional[float] = None  # Time spent in monitoring phase
    
    # Status
    status: str  # "completed", "crashed", "timeout", "interrupted", "sync_timeout"
    error_message: Optional[str] = None  # Details if status != "completed"
    exit_code: Optional[int] = None  # Process exit code if available
    
    # Results summary
    final_memory_rss_mb: Optional[float] = None
    peak_memory_rss_mb: Optional[float] = None
    average_memory_rss_mb: Optional[float] = None
    total_transactions: int = 0
    total_ledgers: int = 0
    final_complete_ledgers: str = "empty"  # Final range
    
    # Configuration and system info (denormalized for standalone files)
    test_configuration: TestConfiguration
    system_info: SystemInfo
    
    # Time series data
    snapshots: List[MemorySnapshot] = []
    
    # Process output (last N lines)
    stdout_tail: List[str] = []  # Last 100 lines
    stderr_tail: List[str] = []  # Last 100 lines


def format_duration(seconds: float) -> str:
    """Format duration in seconds to a readable format like 01m:30.5s or 01h:05m:30s"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes:02d}m:{secs:04.1f}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}h:{minutes:02d}m:{secs:02.0f}s"


class RippledProcess:
    """Represents a running rippled process"""
    
    def __init__(self, binary_path: str, name: str, config: Config):
        self.binary_path = binary_path
        self.name = name
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.stdout_buffer = []
        self.stderr_buffer = []
        self.output_thread = None
        self.stop_output_capture = threading.Event()
        self.logger = logging.getLogger(__name__)
        
    def start(self):
        """Start the rippled process"""
        try:
            cmd = [
                self.binary_path,
                "--conf", self.config.rippled_config_path
            ]
            
            # Add either --standalone or --net
            if self.config.standalone_mode:
                cmd.append("--standalone")
            else:
                cmd.append("--net")
            
            self.logger.info(f"Starting {self.name} with command: {' '.join(cmd)}")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=None,
                bufsize=1,
                universal_newlines=True
            )
            self.pid = self.process.pid
            self.logger.info(f"Started {self.name} with PID: {self.pid}")
            
            # Give it a moment to start
            time.sleep(2)
            
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.logger.error(f"Failed to start {self.name}: {stderr}")
                return False
                
            # Start output capture thread
            self.output_thread = threading.Thread(target=self._capture_output_thread, daemon=True)
            self.output_thread.start()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error starting {self.name}: {e}")
            return False
    
    def stop(self):
        """Stop the rippled process"""
        # Stop output capture thread first
        if self.output_thread:
            self.stop_output_capture.set()
            self.output_thread.join(timeout=2)
        
        if self.process:
            try:
                self.logger.info(f"Stopping {self.name}...")
                self.process.terminate()
                
                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=10)
                    self.logger.info(f"Gracefully stopped {self.name}")
                except subprocess.TimeoutExpired:
                    self.logger.warning(f"Force killing {self.name}")
                    self.process.kill()
                    self.process.wait()
                    
            except Exception as e:
                self.logger.error(f"Error stopping {self.name}: {e}")
    
    def is_alive(self) -> bool:
        """Check if the process is still running"""
        if not self.process:
            return False
        return self.process.poll() is None
    
    def _capture_output_thread(self):
        """Thread function to continuously capture output"""
        while not self.stop_output_capture.is_set() and self.process:
            try:
                # Check both stdout and stderr
                streams = []
                if self.process.stdout:
                    streams.append(self.process.stdout)
                if self.process.stderr:
                    streams.append(self.process.stderr)
                
                if not streams:
                    break
                
                # Wait for data with a timeout
                readable, _, _ = select.select(streams, [], [], 0.1)
                
                for stream in readable:
                    if stream == self.process.stdout:
                        line = stream.readline()
                        if line:
                            line = line.strip()
                            self.stdout_buffer.append(line)
                            if len(self.stdout_buffer) > 100:
                                self.stdout_buffer.pop(0)
                    elif stream == self.process.stderr:
                        line = stream.readline()
                        if line:
                            line = line.strip()
                            self.stderr_buffer.append(line)
                            if len(self.stderr_buffer) > 100:
                                self.stderr_buffer.pop(0)
                
            except Exception as e:
                if not self.stop_output_capture.is_set():
                    self.logger.debug(f"Error in output capture thread: {e}")
                break
    
    def get_memory_usage(self) -> Dict[str, float]:
        """Get memory usage statistics for the process"""
        if not self.pid:
            return {}
        
        try:
            process = psutil.Process(self.pid)
            if not process.is_running():
                return {}
            
            memory_info = process.memory_info()
            memory_percent = process.memory_percent()
            
            return {
                'rss': memory_info.rss / (1024 * 1024),  # MB
                'vms': memory_info.vms / (1024 * 1024),  # MB
                'percent': memory_percent,
                'num_threads': process.num_threads()
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {}


class MemoryMonitor:
    """Main memory monitoring class"""
    
    def __init__(self, config: Config):
        self.config = config
        self.test_duration = timedelta(minutes=config.test_duration_minutes)
        self.current_process: Optional[RippledProcess] = None
        self.client: Optional[AsyncWebsocketClient] = None
        self.monitoring = False
        self.subscribed_to_ledger = False
        self.test_start_time = None
        self.monitoring_start_time = None  # Time when monitoring (post-sync) starts
        self.test_run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.test_output_dir = None
        self.stop_all_tests = False
        self.interrupted = False
        self.total_txns = 0
        self.complete_ledgers = "empty"
        self.ledger_close_count = 0  # Count of ledgerClose events
        self.logger = logging.getLogger(__name__)
        
        # Test result tracking
        self.current_result: Optional[BinaryTestResult] = None
        self.system_info: Optional[SystemInfo] = None
        self.test_config: Optional[TestConfiguration] = None
        
        # Create base output directory
        Path(self.config.output_dir).mkdir(exist_ok=True)
        
        # Create timestamped subdirectory for this test run
        self.test_output_dir = Path(self.config.output_dir) / self.test_run_timestamp
        self.test_output_dir.mkdir(exist_ok=True)
        self.logger.info(f"Test results will be saved to: {self.test_output_dir}")
        
        # Initialize system info and test config
        self._initialize_system_info()
        self._initialize_test_config()
        
        # Shutdown event
        self.shutdown_event = asyncio.Event()
        
    def find_rippled_binaries(self) -> List[str]:
        """Find rippled binaries in the build directory or use specified list"""
        if self.config.specified_binaries:
            # Use specified binaries, but validate they exist
            validated_binaries = []
            for binary in self.config.specified_binaries:
                binary_path = Path(binary)
                
                # If it's absolute, use as-is
                if binary_path.is_absolute():
                    final_path = binary_path
                # If it contains a path separator, treat as relative
                elif '/' in binary or '\\' in binary:
                    final_path = Path.cwd() / binary_path
                # If it's just a name, look in build directory
                else:
                    final_path = Path(self.config.build_dir) / binary
                
                # Resolve the path to handle .. and .
                final_path = final_path.resolve()
                
                if final_path.exists() and final_path.is_file():
                    # Check if executable
                    if final_path.stat().st_mode & 0o111:
                        validated_binaries.append(str(final_path))
                        self.logger.info(f"Found specified binary: {final_path}")
                    else:
                        self.logger.warning(f"Binary not executable: {final_path}")
                else:
                    self.logger.error(f"Binary not found: {binary} (resolved to {final_path})")
            
            return validated_binaries
        
        # Original auto-discovery logic
        build_path = Path(self.config.build_dir)
        if not build_path.exists():
            self.logger.error(f"Build directory {self.config.build_dir} does not exist")
            return []
        
        binaries = []
        for file_path in build_path.glob("rippled-*"):
            if file_path.is_file() and file_path.stat().st_mode & 0o111:  # executable
                binaries.append(str(file_path))
        
        # Sort for consistent ordering
        binaries.sort()
        self.logger.info(f"Found rippled binaries: {binaries}")
        return binaries
    
    def _initialize_system_info(self):
        """Gather system information"""
        self.system_info = SystemInfo(
            platform=platform.system().lower(),
            platform_version=platform.platform(),
            hostname=socket.gethostname(),
            cpu_count=psutil.cpu_count(),
            total_memory_gb=psutil.virtual_memory().total / (1024**3),
            python_version=sys.version.split()[0]
        )
    
    def _initialize_test_config(self):
        """Initialize test configuration"""
        self.test_config = TestConfiguration(
            test_duration_minutes=self.config.test_duration_minutes,
            poll_interval_seconds=self.config.poll_interval,
            websocket_url=self.config.websocket_url,
            rippled_config_path=self.config.rippled_config_path,
            script_version="1.0.0"  # TODO: Could read from git
        )
    
    def _initialize_binary_result(self, binary_path: str, binary_name: str):
        """Initialize result tracking for current binary"""
        binary_path_obj = Path(binary_path)
        binary_size_mb = binary_path_obj.stat().st_size / (1024 * 1024) if binary_path_obj.exists() else 0
        
        self.current_result = BinaryTestResult(
            test_run_id=self.test_run_timestamp,
            binary_name=binary_name,
            binary_path=binary_path,
            binary_size_mb=binary_size_mb,
            start_time=datetime.now().isoformat(),
            end_time="",  # Will be set later
            configured_duration_seconds=self.test_duration.total_seconds(),
            actual_duration_seconds=0,  # Will be calculated
            status="running",
            test_configuration=self.test_config,
            system_info=self.system_info
        )
        
        self.logger.info(f"Initialized result tracking for {binary_name}")
    
    def collect_memory_data(self) -> Dict[str, any]:
        """Collect memory usage data from current process"""
        # Always use test_start_time to show time since binary started
        elapsed = (datetime.now() - self.test_start_time).total_seconds() if self.test_start_time else 0
        
        data = {
            'timestamp': datetime.now().isoformat(),
            'elapsed_seconds': elapsed
        }
        
        if self.current_process:
            memory_stats = self.current_process.get_memory_usage()
            data.update({
                'rss_mb': memory_stats.get('rss', 0),
                'vms_mb': memory_stats.get('vms', 0),
                'memory_percent': memory_stats.get('percent', 0),
                'num_threads': memory_stats.get('num_threads', 0)
            })
        
        return data
    
    def _create_memory_snapshot(self, ledger_index: Optional[int] = None, 
                               ledger_hash: Optional[str] = None,
                               transaction_count: Optional[int] = None) -> MemorySnapshot:
        """Create a memory snapshot"""
        from .cli import parse_ledger_ranges
        
        memory_data = self.collect_memory_data()
        
        # Calculate monitoring elapsed if we're in monitoring phase
        monitoring_elapsed = None
        if self.monitoring_start_time:
            monitoring_elapsed = (datetime.now() - self.monitoring_start_time).total_seconds()
        
        # Calculate ledger count
        ledger_count = parse_ledger_ranges(self.complete_ledgers)
        
        snapshot = MemorySnapshot(
            timestamp=memory_data['timestamp'],
            elapsed_seconds=memory_data['elapsed_seconds'],
            monitoring_elapsed_seconds=monitoring_elapsed,
            ledger_index=ledger_index,
            ledger_hash=ledger_hash,
            transaction_count=transaction_count,
            cumulative_transactions=self.total_txns,
            rss_mb=memory_data.get('rss_mb', 0),
            vms_mb=memory_data.get('vms_mb', 0),
            memory_percent=memory_data.get('memory_percent', 0),
            num_threads=memory_data.get('num_threads', 0),
            complete_ledgers=self.complete_ledgers,
            ledger_count=ledger_count
        )
        
        # Add to current result
        if self.current_result:
            self.current_result.snapshots.append(snapshot)
        
        return snapshot
    
    def _finalize_binary_result(self, status: str, error_message: Optional[str] = None):
        """Finalize the binary test result"""
        if not self.current_result:
            return
        
        end_time = datetime.now()
        self.current_result.end_time = end_time.isoformat()
        self.current_result.status = status
        self.current_result.error_message = error_message
        
        # Calculate durations
        if self.test_start_time:
            self.current_result.actual_duration_seconds = (end_time - self.test_start_time).total_seconds()
            
            if self.monitoring_start_time:
                self.current_result.sync_duration_seconds = (self.monitoring_start_time - self.test_start_time).total_seconds()
                self.current_result.monitoring_duration_seconds = (end_time - self.monitoring_start_time).total_seconds()
        
        # Get process exit code if available
        if self.current_process and self.current_process.process:
            self.current_result.exit_code = self.current_process.process.poll()
        
        # Calculate memory statistics
        if self.current_result.snapshots:
            memory_values = [s.rss_mb for s in self.current_result.snapshots if s.rss_mb > 0]
            if memory_values:
                self.current_result.final_memory_rss_mb = memory_values[-1]
                self.current_result.peak_memory_rss_mb = max(memory_values)
                self.current_result.average_memory_rss_mb = sum(memory_values) / len(memory_values)
        
        # Total transactions and ledgers
        self.current_result.total_transactions = self.total_txns
        self.current_result.total_ledgers = self.ledger_close_count
        
        self.current_result.final_complete_ledgers = self.complete_ledgers
        
        # Capture process output tails
        if self.current_process:
            self.current_result.stdout_tail = self.current_process.stdout_buffer[-100:]
            self.current_result.stderr_tail = self.current_process.stderr_buffer[-100:]
        
        # Log completion
        self.logger.info(f"Test completed for {self.current_result.binary_name}")
        self.logger.info(f"  Status: {status}")
        self.logger.info(f"  Total time: {format_duration(self.current_result.actual_duration_seconds)}")
        if self.current_result.sync_duration_seconds:
            self.logger.info(f"  Syncing time: {format_duration(self.current_result.sync_duration_seconds)}")
        if self.current_result.monitoring_duration_seconds:
            self.logger.info(f"  Monitoring time: {format_duration(self.current_result.monitoring_duration_seconds)}")
        if self.current_result.peak_memory_rss_mb:
            self.logger.info(f"  Peak memory: {self.current_result.peak_memory_rss_mb:.1f}MB")
        self.logger.info(f"  Total transactions: {self.current_result.total_transactions}")
        self.logger.info(f"  Total ledgers: {self.current_result.total_ledgers}")
    
    def _save_binary_result(self):
        """Save the binary test result to JSON file"""
        if not self.current_result:
            return
        
        output_file = self.test_output_dir / f"{self.current_result.binary_name}.json"
        
        try:
            with open(output_file, 'w') as f:
                f.write(self.current_result.model_dump_json(indent=2))
            self.logger.info(f"Results saved to: {output_file}")
        except Exception as e:
            self.logger.error(f"Error saving results: {e}")
    
    async def check_server_info(self) -> Tuple[bool, Optional[str]]:
        """Check server info and return (has_ledgers, complete_ledgers)"""
        from .cli import format_ledger_ranges, parse_ledger_ranges
        
        try:
            if not self.client:
                return False, None
            
            server_info_request = ServerInfo(api_version=self.config.api_version)
            response = await self.client.request(server_info_request)
            
            if response.is_successful():
                info = response.result.get('info', {})
                complete_ledgers = info.get('complete_ledgers', '')
                
                # Check if complete_ledgers is empty or indicates no ledgers
                has_ledgers = complete_ledgers != 'empty' and complete_ledgers != ''
                
                # Calculate ledger count if it's a range
                ledger_count = parse_ledger_ranges(complete_ledgers)
                
                if ledger_count > 0:
                    self.logger.info(f"Server info - complete_ledgers: {format_ledger_ranges(complete_ledgers)} (total: {ledger_count} ledgers)")
                else:
                    self.logger.info(f"Server info - complete_ledgers: {complete_ledgers}")
                return has_ledgers, complete_ledgers
            else:
                self.logger.error(f"Server info request failed: {response}")
                return False, None
                
        except Exception as e:
            self.logger.error(f"Error checking server info: {e}")
            return False, None
    
    async def subscribe_to_ledger(self):
        """Subscribe to ledger close events"""
        try:
            if not self.client or self.subscribed_to_ledger:
                return
            
            subscribe_request = Subscribe(streams=["ledger"], api_version=self.config.api_version)
            response = await self.client.request(subscribe_request)
            
            if response.is_successful():
                self.subscribed_to_ledger = True
                self.logger.info("Successfully subscribed to ledger events")
            else:
                self.logger.error(f"Failed to subscribe to ledger: {response}")
                
        except Exception as e:
            self.logger.error(f"Error subscribing to ledger: {e}")
    
    async def unsubscribe_from_ledger(self):
        """Unsubscribe from ledger events"""
        try:
            if self.client and self.subscribed_to_ledger:
                unsubscribe_request = Unsubscribe(streams=["ledger"], api_version=self.config.api_version)
                await self.client.request(unsubscribe_request)
                self.subscribed_to_ledger = False
                self.logger.info("Unsubscribed from ledger events")
        except Exception as e:
            self.logger.error(f"Error unsubscribing: {e}")
    
    async def handle_ledger_event(self, message: Dict):
        """Handle incoming ledger close events"""
        from .cli import format_ledger_ranges, parse_ledger_ranges
        
        try:
            if message.get('type') == 'ledgerClosed':
                ledger_index = message.get('ledger_index')
                ledger_hash = message.get('ledger_hash')
                txn_count = message.get('txn_count', 0)
                
                # Update total transactions and ledger count
                self.total_txns += txn_count
                self.ledger_close_count += 1
                
                # Calculate ledger count from range
                ledger_count = parse_ledger_ranges(self.complete_ledgers)
                
                if ledger_count > 0:
                    self.logger.info(f"Ledger closed: {ledger_index}, txns: {txn_count} (total txns: {self.total_txns}, ranges: {format_ledger_ranges(self.complete_ledgers)} - {ledger_count} ledgers)")
                else:
                    self.logger.info(f"Ledger closed: {ledger_index}, txns: {txn_count} (total txns: {self.total_txns}, ranges: {self.complete_ledgers})")
                
                # Create and store snapshot
                snapshot = self._create_memory_snapshot(
                    ledger_index=ledger_index,
                    ledger_hash=ledger_hash,
                    transaction_count=txn_count
                )
                
                # Log summary
                self.logger.info(f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Total elapsed: {format_duration(snapshot.elapsed_seconds)}, Test elapsed: {format_duration(snapshot.monitoring_elapsed_seconds or 0)}")
                
        except Exception as e:
            self.logger.error(f"Error handling ledger event: {e}")
    
    def is_test_complete(self) -> bool:
        """Check if the current test duration has elapsed"""
        # During polling phase, we don't count against test duration
        if not self.monitoring_start_time:
            return False
        
        # Only count time after syncing (monitoring phase)
        elapsed = datetime.now() - self.monitoring_start_time
        return elapsed >= self.test_duration
    
    async def polling_phase(self):
        """Poll server info every 10 seconds until ledgers are available"""
        from .cli import parse_ledger_ranges, format_ledger_ranges
        
        self.logger.info("Starting polling phase...")
        
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while self.monitoring and not self.is_test_complete() and not self.shutdown_event.is_set():
            # Check if process is still alive
            if not self.current_process.is_alive():
                self.logger.error(f"Process {self.current_process.name} has crashed!")
                # Log last output
                if self.current_process.stderr_buffer:
                    self.logger.error(f"Last stderr output:")
                    for line in self.current_process.stderr_buffer[-10:]:
                        self.logger.error(f"  [stderr] {line}")
                if self.current_process.stdout_buffer:
                    self.logger.info(f"Last stdout output:")
                    for line in self.current_process.stdout_buffer[-10:]:
                        self.logger.info(f"  [stdout] {line}")
                self.logger.error("Stopping all tests due to process crash")
                self.monitoring = False
                self.stop_all_tests = True
                self._finalize_binary_result("crashed", "Process crashed during polling phase")
                break
            
            # Check server info
            has_ledgers, complete_ledgers = await self.check_server_info()
            
            if has_ledgers:
                ledger_count_msg = parse_ledger_ranges(complete_ledgers)
                if ledger_count_msg > 0:
                    self.logger.info(f"Ledgers available: {format_ledger_ranges(complete_ledgers)} (total: {ledger_count_msg} ledgers)")
                else:
                    self.logger.info(f"Ledgers available: {complete_ledgers}")
                self.complete_ledgers = complete_ledgers
                # Start the monitoring timer now that we're synced
                self.monitoring_start_time = datetime.now()
                if self.current_result:
                    self.current_result.sync_time = self.monitoring_start_time.isoformat()
                self.logger.info(f"Starting {self.test_duration} monitoring period from now")
                consecutive_failures = 0
                break
            
            # Check if we're getting websocket errors
            if complete_ledgers is None:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(f"Failed to connect to websocket {consecutive_failures} times in a row")
                    # Check if process is still alive
                    if not self.current_process.is_alive():
                        self.logger.error("Process appears to have crashed")
                        self.logger.error("Stopping all tests due to process crash")
                        self.stop_all_tests = True
                        self._finalize_binary_result("crashed", "Process crashed - websocket connection failed")
                    else:
                        self._finalize_binary_result("error", f"Websocket connection failed {consecutive_failures} times")
                    self.monitoring = False
                    break
            else:
                consecutive_failures = 0
            
            poll_elapsed = (datetime.now() - self.test_start_time).total_seconds() if self.test_start_time else 0
            
            # Create snapshot during polling
            snapshot = self._create_memory_snapshot()
            
            # Log memory usage during polling
            self.logger.info(f"No ledgers available yet, continuing to poll... (elapsed: {format_duration(poll_elapsed)})")
            self.logger.info(f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Threads: {snapshot.num_threads}")
            
            try:
                await asyncio.sleep(self.config.poll_interval)
            except asyncio.CancelledError:
                self.logger.info("Polling phase cancelled")
                break
    
    async def monitoring_phase(self):
        """Main monitoring phase with ledger subscription"""
        self.logger.info("Starting monitoring phase...")
        
        # Subscribe to ledger events
        await self.subscribe_to_ledger()
        
        # Create a task to listen for messages
        async def message_handler():
            try:
                async for message in self.client:
                    if not self.monitoring or self.is_test_complete():
                        break
                    
                    if isinstance(message, dict):
                        await self.handle_ledger_event(message)
                        
            except Exception as e:
                self.logger.error(f"Error in message handler: {e}")
        
        # Start message handler task
        message_task = asyncio.create_task(message_handler())
        
        # Monitor process health and update complete_ledgers
        last_health_check = time.time()
        last_ledger_update = time.time()
        health_check_interval = 5  # seconds
        ledger_update_interval = 2  # seconds
        
        # Wait until test is complete
        while self.monitoring and not self.is_test_complete() and not self.shutdown_event.is_set():
            # Periodic health check
            if time.time() - last_health_check >= health_check_interval:
                # Check if process is still alive
                if not self.current_process.is_alive():
                    self.logger.error(f"Process {self.current_process.name} has crashed during monitoring!")
                    # Log last output
                    if self.current_process.stderr_buffer:
                        self.logger.error(f"Last stderr output:")
                        for line in self.current_process.stderr_buffer[-10:]:
                            self.logger.error(f"  {line}")
                    if self.current_process.stdout_buffer:
                        self.logger.info(f"Last stdout output:")
                        for line in self.current_process.stdout_buffer[-10:]:
                            self.logger.info(f"  {line}")
                    self.logger.error("Stopping all tests due to process crash")
                    self.monitoring = False
                    self.stop_all_tests = True
                    self._finalize_binary_result("crashed", "Process crashed during monitoring phase")
                    break
                
                last_health_check = time.time()
            
            # Update complete_ledgers periodically
            if time.time() - last_ledger_update >= ledger_update_interval:
                try:
                    has_ledgers, complete_ledgers = await self.check_server_info()
                    if has_ledgers and complete_ledgers:
                        self.complete_ledgers = complete_ledgers
                except Exception as e:
                    self.logger.debug(f"Error updating complete_ledgers: {e}")
                last_ledger_update = time.time()
            
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.logger.info("Monitoring phase cancelled")
                break
        
        # Cancel message handler
        message_task.cancel()
        try:
            await message_task
        except asyncio.CancelledError:
            pass
    
    async def test_binary(self, binary_path: str):
        """Test a single binary for the specified duration"""
        binary_name = Path(binary_path).name
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Starting test for {binary_name}")
        self.logger.info(f"Test duration: {self.test_duration}")
        self.logger.info(f"{'='*60}")
        
        # Initialize result tracking for this binary
        self._initialize_binary_result(binary_path, binary_name)
        
        # Create and start process
        self.current_process = RippledProcess(binary_path, binary_name, self.config)
        
        if not self.current_process.start():
            self.logger.error(f"Failed to start {binary_name}, skipping...")
            self._finalize_binary_result("error", "Failed to start process")
            self._save_binary_result()
            return
        
        # Record test start time
        self.test_start_time = datetime.now()
        
        try:
            # Connect to websocket (with retry)
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    self.client = AsyncWebsocketClient(self.config.websocket_url)
                    await self.client.open()
                    self.logger.info("Connected to rippled websocket")
                    break
                except Exception as e:
                    self.logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(5)
                    else:
                        self.logger.error("Failed to connect to websocket after all retries")
                        self._finalize_binary_result("error", "Failed to connect to websocket")
                        self._save_binary_result()
                        return
            
            self.monitoring = True
            
            # Run polling phase
            await self.polling_phase()
            
            # Run monitoring phase if we still have time and not interrupted
            if self.monitoring and not self.is_test_complete() and not self.shutdown_event.is_set():
                await self.monitoring_phase()
            
            # Finalize results
            self._finalize_binary_result("completed")
            
        except Exception as e:
            self.logger.error(f"Error during {binary_name} test: {e}")
            self._finalize_binary_result("error", str(e))
        finally:
            # Save results before cleanup
            self._save_binary_result()
            # Cleanup for this binary
            await self.cleanup_current_test()
    
    async def cleanup_current_test(self):
        """Cleanup resources for current test"""
        self.monitoring = False
        
        # Unsubscribe from ledger events
        await self.unsubscribe_from_ledger()
        
        # Close websocket connection
        if self.client:
            await self.client.close()
            self.client = None
        
        # Reset result tracking
        self.current_result = None
        
        # Stop current process
        if self.current_process:
            self.current_process.stop()
            self.current_process = None
        
        # Reset state
        self.subscribed_to_ledger = False
        self.test_start_time = None
        self.monitoring_start_time = None
        self.total_txns = 0
        self.complete_ledgers = "empty"
        self.ledger_close_count = 0
        
        # Wait a bit between tests
        if not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
    
    async def run_all_tests(self):
        """Run tests for all binaries sequentially"""
        binaries = self.find_rippled_binaries()
        
        if not binaries:
            if self.config.specified_binaries:
                self.logger.error("No valid binaries found from specified list")
            else:
                self.logger.error("No rippled binaries found")
            return
        
        self.logger.info(f"Found {len(binaries)} binaries to test")
        self.logger.info(f"Each test will run for {self.test_duration}")
        
        try:
            for i, binary in enumerate(binaries, 1):
                if self.stop_all_tests or self.shutdown_event.is_set():
                    if self.shutdown_event.is_set():
                        self.logger.info("\n⚠️  Stopping tests due to interrupt signal")
                        if self.current_result and self.current_result.status == "running":
                            self._finalize_binary_result("interrupted", "User interrupted")
                            self._save_binary_result()
                    else:
                        self.logger.error("\n❌ Stopping all tests due to previous failure")
                    break
                    
                self.logger.info(f"\n🔄 Starting test {i}/{len(binaries)}")
                await self.test_binary(binary)
                
                # Log waiting message only if there are more tests
                if i < len(binaries) and not self.stop_all_tests and not self.shutdown_event.is_set():
                    self.logger.info("Waiting 10 seconds before next test...")
                
            if not self.stop_all_tests and not self.shutdown_event.is_set():
                self.logger.info(f"\n✅ All tests completed!")
            self.logger.info(f"Results saved in: {self.test_output_dir}/")
            
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal, stopping tests...")
        except Exception as e:
            self.logger.error(f"Error in test execution: {e}")
        finally:
            await self.cleanup_current_test()