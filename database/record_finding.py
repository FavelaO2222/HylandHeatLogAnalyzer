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
from .research_records import require_row, resolve_entity_name

CONFIDENCE_LEVELS = ('confirmed', 'strong', 'tentative', 'unknown')
STATUSES = ('active', 'superseded', 'disproven')


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
                subject_entity_id = resolve_entity_name(connection, subject_name)
            require_row(connection, 'entities', subject_entity_id, 'entity')
            require_row(connection, 'source_artifacts', source_artifact_id, 'source artifact')
            require_row(connection, 'test_runs', test_run_id, 'test run')
            return connection.execute(
                '''INSERT INTO findings
                   (subject_entity_id, subject_text, finding, confidence, source_artifact_id, test_run_id, source_line)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (subject_entity_id, subject_text, finding, confidence, source_artifact_id, test_run_id,
                 source_line)).lastrowid


def list_findings(database, status=None):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=6)
        clause, params = ('WHERE f.status = ?', (status,)) if status else ('', ())
        return connection.execute(f'''
            SELECT f.id, f.finding, f.confidence, f.status, f.subject_text, e.name AS subject_entity_name,
                   f.test_run_id, f.source_artifact_id, f.source_line, f.superseded_by_finding_id
            FROM findings f LEFT JOIN entities e ON e.id = f.subject_entity_id
            {clause} ORDER BY f.id''', params).fetchall()


def update_finding_status(database, finding_id, status, *, superseded_by_finding_id=None):
    if status not in STATUSES:
        raise ValueError(f'status must be one of {STATUSES}.')
    if superseded_by_finding_id is not None and status != 'superseded':
        raise ValueError('--superseded-by requires status=superseded.')
    if superseded_by_finding_id == finding_id:
        raise ValueError('A finding cannot supersede itself.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=6)
        with connection:
            require_row(connection, 'findings', superseded_by_finding_id, 'finding')
            # Clearing back to NULL when status leaves 'superseded' keeps the
            # column consistent with its own CHECK constraint on every update,
            # not just ones that set it.
            value = superseded_by_finding_id if status == 'superseded' else None
            cursor = connection.execute(
                '''UPDATE findings SET status = ?, superseded_by_finding_id = ?,
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?''',
                (status, value, finding_id))
            if cursor.rowcount == 0:
                raise ValueError(f'No finding with id {finding_id}.')


def format_finding_row(row):
    subject = row['subject_entity_name'] or row['subject_text'] or '-'
    superseded_by = f" (superseded by #{row['superseded_by_finding_id']})" if row['superseded_by_finding_id'] else ''
    return f"[{row['id']}] ({row['status']}, {row['confidence']}) {subject}: {row['finding']}{superseded_by}"


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
    status_cmd.add_argument('--superseded-by', type=int, dest='superseded_by_finding_id',
                             help='Id of the finding that replaces this one; requires status=superseded.')

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
            update_finding_status(args.database, args.finding_id, args.status,
                                   superseded_by_finding_id=args.superseded_by_finding_id)
            suffix = f' (superseded by {args.superseded_by_finding_id})' if args.superseded_by_finding_id else ''
            print(f'Finding {args.finding_id} set to {args.status}{suffix}.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
