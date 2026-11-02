# Selected bundle recovery

`laya_active_upgrade` keeps the selection ledger in `upgrading` while replacing
the selected bundle. Recovery preserves the original backup, build record,
store counts, generation, vector profile, project scope and collection paths.
The journal's project device/inode and the current settings-derived collections
must match before writes. Project identity and the settings-derived scope are checked again before each
intent or metadata write, journal CAS, activation and fence release.

An interrupted pending update can move to a newly reviewed host bundle even
when both pinned caches are absent or re-extracted. `cas_retarget_pending`
requires the exact epoch and pending predecessor, selected/previous evidence
agreement, no selected write, and actual route bytes pinned by the journal.
It proves the fresh full-plugin candidate and changes only the pending identity,
pending route digest and epoch. It does not activate the candidate or approve
provider equivalence.

The private v2 intent contains one bounded recovery descriptor. Its `normalize`
mode records the exact five metadata files before normalization and the same
files with only the project route adjusted to the current pending bytes.
The v2 intent is fsynced before that write; a v1 worker refuses its schema.
The ledger remains fenced and normalization never activates or runs a provider.
An already journal-pinned route is a no-op; its pending target can differ from
a switch descriptor that was persisted before its CAS. A route replacement
still requires the exact current pending bytes and identity.
Historical attestation bytes and file-identity digests are comparison evidence;
missing files are not represented as a current file proof.

The `switch` descriptor records the exact journal predecessor and metadata
before/after values. Recovery accepts only the predecessor, its exact staged
successor, or its exact activated successor, and only a reachable prefix of
the metadata write order: attestation, reviewed route, project route, ledger,
marker. A changed target uses route-only normalization before a new bounded
descriptor replaces the old one. Descriptors do not contain nested history.

The fresh target passes the normal marketplace review, provider equivalence
against the original sealed attestation, adoption and full-plugin proof.
Provider file identities are measured again before activation. Settings,
project identity, metadata, journal evidence, counts or write-history changes
refuse recovery. An already activated target can advance while fenced only
with complete metadata, no selected write and unchanged counts. The original
backup remains available as evidence; a v2 intent is forward-only and does not
authorize restoring an identity outside the journal's current predecessor.

The bundle worker checks the fence before returning `current` or sealing a
companion copy. A missing prior target therefore reaches normal fresh review
instead of skipping recovery. Readers and writers keep their existing fence
contracts until the fresh journal selection and metadata agree.
