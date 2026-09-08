"""Snapshot the research database as a portable SQL dump git can track, and
rebuild a database from that dump.

data/*.db is gitignored generated data with no git history of its own; the
dump this module writes to backups/ is what actually gets versioned, so the
recorded findings/entities/events are recoverable the same way the code
already is. Dumping never mutates the source; restoring only ever creates a
brand-new file, never overwrites an existing one.

Two kinds of table are deliberately excluded from the dump, for two
different reasons:

- Schema v3's events_fts/errors_fts and v4's source_documents_fts (see
  schema.sql and database.search) -- every FTS5 virtual table and its
  shadow tables. sqlite3.Connection.iterdump() replays a virtual table's
  shadow-table content by patching sqlite_master directly under `PRAGMA
  writable_schema=ON` rather than issuing a real `CREATE VIRTUAL TABLE`,
  and that patched entry is not visible to the very same connection
  running the rest of the script -- a restore attempts an INSERT into a
  table SQLite does not yet consider to exist and fails outright. This is
  a technical limitation, not a choice: restore recreates the FTS5 index
  structurally (a real, ordinary `CREATE VIRTUAL TABLE`, which works fine
  on its own) and rebuilds it from whatever rows were actually restored,
  via database.search.recreate_index.
- Schema v4's source_documents (see database.ingest_source) -- excluded
  unconditionally on a *different*, deliberate policy: it holds full
  ingested file content (mod source or decompiled game-assembly output),
  which can be large and, for decompiled game code, copyrighted. It must
  never land in this SQL dump, which this project commits to a public git
  repo. Restoring its content means re-running ingest_source against the
  original files, not something this module can rebuild on its own.
"""

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from .db import PROJECT_ROOT, connect_database, resolve_database_path, validate_schema_version
from .search import recreate_index

DEFAULT_BACKUP_PATH = PROJECT_ROOT / 'backups' / 'hylandheat.sql'
_FTS5_SHADOW_SUFFIXES = ('_data', '_idx', '_docsize', '_config', '_content')
_ALWAYS_EXCLUDED_TABLES = {'source_documents'}
_DUMP_STATEMENT_TABLE = re.compile(
    r'''^(?:CREATE\s+TABLE|INSERT\s+INTO)\s+["']?([A-Za-z_][A-Za-z0-9_]*)["']?[\s(]'''
    r'''|^CREATE\s+(?:UNIQUE\s+)?INDEX\s+["']?[A-Za-z_][A-Za-z0-9_]*["']?\s+ON\s+["']?([A-Za-z_][A-Za-z0-9_]*)["']?'''
    r'''|^CREATE\s+TRIGGER\s+["']?[A-Za-z_][A-Za-z0-9_]*["']?\s+(?:AFTER|BEFORE|INSTEAD\s+OF)\s+\w+\s+ON\s+'''
    r'''["']?([A-Za-z_][A-Za-z0-9_]*)["']?''')


def _excluded_table_names(connection):
    """Every FTS5 virtual table plus its shadow tables (see module docstring),
    plus source_documents (excluded on a separate, deliberate policy)."""
    virtual = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")]
    return (_ALWAYS_EXCLUDED_TABLES
            | {name for base in virtual for name in (base, *(base + suffix for suffix in _FTS5_SHADOW_SUFFIXES))})


def _dump_line_excluded(line, excluded_tables):
    if line in ('PRAGMA writable_schema=ON;', 'PRAGMA writable_schema=OFF;'):
        return True
    if line.startswith('INSERT INTO sqlite_master('):
        return True
    match = _DUMP_STATEMENT_TABLE.match(line)
    if match is None:
        return False
    # Group 1: CREATE TABLE/INSERT INTO target. Group 2: the table a CREATE
    # INDEX is ON. Group 3: the table a CREATE TRIGGER is ON. An index or
    # trigger on an excluded table must be excluded too, or the dump would
    # reference a table that was never created.
    return (match.group(1) or match.group(2) or match.group(3)) in excluded_tables


def _filter_dump_lines(lines, excluded_tables):
    """Drop every dump entry (each already a complete statement, multi-line
    body and all -- iterdump() does not split one statement across entries)
    that touches an excluded table: CREATE TABLE/INSERT INTO, a CREATE INDEX
    ON it, or a CREATE TRIGGER ON it."""
    return (line for line in lines if not _dump_line_excluded(line, excluded_tables))


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
        excluded = _excluded_table_names(connection)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=backup_path.parent,
                                             prefix=backup_path.name + '.', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                for line in _filter_dump_lines(connection.iterdump(), excluded):
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
        version = validate_schema_version(connection)
        if version >= 3:
            # The dump excluded the FTS5 tables (and, at v4+, source_documents
            # itself -- see module docstring); recreate the FTS5 structures
            # and rebuild each index from whatever rows were actually
            # restored above (source_documents stays empty, since its content
            # was never in the dump -- re-run ingest_source to repopulate
            # it). Only ever adds structures back at the database's own
            # already-restored version -- never touches schema_version, so
            # this is not an implicit migration.
            recreate_index(connection)
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
