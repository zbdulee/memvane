// The load scale of a test run: how much slower this machine starts a Python
// process right now than the reference, between 1.0 and 4.0.
//
// MEMVANE_TEST_TIME_SCALE carries it to every child of a fixture run
// (`fixtureEnv`); a value already in the environment is kept. Test waits and
// timeouts multiply by it (`scaled`, `waitUntil`, tests/support/case.py and the
// module timeout of runner.mjs). Product code never reads it.
import { spawnSync } from 'node:child_process';

export const TIME_SCALE_ENV = 'MEMVANE_TEST_TIME_SCALE';
// Median wall time of `python3 -B -c pass` on an idle Apple M5 Pro: 18-22 ms.
export const REFERENCE_START_MS = 25;
export const SCALE_MIN = 1;
export const SCALE_MAX = 4;
const PROBES = 5;
const PAUSE = new Int32Array(new SharedArrayBuffer(4));

export function clampScale(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return SCALE_MIN;
  return Math.min(SCALE_MAX, Math.max(SCALE_MIN, Math.round(number * 100) / 100));
}

/** The scale of start times `samplesMs` (their median over the reference). */
export function scaleOf(samplesMs) {
  const sorted = samplesMs.filter((value) => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
  if (sorted.length === 0) return SCALE_MIN;
  return clampScale(sorted[Math.floor(sorted.length / 2)] / REFERENCE_START_MS);
}

function probe() {
  const samples = [];
  for (let index = 0; index < PROBES; index += 1) {
    const started = process.hrtime.bigint();
    const r = spawnSync('python3', ['-B', '-c', 'pass'], { stdio: 'ignore', timeout: 30000 });
    if (r.status === 0) samples.push(Number(process.hrtime.bigint() - started) / 1e6);
  }
  return samples;
}

let cached;

/** This run's scale: the environment value when set, otherwise measured once per process. */
export function timeScale(env = process.env) {
  const given = env[TIME_SCALE_ENV];
  if (given !== undefined && given !== '') return clampScale(given);
  if (cached === undefined) cached = scaleOf(probe());
  return cached;
}

/** `value` (seconds or milliseconds) multiplied by this run's scale. */
export function scaled(value) {
  return value * timeScale();
}

/** Poll `predicate` in this process until it is truthy (returned) or the scaled timeout passes. */
export function waitUntil(predicate, { timeoutMs = 10000, intervalMs = 20 } = {}) {
  const end = Date.now() + scaled(timeoutMs);
  for (;;) {
    const value = predicate();
    if (value) return value;
    if (Date.now() >= end) throw new Error(`condition not reached within ${scaled(timeoutMs)} ms`);
    Atomics.wait(PAUSE, 0, 0, intervalMs);
  }
}

/** `waitUntil` that yields to the event loop between polls (child processes are reaped). */
export async function waitUntilAsync(predicate, { timeoutMs = 10000, intervalMs = 20 } = {}) {
  const end = Date.now() + scaled(timeoutMs);
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() >= end) throw new Error(`condition not reached within ${scaled(timeoutMs)} ms`);
    await new Promise((resolve) => { setTimeout(resolve, intervalMs); });
  }
}
