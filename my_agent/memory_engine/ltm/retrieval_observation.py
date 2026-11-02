"""Retrieval's explicit, bounded contract over shared Maintainer reads."""

import copy
import json
import math

from . import ltm_store, maintenance_observation
from .ltm_retrieval import RetrievalProfile
from ..token_counter import count_text_tokens


PROFILE = RetrievalProfile(
    profile_id="memory_retrieval_observation",
    operations=frozenset({"get_object", "get_links", "traverse_objects", "read_event_slots"}),
    object_types=frozenset(ltm_store.OBJECT_TYPES), search_fields=frozenset(),
    projections=frozenset({"title_summary", "core", "payload", "full", "bundle"}),
    relation_types=frozenset(ltm_store.RELATION_TYPES), default_projection="core",
    max_depth=3, max_objects=50, max_payload_objects=10, max_result_tokens=12_000,
)


def _integer(description, minimum=0, maximum=None):
    return {"type": "integer", "description": description, "minimum": minimum,
            **({"maximum": maximum} if maximum is not None else {})}


def _ids(description):
    return {"type": "array", "items": {"type": "string", "minLength": 1},
            "minItems": 1, "maxItems": PROFILE.max_objects, "description": description}


_PROJECTION = {"type": "string", "enum": sorted(PROFILE.projections), "description":
    "Default core: title, summary and metadata, WITHOUT body. Use payload for exact body and structured_data; full adds metadata; bundle reads one object with neighborhood and source-slot ranges."}
_STRENGTH = {"type": "number", "minimum": 0, "description": "Stored derived-strength filter."}
_STRING = {"type": "string", "minLength": 1}
_BOOLEAN = {"type": "boolean"}

CONTRACTS = {
    "get_object": {
        "description": "Read objects using combined filters. Core does not contain body: request projection=payload when verifying exact source content. Payload fields are nested under payload.",
        "properties": {
            "object_id": {**_STRING, "description": "One exact MemoryObject ID."},
            "object_ids": _ids("Exact MemoryObject IDs; omit object_id when using this array."),
            "title": {**_STRING, "description": "Literal case-insensitive title substring."},
            "query": {**_STRING, "description": "SQLite FTS expression; literal/keyword search, not embedding."},
            "min_strength": _STRENGTH, "max_strength": _STRENGTH,
            "linked_to_object_id": {**_STRING, "description": "Objects connected to this object by an active Link."},
            "tag_object_ids": _ids("Objects tagged with any of these tag IDs."),
            "source_slot_after": _integer("Inclusive source-slot overlap lower bound."),
            "source_slot_before": _integer("Inclusive source-slot overlap upper bound."),
            "projection": _PROJECTION,
            "limit": _integer("Maximum objects; payload/full capped at 10, bundle at 1.", 1, 50),
        }, "required": [],
    },
    "get_links": {
        "description": "Read Links. Use endpoint_object_ids for either direction, source_object_ids for outgoing, target_object_ids for incoming. For original Event Slot ranges use source_object_ids plus relation_types=[sourced_from].",
        "properties": {
            "endpoint_object_ids": _ids("Match either Link endpoint."),
            "source_object_ids": _ids("Filter source endpoints (outgoing Links)."),
            "target_object_ids": _ids("Filter target object endpoints (incoming Links)."),
            "relation_types": {"type": "array", "items": {"type": "string", "enum": sorted(PROFILE.relation_types)}, "minItems": 1},
            "include_inactive": {**_BOOLEAN, "description": "Default false: only active Links."},
            "limit": _integer("Maximum Links to return.", 1, 100),
        }, "required": [],
    },
    "traverse_objects": {
        "description": "Traverse active object Links in both directions from start_object_ids. Returns bounded source objects, default core; request payload for body.",
        "properties": {
            "start_object_ids": _ids("Starting MemoryObject IDs."),
            "max_depth": _integer("Maximum graph depth, default 2.", 1, 3),
            "max_nodes": _integer("Maximum visited nodes including starts, default 50.", 1, 50),
            "projection": _PROJECTION, "min_strength": _STRENGTH,
        }, "required": ["start_object_ids"],
    },
    "read_event_slots": {
        "description": "Read a specific Event Stream range. First obtain slot_seq_start/slot_seq_end from sourced_from Links; pass start_slot_seq and limit=end-start+1 (at most 20). This tool does NOT accept object_id, slot_start or slot_end. Follow next_start_slot_seq if the response is partial.",
        "properties": {
            "start_slot_seq": _integer("Required inclusive starting Slot sequence; never inferred from Maintainer coverage."),
            "limit": _integer("Maximum target Slots, default 20.", 1, 20),
            "include_adjacent_context": {**_BOOLEAN, "description": "Default true: two preceding context Slots."},
            "context_before": _integer("Preceding context count; overrides include_adjacent_context.", 0, 5),
            "context_after": _integer("Following context count, default 0.", 0, 5),
            "include_existing_sourced_objects": {**_BOOLEAN, "description": "Default true: include existing objects sourced from the target range."},
        }, "required": ["start_slot_seq"],
    },
}


def tool_schemas():
    return [{"type": "function", "function": {
        "name": name, "description": contract["description"],
        "parameters": {"type": "object", "properties": copy.deepcopy(contract["properties"]),
                       "required": contract["required"], "additionalProperties": False},
    }} for name, contract in CONTRACTS.items()]


def _validate_value(value, schema, path):
    kind = schema["type"]
    valid = ((kind == "string" and isinstance(value, str) and bool(value.strip()))
             or (kind == "boolean" and isinstance(value, bool))
             or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
             or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
             or (kind == "array" and isinstance(value, list)))
    if not valid:
        raise ValueError(f"{path} must be {kind}.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}.")
    if kind in {"integer", "number"}:
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path} is outside its allowed bounds.")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise ValueError(f"{path} has an invalid number of items.")
        for item in value:
            _validate_value(item, schema["items"], path + "[]")


def _arguments(name, args, profile):
    if name not in profile.operations or name not in CONTRACTS:
        raise ValueError(f"{name} is not allowed by the Retrieval observation profile.")
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    contract = CONTRACTS[name]
    unknown = set(args) - set(contract["properties"])
    missing = set(contract["required"]) - set(args)
    if unknown or missing:
        raise ValueError(f"{name}: unknown fields {sorted(unknown)}; missing fields {sorted(missing)}. Allowed fields: {sorted(contract['properties'])}. {contract['description']}")
    for key, value in args.items():
        _validate_value(value, contract["properties"][key], f"{name}.{key}")
    args = dict(args)
    if "object_id" in args and "object_ids" in args:
        raise ValueError("Use object_id or object_ids, not both.")
    if args.get("min_strength", 0) > args.get("max_strength", math.inf):
        raise ValueError("min_strength exceeds max_strength.")
    if args.get("source_slot_after", 0) > args.get("source_slot_before", math.inf):
        raise ValueError("source_slot_after exceeds source_slot_before.")
    if name in {"get_object", "traverse_objects"}:
        projection = args.setdefault("projection", profile.default_projection)
        if projection not in profile.projections:
            raise ValueError("projection is not allowed by the Retrieval profile.")
        cap = profile.max_payload_objects if projection in {"payload", "full", "bundle"} else profile.max_objects
        if projection == "bundle":
            cap = min(cap, 1)
        key = "max_nodes" if name == "traverse_objects" else "limit"
        args[key] = min(args.get(key, 50 if name == "traverse_objects" else 20), cap)
        if name == "traverse_objects":
            if len(args["start_object_ids"]) > args[key]:
                raise ValueError("start_object_ids exceeds the profile's object limit for this projection.")
            args["max_depth"] = min(args.get("max_depth", 2), profile.max_depth)
    return args


def _tokens(result):
    return count_text_tokens(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _bounded(result, profile):
    result = copy.deepcopy(result)
    result.pop("coverage_eligible_slot_seqs", None)
    result["profile_id"] = profile.profile_id
    original_count = len(result.get("target_slots", result.get("items", [])))


    keys = ("context_slots", "existing_sourced_links", "existing_sourced_objects", "items", "target_slots")
    omitted = False
    for key in keys:
        while result.get(key) and _tokens(result) > profile.max_result_tokens:
            item = result[key].pop()
            omitted = True
            result["status"] = "partial"
            result["truncation_reason"] = "token_limit"
            result["returned_count"] = len(result.get("target_slots", result.get("items", [])))
            result["requested_result_count"] = original_count
            if key == "target_slots":
                result["next_start_slot_seq"] = item["slot_seq"]
    if omitted:
        if original_count and not result["returned_count"]:
            return {"status": "error", "items": [], "profile_id": profile.profile_id,
                    "errors": [{"code": "result_item_too_large", "message": "Source content exceeds the observation budget. Request a smaller range or projection; no omitted source content was observed."}]}
    if _tokens(result) > profile.max_result_tokens:
        return {"status": "error", "items": [], "errors": [{"code": "result_too_large", "message": "Observation envelope exceeds the profile budget."}]}
    return result


def run(name, args, *, profile=PROFILE, root=None):
    args = _arguments(name, args, profile)
    function = getattr(maintenance_observation, name)
    result = function(args) if root is None else function(args, root=root)
    if not isinstance(result, dict):
        raise ValueError("Observation returned an invalid result.")
    if name in {"get_object", "traverse_objects"} and result.get("status") in {"ok", "partial"}:

        result["actual_projection"] = args["projection"]
    return _bounded(result, profile)
