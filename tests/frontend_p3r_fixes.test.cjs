/* P3 rerun follow-ups, customer app (P3R-1, Connections nav).
   P3R-1: a hosted session shows no "local" wording in Overview's
          "Collection scope & limits", Settings' Collection scope, the
          Search Audio filter or the alert dialog's checked-against line. Local
          mode keeps its own wording.
   Connections: a hosted session whose pilot has agent access off has
          no Connections nav item, and #/connections shows the off
          state without reading the reader routes. Local mode, and a
          hosted session with agent access on, keep the item.
   Run: node --test tests/frontend_p3r_fixes.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const LOCAL = /\blocal(ly)?\b/i;

function load(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/', href: 'http://127.0.0.1:9174/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
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
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&amp;/g, '&').replace(/\s+/g, ' ');

const OVERVIEW = { coverage: { episodes: 3, episodes_with_transcript: 3, shows: 2 }, recent_events: [], offer_sequence: [],
  alert_totals: [], alert_lifecycle_counts: { active_local: 0, paused: 0, stopped: 0 }, tracked_campaigns: [], campaign_gaps: [], activity: [] };

// A page rendered through the real router, hosted (signed in, workspace
// chosen) or local. The header renders for real into #header.
async function page(route, { hosted, features, role = 'viewer', api = () => ({}) } = {}) {
  const win = load(route);
  await settle();
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const headerEl = { innerHTML: '' };
  const el = () => ({ innerHTML: '', textContent: '', hidden: false, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [] });
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? headerEl : el());
  win.document.querySelectorAll = () => [];
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.toast = () => {};
  win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, o = {}) => { calls.push(`${o.method || 'GET'} ${p}`); return api(p, o); };
  if (hosted) {
    win.EW.current.hosted = true;
    win.EW.current.principal = 'oidc:https://idp.example:judge1';
    win.EW.current.activeWorkspace = 'judges-demo';
    win.EW.current.workspaces = ['judges-demo'];
    win.EW.current.workspaceContext = 'ctx-ws';
    // The session the boot adopted, handed to this render.
    win.EW.current.adoptedSession = { authenticated: true, role, ...(features ? { features } : {}) };
  }
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settle();
  return { win, html: main.innerHTML, header: headerEl.innerHTML, calls };
}
const OFF = { invites: true, sharing: false, agent_access: false };
const ON = { invites: true, sharing: false, agent_access: true };
const overviewApi = p => (p === '/customer/overview' ? OVERVIEW : p.startsWith('/customer/search') ? { items: [] }
  : p.startsWith('/customer/saved-items') ? { items: [] } : p === '/customer/brands' ? { brands: [] } : {});
const scopeOf = html => (String(html).match(/<details class="corpus-disclosure[\s\S]*?<\/details>/) || [''])[0];

// ---- P3R-1: hosted wording ----------------------------------------------------

test('P3R-1 hosted Overview "Collection scope & limits" has no local wording', async () => {
  const { html } = await page('#/overview', { hosted: true, features: OFF, api: overviewApi });
  const scope = visible(scopeOf(html));
  assert.match(scope, /Collection scope & limits/);
  assert.match(scope, /3 episodes · 2 shows · 3 with transcripts/);
  assert.doesNotMatch(scope, LOCAL, scope);
  assert.match(scope, /Counts describe the stored collection only\./);
});

test('P3R-1 local Overview keeps its local collection wording', async () => {
  const { html } = await page('#/overview', { hosted: false, api: overviewApi });
  const scope = visible(scopeOf(html));
  assert.match(scope, /Stored local collection · 3 episodes/);
  assert.match(scope, /Counts describe the stored local collection only\./);
});

const settingsApi = p => (p === '/customer/preferences' ? { preferences: { revision: 1 } } : {});
const collectionScope = html => visible((String(html).match(/<h2>Collection scope<\/h2>[\s\S]*?<\/section>/) || [''])[0]);

test('P3R-1 hosted Settings collection scope has no local wording', async () => {
  const { html } = await page('#/settings', { hosted: true, features: OFF, api: settingsApi });
  const text = collectionScope(html);
  assert.match(text, /Collection scope/);
  assert.doesNotMatch(text, LOCAL, text);
  assert.match(text, /Counts describe the stored collection only\./);
});

test('P3R-1 local Settings keeps its local collection wording', async () => {
  const { html } = await page('#/settings', { hosted: false, api: settingsApi });
  assert.match(collectionScope(html), /Counts describe the stored local collection only\./);
});

test('P3R-1 hosted Search Audio filter says "Recording known"; local keeps "(local or remote)"', async () => {
  const win = load('#/search');
  await settle();
  const audio = html => visible((String(html).match(/<fieldset[^>]*data-group="audio"[\s\S]*?<\/fieldset>/) || [''])[0]);
  win.EW.current.hosted = true;
  const hosted = audio(win.EWViews.filtersBody({}, { brands: [], shows: [], categories: [] }));
  assert.match(hosted, /Recording known/);
  assert.doesNotMatch(hosted, LOCAL, hosted);
  win.EW.current.hosted = false;
  const local = audio(win.EWViews.filtersBody({}, { brands: [], shows: [], categories: [] }));
  assert.match(local, /Recording known \(local or remote\)/);
});

async function alertDialogText(hosted) {
  const win = load('#/alerts');
  await settle();
  const fake = () => ({ innerHTML: '', textContent: '', hidden: false, dataset: {}, value: '', classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null, addEventListener() {}, appendChild() {}, insertBefore() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [] });
  const modal = { ...fake(), open: false, showModal() { this.open = true; }, close() { this.open = false; }, querySelector: () => fake() };
  win.document.querySelector = s => (s === '#modal' ? modal : fake());
  win.EW.current.hosted = hosted;
  win.EW.api = async p => (p === '/customer/shows' ? { shows: [] } : p === '/customer/brands' ? { brands: [] } : {});
  let body = '';
  win.EW.openDialog = (title, html) => { body = html; };
  await win.EWViews.handleAction({ dataset: { action: 'alerts-create' }, disabled: false, closest: () => null });
  await settle();
  return visible(body + ' ' + modal.innerHTML);
}

test('P3R-1 hosted alert dialog says "stored data"; local keeps "stored local data"', async () => {
  const hosted = await alertDialogText(true);
  assert.match(hosted, /Checked against stored data only\./);
  assert.doesNotMatch(hosted, LOCAL, hosted);
  assert.match(await alertDialogText(false), /Checked against stored local data only\./);
});

// ---- Connections nav and route ------------------------------------------------

const navItems = header => [...String(header).matchAll(/<a href="#\/([a-z]+)"/g)].map(m => m[1]);

test('Connections nav: hosted with agent access off has no Connections item', async () => {
  const { header } = await page('#/overview', { hosted: true, features: OFF, api: overviewApi });
  const items = navItems(header);
  assert.ok(items.includes('settings'), items.join(','));
  assert.ok(!items.includes('connections'), items.join(','));
  assert.doesNotMatch(header, />Connections</);
});

test('Connections nav: hosted with agent access on keeps the item', async () => {
  const { header } = await page('#/overview', { hosted: true, features: ON, api: overviewApi });
  assert.ok(navItems(header).includes('connections'));
});

test('Connections nav: local mode keeps the item', async () => {
  const { header } = await page('#/overview', { hosted: false, api: overviewApi });
  assert.ok(navItems(header).includes('connections'));
});

test('Connections route: hosted with agent access off shows the off state, no reader reads', async () => {
  const { html, calls, header } = await page('#/connections', { hosted: true, features: OFF,
    api: p => { throw new Error('unexpected read ' + p); } });
  const text = visible(html);
  assert.match(text, /Agent access is off for this pilot/);
  assert.doesNotMatch(text, LOCAL, text);
  assert.doesNotMatch(text, /failed|unavailable:/i, text);
  assert.deepEqual(calls, []);
  assert.ok(!navItems(header).includes('connections'));
});

test('Connections route: local mode still opens the Connections page', async () => {
  const { html, calls } = await page('#/connections', { hosted: false,
    api: p => (p === '/customer/reader-status' ? { configured: false, profile: {} } : p === '/customer/preferences' ? { preferences: {} } : {}) });
  assert.match(visible(html), /Where alerts can be delivered/);
  assert.ok(calls.includes('GET /customer/reader-status'), calls.join(','));
});

// ---- Sol round 1 (p3r-acceptance-r1.md): FIX-1..FIX-4, hosted only -------------

const dataSection = html => (String(html).match(/<section class="surface pad data-connections">[\s\S]*?<\/section>/) || [''])[0];

test('FIX-1 hosted Settings with agent access off: "Data source", no Connections link', async () => {
  // JE-SETTINGS: the operator line is the owner's; a viewer gets no section.
  assert.equal(dataSection((await page('#/settings', { hosted: true, features: OFF, api: settingsApi })).html), '');
  const { html } = await page('#/settings', { hosted: true, features: OFF, role: 'owner', api: settingsApi });
  const sec = dataSection(html);
  assert.match(sec, /<h2>Data source<\/h2>/);
  assert.match(visible(sec), /The reader for this workspace is set by the operator\. Agent connections are unavailable in this pilot\./);
  assert.doesNotMatch(sec, /#\/connections|Manage connections|See Connections|Data &amp; connections/);
});

test('FIX-1 hosted with agent access on, and local mode, keep the Connections treatment', async () => {
  for (const opts of [{ hosted: true, features: ON }, { hosted: false }]) {
    const sec = dataSection((await page('#/settings', { ...opts, api: settingsApi })).html);
    assert.match(sec, /Data &amp; connections/, JSON.stringify(opts));
    assert.match(sec, /Manage connections →/);
    assert.match(visible(sec), /See Connections\./);
  }
});

const T1 = '2026-10-14T16:27:59+00:00';
const ALERT = { id: 5, name: 'Shopify', revision: 1, definition_revision: 2, last_successful_definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, unread_event_count: 0, query: { version: 2, mode: 'ads', q: '', brands: ['shopify'] },
  current_matches: { count: 0, tracked: 0, tracked_by_kind: {}, held_for_brand_review: 0 }, events: [] };
const alertApi = alert => p => (p === '/customer/alerts/5' ? { alert } : p === '/customer/shows' ? { shows: [] }
  : p === '/customer/brands' ? { brands: [] } : p.startsWith('/customer/alerts') ? { alerts: [{ ...alert }] } : {});
const EVENT = { id: 1, kind: 'new_finding', after_ref: 'sr1:1', read_state: 0, historical: false, observed_at: T1, definition_revision: 1, checked_at: T1,
  summary: { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'S', ep_title: 'E', pub_date: '2026-10-09', span: [10, 40], text: 'words',
    read: { brand: { key: 'shopify', display: 'Shopify', confirmed: true }, needs_review: false, historical: false } } };
async function alertPageHtml(hosted, alert = ALERT) {
  return (await page('#/alert/5', { hosted, features: hosted ? OFF : undefined, api: alertApi(alert) })).html;
}

test('FIX-2 hosted alert page: stored-records wording, no "local"', async () => {
  const html = await alertPageHtml(true);
  const text = visible(html);
  assert.match(text, /Last checked against stored records:/);
  assert.match(html, /<h3>Check stored records<\/h3>/);
  assert.match(html, /data-action="check-alert"[^>]*>Check stored records</);
  assert.doesNotMatch(text, LOCAL, text);
  // Checked under earlier settings, with an event: the latest-check note.
  const unchecked = visible(await alertPageHtml(true, { ...ALERT, unread_event_count: 1, events: [EVENT] }));
  assert.match(unchecked, /Use Check stored records to set the new starting point\./);
  assert.doesNotMatch(unchecked, LOCAL, unchecked);
  const empty = visible(await alertPageHtml(true, { ...ALERT, last_successful_definition_revision: 2 }));
  assert.match(empty, /Press Check stored records later to see new ones\./);
  const nobase = visible(await alertPageHtml(true, { ...ALERT, last_successful_at: null, last_successful_definition_revision: null, baseline_complete: false }));
  assert.match(nobase, /or use Check stored records once the scope fits\./);
  assert.match(nobase, /or run Check stored records once the scope fits\./);
  assert.doesNotMatch(nobase, LOCAL, nobase);
});

test('FIX-2 local alert page keeps its local wording', async () => {
  const html = await alertPageHtml(false);
  assert.match(visible(html), /Last checked locally:/);
  assert.match(html, /<h3>Check local records<\/h3>/);
});

test('FIX-2 the save banner, rendered: stored records when hosted, a local check locally', async () => {
  // The banner shows on the one render a save opened (savedAlert).
  const banner = async hosted => {
    const win = load('#/alert/5'); await settle();
    const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
    const el = () => ({ innerHTML: '', textContent: '', hidden: false, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, removeAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [] });
    win.document.querySelector = sel => (sel === '#main' ? main : el());
    win.document.querySelectorAll = () => [];
    win.setTimeout = () => {}; win.scrollTo = () => {};
    win.EW.toast = () => {}; win.EW.saveNotice = () => {};
    win.EW.api = async p => alertApi(ALERT)(p);
    if (hosted) Object.assign(win.EW.current, { hosted: true, principal: 'oidc:https://idp.example:judge1', activeWorkspace: 'judges-demo',
      workspaces: ['judges-demo'], workspaceContext: 'ctx-ws', adoptedSession: { authenticated: true, role: 'editor', features: OFF } });
    win.EW.current.savedAlert = '5';
    await win.EWViews.render(); await settle();
    return visible((String(main.innerHTML).match(/<div class="notice" role="status">Saved[\s\S]*?<\/div>/) || [''])[0]);
  };
  assert.equal((await banner(true)).trim(), 'Saved. New matches show here after you check stored records.');
  assert.equal((await banner(false)).trim(), 'Saved. New matches show here after a local check.');
});

test('FIX-2 failed-check retry names the hosted label', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /retry \$\{checkRecordsLabel\(\)\}\./);
});

test('FIX-3 (review r1) no hosted not-found path reaches the local dataset-revision copy', async () => {
  const win = load('#/episode/e1'); await settle();
  const E = win.EW;
  assert.match(E.notFound(), /different local dataset revision/, 'local mode keeps it');
  E.current.hosted = true; E.current.activeWorkspace = 'ws-b';
  const cases = [{}, { route: true }, { kind: 'episode' }, { kind: 'evidence' }, { kind: 'no-such-kind' }];
  for (const leftWs of ['', 'ws-a']) {
    E.current.recordLeftWs = leftWs;
    for (const opts of cases) {
      const text = visible(E.notFound(opts));
      assert.doesNotMatch(text, /dataset revision/, JSON.stringify({ leftWs, opts }));
      assert.doesNotMatch(text, LOCAL, JSON.stringify({ leftWs, opts }));
    }
  }
});

test('FIX-4 hosted boot failure: "EarWitness is unavailable"; loopback local keeps the server-start text', async () => {
  const run = async (hostname) => {
    const store = {};
    const main = { innerHTML: '' };
    const sandbox = {
      console, URLSearchParams, AbortController, URL, Intl, setTimeout, clearTimeout,
      location: { hash: '#/overview', pathname: '/app/', hostname, href: `https://${hostname}/app/#/overview` },
      history: { replaceState() {} }, scrollTo() {},
      fetch: async () => { throw new TypeError('Failed to fetch'); },
      localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
      document: { querySelector: s => (s === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } }),
        querySelectorAll: () => [], addEventListener: () => {}, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
      window: {},
    };
    sandbox.window = sandbox; sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) {
      if (file === 'views.js') sandbox.addEventListener = () => {};
      vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
    }
    await settle(30);
    return { refused: sandbox.EW.current.bootRefused, html: main.innerHTML };
  };
  const hosted = await run('pilot.earwitness.test');
  assert.equal(hosted.refused, 'EarWitness is unavailable. Check your connection, then reload.');
  assert.match(hosted.html, /EarWitness is unavailable\. Check your connection, then reload\./);
  assert.doesNotMatch(visible(hosted.html), LOCAL);
  for (const h of ['127.0.0.1', 'localhost', '[::1]']) {
    assert.equal((await run(h)).refused, 'The local server is unreachable. Start it, then reload.', h);
  }
});

// ---- Review r1 of 04cdfcd: test gaps and dead-end links ----------------------------

test('review r1 (a) a hosted session that names no features keeps Connections', async () => {
  const { header } = await page('#/overview', { hosted: true, api: overviewApi });
  assert.ok(navItems(header).includes('connections'), navItems(header).join(','));
});

test('review r1 (b) a hosted session with agent access on opens the real Connections page', async () => {
  const { html, calls } = await page('#/connections', { hosted: true, features: ON,
    api: p => (p === '/customer/reader-status' ? { configured: false, profile: {} } : p === '/customer/preferences' ? { preferences: {} } : {}) });
  assert.match(visible(html), /Where alerts can be delivered/);
  assert.doesNotMatch(visible(html), /Agent access is off/);
  assert.ok(calls.includes('GET /customer/reader-status'), calls.join(','));
});

const connLinks = html => (String(html).match(/href="#\/connections"/g) || []).length;

test('review r1 Alerts: no Connections links while agent access is off; kept when on and locally', async () => {
  const list = async opts => (await page('#/alerts', { ...opts, api: alertApi(ALERT) })).html;
  const detail = async opts => (await page('#/alert/5', { ...opts, api: alertApi(ALERT) })).html;
  const offList = await list({ hosted: true, features: OFF });
  assert.match(visible(offList), /Alerts show here in the app\. EarWitness sends no email\./);
  assert.equal(connLinks(offList), 0);
  const offDetail = await detail({ hosted: true, features: OFF });
  assert.match(visible(offDetail), /Alerts show here in the app\. EarWitness sends no email\./);
  assert.doesNotMatch(visible(offDetail), /No live monitoring/);
  assert.doesNotMatch(offDetail, /See <a|Delivery connections/);
  assert.equal(connLinks(offDetail), 0);
  for (const opts of [{ hosted: true, features: ON }, { hosted: false }]) {
    assert.equal(connLinks(await list(opts)), 1, JSON.stringify(opts));
    const d = await detail(opts);
    assert.equal(connLinks(d), 2, JSON.stringify(opts));
    assert.match(d, /Delivery connections →/);
  }
});

// ---- Coordinator pass 3: personal notes (server delivery_context is always local_only) ----

const NOTE = { id: 7, reason: 'Check the brand read', note_status: 'open', revision: 1, created_at: T1, updated_at: T1, delivery_context: 'local_only' };
const noteApi = p => (p === '/customer/mistakes/7' ? { note: NOTE } : p.startsWith('/customer/mistakes') ? { notes: [NOTE], total: 1, total_relation: 'exact' }
  : p.startsWith('/customer/archive') || p.startsWith('/customer/saved-items') ? { items: [], total: 0, total_relation: 'exact' } : {});

test('notes: hosted note page says Private note, visible only to you; no "local"', async () => {
  const { html } = await page('#/note/7', { hosted: true, features: OFF, api: noteApi });
  const text = visible(html);
  assert.match(text, /Private note/);
  assert.match(text, /Visible only to you in this workspace\./);
  assert.doesNotMatch(text, LOCAL, text);
  assert.doesNotMatch(text, /no service received it/);
});

test('notes: local note page keeps "Local only — no service received it"', async () => {
  const text = visible((await page('#/note/7', { hosted: false, api: noteApi })).html);
  assert.match(text, /Local only/);
  assert.match(text, /Delivery: Local only — no service received it/);
});

test('notes: Saved · Notes tag is "Private note" hosted, "Local only" locally', async () => {
  const hosted = visible((await page('#/saved?tab=notes', { hosted: true, features: OFF, api: noteApi })).html);
  assert.match(hosted, /Check the brand read/);
  assert.match(hosted, /Private note/);
  assert.doesNotMatch(hosted, /Local only/);
  const local = visible((await page('#/saved?tab=notes', { hosted: false, api: noteApi })).html);
  assert.match(local, /Local only/);
});
