"""Small explicit fixtures shared by Phase 4A tests; never touch the live database."""

from pathlib import Path
import sqlite3
import tempfile

from database import db


class ResearchFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'research.db'
        db.initialize_database(self.path)
        self.connection = db.connect_database(self.path)
        self.addCleanup(self.connection.close)
        seed(self.connection)


def seed(connection):
    with connection:
        connection.execute("INSERT INTO source_artifacts(id, artifact_type, path, filename) VALUES (1,'log','missing.log','missing.log')")
        connection.execute("INSERT INTO test_runs(id,source_artifact_id,profile,result) VALUES (1,1,'audit','NEEDS_ATTENTION')")
        connection.execute("INSERT INTO test_runs(id,source_artifact_id,profile,result) VALUES (2,1,'audit','NEEDS_ATTENTION')")
        connection.execute("INSERT INTO entities(id,entity_type,name,canonical_name) VALUES (1,'game_object','Shared','shared')")
        connection.execute("INSERT INTO entities(id,entity_type,name,canonical_name) VALUES (2,'game_object','Old','old')")
        connection.execute("INSERT INTO entities(id,entity_type,name,canonical_name) VALUES (3,'game_object','New','new')")
        connection.execute("INSERT INTO events(id,test_run_id,source_artifact_id,category,event_type,message,source_line) VALUES (1,1,1,'identity','identity','Name=Shared',10)")
        connection.execute("INSERT INTO events(id,test_run_id,source_artifact_id,category,event_type,message,source_line) VALUES (2,2,1,'identity','identity','Name=Shared',99)")
        connection.execute("INSERT INTO errors(id,test_run_id,source_artifact_id,severity,message) VALUES (1,1,1,'ERROR','same error')")
        connection.execute("INSERT INTO errors(id,test_run_id,source_artifact_id,severity,message) VALUES (2,2,1,'ERROR','same error')")
        connection.execute("INSERT INTO findings(id,subject_entity_id,finding,confidence,source_artifact_id,source_line) VALUES (1,1,'Recorded finding','strong',1,10)")
        connection.execute("INSERT INTO unknowns(id,subject_entity_id,question) VALUES (1,1,'Recorded question?')")
        connection.execute("INSERT INTO decisions(id,subject_entity_id,finding_id,topic,decision,reason) VALUES (1,1,1,'Scope','Observe','Incomplete evidence')")
        connection.execute("INSERT INTO relationships(id,source_entity_id,target_entity_id,relationship_type) VALUES (1,1,2,'RELATED_TO')")


def create_v1(path):
    connection = sqlite3.connect(path)
    connection.executescript(Path(__file__).with_name('fixtures').joinpath('schema_v1.sql').read_text())
    seed(connection)
    connection.close()


def snapshot(connection):
    return {row[0]: [tuple(item) for item in connection.execute(f'SELECT * FROM {row[0]} ORDER BY id')]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_metadata'")}
