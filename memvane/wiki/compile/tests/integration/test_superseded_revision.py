"""Cards a newer extraction of their source no longer produces: re-extract, then retire.

State table: ``docs/dev/progress-wiki-stale-retire.md`` (lane C, H and C cells). Every card
and verify row comes from a real writer: this release's compile writer (in process, the
way the worker's ``wiki-compile`` child runs it) or the released 1.1.6 writer, and the
real verify worker. The legacy state "one verified sibling proves the new revision, the
other cards still cite the old one" is what the released worker left behind when an
extraction of the new revision produced only some of the cards: the writer is called
for those cards alone. Paid calls are the fake host CLI's calls (``extract``/``verify``).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

from memvane.wiki.compile import supersession, wiki_compile as writer, wiki_compile_worker as worker
from memvane.wiki.compile.tests.release_tags import archived_tag
from memvane.wiki.pipeline import api as pipeline
from memvane.wiki.pipeline.types import DAY_NS
from memvane.wiki.verify import api as verify
from memvane.wiki.workspace import api as workspace
from tests.support.case import FixtureCase
from tests.support.engines_fake import FakeEngines
from tests.support.project import make_project
from tests.support.settings_fake import expanded, use_config

REPO = Path(__file__).resolve().parents[5]
TESTS = Path(__file__).resolve().parents[1]
CLAIM = {'claim': 'c', 'supported': True, 'quote': 'q'}
PASS = {'verdict': 'pass', 'claims': [CLAIM], 'reasons': []}
FAIL = {'verdict': 'fail', 'claims': [{**CLAIM, 'supported': False}], 'reasons': ['contradicted']}
REV1 = '# Source\n\nDurable decision: cards cite source.\n'
REV2 = '# Source\n\nDurable decision: cards cite source.\n\nSecond revision.\n'
REV3 = '# Source\n\nDurable decision: cards cite source.\n\nThird revision.\n'


def card(title, **extra):
    return {'title': title, 'summary': f'Durable decision about {title.lower()}.', **extra}


class SupersededRevision(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root.resolve()
        self.wiki = self.root / '.memvane/wiki'
        self.source = self.project.write_note('docs/source.md', REV1)
        self.config = expanded(self.project, compile={'semanticDedup': {'enabled': False},
                                                      'extractor': {'builtins': ['claude']}})
        use_config(self, self.project, self.config)
        raw, wiki = self.project.raw_name, self.project.wiki_name
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.root),
            'installId': 'a' * 32, 'generation': 'g-' + 'b' * 32,
            'scopeId': 'c' * 32, 'storeIncarnation': 'd' * 32,
            'collections': [{'name': raw, 'role': 'raw', 'sourceRoot': str(self.root)},
                            {'name': wiki, 'role': 'wiki', 'sourceRoot': str(self.wiki)}],
            'collectionRoots': {raw: str(self.root), wiki: str(self.wiki)},
            'recallSettings': {'skipPaths': []},
        }
        self.bound = (self.decision, {'productRouteAllowed': True})
        self.answers = []
        self.verdicts = {}
        self.fake = FakeEngines({'extract': self.extract, 'verify': self.judge}).install(self)
        self.activity = []
        from memvane.diagnostics import api as event_log
        real_wiki_event = event_log.wiki

        def activity(state_root, action, rel, title, **kwargs):
            # The install event log line of every wiki card activity (retirements included).
            self.activity.append((action, rel, kwargs.get('reason')))
            return real_wiki_event(state_root, action, rel, title, **kwargs)
        for item in (mock.patch.dict(os.environ, {'QMD_DIRTY_QUEUE': str(self.root / 'dirty-queue')}),
                     mock.patch.object(workspace, 'selected', side_effect=lambda _root: self.bound),
                     mock.patch.object(workspace, 'recheck'),
                     mock.patch.object(workspace, 'request_sync'),
                     mock.patch.object(workspace, 'request_compile'),
                     mock.patch.object(event_log, 'wiki', side_effect=activity),
                     mock.patch.object(worker, 'gather_similar_pages', return_value=[]),
                     mock.patch.object(worker, 'compile_candidate', side_effect=self.write)):
            item.start()
            self.addCleanup(item.stop)

    # -- writers -----------------------------------------------------------------------------
    def extract(self, payload, argv):
        return {'candidates': self.answers.pop(0)}

    def judge(self, payload, argv):
        content = payload['card']['content']
        for title, verdict in self.verdicts.items():
            if f'title: "{title}"' in content:
                return verdict
        return PASS

    def write(self, root, candidate, final_reason=None, check=False):
        """The compile worker's ``wiki-compile`` child, run in this process."""
        with workspace.card_write_lock(root), workspace.mutation_lock(root), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = writer._compile_locked(root, self.config, self.config['compile'], self.wiki,
                                          candidate, budget_final=final_reason, check=check)
        if code != 0:
            return None
        lines = out.getvalue().splitlines()
        return json.loads(lines[-1]) if lines else {}

    def direct(self, titles, *, trigger='post_tool_source', extra_sources=(), **fields):
        """The released worker's writer call for some cards of the source as it is now."""
        revisions = [self.project.revision(self.source)] + [self.project.revision(p) for p in extra_sources]
        refs = [{k: item[k] for k in ('kind', 'path', 'collection')} for item in revisions]
        for title in titles:
            result = self.write(self.root, {**card(title, **fields), 'sources': refs,
                                            'authoritativeSources': refs[:1], 'sourceRevisions': revisions,
                                            'engine': 'claude', 'trigger': trigger})
            self.assertIn(result['action'], ('created', 'updated'), result)

    def released_writer(self, tag, *titles):
        bundle = archived_tag(REPO, self.tmp, tag)
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(REPO)))}
        child = subprocess.run(
            [sys.executable, '-B', str(TESTS / 'tagged_superseded_cards.py'), str(bundle), str(self.root),
             json.dumps(self.config), json.dumps(self.bound), *titles],
            cwd=bundle, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(child.returncode, 0, child.stderr)
        return json.loads(child.stdout.splitlines()[-1])['actions']

    # -- worker --------------------------------------------------------------------------------
    def job(self, trigger='session_start_source'):
        return {'ts': '2026-11-28T00:00:00Z', 'trigger': trigger, 'engine': 'claude', 'cwd': str(self.root),
                'source': {'kind': 'file', 'path': 'docs/source.md', 'collection': self.project.raw_name},
                'selectedRoute': workspace.route_identity(self.bound)}

    def process(self, *answers):
        self.answers.extend(answers)
        before = len(self.log())
        result = worker.process_job_settled(self.root, self.config, self.config['compile'], self.job())
        return result, [row.get('reason') for row in self.log()[before:] if row.get('reason')]

    def run_verify(self):
        """The worker's verify pass (reviews, then ``supersession.settle``) until no row is open."""
        for _ in range(10):
            worker._run_verify_pass(self.root, self.config, self.config['compile'])
            if not self.open_rows():
                return
        self.fail(f'verify rows left open: {self.open_rows()}')

    def record(self):
        """The outcome of the source's supersession row for its current bytes, else None."""
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            row = store.get_suppression('supersession', 'docs/source.md')
        if row is None:
            return None
        current = row['bodyHash'] == self.project.revision(self.source)['sha256']
        return row['payload']['outcome'] if current else None

    # -- observation ---------------------------------------------------------------------------
    def log(self):
        return pipeline.log_rows(self.root, 'candidate')

    def calls(self, task):
        return sum(1 for call in self.fake.calls if call['task'] == task)

    def cards(self):
        return sorted(path.stem for path in self.wiki.rglob('*.md'))

    def path(self, name):
        return next(self.wiki.rglob(f'{name}.md'))

    def sha(self, name):
        return self.project.revision(self.source)['sha256'] in self.path(name).read_text(encoding='utf-8')

    def status(self, name):
        return 'verified' if '\nstatus: verified\n' in self.path(name).read_text(encoding='utf-8') else 'generated'

    def open_rows(self):
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            rows = store.queued_jobs('verify', states=('queued', 'claimed'))
        return sorted((row['payload']['kind'], row['payload']['targetPath'].rsplit('/', 1)[-1][:-3])
                      for row in rows)

    def archived(self):
        folder = self.root / '.memvane/compile/retired-wiki'
        if not folder.is_dir():
            return []
        return sorted((json.loads(path.read_text())['path'].rsplit('/', 1)[-1][:-3],
                       json.loads(path.read_text())['proofHash']) for path in folder.glob('*.json'))

    def retired(self):
        return sorted((rel.rsplit('/', 1)[-1][:-3], reason)
                      for action, rel, reason in self.activity if action == 'retired')

    # -- fixtures ------------------------------------------------------------------------------
    def legacy_state(self, *, titles=('alpha', 'beta', 'gamma'), rev1=None, released=None):
        """Every card verified at REV1, then only ``alpha`` re-written and verified at REV2."""
        if released:
            self.assertEqual(self.released_writer(released, *[t.title() for t in titles]),
                             ['created'] * len(titles))
        else:
            self.assertEqual(self.process([card(t.title()) for t in titles])[0][:2], (True, False))
        if rev1:
            rev1()
        self.run_verify()
        self.assertEqual({self.status(t) for t in titles}, {'verified'})
        self.project.write_note('docs/source.md', REV2)
        if released:
            self.assertEqual(self.released_writer(released, 'Alpha'), ['updated'])
        else:
            self.direct(['Alpha'])
        self.run_verify()
        self.assertEqual((self.status('alpha'), self.sha('alpha')), ('verified', True))
        self.assertEqual(self.open_rows(), [])
        self.fake.calls.clear()
        self.activity.clear()

    # -- H3/H4: the sibling proof no longer hides superseded cards -----------------------------
    def test_h3_h4_superseded_card_retires_after_the_rewrite_passes_review(self):
        self.legacy_state()
        alpha = self.path('alpha').read_bytes()
        result, reasons = self.process([card('Beta')])
        self.assertEqual(result[:2], (True, False))
        self.assertNotIn('already_compiled_revision', reasons)
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 0))
        self.assertEqual(self.open_rows(), [('replace', 'beta')])
        # Nothing retires before the rewritten card passed review.
        self.assertEqual((self.cards(), self.archived(), self.record()),
                         (['alpha', 'beta', 'gamma'], [], 'pending'))
        # H4: the same bytes again: the recorded attempt keeps the skip; nothing is paid.
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.run_verify()
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        self.assertEqual((self.status('beta'), self.sha('beta')), ('verified', True))
        self.assertEqual(self.cards(), ['alpha', 'beta'])
        self.assertEqual(self.archived(), [('gamma', 'superseded_by_revision')])
        self.assertEqual(self.retired(), [('gamma', 'superseded_by_revision')])
        self.assertEqual(self.record(), 'retired')
        self.assertEqual(self.path('alpha').read_bytes(), alpha)
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.run_verify()
        self.assertEqual((self.calls('extract'), self.calls('verify'), len(self.archived())), (1, 1, 1))

    def test_h3_cards_of_the_released_writer_converge_the_same_way(self):
        self.legacy_state(released='v1.1.6')
        result, reasons = self.process([card('Gamma')])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual((self.calls('extract'), self.open_rows()), (1, [('replace', 'gamma')]))
        self.run_verify()
        self.assertEqual(self.archived(), [('beta', 'superseded_by_revision')])
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))

    # -- H1/H2: every card comes back ----------------------------------------------------------
    def test_h1_single_card_refresh_replaces_it_and_retires_nothing(self):
        self.process([card('Alpha')])
        self.run_verify()
        self.project.write_note('docs/source.md', REV2)
        self.fake.calls.clear()
        self.assertEqual(self.process([card('Alpha')])[0][:2], (True, False))
        self.assertEqual((self.calls('extract'), self.open_rows(), self.archived()),
                         (1, [('replace', 'alpha')], []))
        self.run_verify()
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual((self.calls('extract'), self.calls('verify'), self.archived()), (1, 1, []))

    def test_h2_every_card_returned_is_replaced_and_none_retires(self):
        self.legacy_state()
        self.process([card('Beta'), card('Gamma')])
        self.assertEqual(self.open_rows(), [('replace', 'beta'), ('replace', 'gamma')])
        self.assertEqual((self.calls('extract'), self.record()), (1, 'settled'))
        self.run_verify()
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual((self.calls('extract'), self.calls('verify'), self.archived()), (1, 2, []))

    # -- H5: a failed replacement review --------------------------------------------------------
    def test_h5_failed_replacement_keeps_every_card_and_is_not_extracted_again(self):
        self.legacy_state()
        self.verdicts['Beta'] = FAIL
        self.process([card('Beta')])
        self.run_verify()
        self.assertEqual((self.status('beta'), self.sha('beta'), self.open_rows()), ('verified', False, []))
        # The verified gamma is not retired while its sibling's rewrite failed review.
        self.assertEqual((self.cards(), self.archived(), self.record()),
                         (['alpha', 'beta', 'gamma'], [], 'held_review_failed'))
        # The lifecycle retry (after REFRESH_RETRY_SECONDS) pays nothing for the same bytes.
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        # A new edit is a new attempt.
        self.project.write_note('docs/source.md', REV3)
        self.direct(['Alpha'])
        self.verdicts.clear()
        self.run_verify()
        self.process([card('Beta')])
        self.assertEqual(self.calls('extract'), 2)

    def test_h6_source_change_ends_a_pending_attempt_without_retirement(self):
        self.legacy_state()
        self.process([card('Beta')])
        old = self.project.revision(self.source)['sha256']
        self.project.write_note('docs/source.md', REV3)
        self.run_verify()  # the replacement review ends stale_revision; settle sees new bytes
        with pipeline.open_pipeline(self.root, readonly=True) as store:
            row = store.get_suppression('supersession', 'docs/source.md')
        self.assertEqual((row['bodyHash'], row['payload']['outcome']), (old, 'source_changed'))
        self.assertEqual((self.archived(), self.calls('verify')), ([], 0))

    # -- G5/G3: generated cards without a body digest are refreshed, never retired ------------
    def stale_revision_state(self):
        self.process([card('Alpha'), card('Beta'), card('Gamma')])
        self.project.write_note('docs/source.md', REV2)
        self.run_verify()  # every first-card review ends done/stale_revision, unpaid
        self.assertEqual(self.calls('verify'), 0)
        self.assertEqual({self.status(t) for t in ('alpha', 'beta', 'gamma')}, {'generated'})
        self.direct(['Alpha'])
        self.run_verify()
        self.assertEqual((self.status('alpha'), self.sha('alpha')), ('verified', True))
        self.fake.calls.clear()

    def test_g5_stale_revision_generated_cards_converge_without_retirement(self):
        self.stale_revision_state()
        self.process([card('Beta')])
        self.assertEqual((self.calls('extract'), self.open_rows()), (1, [('replace', 'beta')]))
        self.run_verify()
        self.assertEqual((self.status('beta'), self.process()[1]), ('verified', ['already_compiled_revision']))
        self.assertEqual((self.calls('extract'), self.calls('verify')), (1, 1))
        self.assertEqual((self.archived(), self.status('gamma'), self.record()), ([], 'generated', 'settled'))

    def test_g3_rowless_generated_cards_converge_without_retirement(self):
        self.stale_revision_state()
        with pipeline.open_pipeline(self.root) as store:
            store.prune_terminal(time.time_ns() + 31 * DAY_NS)
            self.assertEqual(store.queued_jobs('verify', states=('queued', 'claimed', 'done', 'dropped')), [])
        self.process([card('Gamma')])
        self.assertEqual((self.calls('extract'), self.open_rows()), (1, [('replace', 'gamma')]))
        self.run_verify()
        self.assertEqual((self.archived(), self.process()[1]), ([], ['already_compiled_revision']))
        self.assertEqual(self.calls('extract'), 1)

    # -- protected cards neither force an extraction nor retire ---------------------------------
    def test_human_manual_and_shared_cards_neither_force_extraction_nor_retire(self):
        other = self.project.write_note('docs/other.md', 'Other source.\n')

        def protected():
            self.direct(['Delta'], trigger='manual')
            self.direct(['Epsilon'], extra_sources=[other])
        self.legacy_state(titles=('alpha', 'beta', 'gamma', 'zeta'), rev1=protected)
        beta = self.path('beta')
        beta.write_text(beta.read_text(encoding='utf-8') + '\n## Notes\n\nHand-written note.\n', encoding='utf-8')
        gamma = self.path('gamma')
        gamma.write_text(gamma.read_text(encoding='utf-8').replace(
            'Durable decision about gamma.', 'Durable decision about gamma, corrected.'), encoding='utf-8')
        zeta = self.path('zeta')
        zeta.write_text(zeta.read_text(encoding='utf-8').replace('createdBy: memvane', 'createdBy: editor'),
                        encoding='utf-8')
        before = {name: self.path(name).read_bytes() for name in self.cards()}
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual(self.calls('extract'), 0)
        # One verified superseded card makes the source worth one extraction; only it retires.
        self.direct(['Eta'])  # at REV2
        self.run_verify()
        self.project.write_note('docs/source.md', REV3)
        self.direct(['Alpha'])
        self.run_verify()
        self.fake.calls.clear()
        self.process([card('Alpha')])
        self.run_verify()
        self.assertEqual(self.archived(), [('eta', 'superseded_by_revision')])
        self.assertEqual({name: self.path(name).read_bytes() for name in before if name != 'alpha'},
                         {name: data for name, data in before.items() if name != 'alpha'})

    # -- fail-closed: nothing retires unless the extraction finished ---------------------------
    def assert_nothing_retired(self):
        self.run_verify()
        self.assertEqual((self.archived(), self.retired()), ([], []))
        self.assertIn('gamma', self.cards())

    def assert_not_paid_again(self):
        calls = self.calls('extract')
        self.assertEqual(self.process()[1], ['already_compiled_revision'])
        self.assertEqual(self.calls('extract'), calls)

    def test_failed_extraction_retires_nothing_and_keeps_the_job(self):
        self.legacy_state()
        with mock.patch.object(worker, 'run_extractor', return_value=(None, 'extractor_timeout', None)):
            self.assertEqual(self.process()[0][:2], (False, True))
        self.assertEqual(self.record(), None)
        self.assert_nothing_retired()

    def test_unusable_answer_is_recorded_and_not_bought_again(self):
        self.legacy_state()
        self.fake.responses['extract'] = lambda payload, argv: 'not an object'
        self.assertEqual(self.process()[0][:2], (True, False))
        self.assertEqual(self.record(), 'failed')
        self.assert_nothing_retired()
        self.assert_not_paid_again()

    def test_budget_deferral_retires_nothing(self):
        self.legacy_state()
        token = worker._EXTRACTOR_BUDGET.set({'remaining': 0})
        try:
            result, reasons = self.process()
        finally:
            worker._EXTRACTOR_BUDGET.reset(token)
        self.assertEqual((result[:2], reasons, self.calls('extract'), self.record()),
                         ((False, True), ['extractor_budget_deferred'], 0, None))
        self.assert_nothing_retired()

    def test_capped_answer_retires_nothing_and_is_not_bought_again(self):
        self.legacy_state()
        self.config['compile'].setdefault('budget', {})['cardsPerSource'] = 1
        self.assertEqual(self.process([card('Beta'), card('Gamma Two')])[0][:2], (True, False))
        self.assertEqual((self.open_rows(), self.record()), ([('replace', 'beta')], 'incomplete'))
        self.assert_nothing_retired()
        self.assert_not_paid_again()

    def test_empty_answer_retires_nothing_and_is_not_bought_again(self):
        self.legacy_state()
        self.assertEqual(self.process([])[0][:2], (True, False))
        self.assertEqual(self.record(), 'incomplete')
        self.assert_nothing_retired()
        self.assert_not_paid_again()

    def test_non_object_candidate_makes_the_answer_incomplete(self):
        self.legacy_state()
        self.process([card('Beta'), 'not a card'])
        self.assertEqual(self.record(), 'incomplete')
        self.assert_nothing_retired()

    def test_rejected_candidate_retires_nothing(self):
        self.legacy_state()
        rejected = {'title': 'Beta', 'summary': 'user: hello\nassistant: hi\nuser: again\nassistant: ok'}
        self.assertEqual(self.process([rejected])[0][:2], (False, True))
        self.assert_nothing_retired()

    def test_source_change_during_extraction_retires_nothing(self):
        self.legacy_state()

        def edit(payload, argv):
            self.project.write_note('docs/source.md', REV3)
            return {'candidates': [card('Beta')]}
        self.fake.responses['extract'] = edit
        self.assertEqual(self.process()[0][:2], (False, True))
        self.assert_nothing_retired()

    def test_truncated_source_holds_retirement_and_is_not_bought_again(self):
        self.legacy_state()
        self.config['compile']['maxSourceChars'] = 20
        reasons = self.process([card('Beta')])[1]
        self.assertIn('superseded_held_truncated', reasons)
        self.assertEqual(self.record(), 'held_truncated')
        self.assert_nothing_retired()
        self.assert_not_paid_again()

    def test_merge_needed_cards_stay_and_the_rest_retire(self):
        self.legacy_state(titles=('alpha', 'beta', 'gamma', 'delta'),
                          rev1=lambda: self.process([card('Gamma', targetPath='concepts/gamma-copy.md')]))
        self.assertIn('gamma-copy', self.cards())
        self.process([card('Gamma')])  # ambiguous: gamma and gamma-copy both carry the title
        self.assertEqual((self.open_rows(), self.record()), ([], 'pending'))
        self.run_verify()
        self.assertEqual(self.archived(), [('beta', 'superseded_by_revision'),
                                           ('delta', 'superseded_by_revision')])
        self.assertIn('gamma', self.cards())
        self.assertIn('gamma-copy', self.cards())
        self.assert_not_paid_again()

    # -- ratio guard and the per-pass cap -------------------------------------------------------
    def larger_wiki(self, count=12):
        filler = self.project.write_note('docs/filler.md', 'Filler source.\n')
        revision = self.project.revision(filler)
        for index in range(count):
            result = self.write(self.root, {
                **card(f'Filler {index}'), 'sources': [{k: revision[k] for k in ('kind', 'path', 'collection')}],
                'sourceRevisions': [revision], 'engine': 'claude', 'trigger': 'post_tool_source'})
            self.assertEqual(result['action'], 'created')

    def test_ratio_guard_holds_a_source_whose_answer_drops_most_cards(self):
        self.legacy_state(titles=('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta'), rev1=self.larger_wiki)
        reasons = self.process([card('Beta')])[1]
        guarded, = [row for row in self.log() if row.get('reason') == 'superseded_retire_guarded']
        self.assertEqual((guarded['wouldRetire'], guarded['supersededCards']), (4, 5))
        self.assertIn('superseded_retire_guarded', reasons)
        self.assertEqual(self.record(), 'held_ratio')
        self.run_verify()
        self.assertEqual(self.archived(), [])
        self.assert_not_paid_again()

    def test_ratio_guard_lets_half_of_the_cards_retire(self):
        titles = ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta')
        self.legacy_state(titles=titles, rev1=self.larger_wiki)
        self.process([card('Beta'), card('Gamma'), card('Delta')])
        self.assertEqual(self.record(), 'pending')
        self.run_verify()
        self.assertEqual([name for name, _ in self.archived()], ['epsilon', 'eta', 'zeta'])

    def test_settle_retires_at_most_the_pass_cap_and_finishes_on_the_next_pass(self):
        self.legacy_state(titles=('alpha', 'beta', 'gamma', 'delta'))
        self.process([card('Beta')])
        with mock.patch.object(supersession, 'MAX_RETIRE_PER_RUN', 1):
            self.run_verify()
            self.assertEqual((len(self.archived()), self.record()), (1, 'pending'))
            self.run_verify()
        self.assertEqual([name for name, _ in self.archived()], ['delta', 'gamma'])
        self.assertEqual(self.record(), 'retired')

    def test_settle_error_is_logged_and_the_pass_goes_on(self):
        from memvane.wiki.lifecycle import api as lifecycle
        self.legacy_state()
        self.process([card('Beta')])
        with mock.patch.object(lifecycle, 'retire_card', side_effect=ValueError('selected_route_changed')):
            self.run_verify()  # the review still runs and passes
        self.assertEqual((self.status('beta'), self.sha('beta'), self.archived()), ('verified', True, []))
        self.assertIn('superseded_retire_error', [row.get('reason') for row in self.log()])
        self.assertEqual(self.record(), 'pending')
        self.run_verify()
        self.assertEqual(self.archived(), [('gamma', 'superseded_by_revision')])

    # -- released readers of the ledger row ------------------------------------------------------
    def test_released_ledger_readers_keep_and_ignore_the_supersession_row(self):
        self.legacy_state()
        self.process([card('Beta')])
        self.assertEqual(self.record(), 'pending')
        for tag in ('v1.1.4', 'v1.1.6'):
            bundle = archived_tag(REPO, self.tmp, tag)
            env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(bundle), str(REPO)))}
            for command in ('maintain',) + (('summary',) if tag == 'v1.1.6' else ()):
                child = subprocess.run(
                    [sys.executable, '-B', str(TESTS / 'tagged_released_pipeline.py'), str(bundle), command,
                     str(self.root)], cwd=bundle, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(child.returncode, 0, child.stderr)
                data = json.loads(child.stdout.splitlines()[-1])
                if command == 'maintain':
                    self.assertEqual(data['rows']['supersession'], 1, tag)
                else:
                    self.assertEqual(data['byKind']['supersession'], 1)
        self.assertEqual(self.record(), 'pending')
