# Short-Term Memory (STM)

STM is Sema's recent, high-fidelity Event Stream projection. It supplies a
chronological suffix of provider-valid messages without summarizing,
truncating, or reordering selected content.

## Responsibilities And Boundaries

STM is not a separate store. It reads immutable Event Stream messages and:

- preserves leading system messages outside the STM token budget;
- selects recent eligible projection units under the effective STM budget;
- exposes metadata-only selected and omitted ranges;
- hands older ranges to WM through a monotonic ingress watermark; and
- never summarizes or semantically retrieves content.

Working Memory owns compression after content leaves STM. Focus, LTM, and
historical Evidence remain separate consumers of the Event Stream.

## Projection Units

STM budget selection uses these units:

- an ordinary user message is one unit;
- an ordinary assistant message is one unit;
- an assistant message containing tool calls plus all immediately following
  matching tool results is one atomic `tool_bundle`; and
- unmatched legacy messages remain independent orphan units so projection
  never silently drops them.

Tool bundles enter and leave STM together. This preserves the provider
protocol requirement that an assistant tool call and its results remain
adjacent and complete.

`build_interaction_blocks()` remains available for Focus ingest, historical
Evidence, and user-turn range semantics. Interaction blocks are not STM budget
selection units.

## Selection Contract

STM selects one continuous newest suffix of eligible units:

1. Exclude every unit that starts at or before the effective WM ingress
   watermark. This also locks a bundle that intersects the watermark.
2. Walk backward from the newest eligible unit.
3. Add complete units while they fit.
4. Stop at the first unit that does not fit. Do not skip it to select older,
   smaller units.
5. Restore selected units to chronological order.

The newest unit's rendered token count is `STM_required`. When it exceeds the
formula budget but remains within the configured STM maximum, Capacity Planner
raises the current candidate budget enough to fit it. If it exceeds the
maximum, Sema returns a typed capacity error and does not call the Main Agent
provider.

## Tool Ingress Guard

Every Main Agent tool result is checked after execution and before Event Stream
append. The guard measures the whole current tool bundle against the effective
STM maximum.

When a result would make the bundle exceed that maximum:

- the full result is stored as an independent durable Event Stream artifact;
- active history receives a bounded `too_large` envelope;
- the envelope includes a preview, character and token counts, the artifact
  ID, and applicable next actions; and
- an artifact-write failure produces a small technical-error envelope and
  never falls back to appending the oversized body.

File results recommend ranged `read_file` or `get_overview`. Other tools
recommend narrower arguments. `get_overview` is navigation output and does not
replace reading relevant source ranges.

Model-generated oversized assistant text and oversized tool-call arguments are
not rewritten by this guard.

## WM Ingress Watermark

The effective ingress watermark is the maximum of persisted, runtime, and
staged values. Omitted units may stage their highest end slot. After a
successful answer, Runtime projects the complete post-answer history and
commits the staged watermark before WM and Focus maintenance.

The watermark only moves forward. Once a unit intersects or precedes it, that
unit cannot re-enter STM after later budget growth.

Every STM consumer uses the same effective watermark, including Context
Builder, WM candidate selection, Observer, status commands, eval metadata, and
`get_stm_evidence()`. Historical slot, range, interaction-block, and artifact
Evidence APIs remain available independently of STM selection.

## Current-Turn User Handoff

When the latest external user message leaves the current STM projection, WM
derives a current-turn pin from that same projection. The provider context
contains exactly one representation: the original user slot in STM or its WM
pin. The next external user message clears the old pin. Runtime-originated
messages using the provider `user` role do not participate.

## Observability

STM metadata uses unit terminology:

- `selected_units` and `omitted_units`;
- `selected_unit_count` and `omitted_unit_count`;
- `latest_unit_tokens` and `largest_unit_tokens`; and
- watermark and token totals.

Omitted metadata contains only kind, range, message count, and token count. It
does not contain message bodies.

## Implementation Map

- `memory_engine.stm_projection`: interaction blocks, projection units, and
  continuous-suffix selection.
- `memory_engine.capacity_planner`: structural STM requirement and effective
  budget planning.
- `memory_engine.tool_ingress`: provider-visible tool-result guard.
- `memory_engine.wm.ingress`: monotonic STM-to-WM watermark.
- `memory_engine.wm.pins`: current-turn external-user handoff.
- `memory_engine.context_builder`: provider projection assembly.
- `memory_engine.evidence`: bounded read-only STM and historical Evidence.
