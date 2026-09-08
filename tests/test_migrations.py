from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import backup, context_builder, db, evidence, inspect_db, run_comparison, search
from phase4_fixtures import create_v1, snapshot


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'old.db'
        create_v1(self.path)

    def test_v1_to_v2_preserves_every_legacy_row_and_is_idempotent(self):
        with closing(db.connect_database(self.path)) as connection:
            before = snapshot(connection)
            created = connection.execute('SELECT created_at FROM schema_metadata').fetchone()[0]
        db.initialize_database(self.path)
        with closing(db.connect_database(self.path)) as connection:
            after = snapshot(connection)
            self.assertEqual(after.pop('evidence_links'), [])
            self.assertEqual(after.pop('source_documents'), [])
            self.assertEqual(after.pop('experiments'), [])
            self.assertEqual(after.pop('experiment_symbols'), [])
            self.assertEqual(before, after)
            self.assertEqual(db.validate_schema_version(connection), db.SCHEMA_VERSION)
            self.assertEqual(created, connection.execute('SELECT created_at FROM schema_metadata').fetchone()[0])
            metadata = tuple(connection.execute('SELECT * FROM schema_metadata').fetchone())
        evidence.attach_evidence(self.path, 'finding', 1, 'relationship', 1)
        db.initialize_database(self.path)
        with closing(db.connect_database(self.path)) as connection:
            self.assertEqual(tuple(connection.execute('SELECT * FROM schema_metadata').fetchone()), metadata)
        self.assertEqual(evidence.list_evidence(self.path, 'finding', 1)['total'], 1)

    def test_v1_to_v3_backfills_legacy_rows_into_the_search_index(self):
        # The seed fixture's legacy event/error rows predate the FTS5 index by
        # construction (create_v1 writes straight through the v1 schema); a
        # correct migration must backfill them, not just start indexing from here.
        db.initialize_database(self.path)
        events = search.search(self.path, 'Shared', type='events')['items']
        self.assertEqual({item['id'] for item in events}, {1, 2})
        errors = search.search(self.path, 'same error', type='errors')['items']
        self.assertEqual({item['id'] for item in errors}, {1, 2})
        # Reapplying an already-current database rebuilds (not duplicates) the index.
        db.initialize_database(self.path)
        self.assertEqual({item['id'] for item in search.search(self.path, 'Shared', type='events')['items']},
                         {1, 2})

    def test_fresh_and_migrated_schemas_match(self):
        fresh = self.root / 'fresh.db'
        db.initialize_database(fresh)
        db.initialize_database(self.path)
        def definitions(path):
            with closing(db.connect_database(path)) as connection:
                return [tuple(r) for r in connection.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        self.assertEqual(definitions(fresh), definitions(self.path))

    def test_failed_migration_rolls_back_schema_version_and_rows(self):
        with closing(db.connect_database(self.path)) as connection:
            before = snapshot(connection)
        broken = self.root / 'broken.sql'
        broken.write_text(db.SCHEMA_PATH.read_text() + '\nINVALID SQL;')
        with patch.object(db, 'SCHEMA_PATH', broken), self.assertRaises(sqlite3.Error):
            db.initialize_database(self.path)
        with closing(db.connect_database(self.path)) as connection:
            self.assertEqual(db.validate_schema_version(connection), 1)
            self.assertEqual(before, snapshot(connection))

    def test_v1_reads_and_backup_remain_compatible_without_implicit_migration(self):
        before = self.path.read_bytes()
        self.assertIn('Recorded finding', context_builder.build_context(self.path))
        self.assertIn('Database schema: 1', inspect_db.inspect_database(self.path))
        self.assertTrue(run_comparison.compare_runs(1, 1, self.path)['identical'])
        dump = self.root / 'v1.sql'
        backup.dump_database(self.path, dump)
        restored = backup.restore_database(self.root / 'restored.db', dump)
        with closing(db.connect_database(restored)) as connection:
            self.assertEqual(db.validate_schema_version(connection), 1)
        self.assertEqual(before, self.path.read_bytes())
        db.initialize_database(restored)
        evidence.attach_evidence(restored, 'finding', 1, 'run', 1)

    def test_v1_evidence_commands_require_explicit_migration(self):
        before = self.path.read_bytes()
        for action in (lambda: evidence.attach_evidence(self.path, 'finding', 1, 'event', 1),
                       lambda: evidence.list_evidence(self.path, 'finding', 1)):
            with self.assertRaisesRegex(ValueError, 'database.init_db'):
                action()
        self.assertEqual(before, self.path.read_bytes())

    def test_v1_search_requires_explicit_migration(self):
        before = self.path.read_bytes()
        for action in (lambda: search.search(self.path, 'Shared'),
                       lambda: search.rebuild_search_index(self.path)):
            with self.assertRaisesRegex(ValueError, 'database.init_db'):
                action()
        self.assertEqual(before, self.path.read_bytes())
