"""1.0.21 L: prequential admission, evaluation split, operating score and checkpoints."""
from __future__ import annotations

import json
from unittest import mock

from memvane.learning.context_learning import automatic_cohort, candidate_apply, prequential, project_scope, shared
from memvane.learning.context_learning.contracts import digest, request
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import BODY, TeacherFixture, edit_sample, sampling
from tests.support.case import FixtureCase

COHORT = {'schema': project_scope.COHORT_SCHEMA, 'id': 'default-v1', 'notBeforeUnixMs': 0,
          'minTrainCases': 5, 'minQualityTrainCases': 100, 'minTrainFamilies': 5,
          'minValidationCases': 50, 'minValidationFamilies': 5, 'minEmptyValidation': 10}
ELIGIBLE = ['a', 'b'] + [f'x{i}' for i in range(8)] + ['c']


def row(laya, gold, *, at=1000, selection='selector'):
    return {'laya': laya, 'gold': gold, 'eligible': ELIGIBLE, 'capturedAt': at, 'selection': selection}


class Score(FixtureCase):
    def test_l4_metrics_match_hand_computation(self):
        rows = [row([], []), row(['a', 'b'], ['b', 'c']), row(['a'], ['a']), row(['a'], []), row([], ['c'])]
        result = prequential.score(rows, prequential.intervals([]))
        self.assertEqual(set(result), {'base'})
        value = result['base']
        self.assertEqual(value['cases'], 5)
        self.assertAlmostEqual(value['exactRate'], 2 / 5)
        self.assertAlmostEqual(value['microF1'], 2 * 2 / (4 + 4))
        self.assertAlmostEqual(value['emptyAgree'], 1 / 2)
        self.assertAlmostEqual(value['nonEmptyAgree'], 2 / 3)
        self.assertEqual(value['goldOutsideK'], 2)
        self.assertEqual((value['defaultRank'], value['unknownSelection']), (0, 0))

    def test_l4_zero_denominators_are_omitted(self):
        value = prequential.score([row([], []), row([], [])], prequential.intervals([]))['base']
        self.assertNotIn('microF1', value)
        self.assertEqual((value['exactRate'], value['emptyAgree'], value['nonEmptyAgree']), (1.0, 1.0, None))

    def test_the_rank_baseline_scores_the_first_three_eligible_cards_of_every_row(self):
        # ELIGIBLE starts a, b, x0. Rows the selector did not answer count for the baseline too.
        rows = [row([], ['a', 'b', 'x0']), row(['a'], ['a']), row([], [], selection='default_rank'),
                row([], ['c'], selection=None)]
        plain = prequential.score(rows, prequential.intervals([]))['base']
        self.assertEqual({key for key in plain if key.startswith('rank')}, set())
        value = prequential.score(rows, prequential.intervals([]), baseline=True)['base']
        self.assertEqual((value['cases'], value['rankCases']), (2, 4))
        self.assertAlmostEqual(value['rankExactRate'], 1 / 4)
        self.assertAlmostEqual(value['rankMicroF1'], 2 * (3 + 1) / (4 * 3 + 5))
        self.assertEqual({key: value[key] for key in plain}, plain)
        self.assertEqual((prequential.BASELINE_TOP, prequential.BASELINE_MIN_GOLD), (3, 30))

    def test_l5_cases_group_by_application_time_and_default_rank_is_counted_apart(self):
        history = [{'cycle_id': 'cohort-one', 'appliedAtUnixMs': 2_000_000},
                   {'cycle_id': 'cohort-two', 'appliedAtUnixMs': 3_000_000}]
        rows = [row(['a'], ['a'], at=1500), row(['a'], ['b'], at=2000), row(['a'], ['a'], at=2500),
                row(['a'], ['a'], at=2600, selection='default_rank'), row([], [], at=2700, selection='none'),
                row(['a'], ['a'], at=3500), row(['a'], ['a'], at=3600, selection=None),
                row(['a'], ['a'], at=3700, selection='unknown')]
        result = prequential.score(rows, prequential.intervals(history))
        self.assertEqual(set(result), {'base', 'cohort-one', 'cohort-two'})
        self.assertEqual((result['base']['cases'], result['base']['exactRate']), (1, 1.0))
        one = result['cohort-one']
        self.assertEqual((one['cases'], one['exactRate'], one['defaultRank'], one['unknownSelection']),
                         (2, 0.5, 2, 0))
        two = result['cohort-two']
        self.assertEqual((two['cases'], two['defaultRank'], two['unknownSelection']), (1, 0, 2))


class Cases(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.grant = {'scope': project_scope.SCOPE, 'projectRoot': str(self.project),
                      'namespaceRoot': str(self.root),
                      'operations': list(project_scope.DEFAULT_TEACHER_OPERATIONS), 'cohort': COHORT}

    def gold(self, rid, at, *, empty=False, **options):
        sample, bodies = edit_sample(rid, **options)
        self.add(rid, sample=sample, bodies=bodies, status='gold', captured_at=at)
        value = {'selected_ids': [] if empty else [sample['candidates'][0]['candidate_id']],
                 'input_sha256': digest(canonical(sample))}
        with shared._db(self.root) as db:
            db.execute('UPDATE cases SET gold=? WHERE request_id=?', (canonical(value), rid))

    def test_l2_overlapping_cases_all_admitted_without_cohort_rows(self):
        for index in range(30):
            sample, bodies = edit_sample(f'pre-{index:02d}', count=15, selected=3)
            self.add(f'pre-{index:02d}', sample=sample, bodies=bodies, captured_at=100 + index)
        with shared._db(self.root) as db:
            queued = automatic_cohort.admitted(db, self.project, self.grant, statuses=('queued',))
            tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(len(queued), 30)
        self.assertEqual({role for role, _ in queued}, {'train'})
        self.assertEqual(len({r[12] for _, r in queued}), 30)
        self.assertFalse({'project_cohort_exposure', 'project_cohort_assignments'} & tables)

    def test_l6_evaluation_cases_follow_the_job_cutoff(self):
        for index in range(6):
            self.gold(f'j{index}', 100 + index)
        self.gold('early', 103.5)
        for index in range(4):
            self.gold(f'late{index}', 200 + index, empty=index % 2 == 0)
        with shared._db(self.root) as db:
            rows = prequential.training_rows(db, self.project, self.grant)
        members = [{'request_id': r[1], 'input_sha256': r[2]} for r in rows if r[1].startswith('j')]
        job = {'id': 'cohort-' + '0' * 20, 'members': members}
        self.assertEqual(prequential.cutoff(rows, job), 105)
        evaluation = prequential.split(rows, job)
        self.assertEqual([r[1] for r in evaluation], ['late0', 'late1', 'late2', 'late3'])
        self.assertEqual(len(rows), 11)
        self.assertEqual(prequential.split(rows, None), [])
        with shared._db(self.root) as db:
            train, validation = automatic_cohort.training_and_validation(db, self.project, self.grant, job)
            self.assertEqual(len(train), 11)
            self.assertEqual([r[1] for r in validation], [r[1] for r in evaluation])
            # Without a job and with no promotion candidate there is nothing to evaluate.
            self.assertEqual(automatic_cohort.training_and_validation(db, self.project, self.grant)[1], [])

    def test_l10_an_explicit_grant_keeps_the_cohort_assignment(self):
        rows = [{'candidate_id': 'wiki/card0.md', 'revision_sha256': digest(BODY), 'excerpt': BODY,
                 'eligible': True}]
        sample = request({'prompt': 'Which card keeps the cobalt marker?'}, rows, host='codex',
                         request_id='explicit-1', sampling=sampling(['wiki/card0.md'], []))
        self.add('explicit-1', sample=sample, bodies={'wiki/card0.md': BODY}, captured_at=10 ** 10)
        grant = dict(self.grant, operations=list(project_scope.OPERATIONS))
        with shared._db(self.root) as db:
            admitted = automatic_cohort.admitted(db, self.project, grant, statuses=('queued',))
            count = db.execute('SELECT COUNT(*) FROM project_cohort_assignments').fetchone()[0]
        self.assertEqual([r[1] for _, r in admitted], ['explicit-1'])
        self.assertEqual(count, 1)


class Annotation(TeacherFixture):
    def setUp(self):
        super().setUp()
        self.state = self.home / 'state'
        self.state.mkdir(mode=0o700)
        self.decision = {'state': 'selected', 'journalPath': str(self.state / 'journal.json')}

    def record(self, rid, selection):
        from memvane.diagnostics import api as diagnostics
        self.assertTrue(diagnostics.emit('recall.edit', outcome='served', reason='injected',
                                         state_root=self.state, request_id=rid, selection=selection))

    def test_selection_and_checkpoint_are_kept_at_gold_time(self):
        sample, _ = self.add('sel-1', captured_at=2500)
        self.add('sel-2', captured_at=1500)
        self.add('sel-3', captured_at=3500)
        self.record('sel-1', 'selector')
        self.record('sel-2', 'default_rank')
        (self.root / prequential.HISTORY).write_text(json.dumps({'schema': prequential.HISTORY_SCHEMA,
            'entries': [{'cycle_id': 'cohort-one', 'appliedAtUnixMs': 2_000_000}]}))
        (self.root / prequential.HISTORY).chmod(0o600)
        with mock.patch('memvane.journal.api.select_route', return_value=self.decision):
            one = prequential.gold_annotation(sample, ['wiki/card0.md'], root=self.root, project=self.project)
            two = prequential.gold_annotation(self.cases_sample('sel-2'), [], root=self.root, project=self.project)
            three = prequential.gold_annotation(self.cases_sample('sel-3'), [], root=self.root, project=self.project)
        self.assertEqual((one['selection'], one['checkpoint'], one['exact']), ('selector', 'cohort-one', True))
        self.assertEqual((two['selection'], two['checkpoint']), ('default_rank', 'base'))
        self.assertEqual((three['selection'], three['checkpoint']), ('unknown', 'cohort-one'))
        with shared._db(self.root) as db:
            kept = dict(db.execute('SELECT request_id,selection FROM prequential_selection'))
        self.assertEqual(kept, {'sel-1': 'selector', 'sel-2': 'default_rank'})

    def cases_sample(self, rid):
        with shared._db(self.root) as db:
            return json.loads(db.execute('SELECT sample FROM cases WHERE request_id=?', (rid,)).fetchone()[0])

    def test_a_pass_keeps_the_selection_before_gold_and_reads_the_log_once(self):
        grant = {'projectRoot': str(self.project), 'operations': list(project_scope.DEFAULT_TEACHER_OPERATIONS),
                 'cohort': COHORT}
        sample, _ = self.add('pre-1', captured_at=10)
        self.add('pre-2', captured_at=11)
        self.record('pre-1', 'selector')
        from memvane.diagnostics import api as diagnostics
        reads = mock.Mock(side_effect=diagnostics.build_progress_events)
        with mock.patch('memvane.journal.api.select_route', return_value=self.decision), \
                mock.patch('memvane.diagnostics.api.build_progress_events', reads):
            self.assertEqual(prequential.prerecord_selections(self.root, self.project, grant), 1)
            self.assertEqual(reads.call_count, 1)
            # Not in the log yet: no row, looked up again on the next pass.
            self.assertEqual(prequential.prerecord_selections(self.root, self.project, grant), 0)
            self.assertEqual(reads.call_count, 2)
            for name in ('events.jsonl', 'events.jsonl.1', 'events.jsonl.2'):
                if (self.state / name).exists():
                    (self.state / name).unlink()
            self.assertEqual(diagnostics.build_progress_events(self.state), [])
            value = prequential.gold_annotation(sample, [], root=self.root, project=self.project)
            self.assertEqual(value['selection'], 'selector')
            self.record('pre-2', 'default_rank')
            self.assertEqual(prequential.prerecord_selections(self.root, self.project, grant), 1)
            calls = reads.call_count
            self.assertEqual(prequential.prerecord_selections(self.root, self.project, grant), 0)
            self.assertEqual(reads.call_count, calls)
        with shared._db(self.root) as db:
            kept = dict(db.execute('SELECT request_id,selection FROM prequential_selection'))
        self.assertEqual(kept, {'pre-1': 'selector', 'pre-2': 'default_rank'})

    def test_an_unreadable_log_leaves_the_case_unknown_and_never_raises(self):
        sample, _ = self.add('sel-x', captured_at=10)
        with mock.patch('memvane.journal.api.select_route', side_effect=RuntimeError('fixture')):
            value = prequential.gold_annotation(sample, [], root=self.root, project=self.project)
        self.assertEqual((value['selection'], value['checkpoint']), ('unknown', 'base'))

    def test_history_appends_the_applied_pointer_once_with_its_intent_time(self):
        pointer = {'schema_version': 1, 'artifact_path': '/a', 'artifact_sha256': '0' * 64,
                   'cycle_id': 'cohort-' + '1' * 20,
                   'application': {'schema': candidate_apply.SCHEMA, 'id': 'f' * 32}}
        for path, value in ((self.root / 'active-checkpoint.json', pointer),
                            (self.root / candidate_apply.JOURNAL,
                             {'apply_id': 'f' * 32, 'new_receipt': {'createdAt': 1234.5}})):
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.assertTrue(prequential.sync_history(self.root))
        self.assertTrue(prequential.sync_history(self.root))
        self.assertEqual(prequential.read_history(self.root),
                         [{'cycle_id': pointer['cycle_id'], 'appliedAtUnixMs': 1234500}])
        self.assertEqual((self.root / prequential.HISTORY).stat().st_mode & 0o777, 0o600)
