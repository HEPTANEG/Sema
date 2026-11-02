"""Execute Memory Push retrieval channels and build the shared Candidate Pool."""

from . import ltm_retrieval, ltm_store
from . import candidate_generation


CHANNEL_ORDER = ("general_semantic", "latest_semantic", "entity")


def build_candidate_pool(compiled_queries, *, profile=None, root=None, _diagnostic_profiler=None):
    """Thin Push wrapper around the neutral Candidate Generation core.

    Push still owns channel compilation, routing, caching, and its historical
    entity-only lexical policy.  The core only executes the supplied jobs.
    """
    profile = profile or ltm_retrieval.MEMORY_PUSH_PROFILE
    channels = compiled_queries.get("channels") if isinstance(compiled_queries, dict) else None
    hypotheses = []
    query_ids = {}
    if isinstance(channels, list):
        for channel_result in channels:
            if not isinstance(channel_result, dict) or channel_result.get("channel") not in CHANNEL_ORDER:
                continue
            channel = channel_result["channel"]
            for query in channel_result.get("queries") or []:
                if not isinstance(query, str) or not query.strip():
                    continue
                query = query.strip()
                query_id = query_ids.setdefault(query, f"q_{len(query_ids) + 1:04d}")
                hypotheses.append({
                    "query": query,
                    "hypothesis": channel,
                    "query_id": query_id,
                    "lexical": channel == "entity",
                })
    result = candidate_generation.build_candidate_pool(
        hypotheses, profile=profile, root=root, diagnostic_profiler=_diagnostic_profiler,
    )
    result["items"] = [item for item in result.get("items", [])
                       if (item.get("strength") or 0) >= ltm_store.PUSH_STRENGTH_THRESHOLD]

    for item in result.get("items", []):
        evidence = item.get("retrieval_evidence") or {}
        evidence["channels"] = evidence.pop("hypotheses", [])
        evidence["channels"].sort(key=lambda value: CHANNEL_ORDER.index(value) if value in CHANNEL_ORDER else len(CHANNEL_ORDER))
        item["retrieval_evidence"] = evidence
    for item in result.get("query_evidence", []):
        item["channel"] = item.pop("hypothesis", "")
    return result
