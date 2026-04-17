"""
Per-process event store: append-only JSONL for snapshots + meta.json aggregate.

The session dir is keyed on ``(binary_name, pid, create_time)`` so that reattaching
to the same live process lands in the same directory — the monitor keeps appending
to events.jsonl and adds a new entry to meta.json's ``sessions`` list. A fresh
process (different pid OR different create_time even if the pid is recycled) gets
a new directory. This gives attach/detach continuity plus crash resilience: if the
monitor dies mid-run, everything up to the last snapshot is already flushed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


def session_dir_name(binary_name: str, pid: int, create_time: float) -> str:
    """Stable identifier for this rippled invocation.

    create_time is truncated to int seconds — psutil returns microsecond
    precision that would make the dir name noisy and harder to glob.
    """
    safe = binary_name.replace("/", "_")
    return f"{safe}-{pid}-{int(create_time)}"


class SessionStore:
    """Append-only event store for one rippled invocation.

    - ``events.jsonl`` — one JSON object per line, line-buffered so a crash
      loses at most the in-flight line.
    - ``meta.json`` — rewritten atomically on binary start and finalize; holds
      binary info, system/test config, the list of monitor sessions, and a
      running summary (peak_rss_mb, totals, etc).
    """

    EVENTS_FILE = "events.jsonl"
    META_FILE = "meta.json"

    def __init__(
        self,
        output_root: Path,
        binary_name: str,
        pid: int,
        create_time: float,
    ):
        self.output_root = Path(output_root)
        self.binary_name = binary_name
        self.pid = pid
        self.create_time = create_time

        self.dir = self.output_root / session_dir_name(binary_name, pid, create_time)
        self.events_path = self.dir / self.EVENTS_FILE
        self.meta_path = self.dir / self.META_FILE

        self._file: Optional[Any] = None  # actually io.TextIOWrapper once opened

    # --- directory lifecycle --------------------------------------------------

    def exists(self) -> bool:
        """Has this (pid, create_time) been seen before on disk?"""
        return self.events_path.exists()

    def open_for_append(self) -> None:
        """Open events.jsonl for line-buffered append writes."""
        self.dir.mkdir(parents=True, exist_ok=True)
        # buffering=1 → line-buffered; every write_event() hits the FS on the
        # trailing newline, which is what we want for crash resilience.
        self._file = open(self.events_path, "a", buffering=1, encoding="utf-8")

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    # --- event writers --------------------------------------------------------

    def write_event(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Append a JSON line. Always stamped with UTC ISO ``t`` and ``event``."""
        if self._file is None:
            # Lazy-open in case caller forgot; keeps the API forgiving.
            self.open_for_append()
        record: Dict[str, Any] = {
            "t": datetime.utcnow().isoformat() + "Z",
            "event": event,
        }
        if payload:
            record.update(payload)
        assert self._file is not None
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")

    def write_snapshot(self, snapshot_dict: Dict[str, Any]) -> None:
        """Write a MemorySnapshot (already dict-ified) as a snapshot event."""
        self.write_event("snapshot", snapshot_dict)

    def write_session_start(
        self,
        session_n: int,
        mode: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"session": session_n, "mode": mode}
        if extra:
            payload.update(extra)
        self.write_event("session_start", payload)

    def write_session_end(
        self,
        session_n: int,
        status: str,
        reason: Optional[str] = None,
    ) -> None:
        self.write_event(
            "session_end",
            {"session": session_n, "status": status, "reason": reason},
        )

    # --- event replay ---------------------------------------------------------

    def iter_events(self) -> Iterator[Dict[str, Any]]:
        """Stream events.jsonl line-by-line; skip malformed lines.

        A crashed monitor may leave a partially-written trailing line — we
        don't want that to block reattachment, so just drop unparseable rows.
        """
        if not self.events_path.exists():
            return
        with open(self.events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    # --- meta.json ------------------------------------------------------------

    def load_meta(self) -> Optional[Dict[str, Any]]:
        if not self.meta_path.exists():
            return None
        try:
            with open(self.meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return None

    def save_meta(self, meta: Dict[str, Any]) -> None:
        """Write meta.json atomically via tmp + rename (survives a crash)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        tmp.replace(self.meta_path)

    def append_session(self, session_entry: Dict[str, Any]) -> Dict[str, Any]:
        """Add a session entry to meta.json, initializing file if absent."""
        meta = self.load_meta() or {}
        sessions = meta.setdefault("sessions", [])
        if isinstance(sessions, list):
            sessions.append(session_entry)
        self.save_meta(meta)
        return meta

    def update_session(self, session_n: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge ``updates`` into the session with matching ``n``; returns meta."""
        meta = self.load_meta()
        if meta is None:
            return None
        sessions = meta.get("sessions") or []
        if isinstance(sessions, list):
            for entry in sessions:
                if isinstance(entry, dict) and entry.get("n") == session_n:
                    entry.update(updates)
                    break
        self.save_meta(meta)
        return meta

    # --- helpers --------------------------------------------------------------

    def next_session_number(self) -> int:
        """1-based count of prior sessions +1 for the new one."""
        meta = self.load_meta()
        if not meta:
            return 1
        sessions = meta.get("sessions") or []
        if isinstance(sessions, list):
            return len(sessions) + 1
        return 1

    def snapshots(self) -> List[Dict[str, Any]]:
        """Return just snapshot-event payloads from the JSONL stream."""
        return [e for e in self.iter_events() if e.get("event") == "snapshot"]
