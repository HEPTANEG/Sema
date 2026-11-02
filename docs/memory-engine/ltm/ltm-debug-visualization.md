# LTM Debug Visualization

## Scope

LTM Debug Visualization is a local human-facing debug tool for inspecting the
Long-Term Memory graph. It exists because the canonical LTM state is stored in
SQLite, which is difficult to inspect directly when checking whether
MemoryObjects, MemoryLinks, derived strength, and maintenance metadata are
reasonable.

The visualization is not part of LTM formation, retrieval, memory push, or
maintenance reasoning. It is an external read-only view over the current Store
state.

## Command

The entry point is:

```text
/memory ltm visualize
```

On success the command:

1. reads the current LTM SQLite database;
2. writes a timestamped `snapshot.json`;
3. writes a standalone `graph.html`;
4. opens `graph.html` locally;
5. returns the generated artifact paths and counts.

The command opens only `graph.html`. It does not open `snapshot.json`.

If LTM is not initialized, the command returns a clear `not_initialized` result
and does not initialize, migrate, or create `ltm.db`.

## Artifact Location

Visualization artifacts live under the LTM memory area:

```text
.sema/memory/ltm/visualizations/<utc-timestamp>/
  snapshot.json
  graph.html
```

These files are local debug artifacts, not canonical LTM state. They may contain
full memory payload content. Treat `.sema/memory/ltm/visualizations/` as
sensitive local debug output. The visualization tool must not automatically
publish, upload, share, or write these artifacts into Event Stream.

## Boundaries

The visualization tool must not:

- call a model;
- write Event Stream entries;
- connect to the Maintainer ReAct loop;
- run LTM Maintainer;
- mutate canonical LTM tables;
- initialize or migrate LTM storage;
- emit Maintainer UI events;
- depend on npm, a dev server, CDN access, or an external build step.

The implementation may reuse Store constants and path helpers, but it opens
`ltm.db` through a read-only SQLite connection.

The current implementation is in:

```text
my_agent/memory_engine/ltm/ltm_visualization.py
```

The slash command is dispatched through:

```text
my_agent/commands/structured_commands.py
```

## Snapshot

`snapshot.json` is the stable machine-readable representation used by the HTML
view and by future diff tooling. It is a generated debug snapshot, not a source
of truth.

Top-level shape:

```json
{
  "metadata": {},
  "nodes": [],
  "links": [],
  "maintenance": {},
  "health": {}
}
```

Metadata includes generation time, database path, schema version, object count,
link count, and Event Stream provenance node count.

MemoryObject nodes include:

- `kind`;
- `id`;
- `object_id`;
- `object_type`;
- `title`;
- `summary`;
- `created_at`;
- `updated_at`;
- `payload`;
- derived counters and strength;
- source slot ranges;
- root-tag or system-owned display markers when available.

Link records include:

- `link_id`;
- source and target display ids;
- `relation_type`;
- `link_state`;
- timestamps;
- physical direction;
- strength effect metadata.

The snapshot includes both active and inactive MemoryLinks. Inactive links are
important for debugging maintenance behavior and are rendered by default.

## Event Stream Provenance Nodes

Event Stream provenance appears only when it is part of the LTM graph.

The visualization creates Event Stream slot or slot-range nodes only for
`derived_from` links whose target is an Event Stream slot range. It does not
render unrelated, ignored, or unreferenced Event Stream slots.

Labels:

```text
slot N
slots N-M
```

Event provenance inspector content is intentionally lightweight: slot/range
metadata and a short preview. Full Event Stream content is not used as a graph
label.

## HTML Graph

`graph.html` is a single standalone HTML file with embedded JSON, CSS, and
JavaScript. It is intended for local browser inspection.

The current graph uses a small custom browser-side SVG force layout:

- spring force for links;
- repulsion between nodes;
- collision padding;
- center force for MemoryObjects;
- weak outer radial force for Event Stream provenance nodes;
- bounded ticks and velocity clamping for smoother settling.

MemoryObjects are rendered as circles. Event Stream provenance nodes are
rendered as rounded capsules and are biased toward the outside of the graph
while still participating in link forces.

The graph supports:

- pan;
- zoom;
- node drag;
- click-to-inspect;
- selected-node highlighting;
- short internal node labels;
- relation-colored links;
- mid-line direction arrows;
- inactive links as gray dashed lines;
- object and link legends.

## Visual Encoding

MemoryObject color encodes `object_type`:

- `assertion`: blue family;
- `episode`: violet family;
- `tag`: green family.

Event Stream provenance nodes use neutral gray.

Within each MemoryObject type, shade or intensity encodes derived `strength`.
Object type remains the primary visual category; strength is the secondary
signal.

Link color encodes `relation_type`:

- `derived_from`: blue;
- `tagged_with`: green;
- `supports`: cyan;
- `contradicts`: red;
- `supersedes`: orange;
- inactive links: gray dashed.

The graph separates physical edge direction from strength effect. The edge
direction is shown visually, while strength effect is exposed in link metadata.

Relation strength effects:

- `derived_from`: source gain;
- `supports`: target gain;
- `tagged_with`: neutral both;
- `contradicts`: neutral both;
- `supersedes`: target loss.

## Inspector

The inspector is optimized for human debugging rather than schema order.

For MemoryObjects it shows:

1. title;
2. type;
3. strength;
4. summary;
5. created timestamp;
6. updated timestamp;
7. primary payload body;
8. collapsed raw payload;
9. collapsed debug metadata.

Collapsed debug sections include:

- object id;
- derived counters;
- source slots;
- incoming links;
- outgoing links.

The graph already visualizes links and provenance, so these structural details
are available but do not dominate the first screen.

For Event Stream provenance nodes, the inspector shows slot/range metadata,
preview text, and collapsed link/slot details.

## Failure Behavior

Opening the local HTML is best-effort. If artifact generation succeeds but the
local browser open fails, the command still returns the generated paths and an
open failure status. Paths remain the reliable output.

## Future Diff Direction

Future snapshot diff support can compare generated `snapshot.json` files without
writing new canonical state. Useful diff signals include:

- added or removed objects;
- changed titles or summaries;
- changed payload presence or payload hashes;
- added, inactive, removed, or changed links;
- strength deltas;
- derived counter deltas;
- maintenance cursor movement;
- maintenance report deltas.

Diff support is intentionally separate from the current visualization command.

## Acceptance Criteria

- Missing LTM returns `not_initialized` and does not create or migrate `ltm.db`.
- Initialized LTM exports `snapshot.json` and `graph.html`.
- The command opens only `graph.html` on successful slash-command execution.
- The snapshot includes all MemoryObjects.
- The snapshot includes active and inactive MemoryLinks.
- The snapshot includes only referenced Event Stream provenance nodes.
- The HTML renders without a dev server or external dependencies.
- Object type, strength, link type, and inactive link state are visually encoded.
- Clicking a MemoryObject opens a payload-first inspector.
- Clicking an Event Stream provenance node opens slot/range details and preview.
