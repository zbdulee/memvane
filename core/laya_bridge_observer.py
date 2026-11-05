"""Owned native-host QA collector. A wire reply alone is never host authority."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import uuid

import laya_bridge_readiness as B
from laya_bridge_process import (peer_identity as _peer_identity,
    process_identity as _process_identity, descendant_chain, descendants, same_process)
from laya_product_bundle import BundleEvidenceError, inspect_full_plugin_candidate

_MAX = 16384
_TIMEOUT = 8
_ENVELOPE = {'observed_challenge_id','observed_nonce_sha256','plugin_root',
             'release_commit','manifest_sha256','hook_stage'}
_LOADED_QUERY = 'memvaneqa bridge reader sentinel'


def _refuse():
    raise ValueError('reader_hosts_pending')


def _probe_loaded_reader(project, root, *, sentinel_path, sentinel_sha256, deadline=None):
    """Observe a v5 reader on a QA sentinel; this result is not a host receipt."""
    import laya_install_journal as journal
    import laya_reader_dispatch as dispatch
    import laya_reader_lease as lease
    import laya_reader_open as opens
    import laya_journal_reader as reader
    project, root = Path(project), Path(root)
    if (not project.is_absolute() or project.resolve(strict=True) != project or
            not root.is_absolute() or root.resolve(strict=True) != root or
            not isinstance(sentinel_path,str) or not sentinel_path.endswith('.md') or
            sentinel_path.startswith('/') or '\\' in sentinel_path or
            any(part in ('','.','..') for part in sentinel_path.split('/')) or
            not isinstance(sentinel_sha256,str) or not B._SHA.fullmatch(sentinel_sha256)):
        _refuse()
    deadline = time.monotonic()+3 if deadline is None else deadline
    if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.monotonic() >= deadline:
        _refuse()
    info = root.stat()
    plugin_node = [info.st_dev,info.st_ino]
    with journal.read_only_journals():
        decision = journal.select_route(str(project), action='recall')
        if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
            _refuse()
        proof = journal.verify_selected_route(decision)
        if (proof.get('productRouteAllowed') is not True or
                proof.get('executingPluginRoot') != str(root)):
            _refuse()
        with lease.resolve_endpoint(project,action='recall',host_root=root) as endpoint:
            _record,_att,selected = reader._recheck(endpoint)
            if (selected.get('productRouteAllowed') is not True or
                    selected.get('executingPluginRoot') != str(root)):
                _refuse()
            with opens.inspect_warm_snapshot(endpoint,deadline=deadline) as snapshot:
                result=dispatch.query_endpoint(endpoint,checkout=project,prompt=_LOADED_QUERY,
                    query_vector=None,deadline=deadline,snapshot=snapshot,decision=decision,provider=None)
                if (result.store_format != 'v6' or result.endpoint_id != endpoint.endpoint_id or
                        not any(row.get('path') == sentinel_path and row.get('hash') == sentinel_sha256
                                for row in result.rows)):
                    _refuse()
                observation = {'endpoint_id': endpoint.endpoint_id, 'reader_epoch': result.reader_epoch,
                               'store_format': result.store_format, 'generation': result.data_generation,
                               'sentinel_sha256': sentinel_sha256,
                               'plugin_node': plugin_node,
                               'owner_role': 'owner' if decision['pluginRoot'] == str(root) else 'companion'}
            current_root = root.stat()
            if journal.select_route(str(project),action='recall') != decision or \
                    journal.verify_selected_route(decision) != proof or \
                    [current_root.st_dev,current_root.st_ino] != plugin_node:
                _refuse()
            return observation


@dataclass(frozen=True)
class HostSpec:
    argv: tuple
    cwd: Path
    executable: Path
    plugin_root: Path
    release_commit: str
    manifest_sha256: str
    session_id: str


def _host_spec(challenge, host):
    """Resolve registration and native executable; no caller command override."""
    if (host not in ('claude', 'codex') or os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1'
            or os.environ.get('QMD_SANDBOX')):
        _refuse()
    from laya_journal_v4 import host_copies
    from laya_bundle_upgrade import host_layout, installed_commit, select_source
    import laya_install_journal as J
    directory, _nonce = B._issued(challenge, B.BridgeChallenge)
    registry = B._load_challenge(directory)
    journal = Path(registry['state']) / 'install-journal.sqlite'
    loaded = B._loaded_config(registry)
    with J.read_only_journals():
        if J.journal_version(journal) != (5 if loaded is not None else 4):
            _refuse()
        selected = J.inspect_journal(journal)
    anchor = selected['selected']['pluginRoot']
    matches = []
    for name in host_copies(anchor):
        root = Path(name)
        try:
            kind, _source = host_layout(root)
        except BundleEvidenceError:
            continue
        if kind != host or installed_commit(root) != challenge.release_commit:
            continue
        try:
            proof = inspect_full_plugin_candidate(str(root))
        except ValueError:
            if loaded is not None:
                raise
            # A freshly installed cache has not yet been adopted by a worker.
            # Re-prove its exact committed bytes and let the existing adopter
            # recover interrupted seal metadata before observing the host.
            from laya_product_bundle import adopt_host_loaded_plugin
            source, commit = select_source(root)
            if commit != challenge.release_commit:
                _refuse()
            adopt_host_loaded_plugin(source, root, commit_id=commit,
                                     vector_profile=selected['selected']['vectorProfile'])
            proof = inspect_full_plugin_candidate(str(root))
        if proof['sourceCommit'] == challenge.release_commit and proof['pluginRoot'] == str(root):
            matches.append((root, proof))
    if len(matches) != 1:
        _refuse()
    root, proof = matches[0]
    found = shutil.which(host)
    if not found:
        _refuse()
    exe = Path(found).resolve(strict=True)
    info = exe.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in (0, os.getuid()):
        _refuse()
    prompt, session = B.challenge_prompt(challenge), str(uuid.uuid4()) if host == 'claude' else ''
    if host == 'claude':
        argv = (str(exe), '--print', '--session-id', session, '--no-session-persistence',
                '--output-format', 'json', '--tools', '', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', prompt)
    else:
        argv = (str(exe), 'exec', '--json', '--ephemeral', '--sandbox', 'read-only', '--color', 'never', prompt)
    return HostSpec(argv, Path(selected['projectRoot']), exe, root, challenge.release_commit,
                    proof['bundleManifestSha256'], session)


def _private_socket(path, node=None, parent_node=None):
    parent = path.parent
    info = parent.lstat()
    if (parent.resolve(strict=True) != parent or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
            or (parent_node is not None and [info.st_dev, info.st_ino] != parent_node)):
        _refuse()
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (node is not None and [info.st_dev, info.st_ino] != node)):
        _refuse()
    return [info.st_dev, info.st_ino]


def _send(peer, value):
    raw = B._raw(value) + b'\n'
    if len(raw) > _MAX:
        _refuse()
    peer.sendall(raw)


def _receive(peer, *, deadline):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        _refuse()
    data = bytearray()
    original_timeout = peer.gettimeout()
    try:
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _refuse()
            peer.settimeout(remaining if original_timeout is None else min(original_timeout, remaining))
            part = peer.recv(min(2048, _MAX + 1 - len(data)))
            if not part or len(data) + len(part) > _MAX:
                _refuse()
            data.extend(part)
    finally:
        peer.settimeout(original_timeout)
    if time.monotonic() >= deadline:
        _refuse()
    if data.count(b'\n') != 1 or not data.endswith(b'\n'):
        _refuse()
    value = json.loads(data.decode('utf8'))
    if not isinstance(value, dict):
        _refuse()
    return value


def _cleanup(process, owned):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for item in reversed(list(owned.values())):
            if same_process(item):
                try:
                    os.kill(item['pid'], sig)
                except ProcessLookupError:
                    pass
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            process.poll()  # Reap the root, independently of tracked children.
            if not any(same_process(item) for item in owned.values()):
                break
            time.sleep(0.01)
        if not any(same_process(item) for item in owned.values()):
            break
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass  # Never signal an identity that no longer matches its snapshot.


def _dispatcher(chain, plugin_root, host):
    script = plugin_root / 'hooks/run-hook'
    if script.resolve(strict=True) != script or not script.is_file():
        _refuse()
    bash = {path.resolve(strict=True) for path in (Path('/bin/bash'), Path('/usr/bin/bash'))
            if path.is_file()}
    for item in chain[1:-1]:
        arguments = list(item['argv'][1:])
        while arguments and arguments[0] in ('--noprofile', '--norc', '-e', '-u', '-x'):
            arguments.pop(0)
        if (Path(item['executable']) in bash and
                arguments == [str(script), 'recall', host]):
            return item
    _refuse()


def _finish_after_ack(process, selector, deadline, output_bytes, *, captured=None, marker=None):
    """Let the hook print its block and the CLI reap it before owned cleanup."""
    while time.monotonic() < deadline:
        for key, _events in selector.select(0.02):
            data = os.read(key.fileobj.fileno(), 4096)
            if not data:
                selector.unregister(key.fileobj)
                continue
            output_bytes += len(data)
            if output_bytes > 131072:
                _refuse()
            if captured is not None and key.data == 'stdout':
                captured.extend(data)
        code = process.poll()
        if code is not None and not selector.get_map():
            # A native CLI may represent the intended hook block as nonzero.
            # A signal or an unfinished root never constitutes completion.
            if code < 0 or (marker is not None and marker not in captured):
                _refuse()
            return
    _refuse()


def observe_registered_hook(challenge, host):
    directory, _nonce = B._issued(challenge, B.BridgeChallenge)
    registry = B._load_challenge(directory)
    if host not in registry['expected_hosts']:
        _refuse()
    loaded = B._loaded_config(registry)
    expected_envelope = _ENVELOPE | (B._LOADED_FIELDS if loaded is not None else frozenset())
    spec = _host_spec(challenge, host)
    if (spec.release_commit != challenge.release_commit or not spec.argv
            or Path(spec.argv[0]).resolve(strict=True) != spec.executable):
        _refuse()
    from runtime_lease import platform_temp_roots
    base = min(platform_temp_roots(), key=lambda value: len(str(value)))
    temporary = Path(tempfile.mkdtemp(prefix='mv-bo-', dir=base)).resolve()
    temporary.chmod(0o700)
    directory_node = [temporary.stat().st_dev, temporary.stat().st_ino]
    address, ref = temporary / 's.sock', directory / (host + '.transport.json')
    if len(os.fsencode(address)) >= 104:
        _refuse()
    process, owned, ref_node, socket_node = None, {}, None, None
    selector, streams = selectors.DefaultSelector(), {}
    loaded_stdout = bytearray() if loaded is not None else None
    output_bytes, session, deadline = 0, spec.session_id, time.monotonic() + _TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(address))
            address.chmod(0o600)
            socket_node = _private_socket(address)
            server.listen(1)
            selector.register(server, selectors.EVENT_READ, 'accept')
            env = {**os.environ, 'MEMVANE_BRIDGE_PREFLIGHT': challenge.challenge_id}
            process = subprocess.Popen(spec.argv, cwd=str(spec.cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
            root = _process_identity(process.pid)
            owned[root['pid']] = root
            if Path(root['executable']) != spec.executable:
                _refuse()
            B._write(ref, dict(schema='memvane-bridge-transport-v1', challenge_id=challenge.challenge_id,
                host=host, socket=str(address), socket_node=socket_node, directory_node=directory_node, collector_pid=os.getpid(),
                collector_start=list(_process_identity(os.getpid())['start_key']),
                root_pid=root['pid'], root_start=list(root['start_key'])))
            ref_node = [ref.stat().st_dev, ref.stat().st_ino]
            for stream, label in ((process.stdout,'stdout'), (process.stderr,'stderr')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
                streams[label] = bytearray()
            last_scan = 0
            while time.monotonic() < deadline:
                if time.monotonic() - last_scan > 0.25:
                    for item in descendants(root):
                        owned[item['pid']] = item
                    last_scan = time.monotonic()
                for key, _events in selector.select(0.05):
                    if key.data == 'accept':
                        if host == 'codex' and not session:
                            continue  # Typed root thread.started must precede acceptance.
                        with server.accept()[0] as peer:
                            peer.settimeout(max(0.01, deadline - time.monotonic()))
                            _private_socket(address, socket_node, directory_node)
                            uid, pid = _peer_identity(peer)
                            chain = descendant_chain(pid, root)
                            for item in chain:
                                owned[item['pid']] = item
                            if uid != os.getuid() or pid == root['pid']:
                                _refuse()
                            hook = chain[0]
                            script = str(spec.plugin_root / 'core/laya_host_response.py')
                            arguments = list(hook['argv'][1:])
                            while arguments and arguments[0] in ('-B','-I','-u','-s','-E'):
                                arguments.pop(0)
                            if (not arguments or arguments[0] != script
                                    or Path(script).resolve(strict=True) != Path(script)
                                    or not Path(script).is_file()):
                                _refuse()
                            if arguments[1:4] != ['serve-qa-stdin', 'recall', host]:
                                _refuse()
                            dispatcher = _dispatcher(chain, spec.plugin_root, host)
                            packet = _receive(peer, deadline=deadline)
                            if set(packet) != {'schema','challenge_id','host','session_id','observation'}:
                                _refuse()
                            observation = packet['observation']
                            if (not isinstance(observation, dict) or packet['schema'] != 'memvane-bridge-wire-v1' or
                                    packet['challenge_id'] != challenge.challenge_id or packet['host'] != host
                                    or not session or packet['session_id'] != session or set(observation) != expected_envelope
                                    or observation['observed_challenge_id'] != challenge.challenge_id
                                    or observation['observed_nonce_sha256'] != challenge.nonce_sha256
                                    or observation['plugin_root'] != str(spec.plugin_root)
                                    or observation['release_commit'] != spec.release_commit
                                    or observation['manifest_sha256'] != spec.manifest_sha256
                                    or observation['hook_stage'] != ('loaded-v5-reader' if loaded is not None else 'registered-v4')):
                                _refuse()
                            result = dict(observation, session_id=session, process_pid=root['pid'],
                                process_start=root['start'], executable=root['executable'],
                                observed_entrypoint='hooks/run-hook:recall:' + host)
                            B._check_observation(result, registry, host)
                            if (not same_process(hook) or not same_process(dispatcher) or
                                    not same_process(root) or _peer_identity(peer) != (uid,pid)):
                                _refuse()
                            renewed = descendant_chain(pid, root)
                            if _dispatcher(renewed, spec.plugin_root, host)['pid'] != dispatcher['pid']:
                                _refuse()
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                _refuse()
                            peer.settimeout(remaining)
                            _send(peer, dict(ack=True))
                            selector.unregister(server)
                            marker = (('memvane-loaded-v5-reader:' + loaded['sentinel_sha256']).encode('ascii')
                                      if loaded is not None else None)
                            _finish_after_ack(process, selector, deadline, output_bytes,
                                              captured=loaded_stdout, marker=marker)
                            return result
                    else:
                        data = os.read(key.fileobj.fileno(), 4096)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        output_bytes += len(data)
                        if output_bytes > 131072:
                            _refuse()
                        if loaded_stdout is not None and key.data == 'stdout':
                            loaded_stdout.extend(data)
                        if key.data == 'stdout' and host == 'codex':
                            streams['stdout'].extend(data)
                            if len(streams['stdout']) > _MAX:
                                _refuse()
                            while b'\n' in streams['stdout']:
                                line, _, rest = streams['stdout'].partition(b'\n')
                                streams['stdout'] = bytearray(rest)
                                try:
                                    event = json.loads(line)
                                except (ValueError, UnicodeError):
                                    continue
                                if isinstance(event,dict) and event.get('type') == 'thread.started':
                                    value = event.get('thread_id')
                                    if not isinstance(value,str) or not 0 < len(value) <= 256 or (session and session != value):
                                        _refuse()
                                    session = value
                if process.poll() is not None:
                    _refuse()
            _refuse()
    except (OSError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError('reader_hosts_pending') from exc
    finally:
        selector.close()
        if process is not None:
            _cleanup(process, owned)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        if ref_node is not None and ref.exists() and [ref.lstat().st_dev, ref.lstat().st_ino] == ref_node:
            ref.unlink()
        if socket_node is not None and address.exists():
            if _private_socket(address, socket_node, directory_node) == socket_node:
                address.unlink()
        if (temporary.exists() and [temporary.lstat().st_dev, temporary.lstat().st_ino] == directory_node
                and not list(temporary.iterdir())):
            temporary.rmdir()


def emit_registered_hook(challenge_id, host, observation, session_id):
    deadline = time.monotonic() + _TIMEOUT
    if not B._ID.fullmatch(challenge_id) or host not in ('claude','codex'):
        _refuse()
    directory = B._registry() / challenge_id
    registry = B._load_challenge(directory)
    loaded = B._loaded_config(registry)
    expected_envelope = _ENVELOPE | (B._LOADED_FIELDS if loaded is not None else frozenset())
    if host not in registry['expected_hosts'] or set(observation) != expected_envelope or not isinstance(session_id,str):
        _refuse()
    ref = directory / (host + '.transport.json')
    while not ref.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    transport = B._read(ref)
    if transport.get('schema') != 'memvane-bridge-transport-v1' or transport['challenge_id'] != challenge_id or transport['host'] != host:
        _refuse()
    path = Path(transport['socket'])
    _private_socket(path, transport['socket_node'], transport['directory_node'])
    collector = _process_identity(transport['collector_pid'])
    if list(collector['start_key']) != transport['collector_start']:
        _refuse()
    with socket.socket(socket.AF_UNIX) as peer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _refuse()
        peer.settimeout(remaining)
        peer.connect(str(path))
        if _peer_identity(peer) != (os.getuid(), collector['pid']):
            _refuse()
        _private_socket(path, transport['socket_node'], transport['directory_node'])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _refuse()
        peer.settimeout(remaining)
        _send(peer, dict(schema='memvane-bridge-wire-v1', challenge_id=challenge_id,
                        host=host, session_id=session_id, observation=observation))
        if _receive(peer, deadline=deadline) != {'ack': True}:
            _refuse()


def _qa_context(payload, host, decision, root):
    import laya_install_journal as J
    ident = os.environ.get('MEMVANE_BRIDGE_PREFLIGHT', '')
    if not B._ID.fullmatch(ident) or host not in ('claude','codex') or not isinstance(payload,dict):
        _refuse()
    registry = B._load_challenge(B._registry() / ident)
    loaded = B._loaded_config(registry)
    state, root = Path(registry['state']), Path(root)
    with J.read_only_journals():
        info = J.inspect_journal(state / 'install-journal.sqlite')
        version = J.journal_version(state / 'install-journal.sqlite')
    proof = inspect_full_plugin_candidate(str(root))
    if (host not in registry['expected_hosts'] or version != (5 if loaded is not None else 4)
            or decision.get('state') != 'candidate' or decision.get('journalPath') != str(state / 'install-journal.sqlite')
            or info.get('state') not in ('active','restored_old') or info.get('pending') is not None
            or payload.get('cwd') != info['projectRoot'] or decision.get('projectRoot') != info['projectRoot']
            or decision.get('scopeId') != info.get('scopeId') or root.resolve(strict=True) != root
            or proof['pluginRoot'] != str(root) or proof['sourceCommit'] != registry['release_commit']
            or not isinstance(info.get('selected'),dict)
            or any(decision.get(key) != info['selected'].get(key) for key in
                   ('pluginRoot','installId','generation','bundleManifestSha256',
                    'ownershipManifestSha256','vectorProfile'))
            or (loaded is not None and
                proof['bundleManifestSha256'] != info['selected']['bundleManifestSha256'])):
        _refuse()
    return ident, registry, proof


def _qa_envelope(payload, host, decision, root):
    import re
    ident, registry, proof = _qa_context(payload, host, decision, root)
    match = re.fullmatch(r'\[memvane-bridge-preflight ([0-9a-f]{32}) ([0-9a-f]{64})\]', payload.get('prompt',''))
    if not match or match[1] != ident or B._sha(match[2].encode('ascii')) != registry['nonce_sha256']:
        _refuse()
    loaded = B._loaded_config(registry)
    value = dict(observed_challenge_id=ident, observed_nonce_sha256=registry['nonce_sha256'],
                plugin_root=str(Path(root)), release_commit=proof['sourceCommit'],
                manifest_sha256=proof['bundleManifestSha256'],
                hook_stage='loaded-v5-reader' if loaded is not None else 'registered-v4')
    if loaded is not None:
        value.update(_probe_loaded_reader(Path(decision['projectRoot']),Path(root),
            sentinel_path=loaded['sentinel_path'],sentinel_sha256=loaded['sentinel_sha256']))
    return value


def qa_hook_reply(payload, host, decision, root):
    if 'MEMVANE_BRIDGE_PREFLIGHT' not in os.environ:
        return None
    reason = 'MemVane bridge preflight'
    try:
        envelope = _qa_envelope(payload, host, decision, root)
        session = payload.get('session_id', payload.get('sessionId', ''))
        emit_registered_hook(envelope['observed_challenge_id'], host, envelope, session)
        if envelope['hook_stage'] == 'loaded-v5-reader':
            reason = 'memvane-loaded-v5-reader:' + envelope['sentinel_sha256']
    except (ValueError, OSError, KeyError, TypeError):
        pass
    return dict(decision='block', reason=reason)


def qa_session_start(payload, host, decision, root):
    if 'MEMVANE_BRIDGE_PREFLIGHT' not in os.environ:
        return False
    try:
        _qa_context(payload, host, decision, root)
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False
