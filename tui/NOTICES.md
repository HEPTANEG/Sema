# Third-Party Notices

Sema's original code is licensed under the repository-level MIT License. The
TUI also contains code adapted from the projects below. Sema's modifications
do not replace the licenses and notices that apply to upstream portions.

## Raven UI Baseline

- Source: https://github.com/EverMind-AI/Raven
- Imported revision: `721133d3cf25db06bdd990b88078467c15faac73`
- License: Apache-2.0; see `LICENSES/Apache-2.0-Raven.txt`
- Scope: `src/raven/**` and `packages/hermes-ink/**` were imported from
  Raven's `ui-tui/` tree and then modified for Sema.
- Sema modifications include its backend bridge, protocol adaptation,
  commands, configuration, branding, Memory Engine surfaces, and later TUI
  maintenance. The current files therefore differ from the imported revision.

Raven's applicable upstream attributions are preserved below. Claims from the
Raven notice that do not match Sema's imported subset are not repeated as Sema
component claims.

## hermes-agent

- Source: https://github.com/NousResearch/hermes-agent
- Raven vendored revision: `dd0923bb89ed2dd56f82cb63656a1323f6f42e6f`
- Copyright (c) 2025 Nous Research
- License: MIT; see `LICENSES/MIT-hermes-agent.txt`
- Scope: Raven's terminal UI baseline, subsequently adapted by Raven and Sema.

## Ink

- Source: https://github.com/vadimdemedes/ink
- Copyright (c) Vadym Demedes, Sindre Sorhus, and Ink contributors
- License: MIT; see `LICENSES/MIT-ink.txt`
- Scope: the `@hermes/ink` fork under `packages/hermes-ink/**`, subsequently
  adapted by Hermes Agent, Raven, and Sema.

## nanobot Provenance

- Source: https://github.com/HKUDS/nanobot
- Copyright (c) 2025 nanobot contributors
- License: MIT; see `LICENSES/MIT-nanobot.txt`

Raven's repository-level notice identifies its Python agent runtime as derived
from nanobot. Sema did not import that Raven Python runtime: no Sema directory
is attributed to nanobot. The license text is retained only to preserve the
complete provenance recorded by the pinned Raven notice.

## Package-Manager Dependencies

The source repository does not redistribute `node_modules`. JavaScript
dependencies are installed from the versions resolved by `package-lock.json`
and remain subject to their own package licenses. Any future bundled executable
or dependency archive requires a separate binary-distribution license review.
