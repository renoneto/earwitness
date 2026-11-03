/* PERF review r1 (PERF-R1-02, -03, -05): transcript lines mark whole search words beside recorded codes; the frontend uses no regex lookbehind; decomposed accents never split a word.
   Run: node --test tests/frontend_judge_perf_r1.test.cjs */
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
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function renderRoute(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}

test('PERF-R1-02 transcript lines with recorded codes mark whole search words only', async () => {
  const { win } = await renderRoute('#/overview', () => ({}));
  win.EW.current.filters = { q: 'Meta' };
  const out = win.EWViews.markedText("Heavy metal, SAVE10, Meta's Muse", { promo_codes: ['SAVE10'], web_addresses: [] });
  assert.doesNotMatch(out, /<mark>meta<\/mark>l/i, 'metal is not marked');
  assert.match(out, /<mark>Meta<\/mark>/);
  assert.match(out, /<span class="ew-said-code">SAVE10<\/span>/);
});

test('PERF-R1-03 the frontend uses no regex lookbehind (older Safari)', () => {
  for (const f of ['app.js', 'views.js', 'ui.js', 'saved.js']) {
    const src = fs.readFileSync(path.join(APP, f), 'utf8');
    assert.doesNotMatch(src, /\(\?<[!=]/, f + ' has a lookbehind');
  }
  const win = loadAt('#/overview');
  assert.doesNotMatch(win.EW.wordSource('meta'), /\(\?<[!=]/);
  assert.equal(win.EW.highlight("metal Meta's", 'meta'), 'metal <mark>Meta</mark>&#39;s');
  assert.equal(win.EW.highlight('ameta meta_x meta2 xmeta meta', 'meta'), 'ameta meta_x meta2 xmeta <mark>meta</mark>');
});

test('PERF-R1-05 a decomposed accent never splits a word', () => {
  const win = loadAt('#/overview');
  const nfd = 'The nai\u0308ve plan';
  assert.doesNotMatch(win.EW.highlight(nfd, 'nai'), /<mark>/);
  assert.match(win.EW.highlight('The nai\u0308ve nai plan', 'nai'), /<mark>nai<\/mark> plan/, 'the whole word still marks');
});

test('PERF-R1-05 a mark with no composed form still belongs to its word', () => {
  const win = loadAt('#/overview');
  // i + U+0331 (macron below) stays two characters after NFC.
  assert.doesNotMatch(win.EW.highlight('the nai̱ plan', 'nai'), /<mark>/);
});


/* PERF review r2 (PERF-R2-01..03). */
test('PERF-R2-01 an astral letter before a word joins it; a word starting with one keeps its leading edge', () => {
  const win = loadAt('#/overview');
  assert.equal(win.EW.highlight('\u{20000}meta and meta', 'meta'), '\u{20000}meta and <mark>meta</mark>');
  assert.equal(win.EW.highlight('x\u{20000}ab \u{20000}ab', '\u{20000}ab'), 'x\u{20000}ab <mark>\u{20000}ab</mark>');
});

test('PERF-R2-02 punctuation-led words, longest match first, NFC transcript text', () => {
  const win = loadAt('#/overview');
  assert.equal(win.EW.highlight('a$5off', '$5off'), 'a<mark>$5off</mark>');
  assert.match(win.EW.highlight('a free trial today', 'free "free trial"'), /<mark>free trial<\/mark>/);
  assert.equal(win.EW.saysWord('The naïve plan', 'naïve'), true);
  const code = win.EW.matchRanges('Use CAFÉ10 now', [{ text: 'CAFÉ10', cls: 'ew-said-code' }]);
  assert.equal(code.ranges.length, 1, 'the composed code marks decomposed text');
  // After a rejected word, the next match still starts one character on.
  assert.match(win.EW.highlight('xab-c', 'ab-c -c'), /<mark>-c<\/mark>/);
});

test('PERF-R2-03 a rejected word still lets a shorter code at the same spot mark', () => {
  const win = loadAt('#/overview');
  const r = win.EW.matchRanges('xSAVE10off', [{ text: 'SAVE10', cls: 'ew-said-code' }, { text: 'save10off', whole: true }]);
  // JSON: arrays from the app's vm context fail a cross-realm deepEqual.
  assert.equal(JSON.stringify(r.ranges.map(x => [x.start, x.end, x.needle.text])), '[[1,7,"SAVE10"]]');
});

test('PERF-R2-01 a word ending in an astral letter keeps its trailing edge', () => {
  const win = loadAt('#/overview');
  assert.equal(win.EW.highlight('ab\u{20000}c ab\u{20000}', 'ab\u{20000}'), 'ab\u{20000}c <mark>ab\u{20000}</mark>');
});
