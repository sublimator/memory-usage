"""
Process management for rippled binaries
"""

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Union

from ..config import Config
from ..services.attached_process_service import AttachedProcessService
from ..services.process_service import ProcessService
from ..utils.memory_breakdown import MemoryBreakdown, get_memory_breakdown

if TYPE_CHECKING:
    from ..services.logging_service import LoggingService

logger = logging.getLogger(__name__)

# Type alias for either process type
AnyProcessService = Union[ProcessService, AttachedProcessService]


class ProcessManager:
    """Manages rippled processes centrally"""

    def __init__(self, config: Config, logging_service: "LoggingService"):
        if logging_service is None:
            raise ValueError("logging_service is required")
        self.config = config
        self.logging_service = logging_service
        self.current_process: Optional[AnyProcessService] = None
        self._lock = asyncio.Lock()
        self._stdout_callbacks: List[Callable[[str], None]] = []
        self._stderr_callbacks: List[Callable[[str], None]] = []
        self._attach_mode = False

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

    async def start_process(self, binary_path: str, name: str) -> AnyProcessService:
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
            self._attach_mode = False

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

    async def attach_to_process(
        self, pid: int, name: str, binary_path: str
    ) -> AttachedProcessService:
        """Attach to an existing running process"""
        async with self._lock:
            # Stop any existing process (but don't stop attached processes)
            if self.current_process and self.current_process.is_alive():
                if not self._attach_mode:
                    logger.info(f"Stopping existing process {self.current_process.name}")
                    self.current_process.stop()
                else:
                    logger.info(f"Detaching from existing process {self.current_process.name}")

            # Create attached process service
            self.current_process = AttachedProcessService(
                pid, name, binary_path, self.config, self.logging_service
            )
            self._attach_mode = True

            # Hook up output callbacks (no-ops for attached processes)
            for callback in self._stdout_callbacks:
                self.current_process.add_stdout_callback(callback)
            for callback in self._stderr_callbacks:
                self.current_process.add_stderr_callback(callback)

            if self.current_process.start():
                logger.info(f"Successfully attached to {name} (PID: {pid})")
                return self.current_process
            else:
                self.current_process = None
                raise RuntimeError(f"Failed to attach to process {name} (PID: {pid})")

    async def stop_current(self):
        """Stop the current process (or detach if in attach mode).

        ProcessService.stop() blocks on subprocess.wait(timeout=10) and a
        thread join — run it in an executor so the event loop keeps turning
        and any outer asyncio.wait_for timeout can actually fire.
        """
        async with self._lock:
            if self.current_process:
                if self._attach_mode:
                    logger.info(f"Detaching from process {self.current_process.name}")
                else:
                    logger.info(f"Stopping process {self.current_process.name}")
                await asyncio.to_thread(self.current_process.stop)
                self.current_process = None
                self._attach_mode = False

    def get_current_process(self) -> Optional[AnyProcessService]:
        """Get the current process"""
        return self.current_process

    def is_attach_mode(self) -> bool:
        """Check if we're in attach mode"""
        return self._attach_mode

    def is_process_alive(self) -> bool:
        """Check if current process is alive"""
        return self.current_process is not None and self.current_process.is_alive()

    def get_memory_stats(self) -> dict:
        """Get memory stats from current process"""
        if self.current_process:
            return self.current_process.get_memory_usage()
        return {}

    def get_memory_breakdown(self, top_n: int = 5) -> MemoryBreakdown:
        """Get a per-VMA RSS breakdown for the current process (Linux only)."""
        if self.current_process and self.current_process.pid:
            return get_memory_breakdown(self.current_process.pid, top_n=top_n)
        return MemoryBreakdown(supported=False)

    def get_log_file_path(self) -> Optional[str]:
        """Get the log file path for the current process"""
        if self.current_process and self.current_process.log_file_path:
            return str(self.current_process.log_file_path)
        return None

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
