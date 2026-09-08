# Hyland Heat Log Analyzer

A local, offline command-line analyzer for Hyland Heat / MelonLoader `.log` and
`.txt` files. Python 3.9+; standard library only. No game, internet connection,
dependencies, or virtual environment required. Input logs are opened read-only.
The one exception is `database/mcp_server.py` (see
[MCP server](#mcp-server)), which needs the `mcp` package; nothing else in
this project does.

Current milestone: **Phase 4A — Evidence Provenance and Run Comparison**,
using database schema v2. See [Phase 4A](#phase-4a-evidence-provenance-and-run-comparison)
for migration, commands, comparison rules, and the Phase 4B boundary.
Structured and deterministic retrieval first. Semantic retrieval only where
exact retrieval eventually proves insufficient.

## PyCharm and Usage

Open the `HylandHeatLogAnalyzer` folder using PyCharm's **File > Open**. Select an
installed Python 3.9+ interpreter in the project's Python Interpreter settings.
Optionally create a virtual environment there, or run `python -m venv .venv` and
select its interpreter. No packages need installing.

For MCP and the **complete** test suite, use the existing pinned MCP dependency
in a local environment (Python 3.11+ for the protocol tests; verified on 3.12):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python -m unittest discover -s tests -v
```

The checked-in `.mcp.json` launches this environment's interpreter. The analyzer
and non-MCP database commands still need only the standard library.

From the terminal in this folder (`python3` also works on Linux):

```bash
python hyland_heat_log_analyzer.py "/path/to/HylandHeat_20260907_123456.log"
python hyland_heat_log_analyzer.py "/path/to/log.log" --output "./reports"
python hyland_heat_log_analyzer.py sample_logs/synthetic_playtest.log --output reports --csv
python hyland_heat_log_analyzer.py "/path/to/audit.log" --profile read-only-audit --brief --output reports
python hyland_heat_log_analyzer.py "/path/to/log.txt" --no-json
python -m unittest discover -s tests -v
```

By default, text and JSON reports go into a `reports` directory beside the input.
`--output` selects another directory; relative paths are relative to the terminal's
working directory. Report names use the input stem plus `_summary.txt` and
`_summary.json`. `--csv` adds deduplicated event rows in `_summary.csv`.
Existing reports with these names are overwritten. Input aliases are rejected.
The console prints the verdict, reasons, and report paths. Exit status is 0 for
successful analysis (including NEEDS ATTENTION), 2 for invalid input or I/O errors.

## Evidence and Verdicts

Select the test requirements explicitly with `--profile`:

| Profile | Required evidence |
| --- | --- |
| `general` (default) | Both goon transition directions and confirmed SWAT deployment |
| `goon-lifecycle` | Both actual goon transition directions |
| `swat-deployment` | Confirmed SWAT deployment with unit evidence |
| `read-only-audit` | Explicit read-only network audit baseline, registry membership and scene registry messages, and dormant/live-zero observation |

Profiles are user-selected test context, not inferred intent. A read-only audit
does not require spawning or goon transitions. Only the known
`DEPLOY REJECTED: lifecycle research locked` message with `State=Dormant, Live=0`
is treated as an expected gate in that profile. Other rejection reasons still
need attention. Positive live/tracked observations or a non-dormant SWAT state
conflict with read-only scope and need attention. Errors, duplicate identities,
explicit failed/blocked tests, and integrity failures remain visible in every
profile. Analyze one playtest session at a time.

An audit PASS means the required diagnostic observations were captured; it does
not validate the registry values or establish that clones are safe. Prefab
catalog completeness is not a requirement of this basic identity-audit profile.

The following rules apply to the selected profile (the original general profile
still requires both subsystems):

- NEEDS ATTENTION: error/exception/fatal lines, explicit duplicate identities,
  rejected/failed/blocked SWAT attempts, test failures/blocked prerequisites,
  or explicit identity/transition integrity failures.
- INCONCLUSIVE: either goon direction or confirmed SWAT deployment is missing;
  explicit inconclusive test verdicts, warnings, or decoding replacement
  characters also prevent PASS.
- PASS: the selected profile's required observations
  were logged with no detected problems above. This is only an evidence check,
  not proof of gameplay correctness, identity ownership, or safe teardown.

An actual transition requires `GOON ACTUAL TRANSITION:` and one of
`UNSPAWNED_TO_SPAWNED` / `SPAWNED_TO_UNSPAWNED`. The directly following BEFORE/AFTER
lines are attached to it. Each transition occurrence is retained, even if its
classification line repeats. The state labels come from the classification;
snapshot fields are exported separately. Instance appearance, disappearance,
replacement, and ordinary snapshots never count as actual transitions.

SWAT confirmation requires explicit `State=Deployed`, `DEPLOY SUCCESS`, or similar
success wording plus a positive live/tracked count or `TRACKED UNIT: ID=...`.
Unit evidence must appear on that line or within the next five physical lines.
Rejection, recall, zero counts, other states, negative wording, and session
initialization/shutdown clear pending success. Count-only lines do not prove a
deployment. Unrecognized success wording remains inconclusive. Confirmation does
not prove safe network identity or cleanup. Rejections remain attention items
unless the explicit read-only profile recognizes the exact expected safety gate.

## Low-Cost AI Review

Add `--brief` to also write `<log_stem>_brief.txt`, capped at 6,000 characters
(not an exact token budget). Start AI review with this file. It includes the
profile, verdict, counts, recommendations, and ranked evidence with original
source line numbers. Retrieve specific source lines when more context is needed;
do not routinely send the complete JSON or original log to the model.

The detailed summary prioritizes errors, verdicts, rejections, actual transitions,
and diagnostic results over routine observations. Within each priority, later
messages come first, so final results are not hidden behind startup noise.
Both outputs are selections and cannot guarantee every important result fits;
JSON and original source references remain available for follow-up.

## Patterns and Provenance

`patterns.py` centralizes compiled, case-insensitive expressions, field extraction,
severity classification, and evidence markers. Add a named regex to `PATTERNS`
to recognize new messages; category counts and JSON events include it automatically.
Use a `swat_` prefix for SWAT-only classifications. Add a test string in
`tests/test_analyzer.py`; if adding a new report group or verdict rule, update the
analyzer explicitly. Keep observation patterns separate from success rules.

The initial phrases were checked against a locally available saved MelonLoader
network-audit log: `INACTIVE CLONE CREATED`, `OFFICER CLONE QUEUE PROGRESS`,
`REGISTRY MEMBERSHIP`, `SCENE REGISTRY`, `DEPLOY REJECTED`, `BASELINE WORLD READY`,
and `RECALL IGNORED`. Initialization and dormant/live-zero forms were also observed.
Actual goon transition, duplicate identity, and population-ready formats were
checked against local C# logger emit sites; they were not confirmed by that saved
runtime log. SWAT success and test-verdict patterns are provisional vocabulary
tested with synthetic strings. The supplied sample is entirely synthetic and
contains no copied personal paths or real identities.

Useful future examples to supply: successful SWAT deployment and tracking lines,
test-run start/end/verdict formats, shutdown variants, and police-out/infamy/patrol
messages that the general patterns miss. Supply exact message text with private
values redacted. Never broaden a confirmation pattern merely to obtain PASS.

## Deduplication, Exports, and Limitations

Input is read line by line as UTF-8 (optional BOM), replacing invalid bytes.
First/last timestamps mean file order, not chronological minimum/maximum; missing
dates and midnight rollover are not inferred. Timestamp strings preserve their
original offsets and precision.

"Unique logical events" means exact message text after stripping the leading
timestamp and ANSI color codes. Source tags, severity, case, identities, and
numbers remain significant. Distinct messages with changing counters remain
distinct. Category counts overlap, but matching lines count each line once.
Each severity line receives one severity, in priority order FATAL, ERROR,
EXCEPTION, WARNING. Consecutive .NET stack frames and inner-exception markers are
attached to the first occurrence of each exception/error message, up to 40 lines,
with original line references and an omitted-line count. They are not counted
as independent matching events or severity lines. Repeated exception messages
retain the first stack only; later stack variants remain in the original log.
The brief selects up to five frames per included error, including later callers.

JSON schema version 2 contains metadata, selected profile, missing evidence,
expected rejection references, attached stack frames, deduplicated detected events, all actual
transition occurrences, identity labels, SWAT events and confirmation references,
test verdicts, repeated messages, and important source lines. Events contain
first/last line, first timestamp, count, original first line, categories, and
parsed fields. No claim is made that separate identity labels are separate NPCs.
Memory scales with unique matching messages, identity labels, and transition
occurrences, not every input line. Logs with many unique snapshots can still
produce large JSON files. Text sections show at most 12 messages, 30 identity
labels, and 30 important source lines, with every actual transition listed.
JSON retains all recognized unique events. Reports contain original log data;
review them for private information before sharing.

Comma-delimited fields are parsed best-effort, so embedded commas (positions or
lists) may yield partial values; original lines are retained. Multiline transition
association assumes the logger's immediate BEFORE/AFTER layout and cannot prove
association under interleaved writers. General regex patterns can produce false
positives or miss new wording, including unusual negations. Other mods' errors
remain visible rather than being attributed automatically to Hyland Heat.
Concatenated logs without session boundaries should be analyzed separately.

This tool only summarizes evidence present in the log and cannot prove behavior
that was never logged. A logged test PASS cannot substitute for missing subsystem
evidence. Recommendations do not authorize risky spawn or teardown experiments.

The `analyze`, `parse_line`, and `render_report` functions can be reused for future
timeline, comparison, and test-specific evidence extraction features.

## Research Database: Phase 1

This section records the Phase 1 foundation. Optional analyzer ingestion is now
implemented in Phase 2, documented below. This section describes historical
schema v1; Phase 4A adds the explicit v1 -> v2 migration described below.

### Project Structure Found

Before adding the database, the PyCharm workspace contained a separate `main.py`
and the `HylandHeatLogAnalyzer/` directory. The analyzer directory contained
`hyland_heat_log_analyzer.py`, `patterns.py`, this README, `.gitignore`,
`sample_logs/`, `reports/`, and `tests/test_analyzer.py`. It used standalone
Python modules and standard-library `unittest`, with no existing database package
or packaging configuration. The 26 analyzer tests passed before this addition.
No existing analyzer files were moved, renamed, or behaviorally changed.

The independent database component now lives alongside those modules:

```text
HylandHeatLogAnalyzer/
  database/
    __init__.py
    schema.sql
    init_db.py
    db.py
  data/
    hylandheat.db          # generated, ignored by Git
  tests/
    test_analyzer.py       # unchanged
    test_database.py
```

### Role and Sources of Truth

SQLite is a local, portable, file-based structured research/evidence store using
Python's built-in `sqlite3`. No server, ORM, or additional dependencies are needed.

```text
Raw logs/files             = authoritative raw evidence
SQLite                    = structured evidence and research knowledge
Git/source tree           = authoritative code
AI briefs/context packets = disposable derived context for LLMs
```

Raw evidence, structured events/errors, research findings, unresolved questions,
and intentional decisions remain separate records. A decision does not become
runtime evidence; an unknown does not become a confirmed finding automatically.

The intended future flow is:

```text
Raw evidence -> parser/scanner -> SQLite -> context builder -> AI consumer
```

Phase 1 implements only SQLite. It does not ingest logs, integrate the analyzer,
scan assemblies or source code, generate findings, query an LLM, use embeddings
or vector search, or expose MCP tools. Analyzer JSON schema version 2 and database
schema version 1 are separate version schemes.

### Initialization and Paths

From the `HylandHeatLogAnalyzer` directory:

```bash
python -m database.init_db
python -m database.init_db --database data/hylandheat.db
```

Both commands create `HylandHeatLogAnalyzer/data/hylandheat.db`. Parent directories
are created as needed. Absolute paths are also accepted. All relative database
paths are resolved against `HylandHeatLogAnalyzer/`, determined from `db.py`'s
location, regardless of the current shell directory. `~` is expanded normally.
This database-path rule is independent of the analyzer's existing report-path rule.

Python still needs to locate the `database` module: run from the analyzer folder,
set the PyCharm run configuration's working directory there, or add that folder
to `PYTHONPATH`. For example, from elsewhere on Linux/macOS:

```bash
PYTHONPATH="/path/to/HylandHeatLogAnalyzer" python -m database.init_db --database data/hylandheat.db
```

Initialization prints the resolved database path and version, and returns exit
code 0. Errors print a concise message to stderr and return 2. Initialization
now creates schema v2 or explicitly upgrades v1, preserving existing data.
Reapplying v2 preserves metadata timestamps. No sample or research data is inserted.

`schema.sql` is authoritative for fresh databases. The initializer applies it
inside a transaction so schema failures roll back together. It rejects existing
nonempty databases without version metadata and rejects unsupported versions.
It supports the additive v1 -> v2 migration, but does not validate/repair arbitrary
hand-edited schemas or migrate unknown versions.
An initialization failure can leave an empty database file, which can be retried.
Generated `.db`, `.sqlite`, `.sqlite3`, and their journal/WAL/SHM files are ignored;
`schema.sql` remains tracked source. Database backups and raw files must be kept
separately from Git as appropriate.

### Schema and Design Choices

| Table | Responsibility |
| --- | --- |
| `schema_metadata` | Singleton row holding schema version and timestamps |
| `source_artifacts` | Paths, filenames, optional SHA-256 and original timestamps for raw material |
| `test_runs` | Sessions, optional primary artifact, build labels, profile, and result |
| `events` | Structured runtime evidence with direct artifact and optional line/run references |
| `errors` | Actual errors, exceptions, and fatal messages, including full stack traces |
| `entities` | Named research subjects; no automatic entity discovery |
| `findings` | Conclusions with confidence, status, subject, and optional provenance |
| `unknowns` | Open questions, importance, status, and required evidence |
| `decisions` | Intentional choices and reasons, optionally linked to a supporting finding |
| `relationships` | Directed, typed entity-to-entity links with optional provenance |
| `evidence_links` (v2) | Explicit finding/unknown/decision references to runs, events, errors, entities, or relationships |

All tables have integer primary keys. Foreign keys use `ON DELETE RESTRICT`:
deleting an artifact, session, entity, or finding cannot silently orphan records
that reference it. Indexes cover foreign keys, artifact lines/hashes, event
categories, and common research status queries.

Events and errors require a raw source artifact; a run and source line can be
absent. Lines, when provided, must be positive integers. A finding's line also
requires its artifact reference. Findings otherwise permit incomplete provenance
and default to `confidence='unknown'`; applications must not interpret mere
storage as verification. A session's primary artifact need not be the event's
artifact, since one session may eventually involve multiple files.

`decisions.finding_id` is an optional addition to the suggested fields, preserving
a direct finding-to-decision link. Phase 4A adds multiple explicit evidence links
without replacing it. No conclusions or transitions between these record types
happen automatically.

Controlled enums use CHECK constraints. Test results are `PASS`,
`NEEDS_ATTENTION`, `FAIL`, `INCOMPLETE`, and `UNKNOWN`; a future importer must map
analyzer verdicts explicitly. Error severity is `ERROR`, `EXCEPTION`, or `FATAL`;
routine warnings can eventually be structured events. Entity types, relationship
types, profiles, components, and event categories remain extensible text.

Hashes are optional 64-character hexadecimal strings, indexed but not unique:
identical bytes may legitimately appear at different paths. Phase 1 does not
hash or import files. Entity names/canonical names are also not forced unique
because build and namespace identity rules have not been established.

Database-generated timestamps are UTC ISO-style TEXT ending in `Z`. Source
timestamps are optional TEXT so original precision/offsets or time-only values
can be retained. `source_artifacts.created_at` denotes the optional original
artifact creation time; `imported_at` records insertion time. Writers must update
`updated_at` and `resolved_at` explicitly as appropriate; no automatic update
triggers or research-status transitions are installed. Stack traces use TEXT
with no application-imposed truncation or newline normalization.

### Connection Lifecycle

`database.db` provides `resolve_database_path`, `database_exists`,
`connect_database`, and `initialize_database`. `database_exists` checks only for
a file, not schema validity, and has no write side effects. `connect_database`
opens an existing file by default, preventing accidental creation from a typo.
Its `create=True` option permits an empty file but does not create the schema.

Every helper-opened connection enables foreign keys and uses `sqlite3.Row`.
The caller owns transaction commit/rollback and connection closure. A connection
context commits on success and rolls back on an exception; it does not close the
connection, so use `contextlib.closing` as shown below. Initialization owns and
closes its own connection. No WAL or other performance tuning is applied.

```python
from contextlib import closing
from database.db import connect_database

with closing(connect_database()) as connection:
    rows = connection.execute(
        "SELECT question FROM unknowns WHERE status = ?", ("open",)
    ).fetchall()
    # For future writes, use `with connection:` and parameterized SQL values.
```

### Tests and Phase Boundary

All database tests use temporary directories/databases, including CLI tests.

```bash
python -m unittest discover -s tests -p test_database.py -v
python -m unittest discover -s tests -p test_analyzer.py -v
python -m unittest discover -s tests -v
```

Phase 1 adds 18 database tests to the 26 existing analyzer tests. Database tests
cover schema creation and idempotency, foreign keys, representative insert/read
operations, provenance joins, a 2,000-frame multiline trace, constraints,
transactions, path resolution, CLI behavior, and incompatible versions.

Phase 2 requires a separate instruction before connecting logs/analyzer outputs
to database records. Ingestion, deduplication/upserts, verdict mapping, hashing,
and any evidence-to-finding workflow are intentionally deferred.

## Research Database: Phase 2

SQLite is an **optional additional output**. Without `--database`, analysis does
not import SQLite modules, hash logs, open a database, or attempt persistence.
The existing profile, report, JSON/CSV, and 6,000-character brief behavior remains
unchanged. The brief is never registered or imported as evidence.

From `HylandHeatLogAnalyzer/`:

```bash
python -m database.init_db --database data/hylandheat.db
python hyland_heat_log_analyzer.py "/path/to/log.log" \
    --profile read-only-audit --brief --output reports \
    --database data/hylandheat.db
python -m database.inspect_db --database data/hylandheat.db --latest-run
```

Initialization is automatic if the requested database does not exist. Existing
databases are version-checked without running schema creation or migrations.
Incompatible databases fail clearly rather than being recreated. Database paths
remain project-relative; report paths keep their existing working-directory rule.
Report destinations cannot overwrite the requested database or the source log.

### Integration and Capture

`analyze(..., capture=...)` optionally feeds the already parsed lines into
`analysis_capture.AnalysisCapture`. This module has no SQLite dependency. The
ordinary result/report structures remain unchanged. After analysis and report
generation, the CLI passes that result and completed capture to
`database.ingestion.persist_analysis`. SQL lives only in the database package.

Optional capture calculates SHA-256 once in a streaming binary pass, then the
normal parser reads the log once. Capture collects uncapped consecutive .NET
stack frames during that parse, including repeated errors with different traces
and original newline sequences. Recognized inner-exception and end-of-stack
markers are retained. The existing report's first-stack/40-frame selection does
not limit database storage. Warning-level exception traces remain warning-event
provenance instead of being promoted to real errors. Unrecognized multiline
formats remain a parser limitation; raw files remain authoritative.

Source file identity, size, mtime, and ctime are checked around hashing, parsing,
and persistence. Changing logs are rejected; run ingestion after the game/log is
closed. These checks detect ordinary changes, not adversarial file replacement.
UTF-8 decoding errors still use replacement characters in structured text; the
hash always describes the original bytes.

### Records, Deduplication, and Atomicity

Only four tables receive imported rows:

- `source_artifacts`: log path/name and content SHA-256. Original creation time
  stays null because filesystem modification time is not creation time.
- `test_runs`: first observed timestamp, selected profile, and mapped result.
  `PASS` maps to `PASS`, `NEEDS ATTENTION` to `NEEDS_ATTENTION`, and `INCONCLUSIVE`
  to `INCOMPLETE`. The original verdict is also retained in notes. Game/mod build
  fields remain null rather than being guessed.
- `events`: one row per unique non-error message already selected by the analyzer,
  including warnings and diagnostics. Unclassified raw chatter is not stored.
- `errors`: one row per actual ERROR/EXCEPTION/FATAL occurrence, with its complete
  recognized stack. Expected deployment gates remain ordinary classified events.

An import identity is **content SHA-256 + profile + importer version + analysis
JSON schema version**. Repeating that identity returns the existing artifact/run
IDs and stored counts with `Duplicate import: yes`; it does not append rows.
Byte-identical copies share the first artifact's path/name. A different profile
creates a separate run against that artifact; changed bytes create a new artifact.
Importer version changes must be intentional when storage semantics change.

One `BEGIN IMMEDIATE` transaction covers duplicate checking, artifact registration,
run creation, events, errors, and provenance notes. Any insertion failure rolls
back the entire import. Schema initialization is separate and can leave an empty
initialized database after an import failure. Report files are independent outputs:
if persistence fails, generated reports may remain, but the CLI exits with error
code 2 and does not claim database success.

### Provenance Without a Schema Change

Schema v1 has a single `source_line` and category per event, with no occurrence
count, category-list, or stack-line columns. The primary category uses the existing
analyzer taxonomy; actual lifecycle classifications become `event_type` values.
`component` uses the innermost available leading logger tag, or null.

The first source line stays directly queryable on each event. To preserve the
remaining information without changing schema, `test_runs.notes` contains a
versioned JSON import manifest:

- `importer`, `analysis_schema`, `original_verdict`, and `analyzed_path` identify
  the import and its interpretation.
- `events`, keyed by database event ID, retain all categories, severity, count,
  last line, and each occurrence's source line/timestamp. Warning traces are
  retained with their occurrence when present.
- `errors`, keyed by database error ID, retain full stack source-line references;
  stack text itself lives in `errors.stack_trace`.
- `transitions` retains all actual transition occurrences and BEFORE/AFTER
  fields/source references from the analyzer.
- `expected_rejection_lines` preserves the analyzer's expected-gate assessment.

This manifest is structured provenance, not a research conclusion or an AI brief.
It is intentionally not a generic repository abstraction. Future SQL queries
that need indexed repeated occurrences or multiple categories may justify an
explicit migration; none is implemented here. Memory usage with optional capture
scales with recognized occurrences and full traces, not just unique messages.

### Inspection and Validation

`database.inspect_db` opens SQLite in URI `mode=ro`, validates schema version,
and prints counts. It neither creates missing databases nor imports data.
`--latest-run` shows the latest inserted run, its profile/result, source, and counts.

```bash
python -m unittest discover -s tests -v
```

There are 26 original analyzer tests, 18 Phase 1 database tests, and 19 Phase 2
tests. The real-audit regression is optional because private logs are not copied
into the project. To include it, provide the saved audit path:

```bash
HYLAND_HEAT_AUDIT_LOG="/path/to/saved/audit/Latest.log" \
    python -m unittest discover -s tests -v
```

That test checks the attention verdict, expected gate, real errors, brief cap,
all retained original source references, and database provenance. Other Phase 2
tests use temporary controlled fixtures and cover SHA-256, deduplication,
rollback, complete/repeated stacks, optional CLI behavior, report-byte equivalence,
incompatible schemas, source changes, and read-only inspection.

## Research Database: Phase 3 (context builder)

`database/context_builder.py` is the first Phase 3 slice: a read-only tool —
usable directly as a CLI, and the same functions the MCP server wraps — that
renders a compact, character-budgeted Markdown packet from whatever the
database already holds, for handing to an LLM (or to yourself) instead of raw
logs or full JSON. It has two modes: **run-scoped** (evidence from one log
ingestion) and **entity-scoped** (everything linked to one subject).

### Run-scoped context

```bash
python -m database.context_builder --database data/hylandheat.db
python -m database.context_builder --database data/hylandheat.db --run 3
python -m database.context_builder --database data/hylandheat.db --max-chars 4000 --max-events 8
```

Without `--run`, it selects the highest test-run ID. The packet has five
sections: Errors and Notable Events are scoped to that one run (errors ranked
FATAL/EXCEPTION/ERROR then source line; events ranked by the same
`PRIMARY_CATEGORIES` priority order ingestion already uses, then source line,
capped at `--max-events` with the true total shown alongside it). Open
Unknowns, Findings, and Decisions are project-wide research state, not scoped
to one run: only `status='open'`/`'active'` rows are shown (ranked by
importance/confidence, newest first), each with its subject entity's name when
one is linked. Every section is capped at `--max-research-rows` and shows
`(none recorded yet)` while those tables are empty. If the assembled packet
would exceed `--max-chars`, later rows in whichever section hits the limit are
dropped and a truncation footer is appended; nothing is ever cut mid-line.

### Entity-scoped context

As findings/unknowns/decisions/relationships accumulate, a project-wide dump
stops being "minimal context" and starts being noise. `--entity-id`/
`--entity-name` switches to a second mode that scopes the packet to one
subject instead — not usable together with `--run`, since the two modes
answer different questions ("what happened in this run" vs. "what do we know
about this entity"):

```bash
python -m database.context_builder --database data/hylandheat.db --entity-name OfficerLee2
python -m database.context_builder --database data/hylandheat.db --entity-id 2 --max-research-rows 5
```

`--entity-name` resolves by exact case-insensitive entity name/
`canonical_name` match, failing clearly if ambiguous or unmatched, exactly
like the `record_*.py` CLIs' `--subject-name`. The packet has four sections —
Findings, Unknowns, Decisions, Relationships — each showing every row linked
to that entity as either subject (or, for relationships, as either source or
target), ranked the same way the run-scoped packet ranks them. The one
deliberate difference: **every status is shown, not just active/open.** In
the run-scoped, project-wide view, a superseded finding is noise you'd rather
not spend budget on; here, since scope is already narrowed to one subject, a
superseded finding or a reversed decision about *this specific entity* is
exactly the kind of history worth seeing. Same character budget and
truncation behavior as the run-scoped packet.

The context builder only queries; it never writes, infers a finding, or
discovers an entity.

### Entity extraction

`database/entity_extraction.py` catalogs recurring named subjects already
present in ingested event/error messages, so they exist as rows other tools
(and future manual research entries) can reference by ID instead of by
re-typed string.

```bash
python -m database.entity_extraction --database data/hylandheat.db
python -m database.entity_extraction --database data/hylandheat.db --run 3
```

It recognizes a value only when it is tagged by one of a fixed set of exact
identity keys observed in HylandHeat logs (`Name=`, `Source=`, `Root=`,
`Clone=`, `SourcePath=`, `ClonePath=`, `SourceObject=`, `CloneObject=`); a key
must appear as a whole word, so a camelCase merge like `IsActualRoot=` or
`SourceBakedGUID=` is deliberately not a match, and a path value like
`SourcePath=OfficerLee2/Avatar` is cut at the first `/`. Booleans, bare
numbers, and GUIDs never pass as names. Without `--run`, every event/error
ever ingested is scanned, since the catalog is meant to accumulate across
runs; `--run` scopes a scan to one run's evidence only. Matching entities are
inserted once under `entity_type='game_object'`, deduped case-insensitively
against `canonical_name` within that type only — a manually curated entity of
a different `entity_type` sharing the same name is left alone, and re-running
is idempotent (already-cataloged names are reported, not re-inserted).

This still creates no findings, unknowns, decisions, or relationships, and
recognizes nothing beyond that fixed key list — a differently named or
differently tagged identifier is left uncatalogued rather than guessed at.

### Recording findings, unknowns, decisions, and relationships

`database/record_finding.py`, `record_unknown.py`, `record_decision.py`, and
`record_relationship.py` are manual CLIs for the four record types the
project deliberately keeps as human judgment calls rather than something
inferred from evidence. All four share the same entity-resolution and
foreign-key validation logic, factored into `database/research_records.py`
so a behavior change only has to happen once. The first three each have
three subcommands (`add`, `list`, `update-status`); relationships have only
`add` and `list` — see [Relationships](#relationships) below for why.

#### Findings

```bash
python -m database.record_finding --database data/hylandheat.db add \
    --finding "OfficerLee2's copied SceneId maps back to OfficerLee, ruling out an independent clone." \
    --subject-name OfficerLee2 --confidence strong --source-artifact-id 1 --source-line 262
python -m database.record_finding --database data/hylandheat.db list
python -m database.record_finding --database data/hylandheat.db list --status active
python -m database.record_finding --database data/hylandheat.db update-status 1 superseded
```

`add` requires only `--finding`; `--confidence` defaults to `unknown` and
status always starts `active`, matching the schema's own cautious defaults.
`--subject-name` resolves to an existing entity by exact case-insensitive
name/`canonical_name` match (built by `entity_extraction.py` or inserted by
hand) and fails clearly if it matches zero or more than one entity, rather
than guessing; `--subject-entity-id` links directly by ID instead, and
`--subject-text` is an independent freeform label that can be combined with
either. `--source-artifact-id`/`--source-line`/`--test-run-id` are optional
provenance links, each validated to already exist before the row is written
(`--source-line` requires `--source-artifact-id`, matching the schema's own
CHECK constraint, but with a clear message instead of a raw SQLite error).
`list` joins the linked entity's name in and can filter by `--status`.
`update-status` moves a finding through the schema's `active` /
`superseded` / `disproven` lifecycle; the finding text itself is never
edited in place. Every value is exactly what the caller supplies — nothing
here derives a finding from stored evidence, and `context_builder.py` already
surfaces whatever this CLI records.

#### Unknowns

```bash
python -m database.record_unknown --database data/hylandheat.db add \
    --question "Does goon-clone reuse hold under paired same-instance evidence, or does population completeness only look like reuse?" \
    --importance high --required-evidence "Paired same-instance observation across a natural goon transition" \
    --related-feature "SWAT lifecycle"
python -m database.record_unknown --database data/hylandheat.db list
python -m database.record_unknown --database data/hylandheat.db list --status open --importance high
python -m database.record_unknown --database data/hylandheat.db update-status 1 resolved
```

`add` requires only `--question`; `--importance` defaults to `medium` and
status always starts `open`. Subject resolution works exactly like findings
above, except unknowns have no freeform `subject_text` column in the schema
— only `--subject-entity-id`/`--subject-name`. `--required-evidence` and
`--related-feature` are optional freeform notes. `list` can filter by
`--status` and/or `--importance` together. `update-status` moves an unknown
through `open` / `investigating` / `resolved` / `blocked`; `resolved_at` is
set automatically when the new status is `resolved` and cleared otherwise
(including when reopening a previously resolved question), so it always
reflects the current status rather than a history of when it was first
resolved.

#### Decisions

```bash
python -m database.record_decision --database data/hylandheat.db add \
    --topic "SWAT teardown" --decision "Keep F10 creation locked" \
    --reason "Lifecycle research incomplete; disposable SWAT clone identity not yet proven safe to tear down" \
    --subject-name OfficerLee2 --finding-id 1
python -m database.record_decision --database data/hylandheat.db list
python -m database.record_decision --database data/hylandheat.db list --status active
python -m database.record_decision --database data/hylandheat.db update-status 1 reversed
```

`add` requires `--topic`, `--decision`, and `--reason`; status always starts
`active`. Subject resolution again matches findings, minus `subject_text`.
`--finding-id` is an optional, explicit link recording that this decision
was informed by a specific finding — validated to already exist — not a
claim that the finding proves the decision correct; nothing here derives a
decision from a finding automatically. `update-status` moves a decision
through `active` / `superseded` / `reversed`; the decision text itself is
never edited in place.

#### Relationships

`database/record_relationship.py` follows the same shape, with two
differences the `relationships` table's own design forces: **both** a
source and a target entity are required (there is no freeform fallback at
all, since the table has no `subject_text`-equivalent column), and the
schema gives relationships **no status/lifecycle column** — a relationship
is recorded once and either holds or doesn't, so there is no
`update-status` subcommand here, only `add` and `list`.

```bash
python -m database.record_relationship --database data/hylandheat.db add \
    --relationship-type IS_CLONE_OF --source-name OfficerLee2 --target-name OfficerLee \
    --notes "Matched SceneId; see finding 1" --source-artifact-id 1
python -m database.record_relationship --database data/hylandheat.db list
python -m database.record_relationship --database data/hylandheat.db list --relationship-type IS_CLONE_OF
python -m database.record_relationship --database data/hylandheat.db list --entity-id 2
```

`--relationship-type` is any non-empty string (e.g. `IS_CLONE_OF`, `IS_A`,
`OWNS`) — the schema only requires it non-empty, not a fixed enum. Each side
takes `--source-entity-id`/`--source-name` or `--target-entity-id`/
`--target-name` (each pair mutually exclusive, and the CLI requires exactly
one from each pair — argparse itself enforces this, in addition to the same
check in `add_relationship()` for callers that skip the CLI, like the MCP
tool). `list --entity-id` shows every relationship where that entity id
appears as either the source or the target, useful for asking "what do we
know about this entity" without knowing directionality up front.

### MCP server

`database/mcp_server.py` exposes the database to a connected MCP client (an
LLM assistant such as Claude Desktop, Claude Code, or Codex) as a set of
tools, so that assistant can read and write research records directly
instead of a person running each CLI by hand. This is the project's one
feature needing a pip dependency:

```bash
.venv/bin/python -m pip install -r requirements-mcp.txt   # existing mcp==2.2.0 pin
.venv/bin/python -m database.mcp_server --database data/hylandheat.db
```

Point an MCP client's config at that command (stdio transport, the SDK's
default) to connect it. `python -m database.mcp_server` needs its working
directory set to this project root — it's a Python module using relative
imports, which requires the project root on `sys.path`, something `-m`
normally gets from the launching process's cwd. Most MCP client config
formats accept a `cwd` field, but **it does not actually work in Claude
Code's `.mcp.json`** ([confirmed broken, closed as "not planned"](https://github.com/anthropics/claude-code/issues/17565))
and Codex's `config.toml` has no `cwd` field for stdio servers at all — so
the portable fix that works everywhere is a shell wrapper that `cd`s before
exec'ing python, which is exactly what this project's own `.mcp.json` (for
Claude Code) does:

```json
{
  "mcpServers": {
    "hyland-heat-research-db": {
      "type": "stdio",
      "command": "bash",
      "args": ["-c", "cd '/absolute/path/to/HylandHeatLogAnalyzer' && exec .venv/bin/python -m database.mcp_server --database data/hylandheat.db"]
    }
  }
}
```

For Codex, the equivalent goes in `~/.codex/config.toml`:

```toml
[mcp_servers.hyland-heat-research-db]
command = "bash"
args = ["-c", "cd '/absolute/path/to/HylandHeatLogAnalyzer' && exec .venv/bin/python -m database.mcp_server --database data/hylandheat.db"]
```

Claude Code treats a project's `.mcp.json` as untrusted until you approve
it — expect a one-time prompt the first time you open this project after
adding it. Either client needs restarting (or a fresh session opened in
this project) to pick up a new/changed MCP config; it isn't hot-reloaded
into a session that's already running.

The database path is fixed once at server startup (`--database`, defaulting
to `data/hylandheat.db`) and is **never** a tool argument, so a connected
client cannot redirect writes to an arbitrary path; the target database is
initialized automatically if it does not yet exist, the same as ingestion's
own self-init behavior. Every tool is a thin wrapper around an already
validated module function — `add_finding`, `list_findings`,
`update_finding_status` (`record_finding.py`); `add_unknown`,
`list_unknowns`, `update_unknown_status` (`record_unknown.py`);
`add_decision`, `list_decisions`, `update_decision_status`
(`record_decision.py`); `add_relationship`, `list_relationships`
(`record_relationship.py`); `sync_entities`, `list_entities`
(`entity_extraction.py`); `build_context`, `build_entity_context`
(`context_builder.py`); `inspect_database` (`inspect_db.py`); and
`backup_database` (`backup.py`; see [Backup and restore](#backup-and-restore))
— so no new validation,
entity-resolution, or provenance-checking logic exists here, and nothing
here writes a finding/unknown/decision/relationship automatically from
evidence.
A validation error (e.g. an unresolvable `subject_name`, an unknown
`finding_id`) is caught and returned as the tool's own concise error text
(the same wording the CLI prints), rather than the SDK's generic "Error
executing tool X", so a connected assistant can see exactly what to correct.

`findings`, `unknowns`, `decisions`, and `relationships` all now have both a
CLI and MCP tools; only `entities` remains limited to
`entity_extraction.py`'s heuristic catalog — there is still no manual "add
an entity by hand" CLI. Every other CLI here only resolves an existing
entity by name/ID; none of them create one, so a relationship (or a
finding/unknown/decision subject) can only reference an entity the catalog
or a person has already added directly. Phase 3 still has no research-note
import, assembly/source scanning, patrol ingestion, embeddings, vector
search, or RAG. Phase 4B adds SQLite FTS5 full-text search over events/errors;
see [below](#phase-4b-full-text-search-fts5).

## Backup and restore

`data/*.db` is gitignored generated data, so on its own it has no git
history: unlike the source files, a corrupted or deleted `.db` has no commit
to recover from. `database/backup.py` closes that gap by writing the
database's full content as a plain-text SQL dump to `backups/hylandheat.sql`
— a file git *can* meaningfully track and diff — and by rebuilding a fresh
database from that dump.

```bash
python -m database.backup dump --database data/hylandheat.db --backup backups/hylandheat.sql
python -m database.backup restore --database data/restored.db --backup backups/hylandheat.sql
```

`dump` opens the source read-only (it never mutates it) and refuses to dump
a source that already fails its own `PRAGMA foreign_key_check`, rather than
faithfully backing up a corrupted database. `restore` only ever creates a
brand-new file — it refuses outright if the target path already exists,
never overwriting one. Python's `sqlite3.Connection.iterdump()` orders
tables alphabetically rather than by foreign-key dependency (`events`, which
has NOT NULL foreign keys into `source_artifacts`/`test_runs`, is dumped
before either of them), so restoring through a foreign-key-enforcing
connection would reject a perfectly valid dump partway through; `restore`
runs the dump script with foreign keys off for that reason, then runs its
own `PRAGMA foreign_key_check` immediately afterward and refuses (deleting
the partial file) if that check finds anything, before a caller can reach
the restored file through the normal, enforcing `connect_database`.

The CLI itself never touches git — running `dump` only rewrites
`backups/hylandheat.sql` on disk. Actually protecting the data still needs a
`git add backups/hylandheat.sql && git commit` afterward. Two things now
make that easy to keep up with instead of relying on remembering to do it:

### MCP tool

`mcp_server.py` exposes `backup_database()` alongside its other tools (see
[MCP server](#mcp-server)) — a connected assistant can trigger a dump on
request without a person running the CLI by hand. Like every other tool
here, it takes no path argument (it always writes to the default
`backups/hylandheat.sql`) and only writes the file; it still doesn't commit
anything, so ask for that as a separate, explicit step afterward.

### Pre-commit hook

`githooks/pre-commit` refreshes and stages `backups/hylandheat.sql`
automatically before every commit, so the tracked snapshot can never
silently drift out of sync with whatever `data/hylandheat.db` looks like at
commit time. `.git/hooks/` is never itself tracked by git, so a fresh clone
needs one one-time install step:

```bash
cp githooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

The hook is deliberately non-blocking: it skips silently if
`data/hylandheat.db` doesn't exist yet (nothing to back up), and if a dump
attempt fails (e.g. the live database fails its own foreign key check) it
prints a warning to stderr but still lets the commit through, rather than
blocking an otherwise-unrelated commit over a database problem. On success
it runs `dump` and `git add`s the refreshed file into the commit already in
progress — a standard, well-established hook pattern (the same one
auto-formatting pre-commit hooks use).

## Phase 4A: Evidence Provenance and Run Comparison

This phase adds explicit provenance and deterministic comparisons to the existing
SQLite store. No embeddings, vector store, LLM calls, server database, Redis,
LangChain, or ingestion automation is introduced. See
[verification and examples](docs/phase4a-verification.md) for the milestone results.

### Evidence model and migration

`database.evidence` exposes a generic `(record_type, record_id, target_type,
target_id)` API. Record types are `finding`, `unknown`, and `decision`; targets
are `run`, `event`, `error`, `entity`, and `relationship`. Singular names are
deliberate; aliases, arbitrary table names, and nonexistent IDs are rejected.
IDs must be positive SQLite integers. An attachment only stores a reference:
it never changes confidence, resolves a question, or proves a conclusion.
Entities and relationships may themselves contain interpretation; linking them
does not turn them into runtime observations.

Schema v2 adds **one table**, `evidence_links`, with its own ID and `created_at`.
It uses three nullable owner foreign keys and five nullable target foreign keys.
CHECK constraints require exactly one owner and exactly one target; all foreign
keys use `ON DELETE RESTRICT`. This keeps a generic application interface while
SQLite enforces real references, including during updates and after restore.
A unique expression index prevents duplicate pairs despite nullable columns;
indexes on each foreign key support record lookup and deletion checks.
Repeated attachment is idempotent and returns the existing link ID.

Upgrade explicitly, after saving the existing database:

```bash
python3 -m database.backup dump --database data/hylandheat.db --backup backups/before-v2.sql
python3 -m database.init_db --database data/hylandheat.db
python3 -m database.inspect_db --database data/hylandheat.db
```

`init_db` creates fresh v2 databases or applies the additive v1 -> v2 change in
one transaction. Existing table contents, primary keys, research confidence and
status, and legacy provenance columns are preserved. Only the schema version and
its `updated_at` change; reapplying v2 is idempotent. Failed migration rolls back.
Unknown versions and nonempty unversioned databases are rejected.

Existing readers/writers, comparison, and backup/restore continue to support v1.
Evidence commands require v2 and report the migration command when needed.
Read-only tools and server startup never migrate an existing database implicitly.
A restored v1 backup remains v1 until explicitly initialized. Legacy finding
artifact/run/line columns and `decisions.finding_id` remain independent: there is
no automatic backfill, transitive expansion, or synthetic attribution.

### Evidence CLI and MCP

Run from the project root; IDs below refer to this repository's live snapshot:

```bash
python3 -m database.evidence --database data/hylandheat.db attach finding 1 run 1
python3 -m database.evidence attach finding 1 event 341
python3 -m database.evidence attach finding 1 relationship 1
python3 -m database.evidence list finding 1
python3 -m database.evidence list finding 1 --format json --limit 20 --offset 0
# Other record types use the same interface, when those IDs exist:
python3 -m database.evidence attach unknown 1 event 346
python3 -m database.evidence attach decision 1 event 346
```

Listing is ordered by evidence-link ID, defaults to 20 links, allows 1–100, and
returns a total plus `next_offset` for pagination. `attach` also accepts
`--format json`. Invalid input returns exit code 2 with concise stderr output.
The reusable functions are `attach_evidence(database, record_type, record_id,
target_type, target_id)` and `list_evidence(database, record_type, record_id,
limit=20, offset=0)` (pagination arguments are keyword-only).

The MCP server adds three tools, with a fixed database path at startup:

| Tool | Arguments | Result |
| --- | --- | --- |
| `attach_evidence` | `record_type`, `record_id`, `target_type`, `target_id` | Link ID and explicit pair |
| `list_evidence` | `record_type`, `record_id`, optional `limit`, `offset` | `total`, `items`, `next_offset` |
| `compare_runs` | `run_a`, `run_b`, optional `limit` | Structured comparison described below |

These tools return JSON objects in MCP `structuredContent`, with JSON text for
clients consuming text content. Application validation errors return an object
with an `error` field, following the existing convention of exposing actionable
validation messages (older tools still return error text). No new tool accepts
a database path. Restart the client/server after updating to load the new tools.

### Deterministic comparison

```bash
python3 -m database.run_comparison 1 2 --database data/research.db
python3 -m database.run_comparison 1 2 --database data/research.db --format json --limit 5
python3 -m database.run_comparison 1 1 --database data/hylandheat.db
```

Both IDs must already exist. A and B are directional: added means present in B
and absent from A; removed means present in A and absent from B. Reusable Python:

```python
from database.run_comparison import compare_runs, format_comparison
result = compare_runs(1, 2, database="data/research.db", limit=5)
print(format_comparison(result))
```

The service reads one consistent SQLite snapshot and never reads raw files,
ingests anything, or writes research records. Matching rules are explicit:

- Metadata compares profile, stored result, start time, game/mod builds, source
  artifact ID/path/hash, known importer verdict, and non-manifest notes. Database
  insertion timestamps and the other import-manifest fields are excluded.
- Errors match exact `(severity, component, message, stack_trace)`. Error counts
  are stored row counts. Stack-only changes are therefore visible.
- Events match exact `(category, component, event_type, message)`. All stored
  categories participate; display order follows ingestion's `PRIMARY_CATEGORIES`.
  Repeated occurrences use positive integer counts in the recognized importer
  manifest (`analysis_schema=2`), otherwise one per stored row. Coverage reports
  how many event rows lack a usable repeat count. Counts group by category/type
  as well as full signature; no confidence or verdict is recalculated.
- IDs, source lines, and timestamp columns are excluded from error/event
  signature matching. Embedded message values are not normalized or guessed.
  `NULL` and empty text remain distinct. One representative row ID on each side
  lets the reader retrieve the full stored record.
- Entity presence means an unambiguous cataloged `game_object` name explicitly
  tagged in stored event/error messages using the existing identity-key extractor.
  Matching name/canonical_name is case-insensitive; unmatched/ambiguous names are
  excluded and counted in coverage. Bare mentions, other entity types, and
  research relationships do not establish run presence. This describes tagged
  evidence, **not actual spawning, destruction, or lifecycle identity**.

JSON contains metadata differences, total row/occurrence/entity counts, error and
event `added`/`removed`/`count_changes`, category/type count changes, entity
differences, and coverage. Every difference list has `total`, `items`, and
`omitted`. Default `--limit 8` applies independently to each list, with a hard
maximum of 100; all text previews are at most 240 characters. Full values are
compared before truncation and signature SHA-256 values distinguish previews
that look alike. Ordering has deterministic tie-breaks. `identical` reflects
the full comparison under these documented rules, even when display is capped;
it does not mean the raw logs or entire database rows are identical.

Example compact excerpt from controlled synthetic runs:

```text
Run comparison: #1 -> #2
error_rows: 1 -> 0 (-1)
Errors removed: [ERROR] Synthetic failure Name=Old
Entities added: New
Entities removed: Old
```

The live snapshot currently has only one real run. Comparing `1 1` reports no
differences, 350 event rows / 361 occurrences, one error, and 18 tagged entities.
Two-run differences are exercised with isolated synthetic fixtures; no second
real run is invented or added to the research database.

### Context and backup changes

Both context modes show explicit attached evidence directly under included
findings, unknowns, and decisions, for example:

```text
- [active, strong] OfficerLee2's copied SceneId maps back to OfficerLee ...
  Evidence (finding #1): Run #1; Event #341; Event #346; Relationship #1
```

At most six references per record are shown in attachment-ID order, with a
remaining-count marker pointing to `list_evidence`. Record text and its evidence
line are admitted to the character budget together; evidence is not silently
removed from an included record. No raw messages or transitive links expand here.
The `--max-chars` cap now also covers oversized headers/paths/descriptions and
very small positive budgets. A truncation marker is included whenever it fits;
invalid budgets and negative row limits are rejected. Existing ranking, status
filters, and scope remain unchanged.

SQL backups include the new table and indexes. Foreign-key checks reject
dangling evidence before dump and after restore. Dumps now use a consistent
read transaction and atomically replace the destination, preserving an older
backup on write/replace failure. A backup cannot overwrite its source database
or a hardlink alias. Restore still refuses existing targets, with exclusive
file creation also protecting against concurrent creation. The installed
pre-commit hook continues to refresh and stage the tracked SQL dump; its existing
non-blocking warning policy is unchanged.

## Phase 4B: Full-Text Search (FTS5)

Structured, deterministic retrieval (the Phase 3 context builder, Phase 4A's
comparison) already covers exact-field lookups by run/entity/status. It does
not help find message text you can't already name a filter for — "which
events mention 'null reference'" has no SQL shortcut without scanning every
message by hand. SQLite FTS5 closes exactly that gap: ranked search over
already-ingested event/error text. Still fully local, still no embeddings,
external services, or semantic ranking — FTS5's own `bm25()` is enough, per
this exact tradeoff as flagged at the end of Phase 4A (see below).

### Schema v3: the search index

`database/schema.sql` adds two external-content FTS5 virtual tables,
`events_fts` (`message`, `component`, `category`, `event_type`) and
`errors_fts` (`message`, `component`, `stack_trace`), each pointing at its own
table via `content=`/`content_rowid='id'`. **events/errors remain the only
source of truth**; the FTS5 tables hold nothing but a derived search index,
always discardable and rebuildable from them. `AFTER INSERT/UPDATE/DELETE`
triggers on events/errors keep the index live on every ordinary write.
`database.search.rebuild_search_index()` exposes FTS5's own `'rebuild'`
special command standalone, for repairing drift from any write that bypasses
those triggers (a raw `sqlite3` connection, for instance) — CLI `--rebuild`,
MCP `rebuild_search_index`. Migrating an older database (or a plain reapply)
runs that same rebuild unconditionally as schema.sql's last step, so the
index always matches current content afterward regardless of starting state —
a full nuke-and-rebuild rather than an incremental diff, and cheap at this
project's scale, so no separate drift-detection logic exists.

Upgrade explicitly, same as v2 (existing rows are always preserved):

```bash
python3 -m database.backup dump --database data/hylandheat.db --backup backups/before-v3.sql
python3 -m database.init_db --database data/hylandheat.db
python3 -m database.inspect_db --database data/hylandheat.db
```

`init_db` creates fresh v3 databases or applies the additive v1/v2 -> v3
change (`evidence_links` if still missing, plus the FTS5 index and triggers)
in one transaction, straight from whatever version the database was already
at — schema.sql is always the full current schema, not an incremental diff,
so one script application is enough regardless of starting version. Failed
migration rolls back; reapplying an already-v3 database is idempotent (the
index is simply rebuilt, not duplicated). Search commands require v3 and
report the migration command when needed, the same as evidence commands
require v2; read-only tools and server startup never migrate implicitly.

### Search CLI and MCP

```bash
python3 -m database.search "clone" --database data/hylandheat.db --type events --limit 3
python3 -m database.search "IOException" --database data/hylandheat.db --type errors --format json
python3 -m database.search --rebuild --database data/hylandheat.db
```

Results are ranked by FTS5's own `bm25()` (best match first) and bounded by
`--limit` (default 20, 1-100, same range as evidence listing); each item
reports its source table (`event`/`error`), row id, `test_run_id`,
`source_line`, the event category or error severity, component, a
highlighted snippet, and the raw bm25 score, so a match traces straight back
to the run and log line it came from. `--type` restricts to `events` or
`errors` (default: both — no keyword search prefix, just the plain query
text FTS5 already understands). The reusable functions are
`search(database, query, *, type=None, limit=20)` and
`rebuild_search_index(database)` (keyword-only after the positionals, like
the rest of this codebase's module functions).

Example against the live snapshot (356 events, 3 errors as of this writing):

```text
$ python3 -m database.search "clone" --database data/hylandheat.db --type events --limit 3
3 result(s) for "clone" (events only)
- [event #75] (run #1, line 263) [Hyland Heat] [HylandHeat] OFFICER >>CLONE<< QUEUE PROGRESS: Created=1/20, Latest=officerlee2
- [event #264] (run #1, line 490) [Hyland Heat] [HylandHeat] OFFICER >>CLONE<< QUEUE PROGRESS: Created=2/20, Latest=officerlee3
- [event #276] (run #1, line 502) [Hyland Heat] [HylandHeat] OFFICER >>CLONE<< QUEUE PROGRESS: Created=3/20, Latest=officerjackson2
More matches beyond --limit 3.
```

The MCP server adds two tools:

| Tool | Arguments | Result |
| --- | --- | --- |
| `search` | `query`, optional `type`, `limit` | Ranked, bounded results — same shape as the CLI's `--format json` |
| `rebuild_search_index` | none | Confirmation with events/errors indexed counts |

Both require schema v3 and follow the existing convention of returning
concrete, actionable validation errors (`Error: ...` for the CLI,
`{'error': ...}` in MCP `structuredContent`) rather than a generic failure.

### Backup and restore

`database.backup` excludes `events_fts`/`errors_fts` (and their FTS5 shadow
tables) from SQL dumps: `sqlite3.Connection.iterdump()` replays a virtual
table's shadow-table content by patching `sqlite_master` directly under
`PRAGMA writable_schema=ON` rather than issuing a real `CREATE VIRTUAL
TABLE`, and that patched entry is not visible to the very same connection
running the rest of the restore script — restoring it hits `no such table`
before reaching any of the real data. Rather than depend on that, the dump
carries only the authoritative events/errors rows (their sync triggers are
ordinary DDL and dump/restore normally, same as every other trigger and
index here); `restore_database` recreates the FTS5 index structurally
afterward — a plain `CREATE VIRTUAL TABLE`, reliable on its own — and
rebuilds it from the rows just restored, without ever touching
`schema_version`. That makes it a completion of the restore, not an implicit
migration: a restored v1/v2 backup is entirely unaffected by any of this and
stays v1/v2, with no FTS5 tables, exactly as before Phase 4B.

### Open threads and remaining Phase 4B candidates (not implemented)

Provenance is explicitly attached and has no removal/status-edit CLI yet.
There is no historical entity occurrence table: comparisons depend on the
current catalog and the existing tagged-name extractor. Output is bounded,
but comparison work and memory scale with stored rows and traces. No semantic
equivalence, automatic importance/confidence, or causal conclusions are inferred.

Full-text search (above) was the first Phase 4B slice. Remaining candidates
to evaluate separately, still not implemented:

- Git/source-code intelligence and commit ↔ test-run correlation.
- Manual entity creation and entity aliases with ambiguity handling.
- Safe, constrained automatic log ingestion with deduplication and explicit scope.
- Embeddings only after structured and full-text retrieval prove insufficient.

**Structured and deterministic retrieval first. Semantic retrieval only where
exact retrieval eventually proves insufficient.**
