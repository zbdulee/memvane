# Runtime and provider proof

The local embedding runtime is provisioned under the private v1 state base. Provisioning checks supported Node majors and the matching npm executable; downloads require approval. The runtime manifest seals the generation's files, model and native dependencies. `memvane/provider/embed_runtime.py` validates a selected generation and its recorded file digests.

Onboarding reviews the provider, worker and runtime, then records their identities and SHA-256 values in the install attestation. Before using the provider, the selected route checks the attested provider and worker bytes and the runtime identity. A mismatch does not silently adopt a different provider. The user can release the selection and onboard again; the previous install directory remains.

A runtime generation path must be directly under the owner's private `generations/` directory. Missing, changed, linked or outside generations are refused. Generation selection and digest checks prove file identity; they do not themselves prove that native model execution succeeds.

Tests use isolated homes and fixture providers. A real-model check runs separately under the model-test resource guard.
