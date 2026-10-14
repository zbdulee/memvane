# Component readiness implementation plan

Goal: Separate QMD file-index completion, required local Laya readiness, and overall setup readiness without enabling learning.

Architecture: Retain the completed QMD journal and existing QMD guard. Return additive component booleans/details; an incomplete installation returns qmd_ready with exit 0 for its successful QMD operation. Explicit check-update verifies the existing managed runtime attestation, pinned versions, current packaged adapter and local model bundle; it never installs or runs a new smoke. SessionStart performs a cheap local preflight and one notice for missing preparation, without imports/inference or hashing weights. Learning preferences do not exempt preparation.

Alternatives considered: extending setup_guard would block usable QMD; changing journal phase would break resume. Keep both unchanged and clarify only the public setup result and notice.

Tech stack: Python standard library, existing runtime_setup chooser, Node test runner.

1. Add failing synthetic tests for absent/version-drift/model-drift/adapter-drift Laya, cheap notices, opt-out independence and explicit component statuses.
2. Add shared read-only readiness helper, use it in check-update/install, catch invalid managed inventory, and add cheap SessionStart preparation guidance. Keep existing schemas, journal phases and CLI exit codes.
3. Update narrowly affected fixture expectations and setup skill wording; run focused runtime/setup/guard/install regressions, no downloads or live model calls.
4. Obtain fresh independent diff review, address material findings, export patch/evidence. No commit/push/operating apply is part of this task.

Approval boundary: existing user request authorizes this isolated patch/test/review. Laya software/weights download, runtime activation, synthetic training, official host loading/trust and operating deployment remain separate unapproved actions.
