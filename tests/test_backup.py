"""Backup/restore tests; every database and backup path is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import db
from database.backup import dump_database, main, restore_database
from database.evidence import attach_evidence, list_evidence
from database.ingest_source import ingest_directory
from database.search import search
from phase4_fixtures import create_v1, seed, snapshot


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
        self.open_database()
        dump_database(self.path, self.backup_path)
        schema_only = self.backup_path.read_text(encoding='utf-8')
        # relationships has no FTS5 trigger attached (only events/errors do),
        # so this injected row cannot collide with the events_fts_ai-firing
        # issue that using events/errors here would hit once restore's
        # earlier-created triggers are live (see test_dump_excludes_the_
        # fts5_search_index) -- this test targets the foreign-key check alone.
        bad_dump = schema_only.replace(
            'COMMIT;',
            "INSERT INTO relationships (source_entity_id, relationship_type, target_entity_id) "
            "VALUES (999, 'x', 1000);\n"
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

    def test_v2_round_trip_preserves_all_evidence_and_constraints(self):
        connection = self.open_database()
        seed(connection)
        for owner in ('finding', 'unknown', 'decision'):
            for target in ('run', 'event', 'error', 'entity', 'relationship'):
                attach_evidence(self.path, owner, 1, target, 1)
        before = snapshot(connection)
        dump_database(self.path, self.backup_path)
        restored_path = restore_database(Path(self.temp.name) / 'restored.db', self.backup_path)
        with closing(db.connect_database(restored_path)) as restored:
            self.assertEqual(snapshot(restored), before)
            self.assertEqual(db.validate_schema_version(restored), db.SCHEMA_VERSION)
            with self.assertRaises(sqlite3.IntegrityError), restored:
                restored.execute('DELETE FROM events WHERE id=1')
            with self.assertRaises(sqlite3.IntegrityError), restored:
                restored.execute('INSERT INTO evidence_links(finding_id,event_id) VALUES (1,1)')
        self.assertEqual(list_evidence(restored_path, 'decision', 1)['total'], 5)

    def test_restore_rejects_dangling_evidence_even_with_foreign_keys_off(self):
        connection = self.open_database()
        seed(connection)
        dump_database(self.path, self.backup_path)
        sql = self.backup_path.read_text().replace('COMMIT;',
            'INSERT INTO evidence_links(unknown_id,event_id) VALUES (1,999);\nCOMMIT;')
        self.backup_path.write_text(sql)
        target = Path(self.temp.name) / 'broken.db'
        with self.assertRaisesRegex(ValueError, 'foreign key check'):
            restore_database(target, self.backup_path)
        self.assertFalse(target.exists())

    def test_failed_dump_preserves_previous_backup_atomically(self):
        self.open_database()
        dump_database(self.path, self.backup_path)
        before = self.backup_path.read_bytes()
        with patch('database.backup.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                dump_database(self.path, self.backup_path)
        self.assertEqual(self.backup_path.read_bytes(), before)
        self.assertEqual(list(self.backup_path.parent.glob('*.tmp')), [])

    def test_dump_excludes_the_fts5_search_index(self):
        # Triggers are plain DDL on events/errors and dump/restore normally
        # (they run fine before events_fts even exists); only the virtual
        # table itself and its shadow tables -- the actual index state --
        # are excluded. See backup.py's module docstring for why.
        connection = self.open_database()
        with connection:
            self.seed(connection)
        dump_database(self.path, self.backup_path)
        text = self.backup_path.read_text(encoding='utf-8')
        self.assertNotIn('CREATE VIRTUAL TABLE', text)
        self.assertNotIn('writable_schema', text)
        self.assertNotIn('sqlite_master', text)
        for shadow in ('_data', '_idx', '_docsize', '_config'):
            self.assertNotIn(f"events_fts{shadow}", text)
            self.assertNotIn(f"errors_fts{shadow}", text)
        self.assertNotIn('INSERT INTO "events_fts"', text)
        self.assertNotIn('INSERT INTO "errors_fts"', text)
        self.assertIn('CREATE TRIGGER events_fts_ai', text)

    def test_restore_recreates_and_rebuilds_the_search_index(self):
        connection = self.open_database()
        with connection:
            self.seed(connection)
        dump_database(self.path, self.backup_path)
        restored_path = Path(self.temp.name) / 'restored.db'
        restore_database(restored_path, self.backup_path)
        with closing(db.connect_database(restored_path)) as restored:
            self.assertEqual(db.validate_schema_version(restored), db.SCHEMA_VERSION)
            tables = {r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('events_fts', tables)
        result = search(restored_path, 'OfficerLee', type='events')
        self.assertEqual([item['id'] for item in result['items']], [1])
        self.assertIn('OfficerLee', result['items'][0]['snippet'])

    def test_dump_excludes_source_documents_entirely(self):
        # Different rationale from the FTS5 exclusion above (see backup.py's
        # docstring): source_documents holds full ingested file content --
        # potentially large, and for decompiled game assemblies, copyrighted
        # -- so it must never land in this git-tracked dump, regardless of
        # artifact_type.
        self.open_database()
        source = Path(self.temp.name) / 'src'
        source.mkdir()
        (source / 'Secret.cs').write_text('class Secret { /* proprietary game logic */ }', encoding='utf-8')
        ingest_directory(self.path, 'decompiler_export', source)
        dump_database(self.path, self.backup_path)
        text = self.backup_path.read_text(encoding='utf-8')
        self.assertNotIn('source_documents', text)
        self.assertNotIn('proprietary game logic', text)
        self.assertNotIn('Secret.cs', text)

    def test_restore_leaves_source_documents_empty_and_reingest_repopulates_search(self):
        self.open_database()
        source = Path(self.temp.name) / 'src'
        source.mkdir()
        (source / 'A.cs').write_text('void ReingestableMethod() {}', encoding='utf-8')
        ingest_directory(self.path, 'source_code', source)
        dump_database(self.path, self.backup_path)
        restored_path = Path(self.temp.name) / 'restored.db'
        restore_database(restored_path, self.backup_path)
        with closing(db.connect_database(restored_path)) as restored:
            self.assertEqual(restored.execute('SELECT count(*) FROM source_documents').fetchone()[0], 0)
            tables = {r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn('source_documents', tables)
            self.assertIn('source_documents_fts', tables)
        self.assertEqual(search(restored_path, 'ReingestableMethod', type='documents')['items'], [])
        ingest_directory(restored_path, 'source_code', source)
        result = search(restored_path, 'ReingestableMethod', type='documents')
        self.assertEqual(len(result['items']), 1)

    def test_v1_restore_stays_v1_with_no_search_index(self):
        # A v1 backup restores as v1, with no FTS5 index -- restore never
        # migrates a database implicitly, and a v1 database has no search
        # index to rebuild in the first place.
        v1_path = Path(self.temp.name) / 'v1.db'
        create_v1(v1_path)
        v1_dump = Path(self.temp.name) / 'v1.sql'
        dump_database(v1_path, v1_dump)
        restored_path = Path(self.temp.name) / 'restored_v1.db'
        restore_database(restored_path, v1_dump)
        with closing(db.connect_database(restored_path)) as restored:
            self.assertEqual(db.validate_schema_version(restored), 1)
            tables = {r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('events_fts', tables)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            search(restored_path, 'anything')

    def test_dump_cannot_overwrite_database_or_hardlink_alias(self):
        self.open_database()
        alias = Path(self.temp.name) / 'alias.sql'
        alias.hardlink_to(self.path)
        before = self.path.read_bytes()
        for target in (self.path, alias):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'alias'):
                dump_database(self.path, target)
        self.assertEqual(self.path.read_bytes(), before)

if __name__ == '__main__':
    unittest.main()
