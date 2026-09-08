"""Render a compact, character-budgeted Markdown context packet for an LLM.

This is the "AI consumer" endpoint the README describes: SQLite already holds
structured evidence (events/errors) and research knowledge (findings/unknowns/
decisions); this module queries what already exists and renders it, capped to
a character budget, instead of sending raw logs or full JSON to a model. It
performs no writes and infers nothing not already stored.
"""

import argparse
from contextlib import closing
import sqlite3
import sys

from .db import connect_database, validate_schema_version
from .ingestion import PRIMARY_CATEGORIES

DEFAULT_MAX_CHARS = 6000
DEFAULT_MAX_EVENTS = 12
DEFAULT_MAX_RESEARCH_ROWS = 12
SEVERITY_ORDER = {"FATAL": 0, "EXCEPTION": 1, "ERROR": 2}
CONFIDENCE_ORDER = {"confirmed": 0, "strong": 1, "tentative": 2, "unknown": 3}
IMPORTANCE_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _category_rank(category):
    try:
        return PRIMARY_CATEGORIES.index(category)
    except ValueError:
        return len(PRIMARY_CATEGORIES)


def _fetch_run(connection, run_id):
    if run_id is not None:
        row = connection.execute(
            '''SELECT t.id, t.profile, t.result, t.started_at, a.path
               FROM test_runs t LEFT JOIN source_artifacts a ON a.id = t.source_artifact_id
               WHERE t.id = ?''', (run_id,)).fetchone()
        if row is None:
            raise ValueError(f'No test run with id {run_id}.')
        return row
    row = connection.execute(
        '''SELECT t.id, t.profile, t.result, t.started_at, a.path
           FROM test_runs t LEFT JOIN source_artifacts a ON a.id = t.source_artifact_id
           ORDER BY t.id DESC LIMIT 1''').fetchone()
    if row is None:
        raise ValueError('No test runs recorded; nothing to build context from.')
    return row


def _fetch_errors(connection, run_id):
    rows = connection.execute(
        '''SELECT severity, component, message, source_line FROM errors
           WHERE test_run_id = ?''', (run_id,)).fetchall()
    return sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r['severity'], 3), r['source_line'] or 0))


def _fetch_events(connection, run_id):
    rows = connection.execute(
        '''SELECT category, component, event_type, message, source_line FROM events
           WHERE test_run_id = ?''', (run_id,)).fetchall()
    return sorted(rows, key=lambda r: (_category_rank(r['category']), r['source_line'] or 0))


def _entity_label(connection, entity_id):
    if entity_id is None:
        return None
    row = connection.execute('SELECT name FROM entities WHERE id = ?', (entity_id,)).fetchone()
    return row['name'] if row else None


def _fetch_findings(connection):
    rows = connection.execute(
        '''SELECT id, subject_entity_id, subject_text, finding, confidence FROM findings
           WHERE status = 'active' ''').fetchall()
    return sorted(rows, key=lambda r: (CONFIDENCE_ORDER.get(r['confidence'], 4), -r['id']))


def _fetch_unknowns(connection):
    rows = connection.execute(
        '''SELECT id, subject_entity_id, question, importance FROM unknowns
           WHERE status = 'open' ''').fetchall()
    return sorted(rows, key=lambda r: (IMPORTANCE_ORDER.get(r['importance'], 4), -r['id']))


def _fetch_decisions(connection):
    rows = connection.execute(
        '''SELECT id, topic, decision, reason FROM decisions
           WHERE status = 'active' ''').fetchall()
    return sorted(rows, key=lambda r: -r['id'])


def build_context(database=None, run_id=None, max_chars=DEFAULT_MAX_CHARS,
                   max_events=DEFAULT_MAX_EVENTS, max_research_rows=DEFAULT_MAX_RESEARCH_ROWS):
    """Render the packet; truncates by section rather than mid-line."""
    with closing(connect_database(database, read_only=True)) as connection:
        validate_schema_version(connection)
        run = _fetch_run(connection, run_id)
        errors = _fetch_errors(connection, run['id'])
        events = _fetch_events(connection, run['id'])
        findings = _fetch_findings(connection)
        unknowns = _fetch_unknowns(connection)
        decisions = _fetch_decisions(connection)

        header = (f"# Research Context: Run #{run['id']} ({run['profile'] or 'no profile'})\n"
                  f"Result: {run['result']} | Source: {run['path'] or 'unknown'}\n")
        footer = "\n[Budget-truncated selection; query the database directly for the remainder.]\n"
        text = header
        budget = max_chars - len(footer)
        truncated = False

        def add(section_lines):
            nonlocal text, truncated
            addition = "\n".join(section_lines) + "\n"
            if len(text) + len(addition) > budget:
                truncated = True
                return False
            text += addition
            return True

        add([f"\n## Errors ({len(errors)})"])
        if not errors:
            add(["(none)"])
        for error in errors:
            location = f" (line {error['source_line']})" if error['source_line'] else ""
            if not add([f"- [{error['severity']}] {error['message'][:300]}{location}"]):
                break

        shown_events = events[:max_events]
        add([f"\n## Notable Events (top {len(shown_events)} of {len(events)})"])
        if not shown_events:
            add(["(none)"])
        for event in shown_events:
            location = f" (line {event['source_line']})" if event['source_line'] else ""
            if not add([f"- [{event['category']}] {event['message'][:300]}{location}"]):
                break

        shown_unknowns = unknowns[:max_research_rows]
        add(["\n## Open Unknowns"])
        if not shown_unknowns:
            add(["(none recorded yet)"])
        for unknown in shown_unknowns:
            subject = _entity_label(connection, unknown['subject_entity_id'])
            prefix = f"[{unknown['importance']}]" + (f" ({subject})" if subject else "")
            if not add([f"- {prefix} {unknown['question']}"]):
                break

        shown_findings = findings[:max_research_rows]
        add(["\n## Findings"])
        if not shown_findings:
            add(["(none recorded yet)"])
        for finding in shown_findings:
            subject = _entity_label(connection, finding['subject_entity_id']) or finding['subject_text']
            prefix = f"[{finding['confidence']}]" + (f" ({subject})" if subject else "")
            if not add([f"- {prefix} {finding['finding']}"]):
                break

        shown_decisions = decisions[:max_research_rows]
        add(["\n## Decisions"])
        if not shown_decisions:
            add(["(none recorded yet)"])
        for decision in shown_decisions:
            if not add([f"- {decision['topic']}: {decision['decision']} — {decision['reason']}"]):
                break

        if truncated:
            text += footer
        return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--run', type=int, help='Specific test run id; defaults to the latest run.')
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument('--max-events', type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument('--max-research-rows', type=int, default=DEFAULT_MAX_RESEARCH_ROWS)
    args = parser.parse_args(argv)
    try:
        print(build_context(args.database, args.run, args.max_chars, args.max_events, args.max_research_rows), end="")
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
