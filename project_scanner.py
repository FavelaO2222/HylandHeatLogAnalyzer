"""Recursively scans a project directory for "useful" (non-binary,
non-ignored) files and writes two artifacts an AI agent can use to orient
itself quickly: PROJECT_MAP.json (full per-file detail) and AGENT_INDEX.md
(a concise directory-hierarchy summary; see PROJECT_MAP.json for anything
AGENT_INDEX.md doesn't spell out). Standard library only.

Ignore handling is a deliberately simplified, documented subset of
.gitignore syntax (glob patterns via fnmatch, directory-only patterns
ending in "/", comments, blank lines) -- not full git semantics: no
negation ("!pattern") and "**" is not distinguished from "*". See
_match_ignore_pattern. A project root's own .gitignore is read
automatically unless --no-gitignore is passed; --extra-ignore adds more
patterns on top. A handful of common non-project directories/files
(version control metadata, virtualenvs, caches, build output) are always
ignored regardless of any ignore file -- see DEFAULT_IGNORE_DIRS/
DEFAULT_IGNORE_PATTERNS. Symlinked directories are never followed (avoids
symlink cycles); symlinked regular files are scanned normally.

A file counts as binary (skipped, not "useful") if its first 8000 bytes
contain a null byte or fail to decode as UTF-8 -- a cheap, standard
heuristic, not a real content-type sniffer. A file over --max-file-bytes
is still listed but not opened for a line count (marked "truncated"),
so one huge file can't make a scan pathologically slow.
"""

import argparse
from datetime import datetime, timezone
import fnmatch
import json
from pathlib import Path
import sys

DEFAULT_IGNORE_DIRS = frozenset({
    '.git', '__pycache__', '.venv', 'venv', 'env', '.env', 'node_modules',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build', '.idea',
    '.vscode', '.tox', 'htmlcov', '.eggs', '.cache',
})
DEFAULT_IGNORE_PATTERNS = ('*.pyc', '*.pyo', '*.egg-info', '.DS_Store', '*.so', '*.dll', '*.dylib')
# Always excluded by name, regardless of any ignore list, so re-running the
# scanner on its own prior output never treats that output as project content.
GENERATED_OUTPUT_NAMES = frozenset({'PROJECT_MAP.json', 'AGENT_INDEX.md'})
SNIFF_BYTES = 8000
DEFAULT_MAX_FILE_BYTES = 2_000_000


def _load_gitignore(root):
    path = root / '.gitignore'
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        patterns.append(line)
    return patterns


def _match_ignore_pattern(relative_path, is_dir, pattern):
    directory_only = pattern.endswith('/')
    if directory_only:
        if not is_dir:
            return False
        pattern = pattern.rstrip('/')
    name = relative_path.name
    posix = relative_path.as_posix()
    return (fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(posix, pattern)
            or fnmatch.fnmatch(posix, f'*/{pattern}'))


def _is_ignored(relative_path, is_dir, patterns):
    if relative_path.name in GENERATED_OUTPUT_NAMES:
        return True
    if is_dir and relative_path.name in DEFAULT_IGNORE_DIRS:
        return True
    return any(_match_ignore_pattern(relative_path, is_dir, pattern)
              for pattern in (*DEFAULT_IGNORE_PATTERNS, *patterns))


def _looks_binary(path, sniff_bytes=SNIFF_BYTES):
    try:
        with path.open('rb') as handle:
            chunk = handle.read(sniff_bytes)
    except OSError:
        return True
    if b'\x00' in chunk:
        return True
    try:
        chunk.decode('utf-8')
    except UnicodeDecodeError:
        return True
    return False


def _count_lines(path):
    count = 0
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        for _ in handle:
            count += 1
    return count


def scan_project(project_root, *, extra_ignore=(), use_gitignore=True, max_file_bytes=DEFAULT_MAX_FILE_BYTES):
    """Walk `project_root`; return a dict describing every "useful" file
    found (not ignored, not binary, readable as text) plus summary counts.
    Directories are walked but never themselves counted as files; a
    directory only appears in `summary.total_directories` if it contains
    at least one useful file (an empty or fully-ignored directory
    contributes nothing an agent needs to see).
    """
    if max_file_bytes < 1:
        raise ValueError('max_file_bytes must be a positive integer.')
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'{root} is not a directory.')
    patterns = list(extra_ignore)
    ignore_sources = ['built-in defaults']
    if use_gitignore:
        gitignore_patterns = _load_gitignore(root)
        if gitignore_patterns:
            patterns.extend(gitignore_patterns)
            ignore_sources.append('.gitignore')
    if extra_ignore:
        ignore_sources.append('--extra-ignore')

    files = []
    by_extension = {}
    counts = {'skipped_binary': 0, 'skipped_ignored': 0, 'skipped_symlink_dirs': 0}

    def walk(directory):
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            relative = entry.relative_to(root)
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir() and not is_symlink
            if _is_ignored(relative, entry.is_dir(), patterns):
                counts['skipped_ignored'] += 1
                continue
            if entry.is_dir() and is_symlink:
                counts['skipped_symlink_dirs'] += 1
                continue
            if is_dir:
                walk(entry)
                continue
            if not entry.is_file():
                continue
            if _looks_binary(entry):
                counts['skipped_binary'] += 1
                continue
            size = entry.stat().st_size
            extension = entry.suffix or '(none)'
            truncated = size > max_file_bytes
            lines = None if truncated else _count_lines(entry)
            files.append({'path': relative.as_posix(), 'bytes': size, 'lines': lines,
                          'extension': extension, 'truncated': truncated})
            by_extension[extension] = by_extension.get(extension, 0) + 1

    walk(root)
    directories = sorted({str(Path(f['path']).parent) for f in files} - {'.'})
    return {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'root': str(root),
        'ignore_sources': ignore_sources,
        'summary': {
            'total_files': len(files), 'total_directories': len(directories),
            'total_bytes': sum(f['bytes'] for f in files), **counts,
            'by_extension': dict(sorted(by_extension.items())),
        },
        'files': sorted(files, key=lambda f: f['path']),
    }


def _build_tree(files):
    """{'dirs': {name: node, ...}, 'files': [name, ...]} per directory,
    rooted at the scanned project root -- shared by the JSON tree and the
    Markdown hierarchy renderer below, so both read the same structure."""
    root = {'dirs': {}, 'files': []}
    for item in files:
        parts = Path(item['path']).parts
        node = root
        for part in parts[:-1]:
            node = node['dirs'].setdefault(part, {'dirs': {}, 'files': []})
        node['files'].append(parts[-1])
    return root


def _count_files_recursive(node):
    return len(node['files']) + sum(_count_files_recursive(child) for child in node['dirs'].values())


def _human_bytes(n):
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f}{unit}' if unit == 'B' else f'{size:.1f}{unit}'
        size /= 1024
    return f'{size:.1f}TB'


def _render_tree_lines(node, prefix):
    # Directories only, each annotated with its own recursive file count --
    # individual filenames below the root are left to PROJECT_MAP.json, so
    # this stays genuinely concise regardless of project size.
    lines = []
    dirs = sorted(node['dirs'].items())
    for index, (name, child) in enumerate(dirs):
        last = index == len(dirs) - 1
        connector = '└── ' if last else '├── '
        count = _count_files_recursive(child)
        lines.append(f"{prefix}{connector}{name}/ ({count} file{'s' if count != 1 else ''})")
        extension = '    ' if last else '│   '
        lines.extend(_render_tree_lines(child, prefix + extension))
    return lines


def render_agent_index(result):
    root_name = Path(result['root']).name
    s = result['summary']
    tree = _build_tree(result['files'])
    lines = [
        f'# Agent Index: {root_name}', '',
        f"Generated {result['generated_at']} from `{result['root']}`.",
        f"{s['total_files']} files, {s['total_directories']} directories, {_human_bytes(s['total_bytes'])}.",
        f"Skipped: {s['skipped_ignored']} ignored path(s), {s['skipped_binary']} binary file(s)"
        + (f", {s['skipped_symlink_dirs']} symlinked directory(ies) not followed." if s['skipped_symlink_dirs'] else '.'),
        f"Ignore sources: {', '.join(result['ignore_sources'])}.", '',
        '## Directory hierarchy', '', f'{root_name}/',
    ]
    lines.extend(_render_tree_lines(tree, ''))
    if tree['files']:
        lines.append('')
        lines.append(f"Root-level files ({len(tree['files'])}): "
                     + ', '.join(f'`{name}`' for name in sorted(tree['files'])))
    lines.append('')
    lines.append('## File types')
    lines.append('')
    for extension, count in sorted(s['by_extension'].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f'- `{extension}`: {count} file(s)')
    lines.append('')
    lines.append('See `PROJECT_MAP.json` for the full per-file list (size, line count).')
    return '\n'.join(lines)


def render_project_map_json(result):
    return json.dumps({**result, 'tree': _build_tree(result['files'])}, indent=2, sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project_root')
    parser.add_argument('--output-dir', help='Where to write the two output files; defaults to project_root.')
    parser.add_argument('--extra-ignore', action='append', default=[],
                        help='Additional ignore glob pattern; repeatable.')
    parser.add_argument('--no-gitignore', action='store_true',
                        help="Don't read the project root's .gitignore.")
    parser.add_argument('--max-file-bytes', type=int, default=DEFAULT_MAX_FILE_BYTES,
                        help='Files larger than this are listed but not opened for a line count.')
    args = parser.parse_args(argv)
    try:
        result = scan_project(args.project_root, extra_ignore=args.extra_ignore,
                              use_gitignore=not args.no_gitignore, max_file_bytes=args.max_file_bytes)
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else Path(result['root'])
        output_dir.mkdir(parents=True, exist_ok=True)
        map_path = output_dir / 'PROJECT_MAP.json'
        index_path = output_dir / 'AGENT_INDEX.md'
        map_path.write_text(render_project_map_json(result), encoding='utf-8')
        index_path.write_text(render_agent_index(result), encoding='utf-8')
        s = result['summary']
        print(f'Wrote {map_path}')
        print(f'Wrote {index_path}')
        print(f"{s['total_files']} files, {s['total_directories']} directories "
              f"({s['skipped_ignored']} ignored, {s['skipped_binary']} binary skipped).")
    except (OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
