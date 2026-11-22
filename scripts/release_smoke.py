#!/usr/bin/env python3
"""Post-release smoke of an installed MemVane on a separate machine's test account.

A maintenance tool (docs/release.md, Q1 and Q2); no product code imports it.
It runs on the operator's machine, reaches the smoke account over SSH, and
judges only what it reads back: event rows (counts, reasons, times, card and
prompt hashes), file hashes and process lists. It never runs a product entry
point and never answers a host's permission or trust prompt.

  release_smoke.py account-prep --remote USER@HOST --out DIR
  release_smoke.py prepare  --remote USER@HOST --commit SHA --out DIR [--learning]
  release_smoke.py identity --remote USER@HOST --out DIR --commit SHA --installed PATH
  release_smoke.py delta    --remote USER@HOST --out DIR
  release_smoke.py send     --remote USER@HOST --out DIR --host claude|codex --prompt P1
  release_smoke.py model-url --remote USER@HOST --out DIR
  release_smoke.py verdict  --remote USER@HOST --out DIR --session claude=ID --session codex=ID

``--remote`` has no default. Statuses are PASS, FAIL, BLOCKED and NOT RUN; a
missing or partial result is never PASS. Times come from the rows' ``ts``.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PASS, FAIL, BLOCKED, NOT_RUN = 'PASS', 'FAIL', 'BLOCKED', 'NOT RUN'
HOSTS = ('claude', 'codex')
# A shell that carries any of these runs hooks as a sandbox or a test: the run is void.
FORBIDDEN_ENV = ('QMD_SANDBOX', 'QMD_SETUP_GUARD_FIXTURE', 'QMD_FORCE_DEAD_PIDS',
                 'CLAUDE_HEADLESS', 'CLAUDE_SANDBOX', 'CODEX_SANDBOX')
# The only fields copied from the smoke account's event log.
EVENT_FIELDS = ('event', 'ts', 'ms', 'host', 'session', 'install', 'pluginVersion', 'outcome',
                'reason', 'notes', 'semantic', 'candidates', 'cards', 'prompt', 'queryChunks',
                'sessionExcluded', 'selection', 'stage', 'pinMs', 'lexicalMs', 'stillCurrentMs',
                'generation', 'files')
AGED_STAGES = ('pinMs', 'lexicalMs', 'stillCurrentMs')
AGED_STAGE_LIMIT_MS = 50
SELECTED_ZERO = 'laya_selector_selected_zero'


# -- judging: pure functions over copied rows -----------------------------------

def item(ident, status, note='', **values):
    return {'id': ident, 'status': status, 'note': note, **values}


def seconds_between(first, second):
    """Seconds from row ``first`` to row ``second`` by their ``ts``; None when unreadable."""
    try:
        begin, end = (_instant(row['ts']) for row in (first, second))
    except (KeyError, TypeError, ValueError):
        return None
    return round((end - begin).total_seconds(), 3)


_TS = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$')


def _instant(value):
    """An event ``ts`` as an aware datetime (any fraction length; no zone means UTC)."""
    found = _TS.match(str(value))
    if not found:
        raise ValueError('ts')
    base, fraction, zone = found.groups()
    zone = '+00:00' if zone in (None, 'Z') else zone if ':' in zone else zone[:3] + ':' + zone[3:]
    return datetime.datetime.fromisoformat('%s.%s%s' % (base, ((fraction or '') + '000000')[:6], zone))


def _rows(events, name, session=None):
    return [row for row in events if row.get('event') == name
            and (session is None or row.get('session') == session)]


def _prompt_rows(events, session, prompt):
    return [row for row in _rows(events, 'recall', session)
            if isinstance(row.get('prompt'), dict) and row['prompt'].get('sha256') == prompt['sha256']]


def _has_card(row, card):
    return any(isinstance(entry, dict) and entry.get('path') == card['path']
               and entry.get('sha') == card['sha256'] for entry in row.get('cards') or [])


def _has_path(row, card):
    return any(isinstance(entry, dict) and entry.get('path') == card['path']
               for entry in row.get('cards') or [])


def _card(plan, role):
    slug = plan['roles'][role]
    return next(card for card in plan['cards'] if card['slug'] == slug)


def _selected_zero(row):
    return row.get('outcome') == 'empty' and (row.get('reason') == SELECTED_ZERO
                                              or SELECTED_ZERO in (row.get('notes') or []))


def _recall_fault(row, card, *, chunks=None):
    """Why a prompt's recall row does not pass; None when it does."""
    if row.get('semantic') != 'complete':
        return 'semantic %s' % row.get('semantic')
    if not isinstance(row.get('candidates'), int) or row['candidates'] < 1:
        return 'no candidates'
    if row.get('outcome') == 'served':
        if not _has_card(row, card):
            return 'served without the target card hash'
    elif not _selected_zero(row):
        return 'outcome %s/%s' % (row.get('outcome'), row.get('reason'))
    if chunks is not None and not (isinstance(row.get('queryChunks'), int)
                                   and row['queryChunks'] >= chunks):
        return 'queryChunks %s' % row.get('queryChunks')
    return None


def host_reached(events, session):
    """A SessionStart of this session reached the fixture install (an ``update-queued`` row)."""
    return bool(session) and bool(_rows(events, 'update-queued', session))


def judge_recall(plan, events, host, session):
    ident = 'RS-RECALL-' + host
    faults, values = [], {}
    for name, role, chunks in (('P1', 'short', None), ('P2', 'long', 2)):
        rows = _prompt_rows(events, session, plan['prompts'][name])
        if not rows:
            faults.append('%s: no recall row' % name)
            continue
        fault = _recall_fault(rows[0], _card(plan, role), chunks=chunks)
        if fault:
            faults.append('%s: %s' % (name, fault))
        values[name + 'Ms'] = rows[0].get('ms')
    return item(ident, FAIL if faults else PASS, '; '.join(faults), **values)


def judge_dedup(plan, events, session):
    rows = _prompt_rows(events, session, plan['prompts']['P1'])
    if len(rows) < 2:
        return item('RS-DEDUP', FAIL, 'P1 was not recalled twice in the session')
    first, second = rows[0], rows[1]
    excluded = second.get('sessionExcluded')
    repeated = [entry for entry in first.get('cards') or []
                if isinstance(entry, dict) and entry in (second.get('cards') or [])]
    if repeated:
        return item('RS-DEDUP', FAIL, 'the second recall repeats a card of the first')
    if not isinstance(excluded, int) or excluded < 1:
        return item('RS-DEDUP', FAIL, 'sessionExcluded %s' % excluded)
    return item('RS-DEDUP', PASS, sessionExcluded=excluded)


def judge_edit(plan, events, host, session):
    ident = 'RS-EDIT-' + host
    if not _rows(events, 'posttool', session):
        return item(ident, FAIL, 'no posttool row (the edit hook did not reach the install)')
    edits = _rows(events, 'recall.edit', session)
    if not edits:
        return item(ident, FAIL, 'no recall.edit row')
    last = edits[-1]
    if last.get('outcome') not in ('served', 'empty') or (last.get('outcome') == 'empty'
                                                           and not last.get('reason')):
        return item(ident, FAIL, 'recall.edit %s/%s' % (last.get('outcome'), last.get('reason')))
    if not plan.get('learning'):
        return item(ident, PASS, 'learning.capture NOT RUN (learning off)')
    captures = _rows(events, 'learning.capture', session)
    if not captures or not (captures[-1].get('outcome') == 'stored' or captures[-1].get('reason')):
        return item(ident, FAIL, 'no learning.capture row with a result')
    return item(ident, PASS, 'learning.capture ' + str(captures[-1].get('outcome')))


def judge_sync(plan, events, sessions):
    change = plan.get('delta')
    if not change:
        return item('RS-SYNC', NOT_RUN, 'the change was not applied')
    if not change.get('afterTs'):
        # Without it a sync pass of the onboarding would count as the pass after the change.
        return item('RS-SYNC', NOT_RUN, 'the time of the change was not recorded')
    asked = [row for session in sessions.values() if session
             for row in _prompt_rows(events, session, plan['prompts']['P4'])]
    if not asked:
        return item('RS-SYNC', FAIL, 'P4: no recall row')
    row = asked[0]
    before = [entry for entry in events if seconds_between(entry, row) is not None
              and seconds_between(entry, row) >= 0]
    passes = [entry for entry in before if entry.get('event') == 'wake.sync'
              and _since_change(entry, events, plan)]
    if not passes or passes[-1].get('outcome') != 'indexed':
        return item('RS-SYNC', FAIL, 'wake.sync %s' % (passes[-1].get('outcome') if passes else 'absent'))
    if not [entry for entry in before if entry.get('event') == 'index.publish'
            and _since_change(entry, events, plan)]:
        return item('RS-SYNC', FAIL, 'no index.publish after the change')
    if row.get('outcome') != 'served':
        return item('RS-SYNC', PASS, 'cards NOT RUN (P4 %s/%s)' % (row.get('outcome'), row.get('reason')))
    added, after, deleted = change['added'], change['changed']['after'], change['deleted']
    if not (_has_card(row, added) and _has_card(row, after)):
        return item('RS-SYNC', FAIL, 'P4 served without the added and changed card hashes')
    if _has_path(row, deleted):
        return item('RS-SYNC', FAIL, 'P4 served the deleted card')
    return item('RS-SYNC', PASS)


def _since_change(entry, events, plan):
    """``entry`` is later than the change (the newest row read right after it was committed)."""
    gap = seconds_between({'ts': plan['delta']['afterTs']}, entry)
    return gap is not None and gap > 0


def judge_model_url(record):
    if not record:
        return item('RS-MODELURL', NOT_RUN, 'not fetched')
    if record.get('state') == 'unreachable':
        return item('RS-MODELURL', BLOCKED, 'network unreachable')
    if record.get('state') == 'not_found':
        return item('RS-MODELURL', FAIL, 'the pinned revision answered 404')
    if record.get('sha256') != record.get('expected'):
        return item('RS-MODELURL', FAIL, 'sha256 differs from MODEL_SHA256')
    return item('RS-MODELURL', PASS, size=record.get('size'))


def judge_identity(record):
    if not record:
        return item('RS-ID', NOT_RUN, 'not compared')
    if record.get('different') or len(record.get('versions') or []) != 1:
        return item('RS-ID', FAIL, '%d files differ, versions %s'
                    % (len(record.get('different') or []), record.get('versions')))
    return item('RS-ID', PASS, files=record.get('files'))


def _p50(rows, field):
    values = [row[field] for row in rows if type(row.get(field)) in (int, float)]
    return round(statistics.median(values), 3) if values else None


def judge_aged(record):
    """Q1: the long-lived install before and after the update."""
    if not record or not record.get('after'):
        return item('Q1 RS-AGED', NOT_RUN, 'no rows after the update')
    faults, values = [], {}
    recalls = [row for row in record['after'] if row.get('event') == 'recall']
    for field in AGED_STAGES:
        value = _p50(recalls, field)
        values[field] = value
        if value is None:
            faults.append('%s: no samples' % field)
        elif value > AGED_STAGE_LIMIT_MS:
            faults.append('%s p50 %s ms' % (field, value))
    passes = {when: [row for row in record.get(when) or [] if row.get('event') == 'wake.sync']
              for when in ('before', 'after')}
    if not [row for row in passes['after'] if row.get('outcome') == 'indexed']:
        faults.append('the edit was not indexed')
    before, after = (_p50(passes[when], 'ms') for when in ('before', 'after'))
    values.update(syncMsBefore=before, syncMsAfter=after)
    if before is not None and after is not None and after >= before:
        faults.append('sync pass ms %s -> %s' % (before, after))
    stored, live = record.get('storedDev'), record.get('liveDev')
    values.update(storedDev=stored, liveDev=live)
    note = '; '.join(faults)
    if stored is None or live is None or stored == live:
        note = (note + '; ' if note else '') + 'dev NOT RUN (stored dev equals live dev)'
    return item('Q1 RS-AGED', FAIL if faults else PASS, note, **values)


def judge(plan, events, sessions, *, model_url=None, identity=None, aged=None):
    """Every item of the post-release table for one run."""
    if plan.get('forbiddenEnv'):
        note = 'void: the remote shell carries ' + ', '.join(plan['forbiddenEnv'])
        names = (['RS-ID', 'RS-MODELURL', 'RS-HOSTENV', 'RS-ONBOARD', 'RS-DEDUP', 'RS-SYNC',
                  'Q1 RS-AGED'] + ['RS-%s-%s' % (kind, host) for kind in ('RECALL', 'EDIT')
                                   for host in HOSTS])
        return {'valid': False, 'items': [item(name, NOT_RUN, note) for name in names]}
    reached = {host: host_reached(events, sessions.get(host)) for host in HOSTS}
    counts = {host: {name: len(_rows(events, name, sessions.get(host)))
                     for name in ('update-queued', 'recall', 'gate', 'posttool', 'recall.edit')}
              for host in HOSTS}
    silent = [host for host in HOSTS if reached[host] and not counts[host]['recall']]
    items = [judge_identity(identity), judge_model_url(model_url),
             item('RS-HOSTENV', FAIL if silent else PASS,
                  'no recall hook on ' + ', '.join(silent) if silent else '', reached=counts),
             item('RS-ONBOARD', PASS) if [row for row in _rows(events, 'index.publish')
                                          if row.get('outcome') == 'published']
             else item('RS-ONBOARD', FAIL, 'no index.publish')]
    blocked = 'host_env: no SessionStart row of the session (start it after the onboarding)'
    for host in HOSTS:
        session = sessions.get(host)
        if not reached[host]:
            items += [item('RS-RECALL-' + host, BLOCKED, blocked),
                      item('RS-EDIT-' + host, BLOCKED, blocked)]
            continue
        items += [judge_recall(plan, events, host, session), judge_edit(plan, events, host, session)]
    items.append(judge_dedup(plan, events, sessions['claude']) if reached['claude']
                 else item('RS-DEDUP', BLOCKED, blocked))
    items.append(judge_sync(plan, events, {host: sessions.get(host) for host in HOSTS if reached[host]})
                 if any(reached.values()) else item('RS-SYNC', BLOCKED, blocked))
    items.append(judge_aged(aged))
    return {'valid': True, 'items': items}


def gate_table(result):
    """The post-release table of the run record (docs/templates/release-gate-run.md)."""
    lines = ['| ID | 결과 | 비고 |', '|---|---|---|']
    for row in result['items']:
        lines.append('| %s | %s | %s |' % (row['id'], row['status'], row['note'].replace('|', '/')))
    if not result['valid']:
        lines.append('')
        lines.append('이 실행은 무효다.')
    return '\n'.join(lines) + '\n'


# -- the remote account -----------------------------------------------------------

class Remote:
    """Commands and files of the smoke account over SSH (key access, no prompt)."""

    def __init__(self, address):
        if not address or address.startswith('-'):
            raise ValueError('--remote USER@HOST is required')
        self.address = address

    def run(self, command, *, stdin=None, timeout=120):
        """(code, stdout, stderr) of ``command`` in the account's login shell."""
        done = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', self.address,
                               'zsh -lic ' + shlex.quote(command)], input=stdin, capture_output=True,
                              text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr

    def put(self, local, remote, *, timeout=300):
        done = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(local),
                               '%s:%s' % (self.address, remote)], capture_output=True, text=True,
                              timeout=timeout)
        if done.returncode:
            raise RuntimeError('upload failed: ' + done.stderr.strip()[:200])


# Reads event logs on the smoke account and prints only the listed fields.
_COPY_EVENTS = r'''
import glob, json, os, sys
fields, sessions, hashes = json.loads(sys.argv[1]), set(json.loads(sys.argv[2])), set(json.loads(sys.argv[3]))
chosen = None
logs = sorted(glob.glob(os.path.expanduser('~/.local/state/memvane/v1/installs/*/events.jsonl')))
for path in logs:
    rows = []
    for line in open(path, errors='replace'):
        try: row = json.loads(line)
        except ValueError: continue
        if isinstance(row, dict): rows.append(row)
    prompt = lambda row: (row.get('prompt') or {}).get('sha256') if isinstance(row.get('prompt'), dict) else None
    if any(row.get('session') in sessions or prompt(row) in hashes for row in rows):
        chosen = rows
        break
for row in chosen or []:
    print(json.dumps({key: row[key] for key in fields if key in row}, ensure_ascii=False))
'''


def copy_events(remote, plan, sessions):
    """Rows of the fixture install (the one a plan session or prompt reached), listed fields only."""
    hashes = [prompt['sha256'] for prompt in plan['prompts'].values()]
    command = 'python3 -B - %s %s %s' % tuple(shlex.quote(json.dumps(value)) for value in (
        list(EVENT_FIELDS), [value for value in sessions.values() if value], hashes))
    code, out, err = remote.run(command, stdin=_COPY_EVENTS, timeout=180)
    if code:
        raise RuntimeError('event copy failed: ' + err.strip()[:200])
    rows = []
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append({key: row[key] for key in EVENT_FIELDS if key in row})
    return rows


def forbidden_env(remote):
    code, out, _err = remote.run('env')
    if code:
        raise RuntimeError('the remote shell could not be read')
    names = {line.split('=', 1)[0] for line in out.splitlines() if '=' in line}
    return sorted(name for name in FORBIDDEN_ENV if name in names)


def _plan_path(out):
    return Path(out) / 'plan.json'


def _load_plan(out):
    return json.loads(_plan_path(out).read_text(encoding='utf-8'))


def _save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def rpath(path):
    """``path`` quoted for the account's shell; a leading ``~/`` still means its home."""
    if path == '~':
        return '"$HOME"'
    if path.startswith('~/'):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _ship(remote, tree, target):
    """Pack ``tree`` and unpack it as ``target`` on the smoke account."""
    target = target.rstrip('/')
    staged = target + '.tar'
    # The directory first: a new run has no parent for the upload yet.
    code, _out, err = remote.run('mkdir -p %s' % rpath(target))
    if code:
        raise RuntimeError('remote directory failed: ' + err.strip()[:200])
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / 'fixture.tar'
        with tarfile.open(archive, 'w') as packed:
            packed.add(str(tree), arcname='.')
        remote.put(archive, staged)
    code, _out, err = remote.run('tar -xf %s -C %s && rm -f %s' % (
        rpath(staged), rpath(target), rpath(staged)))
    if code:
        raise RuntimeError('unpack failed: ' + err.strip()[:200])


TREE = 'fixture-tree'
_GIT = ('git -c user.name=Smoke -c user.email=smoke@example.invalid -c commit.gpgsign=false '
        '-c core.hooksPath=/dev/null')


def prepare(remote, out, commit, *, learning=False, target=None, build=None):
    """The record directory, the forbidden-variable check and the fixture on the smoke account."""
    from tests.support import smoke_fixture
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    found = forbidden_env(remote)
    # The tree stays in the record directory: the change is applied to these bytes.
    tree = out / TREE
    if _plan_path(out).is_file() and tree.is_dir():
        # A second run with the same record directory ships the same fixture again.
        plan = _load_plan(out)
        if plan.get('shipped'):
            raise RuntimeError('this record directory already holds a shipped fixture')
    else:
        if tree.exists():
            shutil.rmtree(str(tree))  # A build that stopped before its plan was written.
        plan = (build or smoke_fixture.build)(tree, learning=learning)
    target = target or plan.get('remoteFixture') or '~/memvane-smoke/%s/fixture' % plan['runId']
    plan.update(commit=commit, remoteFixture=target, forbiddenEnv=found, shipped=False)
    _save(_plan_path(out), plan)
    for name, prompt in plan['prompts'].items():
        (out / ('prompt-%s.txt' % name)).write_text(prompt['text'], encoding='utf-8')
    if not found:
        _ship(remote, tree, target)
        # Committed only when something is staged: a run cut after the commit is run again.
        code, _out, err = remote.run(
            'cd %s && %s init -q -b main && %s add -A && { %s diff --cached --quiet || %s commit -qm fixture; }'
            % (rpath(target), _GIT, _GIT, _GIT, _GIT))
        if code:
            raise RuntimeError('git init failed: ' + err.strip()[:200])
        plan['shipped'] = True
        _save(_plan_path(out), plan)
    return plan


# The time of the newest event row on the smoke account.
_LAST_TS = r'''
import glob, json, os
last = ''
for path in glob.glob(os.path.expanduser('~/.local/state/memvane/v1/installs/*/events.jsonl')):
    for line in open(path, errors='replace'):
        try: row = json.loads(line)
        except ValueError: continue
        if isinstance(row, dict) and isinstance(row.get('ts'), str) and row['ts'] > last:
            last = row['ts']
print(last)
'''


def last_event_time(remote):
    """The ``ts`` of the newest event row on the account, or None."""
    code, text, _err = remote.run('python3 -B -', stdin=_LAST_TS)
    return text.strip() or None if code == 0 else None


# Counts ``index.publish`` rows later than a time on the smoke account.
_PUBLISHED_AFTER = r'''
import glob, json, os, sys
mark, count = sys.argv[1], 0
for path in glob.glob(os.path.expanduser('~/.local/state/memvane/v1/installs/*/events.jsonl')):
    for line in open(path, errors='replace'):
        if 'index.publish' not in line: continue
        try: row = json.loads(line)
        except ValueError: continue
        if (isinstance(row, dict) and row.get('event') == 'index.publish'
                and row.get('outcome') == 'published' and str(row.get('ts', '')) > mark):
            count += 1
print(count)
'''


def apply_delta(remote, out, *, host=None, last_ts=None, pause=time.sleep, publish_reads=90, **sending):
    """Apply the RS-SYNC change to the remote fixture and record it in the plan.

    Only the three changed documents and cards are written or removed, and only
    those paths are staged: whatever else the project holds by then (the host's
    own files, an edit a prompt asked for) stays as it is. The newest event row
    on the account is read after the commit and kept as ``afterTs``; a sync pass
    counts for RS-SYNC only when it is later. Without a readable time nothing
    is changed. With ``host``, the refresh prompt is then sent to that host's
    session (nothing else wakes a sync after a change made outside a hook) and
    a publish later than the change is waited for (``syncSeen``).
    """
    from tests.support import smoke_fixture
    out = Path(out)
    plan = _load_plan(out)
    tree = out / TREE
    if not (last_ts or last_event_time(remote)):
        raise RuntimeError('no event time can be read on the account: the change is not applied')
    after = smoke_fixture.delta(tree, plan)
    change = after['delta']
    target = plan['remoteFixture'].rstrip('/')
    written = [change['added']['source'], change['added']['file'],
               change['changed']['after']['source'], change['changed']['after']['file']]
    removed = [change['deleted']['source'], change['deleted']['file']]
    for name in written:
        remote.put(tree / name, '%s/%s' % (target, name))
    code, _out, err = remote.run(
        'cd %s && %s rm -q --ignore-unmatch -- %s && %s add -- %s && '
        '{ %s diff --cached --quiet || %s commit -qm change; }'
        % (rpath(target), _GIT, ' '.join(shlex.quote(name) for name in removed),
           _GIT, ' '.join(shlex.quote(name) for name in written), _GIT, _GIT))
    if code:
        raise RuntimeError('change commit failed: ' + err.strip()[:200])
    change['afterTs'] = last_ts or last_event_time(remote)
    _save(_plan_path(out), after)
    for name in ('P4', 'PS'):
        (out / ('prompt-%s.txt' % name)).write_text(after['prompts'][name]['text'], encoding='utf-8')
    if host is not None:
        asked = send(remote, out, host, 'PS', pause=pause, **sending)
        seen = False
        if sent(asked):
            count = 'python3 -B - %s' % shlex.quote(change['afterTs'])
            for _ in range(publish_reads):
                code, text, _err = remote.run(count, stdin=_PUBLISHED_AFTER)
                if code == 0 and text.strip().isdigit() and int(text.strip()) > 0:
                    seen = True
                    break
                pause(2)
        change['syncRequested'], change['syncSeen'] = sent(asked), seen
        _save(_plan_path(out), after)
    return after


# Counts the event rows of a prompt hash on the smoke account.
_COUNT_PROMPT = r'''
import glob, json, os, sys
wanted, count = sys.argv[1], 0
for path in glob.glob(os.path.expanduser('~/.local/state/memvane/v1/installs/*/events.jsonl')):
    for line in open(path, errors='replace'):
        if wanted not in line: continue
        try: row = json.loads(line)
        except ValueError: continue
        if isinstance(row, dict) and isinstance(row.get('prompt'), dict) and row['prompt'].get('sha256') == wanted:
            count += 1
print(count)
'''
# The prompts no hook row can confirm: the project is not onboarded when P0 is sent.
UNCONFIRMED_BY_ROW = ('P0',)


def session_name(host):
    """The multiplexer session of a host: the operator starts it under this name."""
    return 'smoke-' + host


def send(remote, out, host, name, *, screen=None, pause=time.sleep, paste_reads=30, row_reads=45):
    """Paste one prompt into the host's interactive session, then press Enter.

    Every multiplexer command names window 0 (``-p 0``): an old ``screen``
    ignores a windowless ``-X`` and still exits 0. The paste counts as arrived
    only when the window's hardcopy changed and then stopped changing and, for
    a short prompt, shows the prompt's own token; only then is Enter sent, on
    its own. For a prompt a hook can see, the turn is
    confirmed by the event row of its hash. Returns what was observed:
    ``pasted``, ``submitted`` and ``row`` (None when no row can confirm it).
    Only the prompt is typed: a permission or trust screen is the operator's.
    """
    plan = _load_plan(out)
    prompt = plan['prompts'][name]
    session = shlex.quote(screen or session_name(host))
    base = plan['remoteFixture'].rstrip('/').rsplit('/', 1)[0]
    staged, shot = '%s/prompt-%s.txt' % (base, name), '%s/hardcopy-%s.txt' % (base, host)
    remote.put(Path(out) / ('prompt-%s.txt' % name), staged)
    control = 'screen -S %s -p 0 -X ' % session

    def hardcopy():
        code, text, _err = remote.run('rm -f %s; %shardcopy %s && cksum < %s' % (
            rpath(shot), control, rpath(shot), rpath(shot)))
        return text.strip() if code == 0 else ''
    result = {'host': host, 'prompt': name, 'sha256': prompt['sha256'], 'pasted': False,
              'submitted': False, 'row': None}
    before = hardcopy()
    if not before:
        return result
    # Rows of this prompt before it is sent: the turn is confirmed by one more.
    count = 'python3 -B - %s' % shlex.quote(prompt['sha256'])

    def rows():
        code, text, _err = remote.run(count, stdin=_COUNT_PROMPT)
        return int(text.strip()) if code == 0 and text.strip().isdigit() else None
    start = rows() or 0
    remote.run('%sreadbuf %s && %spaste .' % (control, rpath(staged), control))
    last = None
    for _ in range(paste_reads):
        pause(1)
        now = hardcopy()
        if now and now != before and now == last:
            result['pasted'] = True
            break
        last = now
    mark = prompt.get('mark')
    if result['pasted'] and mark:
        # A window that changes by itself is not a paste: the prompt's own token is on it.
        # Bytes, not characters: a hardcopy is not valid text in every locale.
        code, text, _err = remote.run("LC_ALL=C tr -d '\\n' < %s | LC_ALL=C grep -cF %s"
                                      % (rpath(shot), shlex.quote(mark)))
        result['pasted'] = code == 0 and text.strip().isdigit() and int(text.strip()) > 0
    if not result['pasted']:
        return result
    code, _text, _err = remote.run("%sstuff $'\\r'" % control)
    result['submitted'] = code == 0
    if not result['submitted'] or name in UNCONFIRMED_BY_ROW:
        return result
    result['row'] = False
    for _ in range(row_reads):
        seen = rows()
        if seen is not None and seen > start:
            result['row'] = True
            break
        pause(2)
    return result


def sent(result):
    """A send succeeded: the paste arrived, Enter was sent, and no expected row is missing."""
    return bool(result['pasted'] and result['submitted'] and result['row'] is not False)


def _model_constants():
    text = (ROOT / 'memvane/provider/embed_runtime.py').read_text(encoding='utf-8')

    def assigned(name):
        """The first quoted literal of the assignment (a trailing comment is not part of it)."""
        for line in text.splitlines():
            found = re.match(re.escape(name) + r" = '([^']*)'", line)
            if found:
                return found.group(1)
        raise RuntimeError(name + ' not found')
    return (assigned('MODEL_URL') + assigned('MODEL_REVISION') + '/' + assigned('MODEL_NAME'),
            assigned('MODEL_SHA256'))


def model_url(remote):
    """Fetch the pinned model address on the smoke account; size and sha256 only."""
    url, expected = _model_constants()
    code, out, _err = remote.run('curl -sIL -o /dev/null -w "%%{http_code}" %s' % shlex.quote(url),
                                 timeout=120)
    if code:
        return {'state': 'unreachable', 'url': url, 'expected': expected}
    if out.strip() == '404':
        return {'state': 'not_found', 'url': url, 'expected': expected}
    code, out, _err = remote.run(
        'f=$(mktemp) && curl -sfL -o "$f" %s && wc -c < "$f" && shasum -a 256 "$f" | cut -d" " -f1; '
        'r=$?; rm -f "$f"; exit $r' % shlex.quote(url), timeout=1800)
    lines = out.split()
    if code or len(lines) < 2:
        return {'state': 'unreachable', 'url': url, 'expected': expected}
    return {'state': 'fetched', 'url': url, 'expected': expected, 'size': int(lines[0]),
            'sha256': lines[1]}


# Files a host writes into its copy of the plugin; not part of the release commit.
HOST_METADATA = ('.claude-plugin/installed.json', '.orphaned_at', '.in_use')


def installed_identity(remote, commit, installed, *, tree=None):
    """Compare the installed plugin files with the blobs of ``commit``.

    ``tree`` (tests) maps path -> sha256 of the commit; by default it is read
    from this checkout with git.
    """
    if tree is None:
        listed = subprocess.run(['git', '-C', str(ROOT), 'ls-tree', '-r', '--name-only', commit],
                                capture_output=True, text=True, timeout=60, check=True).stdout.split('\n')
        tree = {}
        for name in filter(None, listed):
            blob = subprocess.run(['git', '-C', str(ROOT), 'show', '%s:%s' % (commit, name)],
                                  capture_output=True, timeout=60, check=True).stdout
            tree[name] = hashlib.sha256(blob).hexdigest()
    code, out, err = remote.run(
        'cd %s && find . -type f ! -path "./.git/*" ! -name "*.pyc" -print0 | xargs -0 shasum -a 256'
        % rpath(installed), timeout=300)
    if code:
        raise RuntimeError('installed files could not be read: ' + err.strip()[:200])
    found = {}
    for line in out.splitlines():
        digest, _, name = line.partition('  ')
        if name.startswith('./'):
            found[name[2:]] = digest
    # A host entry is a file or a directory of files (``.in_use/<pid>``).
    for name in [name for name in found
                 if any(name == entry or name.startswith(entry + '/') for entry in HOST_METADATA)]:
        del found[name]
    different = sorted(name for name in set(tree) | set(found) if tree.get(name) != found.get(name))
    versions = sorted({json.loads(text)['version'] for text in [remote.run(
        'cat %s/package.json' % rpath(installed))[1]] if text.strip()})
    return {'commit': commit, 'installed': installed, 'files': len(found),
            'different': different, 'versions': versions}


def account_prep(remote):
    """Read-only checks of the smoke account (A1 to A3, A5); the rest is the user's to confirm."""
    lines = []
    # (label, command, keep the output). A login status may name the account: only its result is kept.
    for label, command, shown in (('A1 ssh', 'true', True), ('A2 claude', 'claude --version', True),
                                  ('A2 claude login', 'claude auth status', False),
                                  ('A2 codex', 'codex --version', True),
                                  ('A2 codex login', 'codex login status', False),
                                  ('A3 screen', 'command -v screen', True),
                                  ('A3 screen version', 'screen -v', True),
                                  ('A5 node', 'node --version', True)):
        try:
            code, out, _err = remote.run(command, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            code, out = 255, ''
        if label == 'A3 screen version':
            code = 0 if out.strip() else code  # ``screen -v`` prints its version and exits 1.
        text = out.strip().splitlines()[0][:80] if shown and out.strip() else ''
        lines.append(('%s: %s %s' % (label, 'ok' if code == 0 else 'failed', text)).rstrip())
    lines += ['A4 codex hook trust: the user confirms in the account', 'A6 teacher calls: the user confirms',
              'A7 reboot: the user confirms']
    return lines


def verdict(remote, out, sessions, *, aged=None):
    """Copy the fixture install's rows, judge them and write the gate files."""
    out = Path(out)
    plan = _load_plan(out)
    events = [] if plan.get('forbiddenEnv') else copy_events(remote, plan, sessions)
    with (out / 'events-fixture.jsonl').open('w', encoding='utf-8') as target:
        for row in events:
            target.write(json.dumps(row, ensure_ascii=False) + '\n')

    def record(name):
        path = out / name
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    result = judge(plan, events, sessions, model_url=record('model-url.json'),
                   identity=record('identity.json'), aged=aged or record('aged.json'))
    gate = {'commit': plan.get('commit'), 'sessions': sessions, **result}
    _save(out / 'gate.json', gate)
    (out / 'gate-after-release.md').write_text(gate_table(result), encoding='utf-8')
    return gate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)

    def command(name):
        sub = commands.add_parser(name)
        sub.add_argument('--remote', required=True, help='USER@HOST of the smoke account (no default)')
        sub.add_argument('--out', required=True, help='the private record directory of this run')
        return sub
    command('account-prep')
    sub = command('identity')
    sub.add_argument('--commit', required=True)
    sub.add_argument('--installed', required=True, help='the installed plugin directory on the account')
    sub = command('prepare')
    sub.add_argument('--commit', required=True)
    sub.add_argument('--learning', action='store_true')
    sub = command('delta')
    sub.add_argument('--host', required=True, choices=HOSTS,
                     help='the host session that is asked to refresh the index after the change')
    sub = command('send')
    sub.add_argument('--host', required=True, choices=HOSTS)
    sub.add_argument('--prompt', required=True)
    command('model-url')
    sub = command('verdict')
    sub.add_argument('--session', action='append', default=[], metavar='HOST=ID')
    args = parser.parse_args(argv)
    remote = Remote(args.remote)
    if args.command == 'account-prep':
        lines = account_prep(remote)
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / 'account-prep.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print('\n'.join(lines))
        return 0
    if args.command == 'identity':
        record = installed_identity(remote, args.commit, args.installed)
        _save(Path(args.out) / 'identity.json', record)
        print(json.dumps(judge_identity(record)))
        return 0
    if args.command == 'prepare':
        plan = prepare(remote, args.out, args.commit, learning=args.learning)
        print(json.dumps({'fixture': plan['remoteFixture'], 'forbiddenEnv': plan['forbiddenEnv']}))
        return 1 if plan['forbiddenEnv'] else 0
    if args.command == 'delta':
        after = apply_delta(remote, args.out, host=args.host)
        if not after['delta'].get('syncSeen'):
            print('BLOCKED (host_env): no index publish was observed after the change', file=sys.stderr)
            return 1
        return 0
    if args.command == 'send':
        result = send(remote, args.out, args.host, args.prompt)
        print(json.dumps(result))
        if not sent(result):
            print('BLOCKED (host_env): the prompt was not observed in the session', file=sys.stderr)
            return 1
        return 0
    if args.command == 'model-url':
        record = model_url(remote)
        _save(Path(args.out) / 'model-url.json', record)
        print(json.dumps(judge_model_url(record)))
        return 0
    sessions = dict(pair.split('=', 1) for pair in args.session)
    gate = verdict(remote, args.out, {host: sessions.get(host) for host in HOSTS})
    print(gate_table(gate))
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
