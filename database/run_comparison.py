"""Compact, deterministic differences between already-ingested runs (A -> B)."""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .entity_extraction import ENTITY_TYPE, extract_names
from .evidence import bounded_limit, positive_id
from .ingestion import IMPORTER, PRIMARY_CATEGORIES

DEFAULT_LIMIT = 8
TEXT_LIMIT = 240
ERROR_FIELDS = ('severity', 'component', 'message', 'stack_trace')
EVENT_FIELDS = ('category', 'component', 'event_type', 'message')


def _preview(value):
    if not isinstance(value, str):
        return value
    text = ' '.join(value.split())
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT - 1] + '…'


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _page(items, limit):
    return {'total': len(items), 'items': items[:limit], 'omitted': max(0, len(items) - limit)}


def _manifest(notes):
    try:
        value = json.loads(notes or '{}')
    except (ValueError, TypeError):
        return {}
    if isinstance(value, dict) and value.get('importer') == IMPORTER and value.get('analysis_schema') == 2:
        return value
    return {}


def _load_run(connection, run_id):
    positive_id(run_id, 'run_id')
    row = connection.execute('''SELECT t.*, a.path AS source_path, a.sha256 AS source_sha256
        FROM test_runs t LEFT JOIN source_artifacts a ON a.id=t.source_artifact_id
        WHERE t.id=?''', (run_id,)).fetchone()
    if row is None:
        raise ValueError(f'No test run with id {run_id}.')
    run = dict(row)
    run['manifest'] = _manifest(run['notes'])
    run['events'] = connection.execute('SELECT * FROM events WHERE test_run_id=? ORDER BY id', (run_id,)).fetchall()
    run['errors'] = connection.execute('SELECT * FROM errors WHERE test_run_id=? ORDER BY id', (run_id,)).fetchall()
    return run


def _event_weight(run, row):
    events = run['manifest'].get('events')
    entry = events.get(str(row['id'])) if isinstance(events, dict) else None
    value = entry.get('count') if isinstance(entry, dict) else None
    # Absent/unrecognized/malformed metadata never manufactures repeat counts.
    valid = type(value) is int and 1 <= value <= 9223372036854775807
    return (value, False) if valid else (1, True)


def _groups(run, table, fields):
    groups, counts, fallbacks = {}, Counter(), 0
    for row in run[table]:
        key = tuple(row[field] for field in fields)
        weight, fallback = _event_weight(run, row) if table == 'events' else (1, False)
        fallbacks += fallback
        if key not in groups:
            groups[key] = {'count': 0, 'example_id': row['id']}
        groups[key]['count'] += weight
        if table == 'events':
            counts[(row['category'], row['event_type'])] += weight
    return groups, counts, fallbacks


def _sort_key(key, table):
    if table == 'events':
        rank = PRIMARY_CATEGORIES.index(key[0]) if key[0] in PRIMARY_CATEGORIES else len(PRIMARY_CATEGORIES)
    else:
        rank = {'FATAL': 0, 'EXCEPTION': 1, 'ERROR': 2}.get(key[0], 3)
    # Null and empty strings remain distinct in equality AND in sort ordering.
    return rank, tuple((value is not None, value or '') for value in key)


def _group_diff(a, b, fields, table, limit):
    added, removed, changed = [], [], []
    for key in sorted(a.keys() | b.keys(), key=lambda k: _sort_key(k, table)):
        left, right = a.get(key), b.get(key)
        count_a, count_b = left['count'] if left else 0, right['count'] if right else 0
        if count_a == count_b:
            continue
        item = {field: _preview(value) for field, value in zip(fields, key)}
        item.update(signature_sha256=_fingerprint(key), count_a=count_a, count_b=count_b,
                    delta=count_b - count_a, example_id_a=left['example_id'] if left else None,
                    example_id_b=right['example_id'] if right else None)
        (added if left is None else removed if right is None else changed).append(item)
    return {'added': _page(added, limit), 'removed': _page(removed, limit),
            'count_changes': _page(changed, limit)}


def _catalog(connection):
    rows = connection.execute('SELECT id, entity_type, name, canonical_name FROM entities WHERE entity_type=?',
                              (ENTITY_TYPE,)).fetchall()
    lookup = {}
    for row in rows:
        for name in {row['name'], row['canonical_name']} - {None}:
            lookup.setdefault(name.lower(), {})[row['id']] = dict(row)
    return lookup


def _entities(run, catalog):
    names = {name.lower() for table in ('events', 'errors') for row in run[table]
             for name in extract_names(row['message'])}
    found, unmatched, ambiguous = {}, 0, 0
    for name in sorted(names):
        matches = catalog.get(name, {})
        if len(matches) == 1:
            found.update(matches)
        elif matches:
            ambiguous += 1
        else:
            unmatched += 1
    return found, {'unmatched_names': unmatched, 'ambiguous_names': ambiguous}


def compare_runs(run_a, run_b, database=None, *, limit=DEFAULT_LIMIT):
    """Compare exact stored signatures; bounded previews never affect matching or totals.

    Presence means an unambiguous cataloged game_object name tagged in stored
    event/error messages using the existing extractor. It is not a lifecycle claim.
    """
    bounded_limit(limit)
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        connection.execute('BEGIN')  # All reads share one SQLite snapshot.
        a, b = _load_run(connection, run_a), _load_run(connection, run_b)
        catalog = _catalog(connection)
        entities_a, coverage_a = _entities(a, catalog)
        entities_b, coverage_b = _entities(b, catalog)

    metadata = []
    for field in ('profile', 'result', 'started_at', 'game_build', 'mod_build',
                  'source_artifact_id', 'source_path', 'source_sha256', 'original_verdict', 'notes'):
        def value(run):
            if field == 'original_verdict':
                verdict = run['manifest'].get(field)
                return verdict if isinstance(verdict, str) else None
            if field == 'notes' and run['manifest']:
                return None  # Never dump a large import manifest as a metadata difference.
            return run[field]
        left, right = value(a), value(b)
        if left != right:
            metadata.append({'field': field, 'a': _preview(left), 'b': _preview(right),
                             'value_sha256_a': _fingerprint(left), 'value_sha256_b': _fingerprint(right)})

    errors_a, _, _ = _groups(a, 'errors', ERROR_FIELDS)
    errors_b, _, _ = _groups(b, 'errors', ERROR_FIELDS)
    events_a, counts_a, fallbacks_a = _groups(a, 'events', EVENT_FIELDS)
    events_b, counts_b, fallbacks_b = _groups(b, 'events', EVENT_FIELDS)
    errors = _group_diff(errors_a, errors_b, ERROR_FIELDS, 'errors', limit)
    events = _group_diff(events_a, events_b, EVENT_FIELDS, 'events', limit)
    event_counts = [{'category': _preview(key[0]), 'event_type': _preview(key[1]),
                     'signature_sha256': _fingerprint(key),
                     'a': counts_a[key], 'b': counts_b[key], 'delta': counts_b[key] - counts_a[key]}
                    for key in sorted(counts_a.keys() | counts_b.keys(), key=lambda k: _sort_key(k, 'events'))
                    if counts_a[key] != counts_b[key]]

    def entity_items(left, right):
        return [{key: _preview(value) for key, value in right[entity_id].items()}
                for entity_id in sorted(right.keys() - left.keys())]

    entities = {'added': _page(entity_items(entities_a, entities_b), limit),
                'removed': _page(entity_items(entities_b, entities_a), limit)}
    totals = {name: {'a': left, 'b': right, 'delta': right - left} for name, left, right in (
        ('error_rows', len(a['errors']), len(b['errors'])),
        ('event_rows', len(a['events']), len(b['events'])),
        ('event_occurrences', sum(counts_a.values()), sum(counts_b.values())),
        ('entities', len(entities_a), len(entities_b)))}
    identical = not (metadata or event_counts or any(item['delta'] for item in totals.values())
                     or any(page['total'] for section in (errors, events, entities) for page in section.values()))
    return {'comparison_version': 1, 'run_a': run_a, 'run_b': run_b, 'identical': identical,
            'limit': limit, 'text_limit': TEXT_LIMIT, 'metadata': _page(metadata, limit),
            'totals': totals, 'errors': errors, 'events': events,
            'event_counts': _page(event_counts, limit), 'entities': entities,
            'coverage': {'a': dict(coverage_a, event_rows_without_repeat_count=fallbacks_a),
                         'b': dict(coverage_b, event_rows_without_repeat_count=fallbacks_b)},
            'rules': {
                'matching': 'Exact fields; IDs, timestamps and source lines excluded from event/error signatures.',
                'events': 'All stored categories, ranked by ingestion priority; valid v2 importer counts or one per row.',
                'entities': 'Unambiguous cataloged game_object names tagged by the existing extractor in stored messages; no lifecycle inference.',
                'metadata': 'Run/source fields and importer verdict; insertion times and other manifest fields excluded.'}}


def format_comparison(result):
    lines = [f"# Run comparison: #{result['run_a']} -> #{result['run_b']}",
             'No differences under the documented comparison rules.' if result['identical'] else 'Structured differences (A -> B):', '']
    lines.extend(f"- {name}: {value['a']} -> {value['b']} ({value['delta']:+d})"
                 for name, value in result['totals'].items())

    def section(title, page, render):
        if not page['total']:
            return
        lines.extend(['', f"## {title} ({page['total']})"])
        lines.extend('- ' + render(item) for item in page['items'])
        if page['omitted']:
            lines.append(f"- … {page['omitted']} omitted; increase --limit (maximum 100).")

    section('Metadata', result['metadata'], lambda item: f"{item['field']}: {item['a']!r} -> {item['b']!r}")
    for name in ('errors', 'events'):
        for kind, page in result[name].items():
            section(f'{name.capitalize()} {kind.replace("_", " ")}', page,
                    lambda item: f"[{item.get('severity', item.get('category'))}] {item['message']} "
                    f"({item['count_a']} -> {item['count_b']}; IDs {item['example_id_a']} -> {item['example_id_b']}; "
                    f"signature {item['signature_sha256'][:12]})")
    section('Event counts', result['event_counts'],
            lambda item: f"{item['category']} / {item['event_type']}: {item['a']} -> {item['b']} ({item['delta']:+d})")
    for kind, page in result['entities'].items():
        section(f'Entities {kind}', page, lambda item: f"{item['name']} (entity #{item['id']})")
    lines.extend(['', 'Entity presence is tagged-name evidence, not a spawn/despawn claim.',
                  'Coverage: ' + json.dumps(result['coverage'], sort_keys=True)])
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_a', type=int)
    parser.add_argument('run_b', type=int)
    parser.add_argument('--database')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='Rows per difference list (1-100).')
    parser.add_argument('--format', choices=('markdown', 'json'), default='markdown')
    args = parser.parse_args(argv)
    try:
        result = compare_runs(args.run_a, args.run_b, args.database, limit=args.limit)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
              else format_comparison(result), end='\n' if args.format == 'json' else '')
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
