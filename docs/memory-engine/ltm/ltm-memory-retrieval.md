# LTM Retriever

## Purpose and Boundary

Memory Retrieval is Sema's explicit, query-driven Long-Term Memory search
surface. The Main Agent requests a memory by Query; an isolated Retrieval Agent
searches the LTM read-only services and returns source-grounded evidence.

Memory Retrieval is distinct from LTM Push. Push derives recommendations from
the current runtime context and injects them into provider context. Retrieval
is driven by an explicit Query and reports its findings back to the Main Agent.
Retrieval never writes MemoryObjects, MemoryLinks, Event Stream slots, or
indexes, and it does not answer the user on the Main Agent's behalf.

The stable Runtime implementation is documented in
[LTM Retriever Runtime](ltm-memory-retrieval-runtime.md). Shared
storage-facing retrieval services are documented in
[LTM Retrieval Service](ltm-structured-retrieval.md). Wake scheduling and
provider-compatible delivery are documented independently in the
[Runtime Wake](../runtime-wake.md) module.

## Public Contract

Maintainer also calls `retrieve_memory(query?, continuation_id?, message?, close?)`
as a more powerful semantic memory-retrieval tool. Its trusted Runtime caller profile
omits context transfer, waits for a consolidated checkpoint or targeted `need_clue`,
and does not deliver intermediate feedback or Main Agent Wake. Evidence, search,
ranking, and read filters are unchanged. Maintenance inspection does not count as
successful use or refresh LTM Push. Maintainer owns semantic decisions and writes.

The Main Agent has two read-only memory tools. Direct object or keyword lookup
uses `get_object`:

```text
get_object(
    object_id?, object_ids?, title?, query?, min_strength?, max_strength?,
    linked_to_object_id?, tag_object_ids?, source_slot_after?,
    source_slot_before?, projection?, limit?
)
```

`get_object` reuses the shared bounded observation implementation. It is a
lexical and filter-based lookup; it does not create a Retrieval Session, invoke
an Agent, or support embedding search. The Main Agent should use it when a
single direct lookup is likely to answer the request. If the result is empty,
ambiguous, or insufficient, the Main Agent can start `retrieve_memory`.

The query-driven Agent entry point is:

```text
retrieve_memory(
    query?, continuation_id?, message?, include_context?, close?
)
```

- `query` creates a new process-local Retrieval Session.
- `continuation_id` resumes the existing Session.
- `message` is unrestricted Main-Agent guidance: a clue, correction, search
  direction, or natural-language continuation.
- `include_context=true` supplies the current provider Context Snapshot when the
  Retriever cannot identify the target from the Query and existing history.
- `close=true` closes an existing Session and stops further Retrieval work.

The original Query is immutable. A new independent Query requires the previous
Session to be closed. Continuations are opaque, stable within the process, and
expire with the Session. The Session's latest request and result are cached so
an identical retry is idempotent. V1 permits one Retrieval Agent and one
Retrieval Session per process.

The result kinds are:

```text
running | feedback | checkpoint | need_clue | need_context | technical | closed
```

`feedback` reports a strongly suspected observed match and does not wait for a
Main-Agent reply. `need_clue` asks for one concrete missing fact;
`need_context` asks for the complete current Context Snapshot. Both pause the
current search until a continuation is supplied. `checkpoint` reports the
current search conclusion and remains resumable; it is not semantic Session
closure. `technical` reports provider, tool, format, timeout, cancellation,
unsupported-query, or no-progress outcomes without implying that the memory is
absent. `closed` reports explicit Session termination.

The Main Agent owns semantic completion. Runtime limits, cancellation, timeout,
and Session TTL are technical safety controls only.

## Retrieval Agent and Channel

The Retrieval Agent uses the existing provider-native Agent path with a
Retrieval-specific prompt. Retrieval Channel is a logical boundary inside the
Agent, not a separate Query Compiler or model call. The Agent itself creates
service-specific search expressions and may choose, repeat, or abandon them.
The Query is always retained as the search target; generated expressions are
supplementary and never replace it.

The Retrieval Agent's System Prompt strictly limits it to recalling and
reconstructing information already present in Sema's memory. Recall targets
may be abstract, uncertain, factual, or relational, but requests with no
recognizable memory-recall objective are rejected before retrieval tools are
used with `technical` error code `unsupported_query`.

The Agent may compose the shared services in any useful order:

```text
Retrieval Agent
  -> search_candidates (lexical | embedding | both)
  -> Session-local Candidate Pool
  -> inspect or Rank 1 / Rank 2 when the pool is too large
  -> get_object / get_links / traverse_objects / read_event_slots
  -> observed source content
  -> feedback, checkpoint, another search, or another observation
```

### Model-Facing Object References

Candidate Pool projections and observation tool results expose session-local
integer `ref` values instead of real MemoryObject IDs. Retrieval tool
arguments use `object_ref`, `object_refs`, `source_object_refs`,
`target_object_refs`, `endpoint_object_refs`, and `start_object_refs` as
appropriate. The mapping is stable across continuations in one Retrieval
Session; newly discovered objects receive new refs. It is released when the
Session closes or expires and is never written to SQLite, Event Stream,
Vector Index, or cache files. Link IDs and Slot IDs are unchanged.

The Retrieval Agent's final evidence also uses `ref`. Runtime validation
resolves it back to the real `object_id` before evidence checks, successful-use
accounting, revalidation, and publication. The public `RetrievalResult`
contract therefore continues to contain real `object_id` values internally;
real IDs are not exposed in model-facing projections or tool results.

This is deliberately not a Light/Full binary workflow. A search can move from
lexical to embedding, graph traversal, and source verification in any order.
The Agent may revisit an earlier service when a later observation creates a new
search hypothesis.

## Candidates, Ranking, and Evidence

Lexical and embedding services produce Candidate Pool items, not Evidence.
Candidate Generation is implemented in the neutral shared core
`memory_engine/candidate_generation.py`; LTM Push and Memory Retrieval call
the same executable implementation while keeping separate orchestration and
Session-local pools.

Rank 1 is the shared deterministic compression stage with a 400-item technical
baseline. Retrieval uses the caller-neutral Rank 2 adapter with the immutable
Query plus Session guidance as its target and an initial 80-item output
profile. Retrieval V1 does not use LTM Push Rank 3. When a pool is too large
for direct inspection, the Agent can first receive a count-only projection and
choose a bounded inspection or ranking operation.

Evidence is source content that the Retriever actually observed through
`get_object`, `get_links`, `traverse_objects`, or `read_event_slots`. Runtime
normalizes model-facing `ref` values to the stored `object_id` before
validation and publication. Published evidence may use the stored fields
`object_id`, `title`, `summary`, `body`, and `structured_data`, with bounded
`source_refs` for relevant Link IDs, Event Slot ranges, or exact excerpts.
Natural-language explanation cannot substitute for source fields. A candidate
hit or Rank output alone is never Evidence.

Only an Evidence-bearing `checkpoint` triggers successful-use accounting and
the existing Main-Agent boundary hook for LTM Push refresh. Object IDs are
deduplicated within the Session. Candidate-only results, empty pools,
clarification requests, technical results, and close operations do not trigger
those side effects.

That accounting rule applies to Main Agent calls. Maintainer checkpoints suppress
both successful-use accounting and the Main Agent refresh hook.

## Main-Agent Collaboration

The Retriever can communicate in both directions through the public tool and
Runtime Wake:

- synchronous results are returned as the ordinary `retrieve_memory` Tool
  Result;
- after the five-second synchronous window, the tool may return `running` while
  the Worker continues in the background;
- later `feedback`, `checkpoint`, `need_clue`, `need_context`, `technical`, or
  `closed` outcomes are delivered to the Main Agent through Wake;
- Main-Agent messages, clues, Context Snapshots, and `close` control the same
  Session through continuation calls.

Retriever Wake messages retain `source=retriever` in Event Stream even when the
provider-compatible projection uses `role=user`. Their Runtime processing
class is `tool_result_like`, so Wake-only turns do not start a new Focus or
LTM Push cycle. A turn containing real user input remains on the ordinary
user path.

## Observation Profile

Retrieval reuses the Maintainer observation foundations through an independent
read-only Retrieval profile. It does not inherit Maintainer coverage cursors,
write capabilities, or maintenance policy. `get_object` remains literal/FTS
and filter based; semantic discovery belongs to `search_candidates`.

The profile enforces bounded projections, traversal depth, object counts, Event
Slot ranges, and total result size. Empty results are explicit successful empty
results; partial results expose their continuation boundary. Source `null`
values remain `null` and are not converted into fabricated payloads.

## Non-Goals

Memory Retrieval does not change LTM Push's three Channels, routing, cache,
thresholds, Rank 3, or provider-context injection. It does not introduce a
second memory store, a global Retrieval Cache, or cross-process Sessions. The
Main Agent's direct access is limited to the bounded read-only `get_object`
tool; it does not expose the Maintainer's full observation surface. Historical
records that mention the retired `recall_memory` entry point remain unchanged.
