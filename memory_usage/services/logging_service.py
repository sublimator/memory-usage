"""
Logging service for the memory monitor
"""

import logging
from typing import Optional, Callable, List
from enum import Enum


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
        self._logger.addHandler(handler)
    
    def add_handler(self, handler: Callable[[str, LogLevel], None]):
        """Add a log handler callback"""
        self._handlers.append(handler)
    
    def remove_handler(self, handler: Callable[[str, LogLevel], None]):
        """Remove a log handler callback"""
        if handler in self._handlers:
            self._handlers.remove(handler)
    
    def debug(self, message: str):
        """Log debug message"""
        self._logger.debug(message)
    
    def info(self, message: str):
        """Log info message"""
        self._logger.info(message)
    
    def warning(self, message: str):
        """Log warning message"""
        self._logger.warning(message)
    
    def error(self, message: str):
        """Log error message"""
        self._logger.error(message)
    
    def critical(self, message: str):
        """Log critical message"""
        self._logger.critical(message)
    
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