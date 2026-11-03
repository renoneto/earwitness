/* DEMO2 pass 11.
   DEMO2-SEC-R1-01 (Astra) and DEMO2-R9-02: no route publishes cache-backed
   data without a fresh request that checks the caller's access on that
   navigation. Brands and Shows read their page fresh every time. Shared
   names and filter values are reused only beside a route's own fresh read
   that succeeded; a 401, 403 or 428 clears them and the page refuses as
   usual. The pass 8 reuse stays where the route's own read succeeded.
   DEMO2-R9-01: held ads are counted in ad units (the server's
   held_ads_for_brand_review / baseline_held_ads_for_brand_review), so the
   "Search shows N matching ad records" sentence adds up.
   Harness copied from frontend_demo2_pass8.test.cjs; only fetch is faked.
   Run: node --test tests/frontend_demo2_pass11.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };

function loadPages(route, fetch) {
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
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');

// A server whose answers can be switched to "access removed" (every
// workspace read answers the given status). Only fetch is simulated: the
// real EW.api, apiCall, sharedRead and render run.
function server(data) {
  const state = { refuse: null, calls: [] };
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    state.calls.push({ method: init.method || 'GET', path: p });
    const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
    if (p === '/session') return reply(200, SESSION);
    if (state.refuse) {
      const status = state.refuse;
      state.calls[state.calls.length - 1].status = status;
      return reply(status, { ok: false, error: { code: status === 401 ? 'SIGNIN_REQUIRED' : 'FORBIDDEN', message: 'You no longer have access to this workspace.', ...(status === 403 ? { access: 'lost' } : {}) } });
    }
    const body = data(p);
    if (body === undefined) return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
    state.calls[state.calls.length - 1].status = 200;
    return reply(200, body);
  };
  state.count = (p, status) => state.calls.filter(c => c.path === p && (status === undefined || c.status === status)).length;
  return state;
}
async function app(route, srv) {
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  return { win, main };
}
async function go(ctx, route) {
  ctx.win.location.hash = route;
  ctx.win.EW.current.route = route;
  await ctx.win.EWViews.render();
  return ctx.main.innerHTML;
}

const SHOWS = [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }];
const BRANDS = [{ key: 'private-brand', display: 'PRIVATE-BRAND', records: 2, ad_breaks: 2 }];
const SHOW_ALERT = { id: 9, name: 'Shows alert', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: '2026-10-14T09:00:00Z', unread_event_count: 0, events: [],
  query: { version: 2, mode: 'ads', q: '', shows: ['cs-private'] } };
const data = p => {
  if (p === '/customer/shows') return { shows: SHOWS };
  if (p === '/customer/brands') return { brands: BRANDS };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  if (p === '/customer/alerts') return { alerts: [SHOW_ALERT] };
  return undefined;
};

// -- DEMO2-SEC-R1-01: Astra's case ------------------------------------------
for (const [route, read, secret] of [['#/shows', '/customer/shows', 'PRIVATE-SHOW'], ['#/brands', '/customer/brands', 'PRIVATE-BRAND']]) {
  // Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
  for (const status of [403, 401]) {
    test(`SEC-R1-01 ${route}: access removed on the server (${status}), the next visit asks, is refused and keeps nothing`, async () => {
      const srv = server(data);
      const ctx = await app(route, srv);
      assert.match(await go(ctx, route), new RegExp(secret), 'fixture: the directory loads while access holds');
      await go(ctx, '#/settings').catch(() => null);
      srv.refuse = status;
      const before = srv.count(read);
      const html = await go(ctx, route);
      assert.ok(srv.count(read) > before, 'the cached directory was published with no request');
      assert.ok(srv.count(read, status) > 0, `the visit got its ${status}`);
      assert.doesNotMatch(html, new RegExp(secret), 'the refused page still shows the old directory');
      // Pass 16 (DEMO2-SEC-R5-01): the boundary's own check is refused too,
      // so the page ends on the boundary's refusal, with Retry.
      assert.match(text(html), / are unavailable: You no longer have access/, 'the page refuses');
    });
  }
}

test('R9-02 Brands and Shows read their page fresh on every visit (no stale counts in a long-lived tab)', async () => {
  const srv = server(data);
  const ctx = await app('#/shows', srv);
  const shows = srv.count('/customer/shows'), brands = srv.count('/customer/brands');
  await go(ctx, '#/shows');
  await go(ctx, '#/brands');
  await go(ctx, '#/shows');
  await go(ctx, '#/brands');
  assert.equal(srv.count('/customer/shows') - shows, 2);
  assert.equal(srv.count('/customer/brands') - brands, 2);
});

// -- shared names stay auxiliary to the route's own fresh read --------------
test('SEC-R1-01 Alerts reuses show names beside its own fresh read, and a refusal clears them', async () => {
  const srv = server(data);
  const ctx = await app('#/alerts', srv);
  assert.match(text(await go(ctx, '#/alerts')), /PRIVATE-SHOW/);
  const first = srv.count('/customer/alerts');
  assert.match(text(await go(ctx, '#/alerts')), /PRIVATE-SHOW/);
  assert.ok(srv.count('/customer/alerts') > first, 'the route reads its own data every time');
  assert.equal(srv.count('/customer/shows'), 1, 'pass 8: names are reused beside a succeeded route read');
  srv.refuse = 403;
  const refused = await go(ctx, '#/alerts');
  assert.ok(srv.count('/customer/alerts', 403) > 0);
  assert.doesNotMatch(refused, /PRIVATE-SHOW/);
  assert.match(text(refused), /Alerts are unavailable/);
  srv.refuse = null;
  await go(ctx, '#/alerts');
  assert.equal(srv.count('/customer/shows'), 2, 'the refusal cleared the shared names');
});

test('SEC-R1-01 a shared name read without a succeeded route read of its own goes to the server', async () => {
  const srv = server(data);
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  srv.refuse = 403;
  await assert.rejects(E.sharedRead('/customer/shows'), err => err.status === 403);
  assert.equal(srv.count('/customer/shows', 403), 1);
});

test('SEC-R1-01 a refused route read rejects the cached names it was waiting on', async () => {
  const srv = server(data);
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  srv.refuse = 403;
  const own = E.api('/customer/alerts');
  own.catch(() => {});
  await assert.rejects(E.sharedRead('/customer/shows', { authorizedBy: own }), err => err.status === 403);
  srv.refuse = null;
  const before = srv.count('/customer/shows');
  await E.sharedRead('/customer/shows', { authorizedBy: E.api('/customer/alerts') });
  assert.equal(srv.count('/customer/shows'), before + 1, 'the refusal left the names in the cache');
});

// -- DEMO2-R9-01: held ads in ad units --------------------------------------
const pastEv = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:p' + id, read_state: 1, historical: 1,
  ad_id: 'ad' + id, observed_at: '2026-10-14T16:27:59+00:00', ...over });
// Two held reads: one overlaps an included read (inside one of the 3 ads),
// the other is its own ad. Held ads = 1, held reads = 2.
const MUSE = { id: 7, name: 'Meta’s Muse mentions', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: '2026-10-14T09:00:00Z', unread_event_count: 0, unread_ad_count: 0, query: { version: 2, mode: 'mentions', q: '"Meta\'s Muse"' },
  events: [...Array.from({ length: 16 }, (_, i) => pastEv(i + 1, { kind: 'new_text', ad_id: null })), pastEv(17), pastEv(18), pastEv(19)],
  baseline_held_for_brand_review: 2, baseline_held_ads_for_brand_review: 1,
  current_matches: { count: 16, tracked: 19, breaks: 3, tracked_by_kind: { new_text: 16, new_finding: 3 },
    held_for_brand_review: 2, held_ads_for_brand_review: 1 } };
const alertsData = alerts => p => {
  if (p === '/customer/alerts') return { alerts };
  if (p.startsWith('/customer/alerts/')) return { alert: alerts.find(a => '/customer/alerts/' + a.id === p) };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [] };
  return undefined;
};
async function pageText(route, alerts) {
  const srv = server(alertsData(alerts));
  const ctx = await app(route, srv);
  return text(await go(ctx, route));
}
const asideOf = t => t.slice(t.indexOf('Current matches'), t.indexOf('Check local records'));

test('R9-01 Search total = included ads + held ads, never + held reads', async () => {
  const t = asideOf(await pageText('#/alert/7', [MUSE]));
  assert.match(t, /Search shows 16 matching passages and 4 matching ad records\. This alert includes 3 of those ads; 1 more has Brand not identified and isn’t counted here\./);
});

test('R9-01 the row and header count held ads, not held reads', async () => {
  for (const route of ['#/alerts', '#/alert/7']) {
    const t = await pageText(route, [MUSE]);
    assert.match(t, /19 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · 16 passages, 3 ads · plus 1 ad with brand not identified/, route);
  }
});

test('R9-01 two held reads in one ad that the alert does not include are one held ad', async () => {
  const one = { ...MUSE, baseline_held_for_brand_review: 2, baseline_held_ads_for_brand_review: 1,
    current_matches: { ...MUSE.current_matches, breaks: 3, held_for_brand_review: 2, held_ads_for_brand_review: 1 } };
  assert.match(asideOf(await pageText('#/alert/7', [one])), /and 4 matching ad records\. This alert includes 3 of those ads; 1 more has Brand not identified/);
});

test('R9-01 a held read inside an included ad adds no ad record', async () => {
  const inside = { ...MUSE, baseline_held_for_brand_review: 1, baseline_held_ads_for_brand_review: 0,
    current_matches: { ...MUSE.current_matches, held_for_brand_review: 1, held_ads_for_brand_review: 0 } };
  const t = asideOf(await pageText('#/alert/7', [inside]));
  assert.doesNotMatch(t, /4 matching ad records|of those ads; 1 more/);
  assert.doesNotMatch(await pageText('#/alerts', [inside]), /Brand not identified/);
});

test('R9-01 an older stored baseline without the ad count never claims ads', async () => {
  const old = { ...MUSE, baseline_held_ads_for_brand_review: undefined,
    current_matches: { ...MUSE.current_matches, held_ads_for_brand_review: undefined } };
  const row = await pageText('#/alerts', [old]);
  assert.match(row, /3 ads · plus 2 with brand not identified/);
  assert.doesNotMatch(row, /2 ads awaiting/);
  const t = asideOf(await pageText('#/alert/7', [old]));
  assert.doesNotMatch(t, /matching ad records/);
  assert.match(t, /2 more ads have Brand not identified and aren’t counted here/);
});
