"""MCP server tests; every database is temporary.

Tool-behavior tests call the tool functions directly (the SDK's decorator
leaves them plain, callable functions). One integration test exercises the
real MCP wire protocol end-to-end over an in-process transport, to catch
wiring mistakes direct calls would not (e.g. missing type hints breaking
schema generation, a tool never actually getting registered).
"""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import db, mcp_server
from database.mcp_server import (
    add_finding, backup_database, build_context, configure, inspect_database, list_entities,
    list_findings, mcp, sync_entities, update_finding_status,
)


class McpServerToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        configure(self.path)

    def seed_run_with_identity_event(self):
        with closing(db.connect_database(self.path)) as connection:
            with connection:
                artifact_id = connection.execute(
                    'INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, ?, ?)',
                    ('log', 'logs/session.log', 'session.log')).lastrowid
                run_id = connection.execute(
                    'INSERT INTO test_runs (source_artifact_id, profile, result) VALUES (?, ?, ?)',
                    (artifact_id, 'general', 'NEEDS_ATTENTION')).lastrowid
                connection.execute(
                    'INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (?, ?, ?, ?)',
                    (run_id, artifact_id, 'diagnostic_result', 'Name=OfficerLee, Root=OfficerLee'))
        return run_id, artifact_id

    def test_configure_initializes_missing_database(self):
        self.assertTrue(self.path.exists())

    def test_db_helper_requires_configure(self):
        mcp_server._database = None
        with self.assertRaises(RuntimeError):
            mcp_server._db()
        configure(self.path)  # restore for any test ordered after this one

    def test_add_and_list_findings(self):
        self.assertEqual(add_finding('A minimal finding'), 'Recorded finding 1.')
        self.assertIn('A minimal finding', list_findings())
        self.assertEqual(list_findings(status='superseded'), 'No findings recorded.')

    def test_add_finding_validation_error_surfaces_real_message(self):
        result = add_finding('text', confidence='guaranteed')
        self.assertEqual(result, "Error: confidence must be one of ('confirmed', 'strong', 'tentative', 'unknown').")

    def test_add_finding_resolves_subject_name(self):
        self.seed_run_with_identity_event()
        sync_entities()
        add_finding('Clone shares an identity', subject_name='OfficerLee')
        self.assertIn('OfficerLee: Clone shares an identity', list_findings())

    def test_update_finding_status(self):
        add_finding('text')
        self.assertEqual(update_finding_status(1, 'superseded'), 'Finding 1 set to superseded.')
        self.assertIn('superseded', list_findings())

    def test_update_finding_status_error(self):
        self.assertEqual(update_finding_status(999, 'superseded'), 'Error: No finding with id 999.')

    def test_sync_and_list_entities(self):
        self.seed_run_with_identity_event()
        summary = sync_entities()
        self.assertIn('Inserted: 1 (OfficerLee)', summary)
        self.assertIn('OfficerLee', list_entities())
        self.assertEqual(list_entities(entity_type='npc'), 'No entities recorded.')

    def test_build_context_reflects_recorded_finding(self):
        run_id, _ = self.seed_run_with_identity_event()
        sync_entities()
        add_finding('Clone shares identity', subject_name='OfficerLee')
        text = build_context()
        self.assertIn(f'Run #{run_id}', text)
        self.assertIn('Clone shares identity', text)

    def test_build_context_error_on_no_runs(self):
        self.assertTrue(build_context().startswith('Error: No test runs'))

    def test_inspect_database(self):
        self.seed_run_with_identity_event()
        summary = inspect_database(latest_run=True)
        self.assertIn('Test runs: 1', summary)
        self.assertIn('Latest Test Run', summary)

    def test_backup_database_writes_dump_at_default_location(self):
        # backup_database() takes no path argument by design, so it always resolves
        # DEFAULT_BACKUP_PATH; patch that constant rather than touching the real,
        # already-committed backups/hylandheat.sql.
        self.seed_run_with_identity_event()
        fake_default = Path(self.temp.name) / 'backups' / 'hylandheat.sql'
        with patch.object(mcp_server.backup, 'DEFAULT_BACKUP_PATH', fake_default):
            result = backup_database()
        self.assertIn('Wrote', result)
        self.assertIn('Ask for it to be committed', result)
        self.assertTrue(fake_default.exists())
        self.assertIn('CREATE TABLE', fake_default.read_text(encoding='utf-8'))

    def test_backup_database_error_surfaces_real_message(self):
        # configure() self-initializes a missing database, so it can't produce this error path;
        # plant a genuine foreign-key violation instead (bypassing connect_database's enforcement,
        # same technique test_backup.py uses), and set _database directly to skip configure()'s init.
        import sqlite3
        db.initialize_database(self.path)
        connection = sqlite3.connect(self.path)
        connection.execute(
            "INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (999, 999, 'x', 'y')")
        connection.commit()
        connection.close()
        mcp_server._database = self.path
        fake_default = Path(self.temp.name) / 'backups' / 'hylandheat.sql'
        with patch.object(mcp_server.backup, 'DEFAULT_BACKUP_PATH', fake_default):
            result = backup_database()
        self.assertTrue(result.startswith('Error:'))
        self.assertFalse(fake_default.exists())


class McpServerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        configure(self.path)

    async def test_tools_are_registered_and_callable_over_real_protocol(self):
        from mcp.client.client import Client
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools.tools}
            for expected in ('add_finding', 'list_findings', 'update_finding_status',
                             'sync_entities', 'list_entities', 'build_context', 'inspect_database',
                             'backup_database'):
                self.assertIn(expected, names)

            result = await client.call_tool('add_finding', {'finding': 'Protocol round trip works'})
            self.assertFalse(result.is_error)
            self.assertIn('Recorded finding', result.content[0].text)

            result = await client.call_tool('list_findings', {})
            self.assertIn('Protocol round trip works', result.content[0].text)


if __name__ == '__main__':
    unittest.main()
