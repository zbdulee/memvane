"""Default-teacher host readiness and one bounded CLI call per case.

A case is labelled only by the CLI of its own session host (``sample.host``);
it is never handed to the other host. Readiness is decided once per pass:
executable, model resolution, the isolation pre-check and, for Codex, the
weekly quota. A call raises ``CallFailure`` whose ``kind`` drives the case,
ledger and host outcome in ``general_teacher.process_pending_default``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import tempfile
import time

from memvane.runtime.api import provider_popen
from memvane.runtime.types import PreSendDeferred
from memvane.kernel.process import stop_owned as _stop_owned

from . import claude_adapter, codex_adapter
from . import teacher_isolation as isolation
from . import teacher_model_tiers as tiers

HOSTS = ('claude', 'codex')
UNAVAILABLE = 'teacher_host_unavailable'
QUOTA = 'deferred_quota'
QUOTA_FLOOR = 30
INIT_REQUEST_ID = 'memvane-teacher-initialize'
REFUSED_CODES = frozenset({'provider_quota_waiting', 'provider_model_unavailable',
                           'provider_auth_waiting'})


class CallFailure(Exception):
    """kind: pre_send | host | refused | content | unknown | invalid | deferred.

    ``pre_send`` and ``host`` prove the payload never reached the provider;
    ``host`` is a host-level condition (model, isolation) that rests the host.
    """

    def __init__(self, kind, code, *, isolation_state='ok', tools=None, detail=None):
        super().__init__(code)
        self.kind = kind
        self.code = code
        self.isolation = isolation_state
        self.tools = tools or []
        self.detail = detail


def child_environment(host, environ):
    env = codex_adapter.child_environment(environ)
    if host == 'claude':
        env.pop('CODEX_HOME', None)
        env.update(isolation.CLAUDE_ENV_FIXED)
    return env


def resolve_executable(host, env, project=None):
    """The host CLI from safe PATH entries of the child env, or None."""
    root = Path(project).resolve() if project is not None else None
    for raw in str(env.get('PATH', '')).split(os.pathsep):
        if not raw or not os.path.isabs(raw):
            continue
        try:
            directory = Path(raw).resolve(strict=True)
            info = directory.stat()
        except OSError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022
                or (root is not None and (directory == root or root in directory.parents))):
            continue
        candidate = Path(raw) / host
        try:
            target = candidate.resolve(strict=True)
            found = target.stat()
        except OSError:
            continue
        if (not stat.S_ISREG(found.st_mode) or not os.access(target, os.X_OK)
                or found.st_uid not in (0, os.getuid()) or found.st_mode & 0o022
                or (root is not None and (target == root or root in target.parents))):
            continue
        return str(candidate)
    return None


def build_argv(host, executable, model, system_prompt):
    if host == 'claude':
        args = claude_adapter.argv(disabled_plugin_ids=isolation.CLAUDE_BUILTIN_PLUGINS,
                                   model=model, system_prompt=system_prompt,
                                   input_format='stream-json')
    else:
        args = codex_adapter.argv(model=model)
    args[0] = executable
    return args


def _clean_base(base):
    """True when neither ``base`` nor an ancestor holds a host-instruction marker."""
    try:
        resolved = Path(base).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return not any(os.path.lexists(folder / marker)
                   for folder in (resolved, *resolved.parents)
                   for marker in isolation.FOLDER_MARKERS)


def _new_folder():
    # A TMPDIR under HOME sits below ~/.claude and ~/.codex, which the isolation
    # check refuses; use the system temp root then instead of resting the host.
    for base in (tempfile.gettempdir(), '/tmp'):
        if _clean_base(base):
            return tempfile.mkdtemp(prefix='memvane-teacher-', dir=base)
    return tempfile.mkdtemp(prefix='memvane-teacher-')


def _remove_folder(path):
    shutil.rmtree(path, ignore_errors=True)


def user_instructions_bytes(env):
    """Size of the global ``AGENTS.md`` (stat only, never its content)."""
    try:
        return os.stat(tiers.codex_home(env) / 'AGENTS.md').st_size
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None


def prepare(host, *, project, environ, system_prompt, read_quota=None):
    """Once per pass: ``{'ready': True, ...}`` or ``{'ready': False, 'reason': ...}``."""
    if host not in HOSTS:
        return {'ready': False, 'reason': UNAVAILABLE}
    env = child_environment(host, environ)
    executable = resolve_executable(host, env, project)
    if executable is None:
        return {'ready': False, 'reason': UNAVAILABLE}
    result = {'ready': True, 'host': host, 'executable': executable, 'tier': tiers.TIER,
              'env': env}
    if host == 'codex':
        try:
            resolved = tiers.resolve_codex(env)
        except ValueError:
            return {'ready': False, 'reason': tiers.UNRESOLVED}
        result.update(model=resolved['model'], catalogAgeDays=resolved['catalogAgeDays'],
                      modelReported=False)
    else:
        result.update(model=tiers.claude_request(), modelReported=True)
    folder = _new_folder()
    try:
        isolation.assert_isolated(host, build_argv(host, executable, result['model'], system_prompt),
                                  env, folder, model=result['model'],
                                  system_prompt=system_prompt, parent=environ, project=project)
    except isolation.IsolationRefused as exc:
        return {'ready': False, 'reason': isolation.REASON, 'detail': exc.detail}
    except ValueError:
        return {'ready': False, 'reason': isolation.REASON, 'detail': 'argv_unbuildable'}
    finally:
        _remove_folder(folder)
    if host == 'codex':
        try:
            remaining = read_quota(environ) if read_quota is not None else None
        except ValueError:
            return {'ready': False, 'reason': QUOTA, 'quotaRemaining': None}
        if type(remaining) not in (int, float) or remaining <= QUOTA_FLOOR:
            return {'ready': False, 'reason': QUOTA,
                    'quotaRemaining': remaining if type(remaining) in (int, float) else None}
        result.update(quotaRemaining=remaining, userInstructionsBytes=user_instructions_bytes(env))
    return result


def call_plan(prepared, *, system_prompt, environ, project):
    """A fresh empty folder plus argv/env, checked again right before reservation."""
    folder = _new_folder()
    host = prepared['host']
    try:
        args = build_argv(host, prepared['executable'], prepared['model'], system_prompt)
        isolation.assert_isolated(host, args, prepared['env'], folder, model=prepared['model'],
                                  system_prompt=system_prompt, parent=environ, project=project)
    except isolation.IsolationRefused:
        _remove_folder(folder)
        raise
    except ValueError as exc:
        _remove_folder(folder)
        raise isolation.IsolationRefused('argv_unbuildable') from exc
    return {'host': host, 'argv': args, 'env': dict(prepared['env']), 'cwd': folder,
            'model': prepared['model']}


def release(plan):
    _remove_folder(plan['cwd'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_event_key')
        result[key] = value
    return result


def _parse_answer(text):
    try:
        value = json.loads(text, object_pairs_hook=_unique)
    except (ValueError, TypeError):
        raise CallFailure('invalid', 'gold_invalid_choice')
    if not isinstance(value, dict) or set(value) != {'selected_ids'}:
        raise CallFailure('invalid', 'gold_invalid_choice')
    return value


def _classify(code, answered):
    if answered:
        return 'unknown'
    if code in REFUSED_CODES:
        return 'refused'
    if code == 'provider_content_rejected':
        return 'content'
    return 'unknown'


class _Run:
    """Owned process plumbing shared by both hosts."""

    def __init__(self, plan, *, seconds, max_output, monitor, guard):
        self.plan, self.max_output, self.monitor, self.guard = plan, max_output, monitor, guard
        self.deadline = time.monotonic() + seconds
        self.began = time.monotonic()
        self.process = None
        self.size = 0
        self.last_check = 0.0

    def launch(self, before_launch, after_launch):
        try:
            self.guard(fresh_inventory=True)
        except Exception as exc:
            raise CallFailure('pre_send', 'teacher_scope_changed') from exc
        try:
            self.process = provider_popen(
                self.plan['argv'], cwd=self.plan['cwd'], env=self.plan['env'],
                before_launch=before_launch, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except PreSendDeferred:
            raise CallFailure('deferred', 'resource_deferred')
        except OSError:
            raise CallFailure('pre_send', 'local_spawn_failed')
        after_launch(self.process.pid)

    def tick(self, sent):
        now = time.monotonic()
        if now >= self.deadline:
            raise CallFailure('unknown' if sent else 'pre_send', 'gold_timeout')
        if now - self.last_check >= .5:
            self.last_check = now
            try:
                self.guard()
                self.monitor(self.process.pid)
            except ValueError as exc:
                code = str(exc) if str(exc).startswith('gold_') else 'teacher_scope_changed'
                raise CallFailure('unknown' if sent else 'pre_send', code)

    def read(self, fd, sent):
        try:
            chunk = os.read(fd, 8192)
        except BlockingIOError:
            return None
        self.size += len(chunk)
        if self.size > self.max_output:
            raise CallFailure('unknown' if sent else 'pre_send', 'gold_output_budget')
        return chunk

    def finish(self, sent):
        while self.process.poll() is None:
            if time.monotonic() >= self.deadline:
                raise CallFailure('unknown' if sent else 'pre_send', 'gold_timeout')
            try:
                self.process.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                pass
        if self.process.returncode != 0:
            raise CallFailure('unknown' if sent else 'pre_send', 'gold_incomplete')

    def close(self):
        if self.process is None:
            return
        try:
            _stop_owned(self.process)
            try:
                self.process.wait(timeout=1)  # reap: the group may already be gone
            except subprocess.TimeoutExpired:
                pass
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def _lines(buffer):
    while b'\n' in buffer:
        line, buffer = buffer.split(b'\n', 1)
        if line.strip():
            yield line, None
    yield None, buffer


def invoke_codex(plan, raw, *, seconds, max_output, monitor, guard, before_launch,
                 after_launch):
    run = _Run(plan, seconds=seconds, max_output=max_output, monitor=monitor, guard=guard)
    tools = {}
    seen = set()
    response = None
    usage = {}
    try:
        run.launch(before_launch, after_launch)
        process = run.process
        offset = 0
        buffer = b''
        thread = started = completed = False

        def fail(kind, code):
            raise CallFailure(kind, code, tools=_tool_list(tools))

        with selectors.DefaultSelector() as selector:
            for stream, event, tag in ((process.stdin, selectors.EVENT_WRITE, 'input'),
                                       (process.stdout, selectors.EVENT_READ, 'output')):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, tag)
            while selector.get_map():
                run.tick(True)
                for key, _ in selector.select(.05):
                    if key.data == 'input':
                        try:
                            offset += os.write(key.fd, raw[offset:])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            fail('unknown', 'gold_incomplete')
                        if offset == len(raw):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = run.read(key.fd, True)
                    if chunk is None:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer += chunk
                    for line, rest in _lines(buffer):
                        if line is None:
                            buffer = rest
                            break
                        if completed:
                            fail('unknown', 'gold_event_after_completion')
                        try:
                            info = codex_adapter.inspect_event(
                                json.loads(line, object_pairs_hook=_unique), count_tools=True)
                        except ValueError as exc:
                            code = str(exc)
                            fail(_classify(code, response is not None), code)
                        kind = info['event_type']
                        if kind == 'thread.started':
                            if thread or started:
                                fail('unknown', 'gold_invalid_event_order')
                            thread = True
                        elif kind == 'turn.started':
                            if not thread or started:
                                fail('unknown', 'gold_invalid_event_order')
                            started = True
                        elif not started:
                            fail('unknown', 'gold_invalid_event_order')
                        item = info.get('tool_item')
                        if item is not None:
                            key_id = item['id'] or ('anonymous', len(seen))
                            if key_id not in seen:
                                seen.add(key_id)
                                tools[item['name']] = tools.get(item['name'], 0) + 1
                        if 'response' in info:
                            if response is not None:
                                fail('unknown', 'gold_multiple_answers')
                            response = info['response']
                        if kind == 'turn.completed':
                            completed = True
                            usage = info['usage']
        if buffer.strip() or not completed or response is None:
            fail('unknown', 'gold_incomplete')
        run.finish(True)
        answer = _parse_answer(response)
    except CallFailure as exc:
        if not exc.tools:
            exc.tools = _tool_list(tools)
        raise
    finally:
        run.close()
    return {'answer': answer, 'usage': usage, 'toolWarnings': _tool_list(tools),
            'elapsed_seconds': round(time.monotonic() - run.began, 3),
            'model': plan['model'], 'modelReported': False}


def _tool_list(tools):
    return [{'name': name, 'count': count} for name, count in sorted(tools.items())]


_CLAUDE_KNOWN = ('system', 'assistant', 'stream_event', 'rate_limit_event', 'result',
                 'control_response', 'control_request', 'keep_alive',
                 # Tool events are known only so that inspect_event reports them as a violation.
                 'tool_use', 'tool_result', 'tool_call', 'tool_progress', 'tool_use_summary')
_CLAUDE_ERRORS = {'rate_limit': 'provider_quota_waiting', 'billing_error': 'provider_quota_waiting',
                  'authentication_failed': 'provider_auth_waiting'}


def _claude_failure(event, assistant_error):
    if assistant_error in _CLAUDE_ERRORS:
        return _CLAUDE_ERRORS[assistant_error]
    status = event.get('api_error_status')
    if status == 429:
        return 'provider_quota_waiting'
    if status in (401, 403):
        return 'provider_auth_waiting'
    parts = [event.get('result'), event.get('error'), event.get('subtype'), assistant_error]
    errors = event.get('errors')
    if isinstance(errors, list):
        parts.extend(errors[:8])
    detail = ' '.join(part for part in parts if isinstance(part, str))
    return codex_adapter._provider_failure({'message': detail}, 'provider_error')


def _models_ok(response, request):
    models = response.get('models')
    if not isinstance(models, list):
        return False
    rows = [row for row in models if isinstance(row, dict) and row.get('value') == request]
    return len(rows) == 1 and tiers.claude_model_ok(rows[0].get('resolvedModel'))


class _ClaudeSession:
    """Event state of one Claude call. ``sent`` turns true only after initialize passes.

    Every failure before ``sent`` proves no payload byte left this process.
    """

    def __init__(self, raw):
        self.request = tiers.claude_request()
        self.init = (json.dumps({'type': 'control_request', 'request_id': INIT_REQUEST_ID,
                                 'request': {'subtype': 'initialize'}}, separators=(',', ':'))
                     + '\n').encode()
        self.body = (json.dumps({'type': 'user', 'message': {'role': 'user', 'content': raw.decode()}},
                                separators=(',', ':')) + '\n').encode()
        self.sent = self.answered = self.initialized = self.completed = False
        self.assistant_error = self.init_model = self.text = None
        self.usage = {}

    def fail(self, code):
        raise CallFailure('unknown' if self.sent else 'pre_send', code)

    def violation(self, code):
        if self.sent:
            raise CallFailure('unknown', code, isolation_state='violated')
        raise CallFailure('host', isolation.REASON, isolation_state='violated', detail=code)

    def control_response(self, event):
        """True when the body may now be sent (model and commands checked)."""
        outer = event.get('response')
        if self.sent or not isinstance(outer, dict) or outer.get('request_id') != INIT_REQUEST_ID:
            self.fail('gold_unexpected_event')
        inner = outer.get('response')
        if outer.get('subtype') != 'success' or not isinstance(inner, dict):
            raise CallFailure('pre_send', 'gold_initialize_failed')
        if not _models_ok(inner, self.request):
            raise CallFailure('host', tiers.UNRESOLVED)
        if inner.get('commands') != []:
            raise CallFailure('host', isolation.REASON, isolation_state='violated',
                              detail='control_commands')
        # Only now does any payload byte leave this process.
        self.sent = True
        return True

    def inspect(self, event):
        try:
            metadata = claude_adapter.inspect_event(
                event, inventory_ids=isolation.CLAUDE_BUILTIN_PLUGINS,
                allow_security_policy=True, model_ok=tiers.claude_model_ok)
        except claude_adapter.StartupRejected as exc:
            if str(exc) == 'unexpected_resolved_model':
                raise CallFailure('unknown' if self.sent else 'host', tiers.UNRESOLVED)
            self.violation(str(exc))
        except (ValueError, TypeError, AttributeError) as exc:
            self.violation(str(exc) if isinstance(exc, ValueError) else 'unsafe_event')
        stream = event.get('event')
        if (isinstance(stream, dict) and isinstance(stream.get('content_block'), dict)
                and stream['content_block'].get('type') in ('tool_use', 'server_tool_use', 'tool_result')):
            self.violation('unexpected_tool_block')
        return metadata

    def assistant(self, event):
        if isinstance(event.get('error'), str):
            self.assistant_error = event['error'][:40]
            return
        message = event.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and any(
                isinstance(block, dict) and block.get('type') == 'text' and block.get('text')
                for block in content):
            self.answered = True

    def result(self, event):
        if event.get('is_error') is not False or event.get('subtype') != 'success':
            code = _claude_failure(event, self.assistant_error)
            raise CallFailure(_classify(code, self.answered), code)
        turns = event.get('num_turns')
        if turns is not None and (type(turns) is not int or turns != 1):
            raise CallFailure('unknown', 'gold_turn_budget')
        if not isinstance(event.get('result'), str):
            raise CallFailure('unknown', 'gold_incomplete')
        self.text = event['result']
        raw_usage = event.get('usage')
        self.usage = ({k: v for k, v in raw_usage.items()
                       if type(v) is int and v >= 0 and len(k) <= 40}
                      if isinstance(raw_usage, dict) else {})
        self.completed = True

    def line(self, line):
        """Handle one stdout line; returns 'send' or 'done' when stdin must change."""
        if self.completed:
            raise CallFailure('unknown', 'gold_event_after_completion')
        try:
            event = json.loads(line, object_pairs_hook=_unique)
        except ValueError:
            self.fail('gold_invalid_event')
        if not isinstance(event, dict) or event.get('type') not in _CLAUDE_KNOWN:
            self.fail('gold_unexpected_event')
        kind = event['type']
        if kind in ('keep_alive', 'rate_limit_event'):
            return None
        if kind == 'control_request':
            self.violation('unexpected_control_request')
        if kind == 'control_response':
            return 'send' if self.control_response(event) else None
        metadata = self.inspect(event)
        if metadata:
            if self.initialized:
                self.fail('gold_duplicate_init')
            self.initialized = True
            self.init_model = metadata['resolved_model']
            return None
        if kind == 'system' or not self.sent:
            self.fail('gold_unexpected_event')
        if kind == 'assistant':
            self.assistant(event)
            return None
        if kind == 'result':
            self.result(event)
            return 'done'
        return None


def invoke_claude(plan, raw, *, seconds, max_output, monitor, guard, before_launch,
                  after_launch):
    """Initialize, check the resolved model and empty commands, then send the body."""
    run = _Run(plan, seconds=seconds, max_output=max_output, monitor=monitor, guard=guard)
    session = _ClaudeSession(raw)
    try:
        run.launch(before_launch, after_launch)
        process = run.process
        pending, offset, buffer = session.init, 0, b''
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
            while 'output' in {key.data for key in selector.get_map().values()}:
                run.tick(session.sent)
                for key, _ in selector.select(.05):
                    if key.data == 'input':
                        if offset < len(pending):
                            try:
                                offset += os.write(key.fd, pending[offset:])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                session.fail('gold_incomplete')
                        if offset == len(pending):
                            selector.unregister(key.fileobj)
                        continue
                    chunk = run.read(key.fd, session.sent)
                    if chunk is None:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer += chunk
                    for line, rest in _lines(buffer):
                        if line is None:
                            buffer = rest
                            break
                        change = session.line(line)
                        if change == 'send':
                            pending, offset = session.body, 0
                            if key_for(selector, 'input') is None:
                                selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
                        elif change == 'done':
                            # The single answer is in; end the input stream so the CLI exits.
                            entry = key_for(selector, 'input')
                            if entry is not None:
                                selector.unregister(entry.fileobj)
                            process.stdin.close()
        if buffer.strip() or not session.completed or session.text is None:
            session.fail('gold_incomplete')
        if not session.initialized or not tiers.claude_model_ok(session.init_model):
            raise CallFailure('unknown', tiers.UNRESOLVED)
        run.finish(True)
        answer = _parse_answer(session.text)
    finally:
        run.close()
    return {'answer': answer, 'usage': session.usage, 'toolWarnings': [],
            'elapsed_seconds': round(time.monotonic() - run.began, 3),
            'model': session.init_model, 'modelReported': True}


def key_for(selector, tag):
    for key in selector.get_map().values():
        if key.data == tag:
            return key
    return None


def invoke(plan, raw, **options):
    if plan['host'] == 'claude':
        return invoke_claude(plan, raw, **options)
    return invoke_codex(plan, raw, **options)
