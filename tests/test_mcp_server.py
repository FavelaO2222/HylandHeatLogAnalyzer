"""MCP server tests; every database is temporary.

Tool-behavior tests call the tool functions directly (the SDK's decorator
leaves them plain, callable functions). One integration test exercises the
real MCP wire protocol end-to-end over an in-process transport, to catch
wiring mistakes direct calls would not (e.g. missing type hints breaking
schema generation, a tool never actually getting registered).
"""

from contextlib import closing
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import db, mcp_server
from database.mcp_server import (
    add_decision, add_finding, add_relationship, add_unknown, backup_database, build_context,
    build_entity_context, configure, inspect_database, list_decisions, list_entities, list_findings,
    list_relationships, list_unknowns, mcp, sync_entities, update_decision_status, update_finding_status,
    update_unknown_status,
    attach_evidence, list_evidence, compare_runs,
    search, rebuild_search_index,
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

    def add_entity(self, name, entity_type='game_object'):
        with closing(db.connect_database(self.path)) as connection:
            with connection:
                return connection.execute(
                    'INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
                    (entity_type, name, name.lower())).lastrowid

    def test_configure_initializes_missing_database(self):
        self.assertTrue(self.path.exists())

    def test_evidence_tools_return_structured_provenance(self):
        run_id, _ = self.seed_run_with_identity_event()
        add_finding('Explicitly linked')
        result = attach_evidence('finding', 1, 'run', run_id)
        self.assertEqual(result['id'], 1)
        self.assertEqual(attach_evidence('finding', 1, 'run', run_id), result)
        self.assertEqual(list_evidence('finding', 1)['items'][0]['target_id'], run_id)
        self.assertIn('Evidence (finding #1): Run #1', build_context())

    def test_evidence_and_comparison_errors_are_actionable(self):
        add_finding('Explicitly linked')
        for result in (attach_evidence('finding', 1, 'bad', 1), attach_evidence('finding', 1, 'run', 999),
                       list_evidence('unknown', 999), list_evidence('finding', 1, limit=0), compare_runs(1, 999)):
            self.assertIn('error', result)
        self.assertEqual(list_evidence('finding', 1)['total'], 0)

    def test_compare_runs_returns_structured_identical_result(self):
        run_id, _ = self.seed_run_with_identity_event()
        result = compare_runs(run_id, run_id)
        self.assertTrue(result['identical'])
        self.assertEqual(result['events']['added'], {'total': 0, 'items': [], 'omitted': 0})

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

    def test_add_and_list_unknowns(self):
        self.assertEqual(add_unknown('A minimal question'), 'Recorded unknown 1.')
        self.assertIn('A minimal question', list_unknowns())
        self.assertEqual(list_unknowns(status='resolved'), 'No unknowns recorded.')

    def test_add_unknown_validation_error_surfaces_real_message(self):
        self.assertEqual(add_unknown('text', importance='urgent'),
                          "Error: importance must be one of ('low', 'medium', 'high', 'critical').")

    def test_update_unknown_status(self):
        add_unknown('text')
        self.assertEqual(update_unknown_status(1, 'resolved'), 'Unknown 1 set to resolved.')
        self.assertIn('resolved', list_unknowns())

    def test_update_unknown_status_error(self):
        self.assertEqual(update_unknown_status(999, 'resolved'), 'Error: No unknown with id 999.')

    def test_add_and_list_decisions(self):
        self.assertEqual(add_decision('scope', 'Defer', 'Insufficient evidence'), 'Recorded decision 1.')
        self.assertIn('scope -> Defer', list_decisions())
        self.assertEqual(list_decisions(status='reversed'), 'No decisions recorded.')

    def test_add_decision_validation_error_surfaces_real_message(self):
        self.assertEqual(add_decision('', 'Defer', 'reason'), 'Error: topic text must not be empty.')

    def test_update_decision_status(self):
        add_decision('scope', 'Defer', 'reason')
        self.assertEqual(update_decision_status(1, 'reversed'), 'Decision 1 set to reversed.')
        self.assertIn('reversed', list_decisions())

    def test_update_decision_status_error(self):
        self.assertEqual(update_decision_status(999, 'reversed'), 'Error: No decision with id 999.')

    def test_sync_and_list_entities(self):
        self.seed_run_with_identity_event()
        summary = sync_entities()
        self.assertIn('Inserted: 1 (OfficerLee)', summary)
        self.assertIn('OfficerLee', list_entities())
        self.assertEqual(list_entities(entity_type='npc'), 'No entities recorded.')

    def test_add_and_list_relationships(self):
        self.add_entity('OfficerLee2')
        self.add_entity('OfficerLee')
        self.assertEqual(add_relationship('IS_CLONE_OF', source_name='OfficerLee2', target_name='OfficerLee'),
                          'Recorded relationship 1.')
        self.assertIn('OfficerLee2 IS_CLONE_OF OfficerLee', list_relationships())
        self.assertEqual(list_relationships(relationship_type='IS_A'), 'No relationships recorded.')

    def test_add_relationship_validation_error_surfaces_real_message(self):
        self.assertEqual(add_relationship('IS_A', target_name='X'),
                          'Error: A source entity is required: specify source_entity_id or source_name.')

    def test_build_context_reflects_recorded_finding(self):
        run_id, _ = self.seed_run_with_identity_event()
        sync_entities()
        add_finding('Clone shares identity', subject_name='OfficerLee')
        text = build_context()
        self.assertIn(f'Run #{run_id}', text)
        self.assertIn('Clone shares identity', text)

    def test_build_context_error_on_no_runs(self):
        self.assertTrue(build_context().startswith('Error: No test runs'))

    def test_build_entity_context_reflects_recorded_finding(self):
        self.seed_run_with_identity_event()
        sync_entities()
        add_finding('Clone shares identity', subject_name='OfficerLee')
        text = build_entity_context(entity_name='OfficerLee')
        self.assertIn('Entity Context: OfficerLee', text)
        self.assertIn('Clone shares identity', text)

    def test_build_entity_context_error_on_unresolvable_name(self):
        self.assertTrue(build_entity_context(entity_name='Nobody').startswith("Error: No entity named 'Nobody'"))

    def test_build_entity_context_requires_an_entity(self):
        self.assertTrue(build_entity_context().startswith('Error: An entity is required'))

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

    def test_search_returns_structured_ranked_results(self):
        run_id, _ = self.seed_run_with_identity_event()
        result = search('OfficerLee')
        self.assertEqual(result['items'][0]['test_run_id'], run_id)
        self.assertEqual(result['items'][0]['source'], 'event')
        self.assertFalse(result['truncated'])

    def test_search_type_filter_and_no_match(self):
        self.seed_run_with_identity_event()
        self.assertEqual(search('OfficerLee', type='errors')['items'], [])
        self.assertEqual(search('nonexistentterm')['items'], [])

    def test_search_error_surfaces_real_message(self):
        result = search('x', type='bogus')
        self.assertIn('error', result)
        self.assertIn("type must be one of", result['error'])

    def test_rebuild_search_index(self):
        self.seed_run_with_identity_event()
        self.assertEqual(rebuild_search_index(), 'Rebuilt search index: 1 events, 0 errors indexed.')
        self.assertEqual(len(search('OfficerLee')['items']), 1)

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
                             'add_unknown', 'list_unknowns', 'update_unknown_status',
                             'add_decision', 'list_decisions', 'update_decision_status',
                             'add_relationship', 'list_relationships',
                             'sync_entities', 'list_entities', 'build_context', 'build_entity_context',
                             'inspect_database', 'backup_database', 'attach_evidence', 'list_evidence', 'compare_runs',
                             'search', 'rebuild_search_index'):
                self.assertIn(expected, names)

            result = await client.call_tool('add_finding', {'finding': 'Protocol round trip works'})
            self.assertFalse(result.is_error)
            self.assertIn('Recorded finding', result.content[0].text)

            result = await client.call_tool('list_findings', {})
            self.assertIn('Protocol round trip works', result.content[0].text)

    async def test_phase4_tools_over_json_rpc_transport(self):
        from mcp.client.client import Client
        from phase4_fixtures import seed
        with closing(db.connect_database(self.path)) as connection:
            seed(connection)
            with connection:
                connection.execute("UPDATE test_runs SET result='FAIL' WHERE id=2")
        # Force JSON-RPC framing: the SDK's default in-process mode now dispatches directly.
        async with asyncio.timeout(30), Client(mcp, mode='legacy') as client:
            result = await client.call_tool('attach_evidence',
                {'record_type': 'finding', 'record_id': 1, 'target_type': 'event', 'target_id': 1})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content['id'], 1)
            result = await client.call_tool('list_evidence', {'record_type': 'finding', 'record_id': 1})
            self.assertEqual(result.structured_content['total'], 1)
            result = await client.call_tool('compare_runs', {'run_a': 1, 'run_b': 2, 'limit': 1})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content['metadata']['items'][0]['field'], 'result')
            self.assertFalse(result.structured_content['identical'])
            result = await client.call_tool('attach_evidence',
                {'record_type': 'finding', 'record_id': 1, 'target_type': 'event', 'target_id': 999})
            self.assertIn('No event', result.structured_content['error'])
            result = await client.call_tool('search', {'query': 'Shared', 'type': 'events'})
            self.assertFalse(result.is_error)
            self.assertEqual({item['id'] for item in result.structured_content['items']}, {1, 2})


if __name__ == '__main__':
    unittest.main()
