import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('opt-in existing Laya runtime: over-budget cards are scored from their leading prefix',
  {skip: process.env.QMD_LAYA_LIVE !== '1'}, () => {
    const raw = execFileSync(process.env.QMD_LAYA_PYTHON,
      ['-I', '-B', 'test/fixtures/context-learning-real-laya-pairing.py'], {
        env: {...process.env, HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1'},
        encoding: 'utf8', timeout: 180000,
      });
    const report = JSON.parse(raw.trim().split('\n').at(-1));
    assert.ok(report.long_card_tokens.every(n => n > report.max_len));
    assert.equal(report.strict_budget_refused, 2);
    assert.equal(report.pairs, 15);
    assert.equal(report.truncated_pairs, 2);
    assert.ok(report.max_pair_tokens <= report.max_len);
    assert.ok(Array.isArray(report.selected_ids) && report.selected_ids.length <= 3);
    assert.equal(report.deterministic, true);
    assert.deepEqual(report.probes, {'1024': [true], '8192': [true]});
    assert.ok(report.heavy_pairing_seconds < 1, `pairing 15x16KiB took ${report.heavy_pairing_seconds}s`);
  });
