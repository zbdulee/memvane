#!/usr/bin/env python3
"""Opt-in bridge from the existing host adapters to sandbox topical v2 cards.

No backend runs unless the caller explicitly enables execution. This module does
not edit project wiki cards, register hooks, or promote a card to ``verified``.
"""
from __future__ import annotations

from runtime_admission import provider_call

import copy
import fcntl
import hashlib
import json
import os
import re
import resource
import stat
import sys
import tempfile
import time
from pathlib import Path

import wiki_compile_worker as worker
import wiki_topical as topical
import wiki_topical_experiment as experiment
import wiki_verify_worker as verifier

ATTESTATION_SCHEMA = "qmd-topical-attestation-v1"
VERIFY_TASK = "verify_topical_claims_sandbox_only"
BUNDLE_VERIFY_TASK = "verify_topical_bundle_sandbox_only"
ATTEMPT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
ATTEMPT_SCHEMA = "qmd-topical-backend-attempt-v1"
RC17_GENERATION = '352eac5670c6655c56cc2d26'
RC17_TRANSPORT = '606394136a35811305b9978683ef422ca28884071cb445e431a9284182bcdd8d'
RC17_ADAPTER = '762a9d37c8c32e0572291f737da8baef2bf4db0fa75c193976a66c644cdb4413'
RC17_LIBRARY = 'abe70feea257d119ec1fb958df993115937b77811906088f0d639cc5aff30fc1'
RC17_GENERATION_REQUEST = '4c62e7af72a487659088bd3cfaaf497b6e047a2c424eb69840b72ccaae7ff8f4'
RC17_GENERATION_RESPONSE = '7ff72335d722e1e5eaba020c44c312015f5bcd5891958d5ffc3b77098247efc8'


def transport_fingerprint(root: Path, argv: list[str]) -> str:
    """Pin bundled adapter bytes across plugin cache version path changes."""
    import topical_story_scope
    if not topical_story_scope.exact_story(root):
        return topical.digest(topical.encoded(argv))
    if (len(argv) != 2 or Path(argv[1]).name != 'codex_adapter.py'
            or not Path(argv[1]).is_file()
            or not Path(argv[1]).with_name('lib.py').is_file()):
        raise topical.TopicalError('topical_story_transport_unreviewed')
    adapter = hashlib.sha256(Path(argv[1]).read_bytes()).hexdigest()
    library = hashlib.sha256(Path(argv[1]).with_name('lib.py').read_bytes()).hexdigest()
    return topical.digest(topical.encoded([
        'qmd-topical-adapter-content-v1', argv[0], 'codex', adapter, library]))


def accepted_transport_fingerprints(root: Path, generation_id: str,
                                    argv: list[str]) -> set[str]:
    """Accept the current adapter and one byte-identical completed rc17 batch."""
    accepted = {transport_fingerprint(root, argv)}
    if generation_id == RC17_GENERATION and len(argv) == 2:
        adapter = Path(argv[1])
        if (adapter.name == 'codex_adapter.py'
                and adapter.is_file() and adapter.with_name('lib.py').is_file()
                and hashlib.sha256(adapter.read_bytes()).hexdigest() == RC17_ADAPTER
                and hashlib.sha256(adapter.with_name('lib.py').read_bytes()).hexdigest() == RC17_LIBRARY):
            accepted.add(RC17_TRANSPORT)
    return accepted


def _audit_fd(root: Path) -> int:
    """Open the private audit directory without following a substituted link."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir("topical-backend-audit", 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        fd = os.open("topical-backend-audit", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=root_fd)
    finally:
        os.close(root_fd)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise topical.TopicalError("unsafe_backend_audit_directory")
    return fd


def _read_audit(fd: int, name: str) -> dict:
    item_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(item_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 2_000_000):
            raise topical.TopicalError("unsafe_backend_attempt")
        value = json.loads(os.read(item_fd, info.st_size + 1))
    finally:
        os.close(item_fd)
    if not isinstance(value, dict):
        raise topical.TopicalError("invalid_backend_attempt")
    return value


def _write_audit(fd: int, name: str, value: dict, *, replace: bool) -> None:
    content = topical.encoded(value) + b"\n"
    if replace:
        temporary = ".attempt-" + os.urandom(12).hex()
        target = temporary
    else:
        target = name
    out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    try:
        with os.fdopen(out_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.rename(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if replace:
            try: os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError: pass


def _process_metrics(start: float, before) -> dict:
    """Measure the reaped adapter/CLI child tree without claiming a per-call RSS peak."""
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    divisor = 1024 * 1024 if sys.platform == 'darwin' else 1024
    return {
        'wallMs': round((time.monotonic() - start) * 1000, 3),
        'childCpuMs': round(max(0.0, (after.ru_utime + after.ru_stime -
                                    before.ru_utime - before.ru_stime) * 1000), 3),
        'childRssHighWaterMiB': round(after.ru_maxrss / divisor, 3),
        'childRssHighWaterBeforeMiB': round(before.ru_maxrss / divisor, 3),
        'rssMeaning': 'reaped_children_lifetime_high_water_not_per_call_peak',
    }


@provider_call
def _attempt(root: Path, kind: str, payload: dict, argv: list[str], timeout: int, runner,
             attempt_id: str, max_attempts: int, estimated_cost_cents: int,
             max_cost_cents: int, *, selected_engine: str | None = None) -> tuple[object, bool]:
    """Reserve a possible bill before calling; an interrupted call stays uncertain.

    A different explicit attempt ID can retry only within the caller's count and
    estimated-cost budget. These cents are a local reservation, not a provider
    billing limit or assertion that a failed transport was free.
    """
    if (not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id)
            or type(max_attempts) is not int or not 1 <= max_attempts <= 3
            or type(estimated_cost_cents) is not int or not 1 <= estimated_cost_cents <= 1000
            or type(max_cost_cents) is not int or not 1 <= max_cost_cents <= 1000):
        raise topical.TopicalError("invalid_backend_attempt_budget")
    import topical_story_scope
    if topical_story_scope.exact_story(root):
        topical_story_scope.read(root)
        if kind == 'generation':
            topical_story_scope.validate_wiki_candidates(
                root, payload.get('existingWikiCandidates'))
        reviewed = json.loads((root / topical_story_scope.AUTO_POLICY).read_text())
        expected = worker.resolve_extractor_argv(reviewed['compileCfg'], 'codex')
        if (argv != expected or selected_engine not in (None, 'codex')
                or (runner is not worker.run_extractor and
                    os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1')):
            raise topical.TopicalError('topical_story_transport_unreviewed')
    request_sha = topical.digest(topical.encoded(payload))
    transport_sha = transport_fingerprint(root, argv)
    prefix = f"{kind}.{request_sha}."
    name = prefix + attempt_id + ".attempt.json"
    fd = _audit_fd(root)
    try:
        lock_fd = os.open(".attempt.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
        try:
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise topical.TopicalError("unsafe_backend_attempt_lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            rows = [_read_audit(fd, item) for item in os.listdir(fd)
                    if item.startswith(prefix) and item.endswith(".attempt.json")]
            if any(row.get("schema") != ATTEMPT_SCHEMA or row.get("kind") != kind
                   or row.get("requestSha256") != request_sha
                   or type(row.get("estimatedCostCents")) is not int
                   or not 1 <= row["estimatedCostCents"] <= 1000 for row in rows):
                raise topical.TopicalError("invalid_backend_attempt")
            current = next((row for row in rows if row.get("attemptId") == attempt_id), None)
            if current is not None:
                if current.get("transportSha256") != transport_sha:
                    raise topical.TopicalError("backend_attempt_transport_changed")
                if kind == 'verification' and current.get('selectedEngine') != selected_engine:
                    raise topical.TopicalError('verification_policy_changed')
                if current.get("state") == "completed" and "adapterResponse" in current:
                    return current["adapterResponse"], True
                raise topical.TopicalError(kind + "_already_attempted")
            if (len(rows) >= max_attempts or
                    sum(row["estimatedCostCents"] for row in rows) + estimated_cost_cents > max_cost_cents):
                raise topical.TopicalError("backend_attempt_budget_exhausted")
            record = {"schema": ATTEMPT_SCHEMA, "kind": kind,
                      "requestSha256": request_sha, "attemptId": attempt_id,
                      "transportSha256": transport_sha,
                      "state": "reserved", "estimatedCostCents": estimated_cost_cents}
            if kind == 'generation':
                record['requestContext'] = {
                    'sourcePaths': [item['path'] for item in payload['sources']],
                    'leadBudgetChars': payload['leadBudgetChars'],
                    'existingWikiCandidates': payload.get('existingWikiCandidates')}
            if kind == 'similarity':
                if payload.get('task') != 'dedup':
                    raise topical.TopicalError('invalid_similarity_request')
                # Full bounded page bytes are necessary to replay the exact
                # semantic comparison after an index reorder or process crash.
                record['requestContext'] = payload
            if kind == 'verification':
                if not isinstance(selected_engine, str) or not selected_engine:
                    raise topical.TopicalError('verification_policy_required')
                record['selectedEngine'] = selected_engine
            _write_audit(fd, name, record, replace=False)
        finally:
            os.close(lock_fd)
    finally:
        os.close(fd)
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        parsed, error, code = runner(argv, payload, timeout, root)
    except BaseException:
        # The host may have been charged, even if our process saw no result.
        _finish_attempt(root, name, record, "uncertain",
                        metrics=_process_metrics(started, before))
        raise
    metrics = _process_metrics(started, before)
    if error or code != 0:
        _finish_attempt(root, name, record, "failed", metrics=metrics)
        raise topical.TopicalError(error or kind + "_backend_failed")
    _finish_attempt(root, name, record, "completed", parsed, metrics=metrics)
    return parsed, False


def _finish_attempt(root: Path, name: str, record: dict, state: str,
                    response: object = None, *, metrics: dict | None = None) -> None:
    fd = _audit_fd(root)
    try:
        current = _read_audit(fd, name)
        if current != record:
            raise topical.TopicalError("backend_attempt_changed")
        updated = {**record, "state": state}
        if metrics is not None:
            updated['runtimeMetrics'] = metrics
        if state == "completed":
            updated["adapterResponse"] = response
        _write_audit(fd, name, updated, replace=True)
    finally:
        os.close(fd)


def stage_generation_response(root: Path, response: dict, lead_budget: int = 600,
                              requested_sources: list[str] | None = None,
                              allow_empty: bool = False,
                              reuse_existing: bool = False) -> dict:
    """Compile a synthetic/backend JSON response through the same atomic v2 writer."""
    experiment.require_sandbox(root)
    if not isinstance(response, dict) or response.get("schema") != topical.SCHEMA:
        raise topical.TopicalError("invalid_generation_response")
    if not isinstance(response.get("cards"), list):
        raise topical.TopicalError("invalid_generation_response")
    if len(response["cards"]) > 12:
        raise topical.TopicalError("too_many_topical_cards")
    normalized = copy.deepcopy(response)
    if requested_sources is not None:
        allowed = set(requested_sources)
        if not allowed or len(allowed) != len(requested_sources) or len(allowed) > 3:
            raise topical.TopicalError("invalid_contract_sources")
        cache = {}
        for card in normalized["cards"]:
            if not isinstance(card, dict) or not isinstance(card.get("claims"), list):
                raise topical.TopicalError("invalid_generation_response")
            for claim in card["claims"]:
                if not isinstance(claim, dict) or not isinstance(claim.get("evidence"), list):
                    raise topical.TopicalError("invalid_generation_response")
                for span in claim["evidence"]:
                    if not isinstance(span, dict) or span.get("sourcePath") not in allowed:
                        raise topical.TopicalError("unrequested_source_reference")
                    revision, _body, _lines = topical.source_snapshot(root, span["sourcePath"], cache)
                    quote = span.get("quoteAnchor")
                    if not isinstance(quote, str):
                        raise topical.TopicalError("invalid_quote_anchor")
                    owned = {"sourceRevisionSha256": revision["sha256"],
                             "quoteSha256": topical.digest(quote.encode("utf-8"))}
                    for key, value in owned.items():
                        if key in span and span[key] != value:
                            raise topical.TopicalError("model_supplied_hash_mismatch")
                        span[key] = value
    if not normalized["cards"]:
        if allow_empty:
            return {"status": "no_candidates", "cardCount": 0, "cards": []}
        raise topical.TopicalError("empty_generation_response")
    payload = {"schema": normalized["schema"], "cards": normalized["cards"]}
    try:
        return topical.compile_sandbox(root, payload, lead_budget)
    except topical.TopicalError as exc:
        if exc.code != 'generation_already_exists' or not reuse_existing:
            raise
        # Recovery of a completed audited response: deterministic generation ID
        # may already exist from a crash before the journal was updated. Reuse
        # only when every staged sidecar exactly matches this same response.
        cards, _cache = topical.normalize(root, payload, lead_budget)
        digest_input = [{key: value for key, value in card.items() if key != 'candidate'}
                        for card in cards]
        generation_id = topical.digest(topical.encoded({
            'cards': digest_input, 'leadBudget': lead_budget}))[:24]
        manifest = json.loads(topical.read_generation_bytes(root, generation_id, 'manifest.json'))
        if (manifest.get('generationId') != generation_id
                or manifest.get('leadBudget') != lead_budget
                or [row.get('cardId') for row in manifest.get('cards', [])] !=
                   [card['cardId'] for card in cards]):
            raise topical.TopicalError('generation_reuse_conflict')
        for card in cards:
            if experiment.load_staged_card(root, generation_id, card['cardId']) != {
                    key: value for key, value in card.items() if key != 'candidate'}:
                raise topical.TopicalError('generation_reuse_conflict')
        return {'status': 'generated_unverified', 'generationId': generation_id,
                'path': str(root / 'topical-generations' / generation_id),
                'cardCount': len(cards), 'cards': manifest['cards']}


def verification_payload(root: Path, generation_id: str, card_id: str) -> dict:
    """Bind the verifier request to the current card and complete source snapshots."""
    root = experiment.require_sandbox(root)
    card = experiment.load_staged_card(root, generation_id, card_id)
    if len(card["sourceRevisions"]) > 3:
        raise topical.TopicalError("too_many_verification_sources")
    experiment.generation_path(root, generation_id)
    page_bytes = topical.read_generation_bytes(root, generation_id, f"cards/{card_id}.md")
    evidence_bytes = topical.read_generation_bytes(root, generation_id, f"cards/{card_id}.evidence.json")
    sources = []
    for revision in card["sourceRevisions"]:
        current, _body, lines = topical.source_snapshot(root, revision["path"], {})
        if current != revision:
            raise topical.TopicalError("source_changed_before_verification")
        sources.append({"path": revision["path"], "sourceRevisionSha256": revision["sha256"],
                        "numberedContent": "\n".join(f"{i}: {line.rstrip(chr(10))}" for i, line in enumerate(lines, 1))})
    return {"task": VERIFY_TASK, "generationId": generation_id, "cardId": card_id,
            "card": {"path": f"topical-generations/{generation_id}/cards/{card_id}.md",
                     "markdownSha256": topical.digest(page_bytes),
                     "evidenceSha256": topical.digest(evidence_bytes),
                     "claims": card["claims"], "lead": card["lead"]},
            "sources": sources}


def validate_verification_response(root: Path, request: dict, response: dict, *, mode: str,
                                   engine: str = "") -> dict:
    """Reject incomplete or unbound claim decisions, preserving all staged cards."""
    root = experiment.require_sandbox(root)
    if mode not in ("mock", "backend"):
        raise topical.TopicalError("invalid_attestation_mode")
    if mode == "backend" and (not isinstance(engine, str) or not engine.strip()):
        raise topical.TopicalError("missing_verifier_engine")
    if not isinstance(request, dict) or request.get("task") != VERIFY_TASK:
        raise topical.TopicalError("invalid_verification_request")
    current = verification_payload(root, request.get("generationId"), request.get("cardId"))
    if current != request:
        raise topical.TopicalError("verification_input_changed")
    if (not isinstance(response, dict) or set(response) != {"verdict", "checks", "reasons"}
            or not isinstance(response["checks"], list) or not isinstance(response["reasons"], list)
            or any(not isinstance(reason, str) for reason in response["reasons"])):
        raise topical.TopicalError("invalid_verification_response")
    card = experiment.load_staged_card(root, request["generationId"], request["cardId"])
    anchors = {(claim["claimId"], span["sourcePath"], span["quoteSha256"]): span["quoteAnchor"]
               for claim in card["claims"] for span in claim["evidence"]}
    checks = []
    for item in response["checks"]:
        if not isinstance(item, dict) or set(item) != {
                "claimId", "sourcePath", "quoteSha256", "quoteAnchor", "supported"}:
            raise topical.TopicalError("invalid_verification_check")
        key = (item["claimId"], item["sourcePath"], item["quoteSha256"])
        if key not in anchors or item["quoteAnchor"] != anchors[key]:
            raise topical.TopicalError("verification_quote_mismatch")
        checks.append({key: item[key] for key in ("claimId", "sourcePath", "quoteSha256", "supported")})
    decision = {"verdict": response["verdict"], "checks": checks}
    verdict = experiment.mock_semantic_verdict(card, decision)
    status = f"{mode}_{verdict}"
    sources_sha = topical.digest(topical.encoded(card["sourceRevisions"]))
    return {"schema": ATTESTATION_SCHEMA, "mode": mode, "status": status,
            "engine": engine.strip() if mode == "backend" else "",
            "generationId": request["generationId"], "cardId": request["cardId"],
            "cardMarkdownSha256": request["card"]["markdownSha256"],
            "evidenceSha256": request["card"]["evidenceSha256"],
            "sourceRevisionsSha256": sources_sha,
            "requestSha256": topical.digest(topical.encoded(request)),
            "responseSha256": topical.digest(topical.encoded(response)),
            "verdict": verdict, "checks": response["checks"], "reasons": response["reasons"]}


def publish_pass_attestation(root: Path, record: dict) -> Path:
    """Write a separate immutable pass record; never replace a prior valid one."""
    root = experiment.require_sandbox(root)
    if not isinstance(record, dict) or record.get("status") not in ("mock_pass", "backend_pass"):
        raise topical.TopicalError("only_pass_can_be_published")
    request = verification_payload(root, record.get("generationId"), record.get("cardId"))
    response = {"verdict": record.get("verdict"), "checks": record.get("checks"),
                "reasons": record.get("reasons")}
    current = validate_verification_response(root, request, response,
                                             mode=record.get("mode"), engine=record.get("engine", ""))
    if current != record:
        raise topical.TopicalError("attestation_input_changed")
    directory = root
    for part in ("topical-attestations", record["mode"], request["generationId"]):
        directory = directory / part
        if directory.is_symlink():
            raise topical.TopicalError("unsafe_attestation_directory")
        directory.mkdir(mode=0o700, exist_ok=True)
        if not directory.is_dir():
            raise topical.TopicalError("unsafe_attestation_directory")
    final = directory / f"{request['cardId']}.json"
    content = topical.encoded(record) + b"\n"
    if final.exists():
        if final.is_symlink() or final.read_bytes() != content:
            raise topical.TopicalError("attestation_already_exists")
        return final
    fd, name = tempfile.mkstemp(prefix=".attestation-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        # Exclusive create; os.replace would silently replace an earlier pass.
        os.link(name, final)
    except FileExistsError as exc:
        raise topical.TopicalError("attestation_already_exists") from exc
    finally:
        Path(name).unlink(missing_ok=True)
    return final


def run_generation_backend(root: Path, paths: list[str], compile_cfg: dict, engine: str,
                           *, allow_backend_execution: bool = False, lead_budget: int = 600,
                           existing_wiki_candidates: list[dict] | None = None,
                           runner=worker.run_extractor, attempt_id: str = "initial",
                           max_attempts: int = 1, estimated_cost_cents: int = 10,
                           max_cost_cents: int = 10) -> dict:
    """Future opt-in host transport. Default path is locked and makes no call."""
    root = experiment.require_sandbox(root)
    if not allow_backend_execution:
        raise topical.TopicalError("backend_execution_not_enabled")
    payload = experiment.generation_contract(root, paths, lead_budget)
    if existing_wiki_candidates is not None:
        if (not isinstance(existing_wiki_candidates, list) or len(existing_wiki_candidates) > 8
                or any(not isinstance(row, dict) or set(row) != {'path', 'lead'}
                       or not isinstance(row['path'], str) or not isinstance(row['lead'], str)
                       or len(row['lead']) > 600 for row in existing_wiki_candidates)):
            raise topical.TopicalError('invalid_existing_wiki_context')
        payload['existingWikiCandidates'] = existing_wiki_candidates
    payload["timeout"] = 420
    argv = worker.resolve_extractor_argv(compile_cfg, engine)
    if argv is None:
        raise topical.TopicalError("generation_backend_unavailable")
    request_sha = topical.digest(topical.encoded(payload))
    parsed, reused = _attempt(root, "generation", payload, argv, 450, runner,
                              attempt_id, max_attempts, estimated_cost_cents, max_cost_cents)
    if reused:
        raise topical.TopicalError("generation_already_attempted")
    # Preserve the adapter's structured output before applying business rules.
    # The caller can diagnose a well-formed but empty result without another call.
    record = {"schema": "qmd-topical-backend-response-audit-v1",
              "requestSha256": request_sha,
              "sourceRevisionSha256": payload["sources"][0]["sourceRevisionSha256"],
              "adapterResponse": parsed}
    audit_fd = _audit_fd(root)
    try:
        _write_audit(audit_fd, request_sha + ".generation.json", record, replace=False)
    finally:
        os.close(audit_fd)
    return stage_generation_response(root, parsed, lead_budget, requested_sources=paths)


def run_verification_backend(root: Path, generation_id: str, card_id: str,
                             compile_cfg: dict, producing_engine: str,
                             *, allow_backend_execution: bool = False,
                             runner=worker.run_extractor, attempt_id: str = "initial",
                             max_attempts: int = 1, estimated_cost_cents: int = 10,
                             max_cost_cents: int = 10) -> dict:
    """Future opt-in verifier transport; backend pass stays outside card metadata."""
    root = experiment.require_sandbox(root)
    if not allow_backend_execution:
        raise topical.TopicalError("backend_execution_not_enabled")
    payload = verification_payload(root, generation_id, card_id)
    vcfg = verifier.verify_cfg_of(compile_cfg)
    attempts, _mode, reason = verifier.plan_verify_attempts(compile_cfg, vcfg, producing_engine, set())
    if not attempts:
        raise topical.TopicalError(reason or "verification_backend_unavailable")
    attempt = attempts[0]
    parsed, _reused = _attempt(root, "verification", payload, attempt["argv"], 120, runner,
                               attempt_id, max_attempts, estimated_cost_cents, max_cost_cents,
                               selected_engine=attempt['engine'])
    response = {key: parsed.get(key) for key in ("verdict", "checks", "reasons")} if isinstance(parsed, dict) else None
    record = validate_verification_response(root, payload, response, mode="backend", engine=attempt["engine"])
    if record["verdict"] == "pass":
        publish_pass_attestation(root, record)
    return record


def verification_bundle_payload(root: Path, generation_id: str, card_ids: list[str]) -> dict:
    """Build the exact bounded payload that one future verifier call would receive."""
    root = experiment.require_sandbox(root)
    if (not isinstance(card_ids, list) or not card_ids or len(card_ids) > 12
            or len(set(card_ids)) != len(card_ids)):
        raise topical.TopicalError("invalid_verification_bundle")
    requests = [verification_payload(root, generation_id, card_id) for card_id in card_ids]
    source_rows = {}
    for request in requests:
        for source in request["sources"]:
            previous = source_rows.setdefault(source["path"], source)
            if previous != source:
                raise topical.TopicalError("source_snapshot_conflict")
    if len(source_rows) > 3:
        raise topical.TopicalError("too_many_verification_sources")
    return {"task": BUNDLE_VERIFY_TASK, "generationId": generation_id,
            "cards": [{"cardId": request["cardId"], **request["card"]} for request in requests],
            "sources": [source_rows[path] for path in sorted(source_rows)], "timeout": 480}


def run_verification_bundle_backend(root: Path, generation_id: str, card_ids: list[str],
                                    compile_cfg: dict, producing_engine: str,
                                    *, allow_backend_execution: bool = False,
                                    runner=worker.run_extractor, attempt_id: str = "initial",
                                    max_attempts: int = 1, estimated_cost_cents: int = 10,
                                    max_cost_cents: int = 10) -> list[dict]:
    """One verifier invocation for every card generated from one source."""
    root = experiment.require_sandbox(root)
    if not allow_backend_execution:
        raise topical.TopicalError("backend_execution_not_enabled")
    payload = verification_bundle_payload(root, generation_id, card_ids)
    requests = [verification_payload(root, generation_id, card_id) for card_id in card_ids]
    vcfg = verifier.verify_cfg_of(compile_cfg)
    attempts, _mode, reason = verifier.plan_verify_attempts(compile_cfg, vcfg, producing_engine, set())
    if not attempts:
        raise topical.TopicalError(reason or "verification_backend_unavailable")
    attempt = attempts[0]
    request_sha = topical.digest(topical.encoded(payload))
    parsed, reused = _attempt(root, "bundle_verification", payload, attempt["argv"], 500,
                              runner, attempt_id, max_attempts, estimated_cost_cents, max_cost_cents)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cards"), list):
        raise topical.TopicalError("verification_backend_failed")
    if not reused:
        audit_fd = _audit_fd(root)
        try:
            _write_audit(audit_fd, request_sha + ".verification.json",
                         {"schema": "qmd-topical-backend-response-audit-v1",
                          "requestSha256": request_sha, "adapterResponse": parsed}, replace=False)
        finally:
            os.close(audit_fd)
    responses = {}
    for row in parsed["cards"]:
        if (not isinstance(row, dict) or not isinstance(row.get("cardId"), str)
                or row["cardId"] in responses):
            raise topical.TopicalError("invalid_verification_bundle")
        responses[row["cardId"]] = {key: row.get(key) for key in ("verdict", "checks", "reasons")}
    if set(responses) != set(card_ids):
        raise topical.TopicalError("incomplete_verification_bundle")
    records = [validate_verification_response(root, request, responses[request["cardId"]],
                                              mode="backend", engine=attempt["engine"])
               for request in requests]
    for record in records:
        if record["verdict"] == "pass":
            publish_pass_attestation(root, record)
    return records
