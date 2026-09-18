# cofload

A Claude Code plugin that keeps large files out of the expensive model's context.
A hook blocks bulk reads before they happen and points at a cheap worker model,
which reads the file and answers in bullets.

Measured on a real TypeScript file: **2,538 lines, ~26,700 estimated tokens
direct, ~1,000 via the worker — 97% less context, 38 seconds.** The trade is
always the same: context for latency.

## Why a hook and not an instruction

Instructions in `CLAUDE.md` are advisory. A model under pressure reads the file
anyway, and nothing stops it. A `PreToolUse` hook runs before the tool and can
refuse. That difference is the whole idea, and it is not mine — Spotify
published it first as [shunt](https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90),
built on their internal Portal runtime. This is the same pattern with no
runtime to install: it drives whatever model CLI you already have.

## Install

```bash
git clone https://github.com/jx-grxf/cofload.git
claude plugin marketplace add ./cofload
claude plugin install cofload@cofload
```

Then check what it found on your machine, and make the CLI reachable:

```bash
./cofload/bin/cofload doctor --probe
ln -s "$PWD/cofload/bin/cofload" ~/.local/bin/cofload
```

Hooks load at session start: run `/reload-plugins` or restart Claude Code once.

## Backends

`cofload` calls a model you already pay for (or already get free). It picks the
first available one in this order, and you can pin any of them.

| Backend | How it is called | Tier |
|---|---|---|
| `ollama` | local HTTP on `:11434` | local |
| `lmstudio` | local HTTP on `:1234` | local |
| `commandcode` | `command-code -p` | private |
| `agy` | `agy -p` (Antigravity CLI) | private |
| `gemini` | Generative Language API | private |
| `opencode` | `opencode run` | private |
| `copilot` | `opencode run -m github-copilot/...` | private |
| `zen` | `opencode run -m opencode/...-free` | training |
| `command` | any command you define | yours |

**Tiers are the point.** `local` never leaves the machine. `private` is an
account-bound or paid service. `training` is a free tier that may train on what
you send it. The default `privacy: "private"` means a free tier is detected,
reported by `doctor`, and never used until you say so.

## Configure

`.cofload.json` in the repository root, or `~/.config/cofload/config.json`:

```json
{
  "privacy": "private",
  "backend": "auto",
  "min_lines": 350,
  "min_bytes": 60000,
  "timeout_seconds": 120
}
```

Per session, without editing anything: `COFLOAD_BACKEND`, `COFLOAD_MODEL`,
`COFLOAD_MIN_LINES`, `COFLOAD_MIN_BYTES`, `COFLOAD_PRIVACY`.

The 350-line default comes from Spotify's measurements and holds up here: below
it, a round trip costs more time than the context is worth.

## Use

```bash
cofload read src/handlers.ts -- "Which handlers touch the cache, and where?"
cofload write /tmp/spec.md src/schemas/board.ts src/schemas/journey.ts
cofload allow src/handlers.ts      # I need the literal lines, lift the guard
cofload doctor --probe             # what is available here, and does it answer
cofload stats                      # what the delegation actually bought you
```

## Stats

Every delegation is logged, successes and failures alike, and `cofload stats`
adds it up:

```
since 2026-09-18  (1 day)

  delegated        6 reads, 0 writes
  lines handled    5,418
  sent to worker   ~24,686 tokens
  came back        ~595 tokens
  context saved    ~24,091 tokens  (97% of what a direct read would have cost)
                   = 0.1 full 200k context windows
  time waited      0m 36s  (avg 18.0s per call)

  backend        calls     avg       saved
  commandcode        6    6.0s     24,091t

  most delegated           calls       saved
  wl.ts                        1     18,994t
  router.ts                    1      5,097t
```

`--days=7` narrows the window, `--json` gives you the raw numbers. Failed
delegations are listed with the backend and the error, because a worker that
quietly stopped working is the failure mode worth catching early.

The savings figure is deliberately conservative: it counts each token once. In
practice a file read into context is re-sent with every later turn in the
session, so the real difference is larger than what this prints.

Two skills ship with the plugin, so the agent knows when each applies:
`bulk-read` and `code-write`.

## What it will not do

- **Block a file that looks like a secret.** `.env`, keys, certificates and
  friends are never sent to a worker and never blocked — the expensive model
  reads those itself, or nobody does.
- **Block when no worker is available.** Every failure path is fail-open, and a
  failed `cofload read` marks the file readable before it exits. A broken worker
  costs you seconds, never a stuck session.
- **Delegate edits.** Worker models do not track line numbers reliably enough to
  patch code. Reads and fresh files only.
- **Delegate thinking.** A cheap model on low effort is an extractor. Anything
  architectural or safety-critical belongs in the expensive model's context.

## Honest numbers

Token counts in this README and in `tools/measure.py` are estimated as
characters / 4, not counted with the model's tokenizer. Measure your own repo:

```bash
tools/measure.py src/big-file.ts -- "What does this export?"
```

Latency is real and it is the cost: 10–40 seconds per delegated read, depending
on the backend and the file. For anything under the threshold, reading directly
is simply faster.

## License

MIT
