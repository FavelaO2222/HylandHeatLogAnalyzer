"""Manual relationships CLI tests; every database is temporary."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from database import db
from database.record_relationship import add_relationship, list_relationships, main


class RecordRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def entity(self, entity_type='game_object', name='OfficerLee'):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
                (entity_type, name, name.lower())).lastrowid

    def test_add_with_entity_ids(self):
        source_id = self.entity(name='OfficerLee2')
        target_id = self.entity(name='OfficerLee')
        relationship_id = add_relationship(
            self.path, 'IS_CLONE_OF', source_entity_id=source_id, target_entity_id=target_id)
        row = self.connection.execute('SELECT * FROM relationships WHERE id = ?', (relationship_id,)).fetchone()
        self.assertEqual(row['source_entity_id'], source_id)
        self.assertEqual(row['target_entity_id'], target_id)
        self.assertEqual(row['relationship_type'], 'IS_CLONE_OF')
        self.assertIsNone(row['notes'])

    def test_add_with_entity_names_resolves_both(self):
        source_id = self.entity(name='OfficerLee2')
        target_id = self.entity(name='OfficerLee')
        relationship_id = add_relationship(
            self.path, 'IS_CLONE_OF', source_name='officerlee2', target_name='officerlee')
        row = self.connection.execute('SELECT * FROM relationships WHERE id = ?', (relationship_id,)).fetchone()
        self.assertEqual(row['source_entity_id'], source_id)
        self.assertEqual(row['target_entity_id'], target_id)

    def test_empty_relationship_type_rejected(self):
        self.entity(name='A')
        self.entity(name='B')
        with self.assertRaisesRegex(ValueError, 'relationship_type must not be empty'):
            add_relationship(self.path, '   ', source_name='A', target_name='B')

    def test_missing_source_rejected(self):
        self.entity(name='B')
        with self.assertRaisesRegex(ValueError, 'A source entity is required'):
            add_relationship(self.path, 'IS_A', target_name='B')

    def test_missing_target_rejected(self):
        self.entity(name='A')
        with self.assertRaisesRegex(ValueError, 'A target entity is required'):
            add_relationship(self.path, 'IS_A', source_name='A')

    def test_both_source_forms_together_rejected(self):
        self.entity(name='B')
        with self.assertRaisesRegex(ValueError, 'only one of source_entity_id or source_name'):
            add_relationship(self.path, 'IS_A', source_entity_id=1, source_name='A', target_name='B')

    def test_both_target_forms_together_rejected(self):
        self.entity(name='A')
        with self.assertRaisesRegex(ValueError, 'only one of target_entity_id or target_name'):
            add_relationship(self.path, 'IS_A', source_name='A', target_entity_id=1, target_name='B')

    def test_unknown_source_name_rejected(self):
        self.entity(name='B')
        with self.assertRaisesRegex(ValueError, "No entity named 'Nobody'"):
            add_relationship(self.path, 'IS_A', source_name='Nobody', target_name='B')

    def test_nonexistent_source_entity_id_rejected(self):
        self.entity(name='B')
        with self.assertRaisesRegex(ValueError, 'No source entity with id 999'):
            add_relationship(self.path, 'IS_A', source_entity_id=999, target_name='B')

    def test_nonexistent_target_entity_id_rejected(self):
        self.entity(name='A')
        with self.assertRaisesRegex(ValueError, 'No target entity with id 999'):
            add_relationship(self.path, 'IS_A', source_name='A', target_entity_id=999)

    def test_full_fields_recorded(self):
        source_id = self.entity(name='OfficerLee2')
        target_id = self.entity(name='OfficerLee')
        with self.connection:
            artifact_id = self.connection.execute(
                'INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, ?, ?)',
                ('log', 'logs/session.log', 'session.log')).lastrowid
            run_id = self.connection.execute(
                'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
                (artifact_id, 'general', 'NEEDS_ATTENTION')).lastrowid
        relationship_id = add_relationship(
            self.path, 'IS_CLONE_OF', source_entity_id=source_id, target_entity_id=target_id,
            source_artifact_id=artifact_id, test_run_id=run_id, notes='Matched SceneId')
        row = self.connection.execute('SELECT * FROM relationships WHERE id = ?', (relationship_id,)).fetchone()
        self.assertEqual(row['source_artifact_id'], artifact_id)
        self.assertEqual(row['test_run_id'], run_id)
        self.assertEqual(row['notes'], 'Matched SceneId')

    def test_list_filters_by_relationship_type_and_entity_id(self):
        lee = self.entity(name='OfficerLee')
        lee2 = self.entity(name='OfficerLee2')
        other_a = self.entity(name='OfficerDavis')
        other_b = self.entity(name='OfficerDavis2')
        add_relationship(self.path, 'IS_CLONE_OF', source_entity_id=lee2, target_entity_id=lee)
        add_relationship(self.path, 'IS_A', source_entity_id=other_a, target_entity_id=other_b)
        rows = list_relationships(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['id'] for r in list_relationships(self.path, relationship_type='IS_A')],
                          [rows[1]['id']])
        self.assertEqual([r['id'] for r in list_relationships(self.path, entity_id=lee)], [rows[0]['id']])
        self.assertEqual([r['id'] for r in list_relationships(self.path, entity_id=lee2)], [rows[0]['id']])

    def test_cli_add_and_list(self):
        self.entity(name='OfficerLee2')
        self.entity(name='OfficerLee')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--relationship-type', 'IS_CLONE_OF',
                                    '--source-name', 'OfficerLee2', '--target-name', 'OfficerLee']), 0)
        self.assertIn('Recorded relationship', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('OfficerLee2 IS_CLONE_OF OfficerLee', output.getvalue())

    def test_cli_requires_source_and_target(self):
        self.entity(name='A')
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            main(['--database', str(self.path), 'add', '--relationship-type', 'IS_A', '--target-name', 'A'])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_empty_list_and_error_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No relationships recorded.', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--relationship-type', 'IS_A',
                                    '--source-entity-id', '999', '--target-entity-id', '998']), 2)
        self.assertIn('Error:', error.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db'), 'list']), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
