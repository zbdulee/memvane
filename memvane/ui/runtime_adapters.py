"""Read-only runtime, resident, disk and learning DTOs of the dashboard (``RuntimeResponse``, ``LearningResponse``).

Every source is a public read facade: the wake queue through
``runtime.api.runtime_main(['snapshot-readonly'])`` (no mkdir, ``PRAGMA
query_only``), the embedding owner through ``resident.api.inspect_owner_state``
(no exhausted-start record, no owner lock), disk previews through
``install.api.state_cleanup`` and ``provider.api.collect`` without approval,
and learning through ``learning.api.inspect_project``. Directory sizes come
from a bounded ``os.scandir`` walk that never follows a link. Nothing here
writes, starts a process or raises for an unreadable source: that source reads
as empty.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import sys
import threading
import time

WAKE_LABELS = {
    'laya-sync': '색인 동기화 (laya-sync)',
    'laya-compile': '위키 생성 (laya-compile)',
    'learning': '학습 사이클 (learning)',
    'auto': '자동 유지보수 (auto)',
}
# Wake reasons a resource admission wrote (``failure_reason`` of a ResourceDeferred).
RESOURCE_REASONS = ('ResourceDeferred', 'dispatch:resource_deferred')
GATE_CODES = ('candidateTrain', 'candidateEval', 'candidateEvalNonEmpty', 'candidateEvalEmpty')
GATE_NEEDS = {'candidateTrain': 100, 'candidateEval': 50, 'candidateEvalNonEmpty': 10, 'candidateEvalEmpty': 10}
CASE_ROWS_CAP = 1800
TEACHER_HOSTS = ('claude', 'codex', 'hermes', 'gemini')
MAX_WAKE_ITEMS = 256
SCAN_ENTRY_LIMIT = 200000
SCAN_DEPTH_LIMIT = 64
DISK_CACHE_TTL_SECONDS = 60.0
_DISK_CACHE = {}
_DISK_LOCK = threading.Lock()


# ----------------------------------------------------------------- helpers


def _iso(epoch):
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec='seconds')
    except (OverflowError, OSError, ValueError):
        return None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0.0 <= value <= 1.0 else None


def tree_size(path, *, limit=SCAN_ENTRY_LIMIT, depth=SCAN_DEPTH_LIMIT):
    """``(bytes, complete)`` of the regular files under ``path``; links and other devices are not followed."""
    total, seen = 0, 0
    try:
        root = os.lstat(path)
    except OSError:
        return 0, True
    if stat.S_ISLNK(root.st_mode):
        return 0, True
    if not stat.S_ISDIR(root.st_mode):
        return (root.st_size if stat.S_ISREG(root.st_mode) else 0), True
    stack = [(str(path), 0)]
    while stack:
        directory, level = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        return total, False
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if info.st_dev != root.st_dev:
                            continue
                        if level + 1 > depth:
                            return total, False
                        stack.append((entry.path, level + 1))
                    elif entry.is_file(follow_symlinks=False):
                        total += info.st_size
        except OSError:
            continue
    return total, True


def _disk_item(label, path):
    size, complete = tree_size(path)
    return {'label': label if complete else label + ' · 일부만 집계', 'bytes': size}


# --------------------------------------------------------------- installs


def _proved_installs(home):
    """``[(install id, project name, project root, decision)]`` of the registry's proved main checkouts."""
    from memvane.ui import read_adapters
    snapshot = read_adapters._registry(home)
    found = []
    for row in read_adapters._rows(snapshot):
        if row.get('kind') != 'main' or row.get('status') != 'active':
            continue
        decision, state_root = read_adapters._prove(row['targetRoot'], row['installId'], worktree=False)
        if decision is None or state_root is None:
            continue
        found.append((row['installId'], Path(row['targetRoot']).name or row['targetRoot'],
                      row.get('projectRoot') or row['targetRoot'], decision))
    return snapshot, found


def _project_index(snapshot):
    """Target and project roots of the registry rows → ``(install id, project name)``."""
    from memvane.ui import read_adapters
    index = {}
    for row in read_adapters._rows(snapshot):
        name = Path(row['targetRoot']).name or row['targetRoot']
        for key in (row['targetRoot'], row.get('projectRoot')):
            if isinstance(key, str) and key not in index:
                index[key] = (row['installId'], name)
    return index


# ------------------------------------------------------------------ wakes


def wake_snapshot(root=None):
    """The read-only wake queue snapshot dict, or None when the runtime answered nothing."""
    from memvane.runtime import api as runtime
    collected = []
    argv = ['snapshot-readonly'] if root is None else ['snapshot-readonly', str(root)]
    try:
        runtime.runtime_main(argv, execute=collected.append)
    except Exception:  # noqa: BLE001 - a diagnostic read; an unreadable queue reads as empty
        return None
    return collected[0] if collected and isinstance(collected[0], dict) else None


def _wake_state(row):
    if row.get('status') == 'running':
        return 'running'
    reason = row.get('reason')
    if isinstance(reason, str) and (reason in RESOURCE_REASONS or 'resource' in reason):
        return 'deferred_for_resources'
    return 'waiting'


def wake_items(snapshot, projects=None):
    """``WakeQueueItem`` rows of a wake snapshot: running first, then by due time."""
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'read':
        return []
    projects = projects or {}
    items = []
    rows = [(row, False) for row in snapshot.get('wakes') or () if isinstance(row, dict)]
    rows += [(row, True) for row in snapshot.get('overflowRows') or () if isinstance(row, dict)]
    for row, overflow in rows:
        kind = row.get('kind') if isinstance(row.get('kind'), str) else ''
        install, name = projects.get(row.get('target'), (None, None))
        attempts = 0 if overflow else _count(row.get('attempts'))
        state = 'waiting' if overflow else _wake_state(row)
        items.append({
            'kind': kind,
            'label': WAKE_LABELS.get(kind, kind or '알 수 없는 작업'),
            'installId': install,
            'projectName': name,
            'state': state,
            # The round being run (running) or the next one (pending).
            'attempt': attempts + 1,
            'dueAt': None if overflow else _iso(row.get('due')),
            'deferrals': attempts,
        })
    order = {'running': 0, 'deferred_for_resources': 1, 'waiting': 2}
    items.sort(key=lambda item: (order[item['state']], item['dueAt'] or ''))
    return items[:MAX_WAKE_ITEMS]


# ---------------------------------------------------------------- resident


def _provider_manifest(decision):
    from memvane.journal import api as journal
    if not isinstance(decision, dict) or decision.get('state') != 'candidate':
        return None
    try:
        proof = journal.verify_selected_route(decision)
    except Exception:  # noqa: BLE001 - an unproved install shows no provider state
        return None
    manifest = proof.get('providerManifest') if isinstance(proof, dict) else None
    return manifest if isinstance(manifest, dict) and isinstance(manifest.get('serviceSocketPath'), str) else None


def owner_views(install_id, view):
    """``(ProviderProofState, SharedWorkerState or None)`` of one ``inspect_owner_state`` answer."""
    view = view if isinstance(view, dict) else {}
    installation = view.get('installation') if isinstance(view.get('installation'), dict) else {}
    socket = view.get('socket') if isinstance(view.get('socket'), dict) else {}
    shared = view.get('sharedWorker') if isinstance(view.get('sharedWorker'), dict) else {}
    reason = installation.get('reason')
    retry = installation.get('retryInSeconds')
    if installation.get('status') == 'present' and reason == 'attestation_invalid':
        proof = {'installId': install_id, 'state': 'attestation_invalid',
                 'reproofInSeconds': int(retry) if isinstance(retry, (int, float)) and retry > 0 else None}
    elif socket.get('status') == 'ready' or (installation.get('status') == 'present' and reason == 'ready'
                                              and not installation.get('exhausted')):
        proof = {'installId': install_id, 'state': 'proved', 'reproofInSeconds': None}
    else:
        proof = {'installId': install_id, 'state': 'unavailable', 'reproofInSeconds': None}
    worker = None
    if isinstance(shared.get('socket'), str):
        worker = {'socket': shared['socket'],
                  'running': shared.get('status') == 'present' and shared.get('reason') == 'ready',
                  'pid': None, 'bundleVersion': None, 'accelerator': None}
    return proof, worker


def resident_states(installs):
    """``(sharedWorkers, proofs)`` of the proved installs, one shared worker row per socket."""
    from memvane.resident import api as resident
    workers, proofs = {}, []
    for install_id, _name, _root, decision in installs:
        manifest = _provider_manifest(decision)
        if manifest is None:
            proofs.append({'installId': install_id, 'state': 'unavailable', 'reproofInSeconds': None})
            continue
        try:
            view = resident.inspect_owner_state(manifest)
        except Exception:  # noqa: BLE001 - the facade never raises; a defect reads as unavailable
            view = None
        proof, worker = owner_views(install_id, view)
        proofs.append(proof)
        if worker is not None:
            known = workers.get(worker['socket'])
            workers[worker['socket']] = dict(worker, running=worker['running'] or bool(known and known['running']))
    return [workers[key] for key in sorted(workers)], proofs


# -------------------------------------------------------------------- disk


def _support_dir(home):
    """The per-user support directory of the managed learning runtime (``runtime_setup.runtime_base``)."""
    from memvane.kernel import memvane_paths
    base = Path(home) if home is not None else Path.home()
    parts = ('Library', 'Application Support') if sys.platform == 'darwin' else ('.local', 'share')
    return base.joinpath(*parts, memvane_paths.NAME)


def disk_usage(home=None):
    """``DiskUsageItem`` rows: install state, reclaimable installs, embedding runtime, learning runtime."""
    from memvane.install import api as install
    from memvane.kernel import memvane_paths
    from memvane.provider import api as provider
    from memvane.ui import platform_io
    if platform_io.fixture_live_home(home):
        return []
    base = memvane_paths.state_base(home)
    items = [_disk_item('설치별 상태 (installs)', base / 'installs')]
    try:
        plan = install.state_cleanup(home=home, everything=False)
    except Exception:  # noqa: BLE001 - a preview that cannot be read shows no reclaimable row
        plan = None
    if isinstance(plan, dict) and isinstance(plan.get('installs'), list):
        rows = [row for row in plan['installs'] if isinstance(row, dict) and row.get('action') == 'remove']
        if rows:
            items.append({'label': f'정리 가능한 설치 상태 ({len(rows)}개)',
                          'bytes': sum(_count(row.get('bytes')) for row in rows)})
    runtime_root = base / 'embed-runtime'
    if os.path.isdir(runtime_root) and not os.path.islink(runtime_root):
        label = '임베딩 런타임 (embed-runtime)'
        try:
            preview = provider.collect(home=home, approve=False)
        except Exception:  # noqa: BLE001 - busy or unsafe runtime: the size alone is shown
            preview = None
        if isinstance(preview, dict) and isinstance(preview.get('generations'), list):
            rows = [row for row in preview['generations'] if isinstance(row, dict)]
            removable = sum(1 for row in rows if row.get('action') in ('remove', 'remove_stale_staging'))
            label = f'임베딩 런타임 (embed-runtime · {len(rows)}세대, 정리 가능 {removable})'
        items.append(_disk_item(label, runtime_root))
    support = _support_dir(home)
    if os.path.isdir(support) and not os.path.islink(support):
        items.append(_disk_item('학습 런타임·상태 (learning runtime)', support))
    return items


# ----------------------------------------------------------------- runtime


def disk_overview(*, home=None, refresh=False, ttl_seconds=DISK_CACHE_TTL_SECONDS):
    """``{'disk': [DiskUsageItem]}`` from a per-home in-process cache that rescans only on refresh or expiry.

    The lock is held across the scan, so concurrent requests share one walk
    instead of each starting their own. A scan that raises is not cached.
    """
    key = os.fspath(home) if home is not None else ''
    with _DISK_LOCK:
        cached = _DISK_CACHE.get(key)
        now = time.monotonic()
        if refresh or cached is None or now - cached[0] >= ttl_seconds:
            items = disk_usage(home)
            cached = (time.monotonic(), [dict(item) for item in items])
            _DISK_CACHE[key] = cached
        return {'disk': [dict(item) for item in cached[1]]}


def runtime_overview(*, home=None, wake_root=None, include_disk=True):
    """``RuntimeResponse`` DTO matching ``ui/src/api/types.ts``; ``include_disk=False`` skips the disk walk."""
    snapshot, installs = _proved_installs(home)
    workers, proofs = resident_states(installs)
    return {
        'sharedWorkers': workers,
        'proofs': proofs,
        'wakes': wake_items(wake_snapshot(wake_root), _project_index(snapshot)),
        'disk': disk_usage(home) if include_disk else [],
    }


# ---------------------------------------------------------------- learning


def _metrics(inspection):
    empty = {'exactRate': None, 'microF1': None, 'rankExactRate': None}
    scores = inspection.get('scores')
    if not isinstance(scores, dict) or not scores:
        return empty
    pointers = inspection.get('pointers') if isinstance(inspection.get('pointers'), dict) else {}
    active = pointers.get('active') if isinstance(pointers.get('active'), dict) else {}
    key = active.get('cycle_id')
    group = scores.get(key) if key in scores else None
    if not isinstance(group, dict):
        candidates = [value for value in scores.values() if isinstance(value, dict)]
        group = max(candidates, key=lambda value: _count(value.get('cases')), default=None)
    if not isinstance(group, dict):
        return empty
    return {name: _rate(group.get(name)) for name in empty}


def _teacher(inspection):
    teacher = inspection.get('teacher') if isinstance(inspection.get('teacher'), dict) else {}
    hosts = teacher.get('hosts') if isinstance(teacher.get('hosts'), dict) else {}
    rows = []
    for host in TEACHER_HOSTS:
        row = hosts.get(host)
        if not isinstance(row, dict):
            continue
        reason = row.get('reason') if isinstance(row.get('reason'), str) else None
        if teacher.get('off') is True:
            reason = reason or 'teacher_off'
        # The teacher state keeps no per-day call count; the queued labelling requests stand in.
        rows.append({'host': host, 'callsToday': _count(row.get('queued')), 'waitingReason': reason})
    return rows


def learning_view(install_id, inspection, *, check_invariants=False):
    """``LearningResponse`` DTO of one ``inspect_project`` answer."""
    inspection = inspection if isinstance(inspection, dict) else {'status': 'unreadable'}
    promotion = inspection.get('promotion') if isinstance(inspection.get('promotion'), dict) else {}
    needs = promotion.get('needs') if isinstance(promotion.get('needs'), dict) else {}
    pointers = inspection.get('pointers') if isinstance(inspection.get('pointers'), dict) else {}
    active = pointers.get('active') if isinstance(pointers.get('active'), dict) else {}
    checkpoint = active.get('cycle_id')
    cap = inspection.get('caseLimit')
    result = {
        'installId': install_id,
        'checkpoint': checkpoint if isinstance(checkpoint, str) else None,
        'gates': [{'code': code, 'current': _count(promotion.get(code)),
                   'required': _count(needs.get(code)) or GATE_NEEDS[code]} for code in GATE_CODES],
        'caseRows': _count(inspection.get('caseRows')),
        'caseRowsCap': cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else CASE_ROWS_CAP,
        'metrics': _metrics(inspection),
        'teacher': _teacher(inspection),
    }
    if check_invariants:
        result['invariants'] = inspection.get('invariants') if isinstance(inspection.get('invariants'), dict) else None
    return result


def learning_overview(install_id, *, check_invariants=False, home=None):
    """The proved install's learning monitor; ``check_invariants`` hashes artifacts (explicit request only)."""
    from memvane.learning import api as learning
    from memvane.ui import read_adapters
    target = read_adapters._install(install_id, home)
    try:
        inspection = learning.inspect_project(target['targetRoot'], check_invariants=check_invariants)
    except Exception:  # noqa: BLE001 - the facade never raises; a defect reads as unreadable
        inspection = {'status': 'unreadable'}
    return learning_view(install_id, inspection, check_invariants=check_invariants)
