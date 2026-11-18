"""1.0.18: v5 edit cases and the one-time deletion of retired prompt cases.

Decision 10: the first worker pass of a default namespace deletes only prompt
cases of schema 4 or lower (shared ``cases`` rows and the private ``samples``
rows of the same request ids), records the count and never runs again. v5
edit cases wait untouched for the 1.0.20 teacher.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

from memvane.learning.context_learning import general_cohort, prompt_retirement, shared
from memvane.learning.context_learning.contracts import digest, request, validate_request
from memvane.learning.context_learning.store import canonical, capture
from tests.support.case import FixtureCase

ROOT = Path(__file__).resolve().parents[4]
BODY = 'The cobalt garden card keeps marker zqcobaltx.'


def candidates(count=2):
    return [{'candidate_id': f'wiki/card{index}.md', 'revision_sha256': digest(BODY + str(index)),
             'excerpt': BODY + str(index), 'eligible': True} for index in range(count)]


def sampling(ids, selected):
    return {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
            'phase_counts': {'primary': len(ids)}, 'per_phase_limit': 30, 'eligible_ids': ids,
            'captured_ids': ids, 'baseline_selected_ids': selected, 'excluded': [],
            'snapshot_failures': [], 'retrieval_truncated': False, 'assessment_complete': True,
            'complete_within_returned_bound': True, 'index_revision': 'unavailable',
            'candidate_limit': 15}


def edit_sample(request_id, *, injected=()):
    rows = candidates()
    ids = [row['candidate_id'] for row in rows]
    payload = {'prompt': 'The cobalt garden keeps its marker. ' * 8, 'context_origin': 'tool_edit',
               'edit_event': {'path_sha256': digest('notes/garden.md'), 'after_sha256': None,
                              'operation': 'write', 'session_id': 's-1', 'tool_use_id': 't-1',
                              'truncated': False, 'injected_ids': list(injected)}}
    return request(payload, rows, host='claude', request_id=request_id,
                   sampling=sampling(ids, ids[:1]))


def prompt_sample(request_id, version):
    rows = candidates()
    ids = [row['candidate_id'] for row in rows]
    meta = sampling(ids, ids[:1])
    if version == 2:
        meta.pop('candidate_limit')
        meta.update(mode='eligible-returned-pool', per_phase_limit=8)
    return request({'prompt': 'Which card keeps the cobalt marker?'}, rows, host='codex',
                   request_id=request_id, sampling=meta)


class EditSampleContract(unittest.TestCase):
    def test_edit_sample_is_v5_with_hashes_only(self):
        sample = edit_sample('edit-1', injected=['wiki/card0.md'])
        self.assertEqual((sample['schema_version'], sample['context_origin']), (5, 'tool_edit'))
        self.assertEqual(set(sample['edit_event']), {
            'path_sha256', 'after_sha256', 'operation', 'session_id', 'tool_use_id', 'truncated',
            'injected_ids'})
        self.assertNotIn('notes/garden.md', canonical(sample))
        validate_request(sample)

    def test_invalid_edit_events_are_refused(self):
        sample = edit_sample('edit-2')
        for key, value in (('operation', 'delete'), ('path_sha256', 'notes/garden.md'),
                           ('injected_ids', ['wiki/absent.md']), ('truncated', 'no'),
                           ('session_id', '')):
            with self.subTest(key=key):
                broken = json.loads(canonical(sample))
                broken['edit_event'][key] = value
                with self.assertRaises(ValueError):
                    validate_request(broken)
        extra = json.loads(canonical(sample))
        extra['edit_event']['path'] = 'notes/garden.md'
        with self.assertRaises(ValueError):
            validate_request(extra)

    def test_the_1_0_17_contract_skips_a_v5_case(self):
        # The 1.0.17 bundle's validate_request (general_cohort._keys) must see
        # an unknown schema, so its cohort admission skips the row unchanged.
        old = subprocess.run(['git', '-C', str(ROOT), 'show',
                              'v1.0.17:memvane/learning/context_learning/contracts.py'],
                             capture_output=True, text=True, timeout=30)
        if old.returncode != 0:
            self.skipTest('v1.0.17 tag unavailable in this checkout')
        namespace = {}
        exec(compile(old.stdout, 'contracts_1_0_17.py', 'exec'), namespace)
        with self.assertRaisesRegex(ValueError, '^unknown_schema$'):
            namespace['validate_request'](edit_sample('edit-old'))


class CohortSkipsEditCases(FixtureCase):
    def test_admitted_assigns_no_exposure_to_v5(self):
        project = self.home / 'project'
        project.mkdir()
        root = self.home / 'ns'
        root.mkdir(mode=0o700)
        sample = edit_sample('edit-cohort')
        bodies = {row['candidate_id']: BODY for row in sample['candidates']}
        with shared._db(root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (shared._project_key(project), str(project.resolve()), sample['request_id'],
                        digest(canonical(sample)), canonical(sample), canonical(bodies), 'corpus',
                        10 ** 10, 'train', '[]', 'active', 'queued'))
        grant = {'projectRoot': str(project.resolve()),
                 'cohort': {'id': 'general-v1', 'notBeforeUnixMs': 100}}
        with mock.patch.object(general_cohort, 'preparation_checkpoint'), shared._db(root) as db:
            self.assertEqual(general_cohort.admitted(db, project, grant), [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM project_cohort_assignments').fetchone(),
                             (0,))
            self.assertEqual(db.execute('SELECT status,use_state FROM cases').fetchone(),
                             ('queued', 'active'))


class ServiceBusy(FixtureCase):
    ROWS = [{'id': 'wiki/a.md', 'revision_sha256': '0' * 64, 'source_text': 'synthetic'}]

    def choose(self, **options):
        from memvane.learning.context_learning import live_select
        return live_select.choose(self.tmp, 'synthetic edit text', self.ROWS,
                                  corpus_fingerprint='0' * 64, **options)

    def test_an_edit_recall_never_queues_behind_a_busy_service(self):
        from memvane.learning.context_learning import inference_service
        for answer in ({'status': 'ready', 'busy': True, 'queued': 0},
                       {'status': 'ready', 'busy': False, 'queued': 2}):
            with self.subTest(answer=answer), \
                    mock.patch.object(inference_service, 'service_root', return_value=self.tmp), \
                    mock.patch.object(inference_service, '_version', return_value=answer), \
                    mock.patch.object(inference_service, 'predict') as predict:
                self.assertEqual(self.choose(skip_busy=True), (None, 'service_busy', {}))
                # A prompt recall still asks (its own deadline bounds it).
                self.assertNotEqual(self.choose()[1], 'service_busy')
                predict.assert_not_called()

    def test_an_idle_or_absent_service_is_not_busy(self):
        from memvane.learning.context_learning import inference_service, live_select
        for answer in (None, {'status': 'ready', 'busy': False, 'queued': 0}):
            with self.subTest(answer=answer), \
                    mock.patch.object(inference_service, 'service_root', return_value=self.tmp), \
                    mock.patch.object(inference_service, '_version', return_value=answer):
                self.assertFalse(live_select.service_busy())
        with mock.patch.object(inference_service, 'service_root', return_value=self.tmp), \
                mock.patch.object(inference_service, '_version', side_effect=OSError('refused')):
            self.assertFalse(live_select.service_busy())
        # No service state directory (no service ever started): not busy.
        with mock.patch.object(inference_service, 'service_root', side_effect=ValueError('absent')):
            self.assertFalse(live_select.service_busy())


class OldBundle(FixtureCase):
    def test_the_1_0_17_cohort_leaves_a_v5_case_unchanged(self):
        old = self.tmp / 'old'
        old.mkdir()
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', 'v1.0.17',
                                  'memvane'], capture_output=True, timeout=60)
        if archive.returncode != 0:
            self.skipTest('v1.0.17 tag unavailable in this checkout')
        subprocess.run(['tar', '-x', '-C', str(old)], input=archive.stdout, check=True, timeout=60)
        project = self.home / 'project'
        project.mkdir()
        root = self.home / 'ns'
        root.mkdir(mode=0o700)
        sample = edit_sample('old-bundle')
        bodies = {row['candidate_id']: BODY for row in sample['candidates']}
        with shared._db(root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (shared._project_key(project), str(project.resolve()), sample['request_id'],
                        digest(canonical(sample)), canonical(sample), canonical(bodies), 'corpus',
                        10 ** 10, 'train', '[]', 'active', 'queued'))
        script = (
            'import json, sys\n'
            'from memvane.learning.context_learning import general_cohort, shared\n'
            'assert general_cohort.__file__.startswith(sys.argv[1]), general_cohort.__file__\n'
            'grant = {"projectRoot": sys.argv[3], "cohort": {"id": "general-v1", "notBeforeUnixMs": 100}}\n'
            'with shared._db(sys.argv[2]) as db:\n'
            '    print(json.dumps(general_cohort.admitted(db, sys.argv[3], grant)))\n')
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        done = subprocess.run([__import__('sys').executable, '-B', '-c', script, str(old), str(root),
                               str(project.resolve())], capture_output=True, text=True, timeout=120,
                              cwd=str(old), env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), [])
        with closing(sqlite3.connect(root / shared.DATABASE_NAME)) as db:
            self.assertEqual(db.execute('SELECT sample,status,use_state,gold,error FROM cases').fetchone(),
                             (canonical(sample), 'queued', 'active', None, None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM project_cohort_assignments').fetchone(), (0,))


class PromptRetirement(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = (self.home / 'project')
        self.project.mkdir()
        self.root = self.home / 'ns'
        self.root.mkdir(mode=0o700)
        self.state = self.home / 'private'
        self.state.mkdir(mode=0o700)
        self.samples = {'p2': prompt_sample('p2', 2), 'p3': prompt_sample('p3', 3),
                        'e5': edit_sample('e5')}
        for sample in self.samples.values():
            self.assertEqual(capture(sample, enabled=True, state_dir=self.state), 'stored')
            bodies = {row['candidate_id']: BODY for row in sample['candidates']}
            with shared._db(self.root) as db:
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,'
                           'sample,bodies,corpus_fingerprint,captured_at,split,family_keys,'
                           'use_state,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (shared._project_key(self.project), str(self.project.resolve()),
                            sample['request_id'], digest(canonical(sample)), canonical(sample),
                            canonical(bodies), 'corpus', 1, 'train', '[]', 'active', 'gold'))
            # Another project's prompt case in the same namespace is not this pass's.
        other = prompt_sample('other-project', 3)
        with shared._db(self.root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('0' * 64, '/elsewhere', 'other-project', digest(canonical(other)),
                        canonical(other), '{}', 'corpus', 1, 'train', '[]', 'active', 'gold'))
        patcher = mock.patch('memvane.learning.context_learning.seam._private_state',
                             return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        with closing(sqlite3.connect(self.root / shared.DATABASE_NAME)) as db:
            cases = sorted(row[0] for row in db.execute('SELECT request_id FROM cases'))
        with closing(sqlite3.connect(self.state / 'learning.sqlite3')) as db:
            private = sorted(row[0] for row in db.execute('SELECT request_id FROM samples'))
        return cases, private

    def test_only_prompt_cases_of_schema_four_or_lower_are_deleted_once(self):
        with mock.patch('memvane.diagnostics.api.emit') as emit, \
                mock.patch('memvane.journal.api.select_route', return_value={'state': 'candidate'}):
            first = prompt_retirement.retire(self.root, self.project, {})
            second = prompt_retirement.retire(self.root, self.project, {})
        self.assertEqual(first, {'status': 'retired', 'count': 2})
        self.assertEqual(second, {'status': 'already_retired', 'count': 0})
        self.assertEqual(self.rows(), (['e5', 'other-project'], ['e5']))
        marker = json.loads((self.root / prompt_retirement.MARKER).read_text())
        self.assertEqual((marker['count'], marker['privateSamples']), (2, 2))
        self.assertEqual((self.root / prompt_retirement.MARKER).stat().st_mode & 0o777, 0o600)
        emit.assert_called_once()
        self.assertEqual((emit.call_args.args[0], emit.call_args.kwargs['outcome'],
                          emit.call_args.kwargs['count']),
                         ('learning.migration', 'prompt_cases_deleted', 2))

    def test_worker_pass_retires_only_in_a_default_namespace(self):
        from memvane.learning.context_learning import project_worker
        with mock.patch('memvane.learning.default_enrollment.default_local_only',
                        return_value=False):
            self.assertIsNone(project_worker._retire_prompt_cases(self.root, self.project, {}, {}))
        self.assertEqual(self.rows()[0], ['e5', 'other-project', 'p2', 'p3'])
        with mock.patch('memvane.learning.default_enrollment.default_local_only',
                        side_effect=ValueError('setup_default_scope_changed')):
            self.assertIsNone(project_worker._retire_prompt_cases(self.root, self.project, {}, {}))
        with mock.patch('memvane.learning.default_enrollment.default_local_only',
                        return_value=True), mock.patch('memvane.diagnostics.api.emit'):
            result = project_worker._retire_prompt_cases(self.root, self.project, {}, {})
        self.assertEqual(result, {'status': 'retired', 'count': 2})
        self.assertEqual(self.rows()[0], ['e5', 'other-project'])


class EditEnqueue(FixtureCase):
    def test_a_v5_case_queues_with_its_immutable_input(self):
        project = self.home / 'project'
        project.mkdir()
        root = self.home / 'ns'
        root.mkdir(mode=0o700)
        sources = {f'wiki/card{index}.md': '---\ntitle: "Card"\nsources:\n  - notes/garden.md\n---\n'
                   + BODY + str(index) + '\n' for index in range(2)}
        rows = [{'candidate_id': cid, 'revision_sha256': digest(text), 'excerpt': BODY,
                 'eligible': True} for cid, text in sources.items()]
        ids = list(sources)
        sample = request({'prompt': 'The cobalt garden keeps its marker. ' * 8,
                          'context_origin': 'tool_edit',
                          'edit_event': {'path_sha256': digest('notes/garden.md'),
                                         'after_sha256': None, 'operation': 'edit',
                                         'session_id': 's-1', 'tool_use_id': '', 'truncated': False,
                                         'injected_ids': ids[:1]}},
                         rows, host='codex', request_id='enqueue-5', sampling=sampling(ids, ids[:1]))
        with mock.patch.object(shared, '_safe_root', return_value=root), \
                mock.patch.object(shared, '_policy', return_value={}), \
                mock.patch.object(shared, '_consent', return_value=True):
            self.assertEqual(shared.enqueue({}, project, sample, sources), 'queued')
        with closing(sqlite3.connect(root / shared.DATABASE_NAME)) as db:
            input_sha, raw, bodies, status = db.execute(
                'SELECT input_sha256,sample,bodies,status FROM cases').fetchone()
        self.assertEqual(input_sha, digest(canonical(sample)))
        self.assertEqual(json.loads(raw), sample)
        self.assertEqual(status, 'queued')
        bodies = json.loads(bodies)
        self.assertEqual(set(bodies), {row['candidate_id'] for row in sample['candidates']})
        self.assertNotIn('sources:', json.dumps(bodies))
        self.assertNotIn('notes/garden.md', raw + json.dumps(bodies))


if __name__ == '__main__':
    unittest.main()
