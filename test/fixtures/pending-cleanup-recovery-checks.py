import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import wiki_verify_worker as verify
import wiki_selected_laya

class PendingRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        (self.root/'.auto-context/wiki/concepts').mkdir(parents=True)
        (self.root/'.auto-context/compile').mkdir(parents=True)
        self.queue=self.root/'.auto-context/compile/verify-queue.jsonl';self.queue.write_text('')
        self.target=self.root/'.auto-context/wiki/concepts/card.md';self.target.write_text('live')
        self.pending=self.target.with_name('.card.md.pending-'+'a'*16);self.pending.write_text('candidate')
        self.job={'targetPath':str(self.target.relative_to(self.root)),'pendingPath':str(self.pending.relative_to(self.root)),
                  'sourceHash':'a'*16,'expectedTargetHash':'b'*64,'ts':'2026-01-01T00:00:00Z'}
        self.cfg={'wikiPath':'.auto-context/wiki'};self.compile={'verify':{'enabled':True}}
        self.route=mock.patch.object(wiki_selected_laya,'selected',return_value=None);self.route.start();self.addCleanup(self.route.stop)
        self.preflight=mock.patch.object(verify,'preflight_block_reason',return_value=None);self.preflight.start();self.addCleanup(self.preflight.stop)
    def claim(self,name,content=''):
        p=self.queue.with_name(self.queue.name+'.claimed.'+str(os.getpid())+'.'+name);p.write_text(content);return p
    def test_empty_claim_late_append_is_not_lost(self):
        claim=self.claim('empty');raw=json.dumps(self.job)
        def read(path):
            self.assertEqual(path,claim);claim.write_text(raw+'\n');return []
        with mock.patch.object(verify.wcw,'claim_queue',return_value=claim), mock.patch.object(verify.wcw,'read_queue',side_effect=read):
            verify.run(self.root,self.cfg,self.compile)
        self.assertTrue(self.pending.exists())
        self.assertTrue(raw in self.queue.read_text() or (claim.exists() and raw in claim.read_text()))
    def test_unreadable_claim_is_not_unlinked(self):
        claim=self.claim('unreadable',json.dumps(self.job)+'\n');real=Path.read_text
        def read(path,*args,**kwargs):
            if path == claim:raise PermissionError('synthetic')
            return real(path,*args,**kwargs)
        with mock.patch.object(verify.wcw,'claim_queue',return_value=claim), mock.patch.object(verify.wcw,'read_queue',return_value=[]), mock.patch.object(Path,'read_text',read):
            verify.run(self.root,self.cfg,self.compile)
        self.assertTrue(claim.exists())
    def test_recovered_old_job_cannot_override_saved_fingerprint(self):
        st=self.pending.stat();saved=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
        self.pending.write_text('candidate with user edits')
        retry={**self.job,'_pendingCleanup':{'fingerprint':saved,'jobKey':verify._cleanup_job_key(self.job)}}
        recovered={**self.job,'_requeue_count':1}
        rows=[(json.dumps(retry),retry),(json.dumps(recovered),recovered)]
        snapshots,_=verify._pending_snapshots(self.root,self.cfg,rows)
        self.assertEqual(snapshots[self.pending],tuple(saved))
    def test_recovered_job_does_not_reverify_an_edited_cleanup_candidate(self):
        st=self.pending.stat();saved=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
        self.pending.write_text('candidate with user edits')
        retry={**self.job,'_pendingCleanup':{'fingerprint':saved,'jobKey':verify._cleanup_job_key(self.job)}}
        recovered={**self.job,'_requeue_count':1}
        self.queue.write_text(json.dumps(retry)+'\n'+json.dumps(recovered)+'\n')
        with mock.patch.object(verify,'process_verify_job',side_effect=AssertionError('must not reverify')):
            verify.run(self.root,self.cfg,self.compile)
        self.assertEqual(self.pending.read_text(),'candidate with user edits')

    def test_conflicting_cleanup_fingerprints_do_not_authorize_deletion(self):
        st=self.pending.stat();saved=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
        earlier={**self.job,'_pendingCleanup':{'fingerprint':saved,'jobKey':verify._cleanup_job_key(self.job)}}
        self.pending.write_text('candidate with user edits')
        st=self.pending.stat();later={**self.job,'_pendingCleanup':{'fingerprint':[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns],'jobKey':verify._cleanup_job_key(self.job)}}
        rows=[(json.dumps(earlier),earlier),(json.dumps(later),later)]
        snapshots,_=verify._pending_snapshots(self.root,self.cfg,rows)
        self.assertNotIn(self.pending,snapshots)
        recovered={**self.job,'_requeue_count':1}
        self.queue.write_text(json.dumps(earlier)+'\n'+json.dumps(later)+'\n'+json.dumps(recovered)+'\n')
        with mock.patch.object(verify,'process_verify_job',side_effect=AssertionError('must not reverify')):
            verify.run(self.root,self.cfg,self.compile)
        self.assertEqual(self.pending.read_text(),'candidate with user edits')

    def test_unprocessed_deferred_job_never_gets_a_cleanup_receipt(self):
        other=self.target.with_name('other.md');other.write_text('live')
        next_pending=other.with_name('.other.md.pending-'+'b'*16);next_pending.write_text('candidate')
        later={**self.job,'targetPath':str(other.relative_to(self.root)),
               'pendingPath':str(next_pending.relative_to(self.root)),'sourceHash':'b'*16}
        self.queue.write_text(json.dumps(self.job)+'\n'+json.dumps(later)+'\n{malformed\n')
        with mock.patch.object(verify.wcw,'budget_of',return_value={'verifyPerRun':1}), \
             mock.patch.object(verify,'process_verify_job',return_value=(True,False)):
            verify.run(self.root,self.cfg,self.compile)
        rows=[]
        for line in self.queue.read_text().splitlines():
            try: rows.append(json.loads(line))
            except ValueError: pass
        self.assertTrue(any(r.get('pendingPath') == later['pendingPath'] and '_pendingCleanup' not in r for r in rows))
        self.assertFalse(any(r.get('pendingPath') == later['pendingPath'] and '_pendingCleanup' in r for r in rows))
        self.assertTrue(next_pending.exists())

    def test_malformed_cleanup_marker_cannot_suppress_recovered_job(self):
        bad={**self.job,'_pendingCleanup':{}}
        recovered={**self.job,'_requeue_count':1}
        self.queue.write_text(json.dumps(bad)+'\n'+json.dumps(recovered)+'\n')
        with mock.patch.object(verify,'process_verify_job',return_value=(False,True)) as call:
            verify.run(self.root,self.cfg,self.compile)
        self.assertEqual(call.call_count,1)
        self.assertTrue(self.pending.exists())

    def test_cleanup_receipt_cannot_suppress_a_different_recovered_job(self):
        st=self.pending.stat();saved=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns]
        retry={**self.job,'_pendingCleanup':{'fingerprint':saved,'jobKey':verify._cleanup_job_key(self.job)}}
        other={**self.job,'ts':'2026-01-02T00:00:00Z','_requeue_count':1}
        self.queue.write_text(json.dumps(retry)+'\n'+json.dumps(other)+'\n')
        with mock.patch.object(verify,'process_verify_job',return_value=(False,True)) as call:
            verify.run(self.root,self.cfg,self.compile)
        self.assertEqual(call.call_count,1)
        self.assertTrue(self.pending.exists())

    def test_two_cleanup_claims_do_not_both_ack_a_shared_candidate(self):
        row=json.dumps(self.job);a=self.claim('a',row+'\n');b=self.claim('b',row+'\n')
        snapshots,_=verify._pending_snapshots(self.root,self.cfg,[(row,self.job)])
        self.assertIs(verify._cleanup_pending(self.root,self.queue,a,snapshots,[(row,self.job)]),False)
        self.assertIs(verify._cleanup_pending(self.root,self.queue,b,snapshots,[(row,self.job)]),False)
        self.assertTrue(self.pending.exists())

if __name__=='__main__':unittest.main(verbosity=2)
