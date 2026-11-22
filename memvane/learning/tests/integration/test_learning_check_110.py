"""The read-only learning check of ``memvane:logs``: counts per invariant rule and nothing else."""
from __future__ import annotations

import json
import sys

from memvane.learning.context_learning import general_gold_ledger as ledger
from memvane.learning.context_learning import invariants, shared
from memvane.learning.tests.old_bundle_121 import ROOT, EnabledInstall


class LearningCheck(EnabledInstall):
    def entry(self, *flags):
        code, out, err = self.run_child([sys.executable, '-B', str(ROOT / 'skills/logs/scripts/logs.py'),
                                         '--cwd', str(self.project), '--learning-check', *flags], patched=True)
        self.assertEqual(code, 0, err)
        return out

    def test_the_check_reports_counts_per_rule_and_writes_nothing(self):
        self.seed_122_rows(include_queued=False)
        before = (self.rows(), self.namespace_files())
        clean = invariants.project_counts(self.project)
        self.assertEqual((clean['status'], clean['rules']), ('checked', invariants.counts(self.root)))
        self.assertEqual(clean['violations'], sum(clean['rules'].values()))
        self.assertEqual(json.loads(self.entry('--json'))['learningCheck'], clean)
        self.assertEqual((self.rows(), self.namespace_files()), before)
        # A job blocked without a reason and a second open job of one family: two rules, counted.
        with shared._db(self.root) as db:
            namespace, approval, cohort, family, manifest = db.execute(
                'SELECT namespace,approval,cohort,family,manifest FROM learning_jobs').fetchone()
            for index, (status, reason) in enumerate((('blocked', None), ('queued', None), ('deferred', None))):
                db.execute('INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,?,NULL,0,0,0,?)',
                           ('cohort-%020d' % index, namespace, approval, cohort, family, manifest, status, reason))
        broken = invariants.project_counts(self.project)
        self.assertEqual({rule: broken['rules'].get(rule, 0) - clean['rules'].get(rule, 0)
                          for rule in ('I-8', 'I-9')}, {'I-8': 1, 'I-9': 1})
        text = self.entry()
        self.assertIn('learning check: %d violation(s)' % broken['violations'], text)
        self.assertIn('  I-8 %d' % broken['rules']['I-8'], text)
        # Counts only: no job id, cycle id or case id is printed.
        self.assertNotIn('cohort-', text)
        self.assertNotIn('seed122', text)

    def test_a_first_and_a_second_opinion_row_of_one_input_are_not_a_repeated_call(self):
        self.seed_122_rows(include_queued=False)
        before = invariants.counts(self.root).get('G-1', 0)
        key = self.key()
        # The ledger refuses a second reservation of one input, so the old rows are written as rows.
        with shared._db(self.root) as db:
            ledger.reserve(db, key, 'legacy-two-opinions', 'first', 'e' * 64, None, rearm=True)
            ledger.reserve(db, key, 'legacy-two-opinions', 'second', 'f' * 64, None, rearm=True)
            db.execute("UPDATE project_gold_attempts SET input_sha256=? WHERE phase='second'", ('e' * 64,))
        self.assertEqual(invariants.counts(self.root).get('G-1', 0), before)
        with shared._db(self.root) as db:
            ledger.reserve(db, key, 'another-case', 'first', 'd' * 64, None, rearm=True)
            db.execute("UPDATE project_gold_attempts SET input_sha256=? WHERE request_id='another-case'", ('e' * 64,))
        self.assertEqual(invariants.counts(self.root).get('G-1', 0), before + 1)

    def test_a_project_without_a_namespace_or_a_path_that_is_gone_answers_without_raising(self):
        self.assertFalse((self.root / shared.DATABASE_NAME).exists())
        self.assertEqual(invariants.project_counts(self.project), {'status': 'no_namespace'})
        self.assertEqual(invariants.project_counts(self.project / 'gone'),
                         {'status': 'unreadable', 'error': 'FileNotFoundError'})
