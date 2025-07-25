"""
Process service that handles rippled process lifecycle with output callbacks
"""

import logging
import os
import select
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import psutil

from ..config import Config

logger = logging.getLogger(__name__)

# Type hints only - these are injected via DI
if TYPE_CHECKING:
    from .logging_service import LoggingService


class ProcessService:
    """Enhanced rippled process with output callback support"""

    def __init__(
        self, binary_path: str, name: str, config: Config, logging_service: "LoggingService"
    ):
        if logging_service is None:
            raise ValueError("logging_service is required")
        self.binary_path = binary_path
        self.name = name
        self.config = config
        self.logging_service = logging_service
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None

        # Output handling
        self.stdout_buffer = []
        self.stderr_buffer = []
        self.output_thread = None
        self.stop_output_capture = threading.Event()

        # Callbacks
        self.stdout_callbacks: List[Callable[[str], None]] = []
        self.stderr_callbacks: List[Callable[[str], None]] = []

    def add_stdout_callback(self, callback: Callable[[str], None]):
        """Add a callback for stdout lines"""
        self.stdout_callbacks.append(callback)

    def add_stderr_callback(self, callback: Callable[[str], None]):
        """Add a callback for stderr lines"""
        self.stderr_callbacks.append(callback)

    def start(self) -> bool:
        """Start the rippled process"""
        try:
            cmd = [self.binary_path, "--conf", self.config.rippled_config_path]

            # Add either --standalone or --net
            if self.config.standalone_mode:
                cmd.append("--standalone")
            else:
                cmd.append("--net")

            logger.info(f"Starting {self.name} with command: {' '.join(cmd)}")

            # Log the actual binary path being used
            self.logging_service.info(f"Spawning rippled binary: {self.binary_path}")

            env = os.environ.copy()
            env["NO_COLOR"] = "1"

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=None,
                bufsize=1,
                universal_newlines=True,
            )
            self.pid = self.process.pid
            logger.info(f"Started {self.name} with PID: {self.pid}")

            # Give it a moment to start
            time.sleep(2)

            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                logger.error(f"Failed to start {self.name}: {stderr}")
                return False

            # Start output capture thread
            self.output_thread = threading.Thread(target=self._capture_output_thread, daemon=True)
            self.output_thread.start()

            return True

        except Exception as e:
            self.logging_service.error(f"Error starting {self.name}: {e}", exc_info=True)
            return False

    def stop(self):
        """Stop the rippled process"""
        # Stop output capture thread first
        if self.output_thread:
            self.stop_output_capture.set()
            self.output_thread.join(timeout=2)

        if self.process:
            try:
                logger.info(f"Stopping {self.name}...")
                self.process.terminate()

                # Wait for graceful shutdown
                try:
                    self.process.wait(timeout=10)
                    logger.info(f"Gracefully stopped {self.name}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force killing {self.name}")
                    self.process.kill()
                    self.process.wait()

            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")

    def is_alive(self) -> bool:
        """Check if the process is still running"""
        if not self.process:
            return False
        return self.process.poll() is None

    def get_memory_usage(self) -> dict:
        """Get memory usage statistics for the process"""
        if not self.pid:
            return {}

        try:
            process = psutil.Process(self.pid)
            if not process.is_running():
                return {}

            memory_info = process.memory_info()
            memory_percent = process.memory_percent()

            return {
                "rss": memory_info.rss / (1024 * 1024),  # MB
                "vms": memory_info.vms / (1024 * 1024),  # MB
                "percent": memory_percent,
                "num_threads": process.num_threads(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {}

    def _capture_output_thread(self):
        """Thread function to continuously capture output"""
        while not self.stop_output_capture.is_set() and self.process:
            try:
                # Check both stdout and stderr
                streams = []
                if self.process.stdout:
                    streams.append(self.process.stdout)
                if self.process.stderr:
                    streams.append(self.process.stderr)

                if not streams:
                    break

                # Wait for data with a timeout
                readable, _, _ = select.select(streams, [], [], 0.1)

                for stream in readable:
                    if stream == self.process.stdout:
                        line = stream.readline()
                        if line:
                            line = line.strip()
                            self.stdout_buffer.append(line)
                            if len(self.stdout_buffer) > 100:
                                self.stdout_buffer.pop(0)

                            # Notify callbacks
                            for callback in self.stdout_callbacks:
                                try:
                                    callback(line)
                                except Exception as e:
                                    logger.error(f"Stdout callback error: {e}")

                    elif stream == self.process.stderr:
                        line = stream.readline()
                        if line:
                            line = line.strip()
                            self.stderr_buffer.append(line)
                            if len(self.stderr_buffer) > 100:
                                self.stderr_buffer.pop(0)

                            # Notify callbacks
                            for callback in self.stderr_callbacks:
                                try:
                                    callback(line)
                                except Exception as e:
                                    logger.error(f"Stderr callback error: {e}")

            except Exception as e:
                if not self.stop_output_capture.is_set():
                    logger.debug(f"Error in output capture thread: {e}")
                break
