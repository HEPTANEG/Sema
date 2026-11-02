# Third-Party Dependencies and Notices

Sema is licensed under the MIT License, but that license does not replace the
licenses of third-party packages, adapted source, models, or datasets.

This document summarizes the direct dependencies declared by Sema 0.0.1. The
source snapshot does not include Python virtual environments, `node_modules`,
model weights, or downloaded datasets. Package managers retrieve those
components separately, and their own license terms continue to apply.

## Python Direct Dependencies

Python versions are resolved from `requirements.txt` at installation time and
are not fully locked in this snapshot.

| Package | Purpose | Declared version | License | Upstream |
| --- | --- | --- | --- | --- |
| Rich | Optional Markdown rendering in `sema-eval` | Unpinned | MIT | [Textualize/rich](https://github.com/Textualize/rich) |
| pytest | Test runner | Unpinned | MIT | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) |
| tiktoken | Context token accounting | Unpinned | MIT | [openai/tiktoken](https://github.com/openai/tiktoken) |
| pywin32 | Windows bridge and process primitives | Unpinned | PSF | [mhammond/pywin32](https://github.com/mhammond/pywin32) |
| python-telegram-bot | Optional Telegram remote control | `>=22,<23` | LGPL-3.0-only | [python-telegram-bot/python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) |
| sentence-transformers | Local embedding and reranking runtime | `>=6,<7` | Apache-2.0 | [UKPLab/sentence-transformers](https://github.com/UKPLab/sentence-transformers) |
| USearch | Long-term-memory vector index | `>=2.26,<3` | Apache-2.0 | [unum-cloud/usearch](https://github.com/unum-cloud/usearch) |

## TUI Direct Dependencies

The versions below are the versions recorded in `tui/package-lock.json` for
Sema 0.0.1. Development-only packages are included because they participate in
building or testing the public source snapshot.

| Package | Version | Scope | License |
| --- | --- | --- | --- |
| `@hermes/ink` | Repository-local | Runtime | MIT AND Apache-2.0; see `tui/NOTICES.md` |
| `@nanostores/react` | 1.1.0 | Runtime | MIT |
| `@types/node-notifier` | 8.0.5 | Runtime | MIT |
| `ink` | 6.8.0 | Runtime | MIT |
| `ink-text-input` | 6.0.0 | Runtime | MIT |
| `nanostores` | 1.5.2 | Runtime | MIT |
| `node-notifier` | 10.0.1 | Runtime | MIT |
| `react` | 19.2.8 | Runtime | MIT |
| `signal-exit` | 4.1.0 | Runtime | ISC |
| `unicode-animations` | 1.0.3 | Runtime | MIT |
| `@types/node` | 20.19.41 | Development | MIT |
| `@types/react` | 19.2.18 | Development | MIT |
| `tsx` | 4.22.3 | Development | MIT |
| `typescript` | 5.9.3 | Development | Apache-2.0 |
| `vitest` | 1.6.1 | Development | MIT |

Package names and versions correspond to their npm registry metadata.
Transitive npm dependencies and their resolved versions are also recorded in
`tui/package-lock.json` and retain their respective licenses.

## Adapted Source Retained in the Repository

The TUI contains adapted or migrated work associated with Raven,
`hermes-agent`, Nanobot, and Ink. Provenance, modifications, retained notices,
and the applicable full license texts are provided in
[`tui/NOTICES.md`](tui/NOTICES.md) and [`tui/LICENSES/`](tui/LICENSES/).

## Models and Datasets

Sema does not distribute model weights or benchmark datasets in the public
source snapshot. Users obtain supported local models from their original
publishers. See [`docs/installation.md`](docs/installation.md) for the model
names, sources, and licenses. Sema's MIT License does not cover those external
artifacts.

Any future binary bundle, standalone installer, vendored dependency, model
bundle, or dataset bundle requires a new distribution-specific license review.
