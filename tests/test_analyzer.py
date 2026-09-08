import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from hyland_heat_log_analyzer import analyze, main, parse_line, render_brief, render_report, write_reports


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "playtest.log"

    def analyze_text(self, text, profile="general"):
        self.source.write_text(text, encoding="utf-8")
        return analyze(self.source, profile)

    AUDIT = ("[SWAT] BASELINE READ-ONLY NETWORK AUDIT: State=Dormant, Live=0\n"
             "[SWAT] REGISTRY MEMBERSHIP: ID=officer1, ObjectId=42\n"
             "[SWAT] SCENE REGISTRY: ID=officer1, SceneId=123\n"
             "[SWAT] DEPLOY REJECTED: lifecycle research locked, State=Dormant, Live=0\n")

    def test_audit_profile_expected_gate_and_unrelated_requirements(self):
        report = self.analyze_text(self.AUDIT, "read-only-audit")
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(report["expected_rejection_lines"], [4])
        self.assertEqual(report["missing_evidence"], [])
        self.assertEqual(self.analyze_text(self.AUDIT)["verdict"], "NEEDS ATTENTION")

    def test_audit_does_not_hide_errors_or_unexpected_rejections(self):
        for line in ("[ERROR] unrelated plugin broke\n", "TEST VERDICT: FAIL\n",
                     "[SWAT] DEPLOY REJECTED: invalid prefab, State=Dormant, Live=0\n",
                     "[SWAT] State=Deployed, Live=1\n"):
            with self.subTest(line=line):
                self.assertEqual(self.analyze_text(self.AUDIT + line, "read-only-audit")["verdict"], "NEEDS ATTENTION")

    def test_audit_requires_actual_audit_evidence(self):
        report = self.analyze_text("[SWAT] DEPLOY REJECTED: lifecycle research locked, State=Dormant, Live=0\n", "read-only-audit")
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        self.assertTrue(report["missing_evidence"])

    def test_subsystem_profiles_do_not_require_other_subsystems(self):
        report = self.analyze_text("GOON ACTUAL TRANSITION: UNSPAWNED_TO_SPAWNED\n"
                                   "GOON ACTUAL TRANSITION: SPAWNED_TO_UNSPAWNED\n", "goon-lifecycle")
        self.assertEqual(report["verdict"], "PASS")
        report = self.analyze_text("[SWAT] State=Deployed, Live=1\n", "swat-deployment")
        self.assertEqual(report["verdict"], "PASS")

    def test_item_name_is_not_test_evidence(self):
        item = parse_line("[HylandHeat] Registered item Hyland Test Cap", 1)
        self.assertNotIn("test_event", item["categories"])
        self.assertIn("test_event", parse_line("[HylandHeat] OFFICERLEE2 GUID TEST: Same=False", 2)["categories"])

    def test_late_results_survive_section_limit(self):
        report = self.analyze_text("".join(f"[SWAT] GOON SNAPSHOT: NPCID=g{i}\n" for i in range(50))
                                   + "[SWAT] PREFAB CATALOG COMPLETE: Candidates=0\nTEST VERDICT: FAIL\n")
        rendered = render_report(report)
        swat_section = rendered.split("\nSWAT\n", 1)[1].split("\nPOLICE,", 1)[0]
        self.assertIn("PREFAB CATALOG COMPLETE", swat_section)
        self.assertIn("TEST VERDICT: FAIL", render_brief(report))

    def test_stack_trace_first_occurrence_and_source_lines(self):
        report = self.analyze_text("[ERROR] System.IO.IOException: failed\n"
                                   "   at System.IO.File.Move()\n   at SwapperPlugin.Plugin.OnPreInitialization()\n"
                                   "ordinary unrelated line\n"
                                   "[ERROR] System.IO.IOException: failed\n   at OtherCaller()\n")
        event = report["detected_events"][0]
        self.assertEqual(event["count"], 2)
        self.assertEqual([f["line"] for f in event["stack_trace"]], [2, 3])
        self.assertIn("SwapperPlugin.Plugin.OnPreInitialization", render_brief(report))
        self.assertEqual(report["matching_lines"], 2)

    def test_brief_has_fixed_character_budget_and_export(self):
        report = self.analyze_text("".join(f"[ERROR] failure {i}: {'x' * 1000}\n" for i in range(100)))
        brief = render_brief(report)
        self.assertLessEqual(len(brief), 6000)
        self.assertIn("Selection is incomplete", brief)
        paths = write_reports(report, self.root / "reports", include_brief=True)
        self.assertEqual(paths[-1].name, "playtest_brief.txt")
        self.assertEqual(paths[-1].read_text(), brief)

    def test_actual_transition_with_real_multiline_format(self):
        report = self.analyze_text(
            "[12:01:00.100] [HylandHeat] GOON ACTUAL TRANSITION: Classification=UNSPAWNED_TO_SPAWNED\n"
            "[12:01:00.101] [HylandHeat] TRANSITION BEFORE: NPCID=goon1, IsGoonSpawned=False, UnityInstanceID=42\n"
            "[12:01:00.102] [HylandHeat] TRANSITION AFTER: NPCID=goon1, IsGoonSpawned=True, UnityInstanceID=42\n")
        transition = report["transitions"][0]
        self.assertEqual(transition["identity"], "goon1")
        self.assertEqual(transition["before_fields"]["isgoonspawned"], "False")
        self.assertEqual(transition["after_state"], "SPAWNED")
        self.assertEqual(transition["source_lines"], [1, 2, 3])
        self.assertEqual(report["verdict"], "INCONCLUSIVE")

    def test_instance_event_and_snapshot_are_not_actual_transitions(self):
        report = self.analyze_text("GOON INSTANCE EVENT: Classification=INSTANCE_REPLACED\n"
                                   "GOON SNAPSHOT: Classification=UNSPAWNED_TO_SPAWNED\n")
        self.assertEqual(report["transitions"], [])
        self.assertEqual(report["category_counts"]["replacement"], 1)

    def test_duplicate_identity_warning(self):
        report = self.analyze_text("[WARNING] GOON DUPLICATE IDENTITY: Field=SceneId, Value=123\n")
        self.assertEqual(report["severity_counts"]["WARNING"], 1)
        self.assertEqual(report["category_counts"]["duplicate_identity"], 1)
        self.assertEqual(report["verdict"], "NEEDS ATTENTION")

    def test_duplicate_audit_does_not_mean_duplicate(self):
        report = self.analyze_text("GOON DUPLICATE AUDIT: Duplicates=NoneAmongObservedFields\n")
        self.assertNotIn("duplicate_identity", report["category_counts"])

    def test_swat_rejection_and_no_tracked_unit(self):
        report = self.analyze_text("[SWAT] DEPLOY REJECTED: lifecycle research locked, State=Dormant, Live=0\n"
                                   "[SWAT] RECALL IGNORED: no tracked unit, State=Dormant, Live=0\n")
        self.assertEqual(report["verdict"], "NEEDS ATTENTION")
        self.assertEqual(report["swat_deployment_confirmations"], [])

    def test_success_requires_state_and_unit_evidence(self):
        for text in ("[SWAT] State=Deployed, Live=2\n",
                     "[SWAT] DEPLOY SUCCESS\n[SWAT] Live=1\n",
                     "[SWAT] State=Deployed\n[SWAT] TRACKED UNIT: ID=swat1\n"):
            with self.subTest(text=text):
                self.assertEqual(len(self.analyze_text(text)["swat_deployment_confirmations"]), 1)
        for text in ("[SWAT] State=Deployed\n", "[SWAT] Live=1\n",
                     "[SWAT] not deployed, Live=1\n", "[SWAT] State=Deployed, Live=0\n",
                     "[SWAT] State=Deployed\n[SWAT] RECALL COMPLETE\n[SWAT] Live=1\n",
                     "[SWAT] State=Deployed\n[HylandHeat] SESSION END\n[SWAT] Live=1\n"):
            with self.subTest(text=text):
                self.assertFalse(self.analyze_text(text)["swat_deployment_confirmations"])

    def test_error_and_exception_priority(self):
        report = self.analyze_text("[ERROR] [SwapperPlugin] System.IO.IOException: File already exists.\n"
                                   "System.NullReferenceException: missing object\n")
        self.assertEqual(report["severity_counts"]["ERROR"], 1)
        self.assertEqual(report["severity_counts"]["EXCEPTION"], 1)
        self.assertEqual(report["verdict"], "NEEDS ATTENTION")

    def test_repeated_warning_normalizes_only_timestamp(self):
        report = self.analyze_text("".join(f"[12:00:{i:02d}.000] [WARNING] [HylandHeat] census unavailable\n" for i in range(40)))
        self.assertEqual(report["matching_lines"], 40)
        self.assertEqual(report["unique_logical_events"], 1)
        self.assertEqual(report["repeat_occurrences"], 39)
        self.assertEqual(report["repeated_messages"][0]["count"], 40)
        self.assertEqual(report["repeated_messages"][0]["line"], 1)
        self.assertIn("40x", render_report(report))

    def test_case_and_iso_timestamp(self):
        item = parse_line("[2026-09-07T12:00:00.1234567-05:00] [warn] goon duplicate identity: NPCID=g1", 8)
        self.assertEqual(item["identity"], "g1")
        self.assertEqual(item["timestamp"], "2026-09-07T12:00:00.1234567-05:00")
        self.assertIn("duplicate_identity", item["categories"])

    def test_exports_input_unchanged_and_decoding(self):
        content = b"\xef\xbb\xbf[12:00:00] [ERROR] bad byte \xff\n"
        self.source.write_bytes(content)
        report = analyze(self.source)
        paths = write_reports(report, self.root / "reports", include_csv=True)
        self.assertEqual(len(paths), 3)
        self.assertEqual(json.loads(paths[1].read_text())["metadata"]["decode_replacement_lines"], 1)
        self.assertEqual(self.source.read_bytes(), content)

    def test_output_alias_cannot_overwrite_source(self):
        report = self.analyze_text("[ERROR] important\n")
        output = self.root / "reports"
        output.mkdir()
        (output / "playtest_summary.txt").symlink_to(self.source)
        with self.assertRaises(ValueError):
            write_reports(report, output)
        self.assertEqual(self.source.read_text(), "[ERROR] important\n")

    def test_empty_log_is_inconclusive(self):
        report = self.analyze_text("")
        self.assertEqual(report["verdict"], "INCONCLUSIVE")
        self.assertIsNone(report["metadata"]["first_timestamp"])

    def test_pass_requires_all_evidence(self):
        report = self.analyze_text("GOON ACTUAL TRANSITION: Classification=UNSPAWNED_TO_SPAWNED\n"
                                   "GOON ACTUAL TRANSITION: Classification=SPAWNED_TO_UNSPAWNED\n"
                                   "[SWAT] DEPLOY SUCCESS: State=Deployed, Live=1\n")
        self.assertEqual(report["verdict"], "PASS")

    def test_failed_verdict(self):
        report = self.analyze_text("[HylandHeat] TEST VERDICT: FAIL\n")
        self.assertEqual(report["test_verdicts"][0]["test_verdict"], "FAIL")
        self.assertEqual(report["verdict"], "NEEDS ATTENTION")

    def test_inconclusive_test_prevents_pass(self):
        report = self.analyze_text("GOON ACTUAL TRANSITION: Classification=UNSPAWNED_TO_SPAWNED\n"
                                   "GOON ACTUAL TRANSITION: Classification=SPAWNED_TO_UNSPAWNED\n"
                                   "[SWAT] State=Deployed, Live=1\n"
                                   "TEST VERDICT: INCONCLUSIVE\n")
        self.assertEqual(report["verdict"], "INCONCLUSIVE")

    def test_each_repeated_transition_retains_its_context(self):
        block = "GOON ACTUAL TRANSITION: Classification=UNSPAWNED_TO_SPAWNED\n"
        report = self.analyze_text(block + "TRANSITION BEFORE: NPCID=one\nTRANSITION AFTER: NPCID=one\n"
                                   + block + "TRANSITION BEFORE: NPCID=two\nTRANSITION AFTER: NPCID=two\n")
        self.assertEqual([t["identity"] for t in report["transitions"]], ["one", "two"])
        self.assertEqual(report["transition_counts"]["UNSPAWNED_TO_SPAWNED"], 2)

    def test_cli_text_only(self):
        self.analyze_text("[HylandHeat] Hyland Heat loaded.\n")
        output = self.root / "cli_reports"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.source), "--output", str(output), "--no-json"]), 0)
        self.assertEqual([p.suffix for p in output.iterdir()], [".txt"])

    def test_cli_failure_is_readable(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main([str(self.root / "missing.log")]), 2)
        self.assertIn("Error:", error.getvalue())


if __name__ == "__main__":
    unittest.main()
