"""
SHAMap internals + inbound-acquire diagnostics.

Renders the following get_counts blocks emitted by patched rippled builds:
  - ``tagged_pointer_pools`` — per-slot pool byte totals + aggregate
  - ``treenode_cache_locks`` — TreeNodeCache lock hold time
  - ``shamap_sources`` — where SHAMap reads are served from (pack, tree
    cache, db, filter, wire) + canonical merge stats
  - ``inbound_acquire`` — generic/consensus/history acquire counters and
    peer packet volume

All values arrive as u64-encoded strings to avoid float64 precision loss;
_to_int handles both str and numeric inputs.
"""

from typing import Any, Dict, Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


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


class SHAMapPoolsDisplay(VerticalScroll):
    """TaggedPointer pool totals + TreeNodeCache lock contention, if emitted."""

    def __init__(self):
        super().__init__()
        self.border_title = "SHAMap Internals"
        self._content = Static("Waiting for data...")

    def compose(self) -> ComposeResult:
        yield self._content

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

        self._content.update(self._format(pools, locks, sources, acquire))

    def _format(
        self,
        pools: Optional[Dict[str, Any]],
        locks: Optional[Dict[str, Any]],
        sources: Optional[Dict[str, Any]],
        acquire: Optional[Dict[str, Any]],
    ) -> Table:
        # Three columns so bytes and chunks line up across all rows. The
        # aggregate/locks rows leave the third column empty.
        table = Table(show_header=False, box=None, expand=False, pad_edge=False)
        table.add_column(style="yellow", no_wrap=True)
        table.add_column(justify="right", style="green", no_wrap=True)
        table.add_column(justify="right", style="dim", no_wrap=True)

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

            # Tree-cache hit ratio is the single most diagnostic number
            # in this section — surface it inline instead of making the
            # user do the math.
            tree_total = tree_hit + tree_miss
            tree_pct = f" ({tree_hit / tree_total * 100:.1f}%)" if tree_total else ""
            db_total = db_hit + db_miss
            db_pct = f" ({db_hit / db_total * 100:.1f}%)" if db_total else ""

            table.add_row("[bold magenta]SHAMap sources[/bold magenta]", "", "")
            table.add_row(
                "  Tree cache hit/miss",
                f"{_format_count(tree_hit)}/{_format_count(tree_miss)}{tree_pct}",
                "",
            )
            table.add_row(
                "  DB hit/miss",
                f"{_format_count(db_hit)}/{_format_count(db_miss)}{db_pct}",
                "",
            )
            table.add_row("  Pack hit", _format_count(pack_hit), "")
            table.add_row("  Filter hit", _format_count(filter_hit), "")
            table.add_row("  Wire nodes accepted", _format_count(wire), "")
            table.add_row(
                "  Canonical fresh/dedup", f"{_format_count(fresh)}/{_format_count(dedup)}", ""
            )
            if merge_nodes or merge_children:
                table.add_row(
                    "  Canonical merges (nodes/children)",
                    f"{_format_count(merge_nodes)}/{_format_count(merge_children)}",
                    "",
                )

        if acquire:
            if pools or locks or sources:
                table.add_row("", "", "")
            table.add_row("[bold magenta]Inbound acquire[/bold magenta]", "", "")
            # The three sub-trees (generic / consensus / history) share a
            # schema; compact into one row each so the panel stays scannable.
            for kind in ("generic", "consensus", "history"):
                sub = acquire.get(kind)
                if not isinstance(sub, dict):
                    continue
                lm_hit = _to_int(sub.get("ledgermaster_hit"))
                lm_miss = _to_int(sub.get("ledgermaster_miss"))
                reused = _to_int(sub.get("reused_existing_inbound"))
                spawned = _to_int(sub.get("spawned_new_inbound"))
                lm_total = lm_hit + lm_miss
                lm_pct = f" ({lm_hit / lm_total * 100:.1f}%)" if lm_total else ""
                table.add_row(
                    f"  {kind} hit/miss",
                    f"{_format_count(lm_hit)}/{_format_count(lm_miss)}{lm_pct}",
                    f"r{_format_count(reused)}/s{_format_count(spawned)}",
                )
            async_hit = _to_int(acquire.get("async_ledgermaster_hit"))
            async_skip = _to_int(acquire.get("async_pending_skip"))
            peer_pkts = _to_int(acquire.get("peer_packets"))
            peer_nodes = _to_int(acquire.get("peer_nodes"))
            if async_hit or async_skip:
                table.add_row(
                    "  Async hit/skip",
                    f"{_format_count(async_hit)}/{_format_count(async_skip)}",
                    "",
                )
            if peer_pkts or peer_nodes:
                table.add_row(
                    "  Peer packets/nodes",
                    f"{_format_count(peer_pkts)}/{_format_count(peer_nodes)}",
                    "",
                )

        return table
