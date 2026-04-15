"""
Per-process memory breakdown.

On Linux, this is the only reliable way to tell whether a heap-saving
optimization (e.g. leaf-pack mmap) is actually shifting pages out of
anonymous heap into file-backed mmap — because total RSS doesn't move
when pages are just relocated between backings. The ``Anonymous`` counter
from ``/proc/PID/smaps_rollup`` is the authoritative number for that.

Data sources:
- Linux: ``/proc/PID/smaps_rollup`` for fast aggregates (Anonymous, Pss,
  Private_Dirty, Swap), plus ``/proc/PID/smaps`` for per-path top-N
  file-backed mappings.
- macOS: psutil.memory_full_info — uss ≈ "resident private" ≈ heap;
  rss - uss approximates file-backed/shared. No per-path detail (would
  require shelling out to vmmap).
- Other platforms: supported=False.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

# smaps region header:
#   7f1234567000-7f1234abc000 r-xp 00000000 08:01 12345     /path/to/libsome.so
_HEADER_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]+ [rwxps-]{4} ")

_NODESTORE_SUFFIXES = (".nudb", ".db", ".sst", ".ldb", ".sqlite", ".sqlite-wal")
_SHARED_LIB_MARKERS = (".so", ".dylib")


@dataclass
class MemoryBreakdown:
    """Per-category RSS breakdown for a process."""

    supported: bool = True
    source: str = ""  # "smaps_rollup+smaps", "smaps_rollup", "psutil", ""

    # Authoritative aggregates
    total_rss_mb: float = 0.0
    # The one you care about for heap-saving optimizations:
    anonymous_mb: float = 0.0  # heap, stack, MAP_ANON, brk — the thing that moves
    # Linux-only extras (None elsewhere)
    pss_mb: Optional[float] = None  # proportional set size
    private_dirty_mb: Optional[float] = None  # pages this process has written
    swap_mb: Optional[float] = None  # bytes swapped out

    # Per-category derived from smaps paths (Linux only; macOS has a single
    # file_backed bucket under other_file_mb)
    nodestore_mb: float = 0.0  # .nudb, .sst, .ldb, .db
    shared_lib_mb: float = 0.0  # .so, .dylib
    other_file_mb: float = 0.0  # binaries, misc file-backed

    # (path, rss_mb) — biggest file-backed mappings, sorted desc (Linux only)
    top_files: List[Tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "supported": self.supported,
            "source": self.source,
            "total_rss_mb": round(self.total_rss_mb, 2),
            "anonymous_mb": round(self.anonymous_mb, 2),
            "pss_mb": round(self.pss_mb, 2) if self.pss_mb is not None else None,
            "private_dirty_mb": round(self.private_dirty_mb, 2)
            if self.private_dirty_mb is not None
            else None,
            "swap_mb": round(self.swap_mb, 2) if self.swap_mb is not None else None,
            "nodestore_mb": round(self.nodestore_mb, 2),
            "shared_lib_mb": round(self.shared_lib_mb, 2),
            "other_file_mb": round(self.other_file_mb, 2),
            "top_files": [(p, round(v, 2)) for p, v in self.top_files],
        }


def _categorize(path: str) -> str:
    """Return the bucket name for a given mapping path."""
    if not path or path.startswith("["):
        return "anon"
    lowered = path.lower()
    if lowered.endswith(_NODESTORE_SUFFIXES):
        return "nodestore"
    if any(marker in lowered for marker in _SHARED_LIB_MARKERS):
        return "shared_lib"
    return "other_file"


def _parse_smaps_rollup(pid: int) -> Dict[str, float]:
    """Parse ``/proc/PID/smaps_rollup`` and return aggregate KB counters."""
    path = Path(f"/proc/{pid}/smaps_rollup")
    if not path.exists():
        return {}
    out: Dict[str, float] = {}
    try:
        with path.open("r") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key.strip()] = float(parts[0])
    except OSError:
        return {}
    return out


def _parse_smaps_paths(pid: int) -> Dict[str, int]:
    """Parse ``/proc/PID/smaps`` and aggregate Rss (KB) per path."""
    smaps_path = Path(f"/proc/{pid}/smaps")
    if not smaps_path.exists():
        return {}

    per_path: Dict[str, int] = {}
    current_path: Optional[str] = None
    try:
        with smaps_path.open("r") as f:
            for line in f:
                if _HEADER_RE.match(line):
                    parts = line.split(maxsplit=5)
                    current_path = parts[5].strip() if len(parts) >= 6 else ""
                elif current_path is not None and line.startswith("Rss:"):
                    try:
                        kb = int(line.split()[1])
                    except (IndexError, ValueError):
                        continue
                    per_path[current_path] = per_path.get(current_path, 0) + kb
    except OSError:
        return {}
    return per_path


def _linux_breakdown(pid: int, top_n: int) -> MemoryBreakdown:
    rollup = _parse_smaps_rollup(pid)
    per_path = _parse_smaps_paths(pid)

    if not rollup and not per_path:
        return MemoryBreakdown(supported=False)

    b = MemoryBreakdown(source="smaps_rollup+smaps" if rollup else "smaps")

    # Aggregates from rollup (authoritative). Fall back to summing smaps.
    if rollup:
        b.total_rss_mb = rollup.get("Rss", 0.0) / 1024
        b.anonymous_mb = rollup.get("Anonymous", 0.0) / 1024
        b.pss_mb = rollup.get("Pss", 0.0) / 1024
        b.private_dirty_mb = rollup.get("Private_Dirty", 0.0) / 1024
        b.swap_mb = rollup.get("Swap", 0.0) / 1024
    else:
        b.total_rss_mb = sum(per_path.values()) / 1024

    # Per-category via smaps paths
    anon_from_paths_mb = 0.0
    for path, kb in per_path.items():
        mb = kb / 1024
        bucket = _categorize(path)
        if bucket == "anon":
            anon_from_paths_mb += mb
        elif bucket == "nodestore":
            b.nodestore_mb += mb
        elif bucket == "shared_lib":
            b.shared_lib_mb += mb
        else:
            b.other_file_mb += mb

    # Prefer rollup's Anonymous when available (it's the true counter); smaps
    # sum-over-[*] regions underestimates because it misses file-backed pages
    # that have been CoW'd dirty.
    if not rollup:
        b.anonymous_mb = anon_from_paths_mb

    file_items = [(p, kb / 1024) for p, kb in per_path.items() if p and not p.startswith("[")]
    file_items.sort(key=lambda item: item[1], reverse=True)
    b.top_files = file_items[:top_n]

    return b


def _macos_breakdown(pid: int) -> MemoryBreakdown:
    try:
        proc = psutil.Process(pid)
        mi = proc.memory_info()
        mfi = proc.memory_full_info()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return MemoryBreakdown(supported=False)

    rss_mb = mi.rss / (1024 * 1024)
    uss_mb = mfi.uss / (1024 * 1024)

    # On macOS uss ≈ resident-private ≈ heap + stack. rss - uss is the
    # shared/file-backed portion (leaf-pack mmap pages land here). No per-path
    # detail without shelling out to vmmap, so everything non-anon lands in
    # other_file_mb.
    return MemoryBreakdown(
        supported=True,
        source="psutil",
        total_rss_mb=rss_mb,
        anonymous_mb=uss_mb,
        other_file_mb=max(0.0, rss_mb - uss_mb),
    )


def get_memory_breakdown(pid: int, top_n: int = 5) -> MemoryBreakdown:
    """Return a per-category RSS breakdown for the given process.

    Heap-saving optimizations show up as a drop in ``anonymous_mb``; pages
    shifted into file-backed mmap show up under ``nodestore_mb`` or
    ``other_file_mb`` (and count toward ``total_rss_mb``, but are evictable
    under memory pressure — the kernel tracks them differently).
    """
    system = platform.system()
    if system == "Linux":
        return _linux_breakdown(pid, top_n)
    if system == "Darwin":
        return _macos_breakdown(pid)
    return MemoryBreakdown(supported=False)
