"""
Status bar component for the dashboard
"""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static
from textual.reactive import reactive

from ...managers.state_manager import ApplicationState
from ...utils.formatters import format_memory, format_ledger_ranges


class StatusBar(Static):
    """Status bar that reacts to state changes"""
    
    # Reactive attributes that trigger re-renders
    status = reactive("Initializing...")
    memory_text = reactive("Memory: N/A")
    complete_ledgers = reactive("Complete Ledgers: empty")
    current_ledger = reactive("Current: N/A")
    
    def __init__(self, state_manager):
        super().__init__()
        self.state_manager = state_manager
        # Subscribe to state changes
        self.state_manager.subscribe(self.update_from_state)
    
    def compose(self) -> ComposeResult:
        """Create the status bar layout"""
        with Horizontal(id="status-bar"):
            yield Static(self.status, id="status-text")
            yield Static(self.memory_text, id="memory-text")
            yield Static(self.complete_ledgers, id="complete-ledgers")
            yield Static(self.current_ledger, id="current-ledger")
    
    def update_from_state(self, state: ApplicationState):
        """Update the status bar from application state"""
        # Update status
        self.query_one("#status-text", Static).update(state.current_status)
        
        # Update memory
        if state.current_memory_mb > 0:
            memory_text = f"Memory: {format_memory(state.current_memory_mb)} ({state.current_memory_percent:.1f}%)"
            if state.num_threads > 0:
                memory_text += f" [{state.num_threads} threads]"
        else:
            memory_text = "Memory: N/A"
        self.query_one("#memory-text", Static).update(memory_text)
        
        # Update complete ledgers
        if state.complete_ledgers and state.complete_ledgers != "empty":
            if state.ledger_count > 0:
                ledgers_text = f"Complete Ledgers: {format_ledger_ranges(state.complete_ledgers)} ({state.ledger_count})"
            else:
                ledgers_text = f"Complete Ledgers: {state.complete_ledgers}"
        else:
            ledgers_text = "Complete Ledgers: empty"
        self.query_one("#complete-ledgers", Static).update(ledgers_text)
        
        # Update current ledger
        self.query_one("#current-ledger", Static).update(f"Current: {state.current_ledger}")