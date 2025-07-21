#!/usr/bin/env python3
"""
Rippled Memory Monitor - Sequential Binary Testing

This script monitors memory usage of rippled binaries one at a time for N minutes each.
It polls every 10 seconds while complete_ledgers is "empty" and then subscribes to 
ledger close events to record ledger number, transaction count, and memory usage.
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
import csv
import signal
import sys
import argparse
import threading
import select
import configparser

try:
    from xrpl.asyncio.clients import AsyncWebsocketClient
    from xrpl.models.requests import ServerInfo, Subscribe, Unsubscribe
    from xrpl.models import Response
except ImportError as e:
    print('e', e)
    print("Please install xrpl-py: pip install xrpl-py")
    sys.exit(1)

try:
    from pydantic import BaseModel
except ImportError:
    print("Please install pydantic: pip install pydantic")
    sys.exit(1)

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

# Default Configuration
DEFAULT_RIPPLED_CONFIG_PATH = "niq-conf/xahaud.cfg"
BUILD_DIR = "build"
POLL_INTERVAL = 4  # seconds
DEFAULT_WEBSOCKET_PORT = 6009
OUTPUT_DIR = "memory_monitor_results"
DEFAULT_API_VERSION = 2  # Standard XRPL uses version 2

# These will be set later
RIPPLED_CONFIG_PATH = DEFAULT_RIPPLED_CONFIG_PATH
WEBSOCKET_URL = None
API_VERSION = DEFAULT_API_VERSION
STANDALONE_MODE = False

# Color configuration (ANSI escape codes)
COLOR_STDOUT = "\033[36m"  # Cyan
COLOR_STDERR = "\033[33m"  # Yellow
COLOR_SCRIPT = "\033[35m"  # Magenta
COLOR_RESET = "\033[0m"

# Get script name for logging
SCRIPT_NAME = Path(__file__).name

# Custom formatter to add colored script tag
class ColoredFormatter(logging.Formatter):
    def format(self, record):
        # Only add script tag if message doesn't already have a tag
        if '[' not in str(record.msg)[:50]:  # Check first 50 chars for existing tag
            record.msg = f"{COLOR_SCRIPT}[{SCRIPT_NAME}]{COLOR_RESET} {record.msg}"
        return super().format(record)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('memory_monitor.log'),
        logging.StreamHandler()
    ]
)

# Apply colored formatter only to console handler
for handler in logging.getLogger().handlers:
    if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
        handler.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s'))

logger = logging.getLogger(__name__)

def detect_xahau(config_path: str) -> bool:
    """Detect if this is a Xahau configuration based on file name or content."""
    # Check file name
    if 'xahau' in config_path.lower():
        return True
    
    # Check file content
    try:
        if Path(config_path).exists():
            with open(config_path, 'r') as f:
                content = f.read().lower()
                if 'xahau' in content:
                    return True
    except Exception:
        pass
    
    return False

def parse_rippled_config(config_path: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse rippled configuration file to extract websocket and RPC ports.
    Returns (websocket_port, rpc_port) or (None, None) if not found."""
    try:
        if not Path(config_path).exists():
            return None, None
            
        config = configparser.ConfigParser()
        # Read the file with custom parsing to handle the rippled config format
        with open(config_path, 'r') as f:
            config_str = f.read()
        
        # rippled uses a custom format, we need to parse it manually
        current_section = None
        sections = {}
        
        for line in config_str.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            # Check if this is a section header
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1]
                if current_section not in sections:
                    sections[current_section] = {}
            elif current_section and '=' in line:
                # Parse key = value
                key, value = line.split('=', 1)
                sections[current_section][key.strip()] = value.strip()
            elif current_section:
                # Single value line (like port numbers)
                if line.replace(' ', '').replace('.', '').replace('_', '').isalnum():
                    if 'values' not in sections[current_section]:
                        sections[current_section]['values'] = []
                    sections[current_section]['values'].append(line)
        
        ws_port = None
        rpc_port = None
        
        # Look for websocket admin port (port_ws_admin_local)
        if 'port_ws_admin_local' in sections:
            port_section = sections['port_ws_admin_local']
            if 'port' in port_section:
                ws_port = int(port_section['port'])
        
        # Fallback to public websocket port
        if not ws_port and 'port_ws_public' in sections:
            port_section = sections['port_ws_public']
            if 'port' in port_section:
                ws_port = int(port_section['port'])
        
        # Look for RPC admin port (port_rpc_admin_local)
        if 'port_rpc_admin_local' in sections:
            port_section = sections['port_rpc_admin_local']
            if 'port' in port_section:
                rpc_port = int(port_section['port'])
        
        # Fallback to public RPC port
        if not rpc_port and 'port_rpc_public' in sections:
            port_section = sections['port_rpc_public']
            if 'port' in port_section:
                rpc_port = int(port_section['port'])
                
        return ws_port, rpc_port
        
    except Exception as e:
        logger.warning(f"Failed to parse rippled config file: {e}")
        return None, None

def parse_ledger_ranges(complete_ledgers: str) -> int:
    """Parse complete_ledgers range set and return total count.
    
    Handles formats like:
    - "empty" -> 0
    - "100-200" -> 101
    - "100-200,300-400" -> 202
    - "100-200,300-400,500-600" -> 303
    """
    if not complete_ledgers or complete_ledgers == 'empty':
        return 0
    
    total_count = 0
    try:
        # Split by comma to get individual ranges
        ranges = complete_ledgers.split(',')
        
        for range_str in ranges:
            range_str = range_str.strip()
            if '-' in range_str:
                parts = range_str.split('-')
                if len(parts) == 2:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    # Include both start and end in count
                    total_count += (end - start + 1)
    except Exception as e:
        logger.debug(f"Error parsing ledger ranges '{complete_ledgers}': {e}")
        return 0
    
    return total_count

def format_ledger_ranges(complete_ledgers: str, max_length: int = 50) -> str:
    """Format complete_ledgers string for display.
    
    If the string is too long, shows first and last range with count.
    E.g., "100-200,300-400,...,900-1000 (5 ranges)"
    """
    if not complete_ledgers or complete_ledgers == 'empty':
        return complete_ledgers
    
    if len(complete_ledgers) <= max_length:
        return complete_ledgers
    
    # Split into ranges
    ranges = [r.strip() for r in complete_ledgers.split(',')]
    if len(ranges) <= 2:
        return complete_ledgers
    
    # Show first and last with ellipsis
    return f"{ranges[0]},...,{ranges[-1]} ({len(ranges)} ranges)"

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
    
    def __init__(self, binary_path: str, name: str):
        self.binary_path = binary_path
        self.name = name
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.stdout_buffer = []
        self.stderr_buffer = []
        self.output_thread = None
        self.stop_output_capture = threading.Event()
        
    def start(self):
        """Start the rippled process"""
        try:
            cmd = [
                self.binary_path,
                "--conf", RIPPLED_CONFIG_PATH
            ]
            
            # Add either --standalone or --net
            if STANDALONE_MODE:
                cmd.append("--standalone")
            else:
                cmd.append("--net")
            
            logger.info(f"Starting {self.name} with command: {' '.join(cmd)}")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=None,
                bufsize=1,
                universal_newlines=True
            )
            self.pid = self.process.pid
            logger.info(f"Started {self.name} with PID: {self.pid}")
            
            # Give it a moment to start
            time.sleep(2)
            
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                logger.error(f"Failed to start {self.name}: {stderr.decode()}")
                return False
                
            # Start output capture thread
            self.output_thread = threading.Thread(target=self._capture_output_thread, daemon=True)
            self.output_thread.start()
            
            return True
            
        except Exception as e:
            logger.error(f"Error starting {self.name}: {e}")
            return False
    
    def stop(self):
        """Stop the rippled process"""
        # Stop output capture thread first
        if self.output_thread:
            self.stop_output_capture.set()
            self.output_thread.join(timeout=2)
        
        if self.process:
            try:
                logger.info(f"Stopping {self.name}...")
                self.process.terminate()
                
                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=10)
                    logger.info(f"Gracefully stopped {self.name}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force killing {self.name}")
                    self.process.kill()
                    self.process.wait()
                    
            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")
    
    def is_alive(self) -> bool:
        """Check if the process is still running"""
        if not self.process:
            return False
        return self.process.poll() is None
    
    def _capture_output_thread(self):
        """Thread function to continuously capture output"""
        import select
        
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
                            logger.info(f"{COLOR_STDOUT}[{self.name} stdout]{COLOR_RESET} {line}")
                    elif stream == self.process.stderr:
                        line = stream.readline()
                        if line:
                            line = line.strip()
                            self.stderr_buffer.append(line)
                            if len(self.stderr_buffer) > 100:
                                self.stderr_buffer.pop(0)
                            logger.warning(f"{COLOR_STDERR}[{self.name} stderr]{COLOR_RESET} {line}")
                
            except Exception as e:
                if not self.stop_output_capture.is_set():
                    logger.debug(f"Error in output capture thread: {e}")
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
    
    def __init__(self, test_duration_minutes: int, binaries: Optional[List[str]] = None):
        self.test_duration = timedelta(minutes=test_duration_minutes)
        self.specified_binaries = binaries
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
        
        # Test result tracking
        self.current_result: Optional[BinaryTestResult] = None
        self.system_info: Optional[SystemInfo] = None
        self.test_config: Optional[TestConfiguration] = None
        
        # Create base output directory
        Path(OUTPUT_DIR).mkdir(exist_ok=True)
        
        # Create timestamped subdirectory for this test run
        self.test_output_dir = Path(OUTPUT_DIR) / self.test_run_timestamp
        self.test_output_dir.mkdir(exist_ok=True)
        logger.info(f"Test results will be saved to: {self.test_output_dir}")
        
        # Initialize system info and test config
        self._initialize_system_info()
        self._initialize_test_config(test_duration_minutes)
        
    def find_rippled_binaries(self) -> List[str]:
        """Find rippled binaries in the build directory or use specified list"""
        if self.specified_binaries:
            # Use specified binaries, but validate they exist
            validated_binaries = []
            for binary in self.specified_binaries:
                binary_path = Path(binary)
                
                # If it's absolute, use as-is
                if binary_path.is_absolute():
                    final_path = binary_path
                # If it contains a path separator, treat as relative
                elif '/' in binary or '\\' in binary:
                    final_path = Path.cwd() / binary_path
                # If it's just a name, look in build directory
                else:
                    final_path = Path(BUILD_DIR) / binary
                
                # Resolve the path to handle .. and .
                final_path = final_path.resolve()
                
                if final_path.exists() and final_path.is_file():
                    # Check if executable
                    if final_path.stat().st_mode & 0o111:
                        validated_binaries.append(str(final_path))
                        logger.info(f"Found specified binary: {final_path}")
                    else:
                        logger.warning(f"Binary not executable: {final_path}")
                else:
                    logger.error(f"Binary not found: {binary} (resolved to {final_path})")
            
            return validated_binaries
        
        # Original auto-discovery logic
        build_path = Path(BUILD_DIR)
        if not build_path.exists():
            logger.error(f"Build directory {BUILD_DIR} does not exist")
            return []
        
        binaries = []
        for file_path in build_path.glob("rippled-*"):
            if file_path.is_file() and file_path.stat().st_mode & 0o111:  # executable
                binaries.append(str(file_path))
        
        # Sort for consistent ordering
        binaries.sort()
        logger.info(f"Found rippled binaries: {binaries}")
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
    
    def _initialize_test_config(self, test_duration_minutes: int):
        """Initialize test configuration"""
        self.test_config = TestConfiguration(
            test_duration_minutes=test_duration_minutes,
            poll_interval_seconds=POLL_INTERVAL,
            websocket_url=WEBSOCKET_URL,
            rippled_config_path=RIPPLED_CONFIG_PATH,
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
        
        logger.info(f"Initialized result tracking for {binary_name}")
    
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
        logger.info(f"Test completed for {self.current_result.binary_name}")
        logger.info(f"  Status: {status}")
        logger.info(f"  Total time: {format_duration(self.current_result.actual_duration_seconds)}")
        if self.current_result.sync_duration_seconds:
            logger.info(f"  Syncing time: {format_duration(self.current_result.sync_duration_seconds)}")
        if self.current_result.monitoring_duration_seconds:
            logger.info(f"  Monitoring time: {format_duration(self.current_result.monitoring_duration_seconds)}")
        if self.current_result.peak_memory_rss_mb:
            logger.info(f"  Peak memory: {self.current_result.peak_memory_rss_mb:.1f}MB")
        logger.info(f"  Total transactions: {self.current_result.total_transactions}")
        logger.info(f"  Total ledgers: {self.current_result.total_ledgers}")
    
    def _save_binary_result(self):
        """Save the binary test result to JSON file"""
        if not self.current_result:
            return
        
        output_file = self.test_output_dir / f"{self.current_result.binary_name}.json"
        
        try:
            with open(output_file, 'w') as f:
                f.write(self.current_result.model_dump_json(indent=2))
            logger.info(f"Results saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving results: {e}")
    
    async def check_server_info(self) -> Tuple[bool, Optional[str]]:
        """Check server info and return (has_ledgers, complete_ledgers)"""
        try:
            if not self.client:
                return False, None
            
            server_info_request = ServerInfo(api_version=API_VERSION)
            response = await self.client.request(server_info_request)
            
            if response.is_successful():
                info = response.result.get('info', {})
                complete_ledgers = info.get('complete_ledgers', '')
                
                # Check if complete_ledgers is empty or indicates no ledgers
                has_ledgers = complete_ledgers != 'empty' and complete_ledgers != ''
                
                # Calculate ledger count if it's a range
                ledger_count = parse_ledger_ranges(complete_ledgers)
                
                if ledger_count > 0:
                    logger.info(f"Server info - complete_ledgers: {format_ledger_ranges(complete_ledgers)} (total: {ledger_count} ledgers)")
                else:
                    logger.info(f"Server info - complete_ledgers: {complete_ledgers}")
                return has_ledgers, complete_ledgers
            else:
                logger.error(f"Server info request failed: {response}")
                return False, None
                
        except Exception as e:
            logger.error(f"Error checking server info: {e}")
            return False, None
    
    async def subscribe_to_ledger(self):
        """Subscribe to ledger close events"""
        try:
            if not self.client or self.subscribed_to_ledger:
                return
            
            subscribe_request = Subscribe(streams=["ledger"], api_version=API_VERSION)
            response = await self.client.request(subscribe_request)
            
            if response.is_successful():
                self.subscribed_to_ledger = True
                logger.info("Successfully subscribed to ledger events")
            else:
                logger.error(f"Failed to subscribe to ledger: {response}")
                
        except Exception as e:
            logger.error(f"Error subscribing to ledger: {e}")
    
    async def unsubscribe_from_ledger(self):
        """Unsubscribe from ledger events"""
        try:
            if self.client and self.subscribed_to_ledger:
                unsubscribe_request = Unsubscribe(streams=["ledger"], api_version=API_VERSION)
                await self.client.request(unsubscribe_request)
                self.subscribed_to_ledger = False
                logger.info("Unsubscribed from ledger events")
        except Exception as e:
            logger.error(f"Error unsubscribing: {e}")
    
    async def handle_ledger_event(self, message: Dict):
        """Handle incoming ledger close events"""
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
                    logger.info(f"Ledger closed: {ledger_index}, txns: {txn_count} (total txns: {self.total_txns}, ranges: {format_ledger_ranges(self.complete_ledgers)} - {ledger_count} ledgers)")
                else:
                    logger.info(f"Ledger closed: {ledger_index}, txns: {txn_count} (total txns: {self.total_txns}, ranges: {self.complete_ledgers})")
                
                # Create and store snapshot
                snapshot = self._create_memory_snapshot(
                    ledger_index=ledger_index,
                    ledger_hash=ledger_hash,
                    transaction_count=txn_count
                )
                
                # Log summary
                logger.info(f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Total elapsed: {format_duration(snapshot.elapsed_seconds)}, Test elapsed: {format_duration(snapshot.monitoring_elapsed_seconds or 0)}")
                
        except Exception as e:
            logger.error(f"Error handling ledger event: {e}")
    
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
        logger.info("Starting polling phase...")
        
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while self.monitoring and not self.is_test_complete() and not shutdown_event.is_set():
            # Check if process is still alive
            if not self.current_process.is_alive():
                logger.error(f"Process {self.current_process.name} has crashed!")
                # Log last output
                if self.current_process.stderr_buffer:
                    logger.error(f"Last stderr output:")
                    for line in self.current_process.stderr_buffer[-10:]:
                        logger.error(f"  {COLOR_STDERR}[stderr]{COLOR_RESET} {line}")
                if self.current_process.stdout_buffer:
                    logger.info(f"Last stdout output:")
                    for line in self.current_process.stdout_buffer[-10:]:
                        logger.info(f"  {COLOR_STDOUT}[stdout]{COLOR_RESET} {line}")
                logger.error("Stopping all tests due to process crash")
                self.monitoring = False
                self.stop_all_tests = True
                self._finalize_binary_result("crashed", "Process crashed during polling phase")
                break
            
            # Output is being captured by the background thread
            
            # Check server info
            has_ledgers, complete_ledgers = await self.check_server_info()
            
            if has_ledgers:
                ledger_count_msg = parse_ledger_ranges(complete_ledgers)
                if ledger_count_msg > 0:
                    logger.info(f"Ledgers available: {format_ledger_ranges(complete_ledgers)} (total: {ledger_count_msg} ledgers)")
                else:
                    logger.info(f"Ledgers available: {complete_ledgers}")
                self.complete_ledgers = complete_ledgers
                # Start the monitoring timer now that we're synced
                self.monitoring_start_time = datetime.now()
                if self.current_result:
                    self.current_result.sync_time = self.monitoring_start_time.isoformat()
                logger.info(f"Starting {self.test_duration} monitoring period from now")
                consecutive_failures = 0
                break
            
            # Check if we're getting websocket errors
            if complete_ledgers is None:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(f"Failed to connect to websocket {consecutive_failures} times in a row")
                    # Check if process is still alive
                    if not self.current_process.is_alive():
                        logger.error("Process appears to have crashed")
                        logger.error("Stopping all tests due to process crash")
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
            logger.info(f"No ledgers available yet, continuing to poll... (elapsed: {format_duration(poll_elapsed)})")
            logger.info(f"Memory: {snapshot.rss_mb:.1f}MB ({snapshot.memory_percent:.1f}%) - Threads: {snapshot.num_threads}")
            
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                logger.info("Polling phase cancelled")
                break
    
    async def monitoring_phase(self):
        """Main monitoring phase with ledger subscription"""
        logger.info("Starting monitoring phase...")
        
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
                logger.error(f"Error in message handler: {e}")
        
        # Start message handler task
        message_task = asyncio.create_task(message_handler())
        
        # Monitor process health and update complete_ledgers
        last_health_check = time.time()
        last_ledger_update = time.time()
        health_check_interval = 5  # seconds
        ledger_update_interval = 2  # seconds
        
        # Wait until test is complete
        while self.monitoring and not self.is_test_complete() and not shutdown_event.is_set():
            # Periodic health check
            if time.time() - last_health_check >= health_check_interval:
                # Check if process is still alive
                if not self.current_process.is_alive():
                    logger.error(f"Process {self.current_process.name} has crashed during monitoring!")
                    # Log last output
                    if self.current_process.stderr_buffer:
                        logger.error(f"Last stderr output:")
                        for line in self.current_process.stderr_buffer[-10:]:
                            logger.error(f"  {line}")
                    if self.current_process.stdout_buffer:
                        logger.info(f"Last stdout output:")
                        for line in self.current_process.stdout_buffer[-10:]:
                            logger.info(f"  {line}")
                    logger.error("Stopping all tests due to process crash")
                    self.monitoring = False
                    self.stop_all_tests = True
                    self._finalize_binary_result("crashed", "Process crashed during monitoring phase")
                    break
                
                # Output is being captured by the background thread
                last_health_check = time.time()
            
            # Update complete_ledgers periodically
            if time.time() - last_ledger_update >= ledger_update_interval:
                try:
                    has_ledgers, complete_ledgers = await self.check_server_info()
                    if has_ledgers and complete_ledgers:
                        self.complete_ledgers = complete_ledgers
                except Exception as e:
                    logger.debug(f"Error updating complete_ledgers: {e}")
                last_ledger_update = time.time()
            
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Monitoring phase cancelled")
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
        logger.info(f"\n{'='*60}")
        logger.info(f"Starting test for {binary_name}")
        logger.info(f"Test duration: {self.test_duration}")
        logger.info(f"{'='*60}")
        
        # Initialize result tracking for this binary
        self._initialize_binary_result(binary_path, binary_name)
        
        # Create and start process
        self.current_process = RippledProcess(binary_path, binary_name)
        
        if not self.current_process.start():
            logger.error(f"Failed to start {binary_name}, skipping...")
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
                    self.client = AsyncWebsocketClient(WEBSOCKET_URL)
                    await self.client.open()
                    logger.info("Connected to rippled websocket")
                    break
                except Exception as e:
                    logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(5)
                    else:
                        logger.error("Failed to connect to websocket after all retries")
                        self._finalize_binary_result("error", "Failed to connect to websocket")
                        self._save_binary_result()
                        return
            
            self.monitoring = True
            
            # Run polling phase
            await self.polling_phase()
            
            # Run monitoring phase if we still have time and not interrupted
            if self.monitoring and not self.is_test_complete() and not shutdown_event.is_set():
                await self.monitoring_phase()
            
            # Finalize results
            self._finalize_binary_result("completed")
            
        except Exception as e:
            logger.error(f"Error during {binary_name} test: {e}")
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
        
        # Wait a bit between tests (but not if we're done)
        # This gets called by run_all_tests after the last test too, so check if we need to wait
        # Don't log the waiting message here, let run_all_tests handle it
        if not shutdown_event.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
    
    async def run_all_tests(self):
        """Run tests for all binaries sequentially"""
        binaries = self.find_rippled_binaries()
        
        if not binaries:
            if self.specified_binaries:
                logger.error("No valid binaries found from specified list")
            else:
                logger.error("No rippled binaries found")
            return
        
        logger.info(f"Found {len(binaries)} binaries to test")
        logger.info(f"Each test will run for {self.test_duration}")
        
        try:
            for i, binary in enumerate(binaries, 1):
                if self.stop_all_tests or shutdown_event.is_set():
                    if shutdown_event.is_set():
                        logger.info("\n⚠️  Stopping tests due to interrupt signal")
                        if self.current_result and self.current_result.status == "running":
                            self._finalize_binary_result("interrupted", "User interrupted")
                            self._save_binary_result()
                    else:
                        logger.error("\n❌ Stopping all tests due to previous failure")
                    break
                    
                logger.info(f"\n🔄 Starting test {i}/{len(binaries)}")
                await self.test_binary(binary)
                
                # Log waiting message only if there are more tests
                if i < len(binaries) and not self.stop_all_tests and not shutdown_event.is_set():
                    logger.info("Waiting 10 seconds before next test...")
                
            if not self.stop_all_tests and not shutdown_event.is_set():
                logger.info(f"\n✅ All tests completed!")
            logger.info(f"Results saved in: {self.test_output_dir}/")
            
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, stopping tests...")
        except Exception as e:
            logger.error(f"Error in test execution: {e}")
        finally:
            await self.cleanup_current_test()

# Global flag for graceful shutdown
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Handle interrupt signals"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

async def main():
    """Main function"""
    # Initialize the shutdown event for this event loop
    global shutdown_event
    shutdown_event = asyncio.Event()
    
    parser = argparse.ArgumentParser(description='Monitor rippled binary memory usage')
    parser.add_argument('--duration', '-d', type=int, default=5, 
                       help='Test duration in minutes for each binary (default: 5)')
    parser.add_argument('--binaries', '-b', nargs='+', 
                       help='Specific binaries to test (e.g. rippled-compact-exact rippled-normal)')
    parser.add_argument('--list', '-l', action='store_true',
                       help='List available binaries and exit')
    parser.add_argument('--config', '-c', type=str, default=DEFAULT_RIPPLED_CONFIG_PATH,
                       help=f'Path to rippled config file (default: {DEFAULT_RIPPLED_CONFIG_PATH})')
    parser.add_argument('--websocket-url', '-w', type=str,
                       help='Override websocket URL (e.g. ws://localhost:6009)')
    parser.add_argument('--api-version', '-v', type=int, choices=[1, 2],
                       help='API version to use (auto-detected if not specified)')
    parser.add_argument('--standalone', '-s', action='store_true',
                       help='Run rippled in standalone mode (mutually exclusive with --net)')
    
    args = parser.parse_args()
    
    # Set up configuration
    global RIPPLED_CONFIG_PATH, WEBSOCKET_URL, API_VERSION, STANDALONE_MODE
    RIPPLED_CONFIG_PATH = args.config
    STANDALONE_MODE = args.standalone
    
    # Determine API version
    if args.api_version:
        API_VERSION = args.api_version
        logger.info(f"Using API version {API_VERSION} from command line")
    else:
        # Auto-detect based on config
        if detect_xahau(RIPPLED_CONFIG_PATH):
            API_VERSION = 1
            logger.info(f"Detected Xahau configuration, using API version 1")
        else:
            API_VERSION = DEFAULT_API_VERSION
            logger.info(f"Using default API version {API_VERSION}")
    
    # If websocket URL is not provided, try to get it from config file
    if args.websocket_url:
        WEBSOCKET_URL = args.websocket_url
    else:
        # Parse config file to get ports
        ws_port, rpc_port = parse_rippled_config(RIPPLED_CONFIG_PATH)
        if ws_port:
            WEBSOCKET_URL = f"ws://localhost:{ws_port}"
            logger.info(f"Using websocket port {ws_port} from config file")
        else:
            # Fallback to default
            WEBSOCKET_URL = f"ws://localhost:{DEFAULT_WEBSOCKET_PORT}"
            logger.warning(f"Could not parse websocket port from config, using default: {DEFAULT_WEBSOCKET_PORT}")
    
    # List available binaries if requested
    if args.list:
        build_path = Path(BUILD_DIR)
        if build_path.exists():
            binaries = []
            for file_path in build_path.glob("rippled-*"):
                if file_path.is_file() and file_path.stat().st_mode & 0o111:
                    binaries.append(file_path.name)
            
            if binaries:
                print("Available binaries:")
                for binary in sorted(binaries):
                    print(f"  - {binary}")
            else:
                print("No rippled binaries found in build/ directory")
        else:
            print(f"Build directory {BUILD_DIR} does not exist")
        return
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if args.binaries:
        logger.info(f"Starting memory monitor with specified binaries: {args.binaries}")
    else:
        logger.info(f"Starting memory monitor with auto-discovery")
    
    logger.info(f"Configuration:")
    logger.info(f"  Config file: {RIPPLED_CONFIG_PATH}")
    logger.info(f"  Websocket URL: {WEBSOCKET_URL}")
    logger.info(f"  API version: {API_VERSION}")
    logger.info(f"  Mode: {'standalone' if STANDALONE_MODE else 'network'}")
    logger.info(f"  Test duration: {args.duration} minutes per binary")
    logger.info(f"  Build directory: {BUILD_DIR}")
    logger.info(f"  Output directory: {OUTPUT_DIR}")
    
    monitor = MemoryMonitor(args.duration, args.binaries)
    await monitor.run_all_tests()

def run():
    """Entry point for the CLI"""
    import argparse
    from .config import Config
    from .monitor_dashboard import MonitorDashboard
    
    # Parse arguments
    parser = argparse.ArgumentParser(description='Monitor rippled binary memory usage')
    parser.add_argument('--duration', '-d', type=int, default=5, 
                       help='Test duration in minutes for each binary (default: 5)')
    parser.add_argument('--binaries', '-b', nargs='+', 
                       help='Specific binaries to test (e.g. rippled-compact-exact rippled-normal)')
    parser.add_argument('--config', '-c', type=str, default=DEFAULT_RIPPLED_CONFIG_PATH,
                       help=f'Path to rippled config file (default: {DEFAULT_RIPPLED_CONFIG_PATH})')
    parser.add_argument('--websocket-url', '-w', type=str,
                       help='Override websocket URL (e.g. ws://localhost:6009)')
    parser.add_argument('--api-version', '-v', type=int, choices=[1, 2],
                       help='API version to use (auto-detected if not specified)')
    parser.add_argument('--standalone', '-s', action='store_true',
                       help='Run rippled in standalone mode (mutually exclusive with --net)')
    
    args = parser.parse_args()
    
    # Determine API version
    api_version = args.api_version
    if not api_version:
        # Auto-detect based on config
        if detect_xahau(args.config):
            api_version = 1
        else:
            api_version = DEFAULT_API_VERSION
    
    # Determine WebSocket URL
    websocket_url = args.websocket_url
    if not websocket_url:
        # Parse config file to get ports
        ws_port, rpc_port = parse_rippled_config(args.config)
        if ws_port:
            websocket_url = f"ws://localhost:{ws_port}"
        else:
            # Fallback to default
            websocket_url = f"ws://localhost:{DEFAULT_WEBSOCKET_PORT}"
    
    # Create config object
    config = Config(
        rippled_config_path=args.config,
        websocket_url=websocket_url,
        api_version=api_version,
        standalone_mode=args.standalone,
        test_duration_minutes=args.duration,
        specified_binaries=args.binaries
    )
    
    # Run the dashboard
    app = MonitorDashboard(config)
    app.run()

if __name__ == "__main__":
    run()