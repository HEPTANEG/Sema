# Security Policy

Sema 0.0.1 is an early developer snapshot. Security reports are handled on a
best-effort basis, and no fixed response or remediation timeline is promised.

## Supported Versions

Only the latest public snapshot is considered for security reports. Private
development history, archived material, and modified third-party distributions
are outside this policy.

## Reporting a Vulnerability

Do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.

Use GitHub's private vulnerability reporting feature from the repository's
Security page. Include:

- the affected component and version;
- reproduction steps or a minimal proof of concept;
- the expected security boundary and observed behavior;
- the potential impact; and
- sanitized logs or screenshots when useful.

Do not include API keys, credentials, private conversation content, personal
data, or unrelated system information. Redact local usernames and paths unless
they are essential to reproducing the issue.

Relevant reports include command-confirmation bypasses, workspace or path
boundary violations, credential exposure, unauthorized remote access, and
unsafe handling of persisted conversation or memory data.

Please allow a reasonable opportunity to investigate before public disclosure.
