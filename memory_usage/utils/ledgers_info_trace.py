"""
Offline trace of ``ledgers_info`` projections from a recorded session.

Streams a compact JSON-patch (RFC 6902) diff per snapshot to stdout:

  - Baseline: the first snapshot's projection emitted in full.
  - Subsequent: jsonpatch operations against the prior projection.

Output is designed for an AI/LLM to scan directly — baseline gives
the initial shape, subsequent diffs give the deltas, and the patch
format is widely recognised.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


def _event_ts(s: Dict[str, Any]) -> Optional[datetime]:
    t = s.get("timestamp") or s.get("t")
    if not isinstance(t, str):
        return None
    try:
        return datetime.fromisoformat(t.rstrip("Z"))
    except ValueError:
        return None


def _filter_projection(
    proj: Dict[str, Any],
    seq: Optional[int],
    view: str,
) -> Dict[str, Any]:
    """Apply --seq and --view filters to a projection.

    --seq keeps only that IBL inside ``ibls`` (and strips peers, which
    are only useful in aggregate).

    --view trims top-level keys:
        pointers  → pointers + gaps only
        ibls      → pointers (headline) + ibls
        peers     → pointers (headline) + peers
        all       → no trim (default)
    """
    out: Dict[str, Any] = dict(proj)

    if seq is not None and "ibls" in out and isinstance(out["ibls"], dict):
        key = str(seq)
        one = out["ibls"].get(key)
        out["ibls"] = {key: one} if one is not None else {}
        out.pop("peers", None)

    if view == "pointers":
        out = {k: v for k, v in out.items() if k in ("pointers", "gaps")}
    elif view == "ibls":
        out = {k: v for k, v in out.items() if k in ("pointers", "ibls", "priority_quorum_hash")}
    elif view == "peers":
        out = {k: v for k, v in out.items() if k in ("pointers", "peers")}
    return out


def _iter_snapshots(events_path: Path) -> Iterable[Dict[str, Any]]:
    with events_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event") != "snapshot":
                continue
            yield ev


def _fmt_rel(start: datetime, cur: datetime) -> str:
    delta = (cur - start).total_seconds()
    if delta < 0:
        return f"{delta:+.1f}s"
    if delta < 60:
        return f"+{delta:.1f}s"
    mins, rem = divmod(int(delta), 60)
    if mins < 60:
        return f"+{mins}m{rem:02d}s"
    hrs, mrem = divmod(mins, 60)
    return f"+{hrs}h{mrem:02d}m"


def _within(ts: Optional[datetime], since: Optional[datetime], until: Optional[datetime]) -> bool:
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def _parse_time(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.rstrip("Z"))
    except ValueError:
        print(f"warning: could not parse time {raw!r}", file=sys.stderr)
        return None


def render_trace(
    events_path: Path,
    *,
    seq: Optional[int] = None,
    view: str = "all",
    since: Optional[str] = None,
    until: Optional[str] = None,
    max_ops: Optional[int] = None,
) -> int:
    """Stream the projection diffs to stdout. Returns exit status."""
    try:
        import jsonpatch  # type: ignore[import-untyped]
    except ImportError:
        print(
            "error: `jsonpatch` is required. Run `uv sync` or `pip install jsonpatch`.",
            file=sys.stderr,
        )
        return 2

    if not events_path.is_file():
        print(f"error: events.jsonl not found at {events_path}", file=sys.stderr)
        return 2

    since_dt = _parse_time(since)
    until_dt = _parse_time(until)

    baseline_printed = False
    prior: Optional[Dict[str, Any]] = None
    first_ts: Optional[datetime] = None
    kept_seqs_ever: Set[str] = set()
    total_kept = 0
    total_skipped_empty = 0

    for ev in _iter_snapshots(events_path):
        proj_raw = ev.get("ledgers_info_projection")
        if not isinstance(proj_raw, dict) or not proj_raw:
            continue
        ts = _event_ts(ev)
        if not _within(ts, since_dt, until_dt):
            continue

        proj = _filter_projection(proj_raw, seq=seq, view=view)

        uptime = ev.get("rippled_uptime_s")
        header_bits: List[str] = []
        if ts is not None:
            if first_ts is None:
                first_ts = ts
                header_bits.append(f"@ {ts.isoformat(timespec='seconds')}")
            else:
                header_bits.append(f"@ {_fmt_rel(first_ts, ts)}")
        if uptime is not None:
            header_bits.append(f"rippled_uptime={uptime}s")

        if not baseline_printed:
            print(f"## baseline {' '.join(header_bits)}".rstrip())
            json.dump(proj, sys.stdout, separators=(",", ":"), sort_keys=True)
            print()
            print()
            prior = proj
            baseline_printed = True
            if seq is not None and proj.get("ibls"):
                kept_seqs_ever.update(proj["ibls"].keys())
            total_kept += 1
            continue

        patch = jsonpatch.make_patch(prior, proj).patch
        if not patch:
            total_skipped_empty += 1
            prior = proj
            continue

        if max_ops is not None and len(patch) > max_ops:
            patch = patch[:max_ops]
            trimmed = True
        else:
            trimmed = False

        print(f"## {' '.join(header_bits)}".rstrip())
        json.dump(patch, sys.stdout, separators=(",", ":"), sort_keys=False)
        print()
        if trimmed:
            print(f"# …diff truncated to {max_ops} ops")
        print()
        prior = proj
        total_kept += 1

    if not baseline_printed:
        msg = (
            "no snapshots with ledgers_info_projection found"
            " — either the session predates projection persistence,"
            " or rippled didn't emit ledgers_info."
        )
        print(f"# {msg}", file=sys.stderr)
        return 1

    print(
        f"# {total_kept} snapshot(s) emitted, {total_skipped_empty} unchanged-skip",
        file=sys.stderr,
    )
    return 0
