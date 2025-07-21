#!/usr/bin/env python3
"""
Command-line interface for Xahaud Memory Monitor
"""

import argparse
from pathlib import Path
import configparser
from typing import Optional, Tuple

from .config import Config
from .monitor_dashboard import MonitorDashboard

# Default Configuration
DEFAULT_RIPPLED_CONFIG_PATH = "niq-conf/xahaud.cfg"
DEFAULT_WEBSOCKET_PORT = 6009
DEFAULT_API_VERSION = 2  # Standard XRPL uses version 2


def detect_xahau(config_path: str) -> bool:
    """Detect if this is a Xahau configuration based on file name or content."""
    # Check file name
    if 'xahau' in config_path.lower():
        return True
    
    # Check file content
    try:
        if Path(config_path).exists():
            with open(config_path, 'r') as f:
                content = f.read().lower()
                if 'xahau' in content:
                    return True
    except Exception:
        pass
    
    return False


def parse_rippled_config(config_path: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse rippled configuration file to extract websocket and RPC ports.
    Returns (websocket_port, rpc_port) or (None, None) if not found."""
    try:
        if not Path(config_path).exists():
            return None, None
            
        config = configparser.ConfigParser()
        # Read the file with custom parsing to handle the rippled config format
        with open(config_path, 'r') as f:
            config_str = f.read()
        
        # rippled uses a custom format, we need to parse it manually
        current_section = None
        sections = {}
        
        for line in config_str.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
                
            # Check if this is a section header
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1]
                if current_section not in sections:
                    sections[current_section] = {}
            elif current_section and '=' in line:
                # Parse key = value
                key, value = line.split('=', 1)
                sections[current_section][key.strip()] = value.strip()
            elif current_section:
                # Single value line (like port numbers)
                if line.replace(' ', '').replace('.', '').replace('_', '').isalnum():
                    if 'values' not in sections[current_section]:
                        sections[current_section]['values'] = []
                    sections[current_section]['values'].append(line)
        
        ws_port = None
        rpc_port = None
        
        # Look for websocket admin port (port_ws_admin_local)
        if 'port_ws_admin_local' in sections:
            port_section = sections['port_ws_admin_local']
            if 'port' in port_section:
                ws_port = int(port_section['port'])
        
        # Fallback to public websocket port
        if not ws_port and 'port_ws_public' in sections:
            port_section = sections['port_ws_public']
            if 'port' in port_section:
                ws_port = int(port_section['port'])
        
        # Look for RPC admin port (port_rpc_admin_local)
        if 'port_rpc_admin_local' in sections:
            port_section = sections['port_rpc_admin_local']
            if 'port' in port_section:
                rpc_port = int(port_section['port'])
        
        # Fallback to public RPC port
        if not rpc_port and 'port_rpc_public' in sections:
            port_section = sections['port_rpc_public']
            if 'port' in port_section:
                rpc_port = int(port_section['port'])
                
        return ws_port, rpc_port
        
    except Exception:
        return None, None


def parse_ledger_ranges(complete_ledgers: str) -> int:
    """Parse complete_ledgers range set and return total count.
    
    Handles formats like:
    - "empty" -> 0
    - "100-200" -> 101
    - "100-200,300-400" -> 202
    - "100-200,300-400,500-600" -> 303
    """
    if not complete_ledgers or complete_ledgers == 'empty':
        return 0
    
    total_count = 0
    try:
        # Split by comma to get individual ranges
        ranges = complete_ledgers.split(',')
        
        for range_str in ranges:
            range_str = range_str.strip()
            if '-' in range_str:
                parts = range_str.split('-')
                if len(parts) == 2:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    # Include both start and end in count
                    total_count += (end - start + 1)
    except Exception:
        return 0
    
    return total_count


def format_ledger_ranges(complete_ledgers: str, max_length: int = 50) -> str:
    """Format complete_ledgers string for display.
    
    If the string is too long, shows first and last range with count.
    E.g., "100-200,300-400,...,900-1000 (5 ranges)"
    """
    if not complete_ledgers or complete_ledgers == 'empty':
        return complete_ledgers
    
    if len(complete_ledgers) <= max_length:
        return complete_ledgers
    
    # Split into ranges
    ranges = [r.strip() for r in complete_ledgers.split(',')]
    if len(ranges) <= 2:
        return complete_ledgers
    
    # Show first and last with ellipsis
    return f"{ranges[0]},...,{ranges[-1]} ({len(ranges)} ranges)"


def run():
    """Entry point for the CLI"""
    # Parse arguments
    parser = argparse.ArgumentParser(description='Monitor rippled binary memory usage')
    parser.add_argument('--duration', '-d', type=int, default=5, 
                       help='Test duration in minutes for each binary (default: 5)')
    parser.add_argument('--binaries', '-b', nargs='+', 
                       help='Specific binaries to test (e.g. rippled-compact-exact rippled-normal)')
    parser.add_argument('--config', '-c', type=str, default=DEFAULT_RIPPLED_CONFIG_PATH,
                       help=f'Path to rippled config file (default: {DEFAULT_RIPPLED_CONFIG_PATH})')
    parser.add_argument('--websocket-url', '-w', type=str,
                       help='Override websocket URL (e.g. ws://localhost:6009)')
    parser.add_argument('--api-version', '-v', type=int, choices=[1, 2],
                       help='API version to use (auto-detected if not specified)')
    parser.add_argument('--standalone', '-s', action='store_true',
                       help='Run rippled in standalone mode (mutually exclusive with --net)')
    
    args = parser.parse_args()
    
    # Determine API version
    api_version = args.api_version
    if not api_version:
        # Auto-detect based on config
        if detect_xahau(args.config):
            api_version = 1
        else:
            api_version = DEFAULT_API_VERSION
    
    # Determine WebSocket URL
    websocket_url = args.websocket_url
    if not websocket_url:
        # Parse config file to get ports
        ws_port, rpc_port = parse_rippled_config(args.config)
        if ws_port:
            websocket_url = f"ws://localhost:{ws_port}"
        else:
            # Fallback to default
            websocket_url = f"ws://localhost:{DEFAULT_WEBSOCKET_PORT}"
    
    # Create config object
    config = Config(
        rippled_config_path=args.config,
        websocket_url=websocket_url,
        api_version=api_version,
        standalone_mode=args.standalone,
        test_duration_minutes=args.duration,
        specified_binaries=args.binaries
    )
    
    # Run the dashboard
    app = MonitorDashboard(config)
    app.run()