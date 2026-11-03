/* JE-SETTINGS (rev 1): Settings shows a judge (editor) no raw account ids
   and no controls the server refuses.
   1. Member names: the server's display field, else the email's local part,
      else "Member"; never the id (text, title, aria-label, data-*), except
      the owner's Remove value and the owner's technical details.
   2. The Create share form follows the sharing pilot switch.
   3. Remove shows only to an owner.
   4. Sharing off: no sharing fold and no "Sharing proposals never…" line,
      and no Data source operator line, for editors and viewers. Owners keep
      what they see today.
   Run: node --test tests/frontend_je_settings.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadViews(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  const win = sandbox.window;
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await flush(); };
const forbidden = (status, message) => Object.assign(new Error(message), { status });

const ISS = 'oidc:https://127.0.0.1:9259:';
const id = n => ISS + 'acct-' + String(n).repeat(32);
const ME = { editor: id(1), viewer: id(2), owner: id(7) };
// The live shape: 7 editors and 1 owner; names per the server's display field.
const MEMBERS = [
  { member: id(1), role: 'editor', email: 'judge.one@corp.example', display: 'judge.one' },
  { member: id(2), role: 'viewer', email: '', display: 'Pat Lee' },
  { member: id(3), role: 'editor', email: '', display: 'Member' },
  { member: id(4), role: 'editor', email: '' },                       // older server: no display
  { member: id(5), role: 'editor', email: 'sam.k@corp.example' },     // older server: email only
  { member: id(7), role: 'owner', email: '', display: 'Reno' },
];

const FEATURES_OFF = { invites: false, sharing: false, agent_access: false };
const FEATURES_ON = { invites: true, sharing: true, agent_access: false };

async function settingsAs(role, { features = FEATURES_OFF } = {}) {
  const route = '#/settings';
  const win = loadViews(route);
  await settle();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {};
  win.EW.toast = () => {};
  win.EW.saveNotice = () => {};
  // A password session carries its email (live: ops-check@…).
  const session = { authenticated: true, principal: ME[role], email: `${role}@screen.example`, role, features, active_workspace: 'ws', workspaces: ['ws'], frontend_contract: 'earwitness/customer-ui/2' };
  Object.assign(win.EW.current, { hosted: true, principal: ME[role], identity: { email: `${role}@screen.example`, subject: '' }, activeWorkspace: 'ws',
    workspaces: ['ws'], workspaceContext: 'ctx-ws', workspaceRole: role, pilotFeatures: features });
  win.EW.api = async (p) => {
    if (p === '/session') return session;
    if (p === '/customer/preferences') return { preferences: {} };
    if (p === '/workspaces/ws/members') return { members: MEMBERS };
    // Only an owner may list invitations (the live 403 for others).
    if (p === '/workspaces/ws/invites') { if (role !== 'owner') throw forbidden(403, 'owner manages membership'); return { invites: [] }; }
    if (p === '/shares') return { shares: [] };
    if (p === '/billing/status') return { configured: false };
    throw new Error('unexpected ' + p);
  };
  win.location.hash = route;
  await win.EWViews.render();
  await settle();
  return main.innerHTML;
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
const membersPart = html => (/<h2>Members<\/h2>[^]*?<\/section>/.exec(html) || [''])[0];
const names = html => [...membersPart(html).matchAll(/<div class="scope-line"><span>([^<]*?) · /g)].map(m => m[1]);

const DATA_SOURCE = /The reader for this workspace is set by the operator\./;
const FOLD = /Sharing and role proposals/;
const NEVER = /Sharing proposals never change coverage/;

for (const role of ['editor', 'viewer']) {
  test(`${role}, sharing off: no acct- anywhere, names by the rule, no share form, Remove, fold or operator line`, async () => {
    const html = await settingsAs(role);
    assert.match(html, /<h2>Members<\/h2>/);
    assert.equal((html.match(/acct-/g) || []).length, 0, 'no raw id in text, title, aria-label or data-*');
    // Lead 11-01: the session's own row reads "You".
    const want = ['judge.one', 'Pat Lee', 'Member', 'Member', 'sam.k', 'Reno'];
    want[role === 'editor' ? 0 : 1] = 'You';
    assert.deepEqual(names(html), want);
    assert.doesNotMatch(html, /id="share-create"|Create share/);
    assert.doesNotMatch(html, /data-action="member-remove"|>Remove</);
    assert.doesNotMatch(html, FOLD);
    assert.doesNotMatch(html, NEVER);
    assert.doesNotMatch(html, DATA_SOURCE);
    assert.doesNotMatch(html, /<h2>Data source<\/h2>/);
    assert.match(text(html), /Counts describe the .* only\./, 'the collection-scope sentence stays');
    assert.match(html, /<h2>Shares<\/h2>/, 'the share list stays');
  });

  test(`${role}, sharing on: the form, fold and sentence show as today; still no Remove or acct-`, async () => {
    const html = await settingsAs(role, { features: FEATURES_ON });
    assert.match(html, /<form id="share-create">/);
    assert.match(html, FOLD);
    assert.match(html, NEVER);
    assert.doesNotMatch(html, /data-action="member-remove"/);
    assert.equal((html.match(/acct-/g) || []).length, 0);
  });
}

test('owner, sharing off: Remove on every member with the id as its value; fold, sentence and Data source line kept', async () => {
  const html = await settingsAs('owner');
  const removes = [...html.matchAll(/data-action="member-remove" data-member="([^"]+)" data-name="([^"]*)"/g)];
  assert.deepEqual(removes.map(m => m[1]), MEMBERS.map(m => m.member));
  assert.deepEqual(removes.map(m => m[2]), ['judge.one', 'Pat Lee', 'Member', 'Member', 'sam.k', 'Reno']);
  assert.deepEqual(names(html), ['judge.one', 'Pat Lee', 'Member', 'Member', 'sam.k', 'You'], 'own row: You (lead 11-01); the Remove prompts above keep the name');
  assert.match(html, FOLD);
  assert.match(html, NEVER);
  assert.match(html, DATA_SOURCE);
  assert.match(html, /<h3>Invite someone<\/h3>/, 'the invite form is unchanged (its own 403 check)');
  assert.match(html, /Technical details/, 'the owner keeps the member id one click away');
  assert.doesNotMatch(html, /id="share-create"/, 'sharing off: the server refuses a share, so no form');
});

test('owner, sharing on: Remove, share form and fold all show', async () => {
  const html = await settingsAs('owner', { features: FEATURES_ON });
  assert.equal([...html.matchAll(/data-action="member-remove"/g)].length, MEMBERS.length);
  assert.match(html, /<form id="share-create">/);
  assert.match(html, FOLD);
  assert.match(html, NEVER);
});

test('a session that names no features keeps the share form (local and older servers)', async () => {
  const html = await settingsAs('editor', { features: null });
  assert.match(html, /<form id="share-create">/);
  assert.match(html, FOLD);
});

// RJS-01: the share form and fold follow the sharing switch alone, never the invites switch.
test('RJS-01: invites on with sharing off hides the form and fold; sharing on with invites off shows them', async () => {
  const offOn = await settingsAs('editor', { features: { invites: true, sharing: false, agent_access: false } });
  assert.doesNotMatch(offOn, /id="share-create"|Create share/);
  assert.doesNotMatch(offOn, FOLD);
  const onOff = await settingsAs('editor', { features: { invites: false, sharing: true, agent_access: false } });
  assert.match(onOff, /<form id="share-create">/);
  assert.match(onOff, FOLD);
});

// Lead 11-01 (JE-SETTINGS review RJS-02/03): the session's own member row reads "You"; the owner's
// Remove prompt keeps the member's name; the Display name field says who sees it.
test('own row reads "You"; the owner never gets "Remove You"; Display name help line', async () => {
  const owner = await settingsAs('owner');
  assert.deepEqual(names(owner), ['judge.one', 'Pat Lee', 'Member', 'Member', 'sam.k', 'You']);
  assert.doesNotMatch(owner, /data-name="You"/);
  assert.match(owner, /data-member="[^"]*" data-name="Reno">Remove</);
  for (const role of ['owner', 'editor', 'viewer']) {
    const html = await settingsAs(role);
    assert.match(html, /<p class="field-help" id="pref-display-name-help">Shown to other members of this workspace\.<\/p>/, role);
    assert.match(html, /<input id="pref-display-name"[^>]*aria-describedby="pref-display-name-help pref-display-name-error"/, role);
  }
});
