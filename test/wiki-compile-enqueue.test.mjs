import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, realpathSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

function setupProject(config = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-enqueue-'));
  mkdirSync(join(dir, '.auto-context'), { recursive: true });
  mkdirSync(join(dir, 'docs'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  writeFileSync(join(dir, 'docs', 'source.md'), '# Source\n');
  writeFileSync(join(dir, 'docs', 'note.txt'), 'plain text\n');
  writeFileSync(join(dir, 'docs', 'tool.py'), 'print(1)\n');
  writeFileSync(join(dir, '.auto-context', 'wiki', 'generated.md'), '# Generated\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['proj-docs', 'proj-wiki'],
    collectionPaths: {
      'proj-docs': 'docs',
      'proj-wiki': '.auto-context/wiki',
    },
    collectionRoles: {
      'proj-docs': 'raw',
      'proj-wiki': 'wiki',
    },
    compile: {
      mode: 'guarded',
      triggers: ['post_tool_source', 'manual'],
      ...config.compile,
    },
    ...config,
  }));
  return dir;
}

function runEnqueue(cwd, payload, env = {}) {
  return execFileSync('python3', ['core/wiki_compile_enqueue.py'], {
    cwd: process.cwd(),
    input: JSON.stringify({ hook_event_name: 'PostToolUse', cwd, ...payload }),
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

function queueLines(project) {
  const q = join(project, '.auto-context', 'compile', 'source-queue.jsonl');
  if (!existsSync(q)) return [];
  return readFileSync(q, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

test('source-text extensions are enqueued; .py and .md under node_modules are not', () => {
  const project = setupProject();
  try {
    mkdirSync(join(project, 'docs', 'node_modules', 'pkg'), { recursive: true });
    writeFileSync(join(project, 'docs', 'node_modules', 'pkg', 'README.md'), '# dep\n');
    writeFileSync(join(project, 'docs', 'guide.RST'), 'Guide\n=====\n');
    for (const rel of ['note.txt', 'guide.RST', 'tool.py', join('node_modules', 'pkg', 'README.md')]) {
      runEnqueue(project, { tool_input: { file_path: join(project, 'docs', rel) } });
    }
    assert.deepEqual(queueLines(project).map((job) => job.source.path).sort(),
      ['docs/guide.RST', 'docs/note.txt']);
  } finally {
    removeTemp(project);
  }
});

function pendingLines(project) {
  const path = join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl');
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map((line) => JSON.parse(line));
}

test('source edit records pending refresh before its source queue job', () => {
  const project = setupProject();
  try {
    const out = runEnqueue(project, {
      engine: 'codex', tool_input: { file_path: join(project, 'docs', 'source.md') },
    });
    assert.equal(out, '');
    const pending = pendingLines(project);
    const jobs = queueLines(project);
    assert.equal(pending.length, 1);
    assert.equal(pending[0].sourcePath, 'docs/source.md');
    assert.equal(pending[0].state, 'pending_refresh');
    assert.equal(pending[0].engine, 'codex');
    assert.match(pending[0].eventId, /^[0-9a-f-]{32,36}$/);
    assert.deepEqual(Object.keys(pending[0]).sort(), ['engine', 'eventId', 'sourcePath', 'state', 'ts']);
    assert.equal(jobs.length, 1);
    assert.deepEqual(jobs[0].pendingRefresh, {
      eventId: pending[0].eventId, ts: pending[0].ts, engine: pending[0].engine,
    }, 'the queue carries the exact pending event that causally precedes its source snapshot');
  } finally {
    removeTemp(project);
  }
});

test('pending append failure keeps the hook silent and does not queue an unguarded source job', () => {
  const project = setupProject();
  const outside = join(mkdtempSync(join(tmpdir(), 'qwiki-pending-outside-')), 'ledger.jsonl');
  try {
    const oldRevision = JSON.parse(execFileSync('python3', ['-c', [
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, "core")',
      'import wiki_freshness as F',
      'print(json.dumps(F.snapshot_file(Path(sys.argv[1]) / "docs/source.md")))',
    ].join('\n'), project], { cwd: process.cwd(), encoding: 'utf8' }));
    mkdirSync(join(project, '.auto-context', 'compile'), { recursive: true });
    writeFileSync(outside, 'outside-must-not-change\n');
    symlinkSync(outside, join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl'));
    writeFileSync(join(project, 'docs', 'source.md'), '# Changed source\n');

    const out = runEnqueue(project, { tool_input: { file_path: join(project, 'docs', 'source.md') } });
    assert.equal(out, '', 'hook failures stay silent');
    assert.deepEqual(queueLines(project), [], 'queueing without durable pending invalidation is unsafe');
    assert.equal(readFileSync(outside, 'utf8'), 'outside-must-not-change\n');

    const freshness = JSON.parse(execFileSync('python3', ['-c', [
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, "core")',
      'import wiki_freshness as F',
      'root = Path(sys.argv[1])',
      'revision = json.loads(sys.argv[2])',
      'card = {"sourceRevisions": [{"kind": "file", "path": "docs/source.md", "collection": "proj-docs", **revision}]}',
      'print(json.dumps(F.check_card(card, root, [(root / "docs").resolve()])))',
    ].join('\n'), project, JSON.stringify(oldRevision)], { cwd: process.cwd(), encoding: 'utf8' }));
    assert.equal(freshness.state, 'stale', 'the final source hash guard remains the safety net');
  } finally {
    removeTemp(project);
    removeTemp(join(outside, '..'));
  }
});

test('raw markdown edit enqueues bounded source job silently', () => {
  const project = setupProject();
  try {
    const out = runEnqueue(project, { tool_input: { file_path: join(project, 'docs', 'source.md') } });
    assert.equal(out, '');
    const jobs = queueLines(project);
    assert.equal(jobs.length, 1);
    assert.equal(jobs[0].trigger, 'post_tool_source');
    assert.equal(jobs[0].engine, 'unknown');
    assert.equal(jobs[0].cwd, realpathSync(project));
    assert.deepEqual(jobs[0].source, { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' });
    assert.equal('content' in jobs[0].source, false);
  } finally {
    removeTemp(project);
  }
});

test('Codex apply_patch command payload enqueues the patched markdown source', () => {
  const project = setupProject();
  try {
    runEnqueue(project, {
      tool_name: 'apply_patch',
      tool_input: {
        command: '*** Begin Patch\n*** Update File: docs/source.md\n@@\n-# Source\n+# Source updated\n*** End Patch',
      },
    });
    const jobs = queueLines(project);
    assert.equal(jobs.length, 1);
    assert.deepEqual(jobs[0].source, { kind: 'file', path: 'docs/source.md', collection: 'proj-docs' });
  } finally {
    removeTemp(project);
  }
});

test('non-text source, outside collection, wiki role, disabled trigger, and sandbox do not enqueue', () => {
  const project = setupProject();
  try {
    runEnqueue(project, { tool_input: { file_path: join(project, 'docs', 'tool.py') } });
    runEnqueue(project, { tool_input: { file_path: join(project, 'README.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, '.auto-context', 'wiki', 'generated.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, 'docs', 'source.md') } }, { QMD_SANDBOX: '1' });
    const optout = setupProject({ indexing: false });
    try {
      runEnqueue(optout, { tool_input: { file_path: join(optout, 'docs', 'source.md') } });
      assert.deepEqual(queueLines(optout), []);
    } finally {
      removeTemp(optout);
    }
    const pending = setupProject({ indexing: null });
    try {
      runEnqueue(pending, { tool_input: { file_path: join(pending, 'docs', 'source.md') } });
      assert.deepEqual(queueLines(pending), []);
    } finally {
      removeTemp(pending);
    }
    assert.deepEqual(queueLines(project), []);

    const disabled = setupProject({ compile: { triggers: ['manual'] } });
    try {
      runEnqueue(disabled, { tool_input: { file_path: join(disabled, 'docs', 'source.md') } });
      assert.deepEqual(queueLines(disabled), []);
    } finally {
      removeTemp(disabled);
    }
    const modeOff = setupProject({ compile: { mode: 'off' } });
    try {
      runEnqueue(modeOff, { tool_input: { file_path: join(modeOff, 'docs', 'source.md') } });
      assert.deepEqual(queueLines(modeOff), []);
    } finally {
      removeTemp(modeOff);
    }
  } finally {
    removeTemp(project);
  }
});

test('dot-directory and hidden markdown sources do not enqueue for automatic compile', () => {
  const project = setupProject({
    collections: ['proj-root'],
    collectionPaths: { 'proj-root': '.' },
    collectionRoles: { 'proj-root': 'raw' },
  });
  try {
    mkdirSync(join(project, '.agents', 'skills'), { recursive: true });
    mkdirSync(join(project, 'docs', '.draft'), { recursive: true });
    writeFileSync(join(project, '.agents', 'skills', 'writer.md'), '# Agent Skill\n');
    writeFileSync(join(project, 'docs', '.draft', 'idea.md'), '# Draft\n');
    writeFileSync(join(project, 'docs', '.hidden.md'), '# Hidden file\n');

    runEnqueue(project, { tool_input: { file_path: join(project, '.agents', 'skills', 'writer.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, 'docs', '.draft', 'idea.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, 'docs', '.hidden.md') } });

    assert.deepEqual(queueLines(project), []);
  } finally {
    removeTemp(project);
  }
});

// 명시적으로 등록된 dot collection 루트(novel의 `.nova/06_Sessions`)는 compile source가
// 되어야 한다. dot 정책은 "등록된 루트 아래"에만 적용되므로 면제 범위가 루트 접두부로
// 한정되고, `collectionPaths: '.'`처럼 접두부가 비면 아무것도 면제되지 않는다.
function setupDotProject(extra = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'qwiki-dotenq-'));
  mkdirSync(join(dir, '.nova', '06_Sessions', 'archive'), { recursive: true });
  mkdirSync(join(dir, '.nova', '06_Sessions', '.drafts'), { recursive: true });
  mkdirSync(join(dir, '.claude'), { recursive: true });
  mkdirSync(join(dir, '.github'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'wiki'), { recursive: true });
  mkdirSync(join(dir, '.auto-context', 'compile'), { recursive: true });
  writeFileSync(join(dir, '.nova', '06_Sessions', '2026-07-29.md'), '# Session\n');
  writeFileSync(join(dir, '.nova', '06_Sessions', 'archive', 'old.md'), '# Old\n');
  writeFileSync(join(dir, '.nova', '06_Sessions', '.drafts', 'wip.md'), '# WIP\n');
  writeFileSync(join(dir, '.nova', '06_Sessions', '.hidden.md'), '# Hidden\n');
  writeFileSync(join(dir, '.claude', 'agent.md'), '# Agent\n');
  writeFileSync(join(dir, '.github', 'workflow.md'), '# CI\n');
  writeFileSync(join(dir, '.auto-context', 'wiki', 'card.md'), '# Card\n');
  writeFileSync(join(dir, '.auto-context', 'compile', 'notes.md'), '# Queue notes\n');
  writeFileSync(join(dir, '.auto-context', 'settings.json'), JSON.stringify({
    indexing: true,
    collections: ['sessions'],
    collectionPaths: { sessions: '.nova/06_Sessions' },
    collectionRoles: { sessions: 'session' },
    compile: {
      mode: 'auto-wiki',
      triggers: ['post_tool_source', 'manual'],
    },
    ...extra,
  }));
  return dir;
}

test('registered dot collection root enqueues, dot paths below it still do not', () => {
  const project = setupDotProject();
  try {
    runEnqueue(project, { tool_input: { file_path: join(project, '.nova', '06_Sessions', '2026-07-29.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, '.nova', '06_Sessions', 'archive', 'old.md') } });
    const jobs = queueLines(project);
    assert.deepEqual(jobs.map((j) => j.source.path), [
      '.nova/06_Sessions/2026-07-29.md',
      '.nova/06_Sessions/archive/old.md',
    ]);
    assert.deepEqual(jobs[0].source, {
      kind: 'file', path: '.nova/06_Sessions/2026-07-29.md', collection: 'sessions',
    });
    assert.equal('content' in jobs[0].source, false);

    // 등록된 루트 아래에서 새로 나타나는 dot segment는 계속 배제된다.
    runEnqueue(project, { tool_input: { file_path: join(project, '.nova', '06_Sessions', '.drafts', 'wip.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, '.nova', '06_Sessions', '.hidden.md') } });
    // 등록되지 않은 dot 경로는 collection 밖이므로 애초에 걸리지 않는다.
    runEnqueue(project, { tool_input: { file_path: join(project, '.claude', 'agent.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, '.github', 'workflow.md') } });
    assert.equal(queueLines(project).length, 2);
  } finally {
    removeTemp(project);
  }
});

test('.auto-context stays out of compile sources even when registered as a raw collection', () => {
  // 최악의 오설정: 사용자가 wikiPath와 compile 큐 디렉터리를 raw collection으로 등록.
  // role 게이트를 우회해도 관리 영역 deny가 남아야 한다 — wiki 카드가 자기 자신을
  // 소스로 재컴파일하면 무한 증식한다.
  const project = setupDotProject({
    collections: ['sessions', 'wiki-as-raw', 'compile-as-raw'],
    collectionPaths: {
      sessions: '.nova/06_Sessions',
      'wiki-as-raw': '.auto-context/wiki',
      'compile-as-raw': '.auto-context/compile',
    },
    collectionRoles: { sessions: 'session', 'wiki-as-raw': 'raw', 'compile-as-raw': 'raw' },
  });
  try {
    runEnqueue(project, { tool_input: { file_path: join(project, '.auto-context', 'wiki', 'card.md') } });
    runEnqueue(project, { tool_input: { file_path: join(project, '.auto-context', 'compile', 'notes.md') } });
    assert.deepEqual(queueLines(project), []);
  } finally {
    removeTemp(project);
  }
});

test('collectionPaths "." does not open dot directories via the registered-root exemption', () => {
  // 최대 리스크: 저장소 전체가 등록된 프로젝트. 면제되는 루트 접두부가 비어 있으므로
  // rel path 전체가 dot 검사를 받아 dot 디렉터리가 하나도 열리지 않아야 한다.
  const project = setupDotProject({
    collections: ['repo-root'],
    collectionPaths: { 'repo-root': '.' },
    collectionRoles: { 'repo-root': 'raw' },
  });
  try {
    mkdirSync(join(project, 'docs'), { recursive: true });
    writeFileSync(join(project, 'docs', 'plain.md'), '# Plain\n');
    for (const p of [
      ['.nova', '06_Sessions', '2026-07-29.md'],
      ['.claude', 'agent.md'],
      ['.github', 'workflow.md'],
      ['.auto-context', 'wiki', 'card.md'],
      ['.auto-context', 'compile', 'notes.md'],
    ]) {
      runEnqueue(project, { tool_input: { file_path: join(project, ...p) } });
    }
    assert.deepEqual(queueLines(project), []);

    // 같은 프로젝트의 non-dot 경로는 정상 큐잉되어야 한다(과도 차단 아님).
    runEnqueue(project, { tool_input: { file_path: join(project, 'docs', 'plain.md') } });
    assert.deepEqual(queueLines(project).map((j) => j.source.path), ['docs/plain.md']);
  } finally {
    removeTemp(project);
  }
});
