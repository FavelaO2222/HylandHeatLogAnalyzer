"""Compact, honest, cross-artifact evidence packet for a symbol or short
research question -- the "AI consumer" endpoint for the research database as
a *system*, not one artifact class at a time. Given e.g. "NPC.EnterBuilding",
returns: Hyland Heat call/patch sites, decompiled declaration/implementation
locations, related runtime-log events/errors, related findings/unknowns/
decisions, and matching recorded experiments (see database.record_experiment)
-- each section explicitly bounded, and each explicitly reporting "No
matches found." rather than being silently omitted when empty, so an absent
category is a visible, honest fact here, never an invented one.

Declaration-vs-reference classification for source/decompiled hits happens
transiently at query time, scanning the already-fetched source_documents.
content with a small regex heuristic -- not a separately persisted symbol
index that could drift out of sync with the content it's supposed to
describe. This is necessarily a heuristic, not a real C#/IL2CPP parser: it
looks for a class/struct/interface/enum or method-signature-shaped line
containing the token, same spirit as the rest of this codebase's regex-based
classification (see patterns.py, entity_extraction.py).

The "interpretation" and "next research target" lines are mechanical and
structural (which categories have/lack coverage), never a causal claim about
game behavior -- this tool has no basis to assert one.
"""

import argparse
from contextlib import closing
import json
import re
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .evidence import bounded_limit
from .search import search as fts_search

DEFAULT_LIMIT = 6
_TOKEN = re.compile(r'[A-Za-z_]\w{2,}')
# A short research *question* ("What code explains when X happens during
# Y?") inevitably mixes filler words in with the one or two that actually
# identify what's being asked about. Left in, they'd either force an
# unreasonably narrow AND (nothing contains "explains") or blow an OR wide
# open (almost everything contains "code" or "public"). Not exhaustive --
# just common enough function words to matter for a plain-English question.
_STOPWORDS = frozenset('''
    a an the this that these those what which who whom when where why how
    is are was were be been being do does did doing will would can could
    should shall may might must have has had having
    and or but not no nor if then than so
    to of in on at by for with from as into about explain explains
    explained code source runtime evidence change changes changed happens
    happen during between while
'''.split())
_MODIFIER = r'(?:public|private|protected|internal|static|sealed|abstract|partial|virtual|override|new|unsafe|async)'
_TYPE_DECL = re.compile(
    r'^\s*(?:\[[^\]]*\]\s*)*(?:' + _MODIFIER + r'\s+)+(?:class|struct|interface|enum)\s+(\w+)')
_METHOD_DECL = re.compile(
    r'^\s*(?:\[[^\]]*\]\s*)*(?:' + _MODIFIER + r'\s+)+[\w<>\[\],\.\?]+\s+(\w+)\s*\(')
# C# keywords/built-in type names: near-universal in any matched line, so
# useless as a "co-occurring symbol" next-research-target suggestion (see
# _next_target) -- filtering these out is what keeps that suggestion a real
# symbol name instead of "public" or "void".
_CODE_NOISE = frozenset(_MODIFIER.strip('(?:)').split('|')) | frozenset('''
    class struct interface enum void return using namespace null true false
    get set this base out ref params var object string int bool float
    double long short byte char decimal
'''.split())


def _page(items, limit):
    return {'total': len(items), 'items': items[:limit], 'omitted': max(0, len(items) - limit)}


def _tokens(query):
    """Identifier-like words (>=3 chars, common English filler words
    excluded) in query, member name(s) first: for a dotted "Type.Member"
    word, the part *after* the last dot (the specific thing being asked
    about) outranks the part before it (its enclosing type, useful as
    fallback context but not what a caller searching "PoliceStation.
    PullOfficer" wants highlighted -- the class declaration line for
    PoliceStation would otherwise win over PullOfficer's own method
    declaration just by appearing earlier in the file). Stopword filtering
    only matters for a plain-English question, not a bare symbol name (which
    won't contain any); see _STOPWORDS and _fts_query."""
    primary, secondary = [], []
    for word in query.split():
        parts = [part for part in _TOKEN.findall(word.replace('.', ' ')) if part.lower() not in _STOPWORDS]
        # word.replace('.', ' ') plus re-matching keeps "Type.Member" splitting
        # simple without a second regex; a non-dotted word yields one part.
        if not parts:
            continue
        primary.append(parts[-1])
        secondary.extend(parts[:-1])
    seen, tokens = set(), []
    for token in primary + secondary:
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def _locate(content, tokens):
    """The single best representative line for `tokens` (priority order --
    see _tokens) in `content`: the first declaration-shaped line for the
    highest-priority token that has one anywhere in the document, else the
    first line mentioning any token at all. Two passes (declarations across
    the whole document for each token in turn, then references) rather than
    one top-to-bottom scan, so a higher-priority token's declaration later in
    the file still wins over a lower-priority token's declaration earlier in
    it (e.g. a specific method beats its own enclosing class). Returns None
    if no token appears as a whole word anywhere -- a document can match
    FTS's tokenizer without any line satisfying a strict word-boundary check,
    e.g. across punctuation."""
    lines = content.splitlines()
    patterns = [re.compile(r'\b' + re.escape(token) + r'\b') for token in tokens]
    for pattern in patterns:
        for line_number, line in enumerate(lines, start=1):
            if pattern.search(line) and (_TYPE_DECL.match(line) or _METHOD_DECL.match(line)):
                return {'line': line_number, 'kind': 'declaration', 'text': line.strip()}
    for pattern in patterns:
        for line_number, line in enumerate(lines, start=1):
            if pattern.search(line):
                return {'line': line_number, 'kind': 'reference', 'text': line.strip()}
    return None


def _document_matches(connection, fts_query, artifact_type):
    """Latest source_documents row per relative_path matching `fts_query`
    under `artifact_type`, so a superseded (changed-and-reingested) version
    of a file never shadows its current content in a research packet."""
    rows = connection.execute('''
        SELECT d.id, d.relative_path, d.content FROM source_documents_fts
        JOIN source_documents d ON d.id = source_documents_fts.rowid
        JOIN source_artifacts a ON a.id = d.source_artifact_id
        WHERE source_documents_fts MATCH ? AND a.artifact_type = ?''', (fts_query, artifact_type)).fetchall()
    latest = {}
    for row in rows:
        current = latest.get(row['relative_path'])
        if current is None or row['id'] > current['id']:
            latest[row['relative_path']] = row
    return list(latest.values())


def _document_section(connection, fts_query, tokens, artifact_type, limit):
    matches = _document_matches(connection, fts_query, artifact_type)
    located = [(row, _locate(row['content'], tokens)) for row in matches]
    items = [{'relative_path': row['relative_path'], 'line': hit['line'] if hit else None,
              'kind': hit['kind'] if hit else None, 'text': hit['text'] if hit else None}
             for row, hit in located]
    # Declarations first, then by path, so the most useful line surfaces
    # within the bound even when there are more matches than `limit`.
    items.sort(key=lambda item: (item['kind'] != 'declaration', item['relative_path']))
    return _page(items, limit)


def _search_section(database, query, type_, limit):
    result = fts_search(database, query, type=type_, limit=limit)
    items = [{'source': item['source'], 'id': item['id'], 'test_run_id': item['test_run_id'],
              'source_line': item['source_line'], 'snippet': item['snippet']} for item in result['items']]
    return {'total': len(items) + (1 if result['truncated'] else 0), 'items': items,
            'omitted': 1 if result['truncated'] else 0}


def _research_records_section(connection, tokens, limit):
    if not tokens:
        return _page([], limit)
    clauses = ' OR '.join(['finding LIKE ?'] * len(tokens))
    findings = connection.execute(
        f'SELECT id, finding AS text, status, confidence FROM findings WHERE {clauses} ORDER BY id DESC',
        [f'%{token}%' for token in tokens]).fetchall()
    clauses = ' OR '.join(['question LIKE ?'] * len(tokens))
    unknowns = connection.execute(
        f'SELECT id, question AS text, status, importance FROM unknowns WHERE {clauses} ORDER BY id DESC',
        [f'%{token}%' for token in tokens]).fetchall()
    clauses = ' OR '.join(['decision LIKE ?'] * len(tokens))
    decisions = connection.execute(
        f'SELECT id, decision AS text, status FROM decisions WHERE {clauses} ORDER BY id DESC',
        [f'%{token}%' for token in tokens]).fetchall()
    items = ([{'kind': 'finding', 'id': r['id'], 'text': r['text'], 'status': r['status']} for r in findings]
             + [{'kind': 'unknown', 'id': r['id'], 'text': r['text'], 'status': r['status']} for r in unknowns]
             + [{'kind': 'decision', 'id': r['id'], 'text': r['text'], 'status': r['status']} for r in decisions])
    return _page(items, limit)


def _experiments_section(connection, tokens, limit):
    if not tokens:
        return _page([], limit)
    clauses = ' OR '.join(['s.symbol LIKE ?'] * len(tokens))
    rows = connection.execute(f'''
        SELECT DISTINCT e.id, e.question, e.observed_result, e.mod_build
        FROM experiments e JOIN experiment_symbols s ON s.experiment_id = e.id
        WHERE {clauses} ORDER BY e.id DESC''', [f'%{token}%' for token in tokens]).fetchall()
    items = [{'id': r['id'], 'question': r['question'], 'observed_result': r['observed_result'],
              'mod_build': r['mod_build']} for r in rows]
    return _page(items, limit)


def _interpretation(sections):
    has_mod, has_game = sections['mod_source']['total'] > 0, sections['decompiled']['total'] > 0
    has_events = sections['events']['total'] > 0 or sections['errors']['total'] > 0
    notes = []
    if has_game and not has_mod:
        notes.append('Decompiled declaration/reference found; no Hyland Heat mod-source call or patch '
                     'site currently ingested for this query.')
    elif has_mod and not has_game:
        notes.append('Hyland Heat mod source references this query; no matching decompiled game code is '
                     'currently ingested (it may exist in an assembly not yet decompiled/ingested).')
    elif not has_mod and not has_game:
        notes.append('No source-code match in either ingested mod source or ingested decompiled game code.')
    else:
        notes.append('Both a decompiled game-code match and a Hyland Heat mod-source match exist for this query.')
    notes.append('Runtime-log evidence found.' if has_events else 'No related runtime-log events/errors recorded.')
    return notes


def _next_target(sections, tokens):
    # Code-anchor gaps first: without knowing where a symbol even lives,
    # capturing runtime evidence for it is premature -- so that suggestion
    # only comes after there's at least one source-code match to anchor to.
    gaps = [
        ('decompiled', 'No decompiled game-code match: decompile and ingest the assembly that defines '
                       'this symbol (verify the name, or that it lies outside the currently-ingested set).'),
        ('mod_source', 'No Hyland Heat patch/call site found: consider whether the mod actually touches '
                       'this symbol yet.'),
        ('events', 'Capture a play session exercising this code path and ingest the resulting log '
                   '(python3 hyland_heat_log_analyzer.py --database ...).'),
    ]
    for key, suggestion in gaps:
        if sections[key]['total'] == 0:
            return suggestion
    co_occurring = {}
    for section_key in ('mod_source', 'decompiled'):
        for item in sections[section_key]['items']:
            for word in _TOKEN.findall(item.get('text') or ''):
                if word not in tokens and word.lower() not in _CODE_NOISE:
                    co_occurring[word] = co_occurring.get(word, 0) + 1
    if co_occurring:
        top = max(co_occurring.items(), key=lambda pair: pair[1])
        if top[1] >= 2:
            return f'"{top[0]}" co-occurs with this query in multiple matches; may be worth its own research pass.'
    return 'All evidence categories have at least one match; consider recording an experiment or finding summarizing this.'


def _fts_query(tokens, *, mode='and'):
    """Build an FTS5 MATCH expression from already-extracted, punctuation-free
    tokens, never from raw query text -- so arbitrary input (a full natural-
    language question, with "?" and other characters that are FTS5 syntax,
    not searchable content) can never reach FTS5 as invalid syntax."""
    return ' '.join(tokens) if mode == 'and' else ' OR '.join(tokens)


def research(database, query, *, limit=DEFAULT_LIMIT):
    """A compact, bounded, honest evidence packet spanning every artifact
    class this database holds. Requires schema v5.

    query is free text: a dotted symbol ("NPC.EnterBuilding") works
    directly, and so does a full natural-language question ("What explains
    officer clones changing ActiveSelf during NPC.EnterBuilding?") -- FTS5
    itself never sees the raw text, only identifier-like tokens extracted
    from it (see _fts_query), so punctuation can't produce a syntax error.
    Each of the five sections is independently bounded to `limit` (1-100)
    and never fabricated -- an empty section is reported as empty, not
    omitted or guessed at. Returns a dict; see format_research for the
    Markdown rendering used by the CLI's default (non-JSON) output.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must not be empty.')
    bounded_limit(limit)
    tokens = _tokens(query)
    if not tokens:
        raise ValueError('query must contain at least one identifier-like word (3+ letters), '
                         'excluding common English filler words.')

    def _sections(mode):
        fts_query = _fts_query(tokens, mode=mode)
        with closing(connect_database(database, read_only=True)) as connection:
            validate_schema_version(connection, minimum=5)
            try:
                mod_source = _document_section(connection, fts_query, tokens, 'source_code', limit)
                decompiled = _document_section(connection, fts_query, tokens, 'decompiler_export', limit)
            except sqlite3.OperationalError as exc:
                raise ValueError(f'Invalid search query syntax: {exc}') from exc
            records = _research_records_section(connection, tokens, limit)
            experiments = _experiments_section(connection, tokens, limit)
        events = _search_section(database, fts_query, 'events', limit)
        errors = _search_section(database, fts_query, 'errors', limit)
        return {'mod_source': mod_source, 'decompiled': decompiled, 'events': events, 'errors': errors,
                'records': records, 'experiments': experiments}

    # AND (every extracted token required) first: precise, and for a bare
    # symbol query -- the documented, common case -- almost always what's
    # wanted. If that finds literally nothing, retry with OR (any token):
    # a short natural-language question inevitably extracts several
    # genuinely meaningful but independent words ("officer", "clone",
    # "ActiveSelf", "EnterBuilding"), and no single file is likely to
    # mention all of them together even when several separately are
    # exactly the evidence worth surfacing. An empty AND result is real
    # signal in its own right (see the 'mode' field: 'and' with nothing
    # found honestly reports "no single match ties these together", not a
    # tool failure), which is why this tries precise first rather than
    # defaulting to the noisier OR.
    # Only the FTS-driven sections participate in this decision: records/
    # experiments already match per-token with an OR-style LIKE regardless
    # of `mode` (see _research_records_section/_experiments_section), so an
    # unrelated finding loosely matching one token must not by itself block
    # the FTS sections from broadening when they found nothing.
    mode = 'and'
    sections = _sections(mode)
    if len(tokens) > 1 and all(sections[key]['total'] == 0 for key in
                               ('mod_source', 'decompiled', 'events', 'errors')):
        mode = 'or'
        sections = _sections(mode)
    return {'query': query, 'limit': limit, 'mode': mode, 'sections': sections,
            'interpretation': _interpretation(sections), 'next_target': _next_target(sections, tokens)}


_SECTION_LABELS = {
    'mod_source': 'Hyland Heat call/patch sites',
    'decompiled': 'Decompiled declaration/implementation locations',
    'events': 'Related runtime-log events',
    'errors': 'Related runtime-log errors',
    'records': 'Related evidence/research records (findings/unknowns/decisions)',
    'experiments': 'Related recorded experiments',
}


def _format_item(section_key, item):
    if section_key in ('mod_source', 'decompiled'):
        where = f"{item['relative_path']}:{item['line']}" if item['line'] else item['relative_path']
        kind = f" [{item['kind']}]" if item['kind'] else ''
        text = f" {item['text']}" if item['text'] else ''
        return f"- {where}{kind}{text}"
    if section_key in ('events', 'errors'):
        run = f"run #{item['test_run_id']}" if item['test_run_id'] is not None else 'no run'
        line = f", line {item['source_line']}" if item['source_line'] is not None else ''
        return f"- [{item['source']} #{item['id']}] ({run}{line}) {item['snippet']}"
    if section_key == 'records':
        return f"- [{item['kind']} #{item['id']}] ({item['status']}) {item['text']}"
    if section_key == 'experiments':
        return f"- [experiment #{item['id']}] {item['question']} -> {item['observed_result']}"
    return f"- {item}"


def format_research(result):
    mode_note = (' (broadened to match any extracted term after an exact-match search found nothing)'
                if result['mode'] == 'or' else '')
    lines = [f'# Research: "{result["query"]}"{mode_note}', '']
    for key, page in result['sections'].items():
        lines.append(f"## {_SECTION_LABELS[key]}")
        if not page['items']:
            lines.append('No matches found.')
        else:
            lines.extend(_format_item(key, item) for item in page['items'])
            if page['omitted']:
                lines.append(f"... {page['omitted']} more not shown (--limit {result['limit']}).")
        lines.append('')
    lines.append('## Observed facts')
    for key, page in result['sections'].items():
        lines.append(f"- {_SECTION_LABELS[key]}: {page['total']} match(es).")
    lines.append('')
    lines.append('## Interpretation')
    lines.extend(f'- {note}' for note in result['interpretation'])
    lines.append('')
    lines.append('## Suggested next research target')
    lines.append(result['next_target'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query', help='A symbol (e.g. "NPC.EnterBuilding") or short research question.')
    parser.add_argument('--database')
    parser.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help='Max items per section (1-100).')
    parser.add_argument('--format', choices=('text', 'json'), default='text')
    args = parser.parse_args(argv)
    try:
        result = research(args.database, args.query, limit=args.limit)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
              else format_research(result))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
