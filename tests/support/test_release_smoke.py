"""scripts/release_smoke.py: verdict rules on synthetic rows, and the remote round trip with a local stand-in."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest import mock

from tests.support.case import FixtureCase, scaled
from tests.support import smoke_fixture

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('release_smoke', ROOT / 'scripts' / 'release_smoke.py')
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

FAKE_SCREEN = r'''#!__PYTHON__
import hashlib, json, os, sys, time
from pathlib import Path
STATE = Path('__STATE__')
args = sys.argv[1:]
session = args[args.index('-S') + 1]
window = args[args.index('-p') + 1] if '-p' in args else None
command, rest = args[args.index('-X') + 1], args[args.index('-X') + 2:]
mode = (STATE / 'mode').read_text()
if window is None:
    sys.exit(0)  # 4.00.03: a windowless -X is ignored and still exits 0
with open(STATE / 'calls.jsonl', 'a') as out:
    out.write(json.dumps({'session': session, 'window': window, 'command': command}) + '\n')
typed = STATE / 'input'
if command == 'readbuf':
    (STATE / 'buffer').write_bytes(Path(os.path.expanduser(rest[0])).read_bytes())
elif command == 'paste' and mode != 'deaf':
    typed.write_bytes((STATE / 'buffer').read_bytes())
    (STATE / 'settling').write_text('2')  # the screen keeps redrawing for two more reads
elif command == 'hardcopy':
    left = int((STATE / 'settling').read_text()) if (STATE / 'settling').exists() else 0
    (STATE / 'settling').write_text(str(max(0, left - 1)))
    size = typed.stat().st_size if typed.exists() else 0
    shown = typed.read_bytes().decode()[:400] if typed.exists() and mode != 'collapsed' else ''
    Path(os.path.expanduser(rest[0])).write_text('window %d %d\n%s\n' % (size, left, shown))
elif command == 'stuff' and typed.exists():
    if (STATE / 'settling').exists() and int((STATE / 'settling').read_text()) > 0:
        (STATE / 'early_enter').write_text('1')  # Enter while the paste is still redrawing
    text = typed.read_bytes()
    typed.replace(STATE / 'submitted')
    if mode != 'no_row':
        log = Path.home() / '.local/state/memvane/v1/installs/fake/events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, 'a') as out:
            out.write(json.dumps({'event': 'recall', 'session': 'fake', 'ts': '2026-11-22T00:00:00Z',
                                  'prompt': {'sha256': hashlib.sha256(text).hexdigest(),
                                             'chars': len(text.decode())}}) + '\n')
'''

SESSIONS = {'claude': 'session-claude', 'codex': 'session-codex'}


class Local:
    """Stands in for the SSH account: commands run in a local shell with a private HOME."""

    def __init__(self, home, env=None):
        self.home, self.env, self.commands = Path(home), dict(env or {}), []

    def run(self, command, *, stdin=None, timeout=120):
        self.commands.append(command)
        env = {'HOME': str(self.home), 'PATH': os.environ['PATH'], **self.env}
        done = subprocess.run(['sh', '-c', command], input=stdin, capture_output=True, text=True,
                              timeout=scaled(timeout), env=env)
        return done.returncode, done.stdout, done.stderr

    def put(self, local, remote, *, timeout=300):
        # As scp: a leading ``~/`` is the account's home.
        target = self.home / remote[2:] if remote.startswith('~/') else Path(remote)
        if not target.parent.is_dir():
            raise RuntimeError('upload failed: no such directory')  # scp makes no parent
        shutil.copyfile(str(local), str(target))


def stamp(index):
    return '2026-11-22T00:%02d:%02dZ' % divmod(index, 60)


class Rows:
    """Synthetic event rows of one passing run; tests remove or change rows."""

    def __init__(self, plan):
        self.plan, self.rows = plan, []
        card = lambda role: next(c for c in plan['cards'] if c['slug'] == plan['roles'][role])
        self.add('index.publish', outcome='published')
        for host in ('claude', 'codex'):
            session = SESSIONS[host]
            self.add('update-queued', host=host, session=session, outcome='ok')
            self.recall(session, host, 'P1', [card('short')])
            self.recall(session, host, 'P2', [card('long')], queryChunks=4)
            if host == 'claude':
                self.recall(session, host, 'P1', [], sessionExcluded=1, outcome='empty',
                            reason='no_candidates_after_exclusion')
            self.add('gate', host=host, session=session, outcome='ok')
            self.add('posttool', host=host, session=session, outcome='ok')
            self.add('recall.edit', host=host, session=session, outcome='served', reason='injected')

    def add(self, event, **fields):
        self.rows.append({'event': event, 'ts': stamp(len(self.rows)), **fields})
        return self.rows[-1]

    def recall(self, session, host, name, cards, **fields):
        prompt = self.plan['prompts'][name]
        return self.add('recall', host=host, session=session, ms=800, **{
            'outcome': 'served', 'reason': 'selected', 'semantic': 'complete', 'candidates': 3,
            'prompt': {'sha256': prompt['sha256'], 'chars': prompt['chars']},
            'cards': [{'path': c['path'], 'sha': c['sha256']} for c in cards], **fields})

    def sync(self, plan, *, outcome='indexed', served=True):
        change = plan['delta']
        # As ``apply_delta``: the newest row before the change is its time.
        change.setdefault('afterTs', self.rows[-1]['ts'])
        self.add('wake.sync', outcome=outcome, ms=900)
        self.add('index.publish', outcome='published')
        self.plan = plan
        cards = [change['added'], change['changed']['after']] if served else []
        extra = {} if served else {'outcome': 'empty', 'reason': smoke.SELECTED_ZERO}
        return self.recall(SESSIONS['claude'], 'claude', 'P4', cards, **extra)


class Verdict(FixtureCase):
    def setUp(self):
        super().setUp()
        self.tree = self.home / 'tree'
        self.plan = smoke_fixture.build(self.tree, cards=6, run_id='feed01')
        self.rows = Rows(self.plan)

    def status(self, plan=None, rows=None, sessions=SESSIONS, **records):
        result = smoke.judge(plan or self.plan, self.rows.rows if rows is None else rows, sessions, **records)
        return {row['id']: row['status'] for row in result['items']}, result

    def test_a_complete_run_passes_every_judged_item(self):
        after = smoke_fixture.delta(self.tree, self.plan)
        self.rows.sync(after)
        statuses, result = self.status(
            after, model_url={'state': 'fetched', 'sha256': 'a' * 64, 'expected': 'a' * 64, 'size': 5},
            identity={'different': [], 'versions': ['1.1.0'], 'files': 3},
            aged={'storedDev': 1, 'liveDev': 2,
                  'before': [{'event': 'wake.sync', 'outcome': 'failed', 'ms': 35000}],
                  'after': [{'event': 'recall', 'pinMs': 18, 'lexicalMs': 9, 'stillCurrentMs': 12},
                            {'event': 'wake.sync', 'outcome': 'indexed', 'ms': 900}]})
        self.assertTrue(result['valid'])
        self.assertEqual(set(statuses.values()), {smoke.PASS}, statuses)
        self.assertEqual(len(statuses), 11)
        self.assertIn('| RS-SYNC | PASS |', smoke.gate_table(result))

    def test_a_missing_recall_row_or_another_card_hash_fails_the_recall(self):
        rows = [row for row in self.rows.rows
                if not (row['event'] == 'recall' and row['session'] == SESSIONS['codex'])]
        statuses, _ = self.status(rows=rows)
        self.assertEqual((statuses['RS-RECALL-codex'], statuses['RS-RECALL-claude']), (smoke.FAIL, smoke.PASS))
        changed = copy.deepcopy(self.rows.rows)
        next(row for row in changed if row['event'] == 'recall')['cards'][0]['sha'] = 'f' * 64
        self.assertEqual(self.status(rows=changed)[0]['RS-RECALL-claude'], smoke.FAIL)

    def test_an_unfinished_semantic_search_or_another_hosts_rows_fail(self):
        deferred = copy.deepcopy(self.rows.rows)
        next(row for row in deferred if row['event'] == 'recall')['semantic'] = 'deferred_deadline'
        self.assertEqual(self.status(rows=deferred)[0]['RS-RECALL-claude'], smoke.FAIL)
        # The codex session id is known and started, but only the claude session recalled.
        other = [row for row in self.rows.rows
                 if not (row['event'] == 'recall' and row['session'] == SESSIONS['codex'])]
        statuses, _ = self.status(rows=other)
        self.assertEqual((statuses['RS-RECALL-codex'], statuses['RS-HOSTENV']), (smoke.FAIL, smoke.FAIL))
        short = copy.deepcopy(self.rows.rows)
        [row for row in short if row['event'] == 'recall' and row.get('queryChunks')][0]['queryChunks'] = 1
        self.assertEqual(self.status(rows=short)[0]['RS-RECALL-claude'], smoke.FAIL)

    def test_a_real_zero_card_choice_passes_and_a_second_prompt_need_not_repeat_the_first_card(self):
        zero = copy.deepcopy(self.rows.rows)
        row = next(row for row in zero if row['event'] == 'recall')
        row.update(outcome='empty', reason=smoke.SELECTED_ZERO, cards=[])
        statuses, _ = self.status(rows=zero)
        self.assertEqual(statuses['RS-RECALL-claude'], smoke.PASS)
        # P2 targets another card: the first prompt's card is absent from it and that is expected.
        self.assertEqual(self.status()[0]['RS-RECALL-claude'], smoke.PASS)

    def test_a_repeated_card_or_no_exclusion_fails_the_repeat_check(self):
        self.assertEqual(self.status()[0]['RS-DEDUP'], smoke.PASS)
        repeated = copy.deepcopy(self.rows.rows)
        first, second = [row for row in repeated if row['event'] == 'recall'
                         and row['session'] == SESSIONS['claude']
                         and row['prompt']['sha256'] == self.plan['prompts']['P1']['sha256']]
        second.update(cards=list(first['cards']), outcome='served')
        self.assertEqual(self.status(rows=repeated)[0]['RS-DEDUP'], smoke.FAIL)
        second.update(cards=[], sessionExcluded=0)
        self.assertEqual(self.status(rows=repeated)[0]['RS-DEDUP'], smoke.FAIL)

    def test_sync_needs_the_time_of_the_change_and_ignores_the_passes_before_it(self):
        after = smoke_fixture.delta(self.tree, self.plan)
        # Only the onboarding's pass and publish exist; P4 is asked and nothing is served.
        self.rows.add('wake.sync', outcome='indexed', ms=900)
        self.rows.add('index.publish', outcome='published')
        mark = self.rows.add('gate', host='claude', session=SESSIONS['claude'], outcome='ok')['ts']
        self.rows.plan = after
        self.rows.recall(SESSIONS['claude'], 'claude', 'P4', [], outcome='empty', reason=smoke.SELECTED_ZERO)
        self.assertIsNone(after['delta'].get('afterTs'))
        sync = lambda: next(row for row in self.status(after)[1]['items'] if row['id'] == 'RS-SYNC')
        self.assertEqual((sync()['status'], sync()['note']),
                         (smoke.NOT_RUN, 'the time of the change was not recorded'))
        after['delta']['afterTs'] = mark
        self.assertEqual((sync()['status'], sync()['note']), (smoke.FAIL, 'wake.sync absent'))

    def test_sync_fails_on_a_failed_pass_and_leaves_cards_unjudged_when_nothing_is_served(self):
        self.assertEqual(self.status()[0]['RS-SYNC'], smoke.NOT_RUN)
        after = smoke_fixture.delta(self.tree, self.plan)
        failed = Rows(self.plan)
        failed.sync(after, outcome='failed')
        self.assertEqual(self.status(after, failed.rows)[0]['RS-SYNC'], smoke.FAIL)
        quiet = Rows(self.plan)
        quiet.sync(after, served=False)
        statuses, result = self.status(after, quiet.rows)
        self.assertEqual(statuses['RS-SYNC'], smoke.PASS)
        self.assertIn('cards NOT RUN', next(row['note'] for row in result['items'] if row['id'] == 'RS-SYNC'))
        stale = Rows(self.plan)
        row = stale.sync(after)
        row['cards'].append({'path': after['delta']['deleted']['path'], 'sha': 'x'})
        self.assertEqual(self.status(after, stale.rows)[0]['RS-SYNC'], smoke.FAIL)
        old = Rows(self.plan)
        old.sync(after)['cards'][1]['sha'] = after['delta']['changed']['before']['sha256']
        self.assertEqual(self.status(after, old.rows)[0]['RS-SYNC'], smoke.FAIL)

    def test_an_edit_without_a_posttool_row_fails_and_learning_is_judged_only_when_on(self):
        rows = [row for row in self.rows.rows
                if not (row['event'] == 'posttool' and row['session'] == SESSIONS['codex'])]
        statuses, _ = self.status(rows=rows)
        self.assertEqual((statuses['RS-EDIT-codex'], statuses['RS-EDIT-claude']), (smoke.FAIL, smoke.PASS))
        learning = dict(self.plan, learning=True)
        self.assertEqual(self.status(learning)[0]['RS-EDIT-claude'], smoke.FAIL)
        self.rows.add('learning.capture', session=SESSIONS['claude'], outcome='stored')
        self.assertEqual(self.status(learning)[0]['RS-EDIT-claude'], smoke.PASS)

    def test_a_host_without_its_session_start_row_is_blocked_not_failed(self):
        rows = [row for row in self.rows.rows if row.get('session') != SESSIONS['codex']]
        statuses, _ = self.status(rows=rows)
        self.assertEqual((statuses['RS-RECALL-codex'], statuses['RS-EDIT-codex']), (smoke.BLOCKED, smoke.BLOCKED))
        self.assertEqual((statuses['RS-RECALL-claude'], statuses['RS-HOSTENV']), (smoke.PASS, smoke.PASS))
        self.assertNotIn(smoke.FAIL, statuses.values())

    def test_the_aged_install_leaves_the_device_item_unrun_when_devices_are_equal(self):
        record = {'storedDev': 7, 'liveDev': 7, 'before': [{'event': 'wake.sync', 'ms': 30000}],
                  'after': [{'event': 'recall', 'pinMs': 880, 'lexicalMs': 9, 'stillCurrentMs': 12},
                            {'event': 'wake.sync', 'outcome': 'indexed', 'ms': 900}]}
        row = smoke.judge_aged(record)
        self.assertEqual(row['status'], smoke.FAIL)
        self.assertIn('pinMs p50 880', row['note'])
        self.assertIn('dev NOT RUN', row['note'])
        record['after'][0]['pinMs'] = 18
        self.assertEqual(smoke.judge_aged(record)['status'], smoke.PASS)
        self.assertEqual(smoke.judge_aged(None)['status'], smoke.NOT_RUN)

    def test_the_model_address_fails_on_another_hash_or_404_and_blocks_without_network(self):
        expected = {'expected': 'a' * 64}
        for record, status in (({'state': 'fetched', 'sha256': 'b' * 64, **expected}, smoke.FAIL),
                               ({'state': 'not_found', **expected}, smoke.FAIL),
                               ({'state': 'unreachable', **expected}, smoke.BLOCKED),
                               ({'state': 'fetched', 'sha256': 'a' * 64, **expected}, smoke.PASS),
                               (None, smoke.NOT_RUN)):
            self.assertEqual(smoke.judge_model_url(record)['status'], status, record)
        self.assertEqual(smoke.judge_identity({'different': ['a.py'], 'versions': ['1.1.0']})['status'], smoke.FAIL)
        self.assertEqual(smoke.judge_identity({'different': [], 'versions': ['1.0.24', '1.1.0']})['status'], smoke.FAIL)

    def test_a_forbidden_variable_voids_the_whole_run(self):
        statuses, result = self.status(dict(self.plan, forbiddenEnv=['QMD_SANDBOX']))
        self.assertFalse(result['valid'])
        self.assertEqual(set(statuses.values()), {smoke.NOT_RUN})
        self.assertIn('무효', smoke.gate_table(result))

    def test_time_comes_from_the_rows(self):
        self.assertEqual(smoke.seconds_between({'ts': '2026-11-22T00:00:00Z'},
                                               {'ts': '2026-11-22T00:01:04.5Z'}), 64.5)
        self.assertIsNone(smoke.seconds_between({'ts': 'x'}, {'ts': '2026-11-22T00:00:00Z'}))
        self.assertEqual(smoke.seconds_between({'ts': '2026-11-22T09:00:00+09:00'},
                                               {'ts': '2026-11-22T00:00:01.250000Z'}), 1.25)


class RoundTrip(FixtureCase):
    def remote(self, **env):
        account = self.home / 'account'
        account.mkdir()
        return Local(account, env), account

    def test_the_remote_address_has_no_default(self):
        with self.assertRaises(SystemExit):
            smoke.main(['verdict', '--out', str(self.home / 'record')])
        with self.assertRaises(ValueError):
            smoke.Remote('')

    def test_prepare_ships_the_fixture_and_verdict_copies_only_listed_fields(self):
        remote, account = self.remote()
        record = self.home / 'record'
        fixture = account / 'smoke' / 'fixture'
        plan = smoke.prepare(remote, record, 'c' * 40, target=str(fixture),
                             build=lambda out, learning: smoke_fixture.build(out, cards=6, learning=learning,
                                                                             run_id='feed02'))
        self.assertEqual(plan['forbiddenEnv'], [])
        listed = subprocess.run(['git', '-C', str(fixture), 'ls-files'], capture_output=True, text=True,
                                check=True).stdout.split()
        self.assertEqual(len(listed), 2 + 2 * 6)
        self.assertEqual((record / 'prompt-P1.txt').read_text(), plan['prompts']['P1']['text'])
        # The account already holds event rows: the change records the newest one's time.
        earlier = account / '.local/state/memvane/v1/installs/older'
        earlier.mkdir(parents=True)
        (earlier / 'events.jsonl').write_text(''.join(
            json.dumps({'event': 'recall', 'session': 'older', 'ts': ts}) + '\n'
            for ts in ('2026-11-21T10:00:00Z', '2026-11-21T23:59:59Z', '2026-11-21T09:00:00Z')))
        after = smoke.apply_delta(remote, record)
        self.assertEqual(after['delta']['afterTs'], '2026-11-21T23:59:59Z')
        self.assertEqual(smoke._load_plan(record)['delta']['afterTs'], '2026-11-21T23:59:59Z')
        self.assertFalse((fixture / after['delta']['deleted']['file']).exists())
        self.assertTrue((fixture / after['delta']['added']['file']).is_file())
        status = subprocess.run(['git', '-C', str(fixture), 'status', '--porcelain'], capture_output=True,
                                text=True, check=True).stdout
        self.assertEqual(status, '')
        # The account's event log: the fixture install's rows and another install's.
        rows = Rows(after)
        rows.sync(after)
        for row in rows.rows:
            row.update(install='fixture-install', title='a private title', path='/private/path')
        log = account / '.local/state/memvane/v1/installs'
        for name, content in (('aaaa', [{'event': 'recall', 'session': 'someone-else', 'ts': stamp(0)}]),
                              ('bbbb', rows.rows)):
            (log / name).mkdir(parents=True)
            (log / name / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in content))
        gate = smoke.verdict(remote, record, SESSIONS)
        statuses = {row['id']: row['status'] for row in gate['items']}
        for name in ('RS-ONBOARD', 'RS-RECALL-claude', 'RS-RECALL-codex', 'RS-DEDUP', 'RS-EDIT-claude',
                     'RS-EDIT-codex', 'RS-SYNC', 'RS-HOSTENV'):
            self.assertEqual(statuses[name], smoke.PASS, (name, gate['items']))
        self.assertEqual((statuses['RS-ID'], statuses['RS-MODELURL'], statuses['Q1 RS-AGED']),
                         (smoke.NOT_RUN,) * 3)
        copied = (record / 'events-fixture.jsonl').read_text()
        self.assertNotIn('private', copied)
        self.assertNotIn('someone-else', copied)
        self.assertNotIn(after['prompts']['P1']['text'], copied)
        self.assertEqual(json.loads((record / 'gate.json').read_text())['commit'], 'c' * 40)
        self.assertIn('| RS-SYNC | PASS |', (record / 'gate-after-release.md').read_text())

    def test_prepare_stops_before_shipping_when_the_shell_carries_a_forbidden_variable(self):
        remote, account = self.remote(CLAUDE_SANDBOX='1')
        record = self.home / 'record'
        plan = smoke.prepare(remote, record, 'c' * 40, target=str(account / 'smoke' / 'fixture'),
                             build=lambda out, learning: smoke_fixture.build(out, cards=6, run_id='feed03'))
        self.assertEqual(plan['forbiddenEnv'], ['CLAUDE_SANDBOX'])
        self.assertFalse((account / 'smoke').exists())
        gate = smoke.verdict(remote, record, SESSIONS)
        self.assertFalse(gate['valid'])

    def fake_screen(self, account, mode='ok'):
        """A ``screen`` like 4.00.03 on the account's PATH: a ``-X`` without ``-p`` is ignored with exit 0."""
        tools = account / 'bin'
        tools.mkdir(exist_ok=True)
        state = account / 'screen-state'
        state.mkdir(exist_ok=True)
        (state / 'mode').write_text(mode)
        script = tools / 'screen'
        script.write_text(FAKE_SCREEN.replace('__PYTHON__', sys.executable).replace('__STATE__', str(state)))
        script.chmod(0o700)
        return state

    def prepared(self, mode='ok'):
        account = self.home / 'account'
        account.mkdir()
        remote = Local(account, {'PATH': '%s:%s' % (account / 'bin', os.environ['PATH'])})
        state = self.fake_screen(account, mode)
        record = self.home / 'record'
        plan = smoke.prepare(remote, record, 'c' * 40,
                             build=lambda out, learning: smoke_fixture.build(out, cards=6, run_id='feed04'))
        return remote, account, state, record, plan

    def calls(self, state):
        path = state / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []

    def test_the_default_remote_path_is_under_the_account_home(self):
        remote, account, _state, _record, plan = self.prepared()
        self.assertEqual(plan['remoteFixture'], '~/memvane-smoke/feed04/fixture')
        self.assertTrue((account / 'memvane-smoke/feed04/fixture/.git').is_dir())
        self.assertFalse((account / '~').exists())
        self.assertEqual(smoke.rpath('~/a b/c'), '"$HOME"/\'a b/c\'')
        self.assertEqual(smoke.rpath('/tmp/a b'), "'/tmp/a b'")

    def test_a_failed_prepare_is_run_again_with_the_same_record_directory(self):
        remote, account = self.remote()
        record = self.home / 'record'
        build = lambda out, learning: smoke_fixture.build(out, cards=6, run_id='feed05')
        put, remote.put = remote.put, mock.Mock(side_effect=RuntimeError('upload failed'))
        with self.assertRaisesRegex(RuntimeError, 'upload failed'):
            smoke.prepare(remote, record, 'c' * 40, build=build)
        first = json.loads((record / 'plan.json').read_text())
        self.assertFalse(first['shipped'])
        remote.put = put
        again = smoke.prepare(remote, record, 'c' * 40,
                              build=mock.Mock(side_effect=AssertionError('built twice')))
        self.assertEqual((again['cards'], again['shipped']), (first['cards'], True))
        self.assertTrue((account / 'memvane-smoke/feed05/fixture/.git').is_dir())
        with self.assertRaisesRegex(RuntimeError, 'already holds a shipped fixture'):
            smoke.prepare(remote, record, 'c' * 40, build=build)
        # A build that stopped before its plan was written is built again.
        (record / 'plan.json').unlink()
        rebuilt = smoke.prepare(remote, record, 'c' * 40, target=str(account / 'second/fixture'), build=build)
        self.assertTrue(rebuilt['shipped'])

    def test_send_pastes_waits_for_the_paste_then_presses_enter_and_finds_the_row(self):
        remote, account, state, record, plan = self.prepared()
        result = smoke.send(remote, record, 'claude', 'P2', pause=lambda _seconds: None)
        self.assertEqual((result['pasted'], result['submitted'], result['row']), (True, True, True))
        self.assertTrue(smoke.sent(result))
        calls = self.calls(state)
        # Every command names window 0, and Enter comes after the last hardcopy.
        self.assertTrue(all(call['window'] == '0' and call['session'] == 'smoke-claude' for call in calls))
        kinds = [call['command'] for call in calls]
        self.assertEqual(kinds[:3], ['hardcopy', 'readbuf', 'paste'])
        self.assertEqual((kinds.count('stuff'), kinds[-1]), (1, 'stuff'))
        self.assertGreaterEqual(kinds.count('hardcopy'), 3)
        # The session received exactly the planned text: its hash is the plan's.
        submitted = (state / 'submitted').read_bytes()
        self.assertEqual(hashlib.sha256(submitted).hexdigest(), plan['prompts']['P2']['sha256'])
        self.assertNotIn(b'\n', submitted)

    def test_enter_is_not_sent_while_the_paste_is_still_redrawing(self):
        remote, _account, state, record, _plan = self.prepared()
        for name in ('P0', 'P1', 'P2'):
            self.assertTrue(smoke.sent(smoke.send(remote, record, 'claude', name, pause=lambda _s: None)), name)
        self.assertFalse((state / 'early_enter').exists())

    def test_a_short_prompt_is_sent_only_when_its_own_token_is_on_the_window(self):
        # The window changes and settles without showing the pasted text.
        remote, _account, state, record, plan = self.prepared('collapsed')
        for name in ('P0', 'P1', 'P3'):
            self.assertTrue(plan['prompts'][name]['mark'].startswith('SMKFEED04'), name)
            result = smoke.send(remote, record, 'claude', name, pause=lambda _s: None, paste_reads=6)
            self.assertEqual((result['pasted'], result['submitted']), (False, False), name)
        self.assertNotIn('stuff', [call['command'] for call in self.calls(state)])
        # The long prompt has no mark: the settled change is its evidence.
        self.assertIsNone(plan['prompts']['P2']['mark'])
        self.assertTrue(smoke.sent(smoke.send(remote, record, 'claude', 'P2', pause=lambda _s: None)))

    def test_the_prompts_of_two_runs_never_share_a_hash(self):
        first = smoke_fixture.build(self.home / 'one', cards=6, run_id='feed0a')
        second = smoke_fixture.build(self.home / 'two', cards=6, run_id='feed0b')
        for name in ('P0', 'P1', 'P2', 'P3'):
            self.assertNotEqual(first['prompts'][name]['sha256'], second['prompts'][name]['sha256'], name)
            self.assertIn('SMKFEED0A', first['prompts'][name]['text'], name)

    def test_a_prepare_cut_after_its_commit_is_run_again(self):
        remote, account, _state, record, plan = self.prepared()
        saved = smoke._load_plan(record)
        saved['shipped'] = False  # The run ended after the remote commit, before this was written.
        smoke._save(smoke._plan_path(record), saved)
        again = smoke.prepare(remote, record, 'c' * 40,
                              build=mock.Mock(side_effect=AssertionError('built twice')))
        self.assertTrue(again['shipped'])
        log = subprocess.run(['git', '-C', str(account / 'memvane-smoke/feed04/fixture'), 'log', '--oneline'],
                             capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(len(log), 1)

    def test_send_reports_failure_when_the_session_shows_nothing(self):
        remote, _account, state, record, _plan = self.prepared('deaf')
        result = smoke.send(remote, record, 'claude', 'P1', pause=lambda _seconds: None, paste_reads=4)
        self.assertEqual((result['pasted'], result['submitted']), (False, False))
        self.assertFalse(smoke.sent(result))
        self.assertNotIn('stuff', [call['command'] for call in self.calls(state)])

    def test_send_fails_without_the_hook_row_except_for_the_onboarding_prompt(self):
        remote, _account, _state, record, _plan = self.prepared('no_row')
        quiet = lambda _seconds: None
        result = smoke.send(remote, record, 'claude', 'P1', pause=quiet, row_reads=3)
        self.assertEqual((result['pasted'], result['submitted'], result['row']), (True, True, False))
        self.assertFalse(smoke.sent(result))
        first = smoke.send(remote, record, 'claude', 'P0', pause=quiet, row_reads=3)
        self.assertEqual((first['pasted'], first['submitted'], first['row']), (True, True, None))
        self.assertTrue(smoke.sent(first))

    def test_account_prep_records_versions_and_only_the_result_of_a_login_check(self):
        remote, account = self.remote()
        tools = account / 'bin'
        tools.mkdir()
        remote.env['PATH'] = '%s:/usr/bin:/bin' % tools
        fake = tools / 'claude'
        fake.write_text('#!/bin/sh\nif [ "$1" = auth ]; then echo "logged in as person@example.invalid"; else echo "9.9.9 (fake)"; fi\n')
        fake.chmod(0o700)
        version = tools / 'screen'
        version.write_text('#!/bin/sh\necho "Screen version 4.00.03 (FAU) 23-Oct-06"; exit 1\n')
        version.chmod(0o700)
        lines = smoke.account_prep(remote)
        self.assertIn('A2 claude: ok 9.9.9 (fake)', lines)
        self.assertIn('A2 claude login: ok', lines)
        self.assertIn('A2 codex: failed', lines)
        self.assertIn('A2 codex login: failed', lines)
        self.assertIn('A3 screen version: ok Screen version 4.00.03 (FAU) 23-Oct-06', lines)
        self.assertNotIn('example.invalid', '\n'.join(lines))

    def test_installed_files_are_compared_with_the_commit(self):
        remote, account = self.remote()
        installed = account / 'plugin'
        (installed / 'memvane').mkdir(parents=True)
        files = {'package.json': '{"version": "1.1.0"}', 'memvane/a.py': 'a = 1\n'}
        for name, text in files.items():
            (installed / name).write_text(text)
        (installed / '.in_use').write_text('host metadata')
        import hashlib
        tree = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
        record = smoke.installed_identity(remote, 'c' * 40, str(installed), tree=tree)
        self.assertEqual((record['different'], record['versions'], record['files']), ([], ['1.1.0'], 2))
        # A path under the account's home is given with ``~/``.
        self.assertEqual(account, remote.home)
        home = smoke.installed_identity(remote, 'c' * 40, '~/plugin', tree=tree)
        self.assertEqual((home['different'], home['versions'], home['files']), ([], ['1.1.0'], 2))
        (installed / 'memvane/a.py').write_text('a = 2\n')
        (installed / 'extra.py').write_text('')
        record = smoke.installed_identity(remote, 'c' * 40, str(installed), tree=tree)
        self.assertEqual(record['different'], ['extra.py', 'memvane/a.py'])
        self.assertEqual(smoke.judge_identity(record)['status'], smoke.FAIL)
