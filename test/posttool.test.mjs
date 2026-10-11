// posttool 코어: collectionPaths 기반 reader-facing 판별 + recall 위임 hint
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolve } from 'node:path';
import { performance } from 'node:perf_hooks';
import { removeTemp } from './helpers/temp.mjs';

const PROJ = resolve('test/fixtures/story-proj');

test('posttool bounds its child recall inside the shared hook deadline', () => {
  const dir = mkdtempSync(join(tmpdir(), 'qmd-posttool-deadline-'));
  try {
    const fake = join(dir, 'python3');
    writeFileSync(fake, '#!/bin/sh\nsleep 5\n');
    chmodSync(fake, 0o700);
    const started = performance.now();
    const out = execFileSync('/usr/bin/python3', ['core/posttool.py'], {
      input: JSON.stringify({hook_event_name: 'PostToolUse', tool_name: 'Write',
        tool_input: {file_path: `${PROJ}/04_Manuscript/ep004-상가-음식.md`,
          content: 'Synthetic source content long enough for a search hint.'}, cwd: PROJ}),
      encoding: 'utf8', timeout: 4000,
      env: {...process.env, QMD_RECALL_LOG: '', QMD_HOOK_TOTAL_SECONDS: '2',
        PATH: `${dir}:${process.env.PATH}`},
    });
    assert.equal(out.trim(), '');
    assert.ok(performance.now() - started < 3500);
  } finally {
    removeTemp(dir);
  }
});

test('apply_patch search text uses added lines, never headers or delete commands', () => {
  const patch = '*** Begin Patch\n*** Delete File: 04_Manuscript/old.md\n*** Update File: 04_Manuscript/new.md\n@@\n-old text\n+새로 쓰인 실제 본문입니다.\n+++counter\n*** End Patch';
  const script = 'import json,sys;sys.path.insert(0,"core");import posttool;print(json.dumps([posttool.extract_text({"tool_name":"apply_patch","tool_input":{"command":p}}) for p in json.load(sys.stdin)]))';
  const out = execFileSync('python3', ['-c', script], {
    input: JSON.stringify([patch, '*** Begin Patch\n*** Delete File: 04_Manuscript/old.md\n*** End Patch']),
    encoding: 'utf8',
  });
  assert.deepEqual(JSON.parse(out), ['새로 쓰인 실제 본문입니다.\n++counter', '']);
});

function posttool(payload, env = {}) {
  const out = execFileSync('python3', ['core/posttool.py'], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: { ...process.env, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response-ep.json',
      QMD_SETUP_GUARD_FIXTURE: '1', ...env },
  });
  return out.trim() ? JSON.parse(out) : null;
}

test('산문 파일(collectionPaths 경로) Write → PostToolUse hint', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'Write',
    tool_input: { file_path: `${PROJ}/04_Manuscript/ep004-상가-음식.md`, content: '4화에 대해서 집필. 도준이 죽었다는 문장을 확인한다.' },
    cwd: PROJ,
  });
  assert.ok(r);
  assert.equal(r.hookSpecificOutput.hookEventName, 'PostToolUse');
});

test('agy write_to_file TargetFile/CodeContent → PostToolUse hint', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'write_to_file',
    tool_input: {
      TargetFile: `${PROJ}/04_Manuscript/ep004-상가-음식.md`,
      CodeContent: '4화에 대해서 집필. 도준이 죽었다는 문장을 확인한다.',
    },
    cwd: PROJ,
  });
  assert.ok(r);
  assert.equal(r.hookSpecificOutput.hookEventName, 'PostToolUse');
});

test('agy replace_file_content TargetFile/ReplacementContent → PostToolUse hint', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'replace_file_content',
    tool_input: {
      TargetFile: `${PROJ}/04_Manuscript/ep004-상가-음식.md`,
      ReplacementContent: '4화 수정. 도준이 죽었다는 문장을 다시 확인한다.',
    },
    cwd: PROJ,
  });
  assert.ok(r);
  assert.equal(r.hookSpecificOutput.hookEventName, 'PostToolUse');
});

test('agy multi_replace_file_content ReplacementChunks → PostToolUse hint', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'multi_replace_file_content',
    tool_input: {
      ReplacementChunks: [
        {
          TargetFile: `${PROJ}/04_Manuscript/ep004-상가-음식.md`,
          ReplacementContent: '4화 수정. 도준이 죽었다는 문장을 다시 확인한다.',
        },
      ],
    },
    cwd: PROJ,
  });
  assert.ok(r);
  assert.equal(r.hookSpecificOutput.hookEventName, 'PostToolUse');
});

test('비-산문 파일은 skip → 빈 출력', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'Write',
    tool_input: { file_path: `${PROJ}/docs/plans/example.md`, content: '4화에 대해서 집필해줘 충분히 긴 텍스트' },
    cwd: PROJ,
  });
  assert.equal(r, null);
});

test('collectionPaths 없는 프로젝트는 보수적으로 skip', () => {
  const r = posttool({
    hook_event_name: 'PostToolUse',
    tool_name: 'Write',
    tool_input: { file_path: '/tmp/plain-proj/04_Manuscript/x.md', content: '충분히 긴 산문 텍스트입니다 도준' },
    cwd: '/tmp/plain-proj',
  });
  assert.equal(r, null);
});

test('events 에 postToolUse 없으면 posttool core skip', () => {
  const tempDir = mkdtempSync(join(tmpdir(), 'qmd-posttool-events-'));
  try {
    mkdirSync(join(tempDir, '.agents'), { recursive: true });
    mkdirSync(join(tempDir, '04_Manuscript'), { recursive: true });
    writeFileSync(join(tempDir, '.agents', 'qmd-recall.json'), JSON.stringify({
      collections: ['story-manuscript'],
      collectionPaths: { '*-manuscript': '04_Manuscript' },
      events: ['userPromptSubmit'],
    }));
    const out = execFileSync('python3', ['core/posttool.py'], {
      input: JSON.stringify({
        hook_event_name: 'PostToolUse',
        tool_input: { file_path: join(tempDir, '04_Manuscript', 'ep001.md'), content: '검색 결과 정렬은 어떻게 동작하는지 내용을 충분히 길게 수정' },
        cwd: tempDir,
      }),
      encoding: 'utf8',
      env: { ...process.env, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response.json' },
    });
    assert.equal(out.trim(), '');
  } finally {
    removeTemp(tempDir);
  }
});

test('posttool core: QMD_SANDBOX=true → 무출력 exit 0', () => {
  const out = execFileSync('python3', ['core/posttool.py'], {
    input: JSON.stringify({
      hook_event_name: 'PostToolUse',
      tool_name: 'Write',
      tool_input: { file_path: `${PROJ}/04_Manuscript/ep004-상가-음식.md`, content: '4화에 대해서 집필. 도준이 죽었다는 문장을 확인한다.' },
      cwd: PROJ,
    }),
    env: { ...process.env, QMD_SANDBOX: 'true', QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response-ep.json' },
  });
  assert.equal(out.toString().trim(), '');
});

test('posttool core: --sandbox 인자 → 무출력 exit 0', () => {
  const out = execFileSync('python3', ['core/posttool.py', '--sandbox'], {
    input: JSON.stringify({
      hook_event_name: 'PostToolUse',
      tool_name: 'Write',
      tool_input: { file_path: `${PROJ}/04_Manuscript/ep004-상가-음식.md`, content: '4화에 대해서 집필. 도준이 죽었다는 문장을 확인한다.' },
      cwd: PROJ,
    }),
  });
  assert.equal(out.toString().trim(), '');
});

test('posttool: .auto-context.json indexing:false → 빈 출력(skip)', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pt-out-'));
  mkdirSync(join(dir, '04_Manuscript'), { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: false,
    collections: ['story-manuscript'],
    collectionPaths: { 'story-manuscript': '04_Manuscript' },
  }));
  try {
    const out = execFileSync('python3', ['core/posttool.py'], {
      input: JSON.stringify({
        hook_event_name: 'PostToolUse',
        tool_name: 'Write',
        tool_input: { file_path: join(dir, '04_Manuscript', 'ep001.md'), content: '충분히 긴 산문 텍스트입니다 indexing false 테스트' },
        cwd: dir,
      }),
      encoding: 'utf8',
      env: { ...process.env, QMD_QUERY_FIXTURE: 'test/fixtures/daemon-response-ep.json' },
    });
    assert.equal(out.trim(), '');
  } finally {
    removeTemp(dir);
  }
});

test('posttool: .auto-context.json indexing:true + collectionPaths → PostToolUse hint', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pt-acj-'));
  mkdirSync(join(dir, '04_Manuscript'), { recursive: true });
  writeFileSync(join(dir, '.auto-context.json'), JSON.stringify({
    indexing: true,
    collections: ['story-manuscript'],
    collectionPaths: { 'story-manuscript': '04_Manuscript' },
  }));
  try {
    const r = posttool({
      hook_event_name: 'PostToolUse',
      tool_name: 'Write',
      tool_input: { file_path: join(dir, '04_Manuscript', 'ep001.md'), content: '충분히 긴 산문 텍스트입니다 도준이 죽었다는 장면' },
      cwd: dir,
    });
    assert.ok(r, '.auto-context.json collectionPaths 기반 story path 인식 실패');
    assert.equal(r.hookSpecificOutput.hookEventName, 'PostToolUse');
  } finally {
    removeTemp(dir);
  }
});
