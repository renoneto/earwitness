/* DEMO2 pass 5, Sol r2 wording: Overview "Alert activity" says new versus
   past matches (R1-05); the Brands index says "Last detected" while no
   brand has a confirmed ad (R1-08); Saved names its notes tab "Standalone
   notes" (R2-02); the search names-failed note names the IDs it shows
   (R2-03). Loads the real app scripts in a stubbed sandbox.
   Run: node --test tests/frontend_demo2_pass5_wording.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

function loadPages(route) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch: sessionFetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  return sandbox;
}
async function pageHtml(route, api) {
  const win = loadPages(route);
  await new Promise(res => setTimeout(res, 0));
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async p => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return main.innerHTML;
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/\s+/g, ' ');

// -- R1-05 Overview ---------------------------------------------------------
const overviewApi = totals => p => {
  if (p === '/customer/overview') return { coverage: {}, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [],
    alert_lifecycle_counts: { active_local: totals.length }, alert_totals: totals };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  if (p === '/customer/brands') return { brands: [] };
  if (p.startsWith('/customer/search')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const total = (over = {}) => ({ alert_id: 2, alert_name: 'New Mark Zuckerberg mentions', lifecycle: 'active_local',
  query: { version: 2, mode: 'mentions', q: 'Mark Zuckerberg' }, latest_observed_at: '2026-10-22T07:58:00Z',
  events: 313, unread: 0, ads: 313, unread_ads: 0, latest_kind: 'new_finding',
  new_ads: 0, past_ads: 313, latest_new_kind: null, latest_past_kind: 'new_finding', ...over });

test('R1-05 Overview calls the card Alert activity and says past, not changed', async () => {
  const t = text(await pageHtml('#/overview', overviewApi([total()])));
  assert.match(t, /Alert activity/);
  assert.doesNotMatch(t, /What changed in your alerts/);
  assert.match(t, /No new matches\. 313 earlier matches\. Latest past match: an ad\./);
  assert.doesNotMatch(t, /313 changes|New advertising passage/);
});

test('R1-05 Overview says new matches apart from past ones', async () => {
  const t = text(await pageHtml('#/overview', overviewApi([total({ new_ads: 2, unread_ads: 1, unread: 1, ads: 315,
    new_unread_ads: 1, latest_new_kind: 'new_text' })])));
  assert.match(t, /2 new matches · 1 unread\. 313 earlier matches\. Latest new match: a transcript mention\./);
});

// -- R1-08 Brands -----------------------------------------------------------
const brandsApi = brands => p => {
  if (p.startsWith('/customer/brands')) return { brands };
  if (p.startsWith('/customer/saved-items')) return { items: [], total: 0, total_relation: 'exact' };
  throw new Error('unexpected ' + p);
};
const brand = (key, confirmed) => ({ key, display: key[0].toUpperCase() + key.slice(1), ad_breaks: 9, confirmed_ads: confirmed, shows: 3, last_heard: '2026-10-21' });

// PLAIN-ADS (DEP7) replaces R1-08: the index always says Last heard and
// never counts confirmed ads, whatever the server's confirmed_ads.
test('PLAIN-ADS with no confirmed ad the Brands index says Last heard, header and rows', async () => {
  const html = await pageHtml('#/brands', brandsApi([brand('quince', 0), brand('chase', 0)]));
  assert.doesNotMatch(text(html), /[Ll]ast detected|confirmed/);
  assert.match(html, /<th scope="col" class="lib-wide lib-heard-col"[^>]*>Last heard</);
  assert.match(text(html), /9 ads · 3 shows · last heard Oct 21/);
});

test('PLAIN-ADS a brand with a confirmed ad reads the same', async () => {
  const html = await pageHtml('#/brands', brandsApi([brand('quince', 0), brand('chase', 2)]));
  assert.match(html, /<th scope="col" class="lib-wide lib-heard-col"[^>]*>Last heard</);
  assert.doesNotMatch(text(html), /confirmed|last detected/);
  assert.equal((text(html).match(/9 ads · 3 shows · last heard Oct 21/g) || []).length, 2);
});

// -- R2-02 Saved notes ------------------------------------------------------
test('R2-02 the notes tab is Standalone notes and its empty state points at Excerpts', async () => {
  const win = loadPages('#/saved?tab=notes');
  const api = async () => ({ items: [], notes: [], total: 0, total_relation: 'exact', counts: { excerpt: 4, query: 6, bookmark: 0 }, next_cursor: null });
  const t = text(await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null }));
  assert.match(t, /Standalone notes/);
  assert.match(t, /No standalone notes yet\./);
  assert.match(t, /Notes attached to saved excerpts appear under Excerpts\. Add a standalone note from any evidence page\./);
  assert.doesNotMatch(t, /No notes yet|Add a personal note/);
});

// -- R2-03 names-failed note ------------------------------------------------
async function searchHtml(route, brandsDown) {
  const bundle = { query: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, revision: 'r1',
    result: { items: [], total: 0, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  return pageHtml(route, p => {
    if (p === '/customer/brands') { if (brandsDown) throw Object.assign(new Error('slow'), { status: 504 }); return { brands: [{ key: 'quince', display: 'Quince' }] }; }
    if (p === '/customer/shows') throw Object.assign(new Error('slow'), { status: 504 });
    return bundle;
  });
}

test('R2-03 the names note names the ID it shows', async () => {
  const t = text(await searchHtml('#/search?mode=ads&brands=quince', true));
  assert.match(t, /Couldn't load the name of brand “quince”, so its ID is shown\./);
  assert.doesNotMatch(t, /Some brand or show names/);
});

test('R2-03 no note when every visible name loaded (a failed show read with no show filter)', async () => {
  const t = text(await searchHtml('#/search?mode=ads&brands=quince', false));
  assert.doesNotMatch(t, /couldn't be loaded|Couldn't load the name/i);
});
