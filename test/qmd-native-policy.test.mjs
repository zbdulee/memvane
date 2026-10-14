import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync,existsSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

test('managed auto launcher always denies native builds/downloads while executing the real CLI contract',()=>{
 assert.ok(existsSync('core/qmd_native_policy.mjs'),'missing no-build automatic backend policy');
 const d=mkdtempSync(join(tmpdir(),'qmd-native-policy-'));
 try {
  mkdirSync(join(d,'dist/cli'),{recursive:true});
  writeFileSync(join(d,'package.json'),'{"type":"module"}');
  writeFileSync(join(d,'dist/llm.js'),`export class LlamaCpp {
   async ensureLlama(allowBuild=true) {if(allowBuild!==false)throw new Error('native build permitted');return {gpu:'metal'};}
  }`);
  writeFileSync(join(d,'dist/cli/qmd.js'),`import {LlamaCpp} from '../llm.js';import {realpathSync} from 'node:fs';
   if(process.env.QMD_FORCE_CPU||process.env.QMD_LLAMA_GPU)throw new Error('inherited override');
   if(realpathSync(process.argv[1])!==new URL(import.meta.url).pathname)throw new Error('CLI main contract');
   await new LlamaCpp().ensureLlama(true);console.log(JSON.stringify(process.argv.slice(2)));`);
  const launcher=readFileSync('core/qmd_native_policy.mjs','utf8').replace('__QMD_CLI_JSON__',JSON.stringify(join(d,'dist/cli/qmd.js')));
  writeFileSync(join(d,'qmd-native.mjs'),launcher);
  const p=spawnSync(process.execPath,[join(d,'qmd-native.mjs'),'embed','synthetic'],{
   encoding:'utf8',env:{...process.env,QMD_FORCE_CPU:'1',QMD_LLAMA_GPU:'false'}});
  assert.equal(p.status,0,p.stderr);assert.deepEqual(JSON.parse(p.stdout),['embed','synthetic']);
  const receipt=JSON.parse(p.stderr.trim());assert.equal(receipt.gpu,'metal');
  assert.equal(receipt.build,'never');assert.equal(receipt.skipDownload,true);
 } finally {rmSync(d,{recursive:true,force:true});}
});
