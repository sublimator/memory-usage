# Migration Guide: Getting the Monitor Working

## Quick Fix (5 minutes)

To get everything working quickly, update `services/monitoring_service.py`:

```python
# Replace the stub implementation with:

from ..monitoring import MemoryMonitor
import logging

logger = logging.getLogger(__name__)

class MonitoringService:
    def __init__(self, config, state_manager, process_manager, websocket_manager):
        self.config = config
        self.state_manager = state_manager
        self.process_manager = process_manager
        self.websocket_manager = websocket_manager
        
        # Create the actual monitor
        self.monitor = MemoryMonitor(config)
        
        # Hook up logging to update state
        self._setup_logging_bridge()
    
    def _setup_logging_bridge(self):
        """Bridge logging to state updates"""
        class StateUpdateHandler(logging.Handler):
            def __init__(self, state_manager):
                super().__init__()
                self.state_manager = state_manager
                
            def emit(self, record):
                # Update status based on log messages
                msg = record.getMessage()
                if "Starting test" in msg:
                    asyncio.create_task(
                        self.state_manager.update_status(msg)
                    )
                # Add more patterns as needed
        
        handler = StateUpdateHandler(self.state_manager)
        logger.addHandler(handler)
    
    async def start_monitoring(self, binaries):
        """Actually run the monitor!"""
        # Update UI state
        await self.state_manager.update_test_progress(0, len(binaries))
        await self.state_manager.update_status("Starting tests...")
        
        # Run the actual monitor
        await self.monitor.run_all_tests()
        
        # Update final state
        await self.state_manager.update_status("All tests completed")
```

## Proper Integration (1-2 hours)

### Step 1: Update ProcessManager to use RippledProcess

```python
# In managers/process_manager.py
from ..monitoring import RippledProcess

async def start_process(self, binary_path: str, name: str):
    # Use the working RippledProcess
    self.current_process = RippledProcess(binary_path, name)
    
    # Hook up output to dashboard
    # ... add callback setup ...
```

### Step 2: Create State Update Adapter

```python
# In services/monitoring_adapter.py
class MonitoringAdapter:
    """Adapts MemoryMonitor events to StateManager updates"""
    
    def __init__(self, monitor, state_manager):
        self.monitor = monitor
        self.state_manager = state_manager
        
        # Monkey-patch monitor methods to add state updates
        self._wrap_methods()
    
    def _wrap_methods(self):
        # Wrap key methods to update state
        original_create_snapshot = self.monitor._create_memory_snapshot
        
        def wrapped_create_snapshot(*args, **kwargs):
            snapshot = original_create_snapshot(*args, **kwargs)
            # Update state with memory info
            asyncio.create_task(
                self.state_manager.update_memory_stats(
                    snapshot.rss_mb,
                    snapshot.memory_percent,
                    snapshot.num_threads
                )
            )
            return snapshot
        
        self.monitor._create_memory_snapshot = wrapped_create_snapshot
```

### Step 3: Fix Process Output Capture

```python
# In dashboard.py
def _setup_process_output(self):
    """Set up process output capture"""
    
    # Create a custom output handler
    def redirect_output():
        # Monkey-patch RippledProcess._capture_output_thread
        from ..monitoring import RippledProcess
        
        original_capture = RippledProcess._capture_output_thread
        
        def capture_with_redirect(self):
            # ... existing capture code ...
            # Add: self.dashboard.process_output.queue_stdout(line)
        
        RippledProcess._capture_output_thread = capture_with_redirect
```

## Testing the Integration

1. **Check State Updates**:
   ```bash
   xahaud-monitor --duration 1 --binaries build/test-binary
   # Should see status bar updating
   ```

2. **Check Process Output**:
   ```bash
   # Should see rippled output in bottom panel
   ```

3. **Check Memory Updates**:
   ```bash
   # Memory stats should update in status bar
   ```

## Debugging Tips

### Enable Debug Logging
```python
# Add to cli.py
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Check State Flow
```python
# In state_manager.py
async def update_status(self, status: str):
    print(f"STATE UPDATE: {status}")  # Debug
    # ... rest of method
```

### Monitor WebSocket
```python
# In websocket_manager.py
async def _message_loop(self):
    async for message in self.client:
        print(f"WS MESSAGE: {message.get('type')}")  # Debug
        # ... rest of method
```

## Common Issues

### Nothing Happens
- Check if MonitoringService.start_monitoring is actually called
- Add logging to verify flow

### No Process Output
- Check if RippledProcess output capture is working
- Verify callbacks are connected

### Status Bar Not Updating
- Check if StateManager observers are registered
- Verify state updates are being called

### Can't Find Binaries
- Check build_dir path
- Verify binary names match pattern

## Full Working Example

See `monitoring.py` for the complete working implementation that needs to be integrated.