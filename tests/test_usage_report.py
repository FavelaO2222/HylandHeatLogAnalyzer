"""Tests for database.usage_report: agent_usage summaries and the
compact-packet-vs-raw-content token estimate comparison."""

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from database import db, usage_report
from database.record_usage import add_usage


class UsageReportFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def document(self, relative_path, content, artifact_type='source_code'):
        with self.connection:
            artifact_id = self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, 'repo', 'repo')",
                (artifact_type,)).lastrowid
            return self.connection.execute(
                '''INSERT INTO source_documents (source_artifact_id, relative_path, language, content)
                   VALUES (?, ?, 'csharp', ?)''', (artifact_id, relative_path, content)).lastrowid


class SummarizeTests(UsageReportFixture, unittest.TestCase):
    def test_empty_database(self):
        result = usage_report.summarize(self.path)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['totals'], {'calls': 0, 'input_tokens': 0, 'output_tokens': 0,
                                            'cost_usd': None, 'unknown_cost_calls': 0})

    def test_groups_by_agent_and_model(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 100, 20, cost_usd=0.01)
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 200, 40, cost_usd=0.02)
        add_usage(self.path, 'ChatGPT', 'gpt-4o', 300, 60)
        result = usage_report.summarize(self.path)
        self.assertEqual(len(result['groups']), 2)
        rider = next(g for g in result['groups'] if g['agent'] == 'Rider')
        self.assertEqual(rider['calls'], 2)
        self.assertEqual(rider['input_tokens'], 300)
        self.assertEqual(rider['output_tokens'], 60)
        self.assertAlmostEqual(rider['cost_usd'], 0.03)
        self.assertEqual(rider['unknown_cost_calls'], 0)
        chatgpt = next(g for g in result['groups'] if g['agent'] == 'ChatGPT')
        self.assertIsNone(chatgpt['cost_usd'])
        self.assertEqual(chatgpt['unknown_cost_calls'], 1)

    def test_totals_sum_across_groups_and_track_unknown_cost(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 100, 20, cost_usd=0.01)
        add_usage(self.path, 'ChatGPT', 'gpt-4o', 300, 60)
        totals = usage_report.summarize(self.path)['totals']
        self.assertEqual(totals['calls'], 2)
        self.assertEqual(totals['input_tokens'], 400)
        self.assertEqual(totals['output_tokens'], 80)
        self.assertAlmostEqual(totals['cost_usd'], 0.01)
        self.assertEqual(totals['unknown_cost_calls'], 1)

    def test_filters_by_agent_model_and_time_range(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 1, 1, occurred_at='2026-09-01T00:00:00.000Z')
        add_usage(self.path, 'Rider', 'gpt-4o', 1, 1, occurred_at='2026-09-05T00:00:00.000Z')
        add_usage(self.path, 'ChatGPT', 'gpt-4o', 1, 1, occurred_at='2026-09-09T00:00:00.000Z')
        self.assertEqual(len(usage_report.summarize(self.path, agent='Rider')['groups']), 2)
        self.assertEqual(len(usage_report.summarize(self.path, model='gpt-4o')['groups']), 2)
        self.assertEqual(len(usage_report.summarize(self.path, since='2026-09-05T00:00:00.000Z')['groups']), 2)
        self.assertEqual(len(usage_report.summarize(self.path, until='2026-09-01T00:00:00.000Z')['groups']), 1)

    def test_format_summary_reports_unknown_cost_and_totals(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 100, 20, cost_usd=0.01)
        add_usage(self.path, 'ChatGPT', 'gpt-4o', 300, 60)
        text = usage_report.format_summary(usage_report.summarize(self.path))
        self.assertIn('Rider/claude-sonnet-5', text)
        self.assertIn('unknown cost', text)
        self.assertIn('Total: 2 call(s)', text)

    def test_format_summary_empty(self):
        text = usage_report.format_summary(usage_report.summarize(self.path))
        self.assertIn('No usage recorded.', text)


class ComparePacketVsRawTests(UsageReportFixture, unittest.TestCase):
    def test_no_matches_reports_zero_raw_and_no_reduction(self):
        result = usage_report.compare_packet_vs_raw(self.path, 'NoSuchSymbolAnywhere')
        self.assertEqual(result['raw_tokens_estimate'], 0)
        self.assertIsNone(result['reduction_pct_estimate'])
        self.assertGreater(result['packet_tokens_estimate'], 0)

    def test_raw_content_is_larger_than_the_compact_packet(self):
        self.document('HylandHeat/Debug/GoonPoolDebug.cs',
                      'GoonPool pool;\n' + ('// filler context line\n' * 200) +
                      'public class GoonPoolDebug { public static void Observe(GoonPool pool) {} }')
        result = usage_report.compare_packet_vs_raw(self.path, 'GoonPool')
        self.assertGreater(result['raw_tokens_estimate'], result['packet_tokens_estimate'])
        self.assertGreater(result['reduction_pct_estimate'], 0)
        self.assertEqual(result['matched_items'], 1)

    def test_respects_limit(self):
        for i in range(3):
            self.document(f'HylandHeat/Debug/GoonPoolDebug{i}.cs',
                          f'public class GoonPoolDebug{i} {{ public static void Observe(GoonPool pool) {{}} }}')
        result = usage_report.compare_packet_vs_raw(self.path, 'GoonPool', limit=2)
        self.assertLessEqual(result['matched_items'], 2)

    def test_requires_schema_v7(self):
        from phase4_fixtures import create_v1
        v1_path = Path(tempfile.mkdtemp()) / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            usage_report.compare_packet_vs_raw(v1_path, 'anything')


class UsageReportCliTests(UsageReportFixture, unittest.TestCase):
    def test_cli_summary_text_and_json(self):
        add_usage(self.path, 'Rider', 'claude-sonnet-5', 100, 20, cost_usd=0.01)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(usage_report.main(['--database', str(self.path), 'summary']), 0)
        self.assertIn('Rider/claude-sonnet-5', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(usage_report.main(['--database', str(self.path), 'summary', '--format', 'json']), 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed['groups'][0]['agent'], 'Rider')

    def test_cli_compare_text_and_json(self):
        self.document('HylandHeat/Debug/GoonPoolDebug.cs',
                      'public class GoonPoolDebug { public static void Observe(GoonPool pool) {} }')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(usage_report.main(['--database', str(self.path), 'compare', 'GoonPool']), 0)
        self.assertIn('estimate only', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                usage_report.main(['--database', str(self.path), 'compare', 'GoonPool', '--format', 'json']), 0)
        parsed = json.loads(output.getvalue())
        self.assertIn('packet_tokens_estimate', parsed)


if __name__ == '__main__':
    unittest.main()
