"""Database tests are isolated from real project databases and game logs."""

from contextlib import closing, redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from database import db
from database.init_db import main


class DatabaseTests(unittest.TestCase):
    TABLES = {'schema_metadata', 'source_artifacts', 'test_runs', 'events', 'errors',
              'entities', 'findings', 'unknowns', 'decisions', 'relationships', 'evidence_links',
              'source_documents', 'experiments', 'experiment_symbols', 'agent_usage'}
    # FTS5 virtual tables plus their shadow tables (search index only; see database/search.py).
    FTS_TABLES = {f'{base}{suffix}' for base in ('events_fts', 'errors_fts', 'source_documents_fts')
                  for suffix in ('', '_data', '_idx', '_docsize', '_config')}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'nested' / 'research.db'

    def open_database(self):
        db.initialize_database(self.path)
        connection = db.connect_database(self.path)
        self.addCleanup(connection.close)
        return connection

    def artifact(self, connection):
        return connection.execute(
            'INSERT INTO source_artifacts (artifact_type, path, filename, sha256) VALUES (?, ?, ?, ?)',
            ('log', 'logs/session.log', 'session.log', 'a' * 64)).lastrowid

    def test_initialization_tables_version_and_no_sample_data(self):
        self.assertFalse(db.database_exists(self.path))
        connection = self.open_database()
        self.assertTrue(db.database_exists(self.path))
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, self.TABLES | self.FTS_TABLES)
        version = connection.execute('SELECT * FROM schema_metadata').fetchone()
        self.assertEqual(version['schema_version'], db.SCHEMA_VERSION)
        self.assertTrue(version['created_at'].endswith('Z'))
        self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT count(*) FROM findings').fetchone()[0], 0)

    def test_repeated_initialization_preserves_rows_and_metadata(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
        original = tuple(connection.execute('SELECT * FROM schema_metadata').fetchone())
        db.initialize_database(self.path)
        self.assertEqual(tuple(connection.execute('SELECT * FROM schema_metadata').fetchone()), original)
        self.assertEqual(connection.execute('SELECT filename FROM source_artifacts WHERE id=?',
                                            (artifact_id,)).fetchone()[0], 'session.log')

    def test_foreign_keys_enforced_on_each_connection(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
        with closing(db.connect_database(self.path)) as second:
            self.assertEqual(second.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError), second:
                second.execute('INSERT INTO events (source_artifact_id, test_run_id, category, message) VALUES (?, ?, ?, ?)',
                               (artifact_id, 999, 'lifecycle', 'observed'))
            with self.assertRaises(sqlite3.IntegrityError), second:
                second.execute('INSERT INTO relationships (source_entity_id, relationship_type, target_entity_id) VALUES (?, ?, ?)',
                               (999, 'IS_A', 1000))

    def test_basic_insert_read_and_provenance_chain(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = connection.execute('INSERT INTO test_runs (source_artifact_id, profile) VALUES (?, ?)',
                                        (artifact_id, 'synthetic-audit')).lastrowid
            entity_id = connection.execute('INSERT INTO entities (entity_type, name) VALUES (?, ?)',
                                           ('npc', 'SyntheticOfficer')).lastrowid
            target_id = connection.execute('INSERT INTO entities (entity_type, name) VALUES (?, ?)',
                                           ('class', 'PoliceOfficer')).lastrowid
            event_id = connection.execute(
                'INSERT INTO events (test_run_id, source_artifact_id, source_line, category, message) VALUES (?, ?, ?, ?, ?)',
                (run_id, artifact_id, 42, 'lifecycle', 'An observed transition')).lastrowid
            finding_id = connection.execute(
                'INSERT INTO findings (subject_entity_id, finding, source_artifact_id, test_run_id, source_line) VALUES (?, ?, ?, ?, ?)',
                (entity_id, 'Synthetic finding only', artifact_id, run_id, 42)).lastrowid
            unknown_id = connection.execute(
                'INSERT INTO unknowns (subject_entity_id, question, required_evidence) VALUES (?, ?, ?)',
                (entity_id, 'Is this repeatable?', 'Another observation')).lastrowid
            decision_id = connection.execute(
                'INSERT INTO decisions (subject_entity_id, finding_id, topic, decision, reason) VALUES (?, ?, ?, ?, ?)',
                (entity_id, finding_id, 'test', 'Continue observation', 'Evidence is incomplete')).lastrowid
            relationship_id = connection.execute(
                'INSERT INTO relationships (source_entity_id, relationship_type, target_entity_id, source_artifact_id, test_run_id) VALUES (?, ?, ?, ?, ?)',
                (entity_id, 'IS_A', target_id, artifact_id, run_id)).lastrowid
        with closing(db.connect_database(self.path)) as reader:
            row = reader.execute('''SELECT e.source_line, a.filename, t.profile
                FROM events e JOIN source_artifacts a ON a.id=e.source_artifact_id
                JOIN test_runs t ON t.id=e.test_run_id WHERE e.id=?''', (event_id,)).fetchone()
            self.assertEqual(tuple(row), (42, 'session.log', 'synthetic-audit'))
            self.assertEqual(reader.execute('SELECT confidence FROM findings WHERE id=?', (finding_id,)).fetchone()[0], 'unknown')
            self.assertEqual(reader.execute('SELECT status FROM unknowns WHERE id=?', (unknown_id,)).fetchone()[0], 'open')
            self.assertEqual(reader.execute('SELECT finding_id, reason FROM decisions WHERE id=?', (decision_id,)).fetchone()[0], finding_id)
            relation = reader.execute('SELECT source_entity_id, target_entity_id FROM relationships WHERE id=?', (relationship_id,)).fetchone()
            self.assertEqual(tuple(relation), (entity_id, target_id))
            self.assertEqual(reader.execute('SELECT name FROM entities WHERE id=?', (entity_id,)).fetchone()[0], 'SyntheticOfficer')

    def test_full_multiline_stack_trace_round_trip(self):
        connection = self.open_database()
        trace = 'System.Exception: synthetic\r\n' + ''.join(f'   at Frame{i}()\n' for i in range(2000)) + '\tinner detail\n'
        with connection:
            artifact_id = self.artifact(connection)
            error_id = connection.execute(
                'INSERT INTO errors (source_artifact_id, severity, message, stack_trace, source_line) VALUES (?, ?, ?, ?, ?)',
                (artifact_id, 'EXCEPTION', 'synthetic', trace, 55)).lastrowid
        with closing(db.connect_database(self.path)) as reader:
            self.assertEqual(reader.execute('SELECT stack_trace FROM errors WHERE id=?', (error_id,)).fetchone()[0], trace)

    def test_deletion_cannot_orphan_evidence(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            connection.execute('INSERT INTO events (source_artifact_id, category, message) VALUES (?, ?, ?)',
                               (artifact_id, 'test', 'evidence'))
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute('DELETE FROM source_artifacts WHERE id=?', (artifact_id,))

    def test_optional_references_and_minimal_research_rows(self):
        connection = self.open_database()
        with connection:
            connection.execute('INSERT INTO test_runs DEFAULT VALUES')
            connection.execute('INSERT INTO findings (subject_text, finding) VALUES (?, ?)', ('manual topic', 'Unverified statement'))
            connection.execute('INSERT INTO unknowns (question) VALUES (?)', ('What needs checking?',))
            connection.execute('INSERT INTO decisions (topic, decision, reason) VALUES (?, ?, ?)', ('scope', 'Defer', 'Insufficient evidence'))
        self.assertEqual(connection.execute('SELECT source_artifact_id, result FROM test_runs').fetchone()[1], 'UNKNOWN')
        self.assertIsNone(connection.execute('SELECT subject_entity_id FROM findings').fetchone()[0])

    def test_source_lines_require_positive_integer_and_artifact(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
        for line in (0, -1, 1.5, 'not a line'):
            with self.subTest(line=line), self.assertRaises(sqlite3.IntegrityError), connection:
                connection.execute('INSERT INTO events (source_artifact_id, source_line, category, message) VALUES (?, ?, ?, ?)',
                                   (artifact_id, line, 'test', 'evidence'))
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute('INSERT INTO findings (finding, source_line) VALUES (?, ?)', ('Missing artifact', 5))
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute('INSERT INTO events (category, message) VALUES (?, ?)', ('test', 'Missing artifact'))

    def test_controlled_enums_and_hash_format(self):
        connection = self.open_database()
        invalid_rows = [
            ('INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, ?, ?)', ('invalid', 'p', 'f')),
            ('INSERT INTO source_artifacts (artifact_type, path, filename, sha256) VALUES (?, ?, ?, ?)', ('log', 'p', 'f', 'z' * 64)),
            ('INSERT INTO test_runs (result) VALUES (?)', ('MAYBE',)),
            ('INSERT INTO findings (finding, confidence) VALUES (?, ?)', ('f', 'guaranteed')),
            ('INSERT INTO findings (finding, status) VALUES (?, ?)', ('f', 'lost')),
            ('INSERT INTO unknowns (question, importance) VALUES (?, ?)', ('q', 'urgent')),
            ('INSERT INTO unknowns (question, status) VALUES (?, ?)', ('q', 'done')),
            ('INSERT INTO decisions (topic, decision, reason, status) VALUES (?, ?, ?, ?)', ('t', 'd', 'r', 'done')),
        ]
        for sql, values in invalid_rows:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), connection:
                connection.execute(sql, values)

    def test_transaction_rollback_and_connection_lifecycle(self):
        connection = self.open_database()
        with self.assertRaises(RuntimeError):
            with connection:
                self.artifact(connection)
                raise RuntimeError('abort')
        self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 0)
        with closing(db.connect_database(self.path)) as other:
            self.assertIsInstance(other.execute('SELECT * FROM schema_metadata').fetchone(), sqlite3.Row)
        with self.assertRaises(sqlite3.ProgrammingError):
            other.execute('SELECT 1')

    def test_missing_database_is_not_created_by_connect_or_exists(self):
        self.assertFalse(db.database_exists(self.path))
        with self.assertRaises(sqlite3.OperationalError):
            db.connect_database(self.path)
        self.assertFalse(self.path.exists())

    def test_relative_and_default_paths_ignore_cwd(self):
        with patch.object(db, 'PROJECT_ROOT', self.root):
            self.assertEqual(db.resolve_database_path(), self.root / 'data' / 'hylandheat.db')
            self.assertEqual(db.initialize_database('another/test.sqlite'), self.root / 'another' / 'test.sqlite')
            self.assertEqual(db.resolve_database_path(self.path), self.path)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 0)
            self.assertTrue((self.root / 'data' / 'hylandheat.db').exists())

    def test_cli_from_other_directory_uses_project_relative_path(self):
        relative = os.path.relpath(self.path, db.PROJECT_ROOT)
        env = dict(os.environ, PYTHONPATH=str(db.PROJECT_ROOT))
        result = subprocess.run([sys.executable, '-m', 'database.init_db', '--database', relative],
                                cwd=self.root, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.path), result.stdout)
        self.assertTrue(self.path.exists())

    def test_cli_error_is_concise(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.root)]), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_future_version_rejected_without_mutation(self):
        connection = self.open_database()
        with connection:
            self.artifact(connection)
            connection.execute('UPDATE schema_metadata SET schema_version=?', (db.SCHEMA_VERSION + 1,))
        with self.assertRaises(ValueError):
            db.initialize_database(self.path)
        self.assertEqual(connection.execute('SELECT schema_version FROM schema_metadata').fetchone()[0], db.SCHEMA_VERSION + 1)
        self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 1)

    def test_unversioned_database_rejected_without_mutation(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')
            connection.commit()
        with self.assertRaises(ValueError):
            db.initialize_database(self.path)
        with closing(db.connect_database(self.path)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {'unrelated'})

    def test_failed_schema_initialization_rolls_back(self):
        broken = self.root / 'broken.sql'
        broken.write_text('CREATE TABLE partial (id INTEGER); INVALID SQL;', encoding='utf-8')
        with patch.object(db, 'SCHEMA_PATH', broken), self.assertRaises(sqlite3.Error):
            db.initialize_database(self.path)
        with closing(db.connect_database(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 0)
        db.initialize_database(self.path)

    def test_authoritative_schema_works_without_helper(self):
        with closing(sqlite3.connect(self.root / 'direct.sqlite3')) as connection:
            connection.executescript(db.SCHEMA_PATH.read_text(encoding='utf-8'))
            self.assertEqual(connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT schema_version FROM schema_metadata').fetchone()[0], db.SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
