# Approved-profile defaults and candidate application design

Status: design only. This document does not change configuration defaults,
permissions, installed packages or active checkpoints. Durable queue/resource
coordination is implemented separately on `feat/user-singleton-laya-service`;
automatic application remains unimplemented.

## Enrollment and explicit existing settings

New projects may receive automatic local learning as the default **inside a
separately approved learning profile**. Profile approval must show the project,
namespace, eligible sources, local runtime/model identity, resource/disk budgets,
training scope and whether candidate application is included. A normal prompt
or SessionStart is not enrollment.

Existing explicit `capture=false`, `autoCycle=false`, shared-learning disable
and opt-out decisions remain authoritative. Missing fields in an existing
project do not constitute approval to import its corpus. A future migration
must show a project-specific before/after proposal, preserve explicit false
values, and obtain approval for any requested change to them. Do not implement
a global defaults merge that silently reverses those decisions.

Local learning reuses eligible, verified labels and semantic consumption. No
fresh admitted data means waiting, irrespective of timer ticks. The approved
profile must bound fresh batch/replay, optimizer updates, attempts, CPU time,
memory, disk and retention. Its quality thresholds must be satisfiable by the
actual bounded candidate batch; do not lower quality checks to fit a default
batch size. Revoked/expired grants and unknown resources block or defer under
the existing boundaries.

Neither an approved learning profile nor a local-training grant authorizes raw
prompt capture from new sources, historical enrollment, external teacher
transmission, API billing, software installation or a model/device change.
Retain each existing operation's scope and approval. Missing labels or teacher
permission produces a waiting state, not a provider call or enrollment prompt
on every query.

## Separate candidate application authority

The current train/evaluate grant stops at staged output. A future application
grant must be distinct and bind the project/namespace, runtime/base/view family,
candidate lineage, permitted validation policy, explicit regression tolerance,
publication scope and expiry/revocation. Never infer apply permission from
training success, existing live selection or an inference identity receipt.

Immediately before publication, recheck the grant and fresh disjoint validation
including empty-choice cases, source/cohort validity, incumbent/candidate
identity, required family coverage and regression criteria. Insufficient or
changed evidence remains staged. The source-of-truth approval protocol and
schema require a separate reviewed implementation; this design does not mint a
new grant or relax a reviewed adapter hash.

## Publication transaction and recovery

1. Write an owner-private durable intent binding expected-old pointer digest,
   candidate artifact digest, quality evidence and apply grant. Keep the
   incumbent available while preparing a separate compatible object, only if
   measured overlap memory headroom exists. Defer otherwise.
2. Validate a synthetic first request and prepared inference identity without
   training, teacher calls or hidden proof renewal. Pin the exact service
   generation and request routing identity.
3. At a request boundary, compare-and-swap the project pointer against the
   expected-old digest. Persist `prepared`, `pointer_committed`, `activated`
   journal transitions and fsync their parent directories. Publish the prepared
   service generation only under the matching committed pointer.
4. Recovery reconciles exact journal, pointer and generation identities. Before
   pointer commit, a failed prepare preserves the incumbent. After commit, an
   incomplete activation is explicit and either completes the same authorized
   generation or waits for reviewed recovery. Never label the old head as the
   new version, silently downgrade to CPU or automatically migrate/rollback the
   operating history database.

Independent fault injection must cover every persistence/CAS boundary, caller
disconnect, resource loss, concurrent namespaces, stale pointers, changed
quality/source/grant, and process crash. Actual operating activation requires
a separately approved rollout after these checks. No existing session or inbox
automation must be stopped merely to use this controller.

## Diagnostics and rollout gates

Expose concise states such as waiting for fresh data, deferred for inference or
memory, blocked by authorization, staged waiting for quality, prepared and
applied. Current queue status supplies counts; the future product presentation
must not imply preparation/application already exists. Keep source bodies,
credentials and provider content out of common diagnostics.

Before any operating rollout, inspect the installed package and running worker
generation, preserve existing data/settings, and explicitly prepare the newly
reviewed runtime/adapter evidence through the normal approved flow. Feature
branch synthetic tests and earlier inference timing do not validate actual
MPS resume, automatic application, hooks or project migration for this design.
