# Memory Monitor Repository Overview

## Directory Structure

```
memory-usage/
├── memory_usage/           # Main package
│   ├── managers/          # State and lifecycle management
│   │   ├── state_manager.py      # Central state management (observer pattern)
│   │   ├── process_manager.py    # Process lifecycle and binary discovery
│   │   └── websocket_manager.py  # WebSocket connection management
│   │
│   ├── services/          # Business logic
│   │   ├── monitoring_service.py # STUB - Should orchestrate monitoring
│   │   └── process_service.py    # Process wrapper with output callbacks
│   │
│   ├── models/            # Data models
│   │   └── memory_models.py      # Pydantic models (MemorySnapshot, etc.)
│   │
│   ├── utils/             # Helper functions
│   │   ├── parsers.py            # Config parsing, ledger range parsing
│   │   └── formatters.py         # Display formatting
│   │
│   ├── ui/                # User interface
│   │   ├── components/           # Reusable widgets
│   │   │   ├── status_bar.py     # Top status bar (reactive)
│   │   │   └── log_viewer.py     # Log display widgets
│   │   └── dashboard.py          # Main Textual app
│   │
│   ├── monitoring.py      # OLD: Complete working implementation (1000+ lines)
│   ├── container.py       # Dependency injection setup
│   ├── config.py          # Configuration (pydantic BaseSettings)
│   └── cli.py             # Entry point
│
├── pyproject.toml         # Poetry dependencies
├── memory_monitor.log     # Log output
└── memory_monitor_results/# JSON test results
```

## How the OLD Code Works (monitoring.py)

### Core Classes
1. **MemoryMonitor** - Main orchestrator
   - Finds binaries to test
   - Runs tests sequentially
   - Manages test lifecycle
   - Saves results as JSON

2. **RippledProcess** - Process wrapper
   - Starts/stops rippled
   - Captures stdout/stderr in threads
   - Monitors memory usage
   - Detects crashes

3. **Data Models** (Pydantic)
   - MemorySnapshot - Point in time measurement
   - BinaryTestResult - Complete test results
   - SystemInfo - System details
   - TestConfiguration - Test settings

### Test Flow
```
1. Find binaries (or use specified list)
2. For each binary:
   a. Start rippled process
   b. Connect to WebSocket
   c. POLLING PHASE:
      - Poll server_info every 4 seconds
      - Wait for complete_ledgers != "empty"
      - Record memory during polling
   d. MONITORING PHASE:
      - Subscribe to ledger events
      - Record memory on each ledger close
      - Continue for test duration
   e. Save results to JSON
   f. Stop process, cleanup
3. Move to next binary
```

## How the NEW Code is Organized

### Dependency Injection Flow
```
CLI → Container → Dashboard → Services/Managers
         ↓
      Config object
         ↓
    All components
```

### Component Responsibilities

#### Managers (Singletons)
- **StateManager**: Central state store with observer pattern
  - All UI updates flow through here
  - Components subscribe to changes
  - Thread-safe with asyncio locks

- **ProcessManager**: Manages rippled processes
  - Binary discovery
  - Start/stop processes
  - Memory stats collection

- **WebSocketManager**: WebSocket lifecycle
  - Connection management
  - Auto-reconnection
  - Message routing

#### Services
- **MonitoringService**: Should orchestrate the test flow
  - Currently just a STUB!
  - Should use MemoryMonitor internally
  - Bridge between old and new code

- **ProcessService**: Enhanced process with callbacks
  - Wraps subprocess.Popen
  - Output callbacks for stdout/stderr
  - Memory monitoring

#### UI Components
- **Dashboard**: Main Textual app
  - Sets up layout
  - Starts monitoring
  - Processes queues

- **StatusBar**: Reactive status display
  - Subscribes to StateManager
  - Auto-updates on state changes

- **LogViewers**: Queued log display
  - MonitorLogViewer - for monitoring output
  - ProcessOutputViewer - for rippled output

## Data Flow

### Current (BROKEN):
```
Dashboard.start_test()
    ↓
MonitoringService.start_monitoring()  [STUB!]
    ↓
Nothing happens...
```

### Should Be:
```
Dashboard.start_test()
    ↓
MonitoringService.start_monitoring()
    ↓
MemoryMonitor.run_all_tests()
    ↓
For each update:
    StateManager.update_xxx()
        ↓
    Notify observers
        ↓
    StatusBar updates
    LogViewers update
```

## Key Integration Points

### 1. Process Output
```python
# OLD: RippledProcess captures in thread
# NEW: ProcessService has callbacks

# Need to connect:
ProcessService.add_stdout_callback(dashboard.process_output.queue_stdout)
```

### 2. State Updates
```python
# OLD: MemoryMonitor logs directly
# NEW: Updates flow through StateManager

# Need adapter:
When MemoryMonitor logs → StateManager.update_status()
When memory collected → StateManager.update_memory_stats()
```

### 3. WebSocket Events
```python
# OLD: MemoryMonitor handles directly
# NEW: WebSocketManager routes messages

# Need to connect:
WebSocketManager.add_message_handler(monitoring_service.handle_ledger_event)
```

## Configuration

### CLI Arguments
- `--duration` - Test duration per binary
- `--binaries` - Specific binaries to test
- `--list` - List available binaries
- `--config` - Rippled config file
- `--websocket-url` - Override WebSocket URL
- `--api-version` - API version (1 for Xahau, 2 for XRPL)
- `--standalone` - Run in standalone mode
- `--build-dir` - Where to find binaries
- `--output-dir` - Where to save results

### Config Object
All settings flow through the Config object (pydantic BaseSettings):
- Validated on creation
- Passed via DI to all components
- Single source of truth

## Testing Different Scenarios

### Test Single Binary
```bash
xahaud-monitor -b rippled-test --duration 10
```

### Test All Binaries
```bash
xahaud-monitor --duration 5
```

### List Available Binaries
```bash
xahaud-monitor --list
```

### Custom Config
```bash
xahaud-monitor --config my-config.cfg --websocket-url ws://localhost:6009
```

## Output

### Console (Dashboard)
- Top panel: Monitor logs
- Bottom panel: Rippled output
- Status bar: Memory, ledgers, status

### Files
- `memory_monitor.log` - All logging
- `memory_monitor_results/TIMESTAMP/binary-name.json` - Detailed results

## Current Issues

1. **MonitoringService is a stub** - Doesn't actually monitor
2. **Process output not connected** - Nothing shows in bottom panel
3. **No state updates** - Status bar never changes
4. **Can't copy from logs** - Need selection support
5. **WebSocket events not handled** - No ledger updates

## Quick Test

To verify the structure is working:
```python
# In monitoring_service.py, add:
async def start_monitoring(self, binaries):
    logger.info(f"Found {len(binaries)} binaries")
    await self.state_manager.update_status("Testing...")
    # Should see status change in UI
```