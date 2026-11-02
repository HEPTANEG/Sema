"""Read-only structured retrieval over the project-local LTM Core Store."""

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass

from . import ltm_store
from ..token_counter import count_text_tokens


PROJECTIONS = ("title", "title_summary", "core", "payload", "full")
SEARCH_FIELDS = ("title", "summary", "payload_body")
SELECT_ORDERS = ("created_at_asc", "created_at_desc")
SEARCH_ORDERS = ("relevance", "created_at_asc", "created_at_desc")
TRAVERSAL_DIRECTIONS = ("incoming", "outgoing", "both")
SEMANTIC_OVERFETCH_FACTOR = 5
SELECT_PARAMS = {
    "object_ids", "object_types", "created_at_from", "created_at_to",
    "projection", "order", "limit", "cursor",
}
SEARCH_PARAMS = {
    "text", "mode", "fields", "object_types", "created_at_from",
    "created_at_to", "projection", "order", "limit", "cursor",
}
TRAVERSE_PARAMS = {
    "start_object_ids", "direction", "relation_types", "object_types",
    "max_depth", "projection", "limit",
}


@dataclass(frozen=True)
class RetrievalProfile:
    profile_id: str
    operations: frozenset
    object_types: frozenset
    search_fields: frozenset
    projections: frozenset
    relation_types: frozenset
    default_projection: str
    max_depth: int
    max_objects: int
    max_payload_objects: int
    max_result_tokens: int


FORMATION_EXISTING_MEMORY_PROFILE = RetrievalProfile(
    profile_id="formation_existing_memory",
    operations=frozenset({"select_objects", "search_objects", "traverse_objects"}),
    object_types=frozenset(ltm_store.OBJECT_TYPES),
    search_fields=frozenset(SEARCH_FIELDS),
    projections=frozenset(PROJECTIONS),
    relation_types=frozenset(ltm_store.RELATION_TYPES),
    default_projection="title_summary",
    max_depth=2,
    max_objects=50,
    max_payload_objects=10,
    max_result_tokens=12000,
)

MEMORY_PUSH_PROFILE = RetrievalProfile(
    profile_id="memory_push",
    operations=frozenset({"semantic_search", "search_objects"}),
    object_types=frozenset(ltm_store.OBJECT_TYPES),
    search_fields=frozenset(SEARCH_FIELDS),
    projections=frozenset({"title", "title_summary"}),
    relation_types=frozenset(),
    default_projection="title_summary",
    max_depth=0,
    max_objects=50,
    max_payload_objects=0,
    max_result_tokens=12000,
)




MEMORY_RETRIEVAL_PROFILE = RetrievalProfile(
    profile_id="memory_retrieval",
    operations=frozenset({"semantic_search", "search_objects"}),
    object_types=frozenset(ltm_store.OBJECT_TYPES),
    search_fields=frozenset(SEARCH_FIELDS),
    projections=frozenset({"title", "title_summary"}),
    relation_types=frozenset(),
    default_projection="title_summary",
    max_depth=0,
    max_objects=50,
    max_payload_objects=0,
    max_result_tokens=12000,
)

FORMATION_RETRIEVAL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "ltm_select_objects",
            "description": "Read active long-term MemoryObjects by IDs, type, or creation time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_ids": {"type": "array", "items": {"type": "string"}},
                    "object_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ltm_store.OBJECT_TYPES)},
                    },
                    "created_at_from": {"type": "string"},
                    "created_at_to": {"type": "string"},
                    "projection": {"type": "string", "enum": list(PROJECTIONS)},
                    "order": {"type": "string", "enum": list(SELECT_ORDERS)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "cursor": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ltm_search_objects",
            "description": "Search active long-term MemoryObjects by literal text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "mode": {"type": "string", "enum": ["contains", "full_text"]},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SEARCH_FIELDS)},
                    },
                    "object_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ltm_store.OBJECT_TYPES)},
                    },
                    "created_at_from": {"type": "string"},
                    "created_at_to": {"type": "string"},
                    "projection": {"type": "string", "enum": list(PROJECTIONS)},
                    "order": {"type": "string", "enum": list(SEARCH_ORDERS)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "cursor": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ltm_traverse_objects",
            "description": "Traverse active MemoryLinks from one or more MemoryObjects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_object_ids": {"type": "array", "items": {"type": "string"}},
                    "direction": {"type": "string", "enum": list(TRAVERSAL_DIRECTIONS)},
                    "relation_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ltm_store.RELATION_TYPES)},
                    },
                    "object_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ltm_store.OBJECT_TYPES)},
                    },
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 2},
                    "projection": {"type": "string", "enum": list(PROJECTIONS)},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["start_object_ids"],
                "additionalProperties": False,
            },
        },
    },
]


class QueryError(ValueError):
    def __init__(self, code, path, message):
        super().__init__(message)
        self.code = code
        self.path = path


def _error(code, path, message):
    return {"code": code, "path": path, "message": str(message)}


def _result(status="ok", items=None, next_cursor=None, truncation_reason=None, errors=None):
    return {
        "status": status,
        "items": items or [],
        "next_cursor": next_cursor,
        "truncation_reason": truncation_reason,
        "errors": errors or [],
    }


def _error_result(error):
    if isinstance(error, QueryError):
        return _result("error", errors=[_error(error.code, error.path, error)])
    return _result("error", errors=[_error("query_failed", "query", error)])


def _connect_read_only(root=None):
    path = ltm_store.ltm_db_path(root)
    if not path.exists():
        raise QueryError("not_initialized", "ltm", "LTM database is not initialized.")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {ltm_store.BUSY_TIMEOUT_MS}")
    required = {
        "memory_objects",
        "memory_payloads",
        "memory_links",
        "memory_object_derived",
        ltm_store.LTM_FTS_TABLE,
    }
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not required <= tables:
        connection.close()
        raise QueryError(
            "retrieval_unavailable",
            "ltm_schema",
            "LTM Retrieval schema is not initialized; run the trusted LTM Store initialization path first.",
        )
    return connection


def _validate_operation(profile, operation):
    if not isinstance(profile, RetrievalProfile):
        raise QueryError("invalid_profile", "profile", "a trusted RetrievalProfile is required.")
    if operation not in profile.operations:
        raise QueryError("operation_denied", "operation", f"{operation} is not allowed by this Retrieval Profile.")


def _active_root_tag_ids(root=None):
    try:
        from .meta_memory import system_owned_tags
        return {
            item.get("object_id")
            for item in system_owned_tags(root)
            if item.get("object_id")
        }
    except Exception:
        return set()


def _validate_params(params, allowed):
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise QueryError("unknown_parameter", "params", f"unsupported query parameters: {', '.join(unknown)}")


def _list(value, path, allowed=None, default=None):
    if value is None:
        return list(default or [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise QueryError("invalid_list", path, f"{path} must be a list of non-empty strings.")
    values = list(dict.fromkeys(value))
    if allowed is not None and not set(values) <= set(allowed):
        raise QueryError("value_denied", path, f"{path} contains values outside the Retrieval Profile.")
    return values


def _projection(params, profile):
    projection = str(params.get("projection") or profile.default_projection)
    if projection not in profile.projections:
        raise QueryError("projection_denied", "projection", "projection is not allowed by this Retrieval Profile.")
    return projection


def _limit(params, profile, projection):
    value = params.get("limit", profile.max_objects)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QueryError("invalid_limit", "limit", "limit must be a positive integer.")
    requested = min(value, profile.max_objects)
    if projection in {"payload", "full"}:
        return min(requested, profile.max_payload_objects), False
    return requested, False


def _object_types(params, profile):
    return _list(params.get("object_types"), "object_types", profile.object_types, profile.object_types)


def _encode_cursor(payload):
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value, kind, signature):
    if not value:
        return None
    try:
        padded = str(value) + "=" * (-len(str(value)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as error:
        raise QueryError("invalid_cursor", "cursor", "cursor is malformed.") from error
    if payload.get("kind") != kind or payload.get("signature") != signature:
        raise QueryError("invalid_cursor", "cursor", "cursor does not match this query.")
    return payload


def _query_signature(operation, params, excluded=("cursor",)):
    normalized = {key: value for key, value in params.items() if key not in excluded}
    body = json.dumps({"operation": operation, "params": normalized}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]


def _projection_columns(projection):
    columns = [
        "o.object_id", "o.object_type", "o.title", "o.summary",
        "o.created_at", "o.updated_at", "d.strength AS strength",
    ]
    if projection in {"payload", "full"}:
        columns.extend([
            "p.schema_version AS payload_schema_version",
            "p.body AS payload_body_value",
            "p.structured_data AS payload_structured_data",
        ])
    return columns


def _project_row(row, projection, include_raw_score=False):
    item = {
        "object_id": row["object_id"],
        "object_type": row["object_type"],
        "strength": float(row["strength"]),
    }
    if projection in PROJECTIONS:
        item["title"] = row["title"]
    if projection in {"title_summary", "core", "payload", "full"}:
        item["summary"] = row["summary"]
    if projection in {"core", "full"}:
        item["created_at"] = row["created_at"]
        item["updated_at"] = row["updated_at"]
    if projection in {"payload", "full"}:
        keys = set(row.keys())
        if "payload_schema_version" in keys and row["payload_schema_version"] is not None:
            item["payload"] = {
                "schema_version": int(row["payload_schema_version"]),
                "body": row["payload_body_value"],
                "structured_data": json.loads(row["payload_structured_data"]) if row["payload_structured_data"] else None,
            }
        else:
            item["payload"] = None
    if include_raw_score and "_rank" in set(row.keys()) and row["_rank"] is not None:
        item["_raw_score"] = float(row["_rank"])
    return item


def _bounded_items(
    rows,
    projection,
    limit,
    profile,
    cursor_factory=None,
    limit_reason="item_limit",
    include_raw_score=False,
):
    items = []
    used_tokens = 0
    reason = None
    has_more = len(rows) > limit
    for row in rows[:limit]:
        item = _project_row(row, projection, include_raw_score=include_raw_score)
        item_tokens = count_text_tokens(json.dumps(item, ensure_ascii=False, sort_keys=True))
        if used_tokens + item_tokens > profile.max_result_tokens:
            if not items:
                return _result(
                    "error",
                    errors=[_error("result_item_too_large", "result", "one result item exceeds the profile token budget.")],
                )
            reason = "token_limit"
            break
        items.append(item)
        used_tokens += item_tokens
    if len(items) < min(len(rows), limit):
        has_more = True
        reason = reason or "token_limit"
    elif has_more:
        reason = limit_reason
    next_cursor = cursor_factory(rows[len(items) - 1], len(items)) if has_more and items and cursor_factory else None
    return _result("partial" if has_more else "ok", items, next_cursor, reason)


def _strength_predicate(profile, alias="d"):
    if profile.profile_id == "memory_push":
        return f"{alias}.strength >= {ltm_store.PUSH_STRENGTH_THRESHOLD}"
    return f"{alias}.strength > 0"


def _common_filters(params, profile, alias="o", derived_alias="d"):
    clauses = [_strength_predicate(profile, derived_alias)]
    values = []
    object_types = _object_types(params, profile)
    if object_types:
        clauses.append(f"{alias}.object_type IN ({','.join('?' for _ in object_types)})")
        values.extend(object_types)
    if params.get("created_at_from") is not None:
        clauses.append(f"{alias}.created_at >= ?")
        values.append(str(params["created_at_from"]))
    if params.get("created_at_to") is not None:
        clauses.append(f"{alias}.created_at <= ?")
        values.append(str(params["created_at_to"]))
    return clauses, values


def select_objects(params, *, profile, root=None):
    try:
        _validate_operation(profile, "select_objects")
        if not isinstance(params, dict):
            raise QueryError("invalid_query", "params", "params must be an object.")
        _validate_params(params, SELECT_PARAMS)
        projection = _projection(params, profile)
        limit, prelimited = _limit(params, profile, projection)
        order = str(params.get("order") or "created_at_asc")
        if order not in SELECT_ORDERS:
            raise QueryError("invalid_order", "order", "select order must be created_at_asc or created_at_desc.")
        signature = _query_signature("select_objects", params)
        cursor = _decode_cursor(params.get("cursor"), "select_objects", signature)
        clauses, values = _common_filters(params, profile)
        object_ids = _list(params.get("object_ids"), "object_ids")
        if len(object_ids) > 200:
            raise QueryError("too_many_ids", "object_ids", "object_ids accepts at most 200 values.")
        if object_ids:
            clauses.append(f"o.object_id IN ({','.join('?' for _ in object_ids)})")
            values.extend(object_ids)
        descending = order.endswith("desc")
        if cursor:
            comparison = "<" if descending else ">"
            clauses.append(
                f"(o.created_at {comparison} ? OR (o.created_at = ? AND o.object_id {comparison} ?))"
            )
            values.extend([cursor["created_at"], cursor["created_at"], cursor["object_id"]])
        join_payload = projection in {"payload", "full"}
        sql = "SELECT " + ", ".join(_projection_columns(projection)) + " FROM live_memory_objects AS o "
        sql += "JOIN live_memory_object_derived AS d ON d.object_id = o.object_id "
        if join_payload:
            sql += "LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id "
        sql += "WHERE " + " AND ".join(clauses)
        direction = "DESC" if descending else "ASC"
        sql += f" ORDER BY o.created_at {direction}, o.object_id {direction} LIMIT ?"
        values.append(limit + 1)
        connection = _connect_read_only(root)
        try:
            rows = connection.execute(sql, values).fetchall()
        finally:
            connection.close()

        def cursor_factory(row, _count):
            return _encode_cursor({
                "kind": "select_objects",
                "signature": signature,
                "created_at": row["created_at"],
                "object_id": row["object_id"],
            })

        reason = "payload_limit" if projection in {"payload", "full"} else "item_limit"
        return _bounded_items(rows, projection, limit, profile, cursor_factory, reason)
    except Exception as error:
        return _error_result(error)


def _literal_fts_query(text, fields):
    escaped = text.replace('"', '""')
    columns = " ".join(fields)
    return f'{{{columns}}} : "{escaped}"'


def search_objects(params, *, profile, root=None, _include_raw_score=False):
    try:
        _validate_operation(profile, "search_objects")
        if not isinstance(params, dict):
            raise QueryError("invalid_query", "params", "params must be an object.")
        _validate_params(params, SEARCH_PARAMS)
        text = str(params.get("text") or "").strip()
        if not text:
            raise QueryError("empty_query", "text", "text must be non-empty.")
        mode = str(params.get("mode") or "full_text")
        if mode not in {"contains", "full_text"}:
            raise QueryError("invalid_mode", "mode", "mode must be contains or full_text.")
        if mode == "full_text" and len(text) < 3:
            mode = "contains"
        fields = _list(params.get("fields"), "fields", profile.search_fields, profile.search_fields)
        projection = _projection(params, profile)
        limit, prelimited = _limit(params, profile, projection)
        default_order = "relevance" if mode == "full_text" else "created_at_desc"
        order = str(params.get("order") or default_order)
        if order not in SEARCH_ORDERS or mode == "contains" and order == "relevance":
            raise QueryError("invalid_order", "order", "search order is incompatible with the selected mode.")
        signature_params = {**params, "mode": mode}
        signature = _query_signature("search_objects", signature_params)
        cursor = _decode_cursor(params.get("cursor"), "search_objects", signature)
        offset = int(cursor.get("offset", 0)) if cursor else 0
        clauses, values = _common_filters(params, profile)
        join_payload = projection in {"payload", "full"} or "payload_body" in fields
        columns = ", ".join(_projection_columns(projection))
        if mode == "contains":
            field_sql = {
                "title": "instr(COALESCE(o.title, ''), ?) > 0",
                "summary": "instr(COALESCE(o.summary, ''), ?) > 0",
                "payload_body": "instr(COALESCE(p.body, ''), ?) > 0",
            }
            clauses.append("(" + " OR ".join(field_sql[field] for field in fields) + ")")
            values.extend([text] * len(fields))
            sql = f"SELECT {columns} FROM live_memory_objects AS o "
            sql += "JOIN live_memory_object_derived AS d ON d.object_id = o.object_id "
            if join_payload:
                sql += "LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id "
            sql += "WHERE " + " AND ".join(clauses)
        else:
            sql = (
                f"SELECT {columns}, bm25({ltm_store.LTM_FTS_TABLE}) AS _rank "
                f"FROM {ltm_store.LTM_FTS_TABLE} "
                "JOIN live_memory_objects AS o "
                f"ON o.object_id = {ltm_store.LTM_FTS_TABLE}.object_id "
                "JOIN live_memory_object_derived AS d ON d.object_id = o.object_id "
            )
            if projection in {"payload", "full"}:
                sql += "LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id "
            sql += f"WHERE {ltm_store.LTM_FTS_TABLE} MATCH ? AND " + " AND ".join(clauses)
            values.insert(0, _literal_fts_query(text, fields))
        if order == "relevance":
            sql += " ORDER BY _rank ASC, o.created_at DESC, o.object_id ASC"
        else:
            direction = "ASC" if order.endswith("asc") else "DESC"
            sql += f" ORDER BY o.created_at {direction}, o.object_id {direction}"
        sql += " LIMIT ? OFFSET ?"
        values.extend([limit + 1, offset])
        connection = _connect_read_only(root)
        try:
            rows = connection.execute(sql, values).fetchall()
        finally:
            connection.close()

        def cursor_factory(_row, count):
            return _encode_cursor({
                "kind": "search_objects",
                "signature": signature,
                "offset": offset + count,
            })

        reason = "payload_limit" if projection in {"payload", "full"} else "item_limit"
        return _bounded_items(
            rows,
            projection,
            limit,
            profile,
            cursor_factory,
            reason,
            include_raw_score=_include_raw_score,
        )
    except Exception as error:
        return _error_result(error)


def _semantic_objects_by_ids_many(object_id_groups, projection, profile, root=None):
    groups = [list(dict.fromkeys(group or [])) for group in object_id_groups]
    all_object_ids = list(dict.fromkeys(object_id for group in groups for object_id in group))
    if not all_object_ids:
        return [[] for _ in groups]
    placeholders = ",".join("?" for _ in all_object_ids)
    columns = _projection_columns(projection)
    sql = "SELECT " + ", ".join(columns) + " FROM live_memory_objects AS o "
    sql += "JOIN live_memory_object_derived AS d ON d.object_id = o.object_id "
    if projection in {"payload", "full"}:
        sql += "LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id "
    sql += (
        f"WHERE o.object_type IN ({','.join('?' for _ in profile.object_types)}) "
        f"AND o.object_id IN ({placeholders}) AND {_strength_predicate(profile)}"
    )
    values = [*sorted(profile.object_types), *all_object_ids]
    connection = _connect_read_only(root)
    try:
        rows = connection.execute(sql, values).fetchall()
    finally:
        connection.close()
    by_id = {row["object_id"]: row for row in rows}
    root_ids = _active_root_tag_ids(root)
    return [[
        _project_row(by_id[object_id], projection)
        for object_id in group
        if object_id in by_id and object_id not in root_ids
    ] for group in groups]


def _semantic_objects_by_ids(object_ids, projection, profile, root=None):
    return _semantic_objects_by_ids_many([object_ids], projection, profile, root)[0]


def _semantic_result_from_matches(index_results, profile, root=None):
    object_ids = [item["object_id"] for item in index_results]
    objects = _semantic_objects_by_ids(object_ids, profile.default_projection, profile, root)
    object_map = {item["object_id"]: item for item in objects}
    items = []
    for match in index_results:
        item = object_map.get(match["object_id"])
        if item is None:
            continue
        items.append({
            **item,
            "similarity": float(match["similarity"]),
            "retrieval_source": "embedding",
        })
    return _result("ok", items[:profile.max_objects])


def _semantic_results_from_matches(match_groups, profile, root=None):
    object_id_groups = [
        [item["object_id"] for item in index_results]
        for index_results in match_groups
    ]
    object_groups = _semantic_objects_by_ids_many(object_id_groups, profile.default_projection, profile, root)
    results = []
    for index_results, objects in zip(match_groups, object_groups):
        object_map = {item["object_id"]: item for item in objects}
        items = []
        for match in index_results:
            item = object_map.get(match["object_id"])
            if item is None:
                continue
            items.append({
                **item,
                "similarity": float(match["similarity"]),
                "retrieval_source": "embedding",
            })
        results.append(_result("ok", items[:profile.max_objects]))
    return results


def semantic_search(query, *, profile, root=None, _diagnostic_profiler=None):
    """Search the local semantic index and hydrate active MemoryObjects."""
    try:
        _validate_operation(profile, "semantic_search")
        if not isinstance(query, str) or not query.strip():
            raise QueryError("empty_query", "query", "query must be a non-empty string.")
        from .embedding_adapter import embed_queries
        from .vector_index import VectorIndexAdapter

        if _diagnostic_profiler is not None:
            _diagnostic_profiler.increment("semantic_query_count")
            batch = embed_queries([query.strip()], _diagnostic_profiler=_diagnostic_profiler)
        else:
            batch = embed_queries([query.strip()])
        if not batch.vectors:
            raise QueryError("embedding_failed", "query", "Embedding Adapter returned no vector.")
        if _diagnostic_profiler is not None:
            with _diagnostic_profiler.measure("vector_index_search_ms"):
                index_results = VectorIndexAdapter(root).search(
                    batch.vectors[0], profile.max_objects * SEMANTIC_OVERFETCH_FACTOR
                )
        else:
            index_results = VectorIndexAdapter(root).search(
                batch.vectors[0], profile.max_objects * SEMANTIC_OVERFETCH_FACTOR
            )
        return _semantic_result_from_matches(index_results, profile, root)
    except Exception as error:
        return _error_result(error)


def semantic_search_many(queries, *, profile, root=None, _diagnostic_profiler=None):
    """Search multiple semantic queries with one batched embedding request."""
    try:
        _validate_operation(profile, "semantic_search")
        if not isinstance(queries, (list, tuple)) or not queries:
            raise QueryError("empty_query", "queries", "queries must be a non-empty sequence.")
        normalized = []
        for index, query in enumerate(queries):
            if not isinstance(query, str) or not query.strip():
                raise QueryError("empty_query", f"queries[{index}]", "query must be a non-empty string.")
            normalized.append(query.strip())

        from .embedding_adapter import embed_queries
        from .vector_index import VectorIndexAdapter

        if _diagnostic_profiler is not None:
            _diagnostic_profiler.increment("semantic_query_count", len(normalized))
            batch = embed_queries(normalized, _diagnostic_profiler=_diagnostic_profiler)
        else:
            batch = embed_queries(normalized)
        if len(batch.vectors) != len(normalized):
            raise QueryError("embedding_failed", "queries", "Embedding Adapter returned an unexpected vector count.")

        index = VectorIndexAdapter(root)
        if _diagnostic_profiler is not None:
            with _diagnostic_profiler.measure("vector_index_search_ms"):
                matches = [
                    index.search(vector, profile.max_objects * SEMANTIC_OVERFETCH_FACTOR)
                    for vector in batch.vectors
                ]
        else:
            matches = [
                index.search(vector, profile.max_objects * SEMANTIC_OVERFETCH_FACTOR)
                for vector in batch.vectors
            ]
        return _semantic_results_from_matches(matches, profile, root)
    except Exception as error:
        result = _error_result(error)
        count = len(queries) if isinstance(queries, (list, tuple)) else 0
        return [dict(result) for _ in range(count)]


def _link_rows(connection, frontier, direction, relation_types):
    if not frontier:
        return []
    placeholders = ",".join("?" for _ in frontier)
    relation_clause = ""
    relation_values = []
    if relation_types:
        relation_clause = f" AND relation_type IN ({','.join('?' for _ in relation_types)})"
        relation_values = list(relation_types)
    rows = []
    if direction in {"outgoing", "both"}:
        rows.extend(
            ("outgoing", row)
            for row in connection.execute(
                f"""
                SELECT * FROM live_memory_links
                WHERE link_state = 'active' AND target_kind = 'memory_object'
                  AND source_object_id IN ({placeholders}){relation_clause}
                ORDER BY created_at, link_id
                """,
                list(frontier) + relation_values,
            ).fetchall()
        )
    if direction in {"incoming", "both"}:
        rows.extend(
            ("incoming", row)
            for row in connection.execute(
                f"""
                SELECT * FROM live_memory_links
                WHERE link_state = 'active' AND target_kind = 'memory_object'
                  AND target_object_id IN ({placeholders}){relation_clause}
                ORDER BY created_at, link_id
                """,
                list(frontier) + relation_values,
            ).fetchall()
        )
    rows.sort(key=lambda item: (item[1]["created_at"], item[1]["link_id"], item[0]))
    return rows


def _objects_by_ids(connection, object_ids, projection):
    if not object_ids:
        return {}
    placeholders = ",".join("?" for _ in object_ids)
    sql = "SELECT " + ", ".join(_projection_columns(projection)) + " FROM live_memory_objects AS o "
    sql += "JOIN live_memory_object_derived AS d ON d.object_id = o.object_id "
    if projection in {"payload", "full"}:
        sql += "LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id "
    sql += f"WHERE o.object_id IN ({placeholders}) AND d.strength > 0"
    return {row["object_id"]: row for row in connection.execute(sql, object_ids).fetchall()}


def traverse_objects(params, *, profile, root=None):
    try:
        _validate_operation(profile, "traverse_objects")
        if not isinstance(params, dict):
            raise QueryError("invalid_query", "params", "params must be an object.")
        _validate_params(params, TRAVERSE_PARAMS)
        starts = _list(params.get("start_object_ids"), "start_object_ids")
        if not starts:
            raise QueryError("missing_start", "start_object_ids", "at least one start object is required.")
        if len(starts) > profile.max_objects:
            raise QueryError("too_many_starts", "start_object_ids", "too many start objects.")
        direction = str(params.get("direction") or "both")
        if direction not in TRAVERSAL_DIRECTIONS:
            raise QueryError("invalid_direction", "direction", "direction must be incoming, outgoing, or both.")
        relation_types = _list(
            params.get("relation_types"), "relation_types", profile.relation_types, profile.relation_types
        )
        object_types = set(_object_types(params, profile))
        projection = _projection(params, profile)
        limit, prelimited = _limit(params, profile, projection)
        max_depth = params.get("max_depth", 1)
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not 1 <= max_depth <= profile.max_depth:
            raise QueryError("invalid_depth", "max_depth", f"max_depth must be between 1 and {profile.max_depth}.")
        connection = _connect_read_only(root)
        try:
            seed_rows = _objects_by_ids(connection, starts, "title")
            if len(seed_rows) != len(set(starts)):
                raise QueryError("start_not_found", "start_object_ids", "every start object must exist and be active.")
            visited = set(starts)
            frontier_paths = {object_id: [] for object_id in starts}
            discovered = []
            discovered_paths = {}
            truncated = prelimited
            for _depth in range(1, max_depth + 1):
                next_paths = {}
                for edge_direction, row in _link_rows(connection, sorted(frontier_paths), direction, relation_types):
                    current = row["source_object_id"] if edge_direction == "outgoing" else row["target_object_id"]
                    neighbor = row["target_object_id"] if edge_direction == "outgoing" else row["source_object_id"]
                    if current not in frontier_paths or neighbor in visited or neighbor in next_paths:
                        continue
                    step = {
                        "link_id": row["link_id"],
                        "relation_type": row["relation_type"],
                        "direction": edge_direction,
                        "source_object_id": row["source_object_id"],
                        "target_object_id": row["target_object_id"],
                    }
                    next_paths[neighbor] = frontier_paths[current] + [step]
                if not next_paths:
                    break
                active_rows = _objects_by_ids(connection, sorted(next_paths), "title")
                frontier_paths = {}
                for object_id in sorted(next_paths):
                    row = active_rows.get(object_id)
                    if row is None:
                        continue
                    visited.add(object_id)
                    frontier_paths[object_id] = next_paths[object_id]
                    if row["object_type"] in object_types:
                        discovered.append(object_id)
                        discovered_paths[object_id] = next_paths[object_id]
                        if len(discovered) > limit:
                            truncated = True
                            break
                if len(discovered) > limit:
                    break
            rows_by_id = _objects_by_ids(connection, discovered[:limit], projection)
            items = []
            used_tokens = 0
            for object_id in discovered[:limit]:
                row = rows_by_id.get(object_id)
                if row is None:
                    continue
                item = _project_row(row, projection)
                item["path"] = discovered_paths[object_id]
                tokens = count_text_tokens(json.dumps(item, ensure_ascii=False, sort_keys=True))
                if used_tokens + tokens > profile.max_result_tokens:
                    if not items:
                        return _result(
                            "error",
                            errors=[_error("result_item_too_large", "result", "one traversal item exceeds the profile token budget.")],
                        )
                    truncated = True
                    break
                items.append(item)
                used_tokens += tokens
            reason = None
            if truncated or len(items) < min(len(discovered), limit):
                reason = "token_limit" if len(items) < min(len(discovered), limit) else (
                    "payload_limit" if projection in {"payload", "full"} else "item_limit"
                )
            return _result("partial" if reason else "ok", items, None, reason)
        finally:
            connection.close()
    except Exception as error:
        return _error_result(error)


def run_formation_retrieval_tool(tool_name, args, root=None):
    mapping = {
        "ltm_select_objects": select_objects,
        "ltm_search_objects": search_objects,
        "ltm_traverse_objects": traverse_objects,
    }
    function = mapping.get(str(tool_name or ""))
    if function is None:
        return _result("error", errors=[_error("unknown_tool", "tool", f"unknown retrieval tool: {tool_name}")])
    return function(args or {}, profile=FORMATION_EXISTING_MEMORY_PROFILE, root=root)
