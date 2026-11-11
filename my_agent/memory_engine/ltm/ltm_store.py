"""SQLite-backed LTM Core Store V1.

This module deliberately implements storage and deterministic validation only.
It does not perform semantic extraction, retrieval, ranking, or maintenance.
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .. import event_stream
from ..token_counter import count_text_tokens
from .storage_gate import serialized_write
from .embedding_adapter import EmbeddingServiceTimeout


LTM_DIR_NAME = "ltm"
LTM_DB_NAME = "ltm.db"

LTM_SCHEMA_VERSION = 1
LTM_DB_SCHEMA_VERSION = 11
LTM_FTS_TABLE = "memory_objects_fts"
SUMMARY_THRESHOLD_BYTES = 2 * 1024
SPLIT_THRESHOLD_BYTES = 8 * 1024
BUSY_TIMEOUT_MS = 5000
MAINTENANCE_VECTOR_SYNC_TIMEOUT_SECONDS = 120
OBJECT_TYPES = {"assertion", "tag", "episode"}
LINK_STATES = {"active", "inactive"}
RELATION_TYPES = {"sourced_from", "tagged_with", "supports", "contradicts", "supersedes"}
LEGACY_RELATION_TYPES = {"legacy_derived_from"}
IDEMPOTENCY_OPERATIONS = {
    "write", "maintenance_plan", "deactivate_link", "update_title", "update_summary", "successful_use",
}
STRENGTH_FORMULA_VERSION = 2
STRENGTH_HALF_LIFE_SLOTS = 2000
PUSH_STRENGTH_THRESHOLD = 0.1
DELETION_STRENGTH_THRESHOLD = 0.01
OBJECT_ID_RE = re.compile(r"^mem_[0-9a-f]{32}$")
EPISODE_STRUCTURED_DATA_ERROR_CODES = frozenset({
    "episode_schema",
    "time_scope",
    "time_scope_fields",
    "empty_time_scope",
    "time_scope_precision",
})


def _now():
    return datetime.now(timezone.utc).isoformat()


def ltm_dir(root=None):
    return event_stream.memory_root(root) / LTM_DIR_NAME


def ltm_db_path(root=None):
    return ltm_dir(root) / LTM_DB_NAME


def _new_id(prefix):
    return f"{prefix}_{uuid4().hex}"


def _connect(root=None, read_only=False):
    path = ltm_db_path(root)
    if read_only:
        if not path.exists():
            return None
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        ltm_dir(root).mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def _create_core_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_objects (
            object_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (object_type IN ('assertion', 'tag', 'episode'))
        );

        CREATE TABLE IF NOT EXISTS memory_payloads (
            object_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            body TEXT,
            structured_data TEXT,
            FOREIGN KEY(object_id) REFERENCES memory_objects(object_id)
        );

        CREATE TABLE IF NOT EXISTS memory_links (
            link_id TEXT PRIMARY KEY,
            source_object_id TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_object_id TEXT,
            slot_seq_start INTEGER,
            slot_seq_end INTEGER,
            relation_type TEXT NOT NULL,
            link_state TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(source_object_id) REFERENCES memory_objects(object_id),
            FOREIGN KEY(target_object_id) REFERENCES memory_objects(object_id),
            CHECK (target_kind IN ('memory_object', 'event_slot')),
            CHECK (relation_type IN ('sourced_from', 'tagged_with', 'supports', 'contradicts', 'supersedes', 'legacy_derived_from')),
            CHECK (link_state IN ('active', 'inactive'))
        );

        CREATE TABLE IF NOT EXISTS memory_object_derived (
            object_id TEXT PRIMARY KEY,
            successful_use_count INTEGER NOT NULL DEFAULT 0,
            gain_link_count INTEGER NOT NULL DEFAULT 0,
            loss_link_count INTEGER NOT NULL DEFAULT 0,
            neutral_link_count INTEGER NOT NULL DEFAULT 0,
            strength REAL NOT NULL DEFAULT 0.5,
            strength_formula_version INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(object_id) REFERENCES memory_objects(object_id)
        );

        CREATE TABLE IF NOT EXISTS ltm_operations (
            idempotency_key TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS system_owned_objects (
            tag_key TEXT PRIMARY KEY,
            object_id TEXT NOT NULL UNIQUE,
            owner TEXT NOT NULL,
            protected_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(object_id) REFERENCES memory_objects(object_id)
        );

        CREATE TABLE IF NOT EXISTS ltm_vector_journal (
            journal_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            staged_index_path TEXT,
            staged_metadata_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (state IN ('prepared', 'completed'))
        );

        CREATE TABLE IF NOT EXISTS maintenance_runtime_state (
            state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
            maintenance_coverage_cursor INTEGER NOT NULL DEFAULT -1,
            active_run_id TEXT,
            run_status TEXT NOT NULL DEFAULT 'idle',
            run_started_at TEXT,
            run_start_cursor INTEGER,
            run_owner_pid INTEGER,
            run_heartbeat_at TEXT,
            last_checkpoint_slot_seq INTEGER,
            next_plan_index INTEGER NOT NULL DEFAULT 0,
            last_committed_plan_index INTEGER,
            last_failure_slot_seq INTEGER,
            last_failure_reason TEXT,
            last_failure_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK (run_status IN ('idle', 'running', 'failed'))
        );

        CREATE TABLE IF NOT EXISTS maintenance_reports (
            maintenance_run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            start_cursor INTEGER NOT NULL,
            end_cursor INTEGER NOT NULL,
            coverage_status TEXT NOT NULL,
            processed_slot_count INTEGER NOT NULL,
            plan_commit_count INTEGER NOT NULL DEFAULT 0,
            created_object_count INTEGER NOT NULL DEFAULT 0,
            created_link_count INTEGER NOT NULL DEFAULT 0,
            deactivated_link_count INTEGER NOT NULL DEFAULT 0,
            updated_title_count INTEGER NOT NULL DEFAULT 0,
            updated_summary_count INTEGER NOT NULL DEFAULT 0,
            supersedes_count INTEGER NOT NULL DEFAULT 0,
            contradiction_count INTEGER NOT NULL DEFAULT 0,
            failure_slot_seq INTEGER,
            failure_reason TEXT,
            final_check_status TEXT NOT NULL,
            semantic_digest_status TEXT NOT NULL DEFAULT 'missing',
            semantic_digest_id TEXT,
            semantic_digest_token_count INTEGER NOT NULL DEFAULT 0,
            maintainable_frontier INTEGER,
            coverage_target_slot_seq INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_ltm_objects_type_created
            ON memory_objects(object_type, created_at);
        CREATE INDEX IF NOT EXISTS idx_ltm_links_source_relation_state
            ON memory_links(source_object_id, relation_type, link_state);
        CREATE INDEX IF NOT EXISTS idx_ltm_links_target_relation_state
            ON memory_links(target_object_id, relation_type, link_state);
        CREATE INDEX IF NOT EXISTS idx_ltm_links_slot_state
            ON memory_links(target_kind, slot_seq_start, slot_seq_end, link_state);
        CREATE INDEX IF NOT EXISTS idx_maintenance_reports_finished
            ON maintenance_reports(finished_at DESC);

        CREATE TABLE IF NOT EXISTS ltm_relation_migration_issues (
            issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id TEXT NOT NULL,
            original_relation_type TEXT NOT NULL,
            source_object_id TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_object_id TEXT,
            original_link_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        """
    )


def _columns(connection, table_name):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _ensure_maintenance_schema(connection):
    if not _table_exists(connection, "maintenance_runtime_state"):
        _create_core_schema(connection)
    runtime_columns = _columns(connection, "maintenance_runtime_state")
    for column, definition in (
        ("run_start_cursor", "INTEGER"),
        ("run_owner_pid", "INTEGER"),
        ("run_heartbeat_at", "TEXT"),
        ("last_checkpoint_slot_seq", "INTEGER"),
        ("next_plan_index", "INTEGER NOT NULL DEFAULT 0"),
        ("last_committed_plan_index", "INTEGER"),
        ("last_failure_slot_seq", "INTEGER"),
        ("last_failure_reason", "TEXT"),
        ("last_failure_at", "TEXT"),
    ):
        if column not in runtime_columns:
            connection.execute(f"ALTER TABLE maintenance_runtime_state ADD COLUMN {column} {definition}")
            runtime_columns.add(column)
    if not _table_exists(connection, "maintenance_reports"):
        _create_core_schema(connection)
    report_columns = _columns(connection, "maintenance_reports")
    for column, definition in (
        ("plan_commit_count", "INTEGER NOT NULL DEFAULT 0"),
        ("created_object_count", "INTEGER NOT NULL DEFAULT 0"),
        ("created_link_count", "INTEGER NOT NULL DEFAULT 0"),
        ("deactivated_link_count", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_title_count", "INTEGER NOT NULL DEFAULT 0"),
        ("updated_summary_count", "INTEGER NOT NULL DEFAULT 0"),
        ("supersedes_count", "INTEGER NOT NULL DEFAULT 0"),
        ("contradiction_count", "INTEGER NOT NULL DEFAULT 0"),
        ("failure_slot_seq", "INTEGER"),
        ("failure_reason", "TEXT"),
        ("final_check_status", "TEXT NOT NULL DEFAULT 'passed'"),
        ("semantic_digest_status", "TEXT NOT NULL DEFAULT 'missing'"),
        ("semantic_digest_id", "TEXT"),
        ("semantic_digest_token_count", "INTEGER NOT NULL DEFAULT 0"),
        ("maintainable_frontier", "INTEGER"),
        ("coverage_target_slot_seq", "INTEGER"),
    ):
        if column not in report_columns:
            connection.execute(f"ALTER TABLE maintenance_reports ADD COLUMN {column} {definition}")
            report_columns.add(column)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_maintenance_reports_finished "
        "ON maintenance_reports(finished_at DESC)"
    )


def _migrate_core_schema(connection):
    if not _table_exists(connection, "memory_objects"):
        _create_core_schema(connection)
        return
    if "status" not in _columns(connection, "memory_objects"):
        _create_core_schema(connection)
        return
    forgotten_count = connection.execute(
        "SELECT COUNT(*) FROM memory_objects WHERE status = 'forgotten'"
    ).fetchone()[0]
    if forgotten_count:
        raise RuntimeError(
            "LTM schema migration refuses legacy forgotten MemoryObjects; prune them explicitly before migrating."
        )
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table_name in (
            "memory_payloads", "memory_links", "memory_object_derived", "ltm_operations",
            "system_owned_objects", "ltm_vector_journal", "memory_objects",
        ):
            if _table_exists(connection, table_name):
                connection.execute(f"ALTER TABLE {table_name} RENAME TO {table_name}_legacy_v2")
        _create_core_schema(connection)
        connection.execute(
            """INSERT INTO memory_objects(object_id, object_type, title, summary, created_at, updated_at)
               SELECT object_id, object_type, title, summary, created_at, updated_at
               FROM memory_objects_legacy_v2"""
        )
        if _table_exists(connection, "memory_payloads_legacy_v2"):
            connection.execute(
                """INSERT INTO memory_payloads(object_id, schema_version, body, structured_data)
                   SELECT object_id, schema_version, body, structured_data FROM memory_payloads_legacy_v2"""
            )
        if _table_exists(connection, "memory_links_legacy_v2"):
            connection.execute(
                """INSERT INTO memory_links(
                       link_id, source_object_id, target_kind, target_object_id, slot_seq_start,
                       slot_seq_end, relation_type, link_state, created_at, updated_at)
                   SELECT link_id, source_object_id, target_kind, target_object_id, slot_seq_start,
                          slot_seq_end,
                          CASE
                              WHEN relation_type = 'derived_from' AND target_kind = 'event_slot' THEN 'sourced_from'
                              WHEN relation_type = 'derived_from' AND target_kind = 'memory_object'
                                   AND target_object_id IN (
                                       SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type = 'tag'
                                   ) THEN 'tagged_with'
                              WHEN relation_type = 'derived_from' AND target_kind = 'memory_object'
                                   AND target_object_id IN (
                                       SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type = 'assertion'
                                   )
                                   AND source_object_id IN (
                                       SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type IN ('assertion', 'episode')
                                   ) THEN 'supports'
                              WHEN relation_type = 'derived_from' THEN 'legacy_derived_from'
                              ELSE relation_type
                          END,
                          CASE
                              WHEN relation_type = 'derived_from'
                                   AND target_kind = 'memory_object'
                                   AND NOT (
                                       target_object_id IN (
                                           SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type = 'tag'
                                       )
                                       OR (
                                           target_object_id IN (
                                               SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type = 'assertion'
                                           )
                                           AND source_object_id IN (
                                               SELECT object_id FROM memory_objects_legacy_v2 WHERE object_type IN ('assertion', 'episode')
                                           )
                                       )
                                   ) THEN 'inactive'
                              WHEN status = 'forgotten' THEN 'inactive' ELSE 'active'
                          END,
                          created_at, updated_at
                   FROM memory_links_legacy_v2"""
            )
            connection.execute(
                """INSERT INTO ltm_relation_migration_issues(
                       link_id, original_relation_type, source_object_id, target_kind,
                       target_object_id, original_link_state, reason, recorded_at)
                   SELECT l.link_id, l.relation_type, l.source_object_id, l.target_kind,
                          l.target_object_id,
                          CASE l.status WHEN 'forgotten' THEN 'inactive' ELSE 'active' END,
                          'object-to-object derived_from could not be mapped to a canonical relation',
                          ?
                   FROM memory_links_legacy_v2 AS l
                   LEFT JOIN memory_objects_legacy_v2 AS source ON source.object_id = l.source_object_id
                   LEFT JOIN memory_objects_legacy_v2 AS target ON target.object_id = l.target_object_id
                   WHERE l.relation_type = 'derived_from'
                     AND l.target_kind = 'memory_object'
                     AND NOT (
                         target.object_type = 'tag'
                         OR (target.object_type = 'assertion' AND source.object_type IN ('assertion', 'episode'))
                     )""",
                (_now(),),
            )
        if _table_exists(connection, "ltm_operations_legacy_v2"):
            connection.execute(
                """INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at)
                   SELECT idempotency_key, operation_type, request_hash, result_json, created_at
                   FROM ltm_operations_legacy_v2"""
            )
        if _table_exists(connection, "system_owned_objects_legacy_v2"):
            connection.execute(
                """INSERT INTO system_owned_objects(tag_key, object_id, owner, protected_kind, created_at)
                   SELECT tag_key, object_id, owner, protected_kind, created_at
                   FROM system_owned_objects_legacy_v2"""
            )
        for table_name in (
            "memory_payloads", "memory_links", "memory_object_derived", "ltm_operations",
            "system_owned_objects", "ltm_vector_journal", "memory_objects",
        ):
            legacy = f"{table_name}_legacy_v2"
            if _table_exists(connection, legacy):
                connection.execute(f"DROP TABLE {legacy}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _recover_vector_journal(connection, root=None):
    rows = connection.execute(
        "SELECT journal_id, staged_index_path, staged_metadata_path FROM ltm_vector_journal WHERE state = 'prepared'"
    ).fetchall()
    if not rows:
        return
    from . import vector_index

    for row in rows:
        staged = {"index_path": row["staged_index_path"], "metadata_path": row["staged_metadata_path"]}
        try:
            vector_index.install_staged_vector(staged, root=root)
        except Exception as error:
            raise RuntimeError(f"LTM Vector journal recovery failed: {error}") from error
        connection.execute(
            "UPDATE ltm_vector_journal SET state = 'completed', updated_at = ? WHERE journal_id = ?",
            (_now(), row["journal_id"]),
        )


def _migrate_relation_semantics(connection):
    """Migrate legacy derived_from links to the canonical relation names."""
    if not _table_exists(connection, "memory_links"):
        return
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_links'"
    ).fetchone()
    table_sql = (table_sql_row[0] or "").lower() if table_sql_row else ""
    if "sourced_from" in table_sql:
        return

    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE memory_links RENAME TO memory_links_legacy_relation_v1")
        connection.execute(
            """
            CREATE TABLE memory_links (
                link_id TEXT PRIMARY KEY,
                source_object_id TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                target_object_id TEXT,
                slot_seq_start INTEGER,
                slot_seq_end INTEGER,
                relation_type TEXT NOT NULL,
                link_state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_object_id) REFERENCES memory_objects(object_id),
                FOREIGN KEY(target_object_id) REFERENCES memory_objects(object_id),
                CHECK (target_kind IN ('memory_object', 'event_slot')),
                CHECK (relation_type IN ('sourced_from', 'tagged_with', 'supports', 'contradicts', 'supersedes', 'legacy_derived_from')),
                CHECK (link_state IN ('active', 'inactive'))
            )
            """
        )
        rows = connection.execute(
            """
            SELECT l.*, source.object_type AS source_type, target.object_type AS target_type
            FROM memory_links_legacy_relation_v1 AS l
            LEFT JOIN memory_objects AS source ON source.object_id = l.source_object_id
            LEFT JOIN memory_objects AS target ON target.object_id = l.target_object_id
            ORDER BY l.created_at, l.link_id
            """
        ).fetchall()
        for row in rows:
            relation = row["relation_type"]
            state = row["link_state"]
            reason = None
            if relation == "derived_from":
                if row["target_kind"] == "event_slot":
                    relation = "sourced_from"
                elif row["target_type"] == "tag":
                    relation = "tagged_with"
                elif row["target_type"] == "assertion" and row["source_type"] in {"assertion", "episode"}:
                    relation = "supports"
                else:
                    relation = "legacy_derived_from"
                    state = "inactive"
                    reason = "object-to-object derived_from could not be mapped to a canonical relation"
            connection.execute(
                """
                INSERT INTO memory_links(
                    link_id, source_object_id, target_kind, target_object_id,
                    slot_seq_start, slot_seq_end, relation_type, link_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["link_id"], row["source_object_id"], row["target_kind"],
                    row["target_object_id"], row["slot_seq_start"], row["slot_seq_end"],
                    relation, state, row["created_at"], row["updated_at"],
                ),
            )
            if reason:
                connection.execute(
                    """
                    INSERT INTO ltm_relation_migration_issues(
                        link_id, original_relation_type, source_object_id, target_kind,
                        target_object_id, original_link_state, reason, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["link_id"], row["relation_type"], row["source_object_id"],
                        row["target_kind"], row["target_object_id"], row["link_state"],
                        reason, _now(),
                    ),
                )
        connection.execute("DROP TABLE memory_links_legacy_relation_v1")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_links_source_relation_state ON memory_links(source_object_id, relation_type, link_state)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_links_target_relation_state ON memory_links(target_object_id, relation_type, link_state)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ltm_links_slot_state ON memory_links(target_kind, slot_seq_start, slot_seq_end, link_state)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


@serialized_write
def ensure_ltm_store(root=None):
    return _ensure_ltm_store(root)


def _ensure_ltm_store(root=None, *, recover_indexes=True):
    """Create or migrate the LTM schema without touching Event Stream storage."""
    connection = _connect(root)
    try:
        previous_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        _migrate_core_schema(connection)
        _migrate_relation_semantics(connection)
        _ensure_maintenance_schema(connection)
        _ensure_forgetting_schema(connection, root)
        _ensure_retrieval_schema(connection)
        _ensure_maintenance_state_row(connection)
        object_count = connection.execute("SELECT COUNT(*) FROM live_memory_objects").fetchone()[0]
        derived_count = connection.execute("SELECT COUNT(*) FROM live_memory_object_derived").fetchone()[0]
        stale_count = connection.execute(
            "SELECT COUNT(*) FROM live_memory_object_derived WHERE strength_formula_version != ?",
            (STRENGTH_FORMULA_VERSION,),
        ).fetchone()[0]
        if previous_version < LTM_DB_SCHEMA_VERSION or derived_count != object_count or stale_count:
            _recompute_derived(connection, _all_object_ids(connection))
        if recover_indexes:
            _recover_vector_journal(connection, root)
        connection.commit()
        if recover_indexes:
            _recover_forgetting_indexes(connection, root)
        return connection
    except Exception:
        connection.rollback()
        connection.close()
        raise


def _recover_forgetting_indexes(connection, root):
    pending = connection.execute("SELECT maintenance_run_id, result_json FROM ltm_forgetting_batches WHERE index_synced=0").fetchall()
    if not pending:
        return
    from .vector_index import get_vector_index
    for row in pending:
        get_vector_index(root).remove_many(json.loads(row["result_json"])["deleted_object_ids"])
        connection.execute("UPDATE ltm_forgetting_batches SET index_synced=1 WHERE maintenance_run_id=?", (row["maintenance_run_id"],))
    connection.commit()


def _ensure_forgetting_schema(connection, root=None):
    """Initialize rollout anchors without inventing historical effective uses."""
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    if "deleted_at" not in _columns(connection, "memory_objects"):
        connection.execute("ALTER TABLE memory_objects ADD COLUMN deleted_at TEXT")
    columns = _columns(connection, "memory_object_derived")
    if "last_effective_use_slot_seq" not in columns:
        connection.execute("ALTER TABLE memory_object_derived ADD COLUMN last_effective_use_slot_seq INTEGER")
    if "decay_origin_slot_seq" not in columns:
        connection.execute("ALTER TABLE memory_object_derived ADD COLUMN decay_origin_slot_seq INTEGER NOT NULL DEFAULT -1")
        connection.execute("UPDATE memory_object_derived SET decay_origin_slot_seq=?", (_latest_event_slot_seq(root),))
    connection.execute("""CREATE TABLE IF NOT EXISTS ltm_forgetting_batches (
        maintenance_run_id TEXT PRIMARY KEY, slot_seq INTEGER NOT NULL,
        result_json TEXT NOT NULL, index_synced INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")

    for view in ("live_memory_links", "live_memory_object_derived", "live_memory_objects"):
        connection.execute(f"DROP VIEW IF EXISTS {view}")

    connection.execute("""CREATE VIEW IF NOT EXISTS live_memory_objects AS
        SELECT * FROM memory_objects WHERE deleted_at IS NULL""")
    connection.execute("""CREATE VIEW IF NOT EXISTS live_memory_links AS
        SELECT l.* FROM memory_links l
        JOIN live_memory_objects s ON s.object_id=l.source_object_id
        WHERE l.target_kind='event_slot' OR EXISTS
          (SELECT 1 FROM live_memory_objects t WHERE t.object_id=l.target_object_id)""")
    connection.execute("""CREATE VIEW IF NOT EXISTS live_memory_object_derived AS
        SELECT d.* FROM memory_object_derived d
        JOIN live_memory_objects o ON o.object_id=d.object_id""")


def _connection_slot(connection):
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    return _latest_event_slot_seq(Path(path).parents[3])


def decayed_strength(baseline, current_slot, reference_slot, *, protected=False):
    baseline = max(0.0, min(1.0, baseline))
    return baseline if protected else baseline * 2.0 ** (-max(0, current_slot - reference_slot) / STRENGTH_HALF_LIFE_SLOTS)


def forgetting_generation(root=None):
    connection = _connect(root, read_only=True)
    if connection is None:
        return 0
    try:
        if not _table_exists(connection, "ltm_forgetting_batches"):
            return 0
        return connection.execute("SELECT COALESCE(MAX(rowid),0) FROM ltm_forgetting_batches WHERE json_extract(result_json,'$.deleted_count') > 0").fetchone()[0]
    finally:
        connection.close()


@serialized_write
def finish_forgetting_batch(maintenance_run_id, root=None):
    """Commit one non-cascading batch per run, then retry durable index cleanup."""
    if not isinstance(maintenance_run_id, str) or not maintenance_run_id:
        raise ValueError("maintenance_run_id is required")
    connection = ensure_ltm_store(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM ltm_forgetting_batches WHERE maintenance_run_id=?", (maintenance_run_id,)
        ).fetchone()
        if existing:
            result = json.loads(existing["result_json"])
        else:
            slot = _latest_event_slot_seq(root)
            object_ids = _all_object_ids(connection)
            _recompute_derived(connection, object_ids, current_slot=slot)
            deleted = [row[0] for row in connection.execute("""
                SELECT o.object_id FROM live_memory_objects o
                JOIN memory_object_derived d ON d.object_id=o.object_id
                WHERE d.strength < ? AND NOT EXISTS
                  (SELECT 1 FROM system_owned_objects s WHERE s.object_id=o.object_id)
                ORDER BY o.object_id""", (DELETION_STRENGTH_THRESHOLD,))]
            now = _now()
            neighbors = set()
            provenance_ranges = set()
            for object_id in deleted:
                provenance_ranges.update(tuple(row) for row in connection.execute(
                    "SELECT slot_seq_start,slot_seq_end FROM memory_links WHERE source_object_id=? AND target_kind='event_slot' AND relation_type='sourced_from'", (object_id,)))
                for row in connection.execute("""SELECT source_object_id, target_object_id
                    FROM live_memory_links WHERE source_object_id=? OR target_object_id=?""", (object_id, object_id)):
                    neighbors.update(value for value in row if value)
            connection.executemany("UPDATE memory_objects SET deleted_at=? WHERE object_id=?", ((now, oid) for oid in deleted))
            connection.executemany(f"DELETE FROM {LTM_FTS_TABLE} WHERE object_id=?", ((oid,) for oid in deleted))
            _recompute_derived(connection, neighbors - set(deleted), current_slot=slot)
            result = {"status": "committed", "slot_seq": slot, "refreshed_count": len(object_ids),
                      "deleted_count": len(deleted), "deleted_object_ids": deleted,
                      "provenance_ranges": [list(pair) for pair in sorted(provenance_ranges)],
                      "maintenance_run_id": maintenance_run_id}
            connection.execute("INSERT INTO ltm_forgetting_batches VALUES (?, ?, ?, 0, ?)",
                               (maintenance_run_id, slot, _canonical_json(result), now))
        connection.commit()
        if not existing or not existing["index_synced"]:
            from .vector_index import get_vector_index
            get_vector_index(root).remove_many(result["deleted_object_ids"])
            connection.execute("UPDATE ltm_forgetting_batches SET index_synced=1 WHERE maintenance_run_id=?", (maintenance_run_id,))
            connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_maintenance_state_row(connection):
    connection.execute(
        """INSERT OR IGNORE INTO maintenance_runtime_state(
               state_id, maintenance_coverage_cursor, run_status, next_plan_index, updated_at)
           VALUES (1, -1, 'idle', 0, ?)""",
        (_now(),),
    )


def _table_exists(connection, table_name):
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone() is not None


def _ensure_retrieval_schema(connection):
    """Create and backfill derived retrieval structures owned by the Store."""
    previous_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ltm_objects_created "
        "ON memory_objects(created_at, object_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ltm_objects_type_created "
        "ON memory_objects(object_type, created_at, object_id)"
    )
    created_fts = not _table_exists(connection, LTM_FTS_TABLE)
    try:
        connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {LTM_FTS_TABLE} USING fts5(
                object_id UNINDEXED,
                title,
                summary,
                payload_body,
                tokenize='trigram'
            )
            """
        )
    except sqlite3.OperationalError as error:
        raise RuntimeError(
            "LTM Retrieval initialization requires SQLite FTS5 with the trigram tokenizer."
        ) from error
    if created_fts or previous_version < LTM_DB_SCHEMA_VERSION:
        _rebuild_retrieval_index(connection)
    connection.execute(f"PRAGMA user_version = {LTM_DB_SCHEMA_VERSION}")


def _rebuild_retrieval_index(connection):
    connection.execute(f"DELETE FROM {LTM_FTS_TABLE}")
    connection.execute(
        f"""
        INSERT INTO {LTM_FTS_TABLE}(object_id, title, summary, payload_body)
        SELECT o.object_id, o.title, COALESCE(o.summary, ''), COALESCE(p.body, '')
        FROM live_memory_objects AS o
        LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id
        ORDER BY o.created_at, o.object_id
        """
    )


def _sync_retrieval_index_object(connection, object_id):
    """Synchronize one canonical object into the derived active-only FTS index."""
    if not _table_exists(connection, LTM_FTS_TABLE):
        raise RuntimeError("LTM Retrieval index is not initialized.")
    connection.execute(f"DELETE FROM {LTM_FTS_TABLE} WHERE object_id = ?", (object_id,))
    connection.execute(
        f"""
        INSERT INTO {LTM_FTS_TABLE}(object_id, title, summary, payload_body)
        SELECT o.object_id, o.title, COALESCE(o.summary, ''), COALESCE(p.body, '')
        FROM live_memory_objects AS o
        LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id
        WHERE o.object_id = ?
        """,
        (object_id,),
    )


def _error(code, path, message, details=None):
    result = {"code": code, "path": path, "message": message}
    if details:
        result["details"] = details
    return result


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_bytes(payload):
    body = payload.get("body") or ""
    structured = payload.get("structured_data")
    structured_text = "" if structured in (None, {}) else _canonical_json(structured)
    return len(body.encode("utf-8")) + len(structured_text.encode("utf-8"))


def _validate_title(title, errors, path):
    if not isinstance(title, str) or not title.strip():
        errors.append(_error("invalid_title", path, "title must be a non-empty string."))
        return
    count = count_text_tokens(title)
    if count < 1 or count > 50:
        error = _error("title_length", path, "title must contain 1-50 tokens.")
        error.update({"actual_tokens": count, "min_tokens": 1, "max_tokens": 50})
        errors.append(error)


def _validate_payload(obj, errors, path):
    payload = obj.get("payload")
    summary = obj.get("summary")
    if payload is None:
        if summary not in (None, ""):
            error = _error("summary_without_payload", f"{path}.summary", "summary requires a payload.")
            error.update({
                "actual_payload_bytes": 0,
                "summary_threshold_bytes": SUMMARY_THRESHOLD_BYTES,
                "max_payload_bytes": SPLIT_THRESHOLD_BYTES,
            })
            errors.append(error)
        return None
    if not isinstance(payload, dict):
        errors.append(_error("invalid_payload", f"{path}.payload", "payload must be an object."))
        return None
    if payload.get("schema_version") != LTM_SCHEMA_VERSION:
        errors.append(_error("schema_version", f"{path}.payload.schema_version", "schema_version must be 1."))
    body = payload.get("body")
    if body is not None and not isinstance(body, str):
        errors.append(_error("invalid_body", f"{path}.payload.body", "body must be a string."))
    structured = payload.get("structured_data")
    if structured is not None and not isinstance(structured, dict):
        errors.append(_error("invalid_structured_data", f"{path}.payload.structured_data", "structured_data must be a JSON object."))
    if structured is None:
        structured = {}
    if obj.get("object_type") in {"assertion", "tag"} and structured:
        errors.append(_error("structured_data_not_allowed", f"{path}.payload.structured_data", "assertion and tag do not allow structured_data in V1."))
    if obj.get("object_type") == "episode" and structured:
        unknown = set(structured) - {"time_scope"}
        if unknown:
            errors.append(_error("episode_schema", f"{path}.payload.structured_data", "episode structured_data only allows time_scope."))
        if "time_scope" in structured:
            time_scope = structured.get("time_scope")
            if not isinstance(time_scope, dict):
                errors.append(_error("time_scope", f"{path}.payload.structured_data.time_scope", "time_scope must be an object."))
            else:
                allowed = {"start", "end", "anchor", "precision"}
                if set(time_scope) - allowed:
                    errors.append(_error("time_scope_fields", f"{path}.payload.structured_data.time_scope", "unknown time_scope field."))
                if not any(time_scope.get(key) not in (None, "") for key in ("start", "end", "anchor")):
                    errors.append(_error("empty_time_scope", f"{path}.payload.structured_data.time_scope", "time_scope needs start, end, or anchor."))
                if "precision" in time_scope and time_scope.get("precision") not in {"exact", "approximate", "relative"}:
                    errors.append(_error("time_scope_precision", f"{path}.payload.structured_data.time_scope.precision", "precision must be exact, approximate, or relative."))
    normalized_payload = {
        "schema_version": payload.get("schema_version"),
        "body": body if body not in (None, "") else None,
        "structured_data": structured if structured else None,
    }
    payload_size = _payload_bytes(normalized_payload)
    if payload_size == 0:
        errors.append(_error("empty_payload", f"{path}.payload", "payload must contain body or structured_data."))
    elif payload_size <= SUMMARY_THRESHOLD_BYTES and summary not in (None, ""):
        error = _error("unexpected_summary", f"{path}.summary", "summary must be empty for payloads up to 2 KiB.")
        error.update({
            "actual_payload_bytes": payload_size,
            "summary_threshold_bytes": SUMMARY_THRESHOLD_BYTES,
            "max_payload_bytes": SPLIT_THRESHOLD_BYTES,
        })
        errors.append(error)
    elif SUMMARY_THRESHOLD_BYTES < payload_size <= SPLIT_THRESHOLD_BYTES and not isinstance(summary, str):
        error = _error("missing_summary", f"{path}.summary", "summary is required for payloads over 2 KiB.")
        error.update({
            "actual_payload_bytes": payload_size,
            "summary_threshold_bytes": SUMMARY_THRESHOLD_BYTES,
            "max_payload_bytes": SPLIT_THRESHOLD_BYTES,
        })
        errors.append(error)
    elif payload_size > SPLIT_THRESHOLD_BYTES:
        error = _error("payload_too_large", f"{path}.payload", "payload over 8 KiB must be split into multiple MemoryObjects.")
        error.update({
            "actual_payload_bytes": payload_size,
            "summary_threshold_bytes": SUMMARY_THRESHOLD_BYTES,
            "max_payload_bytes": SPLIT_THRESHOLD_BYTES,
        })
        errors.append(error)
    return normalized_payload


def _validate_object(obj, index, errors, connection):
    path = f"objects[{index}]"
    if not isinstance(obj, dict):
        errors.append(_error("invalid_object", path, "object must be an object."))
        return None
    object_id = obj.get("object_id")
    if not isinstance(object_id, str) or not OBJECT_ID_RE.fullmatch(object_id):
        errors.append(_error("object_id", f"{path}.object_id", "object_id must match mem_<uuid_hex>."))
    object_type = obj.get("object_type")
    if object_type not in OBJECT_TYPES:
        errors.append(_error("object_type", f"{path}.object_type", "object_type must be assertion, tag, or episode."))
    existing = connection.execute(
        "SELECT 1 FROM memory_objects WHERE object_id = ?",
        (object_id,),
    ).fetchone()
    if existing is not None:
        protected = _system_owned_row(connection, object_id)
        code = "protected_system_object" if protected is not None else "object_exists"
        message = "system-owned objects cannot be overwritten." if protected is not None else "object_id already exists."
        errors.append(_error(code, f"{path}.object_id", message))
    _validate_title(obj.get("title"), errors, f"{path}.title")
    for forbidden in ("status", "created_at", "updated_at"):
        if forbidden in obj:
            errors.append(_error("forbidden_field", f"{path}.{forbidden}", "the Store assigns this field."))
    payload = _validate_payload(obj, errors, path) if object_type in OBJECT_TYPES else None
    return {"object_id": object_id, "object_type": object_type, "title": obj.get("title"), "summary": obj.get("summary"), "payload": payload}


def _slot_exists_read_only(slot_seq, root=None):
    path = event_stream.event_stream_db_path(root)
    if not path.exists():
        return False
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT 1 FROM slots WHERE slot_seq = ?", (slot_seq,)).fetchone() is not None
    finally:
        connection.close()


def _object_exists(connection, object_id, plan_ids):
    return object_id in plan_ids or connection.execute("SELECT 1 FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone() is not None


def _system_owned_row(connection, object_id):
    try:
        return connection.execute(
            "SELECT tag_key, owner, protected_kind FROM system_owned_objects WHERE object_id = ?",
            (object_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _object_type(connection, object_id, objects_by_id):
    if object_id in objects_by_id:
        return objects_by_id[object_id]["object_type"]
    row = connection.execute("SELECT object_type FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone()
    return row["object_type"] if row else None


def _validate_link(link, index, connection, objects_by_id, root, errors):
    path = f"links[{index}]"
    if not isinstance(link, dict):
        errors.append(_error("invalid_link", path, "link must be an object."))
        return None
    source = link.get("source") or {}
    source_id = source.get("object_id")
    if not _object_exists(connection, source_id, set(objects_by_id)):
        errors.append(_error("source_not_found", f"{path}.source.object_id", "source object does not exist."))
    relation = link.get("relation_type")
    if relation not in RELATION_TYPES:
        errors.append(_error("relation_type", f"{path}.relation_type", "unsupported relation_type."))
    target = link.get("target") or {}
    object_target = target.get("object_id")
    event_target = target.get("event_slots")
    if bool(object_target) == bool(event_target):
        errors.append(_error("target", f"{path}.target", "target must contain exactly one object_id or event_slots."))
        return None
    target_kind = "memory_object" if object_target else "event_slot"
    target_id = object_target if object_target else None
    start = end = None
    target_type = None
    if object_target:
        if not _object_exists(connection, object_target, set(objects_by_id)):
            errors.append(_error("target_not_found", f"{path}.target.object_id", "target object does not exist."))
        target_type = _object_type(connection, object_target, objects_by_id)
        if source_id == object_target:
            errors.append(_error("self_link", path, "self-links are not allowed."))
    else:
        if not isinstance(event_target, dict):
            errors.append(_error("event_slots", f"{path}.target.event_slots", "event_slots must be an object."))
        else:
            start = event_target.get("start")
            end = event_target.get("end", start)
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                errors.append(_error("slot_range", f"{path}.target.event_slots", "slot range must be non-negative and start <= end."))
            elif not all(_slot_exists_read_only(seq, root) for seq in range(start, end + 1)):
                errors.append(_error("slot_not_found", f"{path}.target.event_slots", "every referenced Event Stream slot must exist."))
    source_type = _object_type(connection, source_id, objects_by_id)
    valid_relation = (
        relation == "sourced_from" and target_kind == "event_slot"
        or relation == "tagged_with" and target_type == "tag" and source_type in OBJECT_TYPES
        or relation in {"supports", "contradicts"} and target_type == "assertion" and source_type in {"assertion", "episode"}
        or relation == "supersedes" and target_kind == "memory_object" and source_type in OBJECT_TYPES and target_type in OBJECT_TYPES
    )
    if relation == "tagged_with" and target_kind == "event_slot":
        valid_relation = False
    if relation in {"supports", "contradicts", "supersedes"} and target_kind == "event_slot":
        valid_relation = False
    if not valid_relation:
        if relation in {"supports", "contradicts"}:
            message = (
                f"{relation} requires a source assertion or episode and a target assertion; "
                f"got source {source_type or 'unknown'} and target {target_type or target_kind}."
            )
        elif relation == "tagged_with":
            message = f"tagged_with requires a target tag; got target {target_type or target_kind}."
        elif relation == "sourced_from":
            message = f"sourced_from requires an Event Stream slot target; got target {target_type or target_kind}."
        else:
            message = (
                f"supersedes requires MemoryObject endpoints; got source {source_type or 'unknown'} "
                f"and target {target_type or target_kind}."
            )
        expected = {
            "supports": {"source": ["assertion", "episode"], "target": ["assertion"]},
            "contradicts": {"source": ["assertion", "episode"], "target": ["assertion"]},
            "tagged_with": {"source": ["assertion", "tag", "episode"], "target": ["tag"]},
            "sourced_from": {"source": ["assertion", "tag", "episode"], "target": ["event_slot"]},
            "supersedes": {"source": ["assertion", "tag", "episode"], "target": ["assertion", "tag", "episode"]},
        }.get(relation)
        errors.append(_error(
            "relation_contract",
            path,
            message,
            {
                "relation_type": relation,
                "expected": expected,
                "actual": {
                    "source": source_type or "unknown",
                    "target": target_type or target_kind,
                },
            },
        ))
    return {
        "source_object_id": source_id,
        "target_kind": target_kind,
        "target_object_id": target_id,
        "slot_seq_start": start,
        "slot_seq_end": end,
        "relation_type": relation,
    }


def _has_relation_cycle(connection, relation_type, pending_links):
    graph = {}
    for row in connection.execute(
        """SELECT source_object_id, target_object_id FROM live_memory_links
           WHERE relation_type = ? AND target_kind = 'memory_object' AND link_state = 'active'""",
        (relation_type,),
    ):
        graph.setdefault(row["source_object_id"], set()).add(row["target_object_id"])
    for link in pending_links:
        if link["relation_type"] == relation_type and link["target_kind"] == "memory_object":
            graph.setdefault(link["source_object_id"], set()).add(link["target_object_id"])
    visiting = set()
    visited = set()

    def visit(node):
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cyclic = any(visit(child) for child in graph.get(node, ()))
        visiting.remove(node)
        visited.add(node)
        return cyclic

    return any(visit(node) for node in graph)


def _validate_against_connection(plan, connection, root=None):
    errors = []
    if not isinstance(plan, dict):
        return {"valid": False, "errors": [_error("invalid_plan", "plan", "plan must be an object.")]}
    objects = plan.get("objects") or []
    links = plan.get("links") or []
    if not isinstance(objects, list) or not isinstance(links, list) or not objects and not links:
        errors.append(_error("empty_plan", "plan", "plan must contain at least one object or link."))
    normalized_objects = []
    objects_by_id = {}
    for index, obj in enumerate(objects if isinstance(objects, list) else []):
        normalized = _validate_object(obj, index, errors, connection)
        if normalized:
            if normalized["object_id"] in objects_by_id:
                errors.append(_error("duplicate_object_id", f"objects[{index}].object_id", "object_id appears more than once."))
            else:
                objects_by_id[normalized["object_id"]] = normalized
                normalized_objects.append(normalized)
    normalized_links = []
    for index, link in enumerate(links if isinstance(links, list) else []):
        normalized = _validate_link(link, index, connection, objects_by_id, root, errors)
        if normalized:
            normalized_links.append(normalized)
    for link in normalized_links:
        if link["target_kind"] == "memory_object":
            source = link["source_object_id"]
            existing = connection.execute(
                """SELECT 1 FROM live_memory_links
                   WHERE source_object_id = ? AND target_kind = ? AND target_object_id = ?
                     AND relation_type = ? AND link_state = 'active'""",
                (source, link["target_kind"], link["target_object_id"], link["relation_type"]),
            ).fetchone()
        else:
            existing = connection.execute(
                """SELECT 1 FROM live_memory_links
                   WHERE source_object_id = ? AND target_kind = ? AND slot_seq_start = ?
                     AND slot_seq_end = ? AND relation_type = ? AND link_state = 'active'""",
                (link["source_object_id"], link["target_kind"], link["slot_seq_start"], link["slot_seq_end"], link["relation_type"]),
            ).fetchone()
        if existing:
            errors.append(_error("duplicate_active_link", "links", "the same active source-target-relation already exists."))
    for relation_type in ("supersedes",):
        if _has_relation_cycle(connection, relation_type, normalized_links):
            errors.append(_error("relation_cycle", "links", f"active {relation_type} links must be acyclic."))
    normalized = {"objects": normalized_objects, "links": normalized_links}
    return {"valid": not errors, "errors": errors, "normalized_plan": normalized}


def validate_write_plan(plan, root=None):
    connection = _connect(root, read_only=True)
    if connection is None:
        connection = ensure_ltm_store(root)
    try:
        return _validate_against_connection(plan, connection, root)
    finally:
        connection.close()


def _plan_hash(normalized_plan):
    return hashlib.sha256(_canonical_json(normalized_plan).encode("utf-8")).hexdigest()


def _operation(connection, key):
    return connection.execute("SELECT * FROM ltm_operations WHERE idempotency_key = ?", (key,)).fetchone()


def get_idempotent_operation(idempotency_key, operation_type=None, root=None):
    """Read a previously recorded idempotent operation without changing the store."""
    connection = _connect(root, read_only=True)
    if connection is None:
        return None
    try:
        row = _operation(connection, idempotency_key)
        if row is None or (operation_type and row["operation_type"] != operation_type):
            return None
        try:
            result = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(result, dict):
            return None
        return {
            "idempotency_key": row["idempotency_key"],
            "operation_type": row["operation_type"],
            "request_hash": row["request_hash"],
            "created_at": row["created_at"],
            "result": result,
        }
    finally:
        connection.close()


def _strength(successful_use_count, gain_link_count, loss_link_count, neutral_link_count):
    import math

    p_u = 0.08 * math.log(1 + successful_use_count)
    p_g = 0.075 * math.log(1 + gain_link_count)
    p_n = 0.07 * math.log(1 + neutral_link_count)
    p_max = max(p_u, p_g, p_n)
    p_positive = p_max + 0.2 * (p_u + p_g + p_n - p_max)
    return max(0.0, min(1.0, 0.50 + p_positive - 0.36 * math.log(1 + loss_link_count)))


def _ensure_derived_row(connection, object_id, now=None):
    if connection.execute("SELECT 1 FROM memory_object_derived WHERE object_id=?", (object_id,)).fetchone():
        return
    now = now or _now()
    connection.execute(
        """INSERT OR IGNORE INTO memory_object_derived(
               object_id, successful_use_count, gain_link_count, loss_link_count,
               neutral_link_count, strength, strength_formula_version, updated_at, decay_origin_slot_seq)
           VALUES (?, 0, 0, 0, 0, 0.5, ?, ?, ?)""",
        (object_id, STRENGTH_FORMULA_VERSION, now, _connection_slot(connection)),
    )


def _recompute_derived(connection, object_ids, now=None, current_slot=None):
    now = now or _now()
    current_slot = _connection_slot(connection) if current_slot is None else current_slot
    for object_id in sorted(set(object_ids)):
        if connection.execute("SELECT 1 FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone() is None:
            continue
        _ensure_derived_row(connection, object_id, now)
        usage = connection.execute(
            "SELECT successful_use_count, last_effective_use_slot_seq, decay_origin_slot_seq FROM memory_object_derived WHERE object_id = ?", (object_id,)
        ).fetchone()
        use_count = usage["successful_use_count"]
        reference = usage["last_effective_use_slot_seq"]
        if reference is None:
            reference = usage["decay_origin_slot_seq"]
        protected = _system_owned_row(connection, object_id) is not None
        gain = connection.execute(
            """SELECT COUNT(*) FROM live_memory_links
               WHERE link_state = 'active' AND (
                   (relation_type = 'sourced_from' AND source_object_id = ?)
                   OR (relation_type = 'supports' AND target_kind = 'memory_object' AND target_object_id = ?)
               )""",
            (object_id, object_id),
        ).fetchone()[0]
        loss = connection.execute(
            """SELECT COUNT(*) FROM live_memory_links
               WHERE link_state = 'active' AND relation_type = 'supersedes'
                 AND target_kind = 'memory_object' AND target_object_id = ?""",
            (object_id,),
        ).fetchone()[0]
        neutral = connection.execute(
            """SELECT COUNT(*) FROM live_memory_links
               WHERE link_state = 'active' AND relation_type IN ('tagged_with', 'contradicts')
                 AND (source_object_id = ? OR (target_kind = 'memory_object' AND target_object_id = ?))""",
            (object_id, object_id),
        ).fetchone()[0]
        connection.execute(
            """UPDATE memory_object_derived
               SET gain_link_count = ?, loss_link_count = ?, neutral_link_count = ?, strength = ?,
                   strength_formula_version = ?, updated_at = ?
               WHERE object_id = ?""",
            (gain, loss, neutral, decayed_strength(_strength(use_count, gain, loss, neutral), current_slot, reference, protected=protected),
             STRENGTH_FORMULA_VERSION, now, object_id),
        )


def _all_object_ids(connection):
    return [row["object_id"] for row in connection.execute("SELECT object_id FROM live_memory_objects")]


def _stage_vector_objects(objects, root=None, timeout_seconds=None, phase_callback=None):
    from . import vector_index

    if not objects:
        return None
    kwargs = {"root": root, "timeout_seconds": timeout_seconds}
    if phase_callback is not None:
        kwargs["phase_callback"] = phase_callback
    return vector_index.stage_object_upserts(objects, **kwargs)


def _install_staged_vector(staged, root=None):
    from . import vector_index

    if staged:
        vector_index.install_staged_vector(staged, root=root)


def _discard_staged_vector(staged, root=None):
    from . import vector_index

    if staged:
        vector_index.discard_staged_vector(staged, root=root)


@serialized_write
def commit_write_plan(plan, idempotency_key, root=None):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return {"status": "rejected", "errors": [_error("idempotency_key", "idempotency_key", "idempotency_key must be non-empty.")]}
    connection = ensure_ltm_store(root)
    try:
        raw_request_hash = hashlib.sha256(_canonical_json(plan).encode("utf-8")).hexdigest()
        existing = _operation(connection, idempotency_key)
        if existing:
            result = json.loads(existing["result_json"])
            if existing["request_hash"] == raw_request_hash:
                result["status"] = "already_committed"
                return result
            return {"status": "rejected", "errors": [_error("idempotency_conflict", "idempotency_key", "key was used with a different plan.")]}
        initial = _validate_against_connection(plan, connection, root)
        if not initial["valid"]:
            return {"status": "rejected", "errors": initial["errors"]}
        try:
            staged_vector = _stage_vector_objects(initial["normalized_plan"]["objects"], root)
        except Exception as error:
            return {"status": "rejected", "errors": [_error("vector_sync", "vector_index", str(error))]}
        request_hash = raw_request_hash
        connection.execute("BEGIN IMMEDIATE")
        verified = _validate_against_connection(plan, connection, root)
        if not verified["valid"]:
            connection.rollback()
            return {"status": "rejected", "errors": verified["errors"]}
        now = _now()
        object_ids = []
        for obj in verified["normalized_plan"]["objects"]:
            object_ids.append(obj["object_id"])
            connection.execute(
                """INSERT INTO memory_objects(object_id, object_type, title, summary, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (obj["object_id"], obj["object_type"], obj["title"], obj["summary"] or None, now, now),
            )
            payload = obj["payload"]
            if payload:
                connection.execute(
                    """INSERT INTO memory_payloads(object_id, schema_version, body, structured_data)
                       VALUES (?, ?, ?, ?)""",
                    (obj["object_id"], payload["schema_version"], payload["body"],
                     _canonical_json(payload["structured_data"]) if payload["structured_data"] else None),
                )
            _sync_retrieval_index_object(connection, obj["object_id"])
            _ensure_derived_row(connection, obj["object_id"], now)
        link_ids = []
        for link in verified["normalized_plan"]["links"]:
            link_id = _new_id("lnk")
            link_ids.append(link_id)
            connection.execute(
                """INSERT INTO memory_links(
                       link_id, source_object_id, target_kind, target_object_id,
                       slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
                (link_id, link["source_object_id"], link["target_kind"], link["target_object_id"],
                 link["slot_seq_start"], link["slot_seq_end"], link["relation_type"], now, now),
            )
        affected_object_ids = set(object_ids)
        for link in verified["normalized_plan"]["links"]:
            affected_object_ids.add(link["source_object_id"])
            if link["target_kind"] == "memory_object":
                affected_object_ids.add(link["target_object_id"])
        _recompute_derived(connection, affected_object_ids, now)
        result = {
            "status": "committed",
            "commit_id": _new_id("commit"),
            "idempotency_key": idempotency_key,
            "committed_at": now,
            "object_ids": object_ids,
            "link_ids": link_ids,
            "errors": [],
        }
        connection.execute(
            "INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at) VALUES (?, 'write', ?, ?, ?)",
            (idempotency_key, request_hash, _canonical_json(result), now),
        )
        if staged_vector:
            journal_id = _new_id("vector_journal")
            connection.execute(
                """INSERT INTO ltm_vector_journal(
                       journal_id, state, staged_index_path, staged_metadata_path, created_at, updated_at)
                   VALUES (?, 'prepared', ?, ?, ?, ?)""",
                (journal_id, staged_vector["index_path"], staged_vector["metadata_path"], now, now),
            )
            _install_staged_vector(staged_vector, root)
            connection.execute(
                "UPDATE ltm_vector_journal SET state = 'completed', updated_at = ? WHERE journal_id = ?",
                (_now(), journal_id),
            )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        try:
            _discard_staged_vector(locals().get("staged_vector"), root)
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _maintenance_update_candidates(connection, updates, errors):
    candidates = []
    for index, update in enumerate(updates):
        field, object_id, value = update.get("field"), update.get("object_id"), update.get("value")
        path = f"updates[{index}]"
        if field not in {"title", "summary"}:
            errors.append(_error("update_field", path, "only title and summary may be updated.")); continue
        row = connection.execute("SELECT object_type, title, summary FROM live_memory_objects WHERE object_id=?", (object_id,)).fetchone()
        if row is None:
            errors.append(_error("not_found", path, "object was not found.")); continue
        if _system_owned_row(connection, object_id) is not None:
            errors.append(_error("protected_system_object", path, "system-owned root tags cannot be updated.")); continue
        if field == "title":
            _validate_title(value, errors, f"{path}.value")
        elif not (value is None or isinstance(value, str)):
            errors.append(_error("invalid_summary", f"{path}.value", "summary must be a string or null."))
        payload = connection.execute("SELECT schema_version, body, structured_data FROM memory_payloads WHERE object_id=?", (object_id,)).fetchone()
        candidate = {"object_type": row["object_type"], "summary": value if field == "summary" else row["summary"], "payload": None}
        if payload:
            candidate["payload"] = {"schema_version": payload["schema_version"], "body": payload["body"], "structured_data": json.loads(payload["structured_data"]) if payload["structured_data"] else None}
        _validate_payload(candidate, errors, path)
        if row[field] == value:
            errors.append(_error("unchanged", path, f"new {field} must differ from current value.")); continue
        candidates.append({"field": field, "object_id": object_id, "value": value, "row": row, "payload": candidate["payload"]})
    return candidates


@serialized_write
def commit_maintenance_plan(compiled_plan, idempotency_key, root=None, phase_callback=None):
    """Commit an already deterministic Maintainer plan as one Store transaction."""
    if not isinstance(compiled_plan, dict) or not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return {"status": "rejected", "errors": [_error("invalid_plan", "plan", "compiled plan and idempotency key are required.")]}
    creates = {"objects": compiled_plan.get("objects") or [], "links": compiled_plan.get("links") or []}
    deactivate_ids = list(compiled_plan.get("deactivate_link_ids") or [])
    updates = list(compiled_plan.get("updates") or [])
    def notify(phase, status, detail=None):
        if callable(phase_callback):
            try:
                phase_callback(phase, status, detail or {})
            except Exception:
                pass

    notify("ensure_ltm_store", "started")
    try:
        connection = ensure_ltm_store(root)
    except Exception as error:
        notify("ensure_ltm_store", "failed", {"error": str(error)})
        raise
    notify("ensure_ltm_store", "completed")
    staged_vector = None
    sqlite_started = False
    try:
        request_hash = hashlib.sha256(_canonical_json(compiled_plan).encode("utf-8")).hexdigest()
        existing = _operation(connection, idempotency_key)
        if existing:
            result = json.loads(existing["result_json"])
            if existing["request_hash"] == request_hash:
                result["status"] = "already_committed"; return result
            return {"status": "rejected", "errors": [_error("idempotency_conflict", "idempotency_key", "key was used with a different plan.")]}
        initial = _validate_against_connection(creates, connection, root) if (creates["objects"] or creates["links"]) else {"valid": True, "errors": [], "normalized_plan": creates}
        errors = list(initial["errors"])
        if len(deactivate_ids) != len(set(deactivate_ids)):
            errors.append(_error("duplicate_deactivate", "deactivate_link_ids", "a link may only be deactivated once."))
        deactivations = []
        for link_id in deactivate_ids:
            row = connection.execute("SELECT * FROM live_memory_links WHERE link_id=?", (link_id,)).fetchone()
            if row is None or row["link_state"] != "active": errors.append(_error("link_not_active", "deactivate_link_ids", f"link is not active: {link_id}"))
            else: deactivations.append(row)
        update_candidates = _maintenance_update_candidates(connection, updates, errors)
        if errors: return {"status": "rejected", "errors": errors}
        vector_objects = [{"object_id": obj["object_id"], "title": obj["title"], "summary": obj["summary"], "payload": obj["payload"]} for obj in initial["normalized_plan"]["objects"]]
        for update in update_candidates:
            vector_objects.append({"object_id": update["object_id"], "title": update["value"] if update["field"] == "title" else update["row"]["title"], "summary": update["value"] if update["field"] == "summary" else update["row"]["summary"], "payload": update["payload"]})
        notify("stage_vector_embedding", "started", {"object_count": len(vector_objects)})
        try:
            staged_vector = _stage_vector_objects(
                vector_objects,
                root,
                timeout_seconds=MAINTENANCE_VECTOR_SYNC_TIMEOUT_SECONDS,
                phase_callback=notify if phase_callback is not None else None,
            )
        except EmbeddingServiceTimeout as error:
            notify("stage_vector_embedding", "failed", {"code": "vector_sync_timeout", "error": str(error)})
            return {"status": "rejected", "errors": [_error("vector_sync_timeout", "vector_index", str(error))]}
        except Exception as error:
            notify("stage_vector_embedding", "failed", {"code": "vector_sync", "error": str(error)})
            return {"status": "rejected", "errors": [_error("vector_sync", "vector_index", str(error))]}
        notify("stage_vector_embedding", "completed", {"object_count": len(vector_objects)})
        notify("sqlite_commit", "started")
        sqlite_started = True
        connection.execute("BEGIN IMMEDIATE")
        verified = _validate_against_connection(creates, connection, root) if (creates["objects"] or creates["links"]) else {"valid": True, "errors": [], "normalized_plan": creates}
        if not verified["valid"]:
            connection.rollback()
            notify("sqlite_commit", "failed", {"status": "rejected", "errors": verified["errors"]})
            return {"status": "rejected", "errors": verified["errors"]}
        now, affected, object_ids, link_ids = _now(), set(), [], []
        for obj in verified["normalized_plan"]["objects"]:
            object_ids.append(obj["object_id"]); affected.add(obj["object_id"])
            connection.execute("INSERT INTO memory_objects(object_id, object_type, title, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (obj["object_id"], obj["object_type"], obj["title"], obj["summary"] or None, now, now))
            if obj["payload"]:
                payload = obj["payload"]; connection.execute("INSERT INTO memory_payloads(object_id, schema_version, body, structured_data) VALUES (?, ?, ?, ?)", (obj["object_id"], payload["schema_version"], payload["body"], _canonical_json(payload["structured_data"]) if payload["structured_data"] else None))
            _sync_retrieval_index_object(connection, obj["object_id"]); _ensure_derived_row(connection, obj["object_id"], now)
        for link in verified["normalized_plan"]["links"]:
            link_id = _new_id("lnk"); link_ids.append(link_id); affected.add(link["source_object_id"])
            if link["target_kind"] == "memory_object": affected.add(link["target_object_id"])
            connection.execute("INSERT INTO memory_links(link_id, source_object_id, target_kind, target_object_id, slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)", (link_id, link["source_object_id"], link["target_kind"], link["target_object_id"], link["slot_seq_start"], link["slot_seq_end"], link["relation_type"], now, now))
        for row in deactivations:
            connection.execute("UPDATE memory_links SET link_state='inactive', updated_at=? WHERE link_id=?", (now, row["link_id"])); affected.add(row["source_object_id"])
            if row["target_kind"] == "memory_object": affected.add(row["target_object_id"])
        updated_ids = []
        for update in update_candidates:
            connection.execute(f"UPDATE memory_objects SET {update['field']}=?, updated_at=? WHERE object_id=?", (update["value"], now, update["object_id"])); _sync_retrieval_index_object(connection, update["object_id"]); affected.add(update["object_id"]); updated_ids.append(update["object_id"])
        _recompute_derived(connection, affected, now)
        counter_hints = dict(compiled_plan.get("report_counter_hints") or {})
        counter_hints.update({
            "updated_title_count": sum(1 for update in update_candidates if update["field"] == "title"),
            "updated_summary_count": sum(1 for update in update_candidates if update["field"] == "summary"),
            "supersedes_count": sum(1 for link in verified["normalized_plan"]["links"] if link["relation_type"] == "supersedes"),
            "contradiction_count": sum(1 for link in verified["normalized_plan"]["links"] if link["relation_type"] == "contradicts"),
        })
        result = {"status": "committed", "commit_id": _new_id("commit"), "plan_id": compiled_plan.get("plan_id"), "idempotency_key": idempotency_key, "object_ids": object_ids, "link_ids": link_ids, "deactivated_link_ids": deactivate_ids, "updated_object_ids": sorted(set(updated_ids)), "temp_id_map": compiled_plan.get("temp_id_map") or {}, "report_counter_hints": counter_hints, "errors": []}
        connection.execute("INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at) VALUES (?, 'maintenance_plan', ?, ?, ?)", (idempotency_key, request_hash, _canonical_json(result), now))
        if staged_vector:
            journal_id = _new_id("vector_journal"); connection.execute("INSERT INTO ltm_vector_journal(journal_id, state, staged_index_path, staged_metadata_path, created_at, updated_at) VALUES (?, 'prepared', ?, ?, ?, ?)", (journal_id, staged_vector["index_path"], staged_vector["metadata_path"], now, now))
            notify("install_vector_index", "started")
            try:
                _install_staged_vector(staged_vector, root)
            except Exception as error:
                notify("install_vector_index", "failed", {"error": str(error)})
                raise
            notify("install_vector_index", "completed")
            connection.execute("UPDATE ltm_vector_journal SET state='completed', updated_at=? WHERE journal_id=?", (_now(), journal_id))
        connection.commit(); notify("sqlite_commit", "completed"); return result
    except Exception as error:
        if sqlite_started:
            notify("sqlite_commit", "failed", {"error": str(error)})
        connection.rollback()
        try: _discard_staged_vector(staged_vector, root)
        except Exception: pass
        raise
    finally: connection.close()


def _object_row(row, include_payload=False, connection=None):
    result = dict(row)
    if connection is not None:
        derived = connection.execute(
            """SELECT successful_use_count, gain_link_count, loss_link_count, neutral_link_count,
                      strength, strength_formula_version, updated_at,
                      last_effective_use_slot_seq, decay_origin_slot_seq
               FROM memory_object_derived WHERE object_id = ?""",
            (row["object_id"],),
        ).fetchone()
        if derived:
            result["derived"] = dict(derived)
    if include_payload and connection is not None:
        payload = connection.execute("SELECT * FROM memory_payloads WHERE object_id = ?", (row["object_id"],)).fetchone()
        if payload:
            result["payload"] = {
                "schema_version": payload["schema_version"],
                "body": payload["body"],
                "structured_data": json.loads(payload["structured_data"]) if payload["structured_data"] else None,
            }
        else:
            result["payload"] = None
    return result


def get_object(object_id, include_payload=False, root=None):
    connection = _connect(root, read_only=True)
    if connection is None:
        return None
    try:
        row = connection.execute("SELECT * FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone()
        return _object_row(row, include_payload, connection) if row else None
    finally:
        connection.close()


def get_objects(object_ids, include_payload=False, root=None):
    """Read multiple live MemoryObjects with one connection and one query."""
    object_ids = list(dict.fromkeys(object_ids or []))
    if not object_ids:
        return {}
    connection = _connect(root, read_only=True)
    if connection is None:
        return {}
    try:
        placeholders = ",".join("?" for _ in object_ids)
        derived_columns = (
            "d.object_id AS derived_object_id, "
            "d.successful_use_count AS derived_successful_use_count, "
            "d.gain_link_count AS derived_gain_link_count, "
            "d.loss_link_count AS derived_loss_link_count, "
            "d.neutral_link_count AS derived_neutral_link_count, "
            "d.strength AS derived_strength, "
            "d.strength_formula_version AS derived_strength_formula_version, "
            "d.updated_at AS derived_updated_at, "
            "d.last_effective_use_slot_seq AS derived_last_effective_use_slot_seq, "
            "d.decay_origin_slot_seq AS derived_decay_origin_slot_seq"
        )
        payload_columns = ""
        if include_payload:
            payload_columns = ", p.schema_version AS payload_schema_version, p.body AS payload_body, p.structured_data AS payload_structured_data"
        rows = connection.execute(
            f"""
            SELECT o.*, {derived_columns}{payload_columns}
            FROM live_memory_objects AS o
            LEFT JOIN memory_object_derived AS d ON d.object_id = o.object_id
            {"LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id" if include_payload else ""}
            WHERE o.object_id IN ({placeholders})
            """,
            object_ids,
        ).fetchall()
        result = {}
        for row in rows:
            item = {key: row[key] for key in row.keys() if not key.startswith("derived_") and not key.startswith("payload_")}
            if row["derived_object_id"] is not None:
                item["derived"] = {
                    "successful_use_count": row["derived_successful_use_count"],
                    "gain_link_count": row["derived_gain_link_count"],
                    "loss_link_count": row["derived_loss_link_count"],
                    "neutral_link_count": row["derived_neutral_link_count"],
                    "strength": row["derived_strength"],
                    "strength_formula_version": row["derived_strength_formula_version"],
                    "updated_at": row["derived_updated_at"],
                    "last_effective_use_slot_seq": row["derived_last_effective_use_slot_seq"],
                    "decay_origin_slot_seq": row["derived_decay_origin_slot_seq"],
                }
            if include_payload:
                item["payload"] = (
                    {
                        "schema_version": row["payload_schema_version"],
                        "body": row["payload_body"],
                        "structured_data": json.loads(row["payload_structured_data"]) if row["payload_structured_data"] else None,
                    }
                    if row["payload_schema_version"] is not None else None
                )
            result[item["object_id"]] = item
        return {object_id: result[object_id] for object_id in object_ids if object_id in result}
    finally:
        connection.close()


def list_objects(object_type, include_payload=False, root=None):
    connection = _connect(root, read_only=True)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM live_memory_objects WHERE object_type = ? ORDER BY created_at, object_id",
            (object_type,),
        ).fetchall()
        return [_object_row(row, include_payload, connection) for row in rows]
    finally:
        connection.close()


def get_links(object_id, direction, relation_type=None, link_state="active", root=None):
    if direction not in {"outgoing", "incoming", "both"}:
        raise ValueError("direction must be outgoing, incoming, or both")
    connection = _connect(root, read_only=True)
    if connection is None:
        return []
    try:
        clauses = []
        params = []
        if direction in {"outgoing", "both"}:
            clauses.append("source_object_id = ?")
            params.append(object_id)
        if direction in {"incoming", "both"}:
            clauses.append("target_kind = 'memory_object' AND target_object_id = ?")
            params.append(object_id)
        if link_state not in LINK_STATES:
            raise ValueError("link_state must be active or inactive")
        query = "SELECT * FROM live_memory_links WHERE link_state = ? AND (" + " OR ".join(clauses) + ")"
        params.insert(0, link_state)
        if relation_type is not None:
            query += " AND relation_type = ?"
            params.append(relation_type)
        query += " ORDER BY created_at, link_id"
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


def get_memory_link(link_id, root=None):
    connection = _connect(root, read_only=True)
    if connection is None:
        return None
    try:
        row = connection.execute("SELECT * FROM live_memory_links WHERE link_id = ?", (link_id,)).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


MAINTENANCE_PRESSURE_THRESHOLD = 200
MAINTENANCE_REPORT_COUNTER_KEYS = (
    "plan_commit_count",
    "created_object_count",
    "created_link_count",
    "deactivated_link_count",
    "updated_title_count",
    "updated_summary_count",
    "supersedes_count",
    "contradiction_count",
)


def _empty_maintenance_report_counters():
    return {key: 0 for key in MAINTENANCE_REPORT_COUNTER_KEYS}


def aggregate_maintenance_run_counters(run_id, root=None):
    """Rebuild committed maintenance counters from durable plan operation rows."""
    counters = _empty_maintenance_report_counters()
    if not isinstance(run_id, str) or not run_id:
        return counters
    connection = _connect(root, read_only=True)
    if connection is None:
        return counters
    link_ids = []
    try:
        prefix = f"maintenance:{run_id}:plan:"
        rows = connection.execute(
            """SELECT result_json FROM ltm_operations
               WHERE operation_type = 'maintenance_plan'
                 AND idempotency_key LIKE ?
               ORDER BY created_at, idempotency_key""",
            (prefix + "%",),
        ).fetchall()
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if result.get("status") not in {"committed", "already_committed"}:
                continue
            counters["plan_commit_count"] += 1
            counters["created_object_count"] += len(result.get("object_ids") or [])
            counters["created_link_count"] += len(result.get("link_ids") or [])
            counters["deactivated_link_count"] += len(result.get("deactivated_link_ids") or [])
            hints = result.get("report_counter_hints") or {}
            if isinstance(hints, dict):
                counters["updated_title_count"] += int(hints.get("updated_title_count") or hints.get("update_title") or 0)
                counters["updated_summary_count"] += int(hints.get("updated_summary_count") or hints.get("update_summary") or 0)
                counters["supersedes_count"] += int(hints.get("supersedes_count") or 0)
                counters["contradiction_count"] += int(hints.get("contradiction_count") or hints.get("mark_contradiction") or 0)
            link_ids.extend(result.get("link_ids") or [])
        if link_ids:
            placeholders = ",".join("?" for _ in link_ids)
            relation_counts = {
                row["relation_type"]: int(row["count"])
                for row in connection.execute(
                    f"SELECT relation_type, COUNT(*) AS count FROM memory_links WHERE link_id IN ({placeholders}) GROUP BY relation_type",
                    link_ids,
                )
            }
            counters["supersedes_count"] = max(counters["supersedes_count"], relation_counts.get("supersedes", 0))
            counters["contradiction_count"] = max(counters["contradiction_count"], relation_counts.get("contradicts", 0))
        return counters
    finally:
        connection.close()


def _latest_event_slot_seq(root=None):
    """Return -1 when the Event Stream has not been initialized."""
    path = event_stream.event_stream_db_path(root)
    if not path.exists():
        return -1
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT MAX(slot_seq) FROM slots").fetchone()
        return int(row[0]) if row and row[0] is not None else -1
    finally:
        connection.close()


def _maintenance_state_dict(row, root=None):
    result = dict(row)
    latest = _latest_event_slot_seq(root)
    cursor = int(result["maintenance_coverage_cursor"])
    try:


        from . import formation
        frontier = formation.maintainable_slot_frontier(root=root)
    except Exception:


        frontier = -1
    pending = max(0, frontier - cursor)
    result.update({
        "latest_event_stream_slot_seq": latest,
        "maintainable_frontier": frontier,
        "coverage_target_slot_seq": max(cursor, frontier),
        "unmaintained_slot_count": pending,
        "maintenance_pressure": min(1.0, pending / MAINTENANCE_PRESSURE_THRESHOLD),
    })
    return result


def get_maintenance_runtime_state(root=None):
    """Read the singleton maintenance state without initializing storage."""
    connection = _connect(root, read_only=True)
    if connection is None or not _table_exists(connection, "maintenance_runtime_state"):
        if connection is not None:
            connection.close()
        return _maintenance_state_dict({
            "maintenance_coverage_cursor": -1, "active_run_id": None,
            "run_status": "idle", "run_started_at": None,
            "run_start_cursor": None,
            "run_owner_pid": None, "run_heartbeat_at": None,
            "last_checkpoint_slot_seq": None, "next_plan_index": 0,
            "last_committed_plan_index": None, "last_failure_slot_seq": None,
            "last_failure_reason": None, "last_failure_at": None, "updated_at": None,
        }, root)
    try:
        row = connection.execute(
            "SELECT * FROM maintenance_runtime_state WHERE state_id = 1"
        ).fetchone()
        if row is None:
            return _maintenance_state_dict({
                "maintenance_coverage_cursor": -1, "active_run_id": None,
                "run_status": "idle", "run_started_at": None,
                "run_start_cursor": None,
                "run_owner_pid": None, "run_heartbeat_at": None,
                "last_checkpoint_slot_seq": None, "next_plan_index": 0,
                "last_committed_plan_index": None, "last_failure_slot_seq": None,
                "last_failure_reason": None, "last_failure_at": None, "updated_at": None,
            }, root)
        return _maintenance_state_dict(row, root)
    finally:
        connection.close()


def _require_maintenance_run(connection, run_id):
    row = connection.execute("SELECT * FROM maintenance_runtime_state WHERE state_id = 1").fetchone()
    if row["active_run_id"] != run_id or row["run_status"] != "running":
        raise ValueError("maintenance run is not running")
    return row


def _process_is_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def start_or_resume_maintenance_run(root=None):
    connection = ensure_ltm_store(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM maintenance_runtime_state WHERE state_id = 1").fetchone()
        now = _now()
        interrupted_run_id = None
        stale_running = row["run_status"] == "running" and not _process_is_alive(row["run_owner_pid"])
        if row["run_status"] == "running" and not stale_running:
            run_id = row["active_run_id"]
            connection.execute("UPDATE maintenance_runtime_state SET run_status = 'running', run_owner_pid=?, run_heartbeat_at=?, updated_at = ? WHERE state_id = 1", (os.getpid(), now, now))
            status = "running"
        elif row["run_status"] == "failed":
            run_id = row["active_run_id"]
            connection.execute("UPDATE maintenance_runtime_state SET run_status = 'running', run_owner_pid=?, run_heartbeat_at=?, updated_at = ? WHERE state_id = 1", (os.getpid(), now, now))
            status = "resumed"
        else:
            interrupted_run_id = row["active_run_id"] if stale_running else None
            run_id = _new_id("maintenance_run")
            connection.execute(
                """UPDATE maintenance_runtime_state SET active_run_id=?, run_status='running',
                   run_started_at=?, last_checkpoint_slot_seq=maintenance_coverage_cursor,
                   run_start_cursor=maintenance_coverage_cursor,
                   run_owner_pid=?, run_heartbeat_at=?,
                   next_plan_index=?, last_committed_plan_index=?,
                   last_failure_slot_seq=NULL, last_failure_reason=NULL, last_failure_at=NULL,
                   updated_at=? WHERE state_id=1""",
                (run_id, now, os.getpid(), now, int(row["next_plan_index"] or 0), row["last_committed_plan_index"], now),
            )
            status = "started_after_interruption" if interrupted_run_id else "started"
        connection.commit()
        state = get_maintenance_runtime_state(root)
        result = {"status": status, "run_id": run_id, "state": state}
        if interrupted_run_id:
            result["interrupted_run_id"] = interrupted_run_id
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@serialized_write
def heartbeat_maintenance_run(run_id, root=None):
    connection = _ensure_ltm_store(root, recover_indexes=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_maintenance_run(connection, run_id)
        now = _now()
        connection.execute(
            "UPDATE maintenance_runtime_state SET run_owner_pid=?, run_heartbeat_at=?, updated_at=? WHERE state_id=1",
            (os.getpid(), now, now),
        )
        connection.commit()
        return {"status": "alive", "run_id": run_id, "run_heartbeat_at": now}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def checkpoint_maintenance_run(run_id, next_plan_index, last_committed_plan_index=None, last_checkpoint_slot_seq=None, root=None):
    connection = ensure_ltm_store(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_maintenance_run(connection, run_id)
        next_plan_index = int(next_plan_index)
        committed = row["last_committed_plan_index"] if last_committed_plan_index is None else int(last_committed_plan_index)
        checkpoint = row["last_checkpoint_slot_seq"] if last_checkpoint_slot_seq is None else int(last_checkpoint_slot_seq)
        if next_plan_index < int(row["next_plan_index"]) or (committed is not None and committed < (row["last_committed_plan_index"] or -1)) or checkpoint < int(row["maintenance_coverage_cursor"]):
            raise ValueError("maintenance checkpoint must be monotonic")
        if checkpoint > _latest_event_slot_seq(root):
            raise ValueError("maintenance checkpoint exceeds latest Event Stream slot")
        connection.execute("UPDATE maintenance_runtime_state SET next_plan_index=?, last_committed_plan_index=?, last_checkpoint_slot_seq=?, run_heartbeat_at=?, updated_at=? WHERE state_id=1", (next_plan_index, committed, checkpoint, _now(), _now()))
        connection.commit()
        return {"status": "checkpointed", "run_id": run_id, "state": get_maintenance_runtime_state(root)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def advance_maintenance_coverage(run_id, end_slot_seq, root=None):
    connection = ensure_ltm_store(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_maintenance_run(connection, run_id)
        end_slot_seq = int(end_slot_seq)
        current = int(row["maintenance_coverage_cursor"])
        latest = _latest_event_slot_seq(root)
        if end_slot_seq < current or end_slot_seq > latest:
            raise ValueError("maintenance coverage must advance continuously within the Event Stream")
        connection.execute("UPDATE maintenance_runtime_state SET maintenance_coverage_cursor=?, last_checkpoint_slot_seq=?, run_heartbeat_at=?, updated_at=? WHERE state_id=1", (end_slot_seq, end_slot_seq, _now(), _now()))
        connection.commit()
        return {"status": "advanced", "run_id": run_id, "maintenance_coverage_cursor": end_slot_seq}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@serialized_write
def record_maintenance_failure(run_id, slot_seq, reason, root=None):

    connection = _ensure_ltm_store(root, recover_indexes=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_maintenance_run(connection, run_id)
        slot_seq = int(slot_seq)
        if slot_seq > _latest_event_slot_seq(root):
            raise ValueError("failure slot exceeds latest Event Stream slot")
        connection.execute("UPDATE maintenance_runtime_state SET run_status='failed', last_failure_slot_seq=?, last_failure_reason=?, last_failure_at=?, run_heartbeat_at=?, updated_at=? WHERE state_id=1", (slot_seq, str(reason), _now(), _now(), _now()))
        connection.commit()
        return {"status": "failed", "run_id": run_id, "slot_seq": slot_seq}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_maintenance_run(run_id, report, root=None):
    report = dict(report or {})
    connection = ensure_ltm_store(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _require_maintenance_run(connection, run_id)
        now = _now()
        start_cursor, end_cursor = int(row["run_start_cursor"]), int(row["maintenance_coverage_cursor"])
        connection.execute(
            """INSERT INTO maintenance_reports(maintenance_run_id, started_at, finished_at, start_cursor, end_cursor, coverage_status, processed_slot_count, plan_commit_count, created_object_count, created_link_count, deactivated_link_count, updated_title_count, updated_summary_count, supersedes_count, contradiction_count, failure_slot_seq, failure_reason, final_check_status, semantic_digest_status, semantic_digest_id, semantic_digest_token_count, maintainable_frontier, coverage_target_slot_seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, row["run_started_at"] or now, now, int(report.get("start_cursor", start_cursor)), int(report.get("end_cursor", end_cursor)), str(report.get("coverage_status", "completed")), int(report.get("processed_slot_count", 0)), *[int(report.get(k, 0)) for k in ("plan_commit_count", "created_object_count", "created_link_count", "deactivated_link_count", "updated_title_count", "updated_summary_count", "supersedes_count", "contradiction_count")], report.get("failure_slot_seq"), report.get("failure_reason"), str(report.get("final_check_status", "passed")), str(report.get("semantic_digest_status", "missing")), report.get("semantic_digest_id"), int(report.get("semantic_digest_token_count", 0)), report.get("maintainable_frontier"), report.get("coverage_target_slot_seq")),
        )
        connection.execute("DELETE FROM maintenance_reports WHERE maintenance_run_id IN (SELECT maintenance_run_id FROM maintenance_reports ORDER BY finished_at DESC LIMIT -1 OFFSET 50)")
        connection.execute("UPDATE maintenance_runtime_state SET active_run_id=NULL, run_status='idle', run_started_at=NULL, run_start_cursor=NULL, run_owner_pid=NULL, run_heartbeat_at=NULL, last_checkpoint_slot_seq=NULL, next_plan_index=0, last_committed_plan_index=NULL, last_failure_slot_seq=NULL, last_failure_reason=NULL, last_failure_at=NULL, updated_at=? WHERE state_id=1", (now,))
        connection.commit()
        return {"status": "completed", "run_id": run_id, "report": report}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _operation_result_or_conflict(connection, idempotency_key, request_hash, already_status):
    existing = _operation(connection, idempotency_key)
    if existing is None:
        return None
    result = json.loads(existing["result_json"])
    if existing["request_hash"] == request_hash:
        result["status"] = already_status
        return result
    return {"status": "rejected", "errors": [_error("idempotency_conflict", "idempotency_key", "key was used with a different operation.")]}


@serialized_write
def deactivate_memory_link(link_id, idempotency_key, root=None):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return {"status": "rejected", "errors": [_error("idempotency_key", "idempotency_key", "idempotency_key must be non-empty.")]}
    connection = ensure_ltm_store(root)
    try:
        request_hash = hashlib.sha256(f"deactivate_link:{link_id}".encode()).hexdigest()
        existing = _operation_result_or_conflict(connection, idempotency_key, request_hash, "already_inactive")
        if existing is not None:
            return existing
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT source_object_id, target_kind, target_object_id, link_state FROM live_memory_links WHERE link_id = ?",
            (link_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("not_found", link_id, "link was not found.")]}
        if row["link_state"] == "inactive":
            result = {"status": "already_inactive", "idempotency_key": idempotency_key, "errors": []}
        else:
            now = _now()
            connection.execute("UPDATE memory_links SET link_state = 'inactive', updated_at = ? WHERE link_id = ?", (now, link_id))
            affected = {row["source_object_id"]}
            if row["target_kind"] == "memory_object":
                affected.add(row["target_object_id"])
            _recompute_derived(connection, affected, now)
            result = {"status": "inactive", "idempotency_key": idempotency_key, "link_id": link_id, "inactive_at": now, "errors": []}
        connection.execute(
            "INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at) VALUES (?, 'deactivate_link', ?, ?, ?)",
            (idempotency_key, request_hash, _canonical_json(result), _now()),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _update_object_field(object_id, field, value, idempotency_key, root=None):
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return {"status": "rejected", "errors": [_error("idempotency_key", "idempotency_key", "idempotency_key must be non-empty.")]}
    if field == "title":
        errors = []
        _validate_title(value, errors, "new_title")
        if errors:
            return {"status": "rejected", "errors": errors}
    elif not (value is None or isinstance(value, str)):
        return {"status": "rejected", "errors": [_error("invalid_summary", "new_summary", "summary must be a string or null.")]}
    connection = ensure_ltm_store(root)
    try:
        request_hash = hashlib.sha256(_canonical_json({"field": field, "object_id": object_id, "value": value}).encode()).hexdigest()
        existing = _operation_result_or_conflict(connection, idempotency_key, request_hash, "already_committed")
        if existing is not None:
            return existing
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT object_type, title, summary FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone()
        if row is None:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("not_found", object_id, "object was not found.")]}
        if _system_owned_row(connection, object_id) is not None:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("protected_system_object", object_id, "system-owned root tags cannot be updated.")]}
        payload = connection.execute("SELECT schema_version, body, structured_data FROM memory_payloads WHERE object_id = ?", (object_id,)).fetchone()
        candidate = {"object_type": row["object_type"], "summary": value if field == "summary" else row["summary"], "payload": None}
        if payload:
            candidate["payload"] = {"schema_version": payload["schema_version"], "body": payload["body"], "structured_data": json.loads(payload["structured_data"]) if payload["structured_data"] else None}
        errors = []
        _validate_payload(candidate, errors, "object")
        if errors:
            connection.rollback()
            return {"status": "rejected", "errors": errors}
        if row[field] == value:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("unchanged", field, f"new {field} must differ from current value.")]}
        vector_object = {
            "object_id": object_id,
            "title": value if field == "title" else row["title"],
            "summary": value if field == "summary" else row["summary"],
            "payload": candidate["payload"],
        }
        try:
            staged_vector = _stage_vector_objects([vector_object], root)
        except Exception as error:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("vector_sync", "vector_index", str(error))]}
        now = _now()
        connection.execute(f"UPDATE memory_objects SET {field} = ?, updated_at = ? WHERE object_id = ?", (value, now, object_id))
        _sync_retrieval_index_object(connection, object_id)
        _recompute_derived(connection, [object_id], now)
        result = {"status": "committed", "object_id": object_id, "field": field, "updated_at": now, "errors": []}
        connection.execute(
            "INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, f"update_{field}", request_hash, _canonical_json(result), now),
        )
        if staged_vector:
            journal_id = _new_id("vector_journal")
            connection.execute(
                """INSERT INTO ltm_vector_journal(
                       journal_id, state, staged_index_path, staged_metadata_path, created_at, updated_at)
                   VALUES (?, 'prepared', ?, ?, ?, ?)""",
                (journal_id, staged_vector["index_path"], staged_vector["metadata_path"], now, now),
            )
            _install_staged_vector(staged_vector, root)
            connection.execute("UPDATE ltm_vector_journal SET state = 'completed', updated_at = ? WHERE journal_id = ?", (_now(), journal_id))
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        try:
            _discard_staged_vector(locals().get("staged_vector"), root)
        except Exception:
            pass
        raise
    finally:
        connection.close()


@serialized_write
def update_memory_object_title(object_id, new_title, idempotency_key, maintenance_run_id=None, intent_id=None, root=None):
    return _update_object_field(object_id, "title", new_title, idempotency_key, root)


@serialized_write
def update_memory_object_summary(object_id, new_summary, idempotency_key, maintenance_run_id=None, intent_id=None, root=None):
    return _update_object_field(object_id, "summary", new_summary, idempotency_key, root)


@serialized_write
def record_successful_uses(object_ids, idempotency_key, root=None):
    object_ids = sorted(set(object_ids or []))
    if not object_ids:
        return {"status": "rejected", "errors": [_error("object_ids", "object_ids", "at least one object_id is required.")]}
    connection = ensure_ltm_store(root)
    try:
        request_hash = hashlib.sha256(_canonical_json(object_ids).encode()).hexdigest()
        connection.execute("BEGIN IMMEDIATE")
        existing = _operation_result_or_conflict(connection, idempotency_key, request_hash, "already_committed")
        if existing is not None:
            return existing
        missing = [object_id for object_id in object_ids if connection.execute("SELECT 1 FROM live_memory_objects WHERE object_id = ?", (object_id,)).fetchone() is None]
        if missing:
            connection.rollback()
            return {"status": "rejected", "errors": [_error("not_found", "object_ids", f"objects not found: {', '.join(missing)}")]}
        now = _now()
        current_slot = _latest_event_slot_seq(root)
        for object_id in object_ids:
            _ensure_derived_row(connection, object_id, now)
            connection.execute("UPDATE memory_object_derived SET successful_use_count = successful_use_count + 1, last_effective_use_slot_seq=? WHERE object_id = ?", (current_slot, object_id))
        _recompute_derived(connection, object_ids, now, current_slot=current_slot)
        result = {"status": "committed", "object_ids": object_ids, "errors": []}
        connection.execute(
            "INSERT INTO ltm_operations(idempotency_key, operation_type, request_hash, result_json, created_at) VALUES (?, 'successful_use', ?, ?, ?)",
            (idempotency_key, request_hash, _canonical_json(result), now),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ltm_store_snapshot(root=None):
    snapshot = {
        "status": "not_initialized",
        "initialized": False,
        "db_path": str(ltm_db_path(root)),
        "object_count": 0,
        "payload_count": 0,
        "link_count": 0,
        "counts_by_object_type": {},
        "counts_by_link_state": {},
        "counts_by_relation": {},
        "bootstrap_complete": False,
        "system_owned_tag_count": 0,
        "system_owned_tags": [],
        "unanchored_object_count": 0,
        "db_schema_version": 0,
        "retrieval_index_initialized": False,
        "retrieval_index_count": 0,
    }
    if not ltm_db_path(root).exists():
        return snapshot
    connection = _connect(root, read_only=True)
    try:
        snapshot["status"] = "ok"
        snapshot["initialized"] = True
        snapshot["object_count"] = connection.execute("SELECT COUNT(*) FROM live_memory_objects").fetchone()[0]
        snapshot["payload_count"] = connection.execute("SELECT COUNT(*) FROM memory_payloads p JOIN live_memory_objects o ON o.object_id=p.object_id").fetchone()[0]
        snapshot["link_count"] = connection.execute("SELECT COUNT(*) FROM live_memory_links").fetchone()[0]
        snapshot["db_schema_version"] = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if _table_exists(connection, LTM_FTS_TABLE):
            snapshot["retrieval_index_initialized"] = True
            snapshot["retrieval_index_count"] = connection.execute(
                f"SELECT COUNT(*) FROM {LTM_FTS_TABLE}"
            ).fetchone()[0]
        snapshot["counts_by_object_type"] = {
            row["object_type"]: row["count"]
            for row in connection.execute("SELECT object_type, COUNT(*) AS count FROM live_memory_objects GROUP BY object_type")
        }
        snapshot["counts_by_link_state"] = {
            row["link_state"]: row["count"]
            for row in connection.execute("SELECT link_state, COUNT(*) AS count FROM live_memory_links GROUP BY link_state")
        }
        snapshot["counts_by_relation"] = {
            row["relation_type"]: row["count"]
            for row in connection.execute("SELECT relation_type, COUNT(*) AS count FROM live_memory_links GROUP BY relation_type")
        }
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_owned_objects'"
        ).fetchone()
        if table is not None:
            rows = connection.execute(
                """
                SELECT r.tag_key, r.object_id, r.protected_kind, o.title
                FROM system_owned_objects AS r
                JOIN live_memory_objects AS o ON o.object_id = r.object_id
                WHERE r.protected_kind = 'system_tag'
                ORDER BY r.tag_key
                """
            ).fetchall()
            snapshot["system_owned_tag_count"] = len(rows)
            snapshot["bootstrap_complete"] = len(rows) == 4
            snapshot["system_owned_tags"] = [
                {
                    "tag_key": row["tag_key"],
                    "object_id": row["object_id"],
                    "title": row["title"],
                    "protected_kind": row["protected_kind"],
                }
                for row in rows
            ]
            snapshot["unanchored_object_count"] = connection.execute(
                """
                SELECT COUNT(*)
                FROM live_memory_objects AS o
                WHERE NOT EXISTS (
                      SELECT 1
                      FROM system_owned_objects AS owned
                      WHERE owned.object_id = o.object_id
                        AND owned.protected_kind = 'system_tag'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM live_memory_links AS link
                      JOIN system_owned_objects AS target
                        ON target.object_id = link.target_object_id
                       AND target.protected_kind = 'system_tag'
                      WHERE link.source_object_id = o.object_id
                        AND link.target_kind = 'memory_object'
                        AND link.relation_type = 'tagged_with'
                        AND link.link_state = 'active'
                  )
                """
            ).fetchone()[0]
        return snapshot
    finally:
        connection.close()
