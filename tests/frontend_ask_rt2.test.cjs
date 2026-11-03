/* ASK-RT2: the Ask answer's server-made lines. W4 and W5 lead the answer
   (turn.head); count lines (turn.counts) follow them: W1 with its "See them in
   Search" link when the server gave a Search query, W2's rows, W1c. Every
   word is the server's.
   Run: node --test tests/frontend_ask_rt2.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'ask-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const clone = o => JSON.parse(JSON.stringify(o));

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

// A fresh sandbox per test; askAvailable set as the test needs.
function views(available) {
  const win = loadViews(canonicalSessionFetch());
  if (available !== undefined) win.EW.current.askAvailable = available;
  return win;
}
const turnHtml = turn => views(true).EWViews.askTurnHtml(turn);
const articleInner = html => /<article[^>]*>([\s\S]*)<\/article>/.exec(html)[1];
// Rendered text: tags dropped, &nbsp; as a space (chips share a span with the last word).
const plain = html => html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ');

const ANSWERED = fixture('answered');
const NOTHING = fixture('nothing');
const W4 = 'This collection runs from Aug 1, 2026 to Oct 21, 2026. Newer episodes aren’t in it yet.';
const W5 = 'We don’t know what brands spend or how many people heard an ad. We can count the ads we heard:';
const COUNTS = [
  { kind: 'brand', text: 'NordVPN has 12 ads in 10 episodes across 4 shows in this collection.', rows: [],
    link: 'See them in Search', search: { mode: 'ads', brands: ['nordvpn'], categories: ['Comedy'], from: '2026-09-01' } },
  { kind: 'brands', text: '3 brands have ads in this collection, Aug 1, 2026 to Oct 21, 2026. Ads with brand not identified are not counted.', rows: [] },
  { kind: 'ranking', text: 'Most ads in this collection, Aug 1, 2026 to Oct 21, 2026:', rows: ['Quince · 93 ads', 'A & B · 1 ad'] },
];

test('answered: head lines first, then the count lines, then based_on', () => {
  const turn = clone(ANSWERED);
  turn.head = [W4, W5];
  turn.counts = clone(COUNTS);
  const html = turnHtml(turn);
  const at = [W4, W5, COUNTS[0].text, COUNTS[1].text, COUNTS[2].text, 'Based on 3 passages'].map(s => html.indexOf(s));
  assert.ok(at.every(i => i >= 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at, 'in order');
  assert.ok(html.includes('<li>Quince · 93 ads</li>'));
  assert.ok(html.includes('<li>A &amp; B · 1 ad</li>'), 'rows are escaped');
});

test('a brand line links to Search with the same filters; others have no link', () => {
  const turn = clone(ANSWERED);
  turn.counts = clone(COUNTS);
  const html = turnHtml(turn);
  const links = html.match(/<a [^>]*>See them in Search<\/a>/g) || [];
  assert.equal(links.length, 1);
  const href = /href="([^"]*)"/.exec(links[0])[1].replace(/&amp;/g, '&');
  assert.ok(href.startsWith('#/search?'), href);
  const p = new URLSearchParams(href.split('?')[1]);
  assert.equal(p.get('mode'), 'ads');
  assert.deepEqual(p.getAll('brands'), ['nordvpn']);
  assert.deepEqual(p.getAll('categories'), ['Comedy']);
  assert.equal(p.get('from'), '2026-09-01');
  const noLink = clone(ANSWERED);
  noLink.counts = [{ kind: 'brand', text: 'On VPN Show, NordVPN has 2 ads in 1 episode.', rows: [] }];
  assert.ok(!turnHtml(noLink).includes('See them in Search'));
});

test('nothing: the head line still leads', () => {
  const turn = clone(NOTHING);
  turn.head = [W4];
  const html = turnHtml(turn);
  assert.ok(html.indexOf(W4) >= 0 && html.indexOf(W4) < html.indexOf(turn.lines[0]));
});

test('no head and no counts: the answer is unchanged', () => {
  const a = clone(ANSWERED);
  const b = clone(ANSWERED);
  b.head = [];
  b.counts = [];
  assert.equal(turnHtml(a), turnHtml(b));
});
