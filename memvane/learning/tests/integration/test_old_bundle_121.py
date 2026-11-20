"""1.0.21 G-9: 1.0.18 and 1.0.19 bundles leave a default-teacher namespace untouched.

The namespace is ``old_bundle_121.EnabledInstall``'s (real selected install,
real enrollment and enable). Each old bundle comes from its git tag and runs
as a child process: the worker command (``learning-shared run-project``), the
dispatcher's learning worker step (``shared.learning_worker_step`` with the
owned child it starts), the ``recall`` and ``posttool`` hooks
(``hooks/run-hook``) and ``selector-maintenance``. Assertions are bytes, row
lists, exit codes, launch counts and event lines, never elapsed time.
"""
from __future__ import annotations

import json
import unittest

from memvane.learning.tests.old_bundle_121 import (EDIT_FILE, EDIT_TEXT, PROMPT, TAGS, EnabledInstall,
                                                    edit_payload, old_bundle)
from tests.support.install_builder import ROOT

# The old dispatcher's own bookkeeping for the training-only step it starts.
WORKER_RECORDS = {'full-worker.lock', 'training-worker-state.json', 'worker.log'}

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
        seen['capture'] = self.wait_capture(before)
        seen['maintenance'] = self.entry(bundle, 'selector-maintenance', str(self.project), 'force')
        return seen

    def test_g9_old_bundles_leave_rows_files_and_clis_untouched(self):
        rows = self.seed_rows()
        files = self.namespace_files()
        refused = 'ValueError:project_scope_authorization_invalid'
        for tag in TAGS:
            with self.subTest(tag=tag):
                seen = self.observe(tag)
                # (a) rows and namespace files: only the old worker's own records move.
                self.assertEqual(self.rows(), rows)
                after = self.namespace_files()
                for name in self.watched_names():
                    self.assertIn(name, files)
                    self.assertEqual(after.get(name), files[name], name)
                changed = {name for name in set(files) | set(after) if files.get(name) != after.get(name)}
                self.assertLessEqual(changed, WORKER_RECORDS, sorted(changed))
                # (b) no host CLI ran.
                self.assertEqual(self.launches(), [])
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
                self.assertEqual(step['children'], [1])
                self.assertEqual((step['state']['outcome'], step['state']['revision']),
                                 ('failed', int(tag.split('.')[-1])))
                # (c) hooks exit 0; recall answers by hybrid rank.
                code, out, err = seen['recall']
                self.assertEqual(code, 0, err)
                context = json.loads(out)['hookSpecificOutput']['additionalContext']
                self.assertIn('zqheronx', context)
                self.assertIn('hybrid rank score', context)
                self.assertIn('local selection unavailable (older_plugin_version); serving hybrid rank', err)
                code, out, err = seen['posttool']
                self.assertEqual(code, 0, err)
                self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
                # The old capture keeps its sample in the install's private state only.
                self.assertEqual([(row['outcome'], row['pluginVersion']) for row in seen['capture']],
                                 [('stored', tag[1:])])
                self.assertEqual(seen['maintenance'], (0, '', ''))
        # This bundle's recall still answers on the same install afterwards.
        code, out, err = self.hook(ROOT, 'recall', {'session_id': 'new-after-old',
                                                    'hook_event_name': 'UserPromptSubmit', 'prompt': PROMPT})
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.launches(), [])

if __name__ == '__main__':
    unittest.main()
