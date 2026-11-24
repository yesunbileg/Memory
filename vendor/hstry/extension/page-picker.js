// Isolated, top-frame UI. The page supplies visible navigation destinations, never
// scope authority. Only the worker verifies membership and persists a single ID.
(() => {
  if (window !== window.top || !chrome.runtime?.id) return;
  const routes = globalThis.aiMemoryPageRoutes;
  const nav =
    'nav,aside,[role="navigation"],#history,[id*="sidebar" i],[data-testid*="sidebar" i],bard-sidenav,mat-sidenav,side-navigation-v2';
  const heads =
    'h1,h2,h3,h4,h5,h6,[role="heading"],summary,[aria-expanded][aria-controls],button[aria-expanded]';
  const group =
    'section,details,[role="group"],[role="treeitem"],[data-sidebar-section],[data-section]';
  const modal = 'dialog[open],[role="dialog"],[aria-modal="true"]';
  const outside =
    'main,article,[role="main"],[data-message-author-role],user-query,model-response,form,header,footer,dialog,[role="dialog"],[role="menu"],[contenteditable="true"]';
  const hidden = '[hidden],[inert],[aria-hidden="true"],[aria-disabled="true"],[disabled]';
  const privacyAttrs = ['data-locked', 'data-is-locked', 'data-archived', 'data-is-archived'];
  const marked = privacyAttrs
    .map((a) => '[' + a + '="true" i]')
    .concat([
      '[data-testid*="locked-chat" i]',
      '[data-testid*="archived-chat" i]',
      '[data-state="locked" i]',
      '[data-state="archived" i]',
    ])
    .join(',');
  const sectionLabels = ['aria-label', 'data-sidebar-section', 'data-section'];
  const privateLabel =
    /^(?:(?:locked|archived)(?:\s+(?:chats?|conversations?|projects?))?|(?:түгжээтэй|түгжигдсэн|архивласан)\s+чатууд)(?:\s*\(\d+\))?$/i;
  const privateSuffix = /,\s*(?:locked|archived)(?: chat)?(?:,|$)/i;
  const recentsHeading = /^(?:recents|recent chats|сүүлийн чатууд)$/iu;
  const nativeProjectRow = 'data-app-action-sidebar-project-row';
  const nativeProjectId = 'data-app-action-sidebar-project-id';
  const selectors = {
    chatgpt:
      'a[href*="/c/"],a[href*="/g/g-p-" i],button[data-project-id],[role="button"][data-project-id],' +
      `[role="button"][${nativeProjectRow}][${nativeProjectId}]`,
    claude: 'a[href*="/chat/"],a[href*="/project/"]',
    gemini: 'a[href*="/app/"]',
  };
  const css = [
    '[data-memory-sync-row]{anchor-name:var(--ai-memory-picker-anchor)!important;padding-inline-start:2.25rem!important}',
    '[data-memory-sync-row]:has(+input[data-memory-sync-id]:checked):not([aria-current=page]){background-color:rgba(66,127,117,.12);border-radius:.5rem}',
    'input[data-memory-sync-id]{appearance:auto!important;position:absolute!important;position-anchor:var(--ai-memory-picker-anchor)!important;position-visibility:anchors-visible;inset:auto!important;top:anchor(center)!important;left:anchor(left)!important;transform:translate(.5rem,-50%)!important;z-index:2!important;width:1rem!important;height:1rem!important;box-sizing:border-box!important;margin:0!important;padding:0!important;cursor:pointer!important;accent-color:#3c756f!important}',
    'input[data-memory-sync-id]:dir(rtl){left:auto!important;right:anchor(right)!important;transform:translate(-.5rem,-50%)!important}',
    'input[data-memory-sync-id]:indeterminate{accent-color:#5a726d!important}',
    '[data-memory-sync-row=recents]:has(+input[data-memory-sync-id]:checked){padding-inline-end:2.25rem!important}',
    '[data-memory-sync-row=recents]:has(+input[data-memory-sync-id]:checked)::after{content:"All" / "";position:absolute;position-anchor:var(--ai-memory-picker-anchor);position-visibility:anchors-visible;top:anchor(center);right:anchor(right);transform:translate(-.6rem,-50%);color:inherit;opacity:.78;font:500 .75rem/1 system-ui,sans-serif;pointer-events:none}',
    '[data-memory-sync-row=recents]:dir(rtl)::after{left:anchor(left);right:auto;transform:translate(.6rem,-50%)}',
    'input[data-memory-sync-id][aria-busy=true]{opacity:.6!important;cursor:progress!important}',
    'input[data-memory-sync-id]:focus-visible{outline:2px solid currentColor!important;outline-offset:2px!important}',
    '[data-memory-picker-note]{position:fixed!important;z-index:2147483646!important;inset:auto auto 1rem 1rem!important;padding:.48rem .72rem!important;border-radius:.5rem!important;max-width:min(24rem,85vw)!important;background:#2b514d!important;color:white!important;font:500 13px/1.5 system-ui,sans-serif!important;pointer-events:none!important}',
    '[data-memory-picker-note]:empty{display:none!important}',
  ].join('\n');
  const key = (r) => r.kind + ':' + r.id;
  const send = (m) => chrome.runtime.sendMessage(m);
  const release = (token) => {
    if (token) send({ type: 'CLOSE_PAGE_SELECTION', token }).catch(() => {});
  };
  const problem = (code, message) => Object.assign(new Error(message), { code });
  const unavailable = () => problem('page_unavailable', 'List mode is unavailable on this page.');
  const responseError = (r) =>
    problem(
      r?.code || 'catalog_unavailable',
      r?.error || 'Could not verify the sidebar. Reopen AI Memory and try List mode again.',
    );
  const stale = (e) => e?.code === 'principal_changed' || e?.code === 'catalog_expired';
  let active,
    sequence = 0;

  function visible(el) {
    if (
      el.closest(hidden) ||
      !el.checkVisibility({ opacityProperty: true, visibilityProperty: true })
    )
      return false;
    const r = el.getBoundingClientRect();
    return r.height > 0 && r.width > 0 && r.right > 0 && r.left < window.innerWidth;
  }
  function label(el) {
    const id = el.getAttribute('aria-labelledby');
    return (
      el.getAttribute('aria-label') ||
      (id &&
        id
          .split(/\s+/)
          .map((i) => {
            const node = document.getElementById(i);
            return node && !node.closest(outside) ? node.textContent : '';
          })
          .join(' ')) ||
      el.getAttribute('title') ||
      el.textContent ||
      ''
    )
      .trim()
      .slice(0, 320);
  }
  function inLockedDialog() {
    return [...document.querySelectorAll(modal)].some(
      (d) =>
        visible(d) &&
        /(?:locked chats?|unlock|архивласан чатууд|түгжээтэй чатууд)/i.test(
          (d.getAttribute('aria-label') || '') + ' ' + (d.querySelector(heads)?.textContent || ''),
        ),
    );
  }
  function geometrySidebar(row) {
    if (row.closest(nav)) return true;
    // Same visible-left-edge fallback as the supplied example, not a requirement
    // that an English "Recents" heading or one particular sidebar ID exist.
    const r = row.getBoundingClientRect();
    return r.left < Math.min(380, window.innerWidth * 0.6) && r.right > 0;
  }
  function itemOf(el, href) {
    if (el.matches('a[href]')) return routes.rowFromHref(el.getAttribute('href'), href);
    if (el.querySelector('a[href*="/g/g-p-" i]')) return null;
    const id = el.matches(`[${nativeProjectRow}]`)
      ? el.getAttribute(nativeProjectId)
      : el.getAttribute('data-project-id');
    return routes.projectButton(id, href);
  }
  function scan(s) {
    const href = location.href,
      context = routes.pageContext(href);
    if (!context || context.provider !== s.provider || inLockedDialog()) throw unavailable();
    const rows = new Map(),
      denied = new Set(),
      areas = new Map();
    const selector = selectors[s.provider],
      current = routes.rowFromHref(href, href);
    // Query navigation candidates, not messages or titles in the conversation.
    // Excluding main/article here also keeps streamed message links out of scans.
    const query =
      ':is(' + selector + ',' + heads + '):not(:is(' + outside + '),:is(' + outside + ') *)';
    for (const el of document.querySelectorAll(query)) {
      if (!visible(el) || !geometrySidebar(el)) continue;
      let item;
      if (!el.matches(selector)) {
        const root = el.closest(nav);
        if (!root) continue;
        const name = label(el);
        const locked = privateLabel.test(name);
        const closed = el.getAttribute('aria-expanded') === 'false';
        if (s.provider === 'chatgpt' && recentsHeading.test(name)) {
          item = { kind: 'recents', id: 'recents' };
        } else {
          const area = el.closest(group) || root;
          areas.set(area, locked && !closed);
          if (locked && !closed) {
            const controlled = document.getElementById(el.getAttribute('aria-controls') || '');
            if (controlled) areas.set(controlled, true);
          }
          continue;
        }
      } else item = itemOf(el, href);
      if (!item) continue;
      let blocked = !!el.closest(marked);
      const aria = el.getAttribute('aria-label') || '';
      // A chat title ending in "locked" is not itself provider lock metadata.
      if (aria && aria !== el.textContent.trim() && privateSuffix.test(aria)) blocked = true;
      for (let p = el; p && p !== document.body && !blocked; p = p.parentElement) {
        blocked =
          areas.get(p) === true ||
          sectionLabels.some((a) => privateLabel.test(p.getAttribute(a) || ''));
      }
      if (blocked) denied.add(key(item));
      else rows.set(el, item);
    }
    for (const [el, item] of rows) if (denied.has(key(item))) rows.delete(el);
    if (current && denied.has(key(current))) throw unavailable();
    return { rows, current };
  }
  function note(s, text = '') {
    if (s.note.textContent !== text) s.note.textContent = text;
  }
  function detach(s, row, c) {
    c.box.remove();
    if (row.style.getPropertyValue('--ai-memory-picker-anchor') === c.anchor) {
      row.style.removeProperty('--ai-memory-picker-anchor');
      row.removeAttribute('data-memory-sync-row');
    }
    s.controls.delete(row);
    s.boxes.delete(c.box);
  }
  function attach(s, row, item) {
    const box = document.createElement('input'),
      anchor = '--ai-memory-picker-' + ++sequence;
    box.type = 'checkbox';
    box.dataset.memorySyncId = item.id;
    box.dataset.memorySyncKind = item.kind;
    row.setAttribute('data-memory-sync-row', item.kind);
    row.style.setProperty('--ai-memory-picker-anchor', anchor);
    box.style.setProperty('--ai-memory-picker-anchor', anchor);
    // Siblings provide independent link and checkbox semantics. Reserve the
    // inline gutter without moving or wrapping the site's React/Angular nodes.
    row.after(box);
    const c = { row, box, item, anchor };
    s.controls.set(row, c);
    s.boxes.set(box, c);
    s.own.add(box);
    return c;
  }
  function paint(s) {
    for (const [row, c] of s.controls) {
      const i = s.rows.get(row);
      if (
        !i ||
        key(i) !== key(c.item) ||
        !s.known.get(key(i)) ||
        row.nextElementSibling !== c.box ||
        !row.isConnected
      )
        detach(s, row, c);
    }
    // Each sidebar row has its own proof. The open chat may be absent from
    // the catalog even when other visible chats and projects are verified.
    for (const [row, i] of s.rows) {
      const k = key(i),
        state = s.known.get(k);
      if (!state || !row.isConnected) continue;
      const c = s.controls.get(row) || attach(s, row, i);
      c.box.checked = state.checked;
      c.box.indeterminate = !!state.inherited && !state.checked;
      c.box.setAttribute('aria-busy', String(s.pending.has(k)));
      const title = label(row) || (i.kind === 'project' ? 'Untitled project' : 'Untitled chat');
      const ident =
        i.kind === 'recents'
          ? 'All Recent chats'
          : (i.kind === 'project' ? 'Project: ' : 'Chat: ') + title;
      const help = s.pending.has(k)
        ? 'Saving…'
        : i.kind === 'recents'
          ? 'Includes all ordinary Recent chats, including new ones.'
          : i.kind === 'project'
            ? 'Includes every eligible chat in this project.'
            : state.inherited
              ? 'Included through a selected group. The checkbox also lets you save this chat separately.'
              : 'Select or remove this individual chat.';
      c.box.setAttribute('aria-label', ident + '. ' + help);
      c.box.title = ident + ' — ' + help;
    }
  }
  function stop(s) {
    if (!s || active !== s) return;
    active = null;
    s.abort.abort();
    s.observer?.disconnect();
    clearTimeout(s.timer);
    for (const [row, c] of s.controls) detach(s, row, c);
    s.note.remove();
    document.adoptedStyleSheets = document.adoptedStyleSheets.filter((sheet) => sheet !== s.sheet);
    release(s.token);
  }
  function snapshot(s) {
    const v = scan(s);
    s.rows = v.rows;
    s.current = v.current;
    paint(s);
  }
  async function read(s, force = false, retry = true) {
    if (s !== active) return;
    const revision = s.revision,
      href = location.href;
    snapshot(s);
    const wanted = new Map([...s.rows.values()].map((i) => [key(i), i]));
    if (s.current) wanted.set(key(s.current), s.current);
    const ids = [...wanted].filter(([k]) => force || !s.known.has(k)).map(([, i]) => i);
    for (let i = 0; i < ids.length || (!i && !s.token); i += 500) {
      const items = ids.slice(i, i + 500);
      const reply = await send({
        type: 'READ_PAGE_SELECTION',
        items,
        ...(s.token ? { token: s.token } : {}),
      });
      if (active !== s || s.revision !== revision || href !== location.href) {
        release(reply?.token);
        return;
      }
      if (!reply?.ok) {
        const error = responseError(reply);
        if (retry && s.token && stale(error)) {
          release(s.token);
          s.token = null;
          s.known.clear();
          paint(s);
          return read(s, true, false);
        }
        throw error;
      }
      if (typeof reply.token !== 'string' || !reply.token || !Array.isArray(reply.items))
        throw responseError();
      s.token = reply.token;
      const found = new Map(reply.items.map((v) => [key(v), v]));
      for (const item of items) s.known.set(key(item), found.get(key(item)) || null);
    }
    // A virtualized row, a privacy section, or the entire page can have changed
    // while the worker checked account membership. Never decorate a stale node.
    snapshot(s);
    if (
      !s.controls.size &&
      s.current &&
      s.known.has(key(s.current)) &&
      !s.known.get(key(s.current))
    ) {
      throw problem(
        'page_unverified',
        'Cannot verify this chat or project. Open the provider home page and try List mode there.',
      );
    }
    note(
      s,
      s.error ||
        (s.controls.size
          ? ''
          : 'No verified rows here yet. Expand the sidebar, or open AI Memory and turn List mode on again.'),
    );
  }
  // Coalesce subtree changes while a read is running. A streamed transcript
  // causes no row scans, and a new/recycled row requests only previously unseen IDs.
  function schedule(s, force = false) {
    if (active !== s) return;
    s.force ||= force;
    if (s.scheduled) return;
    s.scheduled = true;
    s.timer = setTimeout(() => {
      if (active !== s) return;
      const job = s.chain.then(async () => {
        s.scheduled = false;
        const refresh = s.force;
        s.force = false;
        await read(s, refresh);
      });
      s.chain = job.catch((e) => {
        if (active !== s) return;
        if (e.code === 'page_unavailable' || e.code === 'page_unverified') return stop(s);
        s.known.clear();
        paint(s);
        note(s, 'Could not verify this sidebar. Reopen AI Memory and turn List mode off, then on.');
      });
    }, 60);
  }
  function mutate(s, c) {
    const k = key(c.item),
      state = s.known.get(k);
    if (!state || !s.token || s.pending.has(k)) return;
    // The pending intention is derived from confirmed state, never a page-editable
    // checked, data-* or value attribute. Do not replay after a principal change.
    const token = s.token,
      checked = !state.checked;
    s.pending.add(k);
    s.error = '';
    paint(s);
    const job = s.chain.then(async () => {
      if (active !== s || s.token !== token) return;
      snapshot(s);
      if (s.controls.get(c.row) !== c || c.row.nextElementSibling !== c.box || !s.known.get(k))
        return;
      const r = await send({
        type: 'SET_PAGE_SELECTION',
        token,
        kind: c.item.kind,
        id: c.item.id,
        checked,
      });
      if (active !== s || s.token !== token) return;
      if (!r?.ok || r.kind !== c.item.kind || r.id !== c.item.id || r.checked !== checked)
        throw responseError(r);
      s.known.set(k, { ...state, checked });
      paint(s);
      schedule(s, true);
    });
    s.chain = job
      .catch((e) => {
        if (active !== s) return;
        if (e.code === 'page_unavailable') return stop(s);
        s.error = 'Could not save that change. Check the list and try again.';
        if (stale(e)) {
          release(s.token);
          s.token = null;
          s.known.clear();
          schedule(s, true);
        }
        note(s, s.error);
      })
      .finally(() => {
        s.pending.delete(k);
        if (active === s) paint(s);
      });
  }
  function input(s, e) {
    const c = s.boxes.get(e.target);
    if (!c) return;
    // Stop site row/keyboard navigation without intercepting clicks on the link.
    e.stopImmediatePropagation();
    if (e.type === 'pointerdown' && e.isTrusted && e.button === 0) s.gesture = c;
    if (
      e.type === 'keydown' &&
      e.isTrusted &&
      document.activeElement === c.box &&
      (e.key === ' ' || e.key === 'Enter')
    ) {
      s.gesture = c;
      if (e.key === 'Enter') {
        e.preventDefault();
        if (!e.repeat) {
          s.gesture = null;
          mutate(s, c);
        }
      }
      return;
    }
    if (!['click', 'auxclick', 'dblclick'].includes(e.type)) return;
    e.preventDefault();
    const direct = s.gesture === c;
    s.gesture = null;
    // A site-created label can forward a *trusted* click. It is not activation of
    // our actual checkbox. Screen-reader activation has no pointer/key precursor.
    if (
      e.type !== 'click' ||
      !e.isTrusted ||
      e.button !== 0 ||
      c.box.labels?.length ||
      (!direct && !(e.detail === 0 && document.activeElement === c.box))
    )
      return;
    mutate(s, c);
  }
  async function begin(context) {
    if (inLockedDialog()) throw unavailable();
    const s = {
      provider: context.provider,
      href: location.href,
      revision: 0,
      token: null,
      abort: new AbortController(),
      controls: new Map(),
      boxes: new WeakMap(),
      own: new WeakSet(),
      known: new Map(),
      rows: new Map(),
      pending: new Set(),
      chain: Promise.resolve(),
      error: '',
      note: document.createElement('div'),
      sheet: new CSSStyleSheet(),
    };
    active = s;
    try {
      s.sheet.replaceSync(css);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, s.sheet];
      s.note.dataset.memoryPickerNote = '';
      s.note.setAttribute('role', 'status');
      s.own.add(s.note);
      snapshot(s);
      (document.body || document.documentElement).append(s.note);
      if (!s.current) note(s, 'Checking sidebar…');
      const options = { capture: true, signal: s.abort.signal };
      for (const t of [
        'pointerdown',
        'pointerup',
        'mousedown',
        'mouseup',
        'keydown',
        'keyup',
        'click',
        'auxclick',
        'dblclick',
      ])
        window.addEventListener(t, (e) => input(s, e), options);
      const routeChange = () => {
        if (s.href === location.href) return;
        s.href = location.href;
        s.revision++;
        if (!routes.pageContext(s.href)) return stop(s);
        s.known.clear();
        paint(s);
        release(s.token);
        s.token = null;
        schedule(s, true);
      };
      window.navigation?.addEventListener('currententrychange', routeChange, options);
      for (const t of ['popstate', 'hashchange']) window.addEventListener(t, routeChange, options);
      window.addEventListener('pagehide', () => stop(s), options);
      window.addEventListener(
        'focus',
        (e) => {
          if (e.target === window) schedule(s, true);
        },
        options,
      );
      window.addEventListener('resize', () => schedule(s), options);
      document.addEventListener(
        'visibilitychange',
        () => {
          if (!document.hidden) schedule(s, true);
        },
        options,
      );
      const target = selectors[s.provider] + ',' + nav + ',' + modal;
      s.observer = new MutationObserver((records) => {
        if (active !== s) return;
        for (const r of records) {
          const el = r.target;
          if (s.own.has(el)) continue;
          // A site can open its PIN/locked view as a modal inside main without a
          // URL change. Inspect only the dialog's accessible name/heading.
          if (
            (el.closest(modal) ||
              (r.type === 'childList' &&
                [...r.addedNodes].some((n) => n.nodeType === 1 && n.matches(modal)))) &&
            inLockedDialog()
          ) {
            stop(s);
            return;
          }
          if (s.scheduled || el.closest(outside)) continue;
          if (r.type === 'attributes') {
            const layout = r.attributeName === 'class' || r.attributeName === 'style';
            if (
              s.controls.has(el) ||
              el.matches(target) ||
              (!layout && el.closest(nav)) ||
              (el !== document.body && el.querySelector(target))
            ) {
              schedule(s);
              break;
            }
          } else {
            const nodes = [...r.addedNodes, ...r.removedNodes].filter((n) => !s.own.has(n));
            if (
              nodes.length &&
              (el.closest(nav) ||
                nodes.some(
                  (n) =>
                    n.nodeType === 1 &&
                    !n.matches(outside) &&
                    (n.matches(target) || n.querySelector(target)),
                ))
            ) {
              schedule(s);
              break;
            }
          }
        }
      });
      s.observer.observe(document.documentElement, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: [
          'href',
          'class',
          'style',
          'open',
          'aria-modal',
          'hidden',
          'inert',
          'aria-hidden',
          'aria-disabled',
          'aria-expanded',
          'aria-label',
          'aria-controls',
          'data-testid',
          'data-state',
          'data-project-id',
          nativeProjectRow,
          nativeProjectId,
          ...privacyAttrs,
          ...sectionLabels.filter((a) => a !== 'aria-label'),
        ],
      });
      const first = read(s);
      s.chain = first.catch(() => {});
      await first;
      if (active !== s) throw unavailable();
    } catch (e) {
      stop(s);
      throw e;
    }
  }
  function workerSender(sender) {
    return (
      sender?.id === chrome.runtime.id &&
      !sender.tab &&
      sender.url === chrome.runtime.getURL('background.js')
    );
  }
  async function providerTabResponse(message) {
    const ctx = routes.pageContext(location.href);
    if (!ctx || (ctx.provider !== 'claude' && ctx.provider !== 'gemini')) return null;
    const url = new URL(message.url);
    if (url.origin !== location.origin || url.username || url.password) return null;
    const { method, body } = message;
    const { pathname: path } = url;
    const geminiBoot = ctx.provider === 'gemini' && /^\/(?:u\/\d+\/)?app$/.test(path);
    if (ctx.provider === 'claude') {
      if (
        method !== 'GET' ||
        body != null ||
        !/^\/api\/(?:account|organizations(?:\/[\w-]+\/(?:projects(?:\/[\w-]+\/conversations_v2)?|chat_conversations(?:\/[\w-]+|_v2)?))?)\/?$/.test(
          path,
        )
      )
        return null;
    } else {
      const slot = path.match(/^\/u\/(\d+)\//)?.[1] ?? null;
      if (ctx.slot !== null && ctx.slot !== slot) return null;
      if (geminiBoot) {
        if (method !== 'GET' || body != null || url.search) return null;
      } else {
        if (
          method !== 'POST' ||
          !/^\/(?:u\/\d+\/)?_\/BardChatUi\/data\/batchexecute$/.test(path) ||
          typeof body !== 'string' ||
          body.length > 65536
        )
          return null;
        const calls = JSON.parse(new URLSearchParams(body).get('f.req'));
        const rpc = url.searchParams.get('rpcids');
        if (
          !['MaZiqc', 'hNvQHb'].includes(rpc) ||
          calls?.length !== 1 ||
          calls[0]?.length !== 1 ||
          calls[0][0]?.[0] !== rpc
        )
          return null;
      }
    }
    const response = await fetch(url.href, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: geminiBoot ? 'follow' : 'error',
      signal: AbortSignal.timeout(20_000),
      ...(method === 'POST' && {
        body,
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
          'X-Same-Domain': '1',
        },
      }),
    });
    if (new URL(response.url).origin !== location.origin) return null;
    const text = await response.text();
    if (text.length > 24 * 1024 * 1024) return null;
    return {
      status: response.status,
      url: response.url,
      body: text,
      retryAfter: response.headers.get('retry-after'),
    };
  }
  chrome.runtime.onMessage.addListener((m, sender, reply) => {
    if (m?.type === 'REFRESH_PAGE_SELECTION' && workerSender(sender)) {
      if (active) schedule(active, true);
      return;
    }
    if (m?.type === 'FETCH_PROVIDER_TAB') {
      if (!workerSender(sender)) return;
      providerTabResponse(m).then(reply, () => reply(null));
      return true;
    }
    if (m?.type === 'GET_CHATGPT_TAB_SESSION') {
      if (
        !workerSender(sender) ||
        !routes.pageContext(location.href) ||
        location.origin !== 'https://chatgpt.com'
      )
        return;
      // Use this document's browser session. Some browsers isolate a PWA's
      // cookies from the extension worker's default cookie context.
      fetch('/api/auth/session', {
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal: AbortSignal.timeout(15_000),
      })
        .then((res) => (res.ok ? res.json() : null))
        .then(
          (s) =>
            reply(
              s && {
                user: { id: s.user?.id },
                account: { id: s.account?.id ?? s.account?.account_id },
                accessToken: s.accessToken,
              },
            ),
          () => reply(null),
        );
      return true;
    }
    if (
      !m ||
      !['GET_PAGE_LIST_MODE', 'SET_PAGE_LIST_MODE'].includes(m.type) ||
      sender?.tab ||
      (sender?.frameId !== undefined && sender.frameId !== 0) ||
      sender?.id !== chrome.runtime.id ||
      sender.url !== chrome.runtime.getURL('popup.html')
    )
      return;
    const context = routes.pageContext(location.href);
    (async () => {
      if (m.type === 'SET_PAGE_LIST_MODE' && m.enabled === false) stop(active);
      if (!context) throw unavailable();
      if (m.type === 'SET_PAGE_LIST_MODE') {
        if (typeof m.enabled !== 'boolean') throw unavailable();
        if (m.enabled && !active) await begin(context);
      }
      return { ok: true, provider: context.provider, enabled: !!active };
    })().then(reply, (e) =>
      reply({
        ok: false,
        enabled: !!active,
        code: e.code || 'catalog_unavailable',
        error: e.code ? e.message : 'Could not verify the sidebar. Reopen AI Memory and try again.',
      }),
    );
    return true;
  });
})();
