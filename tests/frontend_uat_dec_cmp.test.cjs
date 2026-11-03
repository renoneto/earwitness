/* Decisions round, worker cmp, replaced by PLAIN-ADS (DEP7):
   UAT-070: a Compare cell counts every ad and its reach is the reach of
   those ads ("N shows · last heard ..."); no "not confirmed" reach and no
   possible_* fields are read.
   D-1: the "Heard in N ads" line is gone: the cell is that number.
   Loader copied from frontend_uat_library.test.cjs.
   Run: node --test tests/frontend_uat_dec_cmp.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const read = f => fs.readFileSync(path.join(APP, f), 'utf8');

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
    vm.runInContext(read(file), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api, { hosted = null } = {}) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flushBoot();
  if (hosted !== null) win.EW.current.hosted = hosted;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  return { html: main.innerHTML, win };
}

const cellOf = (count, over = {}) => ({ count, ads: count, refs: count ? ['sr1:' + 'c'.repeat(32)] : [], coverage: { matched_in_scope: count },
  selector: count ? { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'confirmed_occurrences' } } : null,
  shows: 0, last_heard: null, possible_shows: 0, possible_last_heard: null, ...over });
const cmpData = (rows, over = {}) => ({ brands: rows.map(r => r[0]), period_a: null, period_b: null,
  shared_scale: Math.max(0, ...rows.map(r => r[1].count)), revision: 'r',
  cells: rows.map(([brand, a, b]) => ({ brand, a, ...(b ? { b } : {}) })), ...over });
const DIR = (extra = {}) => ({ brands: [
  { key: 'chatgpt', display: 'ChatGPT', records: 13, ...(extra.chatgpt || {}) },
  { key: 'progressive', display: 'Progressive', records: 1, ...(extra.progressive || {}) }] });
const compareApi = (data, dir = DIR()) => p => (p === '/customer/brands' ? dir : p.startsWith('/customer/compare?') ? data : new Error('unexpected ' + p));
const rowOf = (html, name) => {
  const start = html.indexOf(`<th scope="row">${name}`);
  assert.ok(start >= 0, 'row ' + name);
  return html.slice(start, html.indexOf('</tr>', start));
};

/* -- UAT-070 (PLAIN-ADS) --------------------------------------------------- */
test('UAT-070: a zero cell shows "0 ads" and no reach; possible_* fields are never read', async () => {
  const data = cmpData([
    ['chatgpt', cellOf(0, { possible_shows: 3, possible_last_heard: '2026-09-21' })],
    ['progressive', cellOf(0, { possible_shows: 1, possible_last_heard: '2026-09-02' })]]);
  const { html } = await render('#/compare?brands=chatgpt,progressive', compareApi(data));
  assert.match(html, /<strong class="ew-num">0 ads<\/strong>/);
  assert.doesNotMatch(html, /not confirmed|3 shows|Sep 21/);
  // All zero still draws no bars.
  assert.doesNotMatch(html, /lib-bar/);
});

test('UAT-070: a cell with ads shows its reach', async () => {
  const data = cmpData([
    ['chatgpt', cellOf(2, { shows: 2, last_heard: '2026-09-10', possible_shows: 5, possible_last_heard: '2026-09-21' })],
    ['progressive', cellOf(0, { possible_shows: 4, possible_last_heard: '2026-09-15' })]]);
  const { html } = await render('#/compare?brands=chatgpt,progressive', compareApi(data));
  const chat = rowOf(html, 'ChatGPT');
  assert.match(chat, /<strong class="ew-num">2 ads<\/strong>/);
  assert.match(chat, /<span class="lib-row-sub">2 shows · last heard Sep 10(, 2026)?</);
  assert.doesNotMatch(chat, /not confirmed|5 shows|Sep 21/);
  const prog = rowOf(html, 'Progressive');
  assert.match(prog, /<strong class="ew-num">0 ads<\/strong>/);
  assert.doesNotMatch(prog, /not confirmed|4 shows/);
});

test('UAT-070: date-scoped cells use each period\'s own reach; unknown last heard stays unknown', async () => {
  const data = cmpData([
    ['chatgpt', cellOf(2, { shows: 2, last_heard: '2026-09-21' }), cellOf(1, { shows: 1, last_heard: null })],
    ['progressive', cellOf(0), cellOf(0)]],
  { period_a: { from: '2026-09-01', to: '2026-09-30' }, period_b: { from: '2026-08-01', to: '2026-08-31' } });
  const { html } = await render('#/compare?brands=chatgpt,progressive&from=2026-09-01&to=2026-09-30&compareFrom=2026-08-01&compareTo=2026-08-31', compareApi(data));
  const chat = rowOf(html, 'ChatGPT');
  assert.match(chat, /2 shows · last heard Sep 21(, 2026)?/);
  assert.match(chat, /1 show</);
  assert.doesNotMatch(chat, /last heard (null|undefined|Invalid)|not confirmed/);
  // No ads in the period: no reach line at all.
  assert.doesNotMatch(rowOf(html, 'Progressive'), /show|last heard/);
});

/* -- D-1 heard line (PLAIN-ADS: gone) -------------------------------------- */
test('D-1: no heard line beside the brand; the cell is the brand\'s ads', async () => {
  const data = cmpData([['chatgpt', cellOf(0)], ['progressive', cellOf(0)]]);
  const dir = DIR({ chatgpt: { ad_breaks: 7 }, progressive: { ad_breaks: 1 } });
  const { html } = await render('#/compare?brands=chatgpt,progressive', compareApi(data, dir));
  assert.doesNotMatch(html, /Heard in|only confirmed ads are compared/);
  assert.doesNotMatch(html, /\b13\b|\d+ reads?\b|ad breaks?/, 'the read count is never shown');
});

test('D-1: without ad_breaks the heard line shows no number; dated periods hide it', async () => {
  const data = cmpData([['chatgpt', cellOf(0)], ['progressive', cellOf(0)]]);
  const all = await render('#/compare?brands=chatgpt,progressive', compareApi(data));
  assert.doesNotMatch(rowOf(all.html, 'ChatGPT'), /Heard in/);
  assert.doesNotMatch(all.html, /\b13\b|\d+ reads?\b|ad break/, 'no read count as a fallback');
  const dated = await render('#/compare?brands=chatgpt,progressive&from=2026-09-01&to=2026-09-30',
    compareApi(cmpData([['chatgpt', cellOf(0)], ['progressive', cellOf(0)]], { period_a: { from: '2026-09-01', to: '2026-09-30' } }),
      DIR({ chatgpt: { ad_breaks: 7 } })));
  assert.doesNotMatch(dated.html, /Heard in/);
});
