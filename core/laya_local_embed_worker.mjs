// Exact local GGUF worker. The owner starts --serve after admission; hooks only
// connect to its private socket. No model discovery, download, or store import.
import {readFileSync, chmodSync} from 'node:fs';
import {createServer} from 'node:net';
import {pathToFileURL} from 'node:url';
import {chunkDocumentByTokens, extractQmdMarkdownTitle} from './laya_qmd_chunking.mjs';

function validConfig(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    typeof value.modelPath === 'string' && typeof value.moduleEntryPath === 'string' &&
    Number.isInteger(value.dimension) && value.dimension > 0 && value.dimension <= 2048;
}

function validRequest(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    ['query', 'document', 'document_chunks'].includes(value.kind) &&
    typeof value.text === 'string' && value.text.length > 0 &&
    (value.title === null || (value.kind === 'document' && typeof value.title === 'string')) &&
    (value.kind !== 'document_chunks' || (value.title === null &&
      typeof value.path === 'string' && value.path.endsWith('.md') &&
      !value.path.startsWith('/') &&
      value.path.split('/').every(part => part && part !== '.' && part !== '..')));
}

async function load(config) {
  const {getLlama} = await import(pathToFileURL(config.moduleEntryPath).href);
  if (typeof getLlama !== 'function') throw Error('provider_module_invalid');
  // Installed QMD may ship only a Metal prebuilt. gpuLayers:0 keeps model
  // layers on CPU while the packaged binding avoids a new native build.
  const llama = await getLlama({gpu: 'auto', build: 'never', skipDownload: true,
                                maxThreads: 1, progressLogs: false});
  const model = await llama.loadModel({modelPath: config.modelPath, gpuLayers: 0});
  const contextSize = Math.min(2048, model.trainContextSize || 2048);
  const context = await model.createEmbeddingContext({contextSize, threads: 1});
  return {llama, model, context, contextSize};
}

// Refusals that depend only on the document: the same input is refused on
// every try. They are reported as `refused` so the caller can stop retrying
// that member; busy and every other failure keep the bare failure reply.
const MEMBER_REFUSALS = new Set(['provider_context_exceeded', 'provider_chunks_invalid']);

async function embed(config, state, request) {
  if (!validRequest(request)) throw Error('provider_request_invalid');
  const {model, context, contextSize} = state;
  async function vectorFor(text) {
    const tokens = model.tokenize(text);
    if (!Number.isInteger(tokens.length) || tokens.length > contextSize - 4)
      throw Error('provider_context_exceeded');
    const embedding = await context.getEmbeddingFor(text);
    const vector = Array.from(embedding.vector);
    if (vector.length !== config.dimension || !vector.every(Number.isFinite))
      throw Error('provider_vector_invalid');
    return vector;
  }
  if (request.kind === 'document_chunks') {
    const title = extractQmdMarkdownTitle(request.text, request.path);
    const chunks = await chunkDocumentByTokens(request.text, {
      tokenize: text => model.tokenize(text),
    });
    if (chunks.length < 1 || chunks.length > 1024) throw Error('provider_chunks_invalid');
    const rows = [];
    for (const chunk of chunks) {
      const vector = await vectorFor(`title: ${title || 'none'} | text: ${chunk.text}`);
      rows.push({seq: rows.length, pos: chunk.pos, vector});
    }
    return {chunks: rows};
  }
  const formatted = request.kind === 'query'
    ? `task: search result | query: ${request.text}`
    : `title: ${request.title || 'none'} | text: ${request.text}`;
  return {vector: await vectorFor(formatted)};
}

async function close(state) {
  await state.context.dispose();
  await state.model.dispose();
  if (typeof state.llama.dispose === 'function') await state.llama.dispose();
}

const serving = process.argv[2] === '--serve';
async function readConfig() {
  if (!serving) {
    return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(readFileSync(0)));
  }
  // The owner keeps stdin open after the first line. EOF means its process
  // died, so the model process exits instead of remaining orphaned.
  return await new Promise((resolve, reject) => {
    let bytes = 0;
    const pieces = [];
    function onData(chunk) {
      bytes += chunk.length;
      if (bytes > 65_536) { reject(Error('provider_config_invalid')); return; }
      pieces.push(chunk);
      const data = Buffer.concat(pieces);
      const end = data.indexOf(10);
      if (end < 0) return;
      process.stdin.off('data', onData);
      process.stdin.resume();
      try {
        resolve(JSON.parse(new TextDecoder('utf-8', {fatal: true})
          .decode(data.subarray(0, end))));
      } catch (error) { reject(error); }
    }
    process.stdin.on('data', onData);
    process.stdin.once('end', () => process.exit(0));
  });
}
const config = await readConfig();
if (!validConfig(config)) process.exit(2);
if (serving && (typeof config.serviceSocketPath !== 'string' ||
    !config.serviceSocketPath.startsWith('/') ||
    !/^[0-9a-f]{64}$/.test(config.embeddingFingerprint) ||
    !/^[0-9a-f]{64}$/.test(config.providerArtifactSha256) ||
    typeof config.approvedProviderId !== 'string')) process.exit(2);

const state = await load(config);
if (!serving) {
  try {
    process.stdout.write(JSON.stringify(await embed(config, state, config)));
  } finally { await close(state); }
} else {
  const identity = {
    approvedProviderId: config.approvedProviderId,
    providerArtifactSha256: config.providerArtifactSha256,
    embeddingFingerprint: config.embeddingFingerprint,
  };
  // One model context is shared. Serialize requests; busy queries return a
  // bounded failure so a document batch cannot quietly hold the hook.
  let busy = false;
  const server = createServer({allowHalfOpen: true}, socket => {
    // Client timeout/disconnect is ordinary; an unhandled socket error would
    // otherwise terminate the resident model process.
    socket.on('error', () => {});
    let bytes = 0;
    const pieces = [];
    socket.setTimeout(300_000, () => socket.destroy());
    socket.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > 5_100_000) { socket.destroy(); return; }
      pieces.push(chunk);
    });
    socket.on('end', async () => {
      if (busy) { socket.end(JSON.stringify({...identity, ok: false, result: null})); return; }
      busy = true;
      try {
        const data = new TextDecoder('utf-8', {fatal: true})
          .decode(Buffer.concat(pieces));
        const request = JSON.parse(data.trim());
        const result = await embed(config, state, request);
        socket.end(JSON.stringify({...identity, ok: true, result}));
      } catch (error) {
        const refused = error instanceof Error && MEMBER_REFUSALS.has(error.message)
          ? {refused: error.message} : {};
        socket.end(JSON.stringify({...identity, ok: false, result: null, ...refused}));
      } finally { busy = false; }
    });
  });
  server.maxConnections = 8;
  server.on('error', () => process.exit(6));
  server.listen(config.serviceSocketPath, () => {
    chmodSync(config.serviceSocketPath, 0o600);
    process.stdout.write('READY\n');
  });
  async function stop() {
    server.close();
    await close(state);
    process.exit(0);
  }
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
}
