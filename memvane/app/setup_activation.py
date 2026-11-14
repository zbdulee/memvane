"""Wire the install finalize step to the app's sync dispatch check."""
from __future__ import annotations


def finalize_initial_build(*, project_root, state_root):
    """``install.api.finalize_initial_build`` with the ``laya-sync`` dispatch check as its port."""
    from memvane.app import runtime_dispatch
    from memvane.install import api as install
    return install.finalize_initial_build(
        project_root=project_root, state_root=state_root,
        resolve_sync_target=lambda project: runtime_dispatch.resolve('laya-sync', project))
