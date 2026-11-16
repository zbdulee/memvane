"""Read-only reattestation eligibility and a bounded owner wake."""
from pathlib import Path


def offer(action, decision, proof, payload, *, allowed, successful):
    if not allowed or action not in ('recall', 'update', 'update-queued', 'index', 'file-change-event'):
        return False
    if action in ('update', 'update-queued') and payload.get('hook_event_name') != 'SessionStart':
        return False
    if action in ('index', 'file-change-event') and not successful:
        return False
    try:
        from memvane.diagnostics import api as diagnostics
        from memvane.kernel.memvane_version import plugin_version
        from memvane.provider.api import reattestation_differences
        state = Path(proof['journalPath']).parent
        version = plugin_version()
        attempts = [row for row in diagnostics.build_progress_events(state)
                    if row.get('event') == 'provider.reattest' and row.get('pluginVersion') == version
                    and row.get('outcome') in ('accepted', 'rejected')]
        if len(attempts) >= 2 and all(row['outcome'] == 'rejected' for row in attempts[-2:]):
            return False
        if reattestation_differences(state, proof['providerManifest']):
            return False
        from memvane.runtime.api import request
        result = request('laya-sync', decision['projectRoot'])
        if isinstance(result, dict) and result.get('status') in ('queued', 'busy', 'deferred_queue_capacity_retained'):
            return True
        return 'unavailable'
    except (OSError, ValueError, TypeError, KeyError):
        return False


def mismatch_response(action, host, *, pending=False, host_payload):
    if action == 'recall':
        return host_payload(host, '')
    if action in ('update', 'update-queued'):
        notice = ('[memvane] 임베딩 환경 확인을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.'
                  if pending == 'unavailable' else
                  '[memvane] 임베딩 환경을 확인하는 중입니다.' if pending else
                  '[memvane] 임베딩 환경이 바뀌었습니다. 기존 설치를 복원한 뒤 다시 온보딩해 주세요.')
        if host == 'hermes':
            return {'status': 'provider_identity_changed', 'notice': notice}
        return {'hookSpecificOutput': {'hookEventName': 'SessionStart',
                                       'additionalContext': notice}}
    if action == 'gate':
        from memvane.host.host_rules import gate_response
        return gate_response(host)
    return {}
