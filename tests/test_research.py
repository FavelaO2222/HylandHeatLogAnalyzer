"""Tests for database.research: the cross-artifact evidence-packet command."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from database import db, research


class ResearchFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def artifact(self, artifact_type='log'):
        with self.connection:
            return self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, 'a', 'a')",
                (artifact_type,)).lastrowid

    def run_(self, artifact_id):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO test_runs (source_artifact_id, profile) VALUES (?, ?)',
                (artifact_id, 'audit')).lastrowid

    def event(self, run_id, artifact_id, message):
        with self.connection:
            return self.connection.execute(
                '''INSERT INTO events (test_run_id, source_artifact_id, category, message)
                   VALUES (?, ?, 'diagnostic', ?)''', (run_id, artifact_id, message)).lastrowid

    def document(self, relative_path, content, artifact_type='source_code'):
        with self.connection:
            artifact_id = self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES (?, 'repo', 'repo')",
                (artifact_type,)).lastrowid
            return self.connection.execute(
                '''INSERT INTO source_documents (source_artifact_id, relative_path, language, content)
                   VALUES (?, ?, 'csharp', ?)''', (artifact_id, relative_path, content)).lastrowid

    def finding(self, text):
        with self.connection:
            return self.connection.execute("INSERT INTO findings (finding) VALUES (?)", (text,)).lastrowid

    def experiment(self, question, observed_result, symbols):
        with self.connection:
            experiment_id = self.connection.execute(
                'INSERT INTO experiments (question, observed_result) VALUES (?, ?)',
                (question, observed_result)).lastrowid
            self.connection.executemany(
                'INSERT INTO experiment_symbols (experiment_id, symbol) VALUES (?, ?)',
                [(experiment_id, symbol) for symbol in symbols])
            return experiment_id


class ResearchFunctionTests(ResearchFixture, unittest.TestCase):
    def test_finds_declaration_and_reference_across_both_source_kinds(self):
        self.document('HylandHeat/Police/FootPatrolSpawnPatch.cs',
                      'LawManager.Instance.StartFootpatrol(route, members);',
                      artifact_type='source_code')
        self.document('Il2CppScheduleOne.Law/LawManager.cs',
                      'namespace Il2CppScheduleOne.Law;\n\npublic class LawManager\n{\n'
                      '    public unsafe PatrolGroup StartFootpatrol(FootPatrolRoute route, int requestedMembers)\n'
                      '    {\n        return null;\n    }\n}',
                      artifact_type='decompiler_export')
        result = research.research(self.path, 'LawManager.StartFootpatrol')
        mod = result['sections']['mod_source']
        game = result['sections']['decompiled']
        self.assertEqual(mod['total'], 1)
        self.assertEqual(mod['items'][0]['kind'], 'reference')
        self.assertEqual(game['total'], 1)
        self.assertEqual(game['items'][0]['kind'], 'declaration')
        self.assertEqual(game['items'][0]['line'], 5)
        self.assertIn('StartFootpatrol', game['items'][0]['text'])

    def test_member_declaration_preferred_over_enclosing_type_declaration(self):
        self.document('PoliceStation.cs',
                      'public class PoliceStation : NPCEnterableBuilding\n{\n'
                      '    public unsafe PoliceOfficer PullOfficer()\n    {\n        return null;\n    }\n}',
                      artifact_type='decompiler_export')
        result = research.research(self.path, 'PoliceStation.PullOfficer')
        item = result['sections']['decompiled']['items'][0]
        self.assertEqual(item['kind'], 'declaration')
        self.assertIn('PullOfficer', item['text'])
        self.assertNotIn('NPCEnterableBuilding', item['text'])

    def test_harmony_patch_class_declaration_matches_despite_no_word_boundary(self):
        # Regression for finding #10: a Harmony patch class conventionally
        # concatenates the patched member's name with a type prefix and/or
        # "Patch" suffix, so the member name is a substring with no regex
        # \b on one or both sides. A weaker, unrelated declaration must not
        # win instead.
        self.document('HylandHeat/Debug/PoliceActivationDebugPatch.cs',
                      'private static void DumpNetworkState(string label, NetworkObject networkObject)\n'
                      '{\n}\n\n'
                      '[HarmonyPatch(typeof(NetworkObject), nameof(NetworkObject.TryStartDeactivation))]\n'
                      'private static class NetworkObjectTryStartDeactivationPatch\n{\n}',
                      artifact_type='source_code')
        item = research.research(self.path, 'NetworkObject.TryStartDeactivation')['sections']['mod_source']['items'][0]
        self.assertEqual(item['kind'], 'declaration')
        self.assertIn('NetworkObjectTryStartDeactivationPatch', item['text'])

    def test_closest_declaration_to_primary_token_wins_over_first_by_line(self):
        # Regression for finding #11, isolated from finding #10's fix: even
        # when the primary token has NO declaration of its own anywhere (so
        # the locator must fall back to the secondary/enclosing-type token,
        # here class names deliberately don't contain "Activate" at all),
        # and multiple candidate declarations exist for that fallback token,
        # the one closest to an actual occurrence of the primary token must
        # win -- not whichever comes first by line number.
        self.document('HylandHeat/Debug/PoliceActivationDebugPatch.cs',
                      '[HarmonyPatch(typeof(PoliceOfficer), nameof(PoliceOfficer.Deactivate))]\n'
                      'private static class FirstPatch\n{\n'
                      '    private static void Prefix(PoliceOfficer __instance)\n    {\n    }\n}\n\n'
                      '[HarmonyPatch(typeof(PoliceOfficer), nameof(PoliceOfficer.Activate))]\n'
                      'private static class SecondPatch\n{\n'
                      '    private static void Prefix(PoliceOfficer __instance)\n    {\n    }\n}',
                      artifact_type='source_code')
        item = research.research(self.path, 'PoliceOfficer.Activate')['sections']['mod_source']['items'][0]
        self.assertEqual(item['line'], 12)  # SecondPatch's own Prefix, not FirstPatch's at line 4

    def test_no_match_reports_empty_not_omitted(self):
        result = research.research(self.path, 'TotallyUnknownSymbolXYZ')
        for key, page in result['sections'].items():
            self.assertEqual(page['total'], 0, key)
            self.assertEqual(page['items'], [], key)
        self.assertIn('No source-code match', result['interpretation'][0])

    def test_related_events_are_included(self):
        artifact_id = self.artifact()
        run_id = self.run_(artifact_id)
        self.event(run_id, artifact_id, 'GoonPool exhausted, spawn skipped')
        result = research.research(self.path, 'GoonPool')
        self.assertEqual(result['sections']['events']['total'], 1)
        self.assertIn('GoonPool', result['sections']['events']['items'][0]['snippet'])

    def test_related_findings_are_included(self):
        self.finding('GoonPool appears capped at 20 concurrent goons.')
        result = research.research(self.path, 'GoonPool')
        records = result['sections']['records']
        self.assertEqual(records['total'], 1)
        self.assertEqual(records['items'][0]['kind'], 'finding')

    def test_related_experiments_are_included(self):
        self.experiment('Does GoonPool refill after despawn?', 'Refilled after 3 in-game days.', ['GoonPool'])
        result = research.research(self.path, 'GoonPool')
        experiments = result['sections']['experiments']
        self.assertEqual(experiments['total'], 1)
        self.assertIn('Refilled after 3 in-game days.', experiments['items'][0]['observed_result'])

    def test_bounded_and_reports_omitted(self):
        for i in range(5):
            self.document(f'File{i}.cs', 'void SharedKeyword() {}')
        result = research.research(self.path, 'SharedKeyword', limit=2)
        page = result['sections']['mod_source']
        self.assertEqual(len(page['items']), 2)
        self.assertEqual(page['total'], 5)
        self.assertEqual(page['omitted'], 3)

    def test_next_target_prioritizes_missing_code_match_over_missing_events(self):
        result = research.research(self.path, 'CompletelyAbsentSymbol')
        self.assertIn('decompiled game-code match', result['next_target'])

    def test_next_target_suggests_capturing_a_log_when_code_matches_exist(self):
        self.document('A.cs', 'void KnownMethod() {}', artifact_type='source_code')
        self.document('B.cs', 'void KnownMethod() {}', artifact_type='decompiler_export')
        result = research.research(self.path, 'KnownMethod')
        self.assertIn('Capture a play session', result['next_target'])

    def test_empty_query_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must not be empty'):
            research.research(self.path, '   ')

    def test_requires_schema_v5(self):
        from phase4_fixtures import create_v1
        v1_path = Path(self.temp.name) / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            research.research(v1_path, 'anything')


class ResearchCliTests(ResearchFixture, unittest.TestCase):
    def test_text_output_lists_all_sections_even_when_empty(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main(['--database', str(self.path), 'NothingAtAll']), 0)
        text = output.getvalue()
        self.assertIn('Hyland Heat call/patch sites', text)
        self.assertIn('No matches found.', text)
        self.assertIn('Suggested next research target', text)

    def test_json_output_shape(self):
        self.finding('Something about GoonPool.')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(research.main(['--database', str(self.path), 'GoonPool', '--format', 'json']), 0)
        payload = json.loads(output.getvalue())
        self.assertIn('sections', payload)
        self.assertIn('records', payload['sections'])

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(research.main(['--database', str(self.path), '   ']), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())


if __name__ == '__main__':
    unittest.main()
