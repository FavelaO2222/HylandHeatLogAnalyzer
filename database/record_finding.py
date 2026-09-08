"""Manual CLI to record, list, and change the status of research findings.

Findings are deliberate human judgment calls, kept separate from
automatically ingested runtime evidence (events/errors) and from the
automatically cataloged entities in entity_extraction.py. Nothing here
infers a finding from evidence; every field is exactly what the caller
supplies, and confidence/status default to the schema's own cautious
defaults ('unknown' / 'active') unless the caller overrides them.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version

CONFIDENCE_LEVELS = ('confirmed', 'strong', 'tentative', 'unknown')
STATUSES = ('active', 'superseded', 'disproven')


def _resolve_subject_entity(connection, name):
    rows = connection.execute(
        'SELECT id, entity_type FROM entities WHERE lower(canonical_name) = lower(?) OR lower(name) = lower(?)',
        (name, name)).fetchall()
    if not rows:
        raise ValueError(f"No entity named '{name}'; use subject_entity_id or subject_text instead.")
    if len(rows) > 1:
        options = ', '.join(f"{row['id']} ({row['entity_type']})" for row in rows)
        raise ValueError(f"'{name}' matches more than one entity ({options}); use subject_entity_id to disambiguate.")
    return rows[0]['id']


def _require_row(connection, table, row_id, label):
    if row_id is not None and connection.execute(f'SELECT 1 FROM {table} WHERE id = ?', (row_id,)).fetchone() is None:
        raise ValueError(f'No {label} with id {row_id}.')


def add_finding(database, finding, *, confidence='unknown', subject_entity_id=None, subject_name=None,
                subject_text=None, source_artifact_id=None, source_line=None, test_run_id=None):
    if not finding or not finding.strip():
        raise ValueError('finding text must not be empty.')
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f'confidence must be one of {CONFIDENCE_LEVELS}.')
    if subject_entity_id is not None and subject_name is not None:
        raise ValueError('Specify only one of subject_entity_id or subject_name.')
    if source_line is not None and source_artifact_id is None:
        raise ValueError('source_line requires source_artifact_id.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if subject_name is not None:
                subject_entity_id = _resolve_subject_entity(connection, subject_name)
            _require_row(connection, 'entities', subject_entity_id, 'entity')
            _require_row(connection, 'source_artifacts', source_artifact_id, 'source artifact')
            _require_row(connection, 'test_runs', test_run_id, 'test run')
            return connection.execute(
                '''INSERT INTO findings
                   (subject_entity_id, subject_text, finding, confidence, source_artifact_id, test_run_id, source_line)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (subject_entity_id, subject_text, finding, confidence, source_artifact_id, test_run_id,
                 source_line)).lastrowid


def list_findings(database, status=None):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        clause, params = ('WHERE f.status = ?', (status,)) if status else ('', ())
        return connection.execute(f'''
            SELECT f.id, f.finding, f.confidence, f.status, f.subject_text, e.name AS subject_entity_name,
                   f.test_run_id, f.source_artifact_id, f.source_line
            FROM findings f LEFT JOIN entities e ON e.id = f.subject_entity_id
            {clause} ORDER BY f.id''', params).fetchall()


def update_finding_status(database, finding_id, status):
    if status not in STATUSES:
        raise ValueError(f'status must be one of {STATUSES}.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            cursor = connection.execute(
                "UPDATE findings SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, finding_id))
            if cursor.rowcount == 0:
                raise ValueError(f'No finding with id {finding_id}.')


def format_finding_row(row):
    subject = row['subject_entity_name'] or row['subject_text'] or '-'
    return f"[{row['id']}] ({row['status']}, {row['confidence']}) {subject}: {row['finding']}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record a new finding.')
    add.add_argument('--finding', required=True)
    add.add_argument('--confidence', choices=CONFIDENCE_LEVELS, default='unknown')
    subject = add.add_mutually_exclusive_group()
    subject.add_argument('--subject-entity-id', type=int)
    subject.add_argument('--subject-name', help='Resolved by exact case-insensitive entity name/canonical_name match.')
    add.add_argument('--subject-text', help='Freeform subject label; independent of --subject-entity-id/--subject-name.')
    add.add_argument('--source-artifact-id', type=int)
    add.add_argument('--source-line', type=int)
    add.add_argument('--test-run-id', type=int)

    listing = commands.add_parser('list', help='List recorded findings.')
    listing.add_argument('--status', choices=STATUSES)

    status_cmd = commands.add_parser('update-status', help="Change an existing finding's status.")
    status_cmd.add_argument('finding_id', type=int)
    status_cmd.add_argument('status', choices=STATUSES)

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            finding_id = add_finding(
                args.database, args.finding, confidence=args.confidence, subject_entity_id=args.subject_entity_id,
                subject_name=args.subject_name, subject_text=args.subject_text,
                source_artifact_id=args.source_artifact_id, source_line=args.source_line,
                test_run_id=args.test_run_id)
            print(f'Recorded finding {finding_id}.')
        elif args.command == 'list':
            rows = list_findings(args.database, args.status)
            print('\n'.join(format_finding_row(row) for row in rows) if rows else 'No findings recorded.')
        else:
            update_finding_status(args.database, args.finding_id, args.status)
            print(f'Finding {args.finding_id} set to {args.status}.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
