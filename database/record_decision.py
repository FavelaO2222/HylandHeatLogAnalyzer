"""Manual CLI to record, list, and change the status of research decisions.

Decisions are deliberate human judgment calls, kept separate from
automatically ingested runtime evidence (events/errors) and from the
automatically cataloged entities in entity_extraction.py. Nothing here
infers a decision from evidence or from a finding; finding_id is an
optional, explicit link recording that a decision was informed by a
specific finding, not a claim that the finding proves it.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row, resolve_entity_name

STATUSES = ('active', 'superseded', 'reversed')


def add_decision(database, topic, decision, reason, *, subject_entity_id=None, subject_name=None,
                 finding_id=None):
    for label, value in (('topic', topic), ('decision', decision), ('reason', reason)):
        if not value or not value.strip():
            raise ValueError(f'{label} text must not be empty.')
    if subject_entity_id is not None and subject_name is not None:
        raise ValueError('Specify only one of subject_entity_id or subject_name.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if subject_name is not None:
                subject_entity_id = resolve_entity_name(connection, subject_name)
            require_row(connection, 'entities', subject_entity_id, 'entity')
            require_row(connection, 'findings', finding_id, 'finding')
            return connection.execute(
                '''INSERT INTO decisions (subject_entity_id, finding_id, topic, decision, reason)
                   VALUES (?, ?, ?, ?, ?)''',
                (subject_entity_id, finding_id, topic, decision, reason)).lastrowid


def list_decisions(database, status=None):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        clause, params = ('WHERE d.status = ?', (status,)) if status else ('', ())
        return connection.execute(f'''
            SELECT d.id, d.topic, d.decision, d.reason, d.status, d.finding_id, e.name AS subject_entity_name
            FROM decisions d LEFT JOIN entities e ON e.id = d.subject_entity_id
            {clause} ORDER BY d.id''', params).fetchall()


def update_decision_status(database, decision_id, status):
    if status not in STATUSES:
        raise ValueError(f'status must be one of {STATUSES}.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            cursor = connection.execute(
                "UPDATE decisions SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                (status, decision_id))
            if cursor.rowcount == 0:
                raise ValueError(f'No decision with id {decision_id}.')


def format_decision_row(row):
    subject = row['subject_entity_name'] or '-'
    finding_ref = f" [finding {row['finding_id']}]" if row['finding_id'] is not None else ''
    return f"[{row['id']}] ({row['status']}) {subject}: {row['topic']} -> {row['decision']} ({row['reason']}){finding_ref}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record a new decision.')
    add.add_argument('--topic', required=True)
    add.add_argument('--decision', required=True)
    add.add_argument('--reason', required=True)
    subject = add.add_mutually_exclusive_group()
    subject.add_argument('--subject-entity-id', type=int)
    subject.add_argument('--subject-name', help='Resolved by exact case-insensitive entity name/canonical_name match.')
    add.add_argument('--finding-id', type=int, help='Optional link to the finding that informed this decision.')

    listing = commands.add_parser('list', help='List recorded decisions.')
    listing.add_argument('--status', choices=STATUSES)

    status_cmd = commands.add_parser('update-status', help="Change an existing decision's status.")
    status_cmd.add_argument('decision_id', type=int)
    status_cmd.add_argument('status', choices=STATUSES)

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            decision_id = add_decision(
                args.database, args.topic, args.decision, args.reason, subject_entity_id=args.subject_entity_id,
                subject_name=args.subject_name, finding_id=args.finding_id)
            print(f'Recorded decision {decision_id}.')
        elif args.command == 'list':
            rows = list_decisions(args.database, args.status)
            print('\n'.join(format_decision_row(row) for row in rows) if rows else 'No decisions recorded.')
        else:
            update_decision_status(args.database, args.decision_id, args.status)
            print(f'Decision {args.decision_id} set to {args.status}.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
