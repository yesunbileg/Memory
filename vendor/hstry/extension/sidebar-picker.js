// Only inspect navigation rows, never conversation bodies. Checking a box is local
// to this tab. The one worker call below saves IDs, it does not start a capture.
(() => {
  'use strict';

  const WRAPPER =
    '#stage-slideover-sidebar,#stage-popover-sidebar,#sidebar,[data-testid="sidebar"]';
  const NAV =
    WRAPPER +
    ',nav[aria-label="Chat history" i],nav[aria-label="sidebar" i],aside nav,nav:has(#history),nav:has([data-sidebar-item])';
  const HIDDEN = 'main,article,[role="main"],[hidden],[inert],[aria-hidden="true"]';
  const HEADING = 'h1,h2,h3,h4,h5,h6,[role="heading"]';
  const SECTION = 'section,[role="group"],[data-sidebar-section],[data-section]';
  const ROW = 'a[href],button[data-project-id],[role="button"][data-project-id]';
  const UI = '[data-memory-sync-ui]';
  const UUID = '(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})';
  const projectPath = new RegExp('^/g/(g-p-' + UUID + ')(?:-[^/]*)?/project/?$', 'i');
  const projectId = new RegExp('^g-p-' + UUID + '$', 'i');
  const conversationPath = new RegExp('^/c/(' + UUID + ')/?$', 'i');
  const memberPath = new RegExp('^/g/[^/]+/c/(' + UUID + ')/?$', 'i');
  const selected = new Set();
  const controls = new Map();
  const toolbars = new Map();
  let serial = 0;
  let pending = false;
  let notice = '';

  function text(node) {
    const labelledBy = node.getAttribute('aria-labelledby')?.trim().split(/\s+/);
    return (
      node.getAttribute('aria-label') ||
      labelledBy
        ?.map((id) => document.getElementById(id)?.textContent)
        .filter(Boolean)
        .join(' ') ||
      node.textContent ||
      ''
    ).trim();
  }

  function sectionName(value) {
    const label = (value || '').trim().toLowerCase();
    if (/^(recents?|recent chats|your chats|сүүлийн чатууд)$/.test(label)) return 'recents';
    if (
      /^(pinned(?: chats)?|projects?|locked(?: chats)?|archived?(?: chats)?|төслүүд)$/.test(label)
    )
      return 'other';
    return '';
  }

  function parseRow(node) {
    if (!node.matches('a')) {
      const id = node.getAttribute('data-project-id');
      return projectId.test(id) ? { kind: 'project', id } : null;
    }
    let url;
    try {
      url = new URL(node.getAttribute('href'), location.href);
    } catch {
      return null;
    }
    if (url.origin !== location.origin) return null;
    let match = url.pathname.match(projectPath);
    if (match) return { kind: 'project', id: match[1] };
    match = url.pathname.match(conversationPath);
    if (match) return { kind: 'conversation', id: match[1] };
    match = url.pathname.match(memberPath);
    return match ? { kind: 'member', id: match[1] } : null;
  }

  function markedChat(node) {
    if (
      node.closest(
        '[data-project-id],[data-gizmo-id],[data-pinned="true"],[data-is-pinned="true"],[data-locked="true"],[data-is-locked="true"]',
      )
    )
      return true;
    // Only use extra provider-owned accessibility metadata, not a title the
    // user may literally have named "Research notes, pinned".
    const label = text(node);
    const title = (node.querySelector('.truncate,[dir="auto"]') || node).textContent.trim();
    const suffix = label.startsWith(title) ? label.slice(title.length) : label;
    if (
      /,\s*(?:chat in project\s|pinned(?: chat)?(?:,|$)|locked(?: chat)?(?:,|$)|chat with (?:a )?(?:custom )?gpt\b)/i.test(
        suffix,
      )
    )
      return true;
    // A project with an expanded child list can expose plain /c/ child routes.
    for (
      let parent = node.parentElement
        ?.closest('li,[role="treeitem"]')
        ?.parentElement?.closest('li,[role="treeitem"]');
      parent;
      parent = parent.parentElement?.closest('li,[role="treeitem"]')
    ) {
      if (parent.querySelector('a[href*="/g/g-p-"][href*="/project"],[data-project-id]'))
        return true;
    }
    return false;
  }

  function scan(root, denied) {
    const candidates = new Map();
    const headings = new Map();
    function areaOf(node) {
      for (
        let group = node.closest(SECTION);
        group && root.contains(group);
        group = group.parentElement?.closest(SECTION)
      ) {
        const label =
          group.dataset.sidebarSection || group.dataset.section || group.getAttribute('aria-label');
        const area = headings.get(group) || sectionName(label);
        if (area) return area;
        if (label && !/^chat history$/i.test(label)) return 'other';
      }
      return headings.get(root);
    }
    // Section headings can be controls or plain text next to a list of links.
    for (const node of root.querySelectorAll(
      ROW + ',' + HEADING + ',' + SECTION + ',button,[aria-expanded],span,div',
    )) {
      if (node.closest(UI + ',' + HIDDEN)) continue;
      if (node.matches(ROW)) {
        const item = parseRow(node);
        if (!item) continue;
        const area = areaOf(node);
        if (item.kind === 'project') {
          candidates.set(node, item);
        } else if (item.kind === 'member' || markedChat(node) || area === 'other') {
          denied.add(item.id);
        } else if (area === 'recents') {
          candidates.set(node, item);
        }
      } else if (!node.closest(ROW)) {
        const heading = node.matches(HEADING);
        if (!heading && node.childElementCount && !node.matches('button,[aria-expanded]')) continue;
        const name = sectionName(text(node));
        if (name || heading) {
          const group = node.closest(SECTION);
          headings.set(group && root.contains(group) ? group : root, name || 'other');
        }
      }
    }
    return candidates;
  }

  function stop(event) {
    event.stopPropagation();
  }

  function addControl(row, item) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.dataset.memorySyncUi = '';
    box.dataset.memorySyncKind = item.kind;
    box.dataset.memorySyncId = item.id;
    // Keep interactive elements as siblings. CSS anchors position each checkbox
    // over its reserved gutter without wrapping or moving React-owned nodes.
    const anchor = '--memory-sync-' + ++serial;
    row.dataset.memorySyncRow = '';
    row.style.setProperty('--memory-sync-anchor', anchor);
    box.style.setProperty('--memory-sync-anchor', anchor);
    row.after(box);
    // Page scripts share the DOM, but a hosted page cannot synthesize trusted
    // input events. Cancel synthetic activation before it can produce a change.
    box.addEventListener('click', (event) => {
      stop(event);
      if (!event.isTrusted) event.preventDefault();
    });
    for (const event of ['pointerdown', 'mousedown', 'keydown', 'keyup'])
      box.addEventListener(event, stop);
    box.addEventListener('change', (event) => {
      stop(event);
      if (!event.isTrusted || pending) {
        box.checked = selected.has(item.id);
        return;
      }
      if (box.checked) selected.add(item.id);
      else selected.delete(item.id);
      notice = '';
      refresh();
    });
    controls.set(row, { box, item });
  }

  function removeControl(row, box) {
    box.remove();
    row.removeAttribute('data-memory-sync-row');
    row.style.removeProperty('--memory-sync-anchor');
    controls.delete(row);
  }

  function addToolbar(root) {
    const bar = document.createElement('div');
    bar.dataset.memorySyncUi = 'toolbar';
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.memorySyncAdd = '';
    button.textContent = 'Add to sync list';
    button.title = 'Save the checked projects and chats for a later sync';
    button.addEventListener('click', save);
    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    bar.append(button, status);
    root.prepend(bar);
    toolbars.set(root, { bar, button, status });
  }

  function refresh() {
    for (const { box, item } of controls.values()) {
      box.checked = selected.has(item.id);
      box.disabled = pending;
    }
    for (const { button, status } of toolbars.values()) {
      button.disabled = !selected.size || pending;
      const message = pending
        ? 'Saving…'
        : notice || (selected.size ? selected.size + ' selected' : '');
      if (status.textContent !== message) status.textContent = message;
    }
  }

  async function save(event) {
    stop(event);
    if (!event.isTrusted || pending) return;
    render(); // Recheck visibility and eligibility after any SPA navigation.
    const ids = { projectIds: new Set(), conversationIds: new Set() };
    for (const { item } of controls.values()) {
      if (selected.has(item.id))
        ids[item.kind === 'project' ? 'projectIds' : 'conversationIds'].add(item.id);
    }
    if (!ids.projectIds.size && !ids.conversationIds.size) return;
    pending = true;
    refresh();
    try {
      const result = await chrome.runtime.sendMessage({
        type: 'ADD_CHATGPT_SYNC_SELECTION',
        projectIds: [...ids.projectIds],
        conversationIds: [...ids.conversationIds],
      });
      if (!result?.ok) throw new Error(result?.error || 'Could not save. Try again.');
      selected.clear();
      notice = 'Added to sync list';
    } catch (error) {
      notice = error.message || 'Could not save. Try again.';
    } finally {
      pending = false;
      refresh();
    }
  }

  function render() {
    // The desktop stage may also contain a collapsed rail. Mount in the history
    // navigation itself, not before the stage's absolutely positioned panels.
    const scoped = [...document.querySelectorAll(NAV)].filter((root) => !root.closest(HIDDEN));
    const navs = [
      ...new Set(
        scoped.flatMap((root) =>
          root.matches('nav')
            ? [root]
            : [...root.querySelectorAll('nav')].filter((nav) => !nav.closest(HIDDEN)),
        ),
      ),
    ];
    const roots = [
      ...navs.filter((nav) => !nav.parentElement?.closest('nav')),
      ...scoped.filter(
        (root) =>
          !root.matches('nav') &&
          !navs.some((nav) => root.contains(nav)) &&
          !root.parentElement?.closest(WRAPPER),
      ),
    ];
    const denied = new Set();
    const groups = roots.map((root) => [root, scan(root, denied)]);
    const live = new Map();
    for (const [root, rows] of groups) {
      for (const [row, item] of rows) {
        if (item.kind === 'conversation' && denied.has(item.id)) continue;
        live.set(row, item);
      }
      const toolbar = toolbars.get(root);
      if (toolbar && toolbar.bar.parentNode !== root) toolbars.delete(root);
      if (
        !toolbars.has(root) &&
        [...rows.values()].some((i) => i.kind === 'project' || !denied.has(i.id))
      )
        addToolbar(root);
    }
    for (const [root, { bar }] of toolbars) {
      if (!roots.includes(root)) {
        bar.remove();
        toolbars.delete(root);
      }
    }
    for (const [row, { box, item }] of controls) {
      if (live.get(row)?.id !== item.id || row.nextElementSibling !== box) removeControl(row, box);
    }
    const visibleIds = new Set();
    for (const [row, item] of live) {
      visibleIds.add(item.id);
      if (!controls.has(row)) addControl(row, item);
      const label =
        'Select ' + (item.kind === 'project' ? 'project ' : 'chat ') + text(row) + ' for sync';
      const { box } = controls.get(row);
      if (box.getAttribute('aria-label') !== label) box.setAttribute('aria-label', label);
    }
    // Don't carry stale IDs across a workspace switch, pin, or removal.
    for (const id of selected) if (!visibleIds.has(id)) selected.delete(id);
    refresh();
  }

  let scheduled = false;
  new MutationObserver((changes) => {
    if (scheduled) return;
    const affected = changes.some(({ target, type, addedNodes, removedNodes }) => {
      const el = target.nodeType === Node.ELEMENT_NODE ? target : target.parentElement;
      if (!el || el.closest(UI + ',main,article,[role="main"]')) return false;
      if (type !== 'childList') return !!el.closest(NAV) || !!el.querySelector(NAV);
      const nodes = [...addedNodes, ...removedNodes];
      if (nodes.every((n) => n.nodeType === Node.ELEMENT_NODE && n.matches(UI))) return false;
      return (
        !!el.closest(NAV) ||
        nodes.some(
          (n) => n.nodeType === Node.ELEMENT_NODE && (n.matches(NAV) || !!n.querySelector(NAV)),
        )
      );
    });
    if (affected) {
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        render();
      });
    }
  }).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: [
      'href',
      'aria-label',
      'aria-labelledby',
      'aria-hidden',
      'aria-expanded',
      'hidden',
      'inert',
      'data-pinned',
      'data-is-pinned',
      'data-locked',
      'data-is-locked',
      'data-gizmo-id',
      'data-project-id',
      'data-sidebar-section',
      'data-section',
    ],
  });
  render();
})();
