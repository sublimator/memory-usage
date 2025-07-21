"""
UI components for the dashboard
"""

from .status_bar import StatusBar
from .log_viewer import MonitorLogViewer, ProcessOutputViewer

__all__ = [
    'StatusBar',
    'MonitorLogViewer',
    'ProcessOutputViewer',
]