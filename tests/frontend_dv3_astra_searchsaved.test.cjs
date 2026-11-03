/* Astra triage fixes, area searchsaved (G2: UX-01 counts, UX-04 Search,
   UX-10, UX-11). Same stubbed-browser loader as frontend_dv3_g1-search.
   Run: node --test tests/frontend_dv3_astra_searchsaved.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
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
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const sessionFetch = (extra = {}) => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  if (extra[url]) return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', ...extra[url] }) };
  throw new Error('no network in unit test');
};
async function views(route, apiFn, fetchExtra = {}) {
  const win = loadAt(route, { fetch: sessionFetch(fetchExtra) });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => apiFn(p, o);
  win.location.hash = route;
  win.EW.current.route = route;
  return { win, main, render: async () => { await win.EWViews.render(); return main.innerHTML; } };
}

const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const bundle = (mode, items, extra = {}, summary = { week: [] }, total = 64) => ({ query: { version: 2, mode, q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total, per_page: 10, total_relation: 'exact', dated_total: total, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 288 }, summary });
// Row 1: the server's ad fields (one ad per item); status cells carry their ads.
const AD = (total, extra = {}) => ({ break_total: total, break_dated_total: total, break_undated_total: 0,
  ad_breaks: [{ id: 'brk:a', refs: ['sr1:a'], read_count: 1, span: [62, 121], brands: [] }], ...extra });
const st = (key, n) => ({ key, count: n, breaks: n });
const searchApi = (b, seen) => (p, o) => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') { if (seen) seen.push(o && o.body); return b; }
  throw new Error('unexpected ' + p);
};
const countLine = html => ((html.match(/<p class="ew-count-line"[^>]*>(.*?)<\/p>/) || [])[1] || '').replace(/<[^>]+>/g, '');

/* -- UX-01 counts: Search ------------------------------------------------ */
// PLAIN-ADS (DEP7): the count line names ads only; no status mix, and
// ads mode no longer asks for the status summary.
test('UX-01 Search ads count line says ads, with no status mix', async () => {
  const seen = [];
  const sum = (status) => ({ week: [], status });
  let t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD(21), sum([st('confirmed', 2), st('finding', 19)]), 21), seen));
  let html = await t.render();
  assert.equal(countLine(html), 'Showing 1 to 1 of 21 ads');
  assert.equal(JSON.stringify(seen[0].summaries), '["week"]', 'ads mode asks only for the week');

  t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD(21), sum([st('finding', 20), st('unresolved', 1)]), 21)));
  assert.equal(countLine(await t.render()), 'Showing 1 to 1 of 21 ads');

  t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD(21), { week: [] }, 21)));
  assert.equal(countLine(await t.render()), 'Showing 1 to 1 of 21 ads');
});

test('UX-01 Search count: a capped count stays capped', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD(21, { total_relation: 'lower_bound' }),
    { week: [], status: [st('confirmed', 2), st('finding', 19)] }, 21)));
  assert.equal(countLine(await t.render()), 'Showing 1 to 1 of at least 21 ads');
});

test('UX-01 Search count: other modes and counted-occurrence cohorts get no status breakdown, and ask only for the week', async () => {
  const seen = [];
  const t = await views('#/search?mode=mentions&q=sleep', searchApi(bundle('mentions', [adItem()], {}, { week: [], status: [{ key: 'finding', count: 3 }] }, 3), seen));
  assert.equal(countLine(await t.render()), 'Showing 1 to 1 of 3 mentions');
  assert.equal(JSON.stringify(seen[0].summaries), '["week"]');
  const u = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], { count_units: 'confirmed_occurrences' },
    { week: [], status: [{ key: 'confirmed', count: 2 }] }, 2)));
  assert.doesNotMatch(countLine(await u.render()), /confirmed,/);
});

test('UX-01 the chart names one unit for ads: "Ads", counted from the server ads only', async () => {
  // Row 1 and PLAIN-ADS: the unit is "Ads"; cells with no ad count draw no chart.
  const { win } = await views('#/search', () => ({}));
  const cells = [{ key: 'w1', label: 'Jun 15–Jun 21', count: 4, breaks: 3, interval: { from: '2026-06-15', to: '2026-06-21' } }];
  const html = win.EWViews.weekChart(cells, { mode: 'ads' }, {});
  assert.match(html, /Ads by date · Ads · actual date intervals/);
  assert.doesNotMatch(html, /confirmed|possible/);
  assert.doesNotMatch(html, /Ad detections|Detections matching|Ad reads/, 'one unit, one name');
  const routed = win.EWViews.weekChart([{ ...cells[0], selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'all_results' } } }], { mode: 'ads' }, {});
  assert.match(routed, /: 3 ads\. Open exact records\./);
  const { breaks, ...noAds } = cells[0];
  assert.equal(win.EWViews.weekChart([noAds], { mode: 'ads' }, {}), '', 'no ad count: no number');
});

/* -- UX-01 counts: brand page head --------------------------------------- */
function brandApi(over = {}, readsTotal = 21) {
  const detail = { key: 'betterhelp', display: 'BetterHelp', detections: readsTotal, ad_breaks: 12, confirmed_occurrences: [], findings: ['sr1:x'],
    offer_sequence: [], episodes: 19, shows: 16, last_heard: '2026-10-09', top_shows: [], reads_without_show: 0, ...over };
  return p => {
    if (p === '/customer/brands/betterhelp') return { brand: detail };
    if (p.startsWith('/customer/search?')) return { items: [], total: 0, total_relation: 'exact' };
    if (p.startsWith('/customer/summary?')) return { cells: [] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    return new Error('unexpected ' + p);
  };
}
const lead = html => ((html.match(/<p class="lib-scope">([^<]*)<\/p>/) || [])[1] || '');
// PLAIN-ADS (DEP7): the head says the brand's ads, never how many are confirmed.
test('UX-01 Brand page head says what the number is: ads, and never their review state', async () => {
  let t = await views('#/brand/betterhelp', brandApi());
  assert.equal(lead(await t.render()), 'In this collection: 12 ads in 19 episodes across 16 shows · last heard Oct 9, 2026');
  t = await views('#/brand/betterhelp', brandApi({ confirmed_occurrences: new Array(21).fill('c') }));
  assert.equal(lead(await t.render()), 'In this collection: 12 ads in 19 episodes across 16 shows · last heard Oct 9, 2026');
  t = await views('#/brand/betterhelp', brandApi({ confirmed_occurrences: ['c', 'c'] }));
  assert.match(lead(await t.render()), /^In this collection: 12 ads in 19 episodes/);
  t = await views('#/brand/betterhelp', brandApi({ ad_breaks: 1, confirmed_occurrences: ['c'], findings: [] }, 1));
  assert.match(lead(await t.render()), /^In this collection: 1 ad in 19 episodes/);
  t = await views('#/brand/betterhelp', brandApi({ confirmed_occurrences: undefined }));
  const h = lead(await t.render());
  assert.match(h, /^In this collection: 12 ads in 19 episodes across 16 shows/);
  assert.doesNotMatch(h, /\b21\b|reads?\b/, 'the read count is never shown');
  assert.doesNotMatch(h, /confirmed/, 'no confirmed number the API did not give');
  assert.doesNotMatch(lead(await (await views('#/brand/betterhelp', brandApi())).render()), / · 0 confirmed/, 'the number is said once');
});

/* -- UX-04 Search phone bands --------------------------------------------- */
test('UX-04 the two disclosures share one row inside .ew-results-disclosures', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()], AD(4),
    { week: [{ key: 'w1', label: 'Jun 15–Jun 21', count: 6, breaks: 4, interval: { from: '2026-06-15', to: '2026-06-21' } }] }, 4)));
  const html = await t.render();
  const block = (html.match(/<div class="ew-results-disclosures">[\s\S]*?<\/details><\/div>/) || [''])[0];
  assert.match(block, /Chart data &amp; coverage/);
  assert.match(block, /About these counts/);
  const count = (html.match(/<div class="ew-results-count">[\s\S]*?<div class="ew-results-tools">|<div class="ew-results-count">[\s\S]*?<\/div><\/div>/) || [''])[0];
  assert.doesNotMatch(count, /ew-week-data/, 'the chart data moved out of the count block');
  assert.match(count, /class="ew-week-bars"/, 'the strip stays under the count line');
});
test('UX-04 weekChart without split is unchanged (one section holding the data)', async () => {
  const { win } = await views('#/search', () => ({}));
  const cells = [{ key: 'w1', label: 'Jun 15–Jun 21', count: 6, breaks: 4, interval: { from: '2026-06-15', to: '2026-06-21' } }];
  const one = win.EWViews.weekChart(cells, { mode: 'ads' }, {});
  assert.equal(typeof one, 'string');
  assert.match(one, /^<section class="chart ew-week"[\s\S]*ew-week-data[\s\S]*<\/section>$/);
  const split = win.EWViews.weekChart(cells, { mode: 'ads' }, {}, { split: true });
  assert.match(split.strip, /^<section class="chart ew-week"/);
  assert.doesNotMatch(split.strip, /ew-week-data/);
  assert.match(split.data, /^<details class="ew-details-quiet ew-week-data">/);
  const none = win.EWViews.weekChart([], { mode: 'ads' }, {}, { split: true });
  assert.equal(none.data, '');
  assert.match(none.strip, /No chart intervals/);
});
test('UX-04 setSelection puts the selected count on Export… for phone and clears it at 0', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  assert.match(html, /data-action="export-open"[^>]*>Export…<span class="ew-short ew-export-n"><\/span><\/button>/);
  const n = { textContent: '' };
  t.win.document.querySelector = s => (s === '[data-action="export-open"] .ew-export-n' ? n : null);
  t.win.EWViews.setSelection(t.win.EW.current.route, ['sr1:a', 'sr1:b'], 'rev-1');
  assert.equal(n.textContent, ' (2)');
  t.win.EWViews.setSelection(t.win.EW.current.route, [], 'rev-1');
  assert.equal(n.textContent, '');
});
test('UX-04 a selection already held shows on Export… at render time', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  t.win.EW.current.displayedRevision = 'rev-1';
  const keep = t.win.document.querySelector;
  t.win.document.querySelector = () => null;
  t.win.EWViews.setSelection(t.win.EW.current.route, ['sr1:a'], 'rev-1');
  t.win.document.querySelector = keep;
  const html = await t.render();
  assert.match(html, /data-action="export-open"[^>]*>Export…<span class="ew-short ew-export-n"> \(1\)<\/span><\/button>/);
});
const css = f => fs.readFileSync(path.join(APP, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
function mediaBlocks(text, query) {
  const out = [];
  let i = 0;
  while ((i = text.indexOf('@media', i)) !== -1) {
    const open = text.indexOf('{', i);
    const head = text.slice(i, open);
    let depth = 1, j = open + 1;
    while (depth && j < text.length) { if (text[j] === '{') depth++; else if (text[j] === '}') depth--; j++; }
    if (head.replace(/\s+/g, '').includes(query.replace(/\s+/g, ''))) out.push(text.slice(open + 1, j - 1));
    i = j;
  }
  return out.join('\n');
}
const decls = (text, selector) => {
  const out = [];
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) if (m[1].split(',').map(s => s.trim()).includes(selector)) out.push(m[2]);
  return out.join(';');
};
test('UX-04 CSS: phone hides the Export selected group; the disclosure row and the strip rule exist', () => {
  const phone = mediaBlocks(css('search.css'), '(max-width: 599px)');
  assert.match(decls(phone, '.ew-export-group'), /display: none/);
  assert.match(decls(phone, '.ew-search-results:has(.ew-week-data:not([open])) .ew-week-row'), /display: none/);
  assert.match(decls(css('search.css'), '.ew-results-disclosures'), /display: flex/);
  assert.match(decls(css('search.css'), '.ew-results-disclosures > details[open]'), /flex-basis: 100%/);
});

/* -- UX-10 Saved filtered-empty ------------------------------------------- */
const savedRender = (win, route, api) => { win.EW.current.route = route; return win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null }); };
const emptyApi = async p => (p.startsWith('/customer/saved-items') ? { items: [], total: 0, total_relation: 'exact', counts: { excerpt: 1, query: 1, bookmark: 1 }, next_cursor: null }
  : p.startsWith('/customer/archive') ? { items: [], total: 0, total_relation: 'exact', next_cursor: null }
  : { notes: [], total: 0, total_relation: 'exact', next_cursor: null });
test('UX-10 a filter with no match says so, keeps the items, and offers Clear filter (every tab)', async () => {
  for (const [tab, noun] of [['excerpts', 'excerpts'], ['queries', 'queries'], ['bookmarks', 'bookmarks'], ['reports', 'reports or exports'], ['notes', 'notes']]) {
    const win = loadAt(`#/saved?tab=${tab}&q=zz`);
    const html = await savedRender(win, `#/saved?tab=${tab}&q=zz`, emptyApi);
    assert.match(html, new RegExp(`No ${noun} match “zz”`), tab);
    assert.match(html, /Your saved items are still here\. Clear the filter to see them all\./, tab);
    assert.match(html, /<button class="button primary" type="button" data-action="saved-clear-filter">Clear filter<\/button>/, tab);
    assert.doesNotMatch(html, /Search transcripts|No notes yet|No saved |Open campaigns/, tab);
    assert.doesNotMatch(html, /class="saved-count"/, `${tab}: 0 is not said twice`);
    assert.doesNotMatch(html, /querys/);
  }
});
test('UX-10 an empty collection without a filter spells queries right and keeps its onboarding', async () => {
  const win = loadAt('#/saved?tab=queries');
  const html = await savedRender(win, '#/saved?tab=queries', emptyApi);
  assert.match(html, /No saved queries/);
  assert.doesNotMatch(html, /querys/);
  assert.match(html, /Search transcripts/);
  const ex = await savedRender(win, '#/saved?tab=excerpts', emptyApi);
  assert.match(ex, /No saved excerpts/);
  const nt = await savedRender(win, '#/saved?tab=notes', emptyApi);
  assert.match(nt, /No standalone notes yet/);
});
test('UX-10 the filter text is escaped in the empty state', async () => {
  const win = loadAt('#/saved?tab=notes&q=%3Cb%3E');
  const html = await savedRender(win, '#/saved?tab=notes&q=%3Cb%3E', emptyApi);
  assert.match(html, /No notes match “&lt;b&gt;”/);
});

/* -- UX-11 Saved tabs ------------------------------------------------------ */
function fakeStrip({ scrollWidth, clientWidth, scrollLeft = 0, offsetLeft, offsetWidth }) {
  const cls = new Set();
  const wrap = { classList: { toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); }, contains: c => cls.has(c) } };
  const a = { offsetLeft, offsetWidth, focusCalls: 0, focus() { this.focusCalls++; }, scrollIntoView() { throw new Error('moves the page'); } };
  const listeners = [];
  const nav = { scrollWidth, clientWidth, scrollLeft, dataset: {}, parentElement: wrap, querySelector: s => (s === 'a[aria-current]' ? a : null),
    addEventListener: (t, fn, o) => listeners.push([t, fn, o]) };
  const root = { querySelector: s => (s === '.saved-tabs' ? nav : null) };
  return { root, nav, a, wrap, cls, listeners };
}
test('UX-11 the selected tab is scrolled into view without moving focus', () => {
  const win = loadAt('#/saved?tab=notes');
  const f = fakeStrip({ scrollWidth: 533, clientWidth: 358, offsetLeft: 461, offsetWidth: 71 });
  win.EWSaved.revealCurrentTab(f.root);
  assert.equal(f.nav.scrollLeft, 175, 'clamped to the strip end (533 - 358); the unclamped target is 190');
  assert.equal(f.a.focusCalls, 0);
  assert.ok(f.cls.has('is-more-start') && !f.cls.has('is-more-end'));
  assert.equal(f.listeners.length, 1);
  assert.equal(f.listeners[0][0], 'scroll');
  assert.equal(f.listeners[0][2].passive, true);
  win.EWSaved.revealCurrentTab(f.root);
  assert.equal(f.listeners.length, 1, 'the scroll listener binds once');
  f.nav.scrollLeft = 0; f.listeners[0][1]();
  assert.ok(!f.cls.has('is-more-start') && f.cls.has('is-more-end'), 'the cue follows manual scrolling');
});
test('UX-11 a tab at the start leaves the strip alone and cues more to the right; a tab left of the view scrolls back', () => {
  const win = loadAt('#/saved?tab=excerpts');
  const f = fakeStrip({ scrollWidth: 533, clientWidth: 358, offsetLeft: 0, offsetWidth: 80 });
  win.EWSaved.revealCurrentTab(f.root);
  assert.equal(f.nav.scrollLeft, 0);
  assert.ok(f.cls.has('is-more-end') && !f.cls.has('is-more-start'));
  const g = fakeStrip({ scrollWidth: 533, clientWidth: 358, scrollLeft: 175, offsetLeft: 100, offsetWidth: 80 });
  win.EWSaved.revealCurrentTab(g.root);
  assert.equal(g.nav.scrollLeft, 84);
});
test('UX-11 with no overflow nothing moves and there is no cue; other pages are a no-op', () => {
  const win = loadAt('#/saved?tab=notes');
  const f = fakeStrip({ scrollWidth: 358, clientWidth: 358, offsetLeft: 200, offsetWidth: 71 });
  win.EWSaved.revealCurrentTab(f.root);
  assert.equal(f.nav.scrollLeft, 0);
  assert.equal(f.cls.size, 0);
  assert.doesNotThrow(() => win.EWSaved.revealCurrentTab({ querySelector: () => null }));
});
test('UX-11 the strip sits in a wrap, and render() reveals the selected tab after painting', async () => {
  const win = loadAt('#/saved?tab=notes');
  const html = await savedRender(win, '#/saved?tab=notes', emptyApi);
  assert.match(html, /<div class="saved-tabs-wrap"><nav class="saved-tabs" aria-label="Saved collections">/);
  assert.match(html, /<\/nav><\/div><form id="saved-filter"/);
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /window\.EWSaved && window\.EWSaved\.revealCurrentTab\) window\.EWSaved\.revealCurrentTab\(main\)/);
  assert.ok(src.indexOf('revealCurrentTab(main)') > src.indexOf('Restore focus to the recorded control'), 'after the focus restore');
});
test('UX-11 CSS: the wrap carries the edge fades that show only when the strip overflows', () => {
  const text = css('account.css');
  assert.match(decls(text, '.saved-tabs-wrap'), /position: relative/);
  assert.match(decls(text, '.saved-tabs-wrap::before'), /opacity: 0/);
  assert.match(decls(text, '.saved-tabs-wrap::after'), /right: 0/);
  assert.match(decls(text, '.saved-tabs-wrap.is-more-end::after'), /opacity: 1/);
  assert.match(decls(text, '.saved-tabs-wrap.is-more-start::before'), /opacity: 1/);
  assert.doesNotMatch(decls(text, '.saved-tabs-wrap::after'), /pointer-events: auto/);
  assert.match(decls(text, '.saved-tabs-wrap::after'), /pointer-events: none/);
});

/* -- DEMO-SOL-R1-11 / 13: shared notes and the phone tab strip ------------- */
test('DEMO-SOL-R1-11 a saved item note is a workspace note, never personal', async () => {
  const win = loadAt('#/saved?tab=queries');
  const items = [{ id: 1, type: 'query', title: 'Quince possible ads', note: 'Possible ads associated with Quince in this collection.',
    query: { version: 2, mode: 'ads', q: '', brands: ['quince'] }, revision: 1, created_at: '2026-10-21T00:00:00Z' }];
  const api = async p => (p.startsWith('/customer/saved-items') ? { items, total: 1, total_relation: 'exact', counts: { query: 1 }, next_cursor: null } : emptyApi(p));
  const html = await savedRender(win, '#/saved?tab=queries', api);
  assert.match(html, /<p class="saved-note">Workspace note: Possible ads associated with Quince in this collection\.<\/p>/);
  assert.doesNotMatch(html, /Personal note/);
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.doesNotMatch(src, /Personal note \(optional\)/, 'the excerpt and bookmark dialogs say workspace note');
});
test('DEMO-SOL-R1-13 phone: the Saved tab strip wraps so every tab is visible', () => {
  const phone = mediaBlocks(css('account.css'), '(max-width: 599px)');
  assert.match(decls(phone, '.ew-saved .saved-tabs'), /flex-wrap: wrap/);
  assert.match(decls(phone, '.ew-saved .saved-tabs'), /overflow-x: visible/);
});
