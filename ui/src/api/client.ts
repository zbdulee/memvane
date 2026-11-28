// Same-origin JSON client. Every /api/* request carries `X-MemVane-UI: 1`
// (the server's CSRF header gate) and the HttpOnly session cookie.

import type {
  ApiErrorBody,
  AuthExchangeResponse,
  ErrorsResponse,
  InstallId,
  OverviewResponse,
  SessionMeta,
} from './types';

export const UI_HEADER = 'X-MemVane-UI';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function isErrorBody(value: unknown): value is ApiErrorBody {
  return typeof value === 'object' && value !== null
    && typeof (value as {code?: unknown}).code === 'string'
    && typeof (value as {error?: unknown}).error === 'string';
}

export function uiHeaders(extra?: Record<string, string>): Headers {
  const headers = new Headers(extra);
  headers.set(UI_HEADER, '1');
  return headers;
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!path.startsWith('/api/')) throw new Error(`not an API path: ${path}`);
  const headers = new Headers(init.headers);
  headers.set(UI_HEADER, '1');
  return fetch(path, {...init, headers, credentials: 'same-origin', cache: 'no-store'});
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    if (isErrorBody(body)) throw new ApiError(response.status, body.code, body.error);
    throw new ApiError(response.status, `http_${response.status}`, `HTTP ${response.status}`);
  }
  return body as T;
}

function postJson<T>(path: string, payload: unknown): Promise<T> {
  return requestJson<T>(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
}

const INSTALL_ID = /^[0-9a-f]{32}$/;

function installPath(installId: InstallId): string {
  if (!INSTALL_ID.test(installId)) throw new Error('invalid installId');
  return `/api/installs/${installId}`;
}

// ---------------------------------------------------------------- one-time code exchange

/** The values the launcher put in `#code=...&target=...&tab=...`. */
export interface LaunchHash {
  code: string | null;
  target: string | null;
  tab: string | null;
}

export function parseLaunchHash(hash: string): LaunchHash {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash;
  if (!/(^|&)code=/.test(raw)) return {code: null, target: null, tab: null};
  const params = new URLSearchParams(raw);
  return {code: params.get('code'), target: params.get('target'), tab: params.get('tab')};
}

export type ExchangeOutcome = 'none' | 'exchanged' | 'rejected' | 'failed';

/**
 * Exchanges a `#code=` one-time code for the session cookie, then removes the
 * code from the address bar and history as soon as the server has answered
 * (success or rejection), so the code never lingers in history or bookmarks.
 * `routeHash` builds the app route kept in its place.
 */
export async function exchangeLaunchCode(routeHash: (launch: LaunchHash) => string): Promise<ExchangeOutcome> {
  const launch = parseLaunchHash(window.location.hash);
  if (launch.code === null) return 'none';
  const scrub = () => {
    const {pathname, search} = window.location;
    window.history.replaceState(null, '', `${pathname}${search}${routeHash(launch)}`);
  };
  let response: Response;
  try {
    response = await apiFetch('/api/auth/exchange', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: launch.code}),
    });
  } catch {
    // No answer: the code cannot be reused safely either, so drop it.
    scrub();
    return 'failed';
  }
  scrub();
  if (!response.ok) return 'rejected';
  try {
    const body = (await response.json()) as AuthExchangeResponse;
    return body.ok === true ? 'exchanged' : 'rejected';
  } catch {
    return 'rejected';
  }
}

// ---------------------------------------------------------------- endpoints

export function getSession(): Promise<SessionMeta> {
  return requestJson<SessionMeta>('/api/session');
}

export function getOverview(): Promise<OverviewResponse> {
  return requestJson<OverviewResponse>('/api/overview');
}

export function reconcileOverview(): Promise<OverviewResponse> {
  return postJson<OverviewResponse>('/api/overview/reconcile', {});
}

export function getInstallErrors(installId: InstallId, opts: {details: boolean; hours?: number}): Promise<ErrorsResponse> {
  const query = new URLSearchParams({hours: String(opts.hours ?? 24), details: opts.details ? '1' : '0'});
  return requestJson<ErrorsResponse>(`${installPath(installId)}/errors?${query.toString()}`);
}
