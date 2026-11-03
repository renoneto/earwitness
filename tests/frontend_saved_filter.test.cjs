/* SAVED-FILTER (QA-full G3-01, lead decision 2026-10-24):
   - A typed query that differs from the saved search's query drops the
     brand filter that came from the saved search. Other filters keep what
     Search did before (a new query keeps shows, dates and sort).
   - The same query re-run keeps it. A brand the person set in this
     search stays on a new query until they remove it.
   - An active brand filter is a visible chip "Brand: <name> ×"; × removes
     it and runs the search again.
   - The route carries the state, so a reload or Back shows the same.
   Counts, the server query and saved items are unchanged.
   Loader copied from frontend_uat_scope.test.cjs.
   Run: node --test tests/frontend_saved_filter.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, Intl,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route },
    history: { replaceState() {}, pushState() {}, state: null },
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

const BRANDS = [{ key: 'quince', display: 'Quince' }, { key: 'mint-mobile', display: 'Mint Mobile' }];
const ITEM = { ref: 'sr1:a', evidence_id: 'e1', record_kind: 'ad', status: 'finding', brand: { key: 'quince', display: 'Quince' },
  show_title: 'Show', ep_title: 'Ep', pub_date: '2026-09-08', span: [1711, 1800], text: 'support our show' };

/* One app: Search reads are recorded (the request body the server
   gets) and navigation is captured, then followed by hand. */
async function app(route) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flush();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? { open: false } : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const ctx = { win, main, bodies: [], gone: [] };
  win.EW.go = dest => { ctx.gone.push(String(dest)); };
  win.EW.api = async (p, o = {}) => {
    if (p === '/customer/brands') return { brands: BRANDS };
    if (p === '/customer/shows') return { shows: [] };
    if (p.startsWith('/customer/filter-values?')) return { items: [] };
    if (p === '/customer/research-query') {
      const body = JSON.parse(JSON.stringify(o.body));
      ctx.bodies.push(body);
      return { query: body.query, selector: null, revision: 'rev-1', coverage: { episodes_in_scope: 10 }, summary: { week: [] },
        result: { items: [ITEM], total: 1, per_page: 10, total_relation: 'exact', dated_total: 1, undated_total: 0 } };
    }
    if (p.startsWith('/customer/preferences')) return { preferences: { revision: 1 }, revision: 1 };
    throw new Error('unexpected ' + p);
  };
  return ctx;
}
async function show(ctx, route) {
  ctx.win.location.hash = route;
  ctx.win.EW.current.route = route;
  ctx.bodies.length = 0;
  await ctx.win.EWViews.render();
  for (let i = 0; i < 25; i++) await flush();
  stopTimers();
  return ctx.main.innerHTML;
}
const lastQuery = ctx => { assert.ok(ctx.bodies.length, 'no Search request was made'); return ctx.bodies[ctx.bodies.length - 1].query; };
const brandsOf = q => (Array.isArray(q.brands) ? q.brands : []);
const searchForm = q => ({ getAttribute: k => (k === 'id' ? 'search' : null), dataset: {}, _fields: { q }, closest: () => null });
async function typeQuery(ctx, q) {
  ctx.win.FormData = class { constructor(f) { this.f = f; } *entries() { yield* Object.entries(this.f._fields || {}); } };
  ctx.gone.length = 0;
  await ctx.win.EWViews.handleSubmit(searchForm(q));
  for (let i = 0; i < 5; i++) await flush();
  assert.equal(ctx.gone.length, 1, 'Search did not navigate once: ' + JSON.stringify(ctx.gone));
  return ctx.gone[0];
}
const unescape = s => s.replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
const brandChips = html => [...html.matchAll(/<button[^>]*data-action="remove-filter"[^>]*data-key="brands"[^>]*>([\s\S]*?)<\/button>/g)]
  .map(m => m[1].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim());

/* The seeded "Quince ads" saved search (seed_descriptor ads("quince")),
   opened from its row on Saved › Queries. */
const QUINCE_ADS = { version: 2, mode: 'ads', q: '', brands: ['quince'] };
async function savedRoute(query = QUINCE_ADS) {
  const win = loadAt('#/saved?tab=queries');
  const items = [{ id: 'si-q', type: 'query', title: 'Quince ads', revision: 1, availability: 'current', query }];
  const api = async () => ({ items, total: 1, total_relation: 'exact', counts: { excerpt: 0, query: 1, bookmark: 0 }, next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
  const href = (html.match(/href="(#\/search[^"]*)"/) || [])[1];
  assert.ok(href, 'the saved query row has no Search link');
  return unescape(href);
}
async function openSaved(query = QUINCE_ADS) {
  const route = await savedRoute(query);
  const ctx = await app(route);
  const html = await show(ctx, route);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'fixture: the saved search runs with its brand');
  return { ctx, route, html };
}

/* (1) ------------------------------------------------------------------------ */
test('G3-01 (1): Saved › Quince ads, then type "mint mobile": the request has no brand filter', async () => {
  const { ctx } = await openSaved();
  const dest = await typeQuery(ctx, 'mint mobile');
  const p = new URLSearchParams(dest.split('?')[1]);
  assert.equal(p.get('q'), 'mint mobile');
  assert.deepEqual(p.getAll('brands'), [], 'the new route still names the saved brand: ' + dest);
  const html = await show(ctx, dest);
  const q = lastQuery(ctx);
  assert.equal(q.q, 'mint mobile');
  assert.deepEqual(brandsOf(q), [], 'the new query still sent the saved brand filter');
  assert.deepEqual(brandChips(html), [], 'a brand chip is still shown after the new query');
});

test('G3-01 (1): other filters from the saved search stay on a new query (shows, dates, sort)', async () => {
  const { ctx } = await openSaved({ ...QUINCE_ADS, shows: ['cs-1'], from: '2026-10-01', to: '2026-10-31', date_status: 'known', sort: 'oldest' });
  const dest = await typeQuery(ctx, 'mint mobile');
  await show(ctx, dest);
  const q = lastQuery(ctx);
  assert.deepEqual(brandsOf(q), []);
  assert.deepEqual(q.shows, ['cs-1']);
  assert.equal(q.from, '2026-10-01');
  assert.equal(q.to, '2026-10-31');
  assert.equal(q.sort, 'oldest');
});

/* (2) ------------------------------------------------------------------------ */
test('G3-01 (2): the same query re-run keeps the saved brand filter', async () => {
  const { ctx } = await openSaved();
  const dest = await typeQuery(ctx, '');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'the same query dropped the saved brand');
  // And again from there: still the saved search's query, still kept.
  const again = await typeQuery(ctx, '  ');
  await show(ctx, again);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
});

test('G3-01 (2): a saved search with words keeps its brand when the same words are searched again', async () => {
  const { ctx } = await openSaved({ ...QUINCE_ADS, q: 'cashmere' });
  const dest = await typeQuery(ctx, 'cashmere');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
  const other = await typeQuery(ctx, 'linen');
  await show(ctx, other);
  assert.deepEqual(brandsOf(lastQuery(ctx)), []);
});

/* (3) ------------------------------------------------------------------------ */
test('G3-01 (3): a brand filter set by hand stays on a new query', async () => {
  const route = '#/search?mode=ads&brands=quince';
  const ctx = await app(route);
  await show(ctx, route);
  const dest = await typeQuery(ctx, 'mint mobile');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'a hand-set brand was dropped by a new query');
});

test('G3-01 (3): after a saved search, a brand added by hand stays and only the saved one drops', async () => {
  const { ctx } = await openSaved();
  const E = ctx.win.EW;
  // Filters › Apply goes through the same route builder (goCriteria).
  const added = E.searchURL({ ...E.searchCriteria(), brands: ['quince', 'mint-mobile'], page: 1 });
  await show(ctx, added);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince', 'mint-mobile']);
  const dest = await typeQuery(ctx, 'phone plan');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['mint-mobile'], 'only the saved brand drops');
});

test('G3-01 (3): a saved brand removed and set again by hand is the person\'s own and stays', async () => {
  const { ctx } = await openSaved();
  const E = ctx.win.EW;
  const cleared = E.searchURL({ ...E.searchCriteria(), brands: null, page: 1 });
  await show(ctx, cleared);
  const again = E.searchURL({ ...E.searchCriteria(), brands: ['quince'], page: 1 });
  await show(ctx, again);
  const dest = await typeQuery(ctx, 'mint mobile');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
});

/* (4) ------------------------------------------------------------------------ */
test('G3-01 (4): an active brand filter shows as the chip "Brand: Quince ×"', async () => {
  const { html } = await openSaved();
  assert.deepEqual(brandChips(html), ['Brand: Quince ×']);
  assert.match(html, /data-action="remove-filter" data-key="brands" data-value="quince" aria-label="Remove Brand: Quince"/);
  // A hand-set one too.
  const ctx = await app('#/search?mode=ads&q=sleep&brands=mint-mobile');
  assert.deepEqual(brandChips(await show(ctx, '#/search?mode=ads&q=sleep&brands=mint-mobile')), ['Brand: Mint Mobile ×']);
});

test('G3-01 (4): pressing × on the brand chip removes it and runs the search again', async () => {
  const { ctx } = await openSaved();
  ctx.gone.length = 0;
  await ctx.win.EWViews.handleAction({ dataset: { action: 'remove-filter', key: 'brands', value: 'quince' }, closest: () => null });
  for (let i = 0; i < 5; i++) await flush();
  assert.equal(ctx.gone.length, 1, '× did not run the search again');
  const html = await show(ctx, ctx.gone[0]);
  assert.equal(ctx.bodies.length, 1, 'no new Search request after ×');
  assert.deepEqual(brandsOf(lastQuery(ctx)), []);
  assert.deepEqual(brandChips(html), []);
});

test('G3-01 (4): no stylesheet hides the scope chips on a narrow screen (390)', () => {
  const hides = [];
  for (const file of fs.readdirSync(APP).filter(f => f.endsWith('.css'))) {
    const css = fs.readFileSync(path.join(APP, file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      const sel = m[1], decl = m[2];
      if (!/display\s*:\s*none|visibility\s*:\s*hidden/.test(decl)) continue;
      for (const part of sel.split(',')) {
        // The rule's subject (last compound selector) is a chip or the chip row.
        const subject = part.trim().split(/[\s>+~]+/).pop();
        if (/\.(filter-summary|filter-chip|ew-chips)(?![\w-])/.test(subject)) hides.push(`${file}: ${part.trim()}`);
      }
    }
  }
  assert.deepEqual(hides, [], 'scope chips are hidden by: ' + hides.join('; '));
});

/* (5) ------------------------------------------------------------------------ */
test('G3-01 (5): reload and Back keep the state consistent with the URL', async () => {
  const { ctx, route } = await openSaved();
  const dest = await typeQuery(ctx, 'mint mobile');
  // Reload of the new query's URL in a fresh app: no brand.
  const fresh = await app(dest);
  assert.deepEqual(brandChips(await show(fresh, dest)), []);
  assert.deepEqual(brandsOf(lastQuery(fresh)), []);
  // Back to the saved search's URL: its brand and chip again, and a new
  // typed query from there drops it again.
  await show(ctx, dest);
  const back = await show(ctx, route);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
  assert.deepEqual(brandChips(back), ['Brand: Quince ×']);
  const dest2 = await typeQuery(ctx, 'mint mobile');
  assert.equal(dest2, dest, 'the same steps give a different URL');
});

test('G3-01 (5): sort, page and result-type changes keep the saved brand marked; a typed query then still drops it', async () => {
  const { ctx } = await openSaved();
  const E = ctx.win.EW;
  const sorted = E.searchURL({ ...E.searchCriteria(), sort: 'oldest', page: 1 });
  await show(ctx, sorted);
  const mentions = E.searchURL({ ...E.searchCriteria(), mode: 'mentions', page: 1 });
  await show(ctx, mentions);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
  const dest = await typeQuery(ctx, 'mint mobile');
  await show(ctx, dest);
  const q = lastQuery(ctx);
  assert.deepEqual(brandsOf(q), []);
  assert.equal(q.mode, 'mentions');
  assert.equal(q.sort, 'oldest');
});

test('G3-01: the saved item and the query sent for the saved search are unchanged', async () => {
  const { ctx } = await openSaved();
  const q = lastQuery(ctx);
  for (const k of Object.keys(q)) assert.ok(!/saved/i.test(k), 'the server query carries a saved-search field: ' + k);
  assert.equal(q.q, '');
  assert.deepEqual(q.brands, ['quince']);
  assert.equal(ctx.win.EW.searchCriteria().saved_brands, undefined, 'the marker leaked into the query criteria');
});

/* SAVED-FILTER-FIX1 ---------------------------------------------------------- */
const hrefOf = (html, text) => {
  const m = [...html.matchAll(/<a [^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/g)].find(x => x[2].replace(/<[^>]+>/g, '').includes(text));
  assert.ok(m, `no link "${text}"`);
  return unescape(m[1]);
};

test('SF-R1-02: "Show only ads named Quince" makes Quince the person\'s own; a typed query keeps it', async () => {
  const route = await savedRoute({ ...QUINCE_ADS, q: 'quince', brands: ['quince', 'mint-mobile'] });
  const ctx = await app(route);
  const html = await show(ctx, route);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince', 'mint-mobile']);
  const only = hrefOf(html, 'Show only ads named Quince');
  await show(ctx, only);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince']);
  const dest = await typeQuery(ctx, 'linen');
  await show(ctx, dest);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'a brand picked with "Show only ads named" was dropped by a new query');
});

test('SF-R1-01: after "Search the exact phrase", typing the saved search\'s words keeps its brand; other words drop it', async () => {
  const { ctx, html } = await openSaved({ ...QUINCE_ADS, q: 'cashmere sweater' });
  const exact = hrefOf(html, 'Search the exact phrase');
  await show(ctx, exact);
  assert.equal(lastQuery(ctx).q, '"cashmere sweater"');
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'the exact-phrase link keeps the brand');
  const same = await typeQuery(ctx, 'cashmere sweater');
  await show(ctx, same);
  assert.deepEqual(brandsOf(lastQuery(ctx)), ['quince'], 'the saved search\'s own words dropped its brand');
  await show(ctx, exact);
  const other = await typeQuery(ctx, 'linen');
  await show(ctx, other);
  assert.deepEqual(brandsOf(lastQuery(ctx)), []);
});

test('SF-SCR-R1-01: the brand chip row sits directly under the count line, above the week chart and the view tools', async () => {
  const { html } = await openSaved();
  const at = s => { const i = html.indexOf(s); assert.ok(i >= 0, 'missing ' + s); return i; };
  const count = at('<p class="ew-count-line"'), chips = at('<div class="filter-summary ew-chips"');
  assert.ok(count < chips, 'the chips come before the count line');
  assert.ok(chips < at('class="chart ew-week"'), 'the week chart sits between the count line and the chips');
  assert.ok(chips < at('class="ew-results-tools"'), 'the chips are not in the count column');
  const between = html.slice(count, chips);
  assert.match(between, /^<p class="ew-count-line"[^>]*>[^<]*<\/p>$/, 'something sits between the count line and the chips: ' + between);
});
