/* Astra decisions 2 and 3 (area tags, G2 UX-01 status tags), replaced by
   PLAIN-ADS (DEP7): no review-state tag ("Possible ad", "Confirmed ad",
   "Unresolved", "Name not confirmed", "Needs review") on Search cards,
   list rows or the Brand page, for members and the local operator alike;
   a show's own promotion keeps its tag; a held brand reads "Brand not
   identified" and still counts toward no brand.
   Loader copied from frontend_dv3_g3.test.cjs.
   Run: node --test tests/frontend_dv3_astra_tags.test.cjs */
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
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
      workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(route) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
// Render one route against a path -> body table (an Error rejects).
async function page(route, api) {
  const win = loadViews(route);
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  const paths = [];
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.api = async (p, opts) => {
    paths.push(p);
    const out = api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settleAll();
  return { html: main.innerHTML, paths, win, toasts, main };
}

const boom = (status = 500) => Object.assign(new Error('boom'), { status });
const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const adItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: true }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const bundle = (mode, items) => ({ query: { version: 2, mode, q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchApi = b => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return b;
  throw boom();
};
const cardOf = (html, ref) => (html.match(new RegExp(`<article class="ew-result[^>]*id="row-${ref}"[\\s\\S]*?</article>`)) || [''])[0];
const rowOf = (html, ref) => (html.match(new RegExp(`<li class="ew-row[^>]*id="row-${ref}"[\\s\\S]*?</li>`)) || [''])[0];
const tagsOf = html => [...html.matchAll(/<span class="(?:ew-)?tag [^>]*>(.*?)<\/span>/g)].map(m => m[1].replace(/<[^>]+>/g, ''));

async function searchAs(hosted, items, opts = {}) {
  const route = `#/search?mode=${opts.mode || 'ads'}&q=sleep&view=${opts.view || 'cards'}`;
  const win = loadViews(route);
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  const api = searchApi(bundle(opts.mode || 'ads', items));
  // Hosted is set once the page's own reads start: the shell's hosted
  // admission is not under test, only the labels.
  win.EW.api = async (p, o) => { win.EW.current.hosted = hosted; return api(p, o); };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await settleAll();
  return main.innerHTML;
}

const STATUS_WORDS = /Possible ad|Confirmed ad|Confirmed|Unresolved|Name not confirmed|Needs review|Not counted yet/;
test('Search Ads cards carry no status tag, hosted and local', async () => {
  for (const hosted of [true, false]) {
    const html = await searchAs(hosted, [adItem()]);
    assert.doesNotMatch(cardOf(html, 'sr1:a'), STATUS_WORDS, `hosted=${hosted}`);
    assert.match(cardOf(html, 'sr1:a'), /<summary>Report a mistake<\/summary>/);
  }
});

test('a confirmed ad reads like any other ad on cards and list rows, hosted and local alike', async () => {
  for (const hosted of [true, false]) {
    const items = [adItem({ status: 'confirmed' })];
    const cards = await searchAs(hosted, items);
    assert.doesNotMatch(cardOf(cards, 'sr1:a'), STATUS_WORDS, `card hosted=${hosted}`);
    const list = await searchAs(hosted, items, { view: 'list' });
    assert.doesNotMatch(rowOf(list, 'sr1:a'), STATUS_WORDS, `row hosted=${hosted}`);
    assert.match(rowOf(list, 'sr1:a'), /<span class="ew-row-status ew-row-report"><details class="report-problem"><summary>Report a mistake<\/summary>/);
  }
});

test('hosted and local render the same page on a mixed page', async () => {
  const items = [adItem(), adItem({ ref: 'sr1:c', status: 'confirmed' })];
  for (const view of ['cards', 'list']) {
    const a = await searchAs(true, items, { view }), b = await searchAs(false, items, { view });
    assert.equal(a, b, view);
  }
});

test('a name the namer flagged is shown with no flag, on cards and rows', async () => {
  const item = adItem({ read: { brand: { key: 'ag1', display: 'AG1', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: true } });
  const html = await searchAs(true, [item]);
  const card = cardOf(html, 'sr1:a');
  assert.match(card, /<h3 class="ew-result-title">[^]*AG1/);
  assert.deepEqual(tagsOf(card.slice(card.indexOf('ew-result-head'))), []);
  const row = rowOf(await searchAs(true, [item], { view: 'list' }), 'sr1:a');
  assert.match(row, /AG1/);
  assert.doesNotMatch(row, STATUS_WORDS);
});

test('held brand: card and row say "Brand not identified", no guessed name and no status', async () => {
  const hidden = adItem({ read: { brand: { ...UNDER_REVIEW }, display_span: [10, 40], has_clip: false } });
  const card = cardOf(await searchAs(true, [hidden]), 'sr1:a');
  // PLAIN-ADS-FIX1 (screen R1-02): plain text, no amber held tag.
  assert.match(card, /<h3 class="ew-result-title"><span class="ew-brand-none">Brand not identified<\/span><\/h3>/);
  assert.doesNotMatch(card, /ew-tag-review/);
  const row = rowOf(await searchAs(true, [hidden], { view: 'list' }), 'sr1:a');
  assert.match(row, /Brand not identified/);
  assert.doesNotMatch(row, STATUS_WORDS);
});

test('an unresolved ad reads "Ad", with no status tag', async () => {
  const win = loadAt('#/search');
  assert.equal(win.EWUI.statusLabel('unresolved'), 'Ad');
  assert.equal(win.EWUI.statusBadge('unresolved'), '');
  const card = cardOf(await searchAs(true, [adItem({ status: 'unresolved' })]), 'sr1:a');
  assert.doesNotMatch(card, STATUS_WORDS);
});

test('a show\'s own promotion carries its own tag and no ad status', async () => {
  const item = adItem({ status: 'confirmed', read: { brand: { key: 'ag1', display: 'AG1', confirmed: true }, display_span: [62, 121], has_clip: false, self_promo: true } });
  for (const hosted of [true, false]) {
    const card = cardOf(await searchAs(hosted, [item]), 'sr1:a');
    assert.match(card, /Show’s own promotion/);
    assert.doesNotMatch(card, STATUS_WORDS);
    const row = rowOf(await searchAs(hosted, [item], { view: 'list' }), 'sr1:a');
    assert.doesNotMatch(row, STATUS_WORDS);
  }
});

test('All mentions: no row has a status tag; a mention gets no report control', async () => {
  const items = [adItem(), adItem({ ref: 'sr1:c', status: 'confirmed' }), adItem({ ref: 'sr1:m', record_kind: 'transcript_passage', status: 'mention', read: null })];
  for (const hosted of [true, false]) {
    const html = await searchAs(hosted, items, { mode: 'mentions' });
    for (const ref of ['sr1:a', 'sr1:c', 'sr1:m']) assert.doesNotMatch(cardOf(html, ref), STATUS_WORDS, ref);
    assert.match(cardOf(html, 'sr1:c'), /Report a mistake/);
    assert.doesNotMatch(cardOf(html, 'sr1:m'), /Report a mistake/);
  }
});

test('statusBadge is empty for every status, any viewer; statusLabel says Ad or Mention', () => {
  const W = loadAt('#/search');
  for (const hosted of [true, false]) {
    W.EW.current.hosted = hosted;
    for (const st of ['finding', 'confirmed', 'unresolved', 'mention', '']) assert.equal(W.EWUI.statusBadge(st), '', st);
    assert.equal(W.EWUI.statusLabel('confirmed'), 'Ad');
    assert.equal(W.EWUI.statusLabel('finding'), 'Ad');
    assert.equal(W.EWUI.statusLabel('mention'), 'Mention');
  }
});

const SR = n => 'sr1:' + String(n).repeat(32).slice(0, 32);
const bread = (n, status, over = {}) => ({ ref: SR(n), record_kind: 'evidence', status, episode_id: 'e' + n,
  show_title: 'Show ' + n, ep_title: 'Ep ' + n, pub_date: '2026-10-0' + n, text: 'Talk ' + n,
  read: { brand: { key: 'quince', display: 'Quince', confirmed: status === 'confirmed' }, display_span: [60, 90], has_clip: false }, ...over });
const BRAND = { brand: { brand: 'quince', key: 'quince', display: 'Quince', detections: 2, episodes: 2, shows: 2, last_heard: '2026-10-02',
  confirmed_occurrences: [SR(1)], findings: [SR(2)], offer_sequence: [],
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, mentions: { version: 2, mode: 'mentions', brands: ['quince'] } } } };

test('brand page read rows: no status tag, each with Report a mistake, hosted and local', async () => {
  const reads = [bread(1, 'confirmed'), bread(2, 'finding')];
  for (const hosted of [true, false]) {
    const route = '#/brand/quince';
    const win = loadViews(route);
    await flushBoot();
    const main = { innerHTML: '' };
    win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
    win.EW.header = () => {};
    win.EW.api = async p => {
      win.EW.current.hosted = hosted;
      if (p === '/customer/brands/quince') return BRAND;
      if (p.startsWith('/customer/saved-items?')) return { items: [], next_cursor: null };
      if (p.startsWith('/customer/search?')) return { items: reads, total: 2, total_relation: 'exact' };
      throw boom();
    };
    win.location.hash = route; win.EW.current.route = route;
    await win.EWViews.render(); await settleAll();
    const html = main.innerHTML;
    assert.doesNotMatch(html, STATUS_WORDS, `hosted=${hosted}`);
    assert.equal((html.match(/<span class="lib-read-report"><details class="report-problem"><summary>Report a mistake<\/summary>/g) || []).length, 2, `hosted=${hosted}`);
  }
});
