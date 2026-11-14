// The journal domain's Python tests, one node test per tier (tests/support/runner.mjs).
import { runDomain } from '../../../tests/support/runner.mjs';

runDomain({ domain: 'journal' });
