"""Explicit research provenance; attaching a reference never changes interpretation."""

import argparse
from contextlib import closing
import json
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row

# Only these fixed identifiers may be interpolated into SQL.
RECORDS = {'finding': ('findings', 'finding_id'), 'unknown': ('unknowns', 'unknown_id'),
           'decision': ('decisions', 'decision_id')}
TARGETS = {'run': ('test_runs', 'test_run_id'), 'event': ('events', 'event_id'),
           'error': ('errors', 'error_id'), 'entity': ('entities', 'entity_id'),
           'relationship': ('relationships', 'relationship_id')}


def positive_id(value, label):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError(f'{label} must be a positive SQLite integer.')


def bounded_limit(value, label='limit', maximum=100):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{label} must be an integer from 1 to {maximum}.')


def _reference(mapping, kind, row_id, label):
    if not isinstance(kind, str) or kind not in mapping:
        raise ValueError(f'{label}_type must be one of {tuple(mapping)}.')
    positive_id(row_id, f'{label}_id')
    return mapping[kind]


def attach_evidence(database, record_type, record_id, target_type, target_id):
    """Attach one validated pair atomically; repeated attachment returns the existing ID."""
    record_table, record_column = _reference(RECORDS, record_type, record_id, 'record')
    target_table, target_column = _reference(TARGETS, target_type, target_id, 'target')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=2)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            require_row(connection, record_table, record_id, record_type)
            require_row(connection, target_table, target_id, target_type)
            existing = connection.execute(
                f'SELECT id FROM evidence_links WHERE {record_column}=? AND {target_column}=?',
                (record_id, target_id)).fetchone()
            if existing:
                return existing['id']
            return connection.execute(
                f'INSERT INTO evidence_links ({record_column}, {target_column}) VALUES (?, ?)',
                (record_id, target_id)).lastrowid


def fetch_evidence(connection, record_type, record_id, *, limit=20, offset=0):
    """Connection-level reader, also used by the context builder; stable attachment-ID order."""
    table, column = _reference(RECORDS, record_type, record_id, 'record')
    bounded_limit(limit)
    if type(offset) is not int or not 0 <= offset <= 9223372036854775807:
        raise ValueError('offset must be a nonnegative SQLite integer.')
    require_row(connection, table, record_id, record_type)
    total = connection.execute(
        f'SELECT count(*) FROM evidence_links WHERE {column}=?', (record_id,)).fetchone()[0]
    rows = connection.execute(
        f'SELECT * FROM evidence_links WHERE {column}=? ORDER BY id LIMIT ? OFFSET ?',
        (record_id, limit, offset)).fetchall()
    items = []
    for row in rows:
        kind, target_id = next((kind, row[target_column]) for kind, (_, target_column)
                               in TARGETS.items() if row[target_column] is not None)
        items.append({'id': row['id'], 'target_type': kind, 'target_id': target_id,
                      'created_at': row['created_at']})
    next_offset = offset + len(items)
    return {'record_type': record_type, 'record_id': record_id, 'total': total,
            'items': items, 'offset': offset,
            'next_offset': next_offset if next_offset < total else None}


def list_evidence(database, record_type, record_id, *, limit=20, offset=0):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=2)
        connection.execute('BEGIN')
        return fetch_evidence(connection, record_type, record_id, limit=limit, offset=offset)


def evidence_label(item):
    return f"{item['target_type'].capitalize()} #{item['target_id']}"


def format_evidence(result):
    lines = [f"Evidence for {result['record_type']} #{result['record_id']} ({result['total']} links)"]
    lines.extend(f"- {evidence_label(item)} (link #{item['id']})" for item in result['items'])
    if not result['items']:
        lines.append('(none on this page)')
    if result['next_offset'] is not None:
        lines.append(f"More links: use --offset {result['next_offset']}.")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)
    attach = commands.add_parser('attach', help='Attach an existing evidence record.')
    listing = commands.add_parser('list', help='List explicit evidence links.')
    for command in (attach, listing):
        command.add_argument('record_type', choices=RECORDS)
        command.add_argument('record_id', type=int)
        command.add_argument('--format', choices=('text', 'json'), default='text')
    attach.add_argument('target_type', choices=TARGETS)
    attach.add_argument('target_id', type=int)
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == 'attach':
            link_id = attach_evidence(args.database, args.record_type, args.record_id,
                                      args.target_type, args.target_id)
            result = {'id': link_id, 'record_type': args.record_type, 'record_id': args.record_id,
                      'target_type': args.target_type, 'target_id': args.target_id}
            output = f'Evidence link #{link_id}: {args.record_type} #{args.record_id} -> {evidence_label(result)}.'
        else:
            result = list_evidence(args.database, args.record_type, args.record_id,
                                   limit=args.limit, offset=args.offset)
            output = format_evidence(result)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json' else output)
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
