"""Local execution approval for the normal learning worker.

The only grant is the accepted single-project authorization
(`project_scope`). It binds the shared policy to the reviewed installed
adapter and names the allowed operations; it never grants permission to send
content (teacher consent is separate).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .store import canonical


def proposal_sha256(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def active(root, policy, adapter, *, reviewed_adapter_sha256):
    """Return only a current exact grant; absent or expired means no grant."""
    del reviewed_adapter_sha256  # The project grant pins installed adapters itself.
    from . import project_scope
    if not project_scope.present(root):
        return None
    value = project_scope.require(root, policy, 'local_train')
    approved = {Path(r['root']) / 'core/context_learning/laya_adapter.py'
                for r in value['binding']['installations']}
    if Path(adapter).resolve(strict=True) not in approved:
        raise ValueError('runtime_authorization_adapter_invalid')
    return value


def effective_argv(root, policy, adapter, *, reviewed_adapter_sha256):
    """Resolve only the reviewed host adapter while a grant is current."""
    if active(root, policy, adapter,
              reviewed_adapter_sha256=reviewed_adapter_sha256) is None:
        return policy['trainerArgv']
    return [policy['trainerArgv'][0], str(Path(adapter).resolve(strict=True)),
            policy['trainerArgv'][2]]


def require_operation(root, policy, operation, *, approval_sha256=None):
    """Recheck expiry/revocation at dispatch and irreversible boundaries."""
    from . import project_scope
    if not project_scope.present(root):
        raise ValueError('runtime_authorization_required')
    value = project_scope.require(root, policy, operation, fresh_inventory=True)
    if approval_sha256 is not None and proposal_sha256(value) != approval_sha256:
        raise ValueError('runtime_authorization_required')
    return value
