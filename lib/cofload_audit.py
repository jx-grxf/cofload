"""Measure what every session costs before a single word is typed.

The expensive part of a Claude Code session is not always what you read during
it. Plugins, MCP servers and instruction files load at session start and are
re-sent with every turn. This module weighs that fixed load against how often
those things are actually used, which is the only comparison that tells you
what to switch off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
TRANSCRIPTS = CLAUDE_HOME / "projects"


def _run(cmd, timeout=60) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""


def installed_plugins() -> list:
    """Name, always-on token cost, skills and enabled state for each plugin.

    A disabled plugin still appears in the listing but costs nothing, so its
    state has to be read rather than assumed.
    """
    listing = _run(["claude", "plugin", "list"])
    plugins = []
    blocks = re.split(r"\n\s*❯\s+", listing)[1:]
    for block in blocks:
        name = block.splitlines()[0].strip()
        if not name:
            continue
        enabled = "disabled" not in block.split("Status:")[1][:30] if "Status:" in block else True
        detail = _run(["claude", "plugin", "details", name], timeout=45)
        cost = re.search(r"Always-on:\s*~?([\d,]+)\s*tok", detail or "")
        skills = re.search(r"Skills \(\d+\)\s+(.+)", detail or "")
        plugins.append({
            "name": name,
            "tokens": int(cost.group(1).replace(",", "")) if cost and enabled else 0,
            "skills": [x.strip() for x in skills.group(1).split(",")] if skills else [],
            "enabled": enabled,
        })
    return plugins


def mcp_servers() -> list:
    out = _run(["claude", "mcp", "list"], timeout=90)
    servers = []
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.:\- ]+?):\s+(.*?)\s+-\s+(.*)$", line.strip())
        if m:
            servers.append({"name": m.group(1).strip(), "status": m.group(3).strip()})
    return servers


def instruction_files(cwd: Path) -> list:
    """Files that are loaded into every turn, with their estimated cost."""
    candidates = [
        CLAUDE_HOME / "CLAUDE.md",
        CLAUDE_HOME / "RTK.md",
        cwd / "CLAUDE.md",
        cwd / "AGENTS.md",
        cwd / ".claude" / "CLAUDE.md",
    ]
    slug = str(cwd).replace("/", "-")
    candidates.append(CLAUDE_HOME / "projects" / slug / "memory" / "MEMORY.md")
    found = []
    for path in candidates:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append({"path": path, "chars": size, "tokens": size // 4})
    return found


def usage_counts(days: int = 60) -> dict:
    """How often each MCP server, skill and agent appears in transcripts.

    Only structured call records count: `"name":"mcp__<server>__<tool>"`,
    `"name":"Skill","input":{"skill":"<name>"`, and `"subagent_type":"<name>"`.
    An earlier version also counted bare occurrences of a plugin's name in the
    transcript text, which reported 40,409 uses for a plugin called once - the
    system prompt lists every plugin and is repeated on every turn.

    Counts are a floor, not a census: a component can be used in a way these
    patterns miss, which is why the report says "no calls found", not "unused".
    """
    counts = {"mcp": {}, "skill": {}, "agent": {}}
    if not TRANSCRIPTS.is_dir():
        return counts
    cutoff = time.time() - days * 86400
    patterns = {
        # Non-greedy: with a greedy class, "mcp__a__b__c" credits server "a__b".
        "mcp": re.compile(rb'"name":"mcp__([a-zA-Z0-9_-]+?)__'),
        "skill": re.compile(rb'"name":"Skill","input":\{"skill":"([a-zA-Z0-9:_-]+)"'),
        "agent": re.compile(rb'"subagent_type":"([a-zA-Z0-9:_-]+)"'),
    }
    for path in TRANSCRIPTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        for kind, pat in patterns.items():
            for m in pat.finditer(blob):
                key = m.group(1).decode()
                counts[kind][key] = counts[kind].get(key, 0) + 1
    return counts


def _norm(name: str) -> str:
    """Tool prefixes and server names disagree about - and _ ('apple-mail' vs
    'apple_mail'), which silently reported a server used 107 times as unused.
    Comparing on letters and digits alone removes the whole class of mismatch."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def report(cwd: Path, days: int = 60) -> dict:
    plugins = installed_plugins()
    servers = mcp_servers()
    files = instruction_files(cwd)
    used = usage_counts(days)

    for p in plugins:
        short = p["name"].split("@")[0]
        own = {_norm(n) for n in [short, *p["skills"]]}

        def belongs(key: str) -> bool:
            # A plugin component is logged either bare ("bulk-read") or
            # namespaced ("cofload:bulk-read"); both must resolve to the plugin.
            prefix, _, rest = key.partition(":")
            return (_norm(key) in own or _norm(rest) in own
                    or (rest and _norm(prefix) == _norm(short)))

        p["uses"] = sum(n for k, n in used["skill"].items() if belongs(k))
        p["uses"] += sum(n for k, n in used["agent"].items() if belongs(k))
        p["uses"] += sum(n for k, n in used["mcp"].items() if _norm(short) in _norm(k))

    for s in servers:
        key = _norm(s["name"].replace("plugin:", ""))
        s["uses"] = max([n for k, n in used["mcp"].items()
                         if _norm(k) in key or key in _norm(k)] or [0])

    return {
        "plugins": sorted(plugins, key=lambda p: -p["tokens"]),
        "servers": servers,
        "files": sorted(files, key=lambda f: -f["tokens"]),
        "days": days,
        "plugin_tokens": sum(p["tokens"] for p in plugins),
        "file_tokens": sum(f["tokens"] for f in files),
    }
