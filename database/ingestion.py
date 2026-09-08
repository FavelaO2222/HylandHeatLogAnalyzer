"""Persist already-classified analysis evidence; never generate research findings."""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .db import (connect_database, database_exists, initialize_database,
                 resolve_database_path, validate_schema_version)

IMPORTER = "hyland-heat-log-analyzer/1"
RESULTS = {"PASS": "PASS", "NEEDS ATTENTION": "NEEDS_ATTENTION", "INCONCLUSIVE": "INCOMPLETE"}
PRIMARY_CATEGORIES = (
    "goon_transition", "duplicate_identity", "integrity_failure", "test_failure",
    "swat_rejection", "swat_deploy", "swat_recall", "read_only_audit",
    "diagnostic_result", "replacement", "appearance", "disappearance", "missing",
    "clone_population", "test_event", "swat_readiness", "swat_tracking", "swat",
    "goon_observation", "officer_registry", "identity", "police", "infamy",
    "suspicion", "crime", "patrol", "initialization", "shutdown",
)


def component(message):
    prefix = re.match(r"^(?:\[[^\]]+\]\s*)+", message)
    if not prefix:
        return None
    tags = re.findall(r"\[([^\]]+)\]", prefix.group())
    tags = [tag for tag in tags if tag.upper() not in {"INFO", "WARN", "WARNING", "ERROR", "EXCEPTION", "FATAL", "SESSION"}]
    return tags[-1] if tags else None


def validate_destination(database, source):
    target, source = resolve_database_path(database), Path(source).resolve()
    if target == source or (target.exists() and target.samefile(source)):
        raise ValueError("Database destination cannot be the input log or an alias of it.")
    return target


def _summary(connection, target, artifact_id, run_id, duplicate):
    return {"database": str(target), "source_artifact_id": artifact_id,
            "test_run_id": run_id, "duplicate": duplicate,
            "events": connection.execute('SELECT count(*) FROM events WHERE test_run_id=?', (run_id,)).fetchone()[0],
            "errors": connection.execute('SELECT count(*) FROM errors WHERE test_run_id=?', (run_id,)).fetchone()[0]}


def import_analysis_result(report, capture, database, *, mod_build=None):
    if not capture.complete or capture.source != Path(report["metadata"]["source_path"]).resolve():
        raise ValueError("Import requires completed capture from this analysis, including full traces.")
    capture.verify_source()
    target = validate_destination(database, capture.source)
    if not database_exists(target):
        initialize_database(target)
    with closing(connect_database(target)) as connection:
        validate_schema_version(connection)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            # Dedup is checked under the same write transaction as all inserted rows.
            candidates = connection.execute('''SELECT t.id, t.source_artifact_id, t.notes
                FROM test_runs t JOIN source_artifacts a ON a.id=t.source_artifact_id
                WHERE a.artifact_type=? AND a.sha256=? AND t.profile=?''',
                ('log', capture.sha256, report['profile'])).fetchall()
            for candidate in candidates:
                try:
                    notes = json.loads(candidate['notes'] or '{}')
                except (ValueError, TypeError):
                    continue
                if isinstance(notes, dict) and notes.get('importer') == IMPORTER and notes.get('analysis_schema') == report['schema_version']:
                    capture.verify_source()
                    return _summary(connection, target, candidate['source_artifact_id'], candidate['id'], True)
            artifact = connection.execute('''SELECT id FROM source_artifacts
                WHERE artifact_type=? AND sha256=? ORDER BY id LIMIT 1''', ('log', capture.sha256)).fetchone()
            artifact_id = artifact[0] if artifact else connection.execute('''INSERT INTO source_artifacts
                (artifact_type, path, filename, sha256, notes) VALUES (?, ?, ?, ?, ?)''',
                ('log', str(capture.source), capture.source.name, capture.sha256,
                 'Raw log; path records first registration. Original creation time is unknown.')).lastrowid
            run_id = connection.execute('''INSERT INTO test_runs
                (source_artifact_id, started_at, profile, result, mod_build) VALUES (?, ?, ?, ?, ?)''',
                (artifact_id, report['metadata']['first_timestamp'], report['profile'],
                 RESULTS.get(report['verdict'], 'UNKNOWN'), mod_build)).lastrowid
            manifest = {"importer": IMPORTER, "analysis_schema": report['schema_version'],
                        "original_verdict": report['verdict'], "analyzed_path": str(capture.source),
                        "expected_rejection_lines": report['expected_rejection_lines'],
                        "transitions": report['transitions'], "events": {}, "errors": {}}
            for event in report['detected_events']:
                if event['severity'] in {'ERROR', 'EXCEPTION', 'FATAL'}:
                    continue
                categories = event['categories']
                category = next((name for name in PRIMARY_CATEGORIES if name in categories),
                                categories[0] if categories else 'WARNING')
                event_id = connection.execute('''INSERT INTO events
                    (test_run_id, source_artifact_id, timestamp, category, component, event_type, message, source_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (run_id, artifact_id, event['timestamp'], category, component(event['message']),
                     event['transition'] or category, event['message'], event['line'])).lastrowid
                manifest['events'][str(event_id)] = {
                    'categories': categories, 'severity': event['severity'], 'count': event['count'],
                    'last_line': event['last_line'],
                    'occurrences': capture.occurrences[event['message']]}
            for error in capture.errors:
                stack = ''.join(frame['text'] for frame in error['full_stack'])
                error_id = connection.execute('''INSERT INTO errors
                    (test_run_id, source_artifact_id, timestamp, severity, component, message, stack_trace, source_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (run_id, artifact_id, error['timestamp'], error['severity'], component(error['message']),
                     error['message'], stack or None, error['line'])).lastrowid
                manifest['errors'][str(error_id)] = {'stack_source_lines': [f['line'] for f in error['full_stack']]}
            connection.execute('UPDATE test_runs SET notes=? WHERE id=?', (json.dumps(manifest, ensure_ascii=True), run_id))
            capture.verify_source()
            return _summary(connection, target, artifact_id, run_id, False)


def persist_analysis(report, capture, database, *, mod_build=None):
    """Expose a concise CLI-compatible failure without importing sqlite in the parser."""
    try:
        return import_analysis_result(report, capture, database, mod_build=mod_build)
    except sqlite3.Error as exc:
        raise ValueError(f"Database import failed: {exc}") from exc


def format_import_summary(result):
    return (f"Database: {result['database']}\nSource artifact: {result['source_artifact_id']}\n"
            f"Test run: {result['test_run_id']}\nEvents stored: {result['events']}\n"
            f"Errors stored: {result['errors']}\nDuplicate import: {'yes (existing run reused)' if result['duplicate'] else 'no'}")
