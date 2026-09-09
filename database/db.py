"""Thin connection and initialization helpers for the independent research store."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Optional, Union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().with_name('schema.sql')
SCHEMA_VERSION = 8
SUPPORTED_VERSIONS = (1, 2, 3, 4, 5, 6, 7, 8)
DatabasePath = Optional[Union[str, Path]]


def resolve_database_path(database: DatabasePath = None) -> Path:
    """Relative database paths always belong to the analyzer project, not cwd."""
    path = Path(database if database is not None else 'data/hylandheat.db').expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def database_exists(database: DatabasePath = None) -> bool:
    """Check for a file without creating anything; does not validate its schema."""
    return resolve_database_path(database).is_file()


def connect_database(database: DatabasePath = None, *, create: bool = False,
                     read_only: bool = False) -> sqlite3.Connection:
    """Open an existing database; callers own commit/rollback and close.

    Use initialize_database to create the schema first; create=True only permits
    creating an empty SQLite file. The connection context manager
    commits or rolls back transactions but does NOT close the connection.
    """
    path = resolve_database_path(database)
    if create and read_only:
        raise ValueError('Cannot create a database through a read-only connection.')
    mode = 'ro' if read_only else 'rwc' if create else 'rw'
    connection = sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, isolation_level='DEFERRED')
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise RuntimeError('SQLite foreign key enforcement could not be enabled.')
    except Exception:
        connection.close()
        raise
    return connection


def _check_version(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    if not tables:
        return
    if 'schema_metadata' not in tables:
        raise ValueError('Refusing to initialize a nonempty, unversioned database.')
    versions = connection.execute('SELECT id, schema_version FROM schema_metadata').fetchall()
    if len(versions) != 1 or versions[0][0] != 1 or versions[0][1] not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported or invalid schema version; a migration is required.')


def _pending_column_additions(connection: sqlite3.Connection, table: str, columns: dict) -> str:
    """SQL text adding whichever of `columns` ({name: type}) `table` doesn't have yet, or ''.

    SQLite has no idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so a
    migration that adds a column to a table an *earlier* schema version
    already created and populated (unlike every prior migration here, which
    only ever added whole new tables) needs this explicit check. Returned as
    text to be prepended to the rest of the migration script and executed in
    the very same transaction (rather than run separately beforehand), so a
    later failure in that script rolls the column addition back too -- DDL
    and version insertion still roll back together. A completely fresh
    database has no such table yet at the point this runs (schema.sql is
    about to create it, columns included); returning '' is correct there,
    not a bug to guard against. Only ever called with fixed, internal
    table/column names, never CLI input.
    """
    if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return ''
    existing = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
    return ''.join(f'ALTER TABLE {table} ADD COLUMN {column} {coltype};\n'
                   for column, coltype in columns.items() if column not in existing)


def initialize_database(database: DatabasePath = None) -> Path:
    """Create/reapply the current schema, or explicitly migrate an older database
    to it without changing existing rows.

    schema.sql is always the full, current, idempotent schema (every statement
    is IF NOT EXISTS/OR IGNORE), so applying it to an older database additively
    brings it straight to SCHEMA_VERSION in one pass regardless of its starting
    version -- v1 -> v2 added evidence_links; v1/v2 -> v3 added the events_fts/
    errors_fts search index; v1/v2/v3 -> v4 added source_documents and its own
    FTS5 index; v1..v4 -> v5 adds source_documents.collection/content_sha256
    (via _pending_column_additions, since CREATE TABLE IF NOT EXISTS is a
    no-op against the existing v4 table) plus the experiments/
    experiment_symbols tables; v1..v5 -> v6 adds findings.superseded_by_finding_id
    the same way; v1..v6 -> v7 adds the agent_usage table; v1..v7 -> v8 adds
    the file_index table -- a whole new table each time needs no
    _pending_column_additions entry, CREATE TABLE IF NOT EXISTS handles it
    identically for a fresh or migrated database (see schema.sql).
    Read-only operations and existing writers never migrate implicitly.
    """
    path = resolve_database_path(database)
    schema = SCHEMA_PATH.read_text(encoding='utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(path, create=True)) as connection:
        _check_version(connection)
        try:
            # Read-only, safe before the transaction below even begins. Prepended
            # (not run afterward) so it lands before schema.sql's own statements
            # that assume these columns already exist (e.g. an index on one), and
            # so it shares that transaction rather than being separately committed.
            alterations = (
                _pending_column_additions(
                    connection, 'source_documents', {'collection': 'TEXT', 'content_sha256': 'TEXT'})
                + _pending_column_additions(
                    connection, 'findings', {'superseded_by_finding_id':
                        "INTEGER REFERENCES findings(id) ON DELETE RESTRICT "
                        "CHECK (superseded_by_finding_id IS NULL OR superseded_by_finding_id <> id) "
                        "CHECK (superseded_by_finding_id IS NULL OR status = 'superseded')"}))
            # executescript commits any pending transaction before starting, so
            # BEGIN belongs in the script. DDL and version insertion roll back together.
            connection.executescript('BEGIN IMMEDIATE;\n' + alterations + schema)
            # schema_version < SCHEMA_VERSION (rather than == some single prior
            # version) lets one script jump a database straight from v1 or v2 to
            # v3; it is a no-op once the database is already at SCHEMA_VERSION.
            connection.execute("""UPDATE schema_metadata SET schema_version=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=1 AND schema_version<?""", (SCHEMA_VERSION, SCHEMA_VERSION))
            _check_version(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return path


def validate_schema_version(connection: sqlite3.Connection, *, minimum: int = 1) -> int:
    """Validate an initialized database without running DDL or migrations."""
    _check_version(connection)
    try:
        row = connection.execute('SELECT schema_version FROM schema_metadata WHERE id=1').fetchone()
    except sqlite3.Error as exc:
        raise ValueError('Database is not initialized; schema version metadata is missing.') from exc
    if row is None or row[0] not in SUPPORTED_VERSIONS:
        raise ValueError('Unsupported or invalid schema version; a migration is required.')
    if row[0] < minimum:
        raise ValueError(f'Schema v{minimum} is required; run python -m database.init_db --database PATH to migrate.')
    return row[0]
