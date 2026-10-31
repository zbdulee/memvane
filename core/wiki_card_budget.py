#!/usr/bin/env python3
"""Wiki card body budget for the local selection model.

The live selector scores a (user request, card) pair with the legacy input
view of its checkpoint: ``max_len`` 1024 tokens. It reads a card through
``context_learning.compact_input.derive``, the whole file after the
frontmatter (banner, auto-block markers, summary and any hand-written
section), and ``laya_adapter._pair`` refuses a pair longer than ``max_len``
(``model_token_budget_exceeded``). This module measures a card through the
same ``derive`` call, so a card the compiler writes fits the selector.

Budget arithmetic (mmBERT tokenizer of the live selector, measured):

- ``PAIR_OVERHEAD`` 38: ``[CLS]`` + question head + ``[SEP]`` + two option
  spans + ``[SEP]`` + ``User request:\\n`` + ``\\nWiki card body:\\n`` +
  final ``[SEP]``. Tokenizing request and body jointly inside the state string
  is one token shorter than counting them apart, so the separate count here is
  an upper bound.
- ``BODY_BUDGET`` 700: the derived body. The rest, ``QUERY_ROOM`` 286 tokens,
  is left for the user request. Card titles live in the frontmatter and never
  enter the pair.

Measurement. When the project's selector is prepared, the selector runtime's
own ``tokenizers`` library counts the body with the selector checkpoint's
``tokenizer/tokenizer.json`` (``method: tokenizer``). Otherwise the
tokenizer-free ``estimate`` counts it (``method: estimate``): per character
class, ASCII digit 1, ASCII letter run ceil(len / 4), newline 1, space or tab
0.5, other ASCII punctuation 1, Hangul syllable 1, other BMP character 2,
character outside the BMP 4. On 1765 real wiki card bodies the real token
count is at most 0.921 of the estimate (median 0.76), and no card estimated
within 700 holds more than 578 real tokens. Rare Hangul syllables and CJK
characters that the tokenizer splits into bytes can exceed their weight; the
query room absorbs that.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent.resolve()))

MODEL_MAX_LEN = 1024
PAIR_OVERHEAD = 38
BODY_BUDGET = 700
QUERY_ROOM = MODEL_MAX_LEN - PAIR_OVERHEAD - BODY_BUDGET

METHOD_TOKENIZER = 'tokenizer'
METHOD_ESTIMATE = 'estimate'
EXACT_TIMEOUT_SECONDS = 20

_CLASS_RE = re.compile(
    r'[0-9]|[A-Za-z]+|\n|[ \t]+|[\x00-\x7f]|[가-힣]|[\u0080-￿]|[\U00010000-\U0010ffff]')

_EXACT_SCRIPT = (
    'import json, sys\n'
    'from tokenizers import Tokenizer\n'
    'tok = Tokenizer.from_file(sys.argv[1])\n'
    'texts = json.loads(sys.stdin.read())\n'
    'print(json.dumps([len(tok.encode(t, add_special_tokens=False).ids) for t in texts]))\n'
)


def estimate(text: str) -> int:
    """Tokenizer-free upper estimate of the selector token count of ``text``."""
    total = 0.0
    for match in _CLASS_RE.finditer(text):
        group = match.group(0)
        char = group[0]
        if '0' <= char <= '9':
            total += 1
        elif char.isascii() and char.isalpha():
            total += math.ceil(len(group) / 4)
        elif char == '\n':
            total += 1
        elif char in ' \t':
            total += 0.5 * len(group)
        elif char.isascii():
            total += 1
        elif '가' <= char <= '힣':
            total += 1
        elif ord(char) > 0xFFFF:
            total += 4
        else:
            total += 2
    return math.ceil(total)


def derived_body(card_text: str) -> str | None:
    """The body the selector reads, from the selector's own ``derive``.

    None when ``derive`` refuses the card (no frontmatter, empty or oversized
    body); such a card cannot be scored at all.
    """
    from context_learning.compact_input import derive
    from context_learning.contracts import digest
    try:
        return derive(card_text, digest(card_text))['body_text']
    except ValueError:
        return None


def _selection_state(config: dict, project_root: Path):
    """The live selector state directory the recall path would consult, or None."""
    from context_learning.seam import _private_state
    shared = config.get('sharedLearning', {}) if isinstance(config, dict) else {}
    if isinstance(shared, dict) and shared.get('enabled') is True:
        if shared.get('profile') == 'qmd-local-learning-profile-v1':
            from context_learning.learning_profile import selection_root
            return selection_root(config, project_root)
        if shared.get('trialSelection') is True:
            from context_learning.shared import _safe_root
            return _safe_root(config, project_root)
    return _private_state(config, Path(project_root), create=False)


def selector_tokenizer(config: dict, project_root) -> tuple[str, str] | None:
    """(runtime python, tokenizer.json) of the prepared live selector, or None.

    Read only. The live selector policy names the runtime interpreter and the
    checkpoint directory (``trainerArgv`` = python, adapter, model dir).
    """
    try:
        from context_learning.dataset import read_private_json
        state = _selection_state(config, Path(project_root).resolve())
        if state is None:
            return None
        policy = read_private_json(Path(state) / 'live-selector.json')
        argv = policy.get('trainerArgv') if isinstance(policy, dict) else None
        if (policy.get('schema') != 'qmd-live-selector-v1' or not isinstance(argv, list)
                or len(argv) != 3 or not all(isinstance(item, str) and Path(item).is_absolute()
                                             for item in argv)):
            return None
        python = Path(argv[0])
        tokenizer = Path(argv[2]) / 'tokenizer' / 'tokenizer.json'
        if (not os.access(python, os.X_OK) or tokenizer.is_symlink()
                or not tokenizer.is_file()):
            return None
        return str(python), str(tokenizer)
    except Exception:  # noqa: BLE001 - the estimate is the fallback
        return None


def count_exact(texts: list[str], tokenizer: tuple[str, str] | None) -> list[int] | None:
    """Selector token counts of ``texts`` through the selector runtime, or None."""
    if tokenizer is None or not texts:
        return None
    python, tokenizer_json = tokenizer
    try:
        proc = subprocess.run(
            [python, '-I', '-c', _EXACT_SCRIPT, tokenizer_json],
            input=json.dumps(texts, ensure_ascii=False), text=True, capture_output=True,
            timeout=EXACT_TIMEOUT_SECONDS, shell=False,
            env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'})
        counts = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if (not isinstance(counts, list) or len(counts) != len(texts)
            or not all(type(item) is int and item >= 0 for item in counts)):
        return None
    return counts


def measure_bodies(bodies: list[str], tokenizer: tuple[str, str] | None = None) -> tuple[list[int], str]:
    counts = count_exact(bodies, tokenizer)
    if counts is not None:
        return counts, METHOD_TOKENIZER
    return [estimate(body) for body in bodies], METHOD_ESTIMATE


def measure_card(card_text: str, tokenizer: tuple[str, str] | None = None) -> dict:
    """``{fits, size, budget, method, reason}`` for a complete card file text.

    ``size`` counts the derived body only; ``reason`` is ``over_budget`` (also
    for a body ``derive`` refuses as oversized) or ``selector_input_invalid``
    (no frontmatter or an empty body) when it does not fit.
    """
    body = derived_body(card_text)
    if body is None:
        # ``derive`` refuses an oversized body outright; that is still a card
        # over the budget. Anything else (no frontmatter, empty body) is not a
        # card the selector reads at all.
        import yaml_scalars
        match = yaml_scalars.FRONTMATTER_RE.match(card_text) if isinstance(card_text, str) else None
        rest = card_text[match.end():] if match else ''
        if rest.strip() and '\x00' not in rest:
            return {'fits': False, 'size': estimate(rest), 'budget': BODY_BUDGET,
                    'method': METHOD_ESTIMATE, 'reason': 'over_budget'}
        return {'fits': False, 'size': None, 'budget': BODY_BUDGET,
                'method': METHOD_ESTIMATE, 'reason': 'selector_input_invalid'}
    (size,), method = measure_bodies([body], tokenizer)
    fits = size <= BODY_BUDGET
    return {'fits': fits, 'size': size, 'budget': BODY_BUDGET, 'method': method,
            'reason': None if fits else 'over_budget'}


def summary_budget() -> int:
    """Estimated room for the summary once the compiler's card scaffold is in the body.

    The scaffold is what ``wiki_compile.markdown_page`` puts after the
    frontmatter: banner, auto-block markers (16-hex ``sourceHash``) and the
    ``## Summary`` heading.
    """
    import wiki_markers
    scaffold = '\n'.join(['', wiki_markers.AUTO_DISCLAIMER, '',
                          wiki_markers.AUTO_START_TEMPLATE.format(source_hash='0' * 16),
                          '## Summary', '', wiki_markers.AUTO_END, ''])
    return max(0, BODY_BUDGET - estimate(scaffold))


def prompt_budget() -> dict:
    """The budget the extractor prompt states (``payload.cardBudget``)."""
    return {'bodyTokens': BODY_BUDGET, 'summaryTokens': summary_budget(),
            'modelMaxTokens': MODEL_MAX_LEN}


def emit(root, outcome: str, *, bound, path: str = '', title: str = '',
         reason: str | None = None, **fields) -> bool:
    """One ``wiki.card`` event in the install log. Never raises.

    Field names avoid the word "token": the event log drops such keys.
    """
    try:
        import event_log
        state_root = event_log.wiki_state_root(root, bound)
        if state_root is None:
            return False
        install = event_log.install_of(bound[0]) if bound else None
        return event_log.emit('wiki.card', outcome=outcome, reason=reason,
                              state_root=state_root, install=install,
                              path=str(path)[:256], title=' '.join(str(title).split())[:200],
                              **fields)
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Measure wiki cards against the local selection body budget.')
    parser.add_argument('--cwd', default=os.getcwd())
    parser.add_argument('cards', nargs='+',
                        help='card files; with two or more, "combined" measures the first '
                             "card's body followed by every other card's body")
    args = parser.parse_args(argv)
    tokenizer = None
    try:
        import config as qmd_config
        found = qmd_config.find_project_config(args.cwd)
        tokenizer = selector_tokenizer(found['config'], Path(found['projectRoot']))
    except Exception:  # noqa: BLE001
        tokenizer = None
    texts = [Path(card).read_text(encoding='utf-8') for card in args.cards]
    bodies = [derived_body(text) for text in texts]
    measurable = [body for body in bodies if body is not None]
    combined_body = '\n'.join(measurable) if len(bodies) > 1 and None not in bodies else None
    counts, method = measure_bodies(measurable + ([combined_body] if combined_body else []),
                                    tokenizer)
    cards = []
    position = 0
    for card, body in zip(args.cards, bodies):
        if body is None:
            cards.append({'path': card, 'size': None, 'fits': False,
                          'reason': 'selector_input_invalid'})
            continue
        size = counts[position]
        position += 1
        cards.append({'path': card, 'size': size, 'fits': size <= BODY_BUDGET})
    out = {'budget': BODY_BUDGET, 'method': method, 'cards': cards}
    if combined_body:
        out['combined'] = {'size': counts[-1], 'fits': counts[-1] <= BODY_BUDGET}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
