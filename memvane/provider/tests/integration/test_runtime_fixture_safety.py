"""A fixture invocation refuses to operate on the account HOME."""
from unittest.mock import patch

from memvane.provider import embed_runtime as runtime
from tests.support.case import FixtureCase


class FixtureSafety(FixtureCase):
    def test_fixture_never_touches_the_account_home(self):
        with patch.object(runtime, '_account_home', return_value=self.home):
            with self.assertRaisesRegex(runtime.RuntimeUnavailable,
                                        '^laya_embed_runtime_fixture_real_home$'):
                runtime.select(self.home)
        self.assertFalse((self.home / '.local').exists())
