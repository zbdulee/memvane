"""1.0.19: v5 edit cases against card changes and the git-ignore check.

F-2 (decision 12): a v5 edit case stays ``active`` when a candidate card's
source changes; its other checks (consent, tombstone, private sample) stay.
F-3 (decision 11): the git-ignore check also covers a project in a
subdirectory of a work tree; git failure refuses the case.
Decision 8: a one-time migration deletes, for an affected project only, the
v5 edit cases captured before the fix and their private samples.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

from memvane.learning.context_learning import edit_git_migration, seam, shared
from memvane.learning.context_learning.contracts import digest, request
from memvane.learning.context_learning.store import canonical, capture
from tests.support.case import FixtureCase

BODY = 'The cobalt garden card keeps marker zqcobaltx.'
EDIT = ('Field notes about the cobalt garden. The garden card keeps its marker and this '
        'paragraph was just written by the author.\n')


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


def edit_sample(request_id):
    rows = candidates()
    ids = [row['candidate_id'] for row in rows]
    payload = {'prompt': EDIT * 2, 'context_origin': 'tool_edit',
               'edit_event': {'path_sha256': digest('notes/garden.md'), 'after_sha256': None,
                              'operation': 'write', 'session_id': 's-1', 'tool_use_id': 't-1',
                              'truncated': False, 'injected_ids': []}}
    return request(payload, rows, host='claude', request_id=request_id,
                   sampling=sampling(ids, ids[:1]))


def prompt_sample(request_id):
    rows = candidates()
    ids = [row['candidate_id'] for row in rows]
    return request({'prompt': 'Which card keeps the cobalt marker?'}, rows, host='codex',
                   request_id=request_id, sampling=sampling(ids, ids[:1]))


def git(*args, cwd):
    subprocess.run(['git', *args], cwd=str(cwd), check=True, capture_output=True, timeout=30)


def insert_case(root, project, sample, captured_at, *, key=None):
    bodies = {row['candidate_id']: BODY for row in sample['candidates']}
    with shared._db(root) as db:
        db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                   'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (key or shared._project_key(project), str(Path(project).resolve()),
                    sample['request_id'], digest(canonical(sample)), canonical(sample),
                    canonical(bodies), 'corpus', captured_at, 'train', '[]', 'active', 'queued'))


class EditCaseKeepsActive(FixtureCase):
    """F-2: only the source-revision check is skipped for a v5 edit case."""

    def setUp(self):
        super().setUp()
        self.project = self.home / 'project'
        (self.project / 'wiki').mkdir(parents=True)
        for index in range(2):
            (self.project / 'wiki' / f'card{index}.md').write_text(BODY + str(index),
                                                                    encoding='utf-8')
        self.root = self.home / 'ns'
        self.root.mkdir(mode=0o700)
        self.state = self.home / 'private'
        self.state.mkdir(mode=0o700)
        self.cfg = {'collectionPaths': {'wiki': 'wiki'}}
        for patcher in (mock.patch.object(shared, '_current_config', return_value=self.cfg),
                        mock.patch.object(seam, '_private_state', return_value=self.state)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def add(self, sample):
        self.assertEqual(capture(sample, enabled=True, state_dir=self.state), 'stored')
        insert_case(self.root, self.project, sample, 1)

    def states(self):
        with closing(sqlite3.connect(self.root / shared.DATABASE_NAME)) as db:
            return dict(db.execute('SELECT request_id,use_state FROM cases'))

    def test_a_changed_card_keeps_a_v5_case_and_demotes_a_prompt_case(self):
        self.add(edit_sample('edit'))
        self.add(prompt_sample('prompt'))
        self.assertEqual(shared._reconcile_cases(self.root), 0)
        (self.project / 'wiki' / 'card1.md').write_text(BODY + ' regenerated', encoding='utf-8')
        self.assertEqual(shared._reconcile_cases(self.root), 1)
        self.assertEqual(self.states(), {'edit': 'active', 'prompt': 'historical'})

    def test_a_v5_case_keeps_its_other_checks(self):
        self.add(edit_sample('tombstoned'))
        self.add(edit_sample('mismatch'))
        self.add(edit_sample('kept'))
        from memvane.learning.context_learning.store import database
        with database(self.state) as db:
            db.execute('INSERT INTO tombstones VALUES (?)', ('tombstoned',))
            db.execute('UPDATE samples SET body=? WHERE request_id=?',
                       (canonical(edit_sample('other')), 'mismatch'))
        (self.project / 'wiki' / 'card0.md').unlink()
        self.assertEqual(shared._reconcile_cases(self.root), 2)
        self.assertEqual(self.states(), {'tombstoned': 'historical', 'mismatch': 'historical',
                                         'kept': 'active'})
        with mock.patch.object(shared, '_current_config', return_value=None):
            self.assertEqual(shared._reconcile_cases(self.root), 1)
        self.assertEqual(self.states()['kept'], 'historical')

    def test_a_schema_four_case_stays_inactive(self):
        row = (str(self.project), 'v4', json.dumps({'schema_version': 4}))
        self.assertFalse(shared._case_active(self.root, row, cfg=self.cfg))


class GitIgnoreCheck(FixtureCase):
    """F-3: the check runs for a project below the work tree's top level."""

    def setUp(self):
        super().setUp()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git('init', '-q', cwd=self.repo)
        self.sub = self.repo / 'sub'
        self.sub.mkdir()
        (self.sub / '.gitignore').write_text('ignored.md\n', encoding='utf-8')
        for name in ('ignored.md', 'kept.md'):
            (self.sub / name).write_text(EDIT, encoding='utf-8')
        self.plain = self.home / 'plain'
        self.plain.mkdir()
        (self.plain / 'ignored.md').write_text(EDIT, encoding='utf-8')

    def edit(self, root, rel):
        rows = [{'candidate_id': row['candidate_id'], 'revision_sha256': row['revision_sha256'],
                 'excerpt': row['excerpt']} for row in candidates()]
        payload = {'prompt': EDIT, '_memvane_edit_paths': [rel],
                   'edit_event': {'path_sha256': digest(rel), 'operation': 'write',
                                  'session_id': 's', 'tool_use_id': 't', 'truncated': False,
                                  'injected_ids': []}}
        return seam._edit_capture(payload, root, rows, [], [], {})

    def test_an_ignored_edit_in_a_subdirectory_project_is_not_stored(self):
        self.assertFalse((self.sub / '.git').exists())
        self.assertTrue(seam.inside_git_work_tree(self.sub))
        self.assertTrue(seam._git_ignored(self.sub, ['ignored.md']))
        self.assertEqual(self.edit(self.sub, 'ignored.md'), (None, 'edit_git_ignored'))
        captured, status = self.edit(self.sub, 'kept.md')
        self.assertIsNone(status)
        self.assertEqual(captured['context_origin'], 'tool_edit')

    def test_a_directory_outside_git_still_stores(self):
        self.assertFalse(seam.inside_git_work_tree(self.plain))
        self.assertFalse(seam._git_ignored(self.plain, ['ignored.md']))
        captured, status = self.edit(self.plain, 'ignored.md')
        self.assertIsNone(status)
        self.assertEqual(captured['context_origin'], 'tool_edit')

    def test_git_failure_refuses_the_case(self):
        failures = (OSError('git missing'), subprocess.TimeoutExpired(['git'], 2),
                    subprocess.CompletedProcess(['git'], 128, '', 'fatal: detected dubious ownership'),
                    subprocess.CompletedProcess(['git'], 0, 'maybe\n', ''))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                patch = (mock.patch.object(seam.subprocess, 'run', side_effect=failure)
                         if isinstance(failure, BaseException)
                         else mock.patch.object(seam.subprocess, 'run', return_value=failure))
                with patch:
                    for root in (self.sub, self.plain):
                        with self.assertRaisesRegex(ValueError, '^edit_git_check_failed$'):
                            seam._git_ignored(root, ['kept.md'])
                        with self.assertRaisesRegex(ValueError, '^edit_git_check_failed$'):
                            self.edit(root, 'kept.md')


class FixTime(FixtureCase):
    def test_the_fix_time_is_written_once_before_the_first_queued_edit_case(self):
        project = self.home / 'project'
        project.mkdir()
        root = self.home / 'ns'
        root.mkdir(mode=0o700)
        state = self.home / 'private'
        state.mkdir(mode=0o700)
        marker = root / edit_git_migration._name(edit_git_migration.FIX_PREFIX, project)
        seen = []

        def enqueue(config, project_root, sample, compact_sources):
            seen.append((root / edit_git_migration._name(edit_git_migration.FIX_PREFIX,
                                                         project_root)).is_file())
            return 'duplicate'
        config = {'sharedLearning': {'enabled': True}}
        with mock.patch.object(shared, '_safe_root', return_value=root), \
                mock.patch.object(shared, 'enqueue', side_effect=enqueue):
            self.assertEqual(seam._store(edit_sample('first'), config, project.resolve(), state,
                                         {}, None), 'stored')
            first = marker.read_bytes()
            self.assertEqual(seam._store(edit_sample('second'), config, project.resolve(), state,
                                         {}, None), 'stored')
            # A prompt sample (a direct offline capture) records nothing.
            other = self.home / 'other'
            other.mkdir()
            seam._store(prompt_sample('prompt'), config, other.resolve(), state, {}, None)
        self.assertEqual(seen, [True, True, False])
        self.assertEqual(marker.read_bytes(), first)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(path.name for path in root.iterdir()), [marker.name])


class Migration(FixtureCase):
    """Decision 8: pre-fix v5 edit cases of an affected project go once."""

    def setUp(self):
        super().setUp()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git('init', '-q', cwd=self.repo)
        self.project = (self.repo / 'sub')
        self.project.mkdir()
        self.root = self.home / 'ns'
        self.root.mkdir(mode=0o700)
        self.state = self.home / 'private'
        self.state.mkdir(mode=0o700)
        patcher = mock.patch.object(seam, '_private_state', return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        for patcher in (mock.patch('memvane.journal.api.select_route',
                                   return_value={'state': 'candidate'}),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, project=None, *, fixed_at=1000.0):
        project = project or self.project
        rows = {'old-1': (edit_sample('old-1'), 999.0), 'old-2': (edit_sample('old-2'), 10.0),
                'new': (edit_sample('new'), 1000.0), 'prompt': (prompt_sample('prompt'), 10.0)}
        for sample, captured_at in rows.values():
            self.assertEqual(capture(sample, enabled=True, state_dir=self.state), 'stored')
            insert_case(self.root, project, sample, captured_at)
        insert_case(self.root, self.home / 'elsewhere', edit_sample('other-project'), 10.0,
                    key='0' * 64)
        if fixed_at is not None:
            path = self.root / edit_git_migration._name(edit_git_migration.FIX_PREFIX, project)
            path.write_text(json.dumps({'schema': edit_git_migration.FIX_SCHEMA,
                                        'fixedAt': fixed_at}), encoding='utf-8')
            path.chmod(0o600)

    def rows(self):
        with closing(sqlite3.connect(self.root / shared.DATABASE_NAME)) as db:
            cases = sorted(row[0] for row in db.execute('SELECT request_id FROM cases'))
        with closing(sqlite3.connect(self.state / 'learning.sqlite3')) as db:
            private = sorted(row[0] for row in db.execute('SELECT request_id FROM samples'))
        return cases, private

    def marker(self, project=None):
        return self.root / edit_git_migration._name(edit_git_migration.MARKER_PREFIX,
                                                    project or self.project)

    def test_an_affected_project_loses_only_its_pre_fix_edit_cases_once(self):
        self.seed()
        with mock.patch('memvane.diagnostics.api.emit') as emit:
            first = edit_git_migration.migrate(self.root, self.project, {})
            second = edit_git_migration.migrate(self.root, self.project, {})
        self.assertEqual(first, {'status': 'deleted', 'count': 2, 'privateSamples': 2})
        self.assertEqual(second, {'status': 'already_migrated', 'count': 0, 'privateSamples': 0})
        self.assertEqual(self.rows(), (['new', 'other-project', 'prompt'], ['new', 'prompt']))
        marker = json.loads(self.marker().read_text())
        self.assertEqual((marker['status'], marker['count'], marker['privateSamples']),
                         ('deleted', 2, 2))
        self.assertEqual(self.marker().stat().st_mode & 0o777, 0o600)
        emit.assert_called_once()
        self.assertEqual((emit.call_args.args[0], emit.call_args.kwargs['outcome'],
                          emit.call_args.kwargs['count'], emit.call_args.kwargs['privateSamples']),
                         ('learning.migration', 'edit_cases_deleted', 2, 2))
        # Numbers and the route decision only: no source text, path or prompt.
        self.assertEqual(set(emit.call_args.kwargs),
                         {'outcome', 'reason', 'decision', 'count', 'privateSamples'})
        self.assertFalse(any(path.name.startswith(edit_git_migration.PENDING_PREFIX)
                             for path in self.root.iterdir()))

    def test_without_a_fix_time_the_migration_start_is_the_cutoff(self):
        self.seed(fixed_at=None)
        insert_case(self.root, self.project, edit_sample('later'), 10.0 ** 12)
        self.assertEqual(capture(edit_sample('later'), enabled=True, state_dir=self.state),
                         'stored')
        with mock.patch('memvane.diagnostics.api.emit'):
            result = edit_git_migration.migrate(self.root, self.project, {})
        self.assertEqual(result, {'status': 'deleted', 'count': 3, 'privateSamples': 3})
        self.assertEqual(self.rows(), (['later', 'other-project', 'prompt'], ['later', 'prompt']))

    def test_unaffected_projects_keep_every_case(self):
        own = self.home / 'own'
        own.mkdir()
        git('init', '-q', cwd=own)
        plain = self.home / 'plain'
        plain.mkdir()
        for project in (own, plain):
            with self.subTest(project=project.name):
                self.seed(project)
                before = self.rows()
                with mock.patch('memvane.diagnostics.api.emit') as emit:
                    result = edit_git_migration.migrate(self.root, project, {})
                self.assertEqual(result, {'status': 'not_affected', 'count': 0,
                                          'privateSamples': 0})
                self.assertEqual(self.rows(), before)
                marker = json.loads(self.marker(project).read_text())
                self.assertEqual((marker['status'], marker['count']), ('not_affected', 0))
                self.assertEqual(emit.call_args.kwargs['outcome'], 'edit_cases_kept')
                with closing(sqlite3.connect(self.root / shared.DATABASE_NAME)) as db:
                    db.execute('DELETE FROM cases')
                    db.commit()
                with closing(sqlite3.connect(self.state / 'learning.sqlite3')) as db:
                    db.execute('DELETE FROM samples')
                    db.commit()

    def test_git_failure_writes_no_marker_and_deletes_nothing(self):
        self.seed()
        before = self.rows()
        with mock.patch.object(seam.subprocess, 'run', side_effect=OSError('git missing')):
            result = edit_git_migration.migrate_quietly(self.root, self.project, {})
        self.assertEqual(result['status'], 'migration_deferred')
        self.assertEqual(self.rows(), before)
        self.assertFalse(self.marker().exists())

    def test_counts_survive_a_crash_after_the_deletes(self):
        self.seed()
        from memvane.learning.context_learning import local_cycle
        real = local_cycle._atomic_json

        def crash_on_marker(path, value):
            if Path(path).name.startswith(edit_git_migration.MARKER_PREFIX):
                raise OSError('disk full')
            return real(path, value)
        with mock.patch.object(local_cycle, '_atomic_json', side_effect=crash_on_marker), \
                mock.patch('memvane.diagnostics.api.emit'):
            self.assertEqual(edit_git_migration.migrate_quietly(self.root, self.project, {}),
                             {'status': 'migration_deferred', 'count': 0, 'privateSamples': 0})
        self.assertEqual(self.rows(), (['new', 'other-project', 'prompt'], ['new', 'prompt']))
        with mock.patch('memvane.diagnostics.api.emit') as emit:
            result = edit_git_migration.migrate(self.root, self.project, {})
        self.assertEqual(result, {'status': 'deleted', 'count': 2, 'privateSamples': 2})
        self.assertEqual(emit.call_args.kwargs['count'], 2)

    def test_the_worker_pass_runs_the_migration(self):
        from memvane.learning.context_learning import project_worker
        self.seed()
        with mock.patch('memvane.diagnostics.api.emit'):
            result = project_worker._delete_unchecked_edit_cases(self.root, self.project, {})
        self.assertEqual(result, {'status': 'deleted', 'count': 2, 'privateSamples': 2})


if __name__ == '__main__':
    unittest.main()
