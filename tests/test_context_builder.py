"""Phase 3 context builder tests; every database is temporary and read-only queried."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db
from database.context_builder import build_context, main


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'

    def open_database(self):
        db.initialize_database(self.path)
        connection = db.connect_database(self.path)
        self.addCleanup(connection.close)
        return connection

    def artifact(self, connection, path='logs/session.log'):
        return connection.execute(
            'INSERT INTO source_artifacts (artifact_type, path, filename, sha256) VALUES (?, ?, ?, ?)',
            ('log', path, Path(path).name, 'a' * 64)).lastrowid

    def add_run(self, connection, artifact_id, profile='read-only-audit', result='NEEDS_ATTENTION'):
        return connection.execute(
            'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
            (artifact_id, profile, result)).lastrowid

    def test_no_runs_raises(self):
        self.open_database()
        with self.assertRaisesRegex(ValueError, 'No test runs'):
            build_context(self.path)

    def test_unknown_run_id_raises(self):
        connection = self.open_database()
        with connection:
            self.add_run(connection, self.artifact(connection))
        with self.assertRaisesRegex(ValueError, 'No test run with id 999'):
            build_context(self.path, run_id=999)

    def test_defaults_to_latest_run_and_specific_run_selectable(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            first = self.add_run(connection, artifact_id, profile='general')
            second = self.add_run(connection, artifact_id, profile='swat-deployment')
        self.assertIn(f'Run #{second} (swat-deployment)', build_context(self.path))
        self.assertIn(f'Run #{first} (general)', build_context(self.path, run_id=first))

    def test_empty_sections_show_placeholders(self):
        connection = self.open_database()
        with connection:
            self.add_run(connection, self.artifact(connection))
        text = build_context(self.path)
        self.assertIn('## Errors (0)', text)
        self.assertIn('## Notable Events (top 0 of 0)', text)
        self.assertIn('## Open Unknowns\n(none recorded yet)', text)
        self.assertIn('## Findings\n(none recorded yet)', text)
        self.assertIn('## Decisions\n(none recorded yet)', text)

    def test_errors_ranked_by_severity_then_line(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            connection.execute(
                'INSERT INTO errors (test_run_id, source_artifact_id, severity, message, source_line) '
                'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'ERROR', 'Early error', 10))
            connection.execute(
                'INSERT INTO errors (test_run_id, source_artifact_id, severity, message, source_line) '
                'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'FATAL', 'Later fatal', 50))
        text = build_context(self.path)
        self.assertLess(text.index('Later fatal'), text.index('Early error'))

    def test_events_ranked_by_category_priority_then_line(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            connection.execute(
                'INSERT INTO events (test_run_id, source_artifact_id, category, message, source_line) '
                'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'patrol', 'Low priority chatter', 5))
            connection.execute(
                'INSERT INTO events (test_run_id, source_artifact_id, category, message, source_line) '
                'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'goon_transition', 'High priority transition', 20))
        text = build_context(self.path)
        self.assertLess(text.index('High priority transition'), text.index('Low priority chatter'))
        self.assertIn('## Notable Events (top 2 of 2)', text)

    def test_max_events_caps_shown_count_but_reports_total(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            for line in range(1, 6):
                connection.execute(
                    'INSERT INTO events (test_run_id, source_artifact_id, category, message, source_line) '
                    'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'patrol', f'Event {line}', line))
        text = build_context(self.path, max_events=2)
        self.assertIn('## Notable Events (top 2 of 5)', text)
        self.assertIn('Event 1', text)
        self.assertIn('Event 2', text)
        self.assertNotIn('Event 3', text)

    def test_research_rows_rendered_with_entity_labels_and_ranking(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            entity_id = connection.execute(
                'INSERT INTO entities (entity_type, name) VALUES (?, ?)', ('npc', 'OfficerLee')).lastrowid
            connection.execute(
                'INSERT INTO findings (subject_entity_id, finding, confidence, test_run_id, source_artifact_id) '
                'VALUES (?, ?, ?, ?, ?)', (entity_id, 'Tentative note', 'tentative', run_id, artifact_id))
            connection.execute(
                'INSERT INTO findings (finding, confidence) VALUES (?, ?)', ('Confirmed note', 'confirmed'))
            connection.execute(
                'INSERT INTO findings (finding, status) VALUES (?, ?)', ('Superseded note', 'superseded'))
            connection.execute(
                'INSERT INTO unknowns (subject_entity_id, question, importance) VALUES (?, ?, ?)',
                (entity_id, 'Is this repeatable?', 'critical'))
            connection.execute(
                'INSERT INTO unknowns (question, status) VALUES (?, ?)', ('Resolved question', 'resolved'))
            connection.execute(
                'INSERT INTO decisions (topic, decision, reason) VALUES (?, ?, ?)',
                ('scope', 'Defer', 'Insufficient evidence'))
        text = build_context(self.path)
        self.assertIn('[confirmed] Confirmed note', text)
        self.assertIn('[tentative] (OfficerLee) Tentative note', text)
        self.assertNotIn('Superseded note', text)
        self.assertIn('[critical] (OfficerLee) Is this repeatable?', text)
        self.assertNotIn('Resolved question', text)
        self.assertIn('scope: Defer — Insufficient evidence', text)
        self.assertLess(text.index('Confirmed note'), text.index('Tentative note'))

    def test_max_chars_budget_truncates_and_appends_footer(self):
        connection = self.open_database()
        with connection:
            artifact_id = self.artifact(connection)
            run_id = self.add_run(connection, artifact_id)
            for line in range(1, 20):
                connection.execute(
                    'INSERT INTO events (test_run_id, source_artifact_id, category, message, source_line) '
                    'VALUES (?, ?, ?, ?, ?)', (run_id, artifact_id, 'patrol', f'Event number {line}', line))
        text = build_context(self.path, max_chars=400, max_events=19)
        self.assertLessEqual(len(text), 400)
        self.assertIn('Budget-truncated selection', text)
        self.assertNotIn('Event number 19', text)

    def test_no_truncation_footer_when_everything_fits(self):
        connection = self.open_database()
        with connection:
            self.add_run(connection, self.artifact(connection))
        self.assertNotIn('Budget-truncated', build_context(self.path))

    def test_read_only_and_missing_database_raises(self):
        connection = self.open_database()
        with connection:
            self.add_run(connection, self.artifact(connection))
        build_context(self.path)
        with closing(db.connect_database(self.path, read_only=True)) as reader:
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute('DELETE FROM test_runs')
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaises(sqlite3.Error):
            build_context(missing)

    def test_cli_success_and_error_paths(self):
        connection = self.open_database()
        with connection:
            run_id = self.add_run(connection, self.artifact(connection))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), '--run', str(run_id)]), 0)
        self.assertIn(f'Run #{run_id}', output.getvalue())

        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(Path(self.temp.name) / 'missing.db')]), 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
