"""
SHAMap internals + inbound-acquire diagnostics.

Renders the following get_counts blocks emitted by patched rippled builds:
  - ``tagged_pointer_pools`` — per-slot pool byte totals + aggregate
  - ``treenode_cache_locks`` — TreeNodeCache lock hold time
  - ``shamap_sources`` — where SHAMap reads are served from (tree cache,
    pack, db, filter, wire), canonical dedup/merge stats, primed/locally-
    built tagging, and first-time enrichment transitions (wire+local)
  - ``inbound_acquire`` — generic/consensus/history acquire counters
    (hit/miss, reused/spawned, per-kind peer traffic, wire accepted)
    plus global async + stale counters

All values arrive as u64-encoded strings to avoid float64 precision loss;
_to_int handles both str and numeric inputs.
"""

from collections import deque
from typing import Any, Deque, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

# Trend-detection knobs mirror the ones in CountsDisplay: a move-majority
# over a rolling window of step-deltas, with latching so the arrow
# doesn't blink in and out on every ledger close.
_TREND_WINDOW = 30
_TREND_MAJORITY = 0.6


def _to_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _format_bytes(n_bytes: int) -> str:
    if n_bytes >= 1024**3:
        return f"{n_bytes / (1024**3):.2f} GB"
    if n_bytes >= 1024**2:
        return f"{n_bytes / (1024**2):.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes} B"


def _format_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n}"


def _format_delta(n: int) -> str:
    """Signed compact delta for inline rendering: +12, -3.4k, +1.2M."""
    if n == 0:
        return ""
    sign = "+" if n > 0 else "-"
    absn = abs(n)
    if absn >= 1_000_000:
        return f"{sign}{absn / 1_000_000:.2f}M"
    if absn >= 1_000:
        return f"{sign}{absn / 1_000:.1f}k"
    return f"{sign}{absn}"


class SHAMapPoolsDisplay(VerticalScroll):
    """TaggedPointer pool totals + TreeNodeCache lock contention, if emitted."""

    def __init__(self):
        super().__init__()
        self.border_title = "SHAMap Internals"
        self._content = Static("Waiting for data...")
        # Per-metric trend + delta history. Mirrors CountsDisplay._history
        # so the user gets the same vocabulary across panels: ++ for
        # strictly-monotonic growth, ↑/↓ for up/down trend in a rolling
        # window, and a per-round ±N showing what actually moved this
        # ledger. Key is a dotted path like "shamap_sources.pack_hit" or
        # "inbound_acquire.generic.ledgermaster_miss".
        self._history: Dict[str, Dict[str, Any]] = {}

    def reset_history(self) -> None:
        self._history.clear()

    def compose(self) -> ComposeResult:
        yield self._content

    def _record(self, key: str, value: int) -> int:
        """Track first/last/ever_decreased/deltas. Returns this-round delta.

        On the first observation we seed state and return 0 — a delta
        against "no prior value" is meaningless. Subsequent calls compute
        value - last and update the rolling window used by _trend().
        """
        entry = self._history.get(key)
        if entry is None:
            self._history[key] = {
                "first": value,
                "last": value,
                "ever_decreased": False,
                "deltas": deque(maxlen=_TREND_WINDOW),
                "trend": None,  # "up" | "down" | None, latched
                "last_delta": 0,
            }
            return 0
        delta = int(value - entry["last"])
        if delta > 0:
            entry["deltas"].append(1)
        elif delta < 0:
            entry["deltas"].append(-1)
            entry["ever_decreased"] = True
        else:
            entry["deltas"].append(0)
        entry["last"] = value
        entry["last_delta"] = delta
        return delta

    def _trend(self, key: str) -> str:
        """Rolling up/down arrow with latching — see CountsDisplay for
        the same logic. Returns rich-markup string or ''."""
        entry = self._history.get(key)
        if entry is None:
            return ""
        deltas: Deque[int] = entry["deltas"]
        if len(deltas) < 5:
            return ""
        ups = sum(1 for d in deltas if d > 0)
        downs = sum(1 for d in deltas if d < 0)
        moves = ups + downs
        if moves > 0:
            if ups / moves >= _TREND_MAJORITY:
                entry["trend"] = "up"
            elif downs / moves >= _TREND_MAJORITY:
                entry["trend"] = "down"
        trend = entry.get("trend")
        if trend == "up":
            return "[bold red]↑[/bold red]"
        if trend == "down":
            return "[bold green]↓[/bold green]"
        return ""

    def _monotonic(self, key: str) -> str:
        """'++' (red) if the key has only grown since first observation."""
        entry = self._history.get(key)
        if entry is None:
            return ""
        if entry["ever_decreased"]:
            return ""
        if entry["last"] <= entry["first"]:
            return ""
        return "[bold red]++[/bold red]"

    def _markers(self, key: str) -> str:
        """Combined trend + monotonic marker string, space-separated."""
        parts = [p for p in (self._trend(key), self._monotonic(key)) if p]
        return " ".join(parts)

    def _delta_str(self, key: str) -> str:
        """Per-round delta with +/- sign. Coloured red for growth, green
        for shrink, dim for no change (empty)."""
        entry = self._history.get(key)
        if entry is None:
            return ""
        delta = entry.get("last_delta", 0)
        if delta == 0:
            return ""
        colour = "red" if delta > 0 else "green"
        return f"[{colour}]{_format_delta(delta)}[/{colour}]"

    def _marked_name(self, name: str, key: str) -> str:
        """Append trend+monotonic markers to a metric name, one space between."""
        m = self._markers(key)
        return f"{name} {m}" if m else name

    def _pair_delta(self, key_a: str, key_b: str) -> str:
        """Compact paired delta like '+12/+45'. Empty if both are zero."""
        a = self._history.get(key_a, {}).get("last_delta", 0)
        b = self._history.get(key_b, {}).get("last_delta", 0)
        if a == 0 and b == 0:
            return ""
        colour_a = "red" if a > 0 else ("green" if a < 0 else "dim")
        colour_b = "red" if b > 0 else ("green" if b < 0 else "dim")
        sa = _format_delta(a) or "0"
        sb = _format_delta(b) or "0"
        return f"[{colour_a}]{sa}[/{colour_a}]/[{colour_b}]{sb}[/{colour_b}]"

    def update_counts(self, counts: Optional[Dict[str, Any]]):
        if not counts:
            self._content.update(Text("Waiting for data...", style="dim"))
            return

        if "result" in counts:
            counts = counts["result"]

        pools = counts.get("tagged_pointer_pools")
        locks = counts.get("treenode_cache_locks")
        sources = counts.get("shamap_sources")
        acquire = counts.get("inbound_acquire")
        if not pools and not locks and not sources and not acquire:
            self._content.update(
                Text(
                    "Not emitted by this rippled build — requires patched get_counts.",
                    style="dim",
                )
            )
            return

        # Record history BEFORE rendering so _markers/_delta_str in the
        # row builders have the freshly-updated values to read from.
        self._record_all(sources, acquire)
        self._content.update(self._format(pools, locks, sources, acquire))

    def _record_all(
        self,
        sources: Optional[Dict[str, Any]],
        acquire: Optional[Dict[str, Any]],
    ) -> None:
        """Seed history for every numeric leaf in the sections that
        render markers. Pools/locks are volume-style gauges where the
        trend vocabulary is less informative, so we skip them."""
        if isinstance(sources, dict):
            for k, v in sources.items():
                self._record(f"sources.{k}", _to_int(v))
        if isinstance(acquire, dict):
            for k, v in acquire.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        self._record(f"acquire.{k}.{kk}", _to_int(vv))
                else:
                    self._record(f"acquire.{k}", _to_int(v))

    def _format(
        self,
        pools: Optional[Dict[str, Any]],
        locks: Optional[Dict[str, Any]],
        sources: Optional[Dict[str, Any]],
        acquire: Optional[Dict[str, Any]],
    ) -> Table:
        # Five columns because the Inbound acquire section now needs four
        # data columns (hit/miss, reused/spawned, peer pkts/nodes, wire
        # accepted). Earlier sections (pools / locks / sources) only use
        # the first two or three; leaving the tail columns empty is cheap
        # and keeps everything vertically aligned.
        table = Table(show_header=False, box=None, expand=False, pad_edge=False)
        table.add_column(style="yellow", no_wrap=True)
        table.add_column(justify="right", style="green", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)
        # Extra column for per-round Δ. Most rows leave it empty; filled
        # only for the count-style metrics in shamap_sources + inbound
        # acquire where the tick-to-tick delta is diagnostic.
        table.add_column(justify="right", no_wrap=True)

        if pools:
            total = pools.get("_total") or {}
            current_b = _to_int(total.get("current_bytes"))
            peak_b = _to_int(total.get("peak_bytes"))
            wasted_b = _to_int(total.get("cached_wasted_bytes"))
            cum_allocs = _to_int(total.get("cumulative_allocs"))
            wasted_pct = (wasted_b / peak_b * 100) if peak_b > 0 else 0

            table.add_row("[bold magenta]TaggedPointer pools[/bold magenta]", "", "")
            table.add_row(
                "  Current / Peak",
                f"{_format_bytes(current_b)} / {_format_bytes(peak_b)}",
                "",
            )
            table.add_row(
                "  Cached (peak-cur)",
                f"{_format_bytes(wasted_b)} ({wasted_pct:.1f}%)",
                "",
            )
            table.add_row("  Lifetime allocs", _format_count(cum_allocs), "")

            slot_rows = []
            for key, entry in pools.items():
                if not key.endswith("_slot") or not isinstance(entry, dict):
                    continue
                current = _to_int(entry.get("current"))
                peak = _to_int(entry.get("peak"))
                chunk_bytes = _to_int(entry.get("chunk_bytes"))
                if current == 0 and peak == 0:
                    continue
                cur_bytes = current * chunk_bytes
                slot_rows.append((key.removesuffix("_slot"), cur_bytes, current, peak))
            slot_rows.sort(key=lambda r: r[1], reverse=True)

            if slot_rows:
                table.add_row("", "", "")
                table.add_row("[bold magenta]All slots[/bold magenta]", "bytes", "chunks")
                for slot, cur_bytes, current, peak in slot_rows:
                    cur_str = _format_count(current)
                    peak_str = _format_count(peak)
                    # Drop '/X' when the formatted strings match — pointless
                    # noise when the only difference is below the displayed
                    # precision.
                    chunks = cur_str if cur_str == peak_str else f"{cur_str}/{peak_str}"
                    table.add_row(f"  slot {slot}", _format_bytes(cur_bytes), chunks)

        if locks:
            if pools:
                table.add_row("", "", "")
            held_ms = _to_int(locks.get("held_ms"))
            acquires = _to_int(locks.get("acquires"))
            mean_ns = _to_int(locks.get("mean_ns"))
            table.add_row("[bold magenta]TreeNodeCache locks[/bold magenta]", "", "")
            table.add_row(
                "  Held / Acquires",
                f"{held_ms:,} ms / {_format_count(acquires)}",
                "",
            )
            table.add_row("  Mean hold", f"{mean_ns:,} ns", "")

        if sources:
            if pools or locks:
                table.add_row("", "", "")
            tree_hit = _to_int(sources.get("tree_cache_hit"))
            tree_miss = _to_int(sources.get("tree_cache_miss"))
            pack_hit = _to_int(sources.get("pack_hit"))
            db_hit = _to_int(sources.get("db_hit"))
            db_miss = _to_int(sources.get("db_miss"))
            filter_hit = _to_int(sources.get("filter_hit"))
            wire = _to_int(sources.get("wire_node_accepted"))
            fresh = _to_int(sources.get("canonical_fresh"))
            dedup = _to_int(sources.get("canonical_dedup"))
            merge_nodes = _to_int(sources.get("canonical_merge_nodes"))
            merge_children = _to_int(sources.get("canonical_merge_children"))
            primed_origin = _to_int(sources.get("primed_origin_tagged"))
            locally_built = _to_int(sources.get("locally_built_finalized"))
            enr_any = _to_int(sources.get("enriched_any_flagged"))
            enr_wire = _to_int(sources.get("enriched_from_wire_flagged"))
            enr_local = _to_int(sources.get("enriched_from_local_flagged"))

            # Tree-cache hit ratio is the single most diagnostic number
            # in this section — surface it inline instead of making the
            # user do the math.
            tree_total = tree_hit + tree_miss
            tree_pct = f" ({tree_hit / tree_total * 100:.1f}%)" if tree_total else ""
            db_total = db_hit + db_miss
            db_pct = f" ({db_hit / db_total * 100:.1f}%)" if db_total else ""

            table.add_row("[bold magenta]SHAMap sources[/bold magenta]", "", "")
            # Primary-metric pairs: show markers on the "hit" side, Δ
            # collapses both in a compact +h/+m form.
            table.add_row(
                self._marked_name("  Tree cache hit/miss", "sources.tree_cache_hit"),
                f"{_format_count(tree_hit)}/{_format_count(tree_miss)}{tree_pct}",
                "",
                "",
                "",
                self._pair_delta("sources.tree_cache_hit", "sources.tree_cache_miss"),
            )
            table.add_row(
                self._marked_name("  DB hit/miss", "sources.db_miss"),
                f"{_format_count(db_hit)}/{_format_count(db_miss)}{db_pct}",
                "",
                "",
                "",
                self._pair_delta("sources.db_hit", "sources.db_miss"),
            )
            table.add_row(
                self._marked_name("  Pack hit", "sources.pack_hit"),
                _format_count(pack_hit),
                "",
                "",
                "",
                self._delta_str("sources.pack_hit"),
            )
            table.add_row(
                self._marked_name("  Filter hit", "sources.filter_hit"),
                _format_count(filter_hit),
                "",
                "",
                "",
                self._delta_str("sources.filter_hit"),
            )
            table.add_row(
                self._marked_name("  Wire nodes accepted", "sources.wire_node_accepted"),
                _format_count(wire),
                "",
                "",
                "",
                self._delta_str("sources.wire_node_accepted"),
            )
            if primed_origin:
                table.add_row(
                    self._marked_name("  Primed-origin tagged", "sources.primed_origin_tagged"),
                    _format_count(primed_origin),
                    "",
                    "",
                    "",
                    self._delta_str("sources.primed_origin_tagged"),
                )
            if locally_built:
                table.add_row(
                    self._marked_name(
                        "  Locally built finalized", "sources.locally_built_finalized"
                    ),
                    _format_count(locally_built),
                    "",
                    "",
                    "",
                    self._delta_str("sources.locally_built_finalized"),
                )
            table.add_row(
                self._marked_name("  Canonical fresh/dedup", "sources.canonical_fresh"),
                f"{_format_count(fresh)}/{_format_count(dedup)}",
                "",
                "",
                "",
                self._pair_delta("sources.canonical_fresh", "sources.canonical_dedup"),
            )
            if merge_nodes or merge_children:
                table.add_row(
                    self._marked_name(
                        "  Canonical merges (nodes/children)",
                        "sources.canonical_merge_nodes",
                    ),
                    f"{_format_count(merge_nodes)}/{_format_count(merge_children)}",
                    "",
                    "",
                    "",
                    self._pair_delta(
                        "sources.canonical_merge_nodes",
                        "sources.canonical_merge_children",
                    ),
                )
            # Enrichment counters — first-time transitions where a cached
            # canonical inner gained an enrichment bit. wire and local are
            # INDEPENDENT counters (a node flagged first by wire then later
            # by local counts in both), so wire+local is generally >= any.
            # Split onto two rows with a 'from' subrow to avoid the '+'
            # looking like a sum.
            if enr_any or enr_wire or enr_local:
                table.add_row(
                    self._marked_name("  Enriched (unique nodes)", "sources.enriched_any_flagged"),
                    _format_count(enr_any),
                    "",
                    "",
                    "",
                    self._delta_str("sources.enriched_any_flagged"),
                )
                table.add_row(
                    "    from wire / local",
                    f"{_format_count(enr_wire)} / {_format_count(enr_local)}",
                    "",
                    "",
                    "",
                    self._pair_delta(
                        "sources.enriched_from_wire_flagged",
                        "sources.enriched_from_local_flagged",
                    ),
                )

        if acquire:
            if pools or locks or sources:
                table.add_row("", "", "", "", "")
            table.add_row("[bold magenta]Inbound acquire[/bold magenta]", "", "", "", "")
            # Column header. Four data columns per kind:
            #   hit/miss        = ledgermaster hit vs miss (pct of hits)
            #   reused/spawned  = after a LM miss, joined existing vs spawned new
            #   pkts/nodes      = peer TMLedgerData packets + total nodes() entries
            #                     delivered to this kind's active inbound ledgers
            #   wire accepted   = non-root SHAMap nodes accepted from that traffic
            table.add_row("", "hit/miss", "reused/spawned", "pkts/nodes", "wire accepted")
            for kind in ("generic", "consensus", "history"):
                sub = acquire.get(kind)
                if not isinstance(sub, dict):
                    continue
                lm_hit = _to_int(sub.get("ledgermaster_hit"))
                lm_miss = _to_int(sub.get("ledgermaster_miss"))
                reused = _to_int(sub.get("reused_existing_inbound"))
                spawned = _to_int(sub.get("spawned_new_inbound"))
                peer_pkts = _to_int(sub.get("peer_packets"))
                peer_nodes = _to_int(sub.get("peer_nodes"))
                wire_accepted = _to_int(sub.get("wire_nodes_accepted"))
                lm_total = lm_hit + lm_miss
                lm_pct = f" ({lm_hit / lm_total * 100:.1f}%)" if lm_total else ""
                pkts_nodes = (
                    f"{_format_count(peer_pkts)}/{_format_count(peer_nodes)}"
                    if peer_pkts or peer_nodes
                    else ""
                )
                # Per-kind row: use the wire_nodes_accepted path as the
                # primary trend target since it's the "actually useful
                # work happening" signal for this kind. Per-round Δ in
                # the trailing column uses the same primary.
                key_prefix = f"acquire.{kind}"
                table.add_row(
                    self._marked_name(f"  {kind}", f"{key_prefix}.wire_nodes_accepted"),
                    f"{_format_count(lm_hit)}/{_format_count(lm_miss)}{lm_pct}",
                    f"{_format_count(reused)}/{_format_count(spawned)}",
                    pkts_nodes,
                    _format_count(wire_accepted) if wire_accepted else "",
                    self._delta_str(f"{key_prefix}.wire_nodes_accepted"),
                )

            # Global-scope counters below the per-kind block. Async and
            # stale numbers are separate concerns — broken out so they
            # don't clutter the per-kind row but still live on the panel.
            async_hit = _to_int(acquire.get("async_ledgermaster_hit"))
            async_skip = _to_int(acquire.get("async_pending_skip"))
            total_pkts = _to_int(acquire.get("peer_packets"))
            total_nodes = _to_int(acquire.get("peer_nodes"))
            stale_pkts = _to_int(acquire.get("stale_peer_packets"))
            stale_nodes = _to_int(acquire.get("stale_peer_nodes"))
            if async_hit or async_skip:
                table.add_row(
                    self._marked_name("  async hit/skip", "acquire.async_ledgermaster_hit"),
                    f"{_format_count(async_hit)}/{_format_count(async_skip)}",
                    "",
                    "",
                    "",
                    self._pair_delta(
                        "acquire.async_ledgermaster_hit",
                        "acquire.async_pending_skip",
                    ),
                )
            if total_pkts or total_nodes or stale_pkts or stale_nodes:
                # Format 'live / stale' so the ratio of wasted traffic is
                # scannable — high stale% means peers are delivering for
                # acquires that already finished / aborted.
                stale_pkt_pct = (
                    f" ({stale_pkts / total_pkts * 100:.0f}% stale)" if total_pkts else ""
                )
                stale_node_pct = (
                    f" ({stale_nodes / total_nodes * 100:.0f}% stale)" if total_nodes else ""
                )
                table.add_row(
                    self._marked_name("  peer packets (stale)", "acquire.peer_packets"),
                    f"{_format_count(total_pkts)}",
                    f"{_format_count(stale_pkts)}{stale_pkt_pct}",
                    "",
                    "",
                    self._delta_str("acquire.peer_packets"),
                )
                table.add_row(
                    self._marked_name("  peer nodes (stale)", "acquire.peer_nodes"),
                    f"{_format_count(total_nodes)}",
                    f"{_format_count(stale_nodes)}{stale_node_pct}",
                    "",
                    "",
                    self._delta_str("acquire.peer_nodes"),
                )

        return table
