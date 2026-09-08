"""Full-text search over ingested events, errors, and source documents via SQLite FTS5.

Schema v3 added external-content FTS5 virtual tables events_fts/errors_fts,
indexing message plus the other descriptive text columns (component/category/
event_type for events; component/stack_trace for errors). Schema v4 adds
source_documents_fts the same way, over source_documents (see
database/ingest_source.py) -- one row per ingested mod-source or decompiled-
game-assembly file, indexed as a whole document rather than parsed into
members (see that module's docstring for why). In every case the underlying
table remains the sole source of truth; the FTS5 tables only ever hold a
derived search index, kept live by AFTER INSERT/UPDATE/DELETE triggers
defined in schema.sql. rebuild_search_index() exposes FTS5's own 'rebuild'
command for the cases triggers cannot cover: repairing drift from a write
that bypassed them (e.g. a raw sqlite3 connection), or restoring the index
after database.backup.restore_database (which excludes it from SQL dumps --
see that module's docstring for why).

Ranking is FTS5's built-in bm25(); no embeddings or external services.
"""

import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys

from .db import SCHEMA_PATH, connect_database, validate_schema_version
from .evidence import bounded_limit

SOURCES = ('events', 'errors', 'documents')
# A dot between word characters is FTS5 query syntax (a token separator, not
# a searchable character), but callers routinely search for dotted C# symbols
# like "NPC.EnterBuilding" meaning it literally. Splitting it into two tokens
# ("NPC EnterBuilding") searches for both words rather than erroring out, and
# leaves everything else -- quoted phrases, *, AND/OR/NOT -- untouched.
DOTTED_SYMBOL_SEPARATOR = re.compile(r'(?<=\w)\.(?=\w)')
SNIPPET_PREFIX, SNIPPET_SUFFIX, SNIPPET_ELLIPSIS, SNIPPET_TOKENS = '>>', '<<', ' … ', 12

_EVENT_QUERY = '''
    SELECT 'event' AS source, e.id AS id, e.test_run_id AS test_run_id, e.source_line AS source_line,
           e.category AS kind, e.component AS component, NULL AS relative_path,
           snippet(events_fts, 0, ?, ?, ?, ?) AS snippet, bm25(events_fts) AS score
    FROM events_fts JOIN events e ON e.id = events_fts.rowid
    WHERE events_fts MATCH ?'''
_ERROR_QUERY = '''
    SELECT 'error' AS source, r.id AS id, r.test_run_id AS test_run_id, r.source_line AS source_line,
           r.severity AS kind, r.component AS component, NULL AS relative_path,
           snippet(errors_fts, 0, ?, ?, ?, ?) AS snippet, bm25(errors_fts) AS score
    FROM errors_fts JOIN errors r ON r.id = errors_fts.rowid
    WHERE errors_fts MATCH ?'''
_DOCUMENT_QUERY = '''
    SELECT 'document' AS source, d.id AS id, NULL AS test_run_id, NULL AS source_line,
           NULL AS kind, NULL AS component, d.relative_path AS relative_path,
           snippet(source_documents_fts, 1, ?, ?, ?, ?) AS snippet, bm25(source_documents_fts) AS score
    FROM source_documents_fts JOIN source_documents d ON d.id = source_documents_fts.rowid
    WHERE source_documents_fts MATCH ?'''
_BRANCHES = {'events': _EVENT_QUERY, 'errors': _ERROR_QUERY, 'documents': _DOCUMENT_QUERY}


def _branches(type_):
    if type_ is not None and type_ not in SOURCES:
        raise ValueError(f'type must be one of {SOURCES}.')
    return [_BRANCHES[name] for name in SOURCES if type_ in (None, name)]


def search(database, query, *, type=None, limit=20):
    """Ranked, bounded full-text search over events/errors/documents. Requires schema v4.

    type restricts to 'events', 'errors', or 'documents'; omitted searches all
    three. Results are ordered by bm25() relevance (best match first) and
    bounded to limit (1-100). Each item carries its source table and row id,
    plus either test_run_id/source_line (events/errors, tracing a match back
    to the run and log line it came from) or relative_path (documents).
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must not be empty.')
    bounded_limit(limit)
    branches = _branches(type)
    fts_query = DOTTED_SYMBOL_SEPARATOR.sub(' ', query)
    sql = ' UNION ALL '.join(branches) + ' ORDER BY score LIMIT ?'
    params = []
    for _ in branches:
        params.extend((SNIPPET_PREFIX, SNIPPET_SUFFIX, SNIPPET_ELLIPSIS, SNIPPET_TOKENS, fts_query))
    params.append(limit + 1)
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=4)
        try:
            rows = connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f'Invalid search query syntax: {exc}') from exc
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = [{'source': row['source'], 'id': row['id'], 'test_run_id': row['test_run_id'],
              'source_line': row['source_line'], 'kind': row['kind'], 'component': row['component'],
              'relative_path': row['relative_path'], 'snippet': row['snippet'], 'score': row['score']}
             for row in rows]
    return {'query': query, 'type': type, 'limit': limit, 'truncated': truncated, 'items': items}


def rebuild_search_index(database):
    """Recompute events_fts/errors_fts/source_documents_fts from current content. Requires schema v4.

    Uses FTS5's own 'rebuild' special command: a full nuke-and-rebuild of the
    index from the external content table, not an incremental repair. Safe to
    call at any time; idempotent. Note this rebuilds the *index* only --
    source_documents' own content can only come from re-running
    database.ingest_source (see that module and database.backup's docstring).
    """
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=4)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
            connection.execute("INSERT INTO errors_fts(errors_fts) VALUES('rebuild')")
            connection.execute("INSERT INTO source_documents_fts(source_documents_fts) VALUES('rebuild')")
            events_indexed = connection.execute('SELECT count(*) FROM events_fts').fetchone()[0]
            errors_indexed = connection.execute('SELECT count(*) FROM errors_fts').fetchone()[0]
            documents_indexed = connection.execute('SELECT count(*) FROM source_documents_fts').fetchone()[0]
    return {'events_indexed': events_indexed, 'errors_indexed': errors_indexed,
            'documents_indexed': documents_indexed}


_INDEX_MARKER_START = '-- FTS5 search indexes begin --'
_INDEX_MARKER_END = '-- FTS5 search indexes end --'


def _index_ddl():
    """The exact events_fts/errors_fts/trigger/rebuild block from schema.sql,
    sliced out by its marker comments so it stays byte-identical to the
    authoritative schema instead of being hand-duplicated and drifting."""
    text = SCHEMA_PATH.read_text(encoding='utf-8')
    start = text.index(_INDEX_MARKER_START)
    end = text.index(_INDEX_MARKER_END, start) + len(_INDEX_MARKER_END)
    return text[start:end]


def recreate_index(connection):
    """(Re)create events_fts/errors_fts/source_documents_fts and their sync
    triggers on `connection`, then rebuild each index from current content.

    Used by database.backup.restore_database to restore the search index a
    dump necessarily excludes, without touching schema_version or any other
    table. All statements are IF NOT EXISTS, so this is safe to call even if
    the tables already exist. source_documents itself is also excluded from
    dumps (see database.backup), so this leaves it present but empty after a
    restore -- 'rebuild' on an empty content table correctly yields an empty
    index; restoring actual content means re-running database.ingest_source.
    """
    connection.executescript(_index_ddl())


def format_results(result):
    header = f"{len(result['items'])} result(s) for \"{result['query']}\""
    if result['type']:
        header += f" ({result['type']} only)"
    lines = [header]
    for item in result['items']:
        if item['source'] == 'document':
            where = item['relative_path']
        else:
            run = f"run #{item['test_run_id']}" if item['test_run_id'] is not None else 'no run'
            line = f", line {item['source_line']}" if item['source_line'] is not None else ''
            where = f'{run}{line}'
        lines.append(f"- [{item['source']} #{item['id']}] ({where}) {item['snippet']}")
    if not result['items']:
        lines.append('(no matches)')
    if result['truncated']:
        lines.append(f"More matches beyond --limit {result['limit']}.")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', nargs='?', help='Full-text search query (required unless --rebuild).')
    parser.add_argument('--type', choices=SOURCES, help='Restrict to events, errors, or documents.')
    parser.add_argument('--limit', type=int, default=20, help='Max results returned (1-100).')
    parser.add_argument('--database')
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    parser.add_argument('--rebuild', action='store_true',
                        help='Rebuild the FTS5 search index from events/errors/documents and exit.')
    args = parser.parse_args(argv)
    try:
        if args.rebuild:
            if args.query:
                raise ValueError('Specify either a query or --rebuild, not both.')
            result = rebuild_search_index(args.database)
            output = (json.dumps(result, sort_keys=True) if args.format == 'json' else
                      f"Rebuilt search index: {result['events_indexed']} events, "
                      f"{result['errors_indexed']} errors, {result['documents_indexed']} documents indexed.")
        else:
            if not args.query:
                raise ValueError('query is required unless --rebuild is given.')
            result = search(args.database, args.query, type=args.type, limit=args.limit)
            output = json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json' \
                else format_results(result)
        print(output)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
