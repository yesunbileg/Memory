// Catalog metadata lives only for this popup session. A title or a duplicate ID
// in another account never grants permission to archive a different item.
export const idKey = ({ principalId, kind, id }) => JSON.stringify([principalId, kind, id]);
export function catalogRows(catalog) {
  return catalog.entries.flatMap(({ principalId, title, projects = [], conversations = [] }) =>
    [
      ['project', projects],
      ['conversation', conversations],
    ].flatMap(([kind, items]) =>
      items.map(({ id, title: itemTitle, projectId }) => ({
        id,
        kind,
        principalId,
        principalTitle: title || principalId,
        title: itemTitle || id,
        projectId,
      })),
    ),
  );
}
export function filterRows(rows, search, kind) {
  const term = search.trim().toLocaleLowerCase();
  return rows.filter(
    (r) =>
      (kind === 'all' || kind === r.kind) &&
      (!term || (r.title + ' ' + r.principalTitle).toLocaleLowerCase().includes(term)),
  );
}
export function selectionFor(rows) {
  const entries = new Map();
  for (const { id, kind, principalId } of rows) {
    if (!entries.has(principalId))
      entries.set(principalId, { principalId, projectIds: new Set(), conversationIds: new Set() });
    entries.get(principalId)[kind === 'project' ? 'projectIds' : 'conversationIds'].add(id);
  }
  return {
    entries: [...entries.values()].map((e) => ({
      ...e,
      projectIds: [...e.projectIds],
      conversationIds: [...e.conversationIds],
    })),
  };
}
export function savedRows(scope, catalog, known) {
  const rows = [];
  for (const { principalId, projectIds = [], conversationIds = [] } of scope?.entries || []) {
    const principal = catalog.entries.find((e) => e.principalId === principalId);
    if (!principal) continue;
    for (const [kind, ids] of [
      ['project', projectIds],
      ['conversation', conversationIds],
    ])
      for (const id of ids) {
        const row = {
          id,
          kind,
          principalId,
          principalTitle: principal.title || principalId,
          title: id,
        };
        rows.push(known.get(idKey(row)) || row);
      }
  }
  return rows;
}
