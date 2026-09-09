"""MCP server exposing read/write access to the research database to a connected LLM client.

This is the project's one feature requiring a pip dependency (`mcp`); every
other module stays standard-library only. The database path is fixed once at
server startup (via --database, defaulting to data/hylandheat.db) and is
never accepted as a tool argument, so a connected client cannot redirect
writes to an arbitrary path. Every tool below wraps an existing, already
validated module function; no new business logic (validation, entity
resolution, provenance checks) is introduced here, and none of it writes a
finding/unknown/decision/relationship automatically from evidence.

Run with: python -m database.mcp_server [--database PATH]
"""

import argparse
import sqlite3

from mcp.server.mcpserver import MCPServer

from . import (backup, context_builder, entity_extraction, evidence, file_index, inspect_db, record_decision,
               record_experiment, record_finding, record_relationship, record_unknown, record_usage, run_comparison)
from . import research as research_module
from . import search as fts_search
from . import usage_report
from .db import database_exists, initialize_database, resolve_database_path

mcp = MCPServer('hyland-heat-research-db')
_database = None


def configure(database=None):
    """Fix the database this server's tools operate on; initializes it if missing."""
    global _database
    _database = resolve_database_path(database)
    if not database_exists(_database):
        initialize_database(_database)
    return _database


def _db():
    if _database is None:
        raise RuntimeError('Server not configured; call configure() or main() first.')
    return _database


def _safely(action):
    try:
        return action()
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return f'Error: {exc}'


@mcp.tool()
def add_finding(finding: str, confidence: str = 'unknown', subject_entity_id: int | None = None,
                subject_name: str | None = None, subject_text: str | None = None,
                source_artifact_id: int | None = None, source_line: int | None = None,
                test_run_id: int | None = None) -> str:
    """Record a new research finding (a deliberate human judgment call, never inferred from evidence).

    confidence defaults to 'unknown' and status always starts 'active', matching the schema's own
    cautious defaults. subject_name resolves to an existing cataloged entity by exact
    case-insensitive name/canonical_name match and fails clearly if it matches zero or more than
    one entity, rather than guessing; use subject_entity_id to link by ID instead. subject_text is
    an independent freeform label that can be combined with either. source_line requires
    source_artifact_id. Every id argument is validated to already exist before the row is written.
    """
    def action():
        finding_id = record_finding.add_finding(
            _db(), finding, confidence=confidence, subject_entity_id=subject_entity_id,
            subject_name=subject_name, subject_text=subject_text, source_artifact_id=source_artifact_id,
            source_line=source_line, test_run_id=test_run_id)
        return f'Recorded finding {finding_id}.'
    return _safely(action)


@mcp.tool()
def list_findings(status: str | None = None) -> str:
    """List recorded findings, optionally filtered by status (active/superseded/disproven)."""
    return _safely(lambda: '\n'.join(
        record_finding.format_finding_row(row) for row in record_finding.list_findings(_db(), status)
    ) or 'No findings recorded.')


@mcp.tool()
def update_finding_status(finding_id: int, status: str, superseded_by_finding_id: int | None = None) -> str:
    """Move a finding through its active/superseded/disproven lifecycle. Finding text is never edited in place.

    superseded_by_finding_id optionally records which finding replaced this one; only valid
    together with status='superseded'. The "why" belongs in the replacing finding's own text.
    """
    def action():
        record_finding.update_finding_status(
            _db(), finding_id, status, superseded_by_finding_id=superseded_by_finding_id)
        suffix = f' (superseded by {superseded_by_finding_id})' if superseded_by_finding_id else ''
        return f'Finding {finding_id} set to {status}{suffix}.'
    return _safely(action)


@mcp.tool()
def add_unknown(question: str, importance: str = 'medium', subject_entity_id: int | None = None,
                subject_name: str | None = None, required_evidence: str | None = None,
                related_feature: str | None = None) -> str:
    """Record a new open research question (a deliberate human judgment call, never inferred from
    evidence). importance defaults to 'medium' and status always starts 'open'. subject_name
    resolves to an existing cataloged entity by exact case-insensitive name/canonical_name match
    and fails clearly if it matches zero or more than one entity, rather than guessing; use
    subject_entity_id to link by ID instead.
    """
    def action():
        unknown_id = record_unknown.add_unknown(
            _db(), question, importance=importance, subject_entity_id=subject_entity_id,
            subject_name=subject_name, required_evidence=required_evidence, related_feature=related_feature)
        return f'Recorded unknown {unknown_id}.'
    return _safely(action)


@mcp.tool()
def list_unknowns(status: str | None = None, importance: str | None = None) -> str:
    """List recorded open questions, optionally filtered by status (open/investigating/resolved/
    blocked) and/or importance (low/medium/high/critical)."""
    return _safely(lambda: '\n'.join(
        record_unknown.format_unknown_row(row) for row in record_unknown.list_unknowns(_db(), status, importance)
    ) or 'No unknowns recorded.')


@mcp.tool()
def update_unknown_status(unknown_id: int, status: str) -> str:
    """Move an unknown through open/investigating/resolved/blocked. resolved_at is set
    automatically when status becomes 'resolved' and cleared otherwise (including on reopening).
    Question text is never edited in place."""
    def action():
        record_unknown.update_unknown_status(_db(), unknown_id, status)
        return f'Unknown {unknown_id} set to {status}.'
    return _safely(action)


@mcp.tool()
def add_decision(topic: str, decision: str, reason: str, subject_entity_id: int | None = None,
                 subject_name: str | None = None, finding_id: int | None = None) -> str:
    """Record a new research decision (a deliberate human judgment call, never inferred from
    evidence or from a finding). finding_id is an optional, explicit link recording that this
    decision was informed by a specific finding, not a claim that the finding proves it.
    subject_name resolves to an existing cataloged entity by exact case-insensitive name/
    canonical_name match and fails clearly if it matches zero or more than one entity, rather than
    guessing; use subject_entity_id to link by ID instead.
    """
    def action():
        decision_id = record_decision.add_decision(
            _db(), topic, decision, reason, subject_entity_id=subject_entity_id,
            subject_name=subject_name, finding_id=finding_id)
        return f'Recorded decision {decision_id}.'
    return _safely(action)


@mcp.tool()
def list_decisions(status: str | None = None) -> str:
    """List recorded decisions, optionally filtered by status (active/superseded/reversed)."""
    return _safely(lambda: '\n'.join(
        record_decision.format_decision_row(row) for row in record_decision.list_decisions(_db(), status)
    ) or 'No decisions recorded.')


@mcp.tool()
def update_decision_status(decision_id: int, status: str) -> str:
    """Move a decision through active/superseded/reversed. Decision text is never edited in place."""
    def action():
        record_decision.update_decision_status(_db(), decision_id, status)
        return f'Decision {decision_id} set to {status}.'
    return _safely(action)


@mcp.tool()
def sync_entities(run_id: int | None = None) -> str:
    """Catalog recurring named subjects tagged by a fixed set of identity keys (Name=, Source=,
    Root=, Clone=, SourcePath=, ClonePath=, SourceObject=, CloneObject=) in already-ingested
    event/error messages. Heuristic and idempotent; creates no findings, unknowns, or decisions.
    Without run_id, every event/error ever ingested is scanned.
    """
    return _safely(lambda: entity_extraction.format_summary(entity_extraction.sync_entities(_db(), run_id)))


@mcp.tool()
def list_entities(entity_type: str | None = None) -> str:
    """List cataloged entities, optionally filtered by entity_type (e.g. 'game_object')."""
    return _safely(lambda: entity_extraction.format_entities(entity_extraction.list_entities(_db(), entity_type)))


@mcp.tool()
def add_relationship(relationship_type: str, source_entity_id: int | None = None,
                     source_name: str | None = None, target_entity_id: int | None = None,
                     target_name: str | None = None, source_artifact_id: int | None = None,
                     test_run_id: int | None = None, notes: str | None = None) -> str:
    """Record a new directed, typed entity-to-entity relationship (a deliberate human judgment
    call, never inferred from evidence), e.g. relationship_type='IS_CLONE_OF'. Both a source and a
    target entity are required — each may be given as *_entity_id or resolved from *_name by exact
    case-insensitive entity name/canonical_name match, failing clearly if ambiguous or unmatched.
    Unlike findings/unknowns/decisions, relationships have no status/lifecycle column in the
    schema, so there is no update-status tool for this.
    """
    def action():
        relationship_id = record_relationship.add_relationship(
            _db(), relationship_type, source_entity_id=source_entity_id, source_name=source_name,
            target_entity_id=target_entity_id, target_name=target_name, source_artifact_id=source_artifact_id,
            test_run_id=test_run_id, notes=notes)
        return f'Recorded relationship {relationship_id}.'
    return _safely(action)


@mcp.tool()
def list_relationships(relationship_type: str | None = None, entity_id: int | None = None) -> str:
    """List recorded relationships, optionally filtered by exact relationship_type and/or one
    entity_id appearing as either the source or the target."""
    return _safely(lambda: '\n'.join(
        record_relationship.format_relationship_row(row)
        for row in record_relationship.list_relationships(_db(), relationship_type, entity_id)
    ) or 'No relationships recorded.')


@mcp.tool(structured_output=True)
def attach_evidence(record_type: str, record_id: int, target_type: str, target_id: int) -> dict[str, object]:
    """Explicitly attach evidence to a finding/unknown/decision; never changes confidence or status.

    target_type: run, event, error, entity, relationship. IDs must exist. Requires schema v2.
    Repeating the same pair returns the existing evidence-link ID.
    """
    try:
        return {'id': evidence.attach_evidence(_db(), record_type, record_id, target_type, target_id),
                'record_type': record_type, 'record_id': record_id,
                'target_type': target_type, 'target_id': target_id}
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def list_evidence(record_type: str, record_id: int, limit: int = 20, offset: int = 0) -> dict[str, object]:
    """List explicit attached evidence in attachment-ID order, with total and next_offset.

    record_type: finding, unknown, decision. limit is 1-100. Requires schema v2.
    Legacy finding artifact/run columns and decision.finding_id remain independent.
    """
    try:
        return evidence.list_evidence(_db(), record_type, record_id, limit=limit, offset=offset)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def compare_runs(run_a: int, run_b: int, limit: int = 8) -> dict[str, object]:
    """Return bounded structured differences from run A to B using exact stored fields.

    Each difference list contains total/items/omitted; limit is 1-100. Repeated event counts
    use the known ingestion manifest when available. Entity presence means unambiguous
    cataloged names explicitly tagged in stored messages, never inferred lifecycle behavior.
    """
    try:
        return run_comparison.compare_runs(run_a, run_b, _db(), limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool()
def build_context(run_id: int | None = None, max_chars: int = 6000, max_events: int = 12,
                   max_research_rows: int = 12) -> str:
    """Render the compact, character-budgeted Markdown research-context packet: errors and
    notable events for one test run (defaulting to the latest), plus project-wide open unknowns,
    active findings, and active decisions. Read-only; call this before recording a new finding to
    check for existing, possibly conflicting, research state.
    """
    return _safely(lambda: context_builder.build_context(_db(), run_id, max_chars, max_events, max_research_rows))


@mcp.tool()
def build_entity_context(entity_id: int | None = None, entity_name: str | None = None,
                         max_chars: int = 6000, max_research_rows: int = 12) -> str:
    """Render everything linked to one entity: its findings, unknowns, decisions, and
    relationships in either direction. Prefer this over build_context when the question is about
    a specific subject (e.g. 'what do we know about OfficerLee2') rather than one test run — it
    scopes the packet instead of showing project-wide state, so limited context goes further.
    Unlike build_context, every status is shown here (not just active/open), since scope is
    already narrowed to one subject: a superseded finding or a reversed decision about this
    entity is signal, not noise. entity_name resolves by exact case-insensitive entity name/
    canonical_name match and fails clearly if ambiguous or unmatched; exactly one of entity_id or
    entity_name is required.
    """
    return _safely(lambda: context_builder.build_entity_context(
        _db(), entity_id=entity_id, entity_name=entity_name, max_chars=max_chars,
        max_research_rows=max_research_rows))


@mcp.tool()
def inspect_database(latest_run: bool = False) -> str:
    """Read-only table-count overview of the research database, optionally with the latest test run's summary."""
    return _safely(lambda: inspect_db.inspect_database(_db(), latest_run))


@mcp.tool()
def backup_database() -> str:
    """Write a full SQL dump of the database to backups/hylandheat.sql, so the recorded research
    (not just the code) has git history: data/*.db is gitignored generated data with no history of
    its own. Read-only against the live database; refuses to back up a source that already fails
    its own foreign key check. This only writes the file on disk — it does not touch git. Ask for
    the refreshed dump to actually be committed afterward to protect this data.
    """
    def action():
        path = backup.dump_database(_db())
        return f'Wrote {path}. Ask for it to be committed to actually protect this data.'
    return _safely(action)


@mcp.tool(structured_output=True)
def search(query: str, type: str | None = None, limit: int = 20) -> dict[str, object]:
    """Ranked full-text search over events/errors/source documents (SQLite FTS5, bm25 ranking). Requires schema v4.

    type restricts to 'events', 'errors', or 'documents' (default: all three).
    limit is 1-100. Each item has source ('event'/'error'/'document'), id,
    either test_run_id/source_line (events/errors, tracing a match back to
    the run and log line it came from) or relative_path (documents -- ingested
    mod source or decompiled game-assembly files, see database.ingest_source),
    kind (event category or error severity, None for documents), component, a
    highlighted snippet, and the bm25 score (lower is a better match). No
    embeddings or semantic ranking; this is exact-term/prefix matching over
    stored text only.
    """
    try:
        return fts_search.search(_db(), query, type=type, limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool()
def rebuild_search_index() -> str:
    """Recompute the FTS5 search index from current events/errors/documents content. Requires schema v4.

    Triggers keep the index live on ordinary writes; use this to repair drift
    from a write that bypassed them, or after restoring from a SQL backup
    (which excludes the index -- see database.backup's docstring). Only
    rebuilds the *index*; source_documents' own content, if empty after a
    restore, can only come from re-running database.ingest_source.
    """
    def action():
        result = fts_search.rebuild_search_index(_db())
        return (f"Rebuilt search index: {result['events_indexed']} events, {result['errors_indexed']} errors, "
                f"{result['documents_indexed']} documents indexed.")
    return _safely(action)


@mcp.tool(structured_output=True)
def research(query: str, limit: int = 6) -> dict[str, object]:
    """Compact, honest, cross-artifact evidence packet for a symbol or short research
    question (e.g. "NPC.EnterBuilding"). Requires schema v5.

    Spans every artifact class this database holds: Hyland Heat mod-source
    call/patch sites, decompiled game-code declaration/implementation
    locations, related runtime-log events/errors, related findings/unknowns/
    decisions, and matching recorded experiments. Each section is
    independently bounded to `limit` (1-100 items) and an empty section is
    reported as empty rather than omitted -- absence of evidence is a fact
    here, never invented. Also returns a structural (never causal)
    interpretation and one rule-based suggested next research target. Prefer
    this over raw `search` when the question spans more than one artifact
    class, which is the usual case for "what explains this game behavior."
    """
    try:
        return research_module.research(_db(), query, limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def add_experiment(question: str, observed_result: str, symbols: str | None = None,
                   source_log: str | None = None, test_run_id: int | None = None) -> dict[str, object]:
    """Record an experiment: a deliberate observation ("does X change during Y")
    plus what was actually observed. Requires schema v5.

    Distinct from a finding (a conclusion, possibly drawn from several such
    observations) -- this is the record of having looked. symbols is a
    comma-separated list tying the experiment to specific code symbols, so
    `research`/`list_experiments` can surface it later. source_log accepts
    either a raw game log or a pre-summarized brief text file; either way
    it's registered as an ordinary, sha256-deduped source artifact rather
    than parsed. The server's fixed mod repo path is not implicitly used
    here -- pass the CLI's --mod-repo directly if you need the commit
    recorded (this MCP tool intentionally does not accept an arbitrary repo
    path, for the same reason no tool here accepts a database path).
    """
    try:
        symbol_list = tuple(symbols.split(',')) if symbols else ()
        experiment_id = record_experiment.add_experiment(
            _db(), question, observed_result, symbols=symbol_list,
            source_log=source_log, test_run_id=test_run_id)
        return {'id': experiment_id, 'question': question, 'observed_result': observed_result,
                'symbols': [s.strip() for s in symbol_list if s.strip()]}
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool()
def list_experiments(symbol: str | None = None) -> str:
    """List recorded experiments, optionally filtered to one exact symbol. Requires schema v5."""
    return _safely(lambda: '\n'.join(
        record_experiment.format_experiment_row(row) for row in record_experiment.list_experiments(_db(), symbol)
    ) or 'No experiments recorded.')


@mcp.tool(structured_output=True)
def add_usage(agent: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float | None = None,
             retrieval_mode: str | None = None, query_text: str | None = None,
             experiment_id: int | None = None, test_run_id: int | None = None,
             source_artifact_id: int | None = None, note: str | None = None) -> dict[str, object]:
    """Record one LLM call's real, API-reported token usage. Requires schema v7.

    input_tokens/output_tokens/cost_usd must be exactly what your own API
    response's usage field reported -- this tool never estimates or infers
    them. retrieval_mode ('compact_packet'/'raw_log'/'other') and query_text
    are optional context for later comparison against usage_report's
    estimate. At most one of experiment_id/test_run_id/source_artifact_id
    may be set, to say what the call was for; none is required.
    """
    try:
        usage_id = record_usage.add_usage(
            _db(), agent, model, input_tokens, output_tokens, cost_usd=cost_usd,
            retrieval_mode=retrieval_mode, query_text=query_text, experiment_id=experiment_id,
            test_run_id=test_run_id, source_artifact_id=source_artifact_id, note=note)
        return {'id': usage_id, 'agent': agent, 'model': model,
                'input_tokens': input_tokens, 'output_tokens': output_tokens}
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool()
def list_usage(agent: str | None = None, model: str | None = None,
               since: str | None = None, until: str | None = None) -> str:
    """List recorded agent_usage rows, optionally filtered by agent, model,
    and/or an ISO-timestamp occurred_at range. Requires schema v7."""
    return _safely(lambda: '\n'.join(
        record_usage.format_usage_row(row) for row in
        record_usage.list_usage(_db(), agent=agent, model=model, since=since, until=until)
    ) or 'No usage recorded.')


@mcp.tool(structured_output=True)
def usage_summary(agent: str | None = None, model: str | None = None,
                  since: str | None = None, until: str | None = None) -> dict[str, object]:
    """Summarize recorded agent_usage rows by agent and model, optionally filtered
    by agent, model, and/or an ISO-timestamp occurred_at range. Requires schema v7.
    Real recorded numbers only -- see database.usage_report's module docstring.
    """
    try:
        return usage_report.summarize(_db(), agent=agent, model=model, since=since, until=until)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def compare_usage_packet_vs_raw(query: str, limit: int = 6) -> dict[str, object]:
    """Estimate token cost of answering `query` from the compact evidence packet
    `research` builds vs. from the full raw content of everything it matched.
    Requires schema v7. Both numbers are a chars/4 estimate, not a real API
    count -- see database.usage_report's module docstring; this demonstrates
    the brief-first design's savings, it doesn't measure a real call.
    """
    try:
        return usage_report.compare_packet_vs_raw(_db(), query, limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def ingest_file_index(project_root: str, collection: str) -> dict[str, object]:
    """Scan `project_root` (project_scanner.scan_project(): built-in ignores
    plus its own .gitignore) and (re)build file_index for `collection`,
    replacing that collection's existing rows. Requires schema v8.

    Use the same collection name database.ingest_source used for this root
    (e.g. 'hylandheat-mod-source') so file_index_context can cross-reference
    source_documents; a project_root outside any existing ingestion is fine
    too -- file_index never requires source_documents to exist.
    """
    try:
        return file_index.ingest_directory(_db(), collection, project_root)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def file_index_tree(collection: str, path: str | None = None, limit: int = 50) -> dict[str, object]:
    """Recursively list a file_index collection (or one subtree rooted at `path`).
    Requires schema v8."""
    try:
        return file_index.tree(_db(), collection, path, limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def file_index_find(query: str, collection: str | None = None, extension: str | None = None,
                    limit: int = 50) -> dict[str, object]:
    """Search file_index names for `query` (substring, case-insensitive),
    optionally scoped to one collection and/or extension. Requires schema v8."""
    try:
        return file_index.find(_db(), query, collection=collection, extension=extension, limit=limit)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


@mcp.tool(structured_output=True)
def file_index_context(collection: str, path: str) -> dict[str, object]:
    """Structural context for one path -- parent, siblings, children -- plus
    the matching source_documents row when one shares this exact
    (collection, path), or an explicit null when none does. Requires schema v8.
    """
    try:
        return file_index.context(_db(), collection, path)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        return {'error': str(exc)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    args = parser.parse_args(argv)
    configure(args.database)
    mcp.run()


if __name__ == '__main__':
    main()
