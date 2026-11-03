/* DEMO2 pass 8 (Sol r3, rehearsal r3).
   R3-01: a route paints its own shell and loading state at once, issues its
   independent reads together, and reuses the session's filter values and
   brand/show names across routes (one read per workspace and view
   revision; a write, a new view revision or another workspace reads again).
   R3-02: Overview's "Latest new/past match:" names the latest record's kind
   and review state. R3-03: the Muse totals put 4 matching ad records, 3
   included and 1 awaiting brand review in one place. R3-04: an exact-phrase
   scope reads exact phrase “Meta's Muse”, one quote style.
   Harness copied from frontend_demo2_pass6.test.cjs.
   Run: node --test tests/frontend_demo2_pass8.test.cjs */
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

function loadPages(route, fetch = sessionFetch) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch,
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
const tick = () => new Promise(res => setTimeout(res, 0));
async function app(route, { wide = false } = {}) {
  const win = loadPages(route);
  await tick();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.matchMedia = () => ({ matches: wide });
  const headers = [];
  win.EW.header = section => { headers.push(section); };
  return { win, main, headers };
}
async function go(ctx, route) {
  ctx.win.location.hash = route;
  ctx.win.EW.current.route = route;
  await ctx.win.EWViews.render();
  return ctx.main.innerHTML;
}
async function pageHtml(route, api) {
  const ctx = await app(route);
  ctx.win.EW.api = async p => api(p);
  return go(ctx, route);
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');

// Data the search route reads.
const BRANDS = [{ key: 'quince', display: 'Quince' }];
const SHOWS = [{ id: 's1', display_title: 'Show One' }];
const bundle = (revision = 'rev-1') => ({ result: { items: [], total: 0, total_relation: 'exact' }, summaries: {}, revision,
  query: { version: 2, mode: 'mentions', q: 'Muse' }, selector: null });
const answer = (p, revision) => {
  if (p === '/customer/brands') return { brands: BRANDS };
  if (p === '/customer/shows') return { shows: SHOWS };
  if (p.startsWith('/customer/filter-values')) return { items: [], revision: 'rev-1' };
  if (p === '/customer/research-query') return bundle(revision);
  if (p === '/customer/preferences') return { preferences: {} };
  throw new Error('unexpected ' + p);
};
const kindOf = p => (p.startsWith('/customer/filter-values') ? 'filter-values:' + new URLSearchParams(p.split('?')[1]).get('field') : p.split('?')[0]);

// -- R3-01 independent reads together ---------------------------------------
test('R3-01 Search issues its result read with its name and filter reads, before any of them answers', async () => {
  const ctx = await app('#/search?mode=mentions&q=Muse', { wide: true });
  const asked = [];
  const held = [];
  ctx.win.EW.api = (p) => { asked.push(kindOf(p)); return new Promise(res => held.push(() => res(answer(p)))); };
  const done = go(ctx, '#/search?mode=mentions&q=Muse');
  for (let i = 0; i < 5; i++) await tick();
  assert.deepEqual([...new Set(asked)].sort(), ['/customer/brands', '/customer/research-query', '/customer/shows',
    'filter-values:brands', 'filter-values:categories', 'filter-values:shows'],
    'the result read waits for the name reads');
  while (held.length) held.shift()();
  await done;
});

test('R3-01 a campaign page asks for its draft and its reports together', async () => {
  const ctx = await app('#/campaign/1');
  const asked = [];
  const held = [];
  ctx.win.EW.api = (p) => {
    asked.push(p);
    if (p === '/customer/campaigns/1') return Promise.resolve({ campaign: { id: 1, name: 'Muse', mode: 'research', targets: [] } });
    return new Promise(res => held.push(() => res(p.endsWith('/reports') ? { reports: [] } : { draft: { campaign: { id: 1 }, placements: [], targets: [] } })));
  };
  const done = go(ctx, '#/campaign/1');
  for (let i = 0; i < 5; i++) await tick();
  assert.ok(asked.includes('/customer/campaigns/1/draft'));
  assert.ok(asked.includes('/customer/campaigns/1/reports'), 'reports wait for the draft');
  while (held.length) held.shift()();
  await done;
});

// -- R3-01 shell and loading state at once ---------------------------------
test('R3-01 a route paints its section and its own loading state before its reads answer', async () => {
  for (const [route, section, label] of [['#/campaign/1', 'Campaigns', /Opening the campaign…/], ['#/alerts', 'Alerts', /Opening alerts…/],
    ['#/brand/quince', 'Brands', /Opening the brand…/], ['#/overview', 'Overview', /Opening Overview…/], ['#/saved', 'Saved', /Opening Saved…/]]) {
    const ctx = await app(route);
    const held = [];
    let open = false;
    ctx.win.EW.api = () => (open ? Promise.resolve({}) : new Promise(res => held.push(res)));
    const done = go(ctx, route);
    for (let i = 0; i < 3; i++) await tick();
    assert.deepEqual(ctx.headers, [section], route + ' paints its section first');
    assert.match(ctx.main.innerHTML, label, route + ' says what is opening');
    open = true;
    while (held.length) held.shift()({});
    await done;
  }
});

// -- R3-01 the session's names and filter values are read once -----------------
async function searchTwice({ between = async () => {}, revisions = ['rev-1', 'rev-1'] } = {}) {
  const ctx = await app('#/search?mode=mentions&q=Meta', { wide: true });
  const asked = [];
  let call = 0, seen = revisions[0];
  // Filter values report the view revision the server is at (pass 12:
  // a fixed rev-1 beside a rev-2 research read is a revision change).
  ctx.win.EW.api = async (p) => {
    asked.push(kindOf(p));
    if (p === '/customer/research-query') seen = revisions[Math.min(call++, revisions.length - 1)];
    const out = answer(p, p === '/customer/research-query' ? seen : undefined);
    return p.startsWith('/customer/filter-values') ? { ...out, revision: seen } : out;
  };
  await go(ctx, '#/search?mode=mentions&q=Meta');
  await between(ctx);
  await go(ctx, '#/search?mode=mentions&q=Muse');
  await go(ctx, '#/search?mode=ads&brands=quince');
  const n = k => asked.filter(a => a === k).length;
  return { n, ctx };
}

test('R3-01 later routes reuse the filter values and names, never read them again', async () => {
  const { n } = await searchTwice();
  assert.equal(n('/customer/research-query'), 3);
  for (const k of ['/customer/brands', '/customer/shows', 'filter-values:brands', 'filter-values:shows', 'filter-values:categories']) {
    assert.equal(n(k), 1, k + ' read again on a later route');
  }
});

test('R3-01 a new view revision reads the names and filter values again', async () => {
  const { n } = await searchTwice({ revisions: ['rev-1', 'rev-2', 'rev-2'] });
  assert.equal(n('/customer/brands'), 2);
  assert.equal(n('filter-values:shows'), 2);
});

test('R3-01 another workspace reads the names and filter values again', async () => {
  const { n } = await searchTwice({ between: async ctx => { ctx.win.EW.current.workspaceContext = 'ctx-other'; } });
  assert.equal(n('/customer/brands'), 2);
  assert.equal(n('filter-values:categories'), 2);
});

test('R3-01 a failed name read is not kept', async () => {
  const ctx = await app('#/alerts');
  let fail = true, asked = 0;
  ctx.win.EW.api = async (p) => { if (p === '/customer/shows') { asked++; if (fail) { fail = false; throw new Error('down'); } return { shows: SHOWS }; } return answer(p); };
  // Pass 11: reuse needs the route's own succeeded read (authorizedBy).
  const ok = { authorizedBy: {} };
  await ctx.win.EW.sharedRead('/customer/shows', ok).catch(() => null);
  await ctx.win.EW.sharedRead('/customer/shows', ok);
  await ctx.win.EW.sharedRead('/customer/shows', ok);
  assert.equal(asked, 2);
});

test('R3-01 a saved change reads the names again (real request path)', async () => {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push(`${init.method || 'GET'} ${url}`);
    if (url === '/api/v1/session') return sessionFetch(url);
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(url.endsWith('/brands') ? { brands: BRANDS } : { ok: true }) };
  };
  const win = loadPages('#/overview', fetch);
  await tick();
  const E = win.EW;
  const ok = { authorizedBy: {} };
  await E.sharedRead('/customer/brands', ok);
  await E.sharedRead('/customer/brands', ok);
  assert.equal(calls.filter(c => c === 'GET /api/v1/customer/brands').length, 1);
  await E.api('/customer/saved-items', { method: 'POST', body: { kind: 'brand', brand: 'quince' } }).catch(() => null);
  await E.sharedRead('/customer/brands', ok);
  assert.equal(calls.filter(c => c === 'GET /api/v1/customer/brands').length, 2, 'a write keeps the old names');
});

test('R3-01 each caller gets its own copy of the shared answer', async () => {
  const ctx = await app('#/alerts');
  ctx.win.EW.api = async p => answer(p);
  const a = await ctx.win.EW.sharedRead('/customer/brands', { authorizedBy: {} });
  a.brands.push({ key: 'x' });
  const b = await ctx.win.EW.sharedRead('/customer/brands', { authorizedBy: {} });
  assert.equal(b.brands.length, 1);
});

// -- R3-02 Overview latest match --------------------------------------------
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

// PLAIN-ADS (DEP7): every ad is "ad", whatever its review; a removed one says so.
test('R3-02 the latest past match names its record: mention, ad, removed not an ad', async () => {
  for (const [kind, status, words] of [['new_text', null, 'a transcript mention'], ['new_finding', 'finding', 'an ad'],
    ['new_finding', 'unresolved', 'an ad'], ['new_finding', 'confirmed', 'an ad'], ['new_finding', 'rejected', 'removed: not an ad']]) {
    const t = text(await pageHtml('#/overview', overviewApi([total({ latest_past_kind: kind, latest_past_status: status })])));
    assert.match(t, new RegExp(`No new matches\\. 313 earlier matches\\. Latest past match: ${words}\\.`), `${kind}/${status}`);
  }
});

test('R3-02 the latest new match names its record the same way', async () => {
  for (const [kind, status, words] of [['new_text', null, 'a transcript mention'], ['new_finding', 'rejected', 'removed: not an ad']]) {
    const t = text(await pageHtml('#/overview', overviewApi([total({ new_ads: 2, ads: 315, latest_new_kind: kind, latest_new_status: status })])));
    assert.match(t, new RegExp(`Latest new match: ${words}\\.`), `${kind}/${status}`);
  }
});

// -- R3-03 Muse totals ------------------------------------------------------
const T1 = '2026-10-14T09:00:00Z';
const pastEv = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:p' + id, read_state: 1, historical: 1,
  ad_id: 'ad' + id, observed_at: '2026-10-14T16:27:59+00:00', ...over });
const MUSE = { id: 7, name: 'Meta’s Muse mentions', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, unread_event_count: 0, unread_ad_count: 0, query: { version: 2, mode: 'mentions', q: '"Meta\'s Muse"' },
  events: [...Array.from({ length: 16 }, (_, i) => pastEv(i + 1, { kind: 'new_text', ad_id: null })), pastEv(17), pastEv(18), pastEv(19)],
  baseline_held_for_brand_review: 1, baseline_held_ads_for_brand_review: 1,
  current_matches: { count: 16, tracked: 19, breaks: 3, tracked_by_kind: { new_text: 16, new_finding: 3 }, held_for_brand_review: 1, held_ads_for_brand_review: 1 } };
const alertsApi = alerts => p => {
  if (p === '/customer/alerts') return { alerts };
  if (p.startsWith('/customer/alerts/')) return { alert: alerts.find(a => '/customer/alerts/' + a.id === p) };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
};
const asideOf = html => html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>'));

test('R3-03 the alert row and header put the included, passage, ad and awaiting counts together', async () => {
  const want = /19 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · 16 passages, 3 ads · plus 1 ad with brand not identified/;
  assert.match(text(await pageHtml('#/alerts', alertsApi([MUSE]))), want);
  assert.match(text(await pageHtml('#/alert/7', alertsApi([MUSE]))), want);
  const none = text(await pageHtml('#/alerts', alertsApi([{ ...MUSE, baseline_held_for_brand_review: 0, baseline_held_ads_for_brand_review: 0 }])));
  assert.match(none, /19 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · 16 passages, 3 ads/);
  assert.doesNotMatch(none, /awaiting/);
  const two = text(await pageHtml('#/alerts', alertsApi([{ ...MUSE, baseline_held_for_brand_review: 2, baseline_held_ads_for_brand_review: 2, events: [pastEv(1)] }])));
  assert.match(two, /1 match(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · plus 2 ads with brand not identified/);
});

test('R3-03 Current matches says what Search shows and what the alert holds out, in one place', async () => {
  const t = text(asideOf(await pageHtml('#/alert/7', alertsApi([MUSE]))));
  assert.match(t, /Search shows 16 matching passages and 4 matching ad records\. This alert includes 3 of those ads; 1 more has Brand not identified and isn’t counted here\./);
  assert.doesNotMatch(t, /waits for brand review|It also watches/);
  const many = text(asideOf(await pageHtml('#/alert/7', alertsApi([{ ...MUSE,
    current_matches: { count: 1, tracked: 2, breaks: 1, tracked_by_kind: { new_text: 1, new_finding: 1 }, held_for_brand_review: 2, held_ads_for_brand_review: 2 } }]))));
  assert.match(many, /Search shows 1 matching passage and 3 matching ad records\. This alert includes 1 of those ads; 2 more have Brand not identified and aren’t counted here\./);
});

// -- R3-04 one quote style ---------------------------------------------------
test('R3-04 an exact-phrase scope reads exact phrase “Meta\'s Muse” wherever a saved scope is summarized', async () => {
  const list = text(await pageHtml('#/alerts', alertsApi([MUSE])));
  assert.match(list, /All mentions · exact phrase “Meta's Muse” · All brands · All shows/);
  assert.doesNotMatch(list, /“"|"”/);
  const win = loadPages('#/saved');
  const saved = win.EWSaved.describeQuery({ version: 2, mode: 'mentions', q: '"Meta\'s Muse"' });
  assert.equal(saved, 'All mentions · exact phrase “Meta\'s Muse”');
  assert.equal(win.EWSaved.describeQuery({ version: 2, mode: 'mentions', q: 'Muse' }), 'All mentions · “Muse”');
  const scope = win.EWViews.alertScopeText({ version: 2, mode: 'mentions', q: '"Meta\'s Muse"' }, new Map(), []);
  assert.equal(scope, 'This alert watches: All mentions · matching exact phrase “Meta\'s Muse”.');
});
