---
name: bulk-read
description: Answer a question about one or more large files without pulling them into context. Use when a Read was blocked for size, when a question spans several big files, or when you need to locate something in a file you do not intend to edit.
---

# Bulk read

A large file read costs context for the rest of the session. A worker model reads
it instead and returns bullets, which is roughly two percent of the tokens.

## Use it

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/offload" read path/to/file.ts -- "Which handlers touch the quota cache, and where?"
"${CLAUDE_PLUGIN_ROOT}/bin/offload" read a.ts b.ts -- "Where do these two disagree about the retry policy?"
```

Ask one specific question. "Summarize this file" wastes the round trip; the
answer will be as vague as the question.

## What comes back

Bullets, each anchored to a line number or a symbol name, so the next step is a
targeted `Read` with `offset`/`limit` rather than a whole-file read.

## When not to use it

- **You are about to edit the file.** You need the literal lines. Use a targeted
  `Read`, or `offload allow <file>` to lift the guard for half an hour.
- **The question is architectural.** A worker on low effort is an extractor, not
  a designer. Read the relevant section yourself and think about it.
- **The file holds secrets.** `offload` refuses those outright, and the guard
  never blocks them, so read them directly or not at all.

## When the worker is down

Every failure path calls `offload allow` on the files first, so the direct read
goes through immediately afterwards. A broken worker slows a session down; it
never blocks one.
