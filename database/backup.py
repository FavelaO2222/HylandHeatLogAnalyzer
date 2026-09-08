"""Snapshot the research database as a portable SQL dump git can track, and
rebuild a database from that dump.

data/*.db is gitignored generated data with no git history of its own; the
dump this module writes to backups/ is what actually gets versioned, so the
recorded findings/entities/events are recoverable the same way the code
already is. Dumping never mutates the source; restoring only ever creates a
brand-new file, never overwrites an existing one.
"""

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from .db import PROJECT_ROOT, connect_database, resolve_database_path, validate_schema_version

DEFAULT_BACKUP_PATH = PROJECT_ROOT / 'backups' / 'hylandheat.sql'


def resolve_backup_path(backup=None):
    """Relative backup paths always belong to the analyzer project, not cwd."""
    path = Path(backup if backup is not None else DEFAULT_BACKUP_PATH).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def dump_database(database=None, backup=None):
    """Write a full SQL dump (schema + every row) of `database` to `backup`.

    Opens the source read-only; never mutates it. Refuses to dump a source
    that already fails its own foreign key check, rather than faithfully
    backing up a corrupted database.
    """
    backup_path = resolve_backup_path(backup)
    source_path = resolve_database_path(database)
    if backup_path == source_path or (backup_path.exists() and source_path.exists()
                                      and backup_path.samefile(source_path)):
        raise ValueError('Backup destination cannot be the source database or an alias of it.')
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        connection.execute('BEGIN')
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise ValueError(f'Source database fails its own foreign key check: {violations}')
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=backup_path.parent,
                                             prefix=backup_path.name + '.', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                for line in connection.iterdump():
                    handle.write(line + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, backup_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return backup_path


def restore_database(database, backup=None):
    """Rebuild a fresh database file from a SQL dump. Refuses to overwrite an existing file.

    Foreign keys stay off for the restore itself: iterdump() orders tables
    alphabetically rather than by dependency, so per-statement foreign key
    enforcement (as connect_database always applies) would reject a
    perfectly valid dump partway through. PRAGMA foreign_key_check
    confirms integrity immediately afterward, before any caller can reach
    the restored file through the normal, enforcing connect_database.
    """
    dump_path = resolve_backup_path(backup)
    if not dump_path.is_file():
        raise ValueError(f'No backup file at {dump_path}.')
    target = resolve_database_path(database)
    if target.exists():
        raise ValueError(f'{target} already exists; refusing to overwrite an existing database.')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the exists()/connect() overwrite race.
    with target.open('xb'):
        pass
    connection = sqlite3.connect(target)
    try:
        connection.executescript(dump_path.read_text(encoding='utf-8'))
        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise ValueError(f'Restored database failed its own foreign key check: {violations}')
        validate_schema_version(connection)
    except Exception:
        connection.close()
        target.unlink(missing_ok=True)
        raise
    connection.close()
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    dump_cmd = commands.add_parser('dump', help='Write a SQL dump of the database.')
    dump_cmd.add_argument('--database')
    dump_cmd.add_argument('--backup')

    restore_cmd = commands.add_parser('restore', help='Rebuild a new database file from a SQL dump.')
    restore_cmd.add_argument('--database', required=True, help='Path of the new database file to create.')
    restore_cmd.add_argument('--backup')

    args = parser.parse_args(argv)
    try:
        if args.command == 'dump':
            print(f'Wrote {dump_database(args.database, args.backup)}')
        else:
            print(f'Restored {restore_database(args.database, args.backup)}')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
