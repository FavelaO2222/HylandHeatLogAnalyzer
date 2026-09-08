"""Catalog recurring named subjects already present in structured evidence.

This performs no judgment: it creates no findings, unknowns, decisions, or
relationships, and it never edits an existing entity. It only recognizes a
value already tagged by a known identity key (Name=, Source=, Root=, ...) in
an event/error message, and ensures a matching `entities` row exists. A name
with no recognized key is left uncatalogued rather than guessed; a value that
looks like a boolean, a number, or a GUID is never treated as a name.
"""

import argparse
from contextlib import closing
import re
import sqlite3
import sys

from .db import connect_database, validate_schema_version

ENTITY_TYPE = 'game_object'
# Exact identity keys observed tagging a game-object name in HylandHeat logs.
# A key must be a whole word (a preceding camelCase merge, e.g. "IsActualRoot=",
# "HasPoliceOfficer=", or "SourceBakedGUID=", is deliberately not a match).
IDENTITY_KEYS = ('Name', 'Source', 'Clone', 'Root', 'SourcePath', 'ClonePath', 'SourceObject', 'CloneObject')
_IDENTITY_VALUE = re.compile(
    r'\b(?:' + '|'.join(re.escape(key) for key in IDENTITY_KEYS) + r')=([A-Za-z][A-Za-z0-9_]*)')
_IGNORED_VALUES = {'true', 'false', 'none', 'null'}


def extract_names(message):
    """Return this message's candidate names, first-seen casing, case-insensitive de-duplication."""
    names, seen = [], set()
    for match in _IDENTITY_VALUE.finditer(message):
        name = match.group(1)
        lowered = name.lower()
        if lowered in _IGNORED_VALUES or lowered in seen:
            continue
        seen.add(lowered)
        names.append(name)
    return names


def sync_entities(database=None, run_id=None):
    """Insert entities.rows for names not already cataloged under ENTITY_TYPE.

    Without run_id, every event/error ever ingested is scanned; the catalog is
    meant to accumulate across runs. Existing entities of a different
    entity_type sharing the same name are left alone; this only dedupes
    within its own ENTITY_TYPE.
    """
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if run_id is not None and connection.execute(
                    'SELECT 1 FROM test_runs WHERE id = ?', (run_id,)).fetchone() is None:
                raise ValueError(f'No test run with id {run_id}.')
            clause, params = ('WHERE test_run_id = ?', (run_id,)) if run_id is not None else ('', ())
            messages = [row[0] for table in ('events', 'errors')
                        for row in connection.execute(f'SELECT message FROM {table} {clause}', params).fetchall()]
            candidates = {}
            for message in messages:
                for name in extract_names(message):
                    candidates.setdefault(name.lower(), name)
            existing = {row[0] for row in connection.execute(
                'SELECT canonical_name FROM entities WHERE entity_type = ?', (ENTITY_TYPE,)).fetchall()}
            inserted = []
            for canonical, name in candidates.items():
                if canonical in existing:
                    continue
                connection.execute(
                    'INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
                    (ENTITY_TYPE, name, canonical))
                inserted.append(name)
            return {'scanned_messages': len(messages), 'candidates': len(candidates),
                    'inserted': sorted(inserted), 'already_known': len(candidates) - len(inserted)}


def format_summary(result):
    inserted = ', '.join(result['inserted']) if result['inserted'] else 'none'
    return (f"Scanned messages: {result['scanned_messages']}\n"
            f"Candidate names: {result['candidates']}\n"
            f"Inserted: {len(result['inserted'])} ({inserted})\n"
            f"Already known: {result['already_known']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--run', type=int, help='Scope to one test run; defaults to every ingested run.')
    args = parser.parse_args(argv)
    try:
        print(format_summary(sync_entities(args.database, args.run)))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
