# Security Policy

## Supported versions

Security fixes are made on the latest `main` release line.

## Reporting a vulnerability

Do not include exploit details, credentials, private project content, or tokens
in a public issue. Use GitHub's private vulnerability-reporting flow for this
repository when it is available:

<https://github.com/zbdulee/memvane/security/advisories/new>

If private reporting is unavailable, contact the repository owner through
GitHub without posting technical details publicly. Include affected version,
host, impact, a minimal safe reproduction, and any proposed mitigation.

## Scope

Relevant reports include unsafe path handling, unintended indexing or content
disclosure, hook privilege escalation, command injection, and unsafe writes to
project or user-local state.
