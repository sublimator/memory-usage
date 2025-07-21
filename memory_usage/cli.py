#!/usr/bin/env python3
"""
Command-line interface for Xahaud Memory Monitor
"""

import argparse
from pathlib import Path

from .config import Config
from .container import Container
from .utils import detect_xahau, parse_rippled_config

# Default Configuration
DEFAULT_RIPPLED_CONFIG_PATH = "niq-conf/xahaud.cfg"
DEFAULT_WEBSOCKET_PORT = 6009
DEFAULT_API_VERSION = 2  # Standard XRPL uses version 2


def list_binaries(build_dir: str = "build"):
    """List available binaries and exit"""
    build_path = Path(build_dir)
    if build_path.exists():
        binaries = []
        for file_path in build_path.glob("rippled-*"):
            if file_path.is_file() and file_path.stat().st_mode & 0o111:
                binaries.append(file_path.name)
        
        if binaries:
            print("Available binaries:")
            for binary in sorted(binaries):
                print(f"  - {binary}")
        else:
            print("No rippled binaries found in build/ directory")
    else:
        print(f"Build directory {build_dir} does not exist")


def run():
    """Entry point for the CLI"""
    # Parse arguments
    parser = argparse.ArgumentParser(description='Monitor rippled binary memory usage')
    parser.add_argument('--duration', '-d', type=int, default=5, 
                       help='Test duration in minutes for each binary (default: 5)')
    parser.add_argument('--binaries', '-b', nargs='+', 
                       help='Specific binaries to test (e.g. rippled-compact-exact rippled-normal)')
    parser.add_argument('--list', '-l', action='store_true',
                       help='List available binaries and exit')
    parser.add_argument('--config', '-c', type=str, default=DEFAULT_RIPPLED_CONFIG_PATH,
                       help=f'Path to rippled config file (default: {DEFAULT_RIPPLED_CONFIG_PATH})')
    parser.add_argument('--websocket-url', '-w', type=str,
                       help='Override websocket URL (e.g. ws://localhost:6009)')
    parser.add_argument('--api-version', '-v', type=int, choices=[1, 2],
                       help='API version to use (auto-detected if not specified)')
    parser.add_argument('--standalone', '-s', action='store_true',
                       help='Run rippled in standalone mode (mutually exclusive with --net)')
    parser.add_argument('--build-dir', type=str, default='build',
                       help='Directory containing rippled binaries (default: build)')
    parser.add_argument('--output-dir', type=str, default='memory_monitor_results',
                       help='Directory for output files (default: memory_monitor_results)')
    
    args = parser.parse_args()
    
    # Handle --list command
    if args.list:
        list_binaries(args.build_dir)
        return
    
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
    
    # Create configuration
    config = Config(
        rippled_config_path=args.config,
        websocket_url=websocket_url,
        api_version=api_version,
        standalone_mode=args.standalone,
        test_duration_minutes=args.duration,
        specified_binaries=args.binaries,
        build_dir=args.build_dir,
        output_dir=args.output_dir,
        poll_interval=4  # Default poll interval
    )
    
    # Configure DI container
    container = Container()
    # Pass the config object directly
    container.config.override(config)
    container.wire(modules=[
        "memory_usage.ui.dashboard",
        "memory_usage.services.monitoring_service",
        "memory_usage.services.logging_service",
        "memory_usage.managers.process_manager",
        "memory_usage.managers.websocket_manager",
        "memory_usage.managers.state_manager",
    ])
    
    # Import and run dashboard
    from .ui.dashboard import MemoryMonitorDashboard
    
    # Create and run the dashboard
    app = MemoryMonitorDashboard()
    app.run()


if __name__ == "__main__":
    run()