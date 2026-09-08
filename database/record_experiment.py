"""Manual CLI to record experiments: a deliberate observation ("does X change
during Y") plus what was actually observed -- distinct from a finding (a
conclusion, possibly drawn from several such observations). Nothing here
infers anything; every field is exactly what the caller supplies.

--source-log accepts either a raw game log or a pre-summarized brief text
(e.g. Latest_brief.txt) -- either way it's registered as an ordinary
source_artifacts row (sha256-deduped, so re-recording the same file twice
reuses the artifact, the same pattern database.ingestion already uses for
log ingestion) rather than being structurally parsed; the observed_result
field is what actually captures the finding, in the caller's own words.
"""

import argparse
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row

_ARTIFACT_TYPE_BY_SUFFIX = {'.log': 'log'}


def _register_source_log(connection, source_log):
    path = Path(source_log).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'{path} is not a file.')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    artifact_type = _ARTIFACT_TYPE_BY_SUFFIX.get(path.suffix.lower(), 'research_note')
    existing = connection.execute(
        'SELECT id FROM source_artifacts WHERE artifact_type=? AND sha256=? ORDER BY id LIMIT 1',
        (artifact_type, digest)).fetchone()
    if existing:
        return existing['id']
    return connection.execute(
        'INSERT INTO source_artifacts (artifact_type, path, filename, sha256) VALUES (?, ?, ?, ?)',
        (artifact_type, str(path), path.name, digest)).lastrowid


def add_experiment(database, question, observed_result, *, symbols=(), source_log=None,
                   test_run_id=None, mod_repo=None):
    if not question or not question.strip():
        raise ValueError('question must not be empty.')
    if not observed_result or not observed_result.strip():
        raise ValueError('observed_result must not be empty.')
    mod_build = None
    if mod_repo is not None:
        from .source_revision import capture_revision
        mod_build = capture_revision(mod_repo)
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=5)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            require_row(connection, 'test_runs', test_run_id, 'test run')
            source_artifact_id = _register_source_log(connection, source_log) if source_log is not None else None
            experiment_id = connection.execute(
                '''INSERT INTO experiments (question, observed_result, source_artifact_id, test_run_id, mod_build)
                   VALUES (?, ?, ?, ?, ?)''',
                (question, observed_result, source_artifact_id, test_run_id, mod_build)).lastrowid
            connection.executemany(
                'INSERT INTO experiment_symbols (experiment_id, symbol) VALUES (?, ?)',
                [(experiment_id, symbol.strip()) for symbol in symbols if symbol.strip()])
    return experiment_id


def list_experiments(database, symbol=None):
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=5)
        if symbol:
            rows = connection.execute('''
                SELECT DISTINCT e.id, e.question, e.observed_result, e.mod_build, e.created_at
                FROM experiments e JOIN experiment_symbols s ON s.experiment_id = e.id
                WHERE s.symbol = ? ORDER BY e.id''', (symbol,)).fetchall()
        else:
            rows = connection.execute(
                'SELECT id, question, observed_result, mod_build, created_at FROM experiments ORDER BY id').fetchall()
        result = []
        for row in rows:
            symbols = [r[0] for r in connection.execute(
                'SELECT symbol FROM experiment_symbols WHERE experiment_id=? ORDER BY id', (row['id'],))]
            result.append({**dict(row), 'symbols': symbols})
        return result


def format_experiment_row(row):
    symbols = ', '.join(row['symbols']) if row['symbols'] else '-'
    return f"[{row['id']}] ({symbols}) {row['question']} -> {row['observed_result']}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record a new experiment.')
    add.add_argument('--question', required=True)
    add.add_argument('--observed-result', required=True)
    add.add_argument('--symbols', help='Comma-separated symbols this experiment relates to.')
    add.add_argument('--source-log', help='Path to the raw log or brief text this observation came from.')
    add.add_argument('--test-run-id', type=int)
    add.add_argument('--mod-repo', help='Optional mod source repo path; its git commit is recorded.')

    listing = commands.add_parser('list', help='List recorded experiments.')
    listing.add_argument('--symbol', help='Filter to experiments tagged with this exact symbol.')

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            symbols = tuple(args.symbols.split(',')) if args.symbols else ()
            experiment_id = add_experiment(args.database, args.question, args.observed_result,
                                           symbols=symbols, source_log=args.source_log,
                                           test_run_id=args.test_run_id, mod_repo=args.mod_repo)
            print(f'Recorded experiment {experiment_id}.')
        else:
            rows = list_experiments(args.database, args.symbol)
            print('\n'.join(format_experiment_row(row) for row in rows) if rows else 'No experiments recorded.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
