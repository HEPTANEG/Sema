# Context Builder And Context Capacity

Context Builder assembles the Main Agent provider context from Event Stream
projections. Capacity Planner computes structural STM and WM requirements before
state is committed. STM selection is defined in [Short-Term Memory](stm.md),
and WM compression and pins are defined in [Working Memory](wm.md).

## Provider Order

The provider-visible order is:

```text
system prompt
WM: Focus-aware timeline, including an optional current-turn pin
LTM: automatic Memory Push content when available
STM: continuous newest suffix of raw projection units
```

Leading system messages remain outside the STM budget. WM uses active Event
Stream representations, while STM uses immutable original messages.

## WM Projection

When Focus state exists, WM renders resolved titles, inactive summaries,
active metadata and details, and bounded unowned details. Without Focus state,
it renders a chronological active-trunk suffix. The complete rendered
`<working-memory>` wrapper counts against the WM budget.

The optional current-turn user pin is rendered before ordinary WM details and
shares the same budget. Its source slot is removed from ordinary WM candidates
to prevent duplicate context.

WM selects recent details under its budget; it does not require the whole WM
window to fit. `WM_required` includes only the minimum valid projection:

```text
minimum WM wrapper
+ current-turn pin, when active
+ newest eligible WM record
+ mandatory Focus metadata
```

## Dynamic Context Budget

Dynamic Context Budget computes formula budgets from interaction continuity
`I`, execution state `S`, and token load `L`. Default ranges are 1,000 to
50,000 tokens for both STM and WM. Project-local manual overrides change the
maximum, not the current fixed budget.

### Interaction Continuity

```text
f_turns    = 1 - exp(-recent_turn_count / 4)
f_session  = 1 - exp(-continuous_session_duration_seconds / 1200)
f_user_gap = exp(-user_gap_seconds / 600)

I = 0.30 f_turns + 0.30 f_session + 0.40 f_user_gap
```

`user_gap_seconds` is the latest external user-to-user gap. A missing gap
contributes zero. The assistant gap is not an input.

### Execution State

```text
f_activity = 1 - exp(-recent_tool_count / 4)
f_failure  = 1 - exp(-continuous_session_context_budget_failure_count / 2)

S = 1 - (1 - f_activity)(1 - f_failure)
```

A budget failure means structural required capacity exceeded the capacity
already covered by the current formula and Stabilizer state. It does not mean
provider context ultimately failed. STM and WM share one counter; if either or
both trigger a floor in one real Context Build, the counter increments once.
The new count affects `S` beginning with the next build.

The counter resets when the continuous session key changes. This reset does not
reset Stabilizer budgets.

### Token Load

```text
f_total   = 1 - exp(-recent_session_token_total / 20000)
f_average = 1 - exp(-recent_session_token_average / 4000)
f_max     = 1 - exp(-recent_session_token_max / 10000)
f_current = 1 - exp(-current_user_message_tokens / 4000)

L = 0.15 f_total + 0.30 f_average + 0.15 f_max + 0.40 f_current
```

Continuous-session token statistics use original Event Stream event tokens.
The current external user message is already persisted before this calculation.

### Target Factors

```text
F_STM = 0.35 I + 0.30 S + 0.35 L
F_WM  = max(I, S, L)
```

Formula budgets map each factor linearly into its effective configured range.
Tool failures, repeated commands, textual error matching, test commands, edit
counts, retry phrases, and assistant response delay are not budget inputs.

## Structural Requirements

Formula budgets are not sufficient by themselves because provider-valid units
must fit whole.

```text
STM_required  = rendered tokens of the newest eligible STM atomic unit
STM_candidate = max(STM_base, STM_required)

WM_required   = rendered tokens of the minimum valid WM projection
WM_candidate  = max(WM_base, WM_required)
```

Required floors participate in normal stabilization. The persisted stable
budget covers the same continuing requirement on later builds, so one
persistent requirement does not repeatedly increment the failure counter.

## Capacity Planner Sequence

One real Main Agent provider build performs this sequence:

1. Read the current continuous-session failure count and Stabilizer state.
2. Compute `I`, `S`, `L`, `STM_base`, and `WM_base`.
3. Measure `STM_required`, preview the STM candidate, and build STM.
4. Derive the current-turn pin from that same STM projection.
5. Measure `WM_required` from the resulting boundary.
6. If WM exceeds maximum, perform at most one exact-slot recovery and measure
   once more.
7. Determine one shared failure outcome.
8. Persist failure count, `context_build_seq`, and both stabilized budgets in
   one Stabilizer-state write.
9. Stage the STM-to-WM watermark and persist the current-turn pin.

Observer, Evidence, status commands, and outbound capacity checks run the same
planning logic without incrementing sequence or failure count, saving
Stabilizer state, staging a watermark, creating a pin, or compacting WM.

## Budget Stabilizer

For a candidate budget `B_raw` and prior stable budget `B_prev`:

```text
B_raw > B_prev
    rise immediately

inside an active hold or within a 10% deadband
    retain B_prev

otherwise
    max(B_raw, 0.80 * B_prev)
```

Reaching at least 80% of an effective maximum starts a three-build hold. There
is no failure-specific maximum saturation or failure hold. Session reset clears
the shared failure counter but does not reset Stabilizer state.

## Above-Maximum Behavior

- A current external user instruction whose newest STM unit exceeds the STM
  maximum returns a typed `too_large` capacity error. The provider is not
  called and the instruction is not silently summarized.
- Main Agent tool results are guarded before Event Stream append. Full output
  becomes a durable artifact and active history receives a bounded envelope.
- An oversized WM RAW/ref receives one exact-slot SUM attempt. If no usable SUM
  results, a bounded active REF replaces the active representation while the
  immutable original remains available.
- An oversized pin is removed rather than summarized; its source then follows
  ordinary WM handling.

Model-generated oversized assistant text and oversized tool-call arguments are
outside the current guard.

## Removed Failure Retry

Context construction no longer parses exception strings, writes transient
`context_build_failure` runtime state, forces a target to maximum after an
exception, runs top-three interaction-block Preflight Recovery, or rebuilds
Context automatically. Exact-slot WM recovery is proactive planning, not an
exception retry.

## Observability

Metadata-only status surfaces expose:

```text
continuous_session_context_budget_failure_count
f_failure

stm_base_budget
stm_required_tokens
stm_candidate_budget
stm_effective_budget
stm_floor_applied

wm_base_budget
wm_required_tokens
wm_candidate_budget
wm_effective_budget
wm_floor_applied

capacity_plan
capacity_recovery_action
```

They also report pin source/token metadata, selection ranges, watermark state,
and Stabilizer sequence without exposing omitted message bodies.
