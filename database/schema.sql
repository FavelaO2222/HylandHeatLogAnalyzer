-- Authoritative fresh-database schema v4. db.initialize_database migrates v1/v2/v3 explicitly.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT OR IGNORE INTO schema_metadata (id, schema_version) VALUES (1, 4);

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

-- FTS5 search indexes begin --
-- events_fts/errors_fts/source_documents_fts are external-content FTS5 indexes
-- (content=events/errors/source_documents, content_rowid=id): those tables stay
-- the only source of truth, these virtual tables only ever hold a derived
-- search index over their text columns. Kept live by the AFTER INSERT/UPDATE/
-- DELETE triggers below on every write; the trailing 'rebuild' commands
-- recompute the whole index from current content every time this script runs
-- (fresh init, an explicit migration, or a plain reapply) -- idempotent and
-- cheap at this project's scale, so no separate drift-detection logic is
-- needed here. database.search.rebuild_search_index() exposes the same
-- 'rebuild' command standalone (CLI --rebuild / MCP rebuild_search_index) for
-- repairing drift from any write that bypassed these triggers (e.g. a raw
-- sqlite3 connection). database.backup excludes all of these tables (plus
-- source_documents itself, see below) from SQL dumps and recreates them (via
-- the fragment between these two markers) after a restore -- see that
-- module's docstring: events_fts/errors_fts/source_documents_fts are
-- excluded because a plain dump/restore cannot round-trip FTS5's internal
-- shadow-table state, while source_documents itself is excluded on a
-- different, deliberate policy (never put potentially large/copyrighted
-- ingested source text in a git-tracked file). Because both are excluded,
-- source_documents' own table definition lives in this block too, so a
-- restore recreates it structurally (empty) alongside its index; restoring
-- its actual content means re-running database.ingest_source, not a rebuild.
--
-- Schema v4: one row per ingested source/decompiled-assembly file (see
-- database/ingest_source.py). Unlike events/errors, ingestion never
-- deduplicates against prior runs of the same source_artifact -- re-ingesting
-- after code changes is a deliberate new snapshot, not a reused one, so the
-- history of what a file looked like as of a given ingestion is preserved.
CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY,
    source_artifact_id INTEGER NOT NULL REFERENCES source_artifacts(id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL CHECK (length(trim(relative_path)) > 0),
    language TEXT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_source_documents_artifact_path ON source_documents (source_artifact_id, relative_path);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    message, component, category, event_type, content='events', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS errors_fts USING fts5(
    message, component, stack_trace, content='errors', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS source_documents_fts USING fts5(
    relative_path, content, content='source_documents', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, message, component, category, event_type)
    VALUES (new.id, new.message, new.component, new.category, new.event_type);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message, component, category, event_type)
    VALUES ('delete', old.id, old.message, old.component, old.category, old.event_type);
END;
CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, message, component, category, event_type)
    VALUES ('delete', old.id, old.message, old.component, old.category, old.event_type);
    INSERT INTO events_fts(rowid, message, component, category, event_type)
    VALUES (new.id, new.message, new.component, new.category, new.event_type);
END;

CREATE TRIGGER IF NOT EXISTS errors_fts_ai AFTER INSERT ON errors BEGIN
    INSERT INTO errors_fts(rowid, message, component, stack_trace)
    VALUES (new.id, new.message, new.component, new.stack_trace);
END;
CREATE TRIGGER IF NOT EXISTS errors_fts_ad AFTER DELETE ON errors BEGIN
    INSERT INTO errors_fts(errors_fts, rowid, message, component, stack_trace)
    VALUES ('delete', old.id, old.message, old.component, old.stack_trace);
END;
CREATE TRIGGER IF NOT EXISTS errors_fts_au AFTER UPDATE ON errors BEGIN
    INSERT INTO errors_fts(errors_fts, rowid, message, component, stack_trace)
    VALUES ('delete', old.id, old.message, old.component, old.stack_trace);
    INSERT INTO errors_fts(rowid, message, component, stack_trace)
    VALUES (new.id, new.message, new.component, new.stack_trace);
END;

CREATE TRIGGER IF NOT EXISTS source_documents_fts_ai AFTER INSERT ON source_documents BEGIN
    INSERT INTO source_documents_fts(rowid, relative_path, content)
    VALUES (new.id, new.relative_path, new.content);
END;
CREATE TRIGGER IF NOT EXISTS source_documents_fts_ad AFTER DELETE ON source_documents BEGIN
    INSERT INTO source_documents_fts(source_documents_fts, rowid, relative_path, content)
    VALUES ('delete', old.id, old.relative_path, old.content);
END;
CREATE TRIGGER IF NOT EXISTS source_documents_fts_au AFTER UPDATE ON source_documents BEGIN
    INSERT INTO source_documents_fts(source_documents_fts, rowid, relative_path, content)
    VALUES ('delete', old.id, old.relative_path, old.content);
    INSERT INTO source_documents_fts(rowid, relative_path, content)
    VALUES (new.id, new.relative_path, new.content);
END;

INSERT INTO events_fts(events_fts) VALUES('rebuild');
INSERT INTO errors_fts(errors_fts) VALUES('rebuild');
INSERT INTO source_documents_fts(source_documents_fts) VALUES('rebuild');
-- FTS5 search indexes end --

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

-- Generic application API, concrete foreign keys: exactly one owner and target.
-- Existing finding provenance and decisions.finding_id remain independent links.
CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER REFERENCES findings(id) ON DELETE RESTRICT CHECK (finding_id > 0),
    unknown_id INTEGER REFERENCES unknowns(id) ON DELETE RESTRICT CHECK (unknown_id > 0),
    decision_id INTEGER REFERENCES decisions(id) ON DELETE RESTRICT CHECK (decision_id > 0),
    test_run_id INTEGER REFERENCES test_runs(id) ON DELETE RESTRICT CHECK (test_run_id > 0),
    event_id INTEGER REFERENCES events(id) ON DELETE RESTRICT CHECK (event_id > 0),
    error_id INTEGER REFERENCES errors(id) ON DELETE RESTRICT CHECK (error_id > 0),
    entity_id INTEGER REFERENCES entities(id) ON DELETE RESTRICT CHECK (entity_id > 0),
    relationship_id INTEGER REFERENCES relationships(id) ON DELETE RESTRICT CHECK (relationship_id > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK ((finding_id IS NOT NULL) + (unknown_id IS NOT NULL) + (decision_id IS NOT NULL) = 1),
    CHECK ((test_run_id IS NOT NULL) + (event_id IS NOT NULL) + (error_id IS NOT NULL)
           + (entity_id IS NOT NULL) + (relationship_id IS NOT NULL) = 1)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_links_pair ON evidence_links (
    ifnull(finding_id, 0), ifnull(unknown_id, 0), ifnull(decision_id, 0),
    ifnull(test_run_id, 0), ifnull(event_id, 0), ifnull(error_id, 0),
    ifnull(entity_id, 0), ifnull(relationship_id, 0)
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_finding ON evidence_links (finding_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_unknown ON evidence_links (unknown_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_decision ON evidence_links (decision_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_run ON evidence_links (test_run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_event ON evidence_links (event_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_error ON evidence_links (error_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_entity ON evidence_links (entity_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_relationship ON evidence_links (relationship_id);
