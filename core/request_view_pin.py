"""Immutable per-request pin of the selected QMD route, endpoint and scope.

One recall request can query more than once (primary, then hierarchical raw
backfill). The pin is captured once, before the first query, so every phase
takes the same selected CLI index paths (shared or project-local) and the
same shared endpoint URL when one is selected. The final pre-output check
compares a fresh observation against it.

Scope of the guarantee: the pin fixes *which* route/endpoint/config identity a
request uses and rejects a change of that identity. It does not make the
active QMD database bytes immutable (workers still update it in place), and it
never launches, stops or reloads a daemon. Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
from typing import NamedTuple

import published_view


ROUTE_KIND_SHARED = 'shared'
ROUTE_KIND_PROJECT_LOCAL = 'project_local'
# The paths a selected CLI query receives (qmd_project_query.query). Kept as a
# tuple of pairs inside the pin; callers get a fresh dict on every call.
LOCAL_INDEX_KEYS = ('INDEX_PATH', 'QMD_CONFIG_DIR', 'XDG_CACHE_HOME')


class RequestViewPin(NamedTuple):
    identity: tuple
    route_kind: str
    endpoint_url: str | None
    selected_index: tuple
    route_sha256: str

    def index_paths(self) -> dict:
        """Fresh selected CLI paths for either shared or project-local QMD."""
        return dict(self.selected_index)

    def project_index(self) -> dict:
        """A new dict of the pinned project-local paths; mutation cannot reach the pin."""
        if self.route_kind != ROUTE_KIND_PROJECT_LOCAL:
            raise ValueError('request_view_pin_not_project_local')
        return self.index_paths()

    def require_url(self, url) -> str:
        """Return the pinned shared endpoint URL only when ``url`` is exactly it."""
        if self.route_kind != ROUTE_KIND_SHARED or not self.endpoint_url:
            raise ValueError('request_view_pin_not_shared')
        if not isinstance(url, str) or url != self.endpoint_url:
            raise ValueError('request_view_pin_url_mismatch')
        return self.endpoint_url


def _immutable(value):
    # request_identity copies values into tuples, but a list/dict value would
    # still be shared with the caller. Hashability is the cheap, total check.
    try:
        hash(value)
    except TypeError:
        raise ValueError('request_view_pin_invalid') from None
    return value


def capture(route, endpoint, project_root, config) -> RequestViewPin:
    """Pin one request's selected route. Raises ValueError on any incomplete input."""
    if not isinstance(route, dict) or route.get('selected') is not True:
        raise ValueError('request_view_pin_invalid')
    if route.get('shared') is not None and type(route.get('shared')) is not bool:
        raise ValueError('request_view_pin_invalid')
    if not isinstance(project_root, str) or not project_root:
        raise ValueError('request_view_pin_invalid')
    try:
        identity = published_view.request_identity(route, endpoint, project_root, config)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('request_view_pin_invalid') from None
    _immutable(identity)
    selected_index = tuple((key, route.get(key)) for key in LOCAL_INDEX_KEYS)
    if any(not isinstance(value, str) or not value for _, value in selected_index):
        raise ValueError('request_view_pin_invalid')
    try:
        route_sha256 = hashlib.sha256(json.dumps(
            route, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            allow_nan=False).encode('utf8')).hexdigest()
    except (ValueError, TypeError):
        raise ValueError('request_view_pin_invalid') from None
    if route.get('shared') is True:
        url = endpoint.get('url')
        if not isinstance(url, str) or not url:
            raise ValueError('request_view_pin_invalid')
        return RequestViewPin(identity, ROUTE_KIND_SHARED, url,
                              selected_index, route_sha256)
    return RequestViewPin(identity, ROUTE_KIND_PROJECT_LOCAL, None,
                          selected_index, route_sha256)


def same_view(pin, route, endpoint, project_root, config) -> bool:
    """True only when a fresh observation has the pinned identity; fails closed."""
    if not isinstance(pin, RequestViewPin):
        return False
    try:
        observed = capture(route, endpoint, project_root, config)
    except ValueError:
        return False
    return observed == pin
