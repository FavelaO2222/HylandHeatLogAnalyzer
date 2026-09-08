"""Shared validation helpers for the manual research-record CLIs
(record_finding.py, record_unknown.py, record_decision.py).

Not a CLI itself. These are used identically wherever one of those records
needs to resolve or validate a foreign key reference before writing, so a
behavior change (wording, ambiguity handling) only needs to happen once.
"""


def resolve_entity_name(connection, name):
    """Resolve `name` to an existing entity id by exact case-insensitive name/canonical_name match.

    Raises ValueError if it matches zero or more than one entity, rather than guessing.
    """
    rows = connection.execute(
        'SELECT id, entity_type FROM entities WHERE lower(canonical_name) = lower(?) OR lower(name) = lower(?)',
        (name, name)).fetchall()
    if not rows:
        raise ValueError(f"No entity named '{name}'; use subject_entity_id instead.")
    if len(rows) > 1:
        options = ', '.join(f"{row['id']} ({row['entity_type']})" for row in rows)
        raise ValueError(f"'{name}' matches more than one entity ({options}); use subject_entity_id to disambiguate.")
    return rows[0]['id']


def require_row(connection, table, row_id, label):
    """Raise ValueError if row_id is not None and no row with that id exists in `table`."""
    if row_id is not None and connection.execute(f'SELECT 1 FROM {table} WHERE id = ?', (row_id,)).fetchone() is None:
        raise ValueError(f'No {label} with id {row_id}.')
