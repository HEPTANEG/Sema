"""Memory Push Rank 1."""

from collections import defaultdict
import math


RRF_SMOOTHING = 60
DEFAULT_MAX_ITEMS = 400
VALID_SOURCES = frozenset({"embedding", "lexical"})


def _error(code, message, **fields):
    result = {"code": code, "message": message}
    result.update(fields)
    return result


def _valid_rank(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_evidence(record):
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("object_id"), str) or not record["object_id"]:
        return False
    if not isinstance(record.get("query_id"), str) or not record["query_id"]:
        return False
    if not isinstance(record.get("channel"), str) or not record["channel"]:
        return False
    if record.get("source") not in VALID_SOURCES:
        return False
    if not _valid_rank(record.get("source_rank")):
        return False
    raw_score = record.get("raw_score")
    return raw_score is None or (
        isinstance(raw_score, (int, float))
        and not isinstance(raw_score, bool)
        and math.isfinite(float(raw_score))
    )


def _strength_weight(value):
    """Return the fixed Memory Push weight for a derived strength value."""
    if value is None:


        return 1.0
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return 0.0
    strength = float(value)
    if strength <= 0:
        return 0.0
    if strength < 0.4:
        return 0.5
    if strength <= 0.7:
        return 1.0
    return 1.25


def rank_1_candidate_pool(candidate_pool_result, *, max_items=DEFAULT_MAX_ITEMS):
    """Run Rank 1 using source-local ranks and deterministic ties."""
    if not isinstance(candidate_pool_result, dict):
        return {
            "status": "error",
            "items": [],
            "query_evidence": [],
            "errors": [_error("invalid_candidate_pool", "Candidate Pool must be an object.")],
        }

    try:
        requested_limit = int(max_items)
    except (TypeError, ValueError):
        requested_limit = DEFAULT_MAX_ITEMS
    if requested_limit < 1:
        requested_limit = DEFAULT_MAX_ITEMS
    requested_limit = min(requested_limit, DEFAULT_MAX_ITEMS)

    evidence_by_object = defaultdict(list)
    errors = list(candidate_pool_result.get("errors", []))
    for index, record in enumerate(candidate_pool_result.get("query_evidence", [])):




        if isinstance(record, dict) and not record.get("channel") and record.get("hypothesis"):
            record = {**record, "channel": record.get("hypothesis")}
        if not _valid_evidence(record):
            errors.append(_error(
                "invalid_query_evidence",
                "Malformed Query-level Evidence was ignored.",
                index=index,
            ))
            continue
        key = (
            record["query_id"],
            record.get("channel"),
            record["source"],
            record["object_id"],
        )
        if any(
            existing["query_id"] == key[0]
            and existing.get("channel") == key[1]
            and existing["source"] == key[2]
            and existing["object_id"] == key[3]
            for existing in evidence_by_object[record["object_id"]]
        ):
            continue
        evidence_by_object[record["object_id"]].append(record)

    ranked = []
    for candidate in candidate_pool_result.get("items", []):
        if not isinstance(candidate, dict) or not candidate.get("object_id"):
            errors.append(_error(
                "invalid_candidate",
                "Malformed Candidate Pool item was ignored.",
            ))
            continue
        object_id = candidate["object_id"]
        strength_weight = _strength_weight(candidate.get("strength"))
        if strength_weight <= 0:
            continue
        records = evidence_by_object.get(object_id, [])
        if not records:
            errors.append(_error(
                "candidate_without_evidence",
                "Candidate has no valid Query-level Evidence and was excluded.",
                object_id=object_id,
            ))
            continue

        best_ranks = {
            source: min(
                record["source_rank"]
                for record in records
                if record["source"] == source
            )
            for source in VALID_SOURCES
            if any(record["source"] == source for record in records)
        }
        embedding_part = (
            1 / (RRF_SMOOTHING + best_ranks["embedding"])
            if "embedding" in best_ranks
            else 0.0
        )
        lexical_part = (
            1 / (RRF_SMOOTHING + best_ranks["lexical"])
            if "lexical" in best_ranks
            else 0.0
        )
        base_score = embedding_part + lexical_part
        ranked.append({
            **candidate,
            "rank_1_score": base_score * strength_weight,
            "strength_weight": strength_weight,
            "_source_count": len({record["source"] for record in records}),
            "_channel_count": len({record.get("channel") for record in records}),
            "_query_coverage": len({record["query_id"] for record in records}),
        })

    ranked.sort(key=lambda item: (
        -item["rank_1_score"],
        -item["_source_count"],
        -item["_channel_count"],
        -item["_query_coverage"],
        item["object_id"],
    ))
    ranked = ranked[:requested_limit]
    items = []
    for position, item in enumerate(ranked, start=1):
        item = dict(item)
        item.pop("_source_count", None)
        item.pop("_channel_count", None)
        item.pop("_query_coverage", None)
        item["rank_1_position"] = position
        items.append(item)

    if errors and items:
        status = "partial"
    elif errors:
        status = "error"
    else:
        status = "ok"
    return {
        "status": status,
        "items": items,
        "query_evidence": list(candidate_pool_result.get("query_evidence", [])),
        "errors": errors,
    }
