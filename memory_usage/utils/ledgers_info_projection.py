"""
Compact projection of the patched-rippled ``ledgers_info`` RPC response
for post-hoc diff-based forensics.

Why a projection? The raw payload is ~10 KB per poll, dominated by
consensus-building detail (disputes dict, peer_positions, acquired
array) that churns every tick without carrying signal for most
debugging use cases. The projection keeps only the fields a human or
LLM would want to reason over when asking "why is sync stuck" or
"which IBL is misbehaving":

    * local pointers (closed/validated/published/building) as seqs
    * gaps block + inferred gap
    * RangeSet strings (retained/complete/missing/inbound/replaying)
    * priority-quorum hash when the experiment flag is live
    * per-IBL summary KEYED BY SEQ (not list — so RFC 6902 patches
      don't thrash on index shifts when an IBL rolls off)

Output is compact: ~1-2 KB per snapshot vs ~10 KB raw. Enough to
produce meaningful jsonpatch diffs across consecutive snapshots.
"""

from typing import Any, Dict, List, Optional


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _max_seq_in_ranges(ranges: Any) -> Optional[int]:
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


def project_ledgers_info(info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Reduce a ``ledgers_info`` response to its forensic essentials.

    Returns ``None`` when the input isn't a dict we can walk. Accepts
    both the raw RPC result and the ``{"result": {...}}`` wrapper form.
    """
    if not isinstance(info, dict) or not info:
        return None
    if "result" in info and isinstance(info["result"], dict):
        info = info["result"]
    ls = info.get("ledger_state") if isinstance(info.get("ledger_state"), dict) else info
    if not isinstance(ls, dict):
        return None

    net = ls.get("network") or {}
    loc = ls.get("local") or {}
    gaps = ls.get("gaps") or {}

    closed = loc.get("closed") or {}
    validated = loc.get("validated") or {}
    published = loc.get("published") or {}
    building = loc.get("building") or {}

    retained = loc.get("retained") or {}
    complete = loc.get("complete") or {}
    missing = loc.get("missing") or {}
    inbound = loc.get("inbound_acquiring") or {}
    replaying = loc.get("replaying") or {}

    # Inferred gap: rippled's behind_network can be bogus when
    # highest_validated_seen isn't populated. Keep both so the consumer
    # can judge — and a cold diff reader (LLM) can see the inference
    # directly instead of recomputing from the raw ranges string.
    val_seq = validated.get("seq")
    max_inbound = _max_seq_in_ranges(inbound.get("ranges"))
    inferred_gap: Optional[int] = None
    if isinstance(val_seq, int) and val_seq > 0 and max_inbound is not None:
        inferred_gap = max(0, max_inbound - val_seq)

    pointers: Dict[str, Any] = {
        "net_highest_seen_seq": _g(net, "highest_validation_seen", "seq"),
        "net_preferred_seq": _g(net, "preferred", "seq"),
        "closed": closed.get("seq"),
        "validated": validated.get("seq"),
        "published": published.get("seq"),
        "building_phase": building.get("phase"),
        "building_proposers": building.get("proposers"),
        "building_converge_percent": building.get("converge_percent"),
        "building_current_ms": building.get("current_ms"),
    }

    gaps_block: Dict[str, Any] = {
        "behind_network": gaps.get("behind_network"),
        "awaiting_publish": gaps.get("awaiting_publish"),
        "close_to_validate": gaps.get("close_to_validate"),
        "inferred_gap": inferred_gap,
    }

    ranges_block: Dict[str, Any] = {
        "retained": retained.get("ranges"),
        "complete": complete.get("ranges"),
        "missing": missing.get("ranges"),
        "inbound": inbound.get("ranges"),
        "replaying": replaying.get("ranges"),
    }

    # Per-IBL details. Key by seq string so diffs between snapshots
    # stay focused when a specific seq appears/disappears — not on
    # positional index churn.
    ibls: Dict[str, Any] = {}
    raw_details = inbound.get("details")
    # Build a uniform [(hint_key_or_None, detail_dict)] list so the
    # consumer loop handles list and dict-keyed-by-seq inputs without
    # type-pun gymnastics.
    iter_entries: List[tuple[Optional[str], Dict[str, Any]]] = []
    if isinstance(raw_details, list):
        for d in raw_details:
            if isinstance(d, dict):
                iter_entries.append((None, d))
    elif isinstance(raw_details, dict):
        for k, v in raw_details.items():
            if isinstance(v, dict):
                iter_entries.append((str(k), v))

    for pre_key, d in iter_entries:
        seq = d.get("seq")
        if seq is None and pre_key is not None:
            try:
                seq = int(pre_key)
            except (TypeError, ValueError):
                seq = None
        if seq is None:
            continue
        key = str(seq)

        have = {
            "header": bool(d.get("have_header")),
            "state": bool(d.get("have_state")),
            "transactions": bool(d.get("have_transactions")),
            "skip": bool(d.get("have_skip")),
        }
        skip = {
            "probe_sent": bool(d.get("skip_probe_sent")),
            "harvested": bool(d.get("skip_harvested")),
        }

        # Compact summary of the most recent state_response event —
        # captures the "book-base burrowing tail" signature (small n,
        # inners=0, DirectoryNode dominant) without embedding the full
        # per-round arrays, which would make jsonpatch diffs noisy.
        #
        # Also surface the cumulative count of "burrow" rounds: those
        # whose reply had no useful leaves (n==inners) or whose leaves
        # were all DirectoryNode. That's the single number that tells
        # you "is this IBL genuinely making SLE progress, or walking
        # skeleton/directory pages forever".
        state_last_resp: Optional[Dict[str, Any]] = None
        state_burrow_rounds = 0
        resp_arr = d.get("state_responses")
        if isinstance(resp_arr, list) and resp_arr:
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
                    state_burrow_rounds += 1
            last = resp_arr[-1]
            if isinstance(last, dict):
                last_types_raw = last.get("types")
                last_types: Dict[Any, Any] = (
                    last_types_raw if isinstance(last_types_raw, dict) else {}
                )
                top: List[tuple[str, int]] = []
                if last_types:
                    try:
                        top = sorted(
                            ((str(k), int(v)) for k, v in last_types.items()),
                            key=lambda kv: -kv[1],
                        )[:3]
                    except (TypeError, ValueError):
                        top = []
                state_last_resp = {
                    "t": last.get("t"),
                    "n": last.get("n"),
                    "inners": last.get("inners"),
                    "bc1_inners": last.get("bc1_inners"),
                    "top_types": top,
                }

        ibls[key] = {
            "reason": d.get("reason"),
            "age_ms": d.get("age_ms"),
            "last_admit_ms": d.get("last_admit_ms"),
            "policy_reason": d.get("policy_reason"),
            "policy_history": d.get("policy_history"),
            "timeouts": d.get("timeouts"),
            "peers": d.get("peers"),
            "priority_quorum": bool(d.get("priority_quorum")),
            "have": have,
            "skip": skip,
            "state_last_resp": state_last_resp,
            "state_burrow_rounds": state_burrow_rounds,
            "state_i": d.get("state_nodes_inserted"),
            "state_r": d.get("state_requests_sent"),
            "state_u": d.get("state_unique_hashes"),
            "state_R": d.get("state_rounds"),
            "tx_i": d.get("tx_nodes_inserted"),
            "tx_r": d.get("tx_requests_sent"),
            "tx_u": d.get("tx_unique_hashes"),
            "tx_R": d.get("tx_rounds"),
        }

    projection: Dict[str, Any] = {
        "pointers": pointers,
        "gaps": gaps_block,
        "ranges": ranges_block,
        "priority_quorum_hash": inbound.get("priority_quorum_hash"),
        "ibls": ibls,
    }
    peers = loc.get("peers")
    if isinstance(peers, list) and peers:
        # Key peers by id too so per-peer stat movement shows up in
        # diffs without positional noise.
        projection["peers"] = {
            str(p.get("peer_id")): {
                "sent": p.get("sent"),
                "replied": p.get("replied"),
                "replied_unsolicited": p.get("replied_unsolicited"),
                "nodes_received": p.get("nodes_received"),
                "in_flight": p.get("in_flight"),
                "ibls_active": p.get("ibls_active"),
                "min_ms": p.get("min_ms"),
                "avg_ms": p.get("avg_ms"),
                "median_ms": p.get("median_ms"),
                "max_ms": p.get("max_ms"),
            }
            for p in peers
            if isinstance(p, dict) and p.get("peer_id") is not None
        }
    return projection
