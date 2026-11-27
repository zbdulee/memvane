"""``learning.api.inspect_project``: the status screen's read of a default-teacher namespace; it writes nothing."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3

from memvane.learning import api
from memvane.learning.context_learning import invariants, prequential, project_scope, shared
from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.store import canonical
from memvane.learning.tests.fake_teacher_cli_121 import edit_sample
from memvane.learning.tests.old_bundle_121 import EnabledInstall


class InspectProject(EnabledInstall):
    def inspected_tree(self):
        """Namespace files with mtimes, and the database bytes (absent when there is none)."""
        files = {}
        for path in sorted(self.root.rglob('*')):
            info = path.lstat()
            files[str(path.relative_to(self.root))] = (
                path.read_bytes() if path.is_file() and not path.is_symlink() else None, info.st_mtime_ns)
        return files

    def inspect(self, **options):
        before = self.inspected_tree()
        result = api.inspect_project(self.project, **options)
        self.assertEqual(self.inspected_tree(), before)
        return result

    def grant(self):
        return read_private_json(self.root / project_scope.NAME)['proposal']

    def gold(self, count):
        """Gold edit cases captured inside the accepted cohort, shaped as the capture writer stores them."""
        start = self.grant()['cohort']['notBeforeUnixMs'] / 1000 + 1
        with shared._db(self.root) as db:
            for index in range(count):
                rid = 'inspect-%d' % index
                sample, bodies = edit_sample(rid)
                input_sha = digest(canonical(sample))
                chosen = [sample['candidates'][0]['candidate_id']] if index % 2 else []
                db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                           'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status,gold) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (self.key(), str(self.project), rid, input_sha, canonical(sample), canonical(bodies),
                            'corpus', start + index, 'train', '[]', 'active', 'gold',
                            canonical({'selected_ids': chosen, 'input_sha256': input_sha,
                                       'review': 'single_pass'})))

    def test_a_namespace_without_a_database_is_no_namespace_and_none_is_created(self):
        self.assertFalse((self.root / shared.DATABASE_NAME).exists())
        self.assertEqual(self.inspect(), {'status': 'no_namespace'})
        self.assertEqual(self.inspect(check_invariants=True), {'status': 'no_namespace'})
        self.assertFalse((self.root / shared.DATABASE_NAME).exists())

    def test_counts_pointers_and_teacher_match_the_writers_and_nothing_changes(self):
        self.seed_122_rows(include_queued=False)
        self.gold(4)
        result = self.inspect()
        self.assertEqual(result['status'], 'read', result)
        self.assertNotIn('errors', result)
        self.assertNotIn('invariants', result)
        # The worker pass's own counts (``learning.score``) are the oracle for the four promotion counts.
        with shared._db(self.root) as db:
            progress = prequential.progress(db, self.project, self.grant())
        self.assertEqual(result['caseRows'], progress['caseRows'])
        self.assertEqual(result['caseLimit'], progress['caseLimit'])
        self.assertEqual({key: result['promotion'][key] for key in ('gold', *api_needs())},
                         {key: progress[key] for key in ('gold', *api_needs())})
        self.assertEqual((result['promotion']['gold'], result['caseRows']), (4, 7))
        self.assertEqual(result['promotion']['needs'], {'candidateTrain': 100, 'candidateEval': 50,
                                                        'candidateEvalNonEmpty': 10, 'candidateEvalEmpty': 10})
        active = read_private_json(self.root / 'active-checkpoint.json')
        self.assertEqual(result['pointers']['active']['cycle_id'], active['cycle_id'])
        self.assertEqual(result['pointers']['active']['artifact_sha256'], active['artifact_sha256'])
        self.assertEqual((result['pointers']['pending'], result['pointers']['journalPhase']), (None, None))
        self.assertEqual((result['teacher']['off'], result['teacher']['enablePending']), (False, False))
        self.assertEqual(set(result['teacher']['hosts']), {'claude', 'codex'})
        # The same score the worker pass records, per checkpoint; no selection kind kept yet.
        self.assertEqual(set(result['scores']), {prequential.BASE})
        self.assertEqual((result['scores'][prequential.BASE]['cases'],
                          result['scores'][prequential.BASE]['unknownSelection']), (0, 4))
        self.assertIsNone(result['evaluation'])
        json.dumps(result)

    def test_the_invariant_check_is_read_only_when_asked(self):
        self.seed_122_rows(include_queued=False)
        result = self.inspect(check_invariants=True)
        self.assertEqual(result['invariants'], invariants.project_counts(self.project))
        self.assertEqual(result['invariants']['status'], 'checked')

    def test_an_open_application_journal_is_the_pending_pointer(self):
        from memvane.learning.context_learning import candidate_apply
        from memvane.learning.context_learning.local_cycle import _atomic_json
        self.seed_122_rows(include_queued=False)
        new = dict(read_private_json(self.root / 'active-checkpoint.json'), cycle_id='cohort-' + 'a' * 20)
        _atomic_json(self.root / candidate_apply.JOURNAL, {'phase': 'prepared', 'cycle_id': new['cycle_id'],
                                                           'new_pointer': new})
        pointers = self.inspect()['pointers']
        self.assertEqual((pointers['journalPhase'], pointers['pending']['cycle_id'], pointers['pending']['phase']),
                         ('prepared', new['cycle_id'], 'prepared'))

    def test_a_teacher_switched_off_reads_off(self):
        from memvane.learning.context_learning import teacher_state
        self.seed_122_rows(include_queued=False)
        self.assertTrue(teacher_state.write_switch(self.root, False))
        self.assertTrue(self.inspect()['teacher']['off'])

    def test_an_unsafe_database_names_the_failure_and_the_files_still_read(self):
        self.seed_122_rows(include_queued=False)
        os.chmod(self.root / shared.DATABASE_NAME, 0o644)
        result = self.inspect()
        self.assertEqual((result['status'], result['errors'], result['caseRows']),
                         ('read', {'database': 'unsafe_shared_database'}, None))
        self.assertIsNotNone(result['pointers']['active'])

    def test_a_path_that_is_gone_answers_without_raising(self):
        self.assertEqual(api.inspect_project(self.project / 'gone'),
                         {'status': 'unreadable', 'error': 'FileNotFoundError'})

    def test_the_database_is_opened_query_only(self):
        from memvane.learning import project_inspection
        self.seed_122_rows(include_queued=False)
        with closing(project_inspection._open(self.root / shared.DATABASE_NAME)) as db:
            self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE probe (x)')


def api_needs():
    from memvane.learning.project_inspection import PROMOTION_NEEDS
    return tuple(PROMOTION_NEEDS)
