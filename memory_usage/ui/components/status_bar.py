"""
Status bar component for the dashboard
"""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static
from textual.reactive import reactive

from ...managers.state_manager import ApplicationState
from ...utils.formatters import format_memory, format_ledger_ranges


class StatusItem(Static):
    """Individual status bar item with proper styling"""
    
    def __init__(self, content: str = "", classes: str = ""):
        super().__init__(content)
        if classes:
            self.add_class(classes)


class StatusBar(Static):
    """Status bar that reacts to state changes"""
    
    # Reactive attributes that trigger re-renders
    status = reactive("Initializing...")
    memory_text = reactive("Memory: N/A")
    complete_ledgers = reactive("Complete Ledgers: empty")
    current_ledger = reactive("Current: N/A")
    
    def __init__(self, state_manager):
        super().__init__(id="status-bar-container")
        self.state_manager = state_manager
        # Subscribe to state changes
        self.state_manager.subscribe(self.update_from_state)
    
    def compose(self) -> ComposeResult:
        """Create the status bar layout"""
        with Horizontal(id="status-bar"):
            yield StatusItem(self.status, classes="status-text")
            yield StatusItem(self.memory_text, classes="memory-text")
            yield StatusItem(self.complete_ledgers, classes="complete-ledgers")
            yield StatusItem(self.current_ledger, classes="current-ledger")
    
    def update_from_state(self, state: ApplicationState):
        """Update the status bar from application state"""
        # Update reactive properties which will trigger re-renders
        self.status = state.current_status
        
        # Update memory
        if state.current_memory_mb > 0:
            memory_text = f"Memory: {format_memory(state.current_memory_mb)} ({state.current_memory_percent:.1f}%)"
            if state.num_threads > 0:
                memory_text += f" [{state.num_threads} threads]"
            self.memory_text = memory_text
        else:
            self.memory_text = "Memory: N/A"
        
        # Update complete ledgers
        if state.complete_ledgers and state.complete_ledgers != "empty":
            if state.ledger_count > 0:
                ledgers_text = f"Complete Ledgers: {format_ledger_ranges(state.complete_ledgers)} ({state.ledger_count})"
            else:
                ledgers_text = f"Complete Ledgers: {state.complete_ledgers}"
            self.complete_ledgers = ledgers_text
        else:
            self.complete_ledgers = "Complete Ledgers: empty"
        
        # Update current ledger
        self.current_ledger = f"Current: {state.current_ledger}"
        
        # Force update of child widgets
        self._update_child_widgets()
    
    def _update_child_widgets(self):
        """Update child widgets with current reactive values"""
        try:
            self.query_one(".status-text", StatusItem).update(self.status)
            self.query_one(".memory-text", StatusItem).update(self.memory_text)
            self.query_one(".complete-ledgers", StatusItem).update(self.complete_ledgers)
            self.query_one(".current-ledger", StatusItem).update(self.current_ledger)
        except Exception:
            # Widgets might not be mounted yet
            pass