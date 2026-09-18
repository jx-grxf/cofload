---
name: code-write
description: Generate a predictable file (DTOs, test scaffolding, config, type stubs, mechanical ports) with a cheap model and write it straight to disk. Use when the output follows an existing pattern closely enough that a reference file plus a spec fully determines it.
---

# Code write

Predictable code does not need the expensive model to type it out. Write the
spec, name a reference file, and the worker produces the file on disk. The
result never enters context unless you read it back.

## Use it

```bash
cat > /tmp/spec.md <<'SPEC'
Create the Zod schema for the StationBoard response.
Fields: id (string), name (string), departures (array of Departure), updatedAt (ISO string).
Follow the export style and naming of the reference file exactly.
SPEC

"${CLAUDE_PLUGIN_ROOT}/bin/offload" write /tmp/spec.md src/schemas/station-board.ts src/schemas/journey.ts
```

Arguments: spec file, target path, then any reference files.

## The spec decides the outcome

A weak spec produces plausible code that is wrong in a way you then have to find.
Name the exact file, the exact fields, the exact conventions, and always give a
reference file — matching an existing pattern is the one thing a cheap model does
reliably.

## Always review what comes back

Read the generated file before you build on it, and run whatever check the
project has. A delegate reporting success is a claim, not evidence.

## Never delegate

Edits to existing code (worker models have no reliable line numbers), anything
touching auth, secrets, crypto, migrations or documented invariants, and any file
where being subtly wrong is expensive.
