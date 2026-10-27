"""Disposable Laya index: edited keeper, removed loser, unchanged raw source."""
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
from laya_index_a import LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_runtime import LayaIndexRuntime
from laya_index_sync import prepare_candidate_batch_off_hook, sync_full
from laya_index_vector import LayaVectorIndex

SCOPE = 'a' * 32


class Provider:
    profile = {'model_id': 'synthetic-precomputed-only',
               'embedding_fingerprint': 'f' * 64, 'dimension': 2}
    manifest = {'recipeId': 'qmd-regex-token-chunks-candidate-v1'}

    def __init__(self):
        self.calls = []

    def embed_document_chunks(self, body, path):
        self.calls.append(path)
        return [{'seq': 0, 'pos': 0,
                 'vector': [0.0, 1.0] if path == 'wiki/b.md' else [1.0, 0.0]}]


def members(runtime, generation):
    return [row[0] for row in runtime.index.db.execute(
        'SELECT path FROM membership WHERE gen=? AND eligible=1 ORDER BY path',
        (generation,))]


def exercise():
    with tempfile.TemporaryDirectory(prefix='laya-dedup-index-',
                                     dir=Path(__file__).resolve().parent) as temp:
        root = Path(temp)
        raw, wiki = root / 'raw', root / 'wiki'
        raw.mkdir()
        wiki.mkdir()
        (raw / 'source.md').write_text('# Raw\n\nUnchanged.\n')
        (wiki / 'a.md').write_text('# Keeper\n\nOriginal.\n')
        (wiki / 'b.md').write_text('# Loser\n\nDuplicate.\n')
        (wiki / 'c.md').write_text('# Other\n\nUnchanged.\n')
        db, queue = root / 'index.sqlite3', root / 'queue.sqlite3'
        with closing(LayaIndexA(db, create=True)) as index:
            incarnation = index.incarnation()
        ClaimQueue.create(queue, index_incarnation=incarnation).close()
        with LayaIndexRuntime(source_root={'raw': str(raw), 'wiki': str(wiki)},
                              scope=SCOPE, db_path=str(db), queue_path=str(queue),
                              db_incarnation=incarnation) as runtime:
            first_provider = Provider()
            first_batch = prepare_candidate_batch_off_hook(runtime, first_provider)
            assert sorted(first_provider.calls) == [
                'raw/source.md', 'wiki/a.md', 'wiki/b.md', 'wiki/c.md']
            first = sync_full(runtime, before_publish=lambda: None,
                              vector_batch=first_batch)
            assert first['state'] == 'indexed'
            (wiki / 'a.md').write_text('# Keeper\n\nOriginal. Merged fact.\n')
            (wiki / 'b.md').unlink()
            second_provider = Provider()
            second_batch = prepare_candidate_batch_off_hook(runtime, second_provider)
            assert second_provider.calls == ['wiki/a.md'], second_provider.calls
            second = sync_full(runtime, before_publish=lambda: None,
                               vector_batch=second_batch)
            assert second['state'] == 'indexed'
            assert members(runtime, second['generation']) == [
                'raw/source.md', 'wiki/a.md', 'wiki/c.md']
            assert 'wiki/b.md' in members(runtime, first['generation'])
            token, _ = runtime.index.pin(SCOPE)
            try:
                hits = LayaVectorIndex(runtime.index).search_exact(
                    token, [0.0, 1.0], model_id=Provider.profile['model_id'],
                    embedding_fingerprint=Provider.profile['embedding_fingerprint'],
                    dimension=2, limit=10)
                assert 'wiki/b.md' not in [hit['path'] for hit in hits]
            finally:
                runtime.index.release(token)
    print('Laya keeper edit, loser deletion and unchanged vector reuse passed')


if __name__ == '__main__':
    exercise()
