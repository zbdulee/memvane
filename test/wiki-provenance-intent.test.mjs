import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const interpreter = spawnSync('python3', ['-B', '-c',
  'import sys; print(sys.executable); print(sys.version.split()[0])'], { encoding: 'utf8' });
const revision = spawnSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' });
assert.equal(interpreter.status, 0, interpreter.stderr);
assert.equal(revision.status, 0, revision.stderr);
console.log(`fixture-python=${interpreter.stdout.trim().replace('\n', ' ')} head=${revision.stdout.trim()}`);

function runFixture(caseName, className = 'ProvenanceIntentChecks') {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-provenance-home-')));
  try {
    const run = spawnSync('python3', ['-B', 'test/fixtures/wiki-provenance-intent-checks.py',
      `${className}.${caseName}`], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    assert.equal(run.status, 0, run.stderr || run.stdout);
  } finally { removeTemp(home); }
}

test('selected schema3 verification records the exact stamped card', () => {
  runFixture('test_verified_card_creates_intent_for_stamped_bytes');
});

test('actual verify process creates intent without owner under wiki lock', () => {
  runFixture('test_actual_verify_process_creates_intent_without_owner_under_wiki_lock');
});

test('intent acknowledgement preserves an existing result', () => {
  runFixture('test_existing_ack_is_never_overwritten', 'IntentAckChecks');
});

test('intent acknowledgement refuses a replaced source path', () => {
  runFixture('test_replaced_source_path_is_never_acknowledged', 'IntentAckChecks');
});

test('replay refuses a symlinked card before acknowledging intent', () => {
  runFixture('test_replay_rejects_symlinked_card_before_intent_ack', 'IntentAckChecks');
});

test('invalid digest input keeps verification and creates no intent', () => {
  runFixture('test_invalid_digest_input_keeps_existing_verify_and_creates_no_intent');
});

test('branch change skips intent but preserves the stamp', () => {
  runFixture('test_branch_change_skips_intent_without_blocking_stamp');
});

test('unavailable checkout skips intent but preserves the stamp', () => {
  runFixture('test_unavailable_checkout_keeps_verification_without_intent');
});

test('reader error skips intent but preserves the stamp', () => {
  runFixture('test_reader_error_keeps_verification_without_intent');
});

test('reader error before verify lock preserves the stamp', () => {
  runFixture('test_reader_error_before_verify_lock_preserves_stamp');
});

test('head advance on the same branch preserves provenance', () => {
  runFixture('test_head_advance_on_same_branch_keeps_provenance');
});

test('stamp failure supersedes its intent', () => {
  runFixture('test_stamp_failure_supersedes_the_intent');
});

test('SIGKILL after intent and before stamp leaves the intent waiting', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-provenance-crash-')));
  try {
    const run = spawnSync('python3', ['-B', 'test/fixtures/wiki-provenance-intent-checks.py',
      'ProvenanceIntentChecks.test_crash_before_stamp_leaves_only_waiting_intent'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, TMPDIR: home, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '',
        QMD_TEST_CRASH_MARKER: join(home, 'crash-marker.json') },
    });
    assert.equal(run.signal, 'SIGKILL', run.stderr || run.stdout);
    const marker = JSON.parse(readFileSync(join(home, 'crash-marker.json'), 'utf8'));
    const directory = join(marker.state, 'provenance-intents');
    const files = readdirSync(directory).filter(name => name.startsWith('compile-'));
    assert.equal(files.length, 1);
    assert.match(files[0], /^compile-[0-9a-f]{64}\.json$/);
    const intent = JSON.parse(readFileSync(join(directory, files[0]), 'utf8'));
    const actual = createHash('sha256').update(readFileSync(marker.target)).digest('hex');
    assert.notEqual(intent.cardSha, actual);
    assert.match(readFileSync(marker.target, 'utf8'), /^status: generated$/m);
  } finally { removeTemp(home); }
});

test('SIGKILL after pending stamp leaves the old live card and matching intent', () => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'qwiki-pending-crash-')));
  try {
    const run = spawnSync('python3', ['-B', 'test/fixtures/wiki-provenance-intent-checks.py',
      'ProvenanceIntentChecks.test_crash_after_stamp_before_publish'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 180000,
      env: { ...process.env, HOME: home, TMPDIR: home, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '',
        QMD_TEST_CRASH_MARKER: join(home, 'crash-marker.json') },
    });
    assert.equal(run.signal, 'SIGKILL', run.stderr || run.stdout);
    const marker = JSON.parse(readFileSync(join(home, 'crash-marker.json'), 'utf8'));
    const directory = join(marker.state, 'provenance-intents');
    const files = readdirSync(directory).filter(name => name.startsWith('compile-'));
    assert.equal(files.length, 1);
    const intent = JSON.parse(readFileSync(join(directory, files[0]), 'utf8'));
    const pending = readFileSync(marker.target);
    assert.equal(intent.cardSha, createHash('sha256').update(pending).digest('hex'));
    assert.match(pending.toString('utf8'), /^status: verified$/m);
    assert.equal(readFileSync(marker.live, 'utf8'), 'previous live card\n');
  } finally { removeTemp(home); }
});

for (const [name, method] of [
  ['stamped pending replay after branch change', 'test_stamped_pending_replay_after_branch_change_supersedes_intent'],
  ['live replay after branch change', 'test_live_replay_after_branch_change_supersedes_intent'],
  ['stale pending replay', 'test_stale_pending_replay_supersedes_intent'],
]) {
  test(name + ' supersedes its intent', () => runFixture(method));
}

test('same view pending replay keeps its intent', () => {
  runFixture('test_same_view_pending_replay_retains_intent');
});

test('same view live replay keeps its intent', () => {
  runFixture('test_same_view_live_replay_retains_intent');
});

test('selected checkout uses registered identity and detects a branch change', () => {
  runFixture('test_selected_checkout_tracks_full_registered_capture');
});

test('selected checkout rejects forged and stale authority', () => {
  runFixture('test_checkout_rejects_forged_and_stale_route_authority');
});

test('selected checkout refuses a route fence after issuance', () => {
  runFixture('test_checkout_refuses_route_fence_after_issuance');
});

test('reader selection change during capture is rejected', () => {
  runFixture('test_reader_selection_change_during_capture_is_rejected');
});

test('reader selection change during issuance is rejected', () => {
  runFixture('test_reader_selection_change_during_issuance_is_rejected');
});
