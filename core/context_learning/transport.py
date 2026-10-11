"""Explicit, default-off teacher transport. Never called by recall/hooks.

Only normalized stored samples cross the provider boundary. Authorization and
optional hook audits are trusted-caller assertions, not runtime certifications.
"""
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import claude_adapter, codex_adapter
from .contracts import validate_request
from .offline import save_provisional_batch
from .store import canonical, database
from .teacher import prompt, validate_response

_SERIAL = threading.Lock()
MAX_OUTPUT_BYTES = 131072
DEFAULT_TIMEOUT = 45
_CLAUDE_BUILTINS = ('cc-plugin-agents-md@builtin', 'cc-plugin-telemetry@builtin',
                    'cc-plugin-plugin-authoring@builtin')


class TransportFailure(Exception):
    pass


def _stop_owned(process):
    # Popen creates a new group. Never discover/kill unrelated system processes.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    # A descendant may keep pipes open even after the group leader exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # The leader and its process group have already exited.
        pass
    except PermissionError:
        return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportFailure('duplicate_event_key')
        result[key] = value
    return result


def _numeric_usage(value):
    if not isinstance(value, dict):
        return {}
    allowed = {'input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
               'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
               'reasoning_output_tokens', 'thinking_tokens'}
    return {k: v for k, v in value.items()
            if k in allowed and type(v) is int and v >= 0}


def run_teacher(sample, *, enabled=False, host=None, timeout=DEFAULT_TIMEOUT,
                cancel=None, accept_security_policy=False,
                optional_hooks_audited=False, disabled_plugin_ids=(), model=None):
    """One bounded CLI start, no fallback/retries. Return labels, never review.

    For Codex, optional_hooks_audited acknowledges caller verified installed
    optional hooks are inert in a non-Git cwd with the minimal environment.
    Codex retains builtin context; its exec stream is not an empty-catalog proof.
    Caller must not log the returned labels when they contain private excerpts.
    """
    report = {'status': 'disabled', 'cli_invocations': 0,
              'resolved_model': 'unreported', 'resolved_effort': 'unreported'}
    if enabled is not True:
        return report
    report['status'] = 'failed'
    process = None
    locked = False
    try:
        validate_request(sample)
        if host not in ('claude', 'codex') or host != sample['host']:
            raise TransportFailure('host_mismatch')
        if type(accept_security_policy) is not bool or type(optional_hooks_audited) is not bool:
            raise TransportFailure('invalid_authorization')
        if host == 'codex' and not optional_hooks_audited:
            raise TransportFailure('optional_hooks_not_audited')
        if type(timeout) not in (int, float) or not 0 < timeout <= DEFAULT_TIMEOUT:
            raise TransportFailure('invalid_timeout')
        if cancel is not None and not callable(cancel):
            raise TransportFailure('invalid_cancellation')
        if cancel is not None and cancel():
            raise TransportFailure('cancelled')
        locked = _SERIAL.acquire(blocking=False)
        if not locked:
            raise TransportFailure('transport_busy')
        environment = codex_adapter.child_environment(os.environ)
        if host == 'claude':
            if not isinstance(disabled_plugin_ids, (list, tuple)):
                raise TransportFailure('invalid_disabled_plugin_ids')
            if 'cc-plugin-sec-default@builtin' in disabled_plugin_ids:
                raise TransportFailure('security_policy_override_forbidden')
            ids = list(disabled_plugin_ids) + list(_CLAUDE_BUILTINS)
            args = claude_adapter.argv(disabled_plugin_ids=ids, model=model or 'sonnet')
            environment.update(CLAUDE_CODE_EFFORT_LEVEL='low',
                               CLAUDE_CODE_MAX_OUTPUT_TOKENS='1200',
                               CLAUDE_CODE_MAX_RETRIES='0',
                               CLAUDE_CODE_AUTO_CONNECT_IDE='0')
        else:
            args = codex_adapter.argv(model=model or codex_adapter.MODEL)
        executable = shutil.which(host, path=environment.get('PATH', os.defpath))
        if not executable:
            raise TransportFailure('missing_cli')
        args[0] = executable
        report.update(requested_model=model or ('sonnet' if host == 'claude' else codex_adapter.MODEL),
                      requested_effort='low', events={}, usage={},
                      catalog_evidence='unverified' if host == 'claude'
                      else 'not-reported-by-exec')
        body = prompt(sample).encode()
        with tempfile.TemporaryDirectory(prefix='qmd-teacher-') as cwd:
            directory = Path(cwd)
            if any((p / '.git').exists() for p in (directory, *directory.parents)):
                raise TransportFailure('git_ancestor_present')
            deadline = time.monotonic() + timeout
            process = subprocess.Popen(args, cwd=cwd, env=environment,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            report['cli_invocations'] = 1
            total = 0
            buffer = b''
            parts = []
            initialized = False
            completed = False
            turn_started = False
            with selectors.DefaultSelector() as selector:
                for stream, tag in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, tag)
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                offset = 0
                while selector.get_map():
                    if cancel is not None and cancel():
                        raise TransportFailure('cancelled')
                    if time.monotonic() >= deadline:
                        raise TransportFailure('timeout')
                    for key, _ in selector.select(min(.05, max(0, deadline-time.monotonic()))):
                        if key.data == 'stdin':
                            try:
                                offset += os.write(key.fd, body[offset:])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                raise TransportFailure('stdin_closed')
                            if offset == len(body):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        try:
                            chunk = os.read(key.fd, 8192)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT_BYTES:
                            raise TransportFailure('output_budget')
                        if key.data == 'stderr':
                            # Drain/count; never persist or return raw CLI stderr.
                            report['stderr_present'] = True
                            continue
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if not line.strip():
                                continue
                            try:
                                event = json.loads(line, object_pairs_hook=_unique_object)
                            except (UnicodeError, json.JSONDecodeError):
                                raise TransportFailure('invalid_event_json')
                            if not isinstance(event, dict):
                                raise TransportFailure('invalid_event')
                            kind = event.get('type')
                            known = ('system', 'assistant', 'stream_event', 'rate_limit_event', 'result') if host == 'claude' else ('thread.started', 'turn.started', 'turn.completed', 'item.started', 'item.updated', 'item.completed', 'turn.failed', 'error')
                            if kind not in known:
                                raise TransportFailure('unexpected_event')
                            if completed:
                                raise TransportFailure('event_after_completion')
                            report['events'][kind] = report['events'].get(kind, 0)+1
                            if host == 'claude':
                                try:
                                    metadata = claude_adapter.inspect_event(event,
                                        inventory_ids=disabled_plugin_ids,
                                        allow_security_policy=accept_security_policy)
                                except claude_adapter.StartupRejected as exc:
                                    report['startup'] = exc.metadata
                                    raise TransportFailure('startup_rejected')
                                except (ValueError, TypeError, AttributeError):
                                    raise TransportFailure('unsafe_or_invalid_event')
                                # Streaming tool blocks are not covered by message.content.
                                stream = event.get('event', {})
                                if isinstance(stream, dict) and isinstance(stream.get('content_block'), dict) and stream['content_block'].get('type') in ('tool_use', 'server_tool_use', 'tool_result'):
                                    raise TransportFailure('unexpected_tool_block')
                                if metadata:
                                    if initialized:
                                        raise TransportFailure('duplicate_init')
                                    initialized = True
                                    report['startup'] = metadata
                                    report['catalog_evidence'] = 'runtime-empty-except-accepted-policy' if metadata['retained_security_policy_only'] else 'runtime-empty'
                                    report['resolved_model'] = metadata['resolved_model']
                                    report['resolved_effort'] = metadata['reported_effort']
                                elif kind != 'system' and not initialized:
                                    raise TransportFailure('response_before_init')
                                if kind == 'system' and not metadata:
                                    raise TransportFailure('unexpected_system_event')
                                if kind == 'result':
                                    if event.get('is_error') is not False or event.get('subtype') != 'success':
                                        raise TransportFailure('provider_error')
                                    turns = event.get('num_turns')
                                    if turns is not None and (type(turns) is not int or turns != 1):
                                        raise TransportFailure('turn_budget')
                                    text = event.get('result')
                                    if not isinstance(text, str):
                                        raise TransportFailure('invalid_result')
                                    parts = [text]
                                    report['usage'] = _numeric_usage(event.get('usage'))
                                    completed = True
                            else:
                                try:
                                    checked = codex_adapter.inspect_event(event)
                                except (ValueError, TypeError, AttributeError):
                                    raise TransportFailure('unsafe_or_invalid_event')
                                if kind == 'thread.started':
                                    if initialized:
                                        raise TransportFailure('duplicate_init')
                                    initialized = True
                                elif not initialized:
                                    raise TransportFailure('response_before_init')
                                if kind == 'turn.started':
                                    if turn_started:
                                        raise TransportFailure('turn_budget')
                                    turn_started = True
                                if kind.startswith('item.') and not turn_started:
                                    raise TransportFailure('item_before_turn')
                                if 'response' in checked:
                                    parts.append(checked['response'])
                                if kind == 'turn.completed':
                                    if not turn_started:
                                        raise TransportFailure('completion_before_turn')
                                    report['usage'] = checked['usage']
                                    completed = True
                    report['stream_bytes'] = total
            if buffer.strip():
                raise TransportFailure('unterminated_event')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TransportFailure('timeout')
            while process.poll() is None:
                if cancel is not None and cancel():
                    raise TransportFailure('cancelled')
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TransportFailure('timeout')
                try:
                    process.wait(timeout=min(.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode != 0:
                raise TransportFailure('cli_failed')
            if not initialized or not completed:
                raise TransportFailure('incomplete_stream')
            if cancel is not None and cancel():
                raise TransportFailure('cancelled')
            try:
                labels = validate_response('\n'.join(parts).encode(), sample)
            except (ValueError, TypeError, AttributeError):
                raise TransportFailure('invalid_labels')
            report.update(status='completed', labels=labels, label_status='provisional',
                          schema_result='passed')
    except TransportFailure as exc:
        report['reason'] = str(exc)  # Only our fixed reason codes.
    except (ValueError, TypeError, KeyError, AttributeError):
        report['reason'] = 'invalid_input'
    except (OSError, sqlite3.Error):
        report['reason'] = 'transport_unavailable'
    except Exception:
        report['reason'] = 'transport_internal_error'
    finally:
        try:
            if process is not None:
                try:
                    if not _stop_owned(process):
                        report.update(status='failed', reason='transport_cleanup_unavailable')
                except (OSError, subprocess.SubprocessError):
                    report.update(status='failed', reason='transport_cleanup_unavailable')
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
        finally:
            if locked:
                _SERIAL.release()
    return report


def label_request(state_dir, request_id, *, enabled=False, host=None, **options):
    """Explicitly label an already stored request, atomically as provisional.

    No capture or automatic promotion; original recall behavior is unaffected.
    On failure return a safe status, never partial labels or raw provider errors.
    """
    if enabled is not True:
        return {'status': 'disabled', 'cli_invocations': 0}
    result = {'status': 'failed', 'cli_invocations': 0}
    try:
        with database(state_dir) as db:
            row = db.execute('SELECT body FROM samples WHERE request_id=?', (request_id,)).fetchone()
            if row is None:
                return {'status': 'failed', 'reason': 'unknown_or_deleted_request', 'cli_invocations': 0}
            sample = json.loads(row[0])
        result = run_teacher(sample, enabled=True, host=host, **options)
        if result['status'] != 'completed':
            return result
        labels = result.pop('labels')
        if options.get('cancel') is not None and options['cancel']():
            result.update(status='failed', reason='cancelled')
            return result
        try:
            result['stored_labels'] = save_provisional_batch(state_dir, sample, labels)
        except (ValueError, OSError, sqlite3.Error):
            result.update(status='failed', reason='provisional_storage_rejected', label_status='not-stored')
        return result
    except Exception:
        result.pop('labels', None)
        result.update(status='failed', reason='storage_unavailable')
        return result
