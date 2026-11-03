/* Design v3 UX review T3: Overview (R2-05d, UX-14a/b), Saved (UX-14c, R2-17,
   UX-15b), Settings and Connections proposals (UX-14d, R2-10), local invite/shared.
   Run: node --test tests/frontend_dv3_ux_t3-account.test.cjs */
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

// Overview harness (as frontend_dv3_g1-search.test.cjs).
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
  throw new Error('no network in unit test');
};
async function views(route, apiFn) {
  const win = loadViews(sessionFetch, route);
  await settleAll();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => apiFn(p, o);
  win.location.hash = route;
  win.EW.current.route = route;
  return { win, main, render: async () => { await win.EWViews.render(); return main.innerHTML; } };
}
const overviewData = (over = {}) => ({ coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [], ...over });
const overviewApi = ({ overview = overviewData(), saved = { items: [] }, brands = [], search = { items: [] } } = {}) => p => {
  if (p === '/customer/overview') return overview;
  if (p.startsWith('/customer/saved-items')) return saved;
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/search')) return search;
  throw new Error('unexpected ' + p);
};
const overviewHtml = async opts => (await views('#/overview', overviewApi(opts))).render();
const mk = n => Array.from({ length: n }, (_, i) => ({ id: i + 1, alert_id: 3, alert_name: 'Promo code mentions', kind: 'new_text', read_state: 0,
  observed_at: '2026-10-14T16:27:59+00:00', after_ref: 'sr1:' + i }));

// ---- R2-05d Overview exact alert totals ----
test('R2-05d Overview uses the exact per-alert totals; a paused alert says so; no "50+"', async () => {
  const html = await overviewHtml({ overview: overviewData({ recent_events: mk(50), alert_totals: [
    { alert_id: 3, alert_name: 'Promo code mentions', lifecycle: 'paused', events: 120, unread: 120, query: { version: 2, mode: 'mentions', q: 'promo code' },
      latest_kind: 'new_text', latest_after_ref: 'sr1:z', latest_observed_at: '2026-10-14T16:27:59+00:00' }] }) });
  assert.match(html, /120 changes · 120 unread\. Latest: New matching words\./);
  // SMALL-UI QW9c: the Paused tag, and the unread count as its own tag.
  assert.match(html, /data-tag="alert-state"><span class="ew-work-glyph" aria-hidden="true">❙❙<\/span>Paused</);
  assert.match(html, /data-tag="outline">120 unread</);
  assert.doesNotMatch(html, /50\+/);
  assert.doesNotMatch(html, /data-tag="new"/, 'no New tag on a paused alert');
  assert.match(html, /data-tag="words"/);
  assert.match(html, /href="#\/evidence\/sr1%3Az">Open evidence/);
});

test('R2-05d / UX-11d Overview: a brand alert gets the Brand tag; without totals the "+" stays and no Words tag is guessed', async () => {
  const brand = await overviewHtml({ overview: overviewData({ recent_events: mk(1), alert_totals: [
    { alert_id: 2, alert_name: 'BetterHelp reads', lifecycle: 'active_local', events: 21, unread: 21, query: { version: 2, mode: 'ads', q: '', brands: ['betterhelp'] },
      latest_kind: 'new_finding', latest_after_ref: 'sr1:b', latest_observed_at: '2026-10-14T16:27:59+00:00' }] }) });
  assert.match(brand, /data-tag="brand"/);
  assert.match(brand, /data-tag="new"/);
  const old = await overviewHtml({ overview: overviewData({ recent_events: mk(50) }) });
  assert.match(old, /50\+ recent changes · 50\+ unread/);
  assert.doesNotMatch(old, /data-tag="words"/);
});

// ---- UX-14a placements nest under their campaign ----
test('UX-14a placement gaps sit under their campaign; the campaign is listed once', async () => {
  const gaps = [
    { campaign_id: 1, campaign_name: 'BetterHelp Q4 host reads', placement_id: 'exp:a', outcome: 'unknown', linkage: 'unknown', label: 'Not enough data', placement_label: 'Episode: The Last 12 Weeks - Ep. 5', placement_href: '#/campaign/1/placement/exp:a' },
    { campaign_id: 1, campaign_name: 'BetterHelp Q4 host reads', placement_id: 'exp:b', outcome: 'unknown', linkage: 'unknown', label: 'Not enough data', placement_label: 'Serial · expects 2', placement_href: '#/campaign/1/placement/exp:b' },
  ];
  const html = await overviewHtml({ overview: overviewData({ tracked_campaigns: [{ id: 1, name: 'BetterHelp Q4 host reads', state: 'Tracking', brand: 'betterhelp' }], campaign_gaps: gaps }) });
  const card = html.slice(html.indexOf('Tracked campaigns'), html.indexOf('</section>', html.indexOf('Tracked campaigns')));
  assert.equal((card.match(/BetterHelp Q4 host reads/g) || []).length, 1);
  assert.match(card, /Episode: The Last 12 Weeks - Ep\. 5/);
  assert.match(card, /Serial · expects 2/);
  assert.match(card, /2 placements need attention/);
  assert.match(card, /<ul class="ew-ov-gaps">/);
  assert.doesNotMatch(card, /Needs attention · 2/);
  const stray = await overviewHtml({ overview: overviewData({ tracked_campaigns: [{ id: 1, name: 'A', state: 'Tracking' }],
    campaign_gaps: [{ ...gaps[0], campaign_id: 9, campaign_name: 'Unlisted' }] }) });
  assert.match(stray, /Needs attention · 1/);
  assert.match(stray, /Unlisted/);
});

// ---- UX-14b detections lead with brand ----
test('UX-14b recent detections lead with the brand; under review stays hidden; no "Evidence record"', async () => {
  const items = [
    { ref: 'sr1:w', record_kind: 'evidence', status: 'finding', ep_title: 'NBA One Guy (the West!)', show_title: 'S', pub_date: '2026-10-09', text: 'a', read: { brand: { key: 'whole', display: 'Whole', confirmed: false }, needs_review: false } },
    { ref: 'sr1:u', record_kind: 'evidence', status: 'finding', ep_title: 'Ep U', show_title: 'S', pub_date: '2026-10-09', text: 'b', read: { brand: { key: null, display: null, confirmed: false, under_review: true }, needs_review: true } },
    { ref: 'sr1:p', record_kind: 'transcript_passage', status: 'mention', ep_title: 'Ep P', show_title: 'S', pub_date: '2026-10-09', text: 'c' },
  ];
  const html = await overviewHtml({ search: { items } });
  assert.ok(html.indexOf('Whole') < html.indexOf('NBA One Guy'), 'brand before the episode title');
  assert.doesNotMatch(html, /Name not confirmed|Possible ad/, 'PLAIN-ADS: no review state');
  // PLAIN-ADS-FIX3 (screen R3-02): Recent ads lists only ads whose brand
  // shows: the held ad and the passage are left out.
  assert.doesNotMatch(html, /Brand not identified/);
  assert.doesNotMatch(html, /<strong>Mention<\/strong>/);
  assert.doesNotMatch(html, /Evidence record/);
  assert.doesNotMatch(html, /Ad read/);
});

// ---- UX-14c / R2-17 Saved rows ----
const savedHtml = async (route, items, archive = []) => {
  const win = loadAt(route);
  const api = async (p) => (p.startsWith('/customer/saved-items') ? { items, total: items.length, total_relation: 'exact', counts: {}, next_cursor: null }
    : p.startsWith('/customer/archive') ? { items: archive, total: archive.length, total_relation: 'exact', next_cursor: null }
      : { items: [], notes: [], total: 0, total_relation: 'exact', next_cursor: null, counts: {} });
  return win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
};
test('UX-14c a Saved excerpt row: the title opens it, the rest sits in one Actions menu with Remove last', async () => {
  const item = { id: 5, type: 'excerpt', title: 'Morning brew read', revision: 3, availability: 'current', saved_at: '2026-10-14T16:28:00Z',
    captured: { source_ref: 'sr1:pin' } };
  const html = await savedHtml('#/saved?tab=excerpts', [item]);
  const row = /<li class="saved-row">[^]*?<\/li>/.exec(html)[0];
  assert.match(row, /<h3 class="saved-title"><a class="" href="#\/evidence\/sr1%3Apin"/);
  assert.doesNotMatch(row, /Open saved source/);
  assert.match(row, /<details class="ew-row-menu"><summary class="button" aria-label="Actions for Morning brew read">Actions<\/summary>/);
  const panel = row.slice(row.indexOf('ew-row-menu-panel'));
  assert.match(panel, /Add note on saved source/);
  assert.match(panel, /data-action="edit-saved" data-id="5"/);
  assert.match(panel, /class="button quiet ew-row-menu-danger" data-action="delete-saved" data-id="5" data-rev="3"/);
  assert.ok(panel.indexOf('delete-saved') > panel.indexOf('edit-saved'), 'Remove is last');
});
test('R2-17 report rows open from the title and offer Download; export rows have a plain title', async () => {
  const rows = [{ kind: 'report', id: 1, campaign_id: 3, report_no: 2, title: 'Q4 report', captured_at: '2026-10-14T16:28:00Z', download_path: '/customer/archive/1/download' },
    { kind: 'export', id: 2, title: 'CSV export', format: 'csv', captured_at: '2026-10-14T16:28:00Z', download_path: '/customer/archive/2/download' }];
  const html = await savedHtml('#/saved?tab=reports', [], rows);
  assert.match(html, /<a class="" href="#\/campaign\/3\/report\/2">Q4 report<\/a>/);
  assert.doesNotMatch(html, />Open<\/a>/);
  assert.equal((html.match(/>Download<\/a>/g) || []).length, 2);
  assert.match(html, /<span>CSV export<\/span>/);
});
test('UX-14c / UX-15b Saved CSS: danger Remove, 44 px targets below 1024, static panel on narrow screens', () => {
  const css = fs.readFileSync(path.join(APP, 'account.css'), 'utf8');
  assert.match(css, /\.ew-row-menu-danger[^{]*\{[^}]*color: var\(--ew-gap\)/);
  assert.match(css, /@media \(max-width: 1023px\) \{[^@]*\.ew-row-menu-panel \{ position: static;[^@]*\.ew-row-menu > summary, \.ew-row-menu-panel > \* \{ min-height: 44px; \}/);
});

// ---- UX-14d Settings proposals ----
test('UX-14d Settings: the stored role and sharing proposals sit read-only in one collapsed "not active" block; the workspace name stays outside', async () => {
  const { main } = await settingsWin({ revision: 4 });
  const html = main.innerHTML;
  const start = html.indexOf('<details class="ew-settings-proposals">');
  assert.ok(start > 0, 'closed proposals block');
  const block = html.slice(start, html.indexOf('</details>', start));
  assert.match(block, /<summary>Sharing and role proposals · not active<\/summary>/);
  assert.match(block, /Not available in this workspace yet/);
  assert.doesNotMatch(block, /<form|<input|<select|<button/);
  assert.doesNotMatch(html, /<form id="settings-roles"|<form id="settings-share"/);
  assert.ok(html.indexOf('<form id="settings-workspace"') < start, 'workspace name outside');
  assert.doesNotMatch(html, /Workspace and sharing|not enforced/);
});

// ---- R2-10 Connections ----
test('R2-10 Connections: no Set up buttons; the reader proposal is read-only, collapsed and labelled not active', async () => {
  const calls = [];
  const { fetch } = server({ revision: 1 }, calls);
  const win = loadViews(fetch, '#/connections');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  win.location.hash = '#/connections'; win.EW.current.route = '#/connections';
  await win.EWViews.render(); await settleAll();
  const html = main.innerHTML;
  assert.doesNotMatch(html, />Set up<\/button>/);
  assert.equal((html.match(/Not set up · not available yet/g) || []).length, 3);
  assert.match(html, /<details class="ew-settings-proposals"><summary>Reader scope proposal · not active<\/summary>[^]*Not available in this workspace yet/);
  assert.doesNotMatch(html, /<form id="settings-reader"/);
});

// ---- R2-10c local invite/shared ----
test('R2-10c local #/invite and #/shared say they open on the hosted service; other unknown routes are Page not found', async () => {
  for (const route of ['#/invite?workspace=ws-a&id=inv-1', '#/shared?workspace=ws-a&id=sh-1']) {
    const t = await views(route, () => { throw new Error('no request expected'); });
    const html = await t.render();
    assert.match(html, /open on the hosted service/);
    assert.doesNotMatch(html, /different local dataset revision/);
  }
  // After the shell merge an address that matches no route is "Page not found" (R2-18f).
  const t = await views('#/nope-route', () => { throw new Error('no request expected'); });
  const other = await t.render();
  assert.match(other, /Page not found/);
  assert.doesNotMatch(other, /open on the hosted service/);
});
