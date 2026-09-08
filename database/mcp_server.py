"""MCP server exposing read/write access to the research database to a connected LLM client.

This is the project's one feature requiring a pip dependency (`mcp`); every
other module stays standard-library only. The database path is fixed once at
server startup (via --database, defaulting to data/hylandheat.db) and is
never accepted as a tool argument, so a connected client cannot redirect
writes to an arbitrary path. Every tool below wraps an existing, already
validated module function; no new business logic (validation, entity
resolution, provenance checks) is introduced here, and none of it writes a
finding/unknown/decision automatically from evidence.

Run with: python -m database.mcp_server [--database PATH]
"""

import argparse
import sqlite3

from mcp.server.mcpserver import MCPServer

from . import context_builder, entity_extraction, inspect_db, record_finding
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
def update_finding_status(finding_id: int, status: str) -> str:
    """Move a finding through its active/superseded/disproven lifecycle. Finding text is never edited in place."""
    def action():
        record_finding.update_finding_status(_db(), finding_id, status)
        return f'Finding {finding_id} set to {status}.'
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
def build_context(run_id: int | None = None, max_chars: int = 6000, max_events: int = 12,
                   max_research_rows: int = 12) -> str:
    """Render the compact, character-budgeted Markdown research-context packet: errors and
    notable events for one test run (defaulting to the latest), plus project-wide open unknowns,
    active findings, and active decisions. Read-only; call this before recording a new finding to
    check for existing, possibly conflicting, research state.
    """
    return _safely(lambda: context_builder.build_context(_db(), run_id, max_chars, max_events, max_research_rows))


@mcp.tool()
def inspect_database(latest_run: bool = False) -> str:
    """Read-only table-count overview of the research database, optionally with the latest test run's summary."""
    return _safely(lambda: inspect_db.inspect_database(_db(), latest_run))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    args = parser.parse_args(argv)
    configure(args.database)
    mcp.run()


if __name__ == '__main__':
    main()
