#!/usr/bin/env python3
"""Wiki pipeline end to end: host hooks -> edit inbox -> compile worker -> ledger -> published card.

A temporary HOME holds one project onboarded and selected through the 1.0
install use cases (``tests.support.install_builder``). The host hooks run as
children the way each host starts them: ``hooks/run-hook <action> claude|codex``
for PostToolUse ``compile``/``index``, SessionStart ``update-queued`` and Bash
``file-change-event``, and the Hermes adapter (``hermes_adapter.core_bridge``)
for ``post_tool_call`` and ``on_session_start``. Their PYTHONPATH starts with
``tests/e2e/hook_probe``, whose ``sitecustomize`` logs every SQLite open and
runtime wake of the child and refuses a connect inside ``.memvane/compile``.

The compile worker runs in this process through ``wiki_compile_worker.main``
(run lock, 1.x cutover, inbox absorb, retention, claim, verify pass). Replaced
only where a host CLI is needed: ``engines.api.run_extractor``
(``tests.support.engines_fake``) answers extraction and verification and counts
every paid call, and ``runtime.api.request`` records the wakes. A 1.x project
is written by a release tag's own writers (``git archive``). Every check is a
count of calls, rows, files or bytes; none reads a wall clock.

Run one method per process through ``wiki-pipeline-e2e.test.mjs``.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.case import FixtureCase  # noqa: E402
from tests.support.engines_fake import FakeEngines  # noqa: E402
from tests.support.install_builder import build_install  # noqa: E402
from tests.support.project import make_project  # noqa: E402

PROBE = ROOT / 'tests' / 'e2e' / 'hook_probe'
TAG = 'v1.1.2'
SOURCE = 'docs/pond.md'
TEXT = 'Field notes. The emerald heron keeps the marker zqheronx beside the north pond.\n'
EDITED = TEXT + 'At dusk the heron returns to the reeds.\n'
CLAIM = 'The emerald heron keeps the marker zqheronx beside the north pond.'
VERDICT = {'verdict': 'pass', 'claims': [{'claim': CLAIM}], 'reasons': []}
BACKLOG = 1030


def _answer(title, summary):
    slug = title.lower().replace(' ', '-')
    return {'candidates': [{'title': title, 'summary': summary, 'confidence': 'high',
                            'suggestedType': 'concept',
                            'targetPath': '.memvane/wiki/concepts/%s.md' % slug}]}


class _Crash(BaseException):
    """Stands in for a worker killed inside a card publish."""


class WikiPipelineE2E(FixtureCase):
    # -- project -------------------------------------------------------------------
    def setUp(self):
        super().setUp()
        self.answer = _answer('Emerald heron marker', CLAIM)
        self.engines = FakeEngines({'extract': lambda payload, argv: copy.deepcopy(self.answer),
                                    'verify': VERDICT}).install(self)
        self.wakes = []
        wake = mock.patch('memvane.runtime.api.request',
                          lambda kind, target='', **_kw: self.wakes.append(kind) or {'status': 'queued'})
        wake.start()
        self.addCleanup(wake.stop)

    def onboard(self, notes, name='fixture'):
        self.fixture = make_project(self.home, name=name)
        for rel, text in notes.items():
            self.fixture.write_note(rel, text)
        self.fixture.commit('fixture')
        self.root = self.fixture.root
        self.compile_dir = self.root / '.memvane' / 'compile'
        self.wiki = self.fixture.wiki
        self.install = build_install(self, self.fixture)
        self.install.settle()

    # -- hooks (children) -----------------------------------------------------------
    def hook_env(self):
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONPATH': os.pathsep.join((str(PROBE), str(ROOT))),
               'MEMVANE_E2E_HOOK_LOG': str(self.tmp / 'hook-probe.jsonl'),
               'MEMVANE_E2E_LEDGER_DIR': str(self.compile_dir)}
        for key in ('QMD_SANDBOX', 'GEMINI_SANDBOX', 'CLAUDE_PLUGIN_ROOT', 'PLUGIN_ROOT'):
            env.pop(key, None)
        return env

    def probe(self):
        path = self.tmp / 'hook-probe.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]

    def run_hook(self, action, host, payload):
        """``hooks/run-hook <action> <host>``: the wake kinds this one hook recorded."""
        before = len(self.probe())
        child = subprocess.run(['bash', str(ROOT / 'hooks' / 'run-hook'), action, host],
                               input=json.dumps({'cwd': str(self.root), **payload}), text=True,
                               capture_output=True, timeout=120, env=self.hook_env(), cwd=str(self.root))
        self.assertEqual(child.returncode, 0, child.stderr)
        return [row['wake'] for row in self.probe()[before:] if 'wake' in row]

    def run_hermes(self, function, **kwargs):
        """One Hermes plugin hook (``hermes_adapter.core_bridge``) in a child Python."""
        before = len(self.probe())
        script = ('import json, sys\nfrom hermes_adapter import core_bridge\n'
                  'getattr(core_bridge, sys.argv[1])(**json.loads(sys.argv[2]))\n')
        child = subprocess.run([sys.executable, '-B', '-c', script, function, json.dumps(kwargs)],
                               capture_output=True, text=True, timeout=120, env=self.hook_env(),
                               cwd=str(self.root))
        self.assertEqual(child.returncode, 0, child.stderr)
        return [row['wake'] for row in self.probe()[before:] if 'wake' in row]

    def edit(self, rel, text, host='claude'):
        """New source bytes, then the host's PostToolUse edit hooks; returns their wake kinds."""
        self.fixture.write_note(rel, text)
        if host == 'hermes':
            return self.run_hermes('post_edit_sync', tool_name='write_file',
                                   args={'path': rel, 'content': text}, result={'success': True},
                                   cwd=str(self.root))
        if host == 'codex':
            lines = ''.join('+' + line + '\n' for line in text.splitlines())
            tool, details = 'apply_patch', {'command': '*** Begin Patch\n*** Update File: ' + rel + '\n'
                                            + lines + '*** End Patch'}
        else:
            tool, details = 'Write', {'file_path': rel, 'content': text}
        payload = {'hook_event_name': 'PostToolUse', 'session_id': 'e2e-' + host, 'tool_name': tool,
                   'tool_input': details, 'tool_use_id': 'tool-' + host}
        return {'index': self.run_hook('index', host, payload),
                'compile': self.run_hook('compile', host, payload)}

    def ledger_opens(self):
        return [row['sqlite'] for row in self.probe()
                if 'sqlite' in row and str(self.compile_dir) in row['sqlite']]

    def inbox_rows(self):
        inbox = self.compile_dir / 'edit-inbox.jsonl'
        if not inbox.exists():
            return []
        return [json.loads(line) for line in inbox.read_text(encoding='utf-8').splitlines()]

    # -- worker (this process) --------------------------------------------------------
    def worker_pass(self):
        from memvane.wiki.compile import wiki_compile_worker as worker
        out = io.StringIO()
        argv = ['wiki-compile-worker', '--cwd', str(self.root), '--json', '--flush-all']
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out):
            self.assertEqual(worker.main(), 0)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def calls(self, task):
        return sum(call['task'] == task for call in self.engines.calls)

    def ledger(self, sql, *args):
        with contextlib.closing(sqlite3.connect(str(self.compile_dir / 'wiki-pipeline.sqlite3'))) as db:
            return db.execute(sql, args).fetchall()

    def card(self, title='Emerald heron marker'):
        return self.wiki / 'concepts' / (title.lower().replace(' ', '-') + '.md')

    def wiki_extras(self):
        """Anything in the wiki besides cards and ``.memvane-root`` (hidden files, temps)."""
        return sorted(str(path.relative_to(self.wiki)) for path in self.wiki.rglob('*')
                      if path.is_file() and path.name != '.memvane-root'
                      and (path.name.startswith('.') or path.suffix != '.md'))

    # -- the methods ------------------------------------------------------------------
    def test_three_hosts_feed_one_inbox_and_one_pass_publishes_one_verified_card(self):
        self.onboard({SOURCE: TEXT})
        wakes = {
            'claude': self.edit(SOURCE, EDITED, 'claude'),
            'codex': self.edit(SOURCE, EDITED, 'codex'),
            'hermes': self.edit(SOURCE, EDITED, 'hermes'),
        }
        for host in ('claude', 'codex'):
            # PostToolUse index wakes the sync owner; compile records the edit and wakes the compile owner.
            self.assertIn('laya-sync', wakes[host]['index'], host)
            self.assertEqual(wakes[host]['compile'], ['laya-compile'], host)
        self.assertEqual(sorted(set(wakes['hermes'])), ['laya-compile', 'laya-sync'])
        session = {host: self.run_hook('update-queued', host, {'hook_event_name': 'SessionStart',
                                                               'session_id': 'e2e-' + host})
                   for host in ('claude', 'codex')}
        session['hermes'] = self.run_hermes('session_update', cwd=str(self.root))
        for host, kinds in session.items():
            self.assertIn('laya-sync', kinds, host)
        for host in ('claude', 'codex'):
            # SessionStart always offers the compile owner a pass (it cannot read the ledger).
            self.assertIn('laya-compile', session[host], host)
        bash = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'tool_input': {'command': 'git pull'},
                'tool_response': {'stdout': '', 'stderr': '', 'interrupted': False}}
        for host in ('claude', 'codex'):
            # A successful agent Bash wakes the full-scan sync owner only.
            self.assertEqual(set(self.run_hook('file-change-event', host, bash)), {'laya-sync'}, host)
        # Three edits, three rows; no hook opened the ledger, and none created it.
        rows = self.inbox_rows()
        self.assertEqual(sorted(row['engine'] for row in rows), ['claude', 'codex', 'hermes'])
        self.assertEqual({row['schema'] for row in rows}, {'memvane-edit-inbox-v1'})
        self.assertEqual({row['sourcePath'] for row in rows}, {SOURCE})
        self.assertEqual(self.ledger_opens(), [])
        self.assertFalse((self.compile_dir / 'wiki-pipeline.sqlite3').exists())

        first = self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1), (first, self.engines.calls))
        self.assertIn(b'status: verified', self.card().read_bytes())
        self.assertEqual(self.inbox_rows(), [])
        self.assertEqual(sorted(p.name for p in self.compile_dir.glob('edit-inbox.jsonl*')),
                         ['edit-inbox.jsonl.lock'])
        # Absorbed once, then folded to the latest edit of the source.
        self.assertEqual(self.ledger('SELECT event_id FROM refresh_events'), [(rows[-1]['eventId'],)])
        self.assertEqual(self.ledger('SELECT count(*) FROM card_generations'), [(1,)])
        self.assertEqual(self.ledger("SELECT state, count(*) FROM verify_jobs GROUP BY state"), [('done', 1)])
        self.assertEqual(self.ledger("SELECT count(*) FROM publish_txns WHERE state = 'publishing'"), [(0,)])
        self.assertEqual(self.wiki_extras(), [])
        # A second pass finds nothing to pay for.
        self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))

        # Retention runs in the worker pass: rows older than the audit horizon go, the rest stay.
        self.assertEqual(self.ledger('PRAGMA auto_vacuum'), [(2,)])  # INCREMENTAL
        kept = self.ledger('SELECT count(*) FROM audit_events')[0][0]
        self.assertGreater(kept, 0)
        old_id = self.ledger('SELECT min(id) FROM audit_events')[0][0]
        with contextlib.closing(sqlite3.connect(str(self.compile_dir / 'wiki-pipeline.sqlite3'))) as db, db:
            db.execute('UPDATE audit_events SET created_ns = created_ns - ? WHERE id = ?',
                       (91 * 86400 * 10**9, old_id))
        self.worker_pass()
        self.assertEqual(self.ledger('SELECT count(*) FROM audit_events WHERE id = ?', old_id), [(0,)])
        self.assertGreaterEqual(self.ledger('SELECT count(*) FROM audit_events')[0][0], kept - 1)

    def test_a_worker_killed_inside_the_publish_resumes_without_paying_again(self):
        self.onboard({SOURCE: TEXT})
        self.edit(SOURCE, EDITED, 'claude')
        from memvane.wiki.pipeline import publish
        real = publish._exchange

        def killed(temp_path, target):
            real(temp_path, target)
            raise _Crash('killed after the exchange, before the ledger settled')
        with mock.patch.object(publish, '_exchange', killed), self.assertRaises(_Crash):
            self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        self.assertEqual(self.ledger("SELECT count(*) FROM publish_txns WHERE state = 'publishing'"), [(1,)])
        self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        self.assertIn(b'status: verified', self.card().read_bytes())
        self.assertEqual(self.ledger("SELECT count(*) FROM publish_txns WHERE state = 'publishing'"), [(0,)])
        self.assertEqual(self.ledger("SELECT count(*) FROM verify_jobs WHERE state IN ('queued', 'claimed')"),
                         [(0,)])
        self.assertEqual(self.wiki_extras(), [])

    def test_a_backlog_beyond_1024_open_rows_does_not_stall_a_new_edit(self):
        self.onboard({SOURCE: TEXT, 'docs/kingfisher.md': 'The kingfisher dives at noon.\n'})
        self.edit(SOURCE, EDITED, 'claude')
        self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        # The verify row the compile writer wrote, repeated for cards that are not there.
        payload = json.loads(self.ledger('SELECT payload_json FROM verify_jobs')[0][0])
        from memvane.wiki.pipeline import api as pipeline
        with pipeline.open_pipeline(self.root) as store:
            store.append_jobs('verify', [{**payload, 'targetPath': '.memvane/wiki/concepts/held-%04d.md' % index}
                                         for index in range(BACKLOG)])
        self.answer = _answer('Kingfisher noon', 'The kingfisher dives at noon.')
        self.edit('docs/kingfisher.md', 'The kingfisher dives at noon.\nIt nests in the bank.\n', 'codex')
        self.worker_pass()
        # The new card is extracted and verified first; the held rows cost no call.
        self.assertEqual((self.calls('extract'), self.calls('verify')), (2, 2), self.engines.calls)
        self.assertIn(b'status: verified', self.card('Kingfisher noon').read_bytes())
        open_rows = lambda: self.ledger("SELECT count(*) FROM verify_jobs WHERE state = 'queued'")[0][0]
        before = open_rows()
        self.assertGreater(before, 1024 - 10)
        self.worker_pass()
        self.assertLess(open_rows(), before)
        self.assertEqual(self.calls('verify'), 2)

    def test_a_1x_project_written_by_a_release_writer_migrates_on_the_first_pass(self):
        self.onboard({'docs/alpha.md': 'Alpha source.', 'docs/beta.md': 'Beta source.',
                      'docs/gamma.md': 'Gamma source.'}, name='project')
        files = self.release_state()
        manifest = [json.loads(line) for line in files['generated-manifest.jsonl'].splitlines()]
        self.assertTrue((self.compile_dir / 'generated-manifest.jsonl').exists())
        # The 1.x leftovers do not change the hook: no ledger open, one inbox row.
        self.answer = _answer('Alpha', 'Alpha fact one. Alpha fact two.')
        self.edit('docs/alpha.md', 'Alpha source. Alpha fact two.', 'claude')
        self.assertEqual([row['engine'] for row in self.inbox_rows()], ['claude'])
        self.assertEqual(self.ledger_opens(), [])

        first = self.worker_pass()
        from memvane.wiki.pipeline import api as pipeline
        self.assertFalse(pipeline.needs_v1_migration(self.root, wiki_root=self.wiki), first)
        imported = [json.loads(row[0]) for row in
                    self.ledger('SELECT row_json FROM card_generations ORDER BY id')][:len(manifest)]
        self.assertEqual(imported, manifest)
        for name in ('generated-manifest.jsonl', 'verify-skipped.jsonl', 'dedup-skipped.jsonl'):
            self.assertFalse((self.compile_dir / name).exists(), name)
        self.assertEqual(len(pipeline.log_rows(self.root, 'migration')), 1)
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            self.assertEqual(list(store.suppressions('verify')), ['docs/beta.md'])
            self.assertEqual(list(store.suppressions('dedup')), ['concepts/alpha.md\nconcepts/beta.md'])
        # The edit after the cutover replaces the 1.x card once.
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1), self.engines.calls)
        alpha = (self.wiki / 'concepts/alpha.md').read_text(encoding='utf-8')
        self.assertIn('status: verified', alpha)
        self.assertIn('Alpha fact two.', alpha)
        self.assertEqual(self.wiki_extras(), [])
        self.worker_pass()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        self.assertEqual(len(pipeline.log_rows(self.root, 'migration')), 1)

    def test_a_release_bundle_writes_nothing_into_a_v2_selected_project(self):
        """M9: the ``TAG`` wiki writers find no selected route on a state format 2 install."""
        import hashlib
        self.onboard({SOURCE: TEXT}, name='project')
        bundle = self.tmp / (TAG + '-full')
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', TAG], check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
        state = self.home / '.local/state/memvane/v2'
        self.assertTrue(state.is_dir())
        # One 1.1.4 pass leaves the compile folder, ledger and locks a selected project has.
        self.worker_pass()
        self.assertTrue((self.compile_dir / '.card-write.lock').exists())

        def snapshot():
            seen = {}
            for top in (self.root / '.memvane', state):
                for path in sorted(top.rglob('*')):
                    if path.is_symlink() or not path.is_file():
                        seen[str(path)] = 'dir' if path.is_dir() else 'other'
                    elif path.suffix in ('.sock',) or path.name.endswith(('-wal', '-shm')):
                        seen[str(path)] = 'present'
                    else:
                        seen[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            return seen
        env = {**self.hook_env(), 'PYTHONPATH': str(bundle)}
        candidate = json.dumps({'trigger': 'manual', 'sourceRef': 'session:local',
                                'durable': {'title': 'Heron rule', 'summary': CLAIM,
                                            'type': 'decision', 'confidence': 'high'}})
        payload = json.dumps({'cwd': str(self.root), 'hook_event_name': 'PostToolUse', 'session_id': 'm9',
                              'tool_name': 'Write', 'tool_input': {'file_path': SOURCE, 'content': EDITED},
                              'tool_use_id': 'tool-m9'})
        self.fixture.write_note(SOURCE, EDITED)
        before = snapshot()
        runs = {
            'skill': (['bash', str(bundle / 'skills/wiki-compile/scripts/wiki-compile.sh'), str(self.root)],
                      candidate),
            'extract': ([sys.executable, '-B', str(bundle / 'entry/run.py'), 'wiki-extract',
                         '--cwd', str(self.root)], candidate),
            'worker': ([sys.executable, '-B', str(bundle / 'entry/run.py'), 'wiki-compile-worker',
                        '--cwd', str(self.root), '--json', '--flush-all'], ''),
            'hook': (['bash', str(bundle / 'hooks/run-hook'), 'compile', 'claude'], payload),
        }
        codes = {}
        for name, (argv, stdin) in runs.items():
            child = subprocess.run(argv, input=stdin, text=True, capture_output=True, timeout=120,
                                   env=env, cwd=str(self.root))
            codes[name] = child.returncode
            after = snapshot()
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            self.assertEqual(changed, [], '%s wrote: %s %s' % (name, child.stdout, child.stderr))
        self.assertNotEqual(codes['skill'], 0, codes)
        self.assertEqual(self.calls('extract') + self.calls('verify'), 0)

    def release_state(self):
        """The 1.x compile ledgers and cards the ``TAG`` release's own writers leave in this project."""
        bundle = self.tmp / TAG
        bundle.mkdir()
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', TAG, 'memvane', 'package.json'],
                                 check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(bundle)], input=archive.stdout, check=True)
        scratch = self.tmp / 'release-scratch'
        scratch.mkdir()
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(ROOT)))}
        child = subprocess.run(
            [sys.executable, '-B', str(ROOT / 'memvane/wiki/pipeline/tests/tagged_v1_ledgers.py'),
             str(bundle), str(scratch)], cwd=bundle, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 0, child.stderr)
        written = json.loads(child.stdout)
        self.assertEqual(written['actions'], ['created', 'created', 'created'])
        released = Path(written['root'])
        self.compile_dir.mkdir(parents=True, exist_ok=True)
        for path in (released / '.memvane/compile').iterdir():
            if path.is_file():
                shutil.copy2(path, self.compile_dir / path.name)
        for card in (released / '.memvane/wiki/concepts').glob('*.md'):
            (self.wiki / 'concepts').mkdir(parents=True, exist_ok=True)
            shutil.copy2(card, self.wiki / 'concepts' / card.name)
        return written['files']


if __name__ == '__main__':
    unittest.main(verbosity=2)
