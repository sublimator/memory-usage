#!/usr/bin/env python3
"""
Command-line interface for Xahaud Memory Monitor
"""

import argparse
import subprocess
import sys
from pathlib import Path

from .config import Config
from .container import Container
from .utils import (
    detect_xahau,
    display_process_menu,
    find_rippled_processes,
    get_process_by_pid,
    parse_rippled_config,
)

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


def tail_logs():
    """Tail the latest log file"""
    log_dir = Path.cwd() / ".memory-usage"

    if not log_dir.exists():
        print(f"Error: Log directory {log_dir} does not exist")
        print("No logs have been created yet. Run xahaud-monitor first.")
        sys.exit(1)

    # Find all .log files
    log_files = list(log_dir.glob("*.log"))

    if not log_files:
        print(f"Error: No log files found in {log_dir}")
        sys.exit(1)

    # Sort by modification time, newest first
    log_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    latest_log = log_files[0]

    print(f"Tailing latest log: {latest_log}")
    print("-" * 80)

    # Run tail -f on the latest log
    try:
        subprocess.run(["tail", "-f", str(latest_log)])
    except KeyboardInterrupt:
        print("\nStopped tailing log file")
        sys.exit(0)


def run_attach_mode(args):
    """Run in attach mode - connect to a running process"""
    # Find or select process
    if args.pid:
        # Specific PID provided
        proc = get_process_by_pid(args.pid)
        if not proc:
            print(f"Error: No process found with PID {args.pid}")
            sys.exit(1)
        print(f"Attaching to PID {proc.pid}: {proc.name}")
    else:
        # Interactive menu
        processes = find_rippled_processes()
        proc = display_process_menu(processes)
        if not proc:
            sys.exit(0)

    # Get config path
    config_path = proc.resolved_config_path
    if not config_path:
        print(f"Warning: Could not find config file for process {proc.pid}")
        print(f"  Config from cmdline: {proc.config_path}")
        print(f"  Working dir: {proc.working_dir}")
        if not args.websocket_url:
            print("Error: Cannot determine WebSocket URL. Please provide --websocket-url")
            sys.exit(1)
        config_path = proc.config_path or "unknown"

    print(f"Config: {config_path}")

    # Determine API version
    api_version = args.api_version
    if not api_version:
        if config_path and detect_xahau(config_path):
            api_version = 1
        else:
            api_version = DEFAULT_API_VERSION
    print(f"API version: {api_version}")

    # Determine WebSocket URL
    websocket_url = args.websocket_url
    if not websocket_url:
        ws_port, rpc_port = parse_rippled_config(config_path)
        if ws_port:
            websocket_url = f"ws://localhost:{ws_port}"
        else:
            websocket_url = f"ws://localhost:{DEFAULT_WEBSOCKET_PORT}"
    print(f"WebSocket: {websocket_url}")

    # Get debug log file path from config
    debug_logfile = proc.debug_logfile_path
    if debug_logfile:
        print(f"Debug log: {debug_logfile}")
        if not Path(debug_logfile).exists():
            print("  Warning: Debug log file does not exist yet")
    else:
        print("Debug log: Not configured or could not be determined")

    # Create configuration
    config = Config(
        rippled_config_path=config_path,
        websocket_url=websocket_url,
        api_version=api_version,
        standalone_mode=False,
        test_duration_minutes=args.duration,
        specified_binaries=None,
        build_dir="",
        output_dir=args.output_dir,
        poll_interval=1,
        # Attach mode specific
        attach_mode=True,
        attach_pid=proc.pid,
        attach_binary_name=proc.name,
        attach_binary_path=proc.binary_path,
        attach_debug_logfile=debug_logfile,
    )

    # Configure DI container
    container = Container()
    container.config.override(config)
    container.wire(
        modules=[
            "memory_usage.ui.dashboard",
            "memory_usage.services.monitoring_service",
            "memory_usage.services.logging_service",
            "memory_usage.managers.process_manager",
            "memory_usage.managers.websocket_manager",
            "memory_usage.managers.state_manager",
        ]
    )

    # Import and run dashboard
    from .ui.dashboard import MemoryMonitorDashboard

    # Create and run the dashboard
    app = MemoryMonitorDashboard()
    app.run()


def run():
    """Entry point for the CLI"""
    # Parse arguments with subcommands
    parser = argparse.ArgumentParser(
        description="Monitor rippled binary memory usage",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Monitor command (default)
    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Start memory monitoring (default)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    monitor_parser.add_argument(
        "--duration",
        "-d",
        type=int,
        default=5,
        help="Test duration in minutes for each binary (default: 5)",
    )
    monitor_parser.add_argument(
        "--binaries",
        "-b",
        nargs="+",
        help="Specific binaries to test (e.g. rippled-compact-exact rippled-normal)",
    )
    monitor_parser.add_argument(
        "--list", "-l", action="store_true", help="List available binaries and exit"
    )
    monitor_parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=DEFAULT_RIPPLED_CONFIG_PATH,
        help=f"Path to rippled config file (default: {DEFAULT_RIPPLED_CONFIG_PATH})",
    )
    monitor_parser.add_argument(
        "--websocket-url", "-w", type=str, help="Override websocket URL (e.g. ws://localhost:6009)"
    )
    monitor_parser.add_argument(
        "--api-version",
        "-v",
        type=int,
        choices=[1, 2],
        help="API version to use (auto-detected if not specified)",
    )
    monitor_parser.add_argument(
        "--standalone",
        "-s",
        action="store_true",
        help="Run rippled in standalone mode (mutually exclusive with --net)",
    )
    monitor_parser.add_argument(
        "--build-dir",
        type=str,
        default="build",
        help="Directory containing rippled binaries (default: build)",
    )
    monitor_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Directory for output files (default: memory_monitor_results)",
    )

    # Logs command
    subparsers.add_parser("logs", help="Tail the latest process output log file")

    # Attach command
    attach_parser = subparsers.add_parser(
        "attach",
        help="Attach to a running xahaud/rippled process",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    attach_parser.add_argument(
        "--pid",
        "-p",
        type=int,
        help="PID of the process to attach to (interactive menu if not specified)",
    )
    attach_parser.add_argument(
        "--duration",
        "-d",
        type=int,
        default=5,
        help="Monitoring duration in minutes (default: 5)",
    )
    attach_parser.add_argument(
        "--websocket-url",
        "-w",
        type=str,
        help="Override websocket URL (e.g. ws://localhost:6009)",
    )
    attach_parser.add_argument(
        "--api-version",
        "-v",
        type=int,
        choices=[1, 2],
        help="API version to use (auto-detected if not specified)",
    )
    attach_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Directory for output files (default: memory_monitor_results)",
    )

    # Parse args
    args = parser.parse_args()

    # Default to monitor if no command specified (backwards compatibility)
    if args.command is None:
        args.command = "monitor"
        # Re-parse with monitor defaults
        args = monitor_parser.parse_args(sys.argv[1:])

    # Handle commands
    if args.command == "logs":
        tail_logs()
        return

    if args.command == "attach":
        run_attach_mode(args)
        return

    # Handle monitor command
    if hasattr(args, "list") and args.list:
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
        poll_interval=1,  # Default poll interval
    )

    # Configure DI container
    container = Container()
    # Pass the config object directly
    container.config.override(config)
    container.wire(
        modules=[
            "memory_usage.ui.dashboard",
            "memory_usage.services.monitoring_service",
            "memory_usage.services.logging_service",
            "memory_usage.managers.process_manager",
            "memory_usage.managers.websocket_manager",
            "memory_usage.managers.state_manager",
        ]
    )

    # Import and run dashboard
    from .ui.dashboard import MemoryMonitorDashboard

    # Create and run the dashboard
    app = MemoryMonitorDashboard()
    app.run()


if __name__ == "__main__":
    run()
