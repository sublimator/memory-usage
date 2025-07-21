# Next Steps for Memory Monitor Refactoring

## Current State

### What Was Done
1. **Created modular structure with DI**:
   - ✅ Dependency injection container (`container.py`)
   - ✅ Managers for state, process, and websocket
   - ✅ UI components (StatusBar, LogViewers)
   - ✅ Utils for parsing and formatting
   - ✅ Models separated into their own module
   - ✅ Clean CLI with --list command restored

2. **What's Actually Working**:
   - CLI argument parsing
   - --list command
   - Dashboard launches
   - DI container wiring
   - Basic UI layout

3. **What's NOT Working**:
   - ❌ MonitoringService is just a stub!
   - ❌ Nothing actually monitors memory
   - ❌ Process output capture not hooked up
   - ❌ WebSocket monitoring not connected
   - ❌ Status bar never updates

## The Problem

I created `services/monitoring_service.py` as a stub with this structure:
```python
async def start_monitoring(self, binaries: List[str]):
    # Just updates state, doesn't actually monitor!
    
def _initialize_binary_result(...):
    # Implementation similar to original
    pass  # BUT IT'S JUST "pass"!
```

Meanwhile, the ACTUAL implementation is in `monitoring.py` with the `MemoryMonitor` class that has 1000+ lines of working code!

## What Needs to Be Done

### 1. Integrate Real Monitoring Logic
The `monitoring.py` file contains the complete working `MemoryMonitor` class. We need to:

a) **Option 1**: Port MemoryMonitor methods into MonitoringService
b) **Option 2**: Have MonitoringService use MemoryMonitor internally
c) **Option 3**: Refactor MemoryMonitor to use our new managers

### 2. Fix Process Output Capture
The `RippledProcess` in `monitoring.py` has working output capture. We need to:
- Either use `RippledProcess` directly
- Or update `ProcessService` to actually implement output callbacks
- Hook up stdout/stderr to the dashboard's ProcessOutputViewer

### 3. Connect WebSocket Updates
Currently nothing updates the status bar because:
- MonitoringService doesn't actually connect WebSocket
- No ledger events are being processed
- State updates aren't happening

### 4. Make Logs Copyable
Need to add selection support to RichLog widgets:
```python
# In log_viewer.py
self.can_focus = True
self.mouse_over = True
# Possibly switch to TextLog with selection
```

## Implementation Plan

### Step 1: Fix MonitoringService
```python
# In monitoring_service.py
from ..monitoring import MemoryMonitor

class MonitoringService:
    def __init__(self, ...):
        # Create internal MemoryMonitor
        self.monitor = MemoryMonitor(config)
        
    async def start_monitoring(self, binaries):
        # Actually use the monitor!
        await self.monitor.run_all_tests()
```

### Step 2: Bridge Old and New
Create adapters to connect old MemoryMonitor to new managers:
- When MemoryMonitor updates state → update StateManager
- When MemoryMonitor creates process → use ProcessManager
- When MemoryMonitor connects WebSocket → use WebSocketManager

### Step 3: Process Output Hooks
```python
# In process_manager.py
def start_process(self, ...):
    process = ProcessService(...)  # Use the one with callbacks
    process.add_stdout_callback(self._on_stdout)
    process.add_stderr_callback(self._on_stderr)
```

### Step 4: State Updates
Ensure all state changes flow through StateManager:
- Memory stats
- Ledger info
- Process status
- Test progress

## Files to Modify

1. **`services/monitoring_service.py`** - Add real implementation
2. **`managers/process_manager.py`** - Use ProcessService with callbacks
3. **`ui/dashboard.py`** - Fix process output hooks
4. **`ui/components/log_viewer.py`** - Enable text selection

## Context for Next Session

### Key Classes to Understand
- `monitoring.py:MemoryMonitor` - The actual monitoring logic (1000+ lines)
- `monitoring.py:RippledProcess` - Process management with output capture
- `services/process_service.py:ProcessService` - New process wrapper with callbacks

### Critical Methods in MemoryMonitor
- `run_all_tests()` - Main entry point
- `test_binary()` - Tests single binary
- `polling_phase()` - Waits for ledgers
- `monitoring_phase()` - Actual monitoring
- `_create_memory_snapshot()` - Creates data points
- `_save_binary_result()` - Saves JSON output

### State Flow
1. CLI creates Config
2. Container injects dependencies
3. Dashboard starts MonitoringService
4. MonitoringService should use MemoryMonitor
5. MemoryMonitor updates should flow to StateManager
6. StateManager notifies UI components
7. StatusBar and logs update automatically

## Quick Fix to Get It Working

```python
# In monitoring_service.py
async def start_monitoring(self, binaries: List[str]):
    # Just use the old monitor directly for now
    from ..monitoring import MemoryMonitor
    monitor = MemoryMonitor(self.config)
    
    # Hook up state updates
    # ... adapter code here ...
    
    await monitor.run_all_tests()
```