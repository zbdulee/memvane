"""Installer-bound local GGUF embedding adapter; no model discovery/download.

Construction is inert. Invocation requires an installer-approved manifest with
exact artifacts. The caller owns policy/resource admission and never derives
this manifest from a project marker, environment variable, or hook payload.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat

from laya_index_a import IndexContractError
from laya_index_vector import profile_identity


_HEX = re.compile(r'[0-9a-f]{64}\Z')
# Must match MEMBER_REFUSALS in laya_local_embed_worker.mjs.
MEMBER_REFUSALS = frozenset({'provider_context_exceeded', 'provider_chunks_invalid'})
DEFAULT_EMBED_MODEL_URI = ('hf:ggml-org/embeddinggemma-300M-GGUF/'
                           'embeddinggemma-300M-Q8_0.gguf')
_RUNTIMES = {'qmd-2.5.3-node-llama-cpp-3.18.1',
             'qmd-fork-f4633f9-node-llama-cpp-3.20.0'}


def _fail(reason):
    raise IndexContractError(reason)


def _canonical_file(path):
    if (not isinstance(path, str) or not os.path.isabs(path)
            or os.path.realpath(path) != path):
        _fail('provider_artifact_path_invalid')
    # Refuse a path whose ancestor can redirect between the reviewed name and
    # an unreviewed target. This does not replace installer ownership checks.
    cursor = Path('/')
    for part in Path(path).parts[1:]:
        cursor = cursor / part
        try:
            mode = os.lstat(cursor).st_mode
        except OSError as exc:
            raise IndexContractError('provider_artifact_missing') from exc
        if stat.S_ISLNK(mode):
            _fail('provider_artifact_path_invalid')
    if not stat.S_ISREG(os.lstat(path).st_mode):
        _fail('provider_artifact_path_invalid')
    return path


def _hash_file(path):
    path = _canonical_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            _fail('provider_artifact_changed')
        return digest.hexdigest()
    finally:
        os.close(fd)


def recipe_fingerprint(*, model_id, model_sha256, runtime_id, recipe_id,
                       provider_sha256, worker_sha256, node_sha256, module_entry_sha256,
                       closure_sha256):
    digests = (model_sha256, provider_sha256, worker_sha256, node_sha256,
               module_entry_sha256, closure_sha256)
    if (model_id != DEFAULT_EMBED_MODEL_URI
            or any(not isinstance(d, str) or not _HEX.fullmatch(d) for d in digests)
            or runtime_id not in _RUNTIMES
            or recipe_id not in ('qmd-query-format-single-document-v1',
                                 'qmd-regex-token-chunks-candidate-v1')):
        _fail('provider_recipe_invalid')
    chunk_recipe = ('single full document only; reject over context'
                    if recipe_id == 'qmd-query-format-single-document-v1' else
                    'qmd regex breakpoints; 900/135/200 tokens; '
                    'reject AST and lossy token fallback')
    value = ['laya-embed-fingerprint-v3', model_id, runtime_id, recipe_id,
             model_sha256, provider_sha256, worker_sha256, node_sha256, module_entry_sha256,
             closure_sha256,
             'loader:gpu=auto,gpuLayers=0,maxThreads=1,build=never,'
             'skipDownload=true,contextSize=min(2048,trainContextSize),threads=1',
             'query:task: search result | query: {text}',
             'doc:title: {title|none} | text: {text}',
             chunk_recipe]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


class ReviewedLocalQmdProvider:
    """Consume a manifest reviewed upstream by an installer-owned authority.

    A caller-supplied approval flag and entry-file hashes do not independently
    attest the transitive native dependency closure, model identity, or runtime
    latency. This adapter alone never authorizes product activation.
    """

    def __init__(self, manifest):
        if not isinstance(manifest, dict):
            _fail('provider_manifest_invalid')
        expected = {'approvedProviderId','providerArtifactSha256','providerRelativePath',
                    'providerPath',
                    'workerPath','workerSha256',
                    'serviceSocketPath',
                    'nodePath','nodeSha256','moduleEntryPath','moduleEntrySha256',
                    'modelPath','modelSha256','runtimeId','modelId',
                    'embeddingFingerprint','dimension','recipeId',
                    'dependencyClosureApproved','dependencyClosureSha256'}
        if set(manifest) != expected or manifest.get('dependencyClosureApproved') is not True:
            _fail('provider_closure_unapproved')
        for key in ('providerArtifactSha256','nodeSha256','moduleEntrySha256',
                    'modelSha256','embeddingFingerprint','dependencyClosureSha256',
                    'workerSha256'):
            if not isinstance(manifest[key], str) or not _HEX.fullmatch(manifest[key]):
                _fail('provider_manifest_invalid')
        if (not isinstance(manifest['approvedProviderId'], str)
                or not manifest['approvedProviderId']
                or manifest['runtimeId'] not in _RUNTIMES):
            _fail('provider_manifest_invalid')
        rel = manifest['providerRelativePath']
        if (not isinstance(rel, str) or not rel or rel.startswith('/')
                or '\\' in rel or any(part in ('', '.', '..') for part in rel.split('/'))
                or not isinstance(manifest['providerPath'], str)
                or not manifest['providerPath'].endswith('/' + rel)):
            _fail('provider_manifest_invalid')
        socket_path = manifest['serviceSocketPath']
        try:
            socket_bytes = socket_path.encode('utf-8') if isinstance(socket_path, str) else b''
        except UnicodeEncodeError as exc:
            raise IndexContractError('provider_socket_invalid') from exc
        if (not isinstance(socket_path, str) or not socket_path.startswith('/')
                or os.path.normpath(socket_path) != socket_path
                or os.path.realpath(os.path.dirname(socket_path))
                != os.path.dirname(socket_path)
                or len(socket_bytes) > 100):
            _fail('provider_socket_invalid')
        fingerprint = recipe_fingerprint(model_id=manifest['modelId'],
            model_sha256=manifest['modelSha256'], runtime_id=manifest['runtimeId'],
            recipe_id=manifest['recipeId'],
            provider_sha256=manifest['providerArtifactSha256'],
            worker_sha256=manifest['workerSha256'],
            node_sha256=manifest['nodeSha256'],
            module_entry_sha256=manifest['moduleEntrySha256'],
            closure_sha256=manifest['dependencyClosureSha256'])
        if fingerprint != manifest['embeddingFingerprint']:
            _fail('provider_recipe_mismatch')
        profile_id = profile_identity(manifest['modelId'], fingerprint,
                                      manifest['dimension'])
        self.manifest = dict(manifest)
        self.profile = {'profile_id': profile_id, 'model_id': manifest['modelId'],
                        'embedding_fingerprint': fingerprint,
                        'dimension': manifest['dimension'], 'metric': 'cosine'}

    def _verify_artifacts(self):
        m = self.manifest
        paths = {'providerPath': 'providerArtifactSha256',
                 'workerPath': 'workerSha256',
                 'nodePath': 'nodeSha256', 'moduleEntryPath': 'moduleEntrySha256',
                 'modelPath': 'modelSha256'}
        # The installer resolves the relative provider path into an owned
        # absolute path before this adapter is instantiated.
        for path_key, digest_key in paths.items():
            if _hash_file(m[path_key]) != m[digest_key]:
                _fail('provider_artifact_hash_mismatch')
        fd = os.open(m['modelPath'], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if os.read(fd, 4) != b'GGUF':
                _fail('provider_model_not_gguf')
        finally:
            os.close(fd)

    def _embed(self, kind, text, title=None, path=None, *, timeout=None):
        if not isinstance(text, str) or not text:
            _fail('provider_text_invalid')
        if title is not None and (kind != 'document' or not isinstance(title, str)):
            _fail('provider_text_invalid')
        if kind == 'document_chunks' and (not isinstance(path, str)
                or not path.endswith('.md') or path.startswith('/')
                or any(part in ('', '.', '..') for part in path.split('/'))):
            _fail('provider_text_invalid')
        try:
            text_bytes = text.encode('utf-8')
            if title is not None:
                title.encode('utf-8')
            if path is not None:
                path.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise IndexContractError('provider_text_invalid') from exc
        if len(text_bytes) > 5_000_000:
            _fail('provider_text_invalid')
        m = self.manifest
        request = {'kind': kind, 'text': text, 'title': title}
        if kind == 'document_chunks':
            request['path'] = path
        payload = json.dumps(request, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')
        # Query hooks only connect to an already warm, installer-owned worker.
        # Document preparation runs off hook and may use a longer budget.
        budget = 2.0 if kind == 'query' else 120.0
        if timeout is not None:
            limit = 2 if kind == 'query' else 300
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= limit:
                _fail('provider_timeout_invalid')
            budget = float(timeout)
        output_limit = 64_000_000 if kind == 'document_chunks' else 1_000_000
        socket_path = m['serviceSocketPath']
        try:
            parent = os.stat(os.path.dirname(socket_path), follow_symlinks=False)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077):
                _fail('provider_socket_invalid')
            info = os.lstat(socket_path)
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                _fail('provider_socket_invalid')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(budget)
                conn.connect(socket_path)
                # Refuse a path replaced between the owner/mode check and
                # connect. The private installer state parent is the owner
                # boundary; hooks never select a socket from project data.
                connected = os.lstat(socket_path)
                if (not stat.S_ISSOCK(connected.st_mode)
                        or (connected.st_dev, connected.st_ino) !=
                        (info.st_dev, info.st_ino)):
                    _fail('provider_socket_changed')
                conn.sendall(payload + b'\n')
                conn.shutdown(socket.SHUT_WR)
                output = bytearray()
                while True:
                    block = conn.recv(65536)
                    if not block:
                        break
                    output.extend(block)
                    if len(output) > output_limit:
                        _fail('provider_result_invalid')
        except (OSError, TimeoutError) as exc:
            raise IndexContractError('provider_unavailable') from exc
        try:
            envelope = json.loads(output.decode('utf-8'))
            # A document the worker refuses on content alone (too many chunks,
            # a chunk over the context) is named, so the fill can stop
            # retrying it. Busy and other failures stay provider_result_invalid.
            if (kind == 'document_chunks' and isinstance(envelope, dict)
                    and set(envelope) == {'approvedProviderId', 'providerArtifactSha256',
                                          'embeddingFingerprint', 'ok', 'result', 'refused'}
                    and envelope.get('approvedProviderId') == m['approvedProviderId']
                    and envelope.get('providerArtifactSha256') == m['providerArtifactSha256']
                    and envelope.get('embeddingFingerprint') == m['embeddingFingerprint']
                    and envelope.get('ok') is False and envelope.get('result') is None
                    and envelope.get('refused') in MEMBER_REFUSALS):
                _fail('provider_member_refused')
            if (not isinstance(envelope, dict) or
                    envelope.get('approvedProviderId') != m['approvedProviderId'] or
                    envelope.get('providerArtifactSha256') != m['providerArtifactSha256'] or
                    envelope.get('embeddingFingerprint') != m['embeddingFingerprint'] or
                    envelope.get('ok') is not True or
                    set(envelope) != {'approvedProviderId','providerArtifactSha256',
                                      'embeddingFingerprint','ok','result'}):
                _fail('provider_result_invalid')
            response = envelope['result']
            from laya_index_vector import _blob
            if kind == 'document_chunks':
                if (not isinstance(response, dict) or set(response) != {'chunks'}
                        or not isinstance(response['chunks'], list)
                        or not 1 <= len(response['chunks']) <= 1024):
                    _fail('provider_result_invalid')
                result = []
                last_pos = -1
                for seq, chunk in enumerate(response['chunks']):
                    if (not isinstance(chunk, dict) or set(chunk) != {'seq','pos','vector'}
                            or type(chunk['seq']) is not int or chunk['seq'] != seq
                            or type(chunk['pos']) is not int or chunk['pos'] <= last_pos
                            or not isinstance(chunk['vector'], list)
                            or len(chunk['vector']) != m['dimension']):
                        _fail('provider_result_invalid')
                    _blob(chunk['vector'], m['dimension'])
                    result.append(chunk)
                    last_pos = chunk['pos']
                return result
            if (not isinstance(response, dict) or set(response) != {'vector'}
                    or not isinstance(response['vector'], list)
                    or len(response['vector']) != m['dimension']):
                _fail('provider_result_invalid')
            _blob(response['vector'], m['dimension'])
            return response['vector']
        except IndexContractError:
            # Already named (provider_member_refused, provider_result_invalid);
            # IndexContractError is a ValueError, so keep it from the remap below.
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IndexContractError('provider_result_invalid') from exc

    def __call__(self, prompt, sealed_profile):
        keys = {'profileId': 'profile_id', 'modelId': 'model_id',
                'embeddingFingerprint': 'embedding_fingerprint',
                'dimension': 'dimension', 'metric': 'metric'}
        if not isinstance(sealed_profile, dict) or any(
                sealed_profile.get(public) != self.profile[internal]
                for public, internal in keys.items()):
            _fail('vector_profile_mismatch')
        vector = self._embed('query', prompt)
        m = self.manifest
        return {'profileId': self.profile['profile_id'],
                'approvedProviderId': m['approvedProviderId'],
                'providerArtifactSha256': m['providerArtifactSha256'],
                'vector': vector}

    def embed_document(self, body, title=None):
        """One full-document vector; rejects oversized model-token input.

        This recipe is deliberately distinct from QMD's 900/135 token chunking.
        The worker's tokenizer must reject overflow instead of truncating.
        """
        return self._embed('document', body, title)

    def embed_document_chunks(self, body, path, *, timeout=120):
        """Prepare QMD regex/token candidate chunks outside a blocking hook.

        Only a reviewed matching recipe may call this method. It does not
        attest QMD AST strategy, actual tokenizer parity, or provider approval.
        """
        if self.manifest['recipeId'] != 'qmd-regex-token-chunks-candidate-v1':
            _fail('provider_recipe_invalid')
        return self._embed('document_chunks', body, path=path, timeout=timeout)
