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
_LOADED_SCHEMA = 'memvane-loaded-reader-v1'
_LOADED_FIELDS = frozenset(('endpoint_id','reader_epoch','store_format','generation',
                            'sentinel_sha256','owner_role','plugin_node'))
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


def _sentinel(path, digest):
    return (isinstance(path,str) and len(path) <= 1024 and path.endswith('.md') and
            not path.startswith('/') and '\\' not in path and
            not any(part in ('','.','..') for part in path.split('/')) and
            all(ord(ch) >= 32 and ord(ch) != 127 for ch in path) and
            isinstance(digest,str) and _SHA.fullmatch(digest) is not None)


def _loaded_config(registry):
    path = _registry() / registry['challenge_id'] / 'loaded.json'
    if not os.path.lexists(path):
        return None
    value = _read(path)
    if (set(value) != {'schema','sentinel_path','sentinel_sha256'} or
            value['schema'] != _LOADED_SCHEMA or
            not _sentinel(value['sentinel_path'],value['sentinel_sha256'])):
        _refuse()
    return value


def begin_loaded_bridge(state: Path, *, release_commit: str, expected_hosts: frozenset,
                        sentinel_path: str, sentinel_sha256: str):
    if (expected_hosts != frozenset(('claude','codex')) or
            not _sentinel(sentinel_path,sentinel_sha256)):
        _refuse()
    challenge = begin_bridge_preflight(state,release_commit=release_commit,expected_hosts=expected_hosts)
    directory,_nonce = _issued(challenge,BridgeChallenge)
    _write(directory/'loaded.json',{'schema':_LOADED_SCHEMA,'sentinel_path':sentinel_path,
                                    'sentinel_sha256':sentinel_sha256})
    return challenge


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
    loaded = _loaded_config(registry)
    expected = {'session_id', 'process_pid', 'process_start', 'executable',
                          'plugin_root', 'release_commit', 'manifest_sha256', 'hook_stage',
                          'observed_entrypoint', 'observed_challenge_id', 'observed_nonce_sha256'}
    if loaded is not None:
        expected |= _LOADED_FIELDS
    if (set(observed) != expected
            or observed['observed_challenge_id'] != registry['challenge_id']
            or observed['observed_nonce_sha256'] != registry['nonce_sha256']
            or observed['release_commit'] != registry['release_commit']
            or observed['hook_stage'] != ('loaded-v5-reader' if loaded is not None else 'registered-v4')
            or observed['observed_entrypoint'] != entry
            or type(observed['session_id']) is not str or not 0 < len(observed['session_id']) <= 256
            or type(observed['process_pid']) is not int or observed['process_pid'] <= 0
            or type(observed['process_start']) not in (int, float)
            or not math.isfinite(observed['process_start']) or observed['process_start'] <= 0):
        _refuse()
    if loaded is not None and (
            not isinstance(observed['endpoint_id'],str) or not _SHA.fullmatch(observed['endpoint_id']) or
            type(observed['reader_epoch']) is not int or observed['reader_epoch'] < 1 or
            observed['store_format'] != 'v6' or
            not isinstance(observed['generation'],str) or not 0 < len(observed['generation']) <= 256 or
            observed['sentinel_sha256'] != loaded['sentinel_sha256'] or
            observed['owner_role'] not in ('owner','companion')):
        _refuse()
    exe = Path(observed['executable'])
    root = Path(observed['plugin_root'])
    if not exe.is_file() or exe.resolve(strict=True) != exe or root.resolve(strict=True) != root:
        _refuse()
    proof = inspect_full_plugin_candidate(str(root))
    if (proof['pluginRoot'] != str(root) or proof['sourceCommit'] != registry['release_commit']
            or proof['bundleManifestSha256'] != observed['manifest_sha256']):
        _refuse()
    if loaded is not None:
        info = root.stat()
        if observed['plugin_node'] != [info.st_dev,info.st_ino]:
            _refuse()


def _receipt(registry, observed, host):
    value = {k: registry[k] for k in ('challenge_id', 'nonce_sha256', 'issued_at', 'expires_at')}
    return dict(value, schema=_LOADED_SCHEMA if _loaded_config(registry) is not None else _SCHEMA, host=host,
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
    if (_loaded_config(registry) is not None or registry['release_commit'] != release_commit or registry['expected_hosts'] != hosts
            or set(receipts) != {directory / (host + '.json') for host in hosts}):
        _refuse()
    for host in hosts:
        observed = _read(directory / (host + '.audit'))
        _check_observation(observed, registry, host)
        if _read(directory / (host + '.json')) != _receipt(registry, observed, host):
            _refuse()
    return _issue(BridgeReadiness, {}, (tuple(receipts), release_commit, expected_hosts, registry['state']))


def check_loaded_bridge(receipts: list, *, release_commit: str, expected_hosts: frozenset) -> dict:
    hosts = _hosts(expected_hosts)
    if (expected_hosts != frozenset(('claude','codex')) or type(receipts) is not list or
            len(receipts) != len(hosts) or not all(isinstance(p,Path) for p in receipts)):
        _refuse()
    directory = receipts[0].parent
    registry = _load_challenge(directory)
    loaded = _loaded_config(registry)
    if (loaded is None or registry['release_commit'] != release_commit or
            registry['expected_hosts'] != hosts or
            set(receipts) != {directory/(host+'.json') for host in hosts}):
        _refuse()
    observations=[]
    for host in hosts:
        observed=_read(directory/(host+'.audit'))
        _check_observation(observed,registry,host)
        if _read(directory/(host+'.json')) != _receipt(registry,observed,host):
            _refuse()
        observations.append(observed)
    first,second=observations
    if (any(first[key] != second[key] for key in
            ('endpoint_id','reader_epoch','store_format','generation','sentinel_sha256','manifest_sha256')) or
            {first['owner_role'],second['owner_role']} != {'owner','companion'} or
            first['plugin_root'] == second['plugin_root'] or first['session_id'] == second['session_id'] or
            (first['process_pid'],first['process_start']) == (second['process_pid'],second['process_start'])):
        _refuse()
    import laya_install_journal as journal
    import laya_journal_reader as reader
    path=Path(registry['state'])/'install-journal.sqlite'
    with journal.read_only_journals():
        if journal.journal_version(path) != 5:
            _refuse()
        selected=journal.inspect_journal(path)
        current=reader.read_selection(path)
    owner=next(item for item in observations if item['owner_role']=='owner')
    if (selected.get('state') not in ('active','restored_old') or selected.get('pending') is not None or
            not isinstance(selected.get('selected'),dict) or
            selected['selected'].get('pluginRoot') != owner['plugin_root'] or
            selected['selected'].get('sourceCommit') != release_commit or
            selected['selected'].get('bundleManifestSha256') != first['manifest_sha256'] or
            current.preferred_endpoint != first['endpoint_id'] or current.epoch != first['reader_epoch']):
        _refuse()
    return {'status':'loaded_bridge_verified','endpoint_id':first['endpoint_id'],
            'reader_epoch':first['reader_epoch'],'store_format':first['store_format'],
            'generation':first['generation']}


def find_bridge_preflight(state: Path, *, release_commit: str):
    """Revalidate a live receipt set from the private challenge registry."""
    from laya_journal_paths import check_journal_namespace
    state = _dir(Path(state))
    check_journal_namespace(state)
    if type(release_commit) is not str or not COMMIT.fullmatch(release_commit):
        _refuse()
    try:
        registry = _registry()
        with os.scandir(registry) as entries:
            names = sorted(entry.name for entry in entries
                           if _ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False))
        for name in names:
            directory = registry / name
            try:
                challenge = _load_challenge(directory)
                hosts = frozenset(challenge['expected_hosts'])
                if (challenge['state'] != str(state) or challenge['release_commit'] != release_commit or
                        not {'claude','codex'} <= hosts):
                    continue
                return check_bridge_preflight([directory / (host + '.json') for host in sorted(hosts)],
                                              release_commit=release_commit, expected_hosts=hosts)
            except (OSError, ValueError, TypeError, KeyError):
                continue
    except OSError:
        pass
    _refuse()


def find_loaded_bridge(state: Path, *, release_commit: str) -> list:
    """Receipt paths of a live loaded-reader set (Claude and Codex) for ``state``.

    Scans the private challenge registry like ``find_bridge_preflight`` but
    only for ``loaded.json`` challenges, and returns the first set that
    passes ``check_loaded_bridge``: both hosts observed the current reader
    epoch and preferred endpoint of the journal-selected bundle at
    ``release_commit``, inside the challenge TTL. Nothing is collected here.
    """
    from laya_journal_paths import check_journal_namespace
    state = _dir(Path(state))
    check_journal_namespace(state)
    if type(release_commit) is not str or not COMMIT.fullmatch(release_commit):
        _refuse()
    hosts = frozenset(('claude', 'codex'))
    try:
        registry = _registry()
        with os.scandir(registry) as entries:
            names = sorted(entry.name for entry in entries
                           if _ID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False))
    except (OSError, ValueError):
        _refuse()
    for name in names:
        directory = registry / name
        try:
            challenge = _load_challenge(directory)
            if (challenge['state'] != str(state) or challenge['release_commit'] != release_commit
                    or frozenset(challenge['expected_hosts']) != hosts
                    or _loaded_config(challenge) is None):
                continue
            refs = [directory / (host + '.json') for host in sorted(hosts)]
            check_loaded_bridge(refs, release_commit=release_commit, expected_hosts=hosts)
            return refs
        except (OSError, ValueError, TypeError, KeyError):
            continue
    _refuse()


def ensure_bridge_preflight(state: Path, *, release_commit: str):
    """Reuse fresh host evidence or collect it before the writer acquires locks."""
    def mark_failure(exc, stage, host=None):
        try:
            exc.bridge_stage, exc.bridge_host = stage, host
        except Exception:
            pass  # Diagnostic annotation never changes the failure.
    try:
        return find_bridge_preflight(state, release_commit=release_commit)
    except ValueError as exc:
        if str(exc) != 'reader_hosts_pending':
            mark_failure(exc, 'reuse')
            raise
    except Exception as exc:
        mark_failure(exc, 'reuse')
        raise
    hosts = frozenset(('claude', 'codex'))
    stage, host = 'host_support', None
    try:
        _require_supported_hosts(hosts)
        stage = 'challenge'
        challenge = begin_bridge_preflight(state, release_commit=release_commit, expected_hosts=hosts)
        # Avoid starting Claude if the Codex host cannot complete its observation.
        refs = []
        stage = 'observe_host'
        for host in ('codex', 'claude'):
            refs.append(collect_bridge_preflight(challenge, host=host))
        stage, host = 'validate_receipts', None
        return check_bridge_preflight(refs, release_commit=release_commit, expected_hosts=hosts)
    except Exception as exc:
        mark_failure(exc, stage, host)
        raise


def _require_supported_hosts(hosts):
    import memvane_paths
    from laya_journal_v4 import _hermes_copies, legacy_hermes_present
    if (legacy_hermes_present() or
            ('hermes' not in hosts and _hermes_copies(memvane_paths.PLUGIN_ID, Path.home()))):
        _refuse()


def require_bridge_preflight(token, state, proof):
    refs, commit, hosts, bound_state = _issued(token, BridgeReadiness)
    if (bound_state != str(state) or proof['sourceCommit'] != commit
            or not {'claude', 'codex'} <= hosts):
        _refuse()
    check_bridge_preflight(list(refs), release_commit=commit, expected_hosts=hosts)
    _require_supported_hosts(hosts)
    seen = {str(_read(ref)['plugin_root']) for ref in refs}
    if str(proof['executingPluginRoot']) not in seen:
        _refuse()
    # The registered host observations above are the bridge boundary. Cache
    # directories can include older, unregistered versions and do not prove
    # that another host is installed or running.
    if any(_read(ref)['manifest_sha256'] != proof['bundleManifestSha256'] for ref in refs):
        _refuse()
