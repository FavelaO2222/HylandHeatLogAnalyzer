"""Small read-only database inspection CLI; does not initialize or alter files."""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version

TABLES = ('source_artifacts', 'test_runs', 'events', 'errors', 'entities',
          'findings', 'unknowns', 'decisions', 'relationships')


def inspect_database(database=None, latest_run=False):
    with closing(connect_database(database, read_only=True)) as connection:
        version = validate_schema_version(connection)
        # Identifiers come only from this fixed tuple, never from CLI input.
        tables = TABLES + ('evidence_links',) if version >= 2 else TABLES
        counts = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}
        lines = [f'Database schema: {version}', '']
        lines.extend(f"{table.replace('_', ' ').capitalize()}: {count}" for table, count in counts.items())
        if latest_run:
            row = connection.execute('''SELECT t.id, t.profile, t.result, t.mod_build, a.path
                FROM test_runs t LEFT JOIN source_artifacts a ON a.id=t.source_artifact_id
                ORDER BY t.id DESC LIMIT 1''').fetchone()
            lines.extend(['', 'Latest Test Run'])
            if row is None:
                lines.append('No test runs.')
            else:
                lines.extend([f"ID: {row['id']}", f"Profile: {row['profile']}", f"Result: {row['result']}"])
                for table in ('events', 'errors'):
                    count = connection.execute(f'SELECT count(*) FROM {table} WHERE test_run_id=?', (row['id'],)).fetchone()[0]
                    lines.append(f'{table.capitalize()}: {count}')
                if row['mod_build'] is not None:
                    lines.append(f"Mod build: {row['mod_build']}")
                lines.append(f"Source: {row['path']}")
        return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--latest-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(inspect_database(args.database, args.latest_run))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
