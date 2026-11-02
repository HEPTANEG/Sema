"""Read-only, runtime-bound observations for the future LTM Maintainer."""

import json
from functools import wraps

from .. import event_stream
from . import ltm_store, vector_index


CAPS = {"objects": 50, "payload_objects": 10, "links": 100, "depth": 3,
        "nodes": 100, "slots": 20, "context": 5, "neighborhood": 200}


def _error(code, message):
    return {"status": "rejected", "error": {"code": code, "message": message}}


def _connection(root):
    connection = ltm_store._connect(root, read_only=True)
    if connection is None:
        raise ValueError("LTM Store is not initialized")
    return connection


def _projection(connection, row, projection="core", root=None):
    item = {"object_id": row["object_id"], "object_type": row["object_type"], "title": row["title"],
            "summary": row["summary"], "strength": row["strength"]}
    if projection in {"core", "full", "bundle"}:
        item.update({"created_at": row["created_at"], "updated_at": row["updated_at"]})
    if projection in {"payload", "full", "bundle"}:
        payload = connection.execute("SELECT schema_version, body, structured_data FROM memory_payloads WHERE object_id=?", (row["object_id"],)).fetchone()
        item["payload"] = None if payload is None else {"schema_version": payload["schema_version"], "body": payload["body"], "structured_data": json.loads(payload["structured_data"]) if payload["structured_data"] else None}
    if projection in {"full", "bundle"}:
        item["derived"] = {key: row[key] for key in ("successful_use_count", "gain_link_count", "loss_link_count", "neutral_link_count", "strength", "strength_formula_version", "derived_updated_at")}
    if projection == "bundle":
        links = connection.execute("""SELECT relation_type, source_object_id, target_object_id, target_kind
            FROM live_memory_links WHERE link_state='active' AND (source_object_id=? OR (target_kind='memory_object' AND target_object_id=?))
            ORDER BY created_at, link_id LIMIT ?""", (row["object_id"], row["object_id"], CAPS["neighborhood"])).fetchall()
        neighbors = []
        for link in links:
            other = link["target_object_id"] if link["source_object_id"] == row["object_id"] else link["source_object_id"]
            if other:
                target = connection.execute("SELECT title FROM live_memory_objects WHERE object_id=?", (other,)).fetchone()
                if target:
                    neighbors.append({"object_id": other, "title": target["title"], "relation_type": link["relation_type"], "direction": "outgoing" if link["source_object_id"] == row["object_id"] else "incoming"})
        item["direct_neighborhood_titles"] = neighbors
        item["source_slots"] = [dict(link) for link in connection.execute("""SELECT slot_seq_start, slot_seq_end FROM live_memory_links
            WHERE source_object_id=? AND target_kind='event_slot' AND relation_type='sourced_from' AND link_state='active'
            ORDER BY slot_seq_start LIMIT 20""", (row["object_id"],))]
    return item


def get_object(args=None, root=None):
    args = dict(args or {})
    limit = min(int(args.get("limit", 20)), CAPS["objects"])
    projection = args.get("projection", "core")
    if projection not in {"title_summary", "core", "payload", "full", "bundle"}:
        return _error("projection", "unsupported projection")
    if projection == "bundle":
        limit = 1
    if projection in {"payload", "full"}:
        limit = min(limit, CAPS["payload_objects"])
    terms = None
    if "query" in args and args["query"] is not None:
        if not isinstance(args["query"], str):
            return _error("query", "query must be ordinary text")
        terms = list(dict.fromkeys(term.casefold() for term in args["query"].split()
                                  if any(char.isalnum() for char in term)))
        if not terms:
            return _error("query", "query must contain searchable text")
    connection = _connection(root)
    try:
        where, params = ["1=1"], []
        ids = args.get("object_ids") or ([args["object_id"]] if args.get("object_id") else [])
        if ids:
            where.append("o.object_id IN (%s)" % ",".join("?" for _ in ids)); params.extend(ids)
        if args.get("title"):
            where.append("instr(lower(o.title), lower(?)) > 0"); params.append(str(args["title"]))
        if args.get("min_strength") is not None: where.append("d.strength >= ?"); params.append(float(args["min_strength"]))
        if args.get("max_strength") is not None: where.append("d.strength <= ?"); params.append(float(args["max_strength"]))
        if args.get("linked_to_object_id"):
            where.append("EXISTS (SELECT 1 FROM live_memory_links l WHERE l.link_state='active' AND (l.source_object_id=o.object_id AND l.target_object_id=? OR l.target_kind='memory_object' AND l.target_object_id=o.object_id AND l.source_object_id=?))"); params.extend([args["linked_to_object_id"], args["linked_to_object_id"]])
        if args.get("tag_object_ids"):
            tags = args["tag_object_ids"]; where.append("EXISTS (SELECT 1 FROM live_memory_links l WHERE l.link_state='active' AND l.relation_type='tagged_with' AND l.source_object_id=o.object_id AND l.target_object_id IN (%s))" % ",".join("?" for _ in tags)); params.extend(tags)
        if args.get("source_slot_after") is not None or args.get("source_slot_before") is not None:
            start = int(args.get("source_slot_after", -1)); end = int(args.get("source_slot_before", 2**31-1))
            where.append("EXISTS (SELECT 1 FROM live_memory_links l WHERE l.source_object_id=o.object_id AND l.relation_type='sourced_from' AND l.target_kind='event_slot' AND l.link_state='active' AND l.slot_seq_start <= ? AND l.slot_seq_end >= ?)"); params.extend([end, start])
        join, prefix, score = "", "", ""
        query_params = []
        if terms:
            connection.create_function("maintenance_casefold", 1, lambda value: (value or "").casefold(), deterministic=True)
            matches = []
            for index, term in enumerate(terms):
                if len(term) >= 3:
                    matches.append(f"SELECT object_id, {index} AS term_id FROM {ltm_store.LTM_FTS_TABLE} WHERE {ltm_store.LTM_FTS_TABLE} MATCH ?")
                    query_params.append('"' + term.replace('"', '""') + '"')
                else:
                    matches.append(f"SELECT object_id, {index} AS term_id FROM {ltm_store.LTM_FTS_TABLE} WHERE " +
                                   " OR ".join(f"instr(maintenance_casefold({field}), ?) > 0" for field in ("title", "summary", "payload_body")))
                    query_params.extend([term] * 3)
            prefix = "WITH term_hits AS (" + " UNION ALL ".join(matches) + "), query_hits AS (SELECT object_id, COUNT(DISTINCT term_id) AS matched_query_term_count FROM term_hits GROUP BY object_id) "
            join = " JOIN query_hits q ON q.object_id=o.object_id"
            score = ", q.matched_query_term_count"
        rows = connection.execute(prefix + """SELECT o.*, d.successful_use_count, d.gain_link_count, d.loss_link_count, d.neutral_link_count,
                    d.strength, d.strength_formula_version, d.updated_at AS derived_updated_at
               """ + score + " FROM live_memory_objects o JOIN live_memory_object_derived d ON d.object_id=o.object_id" + join +
               " WHERE " + " AND ".join(where) + " ORDER BY " + ("q.matched_query_term_count DESC, " if terms else "") +
               "d.strength DESC, o.created_at DESC, o.object_id LIMIT ?", (*query_params, *params, limit)).fetchall()
        items = [_projection(connection, row, projection, root) for row in rows]
        if terms:
            for item, row in zip(items, rows):
                item["matched_query_term_count"] = row["matched_query_term_count"]
        return {"status": "ok", "items": items, "limit": limit}
    finally:
        connection.close()


def get_links(args=None, root=None):
    args = dict(args or {}); limit = min(int(args.get("limit", 50)), CAPS["links"])
    connection = _connection(root)
    try:
        where, params = ["1=1"], []
        if not args.get("include_inactive", False): where.append("l.link_state='active'")
        for field, column in (("relation_types", "relation_type"), ("source_object_ids", "source_object_id"), ("target_object_ids", "target_object_id")):
            values = args.get(field)
            if values: where.append(f"l.{column} IN ({','.join('?' for _ in values)})"); params.extend(values)
        if args.get("endpoint_object_ids"):
            values = args["endpoint_object_ids"]; where.append("(l.source_object_id IN (%s) OR l.target_object_id IN (%s))" % (",".join("?" for _ in values), ",".join("?" for _ in values))); params.extend(values + values)
        rows = connection.execute("SELECT l.* FROM live_memory_links l WHERE " + " AND ".join(where) + " ORDER BY l.created_at, l.link_id LIMIT ?", (*params, limit)).fetchall()
        return {"status": "ok", "items": [dict(row) for row in rows], "limit": limit}
    finally: connection.close()


def traverse_objects(args=None, root=None):
    args = dict(args or {}); starts = args.get("start_object_ids") or []
    if not starts: return _error("start_object_ids", "at least one start object is required")
    depth, nodes = min(int(args.get("max_depth", 2)), CAPS["depth"]), min(int(args.get("max_nodes", 50)), CAPS["nodes"])
    visited, frontier = set(starts), list(starts)
    connection = _connection(root)
    try:
        for _ in range(depth):
            if not frontier or len(visited) >= nodes: break
            marks = ",".join("?" for _ in frontier)
            rows = connection.execute("SELECT source_object_id, target_object_id FROM live_memory_links WHERE link_state='active' AND target_kind='memory_object' AND (source_object_id IN (%s) OR target_object_id IN (%s))" % (marks, marks), frontier + frontier).fetchall()
            frontier = []
            for row in rows:
                other = row["target_object_id"] if row["source_object_id"] in visited else row["source_object_id"]
                if other not in visited and len(visited) < nodes: visited.add(other); frontier.append(other)
        return get_object({"object_ids": sorted(visited), "limit": nodes, "projection": args.get("projection", "core"), "min_strength": args.get("min_strength", 0)}, root)
    finally: connection.close()


def read_event_slots(args=None, root=None):
    args = dict(args or {})
    start = int(args["start_slot_seq"]) if "start_slot_seq" in args else ltm_store.get_maintenance_runtime_state(root)["maintenance_coverage_cursor"] + 1
    limit = min(int(args.get("limit", 20)), CAPS["slots"])
    targets = event_stream.load_active_slot_range(start, limit, root)
    before, after = min(int(args.get("context_before", 2 if args.get("include_adjacent_context", True) else 0)), CAPS["context"]), min(int(args.get("context_after", 0)), CAPS["context"])
    context = event_stream.load_active_slot_range(max(0, start - before), before, root) if before else []
    if after and targets: context += event_stream.load_active_slot_range(targets[-1]["slot_seq"] + 1, after, root)
    result = {"status": "ok", "target_slots": targets, "context_slots": context,
              "coverage_eligible_slot_seqs": [x["slot_seq"] for x in targets]}
    if args.get("include_existing_sourced_objects", True) and targets:
        connection = _connection(root)
        try:
            first, last = targets[0]["slot_seq"], targets[-1]["slot_seq"]
            links = connection.execute("""SELECT * FROM live_memory_links WHERE link_state='active'
                AND relation_type='sourced_from' AND target_kind='event_slot'
                AND slot_seq_start <= ? AND slot_seq_end >= ? ORDER BY created_at, link_id""", (last, first)).fetchall()
            object_ids = sorted({row["source_object_id"] for row in links})
            result["existing_sourced_links"] = [dict(row) for row in links]
            result["existing_sourced_objects"] = get_object({"object_ids": object_ids, "limit": CAPS["objects"], "projection": "core"}, root)["items"] if object_ids else []
        finally:
            connection.close()
    return result


def get_overall_health(args=None, root=None):
    args = dict(args or {}); result = {"status": "ok", "runtime_state": ltm_store.get_maintenance_runtime_state(root)}
    connection = ltm_store._connect(root, read_only=True)
    if connection is None: return {"status": "not_initialized", **result}
    try:
        result["derived"] = {"missing": connection.execute("SELECT COUNT(*) FROM live_memory_objects o LEFT JOIN live_memory_object_derived d ON d.object_id=o.object_id WHERE d.object_id IS NULL").fetchone()[0], "stale_formula": connection.execute("SELECT COUNT(*) FROM live_memory_object_derived WHERE strength_formula_version != ?", (ltm_store.STRENGTH_FORMULA_VERSION,)).fetchone()[0]}
        result["links"] = {"dangling_active": connection.execute("SELECT COUNT(*) FROM memory_links l LEFT JOIN memory_objects s ON s.object_id=l.source_object_id LEFT JOIN memory_objects t ON t.object_id=l.target_object_id WHERE l.link_state='active' AND (s.object_id IS NULL OR (l.target_kind='memory_object' AND t.object_id IS NULL))").fetchone()[0]}
        fts_ready = ltm_store._table_exists(connection, ltm_store.LTM_FTS_TABLE)
        store_ids = {row[0] for row in connection.execute("SELECT object_id FROM live_memory_objects")}
        system_owned_ids = {row[0] for row in connection.execute("SELECT object_id FROM system_owned_objects")}
        vector_expected_ids = store_ids - system_owned_ids
        fts_ids = {row[0] for row in connection.execute(f"SELECT object_id FROM {ltm_store.LTM_FTS_TABLE}")} if fts_ready else set()
        result["fts"] = {"initialized": fts_ready, "object_count": len(fts_ids), "store_count": len(store_ids), "missing_objects": len(store_ids - fts_ids), "unknown_objects": len(fts_ids - store_ids)}
        result["event_stream"] = event_stream.check_integrity(root) if event_stream.event_stream_db_path(root).exists() else {"status": "not_initialized"}
        adapter = vector_index.get_vector_index(root)
        vector_ids = set(adapter.object_ids())
        vector_state = adapter.status()
        vector_state.update({"expected_object_count": len(vector_expected_ids), "missing_objects": len(vector_expected_ids - vector_ids), "unknown_objects": len(vector_ids - vector_expected_ids)})
        result["vector"] = vector_state
        result["recent_reports"] = [dict(row) for row in connection.execute("SELECT maintenance_run_id, finished_at, coverage_status, processed_slot_count, final_check_status, semantic_digest_status, semantic_digest_id, semantic_digest_token_count FROM maintenance_reports ORDER BY finished_at DESC LIMIT 5")]
        return result
    finally: connection.close()


OBSERVATION_TOOLS = {"get_object": get_object, "get_links": get_links, "traverse_objects": traverse_objects, "read_event_slots": read_event_slots, "get_overall_health": get_overall_health}

def _parameter(kind, description, **constraints):
    return {"type": [kind, "null"], "description": description, **constraints}


_ID = _parameter("string", "Exact MemoryObject ID.", minLength=1)
_IDS = _parameter("array", "Exact MemoryObject IDs, not search keywords.", items={"type":"string", "minLength":1}, minItems=1)
_PROJECTION = _parameter("string", "title_summary: titles/summaries; core: adds timestamps; payload: includes body; full: payload and derived metadata; bundle: full plus neighborhood and sources. Default core.", enum=[None,"title_summary","core","payload","full","bundle"])
_STRENGTH = _parameter("number", "Inclusive derived strength bound.", minimum=0, maximum=1)
_LIMIT = _parameter("integer", "Positive result limit, capped by projection: objects 50, payload/full 10, bundle 1; links 100; slots 20. Defaults: objects/slots 20, links 50.", minimum=1)
OBSERVATION_PARAMETERS = {
    "get_object": {
        "query": _parameter("string", "Ordinary whitespace-separated literal OR keywords, not IDs or FTS syntax. Distinct matched terms rank first.", minLength=1),
        "object_id": _ID, "object_ids": _IDS,
        "title": _parameter("string", "Case-insensitive title substring.", minLength=1),
        "min_strength": _STRENGTH, "max_strength": _STRENGTH,
        "linked_to_object_id": _ID, "tag_object_ids": _IDS,
        "source_slot_after": _parameter("integer", "Inclusive source interval lower bound (intersection).", minimum=0),
        "source_slot_before": _parameter("integer", "Inclusive source interval upper bound (intersection).", minimum=0),
        "projection": _PROJECTION, "limit": _LIMIT,
    },
    "get_links": {
        "relation_types": _parameter("array", "Match any listed relation.", minItems=1, items={"type":"string", "enum":["sourced_from","supersedes","supports","contradicts","tagged_with"]}),
        "source_object_ids": _IDS, "target_object_ids": _IDS, "endpoint_object_ids": _IDS,
        "include_inactive": _parameter("boolean", "Include inactive links; default false."), "limit": _LIMIT,
    },
    "traverse_objects": {
        "start_object_ids": {**_IDS, "type":"array"},
        "max_depth": _parameter("integer", "Both-direction active-link traversal depth, default 2, capped at 3.", minimum=0),
        "max_nodes": _parameter("integer", "Traversal node budget, default 50, capped at 100; returned projection retains object caps.", minimum=1),
        "projection": _PROJECTION, "min_strength": _STRENGTH,
    },
    "read_event_slots": {
        "start_slot_seq": _parameter("integer", "First target slot. Defaults to coverage cursor + 1; repeated default calls do not advance. Use last returned target slot + 1 for the next page.", minimum=0),
        "limit": _LIMIT,
        "context_before": _parameter("integer", "Preceding context count, capped at 5; default 2 when adjacent context enabled, otherwise 0.", minimum=0),
        "context_after": _parameter("integer", "Following context count, capped at 5; default 0.", minimum=0),
        "include_adjacent_context": _parameter("boolean", "Default true; explicit context counts override this default."),
        "include_existing_sourced_objects": _parameter("boolean", "Include existing sourced objects and links; default true."),
    },
    "get_overall_health": {},
}


def _validate_value(value, schema):
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    valid = {"string": isinstance(value,str), "array": isinstance(value,list),
             "integer": type(value) is int, "number": type(value) in (int,float),
             "boolean": type(value) is bool, "null": value is None}
    if not any(valid.get(k,False) for k in kinds): return "invalid type"
    if value is None: return None
    if "enum" in schema and value not in schema["enum"]: return "invalid enum value"
    if "minimum" in schema and not value >= schema["minimum"]: return "below minimum"
    if "maximum" in schema and not value <= schema["maximum"]: return "above maximum"
    if "minLength" in schema and len(value.strip()) < schema["minLength"]: return "empty string"
    if "minItems" in schema and len(value) < schema["minItems"]: return "empty array"
    if isinstance(value,list):
        for index,item in enumerate(value):
            error = _validate_value(item,schema["items"])
            if error: return f"item {index}: {error}"
    return None


def _validated_observation(name, handler):
    @wraps(handler)
    def call(args=None, root=None):
        fields = OBSERVATION_PARAMETERS[name]
        def reject(path, message):
            return {"status":"rejected", "error":{"code":"invalid_arguments", "path":path,
                    "message":message, "allowed_fields":list(fields)}}
        if args is not None and not isinstance(args,dict): return reject("$", "expected object")
        args = dict(args or {})
        for key,value in args.items():
            if key not in fields: return reject(key, "unknown parameter")
            error = _validate_value(value,fields[key])
            if error: return reject(key,error)
        args = {k:v for k,v in args.items() if v is not None}
        if name == "traverse_objects" and "start_object_ids" not in args: return reject("start_object_ids","required nonempty array")
        if "object_id" in args and "object_ids" in args: return reject("object_ids","cannot combine with object_id")
        for low,high in (("min_strength","max_strength"),("source_slot_after","source_slot_before")):
            if low in args and high in args and args[low]>args[high]: return reject(low,f"must not exceed {high}")
        return handler(args,root)
    return call


for _name, _handler in list(OBSERVATION_TOOLS.items()):
    globals()[_name] = OBSERVATION_TOOLS[_name] = _validated_observation(_name, _handler)

MAINTAINER_OBSERVATION_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": description,
     "parameters": {"type": "object", "properties": OBSERVATION_PARAMETERS[name], "required": ["start_object_ids"] if name == "traverse_objects" else [], "additionalProperties": False}}}
    for name, description in (
        ("get_object", "Read MemoryObjects by ordinary keyword OR search, graph, source-slot, and strength filters. Query results include matched_query_term_count and rank by distinct matched terms before strength."),
        ("get_links", "Read MemoryLinks with sparse endpoint, relation, and active-state filters."),
        ("traverse_objects", "Traverse active MemoryLinks from supplied object ids."),
        ("read_event_slots", "Read bounded Event Stream target slots and optional surrounding context."),
        ("get_overall_health", "Read mechanical LTM consistency state without making repairs."),
    )
]


def maintainer_observation_tool_schemas():
    """Schemas bound only to the Maintainer runtime profile, never Main Agent tools."""
    return list(MAINTAINER_OBSERVATION_TOOL_SCHEMAS)

def run_maintenance_observation_tool(tool_name, args=None, root=None):
    tool = OBSERVATION_TOOLS.get(tool_name)
    if tool is None: return _error("unknown_tool", tool_name)
    try: return tool(args, root)
    except (ValueError, TypeError) as error: return _error("invalid_arguments", str(error))
    except Exception as error: return _error("observation_failed", str(error))
