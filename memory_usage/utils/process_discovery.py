"""
Process discovery utilities for finding running xahaud/rippled processes
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import psutil

from .parsers import parse_debug_logfile

# Binary name prefixes we recognize (order matters only for docs)
_RIPPLED_NAME_PREFIXES = ("rippled", "xahaud", "xrpld")


def _looks_like_rippled(name: str, cmdline: List[str]) -> bool:
    """Return True if `name` or the cmdline exe basename looks like a rippled binary.

    Linux truncates /proc/PID/comm to 15 chars, so we also consult cmdline[0]
    for long binary names (e.g. `rippled-compact-exact`).
    """
    lowered = name.lower()
    # Exclude our own entrypoint and anything similar
    if "monitor" in lowered:
        return False

    candidates = [lowered]
    if cmdline:
        candidates.append(os.path.basename(cmdline[0]).lower())

    return any(c.startswith(_RIPPLED_NAME_PREFIXES) for c in candidates)


def _extract_config_path(cmdline: List[str]) -> Optional[str]:
    """Parse `--conf PATH`, `-c PATH`, `--conf=PATH`, `-c=PATH` from a cmdline."""
    for i, arg in enumerate(cmdline):
        if arg in ("--conf", "-c") and i + 1 < len(cmdline):
            return cmdline[i + 1]
        if arg.startswith("--conf=") or arg.startswith("-c="):
            return arg.split("=", 1)[1]
    return None


@dataclass
class DiscoveredProcess:
    """Information about a discovered xahaud/rippled process"""

    pid: int
    name: str
    binary_path: str
    config_path: Optional[str]
    working_dir: Optional[str]
    memory_mb: float
    cmdline: list[str]

    @property
    def resolved_config_path(self) -> Optional[str]:
        """Get the fully resolved config path"""
        if not self.config_path:
            return None

        config = Path(self.config_path)
        if config.is_absolute():
            return str(config) if config.exists() else None

        # Try relative to working directory
        if self.working_dir:
            resolved = Path(self.working_dir) / config
            if resolved.exists():
                return str(resolved.resolve())

        # Try relative to current directory
        if config.exists():
            return str(config.resolve())

        return None

    @property
    def debug_logfile_path(self) -> Optional[str]:
        """Get the debug log file path from the config"""
        config = self.resolved_config_path
        if not config:
            return None
        return parse_debug_logfile(config)


def get_process_cwd(pid: int) -> Optional[str]:
    """Get the working directory of a process"""
    try:
        # Try psutil first (works on most platforms)
        proc = psutil.Process(pid)
        return proc.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

    # Fallback to lsof on macOS/Linux
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if "cwd" in line.lower():
                parts = line.split()
                if len(parts) >= 9:
                    return parts[-1]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def find_rippled_processes() -> list[DiscoveredProcess]:
    """Find all running xahaud/rippled/xrpld processes (excluding ourselves)."""
    processes = []
    own_pid = os.getpid()

    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "exe"]):
        try:
            pid = proc.info["pid"]
            if pid == own_pid:
                continue

            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []
            if not _looks_like_rippled(name, cmdline):
                continue

            mem_info = proc.info["memory_info"]
            exe = proc.info["exe"] or (cmdline[0] if cmdline else "unknown")
            config_path = _extract_config_path(cmdline)
            working_dir = get_process_cwd(pid)

            processes.append(
                DiscoveredProcess(
                    pid=pid,
                    name=name,
                    binary_path=exe,
                    config_path=config_path,
                    working_dir=working_dir,
                    memory_mb=mem_info.rss / 1024 / 1024 if mem_info else 0,
                    cmdline=cmdline,
                )
            )

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return processes


def get_process_by_pid(pid: int) -> Optional[DiscoveredProcess]:
    """Get process info for a specific PID"""
    try:
        proc = psutil.Process(pid)
        info = proc.as_dict(attrs=["pid", "name", "cmdline", "memory_info", "exe"])

        name = info["name"] or ""
        cmdline = info["cmdline"] or []
        mem_info = info["memory_info"]
        exe = info["exe"] or (cmdline[0] if cmdline else "unknown")
        config_path = _extract_config_path(cmdline)

        # Get working directory
        working_dir = get_process_cwd(pid)

        return DiscoveredProcess(
            pid=pid,
            name=name,
            binary_path=exe,
            config_path=config_path,
            working_dir=working_dir,
            memory_mb=mem_info.rss / 1024 / 1024 if mem_info else 0,
            cmdline=cmdline,
        )

    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def debug_dump_process_discovery() -> None:
    """Print every process psutil can see, annotated with whether we'd match it.

    Useful on Linux when the attach menu comes up empty even though a rippled
    is clearly running — helps spot permission issues, truncated comms, or
    unexpected binary names.
    """
    own_pid = os.getpid()
    matched = 0
    denied = 0
    total = 0

    print("-" * 80)
    print(f"Process discovery debug (own PID: {own_pid})")
    print(f"  Match prefixes: {_RIPPLED_NAME_PREFIXES}")
    print("  Exclusion: name contains 'monitor'")
    print("-" * 80)

    for proc in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
        total += 1
        try:
            pid = proc.info["pid"]
            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []
            exe = proc.info["exe"] or ""

            # Only bother printing things that look remotely relevant
            lowered = name.lower()
            exe_base = os.path.basename(exe or (cmdline[0] if cmdline else "")).lower()
            if not any(
                tok in (lowered + " " + exe_base)
                for tok in ("rippled", "xahaud", "xrpld", "monitor")
            ):
                continue

            tag = "MATCH " if _looks_like_rippled(name, cmdline) and pid != own_pid else "skip  "
            if pid == own_pid:
                tag = "self  "
            if tag.startswith("MATCH"):
                matched += 1

            print(f"  [{tag}] pid={pid} name={name!r} exe={exe_base!r}")
            if cmdline:
                joined = " ".join(cmdline)
                if len(joined) > 100:
                    joined = joined[:97] + "..."
                print(f"             cmdline: {joined}")

        except psutil.AccessDenied:
            denied += 1
            print(f"  [denied] pid={proc.pid} (AccessDenied — try with sudo?)")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    print("-" * 80)
    print(f"Scanned {total} processes, matched {matched}, access-denied {denied}")
    print("-" * 80)


def display_process_menu(processes: list[DiscoveredProcess]) -> Optional[DiscoveredProcess]:
    """Display a menu of processes and let user select one"""
    if not processes:
        print("No running xahaud/rippled processes found.")
        return None

    # If only one process, auto-select it
    if len(processes) == 1:
        proc = processes[0]
        print(f"Found 1 process: {proc.name} (PID: {proc.pid}, {proc.memory_mb:.1f} MB)")
        return proc

    print("\nRunning xahaud/rippled processes:")
    print("-" * 80)

    for i, proc in enumerate(processes, 1):
        config_display = proc.resolved_config_path or proc.config_path or "unknown"
        # Truncate long config paths
        if len(config_display) > 40:
            config_display = "..." + config_display[-37:]

        print(f"  [{i}] PID {proc.pid}: {proc.name}")
        print(f"      Memory: {proc.memory_mb:.1f} MB")
        print(f"      Config: {config_display}")
        print()

    print("-" * 80)

    # Default to first process
    default = 1

    while True:
        try:
            prompt = f"Select process [1-{len(processes)}] (default: {default}, 'q' to quit): "
            choice = input(prompt).strip()

            # Empty input = default
            if not choice:
                return processes[default - 1]

            if choice.lower() == "q":
                return None

            idx = int(choice) - 1
            if 0 <= idx < len(processes):
                return processes[idx]
            print(f"Please enter a number between 1 and {len(processes)}")
        except ValueError:
            print("Invalid input. Please enter a number.")
        except (KeyboardInterrupt, EOFError):
            print()
            return None
