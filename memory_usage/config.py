"""Configuration module for memory monitor."""

from dataclasses import dataclass
from typing import Optional, List


@dataclass
class Config:
    """Configuration for the memory monitor."""
    
    # Paths and URLs
    rippled_config_path: str
    websocket_url: str
    build_dir: str = "build"
    output_dir: str = "memory_monitor_results"
    
    # Runtime options
    api_version: int = 2
    standalone_mode: bool = False
    test_duration_minutes: int = 5
    poll_interval: int = 4
    
    # Binary selection
    specified_binaries: Optional[List[str]] = None
    
    def __post_init__(self):
        """Validate configuration."""
        if self.api_version not in [1, 2]:
            raise ValueError(f"Invalid API version: {self.api_version}")
        
        if self.test_duration_minutes <= 0:
            raise ValueError("Test duration must be positive")