"""Terminal presentation: colour, bars, columns.

Every function degrades to plain text when the output is not a terminal, when
NO_COLOR is set, or when TERM says dumb - piping `cofload audit > report.txt`
should produce a readable file, not a field of escape codes.
"""

from __future__ import annotations

import os
import shutil
import sys

_ENABLED = (
    sys.stdout.isatty()
    and not os.environ.get("NO_COLOR")
    and os.environ.get("TERM", "") != "dumb"
)

CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m",
}


def c(text, *styles) -> str:
    if not _ENABLED or not styles:
        return str(text)
    prefix = "".join(CODES.get(s, "") for s in styles)
    return f"{prefix}{text}{CODES['reset']}"


def width(default: int = 88) -> int:
    try:
        return min(shutil.get_terminal_size().columns, 110)
    except OSError:
        return default


def title(text: str, right: str = "") -> None:
    line = c(f" {text} ", "bold", "cyan")
    pad = width() - len(text) - len(right) - 3
    print()
    print(line + c(" " * max(1, pad) + right, "grey"))
    print(c("─" * width(), "grey"))


def section(text: str, right: str = "") -> None:
    pad = width() - len(text) - len(right) - 2
    print()
    line = c(text.upper(), "bold")
    print(line + (c(" " * max(1, pad) + right, "grey") if right else ""))


def bar(value: float, total: float, cells: int = 22, style: str = "cyan") -> str:
    """A proportional bar. Anything above zero shows at least one cell, so a
    small-but-real value never renders as nothing."""
    if total <= 0:
        return " " * cells
    filled = int(round(value / total * cells))
    if value > 0:
        filled = max(1, filled)
    filled = min(cells, filled)
    return c("█" * filled, style) + c("·" * (cells - filled), "grey")


def kv(label: str, value: str, label_width: int = 14, indent: int = 2) -> None:
    """Label/value line. Padding happens before colouring - an escape code
    counts toward str width but not toward what you see, so colouring first
    silently breaks every column to its right."""
    print(" " * indent + c(f"{label:<{label_width}}", "grey") + value)


def cell(text: str, cw: int, *styles) -> str:
    """Pad to a visible width, then colour."""
    return c(f"{str(text)[:cw]:<{cw}}", *styles)


def row(label: str, value: str, note: str = "", indent: int = 2,
        label_width: int = 40, value_width: int = 12) -> None:
    text = (" " * indent + f"{label[:label_width]:<{label_width}}"
            + f"{value:>{value_width}}")
    if note:
        text += "  " + note
    print(text)


def note(text: str) -> None:
    print(c("  " + text, "grey"))


def verdict(text: str, level: str = "info") -> str:
    return c(text, {"good": "green", "warn": "yellow", "bad": "red"}.get(level, "grey"))


def fmt(n: int) -> str:
    return f"{n:,}"
