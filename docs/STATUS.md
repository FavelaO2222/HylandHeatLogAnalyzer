# Status — 2026-09-09

Fast orientation for picking this project back up, whether that's a future
session, a different agent, or a person. For mechanics (how each command
works, exact schemas, examples), see `README.md` — this file is the
narrative: what's true right now and how it got that way.

## What this is

`HylandHeatLogAnalyzer` is the research/evidence database and log analyzer
for **Hyland Heat**, a MelonLoader mod for *Schedule I* (companion repo:
`~/RiderProjects/HylandHeat`, `FavelaO2222/HylandHeat`). It ingests four
artifact classes — runtime logs, research records (findings/unknowns/
decisions/experiments), mod source, and decompiled game/library code — into
one SQLite database and serves them back as compact, honest evidence
packets (`database.research`) instead of raw dumps. "Structured and
deterministic retrieval first, semantic only where that proves
insufficient" is the standing design philosophy.

## Current state

- **Schema v8.** Live database: `data/hylandheat.db` (local only, never
  committed — see Security incident below). Committed, sanitized copies:
  `backups/hylandheat.sql` (SQL text) and `backups/hylandheat.db` (binary,
  LFS-tracked) — both exclude `source_documents` unconditionally.
- 17 findings recorded (12 active, 5 superseded — supersession chains are
  real and tracked: #5→#7, #8→#9, #10/#11→#12, #16→#17), 1 open unknown
  (#1 — goon-clone reuse vs. population-completeness), 5 test runs
  ingested, 10 source artifacts (mod source, decompiled `Assembly-CSharp`,
  decompiled `S1API`, several logs).
- Mod repo currently at commit `84a060b` with further uncommitted work in
  progress (not mine to touch without being asked).

## How it got here (condensed)

- **Phase 4D** — idempotent ingestion, provenance, `database.research` (the
  compact evidence-packet command), `database.record_experiment`.
- **Phase 4E** (schema v6) — finding-to-finding supersession
  (`superseded_by_finding_id`), added specifically because finding #5 and a
  separate notes system ("Hjarni") once silently disagreed with no way to
  say which finding replaced which. See the README's "System boundaries:
  this database vs. Hjarni" section.
- **Phase 5** (schema v7) — `agent_usage`: real, caller-supplied LLM token
  counts (never estimated), plus `database.usage_report`'s packet-vs-raw
  token-cost comparison (a chars/4 *estimate*, kept structurally separate
  from the real numbers) — the actual point of the brief-first design, made
  measurable.
- **`project_scanner.py`** (new, standalone, no database dependency) —
  scans any project root into `PROJECT_MAP.json`/`AGENT_INDEX.md`,
  respecting `.gitignore` plus built-in defaults.
- **Phase 6** (schema v8) — `database.file_index`: the database adapter on
  top of `project_scanner.py`. Recursive (`WITH RECURSIVE` over a
  `parent_path` adjacency list), decoupled from every other table
  (identified only by `(collection, path)`, no foreign keys) — portable to
  a project with none of this schema's other tables populated.
  `file_index context` cross-references `source_documents` by exact path
  match when the same `--collection` name was used for both, and
  deliberately does *not* try to link findings/unknowns/experiments (no
  path column there — would mean inventing a link).
- **`research.py` locator fixes** (findings #10→#12) — the declaration
  locator was silently matching the wrong (sometimes opposite-meaning)
  symbol for Harmony `<Member>Patch`-convention classes; fixed with a
  PascalCase-aware boundary check plus proximity-based tie-breaking.
- **S1API ingested** (artifact #10, collection `s1api`) — the actual NuGet
  reference build HylandHeat's `.csproj` compiles against, decompiled
  clean (real source + XML doc comments, not IL2CPP marshalling
  boilerplate). Directly produced finding #14.
- **Unknown #3 fixed** (findings #16→#17) — runtime logs spell a compound
  identifier as spaced words ("ENTER BUILDING"), never as the one-token
  code symbol ("EnterBuilding"). Fixed with camelCase-aware FTS widening,
  scoped to events/errors only; a follow-up correction restricted the
  independent per-section OR-broadening to the primary token only, since
  OR-ing in a bare secondary token like "NPC" turned "find this event"
  into "match nearly any event."

## Security incident (read before touching `data/hylandheat.db`)

A different session once committed the raw `data/hylandheat.db` directly —
55MB, including full `source_documents` (real decompiled game code and mod
source). It reached the public GitHub remote before being caught.
Remediation: history rewritten so no commit reachable from any ref still
holds that blob (verified across every ref, not just `HEAD`); the file was
un-ignored back to ignored and untracked going forward; a stripped copy
(`source_documents` emptied, everything else intact — 3.25MB vs 56MB) is
what's actually committed now, as `backups/hylandheat.db`. **The rule going
forward: `data/hylandheat.db` never gets committed, full stop — not via
LFS, not "just this once." Only `backups/hylandheat.sql` and
`backups/hylandheat.db` (both `source_documents`-empty) are safe to
commit.** This rule currently lives only in this file and in commit
`41c2b38`'s message — worth promoting to a `CLAUDE.md` rule if that
recurs as a risk.

## Working alongside another session

Multiple Claude Code sessions have been active in this same working
directory concurrently (same machine, same checkout). That's workable —
cross-session messaging (`ListAgents`/`SendMessage`) let two sessions catch
and correct a real bug in each other's work this session (the unknown #3
fix above went through two independently-verified correction rounds this
way) — but it means **always check `git status`/`git diff` for
uncommitted changes that aren't yours before committing anything**, and
never assume you're the only agent with changes in the working tree.

## Open threads

- **Unknown #1** (open, high importance): does goon-clone reuse hold under
  paired same-instance evidence, or does population completeness only
  *look* like reuse?
- Finding #9's working hypothesis (FishNet's `NetworkObject.TryStartDeactivation`
  reacting to any inactive `NetworkObject`, not something clone-specific)
  is still unconfirmed.
- `research.py`'s declaration/reference classifier is a line-shape
  heuristic, not a real parser — still capable of edge cases beyond the
  two already fixed.
