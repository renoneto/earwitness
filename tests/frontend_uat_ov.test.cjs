/* UAT 2026-10-15 fix round, UAT-009 (ov): Overview first-use.
   The first-use card shows only when the member has nothing set up: no
   alert that is not stopped (alert_lifecycle_counts from the overview),
   no campaign except archived ones (read-only campaigns count as set up
   but stay out of the Tracked campaigns count, VL-10/R1-07), no tracked
   brand, no alert change and no offer. Harness copied from
   frontend_uat_cui.test.cjs. Run: node --test tests/frontend_uat_ov.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9174/app/' + route },
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
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };

async function views(route = '#/overview') {
  const win = loadAt(route);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  return { win, modal, main, toasts };
}
const withApi = (t, api) => {
  t.calls = [];
  t.win.EW.api = async (p, opts = {}) => {
    t.calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  return t;
};
async function at(t, route) {
  t.win.location.hash = route; t.win.EW.current.route = route;
  await t.win.EWViews.render();
  return t.main.innerHTML;
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ')
  .replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ');

const empty = over => ({ coverage: { episodes: 1, episodes_with_transcript: 1, shows: 1 }, recent_events: [], offer_sequence: [],
  alert_totals: [], alert_lifecycle_counts: { active_local: 0, paused: 0, stopped: 0 }, tracked_campaigns: [], campaign_gaps: [], activity: [], ...over });
async function overview(ov, saved = []) {
  const t = withApi(await views(), p => {
    if (p === '/customer/overview') return ov;
    if (p.startsWith('/customer/search')) return { items: [] };
    if (p.startsWith('/customer/saved-items')) return { items: saved };
    if (p === '/customer/brands') return { brands: [] };
    return {};
  });
  return at(t, '#/overview');
}
const FIRST = /You’re not tracking anything yet/;
const readOnly = n => Array.from({ length: n }, (_, i) => ({ id: i + 1, name: 'Legacy ' + (i + 1), state: 'Read-only' }));

test('UAT-009 owner shape: 4 quiet alerts and 4 read-only campaigns are set up, not first use', async () => {
  const html = await overview(empty({ alert_lifecycle_counts: { active_local: 4, paused: 0, stopped: 0 }, tracked_campaigns: readOnly(4) }));
  const text = visible(html);
  assert.doesNotMatch(text, FIRST);
  assert.doesNotMatch(html, /ew-ov-first/);
  assert.match(text, /Alert activity/);
  assert.match(text, /No recent alert matches\. Alerts keep checking the stored collection\./);
  assert.match(html, /Tracked campaigns · 0</);
  assert.match(text, /4 read-only campaigns from before campaign types are on the Campaigns page\./);
  assert.match(text, /Recent ads/);
  assert.match(text, /Tracked brands/);
});

test('alerts alone (active or paused) are set up; the alerts card shows with no matches', async () => {
  for (const counts of [{ active_local: 1, paused: 0, stopped: 0 }, { active_local: 0, paused: 2, stopped: 1 }]) {
    const text = visible(await overview(empty({ alert_lifecycle_counts: counts })));
    assert.doesNotMatch(text, FIRST, JSON.stringify(counts));
    assert.match(text, /No recent alert matches/);
  }
});

test('read-only campaigns alone are set up and keep the VL-10 line', async () => {
  const text = visible(await overview(empty({ tracked_campaigns: readOnly(1) })));
  assert.doesNotMatch(text, FIRST);
  assert.match(text, /Tracked campaigns · 0/);
  assert.match(text, /1 read-only campaign from before campaign types is on the Campaigns page\./);
});

test('a truly empty workspace still shows the first-use card', async () => {
  const html = await overview(empty({}));
  assert.match(visible(html), FIRST);
  assert.doesNotMatch(visible(html), /Alert activity/);
  assert.doesNotMatch(visible(html), /Tracked campaigns/);
});

test('only stopped alerts and archived campaigns are nothing set up: first use', async () => {
  const html = await overview(empty({ alert_lifecycle_counts: { active_local: 0, paused: 0, stopped: 3 },
    tracked_campaigns: [{ id: 5, name: 'Done', state: 'Archived' }] }));
  assert.match(visible(html), FIRST);
});

test('a tracked brand alone is set up (unchanged)', async () => {
  const html = await overview(empty({}), [{ id: 1, target: { kind: 'brand', id: 'acme' }, title: 'Acme' }]);
  assert.doesNotMatch(visible(html), FIRST);
});

/* DEMO2-R3-01: a research campaign with targets or observations arrives
   as "Researching"; Overview shows that tag, not "Draft", and counts it
   among the campaigns that are set up. */
test('DEMO2-R3-01 Overview tags a research campaign with targets "Researching", an empty one "Draft"', async () => {
  const html = await overview(empty({ tracked_campaigns: [
    { id: 1, name: 'Meta’s Muse', state: 'Researching', brand: 'meta' },
    { id: 2, name: 'Blank', state: 'Draft', brand: 'acme' }] }));
  const text = visible(html);
  assert.doesNotMatch(text, FIRST);
  assert.match(text, /Tracked campaigns · 2/);
  assert.match(html, /Meta’s Muse<\/a><span class="tag green">Researching</);
  assert.match(html, /Blank<\/a><span class="tag green">Draft</);
  assert.doesNotMatch(html, /Meta’s Muse<\/a><span class="tag[^"]*">Draft</);
});

/* JUDGE-SMALL (lead 2026-10-25, READ-CACHE field audit): tracking is
   workspace-wide, so the Overview says "Tracked brands", never "Brands
   you track"; the first-use text names tracked brands too. */
test('Tracked brands: the Overview heading is "Tracked brands" and no Overview text says "you track"', async () => {
  const setUp = await overview(empty({ alert_lifecycle_counts: { active_local: 4, paused: 0, stopped: 0 } }));
  assert.match(setUp, /<section class="ew-card ew-ov-card ew-ov-brands"><div class="ew-card-head"><h2>Tracked brands<\/h2>/);
  const firstUse = await overview(empty({}));
  assert.match(firstUse, /ew-ov-first/, 'fixture: first use');
  assert.match(visible(firstUse), /Changes from your alerts and tracked brands will show up here\./);
  for (const html of [setUp, firstUse]) assert.doesNotMatch(html, /you track/i);
  // No other in-app text says it (comments aside).
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js', 'index.html']) {
    const lines = fs.readFileSync(path.join(__dirname, '..', 'app', file), 'utf8').split('\n')
      .filter(l => /you track/i.test(l) && !/^\s*(\/\/|\/\*|\*)/.test(l));
    assert.deepEqual(lines, [], file);
  }
});
