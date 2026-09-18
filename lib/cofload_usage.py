"""Real token usage, read from Claude Code's own transcripts.

Everywhere else this tool estimates (characters / 4). Here it does not: each
assistant message records what the API actually billed, split into fresh input,
cache writes, cache reads and output. That split is the interesting part —
a cache read is an order of magnitude cheaper than the write that created it,
so a session with a poor cache ratio is expensive for reasons that have nothing
to do with how much you read.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
TRANSCRIPTS = CLAUDE_HOME / "projects"

USAGE = re.compile(
    rb'"usage":\{"input_tokens":(\d+)'
    rb'(?:,"cache_creation_input_tokens":(\d+))?'
    rb'(?:,"cache_read_input_tokens":(\d+))?'
    rb'.*?"output_tokens":(\d+)'
)
MODEL = re.compile(rb'"model":"([a-zA-Z0-9._-]+)"')


def _empty() -> dict:
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "messages": 0}


def _add(target: dict, i: int, cw: int, cr: int, o: int) -> None:
    target["input"] += i
    target["cache_write"] += cw
    target["cache_read"] += cr
    target["output"] += o
    target["messages"] += 1


def collect(days: int = 7, project: str | None = None) -> dict:
    """Aggregate usage over recent transcripts.

    Reads line by line and matches with a regex rather than parsing JSON: the
    transcript directory here is 841 MB, and json.loads on every line turns a
    ten-second report into a minute of waiting.
    """
    cutoff = time.time() - days * 86400
    total = _empty()
    by_day, by_model, by_session = {}, {}, {}
    if not TRANSCRIPTS.is_dir():
        return {"total": total, "by_day": by_day, "by_model": by_model,
                "by_session": by_session, "days": days, "files": 0}

    files = 0
    for path in TRANSCRIPTS.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        if project and project.lower() not in str(path.parent.name).lower():
            continue
        files += 1
        day = time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime))
        session = _empty()
        model_seen = None
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if b'"usage"' not in raw:
                        continue
                    m = USAGE.search(raw)
                    if not m:
                        continue
                    i, cw, cr, o = (int(g) if g else 0 for g in m.groups())
                    _add(total, i, cw, cr, o)
                    _add(by_day.setdefault(day, _empty()), i, cw, cr, o)
                    _add(session, i, cw, cr, o)
                    mm = MODEL.search(raw)
                    if mm:
                        model_seen = mm.group(1).decode()
                        _add(by_model.setdefault(model_seen, _empty()), i, cw, cr, o)
        except OSError:
            continue
        if session["messages"]:
            by_session[f"{path.parent.name}/{path.stem[:8]}"] = {
                **session, "project": path.parent.name, "model": model_seen,
            }

    return {"total": total, "by_day": by_day, "by_model": by_model,
            "by_session": by_session, "days": days, "files": files}


def billed(bucket: dict) -> int:
    """Everything the model was charged for, cheap and expensive alike."""
    return (bucket["input"] + bucket["cache_write"]
            + bucket["cache_read"] + bucket["output"])


def cache_ratio(bucket: dict) -> float:
    """Share of input tokens served from cache. Higher is cheaper."""
    served = bucket["input"] + bucket["cache_write"] + bucket["cache_read"]
    return (bucket["cache_read"] / served) if served else 0.0


def pretty_project(name: str) -> str:
    """'-Users-me-Projects-Thing' -> 'Thing'."""
    return name.strip("-").split("-")[-1] or name
