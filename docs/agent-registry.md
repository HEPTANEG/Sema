# Sema Agent Registry

This document is the registry of Sema's semantic Agents.

## Registered Agents

| Agent                           | Stable ID / type                 | Source                                                              | Model call                                                                          | Trigger and execution                                                       | Lifecycle events                                                                        | TUI status                                                |
| ------------------------------- | -------------------------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Main Agent                      | `main_agent`                     | `my_agent/runtime/agent.py` and `my_agent/runtime/model_runtime.py` | Structured tool-calling loop through `call_chat_completion` or streaming equivalent | Every ordinary user turn; foreground and serialized                         | `turn_started`, `model_thinking`, `model_finished`, `final_answer`, error/cancel events | Foreground status; not a background activity              |
| Focus Detector                  | `focus-detector-v1`              | `my_agent/memory_engine/focus/detector.py`                          | One bounded JSON decision call                                                      | During the formal Main Agent context build; synchronous                     | `focus_detector_started/finished`, `focus_shifted`                                      | `Focus Detector is working` while the real call is active |
| Focus Ingester                  | `focus-ingester-v1`              | `my_agent/memory_engine/focus/ingest.py`                            | Bounded JSON assignment/update calls                                                | After an answer, in the background maintenance chain                        | `segment_ingest_started/finished`                                                       | `Focus Ingester is running`                               |
| Focus Maintainer                | `focus-maintainer-v1`            | `my_agent/memory_engine/focus/maintainer.py`                        | Bounded inspect/tool loop                                                           | After an answer, in the background maintenance chain                        | `full_segment_maintenance_started/finished`                                             | `Focus Maintainer is running`                             |
| Working Memory Maintainer       | `working-memory-maintainer-v1`   | `my_agent/memory_engine/wm/working_memory.py`                       | Bounded JSON summarize/keep-raw decisions                                           | After an answer, in the background maintenance chain                        | `working_memory_maintenance_started/finished`                                           | `Working Memory Maintainer is working`                    |
| LTM Retriever                   | `memory_retriever`               | `my_agent/memory_engine/ltm/memory_retrieval.py`                    | Isolated provider-native retrieval Agent loop                                       | Main Agent or LTM Maintainer request; may continue in background            | `retriever_started/finished`, wake events                                               | `LTM Retriever is running` while detached work is active  |
| LTM Push                        | `memory_push`                    | `my_agent/memory_engine/ltm/memory_push_ranking.py`                 | Query compilation and final selection calls                                         | Synchronous context projection, or coalesced full refresh in the background | `memory_push_started/finished` for full background refresh                              | `LTM Push is running` only for full background refresh    |
| LTM Formationer                 | `formation` / `ltm-formation-v4` | `my_agent/memory_engine/ltm/formation.py`                           | LTM Formationer call with optional read-only retrieval tools                        | After an answer, in the background maintenance chain                        | `ltm_formation_started/finished`                                                        | `LTM Formationer is running`                              |
| LTM Formationer (Backfill mode) | `formation`                      | `my_agent/memory_engine/ltm/formation_backfill.py`                  | Reuses the LTM Formationer path                                                     | Historical backfill worker or post-answer maintenance                       | `formation_backfill_started/finished`                                                   | `LTM Formationer (Backfill) is running`                   |
| LTM Memorizer                   | `memorize`                       | `my_agent/memory_engine/ltm/memorize.py`                            | Evidence-bound memory-write Agent call                                              | Explicit `memorize` tool; may run asynchronously with Wake                  | `memorize_started/finished` for async execution                                         | `LTM Memorizer is running`                                |
| LTM Maintainer                  | `ltm_maintainer`                 | `my_agent/memory_engine/ltm/maintenance_runner.py`                  | Multi-stage bounded maintenance Agent loop                                          | Explicit or automatic LTM maintenance; background execution                 | Existing `ltm_maintenance_started`, completion/failure and maintenance events           | `LTM Maintainer is running`                               |

Canonical LTM Agent names are **LTM Retriever**, **LTM Push**,
**LTM Formationer**, **LTM Memorizer**, and **LTM Maintainer**.
These are semantic/display names, not storage identifiers. Existing process
types, prompt versions, tool names, module paths and lifecycle event keys remain
stable. Formation Backfill is an execution mode of LTM Formationer, not a sixth
Agent; its separate activity label identifies the backfill task.

## Non-Agentic Model Calls

The following calls use a model but are not independent semantic Agents and
therefore do not receive separate status-bar rows:

- Vision provider calls used by the image-context tool.
- LTM Push Query Compiler and Rank 1/2/3 operations. They are aggregated
  under LTM Push.
- Provider preflight and setup connection tests.
- Read-only tool calls made inside Formation, Focus Maintainer, LTM Maintainer,
  Retriever, and Memorize.
- Deterministic Temporal, Meta-Memory, candidate-pool, embedding, and storage
  operations.

## Status Contract

Background status events are temporary UI state. A `*_started` event adds one
activity, and its matching finish, completion, failure, cancellation, or
early-return event removes it. Multiple activities may be visible in the same
process-status row. No background status is persisted as a conversation
message, and background activity does not change the semantic ownership of the
Main Agent.

The Backend/Core remains the source of truth for Agent execution, model
selection, tools, permissions, persistence, and memory effects. The TUI only
maps lifecycle events to labels and presentation state.
