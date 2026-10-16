import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync,writeFileSync,readFileSync,existsSync,truncateSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

function fixture(run) {
 const dir=mkdtempSync(join(tmpdir(),'qmd-endpoint-shell-'));
 const log=join(dir,'daemon.log'), calls=join(dir,'calls');
 writeFileSync(log,'synthetic daemon log\n');
 writeFileSync(join(dir,'foreign.pid'),'999999\n');
 const shim=join(dir,'shell-env');
 writeFileSync(shim,`python3() {
 case "$*" in
  *qmd_endpoint.py' port') echo 19485;;
  *qmd_endpoint.py' url') echo 'http://[::1]:19485';;
  *qmd_endpoint.py' log') printf '%s\\n' "$TEST_LOG";;
  *qmd_endpoint.py' reload') echo reload >> "$TEST_CALLS"; return "$TEST_RELOAD_RC";;
  *runtime_admission.py' warm') echo warm-deferred >> "$TEST_CALLS"; return 75;;
  *) return 99;;
 esac
}
kill() { echo "UNSAFE_SIGNAL $*" >> "$TEST_CALLS"; return 0; }
curl() { printf '%s\\n' "$*" >> "$TEST_CALLS"; return 0; }
`);
 const env={...process.env,BASH_ENV:shim,TMPDIR:dir,TEST_LOG:log,TEST_CALLS:calls,
  TEST_RELOAD_RC:'0',QMD_SETUP_GUARD_FIXTURE:'',QMD_BACKEND_MANAGER:'',
  QMD_DAEMON_PID:join(dir,'foreign.pid'),QMD_RECALL_LOG:'',QMD_KEEPALIVE_VEC_WARM:'1'};
 try {run({dir,log,calls,env});} finally {rmSync(dir,{recursive:true,force:true});}
}

test('production log rotation never signals an unverified PID and restores log on proved reload failure',()=>fixture(({log,calls,env})=>{
 truncateSync(log,11*1024*1024);env.TEST_RELOAD_RC='1';
 const p=spawnSync('bash',['backend/logrotate.sh'],{encoding:'utf8',env});
 assert.equal(p.status,0,p.stderr);
 assert.equal(readFileSync(calls,'utf8'),'reload\n');
 assert.ok(existsSync(log));assert.equal(existsSync(log+'.1'),false);
}));

test('production log rotation calls the proved endpoint coordinator without manager environment',()=>fixture(({log,calls,env})=>{
 truncateSync(log,11*1024*1024);
 const p=spawnSync('bash',['backend/logrotate.sh'],{encoding:'utf8',env});
 assert.equal(p.status,0,p.stderr);
 assert.equal(readFileSync(calls,'utf8'),'reload\n');assert.ok(existsSync(log+'.1'));
}));

test('production keepalive pins health address family and defers unbounded warm queries',()=>fixture(({calls,env})=>{
 const p=spawnSync('bash',['backend/keepalive.sh'],{encoding:'utf8',env});
 assert.equal(p.status,0,p.stderr);
 const lines=readFileSync(calls,'utf8').trim().split('\n');
 assert.equal(lines.length,2);assert.match(lines[0],/http:\/\/\[::1\]:19485\/health/);
 assert.equal(lines[1],'warm-deferred');
 assert.ok(lines[0].includes('--noproxy *')&&!lines[0].includes('localhost'));
}));

test('daemon directly executes the verified MCP entry at default8584 or the coordinator-selected explicit port',()=>{
 const dir=mkdtempSync(join(tmpdir(),'qmd-daemon-port-'));
 try {
  const entry=join(dir,'entry.mjs'),node=join(dir,'node'),bin=join(dir,'qmd'),args=join(dir,'args'),shim=join(dir,'env');
  writeFileSync(entry,'// synthetic entry\n');writeFileSync(bin,'#!/bin/sh\nexit 99\n',{mode:0o700});
  writeFileSync(node,'#!/bin/sh\n[ "$1" = "-" ] && exit 0\nprintf "%s\\n" "$@" > "$TEST_ARGS"\n',{mode:0o700});
  writeFileSync(shim,`python3() {
 case "$*" in
  *'qmd_route.py resolve-bin') printf '%s\\n' "$TEST_BIN";;
  *'qmd_route.py daemon-spec') printf '%s\\037%s\\037%s\\n' "$TEST_ENTRY" "$TEST_NODE" "$TEST_BIN";;
  *) return 0;;
 esac
}
`);
  for (const port of [undefined,'19486']) {
   const env={...process.env,HOME:dir,BASH_ENV:shim,QMD_SETUP_GUARD_FIXTURE:'1',QMD_BIN:bin,
    QMD_RECALL_LOG:'',TEST_ENTRY:entry,TEST_NODE:node,TEST_BIN:bin,TEST_ARGS:args};
   if(port===undefined)delete env.QMD_DAEMON_PORT;else env.QMD_DAEMON_PORT=port;
   const p=spawnSync('bash',['backend/daemon.sh'],{encoding:'utf8',env,timeout:5000});
   assert.equal(p.status,0,p.stderr);
   assert.deepEqual(readFileSync(args,'utf8').trim().split('\n'),[entry,'mcp','--http','--port',port??'8584']);
  }
 } finally {rmSync(dir,{recursive:true,force:true});}
});
