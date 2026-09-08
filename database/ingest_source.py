"""Ingest a directory of code files (mod source, or an already-decompiled
game-assembly output directory) as full-text-searchable source_documents.

Deliberately file-level, not member-level: this does not parse C# or
decompiled text into classes/methods. A decompiled IL2CPP type's method
*bodies* are Il2CppInterop marshalling boilerplate, not real game logic, but
its member *signatures* are ground truth -- and FTS5's snippet()/bm25()
already surface the right neighborhood of a large file without needing a
parser or noise-stripping heuristics to get right. See database.search's
'documents' source type.

Each call is a deliberate new snapshot: unlike log ingestion, this never
deduplicates against a prior ingestion of the same root -- re-running after
code changes is meant to create new, separately searchable history (what did
this file look like as of commit X), not silently reuse an old one.

Ingested content is stored only in the local database, never in the git-
tracked SQL dump -- see database.backup's docstring for why (large size and,
for decompiled game assemblies, copyright). Point --root at a directory
outside any git repository this project or the mod source lives in.
"""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from .db import connect_database, validate_schema_version

ARTIFACT_TYPES = ('source_code', 'decompiler_export')
LANGUAGES = {'.cs': 'csharp'}


def ingest_directory(database, artifact_type, root, *, extensions=('.cs',), mod_repo=None):
    """Walk `root` read-only for files matching `extensions` (case-insensitive),
    storing each as one source_documents row under one new source_artifacts row.

    mod_repo, if given, is passed to database.source_revision.capture_revision
    and recorded in the artifact's notes. Files that fail to decode as UTF-8
    are skipped and counted rather than failing the whole ingestion.
    """
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f'artifact_type must be one of {ARTIFACT_TYPES}.')
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f'{root} is not a directory.')
    extensions = tuple(ext.lower() for ext in extensions)
    revision = None
    if mod_repo is not None:
        from .source_revision import capture_revision
        revision = capture_revision(mod_repo)
    documents, skipped = [], 0
    for path in sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in extensions):
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            skipped += 1
            continue
        documents.append((path.relative_to(root).as_posix(), LANGUAGES.get(path.suffix.lower()), content))
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=4)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            notes = json.dumps({'file_count': len(documents), 'skipped': skipped, 'mod_revision': revision},
                               ensure_ascii=True)
            artifact_id = connection.execute(
                '''INSERT INTO source_artifacts (artifact_type, path, filename, notes)
                   VALUES (?, ?, ?, ?)''', (artifact_type, str(root), root.name, notes)).lastrowid
            connection.executemany(
                '''INSERT INTO source_documents (source_artifact_id, relative_path, language, content)
                   VALUES (?, ?, ?, ?)''',
                [(artifact_id, relative_path, language, content) for relative_path, language, content in documents])
    return {'database': str(database), 'source_artifact_id': artifact_id, 'documents_stored': len(documents),
            'skipped': skipped, 'mod_revision': revision}


def format_ingest_summary(result):
    lines = [f"Database: {result['database']}", f"Source artifact: {result['source_artifact_id']}",
            f"Documents stored: {result['documents_stored']}"]
    if result['skipped']:
        lines.append(f"Skipped (undecodable): {result['skipped']}")
    if result['mod_revision'] is not None:
        lines.append(f"Mod revision: {result['mod_revision']}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--root', required=True, help='Directory to walk for source/decompiled files.')
    parser.add_argument('--artifact-type', required=True, choices=ARTIFACT_TYPES)
    parser.add_argument('--extensions', default='.cs',
                        help='Comma-separated, case-insensitive file extensions to ingest (default: .cs).')
    parser.add_argument('--mod-repo', help='Optional mod source repo path; its git commit is recorded on the artifact.')
    args = parser.parse_args(argv)
    try:
        extensions = tuple(ext.strip() for ext in args.extensions.split(','))
        result = ingest_directory(args.database, args.artifact_type, args.root,
                                  extensions=extensions, mod_repo=args.mod_repo)
        print(format_ingest_summary(result))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
