# Developer release coordination

`scripts/release_coordination.py` coordinates a maintainer's installation and
upgrade sequence. Product hooks do not call it. It changes no route, journal,
provider policy or reader selection itself.

## Operator contract

The caller supplies trusted, already discovered project/state mappings and
holds the user-wide development `Lease`. It checks the development hold before
installation and before starting an upgrade. Product operations use their
ordinary environment and resource admission; a fixture/test runtime root is
not a production deployment environment.

`coordinate_release(states, *, install_both, prepare_worker)` accepts a nonempty
mapping of project paths to existing canonical private state directories.
It accepts at most 32 projects and 128 distinct state roots. Every root is
owned by the current UID with mode 0700. The existing
`bundle-auto-upgrade.lock` must be a regular, singly linked, current-UID 0600
file. A missing lock file is created with that mode; it is never unlinked.

The helper acquires every lock nonblockingly before calling `install_both()`
once. A busy or unsafe root refuses before installation. The callback must
finish both supported host installations, verify their versions/source commit,
and use finite installer timeouts without reacquiring the development lease.
It does not use a callback's return value as an installation attestation.

For each project, `prepare_worker(project)` returns an object with
`ready()`, `go()`, `finish()` and `close()`. While `ready()` runs, every remaining
project lock is held. The helper rechecks directory and named-lock identities,
closes only the current project's lock descriptors, and calls `go()`. Other
projects remain locked until their own turn. It closes each worker and its own
remaining descriptors on ordinary failure. It does not signal other workers,
change a refusal into success, or delete a lock file.

This is cooperative coordination, not an atomic filesystem snapshot or a
same-UID security boundary. Another actor may win after unlock. The actual
selected owner and final service/worker paths remain release gates.

## Fresh installed worker

`PreparedWorker(plugin_root, project, *, ready_seconds=30, run_seconds=1800)`
starts a retained interpreter with `-I -B`, non-inherited descriptors and an
anonymous private stderr file. The caller verifies the installed root before
constructing it; an arbitrary directory is not trusted by this API.

The child imports the named bundle and reports `READY`. It refuses the bundle's
fixture/sandbox mode. It calls the ordinary `laya_bundle_upgrade.run` only after
receiving `GO`; EOF before GO cancels without an upgrade. No owner check or
resource guard is replaced. Its result includes the ordinary upgrade outcomes
and the actual bound `pluginRoot` for each state root. A deferred/refused outcome
or another selected root fails `finish()`.

Readiness is bounded to at most 60 seconds; the parent run deadline is at most
2100 seconds. The child uses the ordinary 1800-second upgrade alarm. An alarm
is not a guarantee of immediate interruption inside a native SQLite call.
Parent timeout/exception cleanup targets only the retained helper process and
waits for it. A cleanup that cannot confirm termination raises
`worker_cleanup_unsettled`; the operator must retain observation of that process,
keep the release unsettled and start no further release work. Product residents
are not cleanup targets.

The helper does not allow equal-version drain, forge a newer identity, change
an interpreter to force replacement, or substitute `read_only_handover` for a
failed owner-path gate. Companion-host sealing and normal read/write proofs,
recall checks, service paths and cache integrity are verified separately.
