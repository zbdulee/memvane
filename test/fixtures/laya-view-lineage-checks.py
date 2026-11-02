"""Same-view old snapshots, registry bounds and candidate verifier refusals."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('reader_fixture',HERE/'laya-view-reader-checks.py')
helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)


class LineageChecks(helpers.helpers.GenerationCase):
    published=helpers.ReaderChecks.published

    def output(self,root,*args):
        run=subprocess.run(['git','-c','commit.gpgsign=false','-c','core.hooksPath='+os.devnull,*args],
            cwd=root,env={**os.environ,**helpers.helpers.helpers.GIT_ENV,
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
        rows=store.search(store.pin_view(fresh),'wiki',1)
        self.assertEqual((rows[0]['gen_id'],rows[0]['snapshot_state']),(gen,'stale_snapshot'))
        self.assertIs(rows[0]['owner_verified'],False)

    def test_02_unrelated_head_is_lineage_changed_not_another_view(self):
        root,store,old,gen=self.git_seed('unrelated')
        independent=self.output(root,'commit-tree',old.head_oid+'^{tree}','-m','independent synthetic lineage')
        self.git(root,'update-ref',old.branch_key,independent,old.head_oid)
        fresh=store.capture_view(old.worktree_id)
        rows=store.search(store.pin_view(fresh),'wiki',1)
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
        import laya_view_values as values
        import laya_view_reader as reader
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
        store,_,h,_=self.published();original=self.views._insert
        def insert(db,name,row):
            if name=='generation_seals':raise RuntimeError('synthetic interruption')
            return original(db,name,row)
        with mock.patch.object(self.views,'_insert',side_effect=insert):
            with self.assertRaises(RuntimeError):self.prepared(store,h,self.snapshot(store,h,scan_id='2'*32))
        with sqlite3.connect(self.db) as db:
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='generation_slots_update'").fetchone()[0]
            db.execute('DROP TRIGGER generation_slots_update')
            db.execute("UPDATE generation_slots SET stage_json='{}' WHERE ordinal=(SELECT MAX(ordinal) FROM generation_slots)")
            db.execute(trigger)
        self.refused(store.verify_views)

    def test_06_busy_reader_keeps_pin_and_recovers_after_lock_release(self):
        store,_,h,_=self.published();pin=store.pin_view(h)
        other=sqlite3.connect(self.db,timeout=.1)
        try:
            other.execute('BEGIN EXCLUSIVE')
            self.refused(store.search,pin,'hello',1)
        finally:other.rollback();other.close()
        self.assertEqual(len(store.search(pin,'hello',1)),1)


if __name__=='__main__':unittest.main()
