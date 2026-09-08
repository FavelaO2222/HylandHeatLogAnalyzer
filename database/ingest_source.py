"""Ingest a directory of code files (mod source, or an already-decompiled
game-assembly output directory) as full-text-searchable source_documents.

Deliberately file-level, not member-level: this does not parse C# or
decompiled text into classes/methods. A decompiled IL2CPP type's method
*bodies* are Il2CppInterop marshalling boilerplate, not real game logic, but
its member *signatures* are ground truth -- and FTS5's snippet()/bm25()
already surface the right neighborhood of a large file without needing a
parser or noise-stripping heuristics to get right. See database.search's
'documents' source type; database.research classifies declaration vs.
reference lines transiently at query time from this same stored content,
rather than a separate persisted (and potentially stale) symbol index.

Idempotent, not silently duplicating: each file's content is hashed
(content_sha256) and compared against the latest existing row for the same
(collection, relative_path). Unchanged content is skipped -- no new row --
so re-running this against unmodified files is a no-op for the actual
evidence, while a real change still creates a new row, preserving "what did
this file look like as of commit X" history rather than overwriting it. A
source_artifacts row is still written on every call regardless (the audit
trail of when this collection was last checked), even when nothing changed.

`collection` is a caller-chosen stable identity, deliberately decoupled from
`--root`: real decompiled/git-archive output routinely comes from a fresh
scratch directory each time, so matching on the literal root path would
never converge for exactly the ingestions that matter most. It defaults to
the resolved root path (so simple, same-path-every-time callers get
idempotency for free) but should be given explicitly whenever `--root` is
ephemeral.

Ingested content is stored only in the local database, never in the git-
tracked SQL dump -- see database.backup's docstring for why (large size and,
for decompiled game assemblies, copyright). Point --root at a directory
outside any git repository this project or the mod source lives in.
"""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from .db import connect_database, validate_schema_version

ARTIFACT_TYPES = ('source_code', 'decompiler_export')
LANGUAGES = {'.cs': 'csharp'}


def ingest_directory(database, artifact_type, root, *, extensions=('.cs',), mod_repo=None, collection=None):
    """Walk `root` read-only for files matching `extensions` (case-insensitive),
    storing each changed-or-new file as one source_documents row under one new
    source_artifacts row. Unchanged files (same content_sha256 as the latest
    existing row for this collection+relative_path) are skipped, not duplicated.

    collection defaults to the resolved root path; pass it explicitly when
    `root` is an ephemeral extraction directory that won't be reused, so
    idempotency is judged against a stable identity instead.

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
    collection = collection if collection is not None else str(root.resolve())
    revision = None
    if mod_repo is not None:
        from .source_revision import capture_revision
        revision = capture_revision(mod_repo)
    candidates, undecodable = [], 0
    for path in sorted(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in extensions):
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            undecodable += 1
            continue
        relative_path = path.relative_to(root).as_posix()
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        candidates.append((relative_path, LANGUAGES.get(path.suffix.lower()), content, digest))
    with closing(connect_database(database)) as connection:
        validate_schema_version(connection, minimum=5)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            documents, unchanged = [], 0
            for relative_path, language, content, digest in candidates:
                previous = connection.execute(
                    '''SELECT content_sha256 FROM source_documents
                       WHERE collection = ? AND relative_path = ? ORDER BY id DESC LIMIT 1''',
                    (collection, relative_path)).fetchone()
                if previous is not None and previous[0] == digest:
                    unchanged += 1
                    continue
                documents.append((relative_path, language, content, digest))
            notes = json.dumps({'file_count': len(documents), 'unchanged': unchanged,
                                'undecodable': undecodable, 'mod_revision': revision}, ensure_ascii=True)
            artifact_id = connection.execute(
                '''INSERT INTO source_artifacts (artifact_type, path, filename, notes)
                   VALUES (?, ?, ?, ?)''', (artifact_type, str(root), root.name, notes)).lastrowid
            connection.executemany(
                '''INSERT INTO source_documents
                   (source_artifact_id, relative_path, language, content, collection, content_sha256)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                [(artifact_id, relative_path, language, content, collection, digest)
                 for relative_path, language, content, digest in documents])
    return {'database': str(database), 'source_artifact_id': artifact_id, 'collection': collection,
            'documents_stored': len(documents), 'unchanged': unchanged, 'undecodable': undecodable,
            'mod_revision': revision}


def format_ingest_summary(result):
    lines = [f"Database: {result['database']}", f"Source artifact: {result['source_artifact_id']}",
            f"Collection: {result['collection']}", f"Documents stored: {result['documents_stored']}"]
    if result['unchanged']:
        lines.append(f"Unchanged (skipped): {result['unchanged']}")
    if result['undecodable']:
        lines.append(f"Skipped (undecodable): {result['undecodable']}")
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
    parser.add_argument('--collection', help='Stable identity for idempotency; defaults to the resolved --root path.')
    parser.add_argument('--mod-repo', help='Optional mod source repo path; its git commit is recorded on the artifact.')
    args = parser.parse_args(argv)
    try:
        extensions = tuple(ext.strip() for ext in args.extensions.split(','))
        result = ingest_directory(args.database, args.artifact_type, args.root, extensions=extensions,
                                  mod_repo=args.mod_repo, collection=args.collection)
        print(format_ingest_summary(result))
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
