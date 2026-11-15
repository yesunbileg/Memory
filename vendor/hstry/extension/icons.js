// Crisp inline SVG marks for providers, categories, and controls.
// Pure string templates keep both browser DOM and Node unit tests dependency-free.

const SVG_ATTRS = 'viewBox="0 0 24 24" aria-hidden="true"';

export const PROVIDER_ICONS = {
  chatgpt: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2a4.4 4.4 0 0 1 3.9 2.4 4.4 4.4 0 0 1 4.2 5.4 4.4 4.4 0 0 1-2.2 6.5 4.4 4.4 0 0 1-5.9 3.3 4.4 4.4 0 0 1-5.9-3.3 4.4 4.4 0 0 1-2.2-6.5A4.4 4.4 0 0 1 8.1 5.6 4.4 4.4 0 0 1 12 3.2Z"/><circle cx="12" cy="12" r="2.6"/></svg>`,
  claude: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 3v18M4.2 7.5l15.6 9M19.8 7.5 4.2 16.5M6.5 4.6l11 14.8M17.5 4.6l-11 14.8"/></svg>`,
  gemini: `<svg ${SVG_ATTRS} fill="currentColor"><path d="M12 2.5c.8 5.1 4.4 8.7 9.5 9.5-5.1.8-8.7 4.4-9.5 9.5-.8-5.1-4.4-8.7-9.5-9.5 5.1-.8 8.7-4.4 9.5-9.5Z"/></svg>`,
  codex: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="m8 9.5 3 2.5-3 2.5M13 14.5h3.5"/></svg>`,
};

export const CATEGORY_ICONS = {
  projects: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h4.2l1.8 2h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/></svg>`,
  pinned: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3.5 2.6 5.3 5.9.8-4.2 4.1 1 5.8-5.3-2.8-5.3 2.8 1-5.8-4.2-4.1 5.9-.8Z"/></svg>`,
  agents: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="7" width="16" height="12" rx="3.5"/><path d="M12 3.5V7M9 12.5v1M15 12.5v1M9.5 16h5"/></svg>`,
  recents: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`,
};

export const UI_ICONS = {
  refresh: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 0 0-14.3-4.7M4 4v3.5h3.5M4 13a8 8 0 0 0 14.3 4.7M20 20v-3.5h-3.5"/></svg>`,
  external: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M14 5h5v5M10 14 19 5M19 13.5v4A2.5 2.5 0 0 1 16.5 20h-10A2.5 2.5 0 0 1 4 17.5v-10A2.5 2.5 0 0 1 6.5 5h4"/></svg>`,
  upload: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7.5 8.5 12 4l4.5 4.5M4 17.5v1A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5v-1"/></svg>`,
  check: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>`,
  play: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M7 5.5v13l11-6.5Z"/></svg>`,
  chevron: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`,
  arrowRight: `<svg ${SVG_ATTRS} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13.5 6.5 19 12l-5.5 5.5"/></svg>`,
};
