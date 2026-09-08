# Phase 4A verification — 2026-09-08

Phase 4A is complete. Phase 4B has not begun.

## Baseline and final tests

The initial working tree was clean at `14300bf` (Claude Code MCP configuration
and cwd workaround). Existing code used SQLite schema v1, standard-library
unittest, and the optional pinned `mcp==2.2.0` dependency. There were no applicable
repository AGENTS.md files. The README, schema/initialization, research records,
context builder, MCP server, backup implementation, installed/tracked hook, and
tests were inspected before implementation.

System Python initially lacked `mcp`, so full discovery failed to import the MCP
test module. Installing the existing requirements into ignored `.venv` restored
the intended environment. No requirement or dependency pin was changed. The
checked-in MCP command now launches `.venv/bin/python`, preserving its cwd wrapper.

| Verification | Result |
| --- | --- |
| Unmodified baseline, `.venv/bin/python -m unittest discover -s tests -v` | 204 run; 203 passed, 1 optional audit skipped; 56.390 s |
| Phase 4A suite without optional audit environment variable | 252 run; 251 passed, 1 optional audit skipped; 84.685 s |
| Final full suite with saved real-audit log enabled | **252 passed, no skips or failures; 78.360 s** |
| New coverage | 48 tests, plus updated schema and MCP registration expectations |
| MCP unit/protocol suite | 31 passed, including JSON-RPC structured output and invalid targets |
| Actual server smoke through checked-in `.mcp.json` | stdio JSON-RPC, 20 registered tools; all three new tools exercised |
| Live migration and backup restoration | All legacy rows preserved; restored rows exactly match live database |
| SQLite integrity / foreign-key checks | `ok` / zero violations |
| Pre-commit workflow | Existing installed hook matched tracked hook; refreshed and staged SQL backup in implementation commit |

The sandbox stalled the existing MCP integration test and a stdio handshake.
Both succeeded outside the sandbox with the same interpreter and code; final
tests ran in that working environment. The new protocol test also caught and
fixed bare-dict return annotations omitting `structuredContent`: all three new
tools now explicitly advertise structured output.

Final command (the optional private log stays outside the repository):

```bash
HYLAND_HEAT_AUDIT_LOG=/home/oska/RiderProjects/HylandHeat/TestResults/swat-network-audit-20260906/Latest.log \
  .venv/bin/python -m unittest discover -s tests -v
```

## Schema and compatibility

Schema v2 adds only `evidence_links` and its indexes: three nullable owner foreign
keys, five nullable target foreign keys, positive-ID and exactly-one checks,
`ON DELETE RESTRICT`, and one unique pair index. `database.init_db` performs the
explicit additive v1 -> v2 migration. It preserves legacy rows, IDs, provenance,
confidence, and status. Failed migration rolls back and repeated initialization
does not reset metadata. Tests use a frozen copy of the original schema v1.

Version 1 remains readable and writable through preexisting operations, and its
backups remain restorable. Evidence commands require explicit migration. There
is no automatic copying of legacy finding provenance into the new table.

## Real evidence demonstration

The live database contains one artifact/run, 350 events, one error, 18 entities,
one finding, one unknown, one decision, and one relationship. Those counts and
every preexisting row were preserved. Four explicit links were added to finding
#1, whose existing text states that OfficerLee2's copied SceneId maps to OfficerLee.

| Evidence-link ID | Target | Stored provenance |
| --- | --- | --- |
| 1 | Run #1 | Existing `read-only-audit` run, result `NEEDS_ATTENTION` |
| 2 | Event #341 | Source line 572: OfficerLee scene registry |
| 3 | Event #346 | Source line 577: OfficerLee2 scene registry |
| 4 | Relationship #1 | Existing `OfficerLee2 IS_CLONE_OF OfficerLee` record |

Events #341 and #346 both store SceneId `1693455144402701709`. Event #346 records
the registry entry instance as `1337772` while the clone instance is `-644164`,
with `ServerEntryMatches=False` and `ClientEntryMatches=False`. These references
record the basis selected for the existing finding; attachment does not change
its interpretation or its `strong` confidence. The finding's older artifact/line
reference remains intact as requested.

CLI exercised:

```bash
python3 -m database.evidence attach finding 1 run 1
python3 -m database.evidence attach finding 1 event 341
python3 -m database.evidence attach finding 1 event 346
python3 -m database.evidence attach finding 1 relationship 1
python3 -m database.evidence list finding 1 --format json --limit 2
python3 -m database.context_builder --entity-name OfficerLee2 --max-chars 1500
```

JSON listing returned `total=4`, the first two links, and `next_offset=2`.
The entity context included:

```text
Evidence (finding #1): Run #1; Event #341; Event #346; Relationship #1
```

Through the actual MCP server, repeating `attach_evidence` for event #341 returned
existing link ID 2, `list_evidence` returned four links, and entity context was
596 characters under a 1000-character budget. `backup_database` also succeeded.

## Comparison demonstration

Only one suitable real run exists in the live database. No second run was
invented there. Real `compare_runs(1, 1)` through CLI and MCP returned:

```text
identical: true
error_rows: 1 -> 1
event_rows: 350 -> 350
event_occurrences: 361 -> 361
entities: 18 -> 18
```

Both coverage entries reported zero ambiguous names, unmatched names, or event
rows lacking valid repeat counts. This verifies aggregation over real structured
data but is not a comparison between distinct real sessions.

For a directional end-to-end demonstration, two four-line synthetic logs were
ingested using the actual analyzer into the separate ignored
`reports/phase4a/demo.db`, then the existing entity extractor was run. Neither
synthetic log was inserted into the research database. The resulting comparison:

```text
Run #1 -> Run #2
result: NEEDS_ATTENTION -> INCOMPLETE
error_rows: 1 -> 0 (-1)
event_rows: 3 -> 3 (+0)
event_occurrences: 3 -> 4 (+1)
error removed: Synthetic failure Name=Old
identity event removed: Name=Old (1 occurrence)
identity event added: Name=New (2 occurrences)
identity / identity counts: 1 -> 2 (+1)
entity removed: Old
entity added: New
```

With `--limit 3`, five metadata differences were counted and two were explicitly
reported as omitted. CLI produced both Markdown and JSON. Local disposable
artifacts are under `reports/phase4a/`: `synthetic-comparison.md`,
`synthetic-comparison.json`, `real-self-comparison.json`, and `mcp-smoke.json`.
They are ignored generated outputs, not new tracked infrastructure.

## Backup and Git

A pre-migration SQL snapshot was saved before changing the live database. After
migration and attachment, `database.backup` restored a fresh temporary database;
every table and row, including metadata and all four links, matched the live
database. Both databases passed integrity checks; the restored database had no
foreign-key violations. Tests additionally verify invalid evidence rejection on
restore, duplicate protection after restore, source/alias protection, and keeping
the previous backup intact when atomic replacement fails.

Implementation commit: `3a4b0c8` — `Add evidence provenance and deterministic run comparison`.
The documentation commit follows separately. The pre-commit hook added the
updated `backups/hylandheat.sql` to the implementation commit automatically even
though it was not included in the explicit staging command. No hook policy or
unrelated analyzer behavior was rewritten.

## Files changed

- `.mcp.json`: use the environment containing the pinned SDK.
- `database/evidence.py`: validated generic provenance API, listing, CLI.
- `database/run_comparison.py`: comparison service, Markdown renderer, JSON CLI.
- `database/schema.sql`, `database/db.py`, `database/init_db.py`: v2 and migration.
- `database/context_builder.py`: evidence and hard character-budget handling.
- `database/inspect_db.py`: v2 evidence-link counts with v1 compatibility.
- `database/mcp_server.py`: three structured MCP tools.
- `database/backup.py`: consistent snapshot, atomic dump, alias/exclusive-create safeguards.
- `tests/test_evidence.py`, `tests/test_evidence_context.py`,
  `tests/test_run_comparison.py`, `tests/test_migrations.py`: new feature coverage.
- `tests/phase4_fixtures.py`, `tests/fixtures/schema_v1.sql`: isolated fixtures.
- `tests/test_database.py`, `tests/test_ingestion.py`, `tests/test_backup.py`,
  `tests/test_mcp_server.py`: updated schema expectations and integration coverage.
- `backups/hylandheat.sql`: migrated live snapshot and four evidence links.
- `README.md`, `docs/phase4a-verification.md`: commands, model, limits, verification,
  and next milestone candidates.

## Remaining limits and Phase 4B recommendation

Evidence attachment is manual, with no automatic confidence or conclusion
changes and no deletion/edit CLI. Generic references use a fixed supported set;
adding target types requires a schema change. Comparisons use exact signatures,
known manifest counts, and current catalog names; entity mentions are not proof
of spawn/despawn. There is no persistent per-run entity occurrence table, and
unmatched/ambiguous names are explicitly excluded. Output is bounded while
computation scales with the stored rows and traces. More than one real session
is required to demonstrate a distinct real-run comparison.

Recommended Phase 4B slice: explicit Git revision metadata linked to test runs
and deterministic commit ↔ test-run retrieval. Broader source-code intelligence,
SQLite FTS5, manual entities, aliases, and safe constrained automatic ingestion
remain candidates. Embeddings remain deferred until structured/full-text
retrieval demonstrably falls short. None of Phase 4B was implemented.
