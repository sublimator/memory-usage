"""
Logging service for the memory monitor
"""

import inspect
import logging
import os
from enum import Enum
from typing import Callable, List, Optional


class LogLevel(Enum):
    """Log levels"""

    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


class LoggingService:
    """Centralized logging service that can be injected"""

    def __init__(self):
        self._handlers: List[Callable[[str, LogLevel], None]] = []
        self._logger = logging.getLogger("memory_monitor")
        self._logger.setLevel(logging.DEBUG)

        # Remove any existing handlers to avoid duplicates
        self._logger.handlers = []

        # Create a custom handler that routes to our callbacks
        handler = CallbackHandler(self)
        handler.setLevel(logging.DEBUG)

        # Add formatter with timestamp and location
        formatter = logging.Formatter(
            "%(asctime)s - %(filename)s:%(lineno)d - %(levelname)s - %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)

        self._logger.addHandler(handler)

    def add_handler(self, handler: Callable[[str, LogLevel], None]):
        """Add a log handler callback"""
        self._handlers.append(handler)

    def remove_handler(self, handler: Callable[[str, LogLevel], None]):
        """Remove a log handler callback"""
        if handler in self._handlers:
            self._handlers.remove(handler)

    def _log_with_caller_info(self, level: int, message: str, exc_info=None):
        """Log message with actual caller's file and line info"""
        # Get the caller's frame (skip this method and the public method that called it)
        frame = inspect.currentframe()
        if frame and frame.f_back and frame.f_back.f_back:
            caller_frame = frame.f_back.f_back
            filename = os.path.basename(caller_frame.f_code.co_filename)
            lineno = caller_frame.f_lineno

            # Create a log record manually with caller info
            record = self._logger.makeRecord(
                self._logger.name, level, filename, lineno, message, args=(), exc_info=exc_info
            )
            self._logger.handle(record)
        else:
            # Fallback to normal logging
            self._logger.log(level, message, exc_info=exc_info)

    def debug(self, message: str):
        """Log debug message"""
        self._log_with_caller_info(logging.DEBUG, message)

    def info(self, message: str):
        """Log info message"""
        self._log_with_caller_info(logging.INFO, message)

    def warning(self, message: str):
        """Log warning message"""
        self._log_with_caller_info(logging.WARNING, message)

    def error(self, message: str, exc_info=None):
        """Log error message"""
        self._log_with_caller_info(logging.ERROR, message, exc_info=exc_info)

    def critical(self, message: str):
        """Log critical message"""
        self._log_with_caller_info(logging.CRITICAL, message)

    def _emit_to_handlers(self, message: str, level: LogLevel):
        """Emit message to all registered handlers"""
        for handler in self._handlers:
            try:
                handler(message, level)
            except Exception as e:
                # Don't let handler errors break logging
                print(f"Error in log handler: {e}")


class CallbackHandler(logging.Handler):
    """Custom logging handler that routes to callbacks"""

    def __init__(self, logging_service: LoggingService):
        super().__init__()
        self.logging_service = logging_service

    def emit(self, record):
        """Emit a log record"""
        try:
            msg = self.format(record)
            level = LogLevel(record.levelno)
            self.logging_service._emit_to_handlers(msg, level)
        except Exception:
            self.handleError(record)
