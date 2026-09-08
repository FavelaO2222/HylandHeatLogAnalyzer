"""Tests for database.source_revision: git commit/dirty-state capture."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from database.source_revision import capture_revision


def run_git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)


class SourceRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        run_git(self.repo, 'init', '-q')
        run_git(self.repo, 'config', 'user.email', 'test@example.com')
        run_git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'tracked.txt').write_text('one\n', encoding='utf-8')
        run_git(self.repo, 'add', 'tracked.txt')
        run_git(self.repo, 'commit', '-q', '-m', 'initial')

    def head_short_sha(self):
        return subprocess.run(['git', '-C', str(self.repo), 'rev-parse', '--short', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_clean_checkout_returns_plain_short_sha(self):
        self.assertEqual(capture_revision(self.repo), self.head_short_sha())

    def test_uncommitted_tracked_edit_is_dirty(self):
        (self.repo / 'tracked.txt').write_text('two\n', encoding='utf-8')
        self.assertEqual(capture_revision(self.repo), f'{self.head_short_sha()}-dirty')

    def test_untracked_new_file_is_dirty(self):
        (self.repo / 'new.txt').write_text('new\n', encoding='utf-8')
        self.assertEqual(capture_revision(self.repo), f'{self.head_short_sha()}-dirty')

    def test_non_repo_path_raises_value_error(self):
        not_a_repo = Path(self.temp.name) / 'not-a-repo'
        not_a_repo.mkdir()
        with self.assertRaisesRegex(ValueError, 'not a usable git repository'):
            capture_revision(not_a_repo)

    def test_missing_git_binary_raises_file_not_found(self):
        with patch('subprocess.run', side_effect=FileNotFoundError('git not found')):
            with self.assertRaises(FileNotFoundError):
                capture_revision(self.repo)

    def test_read_only_never_mutates_the_repo(self):
        before = self.head_short_sha()
        status_before = subprocess.run(['git', '-C', str(self.repo), 'status', '--porcelain'],
                                       check=True, capture_output=True, text=True).stdout
        capture_revision(self.repo)
        self.assertEqual(self.head_short_sha(), before)
        status_after = subprocess.run(['git', '-C', str(self.repo), 'status', '--porcelain'],
                                      check=True, capture_output=True, text=True).stdout
        self.assertEqual(status_before, status_after)


if __name__ == '__main__':
    unittest.main()