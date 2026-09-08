"""Manual experiments CLI tests; every database is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

from database import db
from database.record_experiment import add_experiment, list_experiments, main


def run_git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)


class RecordExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def make_run(self):
        with self.connection:
            artifact_id = self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES ('log', 'a.log', 'a.log')"
            ).lastrowid
            return self.connection.execute(
                'INSERT INTO test_runs (source_artifact_id, profile) VALUES (?, ?)', (artifact_id, 'audit')).lastrowid

    def test_add_with_minimal_fields(self):
        experiment_id = add_experiment(self.path, 'Does X change during Y?', 'Not observed yet.')
        row = self.connection.execute('SELECT * FROM experiments WHERE id=?', (experiment_id,)).fetchone()
        self.assertEqual(row['question'], 'Does X change during Y?')
        self.assertEqual(row['observed_result'], 'Not observed yet.')
        self.assertIsNone(row['source_artifact_id'])
        self.assertIsNone(row['test_run_id'])
        self.assertIsNone(row['mod_build'])

    def test_empty_question_rejected(self):
        with self.assertRaisesRegex(ValueError, 'question must not be empty'):
            add_experiment(self.path, '   ', 'result')

    def test_empty_observed_result_rejected(self):
        with self.assertRaisesRegex(ValueError, 'observed_result must not be empty'):
            add_experiment(self.path, 'question', '   ')

    def test_symbols_are_recorded_and_queryable(self):
        experiment_id = add_experiment(self.path, 'q', 'r', symbols=('NPC.EnterBuilding', 'ActiveSelf'))
        symbols = {row[0] for row in self.connection.execute(
            'SELECT symbol FROM experiment_symbols WHERE experiment_id=?', (experiment_id,))}
        self.assertEqual(symbols, {'NPC.EnterBuilding', 'ActiveSelf'})

    def test_test_run_id_must_exist(self):
        with self.assertRaisesRegex(ValueError, 'No test run with id 999'):
            add_experiment(self.path, 'q', 'r', test_run_id=999)

    def test_test_run_id_links_when_valid(self):
        run_id = self.make_run()
        experiment_id = add_experiment(self.path, 'q', 'r', test_run_id=run_id)
        row = self.connection.execute('SELECT test_run_id FROM experiments WHERE id=?', (experiment_id,)).fetchone()
        self.assertEqual(row[0], run_id)

    def test_source_log_is_registered_and_deduped(self):
        log = self.root / 'session.log'
        log.write_text('some log content', encoding='utf-8')
        first = add_experiment(self.path, 'q1', 'r1', source_log=log)
        second = add_experiment(self.path, 'q2', 'r2', source_log=log)
        row1, row2 = (self.connection.execute('SELECT source_artifact_id FROM experiments WHERE id=?', (i,)).fetchone()
                     for i in (first, second))
        self.assertIsNotNone(row1[0])
        self.assertEqual(row1[0], row2[0])
        artifact_type = self.connection.execute(
            'SELECT artifact_type FROM source_artifacts WHERE id=?', (row1[0],)).fetchone()[0]
        self.assertEqual(artifact_type, 'log')

    def test_source_log_brief_text_registers_as_research_note(self):
        brief = self.root / 'Latest_brief.txt'
        brief.write_text('summary of the session', encoding='utf-8')
        experiment_id = add_experiment(self.path, 'q', 'r', source_log=brief)
        artifact_id = self.connection.execute(
            'SELECT source_artifact_id FROM experiments WHERE id=?', (experiment_id,)).fetchone()[0]
        artifact_type = self.connection.execute(
            'SELECT artifact_type FROM source_artifacts WHERE id=?', (artifact_id,)).fetchone()[0]
        self.assertEqual(artifact_type, 'research_note')

    def test_missing_source_log_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'is not a file'):
            add_experiment(self.path, 'q', 'r', source_log=self.root / 'missing.log')

    def test_mod_repo_records_git_revision(self):
        repo = self.root / 'mod-repo'
        repo.mkdir()
        run_git(repo, 'init', '-q')
        run_git(repo, 'config', 'user.email', 'test@example.com')
        run_git(repo, 'config', 'user.name', 'Test')
        (repo / 'a.cs').write_text('// a', encoding='utf-8')
        run_git(repo, 'add', 'a.cs')
        run_git(repo, 'commit', '-q', '-m', 'initial')
        sha = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'],
                             check=True, capture_output=True, text=True).stdout.strip()
        experiment_id = add_experiment(self.path, 'q', 'r', mod_repo=repo)
        row = self.connection.execute('SELECT mod_build FROM experiments WHERE id=?', (experiment_id,)).fetchone()
        self.assertEqual(row[0], sha)

    def test_list_all_and_filtered_by_symbol(self):
        add_experiment(self.path, 'q1', 'r1', symbols=('GoonPool',))
        add_experiment(self.path, 'q2', 'r2', symbols=('CartelGoon',))
        self.assertEqual(len(list_experiments(self.path)), 2)
        filtered = list_experiments(self.path, symbol='GoonPool')
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['question'], 'q1')

    def test_requires_schema_v5(self):
        from phase4_fixtures import create_v1
        v1_path = self.root / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            add_experiment(v1_path, 'q', 'r')


class RecordExperimentCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)

    def test_cli_add_and_list(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--question', 'Does X happen?',
                                   '--observed-result', 'Yes, observed once.', '--symbols', 'NPC.EnterBuilding']), 0)
        self.assertIn('Recorded experiment 1.', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        text = output.getvalue()
        self.assertIn('NPC.EnterBuilding', text)
        self.assertIn('Does X happen?', text)

    def test_cli_list_empty(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No experiments recorded.', output.getvalue())

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--question', '   ',
                                   '--observed-result', 'r']), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
