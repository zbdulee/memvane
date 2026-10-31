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

    def test_the_final_pass_writes_an_over_budget_card_and_logs_it(self):
        import wiki_compile as wc
        out = self.p.compile(candidate(summary=LONG_SUMMARY, budgetFinal=True,
                                       budgetFinalReason='split_part_over_budget'))
        self.assertEqual(out['action'], 'created', out)
        state = self.p.state_root()
        bound = ({'journalPath': str(state / 'install-journal.sqlite')}, {})
        record = {'title': 'Big Card', 'targetPath': 'x', 'canonicalKey': 'big'}
        refused = wc.budget_refused(self.p.root, {}, bound, self.p.wiki,
                                    self.p.wiki / 'concepts' / 'big.md', record,
                                    self.p.base / 'c.jsonl', card_text(LONG_SUMMARY),
                                    final_reason='split_part_over_budget')
        self.assertFalse(refused)
        events = self.p.events(state)
        self.assertEqual([(e['event'], e['outcome'], e['reason'], e['overflow'], e['method'])
                          for e in events],
                         [('wiki.card', 'over_budget_written', 'split_part_over_budget',
                           'over_budget', 'tokenizer')])
        self.assertGreater(events[0]['size'], 700)

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
            answer = answers[len(self.payloads) - 1 - start]
            if isinstance(answer, str):
                return None, answer, 75  # e.g. resource_deferred
            return {'candidates': answer}, None, 0
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

    def written_budget(self, title):
        rows = [r for r in self.p.ledger('candidates.jsonl')
                if r.get('title') == title and r.get('action') in ('created', 'updated')]
        return rows[-1].get('cardBudget') if rows else None

    def test_still_over_budget_after_the_split_is_written_and_logged(self):
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                               [candidate(title='Budget Still Big', summary=LONG_SUMMARY)]])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(len(self.payloads), 2, 'one extraction and one split, no more')
        self.assertEqual(self.cards(), ['decisions/budget-still-big.md'],
                         'the split part is written; the original is replaced by it')
        written = self.written_budget('Budget Still Big')
        self.assertEqual((written['written'], written['reason'], written['method']),
                         ('split_part_over_budget', 'over_budget', 'tokenizer'))
        self.assertGreater(written['size'], 700)

    def test_a_split_that_cannot_run_writes_the_original(self):
        big = candidate(title='Budget Whole', summary=LONG_SUMMARY)
        with patch.object(budget, 'emit', wraps=budget.emit) as emit:
            result = self.run_job([[big]], budget_left=1)
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(len(self.payloads), 1, 'no paid re-prompt past the run budget')
        self.assertEqual(self.cards(), ['decisions/budget-whole.md'])
        self.assertEqual(self.written_budget('Budget Whole')['written'], 'extractor_budget_exhausted')
        calls = [c for c in emit.call_args_list if c.args[1] == 'split_unavailable']
        self.assertEqual([c.kwargs['reason'] for c in calls], ['extractor_budget_exhausted'])

    def test_a_deferred_split_re_prompt_writes_the_original(self):
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                               'resource_deferred'])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(self.cards(), ['decisions/budget-whole.md'])
        self.assertEqual(self.written_budget('Budget Whole')['written'], 'resource_deferred')

    def test_a_card_no_runtime_can_measure_is_written(self):
        for pair in budget.selector_tokenizers({}, self.p.root):
            Path(pair[0]).write_text('#!/bin/sh\nexit 3\n')
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)]])
        self.assertEqual(result[:2], (True, False))
        self.assertEqual(len(self.payloads), 1, 'nothing to split without a size')
        self.assertEqual(self.cards(), ['decisions/budget-whole.md'])
        self.assertEqual(self.written_budget('Budget Whole')['reason'], 'measure_failed')

    def test_a_model_cannot_claim_the_final_pass(self):
        big = candidate(title='Budget Whole', summary=LONG_SUMMARY, budgetFinal=True,
                        budgetFinalReason='model')
        self.run_job([[big], [candidate(title='Budget Part One')]])
        self.assertEqual(len(self.payloads), 2, 'the writer still held it back for a split')
        self.assertEqual(self.cards(), ['decisions/budget-part-one.md'])

    def test_a_rejected_split_part_is_an_ordinary_writer_outcome(self):
        rejected = candidate(title='Budget Part Bad', summary='User: hi\nassistant: hello')
        result = self.run_job([[candidate(title='Budget Whole', summary=LONG_SUMMARY)],
                               [candidate(title='Budget Part Good'), rejected]])
        self.assertEqual(result[:2], (False, True), 'kept for retry like any rejected card')
        self.assertEqual(self.cards(), ['decisions/budget-part-good.md'])

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
        row = [r for r in self.p.ledger('candidates.jsonl') if 'droppedParts' in r][-1]
        self.assertEqual((row['droppedParts'], row['droppedTitles']),
                         (2, ['Budget Part Two', 'Budget Part Three']))

    def test_within_budget_candidates_need_no_re_prompt(self):
        self.run_job([[candidate()]])
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.cards(), ['decisions/source-compile-decision.md'])


class WorkerRunChecks(unittest.TestCase):
    """Real worker runs: a revision costs at most two extractor calls, then settles."""

    def test_a_second_run_on_the_same_revision_makes_zero_extractor_calls(self):
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
        self.assertIsNotNone(wiki_freshness.record_pending_refresh(p.root, 'docs/source.md', 'claude'))

        def run():
            proc = subprocess.run([sys.executable, str(REPO / 'core' / 'wiki_compile_worker.py'),
                                   '--cwd', str(p.root), '--flush-all', '--json'],
                                  text=True, capture_output=True, env=p.env, cwd=str(REPO))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return calls.read_text() if calls.exists() else ''

        self.assertEqual(run(), 'xx', 'first run: one extraction plus one split re-prompt')
        self.assertEqual(sorted(x.name for x in p.wiki.rglob('*.md')), ['budget-whole.md'],
                         'the split part, still over the budget, is written')
        self.assertEqual(wiki_freshness.unresolved_pending_refreshes(p.root), [],
                         'the revision ends written, so its pending refresh resolves')
        self.assertEqual(run(), 'xx', 'second run: zero extractor calls')


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
