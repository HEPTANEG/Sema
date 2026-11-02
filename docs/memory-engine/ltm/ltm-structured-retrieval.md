# LTM Structured Retrieval

This document describes the read-only storage-facing query layer over the LTM
Core Store. It is shared by Formation, LTM Push Candidate Generation, and
the Memory Retrieval observation/search runtime; it does not own any caller's
Agent loop or completion policy.

## Service Boundary

The canonical module is `my_agent/memory_engine/ltm/ltm_retrieval.py`. It exposes
profile-bound `select_objects`, `search_objects`, `semantic_search`,
`semantic_search_many`, and `traverse_objects`. Queries use read-only SQLite
connections and never write or migrate the store. Runtime selects the
immutable caller profile; model arguments cannot expand it.

`formation_existing_memory`, `memory_push`, and `memory_retrieval` are separate
profiles. The latter may evolve independently while using the same lexical and
embedding service implementations. `get_object`, `get_links`,
`traverse_objects`, and `read_event_slots` are Maintainer observation
foundations and are exposed to Retrieval only through its own read-only
profile wrapper.

Every structured retrieval result has `status: ok | partial | error`,
`items`, `next_cursor`, `truncation_reason`, and `errors`. Projection and
limits are enforced by the selected profile. Empty results are successful
empty results; bounded limits are explicit partial results.

## Retrieval Integration

Memory Retrieval does not expose these storage functions directly to Main
Agent. Its isolated Agent receives only the Session-local tools
`search_candidates`, `inspect_candidate_pool`, `rank_1_candidate_pools`,
`rank_2_candidate_pools`, `get_object`, `get_links`, `traverse_objects`, and
`read_event_slots`. The Agent chooses when to call them and may alternate
lexical search, embedding search, ranking, graph traversal, and source
observation.

Candidate Generation ends before Evidence: lexical/embedding hits and Rank
outputs remain candidates. Source observations are required before a terminal
Retrieval checkpoint can expose Evidence. Main Agent semantic stopping and
Session continuation are owned by `memory_retrieval.py`, not this service.

## Formation Integration

Formation continues to use its private `ltm_select_objects`,
`ltm_search_objects`, and `ltm_traverse_objects` provider-native tools and its
existing profile and write-plan rules. Those tools remain outside the Main
Agent registry.

## LTM Push Integration

LTM Push keeps its three end-to-end Query Compiler channels, routing, cache,
thresholds, Rank 1/2/3 policy, and provider-context injection. Its
`memory_push.py` wrapper calls the neutral `candidate_generation` core and
preserves the historical Push Candidate Pool result shape.

## Deferred Work

Generic Agent frameworks, graph diffusion policy, new semantic indexes, and
cross-process Retrieval Sessions remain deferred. The Structured Retrieval
layer remains a storage substrate rather than a second orchestration system.
