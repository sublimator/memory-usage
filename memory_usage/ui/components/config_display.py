"""
Rippled config dashboard widget.

Parses the INI-ish rippled config file the monitor is pointed at and
renders it in a fixed, canonical section order so the eye lands in the
same place every time. Comments and blank lines are stripped; secrets
(``[node_seed]``, ``[validator_token]``) are redacted.

Read-only. Re-reads on each update_config() call so --reattach between
restarts of a rippled that was given a different cfg picks up the change.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

# Canonical section groupings. Each bucket = (title, [section names to
# pull from the parsed config]). Sections listed but absent from the
# file are skipped silently. Anything left over at the end lands in
# "Misc" verbatim so nothing is lost.
_GROUPS: List[Tuple[str, List[str]]] = [
    (
        "Identity",
        [
            "node_size",
            "node_seed",
            "validator_token",
            "validators_file",
            "network_id",
        ],
    ),
    (
        "Ports",
        [
            # Ports are expanded into a single synthetic table. [server]
            # (enabled port names) and every [port_*] are consumed here
            # and skipped by Misc.
            "__ports__",
        ],
    ),
    (
        "Peers",
        ["ips", "ips_fixed", "peer_private", "peers_max", "peers_in_max", "peers_out_max"],
    ),
    (
        "Database",
        [
            "node_db",
            "relational_db",
            "ledger_history",
            "database_path",
            "shard_db",
            "historical_shard_paths",
            "online_delete",
            "advisory_delete",
            "import_db",
            "catalogue",
        ],
    ),
    (
        "Voting / Fees",
        ["voting", "fee_default", "fees"],
    ),
    (
        "Validators",
        ["validators", "validator_list_sites", "validator_list_keys", "validator_key_revocation"],
    ),
    (
        "Features / Amendments",
        ["features", "amendments", "veto_amendments"],
    ),
    (
        "Logging",
        ["debug_logfile", "rpc_startup"],
    ),
]

_REDACTED = {"node_seed", "validator_token", "validator_key_revocation"}

# Secret-looking keys inside a values section (kv-style) we also redact.
_SECRET_KEY_RE = re.compile(r"(secret|seed|token|password|key)", re.IGNORECASE)


def _parse_cfg(path: Path) -> Dict[str, List[str]]:
    """Parse rippled's INI-ish config into {section: [non-comment lines]}.

    Rippled's format isn't quite INI — sections are ``[name]`` headers
    but bodies can be either key=value pairs, bare values (e.g. [ips]),
    or JSON blobs (e.g. [node_db], [port_*]). We don't try to normalise
    the body; we just strip comments/blank lines and keep the remainder
    verbatim so the display is faithful to what rippled actually reads.
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return sections

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        # Trailing comments on the same line.
        for marker in ("#", ";"):
            idx = line.find(marker)
            if idx >= 0:
                # Only treat as a comment if it's not inside quotes — cheap
                # heuristic: require whitespace before the marker.
                if idx == 0 or line[idx - 1].isspace():
                    line = line[:idx].rstrip()
                    stripped = line.strip()
                    break
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        sections[current].append(stripped)
    return sections


def _redact_section(name: str, lines: List[str]) -> List[str]:
    if name in _REDACTED:
        return ["***redacted***"]
    out: List[str] = []
    for ln in lines:
        if "=" in ln:
            k, _, v = ln.partition("=")
            if _SECRET_KEY_RE.search(k):
                out.append(f"{k.strip()}=***")
                continue
        out.append(ln)
    return out


def _fmt_port_block(lines: List[str]) -> Dict[str, str]:
    """Reduce a [port_*] body (key=value pairs) to a small dict."""
    d: Dict[str, str] = {}
    for ln in lines:
        if "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        d[k.strip()] = v.strip()
    return d


class ConfigDisplay(VerticalScroll):
    """Compact canonical view of the running node's config file."""

    def __init__(self) -> None:
        super().__init__()
        self.border_title = "Config"
        self._content = Static(Text("Waiting for config path…", style="dim"))
        self._last_path: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield self._content

    def update_config(self, path: Optional[str]) -> None:
        if not path:
            self._content.update(Text("No config path configured.", style="dim"))
            return
        p = Path(path)
        if not p.is_file():
            self._content.update(
                Text(f"Config file not found: {path}", style="red"),
            )
            return
        self._last_path = str(p)
        sections = _parse_cfg(p)
        self._content.update(self._build_view(p, sections))

    # ------------------------------------------------------------------

    def _build_view(self, path: Path, sections: Dict[str, List[str]]) -> Group:
        header = Text.assemble(
            ("loaded: ", "dim"),
            (str(path), "bold"),
            ("   sections: ", "dim"),
            (str(len(sections)), "bold green"),
        )

        # 2-col grid (same shape as Ledgers Info) but we stack every
        # panel in the LEFT column and leave the right column empty.
        # Keeps each panel at ~50% width so rows don't sprawl across
        # the whole screen — the user's eye lands on values at a
        # predictable x-position.
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)

        consumed: set[str] = set()
        panels: List[Panel] = []
        for title, keys in _GROUPS:
            panel = self._group_panel(title, keys, sections, consumed)
            if panel is not None:
                panels.append(panel)

        # Misc — everything we didn't place into a bucket, verbatim. We
        # skip any port_* that Ports already consumed.
        misc_names = [s for s in sections if s not in consumed and not s.startswith("port_")]
        if misc_names:
            panels.append(self._misc_panel(misc_names, sections))

        for panel in panels:
            grid.add_row(panel, Text(""))

        return Group(header, Text(""), grid)

    def _group_panel(
        self,
        title: str,
        keys: List[str],
        sections: Dict[str, List[str]],
        consumed: set[str],
    ) -> Optional[Panel]:
        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=2)
        tbl.add_column(style="green", no_wrap=False, ratio=5, overflow="fold")
        rows = 0

        for key in keys:
            if key == "__ports__":
                # [server] enumerates which port_* sections are live. Any
                # port_* not referenced from [server] is parsed but not
                # wired — show those dimmed.
                enabled: List[str] = []
                if "server" in sections:
                    consumed.add("server")
                    enabled = [ln for ln in sections["server"] if ln]
                port_sections = sorted([s for s in sections if s.startswith("port_")])
                if not port_sections and not enabled:
                    continue
                for ps in port_sections:
                    consumed.add(ps)
                    d = _fmt_port_block(sections[ps])
                    proto = d.get("protocol", "-")
                    port = d.get("port", "-")
                    ip = d.get("ip", "")
                    admin = d.get("admin", "")
                    sg = d.get("secure_gateway", "")
                    bits = [f"{proto}:{port}"]
                    if ip and ip not in ("0.0.0.0", "127.0.0.1"):
                        bits.append(f"ip={ip}")
                    elif ip:
                        bits.append(ip)
                    if admin:
                        bits.append(f"admin={admin}")
                    if sg:
                        bits.append(f"sg={sg}")
                    name = ps.removeprefix("port_")
                    is_on = ps in enabled
                    label = name if is_on else f"[dim]{name} (off)[/dim]"
                    tbl.add_row(label, "  ".join(bits))
                    rows += 1
                continue

            if key not in sections:
                continue
            consumed.add(key)
            lines = _redact_section(key, sections[key])
            value = self._condense_value(lines)
            tbl.add_row(key, value)
            rows += 1

        if rows == 0:
            return None
        return Panel(tbl, title=f"[bold]{title}[/bold]", border_style="cyan")

    @staticmethod
    def _condense_value(lines: List[str]) -> str:
        """Reduce a section body to a compact representation.

        - 0 lines → ``(empty)``
        - 1 line → verbatim
        - all k=v pairs → join with ``  `` on one row
        - mixed / bare-value multi-line → show up to 10 lines joined by
          newlines; elide the tail with ``+N more`` only beyond that.
        """
        if not lines:
            return "[dim](empty)[/dim]"
        if len(lines) == 1:
            return lines[0]
        if all("=" in ln for ln in lines):
            return "  ".join(lines)
        limit = 10
        if len(lines) <= limit:
            return "\n".join(lines)
        shown = lines[:limit]
        extra = len(lines) - limit
        return "\n".join(shown) + f"\n[dim]+{extra} more[/dim]"

    def _misc_panel(self, names: List[str], sections: Dict[str, List[str]]) -> Panel:
        tbl = Table(show_header=False, box=None, expand=True, pad_edge=False)
        tbl.add_column(style="yellow", no_wrap=True, ratio=2)
        tbl.add_column(style="dim", no_wrap=False, ratio=5, overflow="fold")
        for name in sorted(names):
            lines = _redact_section(name, sections[name])
            tbl.add_row(name, self._condense_value(lines))
        return Panel(tbl, title="[bold]Misc[/bold]", border_style="magenta")
