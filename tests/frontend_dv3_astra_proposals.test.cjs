/* Astra decision 10: the Settings role and sharing proposals and the
   Connections reader-scope proposal are no longer saveable. The stored
   values show read-only as "Not available in this workspace yet". Frontend
   only: the server endpoint and the stored values stay.
   Run: node --test tests/frontend_dv3_astra_proposals.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
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
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });

function server(prefs, calls = []) {
  const fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    calls.push({ url, method });
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: prefs });
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
  return fetch;
}
async function page(route, prefs, calls) {
  const win = loadAt(route, { fetch: server(prefs, calls) });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  win.location.hash = route; win.EW.current.route = route;
  await win.EWViews.render(); await settleAll();
  return main.innerHTML;
}
const block = (html) => {
  const start = html.indexOf('<details class="ew-settings-proposals">');
  assert.ok(start > 0, 'the collapsed not-active block is still there');
  return html.slice(start, html.indexOf('</details>', start));
};
const STORED = { revision: 4, role_matrix: { viewer: 'comment', reviewer: 'read' }, share_audience: 'named_reviewers', share_expiry: '2026-12-31',
  share_fields: ['Passage evidence', 'Episode metadata'], download_preference: 'watermark',
  reader_scope: ['search', 'notes'], reader_fields: ['title', 'excerpt'] };

test('Settings: no role or sharing proposal form, no proposal Save buttons; the workspace name save stays', async () => {
  const html = await page('#/settings', STORED);
  assert.doesNotMatch(html, /<form id="settings-roles"|<form id="settings-share"/);
  assert.doesNotMatch(html, /Save proposals|Save sharing proposals/);
  assert.doesNotMatch(html, /id="role-viewer"|id="role-reviewer"|id="settings-share-audience"|id="share-expiry"|id="download-preference"|id="settings-teams"/);
  assert.match(html, /<form id="settings-workspace"[^]*Save workspace name<\/button>/);
  assert.match(html, /<form id="settings-display"/);
  assert.ok(html.indexOf('<form id="settings-workspace"') < html.indexOf('<details class="ew-settings-proposals">'), 'workspace name outside the block');
});

test('Settings: the collapsed block shows the stored values read-only, labelled "Not available in this workspace yet"', async () => {
  const b = block(await page('#/settings', STORED));
  assert.match(b, /<summary>Sharing and role proposals · not active<\/summary>/);
  assert.match(b, /Not available in this workspace yet/);
  assert.doesNotMatch(b, /<(form|input|select|button|textarea)\b/, 'nothing to edit or submit');
  const dl = b.replace(/<\/?(dl|dt|dd|section|div|h2|span|p)[^>]*>/g, ' ').replace(/\s+/g, ' ');
  for (const [label, value] of [['Viewer role', 'Comment'], ['Reviewer role', 'Read only'], ['Share audience', 'Named reviewers'],
    ['Share link expiry', '2026-12-31'], ['Shared fields', 'Passage evidence, Episode metadata'], ['Downloads', 'Watermark downloads']]) {
    assert.ok(dl.includes(`${label} ${value}`), `${label}: ${value}`);
  }
});

test('Settings: values that were never stored read "Not set", never an invented default; stored text is escaped', async () => {
  const none = block(await page('#/settings', { revision: 0 }));
  assert.equal((none.match(/Not set/g) || []).length, 6, 'all six rows are Not set');
  assert.doesNotMatch(none, /Read only|Only me|Allow downloads/);
  const evil = block(await page('#/settings', { revision: 1, share_fields: ['<b>x</b>'], share_expiry: '<i>' }));
  assert.doesNotMatch(evil, /<b>x|<i>/);
  assert.match(evil, /&lt;b&gt;x&lt;\/b&gt;/);
});

test('Connections: no reader-scope form or Save button; stored scope and fields show read-only as not available yet', async () => {
  const html = await page('#/connections', STORED);
  assert.doesNotMatch(html, /<form id="settings-reader"|Save reader proposal|id="reader-fields"|name="reader_scope"/);
  assert.match(html, /data-action="check-reader"/, 'the real reader check stays');
  assert.equal((html.match(/Not set up · not available yet/g) || []).length, 3);
  const b = block(html);
  assert.match(b, /<summary>Reader scope proposal · not active<\/summary>/);
  assert.match(b, /Not available in this workspace yet/);
  assert.match(b, /The reader’s access is set by the operator\./);
  assert.doesNotMatch(b, /<(form|input|select|button|textarea)\b/);
  const dl = b.replace(/<\/?(dl|dt|dd|section|div|h2|span|p)[^>]*>/g, ' ').replace(/\s+/g, ' ');
  assert.ok(dl.includes('Proposed scope search, notes'), dl);
  assert.ok(dl.includes('Proposed fields title, excerpt'), dl);
  const none = block(await page('#/connections', { revision: 0 }));
  assert.equal((none.match(/Not set/g) || []).length, 2);
});

test('Settings and Connections make no write request while rendering', async () => {
  const calls = [];
  await page('#/settings', STORED, calls); await page('#/connections', STORED, calls);
  assert.deepEqual(calls.filter(c => c.method !== 'GET'), []);
});

test('no orphan handler, held-write selector, field or style for the removed forms', () => {
  const views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const css = fs.readFileSync(path.join(APP, 'account.css'), 'utf8');
  for (const gone of ['settings-roles', 'settings-share', 'settings-reader', 'role_viewer', 'role_reviewer', 'Save proposals', 'Save reader proposal', 'These are proposals only']) {
    assert.ok(!views.includes(gone), `views.js no longer mentions ${gone}`);
  }
  assert.ok(views.includes("id === 'settings-workspace'"), 'the workspace name submit stays');
  assert.match(views, /'settings-display', 'settings-workspace',\s*'invite-create'/, 'held-write list keeps display and workspace only');
  assert.doesNotMatch(css, /ew-settings-proposals > section > form|#settings-teams/);
});
