# Short-Term Memory (STM)

STM is Sema's recent, high-fidelity Event Stream workspace. It preserves the
latest interaction context for provider calls without summarizing, truncating,
or reordering its selected content.

## Responsibilities And Boundaries

STM is an Event Stream projection, not a separate source of truth. It:

- keeps recent trunk RAW events directly injectable;
- may retain tool output and intermediate execution details while work is
  unfolding;
- uses the effective Dynamic Context Budget rather than a fixed time window;
- does not summarize content, create Focus Segments, or perform retrieval; and
- never truncates an individual message or interaction block.

Working Memory owns compression of material that has left STM. Focus Segments
exist only in WM. The Event Stream remains the factual source for all layers.
See [Working Memory](wm.md) for RAW/ref to SUM maintenance and preflight
recovery.

## Projection Contract

STM Projection V1 is:

```text
STM = latest complete eligible interaction blocks that fit in the current budget
```

An interaction block contains:

```text
one user message
+ following Sema/tool/file/system messages
+ stops before the next user message
```

Leading system messages are preserved separately and do not count toward a
normal interaction block. Selection walks backward from the newest eligible
complete block, stops before a block that would exceed the budget, then restores
the selected blocks to their original order.

If the latest eligible complete interaction block alone exceeds the STM budget,
context construction fails with a clear user-visible error. STM does not split
or silently truncate that block.

## STM-Safe File Read Ingress

Large file reads are guarded before they enter the newest STM interaction
block. The `read_file` tool is no longer an all-or-nothing raw file ingestion
path: it supports bounded line ranges, bounded character ranges, returned
character caps, and explicit full-read requests.

`read_file` accepts:

- `path`
- `start_line`
- `line_count`
- `start_char`
- `char_count`
- `max_chars`
- `full_read`

Line ranges and character ranges are mutually exclusive. Line ranges are
navigation controls for normal text and source files; they are not a safety
guarantee, because one line may contain many thousands of tokens. Character
ranges are available for minified JSON, long TXT records, generated payloads,
and other files that do not have useful line breaks. All returned bodies remain
bounded by `max_chars` or the default safe cap.

File reads report one of three statuses:

- `ok`: the requested content was returned within the active limits.
- `warn_large`: the file or requested range is large, so the default response
  returns metadata plus a bounded first-100-line preview and recommends a
  range read or `get_overview`.
- `too_large`: the file is beyond the hard full-read limit, so full raw reading
  is refused and only metadata plus a bounded first-100-line preview is
  returned.

For `warn_large`, the Main Agent may explicitly request `full_read=true`, but
the result is still subject to the absolute safe cap. `too_large` files must be
inspected through line ranges, character ranges, or `get_overview`.

`get_overview` is a general navigation and triage tool for text files. It
accepts `path`, a task-specific `instruction`, and an optional `max_chars`.
Overview output is not source text and must not replace reading the relevant
raw ranges with `read_file`.

## Dynamic Budget

STM capacity comes from the effective STM budget produced by Dynamic Context
Budget and Budget Stabilizer. The pressure model, manual maximum overrides,
failure saturation, and stabilizer rules are defined in
[Context Builder and Context Budget](stm-wm-context.md#dynamic-context-budget).

The same effective budget must be used by Context Builder and Working Memory
candidate selection so they agree on which blocks are outside STM.

## WM Ingress Watermark

STM uses a one-way WM ingress watermark:

- Once a complete interaction block is omitted from STM, its end slot can be
  staged as the WM ingress watermark for the active turn.
- A final assistant message can itself push an older block outside STM. After
  the final answer succeeds, Runtime stages the complete post-answer history
  with the same effective STM budget, then persists the watermark before
  Working Memory and Segment Ingest maintenance run.
- Blocks whose `end_index <= wm_ingress_watermark_slot_seq` are locked out of
  future STM projections, even if later STM budgets grow.
- Budget growth therefore applies only to future blocks after the watermark.

Every STM consumer honors the effective watermark. This includes Context
Builder, WM candidate selection, status and observer projections, and the
read-only `get_stm_evidence()` API. Historical raw inspection remains available
through the separate Event Stream evidence APIs; it is not STM evidence.

## Context And Interfaces

Context Builder assembles provider context in this order:

```text
system prompt
LTM  automatic LTM Push projection when available
WM   active trunk timeline reminder
STM  latest complete raw interaction blocks
```

STM Projection provides the selected and omitted interaction-block metadata to
Context Builder. Omitted blocks may advance WM ingress; later WM maintenance
can summarize eligible older slots. STM itself does not invoke WM maintenance.

`/memory stm status` and Observer context previews are metadata-only. They
report budget, watermark, token counts, block counts, and selected or omitted
ranges without exposing message bodies.

## Implementation Map

- `memory_engine.stm_projection` defines interaction-block construction and
  projection selection.
- `memory_engine.wm.ingress` computes, stages, and persists the monotonic
  ingress watermark. The formal package API is exposed through
  `memory_engine.wm`.
- `memory_engine.context_builder` consumes STM Projection for provider context.
- `memory_engine.evidence.get_stm_evidence` exposes a bounded, read-only STM
  evidence preview using the same effective watermark.
- `tools.read_file_for_context` and `tools.get_file_overview_for_context`
  provide bounded file ingress before large raw file payloads can inflate the
  newest STM interaction block.
