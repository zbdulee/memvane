# Data Handling and Privacy

## Default local processing

MemVane is inactive until a project is onboarded onto a selected Laya
Index route through the reviewed setup flow. A project without a selected route
gets no indexing, recall, compile or learning work.

For an onboarded project, the wiki cards under `.memvane/wiki` are indexed
into a local memvane-db index under the install's private state directory. Raw
project files are read to generate wiki cards and to track path and hash
freshness; their bodies are not indexed. Embeddings are computed on the machine
by the local embedding runtime (`~/.local/state/memvane/v2/embed-runtime`, Node with
node-llama-cpp and a local GGUF model), and recall queries are answered from
that local index.

Preparing the embedding runtime on a new machine downloads packages from the
npm registry and the model file from its pinned Hugging Face URL, and only after
explicit approval. The plugin does not provide a separate telemetry service.

## Optional wiki compile

Optional wiki compile runs only for an onboarded project; its minimal settings
turn compile on for that project. It passes selected Markdown source content and
the bundled card-writing rules, recent `wiki` events from the install event
log (card paths and titles) and similar wiki cards to the configured host CLI to produce or verify
wiki candidates. Depending on the configured host CLI and its account settings,
that CLI may send the content to its provider. Review the host provider's
privacy policy before onboarding a project that holds sensitive material.

The built-in adapters run in an isolated temporary working directory with their
tools disabled. This limits filesystem access by the nested host CLI, but it
does not change the host provider's data policy.

## Learning labels

An onboarded project's default learning profile labels captured edit cases with
the host CLI of the session that captured them: Claude Code cases go to the
`claude` CLI and Codex cases to the `codex` CLI, signed in with the user's own
account. A case is never sent through the other host. Each call passes only the
edit text and the bodies of up to 15 candidate wiki cards stored when the case
was captured; it does not attach source files, card source paths or the injected
context. Edits under git-ignored paths or `skipPaths` are not captured. Depending
on the host CLI and its account settings, that CLI sends this content to its
provider, and the CLI may add its own default instructions and the user's global
Codex instructions file to the model input.

The call runs in an empty temporary working directory with the host CLI's
isolation options enforced before launch; the plugin never reads or moves the
CLI's credentials. Sessions with `CLAUDE_CONFIG_DIR` redirected are excluded
from shared learning capture, and the worker runs `claude` without
`CLAUDE_CONFIG_DIR` so every call stays on the default `~/.claude` account that
captured the case. Asking the setup skill to turn the teacher off for a project
stops these calls while capture, recall and local training continue.

## Event log

Each install keeps one local event log for debugging: `events.jsonl` in the
install's private state directory (the directory of the selected route's install
journal, owner-only `0700`; the file is `0600`). Nothing is written under the
project or a per-user directory, and a project without a selected route writes
nothing. The log rotates past 4 MiB to `events.jsonl.1` and `events.jsonl.2`, so
it holds at most three files. The `logs` skill reads it; nothing leaves the
machine.

A line records which hook action or background step ran and how it ended:
outcome and fixed reason codes, elapsed time, the host session id, recall
diagnostics with the injected card paths and content hashes, wiki card paths and
titles written by compile and verification, and index publication. A hook failure records
the exception class and code location, never the message.

The log holds no prompt text or document bodies. A prompt is recorded as its
SHA-256 hash and character count, and extractor stderr the same way. Fields
whose names refer to secrets (token, password,
secret, credential, API key, authorization, cookie) are dropped from every line.

## Controls

- Keep a project inactive by not onboarding it. A memvane-db index selection can be
  withdrawn on request, including after selected writes; the old install directory remains.
- Decline the SessionStart onboarding notice for a project, or skip it for one
  session. The answer is kept per project under
  `~/.local/state/memvane/v2/onboarding/`, outside the repository, and never
  turns indexing, wiki generation or learning on.
- Add sensitive folders to `skipPaths` so they are excluded from new wiki
  generation; existing cards from those sources are hidden from search.
- Do not include credentials, private keys, or secrets in issue reports, logs,
  or generated wiki content.

This document describes MemVane. Each host CLI has its own terms,
retention, and privacy controls.
