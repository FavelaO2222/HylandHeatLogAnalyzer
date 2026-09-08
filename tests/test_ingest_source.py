"""Tests for database.ingest_source: file-level document ingestion and its CLI."""

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from database import db, ingest_source
from database.search import search


def run_git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)


class IngestSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'research.db'
        db.initialize_database(self.path)

    def write_tree(self, files):
        source = self.root / 'source'
        source.mkdir()
        for relative, content in files.items():
            full = source / relative
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding='utf-8')
        return source

    def test_ingests_matching_files_and_skips_others(self):
        source = self.write_tree({
            'A.cs': 'class A { void Foo() {} }',
            'Sub/B.cs': 'class B {}',
            'notes.txt': 'irrelevant',
        })
        result = ingest_source.ingest_directory(self.path, 'source_code', source)
        self.assertEqual(result['documents_stored'], 2)
        self.assertEqual(result['unchanged'], 0)
        self.assertEqual(result['undecodable'], 0)
        self.assertEqual(result['collection'], str(source.resolve()))
        self.assertIsNone(result['mod_revision'])
        with closing(db.connect_database(self.path)) as connection:
            paths = {row[0] for row in connection.execute('SELECT relative_path FROM source_documents')}
            self.assertEqual(paths, {'A.cs', 'Sub/B.cs'})
            language, digest = connection.execute(
                "SELECT language, content_sha256 FROM source_documents WHERE relative_path='A.cs'").fetchone()
            self.assertEqual(language, 'csharp')
            self.assertEqual(digest, hashlib.sha256(b'class A { void Foo() {} }').hexdigest())
            artifact = connection.execute(
                'SELECT artifact_type, path, notes FROM source_artifacts WHERE id=?',
                (result['source_artifact_id'],)).fetchone()
            self.assertEqual(artifact[0], 'source_code')
            self.assertEqual(artifact[1], str(source))
            self.assertEqual(json.loads(artifact[2]),
                             {'file_count': 2, 'unchanged': 0, 'undecodable': 0, 'mod_revision': None})

    def test_custom_extensions_are_case_insensitive(self):
        source = self.write_tree({'a.CS': 'class A {}', 'b.py': 'pass'})
        result = ingest_source.ingest_directory(self.path, 'source_code', source, extensions=('.cs',))
        self.assertEqual(result['documents_stored'], 1)

    def test_undecodable_file_is_skipped_not_fatal(self):
        source = self.write_tree({'good.cs': 'class Good {}'})
        (source / 'bad.cs').write_bytes(b'\xff\xfe\x00binary garbage')
        result = ingest_source.ingest_directory(self.path, 'source_code', source)
        self.assertEqual(result['documents_stored'], 1)
        self.assertEqual(result['undecodable'], 1)

    def test_changed_content_creates_a_new_row_preserving_history(self):
        source = self.write_tree({'A.cs': 'class A {}'})
        first = ingest_source.ingest_directory(self.path, 'source_code', source)
        (source / 'A.cs').write_text('class A { void Changed() {} }', encoding='utf-8')
        second = ingest_source.ingest_directory(self.path, 'source_code', source)
        self.assertNotEqual(first['source_artifact_id'], second['source_artifact_id'])
        self.assertEqual(second['documents_stored'], 1)
        self.assertEqual(second['unchanged'], 0)
        with closing(db.connect_database(self.path)) as connection:
            # Both versions are preserved as separate rows, not overwritten.
            self.assertEqual(connection.execute('SELECT count(*) FROM source_documents').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 2)

    def test_unchanged_content_is_not_duplicated_but_artifact_row_still_created(self):
        source = self.write_tree({'A.cs': 'class A {}', 'B.cs': 'class B {}'})
        first = ingest_source.ingest_directory(self.path, 'source_code', source)
        second = ingest_source.ingest_directory(self.path, 'source_code', source)
        self.assertEqual(second['documents_stored'], 0)
        self.assertEqual(second['unchanged'], 2)
        self.assertNotEqual(first['source_artifact_id'], second['source_artifact_id'])
        with closing(db.connect_database(self.path)) as connection:
            # No duplicate document rows, but the second ingestion attempt is
            # still logged as its own source_artifacts row (an audit trail of
            # when the collection was last checked), even though it added no data.
            self.assertEqual(connection.execute('SELECT count(*) FROM source_documents').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT count(*) FROM source_artifacts').fetchone()[0], 2)

    def test_partial_change_only_inserts_the_changed_file(self):
        source = self.write_tree({'A.cs': 'class A {}', 'B.cs': 'class B {}'})
        ingest_source.ingest_directory(self.path, 'source_code', source)
        (source / 'A.cs').write_text('class A { void Changed() {} }', encoding='utf-8')
        result = ingest_source.ingest_directory(self.path, 'source_code', source)
        self.assertEqual(result['documents_stored'], 1)
        self.assertEqual(result['unchanged'], 1)
        with closing(db.connect_database(self.path)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM source_documents').fetchone()[0], 3)

    def test_explicit_collection_overrides_root_path_default(self):
        source = self.write_tree({'A.cs': 'class A {}'})
        first = ingest_source.ingest_directory(self.path, 'source_code', source, collection='stable-id')
        self.assertEqual(first['collection'], 'stable-id')
        # A different, ephemeral-looking root, same explicit collection,
        # same unchanged content -> still recognized as unchanged.
        other_root = self.root / 'other-copy'
        other_root.mkdir()
        (other_root / 'A.cs').write_text('class A {}', encoding='utf-8')
        second = ingest_source.ingest_directory(self.path, 'source_code', other_root, collection='stable-id')
        self.assertEqual(second['documents_stored'], 0)
        self.assertEqual(second['unchanged'], 1)

    def test_different_collections_do_not_dedup_against_each_other(self):
        source = self.write_tree({'A.cs': 'class A {}'})
        ingest_source.ingest_directory(self.path, 'source_code', source, collection='collection-one')
        result = ingest_source.ingest_directory(self.path, 'source_code', source, collection='collection-two')
        self.assertEqual(result['documents_stored'], 1)
        self.assertEqual(result['unchanged'], 0)

    def test_mod_repo_records_git_revision(self):
        repo = self.root / 'mod-repo'
        repo.mkdir()
        run_git(repo, 'init', '-q')
        run_git(repo, 'config', 'user.email', 'test@example.com')
        run_git(repo, 'config', 'user.name', 'Test')
        (repo / 'a.cs').write_text('// a', encoding='utf-8')
        run_git(repo, 'add', 'a.cs')
        run_git(repo, 'commit', '-q', '-m', 'initial')
        sha = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'],
                             check=True, capture_output=True, text=True).stdout.strip()
        source = self.write_tree({'A.cs': 'class A {}'})
        result = ingest_source.ingest_directory(self.path, 'source_code', source, mod_repo=repo)
        self.assertEqual(result['mod_revision'], sha)

    def test_invalid_artifact_type_is_rejected(self):
        source = self.write_tree({'A.cs': 'class A {}'})
        with self.assertRaisesRegex(ValueError, 'artifact_type must be one of'):
            ingest_source.ingest_directory(self.path, 'log', source)

    def test_missing_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'not a directory'):
            ingest_source.ingest_directory(self.path, 'source_code', self.root / 'nonexistent')

    def test_requires_schema_v5(self):
        v1_path = self.root / 'v1.db'
        import sqlite3
        connection = sqlite3.connect(v1_path)
        connection.executescript(Path(__file__).with_name('fixtures').joinpath('schema_v1.sql').read_text())
        connection.close()
        source = self.write_tree({'A.cs': 'class A {}'})
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            ingest_source.ingest_directory(v1_path, 'source_code', source)

    def test_ingested_documents_are_searchable(self):
        source = self.write_tree({'PoliceOfficer.cs': 'public void BeginFootPursuit_Networked() {}'})
        ingest_source.ingest_directory(self.path, 'decompiler_export', source)
        result = search(self.path, 'BeginFootPursuit', type='documents')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['relative_path'], 'PoliceOfficer.cs')


class IngestSourceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'research.db'
        db.initialize_database(self.path)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'A.cs').write_text('class A {}', encoding='utf-8')

    def test_cli_success(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(ingest_source.main(
                ['--database', str(self.path), '--root', str(self.source), '--artifact-type', 'source_code']), 0)
        text = output.getvalue()
        self.assertIn('Documents stored: 1', text)

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(ingest_source.main(
                ['--database', str(self.path), '--root', str(self.root / 'missing'),
                 '--artifact-type', 'source_code']), 2)
        self.assertIn('Error:', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_cli_invalid_artifact_type_choice_rejected_by_argparse(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as ctx:
            ingest_source.main(['--database', str(self.path), '--root', str(self.source),
                                '--artifact-type', 'log'])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
