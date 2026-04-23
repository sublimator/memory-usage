"""
Ledgers-info dashboard widget.

Renders the patched-rippled ``ledgers_info`` RPC response — the four
latest-pointers (closed / validated / published / building), retained /
complete / missing / inbound_acquiring range sets, the gaps triangle
(behind_network / awaiting_publish / close_to_validate), and a legend
that explains each field so a reader unfamiliar with the wire schema
can still orient themselves.

Silent/empty on stock rippled where the command isn't registered.
"""

import json
import time
from typing import Any, Dict, List, Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    """Safe nested ``dict.get`` across dotted path."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


def _fmt_hash(v: Any, head: int = 10, tail: int = 6) -> str:
    """Shorten a long hex hash so rows don't wrap."""
    s = str(v) if v is not None else ""
    if len(s) <= head + tail + 1:
        return s or "-"
    return f"{s[:head]}…{s[-tail:]}"


def _max_seq_in_ranges(ranges: Any) -> Optional[int]:
    """Highest seq appearing in a RangeSet string like 'A-B,C-D,E'.

    Used to sanity-check rippled's behind_network when the producer
    side's math looks suspect. If inbound_acquiring has ledgers up to
    seq 103,727,569 and rippled claims behind_network = <absurd>, the
    inbound tip is a reliable lower bound on what the network is at
    right now.
    """
    if not isinstance(ranges, str) or not ranges or ranges == "empty":
        return None
    highest: Optional[int] = None
    for part in ranges.split(","):
        part = part.strip()
        if not part:
            continue
        hi_str = part.split("-", 1)[1] if "-" in part else part
        try:
            hi = int(hi_str)
        except ValueError:
            continue
        if highest is None or hi > highest:
            highest = hi
    return highest


class LedgersInfoDisplay(VerticalScroll):
    """Ledger state-machine overview."""

    def __init__(self) -> None:
        super().__init__()
        self.border_title = "Ledgers Info"
        self._content = Static(
            Text(
                "Waiting for ledgers_info RPC — patched rippled only.",
                style="dim",
            )
        )
        # Cache the pretty-printed raw JSON string, keyed by the id() of
        # the last payload object we rendered. The observer fires every
        # ~2s and the payload dict can be >10KB; re-serialising +
        # re-rendering each tick was the root of the 'AWFULLY slow' tab.
        self._raw_cache_id: Optional[int] = None
        self._raw_cache_text: str = ""
        # Previous poll's counter snapshot keyed by ledger seq — used to
        # render Δ columns (this poll − last poll) for each IBL. Seqs
        # that roll off (ledger finished acquiring, or dashboard was
        # just attached) show a dim "·" rather than a misleading Δ.
        self._prev_inbound: Dict[int, Dict[str, int]] = {}
        # Last rendered Δ strings, keyed by (seq, "state"|"tx"). When a
        # poll produces no net change for an IBL we re-render the last
        # meaningful Δ dimmed + prefixed with a ⏸ marker instead of
        # blanking to a single '·' — avoids the "useful column goes
        # blank every other tick" blinking on slow/idle acquires.
        self._last_delta_cell: Dict[tuple[int, str], str] = {}
        # Peak (max) idle-gap ever observed per (seq, side), in ms.
        # rippled's event arrays grow across poll but the current
        # idle value only reflects the instant we observed — a 6s
        # stall that recovered before the next poll would "flicker
        # away". Latching the max keeps those spikes visible.
        self._peak_idle_ms: Dict[tuple[int, str], int] = {}
        # Last-seen server_info.state_accounting dict — rendered as a
        # compact inline panel in the Ledgers Info tab. None until the
        # first server_info poll lands, or if rippled doesn't emit it.
        self._state_accounting: Optional[Dict[str, Any]] = None
        # Previous-poll transitions counts per state. Comparing against
        # the new payload lets us detect transitions we missed at the
        # poll boundary (e.g. tracking stayed live for 20us — too fast
        # to catch as current server_state but still bumps the counter).
        self._prev_transitions: Dict[str, int] = {}
        # Observed timeline of state changes, appended to as we spot
        # transitions. Each entry = (monotonic_ts_seconds, state_name,
        # source) where source ∈ {"observed", "inferred"}. Capped at
        # _STATE_LOG_MAX so long-lived dashboards don't grow unbounded.
        self._state_log: List[tuple[float, str, str]] = []
        self._STATE_LOG_MAX = 12
        # Last-observed current server_state — the dashboard needs this
        # so "state changed" detection isn't fooled by a poll arriving
        # mid-transition.
        self._last_server_state: Optional[str] = None
        # Monotonic wall origin (time.monotonic() at widget creation).
        # Timeline entries render as offsets from this so the first
        # logged transition is "0.0s" and later ones read as cumulative
        # dashboard uptime.
        self._clock_origin: float = time.monotonic()
        # Number of most-recent policy-history entries to chain in the
        # compact `policy` cell. Producer hands us the full transition
        # log per IBL (in `policy_history`), so we just read + trim.
        self._POLICY_HISTORY_MAX = 6

    def compose(self) -> ComposeResult:
        yield self._content

    def reset_state_timeline(self) -> None:
        """Clear the reconstructed transition log.

        Called from the dashboard on --reattach so the new incarnation
        starts with a blank timeline, and the clock origin rebases to
        "now" so displayed offsets are relative to this run rather than
        absolute-since-widget-creation.
        """
        self._state_log.clear()
        self._prev_transitions.clear()
        self._last_server_state = None
        self._clock_origin = time.monotonic()

    def update_state_accounting(
        self,
        sa: Optional[Dict[str, Any]],
        server_state: Optional[str] = None,
        rippled_uptime_s: Optional[int] = None,
    ) -> None:
        """Server_info.state_accounting → cached + timeline extended.

        Server-side `transitions` counts only report totals; they don't
        tell us WHEN each transition happened. We reconstruct ordering
        client-side: each poll compares transitions counts against the
        prior snapshot and watches for ``server_state`` changes.

        ``rippled_uptime_s`` is preferred as the timeline clock — it's
        server-side and therefore stable across reattaches (so hydrated
        replay and live polls land on a single coherent axis). If
        rippled_uptime_s is None we fall back to our own monotonic
        offset (widget-creation origin), which is fine for the
        cold-start case.
        """
        self._state_accounting = sa if isinstance(sa, dict) else None
        if self._state_accounting is None:
            return

        if rippled_uptime_s is not None:
            now = float(rippled_uptime_s)
        else:
            now = time.monotonic() - self._clock_origin

        if not self._state_log and server_state:
            self._state_log.append((now, server_state, "observed"))
            self._last_server_state = server_state
            for name, entry in self._state_accounting.items():
                if isinstance(entry, dict):
                    try:
                        self._prev_transitions[name] = int(entry.get("transitions") or 0)
                    except (TypeError, ValueError):
                        pass
            return

        # Collect all entries that fire this poll (inferred counter
        # bumps + any observed state change), then sort them by the
        # inherent state-ladder order before appending. This resolves
        # same-timestamp ties with the natural causal order a node
        # climbs: disconnected → connected → syncing → tracking → full.
        pending: List[tuple[str, str]] = []
        for name, entry in self._state_accounting.items():
            if not isinstance(entry, dict):
                continue
            try:
                tr = int(entry.get("transitions") or 0)
            except (TypeError, ValueError):
                continue
            prev = self._prev_transitions.get(name, tr)
            bump = tr - prev
            if bump > 0 and name != server_state:
                for _ in range(bump):
                    pending.append((name, "inferred"))
            self._prev_transitions[name] = tr

        if server_state and server_state != self._last_server_state:
            pending.append((server_state, "observed"))
            self._last_server_state = server_state

        if pending:
            ladder = {
                "disconnected": 0,
                "connected": 1,
                "syncing": 2,
                "tracking": 3,
                "full": 4,
            }
            # Stable sort — within a ladder tie, the original relative
            # insertion order is preserved, which keeps the observed
            # entry at the end of its tie group (it was appended last).
            pending.sort(key=lambda p: ladder.get(p[0], 99))
            for name, source in pending:
                self._state_log.append((now, name, source))

        if len(self._state_log) > self._STATE_LOG_MAX:
            del self._state_log[: len(self._state_log) - self._STATE_LOG_MAX]

    def update_ledgers_info(self, info: Optional[Dict[str, Any]]) -> None:
        if not isinstance(info, dict) or not info:
            self._content.update(
                Text(
                    "Not emitted by this rippled build — requires 'ledgers_info' RPC.",
                    style="dim",
                )
            )
            return
        # Response might arrive wrapped in "result" (xrpl-py convention).
        if "result" in info and isinstance(info["result"], dict):
            info = info["result"]
        # Some handlers wrap under "ledger_state"; others return flat.
        nested = info.get("ledger_state")
        root: Dict[str, Any] = nested if isinstance(nested, dict) else info
        self._content.update(self._build_view(root))

    def _build_view(self, ls: Dict[str, Any]) -> Group:
        # Layout: everything interesting stacks in the LEFT column —
        # pointers + gaps side-by-side at the top, then ranges,
        # inbound, and the legend below. The raw JSON parks in the
        # RIGHT column as a reference that never steals width from
        # the inbound-detail table (which has many cryptic cols and
        # gets unreadable at ~50% of terminal width).
        inner = Table.grid(expand=True, padding=(0, 1))
        inner.add_column(ratio=1)
        inner.add_column(ratio=1)
        inner.add_row(self._pointers_panel(ls), self._gaps_panel(ls))

        left_stack_items: List[Any] = [inner, Text("")]
        sa_panel = self._state_accounting_panel()
        if sa_panel is not None:
            left_stack_items += [sa_panel, Text("")]
        left_stack_items += [
            self._ranges_panel(ls),
            Text(""),
            self._inbound_panel(ls),
        ]
        peers_panel = self._peers_panel(ls)
        if peers_panel is not None:
            left_stack_items += [Text(""), peers_panel]
        left_stack_items += [Text(""), self._legend_panel()]
        left_stack = Group(*left_stack_items)

        outer = Table.grid(expand=True, padding=(0, 1))
        # Left gets the majority — inbound detail has ~11 columns that
        # need room. Right holds raw JSON purely for reference.
        outer.add_column(ratio=3)
        outer.add_column(ratio=1)
        outer.add_row(left_stack, self._raw_json_panel(ls))
        return Group(outer)

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _gaps_panel(self, ls: Dict[str, Any]) -> Panel:
        gaps = ls.get("gaps") or {}
        net = ls.get("network") or {}
        loc = ls.get("local") or {}
        hv_seq = _g(net, "highest_validation_seen", "seq", default=0)
        val_seq = _g(loc, "validated", "seq", default=0)
        max_inbound = _max_seq_in_ranges(_g(loc, "inbound_acquiring", "ranges", default=None))

        # "0" is ambiguous in the JSON: could be a real zero OR a
        # "not populated" default from the rippled side (the highest-
        # validated-seen counter isn't wired on older builds).
        hv_known = bool(hv_seq) and int(hv_seq) > 0

        publish = _g(gaps, "awaiting_publish", default=0)
        close = _g(gaps, "close_to_validate", default=0)
        raw_behind = _g(gaps, "behind_network", default=0)

        # Sanity-check rippled's behind_network against the inbound-
        # acquire tip. Rippled has been caught returning nonsense (e.g.
        # the full ledger seq) when highest_validated_seen is 0 or when
        # local.validated.seq is miscomputed for the response. If the
        # inferred-from-inbound value diverges wildly from what rippled
        # claims, show rippled's as [suspect] and prefer the inferred
        # value in the diagnosis.
        inferred_behind: Optional[int] = None
        if isinstance(val_seq, int) and val_seq > 0 and max_inbound is not None:
            inferred_behind = max(0, max_inbound - val_seq)

        behind_for_diag: Any = None
        rippled_suspect = False
        if hv_known:
            try:
                rb = int(raw_behind)
            except (TypeError, ValueError):
                rb = 0
            # Absurd if > 100k — real networks never lag that far during
            # normal operation; this is the "math on uninitialised state"
            # signature we've seen.
            if rb > 100_000 or (inferred_behind is not None and rb > inferred_behind * 5 + 50):
                rippled_suspect = True
                behind_for_diag = inferred_behind
            else:
                behind_for_diag = rb
        else:
            behind_for_diag = inferred_behind

        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=3)
        tbl.add_column(justify="right", no_wrap=True, ratio=1)
        tbl.add_column(style="dim", no_wrap=False, ratio=4)

        behind_hint = "validator tip − our validated"
        if not hv_known:
            behind_hint += "  [yellow](highest_validated_seen not emitted by this build)[/yellow]"
        if rippled_suspect:
            behind_cell = f"[dim strike]{_fmt_int(raw_behind)}[/dim strike]"
            behind_hint = (
                "rippled value looks bogus (highest_validated_seen or "
                "local.validated miscomputed); see inferred_gap below"
            )
        elif hv_known and behind_for_diag is not None:
            behind_cell = self._styled_gap(behind_for_diag)
        else:
            behind_cell = "[dim]?[/dim]"
        tbl.add_row("behind_network", behind_cell, behind_hint)

        tbl.add_row("awaiting_publish", self._styled_gap(publish), "validated − published")
        tbl.add_row("close_to_validate", self._styled_gap(close), "closed − validated")

        if inferred_behind is not None:
            tbl.add_row(
                "inferred_gap",
                self._styled_gap(inferred_behind),
                "max(inbound_acquiring) − local.validated",
            )

        diagnosis = self._diagnose(behind_for_diag, publish, close)
        tbl.add_row("[bold cyan]diagnosis[/bold cyan]", "", f"[bold]{diagnosis}[/bold]")

        return Panel(tbl, title="[bold]Gaps[/bold]", border_style="magenta")

    def _pointers_panel(self, ls: Dict[str, Any]) -> Panel:
        net = ls.get("network") or {}
        loc = ls.get("local") or {}

        hv = net.get("highest_validation_seen") or {}
        pref = net.get("preferred") or {}

        closed = loc.get("closed") or {}
        validated = loc.get("validated") or {}
        published = loc.get("published") or {}
        building = loc.get("building") or {}

        # 3-col compact table. Tooltip-style inline hint removed from
        # the row itself (would force the whole panel wider than any
        # display values warrant); hints live on row 1 in dim italics
        # above each subsection.
        tbl = Table(
            show_header=True, header_style="bold cyan", box=None, expand=True, pad_edge=False
        )
        tbl.add_column("Pointer", style="yellow", ratio=3)
        tbl.add_column("Seq", justify="right", style="green", ratio=2)
        tbl.add_column("Info", style="dim", ratio=3)

        def _row(name: str, seq: Any, info: str) -> None:
            tbl.add_row(name, _fmt_int(seq), info)

        _row(
            "net.highest_seen",
            hv.get("seq"),
            f"signers {_fmt_int(hv.get('signers_seen'))}/{_fmt_int(hv.get('quorum_needed'))}",
        )
        if pref:
            _row("net.preferred", pref.get("seq"), _fmt_hash(pref.get("hash")))

        tbl.add_row("", "", "")

        if closed:
            _row(
                "local.closed",
                closed.get("seq"),
                "validated" if closed.get("validated") else "unvalidated",
            )
        if validated:
            _row(
                "local.validated",
                validated.get("seq"),
                f"age {_fmt_int(validated.get('sign_time_age_s'))}s",
            )
        if published:
            _row("local.published", published.get("seq"), "")
        if building:
            # Building row uses rippled's actual field names (not the
            # design-doc aliases): proposers, converge_percent,
            # current_ms, len(disputes), len(acquired), and the parent
            # ledger hash from our_position.previous_ledger. Compact
            # summary — full detail lives in the raw JSON panel below.
            phase = building.get("phase") or ""
            disputes = building.get("disputes") or {}
            acquired = building.get("acquired") or []
            dcount = len(disputes) if isinstance(disputes, (dict, list)) else 0
            acount = len(acquired) if isinstance(acquired, list) else 0
            flags = []
            for f in ("proposing", "validating", "synched", "have_time_consensus"):
                if building.get(f):
                    flags.append(f[:4])
            info_bits: List[str] = []
            if phase:
                info_bits.append(phase)
            info_bits.append(f"p={_fmt_int(building.get('proposers'))}")
            if "converge_percent" in building:
                info_bits.append(f"conv={_fmt_int(building.get('converge_percent'))}%")
            if "current_ms" in building:
                info_bits.append(f"ms={_fmt_int(building.get('current_ms'))}")
            if dcount:
                info_bits.append(f"disp={dcount}")
            if acount:
                info_bits.append(f"acq={acount}")
            if flags:
                info_bits.append("[" + ",".join(flags) + "]")
            _row(
                "local.building",
                None,
                " · ".join(info_bits),
            )

        return Panel(tbl, title="[bold]Pointers[/bold]", border_style="cyan")

    def _ranges_panel(self, ls: Dict[str, Any]) -> Panel:
        loc = ls.get("local") or {}
        retained = loc.get("retained") or {}
        complete = loc.get("complete") or {}
        missing = loc.get("missing") or {}

        tbl = Table(
            show_header=True, header_style="bold cyan", box=None, expand=True, pad_edge=False
        )
        tbl.add_column("Range", style="yellow", ratio=2)
        tbl.add_column("N", justify="right", style="green", ratio=1)
        tbl.add_column("Seqs", style="dim", ratio=5)

        def _row(name: str, entry: Dict[str, Any], suffix: str = "") -> None:
            ranges = entry.get("ranges") or "-"
            count = _fmt_int(entry.get("count"))
            tbl.add_row(name, count, f"{ranges}{('  ' + suffix) if suffix else ''}")

        target = retained.get("target")
        _row(
            "retained",
            retained,
            f"target={_fmt_int(target)}" if target is not None else "",
        )
        _row("complete", complete)
        _row("missing", missing)

        return Panel(tbl, title="[bold]Ranges[/bold]", border_style="green")

    def _inbound_panel(self, ls: Dict[str, Any]) -> Panel:
        loc = ls.get("local") or {}
        inbound = loc.get("inbound_acquiring") or {}
        replaying = loc.get("replaying") or {}

        inbound_count = _fmt_int(inbound.get("count"))
        inbound_ranges = inbound.get("ranges") or "-"
        replay_count = _fmt_int(replaying.get("count"))
        replay_ranges = replaying.get("ranges") or "-"

        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=2)
        tbl.add_column(justify="right", style="green", no_wrap=True, ratio=1)
        tbl.add_column(style="dim", no_wrap=False, ratio=5)

        tbl.add_row("inbound_acquiring", inbound_count, inbound_ranges)
        tbl.add_row("replaying", replay_count, replay_ranges)

        # RIPPLED_PRIORITY_QUORUM_IBL experiment exposes which single
        # IBL is currently holding priority — trigger() is suppressed
        # on all others while that one completes. Only present when
        # the experiment flag is on and a priority IBL exists.
        prio_hash = inbound.get("priority_quorum_hash")
        if prio_hash:
            tbl.add_row(
                "priority_quorum",
                "",
                f"[bold yellow]★[/bold yellow] [dim]{_fmt_hash(prio_hash)}[/dim]",
            )

        # Rippled emits details as a dict keyed by seq-string; the
        # design doc originally spec'd a list. Accept both so the
        # handler shape doesn't force us to code against one.
        raw_details = inbound.get("details")
        details: Optional[List[Dict[str, Any]]] = None
        if isinstance(raw_details, list):
            details = [d for d in raw_details if isinstance(d, dict)]
        elif isinstance(raw_details, dict):
            details = []
            for seq_key, val in raw_details.items():
                if not isinstance(val, dict):
                    continue
                try:
                    seq = int(seq_key)
                except (TypeError, ValueError):
                    seq = val.get("seq") or 0
                details.append({"seq": seq, **val})
            details.sort(key=lambda d: int(d.get("seq") or 0))
        if details:
            tbl.add_row("", "", "")
            tbl.add_row(
                "[bold]inbound detail[/bold]",
                "",
                "[dim]admin-only per-acquire breakdown[/dim]",
            )
            dt = Table(
                show_header=True, header_style="bold cyan", box=None, expand=True, pad_edge=False
            )
            dt.add_column("seq", justify="right", style="yellow")
            # Reason compact letter (C=consensus, H=history, G=generic,
            # S=shard). Dim "?" if the wire payload didn't include it.
            dt.add_column("r", style="bold magenta")
            # Age of the IBL from rippled's `age_ms` (ms since IBL
            # construction). Pairs with *_rounds to eyeball rate; a
            # 40s IBL with R3 state is basically idle.
            dt.add_column("age", justify="right", style="blue")
            # held = age_ms − last_admit_ms. Time since the admission
            # policy last let this IBL run trigger(). 0 = ran just now;
            # climbing = policy is suppressing it; ∅ = never admitted.
            dt.add_column("held", justify="right", style="magenta")
            # Short-code for the most recent admission-policy decision:
            # admitted / publish-blocker / frontier / held /
            # held-bootstrap / force-sweep / default. Explains *why* the
            # held value is what it is.
            dt.add_column("policy", style="white")
            dt.add_column("have", style="green")
            # Skip-list probe lifecycle: P=probe_sent / K=have_skip /
            # H=skip_harvested. Lowercase = false for that flag. When
            # all three are off the whole cell dims so only IBLs with
            # skip-list activity catch the eye.
            dt.add_column("skip", style="cyan")
            # state: inserted / requested / unique-first-claim / rounds,
            # followed by a per-poll Δ column for the same tuple.
            dt.add_column("state i/r/u/R", justify="right", style="cyan")
            dt.add_column("Δ state", justify="right", style="magenta")
            dt.add_column("tx i/r/u/R", justify="right", style="cyan")
            dt.add_column("Δ tx", justify="right", style="magenta")
            # stall S/T: current idle gap + peak ever observed, per side.
            # Current = last_request.t − last_response.t (positive iff
            # we've asked after our last reply, i.e. request outstanding).
            # Peak is latched in the widget across polls so transient
            # spikes don't flicker away. 0.1s precision.
            dt.add_column("stall S/T (cur↑peak)", justify="right", style="yellow")
            # resp-gap S/T: min..max inter-response gap in the event
            # array for this IBL, per side. Shows fastest/slowest reply
            # cadence at a glance. Computed fresh each poll from the
            # full event vector — no latching needed.
            dt.add_column("resp gap S/T (min/avg/max)", justify="right", style="green")
            dt.add_column("peers", justify="right", style="dim")
            dt.add_column("t/o", justify="right", style="dim")
            # Build the snapshot we'll stash for next poll's deltas.
            # Keyed by seq, same field names as the wire payload so the
            # delta helper can read both prev + this with identical keys.
            next_snapshot: Dict[int, Dict[str, int]] = {}

            for d in details[:20]:
                if not isinstance(d, dict):
                    continue

                # rippled emits have_{header,state,transactions} as
                # booleans; condense into a flag string like "HST" with
                # lowercase for missing.
                #
                # Progress numbers come from patched rippled's per-IBL
                # atomic counters (see InboundLedger.h):
                #   *_nodes_inserted   — node additions that returned isGood
                #   *_requests_sent    — hashes asked of peers (pre-fanout)
                #   *_unique_hashes    — first-claim winners against the
                #                        InboundLedgers hash registry; the
                #                        delta from requests_sent is the
                #                        cross-IBL duplication this IBL
                #                        would have avoided had it
                #                        consulted a shared claim table
                #   *_rounds           — getMissingNodes descent cycles
                #                        (batches emitted, NOT peer fans)
                def _flag(key: str, letter: str) -> str:
                    return letter if d.get(key) else letter.lower()

                have_str = (
                    _flag("have_header", "H")
                    + _flag("have_state", "S")
                    + _flag("have_transactions", "T")
                )

                # Skip-list probe lifecycle flags. P=probe_sent,
                # K=have_skip, H=skip_harvested. Whole cell dims when
                # no skip-list activity has fired — the producer omits
                # these keys entirely on old builds, which naturally
                # renders as "pkh" dim.
                sp = bool(d.get("skip_probe_sent"))
                sk = bool(d.get("have_skip"))
                sh = bool(d.get("skip_harvested"))
                skip_letters = ("P" if sp else "p") + ("K" if sk else "k") + ("H" if sh else "h")
                if not (sp or sk or sh):
                    skip_cell = f"[dim]{skip_letters}[/dim]"
                else:
                    # Colour each letter independently so the trail of
                    # progress is readable: grey = off, cyan = probe,
                    # green = landed+harvested.
                    parts: List[str] = []
                    parts.append("[cyan]P[/cyan]" if sp else "[dim]p[/dim]")
                    parts.append("[green]K[/green]" if sk else "[dim]k[/dim]")
                    parts.append("[green]H[/green]" if sh else "[dim]h[/dim]")
                    skip_cell = "".join(parts)

                seq_val = d.get("seq")
                try:
                    seq_int = int(seq_val) if seq_val is not None else None
                except (TypeError, ValueError):
                    seq_int = None
                prev = self._prev_inbound.get(seq_int) if seq_int is not None else None

                # Count "burrow" rounds from the state_responses array —
                # rounds that made no useful SLE progress. A round
                # qualifies when *either* of these holds (OR, not
                # exclusive — a single round can satisfy both):
                #   - bc1_inners > 0: the reply added branch-count-1
                #     inner nodes (single-child pass-throughs, a.k.a.
                #     pure single-path skeleton walking down a deep
                #     subtree). Sharper signal than "n == inners"
                #     because it excludes genuine multi-branch inners,
                #     which represent real discovery.
                #   - leaves exist and every non-zero type in the
                #     histogram is DirectoryNode: the book-base
                #     burrowing tail (offer-book directory pages).
                state_burrow: Optional[int] = None
                resp_arr = d.get("state_responses")
                if isinstance(resp_arr, list):
                    burrow = 0
                    for ev in resp_arr:
                        if not isinstance(ev, dict):
                            continue
                        try:
                            n = int(ev.get("n") or 0)
                            inners = int(ev.get("inners") or 0)
                            bc1_inners = int(ev.get("bc1_inners") or 0)
                        except (TypeError, ValueError):
                            continue
                        leaves = max(0, n - inners)
                        types_raw = ev.get("types")
                        ev_types: Dict[Any, Any] = types_raw if isinstance(types_raw, dict) else {}
                        only_dir_leaves = (
                            leaves > 0
                            and len(ev_types) > 0
                            and all((v == 0 or k == "DirectoryNode") for k, v in ev_types.items())
                        )
                        if bc1_inners > 0 or only_dir_leaves:
                            burrow += 1
                    state_burrow = burrow

                def _quad(
                    ins_k: str,
                    req_k: str,
                    uniq_k: str,
                    rounds_k: str,
                    burrow: Optional[int] = None,
                ) -> str:
                    ins = int(d.get(ins_k) or 0)
                    req = int(d.get(req_k) or 0)
                    uniq = d.get(uniq_k)
                    rounds = d.get(rounds_k)
                    if not (ins or req or uniq or rounds):
                        return "-"
                    parts = [f"{_fmt_int(ins)}/{_fmt_int(req)}"]
                    parts.append(_fmt_int(uniq) if uniq is not None else "-")
                    if rounds is not None:
                        rounds_str = f"R{_fmt_int(rounds)}"
                        # Append "(N)" burrow-count when present and
                        # non-zero — noisy to print (0) for the healthy
                        # common case, so keep it off when zero.
                        if burrow is not None and burrow > 0:
                            rounds_str += f"[yellow]({burrow})[/yellow]"
                        parts.append(rounds_str)
                    else:
                        parts.append("-")
                    return "/".join(parts)

                def _quad_delta(
                    side: str, ins_k: str, req_k: str, uniq_k: str, rounds_k: str
                ) -> str:
                    # No prior reading for this seq — nothing to diff.
                    # Render a dim "·" so the column shape stays intact.
                    if prev is None or seq_int is None:
                        return "[dim]·[/dim]"

                    def _d(k: str) -> Optional[int]:
                        cur = d.get(k)
                        old = prev.get(k) if prev else None
                        if cur is None and old is None:
                            return None
                        try:
                            return int(cur or 0) - int(old or 0)
                        except (TypeError, ValueError):
                            return None

                    di = _d(ins_k)
                    dr = _d(req_k)
                    du = _d(uniq_k)
                    drs = _d(rounds_k)
                    key = (seq_int, side)
                    if di is None and dr is None and du is None and drs is None:
                        return "-"
                    if not any((di, dr, du, drs)):
                        # Quiet poll — nothing moved for this IBL. Reuse
                        # the last non-zero Δ string so the column
                        # doesn't blink to blank; dim it + prefix ⏸ to
                        # signal 'stale, unchanged since last poll'.
                        prior = self._last_delta_cell.get(key)
                        if prior:
                            return f"[dim]⏸ {prior}[/dim]"
                        return "[dim]·[/dim]"

                    def _s(v: Optional[int]) -> str:
                        if v is None:
                            return "-"
                        if v > 0:
                            return f"[green]+{v:,}[/green]"
                        if v < 0:
                            return f"[red]{v:,}[/red]"
                        return "[dim]0[/dim]"

                    parts = [_s(di), _s(dr), _s(du)]
                    parts.append("[dim]·[/dim]" if drs in (None, 0) else _s(drs))
                    rendered = "/".join(parts)
                    self._last_delta_cell[key] = rendered
                    return rendered

                state_cell = _quad(
                    "state_nodes_inserted",
                    "state_requests_sent",
                    "state_unique_hashes",
                    "state_rounds",
                    burrow=state_burrow,
                )
                dstate_cell = _quad_delta(
                    "state",
                    "state_nodes_inserted",
                    "state_requests_sent",
                    "state_unique_hashes",
                    "state_rounds",
                )
                tx_cell = _quad(
                    "tx_nodes_inserted",
                    "tx_requests_sent",
                    "tx_unique_hashes",
                    "tx_rounds",
                )
                dtx_cell = _quad_delta(
                    "tx",
                    "tx_nodes_inserted",
                    "tx_requests_sent",
                    "tx_unique_hashes",
                    "tx_rounds",
                )

                # Helpers for event-array driven metrics. Arrays are
                # `[{"t": <ms-since-IBL-start>, "n": <count>}, ...]`.
                def _sorted_ts(key: str) -> List[int]:
                    evs = d.get(key)
                    if not isinstance(evs, list):
                        return []
                    out: List[int] = []
                    for ev in evs:
                        if not isinstance(ev, dict):
                            continue
                        t_val = ev.get("t")
                        if t_val is None:
                            continue
                        try:
                            out.append(int(t_val))
                        except (TypeError, ValueError):
                            continue
                    out.sort()
                    return out

                def _max_t(key: str) -> Optional[int]:
                    ts = _sorted_ts(key)
                    return ts[-1] if ts else None

                def _fmt_s(ms: int) -> str:
                    # 0.1s precision — user asked for finer resolution
                    # than whole seconds so sub-second flickers show up.
                    secs = ms / 1000.0
                    return f"{secs:.1f}s"

                def _stall(side: str, req_key: str, resp_key: str) -> str:
                    last_req = _max_t(req_key)
                    last_resp = _max_t(resp_key)
                    key = (seq_int, side) if seq_int is not None else None
                    if last_req is None and last_resp is None:
                        return "[dim]·[/dim]"
                    if last_req is None:
                        return "[dim]0.0s[/dim]"
                    if last_resp is None:
                        # Asked, no reply ever — pure stall. Latch the
                        # magnitude as the peak so you can see it grew.
                        gap_ms = last_req
                        if key is not None:
                            self._peak_idle_ms[key] = max(self._peak_idle_ms.get(key, 0), gap_ms)
                        peak = self._peak_idle_ms.get(key, gap_ms) if key else gap_ms
                        return f"[red]∅↑{_fmt_s(peak)}[/red]" if peak >= 2000 else "[red]∅[/red]"
                    cur_ms = max(0, last_req - last_resp)
                    if key is not None:
                        self._peak_idle_ms[key] = max(self._peak_idle_ms.get(key, 0), cur_ms)
                    peak_ms = self._peak_idle_ms.get(key, cur_ms) if key else cur_ms

                    def _col(ms: int) -> str:
                        s = _fmt_s(ms)
                        if ms < 2000:
                            return f"[dim]{s}[/dim]"
                        if ms < 10_000:
                            return s
                        return f"[red]{s}[/red]"

                    return f"{_col(cur_ms)}↑{_col(peak_ms)}"

                stall_cell = (
                    f"{_stall('state', 'state_requests', 'state_responses')}"
                    f"/{_stall('tx', 'tx_requests', 'tx_responses')}"
                )

                def _resp_gap(resp_key: str) -> str:
                    """min/avg/max ms between consecutive responses."""
                    ts = _sorted_ts(resp_key)
                    if len(ts) < 2:
                        return "[dim]·[/dim]"
                    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
                    if not gaps:
                        return "[dim]·[/dim]"
                    lo = min(gaps)
                    hi = max(gaps)
                    avg = sum(gaps) // len(gaps)
                    return f"{_fmt_s(lo)}/{_fmt_s(avg)}/{_fmt_s(hi)}"

                respgap_cell = f"{_resp_gap('state_responses')}/{_resp_gap('tx_responses')}"

                # Reason lookup. Rippled-side Reason enum collapsed to a
                # single-letter tag. Accept either the raw enum name or
                # a pre-normalised short form, whichever the RPC emits.
                reason_raw = str(d.get("reason") or "").upper()
                reason_cell = {
                    "CONSENSUS": "C",
                    "HISTORY": "H",
                    "GENERIC": "G",
                    "SHARD": "S",
                    "C": "C",
                    "H": "H",
                    "G": "G",
                    "S": "S",
                }.get(reason_raw, "[dim]?[/dim]")

                # Age formatting: seconds with 1dp under a minute,
                # `MmSs` above — 1m23s reads faster than 83.4s when
                # scanning a column of mixed magnitudes.
                age_ms_val = d.get("age_ms")
                try:
                    age_ms = int(age_ms_val) if age_ms_val is not None else None
                except (TypeError, ValueError):
                    age_ms = None

                def _fmt_age(ms: Optional[int], red_from_ms: int = 120_000) -> str:
                    if ms is None:
                        return "[dim]·[/dim]"
                    if ms < 60_000:
                        return f"{ms / 1000:.1f}s"
                    total = ms // 1000
                    mins, secs = divmod(total, 60)
                    s = f"{mins}m{secs:02d}s"
                    return f"[red]{s}[/red]" if ms >= red_from_ms else s

                age_cell = _fmt_age(age_ms)

                # Held = time since last admit. last_admit_ms == 0 means
                # never admitted (or freshly created — we can't tell on
                # an IBL with age ~0 either, so we only flag ∅ once the
                # IBL has been around long enough for the distinction to
                # matter — say >1s of age with no admit).
                last_admit_raw = d.get("last_admit_ms")
                try:
                    last_admit_ms = int(last_admit_raw) if last_admit_raw is not None else None
                except (TypeError, ValueError):
                    last_admit_ms = None
                if age_ms is None or last_admit_ms is None:
                    held_cell = "[dim]·[/dim]"
                elif last_admit_ms == 0:
                    held_cell = "[dim]∅[/dim]" if age_ms < 1_000 else "[red]∅[/red]"
                else:
                    held_cell = _fmt_age(max(0, age_ms - last_admit_ms), red_from_ms=10_000)

                # Admission-policy trail from the producer's
                # policy_history array: [{t, reason}, ...] with one
                # entry per transition (producer appends only on
                # reason-change). Show the last N as a compact chain of
                # coloured letters; the current state (rightmost) is
                # bold. Fall back to the single policy_reason field on
                # builds without the full history.
                # Letter scheme is unambiguous under the renamed codes
                # (held-bootstrap → bootstrap, force-sweep → evict),
                # and we keep the old names mapped to the same glyphs
                # so this dashboard works against older rippled builds
                # too.
                pol_letter_map = {
                    "admitted": ("A", "green"),
                    "publish-blocker": ("P", "yellow"),
                    "publish-window": ("W", "yellow"),
                    "frontier": ("F", "cyan"),
                    "frontier-no-tx": ("N", "cyan"),
                    "held": ("H", "magenta"),
                    "bootstrap": ("B", "magenta"),
                    "held-bootstrap": ("B", "magenta"),
                    "evict": ("E", "red"),
                    "force-sweep": ("E", "red"),
                    # Cold-catch-up admissions for historical backfill
                    # work: completer finishes the last N retention
                    # entries, support keeps the fetch-pack pump primed.
                    "cold-completer": ("C", "green"),
                    "cold-support": ("S", "green"),
                    # Tip IBL sub-states in CATCHUP: tip-header-only is
                    # pre-state-probe ("we just want the header"),
                    # tip-skip-probe is the active skip-list probe phase.
                    "tip-header-only": ("O", "cyan"),
                    "tip-skip-probe": ("Q", "cyan"),
                    # Pre-header IBL (mySeq == 0). Admitted unconditionally
                    # until the header arrives and seq is known.
                    "pending-seq": ("?", "blue"),
                    "default": ("·", "dim"),
                }
                raw_hist = d.get("policy_history")
                trail_codes: List[str] = []
                if isinstance(raw_hist, list):
                    for ev in raw_hist:
                        if isinstance(ev, dict):
                            r = ev.get("reason")
                            if r is not None:
                                trail_codes.append(str(r))
                if not trail_codes:
                    # Fallback: older rippled with only policy_reason.
                    pol_raw = d.get("policy_reason")
                    if pol_raw is not None:
                        trail_codes.append(str(pol_raw))

                if len(trail_codes) > self._POLICY_HISTORY_MAX:
                    trail_codes = trail_codes[-self._POLICY_HISTORY_MAX :]

                if not trail_codes:
                    policy_cell = "[dim]·[/dim]"
                else:
                    trail_parts: List[str] = []
                    for i, code in enumerate(trail_codes):
                        letter, colour = pol_letter_map.get(code, (code[:1].upper(), "white"))
                        style = colour if i < len(trail_codes) - 1 else f"bold {colour}"
                        trail_parts.append(f"[{style}]{letter}[/{style}]")
                    policy_cell = "→".join(trail_parts)

                peers = d.get("peers") if d.get("peers") is not None else d.get("peers_asked")
                seq_disp = _fmt_int(d.get("seq"))
                if d.get("priority_quorum"):
                    # ★ prefix flags the single IBL currently holding
                    # priority under RIPPLED_PRIORITY_QUORUM_IBL — the
                    # one suppressing trigger() on all the others.
                    seq_disp = f"[bold yellow]★[/bold yellow] {seq_disp}"
                dt.add_row(
                    seq_disp,
                    reason_cell,
                    age_cell,
                    held_cell,
                    policy_cell,
                    have_str,
                    skip_cell,
                    state_cell,
                    dstate_cell,
                    tx_cell,
                    dtx_cell,
                    stall_cell,
                    respgap_cell,
                    _fmt_int(peers),
                    _fmt_int(d.get("timeouts")),
                )

                # Snapshot the counters we'll diff against next poll.
                if seq_int is not None:
                    next_snapshot[seq_int] = {
                        k: int(d.get(k) or 0)
                        for k in (
                            "state_nodes_inserted",
                            "state_requests_sent",
                            "state_unique_hashes",
                            "state_rounds",
                            "tx_nodes_inserted",
                            "tx_requests_sent",
                            "tx_unique_hashes",
                            "tx_rounds",
                        )
                    }

            # Replace (not merge) — seqs that rolled off should stop
            # contributing spurious "still here" deltas if they come
            # back, and RAM stays bounded to the live-acquire set.
            self._prev_inbound = next_snapshot
            # Prune the last-rendered Δ cache to the same live set so
            # the stale-reuse path doesn't resurrect Δs for seqs that
            # have since completed + re-entered the table.
            live = set(next_snapshot.keys())
            self._last_delta_cell = {k: v for k, v in self._last_delta_cell.items() if k[0] in live}
            self._peak_idle_ms = {k: v for k, v in self._peak_idle_ms.items() if k[0] in live}
            return Panel(
                Group(tbl, dt),
                title="[bold]Inbound / Replay[/bold]",
                border_style="yellow",
            )

        return Panel(tbl, title="[bold]Inbound / Replay[/bold]", border_style="yellow")

    def _state_accounting_panel(self) -> Optional[Panel]:
        """Compact row of per-state durations + transition counts.

        Input shape (from server_info.state_accounting): ::

            {
              "full":         {"duration_us": "153220067", "transitions": "1"},
              "syncing":      {"duration_us": "9595533",   "transitions": "1"},
              "tracking":     {"duration_us": "20",        "transitions": "1"},
              "connected":    {"duration_us": "60098626",  "transitions": "2"},
              "disconnected": {"duration_us": "1045263",   "transitions": "2"},
            }

        Render order echoes the server-state ladder (disconnected →
        connected → syncing → tracking → full) so you read left-to-
        right as "how the node climbed to full". Transition counts
        above 1 mean the node has dropped back — highlight yellow.
        """
        sa = self._state_accounting
        if not isinstance(sa, dict) or not sa:
            return None

        order = ("disconnected", "connected", "syncing", "tracking", "full")

        def _fmt_dur(us_str: Any) -> str:
            try:
                us = int(us_str)
            except (TypeError, ValueError):
                return "-"
            secs = us / 1_000_000
            if secs < 1:
                ms = us / 1000
                return f"{ms:.0f}ms"
            if secs < 60:
                return f"{secs:.1f}s"
            mins, rem = divmod(int(secs), 60)
            if mins < 60:
                return f"{mins}m{rem:02d}s"
            hrs, mrem = divmod(mins, 60)
            return f"{hrs}h{mrem:02d}m"

        colour_for = {
            "disconnected": "red",
            "connected": "yellow",
            "syncing": "cyan",
            "tracking": "magenta",
            "full": "green",
        }

        bits: List[str] = []
        for name in order:
            entry = sa.get(name)
            if not isinstance(entry, dict):
                continue
            dur = _fmt_dur(entry.get("duration_us"))
            try:
                tr = int(entry.get("transitions") or 0)
            except (TypeError, ValueError):
                tr = 0
            c = colour_for.get(name, "white")
            tr_part = f"[yellow]×{tr}[/yellow]" if tr > 1 else f"[dim]×{tr}[/dim]"
            bits.append(f"[{c}]{name}[/{c}] {dur} {tr_part}")

        # Append any non-canonical states (defensive — schema changes).
        for name, entry in sa.items():
            if name in order or not isinstance(entry, dict):
                continue
            dur = _fmt_dur(entry.get("duration_us"))
            try:
                tr = int(entry.get("transitions") or 0)
            except (TypeError, ValueError):
                tr = 0
            bits.append(f"[white]{name}[/white] {dur} [dim]×{tr}[/dim]")

        if not bits:
            return None

        totals_line = Text.from_markup("  ·  ".join(bits))

        # Timeline of observed transitions. Uses our client-side log
        # (see update_state_accounting) so entries carry ordering +
        # timestamps beyond what the server's totals-only counters
        # reveal. Dim (inferred) entries are transitions we missed at
        # the poll boundary but reconstructed from counter bumps.
        timeline = self._state_timeline_line()

        if timeline is None:
            return Panel(
                totals_line,
                title="[bold]State Accounting[/bold]",
                border_style="blue",
            )
        return Panel(
            Group(totals_line, Text(""), timeline),
            title="[bold]State Accounting[/bold]",
            border_style="blue",
        )

    def _state_timeline_line(self) -> Optional[Text]:
        if not self._state_log:
            return None
        colour_for = {
            "disconnected": "red",
            "connected": "yellow",
            "syncing": "cyan",
            "tracking": "magenta",
            "full": "green",
        }

        def _fmt_ts(secs: float) -> str:
            if secs < 60:
                return f"{secs:.1f}s"
            mins, rem = divmod(int(secs), 60)
            if mins < 60:
                return f"{mins}m{rem:02d}s"
            hrs, mrem = divmod(mins, 60)
            return f"{hrs}h{mrem:02d}m"

        bits: List[str] = []
        for ts, state, source in self._state_log:
            c = colour_for.get(state, "white")
            label = f"[{c}]{state}[/{c}]"
            t_str = f"@{_fmt_ts(ts)}"
            if source == "inferred":
                # Dim + parens so the eye can distinguish a reconstructed
                # visit from one we caught live.
                bits.append(f"[dim]({label} {t_str})[/dim]")
            else:
                bits.append(f"{label} [dim]{t_str}[/dim]")
        return Text.from_markup(" → ".join(bits))

    def _peers_panel(self, ls: Dict[str, Any]) -> Optional[Panel]:
        """Per-peer aggregated stats from ``local.peers``.

        Producer-side aggregation is a union across every active IBL —
        these are exactly the peers selected for ledger acquisition, so
        this panel naturally belongs alongside the IBL view. Admin-only
        upstream; empty/None if unavailable.
        """
        loc = ls.get("local") or {}
        raw = loc.get("peers")
        if not isinstance(raw, list) or not raw:
            return None
        peers = [p for p in raw if isinstance(p, dict)]
        if not peers:
            return None

        def _ms(v: Any) -> str:
            try:
                n = int(v)
            except (TypeError, ValueError):
                return "-"
            if n <= 0:
                return "[dim]·[/dim]"
            s = f"{n}ms" if n < 1000 else f"{n / 1000:.1f}s"
            if n < 500:
                return f"[dim]{s}[/dim]"
            if n < 2_000:
                return s
            return f"[red]{s}[/red]"

        # Sort by peer_id ascending — stable positions poll-to-poll so
        # the eye can track a specific peer's row across ticks. Flags
        # on individual cells (red sent/unsol/flight) do the "where is
        # the problem" work without needing row reordering.
        def _peer_id_key(p: Dict[str, Any]) -> int:
            try:
                return int(p.get("peer_id") or 0)
            except (TypeError, ValueError):
                return 0

        peers = sorted(peers, key=_peer_id_key)

        tbl = Table(
            show_header=True,
            header_style="bold cyan",
            box=None,
            expand=True,
            pad_edge=False,
        )
        tbl.add_column("peer", justify="right", style="yellow")
        tbl.add_column("IBLs", justify="right", style="blue")
        tbl.add_column("sent", justify="right", style="cyan")
        tbl.add_column("rep", justify="right", style="green")
        tbl.add_column("unsol", justify="right", style="yellow")
        tbl.add_column("nodes", justify="right", style="cyan")
        tbl.add_column("flight", justify="right", style="magenta")
        tbl.add_column("min", justify="right")
        tbl.add_column("avg", justify="right")
        tbl.add_column("med", justify="right")
        tbl.add_column("max", justify="right")

        for p in peers:
            sent = int(p.get("sent", 0) or 0)
            replied = int(p.get("replied", 0) or 0)
            unsol = int(p.get("replied_unsolicited", 0) or 0)
            in_flight = int(p.get("in_flight", 0) or 0)

            # Flag "peer hoarding requests, never replies" and
            # "peer pushing stuff we didn't ask for" inline.
            sent_cell = _fmt_int(sent)
            if sent and replied == 0 and sent > 4:
                sent_cell = f"[bold red]{sent:,}[/bold red]"
            unsol_cell = _fmt_int(unsol) if unsol else "[dim]·[/dim]"
            if replied and unsol * 2 >= replied:
                unsol_cell = f"[bold red]{_fmt_int(unsol)}[/bold red]"
            flight_cell = _fmt_int(in_flight)
            if in_flight > 32:
                flight_cell = f"[red]{in_flight:,}[/red]"
            elif in_flight > 8:
                flight_cell = f"[yellow]{in_flight:,}[/yellow]"
            elif in_flight == 0:
                flight_cell = "[dim]0[/dim]"

            tbl.add_row(
                _fmt_int(p.get("peer_id")),
                _fmt_int(p.get("ibls_active")),
                sent_cell,
                _fmt_int(replied),
                unsol_cell,
                _fmt_int(p.get("nodes_received")),
                flight_cell,
                _ms(p.get("min_ms")),
                _ms(p.get("avg_ms")),
                _ms(p.get("median_ms")),
                _ms(p.get("max_ms")),
            )

        return Panel(
            tbl, title="[bold]Peers (selected for acquisition)[/bold]", border_style="cyan"
        )

    def _legend_panel(self) -> Panel:
        # Two-column grid: left column explains Gaps + Pointers + Ranges
        # shorthand, right column explains the cryptic Inbound detail
        # columns. Dim body text — the legend shouldn't outshine the
        # live data.
        def _row(tbl: Table, name: str, desc: str) -> None:
            tbl.add_row(name, desc)

        left = Table(show_header=False, box=None, expand=True, pad_edge=False)
        left.add_column(style="yellow", no_wrap=True, ratio=2)
        left.add_column(style="dim", no_wrap=False, ratio=5, overflow="fold")
        for name, desc in (
            ("net.highest_seen", "highest seq with a trusted validation"),
            ("net.preferred", "validator-preferred fork tip"),
            ("local.closed", "last ledger consensus agreed to close"),
            ("local.validated", "last ledger trusted quorum signed + we have"),
            ("local.published", "last ledger we told subscribers about"),
            ("local.building", "in-progress ledger (parent + phase)"),
            ("retained", "still held in RAM (target = config floor)"),
            ("complete", "we have the bytes (nodestore coverage)"),
            ("missing", "holes in [firstComplete..validated]"),
            ("inbound_acquiring", "actively pulling from peers"),
            ("replaying", "admin-triggered explicit replay"),
            ("behind_network", "seq lag vs validator-seen tip"),
            ("awaiting_publish", "validated, not yet told subscribers"),
            ("close_to_validate", "closed locally, no quorum yet"),
            ("inferred_gap", "max(inbound) − local.validated"),
        ):
            _row(left, name, desc)

        right = Table(show_header=False, box=None, expand=True, pad_edge=False)
        right.add_column(style="yellow", no_wrap=True, ratio=2)
        right.add_column(style="dim", no_wrap=False, ratio=5, overflow="fold")
        for name, desc in (
            ("r", "reason: C=consensus, H=history, G=generic, S=shard, ?=missing"),
            ("age", "time since IBL construction (red ≥2m)"),
            ("held", "time since last trigger() admit; ∅ = never admitted"),
            (
                "policy",
                "trail of transitions (newest bold): "
                "[green]A[/green]=admitted [yellow]P[/yellow]=publish-blocker "
                "[yellow]W[/yellow]=publish-window [cyan]F[/cyan]=frontier "
                "[cyan]N[/cyan]=frontier-no-tx [magenta]H[/magenta]=held "
                "[magenta]B[/magenta]=bootstrap [red]E[/red]=evict "
                "[green]C[/green]=cold-completer [green]S[/green]=cold-support "
                "[cyan]O[/cyan]=tip-header-only [cyan]Q[/cyan]=tip-skip-probe "
                "[blue]?[/blue]=pending-seq",
            ),
            ("★", "priority_quorum IBL — trigger() suppressed on others"),
            ("have", "HST flags — H=header, S=state, T=transactions; lower = missing"),
            (
                "skip",
                "skip-list probe lifecycle: [cyan]P[/cyan]=probe_sent "
                "[green]K[/green]=have_skip [green]H[/green]=harvested; lower = off",
            ),
            (
                "i/r/u/R",
                "inserted / requested / unique-first-claim / Rounds; "
                "state `R N([yellow]M[/yellow])` = M of N rounds were 'burrow' "
                "(bc1-inners or DirectoryNode-only leaves)",
            ),
            ("Δ", "per-poll delta; ⏸ = unchanged, last non-zero held"),
            ("stall cur↑peak", "gap (last-request − last-response); peak latched per IBL"),
            ("resp gap min/avg/max", "inter-response gap stats from event array"),
            ("∅", "asked, never replied (classic stall)"),
            ("·", "no data yet / first observation"),
            ("peers", "count of peers currently being asked"),
            ("t/o", "cumulative request-timeout retries for this IBL"),
            ("colour", "[dim]< 2s[/dim] · 2–10s · [red]≥ 10s[/red]"),
            ("peers", "per-peer aggregated send/reply/latency across IBLs (admin)"),
        ):
            _row(right, name, desc)

        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(left, right)
        return Panel(grid, title="[bold]Legend[/bold]", border_style="blue")

    def _raw_json_panel(self, ls: Dict[str, Any]) -> Panel:
        """Full pretty-printed JSON of the payload.

        Cached on id(ls) — the monitor typically hands us the same dict
        object until a new poll replaces it, so most observer ticks hit
        the cache and we skip re-serialising ~10KB every 2s. Plain Text
        rendering (not Syntax highlighting) because token-based
        highlighting on this volume was the main source of the UI
        stall on this tab.
        """
        cache_id = id(ls)
        if cache_id != self._raw_cache_id:
            # Strip local.building before dumping — that subtree alone
            # is ~80% of the payload (disputes dict keyed by tx hash,
            # peer_positions dict keyed by validator, acquired array).
            # The structured Pointers panel already summarises the
            # useful scalar fields; the raw panel is for eyeballing
            # everything ELSE, not for scrolling past 300 lines of
            # consensus internals.
            pruned: Dict[str, Any] = dict(ls)
            local = pruned.get("local")
            if isinstance(local, dict) and "building" in local:
                pruned_local = dict(local)
                pruned_local["building"] = "<summarised in Pointers panel>"
                pruned["local"] = pruned_local
            try:
                self._raw_cache_text = json.dumps(pruned, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                self._raw_cache_text = repr(pruned)
            self._raw_cache_id = cache_id
        return Panel(
            Text(self._raw_cache_text, style="dim", no_wrap=True),
            title="[bold]Raw JSON (local.building omitted)[/bold]",
            border_style="blue",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _styled_gap(v: Any) -> str:
        """Colour a gap count: 0 dim, small yellow, large red."""
        try:
            n = int(v)
        except (TypeError, ValueError):
            return "-"
        if n == 0:
            return "[dim]0[/dim]"
        if n < 5:
            return f"[yellow]{n:,}[/yellow]"
        return f"[bold red]{n:,}[/bold red]"

    @staticmethod
    def _diagnose(behind: Any, publish: int, close_v: int) -> str:
        """Map the 3-tuple to the diagnostic table from the design doc.

        ``behind`` may be None to signal 'highest_validated_seen not
        emitted by this rippled build' — we explicitly distinguish that
        from an honest zero so the diagnosis doesn't claim 'healthy' on
        a node we can't actually compare against the network.
        """
        try:
            p = int(publish)
            c = int(close_v)
        except (TypeError, ValueError):
            return "unknown"

        if behind is None:
            # Can't assess network lag — focus the diagnosis on publish +
            # close-to-validate only.
            if p == 0 and c == 0:
                return "[yellow]local healthy; network lag unknown[/yellow]"
            if p > 0 and c == 0:
                return "[yellow]publish thread backed up[/yellow]"
            if c > 0:
                return "[yellow]waiting for validations[/yellow]"
            return f"[dim]local: publish={p} close={c}; network lag unknown[/dim]"

        try:
            b = int(behind)
        except (TypeError, ValueError):
            return "unknown"
        if b == 0 and p == 0 and c == 0:
            return "[green]healthy[/green]"
        if b == 0 and p > 0 and c == 0:
            return "[yellow]publish thread backed up[/yellow]"
        if b > 0 and p == 0 and c == 0:
            return "[yellow]falling behind network[/yellow]"
        if b > 0 and p > 0 and c > 0:
            return "[bold red]full local stall (thrash)[/bold red]"
        if b == 0 and p == 0 and c > 0:
            return "[yellow]waiting for validations[/yellow]"
        return f"[dim]mixed: behind={b} publish={p} close={c}[/dim]"
