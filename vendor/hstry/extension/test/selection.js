// Source metadata controls selection; titles, dates and account names never do.
import assert from 'node:assert/strict';
import {
  CAPTURE_TYPES, DEFAULT_CAPTURE_TYPES, normalizeCaptureTypes,
  classifyConversation, selectConversation,
} from '../providers/selection.js';

assert.deepEqual(Object.keys(CAPTURE_TYPES), ['projects', 'pinned', 'agents', 'recents']);
assert.deepEqual(DEFAULT_CAPTURE_TYPES, { projects: true, pinned: true, agents: true, recents: false });
assert.deepEqual(normalizeCaptureTypes(), DEFAULT_CAPTURE_TYPES);
assert.deepEqual(normalizeCaptureTypes({ projects: false, recents: true }), { projects: false, pinned: true, agents: true, recents: true });
assert.deepEqual(normalizeCaptureTypes({ recents: 'true', projects: null }), DEFAULT_CAPTURE_TYPES);
assert.equal(selectConversation({ id: 'ordinary' }).selected, false);
assert.equal(selectConversation({ id: 'ordinary' }, { recents: true }).selected, true);
assert.equal(selectConversation({ title: 'Project: pinned GPT agent', updated_at: '2026-11-11', account: 'workspace' }).selected, false);
for (const metadata of [{ project_id: 'p' }, { folder_id: 'f' }, { project: { uuid: 'p' } }]) {
  assert.equal(selectConversation(metadata).selected, true);
  assert.ok(classifyConversation(metadata).categories.includes('projects'));
  assert.equal(selectConversation(metadata, { projects: false, pinned: false, agents: false }).selected, false);
}
for (const metadata of [{ is_starred: true }, { is_pinned: true }, { favorite: true }, { is_favorite: true }, { pinned_time: '2026-11-11T00:00:00Z' }]) {
  assert.equal(selectConversation(metadata).selected, true);
  assert.ok(classifyConversation(metadata).categories.includes('pinned'));
}
for (const metadata of [{ gizmo_id: 'g-custom' }, { gpt_id: 'gpt-custom' }, { agent_id: 'agent-custom' }]) {
  assert.equal(selectConversation(metadata).selected, true);
  assert.ok(classifyConversation(metadata).categories.includes('agents'));
}
assert.equal(selectConversation({ project_id: 'p', in_recents: true }, { recents: false }).selected, true);
for (const metadata of [{ project_id: 'p', in_recents: true }, { is_starred: true }, { gizmo_id: 'g-custom' }]) {
  assert.equal(selectConversation(metadata, { projects: false, pinned: false, agents: false, recents: true }).selected, false);
}
assert.equal(selectConversation({ project_id: 'p', is_starred: true }, { projects: false, pinned: true, agents: false }).selected, true);
assert.equal(selectConversation({ is_starred: 'true', project: {}, agent_id: '' }).selected, false);
assert.equal(classifyConversation({ id: 'unknown' }).unknown, true);
assert.equal(classifyConversation({ project_id: null, gizmo_id: null, is_starred: false }).unknown, false);
assert.deepEqual(classifyConversation({ gizmo_id: 'g-project' }, { projectGizmoIds: new Set(['g-project']), recents: false }).categories, ['projects']);
console.log('PASS category defaults, explicit metadata, independent choices, overlap and unknowns');
