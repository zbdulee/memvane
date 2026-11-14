"""Synthetic row-format scale observation: growth, rollback, collection and recall.

S_SCALE_CARDS (default 50) cards are published into one view; the fixture then
changes one card and measures the store growth of that publication,
rollback and collection time, and a
fresh pin + search. It prints one JSON line and asserts only results, never
elapsed time.
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import unittest

from memvane.store.tests.integration import test_s_incremental_publish as publish
CARDS = int(os.environ.get('S_SCALE_CARDS', '50'))


def body(i, word):
    filler = ' '.join(f'w{(i * 7 + k) % 997}' for k in range(48))
    return f'# Card {i}\nunique{word}{i:05d} 본문 {filler}\n'.encode('utf8')


class ScaleChecks(publish.IncrementalPublicationChecks):
    def file_bytes(self):
        db = sqlite3.connect(str(self.db))
        try:
            pages, size, free = (db.execute(f'PRAGMA {name}').fetchone()[0]
                                 for name in ('page_count', 'page_size', 'freelist_count'))
        finally:
            db.close()
        return dict(file=self.db.stat().st_size, used=(pages - free) * size, free=free * size)

    def build(self):
        repository, worktree = self.bind()
        store = self.Store.create(str(self.db), project_root=str(self.root), repository=repository,
                                  worktree=worktree, scope_id=self.scope)
        self.track(store)
        handle = store.capture_view(worktree['worktreeId'])
        wiki = self.root / '.memvane/wiki'
        raw = (self.root / 'notes/src.md').read_bytes()
        fp = store.put_fingerprint(dict(worktree_id=handle.worktree_id, branch_key=handle.branch_key,
            path='notes/src.md', source_sha=hashlib.sha256(raw).hexdigest(), byte_length=len(raw),
            head_oid=handle.head_oid, tracked_state='unknown', binding_state='observed',
            scan_id=publish.seed.SCAN, observed_ns=1))
        started = time.monotonic()
        members = []
        for i in range(CARDS):
            path = f'cards/c{i:05d}.md'
            (wiki / path).write_bytes(body(i, 'base'))
            members.append(self.member(store, handle, path, fp))
        ingest = time.monotonic() - started
        manifest = dict(scan_id=publish.seed.SCAN, policy_digest=handle.policy_digest, revocation_epoch=0,
                        complete=True, sources=[dict(path='notes/src.md', fingerprint_id=fp, presence='present')],
                        members=members)
        started = time.monotonic()
        head = store.head(handle)
        head = store.publish_generation(handle, store.prepare_generation(handle, manifest, expected_head=head),
                                        expected_head=head)
        return store, repository, handle, fp, manifest, head, dict(ingest_s=ingest, first_publish_s=time.monotonic() - started)

    def member(self, store, handle, path, fp):
        blob = store.ingest_card(handle.worktree_id, path)
        trust = store.put_attestation(dict(blob_sha=blob, verification_kind='compiled',
            engine_identity='synthetic:codex', policy_digest=handle.policy_digest,
            receipt=dict(schema='memvane-card-evidence-v1', verdict='verified', result_digest='e' * 64,
                         prior_trust=None), sources=[dict(fingerprint_id=fp, spans=[dict(start=0, end=4)])]))
        return dict(path=path, blob_sha=blob, display_title='T ' + path, trust_digest=trust)

    def measure(self):
        store, repository, handle, fp, manifest, head, result = self.build()
        wiki = self.root / '.memvane/wiki'
        changed_path = 'cards/c00000.md'
        new_body = body(0, 'changed')
        (wiki / changed_path).write_bytes(new_body)
        # Start from a collected view.
        while any(store.collect_view_rows(handle, limit=4096).get(t) for t in ('stage', 's_cards')):
            pass
        before = self.file_bytes()
        members = [self.member(store, handle, changed_path, fp) if m['path'] == changed_path else m
                   for m in manifest['members']]
        after_ingest = self.file_bytes()
        changed = dict(manifest, members=members)
        started = time.monotonic()
        gen = store.prepare_generation(handle, changed, expected_head=head)
        prepared = time.monotonic()
        second = store.publish_generation(handle, gen, expected_head=head)
        result.update(prepare_s=prepared - started, publish_s=time.monotonic() - prepared,
                      card_bytes=len(new_body), before=before, after_ingest=after_ingest, after_publish=self.file_bytes())
        started = time.monotonic()
        current = store.rollback_generation(handle, expected_head=second)
        result['rollback_s'] = time.monotonic() - started
        expected = ('uniquebase00000', manifest)
        (wiki / changed_path).write_bytes(body(0, 'base'))
        started = time.monotonic()
        unchanged = store.prepare_generation(handle, expected[1], expected_head=current)
        current = store.publish_generation(handle, unchanged, expected_head=current)
        result['unchanged_republish_s'] = time.monotonic() - started
        started = time.monotonic()
        batches = 0
        while any(store.collect_view_rows(handle, limit=256).get(t)
                  for t in ('s_cards', 's_deltas', 's_generations', 'stage')):
            batches += 1
        result.update(collect_s=time.monotonic() - started, collect_batches=batches + 1,
                      after_collect=self.file_bytes())
        store.close()
        reader = self.reopen(repository)
        fresh = reader.capture_view(handle.worktree_id)
        started = time.monotonic()
        if os.environ.get('S_SCALE_PROFILE'):
            import cProfile, io, pstats
            profile = cProfile.Profile()
            found = profile.runcall(self.found, reader, fresh, expected[0])
            out = io.StringIO()
            pstats.Stats(profile, stream=out).sort_stats('cumulative').print_stats(25)
            print('S_PROFILE\n' + out.getvalue())
        else:
            found = self.found(reader, fresh, expected[0])
        result['fresh_pin_search_s'] = time.monotonic() - started
        self.assertEqual(found, [changed_path])
        reader.close()
        return result

    def test_scale_observation(self):
        out = dict(cards=CARDS, python=sys.version.split()[0], sqlite=sqlite3.sqlite_version,
                   rows=self.measure())
        print('S_SCALE ' + json.dumps(out, sort_keys=True))


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromName('test_scale_observation', ScaleChecks)


if __name__ == '__main__':
    unittest.main()
