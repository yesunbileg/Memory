// Shared capture policy. Only explicit provider metadata or verified collection
// membership may identify organized conversations. Never inspect transcript text.

export const CAPTURE_TYPES = Object.freeze({
  projects: { name: 'Projects and folders', label: 'Projects and folders', description: 'Conversations explicitly assigned to a project or folder.' },
  pinned: { name: 'Pinned and favorites', label: 'Pinned and favorites', description: 'Conversations marked pinned, starred or favorite by the provider.' },
  agents: { name: 'GPTs and agents', label: 'GPTs and agents', description: 'Conversations with an explicit custom GPT or agent identifier.' },
  recents: { name: 'Ordinary Recents', label: 'Ordinary Recents', description: 'Include the ordinary conversation list, including chats with unavailable category metadata.' },
});

export const DEFAULT_CAPTURE_TYPES = Object.freeze({ projects: true, pinned: true, agents: true, recents: false });

export function normalizeCaptureTypes(values) {
  return Object.fromEntries(Object.entries(DEFAULT_CAPTURE_TYPES).map(([key, fallback]) => [
    key, typeof values?.[key] === 'boolean' ? values[key] : fallback,
  ]));
}

const PROJECT_IDS = ['project_id', 'project_uuid', 'folder_id', 'folder_uuid'];
const PIN_FLAGS = ['is_pinned', 'pinned', 'is_starred', 'starred', 'is_favorite', 'favorite', 'is_favorited'];
const AGENT_IDS = ['gizmo_id', 'gpt_id', 'agent_id', 'custom_gpt_id', 'assistant_id'];
const identifier = value => typeof value === 'string' && value.length > 0;
const objectId = value => value && typeof value === 'object' && !Array.isArray(value) && (identifier(value.id) || identifier(value.uuid));

/**
 * Return {categories, unknown, unavailable} from source-owned conversation metadata.
 * context.projectId / projectGizmoIds / pinned come only from successful known
 * collection endpoints. context.recents marks the ordinary-list discovery path;
 * Recents is a fallback category only when no organized membership is explicit.
 * Display in that list cannot override a deselected organized category. Null/false metadata never
 * counts as positive membership. Opaque arrays are deliberately unclassified.
 */
export function classifyConversation(raw, context = {}) {
  const sources = raw && typeof raw === 'object' && !Array.isArray(raw)
    ? [raw, raw.metadata].filter(value => value && typeof value === 'object' && !Array.isArray(value))
    : [];
  const categories = new Set();
  let projectKnown = context.projectMembershipAvailable === true;
  let pinKnown = context.pinnedMembershipAvailable === true;
  let agentKnown = false;
  if (identifier(context.projectId)) categories.add('projects');
  if (context.pinned === true) categories.add('pinned');
  for (const source of sources) {
    projectKnown ||= PROJECT_IDS.some(key => Object.hasOwn(source, key)) || Object.hasOwn(source, 'project') || Object.hasOwn(source, 'folder');
    pinKnown ||= PIN_FLAGS.some(key => Object.hasOwn(source, key)) || Object.hasOwn(source, 'pinned_time');
    agentKnown ||= AGENT_IDS.some(key => Object.hasOwn(source, key)) || Object.hasOwn(source, 'agent') || Object.hasOwn(source, 'gpt');
    if (PROJECT_IDS.some(key => identifier(source[key])) || objectId(source.project) || objectId(source.folder)) categories.add('projects');
    // pinned_time is the explicit pin record, not a heuristic based on recency.
    if (PIN_FLAGS.some(key => source[key] === true) || (typeof source.pinned_time === 'string' && Number.isFinite(Date.parse(source.pinned_time)))) categories.add('pinned');
    for (const key of AGENT_IDS) {
      if (!identifier(source[key])) continue;
      if (key === 'gizmo_id' && context.projectGizmoIds?.has(source[key])) categories.add('projects');
      else if (key !== 'gizmo_id' || context.gizmoClassificationAvailable !== false) categories.add('agents');
    }
    if (objectId(source.agent) || objectId(source.gpt)) categories.add('agents');
  }
  const unknown = categories.size === 0 && !(projectKnown && pinKnown && agentKnown);
  if (categories.size === 0 && (context.recents !== false || sources.some(source => source.in_recents === true))) categories.add('recents');
  const unavailable = [!projectKnown && 'projects', !pinKnown && 'pinned', !agentKnown && 'agents'].filter(Boolean);
  return { categories: [...categories], unknown, unavailable };
}

export function selectConversation(raw, captureTypes, context = {}) {
  const classified = classifyConversation(raw, context);
  const policy = normalizeCaptureTypes(captureTypes);
  return { ...classified, selected: classified.categories.some(category => policy[category]) };
}

export function createSelectionSummary() {
  return { classified: 0, skipped: 0, unknown: 0, selected: 0, byType: { projects: 0, pinned: 0, agents: 0, recents: 0 } };
}

/** One call per distinct discovered conversation; counters describe eligibility. */
export function recordSelection(summary, decision) {
  if (decision.unknown) summary.unknown++;
  else summary.classified++;
  if (decision.selected) summary.selected++;
  else summary.skipped++;
  for (const category of decision.categories) summary.byType[category]++;
}
