"""An old bundle's hooks beside this bundle's open learning states (design L-5).

After the hooks of each of the three previous releases (recall, an edit with its
capture and launch, selector maintenance) wrote on this install, this bundle's
readers, hook and worker still answer, and the job rows this bundle left open
are as they were. The other direction, an old worker on this bundle's open job
states, needs real gold cases and is part of the end-to-end run
(``test_lp1_e2e``).
"""
from __future__ import annotations

import json
import time

from memvane.learning.context_learning import shared
from memvane.learning.context_learning.state_model import CYCLE_RESULTS
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.integration import test_old_bundle_121 as old_tests
from memvane.learning.tests.old_bundle_121 import EDIT_FILE, EDIT_TEXT, PROMPT, TAGS, edit_payload, old_bundle
from tests.support.install_builder import ROOT

PREVIOUS = ('v1.0.24', 'v1.1.0', 'v1.1.1')
# (status, reason, attempts, failures, retry in the future): what 1.1.0 leaves open.
OPEN_JOBS = (('blocked', 'trainer_failed', 3, 3, True),            # waits for its rearm time
             ('blocked', 'learning_cursor_changed', 1, 1, False),  # closed for good
             ('deferred', 'trainer_progress', 1, 0, True))         # a timeout that made progress


class PreviousBundles(old_tests.OldBundles):
    # The fixture only: its own tests run in their module.
    locals().update({name: None for name in dir(old_tests.OldBundles) if name.startswith('test_')})

    def open_states(self):
        self.seed_122_rows(include_queued=False)
        later = time.time() + 86400
        with shared._db(self.root) as db:
            namespace, approval, cohort, manifest = db.execute(
                'SELECT namespace,approval,cohort,manifest FROM learning_jobs').fetchone()
            for index, (status, reason, attempts, failures, waits) in enumerate(OPEN_JOBS):
                job_id = 'cohort-%020d' % index
                db.execute('INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?)',
                           (job_id, namespace, approval, cohort, '%064d' % index,
                            canonical({**json.loads(manifest), 'id': job_id, 'family': '%064d' % index}),
                            status, attempts, failures, later if waits else 0, reason))
        return self.open_rows()

    def open_rows(self):
        """The rows of this bundle's job, evaluation and cycle states."""
        rows = self.rows()
        return {table: rows[table] for table in ('learning_jobs', 'learning_evaluations', 'cycles')}

    def test_the_previous_three_releases_are_the_last_tags(self):
        self.assertEqual(TAGS[-3:], PREVIOUS)

    def test_after_an_old_bundles_hooks_this_bundle_still_reads_and_answers(self):
        rows = self.open_states()
        for tag in PREVIOUS:
            with self.subTest(tag=tag):
                bundle = old_bundle(tag)
                code, out, err = self.hook(bundle, 'recall', {'session_id': 'old-recall-' + tag,
                                                              'hook_event_name': 'UserPromptSubmit', 'prompt': PROMPT})
                self.assertEqual(code, 0, err)
                self.fixture.write_note(EDIT_FILE, EDIT_TEXT)
                before = len(self.events())
                code, out, err = self.hook(bundle, 'posttool', edit_payload('old-edit-' + tag))
                self.assertEqual(code, 0, err)
                captured = self.wait_capture(before)
                self.assertEqual([(row['outcome'], row['pluginVersion']) for row in captured], [('stored', tag[1:])])
                self.assertEqual(self.entry(bundle, 'selector-maintenance', str(self.project), 'force'), (0, '', ''))
                # What the old hooks left is readable by this bundle: the worker request
                # (none, or one this bundle accepts) and the rows. (A 1.0.24 capture starts that release's own teacher pass.)
                request = shared._full_worker_request(self.root)
                self.assertTrue(request is None or set(request) >= {'schema', 'nonce', 'job', 'afterIndex'})
                self.assertEqual(self.open_rows(), rows)
        # This bundle's hook and worker run on the install the old bundles wrote on.
        code, out, err = self.hook(ROOT, 'recall', {'session_id': 'new-after-old',
                                                    'hook_event_name': 'UserPromptSubmit', 'prompt': PROMPT})
        self.assertEqual(code, 0, err)
        self.assertIn('zqheronx', json.loads(out)['hookSpecificOutput']['additionalContext'])
        code, out, err = self.entry(ROOT, 'learning-shared', 'run-project', str(self.project),
                                    patched=True, resource_ready=True)
        self.assertEqual(code, 0, (out, err))
        result = json.loads(out)
        self.assertIn(result['status'], set(CYCLE_RESULTS) | {'consent_required'}, result)
        jobs = {row[0]: (row[6], row[-1]) for row in self.open_rows()['learning_jobs']}
        self.assertEqual([jobs['cohort-%020d' % index] for index in range(len(OPEN_JOBS))],
                         [(status, reason) for status, reason, *_ in OPEN_JOBS])
