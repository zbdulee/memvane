"""View recipe first pass against the provider's token chunking (U12e/U12g).

A card whose first-pass part is over 900 model tokens is re-split by the
provider (``chunkDocumentByTokens``), so its chunk positions differ from the
tokenizer-free first pass the view recipe reproduces (``qmd_first_pass``); the
recipe then answers that card with its fallback windows (``FALLBACK_CHARS``,
600/90/135 UTF-16 units). A position-equal card keeps the provider positions.
The tokenizer is synthetic: one token per Hangul syllable, one per four other
characters. The provider chunker is the node module itself (one ``node``
child per body).
"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from memvane.store import api as store_api
from memvane.store.types import FALLBACK_CHARS

REPO = Path(__file__).resolve().parents[4]
CHUNKING = REPO / 'memvane/provider/adapters/laya_qmd_chunking.mjs'
PROVIDER = r'''
import { chunkDocumentByTokens } from %s;
const tokenizer = { tokenize(text) {
  let hangul = 0;
  for (const ch of text) if (ch >= '가' && ch <= '힣') hangul += 1;
  return new Array(hangul + Math.ceil((text.length - hangul) / 4)).fill(0);
} };
let body = '';
for await (const part of process.stdin) body += part;
const chunks = await chunkDocumentByTokens(body, tokenizer);
process.stdout.write(JSON.stringify(chunks.map(c => c.pos)));
'''


def provider_positions(body):
    child = subprocess.run(['node', '--input-type=module', '-e', PROVIDER % json.dumps(CHUNKING.as_uri())],
                           input=body, capture_output=True, encoding='utf8', timeout=60, check=False)
    if child.returncode != 0:
        raise AssertionError(child.stderr)
    return json.loads(child.stdout)


def first_pass(body, *chars):
    return [pos for pos, _text in store_api.qmd_first_pass(body, *chars)]


@unittest.skipUnless(shutil.which('node'), 'the provider chunker is a node module')
class ChunkReconstruction(unittest.TestCase):
    def test_a_resplit_korean_card_gets_fallback_windows_and_an_english_card_keeps_positions(self):
        korean = '# 카드\n' + '가나다라마바사아자차카타파하 ' * 70  # ~1,000 syllables
        english = '# Card\n' + 'alpha beta gamma delta epsilon ' * 80  # same UTF-16 scale
        provider = provider_positions(korean)
        self.assertGreater(len(provider), 1)
        self.assertEqual(len(first_pass(korean)), 1)
        self.assertNotEqual(provider, first_pass(korean))
        fallback = first_pass(korean, *FALLBACK_CHARS)
        self.assertGreater(len(fallback), 1)
        self.assertEqual(FALLBACK_CHARS, (600, 90, 135))
        self.assertEqual(provider_positions(english), first_pass(english))


if __name__ == '__main__':
    unittest.main()
