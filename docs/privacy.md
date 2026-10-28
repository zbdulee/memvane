# Data Handling and Privacy

## Default local processing

MemVane is inactive until a project is onboarded onto a selected Laya
Index route through the reviewed setup flow. A project without a selected route
gets no indexing, recall, compile or learning work.

For an onboarded project, the wiki cards under `.auto-context/wiki` are indexed
into a local memvane-db index under the install's private state directory. Raw
project files are read to generate wiki cards and to track path and hash
freshness; their bodies are not indexed. Embeddings are computed on the machine
by the local embedding runtime (`~/.local/state/memvane/embed-runtime`, Node with
node-llama-cpp and a local GGUF model), and recall queries are answered from
that local index.

Preparing the embedding runtime on a new machine downloads packages from the
npm registry and the model file from its pinned Hugging Face URL, and only after
explicit approval. The plugin does not provide a separate telemetry service.

## Optional wiki compile

Optional wiki compile runs only for an onboarded project; its minimal settings
turn compile on for that project. It passes selected Markdown source content and
limited wiki orientation context to the configured host CLI to produce or verify
wiki candidates. Depending on the configured host CLI and its account settings,
that CLI may send the content to its provider. Review the host provider's
privacy policy before onboarding a project that holds sensitive material.

The built-in adapters run in an isolated temporary working directory with their
tools disabled. This limits filesystem access by the nested host CLI, but it
does not change the host provider's data policy.

## Controls

- Keep a project inactive by not onboarding it. A memvane-db index selection can be
  withdrawn on request until the selected index takes its first write.
- Add sensitive folders to `skipPaths` so they are excluded from new wiki
  generation; existing cards from those sources are hidden from search.
- Do not include credentials, private keys, or secrets in issue reports, logs,
  or generated wiki content.

This document describes MemVane. Each host CLI has its own terms,
retention, and privacy controls.
