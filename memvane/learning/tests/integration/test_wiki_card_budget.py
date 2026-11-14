"""Card body budget for the local selector: measurement and the budget CLI.

Private fixture HOME; no owner files, service, model or paid extractor. A
fake selector runtime stands in for the checkpoint tokenizer: it is
registered the way a real live selector is (owner-only
``live-selection/<state>/live-selector.json`` under the fixture HOME) and
counts 3 tokens per byte-fallback character (``RARE``) and one token per 3
UTF-8 bytes of everything else. The compile gate, split and dedup uses of
the budget are tested by their own domains.
"""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from memvane.learning import wiki_card_budget as budget
from memvane.learning.context_learning.compact_input import derive
from memvane.learning.context_learning.contracts import digest
from memvane.learning.tests.support import REPO
from tests.support.case import FixtureCase
from tests.support.project import make_project

LONG_SUMMARY = ' '.join(f'Rule {i}: `compile.budget.cardsPerSource` caps cards per source.'
                        for i in range(120))
RARE = '뷁똠쀍햏'
STDERR_MARKER = 'STDERR-MARKER-91f3'
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
    from memvane.learning.context_learning.runtime_setup import learning_state_base
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


class Fixture:
    """A fixture project and, unless ``selector=False``, a registered fake selector."""

    def __init__(self, case, selector=True, name='budget'):
        self.home = case.home
        self.project = make_project(case.home, name=name, dirname=name)
        self.root = self.project.root
        self.tokenizer = install_selector(self.home) if selector else None


class MeasureChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        self.p = Fixture(self)

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
        bare = Fixture(self, selector=False, name='bare')
        other_home = self.tmp / 'other-home'
        other_home.mkdir(mode=0o700)
        with patch.dict(os.environ, {'HOME': str(other_home)}):
            self.assertIsNone(budget.selector_tokenizer({}, bare.root))
        measured = budget.measure_card(card_text('\n' + LONG_SUMMARY + '\n'), None)
        self.assertEqual((measured['fits'], measured['size'], measured['method'], measured['reason']),
                         (True, None, budget.METHOD_UNAVAILABLE, 'tokenizer_unavailable'))
        broken = other_home / 'broken'
        broken.write_text('#!/bin/sh\nexit 3\n')
        broken.chmod(0o700)
        measured = budget.measure_card(card_text('\nx\n'), (str(broken), 'tok.json'))
        self.assertEqual((measured['fits'], measured['method'], measured['reason']),
                         (False, budget.METHOD_FAILED, 'measure_failed'),
                         'a runtime that exists but fails is not "no tokenizer"')

    def failing_runtimes(self):
        """One runtime per count failure, keyed by the reason code it must yield."""
        def script(name, body):
            path = self.p.home / f'fail-{name}'
            path.write_text(body)
            path.chmod(0o700)
            return (str(path), self.p.tokenizer[1])
        not_exec = self.p.home / 'fail-spawn'
        not_exec.write_text('#!/bin/sh\nexit 0\n')
        not_exec.chmod(0o600)
        return {
            'runtime_spawn_failed': (str(not_exec), self.p.tokenizer[1]),
            'runtime_timeout': script('timeout', '#!/bin/sh\nexec sleep 5\n'),
            'runtime_exit': script('exit', f'#!/bin/sh\necho "{STDERR_MARKER}" >&2\nexit 3\n'),
            'output_undecodable': script('decode', "#!/bin/sh\nprintf '\\377\\376'\n"),
            'output_not_json': script('json', f'#!/bin/sh\necho "{STDERR_MARKER}"\n'),
            'output_shape_invalid': script('shape', '#!/bin/sh\necho \'["x"]\'\n'),
            'count_mismatch': script('count', '#!/bin/sh\necho \'[1, 2]\'\n'),
        }

    def test_each_count_failure_has_its_own_reason_code(self):
        runtimes = self.failing_runtimes()
        text = card_text('\nshort body\n')
        seen = {}
        with patch.object(budget, 'EXACT_TIMEOUT_SECONDS', 0.5):
            for expected, pair in runtimes.items():
                measured = budget.measure_card(text, pair)
                self.assertEqual((measured['method'], measured['reason']),
                                 (budget.METHOD_FAILED, 'measure_failed'), expected)
                self.assertEqual(measured['failure'], expected)
                self.assertEqual([a['reason'] for a in measured['attempts']], [expected])
                seen[expected] = measured['attempts'][0]
        self.assertEqual(seen['runtime_exit']['exit'], 3)
        with patch.object(budget.subprocess, 'run', side_effect=subprocess.SubprocessError('x')):
            self.assertEqual(budget.measure_card(text, self.p.tokenizer)['failure'],
                             'runtime_error')
        working = budget.measure_card(text, self.p.tokenizer)
        self.assertTrue(working['fits'], 'a working runtime still measures')
        self.assertNotIn('failure', working, 'a measured card carries no failure')
        self.assertEqual(len(set(seen) | {'runtime_error'}), 8)

    def test_unencodable_input_is_its_own_code(self):
        counts, method, diagnostic = budget.measure_bodies_detailed(
            ['lone \ud800 surrogate'], self.p.tokenizer)
        self.assertEqual((counts, method, diagnostic['attemptsTotal'], diagnostic['failure']),
                         (None, budget.METHOD_FAILED, 1, 'input_unencodable'))
        self.assertEqual([a['reason'] for a in diagnostic['attempts']], ['input_unencodable'])

    def test_reason_codes_survive_the_event_log(self):
        from memvane.diagnostics import api as event_log
        self.assertEqual(len(set(budget.MEASURE_REASONS)), len(budget.MEASURE_REASONS))
        for code in budget.MEASURE_REASONS:
            self.assertEqual(event_log.code(code), code)

    def test_attempts_stay_bounded_and_counted(self):
        runtimes = self.failing_runtimes()
        broken = runtimes['runtime_exit']
        pairs = [(broken[0], f'{broken[1]}.{i}') for i in range(budget.MAX_ATTEMPTS_LOGGED + 5)]
        pairs.append(runtimes['count_mismatch'])
        measured = budget.measure_card(card_text('\nx\n'), pairs)
        self.assertEqual(measured['failure'], 'count_mismatch', 'the last failure, even unlogged')
        self.assertEqual(len(measured['attempts']), budget.MAX_ATTEMPTS_LOGGED)
        self.assertEqual(measured['attemptsTotal'], budget.MAX_ATTEMPTS_LOGGED + 6)
        self.assertEqual([a['attempt'] for a in measured['attempts']],
                         list(range(1, budget.MAX_ATTEMPTS_LOGGED + 1)))

    def test_a_failing_runtime_yields_to_the_next_selector(self):
        home = self.p.home
        broken = install_selector(home, name='0' * 64, broken=True)
        pairs = budget.selector_tokenizers({}, self.p.root)
        self.assertEqual(pairs[0], broken, 'sorted: the broken selector comes first')
        measured = budget.measure_card(card_text('\nshort body\n'), pairs)
        self.assertEqual((measured['method'], measured['fits']), (budget.METHOD_TOKENIZER, True))

    def test_budget_arithmetic_and_rules_agree(self):
        from memvane.learning import api as learning
        self.assertEqual(budget.PAIR_OVERHEAD + budget.BODY_BUDGET + budget.QUERY_ROOM,
                         budget.MODEL_MAX_LEN)
        self.assertEqual((budget.MODEL_MAX_LEN, budget.BODY_BUDGET), (1024, 700))
        self.assertEqual(learning.card_prompt_budget(), budget.prompt_budget())
        for name in ('default', 'novel'):
            rules = (REPO / 'memvane/wiki/card/rules' / f'{name}.md').read_text()
            self.assertIn('fits 700 tokens', rules)
            self.assertIn('`related`', rules)
            self.assertLess(len(rules), 4000, 'the extractor prompt keeps the first 4000 chars')


def card_text(body, title='Card', extra=''):
    return f'---\ntitle: {title}\nstatus: generated\ncreatedBy: memvane\n{extra}---\n{body}'


class BudgetCli(FixtureCase):
    """``entry/run.py wiki-card-budget``: the one process boundary of the budget."""

    def test_cli_measures_cards_and_their_combined_body(self):
        p = Fixture(self)
        cards = p.project.wiki / 'entities'
        cards.mkdir(parents=True)
        keeper, loser = cards / 'winner.md', cards / 'loser.md'
        keeper.write_text(card_text('\nwinner fact\n', 'Winner Card'))
        loser.write_text(card_text('\nloser fact\n', 'Loser Card'))
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        env.pop('QMD_SANDBOX', None)
        proc = subprocess.run([sys.executable, '-B', str(REPO / 'entry' / 'run.py'), 'wiki-card-budget',
                               '--cwd', str(p.root), str(keeper), str(loser)],
                              text=True, capture_output=True, env=env, cwd=str(REPO), timeout=120)
        out = json.loads(proc.stdout)
        self.assertEqual((out['budget'], out['method']), (700, 'tokenizer'))
        self.assertEqual([c['fits'] for c in out['cards']], [True, True])
        self.assertTrue(out['combined']['fits'])
        self.assertGreater(out['combined']['size'], out['cards'][0]['size'])


if __name__ == '__main__':
    import unittest
    unittest.main()
