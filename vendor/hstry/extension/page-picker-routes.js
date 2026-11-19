// The classic content script and the module worker must agree about which *page*
// is asking. This file has no DOM, extension API, network, or storage dependency.
(() => {
  const uuid = '(?:[a-f\\d]{32}|[a-f\\d]{8}-(?:[a-f\\d]{4}-){3}[a-f\\d]{12})';
  const projectId = new RegExp('^g-p-' + uuid + '$', 'i');
  const projectLink = new RegExp('^/g/(g-p-' + uuid + ')(?:-[^/]*)?/project/?$', 'i');
  const gptChat = new RegExp('^/(?:g/g-[^/]+/)?c/(' + uuid + ')/?$', 'i');
  const claudeItem = new RegExp('^/(chat|project)/(' + uuid + ')/?$', 'i');
  const geminiPage = /^\/(?:u\/(\d+)\/)?app(?:\/([a-zA-Z0-9_-]{1,258}))?\/?$/;
  const restricted =
    /(?:^|[^\p{L}\p{N}])(?:locked?|lockedchats?|archived?|archivedchats?|share[d]?|auth|login|settings|datacontrols)(?:$|[^\p{L}\p{N}])/iu;
  function parsed(value, base) {
    try {
      const u = new URL(value, base);
      if (
        u.protocol !== 'https:' ||
        u.port ||
        u.username ||
        u.password ||
        restricted.test(decodeURIComponent(u.hash))
      )
        return null;
      for (const [k, v] of u.searchParams)
        if (restricted.test(k) || restricted.test(v)) return null;
      return u;
    } catch {
      return null;
    }
  }
  function info(u) {
    if (!u) return null;
    const p = u.pathname;
    if (
      u.hostname === 'chatgpt.com' &&
      (/^\/(?:projects\/?)?$/.test(p) ||
        gptChat.test(p) ||
        projectLink.test(p) ||
        /^\/g\/g-[^/]+\/?$/.test(p))
    )
      return { provider: 'chatgpt', slot: null };
    if (
      u.hostname === 'claude.ai' &&
      (/^\/(?:(?:new|recents|chats|projects)\/?)?$/.test(p) || claudeItem.test(p))
    )
      return { provider: 'claude', slot: null };
    if (u.hostname !== 'gemini.google.com') return null;
    const match = p.match(geminiPage);
    if (p !== '/' && !match) return null;
    if (
      match?.[2] &&
      /^(?:new|gem|gems|settings|activity|library|extensions|saved|share|locked|archived)$/i.test(
        match[2],
      )
    )
      return null;
    const slots = u.searchParams.getAll('authuser');
    if (
      slots.length > 1 ||
      (slots.length && !/^\d{1,9}$/.test(slots[0])) ||
      (match?.[1] && (match[1].length > 9 || (slots.length && match[1] !== slots[0])))
    )
      return null;
    return { provider: 'gemini', slot: match?.[1] ?? slots[0] ?? null };
  }
  const pageContext = (url) => info(parsed(url));
  function rowFromHref(href, pageUrl) {
    if (typeof href !== 'string' || !href.trim() || /^[?#]/.test(href.trim())) return null;
    const base = parsed(pageUrl),
      context = info(base),
      url = parsed(href, base?.href);
    if (!context || !url || url.origin !== base.origin || !info(url)) return null;
    let match;
    if (context.provider === 'chatgpt') {
      if ((match = url.pathname.match(projectLink)))
        return { kind: 'project', id: match[1].toLowerCase() };
      if ((match = url.pathname.match(gptChat)))
        return { kind: 'conversation', id: match[1].toLowerCase() };
    } else if (context.provider === 'claude') {
      if ((match = url.pathname.match(claudeItem)))
        return {
          kind: match[1].toLowerCase() === 'chat' ? 'conversation' : 'project',
          id: match[2].toLowerCase(),
        };
    } else {
      const { slot } = info(url);
      if (slot !== null && slot !== (context.slot ?? '0')) return null;
      const id = url.pathname.match(geminiPage)?.[2]?.replace(/^c_/, '');
      if (id && id.length <= 256) return { kind: 'conversation', id };
    }
    return null;
  }
  function projectButton(id, pageUrl) {
    return typeof id === 'string' &&
      projectId.test(id) &&
      pageContext(pageUrl)?.provider === 'chatgpt'
      ? { kind: 'project', id: id.toLowerCase() }
      : null;
  }
  globalThis.aiMemoryPageRoutes = Object.freeze({ pageContext, rowFromHref, projectButton });
})();
