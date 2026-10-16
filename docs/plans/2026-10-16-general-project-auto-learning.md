# General project automatic learning implementation plan

**Goal:** Enable the approved service-engineering and rccar projects to capture eligible hook observations, obtain bounded Codex CLI gold, and train locally without weakening model publication gates.

**Architecture:** Keep the historical synthetic experiment unchanged. Add explicit project/runtime and separate external-teacher consent, reuse private capture/shared databases, durable batch/evaluation queues, inference-first resource coordination, staged training and transactional candidate application. Preserve existing sessions, inbox automation, QMD and original model/checkpoint files.

**Authorization:** The user approved both projects' query, candidate body, identifier and revision/hash transmission through the existing logged-in Codex CLI to OpenAI. Credentials/secrets are excluded before transmission. No additional API billing, credentials, OS permissions, repository publishing or main modification is authorized. Stop new model work when official weekly remaining usage is at most 30%.

## Decisions and alternatives

Use a separate general-project approval branch rather than changing historical synthetic approvals or replacing the existing queues. Rewriting the whole trainer would duplicate quality and publication safety. Merely enabling two flags would leave missing policy, gold and execution approval, and therefore cannot satisfy this request.

## Task 1 — Scope and cohort (independent owner)

- Add `core/context_learning/project_scope.py` and a general cohort helper as needed.
- Bind canonical project, private namespace, indexed scope, approved installed code, interpreter/model/base artifact, policy and expiration. Keep teacher content consent separate.
- Extend `runtime_authorization.py`, `automatic_cohort.py`, `automatic_staging.py` and `learning_profile.py` only through explicit general-project branches.
- Test first: wrong project/namespace/code, changed source, revoked/expired approval, train/holdout exposure leakage and duplicate evidence rejection.
- Preserve five-case candidate staging and the existing 100 train / 50 validation / 10 empty / five families per side quality requirements. Do not confuse training readiness with publication readiness.

## Task 2 — Teacher and egress (independent owner)

- Add `general_teacher.py`, `general_gold_ledger.py`, `egress_guard.py` and focused tests.
- Require separate exact consent; inspect all transmitted fields for credentials, fail closed by excluding the case and recording only a safe reason/hash.
- Use existing constrained Codex CLI descriptor and authentication, private non-Git scratch, bounded streams/time/RSS, quota check and two independent matching selections.
- Persist dispatch stages before external calls; interrupted/uncertain paid attempts never retry automatically.
- Test zero dispatch on secrets, stale scope/quota, duplicate concurrent claims; reject tool events and malformed output.

## Task 3 — Hook/worker and reversible setup (root owner)

- Extend `shared.py` for general enrolled projects: eligible capture wakes gold then local-only training; SessionStart and index ACK resume durable work.
- Respect `capture`/`autoCycle` independently; retain inference priority, bounded retry and no login service.
- Add explicit setup proposal/activation with whole settings and namespace backups, exact settings CAS, approved code/policy/consent, and recovery journal.
- Test actual hook entry through a synthetic isolated project, durable queue/readback and disabled/revoked behavior; external boundaries use fixtures only in tests.

## Task 4 — Review and operating rollout

- Run focused regressions, then one full deterministic suite appropriate to this cross-cutting change. Independent review before operating writes.
- Report exact two settings files, namespace files, official installation changes, service identity refresh and backup path before application.
- Activate through supported installation/setup paths, preserve original models/checkpoints, verify both flags and actual host hook observation.
- Perform a small synthetic live Codex gold check through the installed package. Train only if admitted fresh data and resource gates permit. Leave the current model active when quality data are insufficient.
- Record performed, unperformed and blocked stages with exact evidence. No main push, release, forced operating rollback or quality bypass.
