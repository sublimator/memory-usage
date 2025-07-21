"""
UI components for the dashboard
"""

from .status_bar import StatusBar, StatusItem
from .log_viewer import MonitorLogViewer, ProcessOutputViewer

__all__ = [
    'StatusBar',
    'StatusItem',
    'MonitorLogViewer',
    'ProcessOutputViewer',
]