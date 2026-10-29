"""Automatic Laya bundle upgrade after a host plugin update.

Every memvane-db install is bound to one exact installed plugin bundle. When a
host executes a different installed bundle, a SessionStart hook calls
``request``; it reads a small status file and may spawn one detached worker
(``run``) off the hook foreground. The worker moves every install of that
project under its own state root to the executing bundle:

* inactive build (registered/building/blocked/sealed): a sealed build is
  first marked for a fresh scan, then ``laya_inactive_upgrade``;
* bound build (prepared selection ledger): ``laya_bound_upgrade``;
* selected route (fresh-wiki ledger): ``laya_active_upgrade``, then one
  selected sync wake.

A state root that still holds a QMD-origin ledger (``qmd-cutover.json``) is
refused as ``qmd_origin_detach_required``; no retry clears it.

Before review the worker makes sure the Laya embedding runtime
(``laya_embed_runtime``) holds the install's attested provider bytes,
copying them from the attested paths (QMD runtime, QMD model cache) once.

Source rule: the host marketplace clone is the only unattended source, and
only when its HEAD is the installed commit (from the host's own record) and
its tracked files are clean. Provider review runs again and is accepted when
its content equals the previously sealed attestation
(``laya_provider_reproof``): provider, worker, node, module entry, native
closure and model bytes, runtime and recipe ids, dimension and vector
profile. Paths (bundle root, runtime directory, model file, service socket)
may differ; the new attestation re-states the old vector profile. A changed
model, runtime, recipe, closure or provider byte is never accepted here; the
install keeps its bundle and the notice says why. A host whose bundle is
older than the bound one never downgrades it.

The same worker converts a v3 install journal to v4
(``laya_journal_v4``) once every host copy of this plugin reads v4.

A selected install whose bound root is the executing root but whose sealed
ownership no longer verifies (the host re-extracted the same bundle in place)
is re-adopted at that root through the same review and switch; the same
commit with other bytes refuses as ``installed_bundle_content_changed``.

A busy provider/compute lane or install lock is contention, not a failure:
the worker waits it out a few times, then leaves a ``deferred`` status that a
later session retries after ``DEFER_RETRY_SECONDS`` without spending an attempt.

Until an upgrade completes, a selected project serves its last published
generation read-only (``laya_install_journal.verify_selected_route_read_only``).
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

SCHEMA = 'laya-bundle-auto-upgrade-v1'
STATUS = 'bundle-auto-upgrade.json'
LOCK = 'bundle-auto-upgrade.lock'
RUNNING_SECONDS = 1800
RETRY_SECONDS = 600
REFUSED_RETRY_SECONDS = 6 * 3600
MAX_FAST_ATTEMPTS = 3
DEADLINE_SECONDS = 1800
DEFER_RETRY_SECONDS = 120
# In-worker waits before a busy lane is left to a later session.
BUSY_BACKOFF_SECONDS = (5, 15, 45)
# Lane or lock contention: the same request succeeds once the holder is done,
# so it is deferred and retried and never counts as a failed attempt.
DEFERRALS = frozenset((
    'learning_compute_busy', 'learning_inference_deadline',
    'initial_build_busy', 'active_upgrade_resident_busy',
    'journal_v4_install_busy', 'embed_runtime_busy',
    'legacy_hermes_copy_present'))
# Deferrals an in-process wait cannot clear (an uninstall the owner does).
NO_BACKOFF = frozenset(('legacy_hermes_copy_present',))
# A state-root move waits for the same lanes plus the install and route locks.
MOVE_DEFERRALS = DEFERRALS | frozenset((
    'install_busy', 'cutover_operation_busy', 'state_move_upgrade_pending',
    'lock_busy'))
# A move a retry cannot clear: the install keeps working where it is.
MOVE_REFUSALS = frozenset((
    'state_move_cross_device', 'state_move_target_exists',
    'qmd_origin_detach_required', 'state_move_base_unsafe',
    'state_move_not_selected', 'state_move_socket_path_too_long',
    'state_move_path_invalid'))
# Refusals that are a supported resting state: no SessionStart notice. An
# unselected build moves once it is selected; another volume keeps its place.
MOVE_SILENT = frozenset(('state_move_cross_device', 'state_move_not_selected',
                         'state_move_socket_path_too_long'))
# Provider manifest keys that must name the new bundle's own files.
BUNDLE_PATH_KEYS = frozenset(('providerPath', 'workerPath'))
# Refusals a later retry cannot clear without a source, host or review change.
REFUSALS = frozenset((
    'provider_changed_rebuild_required', 'older_host_bundle',
    'marketplace_source_unavailable', 'marketplace_source_commit_mismatch',
    'marketplace_source_dirty', 'host_layout_unknown',
    'qmd_origin_detach_required', 'installed_bundle_content_changed',
    'laya_embed_runtime_source_unavailable',
    'laya_embed_runtime_source_changed', 'laya_embed_runtime_mismatch',
    'laya_embed_runtime_source_version_changed'))


class UpgradeRefused(RuntimeError):
    pass


def _refuse(reason):
    raise UpgradeRefused(reason)


def executing_root():
    return Path(__file__).resolve().parent.parent


def _now():
    return time.time_ns()


# ---------------------------------------------------------------- status file

def _private_dir(state):
    state = Path(state)
    info = state.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('bundle_upgrade_state_unsafe')
    return state


def read_status(state):
    """The worker's last status for this install, or None."""
    path = _private_dir(state) / STATUS
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 65536):
            return None
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            value.get('status') not in ('running', 'complete', 'failed', 'refused',
                                        'current', 'deferred') or
            not isinstance(value.get('executingRoot'), str) or
            type(value.get('updatedNs')) is not int or
            type(value.get('attempts')) is not int):
        return None
    return value


def _write_status(state, **fields):
    state = _private_dir(state)
    value = {'schema': SCHEMA, 'updatedNs': _now(), **fields}
    fd, name = tempfile.mkstemp(prefix='.bundle-auto-upgrade-', dir=state)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, sort_keys=True, ensure_ascii=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, state / STATUS)
    finally:
        Path(name).unlink(missing_ok=True)
    return value


def _event(state, event, outcome, reason=None, **fields):
    """One worker event in the install event log (``event_log``); never raises."""
    try:
        import event_log
        event_log.emit(event, outcome=outcome, reason=reason or None,
                       state_root=Path(state), host='worker', **fields)
    except Exception:
        pass


def _bound_bundle(state):
    """(plugin root name, source commit prefix) the install is bound to, or Nones."""
    try:
        _kind, identity = classify(state)
        root = identity.get('pluginRoot') if isinstance(identity, dict) else None
        commit = identity.get('sourceCommit') if isinstance(identity, dict) else None
        return (Path(root).name if isinstance(root, str) else None,
                commit[:12] if isinstance(commit, str) else None)
    except Exception:
        return None, None


# ------------------------------------------------------------ hook side API

def _fixture():
    return (os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or
            bool(os.environ.get('QMD_SANDBOX')))


def _spawn(project_root):
    subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'run',
                      str(project_root)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=True,
                     # HERMES_HOME: the journal v4 host gate must see a Hermes
                     # copy installed under a custom Hermes home.
                     env={**{key: os.environ[key] for key in
                             ('PATH', 'HOME', 'LANG', 'TMPDIR', 'USER', 'HERMES_HOME')
                             if key in os.environ},
                          'PYTHONDONTWRITEBYTECODE': '1'})


def _due(status, root, now):
    """Whether a new worker may start for this executing bundle."""
    if status is None or status['executingRoot'] != root:
        return True
    age = (now - status['updatedNs']) / 1e9
    if status['status'] == 'running':
        return age >= RUNNING_SECONDS
    if status['status'] == 'refused':
        return age >= REFUSED_RETRY_SECONDS
    if status['status'] == 'deferred':
        return age >= DEFER_RETRY_SECONDS
    if status['status'] == 'failed':
        wait = (RETRY_SECONDS * max(1, status['attempts'])
                if status['attempts'] < MAX_FAST_ATTEMPTS else REFUSED_RETRY_SECONDS)
        return age >= wait
    return age >= RETRY_SECONDS


SELECTOR_HEAL_CODE = 'learning_selector'
SELECTOR_REQUEST = 'selector-heal-request.json'
# A recall that misses local selection asks for a worker at most this often
# per install, whatever state the shared bundle status is in.
SELECTOR_REQUEST_SECONDS = 600


def _selector_request_due(state, now=None):
    """Record this selector-heal request if the per-install floor has passed.

    The check and the record happen under one non-blocking lock, so two
    concurrent hooks never both pass; a busy lock means another hook is
    recording a request right now.
    """
    now = _now() if now is None else now
    state = Path(state)
    try:
        fd = os.open(state / (SELECTOR_REQUEST + '.lock'),
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        path = state / SELECTOR_REQUEST
        try:
            last = json.loads(path.read_text('utf-8')).get('requestedNs')
        except (OSError, ValueError, AttributeError):
            last = None
        if type(last) is int and 0 <= now - last < SELECTOR_REQUEST_SECONDS * 10**9:
            return False
        tmp = state / ('.' + SELECTOR_REQUEST + '.' + str(os.getpid()))
        with open(tmp, 'w', encoding='utf-8') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump({'requestedNs': now}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        return True
    finally:
        os.close(fd)


def worker_holds(state):
    """True while a worker holds this install's upgrade lock (it is alive).

    The worker writes ``running`` only under that lock and a dead process
    releases it, so a ``running`` status without a holder is a stopped run.
    """
    try:
        fd = os.open(Path(state) / LOCK, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def request(project_root, journal_path, code='', *, spawn=None, now=None):
    """Hook side: never waits; starts at most one detached worker.

    A ``running`` status whose worker no longer holds the upgrade lock (it
    died, for example inside a state move) is due at once.
    """
    state = Path(journal_path).parent
    root = str(executing_root())
    if code == SELECTOR_HEAL_CODE and not _selector_request_due(state, now):
        return {'status': 'throttled', 'reason': SELECTOR_HEAL_CODE}
    status = read_status(state)
    if (status is not None and status['status'] == 'running' and
            status['executingRoot'] == root and not worker_holds(state)):
        status = None
    if not _due(status, root, _now() if now is None else now):
        return {'status': status['status'], 'reason': status.get('reason', '')}
    if spawn is None:
        if _fixture():
            return {'status': 'queued', 'reason': 'fixture_no_spawn'}
        spawn = _spawn
    spawn(project_root)
    return {'status': 'queued', 'reason': code}


def move_notice(project_root, journal_path, *, request_move=True, spawn=None, now=None):
    """SessionStart line while this install's state moves to the MemVane base.

    None once the install lives under ``~/.local/state/memvane`` (or stays
    where it is for good: another volume). Requests the worker when due.
    """
    import laya_state_move
    state = Path(journal_path).parent
    try:
        pointer = laya_state_move.read_pointer(project_root)
    except Exception:
        pointer = None
    if pointer is None and not laya_state_move.legacy_root(state):
        return None
    status = read_status(state) if state.is_dir() else None
    if (status is not None and status['status'] == 'refused' and
            status.get('reason') in MOVE_SILENT):
        return None
    if request_move:
        try:
            status = request(project_root, journal_path, 'state_root_move',
                             spawn=spawn, now=now)
        except Exception:
            pass
    head = '[memvane] index state moves from ~/.local/state/laya to ~/.local/state/memvane'
    state_name = status.get('status') if isinstance(status, dict) else None
    reason = status.get('reason') if isinstance(status, dict) else ''
    reason = reason if isinstance(reason, str) and reason else 'unknown'
    if pointer is not None or state_name in (None, 'queued', 'running', 'complete',
                                             'current'):
        return head + ' in the background; search keeps serving during the move.'
    if state_name == 'deferred':
        return (head + '; the move waits (' + reason + ') and a later session '
                'retries. Search keeps serving from the current location.')
    if state_name == 'refused':
        return (head + '; the move needs review (' + reason + '). Search keeps '
                'serving from the current location.')
    return (head + '; the move failed (' + reason + ') and a later session '
            'retries. Search keeps serving from the current location.')


def notice_line(state, code):
    """One SessionStart line naming the real reason; ASCII, no paths."""
    status = state.get('status') if isinstance(state, dict) else None
    reason = state.get('reason') if isinstance(state, dict) else ''
    reason = reason if isinstance(reason, str) and reason else code
    head = '[memvane] plugin bundle changed (' + code + '); '
    if status in ('queued', 'running', None):
        return (head + 'search serves the last published index read-only and '
                'sync waits while the index moves to this bundle.')
    if status == 'refused':
        return (head + 'automatic index upgrade needs review (' + reason + '); '
                'search stays read-only on the last published index. Ask the '
                'setup skill to upgrade or rebuild the memvane-db index.')
    if status == 'complete':
        return (head + 'index upgrade finished; the next session uses it.')
    if status == 'deferred' and reason == 'legacy_hermes_copy_present':
        import memvane_paths
        return (head + 'index upgrade waits while the Hermes plugin ' +
                memvane_paths.LEGACY_PLUGIN_ID + ' is installed (it cannot read the '
                'new route and would block edits); remove it from Hermes and a later '
                'session moves the index. Search serves the last published index '
                'read-only.')
    if status == 'deferred':
        return (head + 'index upgrade waits for a busy lane (' + reason + '); '
                'search serves the last published index read-only and a later '
                'session retries shortly.')
    return (head + 'automatic index upgrade failed (' + reason + '); search '
            'stays read-only on the last published index and a later session '
            'retries.')


def request_for_project(project_root):
    """Legacy-selected project (inactive or bound build): request if drifted."""
    import laya_initial_build as build
    from laya_install_journal import inspect_journal
    record = build.lookup(str(project_root))
    if record is None or record['phase'] == 'retired':
        return None
    journal = inspect_journal(record['identity']['journalPath'])
    identity = journal.get('selected') or journal.get('pending')
    if not isinstance(identity, dict) or identity.get('pluginRoot') == str(executing_root()):
        return None
    return request(str(project_root), record['identity']['journalPath'])


# --------------------------------------------------------------- source rule

def host_layout(root, home=None):
    """(host, marketplace clone) for an installed host plugin cache root."""
    home = Path(home or Path.home())
    root = Path(root)
    for host, cache, markets in (
            ('claude', home / '.claude' / 'plugins' / 'cache',
             home / '.claude' / 'plugins' / 'marketplaces'),
            ('codex', home / '.codex' / 'plugins' / 'cache',
             home / '.codex' / '.tmp' / 'marketplaces')):
        try:
            parts = root.relative_to(cache).parts
        except ValueError:
            continue
        if len(parts) == 3:
            return host, markets / parts[0]
    _refuse('host_layout_unknown')


def _git(root, *args):
    from laya_product_bundle import GIT_PINNED, git_pinned_env
    done = subprocess.run((*GIT_PINNED, '-C', str(root), *args), stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=15, check=False,
                          env=git_pinned_env())
    return done.returncode, done.stdout.decode('utf-8', 'replace').strip()


def installed_commit(root, home=None):
    """The host's own record of the commit it installed, when it keeps one."""
    root = Path(root)
    if (root / '.git').exists():
        code, out = _git(root, 'rev-parse', 'HEAD')
        return out if code == 0 and out else None
    record = Path(home or Path.home()) / '.claude' / 'plugins' / 'installed_plugins.json'
    try:
        plugins = json.loads(record.read_text(encoding='utf-8')).get('plugins', {})
    except (OSError, ValueError, AttributeError):
        return None
    for rows in plugins.values() if isinstance(plugins, dict) else ():
        for row in rows if isinstance(rows, list) else ():
            if (isinstance(row, dict) and row.get('installPath') == str(root) and
                    isinstance(row.get('gitCommitSha'), str)):
                return row['gitCommitSha']
    return None


def select_source(root, home=None):
    """The marketplace clone, only at the installed commit with clean tracked files."""
    _host, source = host_layout(root, home)
    if not (source / '.git').exists() or source.is_symlink():
        _refuse('marketplace_source_unavailable')
    code, head = _git(source, 'rev-parse', 'HEAD')
    top_code, top = _git(source, 'rev-parse', '--show-toplevel')
    if code != 0 or top_code != 0 or Path(top).resolve() != source.resolve():
        _refuse('marketplace_source_unavailable')
    wanted = installed_commit(root, home)
    if wanted is not None and wanted != head:
        _refuse('marketplace_source_commit_mismatch')
    # Untracked files are outside the exact committed file set.
    diff, _ = _git(source, 'diff', '--quiet', 'HEAD', '--')
    if diff != 0:
        _refuse('marketplace_source_dirty')
    return source.resolve(), head


_SEMVER = re.compile(r'([0-9]+)\.([0-9]+)\.([0-9]+)'
                     r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?\Z')


def semver_key(version):
    """Sortable SemVer 2.0.0 precedence key, or None for a non-version.

    Build metadata (``+...``) is ignored. A pre-release sorts before its
    release; its dot-separated identifiers compare numerically when numeric,
    lexically otherwise, numeric before alphanumeric, and a shorter equal
    prefix first (``1.0.0-dev.2 < 1.0.0-dev.10 < 1.0.0``).
    """
    match = _SEMVER.fullmatch(version) if isinstance(version, str) else None
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        tail = (1,)
    else:
        tail = (0, *((0, int(part), '') if part.isdigit() else (1, 0, part)
                     for part in pre.split('.')))
    return (int(major), int(minor), int(patch), tail)


def bundle_version(root):
    """Version of an installed bundle: its ``package.json``, else the cache
    directory name (``<cache>/<marketplace>/<plugin>/<version>``), which
    survives the host removing the bundle's files."""
    root = Path(root)
    try:
        value = json.loads((root / 'package.json').read_text('utf-8')).get('version')
        if semver_key(value) is not None:
            return value
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return root.name if semver_key(root.name) is not None else None


def _not_older(source, bound_commit, commit, *, bound_root=None, root=None):
    """Never move an install back to an older bundle (no host ping-pong).

    Git proves the direction when the source clone knows both commits: the
    executing commit an ancestor of the bound one refuses, the bound commit
    an ancestor of the executing one is a forward move. Diverged histories,
    and a clone that does not know the bound commit (another plugin id's
    marketplace, a shallow clone), need a strictly newer bundle version
    (semver of ``package.json`` or the cache directory name). Anything else
    refuses, so two bundles can never take an install from each other.
    """
    if bound_commit == commit:
        return
    backward, _ = _git(source, 'merge-base', '--is-ancestor', commit, bound_commit)
    if backward == 0:
        _refuse('older_host_bundle')
    forward, _ = _git(source, 'merge-base', '--is-ancestor', bound_commit, commit)
    if backward == 1 and forward == 0:
        return
    bound = semver_key(bundle_version(bound_root)) if bound_root is not None else None
    current = semver_key(bundle_version(root)) if root is not None else None
    if bound is None or current is None or current <= bound:
        _refuse('older_host_bundle')


# ------------------------------------------------------- provider equivalence

def provider_equivalent(old_attestation, review, new_root):
    """Same provider content; paths may move, bundle files stay in ``new_root``."""
    from laya_provider_reproof import reattest
    approved = review.get('reviewedProvider') if isinstance(review, dict) else None
    reattested = reattest(old_attestation, approved)
    if reattested is None:
        return False
    manifest = reattested['providerManifest']
    root = Path(new_root)
    return (manifest.get('providerPath') ==
            str(root / str(manifest.get('providerRelativePath'))) and
            manifest.get('workerPath') == str(root / 'core' / 'laya_local_embed_worker.mjs'))


# ---------------------------------------------------------- install discovery

def installs(project_root):
    """State roots bound to this project by its own route or build pointer."""
    import laya_initial_build as build
    from laya_index_host_route import select_route
    found = []
    decision = select_route(str(project_root), action='update')
    if decision.get('state') == 'candidate' and decision.get('projectRoot') == str(project_root):
        found.append(Path(decision['journalPath']).parent)
    try:
        record = build.lookup(str(project_root))
    except Exception:
        record = None
    if record is not None:
        state = Path(record['identity']['stateRoot'])
        if state not in found:
            found.append(state)
    return found


def classify(state):
    """('active'|'bound'|'inactive'|'retired', bound identity) for one install."""
    import laya_initial_build as build
    from laya_install_journal import inspect_journal
    journal = inspect_journal(Path(state) / 'install-journal.sqlite')
    if journal['state'] in ('active', 'restored_old', 'pending'):
        identity = journal['selected'] or journal['previous']
        return 'active', identity
    record = build.read(state)
    if journal['state'] != 'candidate_ready' or record is None:
        return 'retired', None
    if record['phase'] == 'retired':
        return 'retired', None
    if record['phase'] == 'bound':
        return 'bound', journal['pending']
    return 'inactive', journal['pending']


# ------------------------------------------------------------------- worker

def _read_attestation(state):
    from laya_install_journal import PRODUCTION_ATTESTATION
    from laya_product_install import _read_private
    return json.loads(_read_private(Path(state) / PRODUCTION_ATTESTATION, 1024 * 1024))


def _seal_companion(identity, root):
    """Same exact bundle in another host cache: seal it, change no route."""
    from laya_product_bundle import inspect_full_plugin_candidate, seal_companion_host_copy
    owner = inspect_full_plugin_candidate(identity['pluginRoot'])
    if any(owner[key] != identity[key] for key in
           ('sourceCommit', 'bundleManifestSha256', 'vectorProfile')):
        _refuse('companion_owner_changed')
    seal_companion_host_copy(owner['pluginRoot'], root)
    return {'status': 'companion_sealed'}


def _mark_sealed_for_rescan(state):
    """A sealed build becomes ``building`` without waking the old worker."""
    import laya_initial_build as build
    from contextlib import closing
    from laya_index_host_route import wiki_index_roots
    from laya_product_install import _install_lock
    from laya_source_catalog import LayaSourceCatalog
    with _install_lock(state), build.owner(state) as owned:
        if not owned:
            raise RuntimeError('initial_build_busy')
        record = build.read(state)
        if record is None or record['phase'] != 'sealed':
            return
        identity = record['identity']
        with closing(LayaSourceCatalog(identity['catalogDb'],
                                       wiki_index_roots(identity['collections']),
                                       identity['scopeId'], wiki_only=True)) as catalog:
            build.request_freshness(record, catalog)


def _ensure_embed_runtime(attestation):
    """The Laya embedding runtime holding this install's attested bytes.

    A first upgrade after the runtime moved out of the QMD package copies the
    attested node, node-llama-cpp tree, native closure and model (each sha256
    checked) into the Laya runtime; review then names the copies and the
    content-equal re-attestation keeps the vector profile.
    """
    import laya_embed_runtime
    try:
        return laya_embed_runtime.ensure_for_attestation(attestation)
    except laya_embed_runtime.RuntimeUnavailable as exc:
        if str(exc) == 'laya_embed_runtime_busy':
            raise RuntimeError('embed_runtime_busy') from exc
        _refuse(str(exc))


def _require_pinned_runtime(attestation):
    """Refuse before any copy when the old runtime's files cannot be proven.

    Equivalence needs the full-file digest of what the old worker ran: sealed
    in the attestation or read from the Laya generation it names. Copying an
    unpinned tree into a generation would not make it equivalent.
    """
    import laya_embed_runtime
    try:
        pinned = laya_embed_runtime.attested_files(attestation)
    except laya_embed_runtime.RuntimeUnavailable as exc:
        _refuse(str(exc))
    if pinned is None:
        _refuse('provider_changed_rebuild_required')


def _same_root_readopt(kind, identity, root):
    """Whether a selected install bound to ``root`` must be re-adopted there.

    A host can re-extract the same bundle into the same cache directory (new
    inodes and ctimes, same bytes). The journal then pins an ownership digest
    the live files no longer have and every proof fails with
    ``installed_bundle_changed``. Same commit and same content re-adopt
    through the normal review and switch; same commit with other content
    refuses. A different commit at this root (a host reusing one version
    directory) takes the normal upgrade path, whose adoption checks it.
    Bound and inactive builds keep their bundle here.
    """
    if kind != 'active':
        return False
    from laya_product_bundle import BundleEvidenceError, inspect_full_plugin_candidate
    try:
        live = inspect_full_plugin_candidate(str(root))
    except BundleEvidenceError:
        # Unsealed or changed in place: adoption re-proves it against the
        # marketplace commit or refuses.
        return True
    if live['sourceCommit'] != identity.get('sourceCommit'):
        return True
    if (live['bundleManifestSha256'] != identity.get('bundleManifestSha256') or
            live['vectorProfile'] != identity.get('vectorProfile')):
        _refuse('installed_bundle_content_changed')
    return live['ownershipManifestSha256'] != identity.get('ownershipManifestSha256')


def upgrade_install(project_root, state, root, home=None):
    """Move one install to the executing bundle; returns a result row."""
    kind, identity = classify(state)
    if kind == 'retired' or identity is None:
        return {'status': 'current', 'kind': kind}
    same_root = identity['pluginRoot'] == str(root)
    if same_root and not _same_root_readopt(kind, identity, root):
        return {'status': 'current', 'kind': kind}
    from laya_ledger_common import QMD_ORIGIN_DETACH_REQUIRED, qmd_ledger_present
    if qmd_ledger_present(state):
        _refuse(QMD_ORIGIN_DETACH_REQUIRED)
    commit = installed_commit(root, home)
    if (not same_root and commit is not None and commit == identity['sourceCommit'] and
            kind != 'inactive'):
        try:
            return {**_seal_companion(identity, root), 'kind': kind}
        except UpgradeRefused:
            raise
        except Exception:
            pass  # The owner copy is gone: move the install to this root.
    if kind == 'active':
        # Defer before any review work: the switch would publish a route the
        # earlier plugin id's Hermes copy cannot read.
        from laya_journal_v4 import refuse_while_legacy_hermes
        refuse_while_legacy_hermes(home)
    source, commit = select_source(root, home)
    _not_older(source, identity['sourceCommit'], commit,
               bound_root=identity['pluginRoot'], root=root)
    old = _read_attestation(state)
    manifest = old.get('providerManifest') or {}
    _require_pinned_runtime(old)
    _ensure_embed_runtime(old)
    from laya_product_install import review_local_provider
    review = review_local_provider(source_root=str(source), plugin_root=str(root),
                                   state_root=str(state), commit_id=commit,
                                   dimension=manifest.get('dimension'))
    if not provider_equivalent(old, review, root):
        _refuse('provider_changed_rebuild_required')
    options = dict(project_root=str(project_root), state_root=str(state),
                   source_root=str(source), plugin_root=str(root), commit_id=commit,
                   accepted_provider_sha256=review['proposalSha256'],
                   model_path=None, dimension=manifest.get('dimension'))
    if kind == 'active':
        import laya_active_upgrade
        result = laya_active_upgrade.upgrade(**options)
        from laya_product_install import _request_build_wake
        result['syncWake'] = _request_build_wake(str(project_root))
    elif kind == 'bound':
        import laya_bound_upgrade
        result = laya_bound_upgrade.upgrade(**options)
    else:
        _mark_sealed_for_rescan(state)
        import laya_inactive_upgrade
        result = laya_inactive_upgrade.upgrade(**{**options,
                                                  'state_root': Path(state)})
    return {**result, 'kind': kind}


@contextmanager
def _state_lock(state):
    fd = os.open(Path(state) / LOCK, os.O_RDWR | os.O_CREAT |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('bundle_upgrade_lock_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _reason(exc):
    text = str(exc)
    return text if text and len(text) <= 80 and text.replace('_', '').isalnum() \
        else type(exc).__name__


_sleep = time.sleep


def _upgrade_with_backoff(project, state, root, home):
    """``upgrade_install``, waiting out a busy lane a few times in-process."""
    for wait in (*BUSY_BACKOFF_SECONDS, None):
        try:
            return upgrade_install(project, state, root, home)
        except Exception as exc:
            if wait is None or _reason(exc) not in DEFERRALS or _reason(exc) in NO_BACKOFF:
                raise
            _event(state, 'bundle.upgrade', 'busy_wait', _reason(exc), retry_in=wait)
            _sleep(wait)


def _convert_journal(project, state, root, home):
    """v3 -> v4 journal conversion once every host copy reads v4.

    Runs after the bundle step, under the same state lock. A conversion that
    waits or fails never fails the bundle step; a later session retries.
    """
    try:
        import laya_journal_v4
        converted = laya_journal_v4.convert_install(state, project, root=root, home=home)
    except Exception as exc:
        converted = {'status': 'journal_v4_failed', 'reason': _reason(exc)}
    _event(state, 'journal.v4', converted.get('status'), converted.get('reason'))
    return {'journalV4': converted.get('status')}


def _host(root, home):
    try:
        return host_layout(root, home)[0]
    except UpgradeRefused:
        return 'unknown'


def _move_state(project, state, home):
    """Finish or start this install's move to the MemVane state base.

    (state root now, move result or None). A busy lane is waited out a few
    times in-process like the bundle step.
    """
    import laya_state_move
    if not (laya_state_move.intent_present(state) or
            laya_state_move.legacy_root(state, home)):
        return state, None
    for wait in (*BUSY_BACKOFF_SECONDS, None):
        try:
            return laya_state_move.move(project, state, home=home)
        except Exception as exc:
            if wait is None or _reason(exc) not in MOVE_DEFERRALS:
                raise
            _event(state, 'state.move', 'busy_wait', _reason(exc), retry_in=wait)
            _sleep(wait)


def _move_outcome(exc):
    """(status, reason) the status file records for a move that stopped."""
    reason = _reason(exc)
    if reason in MOVE_DEFERRALS:
        return 'deferred', reason
    if reason in MOVE_REFUSALS:
        return 'refused', reason
    return 'failed', reason


def _live_state(state, home):
    """The install directory now: a move may have renamed it mid-run."""
    import laya_state_move
    state = Path(state)
    if not state.exists() and laya_state_move.legacy_root(state, home):
        target = laya_state_move.target_root(state, home)
        if target.is_dir():
            return target
    return state


def _project_states(project, home):
    """A move already started comes first; then the route's and the pointer's roots."""
    import laya_state_move
    found = []
    for state in (*laya_state_move.pending_roots(project, home), *installs(project)):
        if state not in found:
            found.append(state)
    return found


def run(project_root, *, root=None, home=None):
    """Worker: upgrade every install of one project, one state root at a time.

    An install still under the pre-rename state base then moves to the
    MemVane base (``laya_state_move``); a move stopped after its rename is
    finished before anything else runs on that install.
    """
    project = Path(project_root).resolve(strict=True)
    root = Path(root or executing_root())
    results = []
    for state in _project_states(project, home):
        with _state_lock(state) as owned:
            if not owned:
                results.append({'stateRoot': str(state), 'status': 'busy'})
                continue
            prior = read_status(state)
            attempts = (prior['attempts'] if prior is not None and
                        prior['executingRoot'] == str(root) else 0) + 1
            _write_status(state, status='running', executingRoot=str(root),
                          attempts=attempts, reason='')
            started = time.monotonic()
            from_root, from_commit = _bound_bundle(state)
            moves = {'from': from_root, 'fromCommit': from_commit, 'to': root.name}
            _event(state, 'bundle.upgrade', 'started', hostLayout=_host(root, home),
                   attempt=attempts, **moves)
            try:
                import laya_state_move
                resumed = None
                if laya_state_move.intent_present(state):
                    state, resumed = _move_state(project, state, home)
                result = _upgrade_with_backoff(project, state, root, home)
                result = {**result, **_convert_journal(project, state, root, home)}
                status = ('current' if result.get('status') == 'current'
                          else 'complete')
                reason = str(result.get('status', ''))
                try:
                    state, moved = _move_state(project, state, home)
                    moved = moved or resumed
                except Exception as exc:
                    state = _live_state(state, home)
                    status, reason = _move_outcome(exc)
                    result = {**result, 'status': status, 'reason': reason,
                              'stateMove': reason}
                    _event(state, 'state.move', status, reason)
                else:
                    if moved is not None:
                        result = {**result, 'stateMove': moved['status']}
                        _event(state, 'state.move', moved['status'],
                               stateRoot=moved.get('stateRoot'))
                        if moved['status'] == 'state_move_complete':
                            status, reason = 'complete', moved['status']
                            result = {**result, 'status': moved['status']}
                _write_status(state, status=status, executingRoot=str(root),
                              attempts=0 if status in ('current', 'complete') else attempts,
                              reason=reason)
            except UpgradeRefused as exc:
                state = _live_state(state, home)
                result = {'status': 'refused', 'reason': str(exc)}
                _write_status(state, status='refused', executingRoot=str(root),
                              attempts=attempts, reason=str(exc))
            except Exception as exc:
                state = _live_state(state, home)
                reason = _reason(exc)
                if reason in DEFERRALS:
                    # Keep the prior attempt count: contention is not a failure.
                    result = {'status': 'deferred', 'reason': reason}
                    _write_status(state, status='deferred', executingRoot=str(root),
                                  attempts=attempts - 1, reason=reason)
                else:
                    result = {'status': 'failed', 'reason': reason}
                    _write_status(state,
                                  status='refused' if reason in REFUSALS else 'failed',
                                  executingRoot=str(root), attempts=attempts,
                                  reason=reason)
            _event(state, 'bundle.upgrade', result.get('status'), result.get('reason'),
                   ms=(time.monotonic() - started) * 1000, kind=result.get('kind'), **moves)
            results.append({'stateRoot': str(state), **{
                key: value for key, value in result.items()
                if key in ('status', 'reason', 'kind')}})
    _collect_shared(home)
    events = next((Path(row['stateRoot']) for row in results
                   if Path(row['stateRoot']).is_dir()), None)
    learning = _learning_upkeep(project, home, events)
    if learning:
        results.append({'stateRoot': str(events) if events else '', **learning})
    return results


def _learning_event(state, event, outcome, reason=None, **fields):
    try:
        import event_log
        event_log.emit(event, outcome=outcome, reason=reason or None,
                       state_root=Path(state) if state is not None else None,
                       host='worker', **fields)
    except Exception:
        pass


def _learning_upkeep(project, home, state):
    """Move the learning state (once per account), then heal this project's selector.

    Never raises; a deferral or refusal is an event and a later run retries
    once the move's per-user backoff has passed. One attempt is bounded
    (``state_move.BUDGET_SECONDS``); while the move is pending the heal does
    not run.
    """
    out = {}
    try:
        from context_learning import state_move
        moved = state_move.move(home, events=state)
        if moved['status'] == 'learning_state_moved':
            out['learningState'] = moved['status']
            _learning_event(state, 'learning.state', 'moved',
                            rebased=moved.get('checkpointsRebased'),
                            residentRestart=moved.get('residentRestart'))
            for stray in moved.get('strays') or ():
                # A tree an earlier bundle recreated at the old base, set aside.
                _learning_event(state, 'learning.state', 'stray_set_aside', path=stray)
    except Exception as exc:
        out['learningState'] = _reason(exc)
        _learning_event(state, 'learning.state', 'deferred', _reason(exc),
                        retryIn=getattr(exc, 'retry_in', None),
                        residentRestart=getattr(exc, 'restart', None))
        return out
    try:
        from context_learning import selector_heal
        target = selector_heal.namespace(project, home)
        if not (target / selector_heal.POLICY).is_file():
            return out
        _learning_event(state, 'learning.prepare', 'started')
        started = time.monotonic()
        healed = selector_heal.heal(project, home=home)
        out['selector'] = healed['status']
        _learning_event(state, 'learning.prepare', healed['status'],
                        rebound=healed.get('rebound'),
                        ms=(time.monotonic() - started) * 1000)
    except Exception as exc:
        out['selector'] = _reason(exc)
        _learning_event(state, 'learning.prepare', 'failed', _reason(exc))
    return out


def _collect_shared(home):
    """Retire pre-rename shared trees once nothing references them; never raises."""
    try:
        import laya_state_move
        if laya_state_move._legacy_installs(home):
            return None
        result = laya_state_move.collect_shared(home)
    except Exception as exc:
        result = {'status': 'failed', 'reason': _reason(exc)}
    try:
        import event_log
        event_log.emit('state.shared', outcome=result.get('status'),
                       reason=result.get('reason'), host='worker',
                       completed=result.get('completed'))
    except Exception:
        pass
    return result


def move_all(*, root=None, home=None):
    """Owner command: run the worker for every project with a pre-rename install."""
    import laya_initial_build as build
    import laya_state_move
    projects, rows = [], []
    for state in laya_state_move._legacy_installs(home):
        try:
            record = build.read(state)
        except Exception as exc:
            rows.append({'stateRoot': str(state), 'status': 'unreadable',
                         'reason': _reason(exc)})
            continue
        project = record['identity']['projectRoot'] if record else None
        if not project or not Path(project).is_dir():
            rows.append({'stateRoot': str(state), 'status': 'project_missing'})
        elif project not in projects:
            projects.append(project)
    # A move that stopped after its rename lives under the MemVane base now.
    for pending in laya_state_move.pending_moves(home):
        project = pending['projectRoot']
        if not project or not Path(project).is_dir():
            rows.append({'stateRoot': pending['stateRoot'], 'status': 'project_missing'})
        elif project not in projects:
            projects.append(project)
    # Projects whose only reason to run is a prepared selector.
    try:
        from context_learning.selector_heal import selector_projects
        for project in selector_projects(home):
            if project not in projects and Path(project).is_dir():
                projects.append(project)
    except Exception:
        pass
    for project in projects:
        try:
            for row in run(project, root=root, home=home):
                rows.append({'projectRoot': project, **row})
        except Exception as exc:
            rows.append({'projectRoot': project, 'status': 'failed', 'reason': _reason(exc)})
    shared = _collect_shared(home)
    left = [str(path) for path in laya_state_move._legacy_installs(home)]
    pending = laya_state_move.pending_moves(home)
    return {'status': 'moved' if not left and not pending else 'partial',
            'installs': rows, 'legacyInstalls': left, 'pendingMoves': pending,
            'shared': shared}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != 'run':
        return 2
    if _fixture():
        return 0

    def expired(_signum, _frame):
        raise TimeoutError('bundle_upgrade_deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(DEADLINE_SECONDS)
    try:
        run(args[1])
    except Exception:
        return 1
    finally:
        signal.alarm(0)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
