# Agent Responsibilities

This document summarizes current Agent ownership. [Agent Registry](agent-registry.md) is the source of truth for registered Agents, stable identifiers, triggers, lifecycle events, and TUI status.

## Main Agent

The Main Agent handles ordinary user turns through the structured tool-calling loop. It owns user-facing responses and requests tools through the Python runtime. The runtime builds provider context from the Event Stream and Memory Engine projections.

Runtime Wake delivers eligible background results to the Main Agent. Main Agent turns are serialized, and Main Agent execution is mutually exclusive with LTM Maintainer execution. User input received while the Maintainer is active is rejected rather than queued.

## Memory Engine Agents

Memory Engine Agents perform bounded responsibilities over shared project memory:

- Focus Detector, Focus Ingester, and Focus Maintainer manage Focus Segments.
- Working Memory Maintainer updates the Working Memory projection.
- LTM Retriever performs explicit, bounded Long-Term Memory retrieval.
- LTM Push prepares the automatic Long-Term Memory context projection.
- LTM Formationer creates evidence-bound MemoryObjects from eligible Event Stream content.
- LTM Memorizer handles explicit durable-memory writes.
- LTM Maintainer reviews and maintains existing Long-Term Memory objects.

These Agents do not replace the Main Agent as the user-facing conversation owner. Their execution and wake behavior are defined by the Memory Engine and Runtime documentation.

## Supporting Runtime Calls

Provider preflight, setup tests, embeddings, reranking, deterministic runtime-clock calculations, and internal query or candidate operations are supporting calls or services; they are not separately registered semantic Agents unless listed in the Agent Registry.

## Tool Safety

Tool requests are classified and executed by the Python runtime. The TUI and Remote Gateway present confirmation choices and return the user's response; they do not implement separate permission policies.
