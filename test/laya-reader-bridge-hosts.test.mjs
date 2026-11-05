import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,realpathSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {removeTemp} from './helpers/temp.mjs';

test('missing bridge preflight is collected once and invalid observations defer',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-preflight-auto-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_missing_preflight_is_collected_and_existing_receipts_are_reused')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('current v4 worker collects bridge evidence before v5 conversion',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-preflight-worker-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_current_v4_bundle_still_converts_after_preflight')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('v4 upgrade driver defers failed observations and converts after preflight',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-preflight-driver-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_v4_upgrade_driver_requires_preflight_and_converts_before_fence')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('v1 restore retains the selected reader during route reversal',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-reader-restore-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_v1_restore_keeps_reader_during_route_reversal')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('v1 restore resumes every interrupted metadata and journal step',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-restore-resume-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_v1_restore_keeps_reader_during_route_reversal')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    for (const stage of ['fence','file-2','file-0','file-1','journal','file-4','ledger']) {
      const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
        env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
          MEMVANE_TEST_RESTORE_INTERRUPT:stage,PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
      assert.ifError(result.error);assert.equal(result.status,0,stage+'\n'+result.stdout+result.stderr);
      assert.match(result.stderr,/\nOK\b/);
    }
  } finally {removeTemp(home);}
});

test('v5 linked worktree reads the main selected endpoint',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-reader-worktree-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_v5_linked_worktree_uses_main_persisted_reader')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('v5 loaded QA probe proves the selected sentinel',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-reader-qa-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_loaded_qa_probe_proves_actual_v5_sentinel_without_writer')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});

test('bridge preflight does not require unused cache copies to be loaded',()=>{
  const home=realpathSync(mkdtempSync(join(tmpdir(),'laya-reader-old-cache-')));
  const source=`import importlib.util,unittest
s=importlib.util.spec_from_file_location('bridge_fixture','test/fixtures/laya-reader-upgrade-proof-checks.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([m.UpgradeProofChecks('test_preflight_does_not_treat_unused_old_cache_as_loaded_host')]))
raise SystemExit(not r.wasSuccessful())`;
  try {
    const result=spawnSync('python3',['-B','-c',source],{encoding:'utf8',timeout:60000,
      env:{...process.env,HOME:home,TMPDIR:home,QMD_SETUP_GUARD_FIXTURE:'1',
        PYTHONDONTWRITEBYTECODE:'1',QMD_RECALL_LOG:''}});
    assert.ifError(result.error);assert.equal(result.status,0,result.stdout+result.stderr);
    assert.match(result.stderr,/Ran 1 test\b/);assert.match(result.stderr,/\nOK\b/);
  } finally {removeTemp(home);}
});
