"""Released consumers reconcile a returned partial on either side of claim unlink."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.wiki.intake import api as intake
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config


REPO = Path(__file__).resolve().parents[5]
DRIVER = Path(__file__).resolve().parents[1] / 'tagged_backfill_return.py'
RELEASES = {
    'v1.0.22': 'b1e12bf9de0414255e011670093ba365af91c887',
    'v1.0.23': 'bc77a3cf75ec96d8de81c610c766463b841205be',
    'v1.0.24': 'f7293ec14dbc82f3b3be0c84a1135030688cb0f7',
    'v1.1.0': '3f99f775c63ee873b72a5f325092eeb562a3a509',
}


class BackfillReturnControls(FixtureCase):
    def release_tree(self, tag, expected_commit):
        archive = subprocess.check_output(['git', 'archive', tag, 'memvane', 'package.json'], cwd=REPO)
        commit = subprocess.check_output(['git', 'get-tar-commit-id'], input=archive).decode().strip()
        self.assertEqual(commit, expected_commit)
        tree = self.tmp / tag
        tree.mkdir()
        subprocess.run(['tar', '-x', '-C', str(tree)], input=archive, check=True)
        self.assertEqual(json.loads((tree / 'package.json').read_text())['version'], tag[1:])
        return tree

    def dispatch_frozen_job(self, tree, name):
        project = make_project(self.home, dirname=name, git=False)
        source = project.write_note('one.md', 'A durable fact.')
        key = project.raw_name + '/one.md'
        snapshot = {key: project.revision(source)['sha256']}
        state = self.tmp / (name + '-state')
        state.mkdir(mode=0o700)
        journal = state / 'install-journal.sqlite'
        journal.touch()
        roots = {project.raw_name: str(project.root), project.wiki_name: str(project.wiki)}
        decision = {'scopeId': 'a'*32, 'storeIncarnation': 'b'*32, 'installId': 'c'*32,
                    'projectRoot': str(project.root), 'journalPath': str(journal), 'recallSettings': {'skipPaths': []},
                    'storePath': str(state / 'store.sqlite'), 'collectionRoots': roots,
                    'collections': [{'name': collection, 'role': role, 'sourceRoot': roots[collection]}
                                    for collection, role in ((project.raw_name, 'raw'), (project.wiki_name, 'wiki'))]}
        bound = [decision, {'productRouteAllowed': True}]
        use_config(self, project, expanded(project, compile={'mode': 'auto-wiki',
                   'triggers': ['post_sync_source'], 'extractor': {'builtins': ['codex']}}))
        queue = project.root / '.memvane/compile/source-queue.jsonl'
        from memvane.settings import api as settings
        config = settings.find_project_config(project.root)['config']
        result = self.child(tree, 'dispatch', queue, bound, 0, config=config, snapshot=snapshot)
        self.assertEqual(result['initialBackfill']['dispatched'], 1, result)
        job = json.loads(queue.read_bytes())
        cohort, = state.glob('*.backfill.json')
        self.assertEqual(json.loads(cohort.read_bytes())['id'], job['initialBackfill'])
        self.assertEqual(job['contentRevision'], snapshot[key])
        job['_requeue_count'] = 3
        queue.write_text(json.dumps(job) + '\n')
        return queue, job, cohort, key, bound

    def child(self, tree, phase, queue, bound, code, **fields):
        request = {'phase': phase, 'queue': str(queue), 'bound': bound, 'home': str(self.home), **fields}
        child = subprocess.run([sys.executable, '-B', str(DRIVER)], cwd=tree, input=json.dumps(request),
                               env={**os.environ, 'PYTHONPATH': str(tree), 'PYTHONNOUSERSITE': '1'},
                               text=True, capture_output=True)
        self.assertEqual(child.returncode, code, child.stderr)
        return json.loads(child.stdout)

    def test_committed_partial_before_and_after_unlink(self):
        for tag, commit in RELEASES.items():
            tree = self.release_tree(tag, commit)
            for phase in ('before-unlink', 'after-unlink'):
                with self.subTest(tag=tag, phase=phase):
                    queue, job, cohort, key, bound = self.dispatch_frozen_job(tree, tag + '-' + phase)
                    owner = self.child(tree, phase, queue, bound, 17)
                    claim = queue.parent / owner['claim']
                    self.assertEqual(claim.name.split('.')[-2], str(owner['pid']))
                    before = phase == 'before-unlink'
                    self.assertEqual(claim.exists(), before)
                    if before:
                        self.assertEqual(json.loads(claim.read_bytes()), job)
                    private = queue.parent / '.handoff/v1/case'
                    self.assertEqual(json.loads((private / 'raw').read_bytes()), job)
                    self.assertEqual(json.loads((private / 'work').read_bytes()), {'remaining': ['B'], 'claim': claim.name})
                    self.assertEqual((private / 'committed').read_text(), claim.name)
                    partial = dict(job, partialCompile=True)
                    self.assertEqual(json.loads(queue.read_bytes()), partial)
                    self.assertEqual(self.child(tree, 'consume', queue, bound, 0), [partial])
                    self.assertFalse(claim.exists())
                    item = json.loads(cohort.read_bytes())['items'][key]
                    self.assertEqual(item, {'sha256': job['contentRevision'], 'stage': 'skipped' if before else 'dispatched'})
                    ledger = queue.with_name('discard-ledger.jsonl')
                    rows = ledger.read_bytes().splitlines() if ledger.exists() else []
                    self.assertEqual(len(rows), int(before))
                    if before:
                        row = json.loads(rows[0])
                        self.assertEqual(row['requeue_count'], 4)
                        self.assertEqual(row['backfillReceipt']['initialBackfill'], job['initialBackfill'])
                        self.assertEqual(row['backfillReceipt']['contentRevision'], job['contentRevision'])
