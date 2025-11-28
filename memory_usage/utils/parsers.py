"""
Parsing utilities for the memory monitor
"""

import configparser
from pathlib import Path
from typing import Optional, Tuple
import logging


logger = logging.getLogger(__name__)


def detect_xahau(config_path: str) -> bool:
    """Detect if this is a Xahau configuration based on file name or content."""
    # Check file name
    if "xahau" in config_path.lower():
        return True

    # Check file content
    try:
        if Path(config_path).exists():
            with open(config_path, "r") as f:
                content = f.read().lower()
                if "xahau" in content:
                    return True
    except Exception:
        pass

    return False


def parse_rippled_config(config_path: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse rippled configuration file to extract websocket and RPC ports.
    Returns (websocket_port, rpc_port) or (None, None) if not found."""
    try:
        if not Path(config_path).exists():
            return None, None

        config = configparser.ConfigParser()
        # Read the file with custom parsing to handle the rippled config format
        with open(config_path, "r") as f:
            config_str = f.read()

        # rippled uses a custom format, we need to parse it manually
        current_section = None
        sections = {}

        for line in config_str.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Check if this is a section header
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                if current_section not in sections:
                    sections[current_section] = {}
            elif current_section and "=" in line:
                # Parse key = value
                key, value = line.split("=", 1)
                sections[current_section][key.strip()] = value.strip()
            elif current_section:
                # Single value line (like port numbers)
                if line.replace(" ", "").replace(".", "").replace("_", "").isalnum():
                    if "values" not in sections[current_section]:
                        sections[current_section]["values"] = []
                    sections[current_section]["values"].append(line)

        ws_port = None
        rpc_port = None

        # Look for websocket admin port (port_ws_admin_local)
        if "port_ws_admin_local" in sections:
            port_section = sections["port_ws_admin_local"]
            if "port" in port_section:
                ws_port = int(port_section["port"])

        # Fallback to public websocket port
        if not ws_port and "port_ws_public" in sections:
            port_section = sections["port_ws_public"]
            if "port" in port_section:
                ws_port = int(port_section["port"])

        # Look for RPC admin port (port_rpc_admin_local)
        if "port_rpc_admin_local" in sections:
            port_section = sections["port_rpc_admin_local"]
            if "port" in port_section:
                rpc_port = int(port_section["port"])

        # Fallback to public RPC port
        if not rpc_port and "port_rpc_public" in sections:
            port_section = sections["port_rpc_public"]
            if "port" in port_section:
                rpc_port = int(port_section["port"])

        return ws_port, rpc_port

    except Exception as e:
        logger.warning(f"Failed to parse rippled config file: {e}")
        return None, None


def parse_debug_logfile(config_path: str) -> Optional[str]:
    """Parse rippled config to find the debug log file path.

    Args:
        config_path: Path to the rippled config file

    Returns:
        Absolute path to the debug log file, or None if not found
        (relative paths are resolved relative to the config file's directory)
    """
    try:
        config_file = Path(config_path)
        if not config_file.exists():
            return None

        with open(config_file, "r") as f:
            content = f.read()

        # Parse the [debug_logfile] section
        in_section = False
        for line in content.split("\n"):
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            # Check if entering the section
            if line == "[debug_logfile]":
                in_section = True
                continue

            # Check if leaving the section
            if line.startswith("["):
                in_section = False
                continue

            # If we're in the section, the first non-comment line is the path
            if in_section:
                log_path = Path(line)

                # If it's an absolute path, use it directly
                if log_path.is_absolute():
                    return str(log_path)

                # Relative paths in rippled config are relative to the config file's directory
                resolved = config_file.parent / log_path
                return str(resolved.resolve())

    except Exception as e:
        logger.debug(f"Error parsing debug_logfile from config: {e}")

    return None


def parse_ledger_ranges(complete_ledgers: str) -> int:
    """Parse complete_ledgers range set and return total count.

    Handles formats like:
    - "empty" -> 0
    - "100-200" -> 101
    - "100-200,300-400" -> 202
    - "100-200,300-400,500-600" -> 303
    """
    if not complete_ledgers or complete_ledgers == "empty":
        return 0

    total_count = 0
    try:
        # Split by comma to get individual ranges
        ranges = complete_ledgers.split(",")

        for range_str in ranges:
            range_str = range_str.strip()
            if "-" in range_str:
                parts = range_str.split("-")
                if len(parts) == 2:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                    # Include both start and end in count
                    total_count += end - start + 1
    except Exception as e:
        logger.debug(f"Error parsing ledger ranges '{complete_ledgers}': {e}")
        return 0

    return total_count
