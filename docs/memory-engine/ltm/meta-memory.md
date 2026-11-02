# Meta-Memory Bootstrap V1

## Purpose

Meta-Memory represents high-authority memory structures that describe Sema's
own memory architecture, conceptual boundaries, and system-level
classification. V1 implements only deterministic initialization of protected
root Tags. It does not run an LLM Agent or perform online Meta-Memory
maintenance.

The root Tags remain ordinary LTM MemoryObjects with `object_type=tag`.
Meta-Memory does not introduce another MemoryObject type.

## Root Tags

Each project-local LTM Store contains four system-owned root Tags:

```text
sema_self  -> Sema Self
user_model -> User Model
safety     -> Safety
others     -> Others
```

`Sema Self` includes Sema's own memory architecture. `Others` is a fallback for
material that cannot yet be placed under the first three roots; it is not a
default generic classification. Formation V4 links every new MemoryObject
directly to one or more roots with `tagged_with`. `Others` is mutually exclusive
with the other three roots.

V1 does not create a predefined child-Tag hierarchy. Formation root linking
uses these fixed roots only; ordinary Tag assignment and hierarchy remain
deferred.

## Bootstrap Contract

The canonical implementation is
`my_agent/memory_engine/ltm/meta_memory.py::bootstrap_system_tags()`.
It runs only for Event Stream projects and is invoked before Formation starts
discovering or processing WorkItems.

Bootstrap is deterministic and idempotent:

- every root uses a stable `mem_<uuid_hex>` ID;
- all four Tags and their protection records are created in one SQLite
  transaction;
- repeated calls perform an integrity check and do not create duplicates;
- a failed bootstrap prevents Formation from committing new MemoryObjects and
  leaves its WorkItems available for retry;
- rejected architecture selection does not initialize this state.

Each root Tag has only the minimum canonical content:

```text
object_type = tag
title = fixed root title
summary = absent
payload = absent
```

The roots have no `sourced_from` provenance Link and no preset hierarchy Link.

## Ownership And Protection

System ownership is not inferred from `object_type=tag`. It is recorded in the
separate `system_owned_objects` registry:

```text
tag_key
object_id
owner = meta_memory_agent
protected_kind = system_tag
created_at
```

This keeps authorization metadata out of the MemoryObject Core. Ordinary
MemoryWritePlans cannot overwrite protected root IDs, title/summary update APIs
reject protected roots, and future prune/delete paths must reject them as well.
Ordinary objects may still reference them through active `tagged_with` Links.

These controls are deterministic Store/API checks, not prompt-only policy. V1
does not implement the broader OS-like trust and authorization system or an
online capability-bearing Meta-Memory Agent.

## Runtime And Retrieval Boundaries

Bootstrap does not read user messages, perform semantic classification, call a
model, start ReAct, or participate in Formation, Retrieval, Maintenance, or
LTM Push decisions. Once created, the roots are visible as read-only
retrievable objects through the Formation Retrieval Profile.

After a successful Bootstrap, Formation Runtime reads the complete root list
once per Formation processing pass and injects only `object_id`, `tag_key`, and
`title` into each WorkItem projection. This is trusted structural input rather
than a Retrieval Service call. LTM Formationer selects roots semantically;
Runtime enforces target ownership, relation type, uniqueness, and `Others`
exclusivity.

The LTM Observer reports bootstrap completion, protected Tag count, keys, IDs,
titles, and protection kind. Observer reads never trigger bootstrap and never
return Payload content.

## Deferred Work

- Ordinary Tag assignment and hierarchy during Formation.
- Child-Tag creation and hierarchy maintenance.
- Online Meta-Memory Agent behavior.
- Cross-module trust, capability, authorization, and audit infrastructure.
- Migration policy for future changes to system-owned roots.
