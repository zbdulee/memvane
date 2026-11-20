"""Read-only, authority-preserving plans for an activated default enrollment."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import time

from memvane.learning import default_enrollment as enrollment, project_learning_setup as setup
from memvane.learning.context_learning import host_self, learning_profile as profile, project_scope as scope
from memvane.learning.context_learning import selector_heal as heal
from memvane.learning.context_learning.dataset import read_private_json
from memvane.learning.context_learning.inference_identity import file_sha
from memvane.learning.context_learning.store import canonical
from memvane.kernel.memvane_version import version_key

TARGETS = ('shared-policy.json', 'live-selector.json', scope.NAME, profile.INSTALL, profile.NAME)
PROFILE_DERIVED = frozenset(('runtimeApprovalSha256', 'policySha256', 'livePolicySha256', 'trainerArgv', 'codeSha256'))
BINDING_DERIVED = frozenset(('installations', 'codeSha256', 'policySha256', 'hostRegistry'))
KIND_OPERATIONS = {'default-local': scope.LOCAL_OPERATIONS,
                   'default-teacher': scope.DEFAULT_TEACHER_OPERATIONS}
# Exact tracked bytes of v1.0.15 (b9c27c09), not a path-based source claim.
LEGACY_RELEASE = {
    'version': '1.0.15',
    'codeSha256': '9f77daa2c1961039864af37ab654e5c57782ebfd51fa0642dce60fc4d4dfd4d0',
    'executableSha256': '8e73886d331221ecfbdbdc0729f2bb4a85961625c77347054ada093036100786',
    'adapterSha256': 'dd3949afe71aebaa68be44b34feb080883aa28d6f5933f01bce6a5b2e763964c',
    'claude': 'c278a602a0b0a38bbd0b0104dbb47eff15d93c5b081157f984104879359b147a',
    'codex': '9dccce92dc2ae555f31d2e89bd024de581baf84bd2fc7a40cf6a751247a613f7',
}


def raw(value):
    return (canonical(value)+'\n').encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def _envelope(value):
    if not isinstance(value, dict) or set(value) != {'acceptedSha256', 'proposal'}:
        raise ValueError('renewal_acceptance_invalid')
    row = value['proposal']
    if not isinstance(row, dict) or value['acceptedSha256'] != scope.proposal_sha256(row):
        raise ValueError('renewal_acceptance_invalid')
    return row


def _without(value, keys):
    return {key: item for key, item in value.items() if key not in keys}


def _authority(project, root, values):
    policy, live = values['shared-policy.json'], values['live-selector.json']
    grant, accepted = _envelope(values[scope.NAME]), _envelope(values[profile.NAME])
    install = values[profile.INSTALL]
    journal = read_private_json(setup._path(root/setup.NAME, private=True))
    # Both default kinds renew; the kind fixes the only operations the grant may carry.
    kind = enrollment.profile_kind(root, project, policy)
    if kind not in KIND_OPERATIONS:
        raise ValueError('renewal_default_required')
    enrollment.validate_preferences(profile._settings(project))
    original = journal.get('profileProposal')
    plan = journal['plan']
    cohort = {'schema': scope.COHORT_SCHEMA, 'id': plan['cohortId'],
              'notBeforeUnixMs': plan['notBeforeUnixMs'], 'minTrainCases': 5,
              'minQualityTrainCases': 100, 'minTrainFamilies': 5,
              'minValidationCases': 50, 'minValidationFamilies': 5, 'minEmptyValidation': 10}
    if (not isinstance(original, dict)
            or journal.get('written', {}).get(profile.NAME) != sha(raw({
                'acceptedSha256': profile.digest(original), 'proposal': original}))
            or _without(accepted, PROFILE_DERIVED) != _without(original, PROFILE_DERIVED)
            or install != {**values[profile.NAME], 'phase': 'activated'}
            or accepted.get('schema') != profile.SCHEMA
            or accepted.get('choice') != 'local-auto-apply'
            or accepted.get('localTrain') is not True or accepted.get('autoApply') is not True
            or accepted.get('projectRoot') != str(project) or accepted.get('namespaceRoot') != str(root)
            or accepted.get('runtimeApprovalSha256') != scope.proposal_sha256(grant)
            or accepted.get('policySha256') != sha(raw(policy))
            or accepted.get('livePolicySha256') != sha(raw(live))
            or accepted.get('trainerArgv') != policy.get('trainerArgv')
            or accepted.get('codeSha256') != grant.get('binding', {}).get('codeSha256')
            or accepted.get('modelIdentitySha256') != grant['binding'].get('modelIdentitySha256')
            or not profile._binding_matches(accepted, profile._settings(project))
            or grant.get('operations') != KIND_OPERATIONS[kind]
            or grant.get('projectRoot') != str(project) or grant.get('namespaceRoot') != str(root)
            or grant.get('scope') != scope.SCOPE or grant.get('schema') != scope.SCHEMA
            or set(grant) != {'schema', 'scope', 'projectRoot', 'namespaceRoot', 'operations', 'cohort', 'expiresAtUnixMs', 'binding'}
            or grant.get('cohort') != cohort
            or grant.get('expiresAtUnixMs') != plan['expiresAtUnixMs']
            or grant.get('cohort', {}).get('notBeforeUnixMs') != plan['notBeforeUnixMs']
            or grant['cohort'].get('id') != plan['cohortId']
            or time.time()*1000 >= min(grant['expiresAtUnixMs'], accepted['expiresAtUnixMs'])):
        raise ValueError('renewal_authority_changed')
    # The policy comparison in default_local_only normalizes only argv[1].
    if (set(live) != {'schema', 'trainerArgv', 'timeoutSeconds', 'maxRssMib'}
            or live['schema'] != 'qmd-live-selector-v1' or live['timeoutSeconds'] != 30
            or live['maxRssMib'] != 4096 or live['trainerArgv'] != policy['trainerArgv']):
        raise ValueError('renewal_policy_changed')
    runtime, model = heal.official_runtime()
    if (policy['trainerArgv'][0] != runtime or model != policy['trainerArgv'][2]
            or heal._approved_interpreter_hash(root, runtime) != file_sha(os.path.realpath(runtime))):
        raise ValueError('renewal_runtime_changed')
    return grant, accepted, journal


def _previous_bundle(row):
    if set(row) != {'host', 'root', 'version', 'manifestSha256', 'adapterSha256', 'codeSha256', 'executableSha256'}:
        raise ValueError('renewal_previous_bundle_invalid')
    host, root = row['host'], Path(row['root'])
    if host not in ('claude', 'codex') or not root.is_absolute() or version_key(row['version']) is None:
        raise ValueError('renewal_previous_bundle_invalid')
    if os.path.lexists(root):
        root = scope._path(root, directory=True)
        files, fingerprint = scope._code_snapshot(root, Path.home())
        manifest = root/('.'+host+'-plugin/plugin.json')
        if (scope.proposal_sha256(files) != row['codeSha256']
                or scope.proposal_sha256(fingerprint) != row['executableSha256']
                or file_sha(scope._path(manifest)) != row['manifestSha256']
                or files.get('learning/context_learning/adapter.py') != row['adapterSha256']):
            raise ValueError('renewal_previous_bundle_changed')


def _legacy(row, binding, projection):
    from memvane.learning.context_learning import adapter_binding
    release = LEGACY_RELEASE
    host = row['host']
    expected_root = Path.home()/('.'+host)/'plugins/cache'/adapter_binding.MARKETPLACE/'memvane'/row['version']
    source = projection['marketplace']['source']
    if (row['root'] != str(expected_root) or projection['marketplace']['id'] != adapter_binding.MARKETPLACE
            or not adapter_binding._official_source(source)
            or projection['scope'] != 'user' or projection['projectPath'] is not None
            or any(row.get(key) != release[key] for key in ('version', 'codeSha256', 'executableSha256', 'adapterSha256'))
            or row['manifestSha256'] != release[host] or binding['codeSha256'] != release['codeSha256']):
        raise ValueError('renewal_legacy_provenance_unproved')


def plan(project, root, values):
    """Return all five target values; authority fields must be identical."""
    grant, accepted, journal = _authority(project, root, values)
    binding = grant['binding']
    previous = binding.get('installations')
    if not isinstance(previous, list) or len(previous) != 1:
        raise ValueError('renewal_previous_bundle_invalid')
    old = previous[0]
    _previous_bundle(old)
    if binding.get('codeSha256') != old['codeSha256']:
        raise ValueError('renewal_previous_bundle_invalid')
    rows, projection = host_self.selection(project)
    if rows[0]['root'] != str(heal.running_adapter().parents[3]):
        raise ValueError('renewal_execution_changed')
    # Current source is proved independently of version and old cache presence.
    heal._official(heal.running_adapter(), Path.home(), None)
    policy = deepcopy(values['shared-policy.json'])
    policy['trainerArgv'][1] = str(heal.running_adapter())
    fresh = scope._binding(project, root, policy, rows, local_only=True, policy_disk=policy)
    current = fresh['installations'][0]
    before, after = version_key(old['version']), version_key(current['version'])
    if after < before:
        raise ValueError('renewal_owner_newer')
    legacy = host_self.is_legacy(binding.get('hostRegistry'))
    if legacy:
        if (journal['plan']['installations'] != previous
                or journal['profileProposal']['codeSha256'] != binding['codeSha256']):
            raise ValueError('renewal_legacy_provenance_unproved')
        _legacy(old, binding, projection)
    elif binding.get('hostRegistry') != projection:
        if current['host'] == old['host']:
            raise ValueError('renewal_source_changed')
        from memvane.learning.context_learning.adapter_binding import _official_source
        previous_projection = binding.get('hostRegistry', {})
        previous_market = previous_projection.get('marketplace', {})
        market = projection['marketplace']
        # Claude records the official repository as {github, repo} and Codex
        # as {git, url}: across hosts both must name it, not match as objects.
        if (previous_projection.get('schema') != host_self.SCHEMA
                or previous_market.get('id') != market['id']
                or not _official_source(previous_market.get('source'))
                or not _official_source(market['source'])
                or previous_projection.get('scope') != projection['scope']
                or previous_projection.get('projectPath') != projection['projectPath']):
            raise ValueError('renewal_source_changed')
    if _without(binding, BINDING_DERIVED) != _without(fresh, BINDING_DERIVED):
        raise ValueError('renewal_binding_changed')
    if after == before and not legacy:
        if current['host'] != old['host']:
            selected, old_projection = host_self.selection(project, host=old['host'])
            if (selected != [{'host': old['host'], 'root': old['root']}]
                    or old_projection != binding['hostRegistry']
                    or current['codeSha256'] != old['codeSha256']
                    or current['executableSha256'] != old['executableSha256']):
                raise ValueError('renewal_same_version_changed')
            return None
        if fresh != binding:
            raise ValueError('renewal_same_version_changed')
        return None
    if after == before and current['host'] != old['host']:
        raise ValueError('renewal_owner_newer')
    if after == before and (current['codeSha256'] != old['codeSha256']
                            or current['executableSha256'] != old['executableSha256']):
        raise ValueError('renewal_same_version_changed')
    new_grant = {**grant, 'binding': fresh}
    live = deepcopy(values['live-selector.json'])
    live['trainerArgv'][1] = policy['trainerArgv'][1]
    new_profile = {**accepted, 'runtimeApprovalSha256': scope.proposal_sha256(new_grant),
                   'policySha256': sha(raw(policy)), 'livePolicySha256': sha(raw(live)),
                   'trainerArgv': policy['trainerArgv'], 'codeSha256': fresh['codeSha256']}
    # Explicit non-expansion CAS: only binding and the enumerated derived
    # profile fields differ. Whole cohort, operations, choice and expiry stay.
    if (_without(grant, {'binding'}) != _without(new_grant, {'binding'})
            or _without(accepted, PROFILE_DERIVED) != _without(new_profile, PROFILE_DERIVED)):
        raise ValueError('renewal_authority_expansion')
    envelope = {'acceptedSha256': profile.digest(new_profile), 'proposal': new_profile}
    return {'shared-policy.json': policy, 'live-selector.json': live,
            scope.NAME: {'acceptedSha256': scope.proposal_sha256(new_grant), 'proposal': new_grant},
            profile.INSTALL: {**envelope, 'phase': 'activated'}, profile.NAME: envelope}
