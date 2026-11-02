# Context Builder And Context Budget

This document describes the implemented Event Stream provider-context assembly
and Dynamic Context Budget. STM projection and its one-way WM ingress boundary
are defined in [Short-Term Memory](stm.md). Working Memory compression and
preflight recovery are defined in [Working Memory](wm.md).

The implemented Event Stream context stack is:

```text
system prompt
LTM  omitted in V1
WM   Focus-aware working-memory projection
STM  latest complete raw interaction blocks
```

The goal is to keep recent work high fidelity and inject compact older active
trunk material when useful. See [Short-Term Memory](stm.md) for STM selection,
watermark, evidence, and status contracts.

## Context Builder

Context Builder is the Event Stream provider-context assembly layer.

Provider order:

```text
system prompt
LTM: omitted in V1 when empty
WM: Focus-aware working-memory projection
STM: latest complete raw interaction blocks
```

WM projection format:

```text
<working-memory>
Resolved:
- Completed topic

Inactive:
- Parked topic: compact summary

Active:
Current topic: current summary
[42] user: ...
[43] sema: ...
</working-memory>
```

The projection separates logical WM residency from prompt injection. Focus
assignment rows and segment ranges determine which slots remain logically
resident. The dynamic WM budget, normally between 10,000 and 50,000 tokens,
limits the final rendered `<working-memory>` text rather than the raw tokens of
all resident slots.

WM selection:

- load all non-system active-trunk records before the first STM selected slot;
- render detailed records only for active Focus segments;
- render inactive segments as title and summary, and resolved segments as title
  only;
- reserve at most 20 percent of the WM budget, capped at 10,000 tokens, for
  Focus metadata, with active metadata ahead of inactive and resolved metadata;
- reserve up to 10 percent of the detail pool for recent audited-unassigned and
  unaudited unowned records, returning unused capacity to active details;
- pack detail candidates newest first, then render selected records in slot
  order;
- truncate an oversized inactive summary only in the rendered projection;
- fail with the existing oversized-WM error only when the newest active detail
  and required metadata cannot fit.

When Focus state exists but the active segment has no ranges, Context Builder
still renders bounded Focus metadata and unowned context. It uses the legacy raw
timeline suffix only when no Focus state exists. Online Formation and Formation
Backfill continue to use the raw suffix selector, so Focus-aware prompt
projection does not change Formation latency or coverage boundaries.

Projection status reports injected and resident state independently.
`selected_records` and `selected_slot_seqs` identify records whose details were
actually injected, while `selected_tokens` counts the complete rendered WM
text. `resident_slot_count` and `resident_raw_token_count` describe logical WM
residency even when inactive or resolved records are represented only by
metadata.

Recovery rule:

- Normal successful context builds do not run synchronous WM maintenance.
- Large file reads should be bounded before they enter STM. The `read_file`
  tool returns `ok`, `warn_large`, or `too_large` and recommends ranged reads or
  `get_overview` for large files; this is the primary guard for the known
  all-or-nothing file-ingress failure mode.
- If context build fails because the latest STM block or one WM slot exceeds
  the current dynamic budget, Sema first saturates the failed target budget to
  its max and rebuilds once if the content can fit inside that max.
- If the failed content still exceeds the target max budget, Sema runs one
  Working Memory maintenance pass before the provider call.
- After the optional preflight pass, Context Builder rebuilds provider context
  once.
- If context build still fails, Sema returns the existing clear context build
  error.

Typical SUM use cases:

- long assistant answer whose exact wording is no longer needed.
- long tool output.
- pasted logs.
- PDF / file content preview.
- large HTML or rich text content.

Debug commands:

```text
/memory context status
/memory budget status
/memory budget set stm <tokens>
/memory budget set wm <tokens>
/memory budget set both <tokens>
/memory budget reset
```

Status output is metadata-only. It reports budget source, STM/WM budget ranges,
raw budgets, stable/effective budgets, watermark metadata, context pressure
subscores, selected token counts, and selection counts. It does not show
message bodies.

## Dynamic Context Budget

Dynamic Context Budget is the deterministic pressure model that controls STM
and WM budgets in Event Stream mode. The implementation is in
`memory_engine.dynamic_context_budget`.

### Inputs

The model uses these runtime signals:

- recent user-turn count and continuous-session duration;
- the latest user-to-user gap;
- tool-call count in the current Main Agent run;
- continuous-session token totals, average, and maximum from original Event
  Stream event token counts;
- token count of the latest user message;
- context-build failure feedback for the current provider-context attempt.

Tool failures, repeated commands, and error-message counts are not inputs to
the budget formula. The assistant-to-user gap is also excluded.

A continuous session is formed by walking backward through non-system Event
Stream slots while each adjacent gap is at most 600 seconds. Its total duration
can therefore exceed 600 seconds.

### Pressure Model

The model uses saturating and decay curves:

$$
g(x,\tau) = 1 - e^{-x/\tau},
\qquad
d(x,\tau) = e^{-x/\tau}
$$

Interaction continuity is:

$$
\begin{aligned}
I &= 0.30 f_{\mathrm{turns}}
  + 0.30 f_{\mathrm{session}}
  + 0.40 f_{\mathrm{user\_gap}} \\
f_{\mathrm{turns}} &= g(n,4) \\
f_{\mathrm{session}} &= g(d,1200) \\
f_{\mathrm{user\_gap}} &= d(g_u,600)
\end{aligned}
$$

Here, `n` is the recent user-turn count, `d` is continuous-session duration
in seconds, and `g_u` is the latest gap from the previous user turn to the
current user turn. It is not averaged.

State pressure is based on recent execution activity:

$$
\begin{aligned}
S &= f_{\mathrm{activity}} \\
f_{\mathrm{activity}} &= g(a,4) \\
a &= \mathrm{tool\_count}
\end{aligned}
$$

Continuous-session token load is:

$$
\begin{aligned}
L &= 0.15 f_{\mathrm{token\_total}}
  + 0.30 f_{\mathrm{token\_average}}
  + 0.15 f_{\mathrm{token\_max}}
  + 0.40 f_{\mathrm{current\_user}} \\
f_{\mathrm{token\_total}} &= g(T,20000) \\
f_{\mathrm{token\_average}} &= g(A,4000) \\
f_{\mathrm{token\_max}} &= g(M,10000) \\
f_{\mathrm{current\_user}} &= g(U,4000)
\end{aligned}
$$

`T`, `A`, and `M` are the total, average, and maximum original event token
counts in the continuous session. `U` is the token count of the latest user
message. That message is also represented separately in `L` after it has
been persisted into the Event Stream.

The normal pressure and budget factors are:

$$
\begin{aligned}
F_{\mathrm{STM}} &= 0.40I + 0.20S + 0.40L \\
F_{\mathrm{WM}} &= \max(I,S,L) \\
P_{\mathrm{context}} &= F_{\mathrm{STM}}
\end{aligned}
$$

Context-build failure remains a separate reactive override, not a normal
predictive score.

### Budget Mapping

The default runtime ranges are:

```text
STM: 1000..50000 tokens
WM:  1000..50000 tokens
```

$$
\begin{aligned}
\mathrm{STM}_{\mathrm{raw}} &= \mathrm{STM}_{\min}
  + F_{\mathrm{STM}}(\mathrm{STM}_{\max}-\mathrm{STM}_{\min}) \\
\mathrm{WM}_{\mathrm{raw}} &= \mathrm{WM}_{\min}
  + F_{\mathrm{WM}}(\mathrm{WM}_{\max}-\mathrm{WM}_{\min})
\end{aligned}
$$

Manual budget overrides are project-local maximum limits, not fixed budgets.
Each STM or WM maximum may be set between 2000 and 200000 tokens. The dynamic
formula runs inside the resulting effective range:

$$
\mathrm{effective\_min} =
\min(\mathrm{default\_min},\mathrm{manual\_max\_override})
$$

### Context-Build Failure Pressure

Context-build failure is reactive feedback, not a normal predictive signal. For
the failed target:

$$
\mathrm{context\_build\_failure}=1
\quad\Longrightarrow\quad
P_{\mathrm{context}}=1
$$

If the failed target is STM, `F_STM` is set to 1. If it is WM, `F_WM` is set
to 1. The failed target therefore uses its effective maximum for the rebuild.
If the required content still exceeds that maximum, one Preflight Working
Memory Recovery pass runs before one rebuild.

### Budget Stabilizer

The stabilizer tracks `context_build_seq` for every provider context build,
including internal ReAct and tool-call steps. For a budget target with a prior
stable value `B_{\mathrm{prev}}` and raw value `B_{\mathrm{raw}}`:

$$
B_{\mathrm{stable}} =
\begin{cases}
B_{\mathrm{raw}} & B_{\mathrm{raw}} > B_{\mathrm{prev}} \\
B_{\mathrm{prev}} & \text{during a hold or inside the 10\% deadband} \\
\max(B_{\mathrm{raw}},\,0.80B_{\mathrm{prev}}) & \text{otherwise}
\end{cases}
$$

When the raw budget reaches at least 80% of its effective maximum, the target
is held for three context-build steps. A context-build failure holds the failed
target at its effective maximum for five steps. The stabilizer is applied after
the raw dynamic budget and manual maximum override are calculated.

Dynamic Context Budget does not use LLMs, embeddings, semantic retrieval,
graph traversal, or output-budget control. Its pressure result is process-local;
stabilizer and manual maximum state are project-local runtime state.

## Design Validation

The implementation and tests cover:

- sporadic simple questions;
- continuous sessions with short current inputs;
- tool activity and current-input token load;
- empty signal values and the 1000-token minimum;
- the distinct STM weighted-sum and WM maximum aggregations;
- context-build failure forcing only its target to the effective maximum;
- Stabilizer upward, hold, deadband, and downward behavior.

The resulting boundaries are:

- zero-valued signals map both budgets to their 1000-token minimum;
- STM responds to all three scores through its weighted sum;
- WM follows its highest score, so any one dominant signal can raise WM;
- current user input contributes separately to `L`, in addition to its
  inclusion in persisted continuous-session token statistics;
- context-build failure saturates only the failed target and invokes preflight
  recovery only when the effective maximum remains insufficient;
- Budget Stabilizer prevents rapid downward oscillation while allowing an
  immediate upward response.

Runtime defaults are `1000..50000` tokens for both STM and WM; manual maximum
overrides may change the effective ranges.
