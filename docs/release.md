# Release Checklist

## Before creating a GitHub Release

1. Confirm the version matches every host manifest and marketplace entry.
2. Source verification (L0, prerequisite only): run `npm test` and
   `git diff --check` on the release commit. These source checks never
   replace the installed-candidate gate in step 4.
3. Verify JSON manifests parse successfully.
4. **Mandatory for every version release:** run the
   [release agent QA gate](release-agent-qa.md) on the final installed
   candidate in fresh, real Claude Code and Codex host sessions. Record the
   run with the [run record template](templates/release-agent-qa-run.md).
   Every mandatory scenario must be `PASS` in every mandatory matrix cell.
   `FAIL`, `BLOCKED` and `NOT RUN` results are never counted as `PASS` and block
   a formal release in a mandatory matrix cell. Optional, unverified host/OS
   combinations must be named in the release notes.
5. If code or the deployable artifact changes after QA, rerun the affected
   scenarios plus the core smoke. Follow the rules in section 11 of the gate
   spec.
6. If wiki compile is advertised, confirm its one-time notice and disable path
   (RQA-WIKI-03 covers the OFF path).
   For this release's built-in learning path, `RQA-RES-02` must also show the
   durable learning workload queue, resource admission, owned-process stop,
   and exact cursor resume in an approved synthetic installed-candidate run.
   It remains mandatory even if learning is not advertised. An L0 mock or
   model smoke cannot satisfy that row. Search and embedding workload queue
   expansion is an optional decision after v2 opens; search and embedding
   product functionality still needs its own Laya gate.
   For a Laya Index replacement release, require every `LI-*` row in the
   [agent QA gate](release-agent-qa.md) on the exact installed plugin. The
   host-loaded installer path and raw+wiki route have source and synthetic
   coverage; their installed-candidate behaviour, production provider/model
   closure, migration cutover and fresh-host retrieval parity remain
   unverified. The corresponding rows stay `NOT RUN` until the installed
   candidate supplies evidence.
7. Publish a GitHub Release with compatibility notes, user-visible changes,
   upgrade notes, and known limitations. Name every OS or host that was not
   verified in this release.
8. After publication, install from the public marketplace in a clean QA
   account. Confirm that the deployed artifact identity equals the QA'd
   identity (RQA-ID-00 + core smoke).

## Evidence to retain

Retain the completed run record. It covers the commit SHA,
`artifactManifestSha256`, installed and deployed identity, host, OS, Node,
Python, QMD and model versions, search-result proofs, blockers, and any checks
that were intentionally not run or blocked. Keep transcripts and logs in the
private evidence store. Release notes carry summaries and hashes only.
