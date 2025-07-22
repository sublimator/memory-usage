"""
Memory graph widget showing memory usage over time
"""

import time
from collections import deque
from typing import Deque

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Button, Static


class MemoryGraph(Vertical):
    """Display memory usage as a sparkline graph with time controls"""

    # No time windows - just show all data
    show_all_data = True

    def __init__(self):
        super().__init__(id="memory-graph")
        self.memory_data: Deque[tuple[float, float]] = deque(
            maxlen=3600
        )  # Store up to 1 hour of data (1 per second)
        self.graph_display = Static("", id="graph-display")
        self.last_update = time.time()
        self.start_time = time.time()
        self.absolute_min = None  # Track the absolute minimum we've seen
        self.absolute_max = None  # Track the absolute maximum we've seen

    def compose(self) -> ComposeResult:
        """Compose the widget"""
        # Top row: controls and current value
        with Horizontal(classes="graph-controls"):
            yield Static("Memory (All Time):", classes="graph-label")
            yield Static("", classes="graph-spacer")
            yield Static("", id="memory-value", classes="graph-value")

        # Main graph area (labels will be rendered as part of the graph)
        yield self.graph_display

        # Time scale (aligned with graph area)
        with Horizontal(classes="time-scale"):
            yield Static("", classes="time-spacer")  # Space for Y-axis labels
            yield Static("", id="time-start", classes="time-marker")
            yield Static("", id="time-mid", classes="time-marker-center")
            yield Static("", id="time-end", classes="time-marker-right")

    def update_memory(self, memory_mb: float):
        """Update the memory graph with new data"""
        current_time = time.time()

        # Add data point with timestamp
        self.memory_data.append((current_time, memory_mb))

        # Update graph
        self._update_graph()

        # Update current value display
        self.query_one("#memory-value", Static).update(f"{memory_mb:.1f}MB")

        self.last_update = current_time

    def _update_graph(self):
        """Update the graph to show all collected data"""
        if not self.memory_data:
            return

        # Use all data (no filtering)
        filtered_data = list(self.memory_data)

        if filtered_data:
            # Extract just memory values for sparkline
            memory_values = [m for _, m in filtered_data]

            # Calculate min/max for better scaling
            if memory_values:
                current_min = min(memory_values)
                current_max = max(memory_values)

                # Update absolute min/max (only track minimums from the current window)
                if self.absolute_min is None:
                    self.absolute_min = current_min
                else:
                    # Only lower the minimum, never raise it
                    self.absolute_min = min(self.absolute_min, current_min)

                if self.absolute_max is None:
                    self.absolute_max = current_max
                else:
                    # Allow max to grow
                    self.absolute_max = max(self.absolute_max, current_max)

                # Use stable absolute min as the base (never change this)
                min_val = self.absolute_min
                max_val = max(self.absolute_max, current_max)

                # Ensure we have a reasonable range, but never change min_val
                range_size = max_val - min_val
                if range_size < 10:
                    # If range is too small, extend upward only
                    max_val = min_val + 10
                else:
                    # Add 10% padding on top only (but don't let it affect min_val)
                    padding = range_size * 0.1
                    max_val = max_val + padding

                # Update graph display with integrated labels
                graph_text = self._render_graph(memory_values, min_val, max_val)
                self.graph_display.update(graph_text)
            else:
                self.graph_display.update("")

            # Update time markers based on actual data span
            if filtered_data and len(filtered_data) > 1:
                # Calculate actual time span of the data
                oldest_time = filtered_data[0][0]
                newest_time = filtered_data[-1][0]
                actual_span = newest_time - oldest_time

                # Update markers based on actual span
                if actual_span > 0:
                    self.query_one("#time-start", Static).update(f"-{int(actual_span)}s")
                    self.query_one("#time-mid", Static).update(f"-{int(actual_span/2)}s")
                else:
                    self.query_one("#time-start", Static).update("0s")
                    self.query_one("#time-mid", Static).update("0s")
            else:
                # No data or single point
                self.query_one("#time-start", Static).update("0s")
                self.query_one("#time-mid", Static).update("0s")

            self.query_one("#time-end", Static).update("now")

    def _render_graph(self, values: list[float], min_val: float, max_val: float) -> str:
        """Render a bar graph with Y-axis labels using Unicode block characters"""
        if not values or min_val >= max_val:
            return ""

        # Graph dimensions
        graph_height = 10  # lines
        # Get actual width from the widget
        total_width = self.graph_display.size.width
        if total_width <= 0:
            total_width = 90  # fallback

        # Reserve space for Y-axis labels (8 chars: "1234.5 ")
        label_width = 8
        graph_width = max(20, total_width - label_width)

        # Unicode block characters for sub-line precision
        blocks = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

        # Create empty graph
        lines = []
        for _ in range(graph_height):
            lines.append([" "] * graph_width)

        # Plot values (right-aligned, newest on right)
        value_range = max_val - min_val

        # Always use full width - stretch or compress data to fit
        if len(values) == 0:
            plot_values = []
            start_x = 0
        elif len(values) == 1:
            # Single value - show as a single bar on the right
            plot_values = values
            start_x = graph_width - 1
        else:
            # Multiple values - always use full width by stretching/compressing
            plot_values = []
            for i in range(graph_width):
                # Map graph position to data position
                data_pos = (i / (graph_width - 1)) * (len(values) - 1)
                # Simple nearest-neighbor interpolation
                idx = int(round(data_pos))
                if idx < len(values):
                    plot_values.append(values[idx])
            start_x = 0  # Always start from the left

        for i, value in enumerate(plot_values):
            x = start_x + i
            # Normalize value to 0-1
            normalized = (value - min_val) / value_range
            # Convert to precise height (with sub-line precision)
            precise_height = normalized * graph_height * len(blocks)

            # If there's ANY value at all, ensure we show at least the smallest block
            if precise_height > 0:
                # Fill full lines
                full_lines = int(precise_height // len(blocks))
                for y in range(full_lines):
                    lines[graph_height - 1 - y][x] = "█"

                # Add partial block for remaining height
                remaining = precise_height % len(blocks)
                if full_lines < graph_height:
                    if remaining > 0:
                        # Round to nearest block, but always show at least ▁
                        block_index = max(1, int(round(remaining)))
                        # Ensure we stay within bounds
                        if block_index >= len(blocks):
                            block_index = len(blocks) - 1
                        lines[graph_height - 1 - full_lines][x] = blocks[block_index]
                    elif full_lines == 0:
                        # No full lines and no remaining, but we have SOME value
                        # Show the smallest block
                        lines[graph_height - 1][x] = blocks[1]

        # Create final output with Y-axis labels
        final_lines = []
        for i in range(graph_height):
            # Calculate Y-axis value for this line
            if i == 0:
                # Top line - always max_val
                label_value = max_val
            elif i == graph_height - 1:
                # Bottom line - always min_val (stable)
                label_value = min_val
            else:
                # Middle lines - interpolate
                ratio = i / (graph_height - 1)
                label_value = max_val - (ratio * value_range)

            label = f"{label_value:>6.1f} "

            # Combine label and graph line
            final_lines.append(label + "".join(lines[i]))

        return "\n".join(final_lines)
