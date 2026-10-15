# Portable live-selection state root implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let shared project settings omit `contextLearning.stateRoot` without losing an already prepared private selector state.

**Architecture:** The approved design resolves an omitted root from the current user's existing plugin runtime location, with `live-selection` beside `runtimes`. Preserve the canonical project-root SHA-256 namespace and all explicit-root safety checks. Missing local preparation remains unavailable; this change creates no models, checkpoints, parent directories, migrations, or network requests.

**Tech stack:** Python pathlib and existing config/state helpers; Node test runner wrapping deterministic Python filesystem fixtures.

## Scope and decisions

- Keep explicit custom absolute roots compatible. Preserve invalid explicit values as unavailable instead of interpreting them as omission.
- Use the same macOS location already prepared by local setup, derived from the current user's home. Use the existing runtime location rule on other platforms.
- Do not expand literal `~`, add environment overrides, merge machine-local project settings, or change capture/autoCycle permissions.
- No operating plugin update, project settings removal, model call, or data movement in this development task.

## Execution

1. Reuse the existing selector/auto-hook tests as the clean baseline.
2. Add a deterministic regression for omitted root, two users, spaces, existing checkpoint byte/inode preservation, project inheritance/isolation, explicit roots and unsafe/missing roots. Run it against the baseline and confirm the omission failure.
3. Preserve omission in `core/config.py`; resolve it in `core/context_learning/seam.py` through `runtime_setup.default_managed_root()`. Keep explicit invalid values unavailable.
4. Document the default location, local prerequisites and tracked-setting portability in `docs/continual-learning.md`.
5. Run the new and adjacent deterministic tests; verify the existing MacBook states with read-only resolution only. Obtain independent review.
6. Commit and push this branch. Confirm Node 22/24/26 CI at the final SHA. Leave main, installed plugin caches and operating project settings unchanged.
