import json
import sqlite3
import subprocess
import sys
import unittest

from database import db
from database.ingestion import IMPORTER
from database.run_comparison import compare_runs, format_comparison, TEXT_LIMIT
from phase4_fixtures import ResearchFixture


class RunComparisonTests(ResearchFixture, unittest.TestCase):
    def compare(self, a=1, b=2, **kwargs):
        return compare_runs(a, b, self.path, **kwargs)

    def test_identical_run_and_equivalent_rows_ignore_ids_lines_and_timestamps(self):
        with self.connection:
            self.connection.execute("UPDATE events SET timestamp='12:00:00' WHERE id=2")
        for a, b in ((1, 1), (2, 2), (1, 2)):
            result = self.compare(a, b)
            self.assertTrue(result['identical'])
            self.assertIn('No differences', format_comparison(result))

    def test_metadata_and_verdict_differences(self):
        with self.connection:
            self.connection.execute("UPDATE test_runs SET profile='general',result='FAIL',game_build='new',mod_build='v2' WHERE id=2")
        result = self.compare()
        fields = {item['field']: (item['a'], item['b']) for item in result['metadata']['items']}
        self.assertEqual(fields['result'], ('NEEDS_ATTENTION', 'FAIL'))
        self.assertEqual(fields['game_build'], (None, 'new'))
        self.assertEqual(fields['mod_build'], (None, 'v2'))
        self.assertFalse(result['identical'])

    def test_asymmetric_errors_and_stack_variants(self):
        with self.connection:
            self.connection.execute("INSERT INTO errors(test_run_id,source_artifact_id,severity,message) VALUES (1,1,'ERROR','removed')")
            self.connection.execute("INSERT INTO errors(test_run_id,source_artifact_id,severity,message) VALUES (2,1,'FATAL','added')")
            self.connection.execute("UPDATE errors SET stack_trace='at Changed()' WHERE id=2")
        result = self.compare()
        self.assertEqual(result['errors']['added']['total'], 2)
        self.assertEqual(result['errors']['removed']['total'], 2)
        self.assertEqual(result['errors']['added']['items'][0]['severity'], 'FATAL')
        self.assertEqual(result['errors']['count_changes']['total'], 0)

    def test_error_counts_are_row_counts(self):
        with self.connection:
            self.connection.execute("INSERT INTO errors(test_run_id,source_artifact_id,severity,message) VALUES (2,1,'ERROR','same error')")
        changes = self.compare()['errors']['count_changes']['items']
        self.assertEqual((changes[0]['count_a'], changes[0]['count_b']), (1, 2))

    def test_asymmetric_events_and_entities_from_events_and_errors(self):
        with self.connection:
            self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,message) VALUES (1,1,'goon_transition','Name=Old')")
            self.connection.execute("INSERT INTO errors(test_run_id,source_artifact_id,severity,message) VALUES (2,1,'ERROR','Name=New')")
            self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,message) VALUES (2,1,'patrol','new event')")
        result = self.compare()
        self.assertEqual(result['events']['added']['items'][0]['message'], 'new event')
        self.assertEqual(result['events']['removed']['items'][0]['message'], 'Name=Old')
        self.assertEqual(result['entities']['added']['items'][0]['name'], 'New')
        self.assertEqual(result['entities']['removed']['items'][0]['name'], 'Old')

    def test_reversing_runs_reverses_differences(self):
        with self.connection:
            self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,message) VALUES (2,1,'goon_transition','Name=New')")
        forward, reverse = self.compare(), self.compare(2, 1)
        self.assertEqual(forward['events']['added']['total'], reverse['events']['removed']['total'])
        self.assertEqual(forward['entities']['added']['items'], reverse['entities']['removed']['items'])
        for name in forward['totals']:
            self.assertEqual(forward['totals'][name]['delta'], -reverse['totals'][name]['delta'])

    def test_occurrence_count_changes_use_known_import_manifest(self):
        with self.connection:
            for run_id, event_id, count, verdict in ((1, 1, 2, 'NEEDS ATTENTION'), (2, 2, 7, 'PASS')):
                notes = {'importer': IMPORTER, 'analysis_schema': 2, 'original_verdict': verdict,
                         'events': {str(event_id): {'count': count}}}
                self.connection.execute('UPDATE test_runs SET notes=? WHERE id=?', (json.dumps(notes), run_id))
        result = self.compare()
        self.assertEqual(result['totals']['event_occurrences'], {'a': 2, 'b': 7, 'delta': 5})
        self.assertEqual(result['events']['count_changes']['items'][0]['delta'], 5)
        self.assertEqual(result['event_counts']['items'][0]['delta'], 5)
        self.assertEqual(result['metadata']['items'][0]['field'], 'original_verdict')
        self.assertEqual(result['coverage']['a']['event_rows_without_repeat_count'], 0)

    def test_bad_missing_or_unknown_manifest_counts_fall_back_explicitly(self):
        bad_notes = ['invalid JSON', '[]', json.dumps({'importer': 'other', 'events': {'1': {'count': 9}}})]
        for count in (None, True, 0, -1, 1.5, '7', 2**63):
            bad_notes.append(json.dumps({'importer': IMPORTER, 'analysis_schema': 2, 'events': {'1': {'count': count}}}))
        bad_notes += [json.dumps({'importer': IMPORTER, 'analysis_schema': 2, 'events': []})]
        for notes in bad_notes:
            with self.subTest(notes=notes):
                with self.connection:
                    self.connection.execute('UPDATE test_runs SET notes=? WHERE id=1', (notes,))
                result = self.compare()
                self.assertEqual(result['totals']['event_occurrences']['a'], 1)
                self.assertEqual(result['coverage']['a']['event_rows_without_repeat_count'], 1)

    def test_duplicate_signature_rows_aggregate_repeat_counts(self):
        with self.connection:
            self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,event_type,message) VALUES (2,1,'identity','identity','Name=Shared')")
        result = self.compare()
        self.assertEqual(result['events']['count_changes']['items'][0]['count_b'], 2)
        self.assertEqual(result['events']['added']['total'], 0)

    def test_entity_rules_do_not_guess_from_bare_names_or_other_types(self):
        with self.connection:
            self.connection.execute("INSERT INTO entities(entity_type,name,canonical_name) VALUES ('class','ClassOnly','classonly')")
            self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,message) VALUES (2,1,'identity','Old Name=ClassOnly Name=Uncataloged')")
        result = self.compare()
        self.assertEqual(result['entities']['added']['total'], 0)
        self.assertEqual(result['entities']['removed']['total'], 0)
        self.assertEqual(result['coverage']['b']['unmatched_names'], 2)

    def test_ambiguous_entity_names_are_excluded_and_reported(self):
        with self.connection:
            self.connection.execute("INSERT INTO entities(entity_type,name,canonical_name) VALUES ('game_object','SHARED','shared')")
        result = self.compare()
        self.assertEqual(result['totals']['entities']['a'], 0)
        self.assertEqual(result['coverage']['a']['ambiguous_names'], 1)

    def test_output_limits_do_not_change_equality_or_full_signature_matching(self):
        prefix = 'same prefix ' * 1000
        with self.connection:
            self.connection.execute('UPDATE events SET message=? WHERE id=1', (prefix + 'A',))
            self.connection.execute('UPDATE events SET message=? WHERE id=2', (prefix + 'B',))
            for index in range(20):
                self.connection.execute("INSERT INTO events(test_run_id,source_artifact_id,category,message) VALUES (2,1,'patrol',?)", (f'extra {index:02}',))
        result = self.compare(limit=1)
        self.assertFalse(result['identical'])
        self.assertEqual(result['events']['added']['total'], 21)
        self.assertEqual(result['events']['added']['omitted'], 20)
        added = result['events']['added']['items'][0]
        removed = result['events']['removed']['items'][0]
        self.assertLessEqual(len(added['message']), TEXT_LIMIT)
        self.assertEqual(added['message'], removed['message'])
        self.assertNotEqual(added['signature_sha256'], removed['signature_sha256'])
        self.assertIn('20 omitted', format_comparison(result))
        self.assertEqual(result, self.compare(limit=1))

    def test_null_and_empty_components_do_not_collapse(self):
        with self.connection:
            self.connection.execute("UPDATE events SET component='' WHERE id=2")
        result = self.compare()
        self.assertEqual(result['events']['added']['total'], 1)
        self.assertEqual(result['events']['removed']['total'], 1)

    def test_read_only_no_raw_source_access_and_json_serializable(self):
        before = self.path.read_bytes()
        result = self.compare()
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse((db.PROJECT_ROOT / 'missing.log').exists())

    def test_invalid_runs_limits_and_missing_database(self):
        for a, b in ((999, 2), (1, 999), (0, 1), (True, 1), ('1', 2), (2**63, 1)):
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                self.compare(a, b)
        for limit in (0, -1, 101, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.compare(limit=limit)
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.Error):
            compare_runs(1, 2, missing)
        self.assertFalse(missing.exists())

    def test_empty_runs(self):
        with self.connection:
            self.connection.execute('DELETE FROM events')
            self.connection.execute('DELETE FROM errors')
        result = self.compare()
        self.assertTrue(result['identical'])
        self.assertEqual(result['totals']['entities']['a'], 0)

    def test_cli_markdown_json_and_invalid_run(self):
        def cli(*args):
            return subprocess.run([sys.executable, '-m', 'database.run_comparison', '--database', str(self.path), *args],
                                  cwd=db.PROJECT_ROOT, capture_output=True, text=True)
        result = cli('1', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('# Run comparison: #1 -> #2', result.stdout)
        result = cli('1', '2', '--format', 'json')
        self.assertTrue(json.loads(result.stdout)['identical'])
        for args in (('1', '999'), ('1', '2', '--limit', '0')):
            result = cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Error:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
