// U12e (a) reproduction and U12g decision (a'): a card whose first-pass part
// is over 900 model tokens is re-split by the provider
// (chunkDocumentByTokens), so its chunk positions differ from the
// tokenizer-free first pass the view recipe reproduces
// (laya_view_vectors.qmd_first_pass). The view chunker then answers that card
// with the recipe's fallback windows (600/90/135 UTF-16 units) and keeps none
// of the provider's vectors; a position-equal card keeps them. The tokenizer
// here is synthetic: one token per Hangul syllable, one per four other
// characters.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chunkDocumentByTokens } from '../memvane/provider/adapters/laya_qmd_chunking.mjs';

const tokenizer = {
  tokenize(text) {
    let hangul = 0;
    for (const ch of text) if (ch >= '가' && ch <= '힣') hangul += 1;
    return new Array(hangul + Math.ceil((text.length - hangul) / 4)).fill(0);
  },
};

function python(script, input) {
  const r = spawnSync('python3', ['-B', '-c', 'import json,sys;sys.path.insert(0,".");' + script],
    { input, encoding: 'utf8', timeout: 60000, env: { ...process.env, QMD_SANDBOX: '1' } });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

const firstPass = (body) => python(
  'import memvane.store.view.view_vectors as v;print(json.dumps([p for p,_ in v.qmd_first_pass(sys.stdin.read())]))', body);

// The worker's view chunker over the actual provider positions.
const viewChunker = (body, positions) => python(
  'import memvane.sync.product_sync_worker as w; import memvane.store.view.view_vectors as v;d=json.loads(sys.stdin.read());' +
  'P=type("P",(),{"embed_document_chunks":lambda s,b,p:[{"seq":i,"pos":x,"vector":[1.0,0.0,0.0]} ' +
  'for i,x in enumerate(d["positions"])]});ctx={"stash":{},"acquire":lambda c:P()};' +
  'chunker,_e=w._schema3_chunker(ctx,lambda:{});' +
  'rows=chunker({"body":d["body"],"title":"카드","path":"blobs/x.md"},None);' +
  'print(json.dumps({"rows":[r["position"] for r in rows],"stash":len(ctx["stash"]),' +
  '"fallback":[p for p,_ in v.qmd_first_pass(d["body"],600,90,135)]}))',
  JSON.stringify({ body, positions }));

test('a re-split Korean card gets the fallback windows; a position-equal card keeps the provider vectors', async () => {
  const korean = '# 카드\n' + '가나다라마바사아자차카타파하 '.repeat(70);   // ~1,000 syllables
  const english = '# Card\n' + 'alpha beta gamma delta epsilon '.repeat(80); // same UTF-16 scale
  const observed = {};
  for (const [name, body] of [['korean', korean], ['english', english]]) {
    const provider = (await chunkDocumentByTokens(body, tokenizer)).map(c => c.pos);
    const view = firstPass(body);
    const chunked = viewChunker(body, provider);
    observed[name] = { utf16: body.length, provider: provider.length, view: view.length,
                       reproducible: JSON.stringify(provider) === JSON.stringify(view),
                       rows: chunked.rows, fallback: chunked.fallback, stash: chunked.stash };
  }
  console.log(JSON.stringify({ case: 'chunk-recon', ...observed }));
  assert.equal(observed.korean.reproducible, false);
  assert.equal(observed.korean.view, 1);
  assert.ok(observed.korean.provider > 1);
  // (a'): fallback windows, no provider vector kept (each window is embedded alone).
  assert.deepEqual(observed.korean.rows, observed.korean.fallback);
  assert.ok(observed.korean.fallback.length > 1);
  assert.equal(observed.korean.stash, 0);
  assert.equal(observed.english.reproducible, true);
  assert.deepEqual(observed.english.rows, firstPass(english));
  assert.equal(observed.english.stash, observed.english.provider);
});
