"""Tests for the shared validation helpers used by all three manual research-record CLIs."""

from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from database import db
from database.research_records import require_row, resolve_entity_name


class ResearchRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def entity(self, entity_type='game_object', name='OfficerLee'):
        with self.connection:
            return self.connection.execute(
                'INSERT INTO entities (entity_type, name, canonical_name) VALUES (?, ?, ?)',
                (entity_type, name, name.lower())).lastrowid

    def test_resolve_entity_name_matches_by_name_or_canonical_name(self):
        entity_id = self.entity(name='OfficerLee')
        self.assertEqual(resolve_entity_name(self.connection, 'officerlee'), entity_id)
        self.assertEqual(resolve_entity_name(self.connection, 'OfficerLee'), entity_id)

    def test_resolve_entity_name_unmatched_raises(self):
        with self.assertRaisesRegex(ValueError, "No entity named 'Nobody'"):
            resolve_entity_name(self.connection, 'Nobody')

    def test_resolve_entity_name_ambiguous_raises_with_disambiguation_hint(self):
        self.entity(entity_type='game_object', name='OfficerLee')
        self.entity(entity_type='npc', name='OfficerLee')
        with self.assertRaisesRegex(ValueError, 'matches more than one entity.*subject_entity_id to disambiguate'):
            resolve_entity_name(self.connection, 'OfficerLee')

    def test_require_row_allows_none(self):
        require_row(self.connection, 'entities', None, 'entity')  # must not raise

    def test_require_row_allows_existing_row(self):
        entity_id = self.entity()
        require_row(self.connection, 'entities', entity_id, 'entity')  # must not raise

    def test_require_row_rejects_missing_row(self):
        with self.assertRaisesRegex(ValueError, 'No entity with id 999'):
            require_row(self.connection, 'entities', 999, 'entity')


if __name__ == '__main__':
    unittest.main()
