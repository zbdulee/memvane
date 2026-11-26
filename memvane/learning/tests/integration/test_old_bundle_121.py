"""1.0.21 G-9: old bundles leave a default-teacher namespace untouched.

The namespace is ``old_bundle_121.EnabledInstall``'s (real selected install,
real enrollment and enable). Each old bundle comes from its git tag and runs
as a child process: the worker command (``learning-shared run-project``), the
dispatcher's learning worker step (``shared.learning_worker_step`` with the
owned child it starts), the ``recall`` and ``posttool`` hooks
(``hooks/run-hook``) and ``selector-maintenance``. Assertions are bytes, row
lists, exit codes, launch counts and event lines, never elapsed time.

This bundle's install lives in the ``stateFormat`` 2 state base
(``memvane_paths.STATE_FORMAT``), which no 1.x bundle reads: its route proof
finds no selected install (``unselected_host_route``), its hooks answer empty
and capture nothing, and the only host CLI it starts is the legacy route's
plugin listing.
"""
from __future__ import annotations

import json
import unittest

from memvane.learning.tests.old_bundle_121 import (EDIT_FILE, EDIT_TEXT, PROMPT, TAGS, EnabledInstall,
                                                    edit_payload, old_bundle)
from tests.support.install_builder import ROOT

# The old dispatcher's own bookkeeping for the training-only step it starts.
WORKER_RECORDS = {'full-worker.lock', 'training-worker-state.json', 'worker.log'}
# A 1.x bundle's route proof on this bundle's stateFormat 2 install.
REFUSED = 'unselected_host_route'
# The legacy route's host plugin listing: the one CLI start a 1.x hook makes here.
LISTING = ['plugin', 'list', '--json']

STEP = '''
import subprocess
from pathlib import Path
from memvane.learning.context_learning import shared
children = []
def spawn(*args, **kwargs):
    child = subprocess.Popen(*args, **kwargs)
    children.append(child)
    return child
step = shared.learning_worker_step(Path(%r), Path(%r), %d, spawn=spawn)
result = {'step': step, 'children': [child.wait(timeout=120) for child in children],
          'state': json.loads((Path(%r) / 'training-worker-state.json').read_text())}
'''

# 1.0.23 adds ``busyRetries`` to the selector request row: an older bundle reads the
# keys it knows and writes the row back without it.
BUSY_ROW = '''
from memvane.learning import selector_maintenance as maintenance
row = {'requestedNs': 1000 * 10**9, 'failures': 0, 'retryAfterNs': 1060 * 10**9, 'busyRetries': 3}
result = {'failures': maintenance._failure_count(row),
          'retry': maintenance._retry_row(row, 1061 * 10**9, deferred=True)}
'''

GATES = '''
from pathlib import Path
from memvane.learning.context_learning import learning_profile, project_scope, shared
root, project = Path(%r), Path(%r)
def reason(call):
    try:
        value = call()
    except Exception as exc:
        return '%%s:%%s' %% (type(exc).__name__, exc)
    return 'returned:%%s' %% (type(value).__name__ if isinstance(value, dict) else value,)
result = {'policy': reason(lambda: shared._policy(root)),
          'local_train': reason(lambda: project_scope.require(
              root, json.loads((root / 'shared-policy.json').read_text()), 'local_train')),
          'selection': reason(lambda: learning_profile.selection_enabled(root, project)),
          'consent': reason(lambda: shared._consent(root, project))}
'''


class OldBundles(EnabledInstall):
    def observe(self, tag):
        bundle = old_bundle(tag)
        seen = {}
        seen['gates'] = self.driver(bundle, GATES % (str(self.root), str(self.project)))
        seen['worker'] = self.entry(bundle, 'learning-shared', 'run-project', str(self.project))
        seen['step'] = self.driver(bundle, STEP % (str(self.root), str(self.project), int(tag.split('.')[-1]),
                                                   str(self.root)))
        seen['recall'] = self.hook(bundle, 'recall', {'session_id': 'old-recall-' + tag,
                                                      'hook_event_name': 'UserPromptSubmit',
                                                      'prompt': PROMPT})
        self.fixture.write_note(EDIT_FILE, EDIT_TEXT)
        before = len(self.events())
        seen['posttool'] = self.hook(bundle, 'posttool', edit_payload('old-edit-' + tag))
        seen['maintenance'] = self.entry(bundle, 'selector-maintenance', str(self.project), 'force')
        seen['capture'] = [row for row in self.events()[before:] if row.get('event') == 'learning.capture']
        return seen

    def teacher_launches(self):
        """Every fake CLI start that is not the legacy route's plugin listing."""
        return [row for row in self.launches() if row.get('argv') != LISTING]

    def assert_excluded(self, seen):
        """A 1.x bundle's hooks and maintenance on this install: empty answers, no capture, no teacher."""
        self.assertEqual(seen['recall'], (0, '', ''))
        self.assertEqual(seen['posttool'], (0, '', ''))
        self.assertEqual(seen['capture'], [])
        code, out, err = seen['maintenance']
        self.assertEqual((code, out), (0, ''), err)
        self.assertIn('JournalEvidenceError: ' + REFUSED, err)
        self.assertEqual(self.teacher_launches(), [])

    def log_lines(self):
        log = self.root / 'worker.log'
        return log.read_text(encoding='utf8', errors='replace').splitlines() if log.is_file() else []

    def test_g9_old_bundles_leave_rows_files_and_clis_untouched(self):
        rows = self.seed_122_rows(include_queued=True)
        files = self.namespace_files()
        refused = 'ValueError:project_scope_authorization_invalid'
        for tag in ('v1.0.18', 'v1.0.19', 'v1.0.20'):
            with self.subTest(tag=tag):
                logged = len(self.log_lines())
                seen = self.observe(tag)
                # (a) rows and namespace files: only the old worker's own records move.
                self.assertEqual(self.rows(), rows)
                after = self.namespace_files()
                for name in self.watched_names():
                    self.assertIn(name, files)
                    self.assertEqual(after.get(name), files[name], name)
                changed = {name for name in set(files) | set(after) if files.get(name) != after.get(name)}
                self.assertLessEqual(changed, WORKER_RECORDS, sorted(changed))
                # (b) no teacher CLI ran.
                self.assertEqual(self.teacher_launches(), [])
                # The old envelope refuses the new grant shape; the old consent check
                # refuses the widened shared consent. Old selection_enabled does not read
                # the grant: recall's version gate (older_plugin_version) keeps it off.
                self.assertEqual(seen['gates'], {'policy': refused, 'local_train': refused,
                                                 'selection': 'returned:True', 'consent': 'returned:False'})
                code, out, err = seen['worker']
                self.assertEqual((code, json.loads(out)),
                                 (1, {'status': 'failed', 'reason': 'project_scope_authorization_invalid'}), err)
                step = seen['step']
                self.assertEqual(step['step'], {'status': 'started', 'mode': 'training', 'starts': 1})
                # The old child's readiness is a verified selected route (``learning.ready``),
                # checked before it reads resources: it finds none, settles
                # ``setup_in_progress`` with a durable wake and records no outcome.
                # It touches none of the rows, files or CLIs asserted above.
                # The log is shared by the tags: only the lines this tag added count.
                lines = self.log_lines()[logged:]
                self.assertEqual((step['children'], step['state']['outcome']), ([0], None), (step, lines[-3:]))
                self.assertTrue(lines, step)
                self.assertEqual(json.loads(lines[-1]), {'status': 'setup_in_progress',
                                                         'learning_retry': {'status': 'durable_wake_pending'}},
                                 lines[-3:])
                self.assertEqual(step['state']['revision'], int(tag.split('.')[-1]))
                # (c) hooks exit 0 with nothing to say and capture nothing.
                self.assert_excluded(seen)
        # This bundle's recall still answers on the same install afterwards.
        code, out, err = self.hook(ROOT, 'recall', {'session_id': 'new-after-old',
                                                    'hook_event_name': 'UserPromptSubmit', 'prompt': PROMPT})
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.teacher_launches(), [])

    def test_t1_7_v1_0_21_bundle_leaves_122_rows_and_parked_answers_untouched(self):
        self.assertIn('v1.0.21', TAGS)
        rows = self.seed_122_rows(include_queued=False)
        files = self.namespace_files()
        bundle = old_bundle('v1.0.21')

        # (a) Unpatched 1.0.21 bundle: hooks exit 0, no DB row or authority file moves, 0 teacher calls.
        seen = self.observe('v1.0.21')
        self.assertEqual(self.rows(), rows)
        for name in self.watched_names():
            self.assertEqual(self.namespace_files().get(name), files[name], name)
        self.assertEqual(seen['gates'], {'policy': 'JournalEvidenceError:' + REFUSED,
                                         'local_train': 'JournalEvidenceError:' + REFUSED,
                                         'selection': 'returned:True', 'consent': 'returned:True'})
        code, out, err = seen['worker']
        self.assertEqual((code, json.loads(out)), (1, {'status': 'failed', 'reason': REFUSED}), err)
        self.assert_excluded(seen)

        # (b) Patched 1.0.21 worker (the installed-host stand-ins and a ready resource
        # report): its route proof still refuses before any teacher pass or training,
        # 0 CLI calls and every 1.0.22 row stays byte-equal.
        code, out, err = self.entry(bundle, 'learning-shared', 'run-project', str(self.project),
                                    patched=True, resource_ready=True)
        self.assertEqual((code, json.loads(out)), (1, {'status': 'failed', 'reason': REFUSED}), err)
        self.assertEqual(self.teacher_launches(), [])
        self.assertEqual(self.rows(), rows)
        for name in self.watched_names():
            self.assertEqual(self.namespace_files().get(name), files[name], name)

    def test_v1_0_22_bundle_leaves_rows_untouched_and_reads_a_busy_request_row(self):
        self.assertIn('v1.0.22', TAGS)
        rows = self.seed_122_rows(include_queued=False)
        files = self.namespace_files()
        seen = self.observe('v1.0.22')
        self.assertEqual(self.rows(), rows)
        for name in self.watched_names():
            self.assertEqual(self.namespace_files().get(name), files[name], name)
        code, out, err = seen['worker']
        self.assertEqual((code, json.loads(out)), (1, {'status': 'failed', 'reason': REFUSED}), err)
        self.assert_excluded(seen)
        # The ten-minute floor and three keys: the 1.0.22 shape, the busy count gone.
        self.assertEqual(self.driver(old_bundle('v1.0.22'), BUSY_ROW),
                         {'failures': 0, 'retry': {'requestedNs': 1000 * 10**9, 'failures': 1,
                                                   'retryAfterNs': 1600 * 10**9}})

    def test_v1_0_23_bundle_leaves_rows_untouched_and_its_hooks_answer_empty(self):
        self.assertIn('v1.0.23', TAGS)
        rows = self.seed_122_rows(include_queued=False)
        files = self.namespace_files()
        seen = self.observe('v1.0.23')
        self.assertEqual(self.rows(), rows)
        for name in self.watched_names():
            self.assertEqual(self.namespace_files().get(name), files[name], name)
        code, out, err = seen['worker']
        self.assertEqual((code, json.loads(out)), (1, {'status': 'failed', 'reason': REFUSED}), err)
        self.assert_excluded(seen)


if __name__ == '__main__':
    unittest.main()
