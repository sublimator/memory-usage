"""
Status bar component for the dashboard
"""

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from ...managers.state_manager import ApplicationState
from ...utils.formatters import (
    format_duration,
    format_ledger_ranges_display,
    format_memory,
)
from ...utils.parsers import parse_ledger_ranges


def _parse_range_end(complete_ledgers: str) -> Optional[int]:
    """Return the highest ledger index covered by ``complete_ledgers``.

    ``complete_ledgers`` is rippled's ``server_info`` range string, e.g.
    ``"103571598-103573581"`` or ``"100-200,300-400"``. We only care about
    the last range's upper bound.
    """
    try:
        last = complete_ledgers.rsplit(",", 1)[-1].strip()
        return int(last.split("-")[-1].strip())
    except (ValueError, IndexError):
        return None


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
    ledgers_text = reactive("Ledgers: empty")
    timing_text = reactive("Total: 00s")

    def __init__(self, state_manager):
        super().__init__(id="status-bar-container")
        self.state_manager = state_manager
        # Subscribe to state changes
        self.state_manager.subscribe(self.update_from_state)

    def compose(self) -> ComposeResult:
        """Create the status bar layout"""
        with Horizontal(id="status-bar"):
            # yield StatusItem(self.status, classes="status-text")
            yield StatusItem(self.memory_text, classes="memory-text")
            yield StatusItem(self.ledgers_text, classes="ledgers-text")
            yield StatusItem(self.timing_text, classes="timing-text")

    def update_from_state(self, state: ApplicationState):
        """Update the status bar from application state"""
        # Update reactive properties which will trigger re-renders
        self.status = state.current_status

        # Update memory
        if state.current_memory_mb > 0:
            memory_text = f"Memory: {format_memory(state.current_memory_mb)} ({state.current_memory_percent:.1f}%)"
            extras = []
            if state.num_threads > 0:
                extras.append(f"{state.num_threads} threads")
            if state.current_pid is not None:
                extras.append(f"PID {state.current_pid}")
            if extras:
                memory_text += f" [{', '.join(extras)}]"
            self.memory_text = memory_text
        elif state.current_pid is not None:
            self.memory_text = f"Memory: N/A [PID {state.current_pid}]"
        else:
            self.memory_text = "Memory: N/A"

        # Update ledgers (combined format). The brackets are escaped so
        # Rich doesn't try to parse '[syncing]' as a style tag and drop it.
        prefix = f"\\[{state.server_state}] " if state.server_state else ""
        age_suffix = ""
        if state.validated_age_s is not None:
            age_suffix = f" ({state.validated_age_s}s old)"
        elif state.closed_ledger_age_s is not None:
            age_suffix = f" (closed {state.closed_ledger_age_s}s old)"

        if state.complete_ledgers and state.complete_ledgers != "empty":
            ranges = format_ledger_ranges_display(state.complete_ledgers)
            ledger_count = parse_ledger_ranges(state.complete_ledgers)
            range_end = _parse_range_end(state.complete_ledgers)

            ledgers_str = f"{ranges}"

            if state.current_ledger != "N/A":
                current_ledger_num: Optional[int] = None
                try:
                    current_ledger_num = int(state.current_ledger)
                except ValueError:
                    current_ledger_num = None

                # Only show ' @ X' when the current ledger is AHEAD of the
                # tracked range end — otherwise it's redundant with the
                # 'A-B' range already shown.
                if current_ledger_num is not None:
                    if range_end is None or current_ledger_num > range_end:
                        ledgers_str += f" @ {current_ledger_num:,}"
                else:
                    ledgers_str += f" @ {state.current_ledger}"

                ledgers_since_sync = 0
                if state.sync_start_ledger is not None and current_ledger_num is not None:
                    ledgers_since_sync = current_ledger_num - state.sync_start_ledger

                ledgers_str += f" ({ledger_count:,} total"
                if ledgers_since_sync > 0:
                    ledgers_str += f", {ledgers_since_sync:,} tracked"
                ledgers_str += ")"
            else:
                ledgers_str += f" ({ledger_count:,} total)"

            self.ledgers_text = f"{prefix}{ledgers_str}{age_suffix}"
        elif state.closed_ledger_seq is not None:
            # No validated range yet — fall back to closed_ledger so the
            # user sees we're at least making progress.
            self.ledgers_text = f"{prefix}closed #{state.closed_ledger_seq:,}{age_suffix}"
        else:
            self.ledgers_text = f"{prefix}empty"

        # Update timing
        if state.elapsed_seconds > 0:
            total_time = format_duration(state.elapsed_seconds)
            timing_parts = [f"Total: {total_time}"]

            # Add sync time
            if state.sync_duration_seconds is not None:
                sync_time = format_duration(state.sync_duration_seconds)
                timing_parts.append(f"Sync: {sync_time}")

            # Add test time
            if state.monitoring_elapsed_seconds is not None:
                test_time = format_duration(state.monitoring_elapsed_seconds)
                timing_parts.append(f"Test: {test_time}")

            self.timing_text = " | ".join(timing_parts)
        else:
            self.timing_text = "Total: 00s"

        # Force update of child widgets
        self._update_child_widgets()

    def _update_child_widgets(self):
        """Update child widgets with current reactive values"""
        try:
            # self.query_one(".status-text", StatusItem).update(self.status)
            self.query_one(".memory-text", StatusItem).update(self.memory_text)
            self.query_one(".ledgers-text", StatusItem).update(self.ledgers_text)
            self.query_one(".timing-text", StatusItem).update(self.timing_text)
        except Exception:
            # Widgets might not be mounted yet
            pass
