import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('opt-in existing Laya runtime: actual synthetic smoke and trainer protocol',
  {skip: process.env.QMD_LAYA_LIVE !== '1'}, () => {
    const raw = execFileSync('python3',
      ['test/fixtures/context-learning-real-laya-smoke.py'], {
        env: {...process.env, PYTHONPATH: '.'}, encoding: 'utf8', timeout: 180000,
      });
    const report = JSON.parse(raw);
    assert.equal(report.external_calls, 0);
    assert.equal(report.runtime_selected, 'explicit_reuse');
    assert.ok(report.body_bytes > 600);
    assert.ok(report.head_bytes > 0);
    assert.ok(Object.values(report.synthetic_proof).every(v => v === true));
  });
