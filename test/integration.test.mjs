import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

// DF(존재) 좁히기 프로브는 본 recall 질의가 아니다 — term 하나짜리 lex + limit 1
// (본 질의는 vec 을 포함하고, lex 게이트 프로브는 DAEMON_QUERY_LIMIT=8 을 쓴다).
// 시퀀스 단정(requests[0]=wiki, requests[1]=raw backfill)이 프로브에 밀리지 않도록
// 기록에서 제외한다. 응답은 각 서버의 기본 결과 그대로다 — payload 에 테스트 전용
// 표식을 넣으면 라이브 데몬 스키마와 갈린다.
function isDfProbe(payload) {
  const searches = payload?.searches || [];
  return payload?.limit === 1 && searches.length === 1 && searches[0].type === 'lex';
}

function recordQuery(list, payload) {
  if (!isDfProbe(payload)) list.push(payload);
  return payload;
}

function runRecallAsync(input, env) {
  return new Promise((resolve, reject) => {
    const child = spawn('python3', ['core/recall.py'], { env });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', code => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(`recall.py exited ${code}: ${stderr}`));
      }
    });
    child.stdin.end(input);
  });
}

function sourceRevisionLine(project, relativePath, collection = 'proj-docs') {
  const path = join(project, relativePath);
  const bytes = readFileSync(path);
  const stat = statSync(path, { bigint: true });
  const hash = createHash('sha256').update(bytes).digest('hex');
  return `{kind: "file", path: "${relativePath}", collection: "${collection}", sha256: "${hash}", size: ${stat.size}, mtimeNs: ${stat.mtimeNs}}`;
}

function writeTrustedCard(project, name, sourceText, body) {
  const sourcePath = `docs/${name}.md`;
  writeFileSync(join(project, sourcePath), sourceText);
  const revision = sourceRevisionLine(project, sourcePath);
  writeFileSync(join(project, '.auto-context', 'wiki', 'concepts', `${name}.md`),
    `---\ntitle: "${name}"\nstatus: verified\ncreatedBy: qmd-auto-context\nsourceRevisions:\n  - ${revision}\n---\n${body}\n`);
  return sourcePath;
}

test('라이브 데몬 recall 스모크', { skip: !process.env.QMD_LIVE }, () => {
  const out = execFileSync('python3', ['core/recall.py'], {
    input: JSON.stringify({ prompt: '검색 결과 정렬은 어떻게 동작해', cwd: '/Users/example/work/sample' }),
    encoding: 'utf8',
  });
  assert.ok(out.includes('additionalContext'));
});

test('mock HTTP daemon recall integration validates query payload and context output', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        recordQuery(requests, JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          results: [
            { file: 'qmd://mock/docs/oneobil.md', title: 'Oneobil sorting', score: 0.91 },
          ],
        }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-integration-'));
  try {
    mkdirSync(join(tempDir, '.agents'), { recursive: true });
    writeFileSync(join(tempDir, '.agents', 'qmd-recall.json'), JSON.stringify({
      collections: ['mock'],
      queryTimeout: 1.25,
      topN: 1,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: '검색 결과 정렬은 어떻게 동작해?', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].collections, ['mock']);
    assert.deepEqual(requests[0].searches.map(s => s.type), ['lex', 'vec']);
    assert.equal(requests[0].timeout, 1.25);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Oneobil sorting/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall queries wiki collections before raw backfill', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [
          { file: 'qmd://proj-wiki/concepts/config-layout.md', title: 'Config layout', score: 0.93 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-'));
  try {
    mkdirSync(join(tempDir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
    mkdirSync(join(tempDir, 'docs'), { recursive: true });
    writeTrustedCard(tempDir, 'config-layout', '# current config\n', 'Config layout body');
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionPaths: { 'proj-wiki': '.auto-context/wiki', 'proj-docs': 'docs' },
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      wikiPath: '.auto-context/wiki',
      injectSummaryMaxChars: 0,
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout decision 내용을 알려줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /\[wiki:verified\]/);
    assert.match(parsed.hookSpecificOutput.additionalContext, /config-layout/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall backfills raw collections when wiki has no results', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.88 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-raw-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      prefixStyle: 'tag',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /\[raw\]/);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Raw source/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall drops byte-stale and newer-pending verified cards before raw fallback', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [
          { file: 'qmd://proj-wiki/concepts/byte-stale.md', title: 'Byte stale', score: 0.95 },
          { file: 'qmd://proj-wiki/concepts/pending.md', title: 'Pending stale', score: 0.9 },
        ] : [
          { file: 'qmd://proj-docs/docs/current.md', title: 'Current raw source', score: 0.88 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-freshness-'));
  try {
    mkdirSync(join(tempDir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
    mkdirSync(join(tempDir, '.auto-context', 'compile'), { recursive: true });
    mkdirSync(join(tempDir, 'docs'), { recursive: true });
    const byteStale = writeTrustedCard(tempDir, 'byte-stale', '# old bytes\n', 'Stale wiki body');
    const pending = writeTrustedCard(tempDir, 'pending', '# unchanged bytes\n', 'Pending wiki body');
    writeFileSync(join(tempDir, byteStale), '# new bytes\n');
    writeFileSync(join(tempDir, '.auto-context', 'compile', 'source-refresh-pending.jsonl'),
      `${JSON.stringify({
        eventId: 'pending-event-1',
        ts: new Date(Date.now() + 1000).toISOString(),
        sourcePath: pending,
        state: 'pending_refresh',
        engine: 'codex',
      })}\n`);
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionPaths: { 'proj-wiki': '.auto-context/wiki', 'proj-docs': 'docs' },
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      wikiPath: '.auto-context/wiki',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const logPath = join(tempDir, 'recall.log');
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'current freshness fallback 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}`, QMD_RECALL_LOG: logPath },
    );

    assert.equal(requests.length, 2);
    assert.equal(requests[0].limit, 8, 'primary wiki freshness work stays inside daemon candidate bound');
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    const context = parsed.hookSpecificOutput.additionalContext;
    assert.match(context, /Current raw source/);
    assert.doesNotMatch(context, /Stale wiki body|Pending wiki body/);
    const event = readFileSync(logPath, 'utf8').trim().split('\n').map(line => JSON.parse(line))
      .find(row => row.event === 'qmd_recall_selection');
    assert.equal(event.freshness_checked, 2);
    assert.equal(event.dropped_stale, 2);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('recallVerifiedOnly 기본(true): wiki가 미검수 generated뿐이면 live raw backfill로 안전 degrade', async () => {
  // 핵심 신규 동작의 live 경로 검증: wiki 쿼리는 generated 카드를 돌려주지만
  // 디스크에 카드 파일이 없어 미검수로 판정 → drop → filtered_results 빔 →
  // raw 컬렉션 backfill이 트리거돼 원문이 surface된다(미검수 요약 대신 원문).
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [
          { file: 'qmd://proj-wiki/.auto-context/wiki/decisions/config-layout.md', title: 'Config layout', score: 0.93 },
        ] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.7 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-vonly-bf-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      queryTimeout: 1.25,
      // recallVerifiedOnly 미설정 → 기본 true
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout decision 내용을 알려줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 2, 'wiki 쿼리 후 미검수 drop → raw backfill 쿼리까지 2회');
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Raw source/, '미검수 wiki 대신 raw 원문 surface');
    assert.doesNotMatch(parsed.hookSpecificOutput.additionalContext, /config-layout/, '미검수 generated 카드는 제외');
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall does not duplicate raw backfill when raw also has no results', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        recordQuery(requests, JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-empty-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(out, '');
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall backfills raw when the only wiki hit is a contested card (exclude-before-backfill)', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        // wiki는 contested 카드 하나만(minScore 통과, exclude 대상), raw는 정상 문서.
        res.end(JSON.stringify({ results: isWiki ? [
          { file: 'qmd://proj-wiki/concepts/contested.md', title: 'Contested card', score: 0.93 },
        ] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.88 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-contested-'));
  try {
    mkdirSync(join(tempDir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'wiki', 'concepts', 'contested.md'),
      '---\nstatus: contested\ncreatedBy: qmd-auto-context\n---\n# Contested\n');
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionPaths: { 'proj-wiki': '.auto-context/wiki', 'proj-docs': 'docs' },
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      wikiPath: '.auto-context/wiki',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    // 수정 전엔 contested 카드가 filtered_results를 채워 backfill을 막고 이후 exclude로 비워져
    // 빈 출력이 됐다. 이제 exclude가 backfill "전"이라 raw backfill(2번째 query)이 나간다.
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Raw source/);
    assert.doesNotMatch(parsed.hookSpecificOutput.additionalContext, /Contested card/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical drops a wiki hit with unresolvable _collection and backfills raw (fail-closed)', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        // wiki 히트의 file이 슬래시/스킴 없어 _collection이 안 풀린다(unresolvable).
        res.end(JSON.stringify({ results: isWiki ? [
          { file: 'orphan-card', title: 'Orphan wiki', score: 0.9 },
        ] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.88 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-orphan-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    // _collection 미해결 wiki 히트는 fail-closed로 drop → filtered 비어 raw backfill 트리거.
    // 수정 전엔 non-wiki 취급돼 filtered를 채워 backfill을 막고 raw prefix로 샜다.
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Raw source/);
    assert.doesNotMatch(parsed.hookSpecificOutput.additionalContext, /Orphan wiki/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('wikiOnly recall queries only wiki collections and emits the wiki result', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        recordQuery(requests, JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [
          { file: 'qmd://proj-wiki/concepts/config-layout.md', title: 'Config layout', score: 0.93 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-wikionly-'));
  try {
    mkdirSync(join(tempDir, '.auto-context', 'wiki', 'concepts'), { recursive: true });
    mkdirSync(join(tempDir, 'docs'), { recursive: true });
    writeTrustedCard(tempDir, 'config-layout', '# current config\n', 'Config layout body');
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionPaths: { 'proj-wiki': '.auto-context/wiki', 'proj-docs': 'docs' },
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'wikiOnly',
      wikiPath: '.auto-context/wiki',
      injectSummaryMaxChars: 0,
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout decision 내용을 알려줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /config-layout/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('wikiOnly recall does NOT backfill raw when wiki has no results (differs from hierarchical)', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        recordQuery(requests, JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-wikionly-empty-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'wikiOnly',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    // 결정적 차이: hierarchical이면 여기서 raw backfill(2번째 query)이 나가지만 wikiOnly는 안 나간다.
    assert.equal(requests.length, 1);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.equal(out, '');
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('wikiOnly recall makes no query and emits nothing when no wiki role is configured', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        recordQuery(requests, JSON.parse(body));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.99 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-wikionly-norole-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-docs'],
      collectionRoles: { 'proj-docs': 'raw' },
      recallStrategy: 'wikiOnly',
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'raw source 내용을 알려줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    // wiki role이 없으면 query 자체를 만들지 않고(raw 누출 금지) 무출력.
    assert.equal(requests.length, 0);
    assert.equal(out, '');
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall backfills raw when wiki results are below minScore', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [
          { file: 'qmd://proj-wiki/.auto-context/wiki/decisions/stale.md', title: 'Weak wiki result', score: 0.1 },
        ] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Raw source', score: 0.88 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-low-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      minScore: 0.5,
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /\[raw\]/);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Raw source/);
    assert.doesNotMatch(parsed.hookSpecificOutput.additionalContext, /Weak wiki result/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall applies rawFallbackMinScore to raw backfill results', async () => {
  // 임계 이상 raw 후보가 0건이면 순위 폴백도 아무것도 살리지 않는다 — 사용자가 임계로
  // 명시적으로 차단한 것이므로 0건이 정답이다(docs/settings.md 의 rawFallbackMinScore 계약).
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [] : [
          { file: 'qmd://proj-docs/docs/weak-raw.md', title: 'Weak raw source', score: 0.65 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-raw-threshold-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      minScore: 0.5,
      rawFallbackMinScore: 0.7,
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const logPath = join(tempDir, 'recall.log');
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}`, QMD_RECALL_LOG: logPath },
    );

    assert.equal(requests.length, 2);
    assert.deepEqual(requests[0].collections, ['proj-wiki']);
    assert.deepEqual(requests[1].collections, ['proj-docs']);
    assert.equal(out, '', 'raw fallback 임계가 실제로 차단해야 함');
    const selection = readFileSync(logPath, 'utf8')
      .trim().split('\n').map(line => JSON.parse(line))
      .find(e => e.event === 'qmd_recall_selection');
    assert.equal(selection.selected_count, 0);
    assert.deepEqual(selection.selected, []);
    assert.equal(selection.dropped_min_score, 1, '컷 탈락 수는 그대로 기록');
    assert.equal(selection.rank_fallback_used, false, '컷 자체를 무효화하면 안 된다');
    assert.equal(selection.min_score, 0.7, 'active min_score는 rawFallbackMinScore');
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('hierarchical recall can relax raw fallback below the wiki minScore', async () => {
  const requests = [];
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end('{"ok":true}');
      return;
    }
    if (req.method === 'POST' && req.url === '/query') {
      let body = '';
      req.setEncoding('utf8');
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        const payload = JSON.parse(body);
        recordQuery(requests, payload);
        const isWiki = payload.collections.includes('proj-wiki');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ results: isWiki ? [] : [
          { file: 'qmd://proj-docs/docs/raw-source.md', title: 'Relaxed raw source', score: 0.65 },
        ] }));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });

  const tempDir = mkdtempSync(join(process.cwd(), '.tmp-qmd-http-hier-raw-relaxed-'));
  try {
    mkdirSync(join(tempDir, '.auto-context'), { recursive: true });
    writeFileSync(join(tempDir, '.auto-context', 'settings.json'), JSON.stringify({
      indexing: true,
      collections: ['proj-wiki', 'proj-docs'],
      collectionRoles: { 'proj-wiki': 'wiki', 'proj-docs': 'raw' },
      recallStrategy: 'hierarchical',
      minScore: 0.7,
      rawFallbackMinScore: 0,
      queryTimeout: 1.25,
    }));

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address();
    const out = await runRecallAsync(
      JSON.stringify({ prompt: 'config layout 근거를 찾아줘', cwd: tempDir }),
      { ...process.env, QMD_DAEMON_URL: `http://127.0.0.1:${port}` },
    );

    assert.equal(requests.length, 2);
    const parsed = JSON.parse(out);
    assert.match(parsed.hookSpecificOutput.additionalContext, /\[raw\]/);
    assert.match(parsed.hookSpecificOutput.additionalContext, /Relaxed raw source/);
  } finally {
    await new Promise(resolve => server.close(resolve));
    removeTemp(tempDir);
  }
});

test('디스패처가 잘못된 stdin에도 graceful(크래시 없음)', () => {
  const out = execFileSync('bash', ['hooks/run-hook', 'recall', 'claude'], {
    input: 'not json',
    encoding: 'utf8',
    env: { ...process.env, CLAUDE_PLUGIN_ROOT: process.cwd() },
  });
  assert.equal(typeof out, 'string', '크래시 없이 종료');
});
