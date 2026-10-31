"""Card body budget for the local selector: measure, compile gate, split, dedup.

Synthetic projects in a private temp HOME; no owner files, service, model or
paid extractor. A fake selector runtime stands in for the checkpoint tokenizer:
it is registered the way a real live selector is (owner-only
``live-selection/<state>/live-selector.json`` under the temp HOME) and counts
3 tokens per byte-fallback character (``RARE``) and one token per 3 UTF-8
bytes of everything else.
"""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'core'))
sys.path.insert(0, str(REPO / 'core' / 'extractors'))
import wiki_card_budget as budget  # noqa: E402
from context_learning.compact_input import derive  # noqa: E402
from context_learning.contracts import digest  # noqa: E402

LONG_SUMMARY = ' '.join(f'Rule {i}: `compile.budget.cardsPerSource` caps cards per source.'
                        for i in range(120))
SHORT_SUMMARY = 'Generated wiki pages cite source markdown instead of copying raw source.'
RARE = '뷁똠쀍햏'
FAKE_TOKENIZER = f"""#!{sys.executable}
import json, math, sys
RARE = {RARE!r}
def count(text):
    rest = ''.join(c for c in text if c not in RARE)
    return 3 * (len(text) - len(rest)) + math.ceil(len(rest.encode('utf-8')) / 3)
print(json.dumps([count(t) for t in json.loads(sys.stdin.read())]))
"""


def fake_count(text):
    rest = ''.join(c for c in text if c not in RARE)
    return 3 * (len(text) - len(rest)) + math.ceil(len(rest.encode('utf-8')) / 3)


def install_selector(home: Path, name='a' * 64, broken=False) -> tuple[str, str]:
    """Register a fake live selector under ``home`` like setup does; returns its pair.

    ``broken`` installs a runtime that exits non-zero, like a failed import.
    """
    from context_learning.runtime_setup import learning_state_base
    base = learning_state_base(home)
    model = home / 'model'
    (model / 'tokenizer').mkdir(parents=True, exist_ok=True)
    (model / 'tokenizer' / 'tokenizer.json').write_text('{}')
    python = home / f'fake-python-{name[:8]}'
    python.write_text('#!/bin/sh\nexit 3\n' if broken else FAKE_TOKENIZER)
    python.chmod(0o700)
    state = base / 'live-selection' / name
    state.mkdir(parents=True, exist_ok=True)
    for path in (base, base / 'live-selection', state):
        path.chmod(0o700)
    policy = state / 'live-selector.json'
    policy.write_text(json.dumps({'schema': 'qmd-live-selector-v1', 'timeoutSeconds': 30,
                                  'maxRssMib': 4096,
                                  'trainerArgv': [str(python), str(home / 'adapter.py'),
                                                  str(model)]}))
    policy.chmod(0o600)
    return str(python), str(model / 'tokenizer' / 'tokenizer.json')


def card_text(body, title='Card', extra=''):
    return f'---\ntitle: {title}\nstatus: generated\ncreatedBy: memvane\n{extra}---\n{body}'


class Project:
    """A private temp HOME plus a legacy-route project with one raw source."""

    def __init__(self, case, selector=True):
        tmp = tempfile.TemporaryDirectory(prefix='card-budget-')
        case.addCleanup(tmp.cleanup)
        base = Path(os.path.realpath(tmp.name))
        self.home = base / 'home'
        self.home.mkdir()
        self.root = base / 'project'
        (self.root / '.auto-context' / 'compile').mkdir(parents=True)
        self.wiki = self.root / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        (self.root / 'docs').mkdir()
        (self.root / 'docs' / 'source.md').write_text(
            '# Source\n\nDurable decision: generated wiki pages cite source markdown.\n')
        (self.root / '.auto-context' / 'settings.json').write_text(json.dumps({
            'indexing': True,
            'collections': ['proj-docs', 'proj-wiki'],
            'collectionPaths': {'proj-docs': 'docs', 'proj-wiki': '.auto-context/wiki'},
            'collectionRoles': {'proj-docs': 'raw', 'proj-wiki': 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {'mode': 'auto-wiki', 'defaultStatus': 'generated',
                        'verify': {'enabled': False},
                        'extractor': {'backends': {}, 'timeout': 30}},
        }))
        # The worker compiles only for a selected route or this synthetic fixture.
        self.env = {**os.environ, 'HOME': str(self.home), 'QMD_SETUP_GUARD_FIXTURE': '1',
                    'QMD_DIRTY_QUEUE': str(base / 'dirty-queue')}
        self.env.pop('QMD_SANDBOX', None)  # wiki_compile is a no-op in a sandbox
        patcher = patch.dict(os.environ, {'HOME': str(self.home),
                                          'QMD_DIRTY_QUEUE': str(base / 'dirty-queue')})
        patcher.start()
        case.addCleanup(patcher.stop)
        self.base = base
        self.tokenizer = install_selector(self.home) if selector else None

    def compile(self, candidate):
        proc = subprocess.run([sys.executable, str(REPO / 'core' / 'wiki_compile.py'),
                               '--cwd', str(self.root)], input=json.dumps(candidate),
                              text=True, capture_output=True, env=self.env, cwd=str(REPO))
        lines = proc.stdout.strip().splitlines()
        return json.loads(lines[-1]) if lines else {'returncode': proc.returncode,
                                                    'stderr': proc.stderr}

    def ledger(self, name):
        path = self.root / '.auto-context' / 'compile' / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def state_root(self):
        state = self.base / 'state'
        state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(state, 0o700)
        return state

    def events(self, state):
        path = state / 'events.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def candidate(title='Source Compile Decision', summary=SHORT_SUMMARY, **extra):
    return {'trigger': 'manual', 'title': title, 'summary': summary,
            'suggestedType': 'decision', 'confidence': 'high',
            'canonicalKey': title.lower().replace(' ', '-'),
            'sources': [{'kind': 'file', 'path': 'docs/source.md', 'collection': 'proj-docs'}],
            **extra}


class MeasureChecks(unittest.TestCase):
    def setUp(self):
        self.p = Project(self)

    def test_measure_reads_the_selector_derive_body_only(self):
        body = '\nA durable rule about `wiki_compile.py`.\n'
        text = card_text(body)
        self.assertEqual(budget.derived_body(text), derive(text, digest(text))['body_text'])
        bigger = card_text(body, extra='aliases:\n' + ''.join(f'  - alias {i}\n' for i in range(300)))
        measured = budget.measure_card(text, self.p.tokenizer)
        self.assertEqual((measured['method'], measured['size']),
                         (budget.METHOD_TOKENIZER, fake_count(body)))
        self.assertEqual(budget.measure_card(bigger, self.p.tokenizer)['size'], measured['size'],
                         'frontmatter (title, aliases, related) never counts')

    def test_a_body_derive_refuses_does_not_fit(self):
        measured = budget.measure_card(card_text('x' * 17000))
        self.assertEqual((measured['fits'], measured['reason'], measured['method']),
                         (False, 'over_budget', budget.METHOD_DERIVE_LIMIT))
        self.assertEqual(budget.measure_card('no frontmatter')['reason'], 'selector_input_invalid')

    def test_byte_fallback_heavy_text_is_counted_not_guessed(self):
        body = '\n' + RARE[0] * 500 + '\n'
        self.assertLessEqual(budget.estimate(body), 700,
                             'a per-character guess would let this body through')
        tokenizer = budget.selector_tokenizer({}, self.p.root)
        measured = budget.measure_card(card_text(body), tokenizer)
        self.assertEqual((measured['method'], measured['fits'], measured['size']),
                         (budget.METHOD_TOKENIZER, False, fake_count(body)))

    def test_any_user_selector_counts_for_a_project_without_one(self):
        self.assertEqual(budget.selector_tokenizer({}, self.p.root), self.p.tokenizer)

    def test_no_selector_tokenizer_means_unmeasured_not_guessed(self):
        bare = Project(self, selector=False)
        self.assertIsNone(budget.selector_tokenizer({}, bare.root))
        measured = budget.measure_card(card_text('\n' + LONG_SUMMARY + '\n'), None)
        self.assertEqual((measured['fits'], measured['size'], measured['method'], measured['reason']),
                         (True, None, budget.METHOD_UNAVAILABLE, 'tokenizer_unavailable'))
        broken = bare.home / 'broken'
        broken.write_text('#!/bin/sh\nexit 3\n')
        broken.chmod(0o700)
        measured = budget.measure_card(card_text('\nx\n'), (str(broken), 'tok.json'))
        self.assertEqual((measured['fits'], measured['method'], measured['reason']),
                         (False, budget.METHOD_FAILED, 'measure_failed'),
                         'a runtime that exists but fails is not "no tokenizer"')

    def test_a_failing_runtime_yields_to_the_next_selector(self):
        home = self.p.home
        broken = install_selector(home, name='0' * 64, broken=True)
        pairs = budget.selector_tokenizers({}, self.p.root)
        self.assertEqual(pairs[0], broken, 'sorted: the broken selector comes first')
        measured = budget.measure_card(card_text('\nshort body\n'), pairs)
        self.assertEqual((measured['method'], measured['fits']), (budget.METHOD_TOKENIZER, True))

    def test_budget_arithmetic_and_prompt_agree(self):
        import lib
        self.assertEqual(budget.PAIR_OVERHEAD + budget.BODY_BUDGET + budget.QUERY_ROOM,
                         budget.MODEL_MAX_LEN)
        self.assertEqual((budget.MODEL_MAX_LEN, budget.BODY_BUDGET), (1024, 700))
        self.assertEqual(lib.DEFAULT_CARD_BUDGET, budget.prompt_budget())
        prompt = lib.build_prompt({'source': {'path': 'docs/x.md', 'content': 'B'},
                                   'wiki': {'schema': 'S'}, 'cardBudget': budget.prompt_budget()})
        self.assertIn('A card body holds at most 700 tokens', prompt)
        self.assertIn(f'the summary gets about {budget.summary_budget()} tokens', prompt)
        self.assertNotIn('SPLIT REQUEST', prompt)
        split = lib.build_prompt({'source': {'path': 'docs/x.md', 'content': 'B'}, 'wiki': {},
                                  'split': {'cards': [{'title': 'Big', 'canonicalKey': 'big',
                                                       'size': 900}]}})
        self.assertIn('SPLIT REQUEST', split)
        self.assertIn('- Big (canonicalKey: big): body 900 tokens', split)
        for name in ('default', 'novel'):
            rules = (REPO / 'core' / 'wiki_card_rules' / f'{name}.md').read_text()
            self.assertIn('fits 700 tokens', rules)
            self.assertIn('`related`', rules)
            self.assertLess(len(rules), 4000, 'lib.build_prompt keeps the first 4000 chars')


class CompileChecks(unittest.TestCase):
    def setUp(self):
        self.p = Project(self)

    def test_over_budget_candidate_is_refused_not_written(self):
        out = self.p.compile(candidate(summary=LONG_SUMMARY))
        self.assertEqual(out['action'], 'split-required', out)
        self.assertEqual((out['reason'], out['budget'], out['method']),
                         ('over_budget', 700, 'tokenizer'))
        self.assertGreater(out['size'], 700)
        self.assertEqual(list(self.p.wiki.rglob('*.md')), [])
        rows = self.p.ledger('candidates.jsonl')
        self.assertEqual(rows[-1]['action'], 'split-required')
        self.assertEqual(rows[-1]['cardBudget']['reason'], 'over_budget')
        self.assertEqual(self.p.ledger('manifest.jsonl'), [])

    def test_within_budget_card_is_written_unchanged(self):
        import wiki_compile as wc
        sent = candidate()
        out = self.p.compile(sent)
        self.assertEqual(out['action'], 'created', out)
        card = self.p.root / out['targetPath']
        h = wc.source_hash({**sent, 'summary': SHORT_SUMMARY})
        self.assertEqual(card.read_text(), wc.markdown_page(sent, SHORT_SUMMARY, 'generated', [], h))
        self.assertNotIn('related:', card.read_text())
        self.assertTrue(budget.measure_card(card.read_text())['fits'])

    def test_split_siblings_name_each_other_in_related(self):
        out = self.p.compile(candidate(title='Part One', related=['part-two', 'part-one', ' ']))
        text = (self.p.root / out['targetPath']).read_text()
        self.assertIn('related:\n  - "part-two"\n', text)
        self.assertNotIn('- "part-one"', text, "a card never relates to its own key")
        import wiki_compile as wc
        meta, ok = wc.parse_frontmatter(text)
        self.assertTrue(ok, 'a related block never fail-closes verify or dedup parsing')
        self.assertEqual(meta.get('related'), ['part-two'])

    def test_update_over_budget_leaves_the_existing_card_untouched(self):
        out = self.p.compile(candidate())
        card = self.p.root / out['targetPath']
        before = card.read_text()
        refused = self.p.compile(candidate(summary=LONG_SUMMARY))
        self.assertEqual((refused['action'], refused['reason']), ('split-required', 'over_budget'))
        self.assertEqual(card.read_text(), before)

    def test_long_hand_written_section_refuses_a_rewrite_with_its_own_reason(self):
        out = self.p.compile(candidate())
        card = self.p.root / out['targetPath']
        long_card = card.read_text() + '\n## Notes\n' + LONG_SUMMARY + '\n'
        card.write_text(long_card)
        refused = self.p.compile(candidate(summary=SHORT_SUMMARY + ' Updated.'))
        self.assertEqual((refused['action'], refused['reason']),
                         ('split-required', 'manual_section_over_budget'), refused)
        self.assertEqual(card.read_text(), long_card, 'a long card is not split retroactively')

    def test_overflow_the_summary_can_fix_stays_splittable(self):
        out = self.p.compile(candidate())
        card = self.p.root / out['targetPath']
        notes = '\n## Notes\n' + ' '.join(f'note {i} keeps context.' for i in range(70)) + '\n'
        card.write_text(card.read_text() + notes)
        summary = ' '.join(f'Fact {i} about the source.' for i in range(40))
        refused = self.p.compile(candidate(summary=summary))
        self.assertEqual((refused['action'], refused['reason']), ('split-required', 'over_budget'),
                         'the hand-written section alone fits, so a smaller summary can')

    def test_without_a_selector_tokenizer_the_card_is_written_and_logged(self):
        import wiki_compile as wc
        bare = Project(self, selector=False)
        out = bare.compile(candidate(summary=LONG_SUMMARY))
        self.assertEqual(out['action'], 'created', out)
        state = bare.state_root()
        bound = ({'journalPath': str(state / 'install-journal.sqlite')}, {})
        with patch('sys.stdout'):
            refused = wc.budget_refused(bare.root, {}, bound, bare.wiki,
                                        bare.wiki / 'concepts' / 'x.md',
                                        {'title': 'X', 'targetPath': 'x', 'canonicalKey': 'x'},
                                        bare.base / 'c.jsonl', card_text(LONG_SUMMARY))
        self.assertFalse(refused)
        self.assertEqual([(e['event'], e['outcome'], e['method']) for e in bare.events(state)],
                         [('wiki.card', 'unmeasured', 'unavailable')])

    def test_refusal_logs_a_wiki_card_event_for_a_selected_install(self):
        import wiki_compile as wc
        state = self.p.state_root()
        bound = ({'journalPath': str(state / 'install-journal.sqlite'), 'installId': 'i1'}, {})
        record = {'title': 'Big Card', 'targetPath': 'x', 'canonicalKey': 'big'}
        ledger = self.p.base / 'candidates.jsonl'
        target = self.p.wiki / 'concepts' / 'big.md'
        with patch('sys.stdout'):
            refused = wc.budget_refused(self.p.root, {}, bound, self.p.wiki, target, record,
                                        ledger, card_text(LONG_SUMMARY))
        self.assertTrue(refused)
        events = self.p.events(state)
        self.assertEqual([(e['event'], e['outcome'], e['reason']) for e in events],
                         [('wiki.card', 'over_budget', 'over_budget')])
        self.assertEqual((events[0]['path'], events[0]['budget'], events[0]['method']),
                         ('concepts/big.md', 700, 'tokenizer'))
        self.assertGreater(events[0]['size'], 700)


class WorkerChecks(unittest.TestCase):
    def setUp(self):
        self.p = Project(self)
        settings = self.p.root / '.auto-context' / 'settings.json'
        cfg = json.loads(settings.read_text())
        cfg['compile']['mode'] = 'guarded'
        settings.write_text(json.dumps(cfg))
        import config
        import wiki_compile_worker as w
        self.w = w
        self.config = config.find_project_config(str(self.p.root))['config']
        self.job = {'ts': '2026-10-30T00:00:00Z', 'trigger': 'post_tool_source',
                    'engine': 'claude', 'cwd': str(self.p.root),
                    'source': {'kind': 'file', 'path': 'docs/source.md',
                               'collection': 'proj-docs'}}
        self.payloads = []
        for name, value in (('resolve_extractor_argv', lambda *_: ['fake-extractor']),
                            ('gather_similar_pages', lambda *_: None)):
            mocked = patch.object(w, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_job(self, answers, budget_left=None, job=None):
        start = len(self.payloads)

        def run_extractor(_argv, payload, _timeout, _root):
            self.payloads.append(payload)
            return {'candidates': answers[len(self.payloads) - 1 - start]}, None, 0
        token = self.w._EXTRACTOR_BUDGET.set(
            None if budget_left is None else {'remaining': budget_left})
        try:
            with patch.object(self.w, 'run_extractor', run_extractor):
                return self.w.process_job(self.p.root, self.config, self.config['compile'],
                                          dict(self.job) if job is None else job)
        finally:
            self.w._EXTRACTOR_BUDGET.reset(token)

    def cards(self):
        return sorted(p.relative_to(self.p.wiki).as_posix() for p in self.p.wiki.rglob('*.md'))

    def test_over_budget_candidate_is_split_by_one_re_prompt(self):
        parts = [candidate(title='Budget Part One', related=['budget-part-two']),
                 candidate(title='Budget Part Two', related=['budget-part-one'])]
        with patch.object(budget, 'emit', wraps=budget.emit) as emit:
            result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)], parts])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.payloads[0]['cardBudget'], budget.prompt_budget())
        self.assertNotIn('split', self.payloads[0])
        split = self.payloads[1]['split']['cards']
        self.assertEqual([(c['title'], c['reason']) for c in split],
                         [('Budget Whole', 'over_budget')])
        self.assertGreater(split[0]['size'], 700)
        self.assertEqual(self.cards(), ['decisions/budget-part-one.md',
                                        'decisions/budget-part-two.md'])
        self.assertIn('related:\n  - "budget-part-two"',
                      (self.p.wiki / 'decisions' / 'budget-part-one.md').read_text())
        calls = [c for c in emit.call_args_list if c.args[1] == 'split']
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0].kwargs['requested'], calls[0].kwargs['parts'],
                          calls[0].kwargs['written']), (1, 2, 2))

    def test_still_over_budget_after_the_split_writes_nothing(self):
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                               [candidate(title='Budget Still Big', summary=LONG_SUMMARY)]])
        self.assertEqual(result[:2], (True, False), 'one re-prompt, then the job is settled')
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.cards(), [])
        failures = [r for r in self.p.ledger('candidates.jsonl') if r.get('reason') == 'card_over_budget']
        self.assertEqual(failures[-1]['splitTitles'], ['Budget Still Big'])

    def stash(self):
        path = self.p.root / '.auto-context' / 'compile' / 'budget-stash.json'
        return json.loads(path.read_text()) if path.exists() else []

    def test_an_exhausted_run_budget_stashes_and_the_next_run_resumes_the_split(self):
        big = candidate(title='Budget Whole', summary=LONG_SUMMARY)
        result = self.run_job([[big]], budget_left=1)
        self.assertEqual(result[:2], (False, True), 'not settled: the split has not run yet')
        self.assertEqual(len(self.payloads), 1, 'no paid re-prompt past the run budget')
        self.assertEqual((self.cards(), self.p.ledger('budget-refused.jsonl')), ([], []))
        self.assertEqual([(r['reason'], [c['title'] for c in r['candidates']]) for r in self.stash()],
                         [('extractor_budget_exhausted', ['Budget Whole'])])
        parts = [candidate(title='Budget Part One'), candidate(title='Budget Part Two')]
        result = self.run_job([parts])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(len(self.payloads), 2, 'the resume pays only for the split')
        self.assertIn('split', self.payloads[1])
        self.assertEqual(self.cards(), ['decisions/budget-part-one.md',
                                        'decisions/budget-part-two.md'])
        self.assertEqual(self.stash(), [])

    def test_resumes_are_bounded_then_terminal(self):
        big = candidate(title='Budget Whole', summary=LONG_SUMMARY)
        self.run_job([[big]], budget_left=1)
        for attempt in (1, 2, 3):
            self.assertEqual(self.run_job([], budget_left=0)[:2], (False, True))
            self.assertEqual(self.stash()[0]['attempts'], attempt)
        self.assertEqual(self.run_job([], budget_left=0)[:2], (True, False))
        self.assertEqual(len(self.payloads), 1, 'only the first extraction was paid')
        self.assertEqual([r['outcome'] for r in self.p.ledger('budget-refused.jsonl')],
                         ['resume_exhausted'])
        self.assertEqual(self.stash(), [])

    def test_a_card_no_runtime_can_measure_waits_without_terminal_state(self):
        for pair in budget.selector_tokenizers({}, self.p.root):
            Path(pair[0]).write_text('#!/bin/sh\nexit 3\n')
        result = self.run_job([[candidate()]])
        self.assertEqual(result[:2], (False, True))
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.p.ledger('budget-refused.jsonl'), [])
        self.assertEqual([r['reason'] for r in self.stash()], ['measure_failed'])
        self.assertTrue(any(r.get('action') == 'measure-failed'
                            for r in self.p.ledger('candidates.jsonl')))

    def test_a_rejected_split_part_makes_the_revision_terminal(self):
        rejected = candidate(title='Budget Part Bad', summary='User: hi\nassistant: hello')
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                               [candidate(title='Budget Part Good'), rejected]])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual([(r['outcome'], r['reason']) for r in self.p.ledger('budget-refused.jsonl')],
                         [('split_incomplete', 'split_card_rejected')])
        self.payloads.clear()
        self.assertEqual(self.run_job([])[:2], (True, False))
        self.assertEqual(self.payloads, [], 'no re-extraction of the refused revision')

    def test_an_unwritable_budget_store_blocks_paid_calls_with_bounded_retries(self):
        (self.p.root / '.auto-context' / 'compile' / 'budget-refused.jsonl').mkdir()
        job = dict(self.job)
        with patch.object(budget, 'emit', wraps=budget.emit) as emit:
            for retry in (1, 2):
                self.assertEqual(self.run_job([], job=job)[:2], (False, True))
                self.assertEqual(job['_budgetStoreRetries'], retry)
            self.assertEqual(self.run_job([], job=job)[:2], (True, False))
        self.assertEqual(self.payloads, [], 'no extractor call while the store is broken')
        self.assertEqual([c.args[1] for c in emit.call_args_list], ['budget_store_unwritable'] * 3)

    def test_a_failed_terminal_write_is_retried_without_extracting(self):
        import wiki_budget_ledger
        job = dict(self.job)
        answers = [[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                   [candidate(title='Budget Still Big', summary=LONG_SUMMARY)]]
        with patch.object(wiki_budget_ledger, 'record_refused', return_value=False):
            self.assertEqual(self.run_job(answers, job=job)[:2], (False, True))
        self.assertEqual(job['_budgetTerminal']['outcome'], 'split_incomplete')
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.run_job([], job=job)[:2], (True, False))
        self.assertEqual(len(self.payloads), 2, 'the retry records without paying again')
        self.assertEqual([r['outcome'] for r in self.p.ledger('budget-refused.jsonl')],
                         ['split_incomplete'])

    def test_the_terminal_ledger_keeps_live_revisions_however_large_it_grows(self):
        import hashlib
        ledger = self.p.root / '.auto-context' / 'compile' / 'budget-refused.jsonl'
        sha = hashlib.sha256((self.p.root / 'docs' / 'source.md').read_bytes()).hexdigest()
        live = {'path': 'docs/source.md', 'collection': 'proj-docs', 'sha256': sha,
                'outcome': 'split_incomplete', 'reason': 'card_over_budget', 'titles': []}
        stale = [{'path': f'docs/gone-{i}.md', 'collection': 'proj-docs', 'sha256': '0' * 64,
                  'outcome': 'split_incomplete', 'reason': 'card_over_budget',
                  'titles': ['x' * 60]} for i in range(3000)]
        ledger.write_text(''.join(json.dumps(r) + '\n' for r in [live] + stale))
        self.assertGreater(ledger.stat().st_size, 256 * 1024)
        (self.p.root / 'docs' / 'other.md').write_text('# Other\n\nAnother source.\n')
        other = {**self.job, 'source': {**self.job['source'], 'path': 'docs/other.md'}}
        self.run_job([[candidate(title='Other Whole', summary=LONG_SUMMARY)],
                      [candidate(title='Other Still Big', summary=LONG_SUMMARY)]], job=other)
        rows = self.p.ledger('budget-refused.jsonl')
        self.assertEqual(sorted(r['path'] for r in rows), ['docs/other.md', 'docs/source.md'],
                         'live rows stay, rows of missing sources go')
        self.payloads.clear()
        self.assertEqual(self.run_job([])[:2], (True, False))
        self.assertEqual(self.payloads, [], 'the live terminal key survived the rewrite')

    def test_split_parts_share_cards_per_source_with_the_first_answer(self):
        settings = self.p.root / '.auto-context' / 'settings.json'
        cfg = json.loads(settings.read_text())
        cfg['compile']['budget'] = {'cardsPerSource': 3}
        settings.write_text(json.dumps(cfg))
        import config
        self.config = config.find_project_config(str(self.p.root))['config']
        first = [candidate(title='Small One'), candidate(title='Small Two'),
                 candidate(title='Budget Whole', summary=LONG_SUMMARY)]
        parts = [candidate(title=f'Budget Part {n}') for n in ('One', 'Two', 'Three')]
        result = self.run_job([first, parts])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(self.cards(), ['decisions/budget-part-one.md', 'decisions/small-one.md',
                                        'decisions/small-two.md'],
                         'the split gets only the one slot the first answer left')
        row = [r for r in self.p.ledger('candidates.jsonl')
               if r.get('reason') == 'cards_per_source_cap' and 'droppedParts' in r][-1]
        self.assertEqual((row['droppedParts'], row['splitTitles']),
                         (2, ['Budget Part Two', 'Budget Part Three']))
        refused = self.p.ledger('budget-refused.jsonl')
        self.assertEqual([(r['path'], r['outcome'], r['reason']) for r in refused],
                         [('docs/source.md', 'split_incomplete', 'cards_per_source_cap')])

    def test_a_refused_revision_is_never_extracted_again(self):
        self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                      [candidate(title='Budget Still Big', summary=LONG_SUMMARY)]])
        self.assertEqual(len(self.payloads), 2)
        self.payloads.clear()
        self.assertEqual(self.run_job([])[:2], (True, False))
        self.assertEqual(self.payloads, [], 'same revision: no paid extraction')
        (self.p.root / 'docs' / 'source.md').write_text('# Source\n\nEdited.\n')
        self.run_job([[candidate()]])
        self.assertEqual(len(self.payloads), 1, 'a source edit reopens the revision')

    def test_within_budget_candidates_need_no_re_prompt(self):
        self.run_job([[candidate()]])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.cards(), ['decisions/source-compile-decision.md'])


class WorkerRunChecks(unittest.TestCase):
    """Real worker runs: pending refresh recovery must not re-bill a refused revision."""

    def test_pending_recovery_makes_zero_extractor_calls_on_the_second_run(self):
        p = Project(self)
        calls = p.base / 'extractor-calls'
        extractor = p.base / 'extract.py'
        extractor.write_text(
            'import json, sys\n'
            f'open({str(calls)!r}, "a").write("x")\n'
            'json.loads(sys.stdin.read())\n'
            f'print(json.dumps({{"candidates": [{json.dumps(candidate(title="Budget Whole", summary=LONG_SUMMARY))}]}}))\n')
        settings = p.root / '.auto-context' / 'settings.json'
        cfg = json.loads(settings.read_text())
        cfg['compile'].update(mode='guarded', extractor={
            'backends': {'claude': [sys.executable, str(extractor)]}, 'timeout': 30})
        settings.write_text(json.dumps(cfg))
        import wiki_freshness
        pending = wiki_freshness.record_pending_refresh(p.root, 'docs/source.md', 'claude')
        self.assertIsNotNone(pending)

        def run():
            proc = subprocess.run([sys.executable, str(REPO / 'core' / 'wiki_compile_worker.py'),
                                   '--cwd', str(p.root), '--flush-all', '--json'],
                                  text=True, capture_output=True, env=p.env, cwd=str(REPO))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return calls.read_text() if calls.exists() else ''

        self.assertEqual(run(), 'xx', 'first run: one extraction plus one split re-prompt')
        self.assertEqual([r['outcome'] for r in p.ledger('budget-refused.jsonl')],
                         ['split_incomplete'])
        self.assertTrue(wiki_freshness.unresolved_pending_refreshes(p.root),
                        'the source stays pending so recall keeps treating it as stale')
        self.assertEqual(run(), 'xx', 'second run: recovery re-enqueues, zero extractor calls')
        self.assertTrue(any(r.get('reason') == 'budget_refused_revision'
                            for r in p.ledger('candidates.jsonl')))
        self.assertEqual(list(p.wiki.rglob('*.md')), [])


class DedupChecks(unittest.TestCase):
    def setUp(self):
        self.p = Project(self)
        (self.p.wiki / 'entities').mkdir()
        self.loser = self.p.wiki / 'entities' / 'loser.md'
        self.keeper = self.p.wiki / 'entities' / 'winner.md'
        self.loser.write_text(card_text('\nloser fact\n', 'Loser Card'))

    def resolve(self, bound=None):
        import wiki_dedup_resolve as R
        import wiki_selected_laya as S
        entry = {'pageA': 'entities/loser.md', 'pageB': 'entities/winner.md', 'score': 0.95}
        with patch.object(S, 'recheck'), patch.object(S, 'request_sync', return_value=True):
            return R.resolve_entry(self.p.root, self.p.wiki,
                                   self.p.root / '.auto-context' / 'compile', entry, 'merge',
                                   'entities/loser.md', laya_bound=bound)

    def test_merge_into_an_over_budget_keeper_keeps_both_cards(self):
        self.keeper.write_text(card_text('\n' + LONG_SUMMARY + '\n', 'Winner Card'))
        state = self.p.state_root()
        bound = ({'journalPath': str(state / 'install-journal.sqlite')}, {})
        out = self.resolve(bound)
        self.assertEqual((out['action'], out['reason'], out['keeper']),
                         ('kept_separate', 'keeper_over_budget', 'entities/winner.md'))
        self.assertTrue(self.loser.exists())
        self.assertTrue(out['recorded'], 'the pair is recorded as skipped')
        events = self.p.events(state)
        self.assertEqual([(e['event'], e['outcome'], e['path'], e['kept']) for e in events],
                         [('wiki.card', 'merge_kept_separate', 'entities/winner.md',
                           'entities/loser.md')])

    def test_merge_into_a_keeper_within_budget_still_deletes(self):
        self.keeper.write_text(card_text('\nwinner fact\nloser fact\n', 'Winner Card'))
        self.assertEqual(self.resolve()['action'], 'deleted')
        self.assertFalse(self.loser.exists())

    def test_a_keeper_without_frontmatter_is_not_a_budget_case(self):
        self.keeper.write_text('winner\n')
        self.assertEqual(self.resolve()['action'], 'deleted')

    def test_cli_measures_cards_and_their_combined_body(self):
        self.keeper.write_text(card_text('\nwinner fact\n', 'Winner Card'))
        proc = subprocess.run([sys.executable, str(REPO / 'core' / 'wiki_card_budget.py'),
                               '--cwd', str(self.p.root), str(self.keeper), str(self.loser)],
                              text=True, capture_output=True, env=self.p.env)
        out = json.loads(proc.stdout)
        self.assertEqual((out['budget'], out['method']), (700, 'tokenizer'))
        self.assertEqual([c['fits'] for c in out['cards']], [True, True])
        self.assertTrue(out['combined']['fits'])
        self.assertGreater(out['combined']['size'], out['cards'][0]['size'])


if __name__ == '__main__':
    unittest.main(verbosity=1)
