# Explicit inference identity and warm validation

This branch adds an inference-only preparation protocol. It does not install
plugins, change operating policies or grants, replace a model, run training or
smoke, register an OS service, or merge main.

## Evidence and authorization boundary

`core/prepare_laya_inference.py` is an explicit maintainer entrypoint, run with
the policy interpreter and `-I -B`, against an already approved private state.
It requires the existing currently valid full compatibility receipt, exact
normalized official adapter, current model, private policy/pointer/artifact and
resource admission. It loads the approved head and executes one public
synthetic forward with exact response/candidate mapping checks. The original
24h training receipt is never refreshed or rewritten.

Only after final content stamps, code, imported-module origins, device,
policy/pointer/approval, artifact, original deadline and receipt freshness are
rechecked does preparation publish three private files:

- `laya-inference-manifest.json`: full content hashes plus change-detection
  stamps for the closed venv, standalone interpreter/stdlib, model weights,
  tokenizer and config. Bytecode, package metadata and new files are included.
- `laya-inference-proof.json`: `qmd-laya-inference-proof-v1`, namespace,
  original approval/policy/checkpoint digests, interpreter/model/view,
  manifest digest, all core Python code digest, OS/kernel/host/architecture,
  actual MPS/CPU backend, and read-only load/forward/mapping checks.
- `laya-inference-binding.json`: receipt digest. Any one evidence file requires
  the complete protocol; removing only the binding cannot downgrade to legacy.

Creation time records provenance. An unchanged verified inference generation
has no daily training requirement. Training compatibility and operation grants
retain their independent scope, revocation and expiration. An unprepared
legacy policy retains its previous 24h behavior. Explicit preparation is
required to adopt the new protocol; prediction never prepares it implicitly.

A closed isolated venv and actual import paths are required. Editable/external
imports are rejected rather than silently brought into the approved scope.
PyTorch's two virtual modules `torch.ops`/`torch.classes` are bound to their
exact implementation types and instances, whose real files remain covered;
no temporary or external directory is allowed for these placeholders.
Internal runtime symlinks must resolve inside a manifest root.

## Cache and process boundary

The client checks private current receipts, scope, original approval/policy/
pointer, artifact and current code without constructing VerifiedLayaTrainer
or importing ML metadata. The worker independently repeats those checks.
On first use it hashes the full manifest content. Warm validation scans every
entry once through DirEntry stat, detects additions/deletions/mode/owner/link/
mtime/ctime/inode/size changes, and rechecks device and module origins. Stamps
are change detectors after content verification, not content identity or a
claim of protection against an owner/privileged attacker forging filesystem
history. No repeated torch/Laya metadata subprocess runs on this path.

Worker code is pinned at startup. After first successful content verification,
its generation is also pinned: a newly prepared changed manifest requires a
new worker. Hashing new library files does not make already imported Python
objects current. The service family identity incorporates content manifest,
code and device in addition to the historical runtime identity. The service
never kills/replaces an old worker or switches CPU/model implicitly.

Repreparing the same namespace atomically replaces its manifest file, so its
previously cached worker rejects even a byte-identical replacement and requires
a fresh worker. This conservative limitation includes a newly prepared head.
A distinct namespace with the exact same already verified manifest can share
the resident model. No automatic head-apply/restart flow is added here.

The existing serial engine still checks resources and final request identity
before publishing a loaded object and after every warm forward. Failures use
`inference_attestation_required` and retain baseline QMD selection. Existing
sessions and inbox automation are preserved.

## Verification and timing

Focused synthetic tests cover unchanged warm reuse without metadata or
content rehash, clock passage beyond the training TTL, library/model/tokenizer/
config/head mutation, unsafe/added files, device/runtime/view/code/namespace
changes, original approval and binding revocation, partial/malformed evidence,
external module origins and forged virtual modules, prepared-generation
restart, and original prepare deadline/expiry before load/publication. Both
host package layouts execute the real choose and worker request validators;
only synthetic transports/metadata/device fixtures replace actual ML there.
Actual prepare/forward behavior is checked separately in the isolated lab.

Timing contains only bounded numbers/booleans: client identity, initial
validation, identity cache hit, revalidation, queue, load, tokenization,
batch preparation, forward including readback, and selector total. It contains
no source text, raw stderr, credential, or prompt telemetry.

The first real preparation failed closed after load because PyTorch's virtual
module placeholder was interpreted as an external path. No forward or receipt
publication occurred; that failed attempt remains in the evidence directory.
After the exact proxy identity correction, the bounded real lab measured
cold 8.484s; sequential warm 0.877/0.867s (previous 3.562/3.704s); concurrent
0.770/1.584s. One worker loaded once and forwarded five times on MPS, with zero
metadata subprocesses in worker or clients. Worker child CPU was 0.049s versus
52.008s in the prior run (these are cumulative terminated-child CPU seconds,
not peak CPU percentages). Peak process RSS was 2297.28 MiB, not total MPS
allocator memory. Explicit preparation separately took about 10s per namespace
and performed one synthetic forward each. It is outside request latency.
Two revoked lab-receipt requests failed before IPC in about 0.004s. Owned lab
worker exited 0 and removed its socket. Watched operating files/modes/inodes
and model identity were unchanged. No production hook/QMD end-to-end claim is
made from this selector/IPC/model test.

The final code additionally pins startup code and content generation so a
new receipt cannot refresh already imported objects. A final bounded lab
check of that exact code succeeded: cold Claude 8.398s, warm Codex 0.918s,
one model load/two forwards, zero metadata subprocesses, both revoked lab
receipt requests blocked, owned worker exit 0, and zero watched operating
differences. Independent source review approved the final boundaries.
A focused peer-rejection fixture exposed an early-close BrokenPipe race; it
now reads the pre-wire peer refusal directly instead of sending after refusal.
No product guard or timeout was weakened.
