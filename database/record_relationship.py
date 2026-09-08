"""Manual CLI to record and list directed, typed entity-to-entity relationships.

Relationships are deliberate human judgment calls, kept separate from
automatically ingested runtime evidence and from the automatically
cataloged entities in entity_extraction.py. Nothing here infers a
relationship from evidence or from another record. Unlike findings/
unknowns/decisions, the schema gives relationships no status/lifecycle
column: a relationship is recorded once and either holds or doesn't, so
there is no update-status command here.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row, resolve_entity_name


def add_relationship(database, relationship_type, *, source_entity_id=None, source_name=None,
                     target_entity_id=None, target_name=None, source_artifact_id=None,
                     test_run_id=None, notes=None):
    if not relationship_type or not relationship_type.strip():
        raise ValueError('relationship_type must not be empty.')
    if source_entity_id is not None and source_name is not None:
        raise ValueError('Specify only one of source_entity_id or source_name.')
    if source_entity_id is None and source_name is None:
        raise ValueError('A source entity is required: specify source_entity_id or source_name.')
    if target_entity_id is not None and target_name is not None:
        raise ValueError('Specify only one of target_entity_id or target_name.')
    if target_entity_id is None and target_name is None:
        raise ValueError('A target entity is required: specify target_entity_id or target_name.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if source_name is not None:
                source_entity_id = resolve_entity_name(connection, source_name)
            if target_name is not None:
                target_entity_id = resolve_entity_name(connection, target_name)
            require_row(connection, 'entities', source_entity_id, 'source entity')
            require_row(connection, 'entities', target_entity_id, 'target entity')
            require_row(connection, 'source_artifacts', source_artifact_id, 'source artifact')
            require_row(connection, 'test_runs', test_run_id, 'test run')
            return connection.execute(
                '''INSERT INTO relationships
                   (source_entity_id, relationship_type, target_entity_id, source_artifact_id, test_run_id, notes)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (source_entity_id, relationship_type, target_entity_id, source_artifact_id, test_run_id,
                 notes)).lastrowid


def list_relationships(database, relationship_type=None, entity_id=None):
    """List relationships, optionally filtered by exact type and/or one entity appearing as
    either the source or the target."""
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        clauses, params = [], []
        if relationship_type:
            clauses.append('r.relationship_type = ?')
            params.append(relationship_type)
        if entity_id is not None:
            clauses.append('(r.source_entity_id = ? OR r.target_entity_id = ?)')
            params.extend([entity_id, entity_id])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
        return connection.execute(f'''
            SELECT r.id, r.relationship_type, r.source_entity_id, r.target_entity_id, r.notes,
                   s.name AS source_name, t.name AS target_name
            FROM relationships r
            JOIN entities s ON s.id = r.source_entity_id
            JOIN entities t ON t.id = r.target_entity_id
            {where} ORDER BY r.id''', params).fetchall()


def format_relationship_row(row):
    notes = f" ({row['notes']})" if row['notes'] else ''
    return f"[{row['id']}] {row['source_name']} {row['relationship_type']} {row['target_name']}{notes}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record a new relationship.')
    add.add_argument('--relationship-type', required=True, help='e.g. IS_CLONE_OF, IS_A, OWNS.')
    source = add.add_mutually_exclusive_group(required=True)
    source.add_argument('--source-entity-id', type=int)
    source.add_argument('--source-name', help='Resolved by exact case-insensitive entity name/canonical_name match.')
    target = add.add_mutually_exclusive_group(required=True)
    target.add_argument('--target-entity-id', type=int)
    target.add_argument('--target-name', help='Resolved by exact case-insensitive entity name/canonical_name match.')
    add.add_argument('--source-artifact-id', type=int)
    add.add_argument('--test-run-id', type=int)
    add.add_argument('--notes')

    listing = commands.add_parser('list', help='List recorded relationships.')
    listing.add_argument('--relationship-type')
    listing.add_argument('--entity-id', type=int, help='Show relationships where this entity id is the source or target.')

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            relationship_id = add_relationship(
                args.database, args.relationship_type, source_entity_id=args.source_entity_id,
                source_name=args.source_name, target_entity_id=args.target_entity_id,
                target_name=args.target_name, source_artifact_id=args.source_artifact_id,
                test_run_id=args.test_run_id, notes=args.notes)
            print(f'Recorded relationship {relationship_id}.')
        else:
            rows = list_relationships(args.database, args.relationship_type, args.entity_id)
            print('\n'.join(format_relationship_row(row) for row in rows) if rows else 'No relationships recorded.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
