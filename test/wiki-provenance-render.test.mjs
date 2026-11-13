import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

test('verification render is deterministic and leaves the card untouched', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-render-home-')));
  try {
    const code = `
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, '.')
import memvane.wiki.compile.wiki_compile as wc
card = Path.home() / 'card.md'
original = '---\\ntitle: Test\\nstatus: generated\\n---\\nbody\\n'
card.write_text(original, encoding='utf8')
assert hasattr(wc, 'render_verification'), 'deterministic render is missing'
now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
first = wc.render_verification(card, 'verified', 'claude', 'self', 'a' * 64, None, now)
second = wc.render_verification(card, 'verified', 'claude', 'self', 'a' * 64, None, now)
assert first == second and first is not None
assert '2026-01-02T03:04:05.000000Z' in first
assert card.read_text(encoding='utf8') == original
`;
    const run = spawnSync('python3', ['-B', '-c', code], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
      env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1' },
    });
    assert.equal(run.status, 0, run.stderr || run.stdout);
  } finally { removeTemp(home); }
});

test('verdict and compile result digests bind their inputs with separate domains', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-digest-home-')));
  try {
    const code = `
import re, sys
sys.path.insert(0, '.')
import memvane.store.blob.blob_values as verify
assert hasattr(verify, 'provenance_digests'), 'provenance digest contract missing'
verdict = {'verdict': 'pass', 'claims': [{'claim': 'one', 'supported': True}], 'reasons': []}
fingerprint = {'worktree_id': '1' * 32, 'branch_key': 'refs/heads/main', 'path': 'notes/x',
               'source_sha': '2' * 64, 'byte_length': 3, 'head_oid': '3' * 40,
               'tracked_state': 'tracked', 'binding_state': 'observed', 'scan_id': '4' * 32, 'observed_ns': 5}
fingerprints = {'schema': 'memvane-compile-source-fingerprints-v1', 'viewId': 'b' * 64,
                'checkoutDigest': '5' * 64, 'gitObjectFormat': 'sha1', 'scanId': '4' * 32,
                'observedNs': 5, 'fingerprints': [fingerprint]}
result = {'cardSha': 'a' * 64, 'viewId': 'b' * 64, 'sourceFingerprints': fingerprints,
          'targetPath': 'concepts/a.md', 'expectedTargetHash': 'd' * 64,
          'sourceSpans': [], 'verifiedMode': 'self', 'verifyEngine': 'claude', 'producingEngine': 'codex'}
one = verify.provenance_digests(verdict, result)
assert verify.verification_verdict_digest(verdict) == one[0]
assert verify.compile_result_digest(result, one[0]) == one[1]
reordered = verify.provenance_digests(dict(reversed(list(verdict.items()))), dict(reversed(list(result.items()))))
assert one == reordered
assert all(re.fullmatch('[0-9a-f]{64}', value) for value in one)
assert one[0] != one[1]
assert verify.provenance_digests({**verdict, 'verdict': 'fail'}, result) != one
changed = verify.provenance_digests(verdict, {**result, 'cardSha': 'c' * 64})
assert changed[0] == one[0] and changed[1] != one[1]
assert verify.provenance_digests(verdict, {**result, 'targetPath': 'concepts/b.md'})[1] != one[1]
assert verify.provenance_digests(verdict, {**result, 'expectedTargetHash': 'e' * 64})[1] != one[1]
changed_fingerprints = {**fingerprints, 'fingerprints': [{**fingerprint, 'source_sha': '6' * 64}]}
assert verify.provenance_digests(verdict, {**result, 'sourceFingerprints': changed_fingerprints})[1] != one[1]
assert verify.provenance_digests({**verdict, 'claims': [float('nan')]}, result) is None
assert verify.verification_verdict_digest('pass') is None
assert verify.compile_result_digest({'cardSha': 'a' * 64}, one[0]) is None
assert verify.compile_result_digest({**result, 'cardSha': 'invalid'}, one[0]) is None
assert verify.compile_result_digest({**result, 'sourceFingerprints': {**fingerprints, 'fingerprints': [{}]}}, one[0]) is None
assert verify.compile_result_digest({**result, 'sourceSpans': [{'kind': 'bytes', 'start': 0, 'end': 2}]}, one[0]) is None
`;
    const run = spawnSync('python3', ['-B', '-c', code], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
      env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1' },
    });
    assert.equal(run.status, 0, run.stderr || run.stdout);
  } finally { removeTemp(home); }
});
