/* UAT fix round, search worker: Search, result cards and rows, Overview
   copy, toasts, counts. One test group per fix id (F-*, V-*, VL-*).
   Loader and render copied from frontend_uat_fixes.test.cjs.
   Run: node --test tests/frontend_uat_search.test.cjs
   (EW_APP_DIR=<dir> runs it against another copy of app/.) */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
// App timers (the 250 ms preference wait, retries) must not outlive a test.
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
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
let PREFS = {};
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1, ...PREFS }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, setup) {
  const win = loadAt('#/settings', { fetch: sessionFetch }); // boot on a route with no preference redirect
  win.addEventListener = () => {};
  win.__listeners = {};
  win.document.addEventListener = (type, fn) => { (win.__listeners[type] = win.__listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  if (setup) setup(win);
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return setup ? win : main.innerHTML;
}

const bundle = items => ({ query: { version: 2, mode: 'ads', q: 'zz', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  return new Error('unexpected ' + p);
};

const read = (over = {}) => ({ brand: { key: 'whole', display: 'Whole', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false, ...over });
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false, read: read(), ...over });
const noBrand = () => adItem({ read: read({ brand: { key: null, display: null, display_basis: 'none', confirmed: false } }) });
const cell = (from, to, count) => ({ key: from, label: `Week of ${from} – ${to}`, count, interval: { from, to },
  selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell' } } });
const plain = html => html.replace(/<[^>]+>/g, '');
// Row 1: an Ads cell or bundle carries the server's ad count (cell.breaks,
// result.ad_breaks / break_total); without it Ads shows no number.
// R2-08: the evidence count (count) differs from the ad count (breaks), so a test can tell them apart.
const adCell = (from, to, ads, evidence = ads * 2 + 1) => ({ ...cell(from, to, evidence), breaks: ads });
const withAds = (b, total = b.result.items.length, dated = total, undated = 0) => {
  b.result.ad_breaks = b.result.items.map((x, i) => ({ id: 'brk:' + i, refs: [x.ref], read_count: 1, span: [60, 120], brands: [] }));
  Object.assign(b.result, { break_total: total, break_dated_total: dated, break_undated_total: undated });
  return b;
};
const css = f => fs.readFileSync(path.join(APP, f), 'utf8');

async function views() {
  const win = await render('#/settings', () => new Error('unexpected'), () => {});
  return win;
}

/* -- F-1 one honest label for a read with no shown brand -------------------- */
test('F-1: a read with no brand says "Brand not identified" on the card, the row and the inspector', async () => {
  const win = await views();
  const card = win.EWUI.sourceCard(noBrand(), { index: 0 });
  const row = win.EWUI.resultRow(noBrand(), { index: 0 });
  const panel = win.EWViews.inspectorHtml(noBrand(), { q: '' }, 1);
  for (const html of [card, row, panel]) {
    assert.match(html, /Brand not identified/);
    assert.doesNotMatch(html, /Brand not named|Not named/);
  }
  assert.match(win.EWViews.inspectorTitle(noBrand()), /^Selected: Brand not identified, Show one$/);
});
test('F-1: the inspector fact row for a missing brand reads "Not identified"', async () => {
  const win = await views();
  const panel = win.EWViews.inspectorHtml(noBrand(), { q: '' }, 1);
  assert.match(panel, /Brand name[\s\S]{0,120}Not identified/);
});

/* -- F-5 / F-6 cards and rows ------------------------------------------------ */
test('F-5: a list row names an unknown date "Unknown", puts show over episode with a title, and the head names the Clip column', async () => {
  const win = await views();
  const row = win.EWUI.resultRow(adItem({ pub_date: null }), { index: 0 });
  assert.match(row, /<span class="ew-row-date">Unknown<\/span>/);
  assert.match(row, /<span class="ew-row-where" title="Show one · Ep one"><span class="ew-row-show">Show one<\/span><span class="ew-row-ep">Ep one<\/span><\/span>/);
  assert.match(win.EWUI.resultRow(adItem(), { index: 0 }), /<span class="ew-row-date">Oct 9<\/span>/);
});
test('F-5: the list head has a visible Clip header, and the name flag may wrap under the brand', async () => {
  const html = await render('#/search?mode=ads&view=list&q=x', searchApi(bundle([adItem()])));
  assert.match(html, /<span aria-hidden="true">Clip<\/span><span aria-hidden="true">Time<\/span>/);
  assert.doesNotMatch(css('search.css'), /\.ew-row-brand \.ew-tag-uncertain \{ white-space: nowrap/);
});
test('F-6: the card date is its own item, outside the clamped show and episode text, and is in the title', async () => {
  const win = await views();
  const card = win.EWUI.sourceCard(adItem(), { index: 0 });
  assert.match(card, /<span class="ew-result-where-text" title="Show one · Ep one · Oct 9">[^<]*<a [^>]*>Show one<\/a> · Ep one<\/span><span class="ew-result-date">Oct 9<\/span>/);
  assert.match(css('search.css'), /\.ew-result-where > \.ew-result-where-text \{ display: -webkit-box;/);
});

/* -- F-8 what each tab counts ------------------------------------------------ */
test('F-8: the mentions chart counts "Transcript passages"; the counts note says what Ads and All mentions count', async () => {
  const win = await views();
  const chart = win.EWViews.weekChart([cell('2026-09-14', '2026-09-20', 5)], { mode: 'mentions' }, { total: 5 });
  assert.match(chart, /Transcript passages · actual date intervals/);
  assert.doesNotMatch(chart, /ad reads and transcript passages/);
  const html = await render('#/search?mode=ads&q=x', searchApi(bundle([adItem()])));
  assert.match(html, /Ads counts ads\. An ad is evidence in one episode whose times overlap, split where two different brands are named; one ad can have more than one piece of evidence, found by more than one method\./);
  assert.match(html, /All mentions counts transcript passages that say the words, inside or outside ads\./);
});

/* -- F-10 separators --------------------------------------------------------- */
test('F-10: the unknown-date line has no dangling dot (V-18 copy, singular and plural)', async () => {
  const win = await views();
  const one = win.EWViews.weekChart([{ ...adCell('unknown', 'unknown', 1), key: 'unknown', label: 'Unknown' }], { mode: 'ads' }, { total: 1, undated_total: 1, dated_total: 0 }, { split: true }).strip;
  assert.match(plain(one), /1 ad has no date in this scope\. /);
  const many = win.EWViews.weekChart([adCell('2026-09-14', '2026-09-20', 1), { ...adCell('unknown', 'unknown', 1234), key: 'unknown', label: 'Unknown' }], { mode: 'ads' }, { total: 1235 }, { split: true }).strip;
  assert.match(plain(many), /1,234 ads have no date in this scope\. Show the exact unknown set/);
  assert.doesNotMatch(plain(many), /scope · /);
});
test('F-10: the brand-jump separator is aria-hidden and glued to the item that follows', async () => {
  const win = await views();
  const html = win.EWViews.brandJump({ mode: 'ads', q: 'whole' }, [{ key: 'whole', display: 'Whole', confirmed: 0 }]);
  assert.match(html, / <span class="ew-sep" aria-hidden="true">· <\/span><a [^>]*>Show only ads named Whole<\/a>/);
  assert.match(css('search.css'), /\.ew-sep \{ white-space: nowrap; \}/);
});
// PLAIN-ADS (DEP7): no status mix after the count.
test('F-10: no status mix after the count', async () => {
  const bundleWithMix = withAds(bundle([adItem()]));
  bundleWithMix.summary = { week: [], status: [{ key: 'confirmed', count: 0, breaks: 0 }, { key: 'finding', count: 1, breaks: 1 }] };
  const html = await render('#/search?mode=ads&q=zz', searchApi(bundleWithMix));
  assert.doesNotMatch(html, /ew-count-mix|0 confirmed|1 possible/);
});

/* -- F-11 chart line and Export selected ------------------------------------ */
test('F-11: no results draw no chart line; only undated results say why there is no weekly chart', async () => {
  const win = await views();
  assert.deepEqual({ ...win.EWViews.weekChart([], { mode: 'ads' }, { total: 0 }, { split: true }) }, { strip: '', data: '' });
  assert.equal(win.EWViews.weekChart([], { mode: 'ads' }, { total: 0 }), '');
  const undated = win.EWViews.weekChart([], { mode: 'ads' }, { total: 4, dated_total: 0, undated_total: 4 }, { split: true }).strip;
  assert.match(plain(undated), /These results have no air date, so there is no weekly chart\./);
  assert.doesNotMatch(undated, /No chart intervals/);
  // unknown shape keeps the old line
  assert.match(win.EWViews.weekChart([], { mode: 'ads' }, {}, { split: true }).strip, /No chart intervals/);
});
test('F-11: Export selected and its hint are not drawn when the search found nothing; they are with results', async () => {
  assert.doesNotMatch(await render('#/search?mode=ads&q=zz', searchApi(bundle([]))), /export-selected/);
  assert.match(await render('#/search?mode=ads&q=zz', searchApi(bundle([adItem()]))), /data-action="export-selected"/);
});

/* -- F-12 / V-18 numbers ----------------------------------------------------- */
test('V-18: E.num puts thousands separators on counts', async () => {
  const win = await views();
  assert.equal(win.EW.num(1198), '1,198');
  assert.equal(win.EW.num(7), '7');
});
test('F-12: the week label reads "Week of Sep 14–20" and crosses months in full', async () => {
  const win = await views();
  const one = win.EWViews.weekChart([adCell('2026-09-14', '2026-09-20', 5)], { mode: 'ads' }, { total: 5 }, { split: true }).strip;
  assert.match(one, /data-week="Week of Sep 14–20" data-count="5 ads"/);
  assert.match(one, /title="Week of Sep 14–20: 5 ads"/);
  const cross = win.EWViews.weekChart([adCell('2026-09-28', '2026-10-04', 5)], { mode: 'ads' }, { total: 5 }, { split: true }).strip;
  assert.match(cross, /data-week="Week of Sep 28–Oct 4"/);
});
test('F-12: "most:" is left out with one dated week and counts carry separators', async () => {
  const win = await views();
  const one = win.EWViews.weekChart([adCell('2026-09-14', '2026-09-20', 5)], { mode: 'ads' }, { total: 5 }, { split: true }).strip;
  assert.doesNotMatch(one, /most:/);
  const two = win.EWViews.weekChart([adCell('2026-09-14', '2026-09-20', 1500), adCell('2026-09-21', '2026-09-27', 2)], { mode: 'ads' }, { total: 1502 }, { split: true }).strip;
  assert.match(two, /most: 1,500 ads \(Sep 14\)/);
  assert.match(two, /data-count="1,500 ads"/);
});
test('F-12: tab count, Showing line and the counts note use separators', async () => {
  const b = bundle([adItem()]);
  b.result.total = 20000; b.result.dated_total = 19000; b.result.undated_total = 1000; b.result.per_page = 10;
  withAds(b, 12345, 12000, 345);
  const html = await render('#/search?mode=ads&q=zz', searchApi(b));
  assert.match(html, /<span class="ew-tab-count" title="12,345 ads">12,345<\/span>/);
  assert.match(html, /Showing 1 to 1 of 12,345 ads/);
  assert.match(plain(html), /12,345 ads · All dates · 288 episodes in scope · dated: 12,000 ads · date unknown: 345 ads/);
  assert.doesNotMatch(plain(html), /20,000|19,000|1,000/, 'the evidence totals are never shown as ads');
});

/* -- F-13 toasts ------------------------------------------------------------- */
test('F-13: the export toast names what was exported', async () => {
  const win = await views();
  const t = win.EWViews.exportedText;
  assert.equal(t({ total: 2, ad_count: 2 }, 'ads'), 'Exported 2 ads.');
  assert.equal(t({ item_count: 1, ad_count: 1 }, 'ads'), 'Exported 1 ad.');
  // No server ad count: no number (never a row count dressed as ads).
  assert.equal(t({ total: 2 }, 'ads'), 'Exported.');
  assert.equal(t({ total: 1200 }, 'mentions'), 'Exported 1,200 passages.');
  assert.equal(t({ total: 3 }, 'episodes'), 'Exported 3 episodes.');
  assert.equal(t({}, 'ads'), 'Exported.');
  // Judge export Sol r1 EXP-SOL-R1-02: rows left out are named.
  assert.equal(t({ total: 0, withheld: 3 }, 'mentions'), 'Created the file. 3 results containing transcript text were left out.');
  assert.equal(t({ total: 0, withheld: 1 }, 'episodes'), 'Created the file. 1 result containing transcript text was left out.');
  assert.equal(t({ total: 2, withheld: 1 }, 'mentions'), 'Exported 2 passages. 1 result containing transcript text was left out.');
  assert.equal(t({ total: 2, ad_count: 2, withheld: 0 }, 'ads'), 'Exported 2 ads.');
  assert.equal(t({ total: 0, withheld: 0 }, 'mentions'), 'Exported 0 passages.');
});
const toastWin = async () => {
  const win = await render('#/settings', () => new Error('unexpected'), () => {});
  const el = { textContent: '' };
  const prev = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#toast' ? el : prev(sel));
  win.EWViews.render = async () => {};
  return { win, el };
};
test('F-13: a toast does not outlive the page it was about: go() clears it', async () => {
  const { win, el } = await toastWin();
  win.EW.toast('Exported 2 ads.');
  assert.equal(el.textContent, 'Exported 2 ads.');
  win.EW.go('#/overview');
  assert.equal(el.textContent, '');
  assert.equal(win.EW.current.toastTimer, null);
  win.EW.toast('Exported 2 ads.');
  assert.equal(el.textContent, 'Exported 2 ads.', 'the same text can be shown again after it was cleared');
  stopTimers();
});
test('F-13: a toast set after go() stays (Bookmark saved)', async () => {
  const { win, el } = await toastWin();
  win.EW.go('#/saved?tab=bookmarks');
  win.EW.toast('Bookmark saved.');
  assert.equal(el.textContent, 'Bookmark saved.');
  stopTimers();
});
test('F-13: discarding a dialog draft clears the "Not saved" toast', async () => {
  const { win, el } = await toastWin();
  win.EW.toast('Not saved. Your entry is kept…');
  const form = { dataset: { dirty: '1' }, renderedValues: null, querySelector: () => null };
  await win.EWViews.handleAction({ dataset: { action: 'discard-dialog-draft' }, closest: () => form });
  assert.equal(el.textContent, '');
  stopTimers();
});
test('F-13: at 1200 px and up the toast sits bottom right, off the rail Apply button', () => {
  assert.match(css('styles.css'), /@media \(min-width: 1200px\) \{ \.toast \{ left: auto; right: var\(--ew-s6\); \} \}/);
});

/* -- F-17 episode card ------------------------------------------------------- */
const epItem = () => ({ ref: 'ep:1', record_kind: 'episode', episode_id: 'e1', ep_title: 'Ep title', show_title: 'Show one', pub_date: '2026-10-09',
  matching_passages: 1198, indexed_passages: 1198, audio: 'available' });
test('F-17: with no query the Episodes card says "1,198 passages"; with a query it keeps matching and indexed', async () => {
  const win = await views();
  assert.match(win.EWViews.passageCounts(epItem(), ''), /^1,198 passages$/);
  assert.match(win.EWViews.passageCounts({ ...epItem(), indexed_passages: 1 }, ''), /^1 passage$/);
  assert.equal(win.EWViews.passageCounts(epItem(), 'sleep'), '1,198 matching passages · 1,198 indexed passages');
  const panel = win.EWViews.inspectorHtml(epItem(), { q: '' }, 1);
  assert.match(panel, /<p>1,198 passages<\/p>/);
  assert.match(panel, />Open transcript<\/a>/);
  assert.doesNotMatch(panel, /Open episode/);
});

/* -- F-18 / F-19 ------------------------------------------------------------- */
test('F-18: the Filters dialog scrolls a focused field clear of its sticky head, and date fields have a gap', () => {
  const c = css('search.css');
  assert.match(c, /dialog\.ew-sheet \{ scroll-padding-block: 76px; \}/);
  assert.match(c, /\.filter-block\[data-filter-group="dates"\] \.od-field \+ \.od-field \{ margin-top: var\(--ew-s3\); \}/);
});
test('F-19: the rail says "Reset scope" like the chips and the empty state, and the phone sheet drops its eyebrow', async () => {
  assert.match(css('search.css'), /\.ew-inspector-sheet \.ew-inspector-head > \.eyebrow \{ display: none; \}/);
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /button\('reset-filters', 'Reset scope', 'quiet'\)\}<\/div><div id="filters-rail-errors"/);
  assert.doesNotMatch(src, /button\('reset-filters', 'Clear all'/);
});

/* -- V items ---------------------------------------------------------------- */
test('V-14: the Overview campaigns disclosure has a singular form', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /more tracked campaign\$\{campaigns\.length - 3 === 1 \? '' : 's'\}/);
});
test('V-17: attribution names the brand, not its key', async () => {
  const win = await views();
  const text = win.EWViews.attributionText({ read: { brand: { key: 'betterhelp', display: 'BetterHelp' } },
    attribution: [{ brand: 'betterhelp', basis: 'suggested' }, { brand: 'betterhelp', basis: 'suggested' }, { brand: 'acme', basis: 'confirmed' }] },
    [{ key: 'acme', display: 'Acme' }]);
  // R1-10: the other brand is named from the directory, never its key.
  assert.equal(text, 'BetterHelp, Acme', 'PLAIN-ADS: never the review basis');
  assert.equal(win.EWViews.attributionText({ attribution: [{ brand: 'acme', brand_display: 'Acme Co', basis: 'confirmed' }] }), 'Acme Co');
});
test('V-31: buyers see "Collection", not "Corpus", on Search and Overview', async () => {
  const html = await render('#/search?mode=ads&q=zz', searchApi(bundle([adItem()])));
  assert.match(html, />Collection definitions</);
  assert.doesNotMatch(html, /Corpus definitions/);
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /<summary>Collection scope &amp; limits<\/summary>/);
  assert.match(src, /class="corpus-disclosure/, 'class names stay');
});
test('V-38: "No clip kept" has no leading dash', async () => {
  const win = await views();
  const card = win.EWUI.sourceCard(adItem(), { index: 0 });
  assert.match(card, /<span class="ew-noclip">No clip \(transcript only\)<\/span>/);
});
test('V-39: the search placeholder fits a phone, and the public sub-nav wraps instead of hiding its scrollbar', async () => {
  const html = await render('#/search?mode=ads&q=zz', searchApi(bundle([adItem()])));
  assert.match(html, /placeholder="Brand, offer or phrase"/);
  const pub = fs.readFileSync(path.join(APP, 'public', 'public.css'), 'utf8');
  assert.match(pub, /\.doc-nav ul \{ flex-direction: row; flex-wrap: wrap; \}/);
  assert.doesNotMatch(pub, /\.doc-nav \{[^}]*scrollbar-width: none/);
});
test('V-44: the 404 page links Home, not "Product landing"', () => {
  const html = fs.readFileSync(path.join(APP, 'public', '404.html'), 'utf8');
  assert.match(html, /<li><a href="\/">Home<\/a><\/li>/);
  assert.doesNotMatch(html, /Product landing/);
});
test('VL-31: the inspector duration says it is a length', async () => {
  const win = await views();
  const panel = win.EWViews.inspectorHtml(adItem(), { q: '' }, 1);
  assert.match(plain(panel), /to 2:01 · 0:59 long/);
});
