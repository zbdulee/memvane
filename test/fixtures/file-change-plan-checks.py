"""Pure file-change planning and the ordered effect adapters around it.

Synthetic values and fake adapters only: no QMD, model, daemon, queue file,
Git or network. The single filesystem case scans a disposable directory under
the disposable HOME the runner provides.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'core'))
import file_change_plan as P


def meta(n, digest=None):
    value = {'mtimeNs': n, 'size': n}
    if digest:
        value['sha256'] = digest
    return value


class PlanChangeSet(unittest.TestCase):
    def entries(self):
        return [
            # Deliberately not sorted: the wiki cap consumes changes in this order.
            ('zeta', '/w/zeta', {'a.md': meta(1)}, {'a.md': meta(2), 'b.md': meta(1)}, ()),
            ('alpha', '/w/alpha', {'gone.md': meta(1)}, {}, ()),
            ('src', '/w/src', {}, {'s.md': meta(1)}, ()),
            ('quiet', '/w/quiet', {'q.md': meta(1)}, {'q.md': meta(1)}, ()),
            # An unreadable directory is not a delete; its prior entries stay.
            ('locked', '/w/locked', {'d/x.md': meta(1), 'y.md': meta(1)}, {'y.md': meta(1)}, ('d',)),
        ]

    def test_change_set_keeps_order_roles_and_unreadable_entries(self):
        entries = self.entries()
        before = copy.deepcopy(entries)
        plan = P.plan_change_set(entries, lambda name: name != 'src')
        self.assertEqual(entries, before, 'planning must not mutate its inputs')
        self.assertEqual(list(plan['changedFiles']), ['zeta', 'alpha', 'src'])
        self.assertEqual(plan['changedFiles']['zeta'], {
            'root': '/w/zeta', 'created': ['b.md'], 'updated': ['a.md'], 'deleted': []})
        self.assertEqual(plan['changedFiles']['alpha']['deleted'], ['gone.md'])
        # A source role reaches the wiki change set but never the index targets.
        self.assertEqual(plan['indexTargets'], {'zeta': '/w/zeta', 'alpha': '/w/alpha'})
        self.assertEqual(plan['totals'], {'created': 2, 'updated': 1, 'deleted': 1})
        self.assertEqual(set(plan['collections']), {'zeta', 'alpha', 'src', 'quiet', 'locked'})
        self.assertEqual(plan['collections']['locked']['files'],
                         {'d/x.md': meta(1), 'y.md': meta(1)})
        self.assertNotIn('locked', plan['changedFiles'])

    def test_without_role_filter_every_changed_collection_is_a_target(self):
        plan = P.plan_change_set(self.entries())
        self.assertEqual(list(plan['indexTargets']), ['zeta', 'alpha', 'src'])

    def test_markdown_hash_change_is_an_update_even_with_equal_stat(self):
        plan = P.plan_change_set([('c', '/c', {'n.md': meta(1, 'old')},
                                   {'n.md': meta(1, 'new')}, ())])
        self.assertEqual(plan['changedFiles']['c']['updated'], ['n.md'])

    def test_hashed_scan_carries_revisions_only_for_live_hashed_paths(self):
        previous = {'old.md': meta(1, 'h-old'), 'gone.md': meta(1, 'h-gone'),
                    'same.md': meta(1, 'h-same'), 'plain.txt': meta(1)}
        scanned = {'old.md': meta(1, 'h-new'), 'new.md': meta(2, 'h-created'),
                   'same.md': meta(1, 'h-same'), 'plain.txt': meta(2)}
        plan = P.plan_change_set([('c', '/c', previous, scanned, ())])
        entry = plan['changedFiles']['c']
        # A delete has nothing to read; an unhashed update has no revision.
        self.assertEqual(entry['revisions'], {'new.md': 'h-created', 'old.md': 'h-new'})
        self.assertEqual(entry['updated'], ['old.md', 'plain.txt'])
        # The snapshot side is the scanned metas, untouched by the revision view.
        self.assertEqual(plan['collections']['c']['files'], scanned)

    def test_unhashed_change_set_has_no_revisions_key(self):
        plan = P.plan_change_set([('c', '/c', {'a.md': meta(1)}, {'a.md': meta(2), 'b.md': meta(1)}, ())])
        self.assertEqual(set(plan['changedFiles']['c']), {'root', 'created', 'updated', 'deleted'})


class ApplyChangeSet(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.persisted = None
        self.previous = {'collections': {'docs': {'root': '/d', 'files': {
            'kept.md': meta(1), 'held.md': meta(1)}}}}
        self.snapshot = {'collections': {'docs': {'root': '/d', 'files': {
            'kept.md': meta(2), 'held.md': meta(2), 'new.md': meta(1)}}}}
        self.changed = {'docs': {'root': '/d', 'created': ['new.md'],
                                 'updated': ['held.md', 'kept.md'], 'deleted': []}}

    def adapters(self, *, wiki_result=None, fail=None, index=True):
        def step(name, value=None):
            if fail == name:
                raise OSError(f'synthetic {name} failure')
            self.calls.append(name)
            return value

        def persist(snapshot):
            step('persist')
            self.persisted = copy.deepcopy(snapshot)
        result = wiki_result or {'queued': 1, 'deferred': 0, 'deferredFiles': [], 'reason': 'queued'}
        return {'enqueue_index': (lambda targets: step('index', targets)) if index else None,
                'enqueue_wiki': lambda files: step('wiki', result),
                'persist': persist,
                'ack': lambda compiled: step('ack')}

    def apply(self, targets, **options):
        return P.apply_change_set(targets, self.changed, self.snapshot, self.previous,
                                  **self.adapters(**options))

    def test_effects_run_in_order_and_ack_follows_persist(self):
        compiled = self.apply({'docs': '/d'})
        self.assertEqual(self.calls, ['index', 'wiki', 'persist', 'ack'])
        self.assertEqual(compiled['queued'], 1)
        self.assertEqual(self.persisted, self.snapshot)

    def test_wiki_deferred_files_do_not_advance_before_persist(self):
        deferred = {'queued': 1, 'deferred': 2, 'reason': 'capped',
                    'deferredFiles': [('docs', 'held.md'), ('docs', 'new.md')]}
        self.apply({'docs': '/d'}, wiki_result=deferred)
        files = self.persisted['collections']['docs']['files']
        self.assertEqual(files['held.md'], meta(1), 'deferred update keeps its prior entry')
        self.assertNotIn('new.md', files, 'deferred create is dropped so it is seen again')
        self.assertEqual(files['kept.md'], meta(2))

    def test_skipped_or_empty_index_still_runs_wiki_persist_and_ack(self):
        self.apply({'docs': '/d'}, index=False)
        self.assertEqual(self.calls, ['wiki', 'persist', 'ack'])
        self.calls.clear()
        self.apply({})
        self.assertEqual(self.calls, ['wiki', 'persist', 'ack'])

    def test_failure_before_persist_never_persists_or_acks(self):
        for failing, done in (('index', []), ('wiki', ['index']), ('persist', ['index', 'wiki'])):
            self.calls.clear(); self.persisted = None
            with self.assertRaises(OSError):
                self.apply({'docs': '/d'}, fail=failing)
            self.assertEqual(self.calls, done, failing)
            self.assertIsNone(self.persisted, failing)


class SyncCompatibility(unittest.TestCase):
    def test_sync_reexports_and_build_snapshot_uses_the_same_plan(self):
        import sync
        self.assertIs(sync.plan_collection, P.plan_collection)
        self.assertIs(sync.compare_collection, P.compare_collection)
        self.assertIs(sync.defer_snapshot_entries, P.defer_snapshot_entries)
        base = Path(tempfile.mkdtemp(prefix='qmd-file-change-plan-',
                                     dir=os.environ['HOME'])).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        (base / 'b').mkdir(); (base / 'a').mkdir()
        (base / 'b/new.md').write_text('# b\n')
        (base / 'a/same.md').write_text('# a\n')
        info = (base / 'a/same.md').stat()
        previous = {'collections': {
            'a': {'root': str(base / 'a'), 'files': {
                'same.md': {'mtimeNs': info.st_mtime_ns, 'size': info.st_size}}},
            'gone': {'root': str(base / 'gone'), 'files': {'kept.md': meta(1)}}}}
        snapshot, totals, changed, changed_files, warnings = sync.build_snapshot(
            str(base), None, [('b', base / 'b'), ('a', base / 'a'), ('gone', base / 'gone')],
            previous)
        self.assertEqual(totals, {'created': 1, 'updated': 0, 'deleted': 0})
        self.assertEqual(changed, {'b': str(base / 'b')})
        self.assertEqual(changed_files['b']['created'], ['new.md'])
        # A missing root keeps its prior entry and is reported, not deleted.
        self.assertEqual(snapshot['collections']['gone'], previous['collections']['gone'])
        self.assertEqual(warnings, [{'collection': 'gone', 'reason': 'missing_root'}])
        self.assertEqual(snapshot['version'], 1)


if __name__ == '__main__':
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(loader.loadTestsFromTestCase(case)
                               for case in (PlanChangeSet, ApplyChangeSet, SyncCompatibility))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
