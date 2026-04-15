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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

# smaps region header:
#   7f1234567000-7f1234abc000 r-xp 00000000 08:01 12345     /path/to/libsome.so
_HEADER_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]+ [rwxps-]{4} ")

# vmmap 'mapped file' line:
#   mapped file  300000000-45db0c000  [ 5.5G  5.5G  0K  0K] r--/r-x SM=S/A  /path/to/db.pack
# We only need the resident size (2nd value inside brackets) and the trailing path.
_VMMAP_MAPPED_FILE_RE = re.compile(
    r"^mapped file\s+[0-9a-f]+-[0-9a-f]+\s+\[\s*\S+\s+(\S+)\s+\S+\s+\S+\]\s+\S+\s+SM=\S+\s+(.+?)\s*$"
)

_NODESTORE_SUFFIXES = (
    ".nudb",  # NuDB
    ".db",  # SQLite (transactions.db, wallet.db)
    ".sst",  # RocksDB
    ".ldb",  # LevelDB
    ".sqlite",
    ".sqlite-wal",
    ".pack",  # leaf-pack / similar mmap'd derived datasets
)
# Path segments that strongly imply a rippled database file even without a
# matching extension — catches NuDB .dat/.key/.log lurking in db directories
# without false-matching on /var/log/foo.log etc.
_NODESTORE_PATH_MARKERS = ("/db/", "/nudb/", "/rocksdb/")
_SHARED_LIB_MARKERS = (".so", ".dylib")


@dataclass
class MemoryBreakdown:
    """Per-category RSS breakdown for a process."""

    supported: bool = True
    source: str = ""  # "smaps_rollup+smaps", "smaps_rollup", "psutil", ""
    # Hint rendered by the UI when some fields couldn't be collected (e.g.
    # macOS uss requires root). Empty string when everything is available.
    note: str = ""

    # Authoritative aggregates
    total_rss_mb: float = 0.0
    # The one you care about for heap-saving optimizations. None means we
    # couldn't determine it (macOS without sudo).
    anonymous_mb: Optional[float] = None  # heap, stack, MAP_ANON, brk
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
        def maybe_round(v: Optional[float]) -> Optional[float]:
            return round(v, 2) if v is not None else None

        return {
            "supported": self.supported,
            "source": self.source,
            "note": self.note,
            "total_rss_mb": round(self.total_rss_mb, 2),
            "anonymous_mb": maybe_round(self.anonymous_mb),
            "pss_mb": maybe_round(self.pss_mb),
            "private_dirty_mb": maybe_round(self.private_dirty_mb),
            "swap_mb": maybe_round(self.swap_mb),
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
    # Shared libs first — would otherwise false-match /db/ paths on some distros
    if any(marker in lowered for marker in _SHARED_LIB_MARKERS):
        return "shared_lib"
    if lowered.endswith(_NODESTORE_SUFFIXES):
        return "nodestore"
    if any(marker in lowered for marker in _NODESTORE_PATH_MARKERS):
        return "nodestore"
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


def _parse_vmmap_size(token: str) -> float:
    """Parse vmmap's size shorthand into MB. Handles '5.5G', '4096K', '32M', '0K'."""
    if not token:
        return 0.0
    token = token.strip()
    try:
        if token.endswith("G"):
            return float(token[:-1]) * 1024
        if token.endswith("M"):
            return float(token[:-1])
        if token.endswith("K"):
            return float(token[:-1]) / 1024
        if token.endswith("B"):
            return float(token[:-1]) / (1024 * 1024)
        # unitless → bytes
        return float(token) / (1024 * 1024)
    except ValueError:
        return 0.0


def _vmmap_mapped_files(pid: int) -> Optional[Dict[str, float]]:
    """Run ``vmmap`` and return {path: rss_mb} for each file-backed mapping.

    Returns None if vmmap isn't available, fails, or the process is off-limits
    (macOS normally requires root for other processes).
    """
    try:
        result = subprocess.run(
            ["vmmap", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    per_path: Dict[str, float] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("mapped file"):
            continue
        m = _VMMAP_MAPPED_FILE_RE.match(line)
        if not m:
            continue
        rss_mb = _parse_vmmap_size(m.group(1))
        path = m.group(2).strip()
        if path:
            per_path[path] = per_path.get(path, 0.0) + rss_mb
    return per_path


def _macos_breakdown(pid: int, top_n: int = 5) -> MemoryBreakdown:
    try:
        proc = psutil.Process(pid)
        mi = proc.memory_info()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return MemoryBreakdown(supported=False)

    rss_mb = mi.rss / (1024 * 1024)

    # uss (≈ resident-private ≈ heap) requires root on macOS. Without it we
    # can still report total rss but can't split anon vs file-backed.
    uss_mb: Optional[float] = None
    try:
        mfi = proc.memory_full_info()
        uss_mb = mfi.uss / (1024 * 1024)
    except (psutil.AccessDenied, AttributeError, OSError):
        uss_mb = None

    # vmmap gives us per-file rss when we have privileges. Parse its
    # 'mapped file' lines and bucket them via the shared categorizer.
    per_path = _vmmap_mapped_files(pid)

    if uss_mb is None and per_path is None:
        # No privileges at all — degrade gracefully.
        return MemoryBreakdown(
            supported=True,
            source="psutil",
            note="uss and vmmap require sudo on macOS — anon/mmap split unavailable",
            total_rss_mb=rss_mb,
        )

    b = MemoryBreakdown(
        supported=True,
        source="psutil+vmmap" if per_path is not None else "psutil",
        total_rss_mb=rss_mb,
        anonymous_mb=uss_mb,  # may be None if vmmap-only succeeded
    )

    if per_path is not None:
        for path, mb in per_path.items():
            bucket = _categorize(path)
            if bucket == "anon":
                continue  # shouldn't happen for mapped-file lines
            elif bucket == "nodestore":
                b.nodestore_mb += mb
            elif bucket == "shared_lib":
                b.shared_lib_mb += mb
            else:
                b.other_file_mb += mb

        file_items = sorted(per_path.items(), key=lambda item: item[1], reverse=True)
        b.top_files = file_items[:top_n]

        # With full data we can bound other_file by what's not in per_path
        # (system __TEXT sections, stack, kernel pages, etc). uss roughly
        # captures anon; the remainder after file-backed is system overhead.
        mapped_total = sum(per_path.values())
        if uss_mb is not None:
            unaccounted = rss_mb - uss_mb - mapped_total
            if unaccounted > 0:
                b.other_file_mb += unaccounted
    elif uss_mb is not None:
        # No vmmap; fall back to the coarse split.
        b.other_file_mb = max(0.0, rss_mb - uss_mb)
        b.note = "vmmap unavailable — file-backed breakdown not shown"

    return b


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
        return _macos_breakdown(pid, top_n)
    return MemoryBreakdown(supported=False)
