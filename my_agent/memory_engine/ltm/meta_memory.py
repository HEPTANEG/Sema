"""Deterministic bootstrap for project-local Meta-Memory system tags."""

import sqlite3
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from . import ltm_store


BOOTSTRAP_VERSION = "meta-memory-bootstrap-v1"
SYSTEM_TAG_OWNER = "meta_memory_agent"
SYSTEM_TAG_KIND = "system_tag"
_TAG_NAMESPACE = uuid5(NAMESPACE_URL, "sema://meta-memory/system-tags/v1")

SYSTEM_TAG_DEFINITIONS = (
    ("sema_self", "Sema Self"),
    ("user_model", "User Model"),
    ("safety", "Safety"),
    ("others", "Others"),
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def system_tag_object_id(tag_key):
    """Return the stable Core Store object ID for a known system tag."""
    if tag_key not in {key for key, _title in SYSTEM_TAG_DEFINITIONS}:
        raise ValueError(f"unknown system tag key: {tag_key}")
    return f"mem_{uuid5(_TAG_NAMESPACE, tag_key).hex}"


def _result(status, tag_ids=None, created_count=0, error=""):
    return {
        "status": status,
        "bootstrap_version": BOOTSTRAP_VERSION,
        "tag_ids": tag_ids or {},
        "created_count": int(created_count),
        "error": str(error or ""),
    }


def bootstrap_system_tags(root=None):
    """Create the four protected system tags exactly once for this project."""
    connection = None
    try:
        connection = ltm_store.ensure_ltm_store(root)
        connection.execute("BEGIN IMMEDIATE")
        expected = {
            key: {
                "object_id": system_tag_object_id(key),
                "title": title,
            }
            for key, title in SYSTEM_TAG_DEFINITIONS
        }
        rows = connection.execute(
            """
            SELECT r.tag_key, r.object_id, r.owner, r.protected_kind,
                   o.object_type, o.title, o.summary
            FROM system_owned_objects AS r
            JOIN memory_objects AS o ON o.object_id = r.object_id
            WHERE r.protected_kind = ?
            """,
            (SYSTEM_TAG_KIND,),
        ).fetchall()
        existing = {row["tag_key"]: row for row in rows}

        for tag_key, definition in expected.items():
            row = existing.get(tag_key)
            if row is not None:
                if (
                    row["object_id"] != definition["object_id"]
                    or row["owner"] != SYSTEM_TAG_OWNER
                    or row["object_type"] != "tag"
                    or row["title"] != definition["title"]
                    or row["summary"] is not None
                ):
                    raise RuntimeError(f"system tag definition conflict: {tag_key}")
                continue

            object_row = connection.execute(
                "SELECT object_type, title, summary FROM memory_objects WHERE object_id = ?",
                (definition["object_id"],),
            ).fetchone()
            if object_row is not None:
                raise RuntimeError(f"system tag object ID conflict: {tag_key}")

        now = _now()
        created_count = 0
        for tag_key, definition in expected.items():
            if tag_key in existing:
                continue
            connection.execute(
                """
                INSERT INTO memory_objects(
                    object_id, object_type, title, summary, created_at, updated_at
                ) VALUES (?, 'tag', ?, NULL, ?, ?)
                """,
                (definition["object_id"], definition["title"], now, now),
            )
            connection.execute(
                """
                INSERT INTO system_owned_objects(
                    tag_key, object_id, owner, protected_kind, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (tag_key, definition["object_id"], SYSTEM_TAG_OWNER, SYSTEM_TAG_KIND, now),
            )
            ltm_store._sync_retrieval_index_object(connection, definition["object_id"])
            ltm_store._ensure_derived_row(connection, definition["object_id"], now)
            created_count += 1

        connection.commit()
        status = "bootstrapped" if created_count else "already_bootstrapped"
        return _result(
            status,
            {key: definition["object_id"] for key, definition in expected.items()},
            created_count,
        )
    except Exception as error:
        if connection is not None:
            connection.rollback()
        return _result("failed", error=error)
    finally:
        if connection is not None:
            connection.close()


def system_owned_tags(root=None):
    """Return metadata for protected system tags without initializing storage."""
    result = []
    path = ltm_store.ltm_db_path(root)
    if not path.exists():
        return result
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_owned_objects'"
        ).fetchone()
        if table is None:
            return result
        rows = connection.execute(
            """
            SELECT r.tag_key, r.object_id, r.owner, r.protected_kind, o.title
            FROM system_owned_objects AS r
            JOIN memory_objects AS o ON o.object_id = r.object_id
            WHERE r.protected_kind = ?
            ORDER BY r.tag_key
            """,
            (SYSTEM_TAG_KIND,),
        ).fetchall()
        return [
            {
                "tag_key": row["tag_key"],
                "object_id": row["object_id"],
                "title": row["title"],
                "protected_kind": row["protected_kind"],
            }
            for row in rows
        ]
    finally:
        connection.close()
