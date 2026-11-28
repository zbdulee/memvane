"""Values of the local web dashboard: state names, limits, headers, target ids (no I/O)."""
from __future__ import annotations

import hashlib
import re

UI_DIR = 'ui'
SERVER_FILE = 'server.json'
LOCK_FILE = 'server.lock'
PREFERENCES_FILE = 'preferences.json'
AUTO_OPEN_FILE = 'auto-open-state.json'

SERVER_SCHEMA = 'memvane-ui-server-v1'
PREFERENCES_SCHEMA = 'memvane-ui-preferences-v1'
AUTO_OPEN_SCHEMA = 'memvane-ui-auto-open-v1'

SERVER_KEYS = ('schema', 'pid', 'port', 'bundleRoot', 'bundleVersion', 'startedAt',
               'instanceNonce', 'controlSecret')

DEFAULT_PREFERENCES = {
    'schema': PREFERENCES_SCHEMA,
    'autoOpenOnSessionStart': False,
    'autoOpenScope': 'onboarded_sessions',
    'idleShutdownMinutes': 30,
    'autoOpenCooldownMinutes': 240,
    'reconnectGraceSeconds': 10,
    'readOnlyDefault': True,
}
# Integer preferences: (minimum, maximum).
PREFERENCE_RANGES = {
    'idleShutdownMinutes': (1, 1440),
    'autoOpenCooldownMinutes': (0, 10080),
    'reconnectGraceSeconds': (1, 300),
}
PREFERENCE_BOOLS = ('autoOpenOnSessionStart', 'readOnlyDefault')
AUTO_OPEN_SCOPES = ('onboarded_sessions',)

BIND_HOST = '127.0.0.1'
SESSION_COOKIE = 'memvane_ui_session'
MAX_SESSIONS = 16
MAX_PENDING_CODES = 32
MAX_SSE_CLIENTS = 16
MAX_WORKERS = 32
MAX_BODY_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
CODE_TTL_SECONDS = 60
MANUAL_CODE_TTL_SECONDS = 120
CODE_TTLS = (CODE_TTL_SECONDS, MANUAL_CODE_TTL_SECONDS)
HEARTBEAT_SECONDS = 15
WATCH_SECONDS = 2
CONTROL_HEADER = 'X-MemVane-Control'
UI_HEADER = 'X-MemVane-UI'
TABS = ('overview', 'recall', 'wiki', 'runtime')

SSE_EVENTS = ('heartbeat', 'session_started', 'events_updated', 'runtime_updated', 'server_draining')
DRAIN_REASONS = ('bundle_upgraded', 'user_stop', 'idle')

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = (
    ('Cross-Origin-Resource-Policy', 'same-origin'),
    ('X-Content-Type-Options', 'nosniff'),
    ('Cache-Control', 'no-store'),
    ('Referrer-Policy', 'no-referrer'),
)

STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.txt': 'text/plain; charset=utf-8',
}
MAX_STATIC_BYTES = 4 * 1024 * 1024

INSTALL_ID = re.compile(r'[0-9a-f]{32}')
TARGET_ID = re.compile(r'([0-9a-f]{32})(?::([0-9a-f]{12}))?')
HEAD_LIMIT = 4096
MAX_RECALLS = 500
MAX_HOURS = 24 * 30
CARD_SCAN_LIMIT = 4096

# Overview banner: event reasons and notes worth a warning, by exact code or prefix.
BANNER_CODES = ('needs_extractor', 'candidate_storage_full', 'selected_recall_settings_changed',
                'reader_settings_resealing', 'worker_attestation_unavailable', 'attestation_invalid',
                'view_preparing', 'laya_vector_selected_provider_start_failed')
BANNER_PREFIXES = ('laya_vector_',)


def worktree_hash(target_root):
    """The first 12 hex of the SHA-256 of a canonical target root."""
    return hashlib.sha256(str(target_root).encode('utf-8', 'surrogateescape')).hexdigest()[:12]


def target_id(install_id, target_root, kind):
    """``<installId>`` for a main checkout, ``<installId>:<wtHash12>`` for a linked worktree."""
    if kind == 'worktree':
        return f'{install_id}:{worktree_hash(target_root)}'
    return install_id


def parse_target_id(value):
    """``(install id, worktree hash or None)`` of a target id, or None."""
    if not isinstance(value, str):
        return None
    match = TARGET_ID.fullmatch(value)
    return (match.group(1), match.group(2)) if match else None
