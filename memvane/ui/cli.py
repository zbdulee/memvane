"""``entry/run.py ui {open,status,stop,serve,pref}``: the dashboard's command line.

Every command prints one JSON object on stdout. ``open`` never prints a
one-time code in ``url``: a code travels only in the URL handed to the
browser, or in ``manualUrl`` (120 s, one use) when the browser was not
opened (``--no-open``, a headless or test environment, or an opener failure).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from memvane.ui.types import CODE_TTL_SECONDS, MANUAL_CODE_TTL_SECONDS, TABS


def _print(value):
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _target_of(cwd):
    """The target id of the onboarded checkout holding ``cwd``, or None."""
    from memvane.diagnostics import api as diagnostics
    from memvane.journal import api as journal
    from memvane.ui.types import target_id
    try:
        decision = journal.select_route(str(Path(cwd).resolve()), action='recall')
    except Exception:  # noqa: BLE001 - an unclassified folder opens the overview
        return None
    if not isinstance(decision, dict) or decision.get('state') not in ('candidate', 'worktree', 'preview'):
        return None
    install = diagnostics.install_of(decision)
    if install is None:
        return None
    if decision.get('state') == 'worktree':
        root = decision.get('worktreeProjectRoot') or decision.get('projectRoot')
        return target_id(install, root, 'worktree') if isinstance(root, str) else install
    return install


def _code(server, ttl):
    from memvane.ui import local_client
    status, value = local_client.control(server, 'POST', '/api/internal/code', {'ttlSeconds': ttl})
    if status != 200 or not isinstance(value, dict) or not isinstance(value.get('code'), str):
        raise local_client.LocalClientError('ui_code_refused')
    return value['code']


def _session_start_refusal(home):
    """Why a session start must not open the dashboard (fail-closed opt-in gate), or None."""
    from memvane.ui import platform_io, preferences
    prefs, state = preferences.load(home)
    if state != 'ok' or prefs.get('autoOpenOnSessionStart') is not True:
        return 'auto_open_off'
    blocked = platform_io.browser_blocker()
    if blocked is not None:
        return blocked
    record = preferences.auto_open_state(home)
    last = max((value for value in (record['lastAutoOpenAt'], record['lastUserClosedAt']) if value is not None),
               default=None)
    if last is not None and time.time() - last < prefs['autoOpenCooldownMinutes'] * 60:
        return 'cooldown'
    return None


def open_dashboard(*, cwd=None, tab='overview', no_open=False, port=0, from_session_start=False,
                   host=None, home=None, opener=None):
    """Start or reuse the server and open (or describe) the dashboard; returns the JSON result."""
    from memvane.ui import lifecycle, local_client, platform_io
    from memvane.ui.server import page_url
    opener = opener or platform_io.open_browser
    if from_session_start:
        refused = _session_start_refusal(home)
        if refused is not None:
            return {'status': 'ok', 'opened': False, 'reason': refused}
    target = _target_of(cwd) if cwd else None
    started = lifecycle.ensure_server(home, port=port)
    server, status = started['server'], started['status']
    result = {'status': 'ok', 'port': server['port'], 'reused': started['reused'],
              'replaced': started['replaced'], 'bundleVersion': server['bundleVersion'],
              'url': page_url(server['port'], target=target, tab=tab), 'opened': False}
    if from_session_start and status.get('activeClients'):
        payload = {'targetId': target}
        if host is not None:
            payload['host'] = host
        code, value = local_client.control(server, 'POST', '/api/internal/session-started', payload)
        result.update(sessionStarted=code == 200, delivered=(value or {}).get('delivered'))
        return result
    blocked = 'no_open' if no_open else platform_io.browser_blocker()
    if blocked is None:
        opened, reason = opener(page_url(server['port'], code=_code(server, CODE_TTL_SECONDS),
                                         target=target, tab=tab))
        if opened:
            result['opened'] = True
            if from_session_start:
                from memvane.ui import preferences
                preferences.record_auto_open(home, opened_at=time.time())
            return result
        if from_session_start:
            # A session start never prints a login link nobody asked for.
            result['reason'] = reason or 'open_failed'
            return result
        blocked = reason or 'open_failed'
    result.update(reason=blocked, manualUrlTtlSeconds=MANUAL_CODE_TTL_SECONDS,
                  manualUrl=page_url(server['port'], code=_code(server, MANUAL_CODE_TTL_SECONDS),
                                     target=target, tab=tab),
                  hint=('Open manualUrl in a browser within 120 seconds. Over SSH, forward the port first: '
                        f"ssh -L {server['port']}:127.0.0.1:{server['port']} <host>"))
    return result


def status(home=None):
    from memvane.ui import lifecycle, preferences
    prefs, pref_status = preferences.load(home)
    server, state = lifecycle.running(home)
    value = {'running': server is not None, 'preferences': prefs, 'preferencesStatus': pref_status}
    if state is not None:
        value['server'] = {key: state.get(key) for key in (
            'pid', 'port', 'bundleVersion', 'bundleRoot', 'startedAt', 'activeClients', 'lastSseClosedAt',
            'readOnly', 'idleSecondsRemaining')}
    return value


def stop(home=None):
    from memvane.ui import lifecycle
    return lifecycle.stop_server(home)


def update_preferences(changes, home=None):
    from memvane.ui import preferences
    return preferences.update(changes, home)


def _pref_changes(args):
    changes = {}
    if args.auto_open is not None:
        changes['autoOpenOnSessionStart'] = args.auto_open == 'on'
    if args.idle_minutes is not None:
        changes['idleShutdownMinutes'] = args.idle_minutes
    if args.cooldown_minutes is not None:
        changes['autoOpenCooldownMinutes'] = args.cooldown_minutes
    if args.read_only is not None:
        changes['readOnlyDefault'] = args.read_only == 'on'
    return changes


def _parser():
    parser = argparse.ArgumentParser(prog='entry/run.py ui', description='MemVane local web dashboard')
    commands = parser.add_subparsers(dest='command', required=True)
    opened = commands.add_parser('open', help='start or reuse the dashboard server and open it')
    opened.add_argument('--cwd', default=None)
    opened.add_argument('--tab', choices=TABS, default='overview')
    opened.add_argument('--no-open', action='store_true')
    opened.add_argument('--port', type=int, default=0)
    opened.add_argument('--from-session-start', action='store_true')
    opened.add_argument('--host', choices=('claude', 'codex', 'hermes', 'gemini'), default=None)
    commands.add_parser('status', help='the running server and the preferences')
    commands.add_parser('stop', help='stop the running server')
    served = commands.add_parser('serve', help='run the server in the foreground')
    served.add_argument('--port', type=int, default=0)
    served.add_argument('--read-only', action='store_true')
    served.add_argument('--spawned', action='store_true', help=argparse.SUPPRESS)
    pref = commands.add_parser('pref', help='show or change the preferences')
    pref.add_argument('--auto-open', choices=('on', 'off'))
    pref.add_argument('--idle-minutes', type=int)
    pref.add_argument('--cooldown-minutes', type=int)
    pref.add_argument('--read-only', choices=('on', 'off'))
    return parser


def _serve(args):
    from memvane.ui import server

    def announce(url):
        # Only a person at this terminal sees the one-time URL; never stdout of a detached child.
        if sys.stderr is not None and not args.spawned:
            print(f'[memvane] dashboard: {url} (one-time link, valid 120 s)', file=sys.stderr, flush=True)
    reason = server.serve(port=args.port, read_only=args.read_only, spawned=args.spawned,
                          announce=None if args.spawned else announce)
    if not args.spawned:
        _print({'status': 'stopped', 'reason': reason})
    return 0


def main(argv=None):
    from memvane.ui import lifecycle, local_client, platform_io, preferences
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == 'open':
            _print(open_dashboard(cwd=args.cwd or os.getcwd(), tab=args.tab, no_open=args.no_open,
                                  port=args.port, from_session_start=args.from_session_start, host=args.host))
            return 0
        if args.command == 'status':
            _print(status())
            return 0
        if args.command == 'stop':
            result = stop()
            _print(result)
            return 0 if result['stopped'] or result['how'] in ('not_running', 'stale_record_removed') else 1
        if args.command == 'serve':
            return _serve(args)
        changes = _pref_changes(args)
        if changes:
            _print({'preferences': update_preferences(changes)})
        else:
            prefs, state = preferences.load()
            _print({'preferences': prefs, 'status': state})
        return 0
    except (lifecycle.LifecycleError, local_client.LocalClientError, platform_io.UiStateRefused,
            preferences.PreferenceError, TimeoutError, OSError, ValueError) as exc:
        _print({'status': 'error', 'error': str(exc) or type(exc).__name__})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
