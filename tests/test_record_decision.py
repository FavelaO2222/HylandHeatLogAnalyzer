"""Manual decisions CLI tests; every database is temporary."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from database import db
from database.record_decision import add_decision, list_decisions, main, update_decision_status


class RecordDecisionTests(unittest.TestCase):
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

    def finding(self, text='A finding'):
        with self.connection:
            return self.connection.execute('INSERT INTO findings (finding) VALUES (?)', (text,)).lastrowid

    def test_add_with_minimal_fields_defaults_status(self):
        decision_id = add_decision(self.path, 'scope', 'Defer', 'Insufficient evidence')
        row = self.connection.execute('SELECT * FROM decisions WHERE id = ?', (decision_id,)).fetchone()
        self.assertEqual(row['status'], 'active')
        self.assertIsNone(row['subject_entity_id'])
        self.assertIsNone(row['finding_id'])

    def test_empty_field_text_rejected(self):
        with self.assertRaisesRegex(ValueError, 'topic text must not be empty'):
            add_decision(self.path, '  ', 'Defer', 'reason')
        with self.assertRaisesRegex(ValueError, 'decision text must not be empty'):
            add_decision(self.path, 'topic', '  ', 'reason')
        with self.assertRaisesRegex(ValueError, 'reason text must not be empty'):
            add_decision(self.path, 'topic', 'Defer', '  ')

    def test_subject_name_resolves_to_existing_entity(self):
        entity_id = self.entity()
        decision_id = add_decision(self.path, 'scope', 'Defer', 'reason', subject_name='officerlee')
        row = self.connection.execute('SELECT subject_entity_id FROM decisions WHERE id = ?', (decision_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)

    def test_unknown_subject_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "No entity named 'Nobody'"):
            add_decision(self.path, 'scope', 'Defer', 'reason', subject_name='Nobody')

    def test_ambiguous_subject_name_rejected(self):
        self.entity(entity_type='game_object', name='OfficerLee')
        self.entity(entity_type='npc', name='OfficerLee')
        with self.assertRaisesRegex(ValueError, 'matches more than one entity'):
            add_decision(self.path, 'scope', 'Defer', 'reason', subject_name='OfficerLee')

    def test_subject_entity_id_and_subject_name_together_rejected(self):
        with self.assertRaisesRegex(ValueError, 'only one of subject_entity_id or subject_name'):
            add_decision(self.path, 'scope', 'Defer', 'reason', subject_entity_id=1, subject_name='OfficerLee')

    def test_nonexistent_subject_entity_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No entity with id 999'):
            add_decision(self.path, 'scope', 'Defer', 'reason', subject_entity_id=999)

    def test_nonexistent_finding_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No finding with id 999'):
            add_decision(self.path, 'scope', 'Defer', 'reason', finding_id=999)

    def test_full_fields_recorded_and_linked(self):
        entity_id = self.entity()
        finding_id = self.finding()
        decision_id = add_decision(
            self.path, 'SWAT teardown', 'Keep F10 locked', 'Lifecycle research incomplete',
            subject_entity_id=entity_id, finding_id=finding_id)
        row = self.connection.execute('SELECT * FROM decisions WHERE id = ?', (decision_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)
        self.assertEqual(row['finding_id'], finding_id)
        self.assertEqual(row['topic'], 'SWAT teardown')
        self.assertEqual(row['decision'], 'Keep F10 locked')
        self.assertEqual(row['reason'], 'Lifecycle research incomplete')

    def test_list_decisions_joins_entity_name_and_filters_by_status(self):
        entity_id = self.entity()
        add_decision(self.path, 'scope', 'Defer', 'reason', subject_entity_id=entity_id)
        add_decision(self.path, 'other', 'Proceed', 'reason two')
        rows = list_decisions(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['subject_entity_name'], 'OfficerLee')
        self.assertIsNone(rows[1]['subject_entity_name'])
        second_id = rows[1]['id']
        update_decision_status(self.path, second_id, 'reversed')
        self.assertEqual([r['id'] for r in list_decisions(self.path, status='active')], [rows[0]['id']])
        self.assertEqual([r['id'] for r in list_decisions(self.path, status='reversed')], [second_id])

    def test_update_status_rejects_unknown_id_and_invalid_status(self):
        decision_id = add_decision(self.path, 'scope', 'Defer', 'reason')
        with self.assertRaisesRegex(ValueError, 'No decision with id 999'):
            update_decision_status(self.path, 999, 'superseded')
        with self.assertRaisesRegex(ValueError, 'status must be one of'):
            update_decision_status(self.path, decision_id, 'archived')
        update_decision_status(self.path, decision_id, 'superseded')
        row = self.connection.execute('SELECT status FROM decisions WHERE id = ?', (decision_id,)).fetchone()
        self.assertEqual(row['status'], 'superseded')

    def test_cli_add_list_and_update_status(self):
        self.entity()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--topic', 'scope',
                                    '--decision', 'Defer', '--reason', 'Insufficient evidence',
                                    '--subject-name', 'OfficerLee']), 0)
        self.assertIn('Recorded decision', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('scope -> Defer', output.getvalue())
        self.assertIn('OfficerLee', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'update-status', '1', 'reversed']), 0)
        self.assertIn('set to reversed', output.getvalue())

    def test_cli_empty_list_and_error_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No decisions recorded.', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--topic', 't',
                                    '--decision', 'd', '--reason', 'r', '--subject-entity-id', '999']), 2)
        self.assertIn('Error:', error.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db'), 'list']), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
