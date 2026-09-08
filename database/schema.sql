-- Authoritative fresh-database schema. Version changes require a future migration.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO schema_metadata (id, schema_version) VALUES (1, 1);

-- Raw evidence references. created_at is the optional original artifact time.
CREATE TABLE IF NOT EXISTS source_artifacts (
    id INTEGER PRIMARY KEY,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN
        ('log', 'source_code', 'assembly', 'decompiler_export', 'research_note', 'manual_observation', 'other')),
    path TEXT NOT NULL CHECK (length(trim(path)) > 0),
    filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
    sha256 TEXT CHECK (sha256 IS NULL OR
        (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-fA-F]*')),
    created_at TEXT,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_sha256 ON source_artifacts (sha256);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_type_path ON source_artifacts (artifact_type, path);

CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY,
    source_artifact_id INTEGER REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    started_at TEXT,
    profile TEXT,
    game_build TEXT,
    mod_build TEXT,
    result TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (result IN
        ('PASS', 'NEEDS_ATTENTION', 'FAIL', 'INCOMPLETE', 'UNKNOWN')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_test_runs_artifact ON test_runs (source_artifact_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_started_at ON test_runs (started_at);

-- Structured evidence. Each record points directly to its raw artifact.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    test_run_id INTEGER REFERENCES test_runs(id) ON DELETE RESTRICT,
    source_artifact_id INTEGER NOT NULL REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    timestamp TEXT,
    category TEXT NOT NULL CHECK (length(trim(category)) > 0),
    component TEXT,
    event_type TEXT,
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    source_line INTEGER CHECK (source_line IS NULL OR (typeof(source_line) = 'integer' AND source_line > 0)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_events_run_timestamp ON events (test_run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_artifact_line ON events (source_artifact_id, source_line);
CREATE INDEX IF NOT EXISTS idx_events_category_type ON events (category, event_type);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    test_run_id INTEGER REFERENCES test_runs(id) ON DELETE RESTRICT,
    source_artifact_id INTEGER NOT NULL REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    timestamp TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('ERROR', 'EXCEPTION', 'FATAL')),
    component TEXT,
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    stack_trace TEXT,
    source_line INTEGER CHECK (source_line IS NULL OR (typeof(source_line) = 'integer' AND source_line > 0)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_errors_run_timestamp ON errors (test_run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_errors_artifact_line ON errors (source_artifact_id, source_line);
CREATE INDEX IF NOT EXISTS idx_errors_severity ON errors (severity);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    canonical_name TEXT,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities (entity_type, name);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_name ON entities (canonical_name);

-- Research conclusions remain distinct from structured runtime evidence.
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    subject_entity_id INTEGER REFERENCES entities(id) ON DELETE RESTRICT,
    subject_text TEXT,
    finding TEXT NOT NULL CHECK (length(trim(finding)) > 0),
    confidence TEXT NOT NULL DEFAULT 'unknown' CHECK (confidence IN ('confirmed', 'strong', 'tentative', 'unknown')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'disproven')),
    source_artifact_id INTEGER REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    test_run_id INTEGER REFERENCES test_runs(id) ON DELETE RESTRICT,
    source_line INTEGER CHECK (source_line IS NULL OR (typeof(source_line) = 'integer' AND source_line > 0)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (source_line IS NULL OR source_artifact_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_findings_subject ON findings (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_findings_artifact_line ON findings (source_artifact_id, source_line);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings (test_run_id);
CREATE INDEX IF NOT EXISTS idx_findings_status_confidence ON findings (status, confidence);

CREATE TABLE IF NOT EXISTS unknowns (
    id INTEGER PRIMARY KEY,
    subject_entity_id INTEGER REFERENCES entities(id) ON DELETE RESTRICT,
    question TEXT NOT NULL CHECK (length(trim(question)) > 0),
    importance TEXT NOT NULL DEFAULT 'medium' CHECK (importance IN ('low', 'medium', 'high', 'critical')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'resolved', 'blocked')),
    required_evidence TEXT,
    related_feature TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_unknowns_subject ON unknowns (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_unknowns_status_importance ON unknowns (status, importance);

-- An optional finding link records a decision's basis without converting it into evidence.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    subject_entity_id INTEGER REFERENCES entities(id) ON DELETE RESTRICT,
    finding_id INTEGER REFERENCES findings(id) ON DELETE RESTRICT,
    topic TEXT NOT NULL CHECK (length(trim(topic)) > 0),
    decision TEXT NOT NULL CHECK (length(trim(decision)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'reversed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_subject ON decisions (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_decisions_finding ON decisions (finding_id);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions (status);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY,
    source_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    relationship_type TEXT NOT NULL CHECK (length(trim(relationship_type)) > 0),
    target_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    source_artifact_id INTEGER REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    test_run_id INTEGER REFERENCES test_runs(id) ON DELETE RESTRICT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_relationships_source_type ON relationships (source_entity_id, relationship_type);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships (target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relationships_artifact ON relationships (source_artifact_id);
CREATE INDEX IF NOT EXISTS idx_relationships_run ON relationships (test_run_id);
