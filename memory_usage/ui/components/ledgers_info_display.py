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

    def compose(self) -> ComposeResult:
        yield self._content

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
        # Structured summary panels in a 2-col grid, then the raw JSON
        # payload below in a full-width scrollable panel. Outer widget
        # is VerticalScroll so the raw panel grows as tall as the
        # payload needs; user scrolls the widget to reach it.
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        # Pointers on the left — they're the primary "what state is
        # this node in" view. Gaps on the right summarises the delta
        # against the network.
        grid.add_row(self._pointers_panel(ls), self._gaps_panel(ls))
        grid.add_row(self._ranges_panel(ls), self._inbound_panel(ls))
        return Group(grid, Text(""), self._raw_json_panel(ls))

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _gaps_panel(self, ls: Dict[str, Any]) -> Panel:
        gaps = ls.get("gaps") or {}
        net = ls.get("network") or {}
        hv_seq = _g(net, "highest_validation_seen", "seq", default=0)

        # "0" is ambiguous in the JSON: it could be a real zero OR a
        # "not populated" default from the rippled side (the highest-
        # validated-seen counter isn't wired on older builds). Treat
        # seq=0 as unknown so behind_network shows '?' instead of a
        # misleadingly-green 0.
        hv_known = bool(hv_seq) and int(hv_seq) > 0

        publish = _g(gaps, "awaiting_publish", default=0)
        close = _g(gaps, "close_to_validate", default=0)

        if hv_known:
            behind: Any = _g(gaps, "behind_network", default=0)
            behind_cell = self._styled_gap(behind)
            diagnosis = self._diagnose(behind, publish, close)
        else:
            behind = None
            behind_cell = "[dim]?[/dim]"
            diagnosis = self._diagnose(None, publish, close)

        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=3)
        tbl.add_column(justify="right", no_wrap=True, ratio=1)
        tbl.add_column(style="dim", no_wrap=False, ratio=4)

        behind_hint = "validator tip − our validated"
        if not hv_known:
            behind_hint += "  [yellow](highest_validated_seen not emitted by this build)[/yellow]"
        tbl.add_row("behind_network", behind_cell, behind_hint)
        tbl.add_row("awaiting_publish", self._styled_gap(publish), "validated − published")
        tbl.add_row("close_to_validate", self._styled_gap(close), "closed − validated")
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
            dt.add_column("have", style="green")
            dt.add_column("need", style="red")
            dt.add_column("peers", justify="right", style="dim")
            dt.add_column("t/o", justify="right", style="dim")
            for d in details[:20]:
                if not isinstance(d, dict):
                    continue

                # rippled emits have_{header,state,transactions} as
                # booleans; condense into a flag string like "HST" with
                # lowercase for missing. needed_*_hashes are arrays we
                # show as total-to-fetch counts.
                def _flag(key: str, letter: str) -> str:
                    return letter if d.get(key) else letter.lower()

                have_str = (
                    _flag("have_header", "H")
                    + _flag("have_state", "S")
                    + _flag("have_transactions", "T")
                )
                need_state = d.get("needed_state_hashes")
                need_tx = d.get("needed_transaction_hashes")
                need_state_n = len(need_state) if isinstance(need_state, list) else 0
                need_tx_n = len(need_tx) if isinstance(need_tx, list) else 0
                need_str = ""
                if need_state_n or need_tx_n:
                    need_str = f"s{need_state_n}/t{need_tx_n}"
                peers = d.get("peers") if d.get("peers") is not None else d.get("peers_asked")
                dt.add_row(
                    _fmt_int(d.get("seq")),
                    have_str,
                    need_str,
                    _fmt_int(peers),
                    _fmt_int(d.get("timeouts")),
                )
            return Panel(
                Group(tbl, dt),
                title="[bold]Inbound / Replay[/bold]",
                border_style="yellow",
            )

        return Panel(tbl, title="[bold]Inbound / Replay[/bold]", border_style="yellow")

    def _legend_panel(self) -> Panel:
        # Keep the legend concise but exhaustive for the fields we render.
        # No code receipts here — this panel is for a reader scanning the
        # live view, not for auditing source.
        lines = [
            ("network.highest_validation_seen", "highest seq we've seen a trusted validation for"),
            ("network.preferred", "the validator-preferred fork tip"),
            ("local.closed", "last ledger consensus agreed to close"),
            ("local.validated", "last ledger the trusted quorum signed AND we have"),
            ("local.published", "last ledger we told subscribers about"),
            ("local.building", "the in-progress ledger (parent + phase)"),
            ("retained", "ledgers still held in RAM for service (target = config floor)"),
            ("complete", "ledgers we have the bytes for (nodestore coverage)"),
            ("missing", "holes inside [firstComplete..validated] — fill targets"),
            ("inbound_acquiring", "ledgers we're actively pulling from peers"),
            ("replaying", "ledgers under explicit replay (admin-triggered)"),
            ("", ""),
            ("behind_network", "this node lags the validator-seen tip by N seqs"),
            ("awaiting_publish", "validated but not yet told subscribers — publish backpressure"),
            ("close_to_validate", "closed locally but no quorum yet — network or local lag"),
        ]
        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=2)
        tbl.add_column(style="dim", no_wrap=False, ratio=5)
        for name, desc in lines:
            tbl.add_row(name, desc)
        return Panel(tbl, title="[bold]Legend[/bold]", border_style="blue")

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
            try:
                self._raw_cache_text = json.dumps(ls, indent=2, sort_keys=True)
            except (TypeError, ValueError):
                self._raw_cache_text = repr(ls)
            self._raw_cache_id = cache_id
        return Panel(
            Text(self._raw_cache_text, style="dim", no_wrap=True),
            title="[bold]Raw JSON[/bold]",
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
