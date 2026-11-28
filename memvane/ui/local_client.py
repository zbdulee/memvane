"""A loopback HTTP/1.1 control client over a raw socket (``Connection: close``).

Only ``127.0.0.1`` is ever dialled. Responses are read to the end of the
connection (bounded) and cut at their ``Content-Length``.
"""
from __future__ import annotations

import json
import socket

from memvane.ui.types import BIND_HOST, CONTROL_HEADER, UI_HEADER

MAX_RESPONSE = 2 * 1024 * 1024


class LocalClientError(OSError):
    """The local server could not be reached or answered malformed bytes."""


def request(port, method, path, *, headers=None, body=None, timeout=5.0):
    """``(status, headers, body bytes)`` of one request to ``127.0.0.1:<port>``."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise LocalClientError('ui_port_invalid')
    if not path.startswith('/') or any(ch in path for ch in '\r\n '):
        raise LocalClientError('ui_path_invalid')
    data = b'' if body is None else body
    lines = [f'{method} {path} HTTP/1.1', f'Host: {BIND_HOST}:{port}', 'Connection: close',
             f'Content-Length: {len(data)}']
    for name, value in (headers or {}).items():
        if any(ch in f'{name}{value}' for ch in '\r\n'):
            raise LocalClientError('ui_header_invalid')
        lines.append(f'{name}: {value}')
    raw = ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + data
    try:
        with socket.create_connection((BIND_HOST, port), timeout=timeout) as conn:
            conn.sendall(raw)
            chunks, size = [], 0
            while size <= MAX_RESPONSE:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
    except OSError as exc:
        raise LocalClientError('ui_server_unreachable') from exc
    return parse_response(b''.join(chunks))


def parse_response(raw):
    head, sep, rest = raw.partition(b'\r\n\r\n')
    if not sep:
        raise LocalClientError('ui_response_malformed')
    lines = head.decode('latin-1').split('\r\n')
    parts = lines[0].split(' ', 2)
    if len(parts) < 2 or not parts[0].startswith('HTTP/1.') or not parts[1].isdigit():
        raise LocalClientError('ui_response_malformed')
    found = {}
    for line in lines[1:]:
        name, colon, value = line.partition(':')
        if colon:
            found.setdefault(name.strip().lower(), []).append(value.strip())
    length = found.get('content-length')
    if length and length[0].isdigit():
        rest = rest[:int(length[0])]
    return int(parts[1]), found, rest


def control(server, method, path, payload=None, *, timeout=5.0):
    """``(status, JSON value or None)`` of an internal endpoint, authenticated by the control secret."""
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    headers = {CONTROL_HEADER: server['controlSecret'], UI_HEADER: '1'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    status, _headers, data = request(server['port'], method, path, headers=headers, body=body,
                                     timeout=timeout)
    try:
        value = json.loads(data.decode('utf-8')) if data else None
    except (UnicodeDecodeError, ValueError):
        value = None
    return status, value
