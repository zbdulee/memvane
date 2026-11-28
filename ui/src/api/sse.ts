// One SSE stream per browser profile. The tab holding the Web Lock
// `memvane-ui-sse-leader` reads /api/events/stream with fetch (EventSource
// cannot send `X-MemVane-UI`) and rebroadcasts every event on the
// `memvane-ui-sse` BroadcastChannel; other tabs only listen. Without Web Locks
// the tabs elect a leader by BroadcastChannel heartbeats.

import {apiFetch} from './client';
import type {SseEvent, SseEventType} from './types';

export type SseStatus =
  | 'connecting'
  | 'leader'
  | 'follower'
  | 'reconnecting'
  | 'draining'
  | 'rate_limited'
  | 'unauthorized';

export const LOCK_NAME = 'memvane-ui-sse-leader';
export const CHANNEL_NAME = 'memvane-ui-sse';
const STREAM_PATH = '/api/events/stream';
const RETRY_MIN_MS = 1000;
const RETRY_MAX_MS = 30000;
const HEARTBEAT_MS = 1000;
const LEADER_TIMEOUT_MS = 3500;

const EVENT_TYPES: ReadonlySet<SseEventType> = new Set<SseEventType>([
  'heartbeat', 'session_started', 'events_updated', 'runtime_updated', 'server_draining',
]);

type ChannelMessage =
  | {kind: 'event'; event: SseEvent}
  | {kind: 'stopped'; status: 'draining' | 'rate_limited' | 'unauthorized'}
  | {kind: 'leader-heartbeat'; tabId: string};

/** Statuses after which no tab reconnects on its own. */
const TERMINAL: ReadonlySet<SseStatus> = new Set<SseStatus>(['draining', 'rate_limited', 'unauthorized']);

// ---------------------------------------------------------------- frame parser

export interface SseFrame {
  event: string;
  data: string;
}

/**
 * Incremental parser for the text/event-stream format: feed decoded chunks,
 * receive each complete frame. Comment lines and `id`/`retry` fields are
 * ignored; a frame without `data` is dropped.
 */
export class SseParser {
  private buffer = '';
  private eventName = '';
  private dataLines: string[] = [];

  push(chunk: string, emit: (frame: SseFrame) => void): void {
    this.buffer += chunk;
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (match === null) return;
      // A lone `\r` at the end may be the first half of `\r\n`.
      if (match[0] === '\r' && match.index === this.buffer.length - 1) return;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      this.line(line, emit);
    }
  }

  private line(line: string, emit: (frame: SseFrame) => void): void {
    if (line === '') {
      if (this.dataLines.length > 0) {
        emit({event: this.eventName || 'message', data: this.dataLines.join('\n')});
      }
      this.eventName = '';
      this.dataLines = [];
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.eventName = value;
    else if (field === 'data') this.dataLines.push(value);
  }
}

export function toSseEvent(frame: SseFrame): SseEvent | null {
  if (!EVENT_TYPES.has(frame.event as SseEventType)) return null;
  let data: unknown;
  try {
    data = JSON.parse(frame.data);
  } catch {
    return null;
  }
  if (typeof data !== 'object' || data === null) return null;
  return {type: frame.event, data} as SseEvent;
}

// ---------------------------------------------------------------- coordinator

export type SseListener = (event: SseEvent) => void;
export type StatusListener = (status: SseStatus) => void;

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      resolve();
    }, {once: true});
  });
}

export class SseCoordinator {
  private status: SseStatus = 'connecting';
  private readonly events = new Set<SseListener>();
  private readonly statuses = new Set<StatusListener>();
  private readonly stop = new AbortController();
  private readonly tabId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  private channel: BroadcastChannel | null = null;
  private started = false;

  onEvent(listener: SseListener): () => void {
    this.events.add(listener);
    return () => this.events.delete(listener);
  }

  onStatus(listener: StatusListener): () => void {
    this.statuses.add(listener);
    listener(this.status);
    return () => this.statuses.delete(listener);
  }

  current(): SseStatus {
    return this.status;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    if (typeof BroadcastChannel === 'function') {
      this.channel = new BroadcastChannel(CHANNEL_NAME);
      this.channel.addEventListener('message', message => this.fromChannel(message.data as ChannelMessage));
    }
    const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined;
    if (locks !== undefined && typeof locks.request === 'function') {
      this.setStatus('follower');
      // The lock is held for as long as the callback's promise is pending, and
      // the browser releases it when this tab closes.
      void locks.request(LOCK_NAME, {signal: this.stop.signal}, () => this.lead()).catch(() => undefined);
    } else if (this.channel !== null) {
      this.setStatus('follower');
      void this.electByHeartbeat();
    } else {
      void this.lead();
    }
  }

  dispose(): void {
    this.stop.abort();
    this.channel?.close();
    this.channel = null;
  }

  private setStatus(status: SseStatus): void {
    if (this.status === status) return;
    if (TERMINAL.has(this.status) && !TERMINAL.has(status)) return;
    this.status = status;
    for (const listener of this.statuses) listener(status);
  }

  private dispatch(event: SseEvent): void {
    for (const listener of this.events) listener(event);
  }

  private post(message: ChannelMessage): void {
    try {
      this.channel?.postMessage(message);
    } catch {
      // A closed channel only stops the rebroadcast; this tab still has the event.
    }
  }

  private fromChannel(message: ChannelMessage): void {
    if (message.kind === 'event') {
      if (this.status !== 'leader') this.dispatch(message.event);
      if (message.event.type === 'server_draining' && this.status !== 'leader') this.setStatus('draining');
    } else if (message.kind === 'stopped') {
      if (this.status !== 'leader') this.setStatus(message.status);
    } else if (message.kind === 'leader-heartbeat') {
      this.lastHeartbeat = Date.now();
      this.lastHeartbeatFrom = message.tabId;
    }
  }

  private lastHeartbeat = 0;
  private lastHeartbeatFrom = '';

  /** Fallback election without Web Locks: lead when no heartbeat arrives in time. */
  private async electByHeartbeat(): Promise<void> {
    const signal = this.stop.signal;
    await delay(LEADER_TIMEOUT_MS * Math.random(), signal);
    while (!signal.aborted) {
      if (Date.now() - this.lastHeartbeat > LEADER_TIMEOUT_MS) {
        const beat = window.setInterval(() => this.post({kind: 'leader-heartbeat', tabId: this.tabId}), HEARTBEAT_MS);
        this.post({kind: 'leader-heartbeat', tabId: this.tabId});
        try {
          await this.lead(() => this.lastHeartbeatFrom !== '' && this.lastHeartbeatFrom < this.tabId
            && Date.now() - this.lastHeartbeat < LEADER_TIMEOUT_MS);
        } finally {
          window.clearInterval(beat);
        }
        if (TERMINAL.has(this.status)) return;
        this.setStatus('follower');
      }
      await delay(HEARTBEAT_MS, signal);
    }
  }

  /**
   * Holds the stream as leader until a terminal status or disposal. `yieldTo`
   * lets the heartbeat election hand leadership to a tab with a lower id.
   */
  private async lead(yieldTo: () => boolean = () => false): Promise<void> {
    const signal = this.stop.signal;
    let backoff = RETRY_MIN_MS;
    this.setStatus('leader');
    while (!signal.aborted && !TERMINAL.has(this.status)) {
      if (yieldTo()) return;
      const result = await this.readStream(signal, yieldTo);
      if (result === 'terminal' || signal.aborted || yieldTo()) return;
      if (result === 'opened') backoff = RETRY_MIN_MS;
      this.setStatus('reconnecting');
      await delay(backoff, signal);
      backoff = Math.min(backoff * 2, RETRY_MAX_MS);
    }
    // Keep the lock while stopped: a successor would hit the same 429 or drain.
    if (TERMINAL.has(this.status)) await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), {once: true}));
  }

  private terminate(status: 'draining' | 'rate_limited' | 'unauthorized'): 'terminal' {
    this.setStatus(status);
    this.post({kind: 'stopped', status});
    return 'terminal';
  }

  private async readStream(signal: AbortSignal, yieldTo: () => boolean): Promise<'terminal' | 'opened' | 'failed'> {
    const abort = new AbortController();
    const onStop = () => abort.abort();
    signal.addEventListener('abort', onStop, {once: true});
    try {
      const response = await apiFetch(STREAM_PATH, {headers: {Accept: 'text/event-stream'}, signal: abort.signal});
      if (response.status === 429) return this.terminate('rate_limited');
      if (response.status === 401 || response.status === 403) return this.terminate('unauthorized');
      if (!response.ok || response.body === null) return 'failed';
      this.setStatus('leader');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseParser();
      let terminal = false;
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, {stream: true}), frame => {
          const event = toSseEvent(frame);
          if (event === null) return;
          this.dispatch(event);
          this.post({kind: 'event', event});
          if (event.type === 'server_draining') terminal = true;
        });
        if (terminal) {
          abort.abort();
          return this.terminate('draining');
        }
        if (yieldTo()) {
          abort.abort();
          return 'opened';
        }
      }
      return 'opened';
    } catch {
      return 'failed';
    } finally {
      signal.removeEventListener('abort', onStop);
    }
  }
}
