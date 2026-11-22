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
import statistics
import subprocess
import sys
import tarfile
import tempfile

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
    """``entry`` is not older than the change (the last row copied before it was applied)."""
    mark = plan['delta'].get('afterTs')
    if not mark:
        return True
    gap = seconds_between({'ts': mark}, entry)
    return gap is not None and gap >= 0


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
    blocked = 'host_env: no SessionStart row of the session'
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


def _ship(remote, tree, target):
    """Pack ``tree`` and unpack it as ``target`` on the smoke account."""
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / 'fixture.tar'
        with tarfile.open(archive, 'w') as packed:
            packed.add(str(tree), arcname='.')
        staged = target.rstrip('/') + '.tar'
        remote.put(archive, staged)
    code, _out, err = remote.run('mkdir -p %s && tar -xf %s -C %s && rm -f %s' % (
        shlex.quote(target), shlex.quote(staged), shlex.quote(target), shlex.quote(staged)))
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
    plan = (build or smoke_fixture.build)(tree, learning=learning)
    target = target or '~/memvane-smoke/%s/fixture' % plan['runId']
    plan.update(commit=commit, remoteFixture=target, forbiddenEnv=found)
    if not found:
        _ship(remote, tree, target)
        code, _out, err = remote.run(
            'cd %s && %s init -q -b main && %s add -A && %s commit -qm fixture'
            % (target, _GIT, _GIT, _GIT))
        if code:
            raise RuntimeError('git init failed: ' + err.strip()[:200])
    _save(_plan_path(out), plan)
    for name, prompt in plan['prompts'].items():
        (out / ('prompt-%s.txt' % name)).write_text(prompt['text'], encoding='utf-8')
    return plan


def apply_delta(remote, out, *, last_ts=None):
    """Apply the RS-SYNC change to the remote fixture and record it in the plan."""
    from tests.support import smoke_fixture
    out = Path(out)
    plan = _load_plan(out)
    tree = out / TREE
    after = smoke_fixture.delta(tree, plan)
    change = after['delta']
    _ship(remote, tree, plan['remoteFixture'])
    removed = ' '.join(shlex.quote(name) for name in (change['deleted']['source'],
                                                        change['deleted']['file']))
    code, _out, err = remote.run('cd %s && rm -f %s && %s add -A && %s commit -qm change'
                                 % (plan['remoteFixture'], removed, _GIT, _GIT))
    if code:
        raise RuntimeError('change commit failed: ' + err.strip()[:200])
    change['afterTs'] = last_ts
    _save(_plan_path(out), after)
    (out / 'prompt-P4.txt').write_text(after['prompts']['P4']['text'], encoding='utf-8')
    return after


def send(remote, out, host, name, *, screen=None):
    """Paste one prompt into the host's interactive session and press Enter.

    Only the prompt is typed. A permission or trust screen is left for the
    operator (and the Codex hook trust for the user).
    """
    plan = _load_plan(out)
    prompt = plan['prompts'][name]
    screen = screen or 'smoke-' + host
    staged = '%s/../prompt-%s.txt' % (plan['remoteFixture'], name)
    remote.put(Path(out) / ('prompt-%s.txt' % name), staged)
    code, _out, err = remote.run(
        'screen -S %s -X readbuf %s && screen -S %s -X paste . && screen -S %s -X stuff $\'\\r\''
        % (shlex.quote(screen), staged, shlex.quote(screen), shlex.quote(screen)))
    if code:
        raise RuntimeError('the session did not take the prompt: ' + err.strip()[:200])
    return {'host': host, 'prompt': name, 'sha256': prompt['sha256']}


def _model_constants():
    text = (ROOT / 'memvane/provider/embed_runtime.py').read_text(encoding='utf-8')

    def assigned(name):
        for line in text.splitlines():
            if line.startswith(name + ' = '):
                return line.split('=', 1)[1].strip()
        raise RuntimeError(name + ' not found')
    revision, name = (assigned(key).strip("'") for key in ('MODEL_REVISION', 'MODEL_NAME'))
    prefix = assigned('MODEL_URL').split('+')[0].strip().strip("'")
    return prefix + revision + '/' + name, assigned('MODEL_SHA256').strip("'")


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
        % shlex.quote(installed), timeout=300)
    if code:
        raise RuntimeError('installed files could not be read: ' + err.strip()[:200])
    found = {}
    for line in out.splitlines():
        digest, _, name = line.partition('  ')
        if name.startswith('./'):
            found[name[2:]] = digest
    for name in HOST_METADATA:
        found.pop(name, None)
    different = sorted(name for name in set(tree) | set(found) if tree.get(name) != found.get(name))
    versions = sorted({json.loads(text)['version'] for text in [remote.run(
        'cat %s/package.json' % shlex.quote(installed))[1]] if text.strip()})
    return {'commit': commit, 'installed': installed, 'files': len(found),
            'different': different, 'versions': versions}


def account_prep(remote):
    """Read-only checks of the smoke account (A1 to A3, A5); the rest is the user's to confirm."""
    lines = []
    for label, command in (('A1 ssh', 'true'), ('A2 claude', 'claude --version'),
                           ('A2 codex', 'codex --version'), ('A3 screen', 'command -v screen'),
                           ('A5 node', 'node --version')):
        try:
            code, out, _err = remote.run(command, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            code, out = 255, ''
        lines.append('%s: %s %s' % (label, 'ok' if code == 0 else 'failed', out.strip()[:80]))
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
    sub.add_argument('--last-ts')
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
        apply_delta(remote, args.out, last_ts=args.last_ts)
        return 0
    if args.command == 'send':
        print(json.dumps(send(remote, args.out, args.host, args.prompt)))
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
