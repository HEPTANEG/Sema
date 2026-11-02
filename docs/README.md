# Sema Documentation

This directory contains the maintained user, architecture, Memory Engine, and
contributor documentation for Sema. Start with the project overview in the
[root README](../README.md), then use the paths below according to what you
want to understand or change.

Sema 0.0.1 is an early Windows-first developer snapshot. These documents
describe verified current behavior unless a section is explicitly labeled as
a design direction or future work.

## Using Sema

1. [Installation and first-run setup](installation.md) covers dependencies,
   model configuration, local embedding and reranking models, and launch steps.
2. [TUI](tui.md) describes the terminal interface, commands, panels, streaming,
   configuration, and Backend/Core connection.
3. [Tool Calling and Safety](tool-calling.md) explains structured tool calls,
   confirmation, permissions, and unattended operation.
4. [Remote Control](remote-control.md) documents the optional Telegram Gateway
   and its access and lifecycle boundaries.

## Memory Engine

The Memory Engine is the center of Sema's architecture. Begin with the
[Memory Engine overview](memory-engine/README.md), then follow the subsystem
documents as needed:

- [Design Principles](memory-engine/design-principles.md)
- [Event Stream](memory-engine/event-stream.md)
- [Short-Term Memory](memory-engine/stm.md)
- [STM and WM Context](memory-engine/stm-wm-context.md)
- [Working Memory](memory-engine/wm.md)
- [Focus](memory-engine/focus.md)
- [Runtime Wake](memory-engine/runtime-wake.md)
- [Implementation Status](memory-engine/implementation-status.md)
- [Memory Push](memory-engine/ltm/ltm-memory-push.md)
- [Memory Retrieval](memory-engine/ltm/ltm-memory-retrieval.md)
- [Memory Maintainer](memory-engine/ltm/ltm-memory-maintainer.md)

The Memory Engine overview links to the remaining LTM storage, formation,
runtime, structured retrieval, visualization, and meta-memory contracts.

## Architecture and Implementation

- [Architecture](architecture.md) defines the high-level runtime and process
  boundaries.
- [Code Map](code-map.md) maps those boundaries to packages, modules, entry
  points, and important call chains.
- [Bridge Event Schema](bridge-event-schema.md) specifies the JSON-RPC runtime
  event envelope and payload ownership.
- [Observability](observability.md) describes trajectories, diagnostic
  artifacts, retention, and debug commands.
- [Agent Registry](agent-registry.md) lists registered Agents, triggers,
  lifecycle events, and supporting calls.
- [Agent Role Boundaries](multi-agent-role-boundary.md) records Main Agent,
  Memory Engine Agent, and tool-safety ownership.
- [Prompt Management](prompt-management.md) maps maintained prompts to their
  consumers and update rules.

## Testing and Evaluation

- [Testing](testing.md) describes the pytest and Vitest suites, isolation
  requirements, and focused commands.
- [Evaluation](eval.md) describes the external evaluation runner, benchmark
  adapters, judges, and reports. Some integrations require separately obtained
  upstream tools or datasets and are not bundled with Sema.

## Contributing

Read the repository [contribution guide](../CONTRIBUTING.md) before proposing a
change. It records the development setup, verification expectations,
documentation requirements, third-party attribution rules, and pull request
information needed by public contributors.

Maintained documentation distinguishes implemented behavior from future
direction. When code changes an architecture boundary, command, tool, test
strategy, evaluation rule, Memory Engine behavior, or security behavior, the
corresponding document should be updated with the same change.
