import test from 'node:test';
import assert from 'node:assert/strict';
import {
  CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS, CHUNK_WINDOW_TOKENS,
  chunkDocument, chunkDocumentByTokens, extractQmdMarkdownTitle,
  findCodeFences, scanBreakPoints,
} from '../core/laya_qmd_chunking.mjs';

const syntheticTokenizer = {
  async tokenize(text) { return Array.from({length: text.length}, (_, i) => i); },
};

function assertCoverage(content, chunks) {
  let end = 0;
  for (const chunk of chunks) {
    assert.equal(content.slice(chunk.pos, chunk.pos + chunk.text.length), chunk.text);
    assert.ok(chunk.pos <= end, 'no source gap');
    assert.ok(chunk.pos + chunk.text.length > end, 'each chunk progresses');
    end = chunk.pos + chunk.text.length;
  }
  assert.equal(end, content.length);
}

test('QMD regex recipe constants and break priority', () => {
  assert.deepEqual([CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS, CHUNK_WINDOW_TOKENS],
                   [900, 135, 200]);
  const body = 'before\n# Heading\n\n## Subhead\n- entry';
  const points = scanBreakPoints(body);
  assert.equal(points.find(p => p.pos === body.indexOf('\n# Heading')).type, 'h1');
  assert.equal(points.find(p => p.pos === body.indexOf('\n\n')).type, 'blank');
  assert.equal(points.find(p => p.pos === body.indexOf('\n## Subhead')).type, 'h2');
});

test('QMD Markdown title extraction prefers headings and preserves Notes rule', () => {
  assert.equal(extractQmdMarkdownTitle('# Chapter\nbody', 'raw/a.md'), 'Chapter');
  assert.equal(extractQmdMarkdownTitle('# Notes\n## Actual\nbody', 'raw/a.md'), 'Actual');
  assert.equal(extractQmdMarkdownTitle('plain body', 'raw/a.md'), 'a');
  assert.notEqual(extractQmdMarkdownTitle('identical body', 'raw/alpha.md'),
                  extractQmdMarkdownTitle('identical body', 'raw/beta.md'));
});

test('heading cutoff and code fence boundaries match the QMD regex policy', () => {
  const heading = `${'a'.repeat(50)}\n# H\n${'b'.repeat(100)}`;
  const chunks = chunkDocument(heading, 60, 0, 30);
  assert.equal(chunks[0].text, 'a'.repeat(50));
  assertCoverage(heading, chunks);
  const fenced = `${'p'.repeat(40)}\n\`\`\`\n${'x'.repeat(10)}\n# inside\n${'y'.repeat(10)}\n\`\`\`\n${'z'.repeat(40)}`;
  const regions = findCodeFences(fenced);
  assert.equal(regions.length, 1);
  assert.equal(regions[0].start, 40);
  assert.ok(regions[0].end > fenced.indexOf('\n# inside'));
  assert.equal(chunkDocument(fenced, 70, 0, 70)[0].text, 'p'.repeat(40));
});

test('tokenizer drives recursive splitting while preserving source positions', async () => {
  const body = Array.from({length: 100}, (_, i) =>
    `line ${i} ${'lorem ipsum '.repeat(4)}`).join('\n');
  const chunks = await chunkDocumentByTokens(body, syntheticTokenizer);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every(chunk => chunk.tokens <= 900));
  assert.ok(chunks.some((chunk, i) => i && chunk.pos <
    chunks[i - 1].pos + chunks[i - 1].text.length));
  assertCoverage(body, chunks);
});

test('surrogate boundaries remain whole during character and token splits', async () => {
  const body = '😀'.repeat(1200);
  for (const chunks of [chunkDocument(body, 3, 1, 0),
                        await chunkDocumentByTokens(body, syntheticTokenizer)]) {
    assertCoverage(body, chunks);
    for (const chunk of chunks) {
      assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(chunk.text));
    }
  }
});

test('unsupported AST strategy and malformed tokenizer fail closed', async () => {
  await assert.rejects(chunkDocumentByTokens('a', syntheticTokenizer, 'auto'),
                       {code: 'unsupported_chunk_strategy'});
  await assert.rejects(chunkDocumentByTokens('a', {tokenize: async () => ({})}),
                       {code: 'invalid_tokenize_result'});
  await assert.rejects(chunkDocumentByTokens(null, syntheticTokenizer),
                       {code: 'invalid_content'});
});
