"""Versioned local execution approval, separate from synthetic content consent.

The historical shared policy and its capture-time approval hashes stay immutable.
An owner-private authorization may bind that exact policy to one reviewed,
installed multilingual adapter. It does not enroll another project or grant
permission to send any content; synthetic_scope.policy_for still gates gold.
"""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time

from .store import canonical

NAME = 'runtime-execution-authorization.json'
SCHEMA = 'qmd-runtime-execution-authorization-v3'
SCOPE = 'single-synthetic-story'
OPERATIONS = ['gold', 'local_train', 'quality_evaluate']
COHORT_SCHEMA = 'qmd-synthetic-edit-cohort-v1'
MAX_LIFETIME_MS = 14 * 24 * 60 * 60 * 1000
REVIEWED_PLUGIN_VERSION = '0.36.0'
UPDATES_DIR = 'runtime-authorization-updates'
UPDATE_SCHEMA = 'qmd-runtime-authorization-update-v1'


def _approved_adapter_paths():
    """The two existing host installations, never a caller-selected argv."""
    home = Path.home()
    suffix = Path('qmd-auto-context') / REVIEWED_PLUGIN_VERSION / 'core' / (
        'context_learning/laya_adapter.py')
    return {
        'codex': home / '.codex/plugins/cache/qmd-shared-v21-local' / suffix,
        'claude': home / '.claude/plugins/cache/qmd-shared-v44-local' / suffix,
    }


def _installed_adapters(adapter, reviewed_sha):
    approved = _approved_adapter_paths()
    if set(approved) != {'codex', 'claude'}:
        raise ValueError('runtime_authorization_adapter_invalid')
    requested = _reviewed_adapter(adapter, reviewed_sha)
    installed = []
    for host in ('claude', 'codex'):
        path = _reviewed_adapter(approved[host], reviewed_sha)
        manifest = path.parents[2] / ('.claude-plugin' if host == 'claude'
                                      else '.codex-plugin') / 'plugin.json'
        try:
            if (manifest.is_symlink() or manifest.parent.is_symlink()
                    or not manifest.is_file()):
                raise ValueError('runtime_authorization_adapter_invalid')
            info = manifest.stat()
            if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1):
                raise ValueError('runtime_authorization_adapter_invalid')
            metadata = json.loads(manifest.read_text(encoding='utf8'))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError('runtime_authorization_adapter_invalid') from exc
        if (metadata.get('name') != 'qmd-auto-context' or
                metadata.get('version') != REVIEWED_PLUGIN_VERSION):
            raise ValueError('runtime_authorization_adapter_invalid')
        installed.append({'host': host, 'path': str(path),
                          'sha256': reviewed_sha})
    if (requested not in {Path(row['path']) for row in installed}
            and requested not in _claude_executed_adapters()):
        raise ValueError('runtime_authorization_adapter_invalid')
    return installed


def _claude_executed_adapters():
    """The adapter in the folder Claude actually runs, if it is the reviewed one.

    Claude runs a directory-marketplace plugin in place, so the code asking
    for a grant lives outside the cache copy that the grant names. That folder
    counts only when Claude's own install records name it
    (history_activation._claude_load_roots, which also requires it to equal
    the cache copy byte for byte) and it carries the reviewed version. The
    grant keeps naming the cache copies, so this widens no recorded scope.
    """
    from .history_activation import _claude_load_roots
    try:
        roots = _claude_load_roots(Path.home().resolve())
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return set()
    adapters = set()
    for root in roots:
        try:
            metadata = json.loads((Path(root) / '.claude-plugin/plugin.json')
                                  .read_text(encoding='utf8'))
        except (OSError, UnicodeError, ValueError):
            continue
        if (isinstance(metadata, dict)
                and metadata.get('name') == 'qmd-auto-context'
                and metadata.get('version') == REVIEWED_PLUGIN_VERSION):
            adapters.add(Path(root) / 'core/context_learning/laya_adapter.py')
    return adapters


def _cohort(value):
    """An edit-path split is independent of historical case.split values."""
    if not isinstance(value, dict) or set(value) != {
            'schema', 'id', 'notBeforeUnixMs', 'trainEditPrefixes',
            'validationEditPrefixes', 'minTrainCases',
            'minQualityTrainCases', 'minTrainFamilies',
            'minValidationCases', 'minValidationFamilies',
            'minEmptyValidation', 'baselineRawPaths'}:
        raise ValueError('runtime_authorization_cohort_invalid')
    if (value['schema'] != COHORT_SCHEMA or not isinstance(value['id'], str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}', value['id'])
            or type(value['notBeforeUnixMs']) is not int
            or value['notBeforeUnixMs'] <= 0
            or value['minTrainCases'] != 5
            or value['minQualityTrainCases'] != 100
            or value['minTrainFamilies'] != 5
            or value['minValidationCases'] != 50
            or value['minValidationFamilies'] != 5
            or value['minEmptyValidation'] != 10):
        raise ValueError('runtime_authorization_cohort_invalid')
    prefixes = []
    for name in ('trainEditPrefixes', 'validationEditPrefixes'):
        group = value[name]
        if (not isinstance(group, list) or len(group) != 5
                or len(set(group)) != len(group)
                or any(not isinstance(prefix, str) or not re.fullmatch(
                    r'(?:01_Settings|02_Characters|03_Plot)/[A-Za-z0-9_/-]+/',
                    prefix) or '//' in prefix or any(part in ('.', '..')
                    for part in prefix.split('/')) for prefix in group)):
            raise ValueError('runtime_authorization_cohort_invalid')
        prefixes.extend(group)
    if len(set(prefixes)) != len(prefixes) or any(
            left.startswith(right) or right.startswith(left)
            for i, left in enumerate(prefixes) for right in prefixes[i + 1:]):
        raise ValueError('runtime_authorization_cohort_invalid')
    baseline = value['baselineRawPaths']
    if (not isinstance(baseline, list) or len(baseline) > 5000
            or any(not isinstance(path, str) for path in baseline)
            or baseline != sorted(set(baseline))
            or any(not any(path.startswith(prefix) for prefix in prefixes)
                   for path in baseline)):
        raise ValueError('runtime_authorization_cohort_invalid')
    return value


def baseline_raw_paths(story, cohort, root):
    """Freeze existing paths and durable older provenance at activation."""
    _cohort(cohort)
    story = Path(story).resolve(strict=True)
    prefixes = cohort['trainEditPrefixes'] + cohort['validationEditPrefixes']
    found = set()
    for prefix in prefixes:
        folder = story
        for part in Path(prefix).parts:
            folder = folder / part
            if folder.is_symlink():
                raise ValueError('runtime_authorization_baseline_unsafe')
        if not folder.exists() and not folder.is_symlink():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('runtime_authorization_baseline_unsafe')
        for parent, directories, files in os.walk(folder, followlinks=False):
            for name in directories + files:
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError('runtime_authorization_baseline_unsafe')
                if path.is_file():
                    found.add(path.relative_to(story).as_posix())
                    if len(found) > 5000:
                        raise ValueError('runtime_authorization_baseline_too_large')
    db_path = Path(root) / 'shared-learning.sqlite3'
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError('runtime_authorization_cohort_ledger_missing')
    from .synthetic_experiment import project_for
    owner = str(project_for(Path(root)).resolve(strict=True))
    cutoff = cohort['notBeforeUnixMs'] / 1000
    from .history_activation import read_database
    with read_database(db_path, timeout=5) as db:
        db.execute('PRAGMA query_only=ON')
        tickets = db.execute(
            'SELECT relative_path FROM edit_tickets WHERE project_root=? '
            'AND story_root=? AND created_at<?',
            (owner, str(story), cutoff)).fetchall()
        registered = db.execute(
            'SELECT source_path FROM source_card_versions WHERE project_root=? '
            'AND story_root=? AND registered_at<?',
            (owner, str(story), cutoff)).fetchall()
        inventory = db.execute(
            'SELECT relative_path FROM synthetic_source_inventory WHERE '
            'recorded_at<?', (cutoff,)).fetchall()
        events = db.execute(
            'SELECT relative_path FROM synthetic_source_events WHERE '
            'recorded_at<?', (cutoff,)).fetchall()
        topical = db.execute(
            'SELECT source_snapshots FROM topical_worker_versions WHERE '
            'project_root=? AND story_root=? AND registered_at<?',
            (owner, str(story), cutoff)).fetchall()
    for (path,) in tickets + registered + inventory + events:
        if any(path.startswith(prefix) for prefix in prefixes):
            found.add(path)
    for (encoded,) in topical:
        try:
            source_paths = [item['revision']['path']
                            for item in json.loads(encoded)]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('runtime_authorization_baseline_invalid') from exc
        for path in source_paths:
            if not isinstance(path, str):
                raise ValueError('runtime_authorization_baseline_invalid')
            if any(path.startswith(prefix) for prefix in prefixes):
                found.add(path)
    if len(found) > 5000:
        raise ValueError('runtime_authorization_baseline_too_large')
    return sorted(found)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('runtime_authorization_unsafe_file')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise ValueError('runtime_authorization_unsafe_file')
    return path


def _reviewed_adapter(path, expected_sha):
    requested = Path(path)
    if (requested.is_symlink() or any(parent.is_symlink()
                                      for parent in requested.parents)):
        raise ValueError('runtime_authorization_adapter_invalid')
    try:
        info = requested.stat()
    except OSError as exc:
        raise ValueError('runtime_authorization_adapter_invalid') from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or _sha(requested) != expected_sha):
        raise ValueError('runtime_authorization_adapter_invalid')
    return requested.resolve(strict=True)


def proposal(root, project, policy, adapter, *, reviewed_adapter_sha256,
             cohort, expires_at_unix_ms):
    """Deterministic reviewable proposal; no file or pointer is changed."""
    root, project = Path(root).resolve(strict=True), Path(project).resolve(strict=True)
    adapters = _installed_adapters(adapter, reviewed_adapter_sha256)
    from .synthetic_experiment import shared_for, story_for
    from .synthetic_scope import NOVEL_ROOT
    if (project != NOVEL_ROOT.resolve(strict=True) or root != shared_for(project)
            or not story_for(project).is_dir()):
        raise ValueError('runtime_authorization_scope_invalid')
    from .laya_adapter import EDIT_INPUT_VIEW, _read_json
    argv = policy['trainerArgv']
    if (policy.get('modelInputView') != EDIT_INPUT_VIEW
            or not isinstance(argv, list) or len(argv) != 3
            or not all(isinstance(x, str) and Path(x).is_absolute() for x in argv)
            or not Path(argv[0]).is_file() or not Path(argv[2]).is_dir()):
        raise ValueError('runtime_authorization_adapter_invalid')
    model = _read_json(Path(argv[2]) / 'rl_agent_config.json')
    if (model.get('encoder') != 'jhu-clsp/mmBERT-base'
            or (model.get('max_len'), model.get('head_max_len')) != (1024, 256)):
        raise ValueError('runtime_authorization_model_view_invalid')
    from .synthetic_scope import policy_for
    consent = policy_for(root, project, for_inflight_verification=True)
    if (consent['receiver'], consent['purpose'], consent['model']) != (
            'codex-cli', 'selection-gold', policy['goldModel']):
        raise ValueError('runtime_authorization_consent_invalid')
    cohort = _cohort(cohort)
    if (type(expires_at_unix_ms) is not int
            or not cohort['notBeforeUnixMs'] < expires_at_unix_ms
            <= cohort['notBeforeUnixMs'] + MAX_LIFETIME_MS):
        raise ValueError('runtime_authorization_expiry_invalid')
    return {
        'schema': SCHEMA, 'scope': SCOPE, 'projectRoot': str(project),
        'storyRoot': str(story_for(project).resolve(strict=True)),
        'sharedPolicySha256': _sha(_private(root / 'shared-policy.json')),
        'contentConsentSha256': _sha(_private(root / 'synthetic-teacher-policy.json')),
        'adapterInstalls': adapters,
        'pythonPath': argv[0], 'modelDir': argv[2],
        'modelInputView': EDIT_INPUT_VIEW, 'goldReceiver': 'codex-cli',
        'goldModel': policy['goldModel'], 'operations': OPERATIONS,
        'cohort': cohort, 'expiresAtUnixMs': expires_at_unix_ms,
    }


def proposal_sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _adapter_rows(value, reviewed_sha):
    rows = value.get('adapterInstalls') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('runtime_authorization_stale_or_invalid')
    versions = set()
    for row, host, market in zip(rows, ('claude', 'codex'),
                                  ('qmd-shared-v44-local', 'qmd-shared-v21-local')):
        if (not isinstance(row, dict) or set(row) != {'host', 'path', 'sha256'}
                or row['host'] != host or row['sha256'] != reviewed_sha
                or not isinstance(row['path'], str)):
            raise ValueError('runtime_authorization_stale_or_invalid')
        base = Path.home() / ('.claude' if host == 'claude' else '.codex') / \
            'plugins/cache' / market / 'qmd-auto-context'
        path = Path(row['path'])
        version = path.parent.parent.parent.name
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', version)
                or path != base / version / 'core/context_learning/laya_adapter.py'):
            raise ValueError('runtime_authorization_stale_or_invalid')
        versions.add(version)
    if len(versions) != 1:
        raise ValueError('runtime_authorization_stale_or_invalid')


def _same_execution_scope(previous, current, reviewed_sha):
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or set(previous) != set(current)
            or {k: v for k, v in previous.items() if k != 'adapterInstalls'}
            != {k: v for k, v in current.items() if k != 'adapterInstalls'}):
        raise ValueError('runtime_authorization_scope_changed')
    _adapter_rows(previous, reviewed_sha)
    _adapter_rows(current, reviewed_sha)


def _chain(root, reviewed_sha):
    """Read an append-only chain. Missing ancestors, forks, and edits fail closed."""
    base_path = _private(Path(root) / NAME)
    try:
        current = json.loads(base_path.read_text(encoding='utf8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('runtime_authorization_invalid') from exc
    if not isinstance(current, dict) or current.get('schema') != SCHEMA:
        raise ValueError('runtime_authorization_stale_or_invalid')
    _adapter_rows(current, reviewed_sha)
    directory = Path(root) / UPDATES_DIR
    if not directory.exists() and not directory.is_symlink():
        return current
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('runtime_authorization_unsafe_file')
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('runtime_authorization_unsafe_file')
    records = {}
    paths = list(directory.iterdir())
    if len(paths) > 32:
        raise ValueError('runtime_authorization_stale_or_invalid')
    for path in paths:
        _private(path)
        try:
            record = json.loads(path.read_text(encoding='utf8'))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError('runtime_authorization_invalid') from exc
        if (not isinstance(record, dict) or set(record) !=
                {'schema', 'previousProposalSha256', 'proposal'}
                or record['schema'] != UPDATE_SCHEMA
                or not isinstance(record['proposal'], dict)
                or path.name != proposal_sha256(record['proposal']) + '.json'):
            raise ValueError('runtime_authorization_stale_or_invalid')
        previous_hash = record['previousProposalSha256']
        if (not isinstance(previous_hash, str) or
                not re.fullmatch(r'[0-9a-f]{64}', previous_hash)
                or previous_hash in records):
            raise ValueError('runtime_authorization_stale_or_invalid')
        records[previous_hash] = record['proposal']
    for _ in range(len(records)):
        old_hash = proposal_sha256(current)
        if old_hash not in records:
            break
        following = records.pop(old_hash)
        if proposal_sha256(following) == old_hash:
            raise ValueError('runtime_authorization_stale_or_invalid')
        _same_execution_scope(current, following, reviewed_sha)
        current = following
    if records:
        raise ValueError('runtime_authorization_stale_or_invalid')
    return current


def rebind(root, project, policy, adapter, previous_sha256,
           accepted_sha256, *, reviewed_adapter_sha256):
    """Append a same-scope adapter relocation receipt; keep the old grant intact."""
    root = Path(root).resolve(strict=True)
    lock_path = root / (UPDATES_DIR + '.lock')
    if lock_path.is_symlink():
        raise ValueError('runtime_authorization_unsafe_file')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('runtime_authorization_unsafe_file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = _chain(root, reviewed_adapter_sha256)
        old_hash = proposal_sha256(current)
        if old_hash != previous_sha256:
            raise ValueError('runtime_authorization_plan_changed')
        if time.time_ns() // 1_000_000 >= current['expiresAtUnixMs']:
            raise ValueError('runtime_authorization_expired')
        updated = proposal(root, project, policy, adapter,
                           reviewed_adapter_sha256=reviewed_adapter_sha256,
                           cohort=current['cohort'],
                           expires_at_unix_ms=current['expiresAtUnixMs'])
        _same_execution_scope(current, updated, reviewed_adapter_sha256)
        new_hash = proposal_sha256(updated)
        if new_hash != accepted_sha256:
            raise ValueError('runtime_authorization_plan_changed')
        if new_hash == old_hash:
            return {'status': 'already_active', 'proposalSha256': new_hash}
        directory = root / UPDATES_DIR
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError('runtime_authorization_unsafe_file')
        record = {'schema': UPDATE_SCHEMA, 'previousProposalSha256': old_hash,
                  'proposal': updated}
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n').encode()
        tmp_fd, tmp_name = tempfile.mkstemp(prefix='.pending-', dir=directory)
        try:
            os.fchmod(tmp_fd, 0o600)
            os.write(tmp_fd, raw)
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.link(tmp_name, directory / (new_hash + '.json'))
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            Path(tmp_name).unlink(missing_ok=True)
        return {'status': 'rebound', 'previousProposalSha256': old_hash,
                'proposalSha256': new_hash}
    finally:
        os.close(fd)


def active(root, policy, adapter, *, reviewed_adapter_sha256):
    """Return only a current exact grant; absent or expired means no grant."""
    from . import project_scope
    if project_scope.present(root):
        value = project_scope.require(root, policy, 'local_train')
        approved = {Path(r['root']) / 'core/context_learning/laya_adapter.py'
                    for r in value['binding']['installations']}
        if Path(adapter).resolve(strict=True) not in approved:
            raise ValueError('runtime_authorization_adapter_invalid')
        return value
    path = Path(root) / NAME
    if not path.exists() and not path.is_symlink():
        return None
    value = _chain(root, reviewed_adapter_sha256)
    from .synthetic_experiment import project_for
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('runtime_authorization_stale_or_invalid')
    expected = proposal(root, project_for(Path(root)), policy, adapter,
                        reviewed_adapter_sha256=reviewed_adapter_sha256,
                        cohort=value.get('cohort'),
                        expires_at_unix_ms=value.get('expiresAtUnixMs'))
    if (value != expected or not re.fullmatch(r'[0-9a-f]{64}',
            proposal_sha256(value))):
        raise ValueError('runtime_authorization_stale_or_invalid')
    return value if time.time_ns() // 1_000_000 < value['expiresAtUnixMs'] else None


def effective_argv(root, policy, adapter, *, reviewed_adapter_sha256):
    """Resolve only the reviewed host adapter while a v3 grant is current."""
    if active(root, policy, adapter,
              reviewed_adapter_sha256=reviewed_adapter_sha256) is None:
        return policy['trainerArgv']
    return [policy['trainerArgv'][0], str(Path(adapter).resolve(strict=True)),
            policy['trainerArgv'][2]]


def require_operation(root, policy, operation, *, approval_sha256=None):
    """Recheck expiry/revocation at dispatch and irreversible boundaries."""
    from . import project_scope
    if project_scope.present(root):
        value = project_scope.require(root, policy, operation, fresh_inventory=True)
        if approval_sha256 is not None and proposal_sha256(value) != approval_sha256:
            raise ValueError('runtime_authorization_required')
        return value
    from .shared import REVIEWED_ADAPTER_SHA256
    adapter = Path(__file__).with_name('laya_adapter.py').resolve(strict=True)
    value = active(root, policy, adapter,
                   reviewed_adapter_sha256=REVIEWED_ADAPTER_SHA256)
    if (value is None or operation not in value['operations']
            or (approval_sha256 is not None
                and proposal_sha256(value) != approval_sha256)):
        raise ValueError('runtime_authorization_required')
    return value


def activate(root, project, policy, adapter, accepted_sha256,
             *, reviewed_adapter_sha256, cohort, expires_at_unix_ms):
    """Write only an exact reviewed proposal; never alter policy or pointers."""
    value = proposal(root, project, policy, adapter,
                     reviewed_adapter_sha256=reviewed_adapter_sha256,
                     cohort=cohort, expires_at_unix_ms=expires_at_unix_ms)
    if time.time_ns() // 1_000_000 >= expires_at_unix_ms:
        raise ValueError('runtime_authorization_expired')
    if accepted_sha256 != proposal_sha256(value):
        raise ValueError('runtime_authorization_plan_changed')
    root = Path(root)
    path = root / NAME
    if path.exists() or path.is_symlink():
        _private(path)
        if json.loads(path.read_text(encoding='utf8')) == value:
            return {'status': 'already_active', 'proposalSha256': accepted_sha256}
        raise ValueError('runtime_authorization_existing_differs')
    # Activation, rather than repeated runtime validation, must prove the
    # cutoff is later than every preexisting capture and one-use ticket.
    # This cannot be recomputed after fresh cases enter the cohort.
    db_path = root / 'shared-learning.sqlite3'
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError('runtime_authorization_cohort_ledger_missing')
    from .history_activation import read_database
    with read_database(db_path, timeout=5) as db:
        db.execute('PRAGMA query_only=ON')
        case_max = db.execute('SELECT MAX(captured_at) FROM cases').fetchone()[0]
        ticket_max = db.execute('SELECT MAX(created_at) FROM edit_tickets').fetchone()[0]
    cutoff = cohort['notBeforeUnixMs']
    if (cutoff < time.time_ns() // 1_000_000
            or any(value is not None and value * 1000 >= cutoff
                   for value in (case_max, ticket_max))):
        raise ValueError('runtime_authorization_cohort_cutoff_not_fresh')
    if baseline_raw_paths(value['storyRoot'], cohort, root) != cohort['baselineRawPaths']:
        raise ValueError('runtime_authorization_baseline_changed')
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    return {'status': 'activated', 'proposalSha256': accepted_sha256}
