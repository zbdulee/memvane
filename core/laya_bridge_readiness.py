"""Private v4 hook challenge and observer receipts. No selection/write authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
import weakref

from laya_product_bundle import COMMIT, inspect_full_plugin_candidate

_HOSTS = frozenset(('claude', 'codex', 'hermes'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_SCHEMA = 'memvane-bridge-preflight-v1'
_TTL = 600
_ISSUED = weakref.WeakKeyDictionary()


def _refuse():
    raise ValueError('reader_hosts_pending')


@dataclass(frozen=True, eq=False, init=False)
class BridgeChallenge:
    challenge_id: str
    nonce_sha256: str
    issued_at: float
    expires_at: float
    release_commit: str
    expected_hosts: frozenset

    def __init__(self):
        _refuse()


@dataclass(frozen=True, eq=False, init=False)
class BridgeReadiness:
    def __init__(self):
        _refuse()


def _issue(kind, fields, evidence):
    token = object.__new__(kind)
    for key, value in fields.items():
        object.__setattr__(token, key, value)
    _ISSUED[token] = evidence
    return token


def _issued(token, kind):
    if type(token) is not kind or token not in _ISSUED:
        _refuse()
    return _ISSUED[token]


def _hosts(value):
    if type(value) is not frozenset or not value or not value <= _HOSTS:
        _refuse()
    return sorted(value)


def _dir(path, *, private=True):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        _refuse()
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o022)):
        _refuse()
    return path


def _registry(*, create=False):
    from laya_journal_paths import journal_directory
    return journal_directory('bridge-preflight', create=create)


def _raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _write(path, value):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(_raw(value))
        out.flush()
        os.fsync(out.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _read(path):
    _dir(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as src:
            info = os.fstat(src.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 16384 or not os.path.samestat(info, path.lstat())):
                _refuse()
            raw = src.read(16385)
            if len(raw) != info.st_size:
                _refuse()
        value = json.loads(raw)
        if not isinstance(value, dict) or _raw(value) != raw:
            _refuse()
        return value
    except (OSError, TypeError, UnicodeError) as exc:
        raise ValueError('reader_hosts_pending') from exc


def _load_challenge(directory):
    if (directory.parent != _registry() or not _ID.fullmatch(directory.name)
            or _dir(directory) != directory):
        _refuse()
    record = _read(directory / 'challenge.json')
    now = time.time()
    if (set(record) != {'challenge_id', 'nonce_sha256', 'issued_at', 'expires_at',
                        'release_commit', 'expected_hosts', 'state', 'state_node'}
            or record['challenge_id'] != directory.name
            or not _SHA.fullmatch(record['nonce_sha256'])
            or not COMMIT.fullmatch(record['release_commit'])
            or not 0 < record['expires_at'] - record['issued_at'] <= _TTL
            or not record['issued_at'] <= now < record['expires_at']
            or _hosts(frozenset(record['expected_hosts'])) != record['expected_hosts']):
        _refuse()
    state = _dir(Path(record['state'])).stat()
    if [state.st_dev, state.st_ino] != record['state_node']:
        _refuse()
    return record


def begin_bridge_preflight(state: Path, *, release_commit: str, expected_hosts: frozenset):
    from laya_journal_paths import check_journal_namespace
    check_journal_namespace(state)
    if type(release_commit) is not str or not COMMIT.fullmatch(release_commit):
        _refuse()
    state = _dir(Path(state))
    hosts = _hosts(expected_hosts)
    directory = _registry(create=True) / secrets.token_hex(16)
    directory.mkdir(mode=0o700)
    nonce = secrets.token_hex(32)
    now = time.time()
    fields = dict(challenge_id=directory.name, nonce_sha256=_sha(nonce.encode('ascii')),
                  issued_at=now, expires_at=now + _TTL, release_commit=release_commit,
                  expected_hosts=hosts)
    info = state.stat()
    _write(directory / 'challenge.json', dict(fields, state=str(state),
                                              state_node=[info.st_dev, info.st_ino]))
    return _issue(BridgeChallenge, {**fields, 'expected_hosts': expected_hosts}, (directory, nonce))


def bridge_preflight(payload: dict, *, host: str, executing_root: Path) -> dict:
    """Answer an explicit collector challenge. The answer alone is not a receipt."""
    if not isinstance(payload, dict) or host not in _HOSTS:
        _refuse()
    value = payload.get('prompt')
    match = re.fullmatch(r'\[memvane-bridge-preflight ([0-9a-f]{32}) ([0-9a-f]{64})\]',
                         value if isinstance(value, str) else '')
    if match is None:
        _refuse()
    ident, nonce = match.groups()
    challenge = _load_challenge(_registry() / ident)
    if (host not in challenge['expected_hosts'] or _sha(nonce.encode('ascii')) != challenge['nonce_sha256']
            or Path(executing_root) != Path(__file__).resolve().parents[1]):
        _refuse()
    import laya_install_journal as journal
    if journal.journal_version(Path(challenge['state']) / 'install-journal.sqlite') != 4:
        _refuse()
    proof = inspect_full_plugin_candidate(str(executing_root))
    if proof['sourceCommit'] != challenge['release_commit']:
        _refuse()
    return dict(observed_challenge_id=ident, observed_nonce_sha256=_sha(nonce.encode('ascii')),
                plugin_root=str(executing_root), release_commit=proof['sourceCommit'],
                manifest_sha256=proof['bundleManifestSha256'], hook_stage='registered-v4')


def challenge_prompt(challenge):
    directory, nonce = _issued(challenge, BridgeChallenge)
    registry = _load_challenge(directory)
    if _sha(nonce.encode('ascii')) != registry['nonce_sha256']:
        _refuse()
    return f"[memvane-bridge-preflight {directory.name} {nonce}]"


def registered_response(payload, host, decision, root):
    ident = os.environ.get('MEMVANE_BRIDGE_PREFLIGHT', '')
    prompt = payload.get('prompt')
    if (not _ID.fullmatch(ident) or not isinstance(prompt, str)
            or not prompt.startswith(f'[memvane-bridge-preflight {ident} ')):
        return None
    registry = _load_challenge(_registry() / ident)
    if (decision.get('state') != 'candidate' or
            decision.get('journalPath') != str(Path(registry['state']) / 'install-journal.sqlite')):
        _refuse()
    return bridge_preflight(payload, host=host, executing_root=root)


def _observe_registered_hook(challenge, host):
    """Only a collector that owns the actual launched host may attest its descendants."""
    from laya_bridge_observer import observe_registered_hook
    return observe_registered_hook(challenge, host)


def _check_observation(observed, registry, host):
    entry = ('hermes_adapter/core_bridge.py:recall_context' if host == 'hermes'
             else 'hooks/run-hook:recall:' + host)
    if (set(observed) != {'session_id', 'process_pid', 'process_start', 'executable',
                          'plugin_root', 'release_commit', 'manifest_sha256', 'hook_stage',
                          'observed_entrypoint', 'observed_challenge_id', 'observed_nonce_sha256'}
            or observed['observed_challenge_id'] != registry['challenge_id']
            or observed['observed_nonce_sha256'] != registry['nonce_sha256']
            or observed['release_commit'] != registry['release_commit']
            or observed['hook_stage'] != 'registered-v4' or observed['observed_entrypoint'] != entry
            or type(observed['session_id']) is not str or not 0 < len(observed['session_id']) <= 256
            or type(observed['process_pid']) is not int or observed['process_pid'] <= 0
            or type(observed['process_start']) not in (int, float)
            or not math.isfinite(observed['process_start']) or observed['process_start'] <= 0):
        _refuse()
    exe = Path(observed['executable'])
    root = Path(observed['plugin_root'])
    if not exe.is_file() or exe.resolve(strict=True) != exe or root.resolve(strict=True) != root:
        _refuse()
    proof = inspect_full_plugin_candidate(str(root))
    if (proof['pluginRoot'] != str(root) or proof['sourceCommit'] != registry['release_commit']
            or proof['bundleManifestSha256'] != observed['manifest_sha256']):
        _refuse()


def _receipt(registry, observed, host):
    value = {k: registry[k] for k in ('challenge_id', 'nonce_sha256', 'issued_at', 'expires_at')}
    return dict(value, schema=_SCHEMA, host=host,
                **{k: v for k, v in observed.items() if not k.startswith('observed_')},
                observed_entrypoint=observed['observed_entrypoint'])


def collect_bridge_preflight(challenge, *, host: str) -> Path:
    directory, _nonce = _issued(challenge, BridgeChallenge)
    registry = _load_challenge(directory)
    if host not in registry['expected_hosts']:
        _refuse()
    observed = _observe_registered_hook(challenge, host)
    _check_observation(observed, registry, host)
    _write(directory / (host + '.audit'), observed)
    ref = directory / (host + '.json')
    _write(ref, _receipt(registry, observed, host))
    return ref


def check_bridge_preflight(receipts: list, *, release_commit: str, expected_hosts: frozenset):
    hosts = _hosts(expected_hosts)
    if type(receipts) is not list or len(receipts) != len(hosts) or not all(isinstance(p, Path) for p in receipts):
        _refuse()
    directory = receipts[0].parent
    registry = _load_challenge(directory)
    if (registry['release_commit'] != release_commit or registry['expected_hosts'] != hosts
            or set(receipts) != {directory / (host + '.json') for host in hosts}):
        _refuse()
    for host in hosts:
        observed = _read(directory / (host + '.audit'))
        _check_observation(observed, registry, host)
        if _read(directory / (host + '.json')) != _receipt(registry, observed, host):
            _refuse()
    return _issue(BridgeReadiness, {}, (tuple(receipts), release_commit, expected_hosts, registry['state']))


def require_bridge_preflight(token, state, proof):
    refs, commit, hosts, bound_state = _issued(token, BridgeReadiness)
    if (bound_state != str(state) or proof['sourceCommit'] != commit
            or not {'claude', 'codex'} <= hosts):
        _refuse()
    check_bridge_preflight(list(refs), release_commit=commit, expected_hosts=hosts)
    import memvane_paths
    from laya_journal_v4 import _hermes_copies, host_copies, legacy_hermes_present
    if (legacy_hermes_present() or
            ('hermes' not in hosts and _hermes_copies(memvane_paths.PLUGIN_ID, Path.home()))):
        _refuse()
    seen = {str(_read(ref)['plugin_root']) for ref in refs}
    if str(proof['executingPluginRoot']) not in seen:
        _refuse()
    # Discover installed copies independently of paths in the QA responses.
    anchor = (Path.home() / '.claude/plugins/cache' / memvane_paths.MARKETPLACE /
              memvane_paths.PLUGIN_ID / 'reader-capability-inventory')
    copies = set(host_copies(anchor)) - {str(anchor)}
    for copy in copies:
        actual = inspect_full_plugin_candidate(copy)
        if actual['sourceCommit'] != commit or actual['bundleManifestSha256'] != proof['bundleManifestSha256']:
            _refuse()
    if any(_read(ref)['manifest_sha256'] != proof['bundleManifestSha256'] for ref in refs):
        _refuse()
