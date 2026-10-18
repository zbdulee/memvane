#!/usr/bin/env python3
"""Deterministic synthetic publication race; no QMD runtime or model access."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import published_view


def view(revision, **changes):
    row = {'owner': 'approved-project', 'incarnation': 'registration-1',
           'worktree': 'worktree-1', 'scopeRevision': 'settings-1',
           'storeGeneration': 'store-1', 'contentRevision': revision}
    row.update(changes)
    return row


class PublishedViewRace(unittest.TestCase):
    def test_late_worker_cannot_replace_newer_synthetic_pointer(self):
        with tempfile.TemporaryDirectory(prefix='qmd-published-view-') as directory:
            pointer = Path(directory) / 'active.json'
            pointer.write_text(json.dumps(view('base')))
            lock = threading.Lock()
            old_prepared = threading.Event()
            allow_old = threading.Event()
            outcomes = {}

            def publish(label, expected, prepared):
                with lock:
                    current = json.loads(pointer.read_text())
                    next_view = published_view.publication_decision(current, expected, prepared)
                    if next_view is None:
                        outcomes[label] = 'retry'
                        return
                    pointer.write_text(json.dumps(next_view))
                    outcomes[label] = 'published'

            def old_worker():
                old_prepared.set()  # A has finished its inactive synthetic view.
                if not allow_old.wait(5):
                    raise AssertionError('barrier timed out')
                publish('A', view('base'), view('old'))

            thread = threading.Thread(target=old_worker)
            thread.start()
            try:
                self.assertTrue(old_prepared.wait(5))
                publish('B', view('base'), view('new'))
            finally:
                allow_old.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, {'B': 'published', 'A': 'retry'})
            self.assertEqual(json.loads(pointer.read_text()), view('new'))

    def test_incarnation_scope_and_store_generation_are_fences(self):
        current = view('base')
        for changed in ({'incarnation': 'registration-2'},
                        {'scopeRevision': 'settings-2'},
                        {'storeGeneration': 'store-2'},
                        {'worktree': 'worktree-2'}, {'owner': 'other-project'}):
            self.assertIsNone(published_view.publication_decision(
                current, current, view('next', **changed)))
        self.assertIsNone(published_view.publication_decision(
            current, current, view('base')))
        self.assertIsNone(published_view.publication_decision(
            current, view('previous'), view('next')))

    def test_request_identity_detects_route_endpoint_and_settings_changes(self):
        route = {'selected': True, 'shared': True, 'generation': 'store-1',
                 'INDEX_PATH': '/private/store/index.sqlite',
                 'QMD_CONFIG_DIR': '/private/store/config',
                 'XDG_CACHE_HOME': '/private/store/cache'}
        endpoint = {'pid': 100, 'url': 'http://127.0.0.1:8584',
                    'node': '/private/node', 'entry': '/private/qmd.js',
                    'runtime': 'runtime-1', 'store': 'store-1'}
        original = published_view.request_identity(route, endpoint, '/project',
                                                   {'indexing': True})
        self.assertNotEqual(original, published_view.request_identity(
            {**route, 'generation': 'store-2'}, endpoint, '/project', {'indexing': True}))
        self.assertNotEqual(original, published_view.request_identity(
            route, {**endpoint, 'pid': 101}, '/project', {'indexing': True}))
        self.assertNotEqual(original, published_view.request_identity(
            route, endpoint, '/project', {'indexing': False}))
        self.assertNotEqual(original, published_view.request_identity(
            route, endpoint, '/other-project', {'indexing': True}))
        with self.assertRaises(ValueError):
            published_view.request_identity(route, None, '/project', {'indexing': True})


if __name__ == '__main__':
    unittest.main()
