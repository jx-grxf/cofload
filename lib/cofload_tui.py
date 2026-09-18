"""Full-screen dashboard for cofload.

curses from the standard library, on purpose: a tool whose whole argument is
"pay less for what you do not need" should not pull in a UI framework. It also
means the dashboard works over ssh and in tmux without a single install step.
"""

from __future__ import annotations

import curses
import json
import threading
import time
from pathlib import Path

import cofload_core as core

PANES = ["overview", "backends", "stats", "audit", "usage"]
REFRESH = 2.0  # seconds between automatic redraws


class State:
    """Everything the dashboard draws, plus the background audit."""

    def __init__(self) -> None:
        self.cfg = core.load_config()
        self.root = core.repo_root()
        self.backend, self.why = core.usable_backend(self.cfg)
        self.records = []
        self.audit = None
        self.audit_error = None
        self.audit_running = False
        self.usage = None
        self.usage_running = False
        self.usage_days = 7
        self.reload_records()

    def reload_records(self) -> None:
        path = core.CACHE_DIR / "usage.jsonl"
        out = []
        if path.exists():
            for raw in path.read_text().splitlines():
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
        self.records = out

    def totals(self) -> dict:
        done = [r for r in self.records if r.get("ok") is not False]
        tin = sum(core.est_tokens(r.get("chars_in", 0)) for r in done)
        tout = sum(core.est_tokens(r.get("chars_out", 0)) for r in done)
        timed = [r for r in done if r.get("seconds")]
        return {
            "calls": len(self.records),
            "failed": len([r for r in self.records if r.get("ok") is False]),
            "in": tin, "out": tout, "saved": max(0, tin - tout),
            "lines": sum(r.get("lines", 0) for r in done),
            "seconds": sum(r.get("seconds", 0) for r in done),
            "avg": (sum(r["seconds"] for r in timed) / len(timed)) if timed else 0,
        }

    def start_audit(self) -> None:
        if self.audit_running:
            return
        self.audit_running = True
        self.audit_error = None

        def work():
            try:
                import cofload_audit as audit
                self.audit = audit.report(Path.cwd(), 60)
            except Exception as exc:  # the dashboard must survive a failed audit
                self.audit_error = str(exc)[:200]
            finally:
                self.audit_running = False

        threading.Thread(target=work, daemon=True).start()

    def start_usage(self) -> None:
        if self.usage_running:
            return
        self.usage_running = True

        def work():
            try:
                import cofload_usage as usage
                self.usage = usage.collect(self.usage_days)
            except Exception:
                self.usage = None
            finally:
                self.usage_running = False

        threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------
# drawing helpers
# --------------------------------------------------------------------------

def _pair(n: int) -> int:
    try:
        return curses.color_pair(n)
    except curses.error:
        return 0


def line(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write inside the window or not at all; curses raises at the last cell."""
    h, w = win.getmaxyx()
    if not (0 <= y < h):
        return
    try:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)
    except curses.error:
        pass


def bar(value: float, total: float, cells: int) -> str:
    if total <= 0:
        return "·" * cells
    filled = int(round(value / total * cells))
    if value > 0:
        filled = max(1, filled)
    filled = min(cells, filled)
    return "█" * filled + "·" * (cells - filled)


def fmt(n) -> str:
    return f"{int(n):,}"


# --------------------------------------------------------------------------
# panes
# --------------------------------------------------------------------------

def draw_overview(win, st: State, top: int) -> None:
    t = st.totals()
    y = top
    line(win, y, 2, "CONTEXT SAVED", curses.A_BOLD); y += 2

    if t["in"]:
        share = t["saved"] * 100 // t["in"]
        h, w = win.getmaxyx()
        cells = max(10, min(46, w - 24))
        kept = max(1, int(round(t["out"] / t["in"] * cells))) if t["out"] else 0
        line(win, y, 4, "█" * (cells - kept), _pair(2))
        line(win, y, 4 + cells - kept, "█" * kept, _pair(3))
        line(win, y, 6 + cells, f"{share}%", curses.A_BOLD)
        y += 2
        line(win, y, 4, f"would have cost   ~{fmt(t['in'])} tokens"); y += 1
        line(win, y, 4, f"came back         ~{fmt(t['out'])} tokens"); y += 1
        line(win, y, 4, f"saved             ~{fmt(t['saved'])} tokens", _pair(2)); y += 1
        line(win, y, 4, f"                  ≈ {t['saved'] / 200_000:.2f} × a 200k window",
             _pair(4)); y += 2
    else:
        line(win, y, 4, "nothing delegated yet", _pair(4)); y += 2

    line(win, y, 2, "ACTIVITY", curses.A_BOLD); y += 2
    line(win, y, 4, f"delegations       {t['calls']}"
                    + (f"   ({t['failed']} failed)" if t["failed"] else "")); y += 1
    line(win, y, 4, f"lines handled     {fmt(t['lines'])}"); y += 1
    line(win, y, 4, f"time waited       {int(t['seconds']) // 60}m "
                    f"{int(t['seconds']) % 60}s   avg {t['avg']:.1f}s"); y += 2

    line(win, y, 2, "WORKER", curses.A_BOLD); y += 2
    if st.backend:
        line(win, y, 4, f"{st.backend.name} · {st.backend.model}", _pair(2)); y += 1
        line(win, y, 4, f"privacy={st.cfg.get('privacy')}   "
                        f"threshold={st.cfg.get('min_lines')} lines / "
                        f"{int(st.cfg.get('min_bytes', 0)) // 1024} KB", _pair(4))
    else:
        line(win, y, 4, st.why, _pair(1)); y += 1
        line(win, y, 4, "nothing is being blocked while this is the case", _pair(4))


def draw_backends(win, st: State, top: int) -> None:
    y = top
    line(win, y, 2, "BACKENDS", curses.A_BOLD)
    line(win, y, 30, f"privacy={st.cfg.get('privacy')} allows "
                     f"{', '.join(sorted(core.allowed_tiers(st.cfg)))}", _pair(4))
    y += 2
    allowed = core.allowed_tiers(st.cfg)
    for name in core.AUTO_ORDER + ["command"]:
        b = core.detect(name, st.cfg)
        tier = core.TIERS.get(name, "private")
        active = st.backend and st.backend.name == name
        if b and tier in allowed:
            mark, state, colour = ("▶" if active else "•",
                                   "active" if active else "available",
                                   _pair(2) if active else 0)
        elif b:
            mark, state, colour = "•", f"blocked by privacy", _pair(3)
        else:
            mark, state, colour = "·", "not available", _pair(4)
        line(win, y, 2, mark, colour)
        line(win, y, 4, f"{name:<13}", curses.A_BOLD if active else 0)
        line(win, y, 18, f"{state:<16}", colour)
        line(win, y, 35, f"{tier:<10}", _pair(4))
        line(win, y, 46, (b.model if b else "")[:40], _pair(4))
        y += 1
    y += 1
    line(win, y, 2, "p  probe the active backend", _pair(4))


def draw_stats(win, st: State, top: int) -> None:
    done = [r for r in st.records if r.get("ok") is not False]
    y = top
    if not done:
        line(win, y, 2, "nothing delegated yet", _pair(4))
        return

    by_kind, by_backend, by_file = {}, {}, {}
    for r in done:
        saved = max(0, core.est_tokens(r.get("chars_in", 0))
                    - core.est_tokens(r.get("chars_out", 0)))
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
        agg = by_backend.setdefault(r.get("backend", "?"), {"n": 0, "saved": 0, "s": 0.0,
                                                            "timed": 0})
        agg["n"] += 1
        agg["saved"] += saved
        if r.get("seconds"):
            agg["timed"] += 1
            agg["s"] += r["seconds"]
        for name in r.get("paths", []) or []:
            f = by_file.setdefault(name, {"n": 0, "saved": 0})
            f["n"] += 1
            f["saved"] += saved

    line(win, y, 2, "BY KIND", curses.A_BOLD)
    line(win, y, 30, "  ".join(f"{n}× {k}" for k, n in sorted(by_kind.items())), _pair(4))
    y += 2

    line(win, y, 2, "BY BACKEND", curses.A_BOLD); y += 2
    peak = max(a["saved"] for a in by_backend.values()) or 1
    for name, agg in sorted(by_backend.items(), key=lambda kv: -kv[1]["saved"]):
        avg = agg["s"] / agg["timed"] if agg["timed"] else 0
        line(win, y, 4, bar(agg["saved"], peak, 18), _pair(2))
        line(win, y, 24, f"{name:<16}{fmt(agg['saved']) + 't':>10}")
        line(win, y, 52, f"{agg['n']} calls · avg {avg:.1f}s", _pair(4))
        y += 1
    y += 1

    line(win, y, 2, "MOST DELEGATED", curses.A_BOLD); y += 2
    top_files = sorted(by_file.items(), key=lambda kv: -kv[1]["saved"])[:8]
    peak = top_files[0][1]["saved"] if top_files else 1
    for name, agg in top_files:
        line(win, y, 4, bar(agg["saved"], peak or 1, 18), _pair(5))
        line(win, y, 24, f"{name[:24]:<26}{fmt(agg['saved']) + 't':>10}")
        line(win, y, 62, f"{agg['n']}×", _pair(4))
        y += 1

    failed = [r for r in st.records if r.get("ok") is False]
    if failed:
        y += 1
        line(win, y, 2, f"FAILED ({len(failed)})", curses.A_BOLD | _pair(1)); y += 1
        for r in failed[-3:]:
            when = time.strftime("%d %b %H:%M", time.localtime(r.get("ts", 0)))
            line(win, y, 4, f"{when}  {r.get('backend', '?')}: "
                            f"{r.get('error', '')[:60]}", _pair(1))
            y += 1


def draw_audit(win, st: State, top: int) -> None:
    y = top
    if st.audit_running:
        line(win, y, 2, "measuring plugins, servers and transcripts…", _pair(3))
        line(win, y + 2, 2, "this reads every recent transcript, it takes ~10s", _pair(4))
        return
    if st.audit_error:
        line(win, y, 2, f"audit failed: {st.audit_error}", _pair(1))
        return
    if st.audit is None:
        line(win, y, 2, "press a to measure what every session costs here", _pair(4))
        return

    rep = st.audit
    total = rep["plugin_tokens"] + rep["file_tokens"]
    line(win, y, 2, "FIXED COST OF EVERY SESSION", curses.A_BOLD)
    line(win, y, 40, f"~{fmt(total)} tokens, re-sent every turn", _pair(3))
    y += 2

    active = [p for p in rep["plugins"] if p["tokens"] > 0]
    peak = max([p["tokens"] for p in active] or [1])
    line(win, y, 2, f"plugins  {fmt(rep['plugin_tokens'])}t", curses.A_BOLD); y += 1
    for pl in active[:6]:
        used = pl["uses"]
        line(win, y, 4, bar(pl["tokens"], peak, 16), _pair(2) if used else _pair(3))
        line(win, y, 22, f"{pl['name'].split('@')[0][:22]:<24}"
                         f"{fmt(pl['tokens']) + 't':>8}")
        line(win, y, 56, f"{used} uses" if used else "no calls found",
             0 if used else _pair(3))
        y += 1
    y += 1

    peak = max([f["tokens"] for f in rep["files"]] or [1])
    line(win, y, 2, f"instructions  {fmt(rep['file_tokens'])}t", curses.A_BOLD); y += 1
    for f in rep["files"][:5]:
        line(win, y, 4, bar(f["tokens"], peak, 16), _pair(5))
        line(win, y, 22, f"{Path(f['path']).name[:22]:<24}{fmt(f['tokens']) + 't':>8}")
        y += 1
    y += 1

    line(win, y, 2, f"mcp servers  {len(rep['servers'])}", curses.A_BOLD); y += 1
    peak = max([s["uses"] for s in rep["servers"]] or [1])
    for srv in rep["servers"][:8]:
        used = srv["uses"]
        line(win, y, 4, bar(used, peak, 16), _pair(2) if used else _pair(3))
        line(win, y, 22, f"{srv['name'][:30]:<32}")
        line(win, y, 56, f"{used} calls" if used else "no calls found",
             0 if used else _pair(3))
        y += 1


def _big(n) -> str:
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= limit:
            return f"{n / limit:.1f}{suffix}"
    return str(int(n))


def draw_usage(win, st: State, top: int) -> None:
    import cofload_usage as usage

    y = top
    if st.usage_running:
        line(win, y, 2, "reading transcripts…", _pair(3))
        return
    if st.usage is None:
        line(win, y, 2, "press u to read real billed tokens from your transcripts",
             _pair(4))
        return

    data = st.usage
    total = data["total"]
    if not total["messages"]:
        line(win, y, 2, f"no usage recorded in the last {data['days']} days", _pair(4))
        return

    billed = usage.billed(total) or 1
    line(win, y, 2, "BILLED", curses.A_BOLD)
    line(win, y, 30, f"{_big(billed)} tokens over {data['days']} days "
                     f"· {data['files']} transcripts", _pair(4))
    y += 2
    parts = [("cache read", total["cache_read"], 2),
             ("cache write", total["cache_write"], 3),
             ("output", total["output"], 5),
             ("fresh input", total["input"], 5)]
    peak = max(v for _, v, _ in parts) or 1
    for label, value, colour in parts:
        line(win, y, 4, bar(value, peak, 20), _pair(colour))
        line(win, y, 26, f"{label:<14}{_big(value):>9}")
        line(win, y, 52, f"{value * 100 / billed:.1f}%", _pair(4))
        y += 1
    y += 1
    ratio = usage.cache_ratio(total)
    colour = _pair(2) if ratio > 0.9 else (_pair(3) if ratio > 0.7 else _pair(1))
    line(win, y, 2, f"cache ratio   {ratio * 100:.1f}%", colour | curses.A_BOLD)
    line(win, y, 30, "higher is cheaper: context reused, not rebuilt", _pair(4))
    y += 2

    if data["by_day"]:
        line(win, y, 2, "BY DAY", curses.A_BOLD); y += 1
        peak = max(usage.billed(b) for b in data["by_day"].values()) or 1
        for day in sorted(data["by_day"])[-7:]:
            b = data["by_day"][day]
            line(win, y, 4, bar(usage.billed(b), peak, 20), _pair(5))
            line(win, y, 26, f"{day:<12}{_big(usage.billed(b)):>9}")
            line(win, y, 50, f"{b['messages']:,} msgs · cache "
                             f"{usage.cache_ratio(b) * 100:.0f}%", _pair(4))
            y += 1
        y += 1

    busiest = sorted(data["by_session"].items(),
                     key=lambda kv: -usage.billed(kv[1]))[:5]
    if busiest:
        line(win, y, 2, "BUSIEST SESSIONS", curses.A_BOLD); y += 1
        peak = usage.billed(busiest[0][1]) or 1
        for name, b in busiest:
            line(win, y, 4, bar(usage.billed(b), peak, 20), _pair(2))
            line(win, y, 26, f"{usage.pretty_project(b['project'])[:20]:<22}"
                             f"{_big(usage.billed(b)):>9}")
            line(win, y, 60, f"{b['messages']:,} msgs", _pair(4))
            y += 1


DRAW = {"overview": draw_overview, "backends": draw_backends,
        "stats": draw_stats, "audit": draw_audit, "usage": draw_usage}


def _main(stdscr) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    try:
        curses.use_default_colors()
        for i, fg in enumerate([curses.COLOR_RED, curses.COLOR_GREEN,
                                curses.COLOR_YELLOW, 8, curses.COLOR_BLUE], start=1):
            curses.init_pair(i, fg, -1)
    except curses.error:
        pass

    st = State()
    pane = 0
    probe = ""
    last = 0.0

    while True:
        now = time.time()
        if now - last > REFRESH:
            st.reload_records()
            last = now
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        header = f" cofload · {st.root.name} "
        line(stdscr, 0, 0, header + " " * max(0, w - len(header)),
             curses.A_REVERSE | curses.A_BOLD)
        tabs = ""
        for i, name in enumerate(PANES):
            tabs += f"  {i + 1} {name}" + ("  ◂" if i == pane else "   ")
        line(stdscr, 1, 1, tabs, _pair(4))
        line(stdscr, 2, 0, "─" * w, _pair(4))

        DRAW[PANES[pane]](stdscr, st, 4)

        if probe:
            line(stdscr, h - 3, 2, probe, _pair(2) if "OK" in probe else _pair(1))
        footer = "  1-5 panes   a audit   u usage   p probe   r refresh   q quit"
        line(stdscr, h - 1, 0, footer + " " * max(0, w - len(footer)), curses.A_REVERSE)
        stdscr.refresh()

        try:
            key = stdscr.getch()
        except curses.error:
            key = -1
        if key == -1:
            time.sleep(0.08)
            continue
        ch = chr(key) if 0 < key < 256 else ""
        if ch in ("q", "\x1b"):
            return
        if ch in "12345":
            pane = int(ch) - 1
            if PANES[pane] == "audit" and st.audit is None and not st.audit_running:
                st.start_audit()
            if PANES[pane] == "usage" and st.usage is None and not st.usage_running:
                st.start_usage()
        elif ch == "a":
            st.audit = None
            st.start_audit()
            pane = PANES.index("audit")
        elif ch == "u":
            st.usage = None
            st.start_usage()
            pane = PANES.index("usage")
        elif ch == "r":
            st.cfg = core.load_config()
            st.backend, st.why = core.usable_backend(st.cfg)
            st.reload_records()
        elif ch == "p" and st.backend:
            probe = "probing…"
            stdscr.refresh()
            started = time.time()
            try:
                out = core.run_worker(st.backend, "Reply with exactly one word: OK",
                                      st.cfg)
                probe = f"probe: {out.strip()[:30]!r} in {time.time() - started:.1f}s"
            except core.CofloadError as exc:
                probe = f"probe failed: {exc}"[:100]


def run() -> int:
    curses.wrapper(_main)
    return 0
