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
    debug_dump_process_discovery,
    detect_xahau,
    display_process_menu,
    find_rippled_processes,
    get_process_by_pid,
    parse_rippled_config,
)
from .utils.heap_sampler import (
    build_heap_trend,
    filter_sample,
    render_heap_sample,
    render_heap_trend,
    take_heap_sample,
)
from .utils.ledger_diff import (
    build_and_render_summary,
    find_delta_matches,
    load_ledger_snapshots,
    render_diff,
    render_find_results,
    resolve_session_dir,
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


def _resolve_or_exit(args) -> Path:
    """Shared dir resolution + notice-printing for diff/find/summary.

    On resolver error: prints ``error:`` + the fatal message to stderr and
    exits 2. On success, prints any non-fatal notice to stderr (so stdout
    remains pipe-friendly) and returns the path.
    """
    root = Path(args.output_dir)
    dir_path, err, notice = resolve_session_dir(root, args.dir)
    if err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(2)
    assert dir_path is not None
    if notice:
        print(f"note: {notice}", file=sys.stderr)
    return dir_path


def run_diff(args):
    """Diff two ledger-close snapshots from the resolved session dir."""
    dir_path = _resolve_or_exit(args)
    events_path = dir_path / "events.jsonl"
    from_snap, to_snap, first_ledger = load_ledger_snapshots(
        events_path, args.from_ledger, args.to_ledger
    )
    if from_snap is None:
        print(
            f"error: no snapshot with ledger_index={args.from_ledger} in {events_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    if to_snap is None:
        print(
            f"error: no snapshot with ledger_index={args.to_ledger} in {events_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    render_diff(from_snap, to_snap, first_ledger=first_ledger)


def run_summary(args):
    """One-screen triage view of a session dir."""
    import json as _json

    dir_path = _resolve_or_exit(args)
    events_path = dir_path / "events.jsonl"
    meta_path = dir_path / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = _json.load(f)
        except (OSError, _json.JSONDecodeError):
            pass
    build_and_render_summary(events_path, meta, top_n=args.top)


def run_heap(args):
    """On-demand heap sample of the running rippled/xahaud."""
    import json as _json

    if args.pid:
        pid = args.pid
    else:
        procs = find_rippled_processes()
        if not procs:
            print("error: no running rippled/xahaud — pass --pid PID", file=sys.stderr)
            sys.exit(2)
        if len(procs) > 1:
            names = ", ".join(f"{p.name}(pid {p.pid})" for p in procs)
            print(f"error: multiple rippled processes ({names}) — pass --pid PID", file=sys.stderr)
            sys.exit(2)
        pid = procs[0].pid

    sample = take_heap_sample(pid, top_n=args.top)
    if args.binary or args.grep:
        sample = filter_sample(sample, binary=args.binary, grep=args.grep)
    if args.json:
        print(_json.dumps(sample, indent=2))
        return
    render_heap_sample(sample)
    if not sample.get("ok"):
        sys.exit(1)


def run_heap_trend(args):
    """Per-class heap growth across all heap samples in a session dir."""
    dir_path = _resolve_or_exit(args)
    rows, meta = build_heap_trend(
        dir_path,
        binary=args.binary,
        grep=args.grep,
        include_non_object=args.include_non_object,
    )
    render_heap_trend(rows, meta=meta, top_n=args.top, monotonic_only=args.monotonic)


def run_find(args):
    """Scan consecutive ledger-close pairs for delta predicates."""
    dir_path = _resolve_or_exit(args)
    events_path = dir_path / "events.jsonl"
    try:
        threshold = float(args.value)
    except ValueError:
        print(f"error: VALUE must be numeric, got {args.value!r}", file=sys.stderr)
        sys.exit(2)
    matches = find_delta_matches(events_path, args.field, args.op, threshold)
    render_find_results(args.field, args.op, threshold, matches)


def run_attach_mode(args):
    """Run in attach mode - connect to a running process"""
    if getattr(args, "debug", False):
        debug_dump_process_discovery()

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
        websocket_max_retries=args.ws_max_retries,
        websocket_retry_delay_seconds=args.ws_retry_delay,
        use_vmmap=not args.no_vmmap,
        breakdown_interval_seconds=args.breakdown_interval,
        fresh_session=args.fresh,
        heap_every_ledger=args.heap_every_ledger,
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
    monitor_parser.add_argument(
        "--ws-max-retries",
        type=int,
        default=5,
        help="Max websocket connection attempts before giving up (0 = retry forever, default: 5)",
    )
    monitor_parser.add_argument(
        "--ws-retry-delay",
        type=int,
        default=5,
        help="Seconds between websocket connection attempts (default: 5)",
    )
    monitor_parser.add_argument(
        "--no-vmmap",
        action="store_true",
        help="Skip shelling out to vmmap on macOS (faster but loses per-file breakdown)",
    )
    monitor_parser.add_argument(
        "--breakdown-interval",
        type=int,
        default=15,
        help="Seconds between full memory breakdown refreshes (default: 15)",
    )
    monitor_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Move any existing session dir aside (.bak-<ts>) and start clean "
        "— no reattach hydration.",
    )
    monitor_parser.add_argument(
        "--heap-every-ledger",
        type=int,
        default=0,
        metavar="N",
        help="Run macOS heap(1) on every Nth ledger once synced; top-50 classes "
        "land in the snapshot (default: 0 = off, heap suspends target)",
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
    attach_parser.add_argument(
        "--ws-max-retries",
        type=int,
        default=5,
        help="Max websocket connection attempts before giving up (0 = retry forever, default: 5)",
    )
    attach_parser.add_argument(
        "--ws-retry-delay",
        type=int,
        default=5,
        help="Seconds between websocket connection attempts (default: 5)",
    )
    attach_parser.add_argument(
        "--debug",
        action="store_true",
        help="Dump process discovery internals before attaching (useful on Linux)",
    )
    attach_parser.add_argument(
        "--no-vmmap",
        action="store_true",
        help="Skip shelling out to vmmap on macOS (faster but loses per-file breakdown)",
    )
    attach_parser.add_argument(
        "--breakdown-interval",
        type=int,
        default=15,
        help="Seconds between full memory breakdown refreshes (default: 15)",
    )
    attach_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Move any existing session dir aside (.bak-<ts>) and start clean "
        "— no reattach hydration.",
    )
    attach_parser.add_argument(
        "--heap-every-ledger",
        type=int,
        default=0,
        metavar="N",
        help="Run macOS heap(1) on every Nth ledger once synced; top-50 classes "
        "land in the snapshot (default: 0 = off, heap suspends target)",
    )

    # Diff command — compare two ledger-close snapshots from events.jsonl
    diff_parser = subparsers.add_parser(
        "diff",
        help="Diff two ledger-close snapshots (memory + counts deltas)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    diff_parser.add_argument("from_ledger", type=int, help="Lower ledger index")
    diff_parser.add_argument("to_ledger", type=int, help="Upper ledger index")
    diff_parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Session dir (defaults to the currently-running rippled's dir, "
        "if exactly one is running)",
    )
    diff_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Root containing session dirs (default: memory_monitor_results)",
    )

    # Heap command — one-shot macOS heap(1) sample of the running rippled.
    heap_parser = subparsers.add_parser(
        "heap",
        help="Run macOS heap(1) on the running rippled and print the top allocs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    heap_parser.add_argument(
        "--pid",
        "-p",
        type=int,
        help="PID to sample (defaults to the unique running rippled)",
    )
    heap_parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="Top N classes by bytes (default: 50)",
    )
    heap_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout instead of a rich table",
    )
    heap_parser.add_argument(
        "--binary",
        type=str,
        default=None,
        help="Only include rows whose binary matches (substring, e.g. 'xrpld')",
    )
    heap_parser.add_argument(
        "--grep",
        type=str,
        default=None,
        help="Only include rows whose class matches this regex (e.g. SHAMap)",
    )

    # heap-trend — walk events.jsonl, aggregate per-class growth + monotonic flag
    trend_parser = subparsers.add_parser(
        "heap-trend",
        help="Per-class heap growth across all heap samples in a session",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    trend_parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Session dir (defaults to the currently-running rippled's dir)",
    )
    trend_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Root containing session dirs (default: memory_monitor_results)",
    )
    trend_parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="Top N rows to show, sorted by Δbytes desc (default: 50)",
    )
    trend_parser.add_argument(
        "--monotonic",
        action="store_true",
        help="Show only classes that never decreased across samples",
    )
    trend_parser.add_argument(
        "--binary",
        type=str,
        default=None,
        help="Only aggregate rows whose binary matches (substring)",
    )
    trend_parser.add_argument(
        "--grep",
        type=str,
        default=None,
        help="Only aggregate rows whose class matches this regex",
    )
    trend_parser.add_argument(
        "--include-non-object",
        action="store_true",
        help="Include the 'non-object' (raw malloc, typeless) bucket. Often "
        "dominant in class view — hidden by default so it doesn't swamp "
        "per-class signal, but worth surfacing when class-level trends look flat.",
    )

    # Summary command — one-screen triage view of a session dir
    summary_parser = subparsers.add_parser(
        "summary",
        help="Summarise a session dir (sessions, span, net memory, top movers)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    summary_parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Session dir (defaults to the currently-running rippled's dir)",
    )
    summary_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Root containing session dirs (default: memory_monitor_results)",
    )
    summary_parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Top N count movers to show (default: 10, 0 to skip)",
    )

    # Find command — scan consecutive ledger pairs for a delta predicate.
    # Dotted field paths supported: counts.AL_size, memory_breakdown.anonymous_mb.
    find_parser = subparsers.add_parser(
        "find",
        help="Find consecutive ledger pairs where Δ(field) satisfies a predicate",
        description="Scan consecutive ledger-close snapshot pairs and print pairs "
        "where the delta of FIELD satisfies OP VALUE.\n"
        "Examples:\n"
        "  xahaud-monitor find rss_mb '>' 2\n"
        "  xahaud-monitor find counts.AL_size '>' 1000\n"
        "  xahaud-monitor find memory_breakdown.anonymous_mb abs> 5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    find_parser.add_argument(
        "field",
        type=str,
        help="Dotted path (rss_mb, counts.AL_size, memory_breakdown.anonymous_mb) "
        "or derived: heap_mb (rss minus mmap), pool_current_mb, pool_peak_mb, "
        "pool_wasted_mb (tagged_pointer_pools._total.*_bytes in MB)",
    )
    find_parser.add_argument(
        "op",
        type=str,
        choices=[">", ">=", "<", "<=", "==", "!=", "abs>", "abs>=", "abs<", "abs<="],
        help="Comparison operator (use abs> for magnitude)",
    )
    find_parser.add_argument(
        "value",
        type=str,
        help="Numeric threshold to compare delta against",
    )
    find_parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Session dir (defaults to the currently-running rippled's dir)",
    )
    find_parser.add_argument(
        "--output-dir",
        type=str,
        default="memory_monitor_results",
        help="Root containing session dirs (default: memory_monitor_results)",
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

    if args.command == "diff":
        run_diff(args)
        return

    if args.command == "find":
        run_find(args)
        return

    if args.command == "summary":
        run_summary(args)
        return

    if args.command == "heap":
        run_heap(args)
        return

    if args.command == "heap-trend":
        run_heap_trend(args)
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
        websocket_max_retries=args.ws_max_retries,
        websocket_retry_delay_seconds=args.ws_retry_delay,
        use_vmmap=not args.no_vmmap,
        breakdown_interval_seconds=args.breakdown_interval,
        fresh_session=args.fresh,
        heap_every_ledger=args.heap_every_ledger,
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
