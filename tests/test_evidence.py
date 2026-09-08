from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import unittest

from database import db, evidence
from phase4_fixtures import ResearchFixture


class EvidenceTests(ResearchFixture, unittest.TestCase):
    def test_all_owner_target_combinations_preserve_interpretation(self):
        before = tuple(self.connection.execute('SELECT * FROM findings').fetchone())
        for owner in evidence.RECORDS:
            for target in evidence.TARGETS:
                with self.subTest(owner=owner, target=target):
                    evidence.attach_evidence(self.path, owner, 1, target, 1)
            result = evidence.list_evidence(self.path, owner, 1)
            self.assertEqual(result['total'], 5)
            self.assertEqual({r['target_type'] for r in result['items']}, set(evidence.TARGETS))
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM findings').fetchone()), before)
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_duplicate_attachment_is_idempotent_and_sql_duplicate_rejected(self):
        first = evidence.attach_evidence(self.path, 'finding', 1, 'event', 1)
        self.assertEqual(first, evidence.attach_evidence(self.path, 'finding', 1, 'event', 1))
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute('INSERT INTO evidence_links(finding_id,event_id) VALUES (1,1)')
        self.assertEqual(evidence.list_evidence(self.path, 'finding', 1)['total'], 1)

    def test_invalid_record_types_rejected_without_writes(self):
        for kind in ('findings', 'event', '', 'findings; DROP TABLE entities', None, []):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'record_type'):
                evidence.attach_evidence(self.path, kind, 1, 'event', 1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM evidence_links').fetchone()[0], 0)

    def test_invalid_target_types_rejected_without_writes(self):
        for kind in ('events', 'finding', 'source_artifact', '', None, []):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'target_type'):
                evidence.attach_evidence(self.path, 'finding', 1, kind, 1)
        self.assertEqual(evidence.list_evidence(self.path, 'finding', 1)['total'], 0)

    def test_nonexistent_owner_ids_rejected(self):
        for kind in evidence.RECORDS:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'No '):
                evidence.attach_evidence(self.path, kind, 999, 'run', 1)
            with self.assertRaises(ValueError):
                evidence.list_evidence(self.path, kind, 999)

    def test_nonexistent_target_ids_rejected(self):
        for kind in evidence.TARGETS:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'No '):
                evidence.attach_evidence(self.path, 'finding', 1, kind, 999)
        self.assertEqual(evidence.list_evidence(self.path, 'finding', 1)['total'], 0)

    def test_ids_require_positive_integers(self):
        for value in (None, True, False, 0, -1, '1', 1.0, 2**63):
            for field in ('record', 'target'):
                with self.subTest(value=value, field=field), self.assertRaises(ValueError):
                    evidence.attach_evidence(self.path, 'finding', value if field == 'record' else 1,
                                              'event', value if field == 'target' else 1)

    def test_database_rejects_missing_multiple_and_dangling_references(self):
        statements = (
            'INSERT INTO evidence_links(event_id) VALUES (1)',
            'INSERT INTO evidence_links(finding_id) VALUES (1)',
            'INSERT INTO evidence_links(finding_id,unknown_id,event_id) VALUES (1,1,1)',
            'INSERT INTO evidence_links(finding_id,event_id,error_id) VALUES (1,1,1)',
            'INSERT INTO evidence_links(finding_id,event_id) VALUES (999,1)',
            'INSERT INTO evidence_links(finding_id,event_id) VALUES (1,999)',
            'INSERT INTO evidence_links(finding_id,event_id) VALUES (1,-1)',
        )
        for sql in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), self.connection:
                self.connection.execute(sql)

    def test_deletion_and_id_updates_cannot_orphan_any_link(self):
        for kind in evidence.TARGETS:
            evidence.attach_evidence(self.path, 'unknown', 1, kind, 1)
        for table in ('unknowns', 'test_runs', 'events', 'errors', 'entities', 'relationships'):
            for operation in (f'DELETE FROM {table} WHERE id=1', f'UPDATE {table} SET id=999 WHERE id=1'):
                with self.subTest(sql=operation), self.assertRaises(sqlite3.IntegrityError), self.connection:
                    self.connection.execute(operation)
        with self.assertRaises(sqlite3.IntegrityError), self.connection:
            self.connection.execute('UPDATE evidence_links SET event_id=999 WHERE event_id=1')

    def test_paginated_listing_is_stable_and_read_only(self):
        for kind in evidence.TARGETS:
            evidence.attach_evidence(self.path, 'decision', 1, kind, 1)
        before = self.path.read_bytes()
        first = evidence.list_evidence(self.path, 'decision', 1, limit=2)
        second = evidence.list_evidence(self.path, 'decision', 1, limit=3, offset=first['next_offset'])
        self.assertEqual(first['total'], 5)
        self.assertIsNone(second['next_offset'])
        self.assertEqual([r['id'] for r in first['items'] + second['items']], [1, 2, 3, 4, 5])
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_pagination(self):
        for args in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1}, {'offset': 1.5}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                evidence.list_evidence(self.path, 'finding', 1, **args)

    def test_cli_attach_list_json_and_errors(self):
        def cli(*args):
            return subprocess.run([sys.executable, '-m', 'database.evidence', '--database', str(self.path), *args],
                                  cwd=db.PROJECT_ROOT, capture_output=True, text=True)
        result = cli('attach', 'finding', '1', 'event', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Event #1', result.stdout)
        result = cli('list', 'finding', '1', '--format', 'json')
        self.assertEqual(json.loads(result.stdout)['items'][0]['target_id'], 1)
        for args in (('attach', 'finding', '1', 'event', '999'), ('attach', 'finding', '1', 'bad', '1'),
                     ('list', 'finding', '999'), ('list', 'finding', '1', '--limit', '0')):
            result = cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Traceback', result.stderr)

    def test_missing_database_not_created(self):
        missing = self.root / 'missing.db'
        with self.assertRaises(sqlite3.Error):
            evidence.attach_evidence(missing, 'finding', 1, 'event', 1)
        self.assertFalse(missing.exists())
