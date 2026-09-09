"""Tests for project_scanner.py; every scanned project is a temp directory."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

import project_scanner as scanner


class ScannerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, relative_path, content='content'):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path


class ScanProjectTests(ScannerFixture):
    def test_finds_useful_files_recursively(self):
        self.write('a.py', 'print(1)\n')
        self.write('pkg/b.py', 'x = 1\ny = 2\n')
        result = scanner.scan_project(self.root)
        paths = {f['path'] for f in result['files']}
        self.assertEqual(paths, {'a.py', 'pkg/b.py'})
        b = next(f for f in result['files'] if f['path'] == 'pkg/b.py')
        self.assertEqual(b['lines'], 2)
        self.assertEqual(b['extension'], '.py')
        self.assertFalse(b['truncated'])

    def test_nonexistent_root_rejected(self):
        with self.assertRaisesRegex(ValueError, 'is not a directory'):
            scanner.scan_project(self.root / 'missing')

    def test_default_ignore_dirs_excluded(self):
        self.write('.git/config', 'x')
        self.write('__pycache__/a.pyc', 'x')
        self.write('.venv/lib/x.py', 'x')
        self.write('kept.py', 'x')
        result = scanner.scan_project(self.root)
        self.assertEqual({f['path'] for f in result['files']}, {'kept.py'})
        self.assertGreater(result['summary']['skipped_ignored'], 0)

    def test_binary_file_skipped_and_counted(self):
        (self.root / 'binary.dat').write_bytes(b'\x00\x01\x02binary')
        self.write('text.py', 'x = 1\n')
        result = scanner.scan_project(self.root)
        self.assertEqual({f['path'] for f in result['files']}, {'text.py'})
        self.assertEqual(result['summary']['skipped_binary'], 1)

    def test_gitignore_respected_by_default(self):
        # .gitignore itself is legitimate project content and stays scanned;
        # only the paths it lists are excluded.
        self.write('.gitignore', 'ignored_dir/\n*.tmp\n')
        self.write('ignored_dir/x.py', 'x')
        self.write('a.tmp', 'x')
        self.write('kept.py', 'x')
        result = scanner.scan_project(self.root)
        self.assertEqual({f['path'] for f in result['files']}, {'.gitignore', 'kept.py'})
        self.assertIn('.gitignore', result['ignore_sources'])

    def test_no_gitignore_flag_disables_it(self):
        self.write('.gitignore', '*.tmp\n')
        self.write('a.tmp', 'x')
        result = scanner.scan_project(self.root, use_gitignore=False)
        self.assertIn('a.tmp', {f['path'] for f in result['files']})
        self.assertNotIn('.gitignore', result['ignore_sources'])

    def test_extra_ignore_patterns_applied(self):
        self.write('secret.env', 'x')
        self.write('kept.py', 'x')
        result = scanner.scan_project(self.root, extra_ignore=('*.env',))
        self.assertEqual({f['path'] for f in result['files']}, {'kept.py'})
        self.assertIn('--extra-ignore', result['ignore_sources'])

    def test_generated_output_files_always_excluded(self):
        self.write('PROJECT_MAP.json', '{}')
        self.write('AGENT_INDEX.md', '# x')
        self.write('kept.py', 'x')
        result = scanner.scan_project(self.root)
        self.assertEqual({f['path'] for f in result['files']}, {'kept.py'})

    def test_large_file_truncated_not_line_counted(self):
        self.write('big.txt', 'x' * 100)
        result = scanner.scan_project(self.root, max_file_bytes=10)
        big = next(f for f in result['files'] if f['path'] == 'big.txt')
        self.assertTrue(big['truncated'])
        self.assertIsNone(big['lines'])
        self.assertEqual(big['bytes'], 100)

    def test_invalid_max_file_bytes_rejected(self):
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            scanner.scan_project(self.root, max_file_bytes=0)

    def test_empty_directory_not_counted(self):
        (self.root / 'empty_dir').mkdir()
        self.write('kept.py', 'x')
        result = scanner.scan_project(self.root)
        self.assertEqual(result['summary']['total_directories'], 0)

    def test_symlinked_directory_not_followed(self):
        target = self.root / 'real_dir'
        target.mkdir()
        (target / 'inside.py').write_text('x', encoding='utf-8')
        try:
            (self.root / 'link_dir').symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks not supported in this environment')
        result = scanner.scan_project(self.root)
        self.assertEqual({f['path'] for f in result['files']}, {'real_dir/inside.py'})
        self.assertEqual(result['summary']['skipped_symlink_dirs'], 1)

    def test_by_extension_counts(self):
        self.write('a.py', 'x')
        self.write('b.py', 'x')
        self.write('c.md', 'x')
        result = scanner.scan_project(self.root)
        self.assertEqual(result['summary']['by_extension'], {'.md': 1, '.py': 2})


class RenderTests(ScannerFixture):
    def test_agent_index_reports_directory_hierarchy_and_counts(self):
        self.write('pkg/sub/deep.py', 'x')
        self.write('pkg/other.py', 'x')
        self.write('root.py', 'x')
        result = scanner.scan_project(self.root)
        text = scanner.render_agent_index(result)
        self.assertIn('pkg/ (2 files)', text)
        self.assertIn('sub/ (1 file)', text)
        self.assertIn('root.py', text)
        self.assertIn('## File types', text)
        self.assertIn('`.py`: 3 file(s)', text)

    def test_agent_index_omits_individual_nested_filenames(self):
        self.write('pkg/deep.py', 'x')
        result = scanner.scan_project(self.root)
        text = scanner.render_agent_index(result)
        self.assertNotIn('deep.py', text)

    def test_project_map_json_is_valid_and_has_tree(self):
        self.write('pkg/a.py', 'x')
        result = scanner.scan_project(self.root)
        parsed = json.loads(scanner.render_project_map_json(result))
        self.assertIn('tree', parsed)
        self.assertIn('pkg', parsed['tree']['dirs'])
        self.assertEqual(parsed['tree']['dirs']['pkg']['files'], ['a.py'])


class CliTests(ScannerFixture):
    def test_cli_writes_both_files(self):
        self.write('a.py', 'x = 1\n')
        output = io.StringIO()
        with redirect_stdout(output):
            code = scanner.main([str(self.root)])
        self.assertEqual(code, 0)
        map_path = self.root / 'PROJECT_MAP.json'
        index_path = self.root / 'AGENT_INDEX.md'
        self.assertTrue(map_path.is_file())
        self.assertTrue(index_path.is_file())
        self.assertIn('a.py', json.loads(map_path.read_text())['files'][0]['path'])
        self.assertIn('Agent Index', index_path.read_text())

    def test_cli_output_dir_option(self):
        self.write('a.py', 'x')
        out_dir = self.root.parent / 'scan-output'
        output = io.StringIO()
        with redirect_stdout(output):
            code = scanner.main([str(self.root), '--output-dir', str(out_dir)])
        self.assertEqual(code, 0)
        self.assertTrue((out_dir / 'PROJECT_MAP.json').is_file())

    def test_cli_extra_ignore_option(self):
        self.write('secret.env', 'x')
        self.write('a.py', 'x')
        with redirect_stdout(io.StringIO()):
            scanner.main([str(self.root), '--extra-ignore', '*.env'])
        data = json.loads((self.root / 'PROJECT_MAP.json').read_text())
        self.assertEqual({f['path'] for f in data['files']}, {'a.py'})

    def test_cli_error_path(self):
        error = io.StringIO()
        with redirect_stderr(error):
            code = scanner.main([str(self.root / 'missing')])
        self.assertEqual(code, 2)
        self.assertIn('Error:', error.getvalue())


if __name__ == '__main__':
    unittest.main()
