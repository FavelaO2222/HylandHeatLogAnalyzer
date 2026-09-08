"""Backup/restore tests; every database and backup path is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db
from database.backup import dump_database, main, restore_database


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        self.backup_path = Path(self.temp.name) / 'backups' / 'research.sql'

    def open_database(self):
        db.initialize_database(self.path)
        connection = db.connect_database(self.path)
        self.addCleanup(connection.close)
        return connection

    def seed(self, connection):
        artifact_id = connection.execute(
            'INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, ?, ?)',
            ('log', 'logs/session.log', 'session.log')).lastrowid
        run_id = connection.execute(
            'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
            (artifact_id, 'general', 'NEEDS_ATTENTION')).lastrowid
        connection.execute(
            'INSERT INTO events (test_run_id, source_artifact_id, category, message, source_line) '
            'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'diagnostic_result', 'Name=OfficerLee', 10))
        entity_id = connection.execute(
            'INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
            ('game_object', 'OfficerLee', 'officerlee')).lastrowid
        connection.execute(
            'INSERT INTO findings (subject_entity_id, finding, confidence, test_run_id, source_artifact_id) '
            'VALUES (?, ?, ?, ?, ?)', (entity_id, 'A recorded finding', 'strong', run_id, artifact_id))
        return artifact_id, run_id, entity_id

    def counts(self, connection):
        tables = ('source_artifacts', 'test_runs', 'events', 'entities', 'findings')
        return {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables}

    def test_dump_on_missing_source_raises(self):
        with self.assertRaises(sqlite3.Error):
            dump_database(self.path, self.backup_path)
        self.assertFalse(self.backup_path.exists())

    def test_dump_writes_creatable_backup_directory_and_sql_text(self):
        self.open_database()
        result = dump_database(self.path, self.backup_path)
        self.assertEqual(result, self.backup_path.resolve())
        text = self.backup_path.read_text(encoding='utf-8')
        self.assertIn('CREATE TABLE', text)
        self.assertIn('BEGIN TRANSACTION', text)
        self.assertIn('COMMIT', text)

    def test_dump_refuses_a_source_with_foreign_key_violations(self):
        db.initialize_database(self.path)
        # Bypass connect_database (which always enforces foreign keys) to plant a real violation.
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (999, 999, 'x', 'y')")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, 'foreign key check'):
            dump_database(self.path, self.backup_path)
        self.assertFalse(self.backup_path.exists())

    def test_round_trip_preserves_every_row_and_the_restored_database_is_usable(self):
        connection = self.open_database()
        with connection:
            self.seed(connection)
        original_counts = self.counts(connection)

        dump_database(self.path, self.backup_path)
        restored_path = Path(self.temp.name) / 'restored.db'
        result = restore_database(restored_path, self.backup_path)
        self.assertEqual(result, restored_path.resolve())

        with closing(db.connect_database(restored_path)) as restored:
            self.assertEqual(self.counts(restored), original_counts)
            self.assertEqual(restored.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            row = restored.execute('SELECT message FROM events WHERE source_line = 10').fetchone()
            self.assertEqual(row['message'], 'Name=OfficerLee')
            finding = restored.execute('SELECT finding, confidence FROM findings').fetchone()
            self.assertEqual((finding['finding'], finding['confidence']), ('A recorded finding', 'strong'))
            # The restored database really does enforce foreign keys going forward.
            with self.assertRaises(sqlite3.IntegrityError), restored:
                restored.execute(
                    "INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (999, 999, 'x', 'y')")

    def test_restore_refuses_missing_backup_file(self):
        with self.assertRaisesRegex(ValueError, 'No backup file'):
            restore_database(self.path, self.backup_path)

    def test_restore_refuses_to_overwrite_existing_database(self):
        self.open_database()
        dump_database(self.path, self.backup_path)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            restore_database(self.path, self.backup_path)

    def test_restore_of_invalid_dump_cleans_up_partial_file(self):
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path.write_text('CREATE TABLE not_valid_sql_here (', encoding='utf-8')
        target = Path(self.temp.name) / 'broken.db'
        with self.assertRaises(sqlite3.Error):
            restore_database(target, self.backup_path)
        self.assertFalse(target.exists())

    def test_restore_of_dump_with_foreign_key_violation_is_rejected_and_cleaned_up(self):
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        self.open_database()
        schema_only = '\n'.join(sqlite3.connect(self.path).iterdump())
        bad_dump = schema_only.replace(
            'COMMIT;',
            "INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (999, 999, 'x', 'y');\n"
            "COMMIT;")
        self.backup_path.write_text(bad_dump, encoding='utf-8')
        target = Path(self.temp.name) / 'broken.db'
        with self.assertRaisesRegex(ValueError, 'foreign key check'):
            restore_database(target, self.backup_path)
        self.assertFalse(target.exists())

    def test_cli_dump_and_restore_round_trip(self):
        connection = self.open_database()
        with connection:
            self.seed(connection)

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['dump', '--database', str(self.path), '--backup', str(self.backup_path)]), 0)
        self.assertIn('Wrote', output.getvalue())

        restored_path = Path(self.temp.name) / 'restored.db'
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(['restore', '--database', str(restored_path), '--backup', str(self.backup_path)]), 0)
        self.assertIn('Restored', output.getvalue())
        with closing(db.connect_database(restored_path, read_only=True)) as restored:
            self.assertEqual(restored.execute('SELECT count(*) FROM findings').fetchone()[0], 1)

    def test_cli_error_paths(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['dump', '--database', str(self.path), '--backup', str(self.backup_path)]), 2)
        self.assertIn('Error:', error.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(
                main(['restore', '--database', str(self.path), '--backup', str(self.backup_path)]), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
