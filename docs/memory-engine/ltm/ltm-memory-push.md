# LTM Push

## Purpose and Boundary

LTM Push is the implicit LTM consumption path. After a new user message
enters a runtime turn, it finds existing MemoryObjects that may improve the
next Main Agent response and supplies temporary provider context.

LTM Push is distinct from Memory Retrieval: Memory Retrieval receives an explicit
query, while LTM Push derives retrieval queries from the current runtime
context. Both use read-only Retrieval Service operations during retrieval.

LTM Push does not form, update, merge, forget, or delete MemoryObjects. It
does not modify Event Stream, STM, WM, Focus Segments, LTM Core Store, or
Vector Index. It does not expose queries, candidates, scores, Payloads, or
model reasoning to the Main Agent.

The only persistence associated with LTM Push is the developer-readable
runtime cache described in the runtime document. It is diagnostic and
routing state, not memory data.

## Architecture

```text
Current STM / WM context
        -> Focus-aware route
        -> LTM Push Channels
           - Context Projection
           - Query Compiler and Validation
           - Retrieval Execution
        -> Candidate Integration
        -> Candidate Pool
        -> Rank 1 (when needed, <= 400)
        -> Rank 2 (when needed, <= 80)
        -> Rank 3 (always for a non-empty pool, <= 40)
        -> Context Builder
        -> Main Agent provider context
```

A LTM Push Channel is an end-to-end retrieval path. It applies a specific
context projection, uses its Query Compiler to produce retrieval descriptions,
executes the configured Retrieval Service operations, and contributes candidate
objects to the shared Candidate Pool. Retrieval Service performs mechanical
reads using Embedding Adapter, Vector Index, FTS/lexical search, Core Store, and
the LTM Push Retrieval Profile. Candidate Integration merges and deduplicates
the channel contributions. Rank 1 performs deterministic compression, Rank 2
uses the local reranker model, and Rank 3 performs provider-based final
selection. Context Builder assembles the final material.

Semantic retrieval uses one batched Embedding request per retrieval turn. The
SQLite read for multiple semantic queries is then shared across the union of
their matched object IDs. Before Rank 3, complete objects are hydrated through
one ordered batch read covering Core metadata, derived values, and Payloads.
These are storage-read optimizations only: filtering, result order, evidence,
and model-facing data remain unchanged.

## Runtime Routes

| Situation | Synchronous route | Background route |
| --- | --- | --- |
| First usable result | Full route | None |
| Stable Focus, every user turn | Latest Semantic + Entity, then incremental Rank 3 | None |
| Stable Focus, every 10 turns | Current incremental route | General Semantic + Entity full refresh |
| Successful Memory Retrieval | Current incremental route | General Semantic + Entity full refresh |
| Confirmed Focus Shift | General Semantic + Latest Semantic + Entity full route | None |
| Unclear Focus | Stable Focus incremental route | None |

Stable incremental retrieval always considers the current interaction. If it
finds no object outside the previous selected result, that result is reused.
If it finds an object that was not previously selected, only that object is
sent to Rank 3 and any newly selected objects are merged with the prior
result. Periodic full refreshes are responsible for reselecting and removing
older results. See the runtime document for routing and cache details.

One background full refresh is allowed at a time. Periodic and Recall triggers
are coalesced. A refresh records its input message-history revision and cannot
overwrite a newer cached result.

## Input Projection

LTM Push uses the context projection available to the Main Agent. The
current channel inputs are:

| Channel | Input | Purpose |
| --- | --- | --- |
| General Semantic | All non-system STM Slots | Broad, high-recall semantic descriptions |
| Latest Semantic | Latest `ceil(slot_count / 4)` non-system STM Slots | The newest meaningful interaction |
| Entity | All non-system STM Slots | Literal entities, names, terms, identifiers, and keywords |

WM is not passed to these three Compiler calls. System messages are excluded.
Latest Semantic keeps the selected Slots in their original chronological order;
the quarter is calculated by Slot count, not token count. The Main Agent system
prompt, complete Event Stream, internal Agent contexts, and the current Push
result are excluded. Complete Payload text is not used until Rank 3.

## Retrieval Channels

Each channel owns its context projection, Query Compiler, and retrieval policy.
The three Query Compilers run independent prompt calls in parallel under a
shared technical timeout. Each compiler returns:

```json
{
  "channel": "general_semantic",
  "queries": ["retrieval description"]
}
```

The runtime validates the channel name, non-empty strings, exact duplicates,
and the maximum of 20 queries per channel after the complete provider response
is received. An invalid or failed compiler leaves its channel with no queries
without invalidating the other channels. No automatic output repair or
streaming truncation is performed. The compiler prompts ask for fewer than 20
queries when the relevant dimensions or entities are already covered, so the
runtime does not pad the result.

The General Semantic Channel projects all non-system STM Slots. Its compiler
produces broad retrieval descriptions through grounded extraction,
context-supported interpretation, and higher-level concepts. Queries may be
phrases, statements, or meaningful questions; the channel does not assume that
every conversation is a task. Its retrieval execution uses semantic search.

The Latest Semantic Channel projects the latest quarter of non-system STM Slots
by Slot count. It focuses on the newest meaningful interaction while preserving
the selected Slots' chronological order. Its retrieval execution uses semantic
search.

The Entity Channel projects all non-system STM Slots. Its compiler produces short search terms
or necessary short phrases in two layers: direct extraction of original
entities and literal expressions, followed by context-supported aliases,
related keywords, or broader concepts. It should prioritize the recent
interaction while using older WM/STM only when they help resolve references or
support a grounded retrieval term. Its retrieval execution uses both
lexical/FTS and semantic search.

## Candidate Generation

Retrieval execution within each channel is:

```text
General Semantic query -> semantic_search
Latest Semantic query  -> semantic_search
Entity query           -> lexical/FTS + semantic_search
```

Semantic Search embeds each query, searches the Vector Index, and applies the
LTM Push Retrieval Profile and Core Store filtering. It uses vector Top-K,
not a new similarity threshold. Normal candidates have `active` lifecycle
state and derived `strength > 0`; the four system-owned root Tags are
excluded from ordinary semantic candidates.

Entity lexical search uses `full_text` for longer queries and the existing
`contains` fallback for short queries. Embedding similarity and lexical/BM25
scores remain separate. Each query runs independently; failures do not cancel
other queries.

Integration deduplicates results by `object_id` and retains internal evidence:

```text
object_id, query_id, channel, source, source_rank, raw_score
```

The Candidate Pool is a runtime collection. Its external entries contain
object identity, Core display fields, strength, and retrieval evidence, but no
full Payload, field-level embedding hit marker, or final ordering. It is not
persisted as LTM data.

## Rank 1, Rank 2, and Rank 3

Rank 1 is deterministic and operates on the integrated Candidate Pool. It does
not rerun Embedding/FTS, read complete Payloads, or use MemoryLink. It uses
the best source-local rank for embedding and lexical evidence, then applies
the V1 formula:

```text
embedding_part = 1 / (60 + best_embedding_rank)
lexical_part   = 1 / (60 + best_lexical_rank)
base_score     = embedding_part + lexical_part
```

Missing sources contribute zero. Ties use source count, channel count, query
coverage, and `object_id` in that order. Strength is applied as a fixed
weight: `<=0` is excluded, `<0.4` is `0.50`, `0.4..0.7` is `1.00`, and `>0.7`
is `1.25`. Rank 1 keeps at most 400 candidates.

Stage routing is:

| Candidate Pool | Rank 1 | Rank 2 | Rank 3 |
| --- | --- | --- | --- |
| `<= 80` | skip | skip | run |
| `81..400` | skip | run, keep <= 80 | run |
| `> 400` | keep <= 400 | keep <= 80 | run |

Rank 2 uses the local `Qwen/Qwen3-Reranker-0.6B` as an independent pairwise
scorer with STM-only context. The current operating limits are 1,024 STM
content tokens and 12,000 candidate title/summary tokens. The service uses
one model instance and adaptive GPU micro-batches; its external result is a
scalar score and at most 80 candidates.

Rank 3 uses the current Main Agent provider in a tool-free structured call.
It receives the complete non-system STM of the current continuous session,
without WM, and up to 80 candidates with a temporary integer `ref` and full
Payload. The session starts after the current Sema process start and resets
when the gap between adjacent user messages exceeds 600 seconds. It returns
zero to 40 existing refs in
`{"selected_refs": [1, 2]}`. The reference map is created for one Rank 3
call only; it is never persisted. Unknown or duplicate refs in the complete
response, malformed JSON, and invalid output shape fail the complete Push
result. If a valid response contains more than 40 refs, the runtime resolves
and validates the complete response, then keeps the first 40. Forty is a maximum, not a target;
the selector should choose only memories that clearly improve the next response
and should prefer a small relevant subset.

## Context Injection

The final selected objects are ordered by earliest Event Stream provenance
slot, then end slot; objects without provenance use creation time and
`object_id`. Injection order is:

```text
system -> WM -> LTM -> STM
```

If a selected object has a Payload, only the Payload is injected. Otherwise,
only its title is injected. Derived summary and retrieval metadata are not
injected when Payload exists. The result is temporary provider-context
material: it does not enter messages, Event Stream, STM, WM, or the next
turn's memory projections.

The LTM budget is derived from the current short-term budgets:

```text
B_LTM = 2 x (B_STM + B_WM)
```

With the current 50,000-token STM and WM budgets, this is 200,000 tokens. It
is not a permanent constant. No additional proactive overflow policy is
defined before actual use demonstrates a need.

## Failure and Observability

LTM Push is failure-open. Compiler, retrieval, embedding, vector index,
Rank 1, Rank 2, Rank 3, provider, or validation failure discards partial Push output;
the Main Agent continues with an internal LTM placeholder containing only a
stable stage and `reason` code. A successful run with no usable result uses a
separate no-result reason such as `no_compiled_queries`,
`empty_candidate_pool`, or `no_selected_memory`. No raw exception text,
query, candidate, or model output is included. No LTM, Vector Index, Event
Stream, STM, or WM data is changed.

Runtime records metadata-only status and stage information. It does not record
queries, messages, Payloads, candidate text, provider requests, or reasoning.
The temporary developer cache and its five-entry sliding window are documented
in `ltm-memory-push-runtime.md`.

## Related Documents

- [LTM Push Runtime](ltm-memory-push-runtime.md): latency,
  incremental routing, refresh, and cache behavior.
- [LTM Core Store](ltm-core-store.md): storage, validation, indexes, and
  derived-strength rules used by retrieval.
- [LTM Formationer](ltm-formation.md): creation and maintenance of LTM objects.
