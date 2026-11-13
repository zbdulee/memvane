import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { chmodSync, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, unlinkSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

function python(script, ...args) {
  return execFileSync('python3', ['-c', script, ...args], {
    cwd: process.cwd(), encoding: 'utf8',
  }).trim();
}

function sha256(text) {
  return createHash('sha256').update(text).digest('hex');
}

function revision(project, relativePath = 'docs/source.md') {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.wiki_freshness as F',
    'print(json.dumps(F.snapshot_file(Path(sys.argv[1]) / sys.argv[2])))',
  ].join('\n'), project, relativePath));
}

function check(project, card) {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.wiki_freshness as F',
    'root = Path(sys.argv[1])',
    'card = json.loads(sys.argv[2])',
    'print(json.dumps(F.check_card(card, root, [(root / "docs").resolve()])))',
  ].join('\n'), project, JSON.stringify(card)));
}

function sourceRevision(project, path = 'docs/source.md') {
  return { kind: 'file', path, collection: 'proj-docs', ...revision(project, path) };
}

function strictPending(project) {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.wiki_freshness as F',
    'reader = getattr(F, "unresolved_pending_refreshes_strict", None)',
    'print(json.dumps({"api": "missing"} if reader is None else reader(Path(sys.argv[1]))))',
  ].join('\n'), project));
}

function strictPendingFault(project, fault) {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.wiki_freshness as F',
    'fault = sys.argv[2]',
    'real_exists = F.Path.exists',
    'real_read_text = F.Path.read_text',
    'def denied_pending_path(root):',
    '    raise PermissionError("denied parent")',
    'def denied_exists(path):',
    '    if path.name == "source-refresh-pending.jsonl":',
    '        raise PermissionError("denied exists")',
    '    return real_exists(path)',
    'def denied_read_text(path, *args, **kwargs):',
    '    if path.name == "source-refresh-pending.jsonl":',
    '        raise PermissionError("denied read")',
    '    return real_read_text(path, *args, **kwargs)',
    'if fault == "pending_path": F._pending_path = denied_pending_path',
    'if fault == "exists": F.Path.exists = denied_exists',
    'if fault == "read_text": F.Path.read_text = denied_read_text',
    'try:',
    '    result = F.unresolved_pending_refreshes_strict(Path(sys.argv[1]))',
    'except Exception as exc:',
    '    result = {"raised": type(exc).__name__}',
    'print(json.dumps(result))',
  ].join('\n'), project, fault));
}

function setup() {
  const project = mkdtempSync(join(tmpdir(), 'qmd-freshness-'));
  mkdirSync(join(project, 'docs'));
  writeFileSync(join(project, 'docs', 'source.md'), '# original\n');
  return project;
}

test('stable file revision makes an unchanged source fresh and changed bytes stale', () => {
  const project = setup();
  const source = join(project, 'docs', 'source.md');
  try {
    const card = { sourceRevisions: [sourceRevision(project)] };
    assert.deepEqual(check(project, card), { state: 'fresh', checked: 1 });

    writeFileSync(source, '# changed\n');
    assert.deepEqual(check(project, card), { state: 'stale', reason: 'content_hash_mismatch' });
  } finally { removeTemp(project); }
});

test('mtime-only change remains fresh because SHA-256 is the freshness proof', () => {
  const project = setup();
  const source = join(project, 'docs', 'source.md');
  try {
    const card = { sourceRevisions: [sourceRevision(project)] };
    utimesSync(source, new Date('2001-01-01T00:00:00Z'), new Date('2001-01-01T00:00:00Z'));
    assert.deepEqual(check(project, card), { state: 'fresh', checked: 1 });
  } finally { removeTemp(project); }
});

test('outside allow roots and malformed or legacy provenance fail closed as unknown', () => {
  const project = setup();
  const outside = join(tmpdir(), `qmd-freshness-outside-${process.pid}.md`);
  try {
    writeFileSync(outside, '# outside\n');
    const outsideRevision = {
      kind: 'file', path: outside, collection: 'outside',
      sha256: sha256('# outside\n'), size: Buffer.byteLength('# outside\n'), mtimeNs: 1,
    };
    assert.equal(check(project, { sourceRevisions: [outsideRevision] }).state, 'unknown');
    assert.equal(check(project, { sourceRevisions: [] }).state, 'unknown', 'legacy verified cards have no current-state proof');
    assert.equal(check(project, { reviewed: true, sourceRevisions: [] }).state, 'unknown', 'a human approval marker is not a trust substitute');
  } finally {
    try { unlinkSync(outside); } catch {}
    removeTemp(project);
  }
});

test('missing source is stale rather than authorized', () => {
  const project = setup();
  const source = join(project, 'docs', 'source.md');
  try {
    const card = { sourceRevisions: [sourceRevision(project)] };
    unlinkSync(source);
    assert.deepEqual(check(project, card), { state: 'stale', reason: 'missing' });
  } finally { removeTemp(project); }
});

test('snapshot_file refuses a file whose fstat changed during its single read', () => {
  const project = setup();
  try {
    const out = python([
      'import json, os, sys',
      'from pathlib import Path',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.wiki_freshness as F',
      'path = Path(sys.argv[1]) / "docs/source.md"',
      'real_fstat = F.os.fstat',
      'calls = 0',
      'def changed_fstat(fd):',
      '    global calls',
      '    calls += 1',
      '    if calls == 2:',
      '        path.write_bytes(b"# changed after read\\n")',
      '    return real_fstat(fd)',
      'F.os.fstat = changed_fstat',
      'print(json.dumps(F.snapshot_file(path)))',
    ].join('\n'), project);
    assert.equal(out, 'null');
  } finally { removeTemp(project); }
});

test('sourceRevisions uses a closed typed YAML flow-mapping schema', () => {
  const record = {
    kind: 'file', path: 'docs/source.md', collection: 'proj-docs',
    sha256: sha256('# original\n'), size: 11, mtimeNs: 456,
  };
  const out = JSON.parse(python([
    'import json, sys',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.yaml_scalars as Y',
    'record = json.loads(sys.argv[1])',
    'emitted = Y.dump_source_revisions([record])',
    'print(json.dumps({"emitted": emitted, "parsed": Y.load_source_revisions(emitted),',
    '                  "closed": Y.load_source_revisions(["{kind: file, path: docs/a.md, collection: c, sha256: ' + 'a'.repeat(64) + ', size: 1, mtimeNs: 2, extra: no}"])}))',
  ].join('\n'), JSON.stringify(record)));
  assert.deepEqual(out.parsed, [record]);
  assert.equal(out.emitted.length, 1);
  assert.deepEqual(out.closed, [], 'unknown keys must not become compiler-owned provenance');
});

test('quoted numeric fields and empty mapping entries fail closed before card freshness', () => {
  const project = setup();
  try {
    const record = sourceRevision(project);
    const emitted = JSON.parse(python([
      'import json, sys',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.yaml_scalars as Y',
      'record = json.loads(sys.argv[1])',
      'flow = Y.dump_source_revisions([record])[0]',
      'quoted = flow.replace("size: " + str(record["size"]), "size: \\\"" + str(record["size"]) + "\\\"")',
      'empty = flow.replace(", sha256:", ",, sha256:")',
      'print(json.dumps({"quoted": quoted, "empty": empty,',
      '                  "quotedParsed": Y.load_source_revisions([quoted]),',
      '                  "emptyParsed": Y.load_source_revisions([empty])}))',
    ].join('\n'), JSON.stringify(record)));
    assert.deepEqual(emitted.quotedParsed, []);
    assert.deepEqual(emitted.emptyParsed, []);
    assert.equal(check(project, { sourceRevisions: [emitted.quoted] }).state, 'unknown');
    assert.equal(check(project, { sourceRevisions: [emitted.empty] }).state, 'unknown');
  } finally { removeTemp(project); }
});

test('sourceRevisions emission rejects an entire list when any sibling is invalid', () => {
  const record = {
    kind: 'file', path: 'docs/source.md', collection: 'proj-docs',
    sha256: sha256('# original\n'), size: 11, mtimeNs: 456,
  };
  const out = JSON.parse(python([
    'import json, sys',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.yaml_scalars as Y',
    'record = json.loads(sys.argv[1])',
    'invalid = dict(record)',
    'invalid["size"] = "11"',
    'print(json.dumps(Y.dump_source_revisions([record, invalid])))',
  ].join('\n'), JSON.stringify(record)));
  assert.deepEqual(out, [], 'partial provenance must never be emitted');
});

test('strict pending ledger API distinguishes absent or empty from malformed, invalid, and unreadable', () => {
  const project = setup();
  const compileDir = join(project, '.auto-context', 'compile');
  const ledger = join(compileDir, 'source-refresh-pending.jsonl');
  try {
    assert.deepEqual(strictPending(project), [], 'safely absent ledger has no pending invalidation');
    mkdirSync(compileDir, { recursive: true });
    writeFileSync(ledger, '');
    assert.deepEqual(strictPending(project), [], 'empty ledger is known and valid');

    writeFileSync(ledger, '{malformed json\n');
    assert.equal(strictPending(project), null, 'malformed JSON makes ledger state unknown');

    writeFileSync(ledger, `${JSON.stringify({ state: 'pending_refresh', sourcePath: 'docs/source.md' })}\n`);
    assert.equal(strictPending(project), null, 'invalid closed-schema event makes ledger state unknown');

    const pending = {
      eventId: 'event-1', ts: '2026-08-07T00:00:00Z', sourcePath: 'docs/source.md',
      state: 'pending_refresh', engine: 'codex',
    };
    writeFileSync(ledger, `${JSON.stringify(pending)}\n${JSON.stringify(pending)}\n`);
    assert.equal(strictPending(project), null, 'duplicate pending event id makes ledger state unknown');

    writeFileSync(ledger, `${JSON.stringify({
      pendingEventId: 'missing-event', ts: '2026-08-07T00:00:01Z', state: 'resolved',
    })}\n`);
    assert.equal(strictPending(project), null, 'orphan resolved event makes ledger state unknown');

    writeFileSync(ledger, `${JSON.stringify(pending)}\n`);
    assert.deepEqual(strictPending(project), [pending], 'valid pending ledger remains distinguishable');
    chmodSync(ledger, 0o000);
    assert.equal(strictPending(project), null, 'unreadable ledger makes ledger state unknown');
  } finally {
    try { chmodSync(ledger, 0o600); } catch {}
    removeTemp(project);
  }
});

test('strict pending ledger converts every filesystem permission failure to unknown', () => {
  const project = setup();
  try {
    mkdirSync(join(project, '.auto-context', 'compile'), { recursive: true });
    writeFileSync(join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl'), '');
    for (const fault of ['pending_path', 'exists', 'read_text']) {
      assert.equal(strictPendingFault(project, fault), null, `${fault} PermissionError must be unknown`);
    }
  } finally { removeTemp(project); }
});

test('pending refresh resolves only with the successful compile revision still on disk', () => {
  const project = setup();
  try {
    const before = sourceRevision(project);
    writeFileSync(join(project, 'docs', 'source.md'), '# edited after old capture\n');
    const out = JSON.parse(python([
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.wiki_freshness as F',
      'root = Path(sys.argv[1])',
      'old = json.loads(sys.argv[2])',
      'pending = F.record_pending_refresh(root, "docs/source.md", "codex")',
      'old_ok = F.resolve_pending_refresh(root, pending, old, [old])',
      'current = {"kind": "file", "path": "docs/source.md", "collection": "proj-docs", **F.snapshot_file(root / "docs/source.md")}',
      'current_ok = F.resolve_pending_refresh(root, pending, current, [current])',
      'print(json.dumps({"oldOk": old_ok, "currentOk": current_ok, "rows": F.read_pending_refreshes(root)}))',
    ].join('\n'), project, JSON.stringify(before)));
    assert.equal(out.oldOk, false, 'a revision captured before the pending event cannot resolve it');
    assert.equal(out.currentOk, true);
    assert.deepEqual(out.rows.map((row) => row.state), ['pending_refresh', 'resolved']);
    assert.equal(out.rows[1].pendingEventId, out.rows[0].eventId);
    assert.deepEqual(Object.keys(out.rows[1]).sort(), ['pendingEventId', 'state', 'ts']);
    assert.doesNotMatch(JSON.stringify(out.rows), /sha256|sourceRevision|mtimeNs|"size"|content|secret/i);
  } finally { removeTemp(project); }
});

test('ordered history and unique event ids reject prior resolution and token replay for later ABA pending', () => {
  const project = setup();
  try {
    const out = JSON.parse(python([
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.wiki_freshness as F',
      'root = Path(sys.argv[1])',
      'old = F.record_pending_refresh(root, "docs/source.md", "codex")',
      'revision = {"kind": "file", "path": "docs/source.md", "collection": "proj-docs", **F.snapshot_file(root / "docs/source.md")}',
      'assert F.resolve_pending_refresh(root, old, revision, [revision])',
      '# Same bytes represent the A return of an A->B->A edit sequence; the new event id is the causal proof.',
      'later = F.record_pending_refresh(root, "docs/source.md", "codex")',
      'replay_ok = F.resolve_pending_refresh(root, old, revision, [revision])',
      'unresolved = F.unresolved_pending_refreshes(root)',
      'later_ok = F.resolve_pending_refresh(root, later, revision, [revision])',
      'print(json.dumps({"old": old, "later": later, "replayOk": replay_ok, "unresolved": unresolved, "laterOk": later_ok, "rows": F.read_pending_refreshes(root)}))',
    ].join('\n'), project));
    assert.notEqual(out.old.eventId, out.later.eventId);
    assert.equal(out.replayOk, false, 'an already-resolved token cannot resolve a later same-content edit');
    assert.deepEqual(out.unresolved.map((row) => row.eventId), [out.later.eventId],
      'an earlier resolved row does not suppress the later pending event');
    assert.equal(out.laterOk, true);
    assert.equal(out.rows.at(-1).pendingEventId, out.later.eventId);
  } finally { removeTemp(project); }
});

test('pending refresh compaction atomically retains only each source latest unresolved event or resolved pair', () => {
  const project = setup();
  try {
    writeFileSync(join(project, 'docs', 'other.md'), '# other\n');
    const rows = JSON.parse(python([
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.wiki_freshness as F',
      'root = Path(sys.argv[1])',
      'first = F.record_pending_refresh(root, "docs/source.md", "claude")',
      'rev = {"kind": "file", "path": "docs/source.md", "collection": "proj-docs", **F.snapshot_file(root / "docs/source.md")}',
      'assert F.resolve_pending_refresh(root, first, rev, [rev])',
      'latest = F.record_pending_refresh(root, "docs/source.md", "codex")',
      'other = F.record_pending_refresh(root, "docs/other.md", "hermes")',
      'other_rev = {"kind": "file", "path": "docs/other.md", "collection": "proj-docs", **F.snapshot_file(root / "docs/other.md")}',
      'assert F.resolve_pending_refresh(root, other, other_rev, [other_rev])',
      'assert F.compact_pending_refreshes(root, force=True)',
      'print(json.dumps(F.read_pending_refreshes(root)))',
    ].join('\n'), project));
    assert.equal(rows.length, 3);
    assert.deepEqual(rows.map((row) => [row.sourcePath, row.state]), [
      ['docs/source.md', 'pending_refresh'],
      ['docs/other.md', 'pending_refresh'],
      [undefined, 'resolved'],
    ]);
    assert.equal(rows[0].engine, 'codex');
    assert.equal(rows[2].pendingEventId, rows[1].eventId);
    assert.doesNotMatch(JSON.stringify(rows), /sha256|sourceRevision|mtimeNs|"size"|content|secret/i);
    assert.equal(existsSync(join(project, '.auto-context', 'compile', 'source-refresh-pending.jsonl.compact.tmp')), false);
  } finally { removeTemp(project); }
});

// ─────────────────────────────────────────────────────────────────────────────
// 손상 원장 격리(quarantine). 자기치유가 없으면 permissive recovery ↔ strict resolve
// 불일치로 worker run당 유료 2회가 영구 반복되고 recall은 전 후보 unknown이 된다.
// 오탐(정상 원장 격리)은 그 자체가 pending 표식 상실이므로 음성 케이스가 본체다.
// ─────────────────────────────────────────────────────────────────────────────

const PENDING_REL = ['.auto-context', 'compile', 'source-refresh-pending.jsonl'];

function quarantine(project) {
  return JSON.parse(python([
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, ".")',
    'import memvane.wiki.card.wiki_freshness as F',
    'print(json.dumps(F.quarantine_corrupt_pending(Path(sys.argv[1]))))',
  ].join('\n'), project));
}

function writePending(project, text) {
  mkdirSync(join(project, '.auto-context', 'compile'), { recursive: true });
  writeFileSync(join(project, ...PENDING_REL), text);
}

function pendingEvent(overrides = {}) {
  return JSON.stringify({
    eventId: 'e'.repeat(32), ts: '2026-08-01T00:00:00Z', sourcePath: 'docs/source.md',
    state: 'pending_refresh', engine: 'claude', ...overrides,
  });
}

function quarantineFiles(project) {
  const dir = join(project, '.auto-context', 'compile');
  return existsSync(dir)
    ? readdirSync(dir).filter((name) => name.startsWith('source-refresh-pending.corrupt-')) : [];
}

test('격리는 손상 원장을 개명해 치우고 원본 바이트를 남긴다(삭제 아님)', () => {
  const project = setup();
  try {
    // 실측된 발생원 그대로: 정상 1줄 + short-write가 남긴 개행 없는 잘린 줄.
    const torn = pendingEvent() + '\n{"engine": "claude", "eventId": "aaa", "sourceP';
    writePending(project, torn);
    assert.equal(strictPending(project), null, '전제: strict가 거부하는 상태');

    const result = quarantine(project);
    assert.equal(result.reason, 'strict_reject');
    assert.match(result.quarantinedTo, /^source-refresh-pending\.corrupt-\d{8}T\d{6}Z\.jsonl$/);

    // 원장은 **부재**로 남긴다(빈 파일 재생성 아님) — 다음 record_pending_refresh가 만든다.
    assert.equal(existsSync(join(project, ...PENDING_REL)), false, '원장은 부재로 남는다');
    const kept = join(project, '.auto-context', 'compile', result.quarantinedTo);
    assert.equal(readFileSync(kept, 'utf8'), torn, '원본 바이트를 그대로 보존한다');

    // 격리 후 strict가 다시 답을 낸다 = recall의 전 후보 unknown이 풀린다.
    assert.deepEqual(strictPending(project), [], '격리 후 strict는 known-empty');
  } finally { removeTemp(project); }
});

test('격리는 멱등이고 앞선 격리 파일을 덮어쓰지 않는다', () => {
  const project = setup();
  try {
    writePending(project, pendingEvent() + '\ngarbage not json\n');
    const first = quarantine(project);
    assert.equal(first.reason, 'strict_reject');
    assert.equal(quarantine(project), null, '두 번째 호출은 할 일이 없다');

    writePending(project, pendingEvent() + '\ngarbage not json\n');
    const second = quarantine(project);
    assert.notEqual(second, null);
    assert.equal(quarantineFiles(project).length, 2, '앞선 증거를 덮어쓰지 않는다');
  } finally { removeTemp(project); }
});

test('정상 원장은 격리하지 않는다 — 빈 줄·resolved 쌍·부재·빈 파일', () => {
  const project = setup();
  try {
    // strict가 받아들이는 형태는 전부 손상이 아니다. 오탐 = pending 표식 상실.
    assert.equal(quarantine(project), null, '원장 부재');
    writePending(project, '');
    assert.equal(quarantine(project), null, '빈 파일');
    writePending(project, pendingEvent() + '\n\n   \n');
    assert.equal(quarantine(project), null, '빈 줄은 손상이 아니다');
    writePending(project, [pendingEvent(),
      JSON.stringify({ pendingEventId: 'e'.repeat(32), ts: '2026-08-01T00:01:00Z', state: 'resolved' }),
    ].join('\n') + '\n');
    assert.equal(quarantine(project), null, '정상 pending/resolved 쌍');
    assert.equal(quarantineFiles(project).length, 0, '아무것도 격리되지 않았다');
    assert.equal(existsSync(join(project, ...PENDING_REL)), true, '원장이 그대로 남아 있다');
  } finally { removeTemp(project); }
});

test('읽을 수 없는 원장은 격리하지 않는다(권한 문제로 정상 원장을 잃지 않는다)', () => {
  const project = setup();
  try {
    writePending(project, pendingEvent() + '\n');
    const out = python([
      'import json, sys',
      'from pathlib import Path',
      'sys.path.insert(0, ".")',
      'import memvane.wiki.card.wiki_freshness as F',
      'real_read_text = F.Path.read_text',
      'def denied(path, *a, **k):',
      '    if path.name == "source-refresh-pending.jsonl":',
      '        raise PermissionError("denied read")',
      '    return real_read_text(path, *a, **k)',
      'F.Path.read_text = denied',
      'print(json.dumps(F.quarantine_corrupt_pending(Path(sys.argv[1]))))',
    ].join('\n'), project);
    assert.equal(JSON.parse(out), null, 'OSError는 손상이 아니다');
    assert.equal(existsSync(join(project, ...PENDING_REL)), true);
  } finally { removeTemp(project); }
});

test('비UTF8 바이트는 격리한다(내용 손상이므로)', () => {
  const project = setup();
  try {
    mkdirSync(join(project, '.auto-context', 'compile'), { recursive: true });
    writeFileSync(join(project, ...PENDING_REL),
      Buffer.concat([Buffer.from(pendingEvent() + '\n'), Buffer.from([0xff, 0xfe]), Buffer.from('\n')]));
    const result = quarantine(project);
    assert.equal(result.reason, 'invalid_utf8');
    assert.equal(existsSync(join(project, ...PENDING_REL)), false);
  } finally { removeTemp(project); }
});

test('CLI는 손상 여부와 무관하게 exit 0이다(worker를 죽이지 않는다)', () => {
  const project = setup();
  try {
    writePending(project, pendingEvent() + '\ngarbage\n');
    const out = execFileSync('python3', ['core/wiki_freshness.py',
      '--cwd', project, '--quarantine-corrupt-pending', '--json'],
    { cwd: process.cwd(), encoding: 'utf8' });
    assert.equal(JSON.parse(out).reason, 'strict_reject');
    // 할 일이 없어도 exit 0 + 빈 객체.
    const again = execFileSync('python3', ['core/wiki_freshness.py',
      '--cwd', project, '--quarantine-corrupt-pending', '--json'],
    { cwd: process.cwd(), encoding: 'utf8' });
    assert.deepEqual(JSON.parse(again), {});
  } finally { removeTemp(project); }
});
