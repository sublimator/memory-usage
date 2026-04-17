"""
Data models for memory monitoring
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class MemorySnapshot(BaseModel):
    """Single point-in-time memory measurement"""

    timestamp: str  # ISO format timestamp
    elapsed_seconds: float  # Time since binary started
    monitoring_elapsed_seconds: Optional[float] = (
        None  # Time since sync completed (None during polling)
    )
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

    # Diagnostic data captured at snapshot time
    counts: Optional[Dict[str, Any]] = None  # get_counts response
    job_types: Optional[List[Dict[str, Any]]] = None  # server_info job_types
    # Per-VMA RSS breakdown (Linux only; None elsewhere)
    memory_breakdown: Optional[Dict[str, Any]] = None
    catalogue_status: Optional[Dict[str, Any]] = None  # Xahau-only

    # macOS heap(1) sample (class histogram), populated on every N-th ledger
    # when --heap-every-ledger is enabled. Carried in every subsequent
    # snapshot until the next sample lands so the UI + jsonl consumers
    # always see the most recent heap picture.
    heap_sample: Optional[Dict[str, Any]] = None

    # Fields folded in from server_info so the dashboard can repaint the
    # status bar immediately on reattach — otherwise they stay blank until
    # the next periodic RPC fires (~2-3s).
    sync_start_ledger: Optional[int] = None
    server_state: Optional[str] = None  # "syncing", "full", "disconnected", ...
    validated_age_s: Optional[int] = None
    closed_ledger_seq: Optional[int] = None
    closed_ledger_age_s: Optional[int] = None
    rippled_uptime_s: Optional[int] = None


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

    # Time series data now lives in events.jsonl (SessionStore). Summary stats
    # above are tracked in-memory and written into meta.json.

    # Process output (last N lines)
    stdout_tail: List[str] = []  # Last 100 lines
    stderr_tail: List[str] = []  # Last 100 lines
