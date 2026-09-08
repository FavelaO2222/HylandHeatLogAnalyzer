"""Manual CLI to record, list, and change the status of open research questions.

Unknowns are deliberate human judgment calls, kept separate from
automatically ingested runtime evidence (events/errors) and from the
automatically cataloged entities in entity_extraction.py. Nothing here
infers an unknown from evidence, and nothing here resolves one
automatically: an unknown does not become a confirmed finding on its own.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row, resolve_subject_entity

IMPORTANCE_LEVELS = ('low', 'medium', 'high', 'critical')
STATUSES = ('open', 'investigating', 'resolved', 'blocked')


def add_unknown(database, question, *, importance='medium', subject_entity_id=None, subject_name=None,
                required_evidence=None, related_feature=None):
    if not question or not question.strip():
        raise ValueError('question text must not be empty.')
    if importance not in IMPORTANCE_LEVELS:
        raise ValueError(f'importance must be one of {IMPORTANCE_LEVELS}.')
    if subject_entity_id is not None and subject_name is not None:
        raise ValueError('Specify only one of subject_entity_id or subject_name.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if subject_name is not None:
                subject_entity_id = resolve_subject_entity(connection, subject_name)
            require_row(connection, 'entities', subject_entity_id, 'entity')
            return connection.execute(
                '''INSERT INTO unknowns (subject_entity_id, question, importance, required_evidence, related_feature)
                   VALUES (?, ?, ?, ?, ?)''',
                (subject_entity_id, question, importance, required_evidence, related_feature)).lastrowid


def list_unknowns(database, status=None, importance=None):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        clauses, params = [], []
        if status:
            clauses.append('u.status = ?')
            params.append(status)
        if importance:
            clauses.append('u.importance = ?')
            params.append(importance)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        return connection.execute(f'''
            SELECT u.id, u.question, u.importance, u.status, u.required_evidence, u.related_feature,
                   e.name AS subject_entity_name
            FROM unknowns u LEFT JOIN entities e ON e.id = u.subject_entity_id
            {where} ORDER BY u.id''', params).fetchall()


def update_unknown_status(database, unknown_id, status):
    """Move an unknown through open/investigating/resolved/blocked.

    resolved_at is set to now() when the new status is 'resolved', and
    cleared otherwise (including when reopening a previously resolved
    unknown), so it always reflects the current status rather than history.
    """
    if status not in STATUSES:
        raise ValueError(f'status must be one of {STATUSES}.')
    resolved_at_expr = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')" if status == 'resolved' else 'NULL'
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            cursor = connection.execute(
                f"UPDATE unknowns SET status = ?, resolved_at = {resolved_at_expr}, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, unknown_id))
            if cursor.rowcount == 0:
                raise ValueError(f'No unknown with id {unknown_id}.')


def format_unknown_row(row):
    subject = row['subject_entity_name'] or '-'
    return f"[{row['id']}] ({row['status']}, {row['importance']}) {subject}: {row['question']}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record a new open question.')
    add.add_argument('--question', required=True)
    add.add_argument('--importance', choices=IMPORTANCE_LEVELS, default='medium')
    subject = add.add_mutually_exclusive_group()
    subject.add_argument('--subject-entity-id', type=int)
    subject.add_argument('--subject-name', help='Resolved by exact case-insensitive entity name/canonical_name match.')
    add.add_argument('--required-evidence', help='What would resolve this question.')
    add.add_argument('--related-feature', help='Freeform label for what this relates to.')

    listing = commands.add_parser('list', help='List recorded unknowns.')
    listing.add_argument('--status', choices=STATUSES)
    listing.add_argument('--importance', choices=IMPORTANCE_LEVELS)

    status_cmd = commands.add_parser('update-status', help="Change an existing unknown's status.")
    status_cmd.add_argument('unknown_id', type=int)
    status_cmd.add_argument('status', choices=STATUSES)

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            unknown_id = add_unknown(
                args.database, args.question, importance=args.importance, subject_entity_id=args.subject_entity_id,
                subject_name=args.subject_name, required_evidence=args.required_evidence,
                related_feature=args.related_feature)
            print(f'Recorded unknown {unknown_id}.')
        elif args.command == 'list':
            rows = list_unknowns(args.database, args.status, args.importance)
            print('\n'.join(format_unknown_row(row) for row in rows) if rows else 'No unknowns recorded.')
        else:
            update_unknown_status(args.database, args.unknown_id, args.status)
            print(f'Unknown {args.unknown_id} set to {args.status}.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
