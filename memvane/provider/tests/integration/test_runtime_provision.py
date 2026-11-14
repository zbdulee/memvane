"""Runtime provisioning rejects unapproved or unpinned model downloads."""
from unittest.mock import patch

from memvane.provider import embed_runtime as runtime
from tests.support.case import FixtureCase


class Provisioning(FixtureCase):
    def test_download_requires_explicit_approval(self):
        called = []
        with self.assertRaisesRegex(runtime.RuntimeUnavailable,
                                    '^laya_embed_runtime_download_approval_required$'):
            runtime.provision_new(home=self.home, node=self.home / 'node',
                                  fetch=lambda *args: called.append(args))
        self.assertEqual(called, [])

    def test_model_bytes_must_match_the_pinned_sha(self):
        def fetch(_url, target):
            target.write_bytes(b'GGUF synthetic unapproved bytes')

        with patch.object(runtime, '_install_module', return_value=self.home / 'entry'):
            with self.assertRaisesRegex(runtime.RuntimeUnavailable,
                                        '^laya_embed_runtime_model_sha_mismatch$'):
                runtime.provision_new(home=self.home, approve_download=True,
                                      node=self.home / 'node', fetch=fetch)
        root = runtime.root(self.home)
        self.assertFalse((root / 'active.json').exists())
        self.assertEqual(list(root.glob('.provision-*')), [])
