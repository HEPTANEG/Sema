# LTM Retriever Runtime

Normal source reads use the Store's live-object and live-link views. Evidence is
revalidated before publication and idempotent replay; deleted evidence yields a
technical `stale_evidence` result rather than a successful empty checkpoint.
Maintainer observation still accepts weak live objects. No Push-specific 0.1
threshold is added to Retriever.

## Scope

This document describes the implemented Runtime for the stable Memory
Retrieval contract in [LTM Retriever](ltm-memory-retrieval.md). It
covers Session ownership, the background Worker, result delivery, recovery,
and observability. Shared Store-facing services remain documented in
[LTM Structured Retrieval](ltm-structured-retrieval.md). Wake scheduling,
delivery, and provider projection are documented in the standalone
[Runtime Wake](../runtime-wake.md) module.

The Main Agent's direct `get_object` tool follows the ordinary synchronous
tool path. It uses the shared read-only observation implementation and does not
create a Retrieval Session, Worker, Wake Event, or continuation.

## Session and Worker Ownership

The lifecycle below describes the default Main Agent profile; Maintainer-specific
waiting, ownership, and side-effect differences are listed under Current Boundary.

`RetrievalSessionManager` stores the one process-local Session in
`runtime_state`. A Session owns:

- the immutable original Query;
- the Retrieval Agent's internal message history;
- its continuation handle and latest request/result fingerprint;
- Session-local Candidate Pools and observed/Evidence object IDs;
- a Session-local integer Object Reference Map used only at the model boundary;
- status, Worker reference, generation, and last-activity metadata.

There is at most one Retrieval Worker for the Session. The Worker is created on
the first Query and remains available while the Session is open. After a
checkpoint it is idle, not consuming model or search resources, and can resume
from retained history when a continuation arrives. A new independent Query is
rejected until the old Session is closed. The default idle Session TTL is 30
minutes; continuations do not survive process restart.

## Synchronous Window and Background Batches

Each initial Query or continuation submits a command to the Session Worker and
waits up to five seconds. A first result within that window is returned as the
ordinary Tool Result. If the window expires, the public call returns the
ordinary `running` Tool Result with the continuation handle while the Worker
continues independently.

The Worker uses at most 20 internal tool calls per search batch. This is a
technical boundary, not normally a completion condition. When a batch reaches
the boundary without a terminal outcome, the next batch may continue from the
retained Agent history. Runtime records operation signatures, newly observed
Candidate object IDs, and newly observed source Evidence. If consecutive
batches repeat known operations without producing new Candidates or Evidence,
the Worker stops after the configured no-progress threshold. It returns a
checkpoint when prior Evidence is available, otherwise a technical
`no_progress` result. No automatic Light/Full mode or fixed service order is
imposed.

For the Maintainer caller, the initial batch and every automatic continuation
after `tool_limit` share one absolute 120-second technical deadline. Starting a
new public `continuation_id` request creates a new delegation and a new deadline;
the deadline is not reset merely because the Worker starts another batch. When
the deadline is reached, Runtime returns `technical/retrieval_timeout` with the
continuation ID, batch count, and cumulative tool-call count. This result means
that retrieval did not converge within the technical limit; it does not mean
that no matching memory exists and does not fail the surrounding maintenance run.

The default Main Agent caller retains its existing per-batch timeout behavior.

After producing `feedback`, the Worker continues searching without waiting for
Main-Agent acknowledgement. It may produce later feedback or a checkpoint,
subject to the Session's Wake backpressure. `need_clue` and `need_context`
pause the Worker until a continuation supplies the requested input.

## Result Delivery

The Worker publishes the existing Retrieval result kinds:

```text
running | feedback | checkpoint | need_clue | need_context | technical | closed
```

The first synchronous result is delivered directly to the tool caller. Results
after a detached `running` call are sent through Runtime Wake. Wake does not
create a second public Retrieval API and does not change the Retriever's
payload. A Retriever Wake is classified as `tool_result_like` for the target
Main-Agent turn: the turn still receives STM/WM and runs the Main Agent, but
does not trigger Focus Detector or LTM Push. Provider compatibility may use
`role=user`; Event Stream provenance remains explicitly `source=retriever`.

Wake delivery is retried with a stable `wake_id`. At most one undelivered
Retriever feedback is held per Session; the Worker pauses before producing a
second feedback and resumes after delivery. Feedback is ordered and never
coalesced. Main-Agent turns remain serialized. A user input has priority over a
Wake arriving at the same time; if they are merged, the real user input keeps
the normal user processing path.

## Continuation, Preemption, and Close

Main-Agent `message`, clue, or Context Snapshot input cooperatively preempts
the current Worker command before scheduling further calls, then resumes the
same Session with preserved history. In-flight calls are checked again when
they return; late results from a preempted command are not published.

`close=true` is the only public termination operation. It immediately marks the
Session closed, cancels the Worker, prevents future scheduling, discards
undelivered Wake events for that Session, and invalidates late Worker results.
There is no public `busy`, `run_id`, or separate `cancel` parameter. Runtime
timeouts and cancellation signals are technical controls and do not determine
whether the Main Agent considers the memory task complete.

## Recovery and Idempotency

Technical provider, tool, format, timeout, cancellation, unsupported-query,
and no-progress outcomes are returned as resumable `technical` results
whenever the Session remains valid.
Existing Evidence and already delivered feedback are not rolled back. The
original Query, Agent history, Candidate Pools, and observed IDs are retained.
An identical request replays its cached result; a new continuation message is
required to request further search. Already surfaced object IDs are not
surfaced again, and persisted Wake events are not replayed during recovery.

An interrupted tool batch preserves valid assistant/tool pairs in the internal
history. Unexecuted calls are represented as `not_executed` rather than empty
search results, and resumption lets the Retriever choose a new action. A
provider response that reaches its generation limit is treated as a technical
format problem, not as evidence that memory is absent.

## Evidence and Side Effects

The Runtime accepts Evidence only from source observation tools. Candidate hits,
Candidate Pool projections, and Rank outputs remain internal candidates. An
Evidence-bearing `checkpoint` performs successful-use accounting once per
Session/object ID and may trigger the existing LTM Push refresh hook.
`feedback`, candidate-only results, `need_*`, technical results, and closed
Sessions do not trigger those side effects.

The Runtime keeps its Session, history, Candidate Pools, and reasoning out of
Event Stream. Only Main-Agent-facing Wake delivery is persisted as an Event
Stream message, with explicit semantic `source` metadata so downstream
Formation, Focus, Working Memory, and LTM Push can distinguish Retriever
content from a new user utterance.

## Observability and Performance

Retriever trajectories record model requests, internal tool calls, validation
failures, checkpoints, and technical outcomes. Runtime observer metadata reports
request count, tool/model counts, elapsed time, context usage, result kind, and
successful-use status without exposing private Agent reasoning to the Main
Agent.

Performance is evaluated separately for first useful feedback, background
search throughput, and final checkpoint latency. The five-second synchronous
window, 20-call batch ceiling, per-tool bounds, provider timeout, and
no-progress threshold are technical safety controls. A no-progress result does
not claim that memory is absent; it only stops an unchanged search state.

## Current Boundary

The default Main Agent caller retains one Session in its runtime state and its
existing asynchronous delivery. Maintainer uses isolated invocation-local state,
the same Worker and read profiles, and synchronous result notification. The trusted
caller profile permits checkpoint/need_clue and restricted technical outcomes
(not feedback/need_context), controls
usage accounting, and never changes read visibility. Explicit roots flow through
candidate generation, source reads, and trajectory.

Runtime closes the maintenance-owned Session after successful plan compilation
and before Store submission, and on all maintenance exit paths. A rejected compile
keeps it; a failed submission does not restore it. Waiting calls are released on
close, and late results are discarded. Resume retains the maintenance run ID but
starts fresh retrieval state. Retrieval failures are technical tool outcomes, not
automatic maintenance failures. No cross-process continuation or general Session
scheduler is introduced.
