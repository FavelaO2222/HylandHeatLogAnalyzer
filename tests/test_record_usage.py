"""Manual agent_usage CLI tests; every database is temporary."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from database import db
from database.record_usage import add_usage, list_usage, main


class RecordUsageTests(unittest.TestCase):
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

    def make_experiment(self):
        with self.connection:
            return self.connection.execute(
                "INSERT INTO experiments (question, observed_result) VALUES ('q', 'r')").lastrowid

    def test_add_with_minimal_fields(self):
        usage_id = add_usage(self.path, 'Rider', 'claude-sonnet-5', 1500, 300)
        row = self.connection.execute('SELECT * FROM agent_usage WHERE id=?', (usage_id,)).fetchone()
        self.assertEqual(row['agent'], 'Rider')
        self.assertEqual(row['model'], 'claude-sonnet-5')
        self.assertEqual(row['input_tokens'], 1500)
        self.assertEqual(row['output_tokens'], 300)
        self.assertIsNone(row['cost_usd'])
        self.assertIsNone(row['retrieval_mode'])
        self.assertIsNone(row['experiment_id'])
        self.assertIsNone(row['test_run_id'])
        self.assertIsNone(row['source_artifact_id'])
        self.assertIsNotNone(row['occurred_at'])

    def test_empty_agent_rejected(self):
        with self.assertRaisesRegex(ValueError, 'agent must not be empty'):
            add_usage(self.path, '  ', 'model', 1, 1)

    def test_empty_model_rejected(self):
        with self.assertRaisesRegex(ValueError, 'model must not be empty'):
            add_usage(self.path, 'agent', '  ', 1, 1)

    def test_negative_input_tokens_rejected(self):
        with self.assertRaisesRegex(ValueError, 'input_tokens must be a nonnegative integer'):
            add_usage(self.path, 'agent', 'model', -1, 1)

    def test_negative_output_tokens_rejected(self):
        with self.assertRaisesRegex(ValueError, 'output_tokens must be a nonnegative integer'):
            add_usage(self.path, 'agent', 'model', 1, -1)

    def test_non_integer_tokens_rejected(self):
        with self.assertRaisesRegex(ValueError, 'input_tokens must be a nonnegative integer'):
            add_usage(self.path, 'agent', 'model', 1.5, 1)

    def test_negative_cost_rejected(self):
        with self.assertRaisesRegex(ValueError, 'cost_usd must be nonnegative'):
            add_usage(self.path, 'agent', 'model', 1, 1, cost_usd=-0.01)

    def test_invalid_retrieval_mode_rejected(self):
        with self.assertRaisesRegex(ValueError, 'retrieval_mode must be one of'):
            add_usage(self.path, 'agent', 'model', 1, 1, retrieval_mode='raw_json')

    def test_multiple_links_rejected(self):
        run_id = self.make_run()
        experiment_id = self.make_experiment()
        with self.assertRaisesRegex(ValueError, 'at most one of'):
            add_usage(self.path, 'agent', 'model', 1, 1, test_run_id=run_id, experiment_id=experiment_id)

    def test_nonexistent_experiment_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No experiment with id 999'):
            add_usage(self.path, 'agent', 'model', 1, 1, experiment_id=999)

    def test_nonexistent_test_run_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No test run with id 999'):
            add_usage(self.path, 'agent', 'model', 1, 1, test_run_id=999)

    def test_nonexistent_source_artifact_id_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No source artifact with id 999'):
            add_usage(self.path, 'agent', 'model', 1, 1, source_artifact_id=999)

    def test_full_fields_recorded(self):
        run_id = self.make_run()
        usage_id = add_usage(
            self.path, 'Rider', 'claude-sonnet-5', 1500, 300, cost_usd=0.0117,
            retrieval_mode='compact_packet', query_text='NPC.EnterBuilding', test_run_id=run_id,
            note='via database.research', occurred_at='2026-09-09T12:00:00.000Z')
        row = self.connection.execute('SELECT * FROM agent_usage WHERE id=?', (usage_id,)).fetchone()
        self.assertEqual(row['cost_usd'], 0.0117)
        self.assertEqual(row['retrieval_mode'], 'compact_packet')
        self.assertEqual(row['query_text'], 'NPC.EnterBuilding')
        self.assertEqual(row['test_run_id'], run_id)
        self.assertEqual(row['note'], 'via database.research')
        self.assertEqual(row['occurred_at'], '2026-09-09T12:00:00.000Z')

    def test_list_all_and_filtered(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 100, 20)
        add_usage(self.path, 'ChatGPT', 'gpt-4o', 200, 40)
        self.assertEqual(len(list_usage(self.path)), 2)
        filtered = list_usage(self.path, agent='Rider')
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['model'], 'claude-sonnet-5')
        filtered = list_usage(self.path, model='gpt-4o')
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['agent'], 'ChatGPT')

    def test_list_filtered_by_time_range(self):
        add_usage(self.path, 'agent', 'model', 1, 1, occurred_at='2026-09-01T00:00:00.000Z')
        add_usage(self.path, 'agent', 'model', 1, 1, occurred_at='2026-09-08T00:00:00.000Z')
        self.assertEqual(len(list_usage(self.path, since='2026-09-05T00:00:00.000Z')), 1)
        self.assertEqual(len(list_usage(self.path, until='2026-09-05T00:00:00.000Z')), 1)

    def test_requires_schema_v7(self):
        from phase4_fixtures import create_v1
        v1_path = self.root / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            add_usage(v1_path, 'agent', 'model', 1, 1)


class RecordUsageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)

    def test_cli_add_and_list(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'add', '--agent', 'Rider',
                                   '--model', 'claude-sonnet-5', '--input-tokens', '1500',
                                   '--output-tokens', '300', '--cost-usd', '0.0117']), 0)
        self.assertIn('Recorded usage 1.', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        text = output.getvalue()
        self.assertIn('Rider/claude-sonnet-5', text)
        self.assertIn('1500in+300out', text)

    def test_cli_list_empty(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--database', str(self.path), 'list']), 0)
        self.assertIn('No usage recorded.', output.getvalue())

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(main(['--database', str(self.path), 'add', '--agent', '  ',
                                   '--model', 'm', '--input-tokens', '1', '--output-tokens', '1']), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_cli_mutually_exclusive_links(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--database', str(self.path), 'add', '--agent', 'a', '--model', 'm',
                 '--input-tokens', '1', '--output-tokens', '1', '--experiment-id', '1', '--test-run-id', '1'])
        self.assertEqual(raised.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
