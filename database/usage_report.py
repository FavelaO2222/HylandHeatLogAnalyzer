"""Report tooling for database.record_usage's agent_usage table.

Two distinct things live here, and this module is careful never to blur
them together:

1. `summarize` aggregates *real* recorded rows (agent_usage.input_tokens/
   output_tokens/cost_usd) -- exactly the API-reported numbers a caller
   supplied when recording each call. No estimation happens here.
2. `compare_packet_vs_raw` is an *estimate*, for a single database.research
   query, of token cost under two hypothetical framings: answering it from
   the compact evidence packet database.research already builds, versus
   answering it by feeding an LLM the full raw content that packet was
   built from. No LLM call happens for either side -- both figures come
   from a simple chars/4 heuristic, clearly labeled as an estimate, since
   this is the one place in the project where a real token count isn't
   available to measure instead. This is the brief-first design's actual
   value proposition, made concrete rather than assumed.
"""

import argparse
from contextlib import closing
import json
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from . import research as research_module

# A widely-used rough approximation (~4 characters per token for English/
# code text) -- not a real tokenizer. Good enough to compare two framings
# of the *same* underlying text against each other; not meant to predict
# any specific model's actual token count. See the module docstring.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _estimate_tokens(text):
    return max(1, round(len(text) / _CHARS_PER_TOKEN_ESTIMATE)) if text else 0


def summarize(database, *, agent=None, model=None, since=None, until=None):
    """Aggregate agent_usage rows by (agent, model). Real numbers only --
    see the module docstring. cost_usd is summed only across rows where
    it's known; unknown_cost_calls counts the rest so an incomplete total
    is never presented as if it were complete.
    """
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
        rows = connection.execute(f'''
            SELECT agent, model, count(*) AS calls, sum(input_tokens) AS input_tokens,
                   sum(output_tokens) AS output_tokens,
                   sum(cost_usd) AS cost_usd,
                   sum(cost_usd IS NULL) AS unknown_cost_calls
            FROM agent_usage {where}
            GROUP BY agent, model ORDER BY agent, model''', params).fetchall()
    groups = [dict(row) for row in rows]
    totals = {
        'calls': sum(g['calls'] for g in groups),
        'input_tokens': sum(g['input_tokens'] for g in groups),
        'output_tokens': sum(g['output_tokens'] for g in groups),
        'cost_usd': sum(g['cost_usd'] for g in groups if g['cost_usd'] is not None) or None,
        'unknown_cost_calls': sum(g['unknown_cost_calls'] for g in groups),
    }
    return {'filters': {'agent': agent, 'model': model, 'since': since, 'until': until},
            'groups': groups, 'totals': totals}


def format_summary(result):
    lines = ['# Agent usage summary']
    f = result['filters']
    active = [f'{key}={value}' for key, value in f.items() if value]
    lines.append('Filters: ' + (', '.join(active) if active else 'none') + '\n')
    if not result['groups']:
        lines.append('No usage recorded.')
        return '\n'.join(lines)
    for g in result['groups']:
        cost = f"${g['cost_usd']:.4f}" if g['cost_usd'] is not None else 'unknown'
        unknown = f" ({g['unknown_cost_calls']} call(s) with unknown cost)" if g['unknown_cost_calls'] else ''
        lines.append(f"- {g['agent']}/{g['model']}: {g['calls']} call(s), "
                     f"{g['input_tokens']} in + {g['output_tokens']} out tokens, cost {cost}{unknown}")
    t = result['totals']
    cost = f"${t['cost_usd']:.4f}" if t['cost_usd'] is not None else 'unknown'
    unknown = f" ({t['unknown_cost_calls']} call(s) with unknown cost)" if t['unknown_cost_calls'] else ''
    lines.append(f"\nTotal: {t['calls']} call(s), {t['input_tokens']} in + {t['output_tokens']} out tokens, "
                 f"cost {cost}{unknown}")
    return '\n'.join(lines)


def _document_raw_text(connection, relative_path, artifact_type):
    row = connection.execute('''
        SELECT d.content FROM source_documents d JOIN source_artifacts a ON a.id = d.source_artifact_id
        WHERE d.relative_path = ? AND a.artifact_type = ? ORDER BY d.id DESC LIMIT 1''',
        (relative_path, artifact_type)).fetchone()
    return row['content'] if row else ''


def _event_error_raw_text(connection, source, item_id):
    table = 'events' if source == 'event' else 'errors'
    columns = 'message' if table == 'events' else 'message, stack_trace'
    row = connection.execute(f'SELECT {columns} FROM {table} WHERE id = ?', (item_id,)).fetchone()
    if row is None:
        return ''
    return ' '.join(part for part in (row[key] for key in row.keys()) if part)


def compare_packet_vs_raw(database, query, *, limit=6):
    """Estimated token cost of the same `limit`-bounded match set database.research
    returns, rendered two ways: the compact packet (what a caller actually gets),
    versus the full raw content of every matched document/event/error (what a
    caller would have to paste in without this database's distillation). See
    the module docstring: both numbers are estimates, not real API counts.
    """
    result = research_module.research(database, query, limit=limit)
    packet_text = research_module.format_research(result)
    packet_tokens = _estimate_tokens(packet_text)

    raw_parts = []
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=7)
        for key in ('mod_source', 'decompiled'):
            artifact_type = 'source_code' if key == 'mod_source' else 'decompiler_export'
            for item in result['sections'][key]['items']:
                raw_parts.append(_document_raw_text(connection, item['relative_path'], artifact_type))
        for key in ('events', 'errors'):
            for item in result['sections'][key]['items']:
                raw_parts.append(_event_error_raw_text(connection, item['source'], item['id']))
    raw_text = '\n'.join(raw_parts)
    raw_tokens = _estimate_tokens(raw_text)

    reduction_pct = round(100 * (1 - packet_tokens / raw_tokens), 1) if raw_tokens else None
    return {'query': query, 'limit': limit, 'mode': result['mode'],
            'packet_tokens_estimate': packet_tokens, 'raw_tokens_estimate': raw_tokens,
            'reduction_pct_estimate': reduction_pct,
            # len(items), not ['total'] -- 'total' counts every match regardless
            # of --limit, but only the (possibly limit-truncated) items list is
            # what actually got fed into raw_tokens_estimate above.
            'matched_items': sum(len(result['sections'][k]['items']) for k in
                                 ('mod_source', 'decompiled', 'events', 'errors'))}


def format_comparison(result):
    lines = [f'# Packet vs. raw token estimate: "{result["query"]}"',
            f"(estimate only -- see database.usage_report's module docstring; "
            f"mode={result['mode']}, {result['matched_items']} matched item(s) within --limit {result['limit']})",
            '']
    lines.append(f"Compact packet (database.research output): ~{result['packet_tokens_estimate']} tokens")
    lines.append(f"Raw underlying content (full matched documents/events/errors): "
                f"~{result['raw_tokens_estimate']} tokens")
    if result['reduction_pct_estimate'] is not None:
        lines.append(f"Estimated reduction: {result['reduction_pct_estimate']}%")
    else:
        lines.append('No raw content to compare against (no matches).')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    summary = commands.add_parser('summary', help='Summarize recorded agent_usage rows.')
    summary.add_argument('--agent')
    summary.add_argument('--model')
    summary.add_argument('--since', help='ISO timestamp lower bound (inclusive), compared as text.')
    summary.add_argument('--until', help='ISO timestamp upper bound (inclusive), compared as text.')
    summary.add_argument('--format', choices=('text', 'json'), default='text')

    compare = commands.add_parser(
        'compare', help='Estimate token cost of a database.research query: compact packet vs. raw content.')
    compare.add_argument('query')
    compare.add_argument('--limit', type=int, default=research_module.DEFAULT_LIMIT)
    compare.add_argument('--format', choices=('text', 'json'), default='text')

    args = parser.parse_args(argv)
    try:
        if args.command == 'summary':
            result = summarize(args.database, agent=args.agent, model=args.model,
                               since=args.since, until=args.until)
            print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
                  else format_summary(result))
        else:
            result = compare_packet_vs_raw(args.database, args.query, limit=args.limit)
            print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
                  else format_comparison(result))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
