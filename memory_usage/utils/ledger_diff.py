"""
Compute and render a diff between two ledger-close snapshots in events.jsonl.

The diff is intended for "what happened between ledger A and ledger B?"
triage — wall-clock + txn throughput up top, then a memory block, then a
sorted-by-magnitude counts block showing only the numeric metrics that
actually moved.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
from rich.console import Console
from rich.table import Table

from .process_discovery import find_rippled_processes
from .session_store import session_dir_name


def resolve_session_dir(
    root: Path,
    explicit: Optional[Path] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """Figure out which session dir to diff.

    Resolution order, matching ``xahaud-monitor attach`` semantics:

    1. ``--dir PATH`` wins if given.
    2. Otherwise, find running rippled/xahaud processes. If exactly one
       is running and its ``(pid, create_time)`` session dir exists in
       ``root``, use it.
    3. Ambiguous (0 processes, >1 processes, or no matching dir) → return
       a diagnostic string for the caller to print, and None for the path.
    """
    if explicit is not None:
        if not explicit.exists():
            return None, f"--dir {explicit} does not exist"
        if not (explicit / "events.jsonl").exists():
            return None, f"{explicit}/events.jsonl is missing"
        return explicit, None

    procs = find_rippled_processes()
    if not procs:
        return None, "no running rippled/xahaud process — pass --dir PATH"
    if len(procs) > 1:
        names = ", ".join(f"{p.name}(pid {p.pid})" for p in procs)
        return None, f"multiple rippled processes running ({names}) — pass --dir PATH"

    proc = procs[0]
    try:
        create_time = psutil.Process(proc.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        return None, f"cannot read create_time for pid {proc.pid}: {e}"

    dir_path = root / session_dir_name(proc.name, proc.pid, create_time)
    if not dir_path.exists() or not (dir_path / "events.jsonl").exists():
        return None, (
            f"no session dir for running process {proc.name} pid {proc.pid}\n"
            f"  expected: {dir_path}\n"
            f"  (is the monitor currently running on this pid?)"
        )
    return dir_path, None


def load_ledger_snapshots(
    events_path: Path,
    from_ledger: int,
    to_ledger: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[int]]:
    """Scan events.jsonl, return (from_snapshot, to_snapshot, first_ledger).

    ``first_ledger`` is the ledger_index of the earliest snapshot we saw —
    useful context for the diff header (how deep into the session are we?).
    Either snapshot can be None if the ledger wasn't captured.
    """
    from_snap: Optional[Dict[str, Any]] = None
    to_snap: Optional[Dict[str, Any]] = None
    first_ledger: Optional[int] = None
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
            idx = ev.get("ledger_index")
            if idx is None:
                continue
            if first_ledger is None:
                first_ledger = idx
            if idx == from_ledger:
                from_snap = ev
            elif idx == to_ledger:
                to_snap = ev
    return from_snap, to_snap, first_ledger


def _fmt_uptime(seconds: Optional[int]) -> str:
    """Short human-readable uptime: 4478 -> '1h14m', 58 -> '58s'."""
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def _derived(snapshot: Dict[str, Any], name: str) -> Optional[float]:
    """Computed fields — not in the raw event, derived from others.

    ``heap_mb`` = rss_mb minus nodestore mmap minus other file-backed mmap.
    Isolates the part of RSS that's actually heap allocations, which is what
    you want to track when hunting leaks separately from nodestore paging.
    """
    if name == "heap_mb":
        rss = snapshot.get("rss_mb")
        if not isinstance(rss, (int, float)):
            return None
        bd = snapshot.get("memory_breakdown") or {}
        nodestore = bd.get("nodestore_mb") if isinstance(bd, dict) else None
        other = bd.get("other_file_mb") if isinstance(bd, dict) else None
        paged = 0.0
        if isinstance(nodestore, (int, float)):
            paged += float(nodestore)
        if isinstance(other, (int, float)):
            paged += float(other)
        return float(rss) - paged
    return None


def _lookup(snapshot: Dict[str, Any], field_path: str) -> Optional[float]:
    """Dotted-path lookup for a numeric value, with derived-field fallback."""
    derived = _derived(snapshot, field_path)
    if derived is not None:
        return derived
    cur: Any = snapshot
    for part in field_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, bool):
        return None
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


_OPS = {
    ">": lambda d, v: d > v,
    ">=": lambda d, v: d >= v,
    "<": lambda d, v: d < v,
    "<=": lambda d, v: d <= v,
    "==": lambda d, v: d == v,
    "!=": lambda d, v: d != v,
    "abs>": lambda d, v: abs(d) > v,
    "abs>=": lambda d, v: abs(d) >= v,
    "abs<": lambda d, v: abs(d) < v,
    "abs<=": lambda d, v: abs(d) <= v,
}


def find_delta_matches(
    events_path: Path,
    field_path: str,
    op: str,
    threshold: float,
) -> List[Dict[str, Any]]:
    """Walk snapshots in order; emit rows where Δ(field) matches the predicate.

    Only *consecutive* ledger-close snapshots are compared — polling snapshots
    (no ledger_index) are skipped entirely. Each match row carries enough
    context that the caller can print a useful single-line hit.
    """
    predicate = _OPS.get(op)
    if predicate is None:
        raise ValueError(f"unsupported op: {op}")

    prev: Optional[Dict[str, Any]] = None
    matches: List[Dict[str, Any]] = []
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "snapshot" or ev.get("ledger_index") is None:
                continue
            cur = ev
            if prev is not None:
                a = _lookup(prev, field_path)
                b = _lookup(cur, field_path)
                if a is not None and b is not None:
                    delta = b - a
                    if predicate(delta, threshold):
                        matches.append(
                            {
                                "from_ledger": prev.get("ledger_index"),
                                "to_ledger": cur.get("ledger_index"),
                                "from_value": a,
                                "to_value": b,
                                "delta": delta,
                                "t_from": prev.get("t") or prev.get("timestamp"),
                                "t_to": cur.get("t") or cur.get("timestamp"),
                                "txn_count": cur.get("transaction_count"),
                            }
                        )
            prev = cur
    return matches


def render_find_results(
    field_path: str,
    op: str,
    threshold: float,
    matches: List[Dict[str, Any]],
    console: Optional[Console] = None,
) -> None:
    console = console or Console()
    console.print(
        f"[bold cyan]Δ{field_path} {op} {threshold}[/bold cyan]  "
        f"— [bold]{len(matches)}[/bold] match(es)"
    )
    if not matches:
        return
    table = Table(header_style="bold cyan", box=None)
    table.add_column("From", justify="right", style="yellow")
    table.add_column("To", justify="right", style="yellow")
    table.add_column("Δt", justify="right", style="dim")
    table.add_column("Txns", justify="right", style="dim")
    table.add_column("From val", justify="right", style="dim")
    table.add_column("To val", justify="right", style="green")
    table.add_column("Δ", justify="right")
    for m in matches:
        t_from = _parse_ts(m["t_from"])
        t_to = _parse_ts(m["t_to"])
        dt = ""
        if t_from and t_to:
            dt = f"{(t_to - t_from).total_seconds():.1f}s"
        delta = m["delta"]
        style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
        a = m["from_value"]
        b = m["to_value"]
        is_int = a == int(a) and b == int(b)
        fmt = "{:,.0f}" if is_int else "{:,.3f}"
        table.add_row(
            f"{m['from_ledger']:,}",
            f"{m['to_ledger']:,}",
            dt,
            f"{m['txn_count']}" if m.get("txn_count") is not None else "",
            fmt.format(a),
            fmt.format(b),
            f"[{style}]{'+' if delta > 0 else ''}{fmt.format(delta)}[/{style}]",
        )
    console.print(table)


def _flatten_numeric(prefix: str, d: Any, out: Dict[str, float]) -> None:
    """Walk a dict, recording numeric leaves keyed by dotted path."""
    if isinstance(d, dict):
        for k, v in d.items():
            _flatten_numeric(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        out[prefix] = float(d)


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None


def render_diff(
    from_snap: Dict[str, Any],
    to_snap: Dict[str, Any],
    console: Optional[Console] = None,
    first_ledger: Optional[int] = None,
) -> None:
    console = console or Console()

    # --- header ------------------------------------------------------------
    from_idx = from_snap.get("ledger_index")
    to_idx = to_snap.get("ledger_index")
    ledgers_span = (to_idx - from_idx) if (from_idx and to_idx) else None
    txn_delta = int(to_snap.get("cumulative_transactions", 0) or 0) - int(
        from_snap.get("cumulative_transactions", 0) or 0
    )
    t_from = _parse_ts(from_snap.get("t") or from_snap.get("timestamp"))
    t_to = _parse_ts(to_snap.get("t") or to_snap.get("timestamp"))
    wall_s = (t_to - t_from).total_seconds() if (t_from and t_to) else None
    up_from = from_snap.get("rippled_uptime_s")
    up_to = to_snap.get("rippled_uptime_s")

    header_parts = [f"[bold cyan]Ledger {from_idx:,} → {to_idx:,}[/bold cyan]"]
    if ledgers_span is not None:
        header_parts.append(f"{ledgers_span} ledgers")
    header_parts.append(f"{txn_delta:,} txns")
    if wall_s is not None:
        header_parts.append(f"[dim]{wall_s:.1f}s wall[/dim]")
        if wall_s > 0:
            header_parts.append(f"[dim]{txn_delta / wall_s:.1f} tps[/dim]")
    console.print(" | ".join(header_parts))

    # Second line: process uptime at each end + session-start context.
    # Puts the two ledgers in absolute timeline terms — "was the +15 MB
    # jump in the first 2 min of uptime or after 6 hours?" is often the
    # question that matters.
    context_parts: List[str] = []
    if up_from is not None or up_to is not None:
        context_parts.append(f"[dim]uptime[/dim] {_fmt_uptime(up_from)} → {_fmt_uptime(up_to)}")
    if first_ledger is not None and from_idx is not None:
        ledgers_into = from_idx - first_ledger
        context_parts.append(
            f"[dim]session first ledger[/dim] {first_ledger:,} "
            f"[dim]({ledgers_into:,} before FROM)[/dim]"
        )
    if context_parts:
        console.print(" | ".join(context_parts))
    console.print()

    # --- memory ------------------------------------------------------------
    mem_table = Table(title="Memory", title_style="bold", header_style="bold cyan", box=None)
    mem_table.add_column("Metric", style="yellow")
    mem_table.add_column("From", justify="right", style="dim")
    mem_table.add_column("To", justify="right", style="green")
    mem_table.add_column("Δ", justify="right")

    for key, label, unit in [
        ("rss_mb", "RSS", "MB"),
        ("vms_mb", "VMS", "MB"),
        ("memory_percent", "%", "%"),
        ("num_threads", "Threads", ""),
    ]:
        a = from_snap.get(key)
        b = to_snap.get(key)
        if a is None or b is None:
            continue
        delta = b - a
        style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
        mem_table.add_row(
            label,
            f"{a:,.1f}{unit}" if isinstance(a, float) else f"{a}",
            f"{b:,.1f}{unit}" if isinstance(b, float) else f"{b}",
            f"[{style}]{delta:+,.1f}{unit}[/{style}]"
            if isinstance(delta, float)
            else f"[{style}]{delta:+}[/{style}]",
        )
    console.print(mem_table)

    # --- pool totals (pinned) ----------------------------------------------
    # tagged_pointer_pools._total dominates real memory movement on
    # patched-rippled builds but doesn't stand out in the general counts
    # table because the other ~hundreds of counters drown it out. Pin it
    # up here so the biggest driver is always on screen first.
    pool_a = (from_snap.get("counts") or {}).get("tagged_pointer_pools", {}).get("_total", {})
    pool_b = (to_snap.get("counts") or {}).get("tagged_pointer_pools", {}).get("_total", {})
    if isinstance(pool_a, dict) and isinstance(pool_b, dict) and pool_a and pool_b:
        pool_fields = [
            ("current_bytes", "Current", "bytes"),
            ("peak_bytes", "Peak", "bytes"),
            ("cached_wasted_bytes", "Cached (wasted)", "bytes"),
            ("cumulative_allocs", "Lifetime allocs", ""),
        ]
        any_delta = False
        pool_table = Table(
            title="SHAMap pool totals",
            title_style="bold",
            header_style="bold cyan",
            box=None,
        )
        pool_table.add_column("Metric", style="yellow")
        pool_table.add_column("From", justify="right", style="dim")
        pool_table.add_column("To", justify="right", style="green")
        pool_table.add_column("Δ", justify="right")
        for key, label, unit in pool_fields:
            a = pool_a.get(key)
            b = pool_b.get(key)
            if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
                continue
            delta = b - a
            if delta != 0:
                any_delta = True
            style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
            # Render bytes as MB when appropriate for readability.
            if unit == "bytes" and (a >= 1 << 20 or b >= 1 << 20):
                fmt_a = f"{a / (1024 * 1024):,.1f} MB"
                fmt_b = f"{b / (1024 * 1024):,.1f} MB"
                fmt_d = f"{delta / (1024 * 1024):+,.1f} MB"
            else:
                fmt_a = f"{a:,.0f}"
                fmt_b = f"{b:,.0f}"
                fmt_d = f"{delta:+,.0f}"
            pool_table.add_row(label, fmt_a, fmt_b, f"[{style}]{fmt_d}[/{style}]")
        if any_delta:
            console.print()
            console.print(pool_table)

    # --- memory breakdown (if both have it) --------------------------------
    bd_a = from_snap.get("memory_breakdown") or {}
    bd_b = to_snap.get("memory_breakdown") or {}
    if bd_a and bd_b:
        flat_a: Dict[str, float] = {}
        flat_b: Dict[str, float] = {}
        _flatten_numeric("", bd_a, flat_a)
        _flatten_numeric("", bd_b, flat_b)
        breakdown_rows = []
        for k in sorted(set(flat_a) | set(flat_b)):
            a = flat_a.get(k)
            b = flat_b.get(k)
            if a is None or b is None:
                continue
            delta = b - a
            if abs(delta) < 0.05:  # MB rounding — hide noise
                continue
            breakdown_rows.append((k, a, b, delta))
        if breakdown_rows:
            breakdown_rows.sort(key=lambda r: -abs(r[3]))
            bd_table = Table(
                title="Memory breakdown", title_style="bold", header_style="bold cyan", box=None
            )
            bd_table.add_column("Field", style="yellow")
            bd_table.add_column("From", justify="right", style="dim")
            bd_table.add_column("To", justify="right", style="green")
            bd_table.add_column("Δ", justify="right")
            for k, a, b, delta in breakdown_rows:
                style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
                bd_table.add_row(
                    k,
                    f"{a:,.1f}",
                    f"{b:,.1f}",
                    f"[{style}]{delta:+,.1f}[/{style}]",
                )
            console.print()
            console.print(bd_table)

    # --- counts ------------------------------------------------------------
    counts_a = from_snap.get("counts") or {}
    counts_b = to_snap.get("counts") or {}
    if counts_a and counts_b:
        flat_a_c: Dict[str, float] = {}
        flat_b_c: Dict[str, float] = {}
        _flatten_numeric("", counts_a, flat_a_c)
        _flatten_numeric("", counts_b, flat_b_c)

        rows: List[Tuple[str, float, float, float]] = []
        for k in sorted(set(flat_a_c) | set(flat_b_c)):
            a = flat_a_c.get(k)
            b = flat_b_c.get(k)
            if a is None or b is None:
                continue
            delta = b - a
            if delta == 0:
                continue
            rows.append((k, a, b, delta))

        if not rows:
            console.print("\n[dim]Counts: no changes[/dim]")
        else:
            # Sort by absolute delta desc so the biggest movers are on top.
            rows.sort(key=lambda r: -abs(r[3]))
            ct_table = Table(
                title=f"Counts ({len(rows)} changed)",
                title_style="bold",
                header_style="bold cyan",
                box=None,
            )
            ct_table.add_column("Metric", style="yellow")
            ct_table.add_column("From", justify="right", style="dim")
            ct_table.add_column("To", justify="right", style="green")
            ct_table.add_column("Δ", justify="right")
            for k, a, b, delta in rows:
                style = "red" if delta > 0 else ("green" if delta < 0 else "dim")
                is_int = a.is_integer() and b.is_integer()
                fmt = "{:,.0f}" if is_int else "{:,.3f}"
                ct_table.add_row(
                    k,
                    fmt.format(a),
                    fmt.format(b),
                    f"[{style}]{'+' if delta > 0 else ''}{fmt.format(delta)}[/{style}]",
                )
            console.print()
            console.print(ct_table)
