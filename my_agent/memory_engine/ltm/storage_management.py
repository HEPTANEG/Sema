"""Explicit, preview-bound reclamation of tombstoned LTM data only."""

import json
import math
from uuid import uuid4

from . import ltm_store
from .storage_gate import exclusive, memory_task


def _schema(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS ltm_storage_settings (id INTEGER PRIMARY KEY CHECK(id=1), warning_mib REAL)")
    connection.execute("""CREATE TABLE IF NOT EXISTS ltm_purge_previews (
        token TEXT PRIMARY KEY, object_ids TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")
    connection.commit()


def _sizes(root):
    directory = ltm_store.ltm_dir(root)
    db = ltm_store.ltm_db_path(root)
    database_bytes = sum(path.stat().st_size for path in
                         (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")) if path.exists())
    vector_dir = directory / "vector"
    vector_bytes = sum(path.stat().st_size for path in vector_dir.rglob("*") if path.is_file()) if vector_dir.exists() else 0
    return {"database_bytes": database_bytes, "vector_bytes": vector_bytes, "total_bytes": database_bytes + vector_bytes}


def status(root=None):
    connection = ltm_store._connect(root, read_only=True)
    live = deleted = 0
    warning = None
    if connection is not None:
        try:
            if "deleted_at" in ltm_store._columns(connection, "memory_objects"):
                live, deleted = connection.execute("SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL), COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) FROM memory_objects").fetchone()
            else:
                live = connection.execute("SELECT COUNT(*) FROM memory_objects").fetchone()[0]
            if ltm_store._table_exists(connection, "ltm_storage_settings"):
                row = connection.execute("SELECT warning_mib FROM ltm_storage_settings WHERE id=1").fetchone()
                warning = row[0] if row else None
        finally:
            connection.close()
    sizes = _sizes(root)
    return {"status": "ok", "db_path": str(ltm_store.ltm_db_path(root).resolve()),
            "live_object_count": live, "deleted_object_count": deleted, "warning_mib": warning,
            "over_warning": warning is not None and sizes["total_bytes"] >= warning * 1024 * 1024, **sizes}


@memory_task
def update_warning(warning_mib, root=None):
    if warning_mib is not None and (isinstance(warning_mib, bool) or not isinstance(warning_mib, (int, float))
                                    or not math.isfinite(warning_mib) or warning_mib <= 0):
        raise ValueError("warning_mib must be a positive finite number or null (disabled).")
    connection = ltm_store.ensure_ltm_store(root)
    try:
        _schema(connection)
        connection.execute("INSERT INTO ltm_storage_settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET warning_mib=excluded.warning_mib", (warning_mib,))
        connection.commit()
    finally:
        connection.close()
    return status(root)


@memory_task
def preview(root=None):
    connection = ltm_store.ensure_ltm_store(root)
    try:
        _schema(connection)
        ids = [row[0] for row in connection.execute("SELECT object_id FROM memory_objects WHERE deleted_at IS NOT NULL ORDER BY object_id")]
        token = uuid4().hex
        connection.execute("INSERT INTO ltm_purge_previews(token,object_ids,created_at) VALUES (?,?,?)", (token, json.dumps(ids), ltm_store._now()))
        connection.commit()
    finally:
        connection.close()
    return {"status": "preview", "preview_token": token, "object_count": len(ids),
            "db_path": str(ltm_store.ltm_db_path(root).resolve()),
            "warning": "Permanent and irreversible. Removes only this batch of logically deleted memories and incident links; Event Stream and historical diagnostics are retained."}


def _vacuum(connection):
    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint[0]:
        raise RuntimeError("SQLite readers prevented a complete WAL checkpoint; retry later.")
    connection.execute("VACUUM")
    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint[0]:
        raise RuntimeError("SQLite readers prevented final WAL truncation; retry later.")


def purge(preview_token, *, confirmed=False, root=None):
    if confirmed is not True:
        raise ValueError("Explicit confirmation is required.")
    try:
        with exclusive(root):
            return _purge_locked(preview_token, root)
    except RuntimeError as error:
        if "busy" in str(error):
            return {"status": "busy", "message": str(error)}
        raise


def _purge_locked(preview_token, root):
    before = _sizes(root)

    connection = ltm_store._ensure_ltm_store(root)
    try:
        _schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM ltm_purge_previews WHERE token=?", (preview_token,)).fetchone()
        if not row:
            raise ValueError("Unknown cleanup preview for this library.")
        ids = [] if row["consumed"] else [oid for oid in json.loads(row["object_ids"])
            if connection.execute("SELECT 1 FROM memory_objects WHERE object_id=? AND deleted_at IS NOT NULL", (oid,)).fetchone()]
        from .vector_index import get_vector_index
        get_vector_index(root).remove_many(ids)
        removed = 0
        for oid in ids:
            if connection.execute("SELECT 1 FROM memory_objects WHERE object_id=? AND deleted_at IS NOT NULL", (oid,)).fetchone() is None:
                continue
            connection.execute("DELETE FROM memory_links WHERE source_object_id=? OR target_object_id=?", (oid, oid))
            connection.execute("DELETE FROM memory_payloads WHERE object_id=?", (oid,))
            connection.execute("DELETE FROM memory_object_derived WHERE object_id=?", (oid,))
            connection.execute(f"DELETE FROM {ltm_store.LTM_FTS_TABLE} WHERE object_id=?", (oid,))
            connection.execute("DELETE FROM memory_objects WHERE object_id=? AND deleted_at IS NOT NULL", (oid,))
            removed += 1
        connection.execute("UPDATE ltm_purge_previews SET consumed=1 WHERE token=?", (preview_token,))
        connection.commit()
        error = None
        try:
            _vacuum(connection)
        except Exception as failure:
            error = str(failure)
        return {"status": "compaction_failed" if error else "purged", "records_purged": True,
                "removed_count": removed, "preview_token": preview_token,
                "compaction_error": error, "before": before, "after": _sizes(root)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
