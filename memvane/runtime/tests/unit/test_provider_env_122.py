"""1.0.22 A-9 d: the provider adds only its listed keys to a child's checked environment.

The teacher isolation check (``learning/context_learning/teacher_isolation.check_env``)
runs on the environment before ``provider_options`` adds these keys.
"""
from memvane.runtime.resource_controls import CpuThreads
from memvane.runtime.runtime_admission import PROVIDER_ENV_KEYS, provider_options
from tests.support.case import FixtureCase

# The shape of a checked teacher environment: the base keys and Claude's fixed values.
CHECKED = {'HOME': '/home/fixture', 'PATH': '/usr/bin:/bin', 'TMPDIR': '/tmp/fixture', 'LANG': 'C',
           'USER': 'fixture', 'LOGNAME': 'fixture', 'QMD_SANDBOX': '1',
           'CLAUDE_CODE_EFFORT_LEVEL': 'low', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '1200',
           'CLAUDE_CODE_MAX_RETRIES': '0', 'CLAUDE_CODE_AUTO_CONNECT_IDE': '0'}


class ProviderEnvironment(FixtureCase):
    def test_only_the_listed_keys_are_added_and_nothing_checked_is_changed(self):
        env = provider_options(dict(CHECKED))['env']
        self.assertLessEqual(set(env) - set(CHECKED), PROVIDER_ENV_KEYS)
        self.assertEqual({key: env[key] for key in CHECKED}, CHECKED)
        self.assertEqual(PROVIDER_ENV_KEYS, CpuThreads.KEYS | {'QMD_PROVIDER_COORD_FD'})
        self.assertEqual(set(CpuThreads.environment({})), CpuThreads.KEYS)
