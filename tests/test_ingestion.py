"""Phase 2 integration tests; every database/report destination is temporary."""

from contextlib import closing, redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analysis_capture import AnalysisCapture
from hyland_heat_log_analyzer import analyze, main, parse_line, render_brief, write_reports
from database.db import PROJECT_ROOT, SCHEMA_VERSION, connect_database, initialize_database
from database.ingestion import import_analysis_result
from database.inspect_db import inspect_database


class IngestionTests(unittest.TestCase):
    LOG = ("[12:00:00] [HylandHeat] Hyland Heat loaded.\n"
           "ordinary unclassified chatter\n"
           "[12:00:01] [SWAT] DEPLOY REJECTED: lifecycle research locked, State=Dormant, Live=0\n"
           "[12:00:02] [WARNING] [HylandHeat] census incomplete\n"
           "[12:00:03] [ERROR] [SwapperPlugin] System.IO.IOException: Failed\n"
           "   at System.IO.File.Move()\n   at SwapperPlugin.Caller()\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'session.log'
        self.source.write_text(self.LOG, encoding='utf-8')
        self.database = self.root / 'research.db'

    def prepare(self, profile='read-only-audit', source=None):
        capture = AnalysisCapture()
        report = analyze(source or self.source, profile, capture=capture)
        return report, capture

    def import_log(self, profile='read-only-audit', source=None):
        report, capture = self.prepare(profile, source)
        result = import_analysis_result(report, capture, self.database)
        return report, result

    def test_artifact_hash_path_and_raw_unchanged(self):
        before = self.source.read_bytes()
        self.import_log()
        with closing(connect_database(self.database)) as connection:
            row = connection.execute('SELECT * FROM source_artifacts').fetchone()
            self.assertEqual(row['sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(row['path'], str(self.source))
            self.assertEqual(row['filename'], 'session.log')
            self.assertEqual(row['artifact_type'], 'log')
            self.assertIsNone(row['created_at'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_run_profile_result_and_builds(self):
        report, result = self.import_log()
        with closing(connect_database(self.database)) as connection:
            run = connection.execute('SELECT * FROM test_runs').fetchone()
            self.assertEqual(run['source_artifact_id'], result['source_artifact_id'])
            self.assertEqual(run['profile'], report['profile'])
            self.assertEqual(run['result'], 'NEEDS_ATTENTION')
            self.assertEqual(run['started_at'], '12:00:00')
            self.assertIsNone(run['game_build'])
            self.assertIsNone(run['mod_build'])
            self.assertEqual(json.loads(run['notes'])['original_verdict'], report['verdict'])

    def test_inconclusive_maps_to_incomplete(self):
        self.source.write_text('ordinary chatter\n', encoding='utf-8')
        self.import_log()
        with closing(connect_database(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT result FROM test_runs').fetchone()[0], 'INCOMPLETE')

    def test_meaningful_events_and_error_separation(self):
        report, result = self.import_log()
        self.assertEqual(result['events'], 3)
        self.assertEqual(result['errors'], 1)
        with closing(connect_database(self.database)) as connection:
            events = connection.execute('SELECT * FROM events ORDER BY source_line').fetchall()
            self.assertEqual([e['source_line'] for e in events], [1, 3, 4])
            self.assertEqual(events[1]['category'], 'swat_rejection')
            self.assertEqual(events[1]['event_type'], 'swat_rejection')
            self.assertEqual(events[1]['component'], 'SWAT')
            self.assertTrue(all(e['test_run_id'] == result['test_run_id'] for e in events))
            self.assertTrue(all(e['source_artifact_id'] == result['source_artifact_id'] for e in events))
            self.assertIn(3, report['expected_rejection_lines'])
            self.assertNotIn('DEPLOY', connection.execute('SELECT message FROM errors').fetchone()[0])

    def test_full_stacks_and_repeated_error_variants(self):
        frames = ''.join(f'   at Frame{i}()\r\n' for i in range(2000))
        self.source.write_bytes(('[ERROR] System.Exception: repeated\r\n' + frames
                                 + '[ERROR] System.Exception: repeated\r\n   at DifferentCaller()\r\n').encode())
        report, result = self.import_log()
        self.assertEqual(len(report['detected_events'][0]['stack_trace']), 40)
        self.assertEqual(result['errors'], 2)
        with closing(connect_database(self.database)) as connection:
            rows = connection.execute('SELECT * FROM errors ORDER BY source_line').fetchall()
            self.assertEqual(rows[0]['stack_trace'], frames)
            self.assertEqual(rows[1]['stack_trace'], '   at DifferentCaller()\r\n')
            self.assertEqual(rows[1]['source_line'], 2002)

    def test_all_event_and_error_references_match_raw_lines(self):
        self.import_log()
        self.assert_source_references(self.source)

    def assert_source_references(self, source):
        with source.open(encoding='utf-8-sig', errors='replace', newline='') as handle:
            lines = list(handle)
        with closing(connect_database(self.database)) as connection:
            for table in ('events', 'errors'):
                for row in connection.execute(f'SELECT * FROM {table}'):
                    item = parse_line(lines[row['source_line'] - 1], row['source_line'])
                    self.assertEqual(row['message'], item['message'])
                    self.assertEqual(row['timestamp'], item['timestamp'])
            manifest = json.loads(connection.execute('SELECT notes FROM test_runs ORDER BY id DESC').fetchone()[0])
            for event_id, event in manifest['events'].items():
                message = connection.execute('SELECT message FROM events WHERE id=?', (event_id,)).fetchone()[0]
                self.assertEqual(event['count'], len(event['occurrences']))
                for occurrence in event['occurrences']:
                    self.assertEqual(parse_line(lines[occurrence['line'] - 1], occurrence['line'])['message'], message)
            for error_id, error in manifest['errors'].items():
                trace = connection.execute('SELECT stack_trace FROM errors WHERE id=?', (error_id,)).fetchone()[0]
                self.assertEqual(trace or '', ''.join(lines[n - 1] for n in error['stack_source_lines']))

    def test_repeated_events_and_transition_context_are_preserved(self):
        self.source.write_text('[WARNING] same\n[WARNING] same\n'
                               'GOON ACTUAL TRANSITION: UNSPAWNED_TO_SPAWNED\n'
                               'TRANSITION BEFORE: NPCID=one\nTRANSITION AFTER: NPCID=one\n'
                               'GOON ACTUAL TRANSITION: UNSPAWNED_TO_SPAWNED\n'
                               'TRANSITION BEFORE: NPCID=two\nTRANSITION AFTER: NPCID=two\n')
        report, _ = self.import_log()
        with closing(connect_database(self.database)) as connection:
            manifest = json.loads(connection.execute('SELECT notes FROM test_runs').fetchone()[0])
            self.assertEqual(manifest['transitions'], report['transitions'])
            row = connection.execute('SELECT event_type FROM events WHERE source_line=3').fetchone()
            self.assertEqual(row[0], 'UNSPAWNED_TO_SPAWNED')
        self.assert_source_references(self.source)

    def test_duplicate_import_reuses_run(self):
        _, first = self.import_log()
        _, second = self.import_log()
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['test_run_id'], second['test_run_id'])
        self.assertEqual(first['events'], second['events'])
        with closing(connect_database(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM test_runs').fetchone()[0], 1)

    def test_copied_source_deduplicates_and_new_profile_adds_run(self):
        _, first = self.import_log()
        copied = self.root / 'copy.log'
        copied.write_bytes(self.source.read_bytes())
        _, duplicate = self.import_log(source=copied)
        _, another = self.import_log(profile='general', source=copied)
        self.assertTrue(duplicate['duplicate'])
        self.assertFalse(another['duplicate'])
        self.assertEqual(first['source_artifact_id'], another['source_artifact_id'])
        with closing(connect_database(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM test_runs').fetchone()[0], 2)

    def test_changed_contents_at_same_path_create_new_artifact(self):
        _, first = self.import_log()
        self.source.write_text(self.LOG + '[WARNING] new evidence\n')
        _, second = self.import_log()
        self.assertNotEqual(first['source_artifact_id'], second['source_artifact_id'])

    def test_atomicity_when_error_insert_fails(self):
        initialize_database(self.database)
        with closing(connect_database(self.database)) as connection:
            connection.execute("CREATE TRIGGER force_failure BEFORE INSERT ON errors BEGIN SELECT RAISE(ABORT, 'forced'); END")
            connection.commit()
        report, capture = self.prepare()
        with self.assertRaises(sqlite3.IntegrityError):
            import_analysis_result(report, capture, self.database)
        with closing(connect_database(self.database)) as connection:
            for table in ('source_artifacts', 'test_runs', 'events', 'errors'):
                self.assertEqual(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_incompatible_version_not_recreated(self):
        initialize_database(self.database)
        with closing(connect_database(self.database)) as connection, connection:
            connection.execute('UPDATE schema_metadata SET schema_version=999')
        before = self.database.read_bytes()
        report, capture = self.prepare()
        with self.assertRaisesRegex(ValueError, 'version'):
            import_analysis_result(report, capture, self.database)
        self.assertEqual(before, self.database.read_bytes())

    def test_changed_source_is_rejected(self):
        report, capture = self.prepare()
        self.source.write_text(self.LOG + 'more data\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            import_analysis_result(report, capture, self.database)
        self.assertFalse(self.database.exists())

    def test_capture_does_not_change_analysis_or_report_files(self):
        self.source.write_bytes(b'\xef\xbb\xbf' + self.LOG.replace('\n', '\r\n').encode())
        normal = analyze(self.source, 'read-only-audit')
        captured, _ = self.prepare()
        self.assertEqual(normal, captured)
        normal_paths = write_reports(normal, self.root / 'normal', True, True, True)
        captured_paths = write_reports(captured, self.root / 'captured', True, True, True)
        self.assertEqual([p.read_bytes() for p in normal_paths], [p.read_bytes() for p in captured_paths])

    def test_without_database_no_sqlite_dependency_loaded(self):
        code = ("import sys; from hyland_heat_log_analyzer import main; "
                "assert main(sys.argv[1:]) == 0; "
                "assert 'database.ingestion' not in sys.modules; "
                "assert 'sqlite3' not in sys.modules")
        result = subprocess.run([sys.executable, '-c', code, str(self.source), '--brief', '--output', str(self.root / 'reports')],
                                cwd=PROJECT_ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.database.exists())

    def test_cli_optional_database_summary_and_report_equivalence(self):
        normal, captured = self.root / 'normal', self.root / 'captured'
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([str(self.source), '--brief', '--output', str(normal)]), 0)
            self.assertEqual(main([str(self.source), '--brief', '--output', str(captured), '--database', str(self.database)]), 0)
        self.assertIn('Duplicate import: no', output.getvalue())
        for path in normal.iterdir():
            self.assertEqual(path.read_bytes(), (captured / path.name).read_bytes())

    def test_source_and_database_report_aliases_rejected(self):
        error = io.StringIO()
        before = self.source.read_bytes()
        with redirect_stderr(error):
            self.assertEqual(main([str(self.source), '--database', str(self.source)]), 2)
            self.assertEqual(main([str(self.source), '--output', str(self.root),
                                   '--database', str(self.root / 'session_summary.txt')]), 2)
        self.assertEqual(before, self.source.read_bytes())
        self.assertFalse((self.root / 'session_summary.txt').exists())

    def test_inspection_is_read_only_and_no_research_records_created(self):
        self.import_log()
        before = self.database.read_bytes()
        summary = inspect_database(self.database, True)
        self.assertIn(f'Database schema: {SCHEMA_VERSION}', summary)
        self.assertIn('Errors: 1', summary)
        for name in ('Entities', 'Findings', 'Unknowns', 'Decisions', 'Relationships'):
            self.assertIn(f'{name}: 0', summary)
        self.assertEqual(before, self.database.read_bytes())
        with closing(connect_database(self.database, read_only=True)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM test_runs')
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.Error):
            inspect_database(missing)
        self.assertFalse(missing.exists())

    def test_real_audit_regression(self):
        configured = os.environ.get('HYLAND_HEAT_AUDIT_LOG')
        if not configured:
            self.skipTest('Set HYLAND_HEAT_AUDIT_LOG to run the local real-audit regression.')
        source = Path(configured).resolve(strict=True)
        report, result = self.import_log(source=source)
        self.assertEqual(report['verdict'], 'NEEDS ATTENTION')
        self.assertTrue(report['expected_rejection_lines'])
        self.assertGreater(result['errors'], 0)
        self.assertLess(result['events'] + result['errors'], report['metadata']['lines_scanned'])
        self.assertLessEqual(len(render_brief(report)), 6000)
        self.assert_source_references(source)
        with source.open(encoding='utf-8-sig', errors='replace') as handle:
            lines = list(handle)
        for event in report['detected_events']:
            self.assertEqual(event['raw'], lines[event['line'] - 1].rstrip('\r\n'))
            for frame in event['stack_trace']:
                self.assertEqual(frame['raw'], lines[frame['line'] - 1].rstrip('\r\n'))


if __name__ == '__main__':
    unittest.main()
