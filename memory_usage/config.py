"""Configuration module for memory monitor."""

from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """Configuration for the memory monitor."""

    # Paths and URLs
    rippled_config_path: str
    websocket_url: str
    build_dir: str = Field(default="build")
    output_dir: str = Field(default="memory_monitor_results")

    # Runtime options
    api_version: int = Field(default=2)
    standalone_mode: bool = Field(default=False)
    test_duration_minutes: int = Field(default=5)
    poll_interval: int = Field(default=4)

    # Websocket retry tuning (0 max_retries = retry forever)
    websocket_max_retries: int = Field(default=5)
    websocket_retry_delay_seconds: int = Field(default=5)

    # Memory breakdown tuning. Disable vmmap on macOS to skip the subprocess
    # call (useful when diagnosing hangs or when the target process holds a
    # task_for_pid lock). breakdown_interval is how often the full breakdown
    # (smaps / vmmap) is refreshed — aggregate rss still updates every second.
    use_vmmap: bool = Field(default=True)
    breakdown_interval_seconds: int = Field(default=15)

    # When True, any existing session dir for this (pid, create_time) is
    # moved aside to <name>.bak-<ts> on open so hydration is skipped and a
    # clean events.jsonl/meta.json is written. Useful when the prior meta
    # is stale/truncated and you want a clean slate without rm -rf.
    fresh_session: bool = Field(default=False)

    # macOS heap(1) sampling cadence. 0 = off (default). When > 0, after
    # rippled reaches server_state=="full" we run heap on every Nth ledger
    # close and attach the top-50 class histogram to subsequent snapshots.
    # Gated heavily because heap suspends the target via task_for_pid and
    # takes seconds on a fat process.
    heap_every_ledger: int = Field(default=0)

    # Attach-only: when set, detach cleanly on process death and poll for
    # a new rippled with the same binary_path. Each new incarnation gets
    # its own (pid, create_time) session dir — history across restarts
    # lives in sibling dirs, not a single stream.
    reattach_on_death: bool = Field(default=False)

    # Binary selection
    specified_binaries: Optional[List[str]] = Field(default=None)

    # Attach mode (connect to running process)
    attach_mode: bool = Field(default=False)
    attach_pid: Optional[int] = Field(default=None)
    attach_binary_name: Optional[str] = Field(default=None)
    attach_binary_path: Optional[str] = Field(default=None)
    attach_debug_logfile: Optional[str] = Field(default=None)

    @field_validator("api_version")
    def validate_api_version(cls, v):
        """Validate API version."""
        if v not in [1, 2]:
            raise ValueError(f"Invalid API version: {v}")
        return v

    @field_validator("test_duration_minutes")
    def validate_test_duration(cls, v):
        """Validate test duration."""
        if v <= 0:
            raise ValueError("Test duration must be positive")
        return v

    class Config:
        # Allow creation from kwargs
        extra = "forbid"
