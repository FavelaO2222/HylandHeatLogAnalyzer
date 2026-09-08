"""Tests for database.search: FTS5 search, trigger sync, rebuild, and the CLI."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from database import db, search
from phase4_fixtures import create_v1


class SearchFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def artifact(self):
        with self.connection:
            return self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES ('log', 'a.log', 'a.log')"
            ).lastrowid

    def run_(self, artifact_id):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO test_runs (source_artifact_id, profile) VALUES (?, ?)',
                (artifact_id, 'audit')).lastrowid

    def event(self, run_id, artifact_id, message, *, component=None, category='diagnostic', source_line=None):
        with self.connection:
            return self.connection.execute(
                '''INSERT INTO events (test_run_id, source_artifact_id, category, component, message, source_line)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (run_id, artifact_id, category, component, message, source_line)).lastrowid

    def error(self, run_id, artifact_id, message, *, severity='ERROR', stack_trace=None, source_line=None):
        with self.connection:
            return self.connection.execute(
                '''INSERT INTO errors (test_run_id, source_artifact_id, severity, message, stack_trace, source_line)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (run_id, artifact_id, severity, message, stack_trace, source_line)).lastrowid

    def document(self, relative_path, content, *, language='csharp'):
        with self.connection:
            artifact_id = self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES ('source_code', 'repo', 'repo')"
            ).lastrowid
            return self.connection.execute(
                '''INSERT INTO source_documents (source_artifact_id, relative_path, language, content)
                   VALUES (?, ?, ?, ?)''', (artifact_id, relative_path, language, content)).lastrowid


class SearchFunctionTests(SearchFixture, unittest.TestCase):
    def test_dotted_symbol_query_does_not_raise_fts5_syntax_error(self):
        # Regression test: a bare "." between word characters used to reach
        # FTS5 as literal query syntax ("fts5: syntax error near \".\"") for
        # any dotted C# symbol, e.g. "NPC.EnterBuilding" -- every one of the
        # priority research symbols contains one.
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'NPC EnterBuilding invoked for officerlee2')
        result = search.search(self.path, 'NPC.EnterBuilding', type='events')
        self.assertEqual(len(result['items']), 1)

    def test_dotted_symbol_still_matches_documents_split_across_two_words(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'PoliceStation registry updated for PullOfficer call')
        result = search.search(self.path, 'PoliceStation.PullOfficer', type='events')
        self.assertEqual(len(result['items']), 1)

    def test_quoted_phrase_and_boolean_syntax_are_left_untouched(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'exact phrase match here')
        result = search.search(self.path, '"exact phrase"', type='events')
        self.assertEqual(len(result['items']), 1)

    def test_finds_matching_events_and_errors_ranked_by_relevance(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'Physics collider desynchronized', source_line=10)
        self.event(run_id, artifact_id, 'Totally unrelated event', source_line=11)
        self.error(run_id, artifact_id, 'Physics collider raised an exception', source_line=12)

        result = search.search(self.path, 'collider')
        self.assertEqual(result['query'], 'collider')
        self.assertIsNone(result['type'])
        self.assertFalse(result['truncated'])
        sources = {(item['source'], item['id']) for item in result['items']}
        self.assertEqual(sources, {('event', 1), ('error', 1)})
        for item in result['items']:
            self.assertEqual(item['test_run_id'], run_id)
            self.assertIn('llider', item['snippet'])  # snippet contains the matched term

    def test_type_filter_restricts_to_one_table(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'shared keyword event')
        self.error(run_id, artifact_id, 'shared keyword error')
        self.document('Foo.cs', 'class Foo { void shared_keyword_method() {} }')

        events_only = search.search(self.path, 'shared', type='events')
        self.assertEqual([item['source'] for item in events_only['items']], ['event'])
        errors_only = search.search(self.path, 'shared', type='errors')
        self.assertEqual([item['source'] for item in errors_only['items']], ['error'])
        documents_only = search.search(self.path, 'shared', type='documents')
        self.assertEqual([item['source'] for item in documents_only['items']], ['document'])

    def test_documents_are_found_with_relative_path_and_no_run_context(self):
        self.document('HylandHeat/Police/PoliceOfficer.cs',
                      'public void BeginFootPursuit_Networked(string playerCode) { }')
        result = search.search(self.path, 'BeginFootPursuit', type='documents')
        item = result['items'][0]
        self.assertEqual(item['source'], 'document')
        self.assertEqual(item['relative_path'], 'HylandHeat/Police/PoliceOfficer.cs')
        self.assertIsNone(item['test_run_id'])
        self.assertIsNone(item['source_line'])
        self.assertIn('BeginFootPursuit', item['snippet'])

    def test_events_and_errors_have_no_relative_path(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'a searchable event')
        item = search.search(self.path, 'searchable', type='events')['items'][0]
        self.assertIsNone(item['relative_path'])

    def test_no_match_returns_empty_items(self):
        self.artifact()
        result = search.search(self.path, 'nonexistentterm')
        self.assertEqual(result['items'], [])
        self.assertFalse(result['truncated'])

    def test_results_are_bounded_and_report_truncation(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        for i in range(5):
            self.event(run_id, artifact_id, f'repeated keyword occurrence {i}')

        result = search.search(self.path, 'keyword', limit=3)
        self.assertEqual(len(result['items']), 3)
        self.assertTrue(result['truncated'])

        result_all = search.search(self.path, 'keyword', limit=5)
        self.assertEqual(len(result_all['items']), 5)
        self.assertFalse(result_all['truncated'])

    def test_source_run_and_line_are_included_for_traceability(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'traceable keyword', source_line=42)
        item = search.search(self.path, 'traceable')['items'][0]
        self.assertEqual(item['test_run_id'], run_id)
        self.assertEqual(item['source_line'], 42)

    def test_empty_query_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            search.search(self.path, '   ')

    def test_invalid_type_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'type must be one of'):
            search.search(self.path, 'x', type='bogus')

    def test_limit_out_of_range_is_rejected(self):
        self.artifact()
        for limit in (0, -1, 101, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                search.search(self.path, 'x', limit=limit)

    def test_malformed_fts5_query_syntax_is_a_clean_value_error(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'anything')
        with self.assertRaisesRegex(ValueError, 'Invalid search query syntax'):
            search.search(self.path, '"unterminated phrase')

    def test_triggers_keep_the_index_in_sync_on_insert_update_delete(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        event_id = self.event(run_id, artifact_id, 'original wording')
        self.assertEqual(len(search.search(self.path, 'original')['items']), 1)

        with self.connection:
            self.connection.execute('UPDATE events SET message=? WHERE id=?', ('revised wording', event_id))
        self.assertEqual(search.search(self.path, 'original')['items'], [])
        self.assertEqual(len(search.search(self.path, 'revised')['items']), 1)

        with self.connection:
            self.connection.execute('DELETE FROM events WHERE id=?', (event_id,))
        self.assertEqual(search.search(self.path, 'revised')['items'], [])

    def test_rebuild_repairs_an_index_cleared_out_from_under_it(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'rebuildable keyword')
        self.error(run_id, artifact_id, 'rebuildable keyword too')
        with self.connection:
            self.connection.execute("INSERT INTO events_fts(events_fts) VALUES('delete-all')")
            self.connection.execute("INSERT INTO errors_fts(errors_fts) VALUES('delete-all')")
        self.assertEqual(search.search(self.path, 'rebuildable')['items'], [])

        summary = search.rebuild_search_index(self.path)
        self.assertEqual(summary, {'events_indexed': 1, 'errors_indexed': 1, 'documents_indexed': 0})
        result = search.search(self.path, 'rebuildable')
        self.assertEqual({item['source'] for item in result['items']}, {'event', 'error'})

    def test_search_and_rebuild_require_schema_v3(self):
        v1_path = Path(self.temp.name) / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            search.search(v1_path, 'Shared')
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            search.rebuild_search_index(v1_path)


class SearchCliTests(SearchFixture, unittest.TestCase):
    def seed_one(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'cli findable keyword', source_line=7)
        return run_id

    def test_text_output(self):
        run_id = self.seed_one()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(search.main(['--database', str(self.path), 'findable']), 0)
        text = output.getvalue()
        self.assertIn('1 result(s)', text)
        self.assertIn(f'run #{run_id}', text)
        self.assertIn('line 7', text)

    def test_text_output_for_documents_shows_relative_path(self):
        self.document('Foo.cs', 'void FindableMethod() {}')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(search.main(['--database', str(self.path), 'FindableMethod', '--type', 'documents']), 0)
        text = output.getvalue()
        self.assertIn('1 result(s)', text)
        self.assertIn('Foo.cs', text)

    def test_json_output(self):
        self.seed_one()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(search.main(['--database', str(self.path), 'findable', '--format', 'json']), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['source'], 'event')

    def test_type_and_limit_flags(self):
        self.seed_one()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                search.main(['--database', str(self.path), 'findable', '--type', 'errors', '--limit', '5']), 0)
        self.assertIn('0 result(s)', output.getvalue())

    def test_no_match_reports_no_matches(self):
        self.artifact()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(search.main(['--database', str(self.path), 'nothingatall']), 0)
        self.assertIn('no matches', output.getvalue())

    def test_rebuild_flag(self):
        self.seed_one()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(search.main(['--database', str(self.path), '--rebuild']), 0)
        self.assertIn('Rebuilt search index: 1 events, 0 errors, 0 documents indexed.', output.getvalue())

    def test_query_and_rebuild_together_is_an_error(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(search.main(['--database', str(self.path), 'x', '--rebuild']), 2)
        self.assertIn('Error:', error.getvalue())

    def test_missing_query_without_rebuild_is_an_error(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(search.main(['--database', str(self.path)]), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_invalid_type_choice_is_rejected_by_argparse(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as ctx:
            search.main(['--database', str(self.path), 'x', '--type', 'bogus'])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
