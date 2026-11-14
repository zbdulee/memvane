"""A calibration envelope is never an installed shared-policy field (private files only)."""
from pathlib import Path
import tempfile
import unittest


class Calibration(unittest.TestCase):
    def test_calibration_permission_is_not_an_installed_policy_field(self):
        from memvane.learning.context_learning import shared
        from memvane.learning.context_learning.local_cycle import _atomic_json
        keys=('goldModel','maxGoldCalls','batchMin','batchMax','targetExact','trainerArgv',
              'baseArtifact','maxRssMib','minAvailableMib','maxLoadPerCpu','safetySeconds','experimental')
        value={k:None for k in keys}
        # Any envelope: the key itself is refused, whatever the calibration says.
        value.update(schema=shared.SCHEMA,enabled=True,calibration={'schema':'synthetic-calibration'})
        with tempfile.TemporaryDirectory() as name:
            root=Path(name).resolve();root.chmod(0o700)
            _atomic_json(root/shared.POLICY_NAME,value)
            with self.assertRaisesRegex(ValueError,'invalid_shared_policy'):shared._policy(root)


if __name__ == '__main__':
    unittest.main()
