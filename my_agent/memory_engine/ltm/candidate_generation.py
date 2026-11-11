"""Neutral Candidate Generation primitives shared by Memory Push and Retrieval.

This module deliberately stops at Candidate Pool construction.  It does not
compile Push channels, rank candidates, or decide what constitutes evidence.
Callers provide their own query hypotheses and keep the returned pool in their
own scope (Push runtime or a Retrieval Session).
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import time

from . import ltm_retrieval


SOURCE_ORDER = ("embedding", "lexical")


def _candidate_from_item(item, query_id, hypothesis, source, source_rank):
    candidate = {
        "object_id": item["object_id"],
        "object_type": item.get("object_type"),
        "title": item.get("title"),
        "summary": item.get("summary"),
        "strength": item.get("strength"),
        "_query_id": query_id,
        "hypothesis": hypothesis,
        "source": source,
        "_source_rank": source_rank,
    }
    if "similarity" in item:
        candidate["score"] = float(item["similarity"])
    elif "_raw_score" in item:
        candidate["score"] = float(item["_raw_score"])
    return candidate


def _lexical_query(query, profile, root=None, diagnostic_profiler=None):
    measure = diagnostic_profiler.measure("lexical_retrieval_ms") if diagnostic_profiler is not None else None
    if measure is None:
        return ltm_retrieval.search_objects(
            {
                "text": query,
                "mode": "full_text",
                "fields": ["title", "summary", "payload_body"],
                "projection": profile.default_projection,
                "limit": profile.max_objects,
            }, profile=profile, root=root, _include_raw_score=True,
        )
    diagnostic_profiler.increment("lexical_query_count")
    with measure:
        return ltm_retrieval.search_objects(
            {
                "text": query,
                "mode": "full_text",
                "fields": ["title", "summary", "payload_body"],
                "projection": profile.default_projection,
                "limit": profile.max_objects,
            }, profile=profile, root=root, _include_raw_score=True,
        )


def _adapt_result(result, query_id, hypothesis, source):
    candidates = []
    for item in result.get("items", []) if isinstance(result, dict) else []:
        if not isinstance(item, dict) or not item.get("object_id"):
            continue
        candidates.append(_candidate_from_item(item, query_id, hypothesis, source, len(candidates) + 1))
    return candidates


def _run_semantic_batch(jobs, profile, root=None, diagnostic_profiler=None, deadline=None):
    queries = [job[2] for job in jobs]
    retrieval_kwargs = {"profile": profile, "root": root}
    if deadline is not None:
        retrieval_kwargs["deadline"] = deadline
    if diagnostic_profiler is None:
        results = ltm_retrieval.semantic_search_many(queries, **retrieval_kwargs)
    else:
        with diagnostic_profiler.measure("semantic_retrieval_ms"):
            retrieval_kwargs["_diagnostic_profiler"] = diagnostic_profiler
            results = ltm_retrieval.semantic_search_many(queries, **retrieval_kwargs)
    return [
        (job, result, _adapt_result(result, job[0], job[1], job[3]))
        for job, result in zip(jobs, results)
    ]


def _run_lexical_job(job, profile, root=None, diagnostic_profiler=None, deadline=None):
    if deadline is not None and time.monotonic() >= deadline:
        raise RuntimeError("Memory Push exceeded its foreground time budget.")
    return _lexical_query(job[2], profile, root=root, diagnostic_profiler=diagnostic_profiler)


def _integrate(candidates):
    merged = {}
    query_evidence = []
    seen_evidence = set()
    for candidate in candidates:
        object_id = candidate["object_id"]
        item = merged.setdefault(object_id, {
            "object_id": object_id,
            "object_type": candidate.get("object_type"),
            "title": candidate.get("title"),
            "summary": candidate.get("summary"),
            "strength": candidate.get("strength"),
            "retrieval_evidence": {"hypotheses": [], "sources": [], "scores": {}},
        })
        evidence = item["retrieval_evidence"]
        if candidate.get("hypothesis") not in evidence["hypotheses"]:
            evidence["hypotheses"].append(candidate.get("hypothesis"))
        if candidate["source"] not in evidence["sources"]:
            evidence["sources"].append(candidate["source"])
        if "score" in candidate:
            values = evidence["scores"].setdefault(candidate["source"], [])
            if candidate["score"] not in values:
                values.append(candidate["score"])
        key = (candidate.get("_query_id"), candidate.get("hypothesis"), candidate.get("source"), object_id)
        if key not in seen_evidence:
            seen_evidence.add(key)
            query_evidence.append({
                "object_id": object_id,
                "query_id": candidate.get("_query_id"),
                "hypothesis": candidate.get("hypothesis"),
                "source": candidate["source"],
                "source_rank": candidate.get("_source_rank"),
                "raw_score": candidate.get("score"),
            })
    for item in merged.values():
        evidence = item["retrieval_evidence"]
        evidence["hypotheses"].sort(key=lambda value: str(value or ""))
        evidence["sources"] = [source for source in SOURCE_ORDER if source in evidence["sources"]]
        evidence["scores"] = {source: evidence["scores"][source] for source in SOURCE_ORDER if source in evidence["scores"]}
    query_evidence.sort(key=lambda item: (
        item.get("query_id") or "",
        SOURCE_ORDER.index(item.get("source")) if item.get("source") in SOURCE_ORDER else len(SOURCE_ORDER),
        item.get("source_rank") or 0,
        item.get("object_id") or "",
    ))
    return [merged[key] for key in sorted(merged)], query_evidence


def _jobs(hypotheses):
    if not isinstance(hypotheses, (list, tuple)):
        raise ValueError("hypotheses must be an array")
    jobs = []
    seen = set()
    for index, item in enumerate(hypotheses):
        if isinstance(item, str):
            item = {"query": item}
        if not isinstance(item, dict):
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            continue
        hypothesis = str(item.get("hypothesis") or "default").strip() or "default"
        key = (hypothesis, query)
        if key in seen:
            continue
        seen.add(key)
        query_id = str(item.get("query_id") or f"q_{index + 1:04d}")
        service = item.get("service")
        services = item.get("services")
        if isinstance(services, str):
            services = [services]
        if not isinstance(services, (list, tuple)):
            services = [service] if service else (["embedding", "lexical"] if item.get("lexical", True) else ["embedding"])
        services = {str(value).strip().lower() for value in services}
        if "both" in services:
            services = (services - {"both"}) | {"lexical", "embedding"}
        if not services or services - set(SOURCE_ORDER):
            raise ValueError("services must contain lexical, embedding, or both")
        if "embedding" in services:
            jobs.append((query_id, hypothesis, query, "embedding"))
        if "lexical" in services:
            jobs.append((query_id, hypothesis, query, "lexical"))
    return jobs


def build_candidate_pool(hypotheses, *, profile=None, root=None, diagnostic_profiler=None, deadline=None):
    """Search the requested hypotheses through embedding and lexical services.

    Partial service failures are isolated and returned in ``errors``.  The
    result contains candidates only; callers must observe source objects before
    exposing Evidence to Main Agent.
    """
    profile = profile or ltm_retrieval.MEMORY_PUSH_PROFILE
    jobs = _jobs(hypotheses)
    if not jobs:
        return {"status": "ok", "items": [], "query_evidence": [], "errors": []}
    candidates, errors, successful = [], [], 0
    semantic_jobs = [job for job in jobs if job[3] == "embedding"]
    lexical_jobs = [job for job in jobs if job[3] == "lexical"]
    executor = ThreadPoolExecutor(max_workers=min(8, len(lexical_jobs) + bool(semantic_jobs)))
    try:
        semantic_future = executor.submit(_run_semantic_batch, semantic_jobs, profile, root, diagnostic_profiler, deadline) if semantic_jobs else None
        lexical_futures = [
            (job, executor.submit(_run_lexical_job, job, profile, root, diagnostic_profiler, deadline))
            for job in lexical_jobs
        ]
        if semantic_future is not None:
            try:
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                results = semantic_future.result(timeout=timeout)
            except FutureTimeoutError:
                results = [
                    (
                        job,
                        {
                            "status": "error",
                            "items": [],
                            "errors": [{"error": "Memory Push exceeded its foreground time budget."}],
                        },
                        [],
                    )
                    for job in semantic_jobs
                ]
            except Exception as error:
                results = [(job, {"status": "error", "items": [], "errors": [{"error": str(error)}]}, []) for job in semantic_jobs]
            for job, result, adapted in results:
                if result.get("status") in {"ok", "partial"}:
                    successful += 1
                    candidates.extend(adapted)
                else:
                    errors.append({"query_id": job[0], "hypothesis": job[1], "source": job[3], "errors": result.get("errors", [])})
        for job, future in lexical_futures:
            try:
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                result = future.result(timeout=timeout)
                adapted = _adapt_result(result, job[0], job[1], job[3])
            except FutureTimeoutError:
                errors.append({"query_id": job[0], "hypothesis": job[1], "source": job[3], "error": "Memory Push exceeded its foreground time budget."})
                continue
            except Exception as error:
                errors.append({"query_id": job[0], "hypothesis": job[1], "source": job[3], "error": str(error)})
                continue
            if result.get("status") in {"ok", "partial"}:
                successful += 1
                candidates.extend(adapted)
            else:
                errors.append({"query_id": job[0], "hypothesis": job[1], "source": job[3], "errors": result.get("errors", [])})
        items, query_evidence = _integrate(candidates)
        status = "partial" if errors and items else "error" if errors and successful == 0 else "ok"
        return {"status": status, "items": items, "query_evidence": query_evidence, "errors": errors}
    finally:
        for future in [semantic_future, *[future for _job, future in lexical_futures]]:
            if future is not None:
                future.cancel()
        executor.shutdown(wait=deadline is None, cancel_futures=deadline is not None)
