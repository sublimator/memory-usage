# Xahaud Memory Monitor

A memory usage monitoring tool for xahaud/rippled binaries with a Textual-based dashboard.

## Features

- Monitor memory usage of multiple rippled/xahaud binaries
- Real-time memory tracking with ledger synchronization
- Beautiful terminal dashboard using Textual
- Export results to JSON format
- Support for both network and standalone modes

## Installation

```bash
poetry install
```

## Usage

### Command Line Interface

Run memory monitoring tests:

```bash
# Run with default settings
poetry run xahaud-monitor

# Run in standalone mode
poetry run xahaud-monitor --standalone

# Specify binaries to test
poetry run xahaud-monitor --binaries rippled-compact rippled-normal

# Custom test duration (in minutes)
poetry run xahaud-monitor --duration 10
```

### Dashboard

Launch the interactive dashboard:

```bash
poetry run xahaud-dashboard
```

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