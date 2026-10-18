#!/usr/bin/env python3
"""Synthetic request-view pin checks; no QMD runtime, daemon, model or network."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import request_view_pin as pin_mod


def shared_route(**changes):
    row = {'selected': True, 'shared': True, 'generation': 'store-1',
           'INDEX_PATH': '/private/store/index.sqlite',
           'QMD_CONFIG_DIR': '/private/store/config',
           'XDG_CACHE_HOME': '/private/store/cache'}
    row.update(changes)
    return row


def local_route(**changes):
    row = {'selected': True, 'generation': 'local-1',
           'INDEX_PATH': '/project/.cache/index.sqlite',
           'QMD_CONFIG_DIR': '/project/.cache/config',
           'XDG_CACHE_HOME': '/project/.cache/xdg'}
    row.update(changes)
    return row


def endpoint(**changes):
    row = {'schema': 1, 'pid': 100, 'port': 8584, 'url': 'http://127.0.0.1:8584',
           'node': '/private/node', 'entry': '/private/qmd.js',
           'runtime': 'runtime-1', 'store': 'store-1'}
    row.update(changes)
    return row


CONFIG = {'collections': ['docs'], 'recallStrategy': 'hierarchical'}


class SharedPin(unittest.TestCase):
    def test_shared_pins_exact_endpoint_url(self):
        pin = pin_mod.capture(shared_route(), endpoint(), '/project', CONFIG)
        self.assertEqual(pin.route_kind, pin_mod.ROUTE_KIND_SHARED)
        self.assertEqual(pin.endpoint_url, 'http://127.0.0.1:8584')
        self.assertEqual(pin.index_paths()['INDEX_PATH'], '/private/store/index.sqlite')
        # Primary and raw backfill both resolve to the same URL.
        self.assertEqual(pin.require_url('http://127.0.0.1:8584'), pin.endpoint_url)
        self.assertEqual(pin.require_url('http://127.0.0.1:8584'), pin.endpoint_url)
        for other in ('http://127.0.0.1:8585', 'http://[::1]:8584',
                      'http://127.0.0.1:8584/', '', None, b'http://127.0.0.1:8584'):
            with self.assertRaises(ValueError):
                pin.require_url(other)
        with self.assertRaises(ValueError):
            pin.project_index()

    def test_shared_registry_revision_is_part_of_the_route_pin(self):
        route = shared_route(approvedProjects={'/project': 'settings-a'})
        pin = pin_mod.capture(route, endpoint(), '/project', CONFIG)
        self.assertTrue(pin_mod.same_view(pin, route, endpoint(), '/project', CONFIG))
        changed = shared_route(approvedProjects={'/project': 'settings-b'})
        self.assertFalse(pin_mod.same_view(pin, changed, endpoint(), '/project', CONFIG))

    def test_shared_requires_complete_endpoint(self):
        for bad in (None, {}, endpoint(url=''), endpoint(pid=0), endpoint(store=''),
                    'http://127.0.0.1:8584'):
            with self.assertRaises(ValueError):
                pin_mod.capture(shared_route(), bad, '/project', CONFIG)


class LocalPin(unittest.TestCase):
    def test_local_has_no_endpoint_and_fresh_index_copies(self):
        pin = pin_mod.capture(local_route(), None, '/project', CONFIG)
        self.assertEqual(pin.route_kind, pin_mod.ROUTE_KIND_PROJECT_LOCAL)
        self.assertIsNone(pin.endpoint_url)
        self.assertEqual(pin.index_paths()['INDEX_PATH'], '/project/.cache/index.sqlite')
        first = pin.project_index()
        self.assertEqual(first, {'INDEX_PATH': '/project/.cache/index.sqlite',
                                 'QMD_CONFIG_DIR': '/project/.cache/config',
                                 'XDG_CACHE_HOME': '/project/.cache/xdg'})
        first['INDEX_PATH'] = '/elsewhere/index.sqlite'
        self.assertEqual(pin.project_index()['INDEX_PATH'], '/project/.cache/index.sqlite')
        with self.assertRaises(ValueError):
            pin.require_url('http://127.0.0.1:8584')

    def test_local_rejects_any_endpoint(self):
        with self.assertRaises(ValueError):
            pin_mod.capture(local_route(), endpoint(), '/project', CONFIG)
        with self.assertRaises(ValueError):
            pin_mod.capture(local_route(shared=False), endpoint(), '/project', CONFIG)


class InputMutation(unittest.TestCase):
    def test_caller_mutation_does_not_change_pin(self):
        route, ep, config = shared_route(), endpoint(), {'collections': ['docs']}
        pin = pin_mod.capture(route, ep, '/project', config)
        snapshot = pin_mod.capture(shared_route(), endpoint(), '/project',
                                   {'collections': ['docs']})
        route['generation'] = 'store-2'
        route['INDEX_PATH'] = '/other/index.sqlite'
        ep['url'] = 'http://127.0.0.1:9999'
        ep['pid'] = 101
        config['collections'].append('raw')
        self.assertEqual(pin, snapshot)
        self.assertEqual(pin.endpoint_url, 'http://127.0.0.1:8584')
        self.assertEqual(pin.require_url('http://127.0.0.1:8584'), 'http://127.0.0.1:8584')
        # The mutated objects are now a different view.
        self.assertFalse(pin_mod.same_view(pin, route, ep, '/project', config))

    def test_local_route_mutation_does_not_change_pin(self):
        route = local_route()
        pin = pin_mod.capture(route, None, '/project', CONFIG)
        route['INDEX_PATH'] = '/elsewhere/index.sqlite'
        self.assertEqual(pin.project_index()['INDEX_PATH'], '/project/.cache/index.sqlite')

    def test_pin_fields_cannot_be_reassigned(self):
        pin = pin_mod.capture(local_route(), None, '/project', CONFIG)
        with self.assertRaises(AttributeError):
            pin.endpoint_url = 'http://127.0.0.1:8584'
        with self.assertRaises(AttributeError):
            pin.route_kind = pin_mod.ROUTE_KIND_SHARED

    def test_mutable_route_value_is_rejected(self):
        with self.assertRaises(ValueError):
            pin_mod.capture(local_route(generation=['local-1']), None, '/project', CONFIG)


class PhaseSwitching(unittest.TestCase):
    """Observations taken between primary/backfill and before output."""

    def setUp(self):
        self.pin = pin_mod.capture(shared_route(), endpoint(), '/project', CONFIG)

    def test_unchanged_observation_matches_every_phase(self):
        for _phase in ('primary', 'raw_backfill', 'pre_output'):
            self.assertTrue(pin_mod.same_view(
                self.pin, shared_route(), endpoint(), '/project', dict(CONFIG)))

    def test_route_switch_fails_closed(self):
        for route in (shared_route(generation='store-2'),
                      shared_route(INDEX_PATH='/private/store2/index.sqlite'),
                      shared_route(selected=False)):
            self.assertFalse(pin_mod.same_view(self.pin, route, endpoint(),
                                               '/project', CONFIG))
        # shared -> project-local between phases.
        self.assertFalse(pin_mod.same_view(self.pin, local_route(), None,
                                           '/project', CONFIG))

    def test_endpoint_switch_fails_closed(self):
        for ep in (endpoint(pid=101), endpoint(url='http://127.0.0.1:8585', port=8585),
                   endpoint(runtime='runtime-2'), endpoint(store='store-2'), None):
            self.assertFalse(pin_mod.same_view(self.pin, shared_route(), ep,
                                               '/project', CONFIG))

    def test_config_and_project_switch_fails_closed(self):
        self.assertFalse(pin_mod.same_view(self.pin, shared_route(), endpoint(),
                                           '/project', {**CONFIG, 'recallStrategy': 'flat'}))
        self.assertFalse(pin_mod.same_view(self.pin, shared_route(), endpoint(),
                                           '/other-project', CONFIG))

    def test_local_to_shared_switch_fails_closed(self):
        local = pin_mod.capture(local_route(), None, '/project', CONFIG)
        self.assertTrue(pin_mod.same_view(local, local_route(), None, '/project', CONFIG))
        self.assertFalse(pin_mod.same_view(local, shared_route(), endpoint(),
                                           '/project', CONFIG))
        self.assertFalse(pin_mod.same_view(local, local_route(XDG_CACHE_HOME='/x'),
                                           None, '/project', CONFIG))

    def test_backfill_after_switch_still_uses_captured_url(self):
        # The live endpoint moved, but a phase taking the pin keeps the old URL
        # and the pre-output check rejects the request.
        moved = endpoint(url='http://127.0.0.1:8590', port=8590, pid=200)
        self.assertEqual(self.pin.endpoint_url, 'http://127.0.0.1:8584')
        with self.assertRaises(ValueError):
            self.pin.require_url(moved['url'])
        self.assertFalse(pin_mod.same_view(self.pin, shared_route(), moved,
                                           '/project', CONFIG))


class InvalidIdentity(unittest.TestCase):
    def test_incomplete_or_unselected_routes_are_rejected(self):
        cases = [
            (None, None, '/project', CONFIG),
            ({}, None, '/project', CONFIG),
            (local_route(selected=False), None, '/project', CONFIG),
            (local_route(selected='true'), None, '/project', CONFIG),
            (local_route(INDEX_PATH=''), None, '/project', CONFIG),
            (local_route(QMD_CONFIG_DIR=None), None, '/project', CONFIG),
            (local_route(generation=''), None, '/project', CONFIG),
            (local_route(shared='yes'), None, '/project', CONFIG),
            (local_route(), None, '', CONFIG),
            (local_route(), None, None, CONFIG),
            (local_route(), None, '/project', None),
            (local_route(), None, '/project', {'bad': object()}),
            (shared_route(XDG_CACHE_HOME=''), endpoint(), '/project', CONFIG),
        ]
        for route, ep, root, config in cases:
            with self.assertRaises(ValueError):
                pin_mod.capture(route, ep, root, config)

    def test_same_view_fails_closed_on_invalid_inputs(self):
        pin = pin_mod.capture(local_route(), None, '/project', CONFIG)
        self.assertFalse(pin_mod.same_view(None, local_route(), None, '/project', CONFIG))
        self.assertFalse(pin_mod.same_view(pin.identity, local_route(), None,
                                           '/project', CONFIG))
        self.assertFalse(pin_mod.same_view(pin, None, None, '/project', CONFIG))
        self.assertFalse(pin_mod.same_view(pin, local_route(), None, '/project', None))
        self.assertFalse(pin_mod.same_view(pin, local_route(), None, '/project',
                                           {'bad': object()}))


if __name__ == '__main__':
    unittest.main()
