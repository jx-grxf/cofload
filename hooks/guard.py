#!/usr/bin/env python3
"""PreToolUse guard: block expensive reads and point at the cheap path.

Three things this deliberately does NOT do:
  - block when no worker backend is available (a broken worker must never
    strand the session)
  - block files that match a secret pattern (those must never reach a worker,
    so the expensive model reads them itself)
  - block targeted reads (offset/limit, or a piped shell command), because
    those are already cheap
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import offload_core as core  # noqa: E402

BYPASS = core.CACHE_DIR / "bypass.json"
READ_COMMANDS = {"cat", "head", "tail", "less", "more", "bat", "nl"}
OFFLOAD_BIN = str(Path(__file__).resolve().parent.parent / "bin" / "offload")


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(2)


def bypassed(path: Path) -> bool:
    try:
        data = json.loads(BYPASS.read_text())
    except (OSError, ValueError):
        return False
    return data.get(str(path.resolve()), 0) > time.time()


def check(path: Path, cfg: dict, how: str) -> None:
    if not path.is_file() or bypassed(path):
        allow()
    if core.is_secret(path, cfg) or core.matches(path, cfg.get("skip_patterns", [])):
        allow()
    lines = core.count_lines(path)
    if lines is None:
        allow()  # binary
    size = path.stat().st_size
    if lines < int(cfg.get("min_lines", 350)) and size < int(cfg.get("min_bytes", 60_000)):
        allow()
    backend, why = core.usable_backend(cfg)
    if backend is None:
        allow()  # fail open: no worker, no blocking
    deny(
        f"{path.name} is {lines} lines / {size // 1024} KB. Reading it whole costs "
        f"roughly {size // 4000}k tokens of context, and that context stays for "
        f"the rest of the session.\n\n"
        f"Ask the cheap worker instead ({backend.name}: {backend.model}):\n"
        f"  {OFFLOAD_BIN} read {shlex.quote(str(path))} -- \"<your question>\"\n\n"
        f"If you need the literal lines (to edit them), use a targeted read "
        f"({how}) or run:\n"
        f"  {OFFLOAD_BIN} allow {shlex.quote(str(path))}"
    )


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        allow()

    tool = event.get("tool_name", "")
    args = event.get("tool_input", {}) or {}
    cwd = Path(event.get("cwd") or ".")
    cfg = core.load_config(cwd)

    if tool == "Read":
        if args.get("offset") or args.get("limit"):
            allow()
        target = args.get("file_path")
        if not target:
            allow()
        check(Path(target), cfg, "Read with offset/limit")

    if tool == "Bash":
        command = args.get("command", "")
        # A pipe means the output is already being narrowed; leave it alone.
        if any(ch in command for ch in "|><") or "offload" in command:
            allow()
        try:
            parts = shlex.split(command)
        except ValueError:
            allow()
        if not parts or Path(parts[0]).name not in READ_COMMANDS:
            allow()
        for token in parts[1:]:
            if token.startswith("-") or re.match(r"^\d+$", token):
                continue
            candidate = (cwd / token).expanduser()
            if candidate.is_file():
                check(candidate, cfg, "head -n / sed -n '<from>,<to>p'")
        allow()

    allow()


if __name__ == "__main__":
    main()
