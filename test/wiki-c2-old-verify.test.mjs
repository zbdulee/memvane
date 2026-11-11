import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, mkdirSync, readFileSync, realpathSync, statSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const base = 'c463cb0cef92c31d85cf9fd6a4dbff234a7d3826';
const worker = `
import sys
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
import wiki_compile
class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
wiki_compile.datetime = FixedDatetime
import wiki_verify_worker
sys.argv = ['wiki_verify_worker.py', '--cwd', sys.argv[2], '--json']
sys.exit(wiki_verify_worker.main())
`;

test('C1 verify worker ignores added fingerprints while preserving verdict and ACK', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'memvane-old-verify-')));
  try {
    const head = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
    const child = execFileSync('python3', ['-B', '-c',
      'import sys; print(sys.executable); print(sys.version.split()[0])'], { encoding: 'utf8' }).trim().replace('\n', ' ');
    console.log(`head=${head} old-worker=${base} fixture-python=${child}`);
    const oldTree = join(home, 'old');
    const archive = join(home, 'base.tar');
    mkdirSync(oldTree);
    execFileSync('git', ['archive', '--format=tar', `--output=${archive}`, base, 'core'], { encoding: 'utf8' });
    execFileSync('tar', ['-xf', archive, '-C', oldTree], { encoding: 'utf8' });
    const verifier = 'import json; print(json.dumps({"verdict":"pass","claims":[{"claim":"synthetic","supported":True,"quote":"synthetic evidence","sourcePath":"docs/source.md"}],"reasons":[]}))';
    const rows = [];
    for (const withFingerprints of [false, true]) {
      const project = join(home, withFingerprints ? 'with' : 'without');
      const wiki = join(project, '.auto-context', 'wiki', 'concepts');
      const compile = join(project, '.auto-context', 'compile');
      mkdirSync(wiki, { recursive: true }); mkdirSync(compile, { recursive: true });
      mkdirSync(join(project, 'docs'), { recursive: true });
      const source = join(project, 'docs', 'source.md');
      writeFileSync(source, 'synthetic evidence\n');
      utimesSync(source, 1700000000, 1700000000);
      const stats = statSync(source, { bigint: true });
      const revision = { kind: 'file', path: 'docs/source.md', collection: 'docs',
        sha256: createHash('sha256').update(readFileSync(source)).digest('hex'),
        size: Number(stats.size), mtimeNs: Number(stats.mtimeNs) };
      const card = join(wiki, 'card.md');
      writeFileSync(card, '---\ntitle: Card\nstatus: generated\ncreatedBy: qmd-auto-context\n---\n' +
        '<!-- qmd:auto:start id="main" sourceHash="abc123" -->\nSynthetic claim.\n<!-- qmd:auto:end -->\n');
      writeFileSync(join(project, '.auto-context', 'settings.json'), JSON.stringify({
        indexing: true, collections: ['docs', 'wiki'],
        collectionPaths: { docs: 'docs', wiki: '.auto-context/wiki' },
        collectionRoles: { docs: 'raw', wiki: 'wiki' }, wikiPath: '.auto-context/wiki',
        compile: { mode: 'auto-wiki', defaultStatus: 'generated', triggers: ['manual'],
          extractor: { backends: { claude: ['python3', '-B', '-c', verifier] }, timeout: 30 },
          verify: { enabled: true, timeout: 30 } },
      }));
      const fingerprint = { schema: 'memvane-compile-source-fingerprints-v1', viewId: 'a'.repeat(64),
        checkoutDigest: 'b'.repeat(64), gitObjectFormat: 'sha1', scanId: 'c'.repeat(32), observedNs: 1,
        fingerprints: [{ worktree_id: 'd'.repeat(32), branch_key: 'refs/heads/main', path: 'docs/source.md',
          source_sha: revision.sha256, byte_length: revision.size, head_oid: 'e'.repeat(40),
          tracked_state: 'tracked', binding_state: 'observed', scan_id: 'c'.repeat(32), observed_ns: 1 }] };
      const queue = join(compile, 'verify-queue.jsonl');
      const job = { ts: '2026-01-01T00:00:00Z', targetPath: '.auto-context/wiki/concepts/card.md',
        sources: [{ kind: 'file', path: 'docs/source.md', collection: 'docs' }],
        sourceRevisions: [revision], sourceHash: 'abc123', engine: 'claude', trigger: 'manual',
        ...(withFingerprints ? { sourceFingerprints: fingerprint } : {}) };
      writeFileSync(queue, JSON.stringify(job) + '\n');
      const env = { ...process.env, HOME: home, TMPDIR: home, QMD_DIRTY_QUEUE: join(project, 'dirty-queue'),
        QMD_SETUP_GUARD_FIXTURE: '1', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' };
      const run = spawnSync('python3', ['-B', '-c', worker, join(oldTree, 'core'), project],
        { cwd: oldTree, encoding: 'utf8', env, timeout: 120000 });
      assert.equal(run.status, 0, run.stderr || run.stdout);
      const output = JSON.parse(run.stdout);
      const verdicts = readFileSync(join(compile, 'verify-log.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
      rows.push({ output, card: readFileSync(card, 'utf8'), queue: readFileSync(queue, 'utf8'),
        result: verdicts.at(-1).result, verdict: verdicts.at(-1).verdict });
    }
    assert.deepEqual(rows[0], rows[1]);
    assert.equal(rows[0].output.processed, 1);
    assert.equal(rows[0].queue, '');
    assert.equal(rows[0].result, 'verified');
    assert.equal(rows[0].verdict, 'pass');
    assert.match(rows[0].card, /^status: verified$/m);
    assert.equal(existsSync(join(home, '.local', 'state', 'memvane')), false);
  } finally { removeTemp(home); }
});
