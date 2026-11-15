"""Declarative teacher preflight report only; never launches CLI or authorizes it."""
HOST_CLI = {'claude': 'claude', 'codex': 'codex'}
CHECKS = ('tool_catalog_empty', 'execution_disabled', 'context_isolated',
          'startup_extensions_disabled', 'managed_hooks_inactive')
STATES = frozenset({'verified', 'unverified', 'failed'})


def preflight(host, *, checks=None):
    if not isinstance(host, str) or host not in HOST_CLI:
        raise ValueError('unsupported_teacher_host')
    checks = {} if checks is None else checks
    if not isinstance(checks, dict) or not set(checks) <= set(CHECKS):
        raise ValueError('unknown_capability_check')
    normalized = {}
    for name in CHECKS:
        row = checks.get(name, {'state': 'unverified', 'basis': 'not-observed'})
        if not isinstance(row, dict) or set(row) != {'state', 'basis'} or row['state'] not in STATES:
            raise ValueError('invalid_capability_check')
        if not isinstance(row['basis'], str) or not row['basis'].strip() or len(row['basis']) > 256:
            raise ValueError('invalid_capability_basis')
        normalized[name] = dict(row)
    missing = [name for name in CHECKS if normalized[name]['state'] != 'verified']
    return {'host': host, 'cli': HOST_CLI[host], 'fallback': None,
            'checks': normalized, 'missing': missing,
            'capabilities_complete': not missing,
            'provider_enabled': False, 'transport_implemented': True,
            'authorization': 'report-only-not-runtime-attestation',
            'transport_default': 'off-explicit-call-required',
            'transport_scope': 'context-minimization-with-mandatory-policy-retained'}
