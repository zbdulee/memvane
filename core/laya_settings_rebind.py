"""Recoverable rebind of a selected Laya route to new settings bytes.

A selected route binds the exact settings bytes: the route record carries
``settingsSha256`` and its own digest is then bound by the private
``reviewed-route.json``, the install journal's selected route digest, the
selection ledger (fresh-wiki or QMD-origin) and the project selection marker.
``settings_migration`` uses this module when a settings rewrite leaves the
expanded collection identity unchanged: only ``settingsSha256`` moves, never
the index store, scope, generation, vector profile or plugin bundle.

A rewrite may also rename the raw collection or expand its root to an
ancestor, while the wiki collection (name, role, root) stays equal. The index
holds wiki cards only, so its generation, catalog, binding store and vectors
stay valid. The route and ledger ``collections`` then move with the settings
digest, and the raw discovery cursor beside the claim queue is re-keyed to the
new collection (``laya_wiki_discovery.rebased_cursor``) as one more step of
the same intent.

The switch takes the install lock, the cutover operation lock and the
provider lane (the locks the active bundle upgrade takes), writes an intent
in the private state root, then moves every binding with an exact
old-to-new compare. Each step accepts the already-new value, so an
interrupted run resumes; the same steps run backward for a rollback.

While the intent exists, a binding set that mixes the intent's old and new
values classifies as ``rebinding``. The host then serves the last published
generation read-only (``verify_read_only``) and records no work, like a
bundle-drift session. Any other value stays ``blocked``.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

INTENT = 'settings-rebind-intent.json'
SCHEMA = 'laya-settings-rebind-intent-v1'
LIMIT = 6 * 1024 * 1024
REVIEWED = 'reviewed-route.json'
ROUTE = 'laya-index-route.json'
MARKER = 'laya-index-selection.json'
SETTINGS = 'settings.json'
LEDGERS = (('fresh', 'fresh-wiki-cutover.json'), ('qmd', 'qmd-cutover.json'))
_STRINGS = ('origin', 'projectRoot', 'stateRoot', 'journalPath', 'proposalSha256',
            'oldSettings', 'newSettings', 'oldRoute', 'newRoute', 'oldMarker',
            'newMarker')
# Present together only when the raw collection moves.
_DISCOVERY = ('discoveryPath', 'oldDiscovery', 'newDiscovery')


class RebindRefused(RuntimeError):
    def __init__(self, reason, **detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _fail(reason, **detail):
    raise RebindRefused(reason, **detail)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _private(path, limit=LIMIT):
    """Owner-only regular single-link file bytes, or None when absent."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                info.st_size > limit):
            _fail('settings_rebind_file_unsafe')
        raw = os.read(fd, limit + 1)
        if len(raw) != info.st_size:
            _fail('settings_rebind_file_changed')
        return raw
    finally:
        os.close(fd)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, raw):
    fd, name = tempfile.mkstemp(prefix='.settings-rebind-', dir=Path(path).parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.link(temp, path)
        _fsync_dir(Path(path).parent)
    except FileExistsError:
        _fail('settings_rebind_intent_exists')
    finally:
        temp.unlink(missing_ok=True)


def _strip_epoch(row):
    return {key: value for key, value in row.items() if key != 'epoch'}


# ------------------------------------------------------------------ intent

def intent_path(state):
    return Path(state) / INTENT


def _route_collections(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value.get('collections') if isinstance(value, dict) else None


def load_intent(state):
    """The validated in-flight intent of this state root, or None."""
    from laya_install_journal import raw_collection_rebind
    raw = _private(intent_path(state))
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        _fail('settings_rebind_intent_invalid')
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            raw != _canonical(value) or
            any(type(value.get(key)) is not str for key in _STRINGS) or
            value['origin'] not in dict(LEDGERS) or
            value['stateRoot'] != str(Path(state)) or
            value['journalPath'] != str(Path(state) / 'install-journal.sqlite') or
            not isinstance(value.get('oldLedger'), dict) or
            not isinstance(value.get('newLedger'), dict) or
            _strip_epoch(value['oldLedger']) == _strip_epoch(value['newLedger']) or
            value['oldLedger'].get('routeSha256') != _sha(value['oldRoute'].encode()) or
            value['newLedger'].get('routeSha256') != _sha(value['newRoute'].encode())):
        _fail('settings_rebind_intent_invalid')
    moved = [key for key in _DISCOVERY if key in value]
    ignored = {'routeSha256'}
    if moved:
        old_collections = _route_collections(value['oldRoute'])
        new_collections = _route_collections(value['newRoute'])
        if (moved != list(_DISCOVERY) or
                any(type(value[key]) is not str for key in _DISCOVERY) or
                Path(value['discoveryPath']).parent != Path(state) or
                not raw_collection_rebind(old_collections, new_collections) or
                value['oldLedger'].get('collections') != old_collections or
                value['newLedger'].get('collections') != new_collections):
            _fail('settings_rebind_intent_invalid')
        ignored.add('collections')
    if (_strip_epoch({key: item for key, item in value['oldLedger'].items()
                      if key not in ignored}) !=
            _strip_epoch({key: item for key, item in value['newLedger'].items()
                          if key not in ignored})):
        _fail('settings_rebind_intent_invalid')
    return value


def _pairs(intent):
    enc = {key: intent[key].encode('utf-8') for key in
           ('oldSettings', 'newSettings', 'oldRoute', 'newRoute',
            'oldMarker', 'newMarker')}
    return enc


def pending_for_project(project):
    """(state root, intent) of a rebind left in flight for this project, or None."""
    from laya_index_host_route import MAX_RECORD_BYTES, _read_regular
    route = Path(project) / '.auto-context' / ROUTE
    try:
        record = json.loads(_read_regular(route, MAX_RECORD_BYTES).decode('utf-8'))
    except (OSError, ValueError, UnicodeError):
        # No readable route: the migration binding check reports the record.
        return None
    journal = record.get('journalPath') if isinstance(record, dict) else None
    if not isinstance(journal, str) or not Path(journal).is_absolute():
        return None
    state = Path(journal).parent
    intent = load_intent(state)
    if intent is None or intent['projectRoot'] != str(project):
        return None
    return state, intent


# ------------------------------------------------------------- read side

def in_flight_decision(cwd):
    """``rebinding`` decision when only an in-flight rebind blocks the route.

    The route file and settings file must each hold the intent's old or new
    bytes. The decision is computed from the route that matches the settings
    on disk, so it describes one consistent, reviewed pair.
    """
    from laya_index_host_route import (MAX_RECORD_BYTES, MAX_SETTINGS_BYTES,
                                       _candidate_paths, _classify_record,
                                       _read_regular)
    requested = Path(cwd)
    if not requested.is_absolute():
        return None
    current = requested.resolve(strict=False)
    records = list(_candidate_paths(current))
    if len(records) != 1 or not requested.is_dir():
        return None
    root, auto, path = records[0]
    markers = [directory for directory in (current, *current.parents)
               if (directory / '.auto-context' / MARKER).exists() or
               (directory / '.auto-context' / MARKER).is_symlink()]
    if markers != [root]:
        return None
    raw = _read_regular(path, MAX_RECORD_BYTES)
    record = json.loads(raw.decode('utf-8'))
    journal = record.get('journalPath') if isinstance(record, dict) else None
    if not isinstance(journal, str) or not Path(journal).is_absolute():
        return None
    intent = load_intent(Path(journal).parent)
    if (intent is None or intent['projectRoot'] != str(root) or
            intent['journalPath'] != journal):
        return None
    pairs = _pairs(intent)
    if raw not in (pairs['oldRoute'], pairs['newRoute']):
        return None
    settings_raw = _read_regular(auto / SETTINGS, MAX_SETTINGS_BYTES)
    route_raw = {pairs['oldSettings']: pairs['oldRoute'],
                 pairs['newSettings']: pairs['newRoute']}.get(settings_raw)
    if route_raw is None:
        return None
    decision = _classify_record(root, auto, path, current, route_raw=route_raw,
                                settings_raw=settings_raw)
    if decision.get('state') != 'candidate':
        return None
    return {**decision, 'state': 'rebinding', 'reason': 'settings_rebind_in_progress'}


def verify_read_only(decision):
    """Published-generation proof while a rebind is between steps.

    Every binding holds the intent's old or new value; the journal proof is
    ``verify_rebinding_read_only``. The journal epoch is left out so that a
    rebind step between the first proof and a recheck does not fail recall.
    """
    from laya_install_journal import verify_rebinding_read_only
    if not isinstance(decision, dict) or decision.get('state') != 'rebinding':
        _fail('settings_rebind_decision_invalid')
    project = Path(decision['projectRoot'])
    state = Path(decision['journalPath']).parent
    intent = load_intent(state)
    if intent is None or intent['projectRoot'] != str(project):
        _fail('settings_rebind_intent_missing')
    pairs = _pairs(intent)
    auto = project / '.auto-context'
    ledger = _private(state / dict(LEDGERS)[intent['origin']])
    try:
        row = json.loads(ledger.decode('utf-8')) if ledger is not None else None
    except (UnicodeError, ValueError):
        row = None
    from laya_index_host_route import MAX_SETTINGS_BYTES, _read_regular
    settings = _read_regular(auto / SETTINGS, MAX_SETTINGS_BYTES)
    if (_private(auto / ROUTE, 8192) not in (pairs['oldRoute'], pairs['newRoute']) or
            _private(state / REVIEWED, 8192) not in (pairs['oldRoute'], pairs['newRoute']) or
            _private(auto / MARKER, 8192) not in (pairs['oldMarker'], pairs['newMarker']) or
            not isinstance(row, dict) or
            _strip_epoch(row) not in (_strip_epoch(intent['oldLedger']),
                                      _strip_epoch(intent['newLedger'])) or
            row.get('phase') != 'active' or
            settings not in (pairs['oldSettings'], pairs['newSettings'])):
        _fail('settings_rebind_state_unexpected')
    proof = verify_rebinding_read_only(decision, pairs['oldRoute'], pairs['newRoute'])
    return {key: value for key, value in proof.items() if key != 'selectionEpoch'}


# ------------------------------------------------------------ writer side

class _Ledger:
    def __init__(self, origin):
        self.origin = origin
        if origin == 'fresh':
            import laya_fresh_wiki_origin as module
        else:
            # Only a QMD-origin ledger loads the QMD cutover module.
            import laya_qmd_cutover as module
        self.module = module
        self.name = dict(LEDGERS)[origin]

    def load(self, path):
        return (self.module._load(path) if self.origin == 'fresh' else
                self.module._ledger(path))

    def marker(self, row):
        return _canonical(self.module._marker(row))

    def write(self, path, row):
        if self.origin == 'qmd':
            self.module._validated(dict(row), path)
        self.module._write(path, row, replace=True)
        self.load(path)


def _origin(state):
    found = [origin for origin, name in LEDGERS
             if (Path(state) / name).exists() or (Path(state) / name).is_symlink()]
    if len(found) != 1:
        _fail('settings_rebind_ledger_ambiguous')
    return _Ledger(found[0])


def inspect(project, new_settings, new_collections=None):
    """Read-only proof that this selected route can be rebound to ``new_settings``.

    ``new_collections`` (route shape, name-sorted) replaces the bound
    collections when only the raw entry differs. Returns the summary a
    migration proposal binds, plus the exact current bytes the intent needs.
    Refuses a route that is not active and consistent.
    """
    from laya_install_journal import raw_collection_rebind
    from laya_index_host_route import (MAX_SETTINGS_BYTES, SCHEMA_V2, _classify_record,
                                       _read_regular, select_route)
    from laya_install_journal import inspect_journal
    project = Path(project)
    auto = project / '.auto-context'
    decision = select_route(str(project), action='recall')
    if decision.get('state') != 'candidate' or decision.get('projectRoot') != str(project):
        _fail('selected_route_not_verified', routeState=decision.get('state'),
              routeReason=decision.get('reason'))
    # A candidate classification checks only the route record and settings;
    # the installer evidence below proves an active selection.
    try:
        old_route = _private(auto / ROUTE, 8192)
        if old_route is None:
            _fail('selected_route_missing')
        route = json.loads(old_route.decode('utf-8'))
        if not isinstance(route, dict) or route.get('schema') != SCHEMA_V2:
            _fail('selected_route_schema_unsupported')
        journal = Path(route['journalPath'])
        state = journal.parent
        ledger = _origin(state)
        row = ledger.load(state / ledger.name)
        old_marker = _private(auto / MARKER, 8192)
        current = inspect_journal(journal)
        if (row['phase'] != 'active' or row['routeSha256'] != _sha(old_route) or
                row['journalPath'] != str(journal) or row['projectRoot'] != str(project) or
                old_marker != ledger.marker(row) or
                _private(state / REVIEWED, 8192) != old_route or
                current['state'] not in ('active', 'restored_old') or
                current['selectedRouteSha256'] != _sha(old_route)):
            _fail('selected_route_bindings_inconsistent')
    except RebindRefused as exc:
        _fail('selected_route_not_verified', routeState='candidate', routeReason=exc.reason)
    except Exception as exc:
        _fail('selected_route_not_verified', routeState='candidate',
              routeReason=str(exc) or type(exc).__name__)
    old_settings = _read_regular(auto / SETTINGS, MAX_SETTINGS_BYTES)
    if _sha(old_settings) != route['settingsSha256']:
        _fail('selected_route_settings_changed')
    moved = new_collections is not None and new_collections != route['collections']
    if moved and (not raw_collection_rebind(route['collections'], new_collections) or
                  row.get('collections') != route['collections']):
        _fail('rebound_route_identity_changed', changedRouteFields=['collections'])
    new_route = _canonical({**route, 'settingsSha256': _sha(new_settings),
                            **({'collections': new_collections} if moved else {})})
    after = _classify_record(project, auto, auto / ROUTE, project, route_raw=new_route,
                             settings_raw=new_settings)
    if after.get('state') != 'candidate':
        _fail('rebound_route_unsupported', routeReason=after.get('reason'))
    changed = sorted(key for key in set(decision) | set(after)
                     if decision.get(key) != after.get(key))
    allowed = {'recallSettings'} | ({'collections', 'collectionRoots'} if moved else set())
    if set(changed) - allowed:
        _fail('rebound_route_identity_changed', changedRouteFields=changed)
    new_row = {**row, 'epoch': row['epoch'] + 1, 'routeSha256': _sha(new_route),
               **({'collections': new_collections} if moved else {})}
    summary = {'origin': ledger.origin, 'stateRoot': str(state),
               'oldRouteSha256': _sha(old_route), 'newRouteSha256': _sha(new_route),
               'routeDecisionChanges': {key: {'from': decision.get(key),
                                              'to': after.get(key)}
                                        for key in changed}}
    exact = {'oldSettings': old_settings, 'newSettings': new_settings,
             'oldRoute': old_route, 'newRoute': new_route,
             'oldMarker': old_marker, 'newMarker': ledger.marker(new_row),
             'oldLedger': row, 'newLedger': new_row}
    if moved:
        summary.update(_raw_move(ledger, row, new_row, state, decision, after, exact))
    return summary, exact


def _raw_entry(collections):
    return next(item for item in collections if item['role'] == 'raw')


def _raw_move(ledger, row, new_row, state, decision, after, exact):
    """Summary of a raw collection move; adds the cursor bytes to ``exact``."""
    import laya_wiki_discovery as discovery
    old_raw, new_raw = _raw_entry(row['collections']), _raw_entry(new_row['collections'])
    if ledger.origin == 'fresh' and new_raw['sourceRoot'] != row['projectRoot']:
        # A fresh-wiki selection always binds the whole project as raw.
        _fail('rebound_route_unsupported', routeReason='fresh_wiki_raw_root')
    if ledger.origin == 'qmd':
        try:
            ledger.module._validated(dict(new_row), Path(state) / ledger.name)
        except Exception as exc:
            _fail('rebound_route_unsupported', routeReason=str(exc) or type(exc).__name__)
    try:
        path, old, new = discovery.rebased_cursor(decision, after)
    except discovery.DiscoveryBlocked as exc:
        _fail('raw_discovery_rebase_refused', discoveryReason=str(exc))
    except Exception as exc:
        _fail('raw_discovery_rebase_refused',
              discoveryReason=str(exc) or type(exc).__name__)
    exact.update({'discoveryPath': str(path), 'oldDiscovery': old, 'newDiscovery': new})
    forfeits = []
    if ledger.origin == 'qmd' and old_raw['name'] != new_raw['name']:
        # The QMD source digest is bound to the ledger's collection names;
        # the QMD store keeps the old raw name, so restore cannot re-prove it.
        forfeits.append('qmd_origin_restore')
    return {'rawCollection': {'from': old_raw, 'to': new_raw}, 'forfeits': forfeits}


def _provider_lane():
    """Excludes selected-index writers (the sync owner holds this lane)."""
    from context_learning.resource_coordination import Coordinator
    return Coordinator().quiesce_provider()


@contextmanager
def locks(project, state):
    """The locks a selected bundle upgrade takes, in the same order."""
    from laya_product_install import _install_lock
    from laya_ledger_common import _operation_lock
    marker = Path(project) / '.auto-context' / MARKER
    try:
        with _install_lock(Path(state)), _operation_lock(marker), _provider_lane():
            yield
    except RebindRefused:
        raise
    except Exception as exc:
        name = type(exc).__name__
        if name in ('InstallBlocked', 'CutoverBlocked', 'ResourceDeferred'):
            _fail('selected_route_busy', busy=str(exc) or name)
        raise


def _settle_active_upgrade(project, state):
    """A finished bundle upgrade keeps its intent until the next upgrade; that
    next upgrade checks the route digest, so settle the receipt first."""
    import laya_active_upgrade as active
    path = Path(state) / active.INTENT
    if not path.exists() and not path.is_symlink():
        return
    try:
        intent = active._load_intent(Path(state), Path(project))
        active._archive_finished_intent(Path(state), Path(project), intent)
    except Exception as exc:
        _fail('active_upgrade_unsettled', error=str(exc) or type(exc).__name__)


def begin(project, proposal_sha256, new_settings, new_collections=None):
    """Write the intent after re-proving the live route. Caller holds ``locks``."""
    summary, exact = inspect(project, new_settings, new_collections)
    state = Path(summary['stateRoot'])
    if load_intent(state) is not None:
        _fail('settings_rebind_intent_exists')
    _settle_active_upgrade(project, state)
    intent = {'schema': SCHEMA, 'origin': summary['origin'],
              'projectRoot': str(project), 'stateRoot': str(state),
              'journalPath': str(state / 'install-journal.sqlite'),
              'proposalSha256': proposal_sha256,
              **{key: value.decode('utf-8') for key, value in exact.items()
                 if isinstance(value, bytes)},
              **({'discoveryPath': exact['discoveryPath']} if 'discoveryPath' in exact
                 else {}),
              'oldLedger': exact['oldLedger'], 'newLedger': exact['newLedger']}
    _write_new(intent_path(state), _canonical(intent))
    if load_intent(state) != intent:
        _fail('settings_rebind_intent_invalid')
    return intent


def _settings_expected(path, old, new):
    """Atomic exact replace of the settings file, keeping its mode."""
    from laya_index_host_route import MAX_SETTINGS_BYTES, _read_regular
    current = _read_regular(path, MAX_SETTINGS_BYTES)
    if current == new:
        return
    if current != old:
        _fail('settings_rebind_settings_changed')
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    fd, name = tempfile.mkstemp(prefix='settings.json.', suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        if path.is_symlink() or _read_regular(path, MAX_SETTINGS_BYTES) != old:
            _fail('settings_rebind_settings_changed')
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _ledger_step(intent, target):
    state = Path(intent['stateRoot'])
    ledger = _Ledger(intent['origin'])
    path = state / ledger.name
    want = intent['newLedger'] if target == 'new' else intent['oldLedger']
    other = intent['oldLedger'] if target == 'new' else intent['newLedger']
    with ledger.module._ledger_lock(path):
        row = ledger.load(path)
        if _strip_epoch(row) == _strip_epoch(want):
            return
        if _strip_epoch(row) != _strip_epoch(other):
            _fail('settings_rebind_ledger_changed')
        ledger.write(path, {**want, 'epoch': row['epoch'] + 1})


def _journal_step(intent, target):
    from laya_install_journal import cas_rebind_route, inspect_journal
    pairs = _pairs(intent)
    before, after = ((pairs['oldRoute'], pairs['newRoute']) if target == 'new' else
                     (pairs['newRoute'], pairs['oldRoute']))
    current = inspect_journal(intent['journalPath'])
    if current['selectedRouteSha256'] == _sha(after):
        return
    cas_rebind_route(intent['journalPath'], current['epoch'], before, after)


def _route_decision(route_raw):
    """The cursor binding fields of a route record (scope, queue, collections)."""
    route = json.loads(route_raw)
    return {**route, 'collectionRoots': {item['name']: item['sourceRoot']
                                         for item in route['collections']}}


def _discovery_step(intent, target):
    import laya_wiki_discovery as discovery
    if 'discoveryPath' not in intent:
        return
    old, new = intent['oldDiscovery'].encode(), intent['newDiscovery'].encode()
    keep = None
    if target == 'old':
        old, new = new, old
        keep = _route_decision(intent['oldRoute'])
    decision = _route_decision(intent['newRoute'])
    if str(discovery._paths(decision)[0]) != intent['discoveryPath']:
        _fail('settings_rebind_discovery_changed')
    try:
        discovery.replace_cursor(decision, old, new, keep_valid_for=keep)
    except discovery.DiscoveryBlocked as exc:
        _fail('settings_rebind_discovery_changed', discoveryReason=str(exc))


def _file_steps(intent, target):
    from laya_inactive_upgrade import _atomic_expected
    pairs = _pairs(intent)
    state, auto = Path(intent['stateRoot']), Path(intent['projectRoot']) / '.auto-context'
    side = (lambda key: (pairs['old' + key], pairs['new' + key])) if target == 'new' \
        else (lambda key: (pairs['new' + key], pairs['old' + key]))
    return {'reviewed': lambda: _atomic_expected(state / REVIEWED, *side('Route')),
            'route': lambda: _atomic_expected(auto / ROUTE, *side('Route')),
            'marker': lambda: _atomic_expected(auto / MARKER, *side('Marker')),
            'settings': lambda: _settings_expected(auto / SETTINGS, *side('Settings')),
            'ledger': lambda: _ledger_step(intent, target),
            'journal': lambda: _journal_step(intent, target),
            'discovery': lambda: _discovery_step(intent, target)}


FORWARD = ('discovery', 'journal', 'reviewed', 'route', 'ledger', 'marker', 'settings')


def forward(intent):
    """Move every binding to the new settings bytes; resumable. Caller holds ``locks``."""
    steps = _file_steps(intent, 'new')
    for name in FORWARD:
        steps[name]()
    _verify_selected(intent)
    return _archive(intent, 'settled')


def rollback(intent):
    """Return every binding to the old settings bytes. Caller holds ``locks``."""
    steps = _file_steps(intent, 'old')
    for name in reversed(FORWARD):
        steps[name]()
    _verify_selected(intent)
    return _archive(intent, 'rolled-back')


def _verify_selected(intent):
    """The finished route is an ordinary candidate with a read-only proof."""
    from laya_index_host_route import select_route
    from laya_install_journal import verify_selected_route_read_only
    decision = select_route(intent['projectRoot'], action='recall')
    if decision.get('state') != 'candidate':
        _fail('settings_rebind_unverified', routeReason=decision.get('reason'))
    try:
        verify_selected_route_read_only(decision)
        if 'discoveryPath' in intent:
            import laya_wiki_discovery as discovery
            path = discovery._paths(decision)[0]
            if str(path) != intent['discoveryPath']:
                _fail('settings_rebind_discovery_changed')
            discovery._validate_state(discovery._read(path), decision)
    except Exception as exc:
        _fail('settings_rebind_unverified', error=str(exc) or type(exc).__name__)


def _archive(intent, label):
    state = Path(intent['stateRoot'])
    target = state / ('settings-rebind-%s-%s.json' % (_sha(_canonical(intent))[:20], label))
    if target.exists() or target.is_symlink():
        _fail('settings_rebind_receipt_exists')
    os.rename(intent_path(state), target)
    _fsync_dir(state)
    return str(target)
