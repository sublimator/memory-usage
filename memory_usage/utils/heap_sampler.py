"""
macOS ``heap`` subprocess wrapper + output parser.

``heap`` buckets live allocations by class name and is the cheapest way
to get a "what's actually on the heap" view of a running rippled. Output
rows look like::

    COUNT   BYTES    AVG    CLASS_NAME                             C/O   BINARY
    30      2400     80.0   Class.data.readonly (class_ro_t)       C     libobjc.A.dylib
    1234    567890   460.2  SHAMapInnerNode                        C     xrpld

This module:
  - shells out to ``heap PID`` (macOS only; errors cleanly elsewhere)
  - parses the tabular body into a list of dicts
  - returns a structured sample suitable for jsonl persistence + TUI
    rendering

``heap`` suspends the target via task_for_pid while it runs, same cost
as ``vmmap``. Callers must gate invocation (server_state == "full", opt-in
flag, don't stack calls) — this module is dumb and fires on demand.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# count    bytes    avg       class_name (possibly with spaces/parens)    C|O    binary
# The non-greedy (.+?) for class_name is anchored by the fixed C/O one-
# char column + binary basename that follow. Works for both C (class) and
# O (ObjC object) rows.
_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)\s+(.+?)\s+([CO])\s+(\S+)\s*$")


def is_supported() -> bool:
    """``heap`` is macOS-only — return False on Linux so callers can bail."""
    return platform.system() == "Darwin"


def take_heap_sample(
    pid: int,
    top_n: int = 50,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """Run ``heap`` for ``pid`` and return a parsed sample dict.

    Shape::

        {
          "t": "2026-04-17T12:00:00",
          "pid": 12345,
          "duration_ms": 3200,
          "ok": true,
          "error": null,        # populated on failure
          "total_bytes": 12_345_678,  # sum of 'bytes' across parsed rows
          "row_count": 312,           # total parsed rows (not just top_n)
          "top": [
            {"rank": 1, "class": "SHAMapInnerNode", "count": 1234,
             "bytes": 567890, "avg": 460.2, "type": "C", "binary": "xrpld"},
            ...
          ]
        }

    On error ``ok=False``, ``error`` is set, other fields are zero/empty.
    Top list is sorted by bytes desc and truncated to ``top_n`` — callers
    that want the full list should call with a huge ``top_n``.
    """
    t0 = time.monotonic()
    now_iso = datetime.now().isoformat()

    if not is_supported():
        return {
            "t": now_iso,
            "pid": pid,
            "duration_ms": 0,
            "ok": False,
            "error": "heap: macOS only",
            "total_bytes": 0,
            "row_count": 0,
            "top": [],
        }

    try:
        proc = subprocess.run(
            ["heap", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except FileNotFoundError:
        return {
            "t": now_iso,
            "pid": pid,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "ok": False,
            "error": "heap: command not found (is it on PATH?)",
            "total_bytes": 0,
            "row_count": 0,
            "top": [],
        }
    except subprocess.TimeoutExpired:
        return {
            "t": now_iso,
            "pid": pid,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "ok": False,
            "error": f"heap: timed out after {timeout_s}s",
            "total_bytes": 0,
            "row_count": 0,
            "top": [],
        }

    duration_ms = int((time.monotonic() - t0) * 1000)

    if proc.returncode != 0:
        # Most common cause: no task_for_pid entitlement for another user's
        # process. Surface stderr verbatim so the user can act on it.
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"exit {proc.returncode}"
        return {
            "t": now_iso,
            "pid": pid,
            "duration_ms": duration_ms,
            "ok": False,
            "error": f"heap: {detail}",
            "total_bytes": 0,
            "row_count": 0,
            "top": [],
        }

    rows = _parse_heap_output(proc.stdout)
    total_bytes = sum(r["bytes"] for r in rows)
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    top = rows[:top_n]
    for i, r in enumerate(top):
        r["rank"] = i + 1

    return {
        "t": now_iso,
        "pid": pid,
        "duration_ms": duration_ms,
        "ok": True,
        "error": None,
        "total_bytes": total_bytes,
        "row_count": len(rows),
        "top": top,
        "mode": detect_mode(top),
    }


def detect_mode(rows: List[Dict[str, Any]]) -> str:
    """class view vs alloc-site view.

    With MallocStackLogging enabled, heap groups by call-site so class names
    read "malloc in FUNCTION_NAME" or similar. Without it, rows are honest
    class names ("SHAMapInnerNode", "std::__1::vector"). The >50% heuristic
    on the top-N is enough because MSL transforms ~every line.

    Returned literally as "alloc-site" or "class" — callers render it in
    the header so users know which question they're answering.
    """
    if not rows:
        return "class"
    alloc_site_hits = sum(1 for r in rows if (r.get("class") or "").startswith("malloc in "))
    return "alloc-site" if alloc_site_hits * 2 > len(rows) else "class"


def filter_sample(
    sample: Dict[str, Any],
    binary: Optional[str] = None,
    grep: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a new sample with ``top`` filtered (non-destructive).

    - ``binary``: substring match on the binary column (``xrpld`` catches
      ``xrpld``/``libxrpld.dylib`` but not ``libcrypto``).
    - ``grep``: regex match on the class column.

    Row-count / total_bytes stay the original (pre-filter) values so the
    header still tells the user how much was pruned.
    """
    if not sample.get("ok"):
        return sample
    rows = list(sample.get("top") or [])
    if binary:
        needle = binary.lower()
        rows = [r for r in rows if needle in (r.get("binary") or "").lower()]
    if grep:
        try:
            pat = re.compile(grep)
        except re.error:
            pat = re.compile(re.escape(grep))
        rows = [r for r in rows if pat.search(r.get("class") or "")]
    # Re-rank filtered rows so display is still numbered 1..N.
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return {**sample, "top": rows, "filtered": bool(binary or grep)}


def _parse_heap_output(stdout: str) -> List[Dict[str, Any]]:
    """Pull data rows out of ``heap``'s mixed-format text output.

    The tool prints a preamble (process name, zones summary, etc) followed
    by one or more class-histogram tables. We don't try to segment by
    zone — every ``count bytes avg class C/O binary`` line is treated as
    a datapoint and deduplicated on ``(class, binary)``. Rows that don't
    match the regex are silently skipped.
    """
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for line in stdout.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        count = int(m.group(1))
        byt = int(m.group(2))
        avg = float(m.group(3))
        cls = m.group(4).strip()
        typ = m.group(5)
        binary = m.group(6)
        key = (cls, binary, typ)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = {
                "class": cls,
                "count": count,
                "bytes": byt,
                "avg": avg,
                "type": typ,
                "binary": binary,
            }
        else:
            # Same class appears in multiple zones — aggregate.
            existing["count"] += count
            existing["bytes"] += byt
            existing["avg"] = existing["bytes"] / existing["count"] if existing["count"] else 0.0
    return list(by_key.values())


def render_heap_sample(sample: Dict[str, Any], top_n: Optional[int] = None) -> None:
    """Print a heap sample as a Rich table. Falls back to plain text on error."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not sample.get("ok"):
        console.print(f"[red]heap failed:[/red] {sample.get('error')}")
        return

    pid = sample.get("pid")
    total_mb = (sample.get("total_bytes") or 0) / (1024 * 1024)
    row_count = sample.get("row_count") or 0
    duration_ms = sample.get("duration_ms") or 0
    mode = sample.get("mode") or "class"
    mode_note = (
        " [yellow](MallocStackLogging active — alloc-site view, not class view)[/yellow]"
        if mode == "alloc-site"
        else ""
    )
    filtered_note = " [dim](filtered)[/dim]" if sample.get("filtered") else ""
    console.print(
        f"[bold cyan]heap pid {pid}[/bold cyan] mode={mode}{mode_note}  "
        f"[dim]{row_count:,} classes  {total_mb:,.1f} MB total  "
        f"({duration_ms} ms){filtered_note}[/dim]"
    )

    top = sample.get("top") or []
    if top_n is not None:
        top = top[:top_n]
    table = Table(header_style="bold cyan", box=None)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Count", justify="right", style="yellow")
    table.add_column("Bytes", justify="right", style="green")
    table.add_column("MB", justify="right", style="green")
    table.add_column("Avg", justify="right", style="dim")
    table.add_column("Class", style="yellow")
    table.add_column("Binary", style="dim")
    for row in top:
        byt = row.get("bytes") or 0
        table.add_row(
            str(row.get("rank") or ""),
            f"{row.get('count', 0):,}",
            f"{byt:,}",
            f"{byt / (1024 * 1024):,.2f}",
            f"{row.get('avg', 0):,.1f}",
            row.get("class") or "",
            row.get("binary") or "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# heap-trend: per-class growth across all heap_sample events in events.jsonl
# ---------------------------------------------------------------------------


def build_heap_trend(
    events_path: Path,
    binary: Optional[str] = None,
    grep: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Walk events.jsonl and aggregate per-class heap size over time.

    Emits one row per (class, binary) that appeared in any snapshot's
    heap_sample. The "monotonic" column is the real signal — a class that
    never shrinks between consecutive samples is a genuine leak suspect
    (as opposed to a churning pool that climbs and sweeps).

    Caveat: classes fall in and out of the top-50 over time. We treat
    "absent from a sample" as "no data" (not as decreased), so a class
    that merely drops out of top-50 isn't disqualified from monotonic.
    """
    # class_key -> list of (sample_index, bytes, count, binary, type)
    per_class: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    sample_index = 0

    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "snapshot":
                continue
            sample = ev.get("heap_sample")
            if not isinstance(sample, dict) or not sample.get("ok"):
                continue
            sample_index += 1
            for row in sample.get("top") or []:
                cls = row.get("class") or ""
                bin_ = row.get("binary") or ""
                if binary and binary.lower() not in bin_.lower():
                    continue
                if grep:
                    try:
                        pat = re.compile(grep)
                    except re.error:
                        pat = re.compile(re.escape(grep))
                    if not pat.search(cls):
                        continue
                per_class.setdefault((cls, bin_), []).append(
                    {
                        "sample_idx": sample_index,
                        "bytes": int(row.get("bytes") or 0),
                        "count": int(row.get("count") or 0),
                        "type": row.get("type") or "",
                    }
                )

    results: List[Dict[str, Any]] = []
    for (cls, bin_), series in per_class.items():
        if not series:
            continue
        series.sort(key=lambda s: s["sample_idx"])
        first = series[0]
        last = series[-1]
        # Monotonic = never decreased between consecutive *appearances*.
        # Skipping samples where the class fell off top-N is fine; we
        # only have data when we have data.
        ever_decreased = False
        prev_bytes = series[0]["bytes"]
        for s in series[1:]:
            if s["bytes"] < prev_bytes:
                ever_decreased = True
                break
            prev_bytes = s["bytes"]
        results.append(
            {
                "class": cls,
                "binary": bin_,
                "samples": len(series),
                "first_bytes": first["bytes"],
                "last_bytes": last["bytes"],
                "max_bytes": max(s["bytes"] for s in series),
                "delta_bytes": last["bytes"] - first["bytes"],
                "first_count": first["count"],
                "last_count": last["count"],
                "monotonic": not ever_decreased,
            }
        )

    # Default ordering: biggest delta first; monotonic climbers stick out
    # naturally because they keep gaining without ever giving back.
    results.sort(key=lambda r: -r["delta_bytes"])
    return results


def render_heap_trend(
    rows: List[Dict[str, Any]],
    top_n: int = 50,
    monotonic_only: bool = False,
) -> None:
    """Pretty-print a heap_trend table. Highlights monotonic growers.

    Rows are already sorted by Δbytes desc; callers pick how many to show.
    `monotonic_only` filters to leak-shaped climbers only.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    filtered = [r for r in rows if r["monotonic"]] if monotonic_only else rows
    if not filtered:
        note = "monotonic-only filter in effect" if monotonic_only else "no data"
        console.print(f"[dim]heap-trend: {note}[/dim]")
        return

    shown = filtered[:top_n]
    total = len(filtered)
    mono_count = sum(1 for r in rows if r["monotonic"])
    console.print(
        f"[bold cyan]heap trend[/bold cyan]  "
        f"[dim]{total:,} classes seen, {mono_count} monotonic; showing top {len(shown)}[/dim]"
    )
    table = Table(header_style="bold cyan", box=None)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Mono", justify="center")
    table.add_column("Class", style="yellow")
    table.add_column("Binary", style="dim")
    table.add_column("Samples", justify="right", style="dim")
    table.add_column("First MB", justify="right", style="dim")
    table.add_column("Last MB", justify="right", style="green")
    table.add_column("Δ MB", justify="right")
    for i, r in enumerate(shown, 1):
        delta = r["delta_bytes"]
        style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
        mono = "[bold red]↑↑[/bold red]" if r["monotonic"] and delta > 0 else ""
        table.add_row(
            str(i),
            mono,
            r["class"],
            r["binary"],
            str(r["samples"]),
            f"{r['first_bytes'] / (1024 * 1024):,.2f}",
            f"{r['last_bytes'] / (1024 * 1024):,.2f}",
            f"[{style}]{delta / (1024 * 1024):+,.2f}[/{style}]",
        )
    console.print(table)
