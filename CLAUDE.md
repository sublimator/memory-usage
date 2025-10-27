# Xahaud Memory Monitor Project

## Overview

A Python tool for monitoring memory usage of xahaud/rippled binaries with a real-time Textual TUI dashboard. Tests multiple binaries sequentially, captures memory metrics, ledger synchronization data, and internal diagnostics.

## Purpose

- **Compare memory usage** across different rippled binary builds
- **Track memory during sync** and steady-state operation
- **Collect diagnostic data** (job types, internal counts, catalogue loading)
- **Generate JSON reports** for analysis and comparison

## Architecture

### Design Patterns

- **Dependency Injection**: Uses `dependency-injector` for clean service composition
- **Observer Pattern**: StateManager broadcasts state changes to UI components
- **Manager Pattern**: Centralized managers for processes, WebSockets, and state
- **Service Layer**: Business logic separated from infrastructure concerns

### Component Hierarchy

```
CLI Entry (cli.py)
    ↓
DI Container (container.py)
    ↓
Dashboard (ui/dashboard.py)
    ↓
┌─────────────────┬──────────────────┬─────────────────┐
│ MonitoringService │ ProcessManager   │ WebSocketManager│
└─────────────────┴──────────────────┴─────────────────┘
         ↓                  ↓                   ↓
    StateManager ←─── (Observable State) ───→ UI Components
```

## Directory Structure

```
memory_usage/
├── __init__.py           # Package version
├── cli.py                # Entry point, argument parsing
├── config.py             # Pydantic settings model
├── container.py          # DI container configuration
│
├── managers/             # Resource managers
│   ├── process_manager.py      # Rippled process lifecycle
│   ├── state_manager.py        # Centralized app state (observable)
│   └── websocket_manager.py    # WebSocket connections/subscriptions
│
├── services/             # Business logic
│   ├── monitoring_service.py   # Orchestrates test workflow
│   ├── process_service.py      # Individual process handling
│   └── logging_service.py      # Centralized logging with callbacks
│
├── models/               # Data models
│   └── memory_models.py        # Pydantic models for snapshots/results
│
├── ui/                   # TUI components
│   ├── dashboard.py            # Main Textual app
│   └── components/
│       ├── status_bar.py       # Real-time status display
│       ├── memory_graph.py     # Sparkline memory graph
│       ├── log_viewer.py       # Monitor & process logs
│       ├── counts_display.py   # Internal diagnostics
│       ├── jobs_display.py     # Job types table
│       └── catalogue_status_display.py  # Catalogue loading
│
└── utils/                # Utilities
    ├── parsers.py              # Config/ledger parsing
    └── formatters.py           # Display formatting
```

## Key Components

### Managers (Singleton, Injected)

- **ProcessManager**: Manages rippled processes, finds binaries, captures stdout/stderr
- **StateManager**: Observable state container, notifies UI of changes
- **WebSocketManager**: Handles connections, subscriptions, retries, message routing

### Services

- **MonitoringService**: Orchestrates the test workflow (polling → monitoring → results)
- **ProcessService**: Individual process lifecycle with output callbacks
- **LoggingService**: Centralized logging with handler callbacks for UI integration

### UI Components

- **Dashboard**: Main Textual app, composes all widgets, handles bindings
- **StatusBar**: Reactive display of memory, ledgers, timing
- **MemoryGraph**: Sparkline graph with all-time data
- **LogViewer**: Queued message display for monitor and process logs
- **CountsDisplay**: Formatted table of `get_counts` diagnostics
- **JobsDisplay**: Job types from `server_info`
- **CatalogueStatusDisplay**: Catalogue loading progress

## Workflow

### Test Lifecycle

1. **Initialization**
   - Parse CLI args
   - Create Config object
   - Set up DI container
   - Wire dependencies

2. **Binary Discovery**
   - Find binaries in `build_dir` (or use `specified_binaries`)
   - Validate executability

3. **For Each Binary**
   - **Start Process**: Spawn rippled with config
   - **Polling Phase**: Wait for ledgers to become available (watch for ledger close events)
   - **Monitoring Phase**: Collect data for `test_duration_minutes`
   - **Finalization**: Calculate stats, save JSON results
   - **Cleanup**: Stop process, disconnect WebSocket

4. **Completion**
   - All results saved to `output_dir/timestamp/`
   - Dashboard remains open for review

### State Flow

```
Initializing → Starting Process → Polling (waiting for sync)
    ↓
Synced (ledgers available) → Monitoring (collecting data)
    ↓
Completed → Results Saved
```

### Data Collection

- **Memory Snapshots**: Created on every ledger close event during monitoring
- **Diagnostic Data**: Collected periodically via WebSocket:
  - `server_info` (ledger ranges, job types)
  - `get_counts` (internal metrics)
  - `catalogue_status` (loading progress)

## Configuration

### Config Object (config.py)

```python
rippled_config_path: str      # Path to rippled config
websocket_url: str             # WebSocket endpoint
api_version: int               # 1 (Xahau) or 2 (XRPL)
standalone_mode: bool          # --standalone or --net
test_duration_minutes: int     # Monitoring duration
poll_interval: int             # Seconds between polls
build_dir: str                 # Where to find binaries
output_dir: str                # Where to save results
specified_binaries: List[str]  # Optional specific binaries
```

### CLI Arguments

```bash
xahaud-monitor [options]
  -d, --duration MINUTES       # Test duration (default: 5)
  -b, --binaries NAMES...      # Specific binaries to test
  -c, --config PATH            # Rippled config (default: niq-conf/xahaud.cfg)
  -w, --websocket-url URL      # Override WebSocket URL
  -v, --api-version 1|2        # API version (auto-detected)
  -s, --standalone             # Run in standalone mode
  --build-dir DIR              # Binary directory (default: build)
  --output-dir DIR             # Results directory
  -l, --list                   # List available binaries
```

## Important Conventions

### Dependency Injection

- **All major components** receive dependencies via constructor injection
- **Container wiring** happens in `cli.py` before dashboard starts
- **Use type hints** with `TYPE_CHECKING` to avoid circular imports
- **@inject decorator** on Dashboard `__init__` with `Provide[Container.x]`

### Async Patterns

- **Managers use async/await** with locks for thread safety
- **State updates are async** to allow notification broadcasting
- **WebSocket operations are async** with reconnection logic
- **Textual workers** for background tasks (`run_worker`)

### State Management

- **Single source of truth**: `StateManager.state` (ApplicationState dataclass)
- **Updates are atomic**: Use `async with self._lock`
- **Observers are notified**: `await self._notify_observers()`
- **UI components subscribe**: `state_manager.subscribe(callback)`

### Logging

- **Use LoggingService**, not direct `logging` module (for UI integration)
- **Service automatically captures** file/line info of caller
- **Handlers receive** formatted messages with log levels
- **UI log viewer** subscribes to logging service callbacks

### Output Capture

- **ProcessService** uses threading to capture stdout/stderr
- **Callbacks are registered** via ProcessManager subscriptions
- **UI components queue messages** for batch processing
- **Process queues every 0.1s** to avoid blocking

## Common Tasks

### Adding a New UI Component

1. Create widget in `ui/components/`
2. Add to `dashboard.compose()`
3. Subscribe to state changes if needed
4. Add CSS styling in `Dashboard.CSS`

### Adding a New Diagnostic Metric

1. Extend `ApplicationState` dataclass in `state_manager.py`
2. Add collection logic in `monitoring_service.py`
3. Update UI component to display new data
4. Add to `MemorySnapshot` if capturing over time

### Debugging Process Issues

- Check `ProcessService.start()` for spawn errors
- Verify binary path and permissions
- Check rippled config file exists
- Review process stdout/stderr in dashboard
- Look for "Failed to start" or "crashed" messages

### Testing Locally

```bash
# Install with uv
uv tool install --python 3.14 -e .

# Run with default config
xahaud-monitor

# Run specific binaries
xahaud-monitor -b rippled-compact -b rippled-normal -d 1

# List available binaries
xahaud-monitor --list

# Use custom config
xahaud-monitor -c /path/to/xahaud.cfg -w ws://localhost:6005
```

## Data Models

### MemorySnapshot

Captured on every ledger close during monitoring:
- `timestamp`, `elapsed_seconds`, `monitoring_elapsed_seconds`
- `ledger_index`, `ledger_hash`, `transaction_count`
- `rss_mb`, `vms_mb`, `memory_percent`, `num_threads`
- `complete_ledgers`, `ledger_count`
- `counts`, `job_types` (diagnostic data)

### BinaryTestResult

Complete test result saved to JSON:
- Metadata (binary name/path/size)
- Timing (start/sync/end times, durations)
- Status (completed/crashed/timeout/error)
- Summary stats (final/peak/average memory)
- Transaction/ledger counts
- Test configuration and system info
- All snapshots (time series)
- Process output tails

## Development Notes

### Python Version

- **Requires Python 3.13+** (uses modern type hints, Pydantic 2.x)
- **Tested with 3.14** (latest)

### Key Dependencies

- `textual` - TUI framework
- `xrpl-py` - WebSocket client for XRPL/Xahau
- `psutil` - Process memory monitoring
- `pydantic` - Data validation and settings
- `dependency-injector` - DI framework
- `aiofiles` - Async file I/O (if needed)

### Testing Strategy

- **Manual testing** with real binaries and networks
- **Integration testing** via actual rippled processes
- **No unit tests yet** (TODO: add pytest tests)

### Code Style

- **Line length**: 100 characters (see pyproject.toml)
- **Formatter**: Black
- **Linter**: Ruff with import sorting
- **Type checker**: mypy (strict mode)
- **Import order**: isort

### Performance Considerations

- **Memory graph** limited to 3600 points (1 hour at 1s intervals)
- **Log buffers** limited to 100 lines per stream
- **Queue processing** batched (max 10 messages per cycle)
- **WebSocket reconnection** with exponential backoff (max 5 retries)

## Troubleshooting

### Common Issues

1. **"No binaries found"**
   - Check `build_dir` path
   - Ensure binaries are executable
   - Use `--list` to verify

2. **"WebSocket connection failed"**
   - Verify rippled is running
   - Check `websocket_url` and port
   - Review rippled config for port settings
   - Check API version (1 for Xahau, 2 for XRPL)

3. **"Process crashed during polling"**
   - Check rippled config file path
   - Review process stderr output
   - Verify database paths exist
   - Check disk space

4. **"Sync timeout"**
   - Increase `test_duration_minutes`
   - Check network connectivity
   - Verify peers in rippled config
   - Use `--standalone` for local testing

### Debug Logging

- Set `DEBUG=1` environment variable for verbose output
- Check `memory_monitor_results/` for JSON results
- Review process output in dashboard (bottom panel)
- Monitor log in dashboard (top-left panel)

## Future Enhancements

- [ ] Add unit tests (pytest)
- [ ] Add comparative analysis between binaries
- [ ] Export graphs as images
- [ ] Real-time CSV export
- [ ] Support for multiple concurrent tests
- [ ] Historical data visualization
- [ ] Alerting for memory thresholds
- [ ] Docker support
- [ ] Web-based dashboard alternative

## Project Context

This tool is part of the Xahau development workflow for:
- **Performance testing** across compilation flags
- **Memory regression detection** between commits
- **Optimization validation** for memory-compact builds
- **Long-running stability testing** with real-world data
