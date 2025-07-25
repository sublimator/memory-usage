"""
Process management for rippled binaries
"""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from ..config import Config
from ..services.process_service import ProcessService

if TYPE_CHECKING:
    from ..services.logging_service import LoggingService

logger = logging.getLogger(__name__)


class ProcessManager:
    """Manages rippled processes centrally"""

    def __init__(self, config: Config, logging_service: "LoggingService"):
        if logging_service is None:
            raise ValueError("logging_service is required")
        self.config = config
        self.logging_service = logging_service
        self.current_process: Optional[ProcessService] = None
        self._lock = asyncio.Lock()
        self._stdout_callbacks: List[Callable[[str], None]] = []
        self._stderr_callbacks: List[Callable[[str], None]] = []

    def subscribe_stdout(self, callback: Callable[[str], None]):
        """Subscribe to stdout output"""
        self._stdout_callbacks.append(callback)
        # If process already running, add to it
        if self.current_process:
            self.current_process.add_stdout_callback(callback)

    def subscribe_stderr(self, callback: Callable[[str], None]):
        """Subscribe to stderr output"""
        self._stderr_callbacks.append(callback)
        # If process already running, add to it
        if self.current_process:
            self.current_process.add_stderr_callback(callback)

    async def start_process(self, binary_path: str, name: str) -> ProcessService:
        """Start a new process"""
        async with self._lock:
            # Stop any existing process
            if self.current_process and self.current_process.is_alive():
                logger.info(f"Stopping existing process {self.current_process.name}")
                self.current_process.stop()

            # Create and start new process
            self.current_process = ProcessService(
                binary_path, name, self.config, self.logging_service
            )

            # Hook up output callbacks
            for callback in self._stdout_callbacks:
                self.current_process.add_stdout_callback(callback)
            for callback in self._stderr_callbacks:
                self.current_process.add_stderr_callback(callback)

            if self.current_process.start():
                logger.info(f"Successfully started {name} (PID: {self.current_process.pid})")
                return self.current_process
            else:
                self.current_process = None
                raise RuntimeError(f"Failed to start process {name}")

    async def stop_current(self):
        """Stop the current process"""
        async with self._lock:
            if self.current_process:
                logger.info(f"Stopping process {self.current_process.name}")
                self.current_process.stop()
                self.current_process = None

    def get_current_process(self) -> Optional[ProcessService]:
        """Get the current process"""
        return self.current_process

    def is_process_alive(self) -> bool:
        """Check if current process is alive"""
        return self.current_process is not None and self.current_process.is_alive()

    def get_memory_stats(self) -> dict:
        """Get memory stats from current process"""
        if self.current_process:
            return self.current_process.get_memory_usage()
        return {}

    def find_binaries(self) -> List[str]:
        """Find available rippled binaries"""
        if self.config.specified_binaries:
            # Use specified binaries, but validate they exist
            validated_binaries = []
            for binary in self.config.specified_binaries:
                binary_path = Path(binary)

                # If it's absolute, use as-is
                if binary_path.is_absolute():
                    final_path = binary_path
                # If it contains a path separator, treat as relative
                elif "/" in binary or "\\" in binary:
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
                        logger.info(f"Found specified binary: {final_path}")
                    else:
                        logger.warning(f"Binary not executable: {final_path}")
                else:
                    logger.error(f"Binary not found: {binary} (resolved to {final_path})")

            return validated_binaries

        # Original auto-discovery logic
        build_path = Path(self.config.build_dir)
        if not build_path.exists():
            logger.error(f"Build directory {self.config.build_dir} does not exist")
            return []

        binaries = []
        for file_path in build_path.glob("rippled-*"):
            if file_path.is_file() and file_path.stat().st_mode & 0o111:  # executable
                binaries.append(str(file_path))

        # Sort for consistent ordering
        binaries.sort()
        logger.info(f"Found rippled binaries: {binaries}")
        return binaries
