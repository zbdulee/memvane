# Runtime and bundle review contracts

## Runtime selection

Automatic new-machine provisioning checks the supported Node majors (22, 24,
26) and that candidate installation's npm CLI. It checks both the conventional
`lib/node_modules/npm/bin/npm-cli.js` and Homebrew's
`libexec/lib/node_modules/npm/bin/npm-cli.js` beneath the resolved Node prefix.
A supported candidate without npm yields to the next candidate. Explicit Node
selection stays explicit; Node-only discovery does not require npm. Downloads
still require the existing approval.

Runtime producers return the generation they seal, copy or reactivate. Their
return does not reselect global `active.json` after releasing the runtime lock.
The bundle worker passes that generation through its first provider review and
active, bound, inactive and fenced-recovery reviews.

`select_generation()` accepts only a canonical path directly under the current
owner's private runtime `generations/` directory. It rechecks the complete sealed
file inventory and ownership without reading or writing active/previous
pointers. Missing, changed, linked or outside generations refuse. Provider review
without an explicit generation keeps the active-runtime selection behavior.
Generation selection is evidence of sealed bytes, not a native execution test.

## Partial marketplace clones

Product bundle and provider proofs use the committed tree's blob OIDs and bounded
installed bytes. A materialized source file must match its committed mode and
blob identity. Only an actual missing skip-worktree file can use installed bytes
as its blob proof. Parent links, dirty source bytes, extra cache files and cache
mode changes still refuse. Proof does not expand the checkout or read blob
objects through `cat-file`.

Product Git commands disable replacements, grafts, lazy fetching and transport
protocols. The no-lazy-fetch admission probes the actual Git executable before an
object command. Supported releases include Git 2.46+ and the explicitly listed
maintenance backports. Older or unknown releases may inspect a full repository;
a partial repository refuses with `source_git_no_lazy_fetch_unsupported`.
Unreadable admission evidence refuses with
`source_git_no_lazy_fetch_unavailable`. These are retryable deferrals without the
source-commit backoff. The capture layer shares the pure version predicate;
product proof does not call checkout capture.

The shared Git helper lives in the existing required `package.py` artifact.
The engine subbundle's artifact list, manifest and ownership schemas keep their
exact existing contracts. Inspection accepts a historical sealed bundle without
rewriting its bytes or identity; a new bundle imports and calls the helper from
its own packaged modules.

On unsupported Git, every related config row is validated. Any true or valueless
promisor, filter or partial-clone extension registers partial-clone evidence; a
later false does not remove it. A malformed row still refuses when later rows
contain false. A false-only full repository remains eligible.

## Synthetic verification

The provisioning fixture covers Homebrew, npm-less candidates and explicit
selection. The partial-clone fixture uses real `file://` blob-filtered clones,
counts transport/fetch attempts and missing objects, and checks online/offline
proof plus dirty-source and cache rejection. The generation fixture switches G0
to G1 through normal APIs at lock-release and review boundaries. Ordinary JS
dependency bytes differ while node, model, module entry and native bytes match.
The active/bound/inactive/recovery review check stops immediately after the real
proposal; journal completion and crash recovery have separate integration tests.
The historical compatibility fixture uses the byte-preserved packaging module
from `463e029`, so shallow CI needs no history fetch. The current subbundle runs
in a separate `python -B -I` process without a host source import path. The
unsupported-Git control uses the installed Git with its no-lazy-fetch environment
fence removed; it is not an execution of a historical Git binary. It retains the
transport fence and restores config bytes/mode if the control's failed fetch
writes a filter before checking product admission.

These fixtures use temporary homes and synthetic files, never live indexes or
model execution.
