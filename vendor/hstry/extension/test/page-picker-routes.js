import test from 'node:test';
import assert from 'node:assert/strict';
import '../page-picker-routes.js';
const routes = globalThis.aiMemoryPageRoutes;
const chat = '33333333-3333-4333-8333-333333333333';
const project = 'g-p-69117ae2458c8191bd82c1c21393de82';
const home = 'https://chatgpt.com/';

test('ordinary provider pages bind to their actual provider and Gemini slot', () => {
  assert.ok(routes, 'shared provider route policy is installed');
  for (const path of ['', 'projects', 'c/' + chat, 'g/' + project + '-my-notes/project'])
    assert.deepEqual(routes.pageContext(home + path), { provider: 'chatgpt', slot: null });
  assert.deepEqual(routes.pageContext('https://claude.ai/new'), { provider: 'claude', slot: null });
  for (const path of ['/u/2/app', '/u/2/app/c_abcdef0123456789?authuser=2', '/app?authuser=2'])
    assert.deepEqual(routes.pageContext('https://gemini.google.com' + path), {
      provider: 'gemini',
      slot: '2',
    });
});

test('locked, archived, shared, authenticated utility and ambiguous account surfaces have no picker', () => {
  assert.ok(routes, 'shared provider route policy is installed');
  for (const url of [
    home + 'locked',
    home + 'g/locked-chats/project',
    home + 'share/' + chat,
    home + '#settings/DataControls',
    home + '?view=locked-chats',
    home + '?modal=archive',
    'http://chatgpt.com/',
    'https://x@chatgpt.com/',
    'https://chatgpt.com:8443/',
    'https://chatgpt.com.other.invalid/',
    'https://claude.ai/login',
    'https://gemini.google.com/u/1/app?authuser=2',
    'https://gemini.google.com/app?authuser=user%40example.com',
    'https://gemini.google.com/app?authuser=1&authuser=2',
    'https://gemini.google.com/share/public',
  ])
    assert.equal(routes.pageContext(url), null, url);
});

test('ChatGPT project tiers normalize native IDs, including compact IDs and named project links', () => {
  assert.ok(routes, 'shared provider route policy is installed');
  for (const href of [
    '/g/' + project + '/project',
    '/g/' + project.toUpperCase() + '-named-%D1%82/project?x=1',
  ])
    assert.deepEqual(routes.rowFromHref(href, home), { kind: 'project', id: project });
  for (const href of [
    '/c/' + chat,
    '/g/' + project + '-research/c/' + chat,
    '/g/g-AbC123-a-gpt/c/' + chat,
  ])
    assert.deepEqual(routes.rowFromHref(href, home), { kind: 'conversation', id: chat });
  assert.deepEqual(routes.projectButton(project, home), { kind: 'project', id: project });
  assert.equal(routes.projectButton(chat, home), null);
  assert.equal(routes.projectButton(project, 'https://claude.ai/new'), null);
  for (const href of [
    '#link',
    '?x=1',
    '/c/not-a-uuid',
    '/share/' + chat,
    '/g/custom/c/' + chat,
    '/g/' + project + '/project/files',
    'https://example.org/c/' + chat,
    'javascript:void(0)',
  ])
    assert.equal(routes.rowFromHref(href, home + 'c/' + chat), null, href);
});

test('Claude and Gemini rows use native API identifiers, not folders or a different Google account', () => {
  assert.ok(routes, 'shared provider route policy is installed');
  const claude = 'https://claude.ai/new';
  assert.deepEqual(routes.rowFromHref('/project/' + chat, claude), { kind: 'project', id: chat });
  assert.deepEqual(routes.rowFromHref('/chat/' + chat, claude), { kind: 'conversation', id: chat });
  assert.equal(routes.rowFromHref('/share/' + chat, claude), null);
  const gemini = 'https://gemini.google.com/u/2/app';
  for (const href of ['/u/2/app/abcdef123456abcd', '/app/c_abcdef123456abcd?authuser=2'])
    assert.deepEqual(routes.rowFromHref(href, gemini), {
      kind: 'conversation',
      id: 'abcdef123456abcd',
    });
  for (const href of [
    '/app?authuser=2',
    '/u/1/app/abcdef123456abcd',
    '/app/abcdef123456abcd?authuser=1',
    '/app/gems',
    '/app/settings',
    '/app/gem/my-gem',
    '/share/abcdef123456abcd',
  ])
    assert.equal(routes.rowFromHref(href, gemini), null, href);
});
