"""Tests for githooks/pre-commit, run end-to-end inside a disposable git repository.

The database package is copied into that scratch repo (not the whole
project) so the hook's own subprocess call to `python -m database.backup`
resolves paths against the scratch repo, exactly as it would in a real
clone. The real project's already-imported database.db is used only to seed
a valid database file directly (never through the hook's subprocess).
"""

from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

from database import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PreCommitHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        shutil.copytree(PROJECT_ROOT / 'database', self.repo / 'database')
        shutil.copytree(PROJECT_ROOT / 'githooks', self.repo / 'githooks')
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.repo, check=True)
        hook_path = self.repo / '.git' / 'hooks' / 'pre-commit'
        shutil.copy(self.repo / 'githooks' / 'pre-commit', hook_path)
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)

    def seed_database(self, corrupt=False):
        database = self.repo / 'data' / 'hylandheat.db'
        database.parent.mkdir(parents=True, exist_ok=True)
        db.initialize_database(database)
        if corrupt:
            # Bypass connect_database's enforcement to plant a real foreign key violation,
            # forcing the hook's dump attempt to fail its own check.
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO events (test_run_id, source_artifact_id, category, message) VALUES (999, 999, 'x', 'y')")
            connection.commit()
            connection.close()
        return database

    def commit(self):
        (self.repo / 'trivial.txt').write_text('content', encoding='utf-8')
        subprocess.run(['git', 'add', 'trivial.txt'], cwd=self.repo, check=True, capture_output=True, text=True)
        return subprocess.run(['git', 'commit', '-m', 'test commit'], cwd=self.repo, capture_output=True, text=True)

    def test_skips_silently_when_no_database_exists(self):
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / 'backups' / 'hylandheat.sql').exists())

    def test_refreshes_and_stages_backup_into_the_same_commit(self):
        self.seed_database()
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup_file = self.repo / 'backups' / 'hylandheat.sql'
        self.assertTrue(backup_file.exists())
        self.assertIn('CREATE TABLE', backup_file.read_text(encoding='utf-8'))

        status = subprocess.run(['git', 'status', '--porcelain'], cwd=self.repo,
                                 capture_output=True, text=True, check=True)
        self.assertNotIn('backups', status.stdout)  # not left uncommitted/untracked

        show = subprocess.run(['git', 'show', '--stat', 'HEAD'], cwd=self.repo,
                               capture_output=True, text=True, check=True)
        self.assertIn('backups/hylandheat.sql', show.stdout)

    def test_does_not_block_commit_when_dump_fails_and_leaves_no_backup(self):
        self.seed_database(corrupt=True)
        result = self.commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('was not refreshed', result.stderr)
        self.assertFalse((self.repo / 'backups' / 'hylandheat.sql').exists())

if __name__ == '__main__':
    unittest.main()
