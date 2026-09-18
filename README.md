# offload

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
git clone https://github.com/<you>/offload.git
claude plugin install --plugin-dir ./offload
```

Then check what it found:

```bash
./offload/bin/offload doctor --probe
```

## Backends

`offload` calls a model you already pay for (or already get free). It picks the
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

`.offload.json` in the repository root, or `~/.config/offload/config.json`:

```json
{
  "privacy": "private",
  "backend": "auto",
  "min_lines": 350,
  "min_bytes": 60000,
  "timeout_seconds": 120
}
```

Per session, without editing anything: `OFFLOAD_BACKEND`, `OFFLOAD_MODEL`,
`OFFLOAD_MIN_LINES`, `OFFLOAD_MIN_BYTES`, `OFFLOAD_PRIVACY`.

The 350-line default comes from Spotify's measurements and holds up here: below
it, a round trip costs more time than the context is worth.

## Use

```bash
offload read src/handlers.ts -- "Which handlers touch the cache, and where?"
offload write /tmp/spec.md src/schemas/board.ts src/schemas/journey.ts
offload allow src/handlers.ts     # I need the literal lines, lift the guard
offload doctor                     # what is available here
offload stats                      # what has been routed away so far
```

Two skills ship with the plugin, so the agent knows when each applies:
`bulk-read` and `code-write`.

## What it will not do

- **Block a file that looks like a secret.** `.env`, keys, certificates and
  friends are never sent to a worker and never blocked — the expensive model
  reads those itself, or nobody does.
- **Block when no worker is available.** Every failure path is fail-open, and a
  failed `offload read` marks the file readable before it exits. A broken worker
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
