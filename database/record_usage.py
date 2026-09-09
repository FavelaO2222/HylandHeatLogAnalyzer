"""Manual CLI to record LLM token usage: one row per API call an external
agent (Rider/PyCharm/ChatGPT/...) makes while using this database.

Nothing here infers or estimates a token count. Every input_tokens/
output_tokens value is exactly what the caller supplies -- pulled from their
own API response's usage field at the call site, not re-estimated from logs
afterward. cost_usd is likewise caller-supplied: this module has no pricing
table and does not compute one, since a hardcoded price would silently go
stale. See database.usage_report for summarizing recorded rows and for the
compact-packet-vs-raw-log token *estimate* comparison -- a distinct,
explicitly-labeled estimate, never mixed into these real recorded numbers.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .research_records import require_row

RETRIEVAL_MODES = ('compact_packet', 'raw_log', 'other')


def add_usage(database, agent, model, input_tokens, output_tokens, *, cost_usd=None,
              retrieval_mode=None, query_text=None, experiment_id=None, test_run_id=None,
              source_artifact_id=None, note=None, occurred_at=None):
    if not agent or not agent.strip():
        raise ValueError('agent must not be empty.')
    if not model or not model.strip():
        raise ValueError('model must not be empty.')
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        raise ValueError('input_tokens must be a nonnegative integer.')
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
        raise ValueError('output_tokens must be a nonnegative integer.')
    if cost_usd is not None and cost_usd < 0:
        raise ValueError('cost_usd must be nonnegative.')
    if retrieval_mode is not None and retrieval_mode not in RETRIEVAL_MODES:
        raise ValueError(f'retrieval_mode must be one of {RETRIEVAL_MODES}.')
    links = (experiment_id, test_run_id, source_artifact_id)
    if sum(link is not None for link in links) > 1:
        raise ValueError('Specify at most one of experiment_id, test_run_id, or source_artifact_id.')
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=7)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            require_row(connection, 'experiments', experiment_id, 'experiment')
            require_row(connection, 'test_runs', test_run_id, 'test run')
            require_row(connection, 'source_artifacts', source_artifact_id, 'source artifact')
            params = (agent, model, input_tokens, output_tokens, cost_usd, retrieval_mode, query_text,
                     experiment_id, test_run_id, source_artifact_id, note)
            if occurred_at is not None:
                return connection.execute(
                    '''INSERT INTO agent_usage
                       (agent, model, input_tokens, output_tokens, cost_usd, retrieval_mode, query_text,
                        experiment_id, test_run_id, source_artifact_id, note, occurred_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    params + (occurred_at,)).lastrowid
            return connection.execute(
                '''INSERT INTO agent_usage
                   (agent, model, input_tokens, output_tokens, cost_usd, retrieval_mode, query_text,
                    experiment_id, test_run_id, source_artifact_id, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', params).lastrowid


def list_usage(database, *, agent=None, model=None, since=None, until=None):
    clauses, params = [], []
    if agent:
        clauses.append('agent = ?')
        params.append(agent)
    if model:
        clauses.append('model = ?')
        params.append(model)
    if since:
        clauses.append('occurred_at >= ?')
        params.append(since)
    if until:
        clauses.append('occurred_at <= ?')
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=7)
        return connection.execute(f'''
            SELECT id, agent, model, input_tokens, output_tokens, cost_usd, retrieval_mode, query_text,
                   experiment_id, test_run_id, source_artifact_id, note, occurred_at
            FROM agent_usage {where} ORDER BY id''', params).fetchall()


def format_usage_row(row):
    cost = f"${row['cost_usd']:.4f}" if row['cost_usd'] is not None else 'cost unknown'
    link = next((f"{name} #{row[name]}" for name in ('experiment_id', 'test_run_id', 'source_artifact_id')
                if row[name] is not None), 'no link')
    return (f"[{row['id']}] {row['occurred_at']} {row['agent']}/{row['model']}: "
            f"{row['input_tokens']}in+{row['output_tokens']}out tokens, {cost} ({link})")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='Record one LLM call\'s real, API-reported token usage.')
    add.add_argument('--agent', required=True, help='e.g. Rider, PyCharm, ChatGPT.')
    add.add_argument('--model', required=True, help='e.g. claude-sonnet-5, gpt-4o.')
    add.add_argument('--input-tokens', type=int, required=True)
    add.add_argument('--output-tokens', type=int, required=True)
    add.add_argument('--cost-usd', type=float, help='Caller-computed cost; no pricing table is assumed here.')
    add.add_argument('--retrieval-mode', choices=RETRIEVAL_MODES,
                     help='compact_packet if answered from database.research\'s packet, raw_log if fed raw logs.')
    add.add_argument('--query-text', help='The research question/symbol this call answered, if any.')
    link = add.add_mutually_exclusive_group()
    link.add_argument('--experiment-id', type=int)
    link.add_argument('--test-run-id', type=int)
    link.add_argument('--source-artifact-id', type=int)
    add.add_argument('--note', help='Freeform context, e.g. when no specific link applies.')
    add.add_argument('--occurred-at', help='ISO timestamp of the actual API call; defaults to now.')

    listing = commands.add_parser('list', help='List recorded usage rows.')
    listing.add_argument('--agent')
    listing.add_argument('--model')
    listing.add_argument('--since', help='ISO timestamp lower bound (inclusive), compared as text.')
    listing.add_argument('--until', help='ISO timestamp upper bound (inclusive), compared as text.')

    args = parser.parse_args(argv)
    try:
        if args.command == 'add':
            usage_id = add_usage(
                args.database, args.agent, args.model, args.input_tokens, args.output_tokens,
                cost_usd=args.cost_usd, retrieval_mode=args.retrieval_mode, query_text=args.query_text,
                experiment_id=args.experiment_id, test_run_id=args.test_run_id,
                source_artifact_id=args.source_artifact_id, note=args.note, occurred_at=args.occurred_at)
            print(f'Recorded usage {usage_id}.')
        else:
            rows = list_usage(args.database, agent=args.agent, model=args.model,
                              since=args.since, until=args.until)
            print('\n'.join(format_usage_row(row) for row in rows) if rows else 'No usage recorded.')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
