"""Generated cards left without a review row: cutover import, one-time reload, kept suppression.

State table: ``docs/dev/progress-wiki-stale-retire.md`` (lane D). Every card and row here is
written by a real writer: the 1.0.24/1.1.0 compile writer for 1.x review rows, the released
1.1.4/1.1.6 cutover for the rowless state it leaves, and this release's compile writer for
2.0 rows. Paid calls are the fake host CLI's call count.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest import mock

from memvane.wiki.compile import wiki_compile as writer, wiki_compile_worker as worker
from memvane.wiki.compile.tests.release_tags import archived_tag
from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import DAY_NS, KEPT_SUPPRESSION
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.engines_fake import FakeEngines
from tests.support.project import Project, make_project
from tests.support.settings_fake import expanded

REPO = Path(__file__).resolve().parents[5]
TESTS = Path(__file__).resolve().parents[1]
CLAIM = {'claim': 'c', 'supported': True, 'quote': 'q'}
PASS = {'verdict': 'pass', 'claims': [CLAIM], 'reasons': []}
FAIL = {'verdict': 'fail', 'claims': [{**CLAIM, 'supported': False}], 'reasons': ['contradicted']}


class ReviewReload(FixtureCase):
    # -- writers -----------------------------------------------------------------------------
    def released(self, tag, script, *args):
        bundle = archived_tag(REPO, self.tmp, tag)
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(REPO)))}
        child = subprocess.run([sys.executable, '-B', str(TESTS / script), str(bundle), *map(str, args)],
                               cwd=bundle, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout.splitlines()[-1])

    def v1_project(self, tag):
        """Five 1.x compiles (alpha, beta, gamma, delta twice) and their review rows."""
        scratch = self.tmp / f'{tag}-scratch-{self.next()}'
        scratch.mkdir()
        data = self.released(tag, 'tagged_v1_reviews.py', scratch)
        self.assertEqual(data['actions'], ['created'] * 4 + ['updated'])
        self.assertEqual(len(data['rows']), 5)
        self.use(Project(Path(data['root']), 'project', []))
        return data['rows']

    def fresh_project(self):
        self.use(make_project(self.home, git=False, dirname=f'project-{self.next()}'))

    def next(self):
        self.count = getattr(self, 'count', 0) + 1
        return self.count

    def use(self, project):
        self.project = project
        self.root = project.root.resolve()
        self.wiki = self.root / '.memvane/wiki'
        self.config = expanded(project, compile={'semanticDedup': {'enabled': False}})

    def compile_card(self, name, text=None):
        """This release's compile writer: a ``review`` row, then the generated card."""
        note = self.project.write_note(f'docs/{name}.md', text or f'{name.title()} source.')
        revision = self.project.revision(note)
        candidate = {'sources': [{k: revision[k] for k in ('kind', 'collection', 'path')}],
                     'sourceRevisions': [revision], 'summary': f'{name.title()} fact.', 'title': name.title(),
                     'targetPath': f'.memvane/wiki/concepts/{name}.md', 'engine': 'claude',
                     'trigger': 'post_tool_source'}
        with workspace.card_write_lock(self.root), workspace.mutation_lock(self.root), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = writer._compile_locked(self.root, self.config, self.config['compile'], self.wiki, candidate)
        self.assertEqual(code, 0, out.getvalue())
        return self.wiki / f'concepts/{name}.md'

    def released_cutover(self, tag='v1.1.6'):
        """The cutover released 1.1.4~1.1.6 ran: 1.x review rows are discarded."""
        counts = self.released(tag, 'tagged_released_pipeline.py', 'migrate', self.root)
        self.assertEqual(counts['alreadyMigrated'], False)
        self.assertEqual(self.rows(), [])

    # -- observation -----------------------------------------------------------------------------
    def rows(self):
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            rows = store.queued_jobs('verify', states=('queued', 'claimed', 'done', 'dropped'))
            return [(row['payload']['targetPath'].rsplit('/', 1)[-1], row['state'],
                     row['payload'].get('expectedTargetHash')) for row in rows]

    def open_rows(self):
        return sorted(name for name, state, _ in self.rows() if state in ('queued', 'claimed'))

    def fake(self, answer):
        return FakeEngines({'verify': answer}).install(self)

    def run_verify(self, times=1):
        for _ in range(times):
            verify.run(self.root, self.config, self.config['compile'])

    def status(self, name):
        text = (self.wiki / f'concepts/{name}.md').read_text(encoding='utf-8')
        return 'verified' if '\nstatus: verified\n' in text else 'generated'

    def sha(self, name):
        return hashlib.sha256((self.wiki / f'concepts/{name}.md').read_bytes()).hexdigest()

    def kept(self):
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            return store.suppressions(KEPT_SUPPRESSION)

    def retention(self, days=31):
        """The retention step that removes terminal rows, run as if ``days`` had passed."""
        with pipeline.open_pipeline(self.root) as store:
            return store.prune_terminal(pipeline_now() + days * DAY_NS)

    def reload(self):
        return worker.reload_unreviewed_cards(self.root, self.config)

    # -- G2: the cutover imports unfinished 1.x rows ---------------------------------------------
    def test_g2_cutover_imports_release_rows_of_fresh_unchanged_generated_cards(self):
        for tag, claimed in (('v1.0.24', True), ('v1.1.0', False)):
            with self.subTest(tag=tag):
                self.v1_project(tag)
                compile_dir = self.root / '.memvane/compile'
                if claimed:
                    # A dead 1.x verifier's claim: the release renames the whole queue.
                    os.rename(compile_dir / 'verify-queue.jsonl', compile_dir / 'verify-queue.jsonl.claimed.4242.abcd')
                self.project.write_note('docs/beta.md', 'Beta source, edited after the card.')
                (self.wiki / 'concepts/gamma.md').unlink()
                fake = self.fake(PASS)
                self.assertIs(worker.migrate_v1_state(self.root, self.config), True)
                # alpha and delta (its last row names the refreshed card); beta is stale, gamma gone.
                self.assertEqual(self.open_rows(), ['alpha.md', 'delta.md'])
                self.assertEqual({sha for _, _, sha in self.rows()}, {self.sha('alpha'), self.sha('delta')})
                self.assertFalse(pipeline.needs_v1_migration(self.root))
                self.run_verify()
                self.assertEqual(len(fake.calls), 2)
                self.assertEqual((self.status('alpha'), self.status('delta'), self.status('beta')),
                                 ('verified', 'verified', 'generated'))
                self.run_verify()
                self.assertEqual(len(fake.calls), 2)
                # The reload after the cutover finds only the stale card: nothing to queue.
                counts = self.reload()
                self.assertEqual((counts['cards'], counts['stale'], counts['queued']), (1, 1, 0))
                self.assertIsNone(self.reload())

    def test_g2_cutover_without_a_filter_discards_the_rows(self):
        self.v1_project('v1.1.0')
        counts = pipeline.migrate_from_v1(self.root, wiki_root=self.wiki)
        self.assertEqual(counts['reviews'], 0)
        self.assertEqual(self.rows(), [])

    # -- G2 after a released cutover: the one-time reload ------------------------------------------
    def test_g2_reload_queues_rowless_fresh_cards_once_and_released_verifiers_settle_them(self):
        for tag in ('v1.1.4', 'v1.1.6'):
            with self.subTest(verifier=tag):
                self.v1_project('v1.1.0')
                self.released_cutover(tag)
                self.project.write_note('docs/beta.md', 'Beta source, edited after the card.')
                self.compile_card('zeta')
                human = self.project.write_card('concepts/human.md', title='Human', body='Notes.\n',
                                                status='generated', created_by='person')
                counts = self.reload()
                self.assertEqual({k: counts[k] for k in ('cards', 'stale', 'engineUnknown', 'open', 'queued')},
                                 {'cards': 5, 'stale': 1, 'engineUnknown': 0, 'open': 1, 'queued': 3})
                self.assertEqual(self.open_rows(), ['alpha.md', 'delta.md', 'gamma.md', 'zeta.md'])
                # The marker: a second pass queues nothing.
                self.assertIsNone(self.reload())
                self.assertEqual(len(self.rows()), 4)
                # Rows of the released shape: that release's verifier pays once per card.
                calls = self.released(tag, 'tagged_released_pipeline.py', 'verify', self.root, 2)['calls']
                self.assertEqual(sorted(Path(c).name for c in calls), ['alpha.md', 'delta.md', 'gamma.md', 'zeta.md'])
                self.assertEqual([self.status(n) for n in ('alpha', 'gamma', 'delta', 'zeta', 'beta')],
                                 ['verified'] * 4 + ['generated'])
                self.assertIn('status: generated', human.read_text())
                self.assertEqual(self.open_rows(), [])
                fake = self.fake(PASS)
                self.run_verify()
                self.assertEqual(fake.calls, [])

    def test_reload_leaves_cards_whose_producing_engine_is_unknown(self):
        self.v1_project('v1.1.0')
        self.released_cutover()
        counts = self.reload()
        self.assertEqual((counts['cards'], counts['engineUnknown'], counts['queued']), (4, 4, 0))
        self.assertIsNone(self.reload())
        self.assertEqual(self.rows(), [])

    def test_g4_a_kept_card_whose_engine_is_gone_counts_as_kept(self):
        # Retention removed every row, so no engine is known; the suppression still settles it.
        self.fresh_project()
        self.compile_card('alpha')
        fake = self.fake(FAIL)
        self.run_verify()
        self.retention()
        counts = self.reload()
        self.assertEqual((counts['kept'], counts['engineUnknown'], counts['queued']), (1, 0, 0))
        self.assertEqual(len(fake.calls), 1)

    def test_a_failed_reload_writes_nothing_and_the_next_pass_reloads_once(self):
        self.v1_project('v1.1.0')
        self.released_cutover()
        self.compile_card('zeta')
        with mock.patch.object(pipeline, 'reload_reviews', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.reload(), {'reviewReloadFailed': 'OperationalError'})
        self.assertFalse(pipeline.review_reload_marked(self.root))
        self.assertEqual(self.open_rows(), ['zeta.md'])
        self.assertEqual(self.reload()['queued'], 4)
        self.assertIsNone(self.reload())
        self.assertEqual(len(self.open_rows()), 5)
        logged = [row for row in pipeline.log_rows(self.root, 'migration') if 'reviewReload' in row]
        self.assertEqual(len(logged), 2)

    def test_reload_is_skipped_while_review_is_off(self):
        self.fresh_project()
        self.config = expanded(self.project, compile={'semanticDedup': {'enabled': False},
                                                      'verify': {'enabled': False}})
        self.assertIsNone(self.reload())
        self.assertFalse(pipeline.review_reload_marked(self.root))

    # -- G1/G4: a negative verdict and retention -------------------------------------------------------
    def test_g1_g4_a_kept_card_is_never_reviewed_again_before_or_after_retention(self):
        for retained in (False, True):
            with self.subTest(retention=retained):
                self.fresh_project()
                self.compile_card('alpha')
                fake = self.fake(FAIL)
                self.run_verify()
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(self.rows(), [('alpha.md', 'done', self.sha('alpha'))])
                self.assertEqual(self.kept(), {self.sha('alpha'): self.sha('alpha')})
                if retained:
                    self.assertEqual(self.retention()['verifyJobs'], 1)
                    self.assertEqual(self.rows(), [])
                counts = self.reload()
                self.assertEqual((counts['queued'], counts['reviewed'], counts['kept'], counts['engineUnknown']),
                                 (0, 0, 1, 0) if retained else (0, 1, 0, 0))
                self.run_verify()
                self.assertEqual(len(fake.calls), 1)
                self.assertEqual(self.kept(), {self.sha('alpha'): self.sha('alpha')}, 'retention keeps it')
                self.assertEqual(self.status('alpha'), 'generated')

    # -- G5: stale_revision ---------------------------------------------------------------------------
    def test_g5_stale_revision_cards_wait_for_their_refresh_and_a_reverted_source_is_not_paid_twice(self):
        for case in ('stale', 'reverted', 'reverted_after_retention'):
            with self.subTest(case=case):
                self.fresh_project()
                self.compile_card('alpha', 'Alpha source.')
                self.project.write_note('docs/alpha.md', 'Alpha source, changed before review.')
                fake = self.fake(PASS)
                self.run_verify()
                self.assertEqual(fake.calls, [])
                self.assertEqual([state for _, state, _ in self.rows()], ['done'])
                if case != 'stale':
                    self.project.write_note('docs/alpha.md', 'Alpha source.')
                if case == 'reverted_after_retention':
                    self.retention()
                    self.assertEqual(self.rows(), [])
                    # A later compile names the project's engine again.
                    self.compile_card('beta')
                counts = self.reload()
                expected = {'stale': (1, 0, 0), 'reverted': (0, 1, 0), 'reverted_after_retention': (0, 0, 1)}[case]
                self.assertEqual((counts['stale'], counts['reviewed'], counts['queued']), expected)
                self.run_verify()
                self.assertEqual(len([c for c in fake.calls]), 2 * expected[2])
                if case == 'reverted_after_retention':
                    self.assertEqual((self.status('alpha'), self.status('beta')), ('verified', 'verified'))

    # -- G6: a row whose card never landed ---------------------------------------------------------------
    def test_g6_a_row_without_its_card_closes_without_a_call_and_reloads_nothing(self):
        self.fresh_project()
        self.compile_card('alpha').unlink()
        counts = self.reload()
        self.assertEqual((counts['cards'], counts['queued']), (0, 0))
        fake = self.fake(PASS)
        self.run_verify()
        self.assertEqual(fake.calls, [])
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            self.assertEqual([r['state'] for r in store.queued_jobs('verify', states=('done',))], ['done'])

    def test_g1_an_open_row_is_not_queued_twice(self):
        self.fresh_project()
        self.compile_card('alpha')
        counts = self.reload()
        self.assertEqual((counts['open'], counts['queued']), (1, 0))
        fake = self.fake(PASS)
        self.run_verify(2)
        self.assertEqual(len(fake.calls), 1)

    def test_the_worker_reloads_after_the_cutover_and_before_any_claim(self):
        source = Path(worker.__file__).read_text()
        main = source[source.index('def _main():'):source.index('def migrate_v1_state')]
        call = main.index('reload_unreviewed_cards(root, config)')
        self.assertLess(main.index('migrated = migrate_v1_state(root, config)'), call)
        self.assertLess(call, main.index('return _main_locked('))


def pipeline_now():
    import time
    return time.time_ns()
