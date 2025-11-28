"""
Attached process service for monitoring an existing running process
"""

import logging
from typing import TYPE_CHECKING, Optional

import psutil

from ..config import Config

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .logging_service import LoggingService


class AttachedProcessService:
    """Service for monitoring an already-running process (no spawn/stop)"""

    def __init__(
        self,
        pid: int,
        name: str,
        binary_path: str,
        config: Config,
        logging_service: "LoggingService",
    ):
        if logging_service is None:
            raise ValueError("logging_service is required")
        self.pid = pid
        self.name = name
        self.binary_path = binary_path
        self.config = config
        self.logging_service = logging_service
        self._psutil_process: Optional[psutil.Process] = None

        # Output buffers (empty for attached processes - can't capture stdout/stderr)
        self.stdout_buffer = []
        self.stderr_buffer = []

        # Log file path (not applicable for attached processes)
        self.log_file_path = None

    def start(self) -> bool:
        """Verify the process exists and start monitoring"""
        try:
            self._psutil_process = psutil.Process(self.pid)
            if not self._psutil_process.is_running():
                self.logging_service.error(f"Process {self.pid} is not running")
                return False

            self.logging_service.info(f"Attached to process: {self.name} (PID: {self.pid})")
            self.logging_service.info(f"Binary: {self.binary_path}")

            # Log memory usage at attach time
            mem = self.get_memory_usage()
            if mem:
                self.logging_service.info(
                    f"Current memory: {mem.get('rss', 0):.1f} MB ({mem.get('percent', 0):.1f}%)"
                )

            return True

        except psutil.NoSuchProcess:
            self.logging_service.error(f"Process {self.pid} does not exist")
            return False
        except psutil.AccessDenied:
            self.logging_service.error(f"Access denied to process {self.pid}")
            return False
        except Exception as e:
            self.logging_service.error(f"Error attaching to process: {e}")
            return False

    def stop(self):
        """Detach from the process (does NOT stop the actual process)"""
        self.logging_service.info(
            f"Detaching from process {self.pid} (process will continue running)"
        )
        self._psutil_process = None

    def is_alive(self) -> bool:
        """Check if the process is still running"""
        if not self._psutil_process:
            try:
                self._psutil_process = psutil.Process(self.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False

        try:
            return self._psutil_process.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def get_memory_usage(self) -> dict:
        """Get memory usage statistics for the process"""
        if not self._psutil_process:
            try:
                self._psutil_process = psutil.Process(self.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return {}

        try:
            if not self._psutil_process.is_running():
                return {}

            memory_info = self._psutil_process.memory_info()
            memory_percent = self._psutil_process.memory_percent()

            return {
                "rss": memory_info.rss / (1024 * 1024),  # MB
                "vms": memory_info.vms / (1024 * 1024),  # MB
                "percent": memory_percent,
                "num_threads": self._psutil_process.num_threads(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {}

    def add_stdout_callback(self, callback):
        """No-op for attached processes (can't capture stdout)"""
        pass

    def add_stderr_callback(self, callback):
        """No-op for attached processes (can't capture stderr)"""
        pass
