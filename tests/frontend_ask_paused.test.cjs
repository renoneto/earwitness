/* ASK-PAUSED (lead U3-17): at the spend stop the session says ask_paused
   true (ask_available false). The nav item "Ask" stays, the Overview Ask card
   stays in a paused state with only approved words ("Ask is paused right
   now.") and no input or start button, brand buttons stay hidden, and #/ask
   keeps its paused line. The next session with ask_available true brings
   everything back; a session with neither flag is unchanged.
   Loaders copied from frontend_ask_screens.test.cjs.
   Run: node --test tests/frontend_ask_paused.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const PAUSED_LINE = 'Ask is paused right now.';

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
function loadViews(route = '#/overview') {
  const win = loadAt(route, { fetch: canonicalSessionFetch() });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const plain = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

// One sandbox; each render adopts `session` and draws `route`. The header is
// the real one (captured), so the nav and the page come from the same render.
async function page() {
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  let nav = '';
  const header = { set innerHTML(v) { nav = v; }, get innerHTML() { return nav; } };
  const stub = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? header : stub);
  win.scrollTo = () => {};
  let api = () => undefined;
  win.EW.api = async p => { const out = api(p); if (out === undefined) throw Object.assign(new Error('not stubbed ' + p), { status: 500 }); return out; };
  return {
    win,
    async show(route, session, routeApi) {
      api = routeApi || (() => undefined);
      win.location.hash = route;
      win.EW.current.route = route;
      win.EW.current.adoptedSession = Object.assign({ role: 'editor' }, session);
      await win.EWViews.render().catch(() => {});
      for (let i = 0; i < 5; i++) await new Promise(res => setTimeout(res, 0));
      return { nav, html: main.innerHTML };
    },
  };
}
const OV_EMPTY = { coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [] };
const ovApi = overview => p => (p === '/customer/overview' ? overview
  : p.startsWith('/customer/saved-items') ? { items: [], next_cursor: null } : p.startsWith('/customer/search') ? { items: [] } : undefined);
const ovDown = p => (p === '/customer/overview' ? (() => { throw Object.assign(new Error('down'), { status: 500 }); })() : ovApi(OV_EMPTY)(p));
const BRAND_READ = ask_button => ({ brand: { brand: 'quince', key: 'quince', display: 'Quince', detections: 3, ad_breaks: 2, episodes: 3, shows: 2, last_heard: '2026-10-09',
  confirmed_occurrences: [], findings: [], offer_sequence: [], ask_button,
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['quince'], sort: 'newest', page: 1 }, mentions: { version: 2, mode: 'mentions', brands: ['quince'] } } } });
const brandApi = ask_button => p => (p === '/customer/brands/quince' ? BRAND_READ(ask_button)
  : p.startsWith('/customer/saved-items') ? { items: [], next_cursor: null }
  : p.startsWith('/customer/search') ? { items: [], total: 0, break_total: 0, total_relation: 'exact' }
  : p.startsWith('/customer/summary') ? { cells: [] } : undefined);

const PAUSED = { ask_available: false, ask_paused: true };
const OPEN = { ask_available: true };
const OFF = { ask_available: false };
const askAnchor = nav => /<a href="#\/ask"[^>]*>Ask<\/a>/.test(nav);
const card = html => (/<section class="ew-card ask-card[^"]*">[\s\S]*?<\/section>/.exec(html) || [''])[0];

test('adoption: askPaused follows ask_paused === true only, and never alongside askAvailable', async () => {
  const p = await page();
  for (const [session, paused, available] of [
    [PAUSED, true, false], [OPEN, false, true], [OFF, false, false], [{}, false, false],
    [{ ask_available: false, ask_paused: 'true' }, false, false],
    [{ ask_available: true, ask_paused: true }, false, true],
  ]) {
    await p.show('#/overview', session, ovApi(OV_EMPTY));
    assert.equal(p.win.EW.current.askPaused === true, paused, JSON.stringify(session));
    assert.equal(p.win.EW.current.askAvailable === true, available, JSON.stringify(session));
  }
});

test('paused: the nav keeps the Ask item, in the same place as when available', async () => {
  const p = await page();
  const paused = (await p.show('#/overview', PAUSED, ovApi(OV_EMPTY))).nav;
  const open = (await p.show('#/overview', OPEN, ovApi(OV_EMPTY))).nav;
  assert.ok(askAnchor(paused), 'paused nav has Ask');
  assert.equal(paused, open, 'the paused nav equals the available nav');
  assert.ok(!askAnchor((await p.show('#/overview', OFF, ovApi(OV_EMPTY))).nav), 'off: no Ask');
});

test('paused: the Overview Ask card stays, says only approved words, and offers no input or start', async () => {
  for (const api of [ovApi(OV_EMPTY), ovDown]) {
    const p = await page();
    const { html } = await p.show('#/overview', PAUSED, api);
    const c = card(html);
    assert.ok(c, 'card present');
    assert.equal(plain(c), 'Ask what the shows are saying ' + PAUSED_LINE);
    assert.doesNotMatch(c, /<(form|input|textarea|button)\b/, 'no input or button');
    assert.doesNotMatch(c, /Ask a question|Type a question/, 'no start wording');
    assert.doesNotMatch(c, /href="#\/ask/, 'no start link');
  }
});

test('the card and nav return to normal on the next session with ask_available true, and leave when neither', async () => {
  for (const api of [ovApi(OV_EMPTY), ovDown]) {
    const p = await page();
    await p.show('#/overview', PAUSED, api);
    const back = await p.show('#/overview', OPEN, api);
    assert.ok(askAnchor(back.nav));
    assert.match(card(back.html), /Ask a question/);
    assert.ok(!back.html.includes(PAUSED_LINE), 'no paused line once open');
    const gone = await p.show('#/overview', OFF, api);
    assert.ok(!askAnchor(gone.nav));
    assert.equal(card(gone.html), '');
    assert.ok(!gone.html.includes(PAUSED_LINE));
  }
});

test('paused: the brand page shows no Ask button, even with ask_button true', async () => {
  const p = await page();
  const { html } = await p.show('#/brand/quince', PAUSED, brandApi(true));
  assert.ok(html.includes('Quince'), 'brand page drawn');
  assert.doesNotMatch(html, /#\/ask\?brand=/);
  assert.ok(!html.includes(PAUSED_LINE), 'the brand page adds no paused line');
});

test('paused: #/ask keeps its paused line and no form', async () => {
  const p = await page();
  const { html, nav } = await p.show('#/ask', PAUSED);
  assert.match(html, /<p class="ask-off" role="status">Ask is paused right now\.<\/p>/);
  assert.doesNotMatch(html, /<(form|textarea)\b/);
  assert.match(nav, /<a href="#\/ask" aria-current="page">Ask<\/a>/, 'the nav marks Ask as the page');
});
