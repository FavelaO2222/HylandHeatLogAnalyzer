"""Manual findings CLI tests; every database is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db
from database.record_finding import add_finding, format_finding_row, list_findings, main, update_finding_status


class RecordFindingTests(unittest.TestCase):
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

    def artifact(self):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, ?, ?)',
                ('log', 'logs/session.log', 'session.log')).lastrowid

    def make_run(self, artifact_id):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
                (artifact_id, 'general', 'NEEDS_ATTENTION')).lastrowid

    def test_add_with_minimal_fields_defaults_confidence_and_status(self):
        finding_id = add_finding(self.path, 'A minimal finding')
        row = self.connection.execute('SELECT * FROM findings WHERE id = ?', (finding_id,)).fetchone()
        self.assertEqual(row['confidence'], 'unknown')
        self.assertEqual(row['status'], 'active')
        self.assertIsNone(row['subject_entity_id'])
        self.assertIsNone(row['subject_text'])

    def test_empty_finding_text_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            add_finding(self.path, '   ')

    def test_invalid_confidence_rejected(self):
        with self.assertRaisesRegex(ValueError, 'confidence must be one of'):
            add_finding(self.path, 'text', confidence='guaranteed')

    def test_subject_name_resolves_to_existing_entity(self):
        entity_id = self.entity()
        finding_id = add_finding(self.path, 'Clone shares an identity', subject_name='officerlee')
        row = self.connection.execute('SELECT subject_entity_id FROM findings WHERE id = ?', (finding_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)

    def test_unknown_subject_name_rejected(self):
        with self.assertRaisesRegex(ValueError, "No entity named 'Nobody'"):
            add_finding(self.path, 'text', subject_name='Nobody')

    def test_ambiguous_subject_name_rejected(self):
        self.entity(entity_type='game_object', name='OfficerLee')
        self.entity(entity_type='npc', name='OfficerLee')
        with self.assertRaisesRegex(ValueError, 'matches more than one entity'):
            add_finding(self.path, 'text', subject_name='OfficerLee')

    def test_subject_entity_id_and_subject_name_together_rejected(self):
        with self.assertRaisesRegex(ValueError, 'only one of subject_entity_id or subject_name'):
            add_finding(self.path, 'text', subject_entity_id=1, subject_name='OfficerLee')

    def test_nonexistent_subject_entity_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No entity with id 999'):
            add_finding(self.path, 'text', subject_entity_id=999)

    def test_source_line_without_artifact_rejected(self):
        with self.assertRaisesRegex(ValueError, 'source_line requires source_artifact_id'):
            add_finding(self.path, 'text', source_line=5)

    def test_nonexistent_source_artifact_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No source artifact with id 999'):
            add_finding(self.path, 'text', source_artifact_id=999, source_line=1)

    def test_nonexistent_test_run_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No test run with id 999'):
            add_finding(self.path, 'text', test_run_id=999)

    def test_full_fields_recorded_and_linked(self):
        entity_id = self.entity()
        artifact_id = self.artifact()
        run_id = self.make_run(artifact_id)
        finding_id = add_finding(
            self.path, 'Full evidence chain', confidence='confirmed', subject_entity_id=entity_id,
            subject_text='extra label', source_artifact_id=artifact_id, source_line=42, test_run_id=run_id)
        row = self.connection.execute('SELECT * FROM findings WHERE id = ?', (finding_id,)).fetchone()
        self.assertEqual(row['subject_entity_id'], entity_id)
        self.assertEqual(row['subject_text'], 'extra label')
        self.assertEqual(row['source_artifact_id'], artifact_id)
        self.assertEqual(row['source_line'], 42)
        self.assertEqual(row['test_run_id'], run_id)
        self.assertEqual(row['confidence'], 'confirmed')

    def test_list_findings_joins_entity_name_and_filters_by_status(self):
        entity_id = self.entity()
        add_finding(self.path, 'Linked to an entity', subject_entity_id=entity_id)
        add_finding(self.path, 'Freeform subject only', subject_text='ingestion pipeline')
        rows = list_findings(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['subject_entity_name'], 'OfficerLee')
        self.assertEqual(rows[1]['subject_entity_name'], None)
        self.assertEqual(rows[1]['subject_text'], 'ingestion pipeline')
        second_id = rows[1]['id']
        update_finding_status(self.path, second_id, 'superseded')
        self.assertEqual([r['id'] for r in list_findings(self.path, status='active')], [rows[0]['id']])
        self.assertEqual([r['id'] for r in list_findings(self.path, status='superseded')], [second_id])

    def test_update_status_rejects_unknown_id_and_invalid_status(self):
        finding_id = add_finding(self.path, 'text')
        with self.assertRaisesRegex(ValueError, 'No finding with id 999'):
            update_finding_status(self.path, 999, 'superseded')
        with self.assertRaisesRegex(ValueError, 'status must be one of'):
            update_finding_status(self.path, finding_id, 'archived')
        update_finding_status(self.path, finding_id, 'disproven')
        row = self.connection.execute('SELECT status FROM findings WHERE id = ?', (finding_id,)).fetchone()
        self.assertEqual(row['status'], 'disproven')

    def test_superseded_by_recorded_and_shown_in_list_and_format(self):
        old_id = add_finding(self.path, 'Earlier, now-wrong claim')
        new_id = add_finding(self.path, 'Corrected claim')
        update_finding_status(self.path, old_id, 'superseded', superseded_by_finding_id=new_id)
        row = self.connection.execute(
            'SELECT * FROM findings WHERE id = ?', (old_id,)).fetchone()
        self.assertEqual(row['status'], 'superseded')
        self.assertEqual(row['superseded_by_finding_id'], new_id)
        listed = {r['id']: r for r in list_findings(self.path)}
        self.assertEqual(listed[old_id]['superseded_by_finding_id'], new_id)
        self.assertIn(f'(superseded by #{new_id})', format_finding_row(listed[old_id]))
        self.assertNotIn('superseded by', format_finding_row(listed[new_id]))

    def test_superseded_by_requires_superseded_status(self):
        old_id = add_finding(self.path, 'text')
        new_id = add_finding(self.path, 'other text')
        with self.assertRaisesRegex(ValueError, 'requires status=superseded'):
            update_finding_status(self.path, old_id, 'disproven', superseded_by_finding_id=new_id)

    def test_superseded_by_rejects_nonexistent_finding(self):
        old_id = add_finding(self.path, 'text')
        with self.assertRaisesRegex(ValueError, 'No finding with id 999'):
            update_finding_status(self.path, old_id, 'superseded', superseded_by_finding_id=999)

    def test_superseded_by_rejects_self_reference(self):
        finding_id = add_finding(self.path, 'text')
        with self.assertRaisesRegex(ValueError, 'cannot supersede itself'):
            update_finding_status(self.path, finding_id, 'superseded', superseded_by_finding_id=finding_id)

    def test_superseded_by_cleared_when_status_moves_away_from_superseded(self):
        old_id = add_finding(self.path, 'text')
        new_id = add_finding(self.path, 'other text')
        update_finding_status(self.path, old_id, 'superseded', superseded_by_finding_id=new_id)
        update_finding_status(self.path, old_id, 'active')
        row = self.connection.execute(
            'SELECT status, superseded_by_finding_id FROM findings WHERE id = ?', (old_id,)).fetchone()
        self.assertEqual(row['status'], 'active')
        self.assertIsNone(row['superseded_by_finding_id'])

    def test_cli_update_status_with_superseded_by(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--finding', 'Old claim']), 0)
            self.assertEqual(main(['--database', str(self.path), 'add', '--finding', 'New claim']), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'update-status', '1', 'superseded',
                                    '--superseded-by', '2']), 0)
        self.assertIn('set to superseded (superseded by 2)', output.getvalue())

    def test_cli_add_list_and_update_status(self):
        self.entity()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--finding', 'CLI recorded finding',
                                    '--subject-name', 'OfficerLee', '--confidence', 'strong']), 0)
        self.assertIn('Recorded finding', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('CLI recorded finding', output.getvalue())
        self.assertIn('OfficerLee', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'update-status', '1', 'superseded']), 0)
        self.assertIn('set to superseded', output.getvalue())

    def test_cli_empty_list_and_error_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No findings recorded.', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--finding', 'x',
                                    '--subject-entity-id', '999']), 2)
        self.assertIn('Error:', error.getvalue())

        # argparse itself enforces --subject-entity-id/--subject-name mutual exclusion
        # before main()'s own try/except runs, so it exits directly rather than returning 2.
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--database', str(self.path), 'add', '--finding', 'x',
                  '--subject-entity-id', '1', '--subject-name', 'OfficerLee'])
        self.assertEqual(raised.exception.code, 2)

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db'), 'list']), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
