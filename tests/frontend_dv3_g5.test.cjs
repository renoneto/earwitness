/* Design v3 G5: Saved tab counts, Settings display preferences,
   Connections channels. Loads the real app scripts in a stubbed
   browser sandbox (helpers copied from frontend_query.test.cjs).
   Run: node --test tests/frontend_dv3_g5.test.cjs */
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

function loadViews(fetch, route) {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// Local owner session + a preferences store the PUT really changes.
function server(prefs, calls) {
  const state = { prefs: { ...prefs } };
  const fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    calls.push({ url, method, body: init.body ? JSON.parse(init.body) : null });
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: state.prefs });
    if (url === '/api/v1/customer/preferences' && method === 'PUT') {
      const body = JSON.parse(init.body);
      if (Number(body.expected_revision) !== state.prefs.revision) return json({ ok: false, error: { code: 'CONFLICT', message: 'stale revision', current_revision: state.prefs.revision } }, 412);
      delete body.expected_revision;
      state.prefs = { ...state.prefs, ...body, revision: state.prefs.revision + 1 };
      return json({ ok: true, workspace_context: 'ctx-ws', preferences: state.prefs, revision: state.prefs.revision });
    }
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
  return { fetch, state };
}
async function settingsWin(prefs, calls = []) {
  const { fetch, state } = server(prefs, calls);
  const win = loadViews(fetch, '#/settings');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  return { win, main, state, toasts };
}
// A minimal settings-display form: the fields the submit reads.
function displayForm(win, entries, main) {
  const field = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }, focus() {}, parentElement: null };
  const slot = { textContent: '' };
  const state = { dataset: {}, textContent: '' };
  const errors = { innerHTML: '', setAttribute() {}, focus() {} };
  const submit = { disabled: false, textContent: 'Save changes' };
  const form = {
    dataset: {}, isConnected: true, elements: [],
    getAttribute: a => (a === 'id' ? 'settings-display' : null),
    closest: () => null, contains: () => false,
    querySelector: s => ({ '#pref-display-name': field, '[data-field-error]': slot, '.ew-save-state': state,
      '[type=submit]': submit, '[data-form-errors]': errors }[s] || null),
    querySelectorAll: () => [],
  };
  win.FormData = class { *entries() { for (const e of entries) yield e; } };
  main.querySelector = s => (s === '#settings-display' ? form : null);
  return { form, field, slot, state, errors };
}

test('G5 Saved tabs show the server counts per collection, and no number it does not know', async () => {
  const win = loadAt('#/saved?tab=excerpts');
  const paths = [];
  const api = async (p) => {
    paths.push(p);
    if (p.startsWith('/customer/saved-items')) return { items: [], total: 0, total_relation: 'exact', counts: { excerpt: 4, query: 3, bookmark: 2 }, next_cursor: null };
    if (p.startsWith('/customer/archive')) return { items: [], total: 5, total_relation: 'exact', next_cursor: null };
    throw new Error('notes read failed');
  };
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  assert.match(html, /Excerpts <span class="ew-tab-count">4<\/span>/);
  assert.match(html, /Queries <span class="ew-tab-count">3<\/span>/);
  assert.match(html, /Bookmarks <span class="ew-tab-count">2<\/span>/);
  assert.match(html, /Reports <span class="ew-tab-count">5<\/span>/);
  assert.match(html, />Standalone notes<\/a>/, 'a failed count read shows no number, never 0');
  assert.doesNotMatch(html, /notice error/, 'a failed count read is not a failed tab');
  assert.ok(paths.includes('/customer/archive?limit=1') && paths.includes('/customer/mistakes?limit=1'), paths.join(' | '));
});

test('G5 Saved rows say Current / Changed since saved / Unavailable from the server state', async () => {
  const win = loadAt('#/saved?tab=bookmarks');
  const items = [
    { id: 1, type: 'bookmark', title: 'Show A', revision: 1, availability: 'current', target: { kind: 'show', id: 's1' } },
    { id: 2, type: 'bookmark', title: 'Show B', revision: 1, availability: 'source_changed', availability_reason: 'newer', target: { kind: 'show', id: 's2' } },
    { id: 3, type: 'bookmark', title: 'Tracked brand unavailable', revision: 1, availability: 'unavailable', availability_reason: 'gone', target: { kind: 'brand', id: null } },
  ];
  const api = async () => ({ items, total: 3, total_relation: 'exact', counts: { excerpt: 0, query: 0, bookmark: 3 }, next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  assert.match(html, /data-tag="ink">Current</);
  assert.match(html, /data-tag="info">Changed since saved</);
  assert.match(html, /data-tag="gap">.*Unavailable</);
  assert.match(html, /Changed since saved: the source was updated after you saved\. Your copy keeps what you saw\./);
  assert.doesNotMatch(html, /#\/brand\/(null|undefined|")/);
});

test('G5 Settings shows the stored display preferences, and defaults only where none is stored', async () => {
  const { main } = await settingsWin({ revision: 4, search_tab: 'episodes', results_view: 'list', default_range: '30d', time_zone: 'Europe/London', clip_lead_in: true, display_name: 'Reno' });
  const html = main.innerHTML;
  assert.match(html, /<form id="settings-display"/);
  assert.match(html, /<option value="episodes" selected>Episodes<\/option>/);
  assert.match(html, /value="list" checked/);
  assert.match(html, /<option value="30d" selected>Last 30 days<\/option>/);
  assert.match(html, /<option value="Europe\/London" selected>Europe\/London<\/option>/);
  assert.doesNotMatch(html, /clip_lead_in/);
  assert.match(html, /id="pref-display-name"[^>]*value="Reno"/);
  // The workspace name section stays, below; the role and sharing proposals are read-only now (astra decision 10).
  assert.ok(html.indexOf('id="settings-display"') < html.indexOf('id="settings-workspace"'));
  for (const id of ['settings-roles', 'settings-share']) assert.doesNotMatch(html, new RegExp(`<form id="${id}"`));
  const empty = (await settingsWin({ revision: 0 })).main.innerHTML;
  assert.match(empty, /<option value="ads" selected>Ads<\/option>/);
  assert.match(empty, /value="cards" checked/);
  assert.match(empty, /<option value="" selected>|<option value="">This device/);
  assert.doesNotMatch(empty, /clip_lead_in/);
  assert.match(empty, /id="pref-display-name"[^>]*value=""/);
});

test('G5 Save changes sends the display preferences with the shown revision and refreshes the cached preferences', async () => {
  const calls = [];
  const { win, main } = await settingsWin({ revision: 4 }, calls);
  const before = calls.filter(c => c.url === '/api/v1/customer/preferences' && c.method === 'GET').length;
  const { form } = displayForm(win, [['expected_revision', '4'], ['display_name_set', ''], ['search_tab', 'mentions'], ['results_view', 'list'],
    ['default_range', '7d'], ['time_zone', 'Europe/London'], ['clip_lead_in', '1'], ['display_name', '  Reno  ']], main);
  await win.EWViews.handleSubmit(form); await settleAll();
  const put = calls.find(c => c.method === 'PUT');
  assert.deepEqual(put.body, { search_tab: 'mentions', results_view: 'list', default_range: '7d', time_zone: 'Europe/London',
    display_name: 'Reno', expected_revision: 4 });
  // Other pages read the saved values without asking the server again.
  const prefs = await win.EW.preferences();
  assert.equal(prefs.search_tab, 'mentions');
  assert.equal(win.EW.prefResultsView(), 'list');
  assert.equal(win.EW.clipStart(10), 7);
  // The page re-rendered with the saved state line.
  assert.match(main.innerHTML, /data-state="saved"[^>]*>✓ Saved <time datetime="[^"]+" title="[^"]+ UTC">\d{1,2}:\d{2}/);
  const gets = calls.filter(c => c.url === '/api/v1/customer/preferences' && c.method === 'GET').length;
  assert.equal(gets, before + 1, 'one re-read for the re-render, none for the cache');
});

test('G5 an unset display name stays unset; a set one cannot be blanked, and nothing is sent', async () => {
  const calls = [];
  const { win, main } = await settingsWin({ revision: 2 }, calls);
  let f = displayForm(win, [['expected_revision', '2'], ['display_name_set', ''], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', ''], ['display_name', '']], main);
  await win.EWViews.handleSubmit(f.form); await settleAll();
  const put = calls.find(c => c.method === 'PUT');
  assert.ok(put, 'saved without a name');
  assert.equal('display_name' in put.body, false);
  assert.equal('clip_lead_in' in put.body, false);
  const n = calls.filter(c => c.method === 'PUT').length;
  f = displayForm(win, [['expected_revision', '3'], ['display_name_set', '1'], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', ''], ['display_name', '   ']], main);
  await win.EWViews.handleSubmit(f.form); await settleAll();
  assert.equal(calls.filter(c => c.method === 'PUT').length, n, 'nothing sent');
  assert.equal(f.slot.textContent, 'Enter a name, up to 60 characters.');
  assert.equal(f.field.attrs['aria-invalid'], 'true');
  assert.equal(f.state.textContent, 'Not saved. Your changes are kept. Try again.');
  assert.equal(f.state.dataset.state, 'failed');
});

test('G5 a refused save says it was not saved and keeps the entries (no rebase)', async () => {
  const calls = [];
  const { win, main } = await settingsWin({ revision: 5 }, calls);
  const f = displayForm(win, [['expected_revision', '4'], ['display_name_set', ''], ['search_tab', 'episodes'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', '']], main);
  await win.EWViews.handleSubmit(f.form); await settleAll();
  assert.equal(calls.filter(c => c.method === 'PUT').length, 1, 'one attempt, no retry');
  assert.equal(f.state.dataset.state, 'failed');
  assert.equal(f.state.textContent, 'Not saved. Your changes are kept. Try again.');
  assert.match(f.errors.innerHTML, /This record changed while you were editing\./);
  assert.equal((await win.EW.preferences()).search_tab, undefined, 'the cache keeps the server state');
});

test('G5 saving a workspace proposal drops the cached preferences', async () => {
  const calls = [];
  const { win } = await settingsWin({ revision: 1, search_tab: 'ads' }, calls);
  await win.EW.preferences();
  const gets = () => calls.filter(c => c.url === '/api/v1/customer/preferences' && c.method === 'GET').length;
  const n = gets();
  await win.EW.preferences();
  assert.equal(gets(), n, 'cached');
  const form = { dataset: {}, isConnected: true, elements: [], getAttribute: a => (a === 'id' ? 'settings-workspace' : null), closest: () => null,
    querySelector: () => null, querySelectorAll: () => [] };
  win.FormData = class { *entries() { yield ['expected_revision', '1']; yield ['workspace_name', 'Acme']; } };
  await win.EWViews.handleSubmit(form); await settleAll();
  const afterRender = gets();
  await win.EW.preferences();
  assert.equal(gets(), afterRender + 1, 'the cache was dropped: read again after the save');
});

test('G5 Connections: in the app is on; email, Slack and webhook say not set up, with no live Set up', async () => {
  const { fetch } = server({ revision: 0 }, []);
  const win = loadViews(fetch, '#/connections');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  win.location.hash = '#/connections'; win.EW.current.route = '#/connections';
  await win.EWViews.render(); await settleAll();
  const html = main.innerHTML;
  assert.match(html, /<h3>In the app<\/h3>[^]*data-tag="confirmed">[^]*On<\/span>/);
  for (const name of ['Email', 'Slack', 'Webhook']) {
    assert.match(html, new RegExp(`<h3>${name}</h3>[^]*?Not set up · not available yet`));
  }
  // UX review R2-10b: no setup flow exists, so no (disabled) Set up button is drawn.
  assert.doesNotMatch(html, />Set up<\/button>/);
  assert.doesNotMatch(html, /Meaning/);
  assert.doesNotMatch(html, /<form id="settings-reader"/, 'the reader proposal is read-only: no form');
  assert.match(html, /data-action="check-reader"/);
});

test('G5 Saved item counts follow the server relation: none without one, at least N for a lower bound', async () => {
  const win = loadAt('#/saved?tab=queries');
  const render = (rel) => win.EWSaved.render({ esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null,
    api: async (p) => {
      if (p.startsWith('/customer/saved-items')) return { items: [], total: 0, ...(rel ? { total_relation: rel } : {}), counts: { excerpt: 4, query: 0, bookmark: 2 }, next_cursor: null };
      return { items: [], notes: [], total: 1, total_relation: 'exact', next_cursor: null };
    } });
  const none = await render(null);
  assert.match(none, />Excerpts<\/a>/);
  assert.match(none, />Bookmarks<\/a>/);
  assert.match(none, /Reports <span class="ew-tab-count">1<\/span>/, 'other tabs keep their own relation');
  const lower = await render('lower_bound');
  assert.match(lower, /Excerpts <span class="ew-tab-count"><span aria-hidden="true">4\+<\/span><span class="visually-hidden">at least 4<\/span><\/span>/);
  const other = await render('estimated');
  assert.match(other, /Excerpts <span class="ew-tab-count">4 · estimated<\/span>/);
  const exact = await render('exact');
  assert.match(exact, /Excerpts <span class="ew-tab-count">4<\/span>/);
});

// ---- review fixes (round 2) ----
// A server whose PUT waits on a gate, then answers with a status/body.
function gatedServer(prefs, calls, gate, answer) {
  const base = server(prefs, calls);
  const fetch = async (url, init = {}) => {
    if (url === '/api/v1/customer/preferences' && (init.method || 'GET') === 'PUT') {
      calls.push({ url, method: 'PUT', body: JSON.parse(init.body) });
      await gate.promise;
      return json(answer.body, answer.status);
    }
    return base.fetch(url, init);
  };
  return fetch;
}
async function gatedSettings(answer) {
  const calls = [];
  const gate = {}; gate.promise = new Promise(res => { gate.open = res; });
  const fetch = gatedServer({ revision: 5 }, calls, gate, answer);
  const win = loadViews(fetch, '#/settings');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {}; win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  return { win, main, gate };
}
const REFUSED = { status: 412, body: { ok: false, error: { code: 'CONFLICT', message: 'stale revision', current_revision: 6 } } };
const ENTRIES = [['expected_revision', '5'], ['display_name_set', ''], ['search_tab', 'ads'], ['results_view', 'cards'], ['default_range', 'all'], ['time_zone', '']];

test('G5 Settings shows Saving… in the status line while the save is in flight', async () => {
  const { win, main, gate } = await gatedSettings(REFUSED);
  const f = displayForm(win, ENTRIES, main);
  const p = win.EWViews.handleSubmit(f.form); await settleAll();
  assert.equal(f.state.textContent, 'Saving…');
  assert.equal(f.state.dataset.state, 'saving');
  gate.open(); await p; await settleAll();
  assert.equal(f.state.dataset.state, 'failed');
});

test('G5 a failed save is written to the form that is live after a mid-save re-render', async () => {
  const { win, main, gate } = await gatedSettings(REFUSED);
  const a = displayForm(win, ENTRIES, main);
  const p = win.EWViews.handleSubmit(a.form); await settleAll();
  // A re-render swapped the form while the save was in flight.
  a.form.isConnected = false;
  const b = displayForm(win, ENTRIES, main);
  gate.open(); await p; await settleAll();
  assert.equal(b.state.dataset.state, 'failed', 'the live form says it failed');
  assert.equal(b.state.textContent, 'Not saved. Your changes are kept. Try again.');
  assert.notEqual(a.state.dataset.state, 'failed', 'the replaced form is not written to');
});

test('G5 a refused time zone says so, and other refusals keep the generic line', async () => {
  const tz = await gatedSettings({ status: 400, body: { ok: false, error: { code: 'BAD_REQUEST', message: 'time_zone must be an IANA time zone name or empty' } } });
  const f = displayForm(tz.win, ENTRIES, tz.main);
  const p = tz.win.EWViews.handleSubmit(f.form); await settleAll();
  tz.gate.open(); await p; await settleAll();
  assert.equal(f.state.textContent, "Not saved. That time zone isn't supported. Your changes are kept. Try again.");
});

test('G5 Saved hh:mm follows the time zone preference, not the device zone', async () => {
  const { win, main } = await settingsWin({ revision: 4, time_zone: 'Asia/Tokyo' });
  win.EW.current.settingsSaved = { ctx: 'ctx-ws', at: new Date('2026-10-14T00:05:00Z') };
  await win.EWViews.render(); await settleAll();
  assert.match(main.innerHTML, /✓ Saved <time datetime="2026-10-14T00:05:00Z" title="Oct 14, 2026, 12:05 AM UTC">9:05\s?AM<\/time>/);
});

test('G5 saved dates are day-only and use the time zone preference', async () => {
  const win = loadAt('#/saved?tab=bookmarks');
  assert.equal(win.EW.formatDay('2026-10-14T16:28:00Z', { time_zone: 'America/Los_Angeles' }), 'Oct 14');
  assert.equal(win.EW.formatDay('2026-10-14T16:28:00Z', { time_zone: 'Pacific/Auckland' }), 'Oct 15');
  assert.equal(win.EW.formatDay('2026-10-10', {}), win.EW.date('2026-10-10'));
  assert.equal(win.EW.formatDay('garbage', {}), 'Date unknown');
  const item = { id: 1, type: 'bookmark', title: 'Show A', revision: 1, availability: 'current', saved_at: '2026-10-14T16:28:00Z', target: { kind: 'show', id: 's1' } };
  const api = async (p) => (p.startsWith('/customer/saved-items') ? { items: [item], total: 1, total_relation: 'exact', counts: {}, next_cursor: null }
    : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  // d16 decision 14: the visible label is day-only; the UTC time is on hover (title).
  assert.match(html, /saved <time datetime="2026-10-14T16:28:00Z" title="Oct 14, 2026, 4:28 PM UTC">Oct 1[45]<\/time>/);
  const visible = html.replace(/ title="[^"]*"/g, '');
  assert.doesNotMatch(visible, /saved Oct 1[45],? \d/);
  assert.doesNotMatch(visible, /\b(AM|PM|UTC|GMT|P[SD]T)\b/);
});

test('G5 archive and note dates are day-only too', async () => {
  const win = loadAt('#/saved?tab=reports');
  const rows = [{ kind: 'report', id: 1, campaign_id: 3, report_no: 1, title: 'R', captured_at: '2026-10-14T16:28:00Z', stale: true, stale_reasons: ['campaign changed'] },
    { kind: 'export', id: 2, title: 'E', format: 'csv', captured_at: '2026-10-14T16:28:00Z', stale: false }];
  const api = async (p) => (p.startsWith('/customer/archive') ? { items: rows, total: 2, total_relation: 'exact', next_cursor: null }
    : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null, counts: {} });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  assert.match(html, /frozen <time datetime="2026-10-14T16:28:00Z" title="[^"]*">Oct 1[45]<\/time>/);
  assert.match(html, /captured <time datetime="2026-10-14T16:28:00Z" title="[^"]*">Oct 1[45]<\/time>/);
  assert.doesNotMatch(html.replace(/ title="[^"]*"/g, ''), /\b(AM|PM|UTC|GMT|P[SD]T)\b/, 'the visible label has no clock; UTC is on hover');
});

test('G5 a stale archive row uses the uncertain tag next to Frozen, not the old amber tag', async () => {
  const win = loadAt('#/saved?tab=reports');
  const rows = [{ kind: 'report', id: 1, campaign_id: 3, report_no: 1, title: 'R', captured_at: '2026-10-14T16:28:00Z', stale: true, stale_reasons: ['campaign changed'] }];
  const api = async (p) => (p.startsWith('/customer/archive') ? { items: rows, total: 1, total_relation: 'exact', next_cursor: null }
    : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null, counts: {} });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  assert.match(html, /data-tag="uncertain">[^<]*(<[^>]*>[^<]*)*Stale: /);
  assert.doesNotMatch(html, /amber/);
});

test('G5 a Saved row with no availability from the server shows no availability tag', async () => {
  const win = loadAt('#/saved?tab=bookmarks');
  const items = [{ id: 1, type: 'bookmark', title: 'Show A', revision: 1, target: { kind: 'show', id: 's1' } }];
  const api = async () => ({ items, total: 1, total_relation: 'exact', counts: {}, next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  assert.match(html, /Show A/);
  assert.doesNotMatch(html, /data-tag="(ink|neutral|info|gap)">[^<]*(Current|Historical|Changed|Unavailable)/);
});

test('G5 the Saved nav has no leftover #saved-tab select handler', () => {
  assert.doesNotMatch(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), /saved-tab/);
  assert.doesNotMatch(fs.readFileSync(path.join(APP, 'saved.js'), 'utf8'), /id="saved-tab"/);
});

// Replaces the old #saved-tab select checks (frontend_query UI25 / ui15-X4):
// the Saved tabs are now plain links, and they go through the router's draft guard.
test('G5 switching the Saved tab with an unsaved page draft asks first: Stay keeps the route, Discard goes to the tab', async () => {
  const win = loadAt('#/saved?tab=excerpts');
  const html = await win.EWSaved.render({ esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null,
    api: async (p) => (p.startsWith('/customer/saved-items') ? { items: [], total: 0, total_relation: 'exact', counts: {}, next_cursor: null }
      : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null }) });
  const href = (html.match(/<a href="([^"]+)"[^>]*>Standalone notes/) || [])[1];
  assert.equal(href, '#/saved?tab=notes', 'the Notes tab is a hash link');
  assert.doesNotMatch(html.match(/<a href="#\/saved\?tab=notes"[^>]*>/)[0], /data-action/, 'no action handler bypasses the guard');

  // A fresh app with the Settings page draft the user typed.
  const store = {};
  const listeners = {};
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  const draft = { dataset: { dirty: '1' }, isConnected: true, elements: [{ type: 'text', name: 'display_name', value: 'Typed', disabled: false }],
    getAttribute: a => (a === 'id' ? 'settings-display' : null), closest: () => null };
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, location: { hash: '#/settings' }, history: { replaceState() {} }, scrollTo() {},
    fetch: async () => { throw new Error('no network'); },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: s => (s === '#modal' ? modal : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } }),
      querySelectorAll: s => (/data-dirty/.test(s) ? [draft] : []),
      addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {}, setTimeout: () => 0,
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox.addEventListener = () => {};
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  const E = sandbox.window.EW;
  E.current.route = '#/settings'; E.header = () => {};
  const went = []; E.go = r => { went.push(r); E.current.route = r; };
  const anchor = { getAttribute: () => href, hasAttribute: () => false, target: '' };
  const click = () => { for (const fn of listeners.click) fn({ target: { closest: s => (s === 'a[href^="#/"]' ? anchor : null) }, button: 0, preventDefault() {} }); };

  click();
  assert.equal(modal.open, true, 'the leave prompt opens');
  assert.match(modal.innerHTML, /Leave this page\?/);
  assert.deepEqual(went, [], 'nothing navigated yet');
  await sandbox.window.EWViews.handleAction({ dataset: { action: 'leave-stay' } });
  assert.equal(modal.open, false);
  assert.equal(E.current.route, '#/settings', 'Stay keeps the current route');
  assert.deepEqual(went, []);
  assert.equal(draft.dataset.dirty, '1', 'the draft is untouched');

  click();
  assert.equal(modal.open, true, 'asks again');
  await sandbox.window.EWViews.handleAction({ dataset: { action: 'leave-discard' } });
  assert.deepEqual(went, ['#/saved?tab=notes'], 'Discard proceeds to the tab');
});
