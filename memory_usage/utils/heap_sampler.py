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

# Three flavors of heap(1) rows we accept:
#   count  bytes  avg  class_name  C|O  binary       ← typed row
#   count  bytes  avg  class_name  C|O               ← typed row, no binary
#   count  bytes  avg  class_name                    ← typeless (raw malloc, non-object)
# heap sometimes prints thousand-separator commas in the byte column — the
# _num helper strips them before int(). Typeless rows are tagged with
# type="N" so downstream commands can include/exclude them explicitly.
_ROW_RE_TYPED_BIN = re.compile(r"^\s*([\d,]+)\s+([\d,]+)\s+([\d.,]+)\s+(.+?)\s+([CO])\s+(\S+)\s*$")
_ROW_RE_TYPED_NOBIN = re.compile(r"^\s*([\d,]+)\s+([\d,]+)\s+([\d.,]+)\s+(.+?)\s+([CO])\s*$")
_ROW_RE_BARE = re.compile(r"^\s*([\d,]+)\s+([\d,]+)\s+([\d.,]+)\s+(.+?)\s*$")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def is_supported() -> bool:
    """``heap`` is macOS-only — return False on Linux so callers can bail."""
    return platform.system() == "Darwin"


def take_heap_sample(
    pid: int,
    top_n: int = 50,
    timeout_s: float = 30.0,
    raw_output_path: Optional[Path] = None,
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

    # Stash raw stdout alongside the structured sample if the caller
    # gave us a path. This is the durable evidence when something looks
    # off downstream — the parser can lose ground against new heap(1)
    # formats and there's no second chance to re-run once the process
    # has moved on.
    raw_path_str: Optional[str] = None
    if raw_output_path is not None:
        try:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text(proc.stdout, encoding="utf-8")
            raw_path_str = str(raw_output_path)
        except OSError:
            raw_path_str = None

    rows, diag = _parse_heap_output(proc.stdout)
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
        "raw_path": raw_path_str,
        "lines_scanned": diag["lines_scanned"],
        "lines_matched": diag["lines_matched"],
        "unmatched_numeric": diag["unmatched_numeric"],
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


def _parse_heap_output(stdout: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Parse heap(1) output into (rows, diag) where ``diag`` carries stats.

    Returns every ``count bytes avg class [C|O] [binary]`` line as a row,
    deduplicating on (class, binary, type). Rows that don't match any of
    the three accepted shapes are counted in ``diag.unmatched`` so a
    surprisingly-small top-N on a fat process can be debugged via the
    raw output file we stash alongside.
    """
    by_key: Dict[tuple, Dict[str, Any]] = {}
    # Rows that summarise the whole sample rather than a bucket — skip them
    # so they don't dominate the top-K like an uncategorised giant class.
    summary_labels = {"total", "all zones", "process"}
    diag = {"lines_scanned": 0, "lines_matched": 0, "unmatched_numeric": 0}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        diag["lines_scanned"] += 1
        cls = binary = ""
        typ = ""
        count = byt = 0
        avg = 0.0
        matched = False
        m = _ROW_RE_TYPED_BIN.match(line)
        if m:
            count = int(_num(m.group(1)))
            byt = int(_num(m.group(2)))
            avg = _num(m.group(3))
            cls = m.group(4).strip()
            typ = m.group(5)
            binary = m.group(6)
            matched = True
        else:
            m2 = _ROW_RE_TYPED_NOBIN.match(line)
            if m2:
                count = int(_num(m2.group(1)))
                byt = int(_num(m2.group(2)))
                avg = _num(m2.group(3))
                cls = m2.group(4).strip()
                typ = m2.group(5)
                binary = ""
                matched = True
            else:
                m3 = _ROW_RE_BARE.match(line)
                if m3:
                    count = int(_num(m3.group(1)))
                    byt = int(_num(m3.group(2)))
                    avg = _num(m3.group(3))
                    cls = m3.group(4).strip()
                    typ = "N"  # non-object (typeless) — raw malloc
                    binary = ""
                    if cls.lower() in summary_labels or cls.lower().startswith("all "):
                        continue
                    matched = True
                elif line.lstrip()[:1].isdigit():
                    # Looked data-ish (starts with a digit) but didn't
                    # match any shape — count it so users can spot parser
                    # drift against a new heap(1) version.
                    diag["unmatched_numeric"] += 1
        if not matched:
            continue
        diag["lines_matched"] += 1
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
    return list(by_key.values()), diag


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
    session_dir: Path,
    binary: Optional[str] = None,
    grep: Optional[str] = None,
    include_non_object: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Walk a session's heap samples and aggregate per-class size over time.

    Prefers the raw ``heap_samples/<ledger>.txt`` files when present — they
    carry every class heap(1) emitted, not just the top-50 we stashed in
    the snapshot. That matters when user-space C++ classes get outranked
    by ObjC runtime chunks and would otherwise be invisible to trend
    analysis. Falls back to the jsonl snapshot ``heap_sample.top`` when
    raw files are missing (older sessions, or the sample failed to flush).

    ``include_non_object`` is off by default because the non-object bucket
    can dominate and hide per-class signal; surface it explicitly when
    class-level trends look flat.
    """
    per_class: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    alloc_site_samples = 0
    class_samples = 0
    sample_index = 0
    source = "jsonl"  # switched to "raw" when we find raw files

    compiled_grep = None
    if grep:
        try:
            compiled_grep = re.compile(grep)
        except re.error:
            compiled_grep = re.compile(re.escape(grep))

    def _accept(row: Dict[str, Any]) -> bool:
        cls = row.get("class") or ""
        bin_ = row.get("binary") or ""
        typ = row.get("type") or ""
        if not include_non_object and typ == "N":
            return False
        if binary and binary.lower() not in bin_.lower():
            return False
        if compiled_grep is not None and not compiled_grep.search(cls):
            return False
        return True

    def _record(sample_idx: int, row: Dict[str, Any]) -> None:
        per_class.setdefault((row["class"] or "", row["binary"] or ""), []).append(
            {
                "sample_idx": sample_idx,
                "bytes": int(row.get("bytes") or 0),
                "count": int(row.get("count") or 0),
                "type": row.get("type") or "",
            }
        )

    raw_dir = session_dir / "heap_samples"
    if raw_dir.exists():
        # Sort by ledger index (filename stem is int) so the time series
        # is consistent. Falls back to string sort if names aren't ints.
        def _sort_key(p: Path) -> Tuple[int, str]:
            try:
                return (int(p.stem), "")
            except ValueError:
                return (0, p.stem)

        raw_files = sorted((p for p in raw_dir.glob("*.txt") if p.is_file()), key=_sort_key)
        if raw_files:
            source = "raw"
            for p in raw_files:
                try:
                    text = p.read_text(encoding="utf-8")
                except OSError:
                    continue
                rows, _diag = _parse_heap_output(text)
                if not rows:
                    continue
                sample_index += 1
                mode = detect_mode(rows)
                if mode == "alloc-site":
                    alloc_site_samples += 1
                else:
                    class_samples += 1
                for r in rows:
                    if _accept(r):
                        _record(sample_index, r)

    if source == "jsonl":
        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            meta = {
                "sample_count": 0,
                "alloc_site_samples": 0,
                "class_samples": 0,
                "source": source,
            }
            return [], meta
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
                mode = sample.get("mode") or "class"
                if mode == "alloc-site":
                    alloc_site_samples += 1
                else:
                    class_samples += 1
                for row in sample.get("top") or []:
                    if _accept(row):
                        _record(sample_index, row)

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
    meta = {
        "sample_count": sample_index,
        "alloc_site_samples": alloc_site_samples,
        "class_samples": class_samples,
        "source": source,
    }
    return results, meta


def render_heap_trend(
    rows: List[Dict[str, Any]],
    meta: Optional[Dict[str, Any]] = None,
    top_n: int = 50,
    monotonic_only: bool = False,
) -> None:
    """Pretty-print a heap_trend table. Highlights monotonic growers.

    Rows are already sorted by Δbytes desc; callers pick how many to show.
    ``monotonic_only`` filters to leak-shaped climbers only. ``meta``
    drives the mode banner — without it users may read a flat class
    trend while actually looking at MallocStackLogging call-site data
    and think they're done.
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()

    if meta:
        n = meta.get("sample_count") or 0
        alloc_site = meta.get("alloc_site_samples") or 0
        source = meta.get("source") or "jsonl"
        if n == 0:
            console.print("[yellow]no heap samples in session dir[/yellow]")
            return
        source_note = (
            "raw heap_samples/*.txt (full class list)"
            if source == "raw"
            else "events.jsonl (top-50 per snapshot)"
        )
        console.print(f"[dim]source: {source_note}[/dim]")
        if alloc_site and alloc_site == n:
            console.print(
                "[yellow]banner:[/yellow] all "
                f"{n} sample(s) were in [bold]alloc-site[/bold] mode "
                "(MallocStackLogging active). This view groups by call site "
                "and only captures a sample of allocations — 'flat' trends "
                "do NOT mean memory is steady. Disable MallocStackLogging "
                "for a true class-level view."
            )
        elif alloc_site:
            console.print(
                f"[yellow]banner:[/yellow] {alloc_site} of {n} samples were "
                "alloc-site mode (MallocStackLogging) and {} were class view; "
                "trends below mix the two.".format(meta.get("class_samples") or 0)
            )

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
