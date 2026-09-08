"""Manual unknowns CLI tests; every database is temporary."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from database import db
from database.record_unknown import add_unknown, list_unknowns, main, update_unknown_status


class RecordUnknownTests(unittest.TestCase):
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

    def test_add_with_minimal_fields_defaults_importance_and_status(self):
        unknown_id = add_unknown(self.path, 'A minimal question')
        row = self.connection.execute('SELECT * FROM unknowns WHERE id = ?', (unknown_id,)).fetchone()
        self.assertEqual(row['importance'], 'medium')
        self.assertEqual(row['status'], 'open')
        self.assertIsNone(row['subject_entity_id'])
        self.assertIsNone(row['resolved_at'])

    def test_empty_question_text_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            add_unknown(self.path, '   ')

    def test_invalid_importance_rejected(self):
        with self.assertRaisesRegex(ValueError, 'importance must be one of'):
            add_unknown(self.path, 'text', importance='urgent')

    def test_subject_name_resolves_to_existing_entity(self):
        entity_id = self.entity()
        unknown_id = add_unknown(self.path, 'Is this repeatable?', subject_name='officerlee')
        row = self.connection.execute('SELECT subject_entity_id FROM unknowns WHERE id = ?', (unknown_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)

    def test_unknown_subject_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "No entity named 'Nobody'"):
            add_unknown(self.path, 'text', subject_name='Nobody')

    def test_ambiguous_subject_name_rejected(self):
        self.entity(entity_type='game_object', name='OfficerLee')
        self.entity(entity_type='npc', name='OfficerLee')
        with self.assertRaisesRegex(ValueError, 'matches more than one entity'):
            add_unknown(self.path, 'text', subject_name='OfficerLee')

    def test_subject_entity_id_and_subject_name_together_rejected(self):
        with self.assertRaisesRegex(ValueError, 'only one of subject_entity_id or subject_name'):
            add_unknown(self.path, 'text', subject_entity_id=1, subject_name='OfficerLee')

    def test_nonexistent_subject_entity_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No entity with id 999'):
            add_unknown(self.path, 'text', subject_entity_id=999)

    def test_full_fields_recorded_and_linked(self):
        entity_id = self.entity()
        unknown_id = add_unknown(
            self.path, 'Full evidence question', importance='critical', subject_entity_id=entity_id,
            required_evidence='A paired same-instance observation', related_feature='SWAT lifecycle')
        row = self.connection.execute('SELECT * FROM unknowns WHERE id = ?', (unknown_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)
        self.assertEqual(row['importance'], 'critical')
        self.assertEqual(row['required_evidence'], 'A paired same-instance observation')
        self.assertEqual(row['related_feature'], 'SWAT lifecycle')

    def test_list_unknowns_joins_entity_name_and_filters_by_status_and_importance(self):
        entity_id = self.entity()
        add_unknown(self.path, 'Linked to an entity', subject_entity_id=entity_id, importance='high')
        add_unknown(self.path, 'Freeform, no entity', importance='low')
        rows = list_unknowns(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['subject_entity_name'], 'OfficerLee')
        self.assertIsNone(rows[1]['subject_entity_name'])
        self.assertEqual([r['id'] for r in list_unknowns(self.path, importance='low')], [rows[1]['id']])
        update_unknown_status(self.path, rows[1]['id'], 'resolved')
        self.assertEqual([r['id'] for r in list_unknowns(self.path, status='open')], [rows[0]['id']])
        self.assertEqual([r['id'] for r in list_unknowns(self.path, status='resolved')], [rows[1]['id']])

    def test_update_status_rejects_unknown_id_and_invalid_status(self):
        unknown_id = add_unknown(self.path, 'text')
        with self.assertRaisesRegex(ValueError, 'No unknown with id 999'):
            update_unknown_status(self.path, 999, 'resolved')
        with self.assertRaisesRegex(ValueError, 'status must be one of'):
            update_unknown_status(self.path, unknown_id, 'archived')

    def test_update_status_sets_resolved_at_on_resolved_and_clears_on_reopen(self):
        unknown_id = add_unknown(self.path, 'text')
        update_unknown_status(self.path, unknown_id, 'resolved')
        row = self.connection.execute('SELECT resolved_at FROM unknowns WHERE id = ?', (unknown_id,)).fetchone()
        self.assertIsNotNone(row['resolved_at'])
        update_unknown_status(self.path, unknown_id, 'investigating')
        row = self.connection.execute('SELECT status, resolved_at FROM unknowns WHERE id = ?', (unknown_id,)).fetchone()
        self.assertEqual(row['status'], 'investigating')
        self.assertIsNone(row['resolved_at'])

    def test_cli_add_list_and_update_status(self):
        self.entity()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--question', 'CLI recorded question',
                                    '--subject-name', 'OfficerLee', '--importance', 'high']), 0)
        self.assertIn('Recorded unknown', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('CLI recorded question', output.getvalue())
        self.assertIn('OfficerLee', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'update-status', '1', 'resolved']), 0)
        self.assertIn('set to resolved', output.getvalue())

    def test_cli_empty_list_and_error_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No unknowns recorded.', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--question', 'x',
                                    '--subject-entity-id', '999']), 2)
        self.assertIn('Error:', error.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db'), 'list']), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
