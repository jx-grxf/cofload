#!/usr/bin/env python3
"""PreToolUse guard: refuse expensive reads and name the cheap path instead.

Four things this deliberately does NOT do:
  - block when no worker backend is available (a broken worker must never
    strand a session)
  - block files matching a secret pattern (those must not reach a worker, so
    the expensive model reads them itself)
  - block targeted reads: offset/limit, or a shell command whose output is
    piped or redirected away
  - crash (any unexpected error ends in allow, never in a stuck tool call)
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import cofload_core as core  # noqa: E402

BYPASS = core.CACHE_DIR / "bypass.json"
COFLOAD_BIN = str(Path(__file__).resolve().parent.parent / "bin" / "cofload")
READ_COMMANDS = {"cat", "head", "tail", "less", "more", "bat", "nl", "view"}
# Wrappers that change who runs a command but not what it prints.
WRAPPERS = {"sudo", "doas", "env", "command", "time", "nice", "ionice",
            "stdbuf", "nohup", "caffeinate"}
OPERATORS = {";", "&&", "||", "|", "&", "|&"}
REDIRECTS = {">", ">>", "<", "2>", "&>", ">|"}


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
        return float(data.get(str(path), 0)) > time.time()
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def verdict(path: Path, cfg: dict, how: str) -> str | None:
    """The reason this read should be refused, or None to let it through."""
    try:
        if not path.is_file():
            return None
        resolved = path.resolve()
        if bypassed(resolved) or bypassed(path):
            return None
        if core.is_secret(resolved, cfg) or core.matches(resolved,
                                                         cfg.get("skip_patterns", [])):
            return None
        lines = core.count_lines(resolved)
        if lines is None:
            return None  # binary or unreadable
        size = resolved.stat().st_size
    except OSError:
        return None

    if lines < cfg["min_lines"] and size < cfg["min_bytes"]:
        return None
    backend, _ = core.usable_backend(cfg)
    if backend is None:
        return None  # fail open: no worker, no blocking

    return (
        f"{path.name} is {lines} lines / {size // 1024} KB. Reading it whole costs "
        f"roughly {size // 4000}k tokens of context, and that context stays for the "
        f"rest of the session.\n\n"
        f"Ask the cheap worker instead ({backend.name}: {backend.model}):\n"
        f"  {COFLOAD_BIN} read {shlex.quote(str(resolved))} -- \"<your question>\"\n\n"
        f"If you need the literal lines, read a slice ({how}) or lift the guard:\n"
        f"  {COFLOAD_BIN} allow {shlex.quote(str(resolved))}"
    )


def loud_verdict(words: list, cfg: dict) -> str | None:
    """Commands that reliably print thousands of lines."""
    joined = " ".join(words)
    if any(flag in words for flag in cfg.get("quiet_flags", [])):
        return None
    for loud in cfg.get("loud_commands", []):
        parts = loud.split()
        if words[:len(parts)] == parts or Path(words[0]).name == parts[0] and \
                words[1:len(parts)] == parts[1:]:
            backend, _ = core.usable_backend(cfg)
            if backend is None:
                return None
            return (
                f"`{joined[:60]}` prints its whole run into context, and it stays "
                f"there for the rest of the session.\n\n"
                f"Run it through the worker instead — same command, same exit code, "
                f"only the failure comes back:\n"
                f"  {COFLOAD_BIN} run {shlex.quote(joined)}\n\n"
                f"Short output is passed through unchanged, so nothing is lost. To "
                f"see the raw output anyway, narrow it yourself with a pipe "
                f"(| tail -50, | grep -E 'error|fail')."
            )
    return None


def segments(command: str) -> list:
    """Split a shell command into (words, narrowed) pairs.

    narrowed means the segment's output does not reach the model: it is piped
    into something else, or redirected to a file. Splitting matters because
    `echo hi && cat huge.ts` used to pass the whole check — the operator hid
    the second command from a scan that only looked at the first word.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []  # unbalanced quotes: nothing safe to say about this command

    out, current, redirected = [], [], False
    for token in tokens:
        if token in OPERATORS:
            piped = token.startswith("|")
            if current:
                out.append((current, redirected or piped))
            current, redirected = [], False
        elif token in REDIRECTS or re.match(r"^\d?>{1,2}$", token):
            redirected = True
        else:
            current.append(token)
    if current:
        out.append((current, redirected))
    return out


def strip_wrappers(words: list) -> list:
    """Drop sudo/env/time and leading VAR=value assignments."""
    i = 0
    while i < len(words):
        word = words[i]
        if Path(word).name in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
            continue
        break
    return words[i:]


def is_cofload(word: str) -> bool:
    """Only the command itself counts — a filename containing 'cofload' used to
    switch the whole check off."""
    return Path(word).name in ("cofload", "cofload.py")


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            allow()
    except (ValueError, OSError):
        allow()

    tool = event.get("tool_name", "")
    args = event.get("tool_input") or {}
    if not isinstance(args, dict):
        allow()
    cwd = Path(event.get("cwd") or ".")
    cfg = core.load_config(cwd)

    if tool == "Read":
        if args.get("offset") is not None or args.get("limit") is not None:
            allow()
        target = args.get("file_path")
        if not target:
            allow()
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = (cwd / path)
        reason = verdict(path, cfg, "Read with offset/limit")
        if reason:
            deny(reason)
        allow()

    if tool == "Bash":
        command = args.get("command", "")
        for words, narrowed in segments(command):
            if narrowed:
                continue
            words = strip_wrappers(words)
            if not words or is_cofload(words[0]):
                continue
            reason = loud_verdict(words, cfg)
            if reason:
                deny(reason)
            if Path(words[0]).name not in READ_COMMANDS:
                continue
            for token in words[1:]:
                if token.startswith("-") or re.match(r"^\d+$", token):
                    continue
                candidate = Path(token).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                reason = verdict(candidate, cfg, "head -n / sed -n '<from>,<to>p'")
                if reason:
                    deny(reason)
    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # A guard that crashes must not take the tool call with it.
        sys.exit(0)
