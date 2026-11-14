"""Generation collection must not follow symlinks outside its private root."""
import json
from pathlib import Path
from unittest import mock

from memvane.kernel import memvane_paths, safe_rmtree

from memvane.provider import embed_runtime as R
from memvane.provider.tests.support import Machine, write


class GenerationGc(Machine):
    def test_symlinked_generations_never_deletes_outside_the_runtime(self):
        self.provision()
        self.model_bytes += b'2'
        self.provision()
        self.model_bytes += b'3'
        self.provision()
        generations = R.root() / 'generations'
        outside = self.base / 'outside-generations'
        generations.rename(outside)
        generations.symlink_to(outside)
        (outside / '.staging-dead').mkdir()
        before = sorted(p.name for p in outside.iterdir())
        self.assertEqual(len(before), 4)
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_unsafe$'):
            R.collect(approve=True)
        with self.assertRaisesRegex(R.RuntimeUnavailable, '^laya_embed_runtime_unsafe$'):
            R.collect()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), before)

    def test_link_inside_a_removed_generation_is_not_followed(self):
        first = Path(self.provision()['generation'])
        for bump in (b'2', b'3'):
            self.model_bytes += bump
            self.provision()
        keep = self.base / 'keep-me'
        write(keep / 'data', 'outside')
        (first / 'escape').symlink_to(keep)
        done = R.collect(approve=True)
        self.assertEqual(done['changesApplied'], 1)
        self.assertFalse(first.exists())
        self.assertEqual((keep / 'data').read_text(), 'outside')

    def generations(self):
        return sorted(p.name for p in (R.root() / 'generations').iterdir())

    def test_gc_keeps_active_previous_and_attested(self):
        first = Path(self.provision()['generation']).name
        self.model_bytes += b'2'
        second = Path(self.provision()['generation']).name
        self.model_bytes += b'3'
        third = Path(self.provision()['generation']).name
        self.model_bytes += b'4'
        fourth = Path(self.provision()['generation']).name
        self.assertEqual(len({first, second, third, fourth}), 4)
        # An install attestation still names the first generation.
        install = memvane_paths.installs_base(self.home) / ('a' * 32)
        write(install / 'provider-attestation.json', json.dumps({
            'providerManifest': {'modelPath': str(R.root() / 'generations' / first /
                                                  'models' / R.MODEL_NAME)},
            'profileDependencyClosure': [{'path': str(R.root() / 'generations' / second /
                                                      'x.node')}]}))
        stale = R.root() / 'generations' / '.staging-dead'
        stale.mkdir()
        plan = R.collect()
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['changesApplied'], 0)
        self.assertEqual(len(self.generations()), 5)
        actions = {row['generation']: row for row in plan['generations']}
        self.assertEqual(actions[fourth]['reason'], 'active')
        self.assertEqual(actions[third]['reason'], 'previous')
        self.assertEqual(actions[first]['reason'], 'attested')
        # The historical profile row is data, not a live reference.
        self.assertEqual(actions[second]['action'], 'remove')
        done = R.collect(approve=True)
        self.assertEqual(done['changesApplied'], 2)
        self.assertEqual(self.generations(), sorted([first, third, fourth]))
        self.assertEqual(Path(R.select()['generation']).name, fourth)

    def three_generations(self):
        first = Path(self.provision()['generation'])
        for bump in (b'2', b'3'):
            self.model_bytes += bump
            self.provision()
        stale = R.root() / 'generations' / '.staging-dead'
        stale.mkdir()
        return first, stale

    def test_mount_point_inside_a_generation_is_refused_untouched(self):
        first, stale = self.three_generations()
        write(first / 'mnt' / 'volume-data', 'mounted')
        with mock.patch.object(safe_rmtree, '_mount_points',
                               return_value={str(first / 'mnt')}):
            done = R.collect(approve=True)
        self.assertEqual(done['status'], 'partial')
        self.assertEqual(done['failed'], [{'generation': first.name,
                                           'reason': 'laya_embed_runtime_mount_point'}])
        self.assertTrue((first / 'mnt' / 'volume-data').exists())
        self.assertTrue((first / 'runtime.json').exists())
        self.assertFalse(stale.exists())

    def test_unreadable_attestation_blocks_gc(self):
        self.provision()
        install = memvane_paths.installs_base(self.home) / ('b' * 32)
        write(install / 'provider-attestation.json', '{')
        self.assertEqual(R.collect(approve=True)['status'], 'blocked')

    def test_failed_generation_keeps_the_removed_ones_reported(self):
        first, stale = self.three_generations()
        locked = first / 'locked'
        write(locked / 'pinned', 'x')
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        done = R.collect(approve=True)
        self.assertEqual((done['status'], done['partial'], done['changesApplied']),
                         ('partial', True, 1))
        self.assertEqual(done['failed'], [{'generation': first.name,
                                           'reason': 'laya_embed_runtime_remove_failed'}])
        self.assertFalse(stale.exists())
