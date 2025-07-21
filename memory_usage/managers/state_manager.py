"""
Centralized state management for the application
"""

import asyncio
from typing import Optional, List, Callable, Union
from dataclasses import dataclass, field


@dataclass
class ApplicationState:
    """Current application state"""
    # Ledger information
    complete_ledgers: str = "empty"
    current_ledger: str = "N/A"
    ledger_count: int = 0
    
    # Memory stats
    current_memory_mb: float = 0.0
    current_memory_percent: float = 0.0
    num_threads: int = 0
    
    # Process info
    current_status: str = "Initializing..."
    current_binary: Optional[str] = None
    current_pid: Optional[int] = None
    
    # Test info
    test_results_dir: Optional[str] = None
    tests_completed: int = 0
    total_tests: int = 0
    
    # Monitoring state
    is_monitoring: bool = False
    is_synced: bool = False
    is_paused: bool = False


class StateManager:
    """Manages shared application state with observer pattern"""
    
    def __init__(self):
        self.state = ApplicationState()
        self._lock = asyncio.Lock()
        self._observers: List[Callable] = []
    
    def subscribe(self, callback: Callable) -> Callable:
        """Subscribe to state changes"""
        self._observers.append(callback)
        return callback
    
    def unsubscribe(self, callback: Callable):
        """Unsubscribe from state changes"""
        if callback in self._observers:
            self._observers.remove(callback)
    
    async def update_ledger_info(self, complete_ledgers: str, current_ledger: str, ledger_count: int = 0):
        """Update ledger information"""
        async with self._lock:
            self.state.complete_ledgers = complete_ledgers
            self.state.current_ledger = current_ledger
            self.state.ledger_count = ledger_count
            self.state.is_synced = complete_ledgers != "empty"
            await self._notify_observers()
    
    async def update_memory_stats(self, memory_mb: float, memory_percent: float, num_threads: int = 0):
        """Update memory statistics"""
        async with self._lock:
            self.state.current_memory_mb = memory_mb
            self.state.current_memory_percent = memory_percent
            self.state.num_threads = num_threads
            await self._notify_observers()
    
    async def update_status(self, status: str):
        """Update current status"""
        async with self._lock:
            self.state.current_status = status
            await self._notify_observers()
    
    async def update_process_info(self, binary_name: Optional[str], pid: Optional[int]):
        """Update process information"""
        async with self._lock:
            self.state.current_binary = binary_name
            self.state.current_pid = pid
            await self._notify_observers()
    
    async def update_test_progress(self, tests_completed: int, total_tests: int):
        """Update test progress"""
        async with self._lock:
            self.state.tests_completed = tests_completed
            self.state.total_tests = total_tests
            await self._notify_observers()
    
    async def set_monitoring(self, is_monitoring: bool):
        """Set monitoring state"""
        async with self._lock:
            self.state.is_monitoring = is_monitoring
            await self._notify_observers()
    
    async def toggle_pause(self) -> bool:
        """Toggle pause state and return new state"""
        async with self._lock:
            self.state.is_paused = not self.state.is_paused
            await self._notify_observers()
            return self.state.is_paused
    
    def get_state(self) -> ApplicationState:
        """Get current state (safe copy)"""
        # For now just return the state, could deep copy if needed
        return self.state
    
    async def _notify_observers(self):
        """Notify all observers of state change"""
        for callback in self._observers:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(self.state)
                else:
                    callback(self.state)
            except Exception as e:
                # Log but don't crash on observer errors
                import logging
                logging.error(f"Observer callback error: {e}")