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

import platform
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    }


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
    console.print(
        f"[bold cyan]heap pid {pid}[/bold cyan]  "
        f"[dim]{row_count:,} classes  {total_mb:,.1f} MB total  "
        f"({duration_ms} ms)[/dim]"
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
