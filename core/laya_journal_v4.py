"""Gated v3 -> v4 install-journal conversion for one Laya install.

A v4 journal no longer stores the global QMD directory inventory. Code that
reads only v3 (0.38.0 and older) refuses a v4 file as
``journal_schema_unknown``, so a journal converts only when every host copy
of this plugin that can execute against it reads v4:

* the executing bundle (this code);
* Claude's installed copy of the same marketplace plugin, from
  ``~/.claude/plugins/installed_plugins.json``;
* every Codex cache copy under ``~/.codex/plugins/cache/<market>/<plugin>/``;
* the Hermes Agent plugin copy under ``${HERMES_HOME:-~/.hermes}/plugins/``
  (the directory named after the plugin, or any whose ``plugin.yaml``
  declares that name).

A copy is v4-capable when its ``core/laya_install_journal.py`` declares
``VERSION = 4`` or later. An unknown layout, an unreadable record or any
older copy leaves the journal at v3; new code reads and CAS-writes v3
unchanged and no longer re-checks its QMD inventory, so search keeps serving.

Lock order is the active upgrade's prefix: install lock, then the project's
route operation lock. The provider lane is not taken: conversion writes only
the journal file, in one SQLite transaction, and changes no store, route,
ledger or attestation. Readers see v3 or v4, never a mixture; a crash before
COMMIT is rolled back by the next opener.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from laya_install_journal import convert_to_v4, inspect_journal, journal_version

V4 = 4
WAITING = 'journal_v4_waiting_hosts'
BUSY = 'journal_v4_install_busy'
_VERSION_LINE = re.compile(r'^VERSION = (\d+)\s*$', re.MULTILINE)
_NAME_LINE = re.compile(r'^name:\s*["\']?([A-Za-z0-9._-]+)["\']?\s*$', re.MULTILINE)


def _executing_root():
    return Path(__file__).resolve().parent.parent


def reads_v4(plugin_root):
    """True when the copy's journal module writes (and so reads) v4."""
    try:
        text = (Path(plugin_root) / 'core' / 'laya_install_journal.py').read_text(
            encoding='utf-8')
    except (OSError, UnicodeError):
        return False
    found = _VERSION_LINE.findall(text)
    return len(found) == 1 and int(found[0]) >= V4


def _hermes_homes(home, explicit):
    homes = [home / '.hermes']
    configured = None if explicit else os.environ.get('HERMES_HOME')
    if configured:
        homes.append(Path(configured).expanduser())
    return homes


def _hermes_copies(plugin, home, explicit):
    """Hermes plugin copies of ``plugin``; raises when the plugin dir is unreadable."""
    copies = set()
    for hermes in _hermes_homes(home, explicit):
        plugins = hermes / 'plugins'
        if not plugins.exists() and not plugins.is_symlink():
            continue
        try:
            children = sorted(plugins.iterdir())
        except OSError as exc:
            raise ValueError('hermes_install_unreadable') from exc
        for child in children:
            if not child.is_dir():
                continue
            if child.name == plugin:
                copies.add(str(child))
                continue
            try:
                declared = _NAME_LINE.findall((child / 'plugin.yaml').read_text(
                    encoding='utf-8'))
            except (OSError, UnicodeError):
                continue
            if declared[:1] == [plugin]:
                copies.add(str(child))
    return copies


def host_copies(root=None, home=None):
    """Every host copy of the executing plugin; raises when unprovable."""
    root = Path(root or _executing_root())
    explicit_home = home is not None
    home = Path(home or Path.home())
    caches = {'claude': home / '.claude' / 'plugins' / 'cache',
              'codex': home / '.codex' / 'plugins' / 'cache'}
    market = plugin = None
    for cache in caches.values():
        try:
            parts = root.relative_to(cache).parts
        except ValueError:
            continue
        if len(parts) == 3:
            market, plugin = parts[0], parts[1]
    if market is None:
        raise ValueError('host_layout_unknown')
    copies = {str(root)}
    record = home / '.claude' / 'plugins' / 'installed_plugins.json'
    if record.exists() or record.is_symlink():
        try:
            rows = json.loads(record.read_text(encoding='utf-8'))['plugins'].get(
                plugin + '@' + market, [])
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            raise ValueError('claude_install_record_unreadable') from exc
        if not isinstance(rows, list):
            raise ValueError('claude_install_record_unreadable')
        for row in rows:
            path = row.get('installPath') if isinstance(row, dict) else None
            if not isinstance(path, str):
                raise ValueError('claude_install_record_unreadable')
            copies.add(path)
    codex = caches['codex'] / market / plugin
    if codex.is_dir():
        copies.update(str(child) for child in codex.iterdir() if child.is_dir())
    # Hermes runs the same core against the same install journals.
    copies.update(_hermes_copies(plugin, home, explicit_home))
    return sorted(copies)


def hosts_ready(root=None, home=None):
    """(ready, reason): every host copy reads v4."""
    try:
        copies = host_copies(root, home)
    except ValueError as exc:
        return False, str(exc)
    if not all(reads_v4(copy) for copy in copies):
        return False, WAITING
    return True, ''


def convert_install(state, project_root, *, root=None, home=None):
    """Convert one install's journal when every host reads v4."""
    state = Path(state)
    journal = state / 'install-journal.sqlite'
    if journal_version(journal) == V4:
        return {'status': 'journal_v4_current'}
    ready, reason = hosts_ready(root, home)
    if not ready:
        return {'status': 'journal_v4_waiting', 'reason': reason}
    import laya_fresh_wiki_origin as fresh
    from laya_ledger_common import CutoverBlocked, _operation_lock
    from laya_product_install import InstallBlocked, _install_lock
    _ledger, marker, _route = fresh._paths(Path(project_root), journal)
    try:
        with _install_lock(state), _operation_lock(marker):
            if inspect_journal(journal)['state'] == 'pending':
                # A route switch is in flight; its own recovery settles it first.
                return {'status': 'journal_v4_waiting', 'reason': 'journal_switch_pending'}
            return convert_to_v4(journal)
    except (InstallBlocked, CutoverBlocked) as exc:
        if str(exc) in ('install_busy', 'cutover_operation_busy'):
            raise RuntimeError(BUSY) from exc
        raise
