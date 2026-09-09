"""Tests for database.file_index: the recursive, navigable file/directory
index and its soft cross-reference to source_documents."""

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from database import db, file_index


class FileIndexFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.root / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)

    def write(self, relative_path, content='x'):
        target = self.project / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')

    def make_source_document(self, relative_path, content, collection):
        with self.connection:
            artifact_id = self.connection.execute(
                "INSERT INTO source_artifacts (artifact_type, path, filename) VALUES ('source_code', 'r', 'r')"
            ).lastrowid
            return self.connection.execute(
                '''INSERT INTO source_documents (source_artifact_id, relative_path, content, collection)
                   VALUES (?, ?, ?, ?)''', (artifact_id, relative_path, content, collection)).lastrowid


class IngestTests(FileIndexFixture):
    def test_ingest_directory_creates_file_and_directory_nodes(self):
        self.write('pkg/a.py', 'x = 1\ny = 2\n')
        self.write('root.py', 'z = 3\n')
        result = file_index.ingest_directory(self.path, 'proj', self.project)
        self.assertEqual(result['files'], 2)
        self.assertEqual(result['directories'], 1)
        rows = {row[0]: row for row in self.connection.execute(
            'SELECT path, is_directory, bytes, lines, extension FROM file_index ORDER BY path')}
        self.assertIn('pkg', rows)
        self.assertEqual(rows['pkg'][1], 1)
        self.assertEqual(rows['pkg/a.py'][1], 0)
        self.assertEqual(rows['pkg/a.py'][3], 2)
        self.assertEqual(rows['root.py'][4], '.py')

    def test_empty_collection_rejected(self):
        with self.assertRaisesRegex(ValueError, 'collection must not be empty'):
            file_index.ingest_scan(self.path, '  ', {'files': [], 'root': str(self.project)})

    def test_reingest_fully_replaces_collection(self):
        self.write('a.py', 'x')
        file_index.ingest_directory(self.path, 'proj', self.project)
        (self.project / 'a.py').unlink()
        self.write('b.py', 'x')
        file_index.ingest_directory(self.path, 'proj', self.project)
        paths = {row[0] for row in self.connection.execute(
            "SELECT path FROM file_index WHERE collection = 'proj'")}
        self.assertEqual(paths, {'b.py'})

    def test_reingest_does_not_touch_other_collections(self):
        self.write('a.py', 'x')
        file_index.ingest_directory(self.path, 'proj-one', self.project)
        file_index.ingest_directory(self.path, 'proj-two', self.project)
        file_index.ingest_directory(self.path, 'proj-one', self.project)
        counts = dict(self.connection.execute(
            'SELECT collection, count(*) FROM file_index GROUP BY collection'))
        self.assertEqual(counts, {'proj-one': 1, 'proj-two': 1})

    def test_empty_directory_produces_no_node(self):
        (self.project / 'empty').mkdir()
        self.write('a.py', 'x')
        file_index.ingest_directory(self.path, 'proj', self.project)
        paths = {row[0] for row in self.connection.execute("SELECT path FROM file_index")}
        self.assertNotIn('empty', paths)

    def test_requires_schema_v8(self):
        from phase4_fixtures import create_v1
        v1_path = self.root / 'v1.db'
        create_v1(v1_path)
        with self.assertRaisesRegex(ValueError, 'database.init_db'):
            file_index.ingest_directory(v1_path, 'proj', self.project)


class NavigationTests(FileIndexFixture):
    def setUp(self):
        super().setUp()
        self.write('pkg/sub/deep.py', 'a\nb\nc\n')
        self.write('pkg/other.py', 'x\n')
        self.write('root.py', 'y\n')
        file_index.ingest_directory(self.path, 'proj', self.project)

    def test_children_root_level(self):
        items = file_index.children(self.path, 'proj')
        names = {item['name'] for item in items}
        self.assertEqual(names, {'pkg', 'root.py'})
        self.assertEqual(items[0]['name'], 'pkg')  # directories before files

    def test_children_of_directory(self):
        items = file_index.children(self.path, 'proj', 'pkg')
        self.assertEqual({item['name'] for item in items}, {'sub', 'other.py'})

    def test_tree_full_collection(self):
        result = file_index.tree(self.path, 'proj')
        paths = {item['path'] for item in result['items']}
        self.assertEqual(paths, {'pkg', 'pkg/sub', 'pkg/sub/deep.py', 'pkg/other.py', 'root.py'})

    def test_tree_subtree_excludes_unrelated_branches(self):
        result = file_index.tree(self.path, 'proj', 'pkg')
        paths = {item['path'] for item in result['items']}
        self.assertEqual(paths, {'pkg/sub', 'pkg/sub/deep.py', 'pkg/other.py'})
        self.assertNotIn('root.py', paths)
        self.assertNotIn('pkg', paths)  # the subtree root itself is excluded

    def test_tree_respects_limit_and_reports_omitted(self):
        result = file_index.tree(self.path, 'proj', limit=2)
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['total'], 5)
        self.assertEqual(result['omitted'], 3)

    def test_tree_nonexistent_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "No path 'missing'"):
            file_index.tree(self.path, 'proj', 'missing')

    def test_find_by_name_substring(self):
        result = file_index.find(self.path, 'deep')
        self.assertEqual([item['path'] for item in result['items']], ['pkg/sub/deep.py'])

    def test_find_scoped_by_collection_and_extension(self):
        file_index.ingest_directory(self.path, 'other-proj', self.project)
        result = file_index.find(self.path, 'py', collection='proj', extension='.py')
        self.assertTrue(all(item['collection'] == 'proj' for item in result['items']))
        self.assertEqual(len(result['items']), 3)


class ContextTests(FileIndexFixture):
    def test_context_for_file_with_no_source_document_match(self):
        self.write('a.py', 'x\ny\n')
        file_index.ingest_directory(self.path, 'proj', self.project)
        result = file_index.context(self.path, 'proj', 'a.py')
        self.assertEqual(result['node']['path'], 'a.py')
        self.assertIsNone(result['source_document'])

    def test_context_for_file_with_source_document_match(self):
        self.write('a.py', 'x\ny\n')
        file_index.ingest_directory(self.path, 'proj', self.project)
        self.make_source_document('a.py', 'x\ny\n', collection='proj')
        result = file_index.context(self.path, 'proj', 'a.py')
        self.assertIsNotNone(result['source_document'])
        self.assertEqual(result['source_document']['artifact_type'], 'source_code')

    def test_context_ignores_source_document_in_different_collection(self):
        self.write('a.py', 'x\n')
        file_index.ingest_directory(self.path, 'proj', self.project)
        self.make_source_document('a.py', 'x\n', collection='other-collection')
        result = file_index.context(self.path, 'proj', 'a.py')
        self.assertIsNone(result['source_document'])

    def test_context_reports_siblings_and_children(self):
        self.write('pkg/a.py', 'x')
        self.write('pkg/b.py', 'x')
        file_index.ingest_directory(self.path, 'proj', self.project)
        result = file_index.context(self.path, 'proj', 'pkg/a.py')
        self.assertEqual([s['name'] for s in result['siblings']], ['b.py'])
        result = file_index.context(self.path, 'proj', 'pkg')
        self.assertEqual({c['name'] for c in result['children']}, {'a.py', 'b.py'})

    def test_context_nonexistent_path_rejected(self):
        with self.assertRaisesRegex(ValueError, "No path 'missing'"):
            file_index.context(self.path, 'proj', 'missing')


class RenderTests(FileIndexFixture):
    def test_format_tree_and_context(self):
        self.write('a.py', 'x\ny\n')
        file_index.ingest_directory(self.path, 'proj', self.project)
        tree_text = file_index.format_tree(file_index.tree(self.path, 'proj'), 'proj')
        self.assertIn('a.py', tree_text)
        self.assertIn('2 line(s)', tree_text)
        context_text = file_index.format_context(file_index.context(self.path, 'proj', 'a.py'))
        self.assertIn('a.py (file', context_text)
        self.assertIn('source_documents match: none', context_text)

    def test_format_tree_empty(self):
        text = file_index.format_tree({'items': [], 'total': 0, 'omitted': 0}, 'proj')
        self.assertEqual(text, 'No matches found.')


class CliTests(FileIndexFixture):
    def test_cli_ingest_and_tree(self):
        self.write('a.py', 'x\n')
        output = io.StringIO()
        with redirect_stdout(output):
            code = file_index.main(['--database', str(self.path), 'ingest', str(self.project), '--collection', 'proj'])
        self.assertEqual(code, 0)
        self.assertIn('Indexed', output.getvalue())

        output = io.StringIO()
        with redirect_stdout(output):
            file_index.main(['--database', str(self.path), 'tree', 'proj'])
        self.assertIn('a.py', output.getvalue())

    def test_cli_json_format(self):
        self.write('a.py', 'x\n')
        with redirect_stdout(io.StringIO()):
            file_index.main(['--database', str(self.path), 'ingest', str(self.project), '--collection', 'proj'])
        output = io.StringIO()
        with redirect_stdout(output):
            file_index.main(['--database', str(self.path), 'tree', 'proj', '--format', 'json'])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed['items'][0]['path'], 'a.py')

    def test_cli_context(self):
        self.write('a.py', 'x\n')
        with redirect_stdout(io.StringIO()):
            file_index.main(['--database', str(self.path), 'ingest', str(self.project), '--collection', 'proj'])
        output = io.StringIO()
        with redirect_stdout(output):
            code = file_index.main(['--database', str(self.path), 'context', 'proj', 'a.py'])
        self.assertEqual(code, 0)
        self.assertIn('a.py (file', output.getvalue())

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = file_index.main(['--database', str(self.path), 'context', 'proj', 'missing'])
        self.assertEqual(code, 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
