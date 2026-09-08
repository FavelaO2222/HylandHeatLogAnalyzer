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
from .evidence import evidence_label, fetch_evidence
from .ingestion import PRIMARY_CATEGORIES
from .research_records import resolve_entity_name

DEFAULT_MAX_CHARS = 6000
DEFAULT_MAX_EVENTS = 12
DEFAULT_MAX_RESEARCH_ROWS = 12
MAX_EVIDENCE_PER_RECORD = 6
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


class _BudgetedWriter:
    """Accumulates Markdown within a character budget; truncates by section, never mid-line."""

    FOOTER = "\n[Budget-truncated selection; query the database directly for the remainder.]\n"

    def __init__(self, header, max_chars):
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError('max_chars must be a positive integer.')
        self.footer = self.FOOTER if max_chars >= len(self.FOOTER) else '[Truncated]\n'
        if len(self.footer) > max_chars:
            self.footer = ''
        self.text = ''
        self.budget = max_chars - len(self.footer)
        self.truncated = False
        # Headers (paths/descriptions included) must obey the same hard cap.
        for line in header.splitlines():
            self.add([line])

    def add(self, section_lines):
        addition = "\n".join(section_lines) + "\n"
        if len(self.text) + len(addition) > self.budget:
            self.truncated = True
            return False
        self.text += addition
        return True

    def finish(self):
        if self.truncated:
            self.text += self.footer
        return self.text


def _research_lines(connection, version, kind, row_id, text):
    lines = [text]
    if version >= 2:
        evidence = fetch_evidence(connection, kind, row_id, limit=MAX_EVIDENCE_PER_RECORD)
        if evidence['total']:
            labels = '; '.join(evidence_label(item) for item in evidence['items'])
            remaining = evidence['total'] - len(evidence['items'])
            suffix = f'; +{remaining} more (list_evidence)' if remaining else ''
            lines.append(f'  Evidence ({kind} #{row_id}): {labels}{suffix}')
    return lines


def _validate_row_limit(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label} must be a nonnegative integer.')


def build_context(database=None, run_id=None, max_chars=DEFAULT_MAX_CHARS,
                   max_events=DEFAULT_MAX_EVENTS, max_research_rows=DEFAULT_MAX_RESEARCH_ROWS):
    """Render the packet; truncates by section rather than mid-line."""
    _validate_row_limit(max_events, 'max_events')
    _validate_row_limit(max_research_rows, 'max_research_rows')
    with closing(connect_database(database, read_only=True)) as connection:
        version = validate_schema_version(connection)
        connection.execute('BEGIN')
        run = _fetch_run(connection, run_id)
        errors = _fetch_errors(connection, run['id'])
        events = _fetch_events(connection, run['id'])
        findings = _fetch_findings(connection)
        unknowns = _fetch_unknowns(connection)
        decisions = _fetch_decisions(connection)

        header = (f"# Research Context: Run #{run['id']} ({run['profile'] or 'no profile'})\n"
                  f"Result: {run['result']} | Source: {run['path'] or 'unknown'}\n")
        writer = _BudgetedWriter(header, max_chars)
        add = writer.add

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
            if not add(_research_lines(connection, version, 'unknown', unknown['id'],
                                       f"- {prefix} {unknown['question']}")):
                break

        shown_findings = findings[:max_research_rows]
        add(["\n## Findings"])
        if not shown_findings:
            add(["(none recorded yet)"])
        for finding in shown_findings:
            subject = _entity_label(connection, finding['subject_entity_id']) or finding['subject_text']
            prefix = f"[{finding['confidence']}]" + (f" ({subject})" if subject else "")
            if not add(_research_lines(connection, version, 'finding', finding['id'],
                                       f"- {prefix} {finding['finding']}")):
                break

        shown_decisions = decisions[:max_research_rows]
        add(["\n## Decisions"])
        if not shown_decisions:
            add(["(none recorded yet)"])
        for decision in shown_decisions:
            if not add(_research_lines(connection, version, 'decision', decision['id'],
                                       f"- {decision['topic']}: {decision['decision']} — {decision['reason']}")):
                break

        return writer.finish()


def _fetch_entity(connection, entity_id):
    row = connection.execute(
        'SELECT id, entity_type, name, canonical_name, description FROM entities WHERE id = ?',
        (entity_id,)).fetchone()
    if row is None:
        raise ValueError(f'No entity with id {entity_id}.')
    return row


def _fetch_entity_findings(connection, entity_id):
    rows = connection.execute(
        'SELECT id, finding, confidence, status FROM findings WHERE subject_entity_id = ?',
        (entity_id,)).fetchall()
    return sorted(rows, key=lambda r: (CONFIDENCE_ORDER.get(r['confidence'], 4), -r['id']))


def _fetch_entity_unknowns(connection, entity_id):
    rows = connection.execute(
        'SELECT id, question, importance, status FROM unknowns WHERE subject_entity_id = ?',
        (entity_id,)).fetchall()
    return sorted(rows, key=lambda r: (IMPORTANCE_ORDER.get(r['importance'], 4), -r['id']))


def _fetch_entity_decisions(connection, entity_id):
    rows = connection.execute(
        'SELECT id, topic, decision, reason, status FROM decisions WHERE subject_entity_id = ?',
        (entity_id,)).fetchall()
    return sorted(rows, key=lambda r: -r['id'])


def _fetch_entity_relationships(connection, entity_id):
    rows = connection.execute('''
        SELECT r.id, r.relationship_type, r.notes, s.name AS source_name, t.name AS target_name
        FROM relationships r
        JOIN entities s ON s.id = r.source_entity_id
        JOIN entities t ON t.id = r.target_entity_id
        WHERE r.source_entity_id = ? OR r.target_entity_id = ?''', (entity_id, entity_id)).fetchall()
    return sorted(rows, key=lambda r: -r['id'])


def build_entity_context(database=None, entity_id=None, entity_name=None,
                         max_chars=DEFAULT_MAX_CHARS, max_research_rows=DEFAULT_MAX_RESEARCH_ROWS):
    """Render everything linked to one entity: its findings, unknowns, decisions, and
    relationships in either direction. Unlike build_context, every status is shown (not just
    active/open) since scope is already narrowed to one subject, so a superseded finding or a
    reversed decision about this entity is signal, not noise, here. Read-only; infers nothing.
    """
    if entity_id is not None and entity_name is not None:
        raise ValueError('Specify only one of entity_id or entity_name.')
    if entity_id is None and entity_name is None:
        raise ValueError('An entity is required: specify entity_id or entity_name.')
    _validate_row_limit(max_research_rows, 'max_research_rows')
    with closing(connect_database(database, read_only=True)) as connection:
        version = validate_schema_version(connection)
        connection.execute('BEGIN')
        if entity_name is not None:
            entity_id = resolve_entity_name(connection, entity_name)
        entity = _fetch_entity(connection, entity_id)
        findings = _fetch_entity_findings(connection, entity_id)
        unknowns = _fetch_entity_unknowns(connection, entity_id)
        decisions = _fetch_entity_decisions(connection, entity_id)
        relationships = _fetch_entity_relationships(connection, entity_id)

        header = f"# Entity Context: {entity['name']} ({entity['entity_type']})\n"
        if entity['description']:
            header += f"{entity['description']}\n"
        writer = _BudgetedWriter(header, max_chars)
        add = writer.add

        shown_findings = findings[:max_research_rows]
        add([f"\n## Findings (top {len(shown_findings)} of {len(findings)})"])
        if not shown_findings:
            add(["(none recorded)"])
        for finding in shown_findings:
            if not add(_research_lines(connection, version, 'finding', finding['id'],
                                       f"- [{finding['status']}, {finding['confidence']}] {finding['finding']}")):
                break

        shown_unknowns = unknowns[:max_research_rows]
        add([f"\n## Unknowns (top {len(shown_unknowns)} of {len(unknowns)})"])
        if not shown_unknowns:
            add(["(none recorded)"])
        for unknown in shown_unknowns:
            if not add(_research_lines(connection, version, 'unknown', unknown['id'],
                                       f"- [{unknown['status']}, {unknown['importance']}] {unknown['question']}")):
                break

        shown_decisions = decisions[:max_research_rows]
        add([f"\n## Decisions (top {len(shown_decisions)} of {len(decisions)})"])
        if not shown_decisions:
            add(["(none recorded)"])
        for decision in shown_decisions:
            if not add(_research_lines(connection, version, 'decision', decision['id'],
                                       f"- [{decision['status']}] {decision['topic']}: {decision['decision']} — {decision['reason']}")):
                break

        shown_relationships = relationships[:max_research_rows]
        add([f"\n## Relationships (top {len(shown_relationships)} of {len(relationships)})"])
        if not shown_relationships:
            add(["(none recorded)"])
        for relationship in shown_relationships:
            notes = f" ({relationship['notes']})" if relationship['notes'] else ''
            if not add([f"- [{relationship['id']}] {relationship['source_name']} "
                        f"{relationship['relationship_type']} {relationship['target_name']}{notes}"]):
                break

        return writer.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database')
    parser.add_argument('--run', type=int, help='Specific test run id; defaults to the latest run. '
                        'Not used together with --entity-id/--entity-name.')
    entity = parser.add_mutually_exclusive_group()
    entity.add_argument('--entity-id', type=int, help='Switch to entity-scoped context for this entity.')
    entity.add_argument('--entity-name', help='Switch to entity-scoped context, resolved by exact '
                        'case-insensitive entity name/canonical_name match.')
    parser.add_argument('--max-chars', type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument('--max-events', type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument('--max-research-rows', type=int, default=DEFAULT_MAX_RESEARCH_ROWS)
    args = parser.parse_args(argv)
    try:
        if args.entity_id is not None or args.entity_name is not None:
            if args.run is not None:
                raise ValueError('--run is not used together with --entity-id/--entity-name.')
            text = build_entity_context(args.database, entity_id=args.entity_id, entity_name=args.entity_name,
                                        max_chars=args.max_chars, max_research_rows=args.max_research_rows)
        else:
            text = build_context(args.database, args.run, args.max_chars, args.max_events, args.max_research_rows)
        print(text, end="")
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
