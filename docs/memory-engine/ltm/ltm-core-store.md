# LTM Core Store

## Scope

LTM Core Store is the durable storage foundation for Sema's Event Stream memory
architecture. It owns MemoryObject, MemoryPayload, MemoryLink, derived
strength state, FTS synchronization, Vector Index synchronization, operation
idempotency, and maintenance runtime metadata.

The Store is mechanical infrastructure. It does not decide what deserves
memory, perform semantic extraction, rank memories, answer recall requests, or
run the LTM Maintainer model loop. Those behaviors live in Formation,
Retrieval, LTM Push, Recall, and Maintainer runtime layers above the Store.

## Storage

The Store uses a project-scoped SQLite database:

```text
.sema/memory/ltm/ltm.db
```

It is separate from Event Stream storage:

```text
.sema/memory/event_stream/stream.db
```

Canonical graph tables:

```text
memory_objects
memory_payloads
memory_links
```

Store-owned derived and metadata tables:

```text
memory_object_derived
memory_objects_fts
ltm_operations
system_owned_objects
maintenance_runtime_state
maintenance_reports
vector_commit_journal
ltm_relation_migration_issues
```

`maintenance_reports` may include auxiliary semantic digest metadata
(`semantic_digest_status`, `semantic_digest_id`,
`semantic_digest_token_count`), but it does not store digest body text. The
digest body is kept in the Maintainer cache outside the canonical Store graph.

SQLite uses WAL mode, foreign-key enforcement, and a bounded busy timeout.
Payloads are stored in SQLite. Event Stream provenance validation uses a
read-only Event Stream connection and never initializes or mutates Event Stream.

## MemoryObject

All object types share this core:

```text
object_id
object_type
title
summary
created_at
updated_at
```

`object_id` must match:

```text
mem_<32 lowercase hexadecimal characters>
```

`object_type` is one of:

```text
assertion
tag
episode
```

`title` is required, should express the object's core meaning, and must not
encode identity, lifecycle, confidence, time, or source metadata.

`summary` is a derived compression of the title and Payload. It must not
introduce information absent from them. The Store validates summary presence
according to Payload size but does not generate or semantically verify it.

MemoryObject has no `status` field. Retrieval eligibility and maintenance
priority come from Store-owned numeric derived strength.

## Payload

Payload is a component of MemoryObject, not an independent graph object.

```text
memory_payloads:
  object_id
  schema_version
  body
  structured_data
```

There is at most one Payload row per MemoryObject. If a Payload exists, at
least one of `body` or `structured_data` must be non-empty. `schema_version=1`
is the only current Payload schema version.

Payload size is the UTF-8 byte count of `body` plus canonical JSON for
`structured_data`:

```text
payload <= 2 KiB          summary must be empty
2 KiB < payload <= 8 KiB  summary is required
payload > 8 KiB           write is rejected; split into objects
```

The Store does not split content or generate summaries.

For `episode` objects, `structured_data` is either `null`, an empty object
(stored as `null`), or an object containing only `time_scope`. `time_scope`
allows `start`, `end`, `anchor`, and optional `precision`; at least one of
`start`, `end`, or `anchor` must be non-empty, and `precision` is one of
`exact`, `approximate`, or `relative`. The Store rejects unknown fields,
empty time scopes, and invalid precision values without guessing or silently
removing them. Assertion and Tag objects retain their existing empty-or-null
structured-data compatibility.

## MemoryLink

Each Link has one MemoryObject source and one target:

```text
link_id
source_object_id
target_kind: memory_object | event_slot
target_object_id
slot_seq_start
slot_seq_end
relation_type
link_state: active | inactive
created_at
updated_at
```

`link_id` uses:

```text
lnk_<32 lowercase hexadecimal characters>
```

A single Event Stream slot uses equal `slot_seq_start` and `slot_seq_end`.
Contiguous ranges use inclusive start and end. Discrete slot sets use separate
Links.

Relation types:

| relation | source | target | target kind | strength effect |
| --- | --- | --- | --- | --- |
| `sourced_from` | assertion, episode, or tag | Event Stream slot/range | `event_slot` | source gain |
| `tagged_with` | assertion, episode, or tag | tag | `memory_object` | neutral both ends |
| `supports` | assertion or episode | assertion | `memory_object` | target gain |
| `contradicts` | assertion or episode | assertion | `memory_object` | neutral both ends |
| `supersedes` | assertion, episode, or tag | assertion, episode, or tag | `memory_object` | target loss |

Physical Links are always directional. Some relations, such as
`contradicts`, are semantically undirected but are still stored as one directed
edge. Neutral relations count for both endpoints when derived strength is
recomputed.

Only `sourced_from` may target Event Stream slots or ranges. `supports`,
`contradicts`, and `supersedes` must target MemoryObjects.

The same active source, target, and relation combination cannot be written
twice. Self-links are rejected. Inactive links remain stored but do not
participate in normal reads, derived strength counts, or DAG validation.

## Derived Strength

`memory_object_derived` is owned by Store Service:

```text
object_id
successful_use_count
gain_link_count
loss_link_count
neutral_link_count
strength
strength_formula_version
updated_at
last_effective_use_slot_seq
decay_origin_slot_seq
```

New objects start with `successful_use_count = 0`. Store writes recompute
derived rows for affected objects after object creation, link creation, link
deactivation, title update, summary update, and successful-use recording.

Strength is a derived activation signal. Logically deleted objects are excluded
regardless of Strength; changing the formula does not revive them.

The current formula is:

```text
p_u = 0.08 * log(1 + u)
p_G = 0.075 * log(1 + G)
p_N = 0.07 * log(1 + N)
p_max = max(p_u, p_G, p_N)
p_positive = p_max + 0.2 * (p_u + p_G + p_N - p_max)
baseline = clip_to_0_1(0.50 + p_positive - 0.36 * log(1 + L))
reference = last_effective_use_slot_seq ?? decay_origin_slot_seq
gap = max(0, latest_persisted_slot - reference)
strength = baseline * 2 ** (-gap / 2000)
```

Where:

- `u` is all-time successful use count.
- `G` is gain link count.
- `N` is neutral link count.
- `L` is loss link count.

Successful use is counted only when a MemoryObject appears in a final valid
Recall result or is actually injected into provider context by LTM Push.
Candidate hits, FTS hits, semantic hits, and Rank inputs do not count.

Physical schema version 11 contains the forgetting and maintenance-runtime
state migrations; Strength formula version 2 introduces the forgetting
calculation. Payload schema version remains 1. New objects start at the current library slot
(empty stream: -1), not their provenance slot. Legacy objects receive a full
rollout buffer from the migration slot, preserving use counts without inventing
historical last-use records. Effective-use idempotent transactions also record
the current persisted slot. Protected system root Tags use the baseline without
decay. Ordinary reads use cached Strength; mutation paths and end-of-maintenance
refresh recompute from baseline, never from already-decayed Strength.

### Logical deletion and reclamation

After semantic work and finish coverage validation, Runtime refreshes all live
Strengths at one slot snapshot. It selects objects strictly below 0.01, excludes
protected roots, tombstones them and recomputes surviving neighbors in one SQL
transaction. `ltm_forgetting_batches` fixes the batch per maintenance run, so
retry/resume cannot expand it. A neighbor that falls below the threshold waits
until a later run. Index cleanup is durable and retryable; index failure prevents
successful completion but does not resurrect committed tombstones.

`live_memory_objects`, `live_memory_links` and `live_memory_object_derived` are
the normal read predicates. Incident Links retain their original records but no
longer participate in traversal, Strength or normal observations. No replacement
edge is synthesized. Maintainer can still observe weak live objects; it does not
apply a blanket 0.01 floor. Push requires Strength >= 0.1 at candidate selection
and again before injection. Retrieval keeps its existing numerical policies.

Settings > LTM Storage provides a project-local MiB warning (disabled by default),
live/deleted counts and actual SQLite/vector footprint. Warnings do not block
writes. Explicit preview-bound confirmation physically purges only that batch
of tombstones and incident Links, then checkpoints/VACUUMs SQLite. Active memory
tasks return busy without cancellation. A compaction failure is separate from
committed row deletion and can be retried. Later tombstones wait for another
preview. Event Stream, completed formation progress and historical diagnostics
are not erased; this is not a privacy-erasure feature.

## Validation

The Store validator enforces:

- object ID format and object type contract;
- title and Payload constraints;
- root tag protection;
- relation type and endpoint contracts;
- Event Stream slot/range existence for provenance Links;
- no self-links;
- no duplicate active Links;
- active `supersedes` MemoryObject graph is acyclic.

Event Stream targets, inactive Links, and mixed-relation cycles do not
participate in DAG validation.

## Writes

Formation and other trusted runtime layers create new memory through:

```python
validate_write_plan(plan, root=None)
commit_write_plan(plan, idempotency_key, root=None)
```

Maintainer writes use the plan-level API:

```python
commit_maintenance_plan(compiled_plan, idempotency_key, root=None)
```

Specialized Store APIs:

```python
deactivate_memory_link(link_id, idempotency_key, root=None)
update_memory_object_title(object_id, new_title, idempotency_key, maintenance_run_id=None, intent_id=None, root=None)
update_memory_object_summary(object_id, new_summary, idempotency_key, maintenance_run_id=None, intent_id=None, root=None)
record_successful_uses(object_ids, idempotency_key, root=None)
```

All writes use Store idempotency:

```text
same key + same request -> already_committed
same key + different request -> rejected with idempotency_conflict
```

`record_successful_uses` deduplicates object IDs within one operation and
increments each object at most once per idempotent use record.

Schema migration version 8 renames the legacy `derived_from` relation. Event
Stream provenance becomes `sourced_from`; object-to-tag links become
`tagged_with`; and assertion/episode-to-assertion links become `supports` when
their endpoint types allow that mapping. Link IDs and timestamps are preserved,
affected derived strength rows are recomputed, and unmappable legacy links are
kept inactive as `legacy_derived_from` with an audit row. The migration is
transactional and leaves the old table intact if it fails.

Schema migration version 9 adds lightweight Maintainer report fields for the
dynamic `maintainable_frontier` and invocation `coverage_target_slot_seq`.
These fields are diagnostic metadata only and do not change Store validation.

Payload changes are not supported as in-place updates. Semantic content
replacement must create a new MemoryObject and express evolution with
`supersedes`.

## Strong Index Consistency

The Store maintains FTS inside the same SQLite transaction as canonical writes.
FTS no longer filters by object status because MemoryObjects have no status.

Vector synchronization uses a staged commit path:

1. compute affected object embedding projections and stage Vector artifacts;
2. write canonical rows, derived rows, FTS updates, idempotency, and prepared
   journal state in SQLite;
3. install staged Vector artifacts;
4. mark the journal complete.

Embedding or Vector failures reject the LTM write. Maintenance Plan staging has
a bounded embedding timeout and reports `vector_sync_timeout` separately from
other `vector_sync` failures; a timed-out local Embedding Service is closed.
Native staged mutations are executed by an isolated worker with a timeout;
worker failure leaves the installed index and canonical rows untouched.
Store initialization first
recovers prepared Vector journals. If recovery cannot establish consistency,
the Store fails closed instead of allowing reads or writes against a possibly
inconsistent LTM.

System-owned root Tags keep their existing Vector exclusion behavior.

## Reads

Store reads return object projections with derived strength metadata. Normal
Retrieval paths use `strength > 0` as the retrievable-object condition.

Current Store-facing read helpers include:

```python
get_object(object_id, include_payload=False, root=None)
list_objects(object_type=None, include_payload=False, root=None)
get_links(object_id, direction, relation_type=None, link_state="active", root=None)
```

`get_links` supports outgoing, incoming, and both directions. Link lifecycle is
expressed through `link_state`, not object status.

## Meta-Memory Bootstrap

Meta-Memory bootstrap creates the four project-local protected root Tags:

```text
Sema Self
User Model
Safety
Others
```

Root Tags are ordinary `object_type=tag` MemoryObjects plus records in
`system_owned_objects`. They have no Payload, summary, provenance Link, or
preset hierarchy Link.

Root Tag object identity and protected structure are immutable. Store APIs
reject overwrites, title updates, summary updates, deletion/prune operations,
and object-type changes for protected roots. Ordinary objects may still link to
them through active `tagged_with` Links.

## Observer Boundary

The LTM Observer exposes metadata only:

```text
initialized
db_path
object_count
payload_count
link_count
derived_count
counts_by_object_type
counts_by_link_state
counts_by_relation
db_schema_version
retrieval_index_initialized
retrieval_index_count
maintenance runtime state
```

Observer reads never create or migrate the Store, expose Payload bodies,
perform Retrieval, run Maintainer, change derived state, write Event Stream, or
repair indexes.

## Deferred Work

- user-question workflow for unresolved contradictions;
- broader authorization and capability system;
- Revision records;
- in-place Payload updates;
- relation-specific observation tools;
- general shared ReAct runtime abstraction.
