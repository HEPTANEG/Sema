# LTM Push Runtime

Logical deletion excludes objects from all candidate sources. Push additionally
requires cached Strength >= 0.1, and rehydrates/revalidates cached selections
immediately before context injection. Objects that no longer qualify are not
injected or counted as successful uses. The threshold does not apply to other
retrieval profiles.

## Scope

This document records the implemented performance and routing behavior for
LTM Push. The stable architecture and data contracts are defined in
[`ltm-memory-push.md`](ltm-memory-push.md).

The runtime strategy has two objectives:

1. reduce the cost of the complete pipeline;
2. avoid repeating complete work during a continuous conversation while still
   considering every current user message.

## Current Performance Measures

Each real `run_memory_push()` call records temporary metadata-only diagnostics
for the current process. The records are written to:

```text
.sema/logs/memory-push-latency.jsonl
.sema/logs/memory-push-latency-summary.json
```

The diagnostics cover query compilation, candidate generation, Rank 1,
Rank 2, Rank 3, object hydration, service startup, embedding requests, and
reranker requests. Parallel stages use wall-clock duration. The records do
not contain queries, messages, Payloads, candidate text, provider requests,
or model output. Diagnostic write failures do not affect a reply.

The first optimization reduced semantic embedding from one request per query
to one internal batch request. Query identity and Query-level Evidence remain
unchanged. Lexical queries remain independent.

The current storage-read optimization also batches the mechanical SQLite work.
Multi-query semantic retrieval performs one read for the union of matched
object IDs instead of one read per query. Rank 3 Object Hydration performs one
ordered read for all required Core metadata, derived values, and Payloads
instead of opening a separate read for each candidate. Both paths preserve
active-object filtering, root exclusion, input ordering, and the existing
runtime contracts.

The temporary diagnostic collector may be removed after the observation period
and any follow-up performance work are complete; it is not part of the
permanent LTM Push contract.

## Provider Connection Reuse

OpenAI-compatible model calls use a process-local standard-library HTTP
Keep-Alive pool. Connections are isolated by network origin, are not shared
by concurrent requests, and keep at most eight idle connections per origin.
Idle connections older than 60 seconds are discarded. A response must be
fully consumed before its connection can be reused.

The pool covers DeepSeek, Kimi, and custom OpenAI-compatible Providers. The
Ollama native transport is unchanged. A broken or provider-closed connection
is discarded and the existing Runtime error handling remains responsible for
any retry; the transport does not automatically repeat POST requests.

The pool is closed by normal Runtime shutdown paths and by an `atexit`
fallback. This optimization only reduces client-side TCP/TLS connection
overhead. It does not reduce Provider queueing, request upload, or model
Prefill time.

## Latency Optimization Checkpoint

The current latency optimization phase is provisionally complete. Real usage
measurements improved representative foreground Main Agent latency from about
14.6 seconds to about 7.5 seconds, and foreground LTM Push from about 8.7
seconds to about 4 seconds. The result varies with Provider service behavior,
but the reduction remained visible across repeated warm-process turns.

The latency diagnostics remain enabled for a further observation period. The
temporary JSONL records, Main Agent timing spans, and Provider streaming timing
must not be removed yet. No additional fixed candidate limit or extra Rank 3
compression is part of the current implementation; those decisions remain
deferred until new measurements justify them.

## Temporary Streaming Timing Instrumentation

Query Compiler and Rank 3 currently use the existing streaming provider
transport while buffering the complete response in Runtime before JSON
validation. The user-visible behavior and Memory Push result contract remain
non-streaming. Per-call metadata records response-header arrival, first
content delta, stream completion, and content-delta count for these two model
stages. The derived output interval is the time from the first content delta
to stream completion; JSON validation is recorded separately.

This streaming transport and its timing fields are temporary performance
diagnostics. After the observation period and any follow-up latency work are
complete, the streaming instrumentation and its documentation must be
reviewed for removal; it is not a permanent LTM Push API requirement.

## Temporary Main Agent Context Timing

Main Agent Trajectories also contain temporary metadata-only child spans for
the synchronous context-building path. The spans cover Focus Detector,
context pressure, STM projection, WM ingress, WM projection, window
observation, LTM result processing, final context assembly, final context
token counting, and Provider preparation. The existing Memory Push span is
kept as the LTM child stage and is not duplicated in the LTM JSONL metrics.

These spans are enabled only for Main Agent runtime turns. They exclude
Maintainer, Formation, and other background work, and never store Query text,
STM/WM content, Payloads, Provider requests, or model output. This timing is
temporary investigation instrumentation. It must be removed with its tests
and documentation only after the observation period and any follow-up latency
work are complete.

## Compiler Input and Output Bounds

All three Query Compilers receive non-system STM Slots only. General Semantic
and Entity receive the complete non-system STM projection. Latest Semantic
receives the latest `ceil(slot_count / 4)` Slots, in their original chronological
order. The quarter is calculated by Slot count rather than token count, and WM
is not passed to the Compiler calls.

Each Compiler is instructed to generate queries as needed, up to 20 per
channel, without padding repetitive or low-value output. The runtime waits for
the complete non-streaming provider response, validates it, removes exact
duplicates, and keeps the first 20 queries. No Max Tokens parameter or
real-time output truncation is used.

Rank 3 keeps its current input candidates, full Payloads, and real object IDs.
Its complete structured response is validated before use. A valid selection
longer than 40 IDs is truncated to its first 40 IDs; unknown or duplicate IDs
in the retained portion remain errors.

## Focus-Based Runtime Routing

Focus controls execution strategy, not retrieval meaning. The latest STM
interaction remains part of every synchronous route.

```text
Stable Focus, every user turn:
  Latest Semantic + Entity
  -> incremental candidate evaluation
  -> current result

Periodic refresh or successful Memory Retrieval:
  General Semantic + Entity
  -> complete refresh in background
  -> replace the complete result

Confirmed Focus Shift:
  General Semantic + Latest Semantic + Entity
  -> complete refresh synchronously
  -> reply after the refresh completes
```

Focus is evaluated against the newest user message before WM Ingress. WM may
contain older rendered material, but this does not make Focus Detector itself
lag behind the latest message.

The preflight context check does not run Focus Detector or Memory Push. The
formal Main Agent context build runs Focus Detector once and uses that action to
select the Memory Push mode.

When Focus is unclear, use the stable incremental route to avoid large result
changes caused by an uncertain transition.

The Detector action is the only authority for Memory Push routing. `active_title`
and the persisted active segment are display and state information; they do not
independently trigger a synchronous refresh. The Main Agent Trajectory records
the Detector action and the selected Memory Push mode together so that a
`activated_inactive` or `created_active` action can be distinguished from
`no_change`.

The UI `focus_shifted` event is associated with the Bridge `turn_id`. The UI
shows the temporary Focus shifted notice only for the current turn, so delayed
events from an earlier turn cannot change the current status display.

## Stable Incremental Route

Stable Focus does not reuse a prior result without checking the current
interaction. Latest Semantic and Entity run on the current turn.

Compare current incremental hits with the previous `selected_objects`, not
with the previous Candidate Pool:

```text
current incremental object IDs
vs.
previous selected object IDs
```

The rules are:

- if retrieval finds no LTM object, reuse the previous selected result;
- if every hit is already selected, reuse the previous result;
- if an unselected object is found, hydrate only those objects and send only
  them to Rank 3;
- merge newly selected objects with the previous selected result and remove
  duplicates;
- existing selected objects do not re-compete during this incremental route;
- periodic complete refresh handles removal or replacement of stale results;
- the merged result is capped at 40 objects and uses the normal provenance
  ordering.

An object that existed in an older Candidate Pool but was not selected is
eligible for the current incremental Rank 3. This prevents a previously
missed relevant memory from being permanently hidden by Candidate Pool reuse.

Rank 3 receives a separate current-session STM projection. It excludes WM,
system messages, messages from before the current Sema process started, and
messages before the most recent user-message gap greater than 600 seconds.
All remaining current-session Slots are passed in their original order without
an additional Rank 3 Slot or token truncation. The regular STM projection used
by the Query Compiler is unchanged.

## Periodic and Recall Refresh

Every ten user turns, the runtime requests a complete background refresh. A
successful Memory Retrieval also requests one. The refresh uses General
Semantic and Entity; Latest Semantic continues to serve the synchronous
incremental route.

The background refresh does not block the current Main Agent reply. Only one
complete refresh runs at a time; simultaneous triggers are coalesced. A
refresh records the message-history revision it used. It may replace the
cached complete result only when it is not older than the current cached
revision. Older results are discarded and cannot overwrite newer results.

## Small Candidate Pools

Complete refreshes avoid stages that would not compress the result:

| Candidate Pool | Rank 1 | Rank 2 | Rank 3 |
| --- | --- | --- | --- |
| `<= 80` | skip | skip | run |
| `81..400` | skip | run, keep <= 80 | run |
| `> 400` | keep <= 400 | keep <= 80 | run |

Rank 3 remains mandatory for every non-empty pool because it decides whether
a memory clearly improves the next response. A small pool must not be treated
as permission to select every object.

## Runtime Cache

Developer-readable routing state is stored under:

```text
.sema/memory/memory_push_cache/
```

The cache is a five-entry sliding window and may contain the Candidate Pool,
final selected objects, rendered result, Focus state, message-history
revision, route, status, and minimal timing metadata. It does not contain
channel queries, prompts, reasoning, intermediate Rank 1 or Rank 2 data, or
unselected Payloads.

Cache writes are best effort. A cache failure never affects the Main Agent
reply. Cache state is not Event Stream data, LTM data, or provider context.

## Failure Behavior

```text
Stable incremental failure -> retain previous complete result
Background refresh failure -> retain current result and retry later
Focus Shift synchronous failure -> omit LTM for the current reply
```

No route writes partial Push output. Failure-open behavior, placeholders, and
metadata-only observability remain as defined in the main LTM Push
document.

## Deferred Optimizations

The following are intentionally not part of this runtime document's
implemented contract:

- proactive preloading before a user turn;
- additional retrieval channels;
- graph or temporal candidate expansion;
- cache-based Rank reuse beyond the confirmed incremental
  rules;
- a fixed end-to-end latency target;
- proactive Payload-overflow handling.
