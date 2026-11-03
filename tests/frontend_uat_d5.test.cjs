/* d16 d5: decisions 5, 6 and 11. Show-only alerts watch new episodes;
   All mentions with a brand and no words says what it lists; All
   mentions with no words and no brand shows a prompt and sends no
   research request; default alert names are at most 60 characters.
   Loader copied from frontend_uat_dec_d1ui.test.cjs.
   Run: node --test tests/frontend_uat_d5.test.cjs */
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
    console, URLSearchParams, AbortController, clearTimeout, URL,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route, href: 'http://127.0.0.1:9172/app/' + route },
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
async function render(route, api, setup) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  if (setup) setup(win);
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return { html: main.innerHTML, win };
}

const q = (vals, initial, touched) => loadViewsOnly().EWViews.alertScopeFromDraft(vals, JSON.stringify(initial), 'null', JSON.stringify(touched)).query;
let VIEWS = null;
function loadViewsOnly() {
  if (VIEWS) return VIEWS;
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  VIEWS = win; return win;
}
const base = { version: 2, mode: 'mentions', q: '', brands: null };

/* -- decision 5: show-only alerts watch new episodes -- */
test('D5 a draft with a show and no words saves the new-episodes alert', () => {
  assert.equal(q({ mode: 'mentions', q: '', show: 'cs-1' }, base, ['show']).mode, 'episodes');
  assert.equal(JSON.stringify(q({ mode: 'mentions', q: '', show: 'cs-1' }, base, ['show']).shows), '["cs-1"]');
});
test('D5 a brand with no words still goes to Ads, with or without a show', () => {
  assert.equal(q({ mode: 'mentions', q: '', brand: 'shopify' }, base, ['brand']).mode, 'ads');
  assert.equal(q({ mode: 'mentions', q: '', brand: 'shopify', show: 'cs-1' }, base, ['brand', 'show']).mode, 'ads');
});
test('D5 a show with words stays All mentions; a Look in the user picked wins; a stored alert is not rewritten', () => {
  assert.equal(q({ mode: 'mentions', q: 'promo', show: 'cs-1' }, base, ['show', 'q']).mode, 'mentions');
  assert.equal(q({ mode: 'mentions', q: '', show: 'cs-1' }, base, ['show', 'mode']).mode, 'mentions');
  assert.equal(q({ mode: 'mentions', q: '', show: 'cs-1' }, { ...base, shows: ['cs-1'] }, ['name']).mode, 'mentions');
  assert.equal(q({ mode: 'ads', q: '', show: 'cs-1' }, { ...base, mode: 'ads' }, ['show']).mode, 'ads');
});
test('D5 clearing the words on a show draft goes back to new episodes', () => {
  assert.equal(q({ mode: 'mentions', q: '', show: 'cs-1' }, { ...base, shows: ['cs-1'] }, ['q']).mode, 'episodes');
});
test('D5 evidence preset: a show with no brand or words opens new episodes, named for the show', () => {
  const p = loadViewsOnly().EWViews.evidenceAlertPreset;
  const ad = p({ show: 'cs-upfirst', showName: 'Up First' });
  assert.equal(JSON.stringify(ad.query), JSON.stringify({ version: 2, mode: 'episodes', shows: ['cs-upfirst'] }));
  assert.equal(ad.name, 'Up First new episodes');
  const ps = p({ kind: 'passage', show: 'cs-upfirst', showName: 'Up First' });
  assert.equal(ps.query.mode, 'episodes');
  assert.equal(p({ brand: 'shopify', brandName: 'Shopify', show: 'cs-1' }).query.mode, 'ads', 'a brand still goes to Ads');
  assert.equal(p({ brand: 'shopify', brandName: 'Shopify' }).name, 'Shopify ads');
  assert.equal(p({ kind: 'passage', q: 'free trial', show: 'cs-1' }).query.mode, 'mentions');
});
test('D5 the show page action opens the dialog on Episodes with the show selected', async () => {
  const { win } = await render('#/overview', () => new Error('x'));
  const focusable = { focus() {} };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: sel => (sel === '#alert-form' ? null : focusable), querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.api = async p => {
    if (p === '/customer/shows') return { shows: [{ id: 'cs-1', display_title: 'Casefile' }] };
    if (p === '/customer/brands') return { brands: [] };
    throw new Error('unexpected ' + p);
  };
  await win.EWViews.handleAction({ dataset: { action: 'alert-show', id: 'cs-1', name: 'Casefile' } });
  for (let i = 0; i < 20; i++) await flushBoot();
  assert.match(modal.innerHTML, /<option value="episodes" selected>Episodes<\/option>/);
  assert.match(modal.innerHTML, /<option value="cs-1" selected>/);
  assert.match(modal.innerHTML, /id="alert-name" name="name" required maxlength="120" value="Casefile"/);
  assert.match(modal.innerHTML, /id="alert-scope"[^>]*>This alert watches: Episodes · show Casefile\./);
});
test('D5 Search All mentions with a brand and no words says what it lists', async () => {
  const bundleFor = query => ({ query: { version: 2, sort: 'newest', page: 1, ...query }, revision: 'r', selector: null, coverage: {},
    result: { items: [], total: 0, dated_total: 0, undated_total: 0, total_relation: 'exact', per_page: 10 }, summary: { week: [] } });
  const api = bundle => p => (p === '/customer/research-query' ? bundle : p === '/customer/brands' ? { brands: [{ key: 'shopify', display: 'Shopify' }] } : p === '/customer/shows' ? { shows: [] } : new Error('unexpected ' + p));
  const { html } = await render('#/search?mode=mentions&brands=shopify', api(bundleFor({ mode: 'mentions', q: '', brands: ['shopify'] })));
  assert.match(html, /<p class="ew-match-mode">Passages from episodes where Shopify was heard in an ad\.<\/p>/);
  const withWords = await render('#/search?mode=mentions&brands=shopify&q=promo', api(bundleFor({ mode: 'mentions', q: 'promo', brands: ['shopify'] })));
  assert.doesNotMatch(withWords.html, /was heard in an ad/);
  const ads = await render('#/search?mode=ads&brands=shopify', api(bundleFor({ mode: 'ads', q: '', brands: ['shopify'] })));
  assert.doesNotMatch(ads.html, /was heard in an ad/);
});

/* -- decision 6: no words and no brand shows a prompt, no request -- */
test('D6 All mentions with no words and no brand shows the prompt, sends no research request, renders no cards', async () => {
  const calls = [];
  const api = p => { calls.push(p); return p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] } : new Error('unexpected ' + p); };
  const { html } = await render('#/search?mode=mentions', api);
  assert.ok(!calls.includes('/customer/research-query'), JSON.stringify(calls));
  assert.match(html, /Type words to search all transcripts/);
  assert.doesNotMatch(html, /ew-result-list|ew-rows|data-action="inspect"|No mentions found|Showing \d/);
  assert.match(html, /aria-pressed="true">All mentions/, 'the tabs stay');
  assert.match(html, /name="q"/, 'the search box stays');
});
test('D6 with words, or with a brand, the search runs as before', async () => {
  const calls = [];
  const bundle = { query: { version: 2, mode: 'mentions', q: 'x', sort: 'newest', page: 1 }, revision: 'r', selector: null, coverage: {},
    result: { items: [{ ref: 'tx:1', record_kind: 'transcript_passage', text: 'x', episode_id: 'e1', show_title: 'S', pub_date: '2026-10-09' }], total: 1, dated_total: 1, undated_total: 0, total_relation: 'exact', per_page: 10 }, summary: { week: [] } };
  const api = p => { calls.push(p); return p === '/customer/research-query' ? bundle : p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] } : new Error('unexpected ' + p); };
  const a = await render('#/search?mode=mentions&q=x', api);
  assert.match(a.html, /Showing 1 to 1 of 1 mention/);
  assert.doesNotMatch(a.html, /Type words to search all transcripts/);
  assert.ok(calls.includes('/customer/research-query'));
  calls.length = 0;
  await render('#/search?mode=mentions&brands=shopify', api);
  assert.ok(calls.includes('/customer/research-query'), 'a brand with no words still searches');
  calls.length = 0;
  await render('#/search?mode=ads', api);
  assert.ok(calls.includes('/customer/research-query'), 'Ads with no words still lists');
});

/* -- decision 11: default alert names are at most 60 characters -- */
test('D11 defaultAlertName: short names unchanged; a 59-character brand plus suffix; one 80-character word; exactly 60', () => {
  const V = loadViewsOnly().EWViews;
  const name = V.defaultAlertName, SUF = ' ads';
  assert.equal(name('Shopify', SUF), 'Shopify ads');
  assert.equal(name('Shopify'), 'Shopify');
  const words = 'Acme Holdings International Group of Wonderful Companies Limited Co';
  assert.equal(words.length >= 59, true);
  const b59 = (words + ' ' + words).slice(0, 59);
  assert.equal(b59.length, 59);
  const out = name(b59, SUF);
  assert.ok(out.length <= 60, `${out.length} ${out}`);
  assert.ok(out.endsWith('…' + SUF), out);
  const cut = out.slice(0, -(SUF.length + 1));
  assert.ok(b59.startsWith(cut), 'the cut is a prefix of the brand');
  assert.ok(/\s/.test(b59[cut.length]) || b59[cut.length] === undefined, 'cut at a word boundary: ' + JSON.stringify(cut));
  const one = 'a'.repeat(80);
  const hard = name(one, SUF);
  assert.equal(hard.length, 60);
  assert.equal(hard, 'a'.repeat(60 - SUF.length - 1) + '…' + SUF);
  assert.equal(name(one), 'a'.repeat(59) + '…');
  const exact = 'b'.repeat(60 - SUF.length) + SUF;
  assert.equal(exact.length, 60);
  assert.equal(name('b'.repeat(60 - SUF.length), SUF), exact, 'exactly 60 is unchanged');
  assert.equal(name('c'.repeat(61 - SUF.length), SUF).length, 60, 'one over is cut to 60');
  assert.equal(name('d'.repeat(60)), 'd'.repeat(60));
  assert.equal(name('d'.repeat(61)).length, 60);
});
test('D11 the evidence presets and the brand and show actions use the cap', async () => {
  const V = loadViewsOnly().EWViews;
  const long = 'Very Long Brand Name Incorporated International Holdings Group';
  const p = V.evidenceAlertPreset({ brand: 'k', brandName: long });
  assert.ok(p.name.length <= 60 && p.name.endsWith('… ads'), p.name);
  const s = V.evidenceAlertPreset({ show: 'x', showName: long + ' ' + long });
  assert.ok(s.name.length <= 60 && s.name.endsWith('… new episodes'), s.name);
  const m = V.evidenceAlertPreset({ kind: 'passage', q: 'a '.repeat(50).trim() });
  assert.ok(m.name.length <= 60 && m.name.endsWith(' mentions'), m.name);
});

/* -- review 1: F4 brand line grammar, F5 closing quote -- */
test('F4 the brand line joins two or more brands with "or"', async () => {
  const bundleFor = query => ({ query: { version: 2, sort: 'newest', page: 1, ...query }, revision: 'r', selector: null, coverage: {},
    result: { items: [], total: 0, dated_total: 0, undated_total: 0, total_relation: 'exact', per_page: 10 }, summary: { week: [] } });
  const api = bundle => p => (p === '/customer/research-query' ? bundle : p === '/customer/brands' ? { brands: [{ key: 'shopify', display: 'Shopify' }, { key: 'squarespace', display: 'Squarespace' }, { key: 'wix', display: 'Wix' }] } : p === '/customer/shows' ? { shows: [] } : new Error('unexpected ' + p));
  const two = await render('#/search?mode=mentions&brands=shopify,squarespace', api(bundleFor({ mode: 'mentions', q: '', brands: ['shopify', 'squarespace'] })));
  assert.match(two.html, /where Shopify or Squarespace was heard in an ad\./);
  const three = await render('#/search?mode=mentions&brands=shopify,squarespace,wix', api(bundleFor({ mode: 'mentions', q: '', brands: ['shopify', 'squarespace', 'wix'] })));
  assert.match(three.html, /where Shopify or Squarespace or Wix was heard in an ad\./);
});
test('F5 a long quoted phrase is shortened inside the quotes, so both quotes stay', () => {
  const V = loadViewsOnly().EWViews;
  const m = V.evidenceAlertPreset({ kind: 'passage', q: 'a '.repeat(50).trim() });
  assert.ok(m.name.length <= 60, m.name);
  assert.ok(m.name.startsWith('“') && m.name.endsWith('” mentions'), m.name);
  assert.ok(m.name.includes('…”'), m.name);
  const hard = V.evidenceAlertPreset({ kind: 'passage', q: 'x'.repeat(100) });
  assert.ok(hard.name.length <= 60 && hard.name.endsWith('…” mentions'), hard.name);
  assert.equal(V.evidenceAlertPreset({ kind: 'passage', q: 'free trial' }).name, '“free trial” mentions');
});

/* -- UAT-013: the All mentions prompt keeps the saved-settings note -- */
test('UAT-013 a bare Search opened on saved All mentions shows the prompt and the saved-settings note', async () => {
  const calls = [];
  const api = p => { calls.push(p); return p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] } : new Error('unexpected ' + p); };
  const route = '#/search?mode=mentions&view=list&from=2026-09-16';
  const on = await render(route, api, win => { win.EW.current.searchPrefNote = route; });
  assert.ok(!calls.includes('/customer/research-query'), JSON.stringify(calls));
  assert.match(on.html, /Type words to search all transcripts/);
  assert.match(on.html, /<p class="ew-pref-note" role="status">Opened with your saved Search settings \(All mentions · List · since [A-Z][a-z]{2} \d{1,2}\)\. <a class="text-link" href="#\/settings">Change in Settings<\/a>/);
  assert.match(on.html, /data-action="dismiss-pref-note"/);
  assert.equal((on.html.match(/ew-pref-note/g) || []).length, 1, 'once');
  const typed = await render('#/search?mode=mentions', api);
  assert.doesNotMatch(typed.html, /ew-pref-note/, 'a typed route is canonical: no note');
});
