// U12e (a) minimal reproduction, not a fix: a card whose first-pass part is
// over 900 model tokens is re-split by the provider (chunkDocumentByTokens),
// so its chunk positions differ from the tokenizer-free first pass the view
// recipe can reproduce (laya_view_vectors.qmd_first_pass) and the schema3
// fill refuses it (chunk_text_unreconstructable). The tokenizer here is
// synthetic: one token per Hangul syllable, one per four other characters.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chunkDocumentByTokens } from '../core/laya_qmd_chunking.mjs';

const tokenizer = {
  tokenize(text) {
    let hangul = 0;
    for (const ch of text) if (ch >= '가' && ch <= '힣') hangul += 1;
    return new Array(hangul + Math.ceil((text.length - hangul) / 4)).fill(0);
  },
};

function firstPass(body) {
  const r = spawnSync('python3', ['-B', '-c',
    'import json,sys;sys.path.insert(0,"core");import laya_view_vectors as v;' +
    'print(json.dumps([p for p,_ in v.qmd_first_pass(sys.stdin.read())]))'],
  { input: body, encoding: 'utf8', timeout: 30000 });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

test('a Korean card over 900 tokens in one first-pass part is re-split by the provider', async () => {
  const korean = '# 카드\n' + '가나다라마바사아자차카타파하 '.repeat(70);   // ~1,000 syllables
  const english = '# Card\n' + 'alpha beta gamma delta epsilon '.repeat(80); // same UTF-16 scale
  const observed = {};
  for (const [name, body] of [['korean', korean], ['english', english]]) {
    const provider = (await chunkDocumentByTokens(body, tokenizer)).map(c => c.pos);
    const view = firstPass(body);
    observed[name] = { utf16: body.length, utf8: Buffer.byteLength(body), provider: provider.length,
                       view: view.length, reproducible: JSON.stringify(provider) === JSON.stringify(view) };
  }
  console.log(JSON.stringify({ case: 'chunk-recon', ...observed }));
  assert.equal(observed.korean.reproducible, false);
  assert.equal(observed.korean.view, 1);
  assert.ok(observed.korean.provider > 1);
  assert.equal(observed.english.reproducible, true);
});
