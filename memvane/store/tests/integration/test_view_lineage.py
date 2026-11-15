"""Same-view old snapshots, registry bounds and bounded verifier refusals."""
import json
import os
import sqlite3
import subprocess
import time
import unittest
from unittest import mock

from memvane.store.tests.support import GIT_ENV, GenerationCase, drop_triggers_and


class LineageChecks(GenerationCase):
    def output(self,root,*args):
        run=subprocess.run(['git','-c','commit.gpgsign=false','-c','core.hooksPath='+os.devnull,*args],
            cwd=root,env={**os.environ,**GIT_ENV,
                         'GIT_CEILING_DIRECTORIES':str(self.base)},
            stdin=subprocess.DEVNULL,text=True,capture_output=True,timeout=30)
        self.assertEqual(run.returncode,0,run.stderr)
        return run.stdout.strip()

    def git_seed(self,name,commit=True):
        root=self.project(name);self.git(root,'init','-q')
        if commit:
            self.git(root,'add','-A');self.git(root,'commit','-q','-m','fixture')
        store,_,h=self.seeded(root)
        gen,base=self.prepared(store,h,self.snapshot(store,h,root=root))
        store.publish_generation(h,gen,expected_head=base)
        return root,store,h,gen

    def test_01_forward_head_keeps_same_view_last_snapshot(self):
        root,store,old,gen=self.git_seed('forward')
        pin=store.pin_view(old)
        (root/'notes/next.md').write_text('synthetic next commit\n')
        self.git(root,'add','-A');self.git(root,'commit','-q','-m','next')
        self.refused(store.search,pin,'wiki',1)
        fresh=store.capture_view(old.worktree_id)
        self.assertEqual(fresh.view_id,old.view_id)
        if os.environ.get('MEMVANE_TEST_GIT_TIMING'):
            from memvane.store.checkout import checkout_view as checkout
            timings=[]
            original=checkout._git
            def timed(args,*pos,**kw):
                start=time.monotonic()
                try:return original(args,*pos,**kw)
                finally:timings.append((args[0],time.monotonic()-start))
            pin=store.pin_view(fresh)
            try:
                with mock.patch.object(checkout,'_git',side_effect=timed):
                    rows=store.search(pin,'wiki',1)
            finally:
                print(json.dumps({'case':'synthetic-search-git-timing',
                    'count':len(timings),'seconds':sum(item[1] for item in timings),
                    'byCommand':{name:sum(item[1] for item in timings if item[0]==name)
                                 for name in sorted({item[0] for item in timings})}}),flush=True)
        else:
            rows=store.search(store.pin_view(fresh),'wiki',1)
        self.assertEqual((rows[0]['gen_id'],rows[0]['snapshot_state']),(gen,'stale_snapshot'))
        self.assertIs(rows[0]['owner_verified'],False)

    def test_02_unrelated_head_is_lineage_changed_not_another_view(self):
        root,store,old,gen=self.git_seed('unrelated')
        independent=self.output(root,'commit-tree',old.head_oid+'^{tree}','-m','independent synthetic lineage')
        self.git(root,'update-ref',old.branch_key,independent,old.head_oid)
        fresh=store.capture_view(old.worktree_id)
        rows=store.search(store.pin_view(fresh),'wiki',1,deadline_seconds=10)
        self.assertEqual(fresh.view_id,old.view_id)
        self.assertEqual((rows[0]['gen_id'],rows[0]['snapshot_state']),(gen,'lineage_changed'))
        self.assertIs(rows[0]['owner_verified'],False)

    def test_03_unborn_snapshot_can_be_read_after_first_commit_without_lineage_claim(self):
        root,store,old,gen=self.git_seed('unborn',commit=False)
        self.assertIsNone(old.head_oid)
        self.git(root,'add','-A');self.git(root,'commit','-q','-m','first')
        fresh=store.capture_view(old.worktree_id)
        rows=store.search(store.pin_view(fresh),'wiki',1)
        self.assertEqual((rows[0]['gen_id'],rows[0]['snapshot_state']),(gen,'lineage_unverified'))

    def test_04_active_pin_survives_handle_eviction_and_pin_limit_recovers(self):
        from memvane.store.view import view_values as values
        from memvane.store.view import view_reader as reader
        store,_,h,_=self.published();pin=store.pin_view(h)
        with mock.patch.object(values,'MAX_HANDLES',2):
            for _ in range(3):store.capture_view(h.worktree_id)
        self.refused(store.head,h)
        self.assertEqual(len(store.search(pin,'hello',1)),1)
        fresh=store.capture_view(h.worktree_id)
        with mock.patch.object(reader,'MAX_PINS',2):
            second=store.pin_view(fresh)
            error=self.refused(store.pin_view,fresh)
            self.assertEqual(error.reason,'view_pin_limit')
            store.release(second)
            third=store.pin_view(fresh);store.release(third)
        store.release(pin)

    def test_05_malformed_orphan_stage_uses_bounded_verifier_error(self):
        store,_,h,gen=self.published();head=store.head(h);original=self.views._stage_put_batch
        def interrupted(*args,**kwargs):raise RuntimeError('synthetic interruption')
        changed=dict(scan_id='2'*32,body=b'# Beta\nhello changed snapshot\n')
        with mock.patch.object(self.views,'_stage_put_batch',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.prepared(store,h,self.snapshot(store,h,**changed))
        self.assertIs(self.views._stage_put_batch,original)
        late=store.prepare_generation(h,self.snapshot(store,h,**changed),expected_head=head)
        # A stage item whose metadata is not canonical JSON: a bounded store refusal, not a raw error.
        self.assertEqual(drop_triggers_and(self.db,'s_stage_items',(
            'UPDATE s_stage_items SET metadata_json=? WHERE job_id=?',('{"z":1,"a":1}',late[2:34]))),0)
        error=self.refused(store.publish_generation,h,late,expected_head=head)
        self.assertLessEqual(len(error.reason),160)
        self.assertEqual(store.head(h),head)
        self.assertEqual(self.refused(store.verify_views).reason,'row_format_unsupported')
        pin=store.pin_view(h)
        try:self.assertEqual([row['gen_id'] for row in store.search(pin,'hello',1)],[gen])
        finally:store.release(pin)

    def test_06_busy_reader_keeps_pin_and_recovers_after_lock_release(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        other=sqlite3.connect(self.db,timeout=.1)
        try:
            other.execute('BEGIN EXCLUSIVE')
            self.refused(store.search,pin,'hello',1)
        finally:other.rollback();other.close()
        self.assertEqual(len(store.search(pin,'hello',1)),1)


if __name__=='__main__':unittest.main()
