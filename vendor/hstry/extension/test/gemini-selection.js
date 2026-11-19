import assert from 'node:assert/strict';
import { DEFAULT_CAPTURE_TYPES, providerState } from '../ui.js';

const captureTypes = { ...DEFAULT_CAPTURE_TYPES };
const context = { apiConnected: true, providerId: 'gemini', captureTypes };
assert.equal(
  providerState(undefined, true, context).tone,
  'warning',
  'unsupported Gemini selection must be explained before the first capture',
);
assert.equal(
  providerState({ lastRunMs: 1, capturePolicy: captureTypes }, true, context).tone,
  'warning',
);
assert.equal(providerState(undefined, false, context).tone, 'muted');
assert.equal(providerState(undefined, true, { ...context, apiConnected: false }).tone, 'muted');
assert.equal(providerState(undefined, true, { ...context, providerId: 'chatgpt' }).tone, 'idle');
assert.equal(
  providerState(undefined, true, { ...context, captureTypes: { ...captureTypes, recents: true } })
    .tone,
  'idle',
);
assert.equal(captureTypes.recents, false, 'presentation cannot enable Recents');

console.log('PASS Gemini provider state remains conservative.');
