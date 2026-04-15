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
