# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python tool that spawns xahaud/rippled binaries sequentially, monitors their memory usage during ledger sync and steady-state operation, and renders a live Textual TUI dashboard. Each binary's run produces a JSON report in `memory_monitor_results/<timestamp>/` for cross-binary comparison.

## Commands

Install and run (uv is current; `poetry.lock` is legacy — prefer `uv`):

```bash
uv tool install --python 3.14 -e .
xahaud-monitor monitor -c path/to/xahaud.cfg           # spawn + monitor binaries
xahaud-monitor monitor -c cfg -b rippled-compact -d 1  # one binary, 1 min
xahaud-monitor monitor --list                          # list binaries in build_dir
xahaud-monitor attach                                  # attach to running process (interactive PID picker)
xahaud-monitor attach -p 12345
xahaud-monitor logs                                    # tail latest .memory-usage/*.log
```

The CLI uses argparse subcommands — `monitor`, `attach`, `logs` must come **before** any flags. Bare `xahaud-monitor` (no subcommand) falls back to `monitor` via a re-parse; as soon as you add a flag, the subcommand name is required.

Entry point: `memory_usage.cli:run`. Requires Python 3.13+.

Quality gates (dev extras: `uv sync --extra dev`):

```bash
ruff check memory_usage/
black memory_usage/
mypy memory_usage/
pytest                                      # no tests exist yet
```

## Architecture

Three patterns compose the app — understand these before making changes:

- **DI container** (`memory_usage/container.py`): `dependency_injector.DeclarativeContainer` wires singletons (`ProcessManager`, `StateManager`, `WebSocketManager`, `LoggingService`) and a factory for `MonitoringService`. `Config` is bound at startup in `cli.py` via `container.config.override(...)`, then wired into the `Dashboard` module so `@inject` on its `__init__` resolves `Provide[Container.x]`.
- **Observer state** (`managers/state_manager.py`): `StateManager` holds the single `ApplicationState` dataclass. All mutations take `async with self._lock` and then `await self._notify_observers()`. UI widgets subscribe via `state_manager.subscribe(callback)` — they never read managers directly.
- **Managers vs services**: managers own long-lived resources (processes, sockets, state). Services (`monitoring_service`, `process_service`, `logging_service`) orchestrate workflow and are the only layer that calls multiple managers.

## Test lifecycle

For each binary in `build_dir` (or `--binaries`), `MonitoringService` runs:

1. **Start** — `ProcessManager` spawns rippled with the given config; stdout/stderr captured via threads and forwarded through `ProcessService` callbacks.
2. **Polling** — wait for ledger close events over WebSocket to confirm sync.
3. **Monitoring** — for `test_duration_minutes`, snapshot memory **on every ledger close** (not on a timer). Periodically poll `server_info`, `get_counts`, `catalogue_status` for diagnostic fields on the snapshot.
4. **Finalize** — compute summary stats, write JSON, stop process, disconnect WS, move to next binary.

State progression: `Initializing → Starting → Polling → Synced → Monitoring → Completed`.

## Conventions

- Use `LoggingService`, not the stdlib `logging` module — the UI log viewer subscribes to its callbacks and relies on caller file/line capture.
- New diagnostic metrics: extend `ApplicationState`, collect in `MonitoringService`, render in a `ui/components/` widget, and add to `MemorySnapshot` if it should be in the time series.
- Import cycles: use `TYPE_CHECKING` guards — managers and services reference each other's types heavily.
- Line length 100 (`pyproject.toml`). Ruff rules: `E, F, I, N, W` with `E501` ignored.

## Config

`Config` (`memory_usage/config.py`, Pydantic settings) auto-detects WS port and API version (1=Xahau, 2=XRPL) from the rippled config file. CLI flags override. Default config path: `niq-conf/xahaud.cfg`.
