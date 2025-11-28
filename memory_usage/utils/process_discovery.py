"""
Process discovery utilities for finding running xahaud/rippled processes
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil


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
    """Find all running xahaud/rippled processes"""
    processes = []

    for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "exe"]):
        try:
            name = proc.info["name"] or ""
            if not ("rippled" in name.lower() or "xahaud" in name.lower()):
                continue

            pid = proc.info["pid"]
            cmdline = proc.info["cmdline"] or []
            mem_info = proc.info["memory_info"]
            exe = proc.info["exe"] or (cmdline[0] if cmdline else "unknown")

            # Extract config path from cmdline
            config_path = None
            for i, arg in enumerate(cmdline):
                if arg in ("--conf", "-c") and i + 1 < len(cmdline):
                    config_path = cmdline[i + 1]
                    break

            # Get working directory
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

        # Extract config path from cmdline
        config_path = None
        for i, arg in enumerate(cmdline):
            if arg in ("--conf", "-c") and i + 1 < len(cmdline):
                config_path = cmdline[i + 1]
                break

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


def display_process_menu(processes: list[DiscoveredProcess]) -> Optional[DiscoveredProcess]:
    """Display a menu of processes and let user select one"""
    if not processes:
        print("No running xahaud/rippled processes found.")
        return None

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

    while True:
        try:
            choice = input(f"Select process [1-{len(processes)}] (or 'q' to quit): ").strip()
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
