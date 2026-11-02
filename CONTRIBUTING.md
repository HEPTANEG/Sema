# Contributing to Sema

Sema 0.0.1 is an early Windows-first developer snapshot. Issues and pull
requests are welcome, but review, acceptance, and response times are not
guaranteed.

## Before Opening an Issue

- Search existing issues for the same behavior.
- Use the bug report form for reproducible defects.
- Use the feature request form to describe a problem before proposing a large
  implementation.
- Do not report security vulnerabilities publicly. Follow `SECURITY.md`.
- Remove API keys, credentials, private conversations, personal paths, and
  other sensitive data from examples and logs.

## Development Setup

Follow `docs/installation.md` for the current Windows source setup.

Run the main verification commands from the repository root:

```powershell
python -m pytest -q
npm test --prefix tui
npm run build --prefix tui
```

Some integration and model tests require separately configured services or
local models. State clearly which checks were run and which were not.

## Change Rules

- Keep changes focused on one behavior or concern.
- Add or update tests for behavior changes.
- Update the relevant maintained documentation when behavior changes.
- Update `docs/architecture.md` and `docs/code-map.md` when architecture
  boundaries change.
- Write maintained documentation in English and UTF-8.
- Describe verified current behavior or an explicitly labeled proposal.
- Do not add comments to Sema-maintained Python source. Preserve upstream
  notices and comments in vendored or migrated third-party code.
- Do not commit runtime state, model files, credentials, logs, caches, build
  output, or private workspace material.
- Identify copied or adapted third-party material and preserve its license and
  attribution requirements.

Memory Engine changes should include focused tests and update the corresponding
documents under `docs/memory-engine/`.

## Pull Requests

Pull requests should explain the problem, the chosen approach, observable
behavior changes, verification performed, documentation impact, and any new
third-party dependency or license obligation.

Submitting a contribution represents that you have the right to submit it and
agree that it is provided under the repository's MIT License. Contributors
retain copyright in their contributions. No separate contributor license
agreement is required for Sema 0.0.1.
