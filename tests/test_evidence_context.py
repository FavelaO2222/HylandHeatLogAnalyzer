import unittest

from database.context_builder import build_context, build_entity_context
from database.evidence import attach_evidence
from phase4_fixtures import ResearchFixture


class EvidenceContextTests(ResearchFixture, unittest.TestCase):
    def test_both_context_modes_include_evidence_for_all_record_types(self):
        for record, target in (('finding', 'event'), ('unknown', 'run'), ('decision', 'relationship')):
            attach_evidence(self.path, record, 1, target, 1)
        for text in (build_context(self.path), build_entity_context(self.path, entity_id=1)):
            self.assertIn('Evidence (finding #1): Event #1', text)
            self.assertIn('Evidence (unknown #1): Run #1', text)
            self.assertIn('Evidence (decision #1): Relationship #1', text)

    def test_evidence_expansion_is_bounded_without_hiding_total(self):
        for target in ('run', 'event', 'error', 'entity', 'relationship'):
            attach_evidence(self.path, 'finding', 1, target, 1)
        attach_evidence(self.path, 'finding', 1, 'run', 2)
        attach_evidence(self.path, 'finding', 1, 'event', 2)
        text = build_entity_context(self.path, entity_id=1)
        self.assertIn('+1 more (list_evidence)', text)
        self.assertNotIn('Event #2', text)

    def test_character_budgets_hold_with_evidence_and_oversized_headers(self):
        attach_evidence(self.path, 'finding', 1, 'event', 1)
        with self.connection:
            self.connection.execute('UPDATE entities SET description=? WHERE id=1', ('X' * 10000,))
            self.connection.execute('UPDATE source_artifacts SET path=?', ('Y' * 10000,))
        for budget in (1, 10, 50, 100, 300, 700, 6000):
            for builder in (lambda: build_context(self.path, max_chars=budget),
                            lambda: build_entity_context(self.path, entity_id=1, max_chars=budget)):
                with self.subTest(budget=budget):
                    text = builder()
                    self.assertLessEqual(len(text), budget)
                    if 'Recorded finding' in text:
                        self.assertIn('Evidence (finding #1): Event #1', text)

    def test_invalid_budgets_and_row_limits(self):
        for value in (0, -1, True, 3.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_context(self.path, max_chars=value)
        for kwargs in ({'max_events': -1}, {'max_research_rows': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_context(self.path, **kwargs)

    def test_no_evidence_does_not_create_or_imply_links(self):
        text = build_entity_context(self.path, entity_id=1)
        self.assertNotIn('Evidence (', text)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM evidence_links').fetchone()[0], 0)
