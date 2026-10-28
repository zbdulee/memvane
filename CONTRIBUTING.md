# Contributing

Thanks for helping improve MemVane.

## Before opening a change

1. Search existing issues for the same problem.
2. Keep the change host-neutral in `core/` where possible, then verify Claude
   Code, Codex, and Hermes adapter implications.
3. Do not add project-specific paths, credentials, logs, or user content.
4. Add or update a deterministic regression test for behavior changes.

## Local checks

Run `npm test` and `git diff --check` before requesting review. Live qmd or host
CLI checks are optional and must be labeled as such; do not treat them as a
replacement for deterministic tests.

## Pull requests

Use a focused branch and explain the user-visible behavior, supported hosts,
and verification evidence. Changes to manifests, hooks, or public docs should
state which installation and runtime contract they affect.
