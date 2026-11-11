"""Deterministic compilation of Maintainer semantic intents into Store plans."""

from collections import Counter

from . import ltm_store


INTENT_TYPES = {
    "replace",
    "merge",
    "split",
    "abstract",
    "retag",
    "relink",
    "mark_contradiction",
    "resolve_contradiction",
    "mark_support",
    "update_title",
    "update_summary",
}
ATOMIC_TYPES = {"create_object", "create_link", "deactivate_link", "update_title", "update_summary"}
PLAN_TOP_LEVEL_KEYS = {"intents", "atomic_operations"}
COMMON_INTENT_KEYS = {"intent_id", "intent_type"}


def _string(description=""):
    result = {"type": "string"}
    if description:
        result["description"] = description
    return result


def _string_array(description=""):
    result = {"type": "array", "items": {"type": "string"}}
    if description:
        result["description"] = description
    return result


def _object_ref_schema():
    return {
        "type": "object",
        "properties": {
            "object_id": _string("Existing MemoryObject id."),
            "temp_id": _string("Temp id created earlier in the same plan."),
        },
        "additionalProperties": False,
        "anyOf": [{"required": ["object_id"]}, {"required": ["temp_id"]}],
    }


def episode_structured_data_schema():
    return {
        "oneOf": [
            {"type": "null"},
            {
                "type": "object",
                "properties": {
                    "time_scope": {
                        "type": "object",
                        "properties": {
                            "start": {},
                            "end": {},
                            "anchor": {},
                            "precision": {"type": "string", "enum": ["exact", "approximate", "relative"]},
                        },
                        "additionalProperties": False,
                        "anyOf": [
                            {"required": ["start"]},
                            {"required": ["end"]},
                            {"required": ["anchor"]},
                        ],
                    },
                },
                "additionalProperties": False,
                "anyOf": [{"maxProperties": 0}, {"required": ["time_scope"]}],
            },
        ]
    }


def _empty_or_null_structured_data_schema():
    return {
        "oneOf": [
            {"type": "null"},
            {"type": "object", "maxProperties": 0, "additionalProperties": False},
        ]
    }


def _payload_schema(structured_data_schema=None):
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "enum": [1]},
            "body": {"type": "string"},
            "structured_data": structured_data_schema or {"type": ["object", "null"], "additionalProperties": True},
        },
        "required": ["schema_version"],
        "additionalProperties": False,
        "anyOf": [{"required": ["body"]}, {"required": ["structured_data"]}],
    }


def _new_object_schema():
    common = {
        "temp_id": _string("Plan-local temp id for this new object."),
        "title": _string("Short MemoryObject index label. Use 1-50 Runtime-counted tokens; put detailed facts in the payload."),
        "summary": {
            "type": ["string", "null"],
            "description": (
                "For payloads at or below 2048 UTF-8 bytes, use null. "
                "For payloads above 2048 and at or below 8192 bytes, provide "
                "a concise summary. Payloads above 8192 bytes must be split."
            ),
        },
    }
    required = ["temp_id", "object_type", "title", "payload"]
    return {
        "type": "object",
        "properties": {**common, "object_type": {"type": "string", "enum": sorted(ltm_store.OBJECT_TYPES)}, "payload": _payload_schema()},
        "required": required,
        "additionalProperties": False,
        "oneOf": [
            {
                "type": "object",
                "properties": {**common, "object_type": {"type": "string", "enum": ["assertion", "tag"]}, "payload": _payload_schema(_empty_or_null_structured_data_schema())},
                "required": required,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {**common, "object_type": {"type": "string", "enum": ["episode"]}, "payload": _payload_schema(episode_structured_data_schema())},
                "required": required,
                "additionalProperties": False,
            },
        ]
    }


def _event_slots_schema():
    return {
        "type": "object",
        "properties": {
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 0},
        },
        "required": ["start"],
        "additionalProperties": False,
    }


def _link_target_schema():
    return {
        "type": "object",
        "properties": {
            "object_id": _string("Existing MemoryObject id."),
            "temp_id": _string("Temp id created earlier in the same plan."),
            "event_slots": _event_slots_schema(),
        },
        "additionalProperties": False,
        "oneOf": [{"required": ["object_id"]}, {"required": ["temp_id"]}, {"required": ["event_slots"]}],
    }


def _link_schema():
    return {
        "type": "object",
        "properties": {
            "source": _object_ref_schema(),
            "relation_type": {
                "type": "string",
                "enum": sorted(ltm_store.RELATION_TYPES),
                "description": (
                    "Endpoint contract: sourced_from is MemoryObject -> Event Stream slot; "
                    "tagged_with is MemoryObject -> tag; supports and contradicts require "
                    "an assertion or episode source and an assertion target; supersedes is MemoryObject -> "
                    "MemoryObject. Runtime validates existing object IDs and temp IDs."
                ),
            },
            "target": _link_target_schema(),
        },
        "required": ["source", "relation_type", "target"],
        "additionalProperties": False,
    }


def _intent_schema(intent_type, properties, required):
    return {
        "type": "object",
        "properties": {
            "intent_id": _string("Optional debug/reference id. Not trusted for idempotency."),
            "intent_type": {"type": "string", "enum": [intent_type]},
            **properties,
        },
        "required": ["intent_type", *required],
        "additionalProperties": False,
    }


def _atomic_schema(operation_type, properties, required):
    return {
        "type": "object",
        "properties": {
            "operation_type": {"type": "string", "enum": [operation_type]},
            **properties,
        },
        "required": ["operation_type", *required],
        "additionalProperties": False,
    }


def maintenance_action_plan_schema():
    """Return the model-visible strict JSON schema for submit_maintenance_plan."""
    intent_items = [
        _intent_schema("replace", {"new_object": _new_object_schema(), "old_object_id": _string()}, ["new_object", "old_object_id"]),
        _intent_schema("merge", {"new_object": _new_object_schema(), "merged_object_ids": _string_array()}, ["new_object", "merged_object_ids"]),
        _intent_schema("split", {"old_object_id": _string(), "new_objects": {"type": "array", "items": _new_object_schema(), "minItems": 1}}, ["old_object_id", "new_objects"]),
        _intent_schema("abstract", {"relation_type": {"type": "string", "enum": ["tagged_with"], "description": "Create a Tag and link every source object directly to that Tag. Sources may be any live MemoryObject type."}, "new_object": {**_new_object_schema(), "properties": {**_new_object_schema()["properties"], "object_type": {"type": "string", "enum": ["tag"]}}}, "source_object_ids": {**_string_array("Live MemoryObject IDs to tag directly."), "minItems": 1}}, ["relation_type", "new_object", "source_object_ids"]),
        _intent_schema("abstract", {"relation_type": {"type": "string", "enum": ["supports"], "description": "Create an assertion supported by source assertions or episodes. Each source must be an assertion or episode; Tags are not valid sources."}, "new_object": {**_new_object_schema(), "properties": {**_new_object_schema()["properties"], "object_type": {"type": "string", "enum": ["assertion"]}}}, "source_object_ids": {**_string_array("IDs of existing source assertions or episodes. Do not use Tag IDs."), "minItems": 1}}, ["relation_type", "new_object", "source_object_ids"]),
        _intent_schema("retag", {"object_id": _string(), "add_tag_ids": _string_array(), "deactivate_tag_link_ids": _string_array()}, ["object_id"]),
        _intent_schema("relink", {"deactivate_link_ids": _string_array(), "create_links": {"type": "array", "items": _link_schema()}}, []),
        _intent_schema("mark_contradiction", {"source_object_id": _string(), "target_object_id": _string()}, ["source_object_id", "target_object_id"]),
        _intent_schema("resolve_contradiction", {"contradiction_link_ids": _string_array(), "resolution_links": {"type": "array", "items": _link_schema()}}, ["contradiction_link_ids"]),
        _intent_schema("mark_support", {"source_object_id": _string(), "target_object_id": _string()}, ["source_object_id", "target_object_id"]),
        _intent_schema("update_title", {"object_id": _string(), "new_title": _string("Short replacement title of 1-50 Runtime-counted tokens.")}, ["object_id", "new_title"]),
        _intent_schema("update_summary", {"object_id": _string(), "new_summary": {"type": ["string", "null"]}}, ["object_id", "new_summary"]),
    ]
    atomic_items = [
        _atomic_schema("create_object", {"new_object": _new_object_schema()}, ["new_object"]),
        _atomic_schema("create_link", {"link": _link_schema()}, ["link"]),
        _atomic_schema("deactivate_link", {"link_id": _string()}, ["link_id"]),
        _atomic_schema("update_title", {"object_id": _string(), "new_title": _string("Short replacement title of 1-50 Runtime-counted tokens.")}, ["object_id", "new_title"]),
        _atomic_schema("update_summary", {"object_id": _string(), "new_summary": {"type": ["string", "null"]}}, ["object_id", "new_summary"]),
    ]
    return {
        "type": "object",
        "properties": {
            "intents": {"type": "array", "items": {"oneOf": intent_items}, "minItems": 1},
            "atomic_operations": {"type": "array", "items": {"oneOf": atomic_items}, "minItems": 1},
        },
        "additionalProperties": False,
        "anyOf": [{"required": ["intents"]}, {"required": ["atomic_operations"]}],
    }


INTENT_ALLOWED_KEYS = {
    "replace": COMMON_INTENT_KEYS | {"new_object", "old_object_id"},
    "merge": COMMON_INTENT_KEYS | {"new_object", "merged_object_ids"},
    "split": COMMON_INTENT_KEYS | {"old_object_id", "new_objects"},
    "abstract": COMMON_INTENT_KEYS | {"relation_type", "new_object", "source_object_ids"},
    "retag": COMMON_INTENT_KEYS | {"object_id", "add_tag_ids", "deactivate_tag_link_ids"},
    "relink": COMMON_INTENT_KEYS | {"deactivate_link_ids", "create_links"},
    "mark_contradiction": COMMON_INTENT_KEYS | {"source_object_id", "target_object_id"},
    "resolve_contradiction": COMMON_INTENT_KEYS | {"contradiction_link_ids", "resolution_links"},
    "mark_support": COMMON_INTENT_KEYS | {"source_object_id", "target_object_id"},
    "update_title": COMMON_INTENT_KEYS | {"object_id", "new_title"},
    "update_summary": COMMON_INTENT_KEYS | {"object_id", "new_summary"},
}
ATOMIC_ALLOWED_KEYS = {
    "create_object": {"operation_type", "new_object"},
    "create_link": {"operation_type", "link"},
    "deactivate_link": {"operation_type", "link_id"},
    "update_title": {"operation_type", "object_id", "new_title"},
    "update_summary": {"operation_type", "object_id", "new_summary"},
}


class CompileError(ValueError):
    pass


def _new_object(spec, mapping, objects):
    if not isinstance(spec, dict) or not isinstance(spec.get("temp_id"), str) or not spec["temp_id"]:
        raise CompileError("new_object requires a non-empty temp_id")
    temp_id = spec["temp_id"]
    if temp_id in mapping:
        raise CompileError(f"duplicate temp_id: {temp_id}")
    object_id = ltm_store._new_id("mem")
    mapping[temp_id] = object_id
    object_spec = {key: spec.get(key) for key in ("object_type", "title", "summary", "payload")}
    object_spec["object_id"] = object_id
    objects.append(object_spec)
    return object_id


def _ref(value, mapping):
    if isinstance(value, str): return value
    if isinstance(value, dict):
        if value.get("object_id"): return value["object_id"]
        if value.get("temp_id") in mapping: return mapping[value["temp_id"]]
    raise CompileError("object reference is missing or unresolved")


def _link(spec, mapping, links):
    if not isinstance(spec, dict): raise CompileError("link must be an object")
    source = _ref(spec.get("source") or spec.get("source_object_id"), mapping)
    target = spec.get("target")
    if target is None and spec.get("target_object_id"):
        target = {"object_id": spec["target_object_id"]}
    if not isinstance(target, dict): raise CompileError("link target is required")
    if target.get("object_id") or target.get("temp_id"):
        target = {"object_id": _ref(target, mapping)}
    elif "event_slots" not in target:
        raise CompileError("link target must be an object or event_slots")
    links.append({"source": {"object_id": source}, "relation_type": spec.get("relation_type"), "target": target})


def _intent(intent, mapping, objects, links, deactivations, updates, counters, root=None):
    kind = intent.get("intent_type")
    if kind not in INTENT_TYPES: raise CompileError(f"unsupported intent_type: {kind}")
    unknown = sorted(set(intent) - INTENT_ALLOWED_KEYS[kind])
    if unknown: raise CompileError(f"unsupported fields for intent_type {kind}: {', '.join(unknown)}")
    counters[kind] += 1
    if kind == "replace":
        new_id = _new_object(intent.get("new_object"), mapping, objects); _link({"source": new_id, "relation_type": "supersedes", "target_object_id": intent.get("old_object_id")}, mapping, links)
    elif kind == "merge":
        new_id = _new_object(intent.get("new_object"), mapping, objects)
        for old_id in intent.get("merged_object_ids") or []: _link({"source": new_id, "relation_type": "supersedes", "target_object_id": old_id}, mapping, links)
        if not intent.get("merged_object_ids"): raise CompileError("merge requires merged_object_ids")
    elif kind == "split":
        old_id, new_specs = intent.get("old_object_id"), intent.get("new_objects") or []
        if not new_specs: raise CompileError("split requires new_objects")
        for spec in new_specs:
            new_id = _new_object(spec, mapping, objects); _link({"source": new_id, "relation_type": "supersedes", "target_object_id": old_id}, mapping, links)
    elif kind == "abstract":
        relation = intent.get("relation_type")
        if relation not in {"tagged_with", "supports"}:
            raise CompileError("abstract relation_type must be tagged_with or supports")
        new_spec = intent.get("new_object") or {}
        expected_type = "tag" if relation == "tagged_with" else "assertion"
        if new_spec.get("object_type") == "episode":
            raise CompileError(
                "abstract cannot create an episode; use atomic_operations with create_object "
                "(object_type=episode) and create_link (relation_type=sourced_from) to its Event Stream slots."
            )
        if new_spec.get("object_type") != expected_type:
            raise CompileError(f"abstract {relation} requires new_object.object_type={expected_type}")
        new_id = _new_object(new_spec, mapping, objects)
        sources = intent.get("source_object_ids") or []
        if not sources: raise CompileError("abstract requires source_object_ids")
        for index, source in enumerate(sources):
            if relation == "supports":
                source_object = ltm_store.get_object(source, root=root)
                if source_object and source_object.get("object_type") not in {"assertion", "episode"}:
                    raise CompileError(
                        f"abstract supports source_object_ids[{index}] is {source_object.get('object_type')}; "
                        "supports requires each source to be an assertion or episode. Tags cannot support assertions."
                    )
            _link({"source_object_id": source, "relation_type": relation, "target_object_id": new_id}, mapping, links)
    elif kind == "retag":
        object_id = _ref(intent.get("object_id"), mapping)
        for tag_id in intent.get("add_tag_ids") or []: _link({"source": object_id, "relation_type": "tagged_with", "target_object_id": tag_id}, mapping, links)
        deactivations.extend(intent.get("deactivate_tag_link_ids") or [])
    elif kind == "relink":
        deactivations.extend(intent.get("deactivate_link_ids") or [])
        for spec in intent.get("create_links") or []: _link(spec, mapping, links)
    elif kind == "mark_contradiction": _link({"source": intent.get("source_object_id"), "relation_type": "contradicts", "target_object_id": intent.get("target_object_id")}, mapping, links)
    elif kind == "mark_support": _link({"source": intent.get("source_object_id"), "relation_type": "supports", "target_object_id": intent.get("target_object_id")}, mapping, links)
    elif kind == "resolve_contradiction":
        deactivations.extend(intent.get("contradiction_link_ids") or [])
        for spec in intent.get("resolution_links") or []: _link(spec, mapping, links)
    elif kind in {"update_title", "update_summary"}:
        updates.append({"field": "title" if kind == "update_title" else "summary", "object_id": _ref(intent.get("object_id"), mapping), "value": intent.get("new_title") if kind == "update_title" else intent.get("new_summary")})


def _atomic(operation, mapping, objects, links, deactivations, updates):
    kind = operation.get("operation_type")
    if kind not in ATOMIC_TYPES: raise CompileError(f"unsupported atomic operation: {kind}")
    unknown = sorted(set(operation) - ATOMIC_ALLOWED_KEYS[kind])
    if unknown: raise CompileError(f"unsupported fields for atomic operation {kind}: {', '.join(unknown)}")
    if kind == "create_object": _new_object(operation.get("new_object"), mapping, objects)
    elif kind == "create_link": _link(operation.get("link"), mapping, links)
    elif kind == "deactivate_link": deactivations.append(operation.get("link_id"))
    else: updates.append({"field": "title" if kind == "update_title" else "summary", "object_id": _ref(operation.get("object_id"), mapping), "value": operation.get("new_title") if kind == "update_title" else operation.get("new_summary")})


def _new_object_specs(plan):
    specs = []
    for intent in plan.get("intents") or []:
        intent_type = intent.get("intent_type") if isinstance(intent, dict) else None
        if intent_type in {"replace", "merge", "abstract"}:
            if isinstance(intent.get("new_object"), dict):
                specs.append(intent["new_object"])
        elif intent_type == "split":
            specs.extend(spec for spec in intent.get("new_objects") or [] if isinstance(spec, dict))
    for operation in plan.get("atomic_operations") or []:
        if isinstance(operation, dict) and operation.get("operation_type") == "create_object":
            if isinstance(operation.get("new_object"), dict):
                specs.append(operation["new_object"])
    return specs


def compile_maintenance_action_plan(plan, maintenance_run_id, plan_index, root=None):
    if not isinstance(plan, dict) or not isinstance(maintenance_run_id, str) or not maintenance_run_id:
        return {"status": "rejected", "errors": [{"code": "invalid_plan", "message": "plan and maintenance_run_id are required."}]}
    unknown = sorted(set(plan) - PLAN_TOP_LEVEL_KEYS)
    if unknown:
        return {"status": "rejected", "errors": [{"code": "compile_error", "message": f"unsupported top-level fields: {', '.join(unknown)}"}]}
    intents, atomics = plan.get("intents") or [], plan.get("atomic_operations") or []
    if not isinstance(intents, list) or not isinstance(atomics, list) or not (intents or atomics):
        return {"status": "rejected", "errors": [{"code": "empty_plan", "message": "intents or atomic_operations are required."}]}
    try:
        mapping, objects, links, deactivations, updates, counters = {}, [], [], [], [], Counter()
        for intent in intents: _intent(intent, mapping, objects, links, deactivations, updates, counters, root=root)
        for operation in atomics: _atomic(operation, mapping, objects, links, deactivations, updates)
        canonical_links, seen_links = [], set()
        for link in links:
            key = repr(link)
            if key not in seen_links: seen_links.add(key); canonical_links.append(link)
        if len(deactivations) != len(set(deactivations)) or any(not isinstance(link_id, str) or not link_id for link_id in deactivations): raise CompileError("duplicate or invalid deactivation")
        seen_updates = {}
        for update in updates:
            key = (update["object_id"], update["field"])
            if key in seen_updates and seen_updates[key] != update["value"]: raise CompileError("conflicting updates for one object field")
            seen_updates[key] = update["value"]
        normalized_updates = [{"object_id": object_id, "field": field, "value": value} for (object_id, field), value in seen_updates.items()]
        created_ids = set(mapping.values())
        referenced_ids = {link["source"]["object_id"] for link in canonical_links}
        referenced_ids.update(link["target"].get("object_id") for link in canonical_links if link["target"].get("object_id"))
        referenced_ids.update(update["object_id"] for update in normalized_updates)
        missing = sorted(object_id for object_id in referenced_ids if object_id not in created_ids and ltm_store.get_object(object_id, root=root) is None)
        if missing: raise CompileError(f"object references not found: {', '.join(missing)}")
        unknown_links = sorted(link_id for link_id in deactivations if ltm_store.get_memory_link(link_id, root=root) is None)
        if unknown_links: raise CompileError(f"link references not found: {', '.join(unknown_links)}")
        plan_id = f"maintenance:{maintenance_run_id}:plan:{int(plan_index)}"
        object_targets = [{"kind": "object", "object_index": index, "target": {"temp_id": spec.get("temp_id")}}
                          for index, spec in enumerate(_new_object_specs(plan))]
        update_targets = [
            {"kind": "update", "update_index": index, "target": {"object_id": update["object_id"]}}
            for index, update in enumerate(normalized_updates)
        ]
        repair_targets = object_targets + update_targets
        title_targets = object_targets + [
            target for target, update in zip(update_targets, normalized_updates)
            if update.get("field") == "title"
        ]
        return {"status": "compiled", "plan_id": plan_id, "maintenance_run_id": maintenance_run_id, "plan_index": int(plan_index), "idempotency_key": plan_id, "objects": objects, "links": canonical_links, "deactivate_link_ids": deactivations, "updates": normalized_updates, "temp_id_map": mapping, "title_targets": title_targets, "repair_targets": repair_targets, "report_counter_hints": dict(counters), "errors": []}
    except (CompileError, TypeError, ValueError) as error:
        return {"status": "rejected", "errors": [{"code": "compile_error", "message": str(error)}]}
