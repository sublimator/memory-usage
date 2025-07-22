# Xahaud Memory Monitor

A memory usage monitoring tool for xahaud/rippled binaries with an integrated Textual-based dashboard.

## Features

- Monitor memory usage of multiple rippled/xahaud binaries
- Real-time memory tracking with ledger synchronization
- Beautiful terminal dashboard using Textual with:
  - Live memory graph with time windows
  - Internal diagnostics (get_counts) display
  - Job types monitoring from server_info
  - Ledger synchronization status with counts
- Export comprehensive results to JSON format including:
  - Memory snapshots with timestamps
  - Diagnostic counts and job types at each snapshot
  - System information and test configuration
- Support for both network and standalone modes

## Installation

```bash
poetry install
```

## Usage

Run memory monitoring tests with the integrated dashboard:

```bash
# Run with specified config file
poetry run xahaud-monitor --config path/to/xahaud.cfg

# Run in standalone mode
poetry run xahaud-monitor --config path/to/xahaud.cfg --standalone

# Specify binaries to test
poetry run xahaud-monitor --config path/to/xahaud.cfg --binaries rippled-compact rippled-normal

# Custom test duration (in minutes)
poetry run xahaud-monitor --config path/to/xahaud.cfg --duration 10

# List available binaries
poetry run xahaud-monitor --list
```

### Dashboard Features

The integrated dashboard displays:
- Real-time memory usage with graphs
- Ledger synchronization progress
- Internal server diagnostics
- Job queue statistics
- Process output logs

## Configuration

The tool reads xahaud/rippled configuration files to auto-detect ports and settings. You can override these with command-line options:

- `--config`: Path to configuration file
- `--websocket-url`: Override WebSocket URL
- `--api-version`: Specify API version (1 or 2)

## Development

```bash
# Install development dependencies
poetry install

# Run tests
poetry run pytest

# Format code
poetry run black memory_usage/
poetry run ruff check memory_usage/
```