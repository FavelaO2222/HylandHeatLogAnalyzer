"""Ingest a project_scanner.scan_project() result into a recursive,
navigable file/directory index -- schema v8's file_index table.

Deliberately decoupled from every other table in this schema: a row is
identified only by (collection, path) TEXT, never a foreign key into
source_artifacts/source_documents/findings/etc. That's what makes this
portable to a project with none of those tables populated at all -- it
never assumes they exist. The one cross-reference this module offers
(`context`, and the CLI/MCP `context` command) is a *soft*, opt-in join:
when a source_documents row shares the same (collection, path), it's
surfaced alongside the file_index node; when it doesn't, that's reported
plainly, never fabricated. findings/unknowns/decisions/experiments have no
comparable path column (see database.research's own scoping of what it can
honestly answer), so they are deliberately not part of this join -- only
source_documents genuinely has one.

parent_path is a plain adjacency-list pointer (not a nested-set or
materialized-path scheme), so recursive descent uses SQLite's own WITH
RECURSIVE rather than a bespoke tree format -- see `tree`. Directories are
synthesized from the scanned files' own parent paths, matching
project_scanner.scan_project()'s existing convention exactly: an empty or
fully-ignored directory contributes no node, since it has nothing an agent
needs to navigate to.

Each ingest fully replaces its collection's rows in one transaction (delete
then insert), not an incremental diff: this is a snapshot of what exists
*now*, not a history to accumulate the way test_runs/experiments are.
"""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .evidence import bounded_limit
import project_scanner

DEFAULT_LIMIT = 50


def _directory_nodes(files):
    """Every parent directory (at every depth) of at least one scanned
    file, as {'path': ..., 'name': ...} -- never an empty/fully-ignored one,
    matching project_scanner's own convention (see module docstring)."""
    seen = {}
    for item in files:
        parts = Path(item['path']).parts
        for depth in range(1, len(parts)):
            directory_parts = parts[:depth]
            path = '/'.join(directory_parts)
            seen.setdefault(path, directory_parts[-1])
    return [{'path': path, 'name': name} for path, name in seen.items()]


def ingest_scan(database, collection, scan_result):
    """Write `scan_result` (a project_scanner.scan_project() return dict)
    into file_index under `collection`, replacing that collection's
    existing rows. Returns a summary dict.
    """
    if not collection or not collection.strip():
        raise ValueError('collection must not be empty.')
    files = scan_result['files']
    directories = _directory_nodes(files)
    rows = [
        (collection, d['path'], str(Path(d['path']).parent) if '/' in d['path'] else None,
         d['name'], 1, None, None, None)
        for d in directories
    ] + [
        (collection, f['path'], str(Path(f['path']).parent) if '/' in f['path'] else None,
         Path(f['path']).name, 0, f['extension'], f['bytes'], f['lines'])
        for f in files
    ]
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=8)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM file_index WHERE collection = ?', (collection,))
            connection.executemany(
                '''INSERT INTO file_index (collection, path, parent_path, name, is_directory, extension, bytes, lines)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', rows)
    return {'collection': collection, 'directories': len(directories), 'files': len(files),
            'root': scan_result['root']}


def ingest_directory(database, collection, project_root, *, extra_ignore=(), use_gitignore=True,
                     max_file_bytes=project_scanner.DEFAULT_MAX_FILE_BYTES):
    """Scan `project_root` with project_scanner.scan_project() and ingest
    it in one step -- the CLI's `ingest` command. See ingest_scan for the
    two-step form when the scan result is already in hand."""
    scan_result = project_scanner.scan_project(
        project_root, extra_ignore=extra_ignore, use_gitignore=use_gitignore, max_file_bytes=max_file_bytes)
    return ingest_scan(database, collection, scan_result)


def _row_dict(row):
    return {key: row[key] for key in row.keys()}


def children(database, collection, parent_path=None):
    """One level: every file_index row directly under `parent_path`
    (root-level entries when None), directories first, then files, each
    alphabetically."""
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=8)
        clause = 'parent_path IS NULL' if parent_path is None else 'parent_path = ?'
        params = (collection,) if parent_path is None else (collection, parent_path)
        rows = connection.execute(f'''
            SELECT path, parent_path, name, is_directory, extension, bytes, lines
            FROM file_index WHERE collection = ? AND {clause}
            ORDER BY is_directory DESC, name''', params).fetchall()
        return [_row_dict(row) for row in rows]


def tree(database, collection, path=None, *, limit=DEFAULT_LIMIT):
    """Every file_index row at or below `path` (the whole collection when
    None), via a WITH RECURSIVE walk of the parent_path adjacency list --
    the "recursive" in this module's own description, made concrete."""
    bounded_limit(limit)
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=8)
        if path is None:
            rows = connection.execute('''
                SELECT path, parent_path, name, is_directory, extension, bytes, lines
                FROM file_index WHERE collection = ? ORDER BY path''', (collection,)).fetchall()
        else:
            root = connection.execute(
                'SELECT 1 FROM file_index WHERE collection = ? AND path = ?', (collection, path)).fetchone()
            if root is None:
                raise ValueError(f"No path '{path}' in collection '{collection}'.")
            rows = connection.execute('''
                WITH RECURSIVE descendant(path, parent_path, name, is_directory, extension, bytes, lines) AS (
                    SELECT path, parent_path, name, is_directory, extension, bytes, lines
                    FROM file_index WHERE collection = :collection AND path = :path
                    UNION ALL
                    SELECT f.path, f.parent_path, f.name, f.is_directory, f.extension, f.bytes, f.lines
                    FROM file_index f JOIN descendant d ON f.parent_path = d.path AND f.collection = :collection
                )
                SELECT * FROM descendant WHERE path != :path ORDER BY path''',
                {'collection': collection, 'path': path}).fetchall()
        items = [_row_dict(row) for row in rows]
    return {'total': len(items), 'items': items[:limit], 'omitted': max(0, len(items) - limit)}


def find(database, query, *, collection=None, extension=None, limit=DEFAULT_LIMIT):
    """Rows whose name contains `query` (case-insensitive), optionally
    scoped to one collection and/or extension."""
    bounded_limit(limit)
    clauses, params = ['name LIKE ?'], [f'%{query}%']
    if collection:
        clauses.append('collection = ?')
        params.append(collection)
    if extension:
        clauses.append('extension = ?')
        params.append(extension)
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=8)
        rows = connection.execute(f'''
            SELECT collection, path, parent_path, name, is_directory, extension, bytes, lines
            FROM file_index WHERE {' AND '.join(clauses)} ORDER BY collection, path''', params).fetchall()
    items = [_row_dict(row) for row in rows]
    return {'total': len(items), 'items': items[:limit], 'omitted': max(0, len(items) - limit)}


def context(database, collection, path):
    """Structural context for one path: its file_index node, parent,
    siblings, and (if a directory) direct children -- plus the matching
    source_documents row when one shares this exact (collection, path), or
    an explicit note that none does. Never fabricates a link; see module
    docstring for why only source_documents is cross-referenced here.
    """
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection, minimum=8)
        node = connection.execute('''
            SELECT collection, path, parent_path, name, is_directory, extension, bytes, lines
            FROM file_index WHERE collection = ? AND path = ?''', (collection, path)).fetchone()
        if node is None:
            raise ValueError(f"No path '{path}' in collection '{collection}'.")
        node = _row_dict(node)
        siblings = children(database, collection, node['parent_path'])
        siblings = [item for item in siblings if item['path'] != path]
        child_items = children(database, collection, path) if node['is_directory'] else []
        document = connection.execute('''
            SELECT d.id, d.language, length(d.content) AS content_length, a.artifact_type
            FROM source_documents d JOIN source_artifacts a ON a.id = d.source_artifact_id
            WHERE d.relative_path = ? AND d.collection = ?
            ORDER BY d.id DESC LIMIT 1''', (path, collection)).fetchone()
    return {
        'node': node, 'parent_path': node['parent_path'], 'siblings': siblings, 'children': child_items,
        'source_document': _row_dict(document) if document else None,
    }


def format_tree(result, root_label):
    if not result['items']:
        return 'No matches found.'
    lines = [f'{root_label}:'] if root_label else []
    for item in result['items']:
        marker = '/' if item['is_directory'] else ''
        detail = '' if item['is_directory'] else f" ({item['bytes']}B, {item['lines']} line(s))"
        lines.append(f"- {item['path']}{marker}{detail}")
    if result['omitted']:
        lines.append(f"... {result['omitted']} more not shown.")
    return '\n'.join(lines)


def format_context(result):
    node = result['node']
    kind = 'directory' if node['is_directory'] else 'file'
    lines = [f"# {node['path']} ({kind}, collection '{node['collection']}')"]
    if not node['is_directory']:
        lines.append(f"{node['bytes']} bytes, {node['lines']} line(s), extension {node['extension']}")
    lines.append(f"Parent: {result['parent_path'] or '(root)'}")
    lines.append(f"Siblings ({len(result['siblings'])}): "
                + (', '.join(s['name'] for s in result['siblings']) if result['siblings'] else 'none'))
    if node['is_directory']:
        lines.append(f"Children ({len(result['children'])}): "
                    + (', '.join(c['name'] for c in result['children']) if result['children'] else 'none'))
    doc = result['source_document']
    lines.append(f"source_documents match: id #{doc['id']} ({doc['artifact_type']}, {doc['content_length']} chars)"
                if doc else 'source_documents match: none (not ingested via database.ingest_source under this collection).')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    commands = parser.add_subparsers(dest='command', required=True)

    ingest = commands.add_parser('ingest', help='Scan a project root and (re)build its file_index collection.')
    ingest.add_argument('project_root')
    ingest.add_argument('--collection', required=True,
                        help='Stable identity for this root, e.g. matching database.ingest_source\'s '
                             '--collection so `context` can cross-reference source_documents.')
    ingest.add_argument('--extra-ignore', action='append', default=[])
    ingest.add_argument('--no-gitignore', action='store_true')
    ingest.add_argument('--max-file-bytes', type=int, default=project_scanner.DEFAULT_MAX_FILE_BYTES)

    tree_cmd = commands.add_parser('tree', help='Recursively list a collection (or one subtree).')
    tree_cmd.add_argument('collection')
    tree_cmd.add_argument('--path', help='Subtree root; the whole collection when omitted.')
    tree_cmd.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    tree_cmd.add_argument('--format', choices=('text', 'json'), default='text')

    find_cmd = commands.add_parser('find', help='Search file/directory names.')
    find_cmd.add_argument('query')
    find_cmd.add_argument('--collection')
    find_cmd.add_argument('--extension')
    find_cmd.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    find_cmd.add_argument('--format', choices=('text', 'json'), default='text')

    context_cmd = commands.add_parser('context', help='Structural + source_documents context for one path.')
    context_cmd.add_argument('collection')
    context_cmd.add_argument('path')
    context_cmd.add_argument('--format', choices=('text', 'json'), default='text')

    args = parser.parse_args(argv)
    try:
        if args.command == 'ingest':
            result = ingest_directory(
                args.database, args.collection, args.project_root, extra_ignore=args.extra_ignore,
                use_gitignore=not args.no_gitignore, max_file_bytes=args.max_file_bytes)
            print(f"Indexed collection '{result['collection']}': {result['files']} file(s), "
                 f"{result['directories']} director(y/ies), from {result['root']}.")
        elif args.command == 'tree':
            result = tree(args.database, args.collection, args.path, limit=args.limit)
            import json as json_module
            print(json_module.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
                  else format_tree(result, args.path or args.collection))
        elif args.command == 'find':
            result = find(args.database, args.query, collection=args.collection,
                          extension=args.extension, limit=args.limit)
            import json as json_module
            print(json_module.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
                  else format_tree(result, None))
        else:
            result = context(args.database, args.collection, args.path)
            import json as json_module
            print(json_module.dumps(result, ensure_ascii=True, sort_keys=True) if args.format == 'json'
                  else format_context(result))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
