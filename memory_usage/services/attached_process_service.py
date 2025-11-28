"""
Attached process service for monitoring an existing running process
"""

import logging
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import psutil

# Regex to strip ANSI escape sequences
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

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

        # Output buffers (we'll populate from debug log file if available)
        self.stdout_buffer: List[str] = []
        self.stderr_buffer: List[str] = []

        # Log file path - use the debug logfile from the rippled config
        self.log_file_path = config.attach_debug_logfile
        self._debug_log_path = config.attach_debug_logfile

        # Log file tailing
        self._stdout_callbacks: List[Callable[[str], None]] = []
        self._stderr_callbacks: List[Callable[[str], None]] = []
        self._log_tail_thread: Optional[threading.Thread] = None
        self._stop_tailing = threading.Event()

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

            # Start tailing debug log file if available
            if self._debug_log_path:
                self._start_log_tailing()

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

    def _start_log_tailing(self):
        """Start a background thread to tail the debug log file"""
        if not self._debug_log_path:
            return

        log_path = Path(self._debug_log_path)
        if not log_path.exists():
            self.logging_service.warning(f"Debug log file not found: {self._debug_log_path}")
            return

        self.logging_service.info(f"Tailing debug log: {self._debug_log_path}")

        self._stop_tailing.clear()
        self._log_tail_thread = threading.Thread(
            target=self._tail_log_file,
            args=(log_path,),
            daemon=True,
            name="debug-log-tail",
        )
        self._log_tail_thread.start()

    def _tail_log_file(self, log_path: Path):
        """Background thread to tail the log file"""
        try:
            with open(log_path, "r") as f:
                # First, show last N lines of history (like tail -f)
                # Read entire file and get last 50 lines
                f.seek(0, os.SEEK_END)
                file_size = f.tell()

                # Read last chunk to find recent lines
                chunk_size = min(file_size, 64 * 1024)  # 64KB max
                f.seek(max(0, file_size - chunk_size))

                # Read the chunk and split into lines
                chunk = f.read()
                lines = chunk.splitlines()

                # Show last 50 lines as history
                history_lines = lines[-50:] if len(lines) > 50 else lines
                for line in history_lines:
                    # Strip ANSI escape codes
                    line = ANSI_ESCAPE_RE.sub("", line)
                    for callback in self._stdout_callbacks:
                        try:
                            callback(line)
                        except Exception as e:
                            logger.debug(f"Callback error: {e}")
                    self.stdout_buffer.append(line)

                # Now tail for new content
                while not self._stop_tailing.is_set():
                    line = f.readline()
                    if line:
                        line = line.rstrip("\n\r")
                        # Strip ANSI escape codes
                        line = ANSI_ESCAPE_RE.sub("", line)
                        # Send to stdout callbacks (debug log is effectively stdout)
                        for callback in self._stdout_callbacks:
                            try:
                                callback(line)
                            except Exception as e:
                                logger.debug(f"Callback error: {e}")

                        # Also buffer it
                        self.stdout_buffer.append(line)
                        # Keep buffer limited
                        if len(self.stdout_buffer) > 1000:
                            self.stdout_buffer = self.stdout_buffer[-500:]
                    else:
                        # No new content, wait a bit
                        self._stop_tailing.wait(0.1)

        except Exception as e:
            self.logging_service.error(f"Error tailing log file: {e}")

    def stop(self):
        """Detach from the process (does NOT stop the actual process)"""
        # Stop log tailing
        self._stop_tailing.set()
        if self._log_tail_thread and self._log_tail_thread.is_alive():
            self._log_tail_thread.join(timeout=1.0)

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

    def add_stdout_callback(self, callback: Callable[[str], None]):
        """Register a callback for stdout (debug log) output"""
        self._stdout_callbacks.append(callback)

    def add_stderr_callback(self, callback: Callable[[str], None]):
        """Register a callback for stderr output (not used for attached processes)"""
        self._stderr_callbacks.append(callback)
