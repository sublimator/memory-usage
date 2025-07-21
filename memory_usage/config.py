"""Configuration module for memory monitor."""

from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator


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
    
    # Binary selection
    specified_binaries: Optional[List[str]] = Field(default=None)
    
    @field_validator('api_version')
    def validate_api_version(cls, v):
        """Validate API version."""
        if v not in [1, 2]:
            raise ValueError(f"Invalid API version: {v}")
        return v
    
    @field_validator('test_duration_minutes')
    def validate_test_duration(cls, v):
        """Validate test duration."""
        if v <= 0:
            raise ValueError("Test duration must be positive")
        return v
    
    class Config:
        # Allow creation from kwargs
        extra = "forbid"