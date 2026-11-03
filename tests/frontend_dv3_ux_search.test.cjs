/* Design v3 UX-review fixes, Search (T2 + T5 search items).
   Same stubbed-browser loader as frontend_dv3_g1-search.test.cjs; product
   rules held: unreviewed names keep their single flag (Brand not identified
   unchanged), the match rule is only labelled, unknown stays unknown, no
   per-filter counts, Escape still closes a dialog (a changed entry is kept, UX-09).
   Run: node --test tests/frontend_dv3_ux_search.test.cjs
   (EW_APP_DIR=<dir> runs it against another copy of app/.) */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const listeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.listeners = listeners;
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

const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'whole', display: 'Whole', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const passage = (over = {}) => ({ ref: 'sr1:p:w:264.46:277.46', record_kind: 'transcript_passage', status: 'mention', ep_title: 'The Ouija Defense',
  show_title: 'Triggered', show: { show_identity_id: 'cs-2', display_title: 'Triggered' }, pub_date: '2026-10-09',
  text: 'two months free, trial eligibility applies', quotation_span: [264.46, 277.46], audio: 'available', audio_local: false, read: null, ...over });
const bundle = (mode, items, extra = {}, q = 'sleep', summary = []) => ({ query: { version: 2, mode, q, sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0, ...extra },
  coverage: { episodes_in_scope: 288 }, summary: { week: summary } });
const searchApi = (b, brands = []) => p => {
  if (p === '/customer/brands') return { brands };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  throw new Error('unexpected ' + p);
};
const cardOf = (html, ref) => (html.match(new RegExp(`<article class="ew-result[^>]*id="row-${ref.replace(/[:.]/g, '\\$&')}"[\\s\\S]*?</article>`)) || [''])[0];
const rowOf = (html, ref) => (html.match(new RegExp(`<li class="ew-row[^>]*id="row-${ref}"[\\s\\S]*?</li>`)) || [''])[0];
const asideOf = html => (html.match(/<aside class="ew-inspector"[\s\S]*?<\/aside>/) || [''])[0];

/* -- UX-02: Name not confirmed ------------------------------------------- */
const NAME_CASES = {
  unconfirmed: { brand: { key: 'whole', display: 'Whole', confirmed: false }, needs_review: false },
  confirmed: { brand: { key: 'whole', display: 'Whole', confirmed: true }, needs_review: false },
  review: { brand: { ...UNDER_REVIEW }, needs_review: false },
  needs: { brand: { key: 'whole', display: 'Whole', confirmed: false }, needs_review: true },
};
const readFor = k => ({ ...NAME_CASES[k], display_span: [62, 121], has_clip: false });

// PLAIN-ADS (DEP7) replaces UX-02: a shown name carries no flag on the
// card, the list row or the inspector; a held brand keeps its held tag
// ("Brand not identified") and never a name.
test('UX-02 card: a shown name carries no flag, whatever its review; a held brand keeps the held tag', () => {
  const ui = loadAt('#/search').EWUI;
  const card = k => ui.sourceCard(adItem({ read: readFor(k) }), { query: 'sleep' });
  for (const k of ['unconfirmed', 'confirmed', 'needs']) assert.doesNotMatch(card(k), /Name not confirmed|Needs review/, k);
  const review = card('review');
  assert.match(review, /<span class="ew-brand-none">Brand not identified<\/span>/);
  assert.doesNotMatch(review, /data-tag="review"/, 'PLAIN-ADS-FIX1: no amber held tag');
  assert.doesNotMatch(review, /Whole/, 'held: the held tag only, never a name');
});

test('UX-02 list row: the name sits in its own span with no flag', () => {
  const ui = loadAt('#/search').EWUI;
  const row = k => ui.resultRow(adItem({ read: readFor(k) }), { index: 0 });
  assert.match(row('unconfirmed'), /<span class="ew-row-brand"><span class="ew-row-brand-name"><span class="ew-brand">Whole<\/span><\/span><\/span>/);
  for (const k of ['unconfirmed', 'confirmed', 'needs']) assert.doesNotMatch(row(k), /Name not confirmed|Needs review/, k);
  assert.match(row('review'), /<span class="ew-brand-none">Brand not identified<\/span>/);
  assert.doesNotMatch(row('review'), /data-tag="review"/);
  assert.doesNotMatch(row('review'), /Whole/);
});

test('UX-02 inspector and its live title name the brand with no flag; Brand not identified unchanged', async () => {
  for (const k of ['unconfirmed', 'confirmed', 'review', 'needs']) {
    const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem({ read: readFor(k) })])));
    const aside = asideOf(await t.render());
    assert.doesNotMatch(aside, /[Nn]ame not confirmed|Name review|Needs review/, k);
    if (k === 'review') assert.doesNotMatch(aside, /Whole/);
    if (k === 'unconfirmed') assert.match(aside, /data-inspector-live>Selected: Whole, Show one</);
  }
});

/* -- UX-04: match mode, mention titles, matched passage first ------------ */
test('UX-04 match-mode line labels the AND rule and offers the exact phrase', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  const V = t.win.EWViews;
  const both = V.matchModeLine({ mode: 'ads', q: 'free trial' });
  assert.match(both, /Both words, in any order: free, trial/);
  const href = both.match(/href="([^"]+)"/)[1].replace(/&amp;/g, '&');
  assert.equal(new URLSearchParams(href.split('?')[1]).get('q'), '"free trial"');
  assert.match(both, /Search the exact phrase “free trial”/);
  assert.match(V.matchModeLine({ mode: 'ads', q: '"free trial"' }), /Exact phrase: “free trial”/);
  assert.doesNotMatch(V.matchModeLine({ mode: 'ads', q: '"free trial"' }), /href=/);
  assert.match(V.matchModeLine({ mode: 'ads', q: 'a b c' }), /All of these words, in any order: a, b, c/);
  assert.match(V.matchModeLine({ mode: 'ads', q: '"free trial" code' }), /Exact phrase “free trial” plus the word code, in any order/);
  assert.equal(V.matchModeLine({ mode: 'ads', q: 'cola' }), '');
  assert.equal(V.matchModeLine({ mode: 'ads', q: '' }), '');
  assert.doesNotMatch(V.matchModeLine({ mode: 'ads', q: '<b> x' }), /<b>/, 'user text is escaped');
  const html = await t.render();
  assert.doesNotMatch(html, /class="ew-match-mode"/, 'one loose word: no line');
  const u = await views('#/search?mode=mentions&q=free+trial', searchApi(bundle('mentions', [passage()], {}, 'free trial')));
  assert.match(await u.render(), /<p class="ew-count-line"[^>]*>[^<]*<\/p><p class="ew-match-mode">Both words/);
});

test('UX-04 a mention is titled by show · episode · date, kind as a tag, and shows its time', async () => {
  const t = await views('#/search?mode=mentions&q=free+trial', searchApi(bundle('mentions', [passage()], {}, 'free trial')));
  const html = await t.render();
  const card = cardOf(html, 'sr1:p:w:264.46:277.46');
  assert.match(card, /<h3 class="ew-result-title ew-mention-title od-clamp-2"><a [^>]*href="#\/show\/cs-2">Triggered<\/a> · The Ouija Defense · Oct 9<\/h3>/);
  assert.match(card, /<span class="ew-tag ew-tag-outline" data-tag="outline">Mention<\/span>/);
  assert.match(card, /4:24–4:37/, 'the passage span gives the card its time');
  const aside = asideOf(html);
  assert.match(aside, /<span class="eyebrow">Selected · Mention<\/span><h2>Triggered<\/h2>/);
  assert.match(aside, /<span class="ew-inspector-where">The Ouija Defense · Oct 9<\/span>/);
  assert.match(aside, /data-inspector-live>Selected: Mention in Triggered at 4:24–4:37</);
  assert.match(aside, /<th scope="row">Time in episode<\/th><td class="ew-ledger-value-mono">4:24–4:37/);
});

test('UX-04 the matched passage leads the card and the inspector; text stays the fallback', async () => {
  const late = adItem({ text: 'opening words only', match_excerpt: { text: 'start a free trial today', start: 805 } });
  const t = await views('#/search?mode=ads&q=free+trial', searchApi(bundle('ads', [late], {}, 'free trial')));
  const html = await t.render();
  assert.match(cardOf(html, 'sr1:a'), /<p class="ew-result-quote od-clamp-3">“…start a <mark>free<\/mark> <mark>trial<\/mark> today”/);
  assert.match(asideOf(html), /<p class="ew-inspector-quote">“…start a <mark>free/);
  assert.doesNotMatch(html, /opening words only/);
  const ui = t.win.EWUI;
  assert.match(ui.sourceCard(adItem(), { query: 'sleep' }), /“<mark>sleep<\/mark> better tonight”/, 'no excerpt: the record text, no ellipsis');
});

/* -- UX-09: week readout, brand filter marker ---------------------------- */
// Row 1: an ads chart counts the ads its cells carry (here one ad per piece of evidence).
const cell = (from, to, count) => ({ key: from, label: `${from}–${to}`, count, breaks: count, interval: { from, to },
  selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'summary_cell' } } });
test('UX-09 week strip: bars carry their week and count; the caption names the peak and missing weeks', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  const V = t.win.EWViews;
  const gap = V.weekChart([cell('2026-06-15', '2026-06-21', 10), cell('2026-07-20', '2026-07-26', 3)], { mode: 'ads' }, { total_relation: 'exact' });
  assert.match(gap, /data-week="Week of Jun 15–21" data-count="10 ads"/);
  assert.match(gap, /class="ew-week-caption" data-default="[^"]*most: 10 ads \(Jun 15\)[^"]*weeks with no matches are not drawn/);
  const near = V.weekChart([cell('2026-09-28', '2026-10-04', 291), cell('2026-10-05', '2026-10-11', 1007)], { mode: 'ads' }, { total_relation: 'exact' });
  assert.match(near, /most: 1,007 ads \(Oct 5\)/);
  assert.doesNotMatch(near, /not drawn/);
  const low = V.weekChart([cell('2026-09-28', '2026-10-04', 2)], { mode: 'ads' }, { total_relation: 'lower_bound' });
  assert.match(low, /data-count="at least 2 ads"/);
  assert.match(low, /highest shown: at least 2 .*lower bounds/, 'a lower bound stays a lower bound');
});

test('UX-09 week readout follows hover and focus, then restores the caption', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  const fns = ['mouseover', 'mouseout'].map(type => t.win.listeners[type] || []);
  assert.ok(fns[0].length && fns[1].length, 'delegated listeners exist');
  const cap = { dataset: { default: 'By week · most: 10' }, textContent: 'By week · most: 10', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; } };
  const bars = { dataset: { noun: 'ads', nounOne: 'ad' } };
  const row = { querySelector: () => cap };
  const bar = { dataset: { week: 'Jun 15–Jun 21', count: '10' }, closest: s => (s.includes('ew-week-bars') ? bars : s.includes('ew-week-bar') ? bar : s.includes('ew-week-row') ? row : null) };
  const ev = type => ({ type, target: { closest: s => (s.includes('ew-week-bar') ? bar : null) } });
  fns[0].forEach(f => f(ev('mouseover')));
  assert.equal(cap.textContent, 'Jun 15–Jun 21: 10 ads');
  assert.equal(cap.attrs['aria-hidden'], 'true');
  fns[1].forEach(f => f(ev('mouseout')));
  assert.equal(cap.textContent, 'By week · most: 10');
  assert.equal(cap.attrs['aria-hidden'], undefined);
});

test('UX-09 / UX-15e brand filter: no review tag (PLAIN-ADS); hints in plain words; no counts', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  const body = t.win.EWViews.filtersBody({ brands: null, shows: null, categories: null },
    { brands: [{ value: 'acme', label: 'Acme', confirmed: true }, { value: 'lumen', label: 'Lumen', confirmed: false }], shows: [], categories: [] });
  assert.match(body, /value="acme" > Acme</);
  assert.doesNotMatch(body, /data-tag="confirmed"|[Cc]onfirmed/);
  assert.match(body, /Most heard first, one name per brand with spellings merged\. Brands you already picked stay listed\./);
  assert.doesNotMatch(body, /Record status|name="statuses"/, 'no status filter');
  assert.match(body, /Shows are told apart by their feed, never by title alone\./);
  assert.doesNotMatch(body, /Canonical|Provenance-backed/);
  assert.match(body, /data-filter-group="dates"/);
  assert.match(body, /data-filter-group="sort"/);
});

/* -- UX-10: one verb, resting cue, the phone sheet ------------------------ */
test('UX-10 Search says "Open evidence" on cards, rows and the inspector, with a preview hint', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  assert.doesNotMatch(html, /View evidence|Inspect evidence/);
  assert.match(cardOf(html, 'sr1:a'), /<a class="button ew-view" href="#\/evidence\/sr1%3Aa">Open evidence<\/a>/);
  assert.match(cardOf(html, 'sr1:a'), /data-action="inspect"[^>]*title="Preview this result"/);
  assert.match(t.win.EWUI.resultRow(adItem(), { index: 0 }), /aria-label="Open evidence: Whole/);
});

function sheetHarness(t, wide) {
  const modal = { open: false, innerHTML: '', classes: new Set(), listeners: {},
    classList: { add: c => modal.classes.add(c), remove: c => modal.classes.delete(c) },
    addEventListener: (type, fn) => { modal.listeners[type] = fn; }, querySelector: () => null, querySelectorAll: () => [] };
  const body = { innerHTML: '' }, live = { textContent: '' };
  const aside = { querySelector: s => (s === '.ew-inspector-body' ? body : s === '[data-inspector-live]' ? live : null) };
  t.win.matchMedia = q => ({ matches: wide && /1200/.test(q) });
  t.win.document.querySelector = s => (s === '#modal' ? modal : s === '.ew-inspector' ? aside : null);
  t.win.document.querySelectorAll = () => [];
  t.win.EW.openDialog = (title, html) => { modal.open = true; modal.title = title; modal.innerHTML = html; };
  return { modal, body, live };
}
test('UX-10 below 1200 px "inspect" opens the inspector in the bottom sheet; at 1200 px it paints the panel', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem(), adItem({ ref: 'sr1:b', read: readFor('confirmed') })])));
  await t.render();
  const phone = sheetHarness(t, false);
  await t.win.EWViews.handleAction({ dataset: { action: 'inspect', index: '1' }, closest: () => null });
  assert.equal(phone.modal.title, 'Selected ad');
  assert.ok(phone.modal.classes.has('ew-sheet'));
  assert.match(phone.modal.innerHTML, /<div class="ew-inspector ew-inspector-sheet">/);
  assert.match(phone.modal.innerHTML, /<a class="button primary" href="#\/evidence\/sr1%3Ab">Open evidence<\/a>/);
  assert.match(phone.modal.innerHTML, /data-action="save-excerpt" data-ref="sr1:b"/);
  phone.modal.listeners.close();
  assert.ok(!phone.modal.classes.has('ew-sheet'), 'the class leaves with the dialog');
  const wide = sheetHarness(t, true);
  await t.win.EWViews.handleAction({ dataset: { action: 'inspect', index: '1' }, closest: () => null });
  assert.equal(wide.modal.open, false, 'no dialog at 1200 px and up');
  assert.match(wide.body.innerHTML, /Open evidence/);
  assert.match(wide.live.textContent, /^Selected: Whole, Show one$/);
});

/* -- UX-13: Filters open where the user was ------------------------------ */
async function filtersFocus(route, last) {
  const t = await views(route, searchApi(bundle('ads', [adItem()])));
  await t.render();
  if (last) t.win.EW.current.filtersLastGroup = last;
  let focused = null;
  const fsFor = name => ({ scrollIntoView() {}, querySelector: s => (s === '[data-lookup]' && !['dates', 'sort', 'statuses', 'audio'].includes(name) ? { focus: () => { focused = name + ':lookup'; } }
    : s.startsWith('input') ? { focus: () => { focused = name + ':input'; } } : null) });
  const modal = { open: true, innerHTML: '', classList: { add() {}, remove() {} }, addEventListener() {},
    querySelector: s => { const m = s.match(/data-group="([a-z]+)"/); if (m) return fsFor(m[1]); if (s.startsWith('[autofocus]')) return { focus: () => { focused = 'first-input'; } }; return null; } };
  t.win.document.querySelector = s => (s === '#modal' ? modal : null);
  t.win.document.activeElement = t.win.document.body = {};
  t.win.EW.openDialog = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'filters' }, closest: () => null });
  return focused;
}
test('UX-13 Filters open on the last-used group, else the first set group, else as before', async () => {
  assert.equal(await filtersFocus('#/search?mode=ads&q=x', 'brands'), 'brands:lookup');
  assert.equal(await filtersFocus('#/search?mode=ads&q=x&shows=%5B%22cs-1%22%5D'), 'shows:lookup');
  assert.equal(await filtersFocus('#/search?mode=ads&q=x'), 'first-input');
});

test('UX-13 the last-used Filters group is noted from the rail or the dialog only', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  const fs = { dataset: { group: 'shows' } };
  const target = form => ({ closest: s => (s.includes('#filters') ? form : s.includes('data-group') ? fs : null) });
  for (const f of t.win.listeners.change) f({ target: target({ id: 'filters-rail' }) });
  assert.equal(t.win.EW.current.filtersLastGroup, 'shows');
  t.win.EW.current.filtersLastGroup = undefined;
  for (const f of t.win.listeners.change) f({ target: target(null) });
  assert.equal(t.win.EW.current.filtersLastGroup, undefined);
});

/* -- R2-15: brand jump, empty state, Escape toast, saved-view note -------- */
test('R2-15 a brand-name query links its brand page and a one-step brand filter', async () => {
  // A confirmed name; an unconfirmed one: frontend_dv3_ux_fixA (F-01).
  const brands = [{ key: 'betterhelp', brand: 'betterhelp', display: 'BetterHelp', records: 21, confirmed: 3 }];
  const t = await views('#/search?mode=ads&q=betterhelp', searchApi(bundle('ads', [adItem()], {}, 'betterhelp'), brands));
  const html = await t.render();
  assert.match(html, /<p class="ew-brand-jump">BetterHelp is a brand heard in ads\. <a class="text-link" href="#\/brand\/betterhelp">See where BetterHelp advertises<\/a>/);
  const href = html.match(/href="([^"]+)">Show only ads named BetterHelp/)[1].replace(/&amp;/g, '&');
  const p = new URLSearchParams(href.split('?')[1]);
  assert.equal(p.get('q'), null);
  assert.match(p.get('brands'), /betterhelp/);
  const V = t.win.EWViews;
  assert.equal(V.brandJump({ mode: 'ads', q: '"betterhelp"' }, brands), '', 'a quoted query is a phrase search');
  assert.equal(V.brandJump({ mode: 'ads', q: 'cola' }, brands), '');
  assert.equal(V.brandJump({ mode: 'episodes', q: 'betterhelp' }, brands), '');
  assert.equal(V.brandJump({ mode: 'ads', q: 'betterhelp', brands: ['betterhelp'] }, brands), '', 'already filtered');
});

test('R2-15 no results: no view switch, Sort or inspector; Export stays disabled with its reason', async () => {
  const t = await views('#/search?mode=ads&q=zzz', searchApi(bundle('ads', [], {}, 'zzz')));
  const html = await t.render();
  assert.match(html, /No ads say “zzz” in this scope/);
  assert.doesNotMatch(html, /ew-inspector/);
  assert.doesNotMatch(html, /search-sort/);
  assert.doesNotMatch(html, /results_view/);
  assert.match(html, /class="ew-search-body no-inspector"/);
  assert.match(html, /data-action="export-open"[^>]*disabled/);
  assert.doesNotMatch(html, /export-selected/, 'no results: no Export selected control or hint (UAT F-11)');
});

test('R2-15 / UX-09 Escape on a typed, unsaved dialog says it was not saved and the entry is kept; untouched closes silently', async () => {
  for (const [value, dirty, toasts] of [['draft', true, 1], ['', true, 0], ['draft', false, 0]]) {
    const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
    const title = { name: 'title', type: 'text', value, defaultValue: '' };
    const form = { dataset: dirty ? { dirty: '1' } : {}, elements: [title], getAttribute: k => (k === 'id' ? 'save-excerpt' : null),
      querySelectorAll: () => [title] };
    const modal = { open: true, querySelectorAll: s => (s === 'form[data-dirty="1"]' && dirty ? [form] : []) };
    t.win.document.querySelector = s => (s === '#modal' ? modal : null);
    t.win.FormData = class { constructor() { this.e = [['title', value]]; } *entries() { yield* this.e; } };
    const said = [];
    t.win.EW.toast = m => said.push(m);
    t.win.EW.closeDialog = () => { modal.open = false; };
    for (const f of t.win.listeners.keydown) f({ key: 'Escape' });
    assert.equal(modal.open, false, 'Escape still closes the dialog');
    assert.equal(said.filter(m => /^Not saved\. Your entry is kept — reopen it to continue\.$/.test(m)).length, toasts, `${value}/${dirty}`);
    assert.equal(t.win.EW.current.dialogDrafts.size, toasts, 'kept in memory exactly when said');
  }
});

test('R2-15 a bare Search redirected to the saved settings says so once, on that route', async () => {
  const prefs = { '/api/v1/customer/preferences': { preferences: { search_tab: 'ads', results_view: 'list', default_range: '30d', revision: 1 } } };
  const t = await views('#/search', searchApi(bundle('ads', [adItem()])), prefs);
  t.win.setTimeout = setTimeout;
  const went = [];
  t.win.EW.go = r => went.push(r);
  await t.render();
  assert.equal(went.length, 1);
  t.win.location.hash = went[0];
  t.win.EW.current.route = went[0];
  const html = await t.render();
  assert.match(html, /<p class="ew-pref-note" role="status">Opened with your saved Search settings \(Ads · List · since [A-Z][a-z]{2} \d{1,2}\)\. <a class="text-link" href="#\/settings">Change in Settings<\/a>/);
  assert.match(html, /data-action="dismiss-pref-note"/);
  const u = await views('#/search?mode=ads', searchApi(bundle('ads', [adItem()])), prefs);
  assert.doesNotMatch(await u.render(), /ew-pref-note/, 'a typed route is canonical: no note');
  await t.win.EWViews.handleAction({ dataset: { action: 'dismiss-pref-note' }, closest: () => ({ remove() {} }) });
  assert.doesNotMatch(await t.render(), /ew-pref-note/);
});

/* -- T5 search items ------------------------------------------------------ */
test('T5 attribution: one entry per brand, never its review basis (PLAIN-ADS)', async () => {
  const t = await views('#/search?mode=ads&q=x', searchApi(bundle('ads', [adItem()])));
  // R1-10: names come from the brand directory, never the raw key.
  const text = t.win.EWViews.attributionText({ attribution: [{ brand: 'betterhelp', basis: 'suggested' }, { brand: 'betterhelp', basis: 'suggested' }, { brand: 'acme', basis: 'confirmed' }] },
    [{ key: 'betterhelp', display: 'BetterHelp' }, { key: 'acme', display: 'Acme' }]);
  assert.equal(text, 'BetterHelp, Acme');
  const channels = t.win.EWViews.attributionText({ attribution: [{ brand: 'betterhelp', basis: 'suggested', channel: 'spoken' }, { brand: 'betterhelp', basis: 'confirmed', channel: 'url' }] },
    [{ key: 'betterhelp', display: 'BetterHelp' }]);
  assert.equal(channels, 'BetterHelp (heard, web address)');
});

test('T5 Export selected: an outlined button with its reason at 0; the reason hides once something is selected', async () => {
  const t = await views('#/search?mode=ads&q=sleep', searchApi(bundle('ads', [adItem()])));
  const html = await t.render();
  assert.match(html, /<div class="ew-export-group"><button type="button" class="button " data-action="export-selected" aria-describedby="export-selected-why" disabled>Export selected \(0\)<\/button><span class="ew-help" id="export-selected-why">Select results to export them\.<\/span><\/div>/);
  const attrs = {};
  const btn = { textContent: '', dataset: {}, setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: k => { delete attrs[k]; } };
  const why = { hidden: false };
  t.win.document.querySelector = s => (s === '[data-action="export-selected"]' ? btn : s === '#export-selected-why' ? why : null);
  t.win.EWViews.setSelection(t.win.EW.current.route, ['sr1:a'], 'rev-1');
  assert.equal(why.hidden, true);
  assert.equal(btn.textContent, 'Export selected (1)');
  t.win.EWViews.setSelection(t.win.EW.current.route, [], 'rev-1');
  assert.equal(why.hidden, false);
});

/* -- CSS ------------------------------------------------------------------ */
const css = () => fs.readFileSync(path.join(APP, 'search.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
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
const outsideMedia = text => text.replace(/@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, '');
const decls = (text, selector) => {
  const out = [];
  for (const m of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) if (m[1].split(',').map(s => s.trim()).includes(selector)) out.push(m[2]);
  return out.join(';');
};

test('CSS UX-10: the inspect control shows at every width with a resting cue; cards preview on hover at 1200', () => {
  const base = outsideMedia(css());
  const inspect = decls(base, '.ew-inspect');
  assert.doesNotMatch(inspect, /display:\s*none/);
  assert.match(inspect, /text-decoration: underline dotted/);
  assert.match(decls(mediaBlocks(css(), '(min-width: 1200px)'), '.ew-search-card:hover:not(.is-selected)'), /border-color: var\(--ew-border-strong\)/);
  assert.match(decls(base, 'dialog.ew-sheet .ew-inspector-sheet'), /display: flex/);
});

test('CSS R2-11 / UX-13: compact phone chrome and shorter cards', () => {
  const phone = mediaBlocks(css(), '(max-width: 599px)');
  assert.match(decls(phone, '.ew-search-card .ew-result-actions'), /grid-template-areas: "clip clip" "report view"/);
  assert.match(decls(phone, '.ew-results-tools .ew-sort'), /display: none/);
  assert.match(decls(phone, '.ew-quick-scope'), /display: none/);
  assert.match(decls(phone, '.ew-search-head'), /position: sticky/);
  assert.match(decls(phone, '.ew-search-results .ew-week:has(.ew-week-data:not([open])) .ew-week-row'), /display: none/);
  assert.match(decls(phone, '.ew-scope-line > span:not(.ew-tag)'), /text-overflow: ellipsis/);
});

test('CSS T5: 44 px checkbox targets on touch; the whole List row opens below 1200 px', () => {
  assert.match(decls(mediaBlocks(css(), '(max-width: 1023px), (pointer: coarse)'), '.ew-select'), /min-width: 44px; min-height: 44px/);
  assert.match(decls(outsideMedia(css()), '.ew-select'), /min-height: var\(--ew-target\)/, 'the base rule stays');
  const narrow = mediaBlocks(css(), '(max-width: 1199px)');
  assert.match(decls(narrow, '.ew-row-open::after'), /position: absolute; inset: 0/);
  assert.match(decls(narrow, '.ew-row .ew-select'), /z-index: 1/);
  assert.match(decls(narrow, '.ew-row .ew-inspect'), /z-index: 1/);
});
