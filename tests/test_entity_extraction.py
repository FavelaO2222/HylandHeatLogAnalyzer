"""Phase 3 entity extraction tests; every database is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db
from database.entity_extraction import ENTITY_TYPE, extract_names, main, sync_entities


class ExtractNamesTests(unittest.TestCase):
    def test_recognized_identity_keys_are_captured(self):
        message = "SOURCE OBJECT: Name=OfficerLee, Root=OfficerLee, IsActualRoot=True"
        self.assertEqual(extract_names(message), ["OfficerLee"])

    def test_path_value_stops_before_slash(self):
        message = "LOOKUP NPCInventory: Source=True, SourcePath=OfficerLee2/Avatar, Clone=True, ClonePath=OfficerLee3/Avatar"
        self.assertEqual(extract_names(message), ["OfficerLee2", "OfficerLee3"])

    def test_boolean_number_and_guid_values_are_ignored(self):
        message = ("INACTIVE CLONE CREATED: Name=OfficerLee2, Active=False, HasPoliceOfficer=True, "
                   "InstanceID=-644148, RegistryCount=113->113, SourceBakedGUID=cbe751b8-edba-4313")
        self.assertEqual(extract_names(message), ["OfficerLee2"])

    def test_camel_case_merge_does_not_match_bare_key(self):
        # "Root" and "Source"/"Clone" are real keys, but only as a whole word.
        message = "PARENT 0: Name=OfficerLee, IsActualRoot=True, SourceActiveSelf=True, CloneActiveSelf=False"
        self.assertEqual(extract_names(message), ["OfficerLee"])

    def test_case_insensitive_dedup_keeps_first_seen_casing(self):
        message = "Source=OfficerLee, ID=officerlee, Root=OFFICERLEE"
        self.assertEqual(extract_names(message), ["OfficerLee"])

    def test_no_identity_keys_yields_nothing(self):
        self.assertEqual(extract_names("[Hyland_Heat] Hourly suspicion decay. Current suspicion: 0.0"), [])


class SyncEntitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'

    def open_database(self):
        db.initialize_database(self.path)
        connection = db.connect_database(self.path)
        self.addCleanup(connection.close)
        return connection

    def artifact(self, connection):
        return connection.execute(
            'INSERT INTO source_artifacts (artifact_type, path, filename, sha256) VALUES (?, ?, ?, ?)',
            ('log', 'logs/session.log', 'session.log', 'a' * 64)).lastrowid

    def add_run(self, connection, artifact_id, profile='read-only-audit'):
        return connection.execute(
            'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
            (artifact_id, profile, 'NEEDS_ATTENTION')).lastrowid

    def event(self, connection, run_id, artifact_id, message):
        connection.execute(
            'INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (?, ?, ?, ?)',
            (run_id, artifact_id, 'diagnostic_result', message))

    def error(self, connection, run_id, artifact_id, message):
        connection.execute(
            'INSERT INTO errors (test_run_id, source_artifact_id, severity, message) VALUES (?, ?, ?, ?)',
            (run_id, artifact_id, 'ERROR', message))

    def test_inserts_new_entities_from_events_and_errors(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'SOURCE OBJECT: Name=OfficerLee, Root=OfficerLee')
            self.error(connection, run_id, artifact_id, 'CLONE OBJECT: Name=OfficerLee2, Root=OfficerLee2')
        result = sync_entities(self.path)
        self.assertEqual(result['scanned_messages'], 2)
        self.assertEqual(result['candidates'], 2)
        self.assertEqual(result['inserted'], ['OfficerLee', 'OfficerLee2'])
        self.assertEqual(result['already_known'], 0)
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            rows = reader.execute('SELECT name, canonical_name, entity_type FROM entities ORDER BY name').fetchall()
        self.assertEqual([(r['name'], r['canonical_name'], r['entity_type']) for r in rows],
                          [('OfficerLee', 'officerlee', ENTITY_TYPE), ('OfficerLee2', 'officerlee2', ENTITY_TYPE)])

    def test_rerun_is_idempotent_and_reports_already_known(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'Name=OfficerLee')
        sync_entities(self.path)
        second = sync_entities(self.path)
        self.assertEqual(second['inserted'], [])
        self.assertEqual(second['already_known'], 1)
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM entities').fetchone()[0], 1)

    def test_scoped_to_one_run_ignores_other_runs(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            first_run = self.add_run(connection, artifact_id)
            second_run = self.add_run(connection, artifact_id)
            self.event(connection, first_run, artifact_id, 'Name=OfficerLee')
            self.event(connection, second_run, artifact_id, 'Name=OfficerBailey')
        result = sync_entities(self.path, run_id=first_run)
        self.assertEqual(result['inserted'], ['OfficerLee'])

    def test_unknown_run_id_raises_and_writes_nothing(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'Name=OfficerLee')
        with self.assertRaisesRegex(ValueError, 'No test run with id 999'):
            sync_entities(self.path, run_id=999)
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM entities').fetchone()[0], 0)

    def test_different_entity_type_with_same_name_is_not_deduped_against(self):
        connection = self.open_database()
        with connection:
            connection.execute('INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
                                ('npc', 'OfficerLee', 'officerlee'))
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'Name=OfficerLee')
        result = sync_entities(self.path)
        self.assertEqual(result['inserted'], ['OfficerLee'])
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            self.assertEqual(reader.execute('SELECT count(*) FROM entities').fetchone()[0], 2)

    def test_never_writes_findings_unknowns_decisions_or_relationships(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'Name=OfficerLee, Root=OfficerLee')
        sync_entities(self.path)
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            for table in ('findings', 'unknowns', 'decisions', 'relationships'):
                self.assertEqual(reader.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_cli_success_and_error_paths(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            self.event(connection, run_id, artifact_id, 'Name=OfficerLee')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path)]), 0)
        self.assertIn('Inserted: 1 (OfficerLee)', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db')]), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
