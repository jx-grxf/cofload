#!/usr/bin/env python3
"""Measure what a bulk read actually saves, on your own files.

  tools/measure.py <file>... -- "<question>"

Token counts are estimates (characters / 4). They are not the model's own
tokenizer, so treat them as an order of magnitude, not an invoice.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def main() -> int:
    argv = sys.argv[1:]
    if "--" not in argv:
        print(__doc__)
        return 2
    split = argv.index("--")
    files = [Path(f) for f in argv[:split]]
    question = " ".join(argv[split + 1:])

    rows, tot_before, tot_after, tot_time = [], 0, 0, 0.0
    for f in files:
        if not f.is_file():
            print(f"skipping {f}: not a file")
            continue
        raw = f.read_text(errors="replace")
        before = est_tokens(raw)
        started = time.time()
        proc = subprocess.run(
            [str(ROOT / "bin" / "offload"), "read", str(f), "--", question],
            capture_output=True, text=True,
        )
        elapsed = time.time() - started
        if proc.returncode != 0:
            print(f"{f.name}: worker failed ({proc.stderr.strip()[:120]})")
            continue
        after = est_tokens(proc.stdout)
        rows.append((f.name, len(raw.splitlines()), before, after, elapsed))
        tot_before += before
        tot_after += after
        tot_time += elapsed

    if not rows:
        return 1
    print(f"{'file':<34}{'lines':>7}{'direct':>9}{'offload':>9}{'saved':>8}{'time':>8}")
    for name, lines, before, after, elapsed in rows:
        print(f"{name[:33]:<34}{lines:>7}{before:>9}{after:>9}"
              f"{100 - after * 100 // before:>7}%{elapsed:>7.1f}s")
    print(f"{'total':<34}{'':>7}{tot_before:>9}{tot_after:>9}"
          f"{100 - tot_after * 100 // tot_before:>7}%{tot_time:>7.1f}s")
    print("\ntokens estimated as characters/4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
