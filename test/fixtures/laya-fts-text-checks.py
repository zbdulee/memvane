"""_qmd_fts_text must stay byte-identical to the original per-char algorithm.

The per-generation FTS check compares stored FTS rows with this function's
output, so any difference makes every existing generation fail closed.
"""
from __future__ import annotations

from pathlib import Path
import random
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import laya_index_a as a


def reference(text):
    """The original loop, kept verbatim as the oracle (deadline omitted)."""
    out = []
    in_run = False
    for ch in text:
        if a._cjk(ch):
            out.append(' ')
            out.append(ch)
            in_run = True
        else:
            if in_run:
                out.append(' ')
                in_run = False
            out.append(ch)
    if in_run:
        out.append(' ')
    return ''.join(out)


class FtsTextChecks(unittest.TestCase):
    def boundary_chars(self):
        chars = []
        for start, end in a._QMD_CJK_SCRIPT_RANGES:
            for code in (start - 1, start, start + 1, end - 1, end, end + 1):
                if 0 < code < 0x110000 and not 0xd800 <= code <= 0xdfff:
                    chars.append(chr(code))
        return chars

    def test_every_range_boundary_matches(self):
        for ch in self.boundary_chars():
            for text in (ch, 'a' + ch, ch + 'a', ch * 3, 'x' + ch + ch + 'y'):
                self.assertEqual(a._qmd_fts_text(text), reference(text), repr(text))

    def test_random_mixed_text_matches(self):
        rng = random.Random(20261019)
        pool = self.boundary_chars() + list('ab Z9\n\t-_/.') + ['한', '日', 'カ', '😀']
        for _ in range(3000):
            text = ''.join(rng.choice(pool) for _ in range(rng.randint(0, 40)))
            self.assertEqual(a._qmd_fts_text(text), reference(text), repr(text))

    def test_plain_ascii_is_unchanged(self):
        self.assertEqual(a._qmd_fts_text(''), '')
        self.assertEqual(a._qmd_fts_text('plain text 123'), 'plain text 123')

    def test_expired_deadline_still_raises(self):
        with self.assertRaisesRegex(a.IndexContractError, 'fts_deadline_exceeded'):
            a._qmd_fts_text('text', deadline=time.monotonic() - 1)
        self.assertEqual(a._qmd_fts_text('', deadline=time.monotonic() - 1), '')


if __name__ == '__main__':
    unittest.main()
