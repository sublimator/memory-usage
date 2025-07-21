"""
Data models for memory monitoring
"""

from typing import Optional, List
from pydantic import BaseModel


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