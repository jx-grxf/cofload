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
    """Name, always-on token cost and skill names for each enabled plugin."""
    listing = _run(["claude", "plugin", "list"])
    names = re.findall(r"❯\s+(\S+)", listing)
    plugins = []
    for name in names:
        detail = _run(["claude", "plugin", "details", name], timeout=45)
        if not detail:
            continue
        cost = re.search(r"Always-on:\s*~?([\d,]+)\s*tok", detail)
        skills = re.search(r"Skills \(\d+\)\s+(.+)", detail)
        plugins.append({
            "name": name,
            "tokens": int(cost.group(1).replace(",", "")) if cost else 0,
            "skills": [s.strip() for s in skills.group(1).split(",")] if skills else [],
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


def usage_counts(days: int = 60, extra_terms=()) -> dict:
    """How often each MCP server and skill actually appears in transcripts.

    Counts are a floor, not a census: they come from matching patterns in
    transcript files, so a component can be used in a way this misses. That is
    why the report says "no calls found" rather than "unused".
    """
    counts = {"mcp": {}, "skill": {}, "text": {}}
    if not TRANSCRIPTS.is_dir():
        return counts
    cutoff = time.time() - days * 86400
    files = [p for p in TRANSCRIPTS.rglob("*.jsonl")
             if p.stat().st_mtime >= cutoff]
    mcp_pat = re.compile(rb'"name":"mcp__([a-z0-9_-]+)__')
    skill_pat = re.compile(rb'"skill":"([a-zA-Z0-9:_-]+)"')
    for path in files:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for m in mcp_pat.finditer(blob):
            key = m.group(1).decode()
            counts["mcp"][key] = counts["mcp"].get(key, 0) + 1
        for m in skill_pat.finditer(blob):
            key = m.group(1).decode()
            counts["skill"][key] = counts["skill"].get(key, 0) + 1
        for term in extra_terms:
            hits = blob.count(term.encode())
            if hits:
                counts["text"][term] = counts["text"].get(term, 0) + hits
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
    # Plugin skills also appear as plain text (an agent name, a slash command),
    # so the plugin's own short name is searched literally as a second source.
    used = usage_counts(days, extra_terms=[p["name"].split("@")[0] for p in plugins])

    for p in plugins:
        short = p["name"].split("@")[0]
        names = {_norm(n) for n in [short, *p["skills"]]}
        p["uses"] = sum(n for k, n in used["skill"].items() if _norm(k) in names)
        p["uses"] += sum(n for k, n in used["mcp"].items() if _norm(short) in _norm(k))
        p["uses"] += used["text"].get(short, 0)

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
