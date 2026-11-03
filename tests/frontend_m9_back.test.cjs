/* M9-BACK (lead LEDGER-LEAD3-PERF-1 item 2; UX-M9 Back part, G2-10, G1-02):
   Back to Overview, Search or Alerts shows the last page this tab rendered
   there at once, scrolled where it was, and reads it again behind it; the
   answer replaces only what changed. A page the tab has not shown loads as
   before.
   Rules (brief): the kept page is per tab, workspace, signed-in person and
   role; sign-out, a workspace switch, a role change and the person's own
   write drop it; it lives in memory only; a failed refresh keeps the page
   with the page's own failure notice, never a blank page; a refusal still
   refuses; another person never sees it.
   Only fetch, DOM, history, storage and location are simulated (the pass 24
   browser model plus a small DOM for the patch); the real EW.api, apiCall,
   boot, go, render, handleAction and the page builders run.
   Run: node --test tests/frontend_m9_back.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const OWNER = 'oidc:https://i.example:owner';
const OTHER = 'oidc:https://i.example:other';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws', 'ws-b'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));

// ============================================================================
// A small DOM: enough HTML parsing, serializing and node methods for the
// page shell and the patch (childNodes, attributes, isEqualNode, insert,
// remove, replace). Every node removed from a parent is counted.
// ============================================================================
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
class DNode {
  constructor(type, name, value = null) {
    this.nodeType = type; this.nodeName = name; this.nodeValue = value;
    this.childNodes = []; this.attributes = []; this.parentNode = null; this.dataset = {};
    this.style = {}; this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  get tagName() { return this.nodeName; }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return !!n.isRoot; }
  getAttribute(n) { const a = this.attributes.find(x => x.name === n); return a ? a.value : null; }
  hasAttribute(n) { return this.attributes.some(x => x.name === n); }
  setAttribute(n, v) { const a = this.attributes.find(x => x.name === n); if (a) a.value = String(v); else this.attributes.push({ name: n, value: String(v) }); }
  removeAttribute(n) { this.attributes = this.attributes.filter(x => x.name !== n); }
  get id() { return this.getAttribute('id') || ''; }
  isEqualNode(o) {
    if (!o || o.nodeType !== this.nodeType || o.nodeName !== this.nodeName || o.nodeValue !== this.nodeValue) return false;
    if (o.attributes.length !== this.attributes.length) return false;
    for (const a of this.attributes) if (o.getAttribute(a.name) !== a.value) return false;
    if (o.childNodes.length !== this.childNodes.length) return false;
    return this.childNodes.every((c, i) => c.isEqualNode(o.childNodes[i]));
  }
  detach(n) { const i = this.childNodes.indexOf(n); if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null; DOC.removed += 1; } }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    if (n.nodeType === 11) { for (const c of [...n.childNodes]) this.insertBefore(c, ref); return n; }
    if (n.parentNode) { const p = n.parentNode; const i = p.childNodes.indexOf(n); p.childNodes.splice(i, 1); }
    const at = ref ? this.childNodes.indexOf(ref) : -1;
    if (at < 0) this.childNodes.push(n); else this.childNodes.splice(at, 0, n);
    n.parentNode = this;
    return n;
  }
  removeChild(n) { this.detach(n); return n; }
  replaceChild(n, old) { this.insertBefore(n, old); this.detach(old); return old; }
  get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map(c => c.textContent).join(''); }
  set textContent(v) { for (const c of [...this.childNodes]) this.detach(c); this.appendChild(new DNode(3, '#text', String(v))); }
  get outerHTML() { return serialize(this); }
  get innerHTML() { return this.childNodes.map(serialize).join(''); }
  set innerHTML(html) {
    for (const c of [...this.childNodes]) this.detach(c);
    for (const c of parse(html)) this.appendChild(c);
    if (this.onPaint) this.onPaint(html);
  }
  insertAdjacentHTML(pos, html) { const kids = parse(html); const ref = pos === 'afterbegin' ? this.childNodes[0] || null : null; for (const c of kids) this.insertBefore(c, ref); }
  all() { const out = []; const walk = n => { for (const c of n.childNodes) if (c.nodeType === 1) { out.push(c); walk(c); } }; walk(this); return out; }
  querySelectorAll(sel) { return this.all().filter(n => matches(n, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode; } return null; }
  focus() { DOC.activeElement = this; }
  addEventListener() {}
  scrollIntoView() {}
}
// Simple selectors only: tag, #id, .class, [attr], [attr="v"] and their
// compounds; anything else matches nothing.
function matches(n, sel) {
  return sel.split(',').some(s => {
    s = s.trim();
    if (/[\s>+~:]/.test(s.replace(/"[^"]*"/g, ''))) return false;
    const parts = s.match(/#[\w-]+|\.[\w-]+|\[[^\]]+\]|^[a-z0-9-]+/gi) || [];
    if (!parts.length || parts.join('') !== s) return false;
    return parts.every(p => {
      if (!p) return true;
      if (p[0] === '#') return n.getAttribute('id') === p.slice(1);
      if (p[0] === '.') return (n.getAttribute('class') || '').split(/\s+/).includes(p.slice(1));
      if (p[0] === '[') { const m = p.match(/^\[([\w-]+)(?:([\^]?=)"?([^"\]]*)"?)?\]$/); if (!m) return false; const v = n.getAttribute(m[1]); return m[2] === '=' ? v === m[3] : m[2] === '^=' ? v !== null && v.startsWith(m[3]) : v !== null; }
      return n.nodeName.toLowerCase() === p.toLowerCase();
    });
  });
}
function parse(html) {
  const root = new DNode(11, '#fragment');
  let cur = root;
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9-]+)\s*>|<([a-zA-Z0-9-]+)((?:\s+[^\s=>/"']+(?:="[^"]*"|='[^']*'|=[^\s>]+)?)*)\s*(\/?)>|([^<]+|<)/g;
  let m;
  while ((m = re.exec(String(html)))) {
    if (m[0].startsWith('<!--')) cur.appendChild(new DNode(8, '#comment', m[0].slice(4, -3)));
    else if (m[1]) { let n = cur; while (n && n.nodeName !== m[1].toUpperCase()) n = n.parentNode; if (n && n !== root) cur = n.parentNode; }
    else if (m[2]) {
      const el = new DNode(1, m[2].toUpperCase());
      for (const a of (m[3] || '').matchAll(/([^\s=>/"']+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g)) el.setAttribute(a[1], a[2] ?? a[3] ?? a[4] ?? '');
      cur.appendChild(el);
      if (!m[4] && !VOID.has(m[2].toLowerCase())) cur = el;
    } else cur.appendChild(new DNode(3, '#text', m[5]));
  }
  return [...root.childNodes];
}
function serialize(n) {
  if (n.nodeType === 3) return n.nodeValue;
  if (n.nodeType === 8) return `<!--${n.nodeValue}-->`;
  if (n.nodeType === 11) return n.childNodes.map(serialize).join('');
  const tag = n.nodeName.toLowerCase();
  const attrs = n.attributes.map(a => ` ${a.name}="${a.value}"`).join('');
  return VOID.has(tag) ? `<${tag}${attrs}>` : `<${tag}${attrs}>${n.childNodes.map(serialize).join('')}</${tag}>`;
}
const DOC = { removed: 0, activeElement: null };
function domMain() {
  const main = new DNode(1, 'MAIN');
  main.isRoot = true;
  main.painted = [];
  main.onPaint = h => main.painted.push(h);
  main.shown = () => main.innerHTML;
  return main;
}
function template() {
  const t = new DNode(1, 'TEMPLATE');
  t.content = new DNode(11, '#fragment');
  Object.defineProperty(t, 'innerHTML', { set: h => { t.content.childNodes = []; for (const c of parse(h)) t.content.appendChild(c); }, get: () => serialize(t.content) });
  return t;
}

// ============================================================================
// Fixture (pass 24's), with the three kept pages' reads
// ============================================================================
function loadPages(route, fetch, before) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null },
    scrollY: 0, scrolls: [], fetch, CSS: { escape: s => String(s) },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.scrollTo = (x, y) => { sandbox.scrolls.push(y); sandbox.scrollY = y; };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  if (before) before(sandbox);
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  sandbox.listeners = listeners;
  sandbox.docListeners = docListeners;
  return sandbox;
}
// The workspace's data; tests change it to make a refresh differ.
const world = () => ({
  alerts: [
    { id: 4, name: 'New Quince ads', state: 'active_local', lifecycle: 'active_local', unread_ad_count: 2, unread_event_count: 2, query: { version: 2, mode: 'ads', q: 'quince' } },
    { id: 1, name: 'New ChatGPT ads', state: 'active_local', lifecycle: 'active_local', unread_ad_count: 0, unread_event_count: 0, query: { version: 2, mode: 'ads', q: 'chatgpt' } },
  ],
  marker: 'OWNER-KEPT-MARKER',
});
function server(w, session) {
  const state = { calls: [], answers: {}, session: { ...session }, signedOut: false, w };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  const data = (p, init = {}) => {
    const method = init.method || 'GET';
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [{ key: 'quince', id: 'quince', display_name: 'Quince', ads: 93 }] };
    if (p === '/customer/alerts') return method !== 'GET' ? { alert: { id: 9 } } : { alerts: state.w.alerts };
    if (p === '/customer/overview') {
      return { coverage: {}, recent_events: [], tracked_campaigns: [], activity: [], offer_sequence: [],
        alert_totals: state.w.alerts.map(a => ({ alert_id: a.id, alert_name: `${a.name} ${state.w.marker}`, lifecycle: 'active_local', ads: 3, unread_ads: a.unread_ad_count, query: a.query })) };
    }
    if (p.startsWith('/customer/saved-items')) return { items: [{ id: 1, target: { kind: 'brand', id: 'quince' } }] };
    if (p.startsWith('/customer/search?')) return { items: [], total: 0 };
    if (p.startsWith('/customer/filter-values')) return { items: [] };
    if (p.startsWith('/customer/summary')) return { groups: [] };
    if (p === '/customer/preferences') return { preferences: {} };
    if (p === '/customer/research-query') {
      const body = init.body ? JSON.parse(init.body) : {};
      return { query: body.query, revision: 'r1', coverage: {}, summary: { week: [] },
        result: { items: [], total: 0, per_page: 10, total_relation: 'exact' } };
    }
    return undefined;
  };
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const method = init.method || 'GET';
    state.calls.push({ method, path: p });
    if (p === '/session') return state.signedOut ? reply(401, { ok: false, error: 'signin', signin: { available: true } }) : reply(200, clone(state.session));
    const fixed = Object.entries(state.answers).find(([k, a]) => p.startsWith(k) && (!a.method || a.method === method));
    if (fixed) return reply(fixed[1].status, { ok: false, error: { code: fixed[1].code || 'FORBIDDEN', message: fixed[1].message || 'refused', ...(fixed[1].lost ? { access: 'lost' } : {}) } });
    const body = data(p, init);
    if (body === undefined) return reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
    const copy = clone(body);
    if (copy && typeof copy === 'object' && !('workspace_context' in copy)) copy.workspace_context = state.session.workspace_context;
    return reply(200, copy);
  };
  return state;
}
function stub(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false, ...props };
}
// #main as a string that remembers every full paint (pass 24's).
function stringMain() {
  let html = '';
  const main = stub({ painted: [] });
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = v; main.painted.push(v); } });
  main.shown = () => html;
  return main;
}
// A browser history (pass 24's): pushState/replaceState update the URL;
// Back fires popstate, then hashchange, synchronously.
function browserModel(url) {
  const B = { entries: [{ url, state: null }], i: 0, win: null };
  B.cur = () => B.entries[B.i];
  B.bind = win => {
    B.win = win;
    Object.defineProperty(win.location, 'hash', { configurable: true, get: () => B.cur().url,
      set: v => { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: v, state: null }); B.i++; } });
    win.history = {
      get state() { return clone(B.cur().state); },
      pushState(st, ti, u) { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: u === undefined ? B.cur().url : u, state: clone(st) }); B.i++; },
      replaceState(st, ti, u) { B.entries[B.i] = { url: u === undefined ? B.cur().url : u, state: clone(st) }; },
    };
  };
  // Back, without waiting: the page it paints synchronously is checked first.
  B.back = () => {
    const old = B.cur().url;
    B.i -= 1;
    for (const fn of B.win.listeners.popstate || []) fn({ state: clone(B.cur().state) });
    if (B.cur().url !== old) for (const fn of B.win.listeners.hashchange || []) fn({});
  };
  return B;
}
async function page(route, { dom = false, w = world(), session = HOSTED } = {}) {
  const srv = server(w, session);
  const B = browserModel(route);
  const win = loadPages(route, srv.fetch, sb => B.bind(sb));
  for (let i = 0; i < 5; i++) await tick();
  const main = dom ? domMain() : stringMain();
  const modal = stub({ open: false, showModal() { this.open = true; }, close() { this.open = false; } });
  const toastEl = stub();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#toast' ? toastEl : (dom && main.querySelector(sel)) || stub());
  win.document.querySelectorAll = sel => (dom && !/form/.test(sel) ? main.querySelectorAll(sel) : []);
  win.document.getElementById = id => (dom ? main.querySelector('#' + id) : null);
  if (dom) {
    win.document.createElement = tag => (tag === 'template' ? template() : new DNode(1, String(tag).toUpperCase()));
    Object.defineProperty(win.document, 'activeElement', { configurable: true, get: () => DOC.activeElement });
  }
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  const E = win.EW;
  E.toast = () => {};
  await win.EWViews.render();
  await drain(40);
  return { srv, win, E, main, B };
}
// Every customer read is held until released (session reads pass).
function holdReads(win) {
  const inner = win.fetch;
  const held = [];
  win.fetch = (url, opts = {}) => {
    if (!/^\/api\/v1\/customer\//.test(url) || (opts.method && opts.method !== 'GET' && !/research-query/.test(url))) return inner(url, opts);
    let open;
    const gate = new Promise(res => { open = res; });
    held.push(open);
    return gate.then(() => inner(url, opts));
  };
  return { count: () => held.length, release: () => { win.fetch = inner; for (const open of held.splice(0)) open(); } };
}
async function visit(t, route, scrollY = 0) {
  t.win.scrollY = scrollY;
  t.E.go(route);
  await drain(60);
}
const OVERVIEW_SHOWN = /OWNER-KEPT-MARKER/;
const LOADING = /class="loading"|Opening Overview|Opening alerts|class="skeleton/;

// ============================================================================
// Back paints the kept page at once, with its scroll
// ============================================================================
for (const [label, start, other, shown] of [
  ['Overview', '#/overview', '#/alerts', OVERVIEW_SHOWN],
  ['Alerts', '#/alerts', '#/overview', /ew-work-alert/],
  ['Search', '#/search?mode=ads&q=quincekept', '#/alerts', /quincekept/],
]) {
  test(`M9 ${label}: browser Back paints the kept page synchronously, scrolled where it was, then refreshes behind it`, async () => {
    const t = await page(start);
    assert.match(t.main.shown(), shown, `fixture: ${label} rendered`);
    await visit(t, other, 640);
    assert.doesNotMatch(t.main.shown(), shown, `fixture: ${other} is on screen`);
    const reads = holdReads(t.win);
    const painted = t.main.painted.length;
    t.win.scrolls.length = 0;
    t.B.back();
    // Synchronously, before any read answers:
    assert.match(t.main.shown(), shown, `Back to ${label} did not paint the kept page at once`);
    assert.doesNotMatch(t.main.shown(), LOADING, 'a loading block was painted');
    assert.equal(t.win.scrolls.at(-1), 640, 'the scroll was not restored with the kept page');
    await drain(10);
    assert.ok(reads.count() >= 1, 'the route reads again behind the kept page');
    reads.release();
    await drain(60);
    assert.match(t.main.shown(), shown, 'the refreshed page');
    assert.equal(t.main.painted.slice(painted).some(h => LOADING.test(h)), false, 'a loading block flashed during the refresh');
    assert.equal(t.win.scrolls.filter(y => y !== 640).length, 0, 'the refresh moved the scroll');
  });
}

test('M9 the nav and a Back link (a new entry, same route) paint the kept page at once too', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts', 300);
  const reads = holdReads(t.win);
  t.E.go('#/overview');
  assert.match(t.main.shown(), OVERVIEW_SHOWN, 'the nav to Overview did not paint the kept page at once');
  assert.equal(t.win.scrolls.at(-1), 300);
  reads.release();
  await drain(60);
});

test('M9 a page this tab has not shown loads as before', async () => {
  const t = await page('#/alerts');
  const reads = holdReads(t.win);
  t.E.go('#/overview');
  assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN);
  assert.match(t.main.shown(), /Opening Overview/, 'an unseen page shows its loading state');
  reads.release();
  await drain(60);
  assert.match(t.main.shown(), OVERVIEW_SHOWN);
});

test('M9 kept pages live in memory only: nothing of them is written to storage', async () => {
  const writes = [];
  const t = await page('#/overview');
  t.win.sessionStorage = { getItem: () => null, setItem: (k, v) => writes.push(String(v)), removeItem() {} };
  t.win.localStorage = t.win.sessionStorage;
  await visit(t, '#/alerts');
  t.B.back();
  await drain(60);
  assert.equal(writes.some(v => /OWNER-KEPT-MARKER|New Quince ads/.test(v)), false, 'page data went to storage');
  const state = JSON.stringify(t.B.entries.map(e => e.state));
  assert.doesNotMatch(state, /OWNER-KEPT-MARKER|New Quince ads/, 'page data went into history state');
});

// ============================================================================
// The refresh replaces only what changed
// ============================================================================
test('M9 the background refresh replaces only the changed nodes; the focused control and the other rows stay the same nodes', async () => {
  const t = await page('#/alerts', { dom: true });
  const chat = () => t.main.querySelectorAll('a[href="#/alert/1"]')[0];
  const quince = () => t.main.querySelectorAll('a[href="#/alert/4"]')[0];
  assert.ok(chat() && quince(), 'fixture: both alert rows rendered');
  await visit(t, '#/overview', 200);
  t.srv.w.alerts = t.srv.w.alerts.map(a => (a.id === 4 ? { ...a, unread_ad_count: 5, unread_event_count: 5 } : a));
  const reads = holdReads(t.win);
  t.B.back();
  const keptChat = chat(), keptQuince = quince();
  assert.ok(keptChat, 'the kept Alerts page is on screen at once');
  keptChat.focus();
  const before = t.main.innerHTML;
  DOC.removed = 0;
  const paints = t.main.painted.length;
  reads.release();
  await drain(60);
  assert.notEqual(t.main.innerHTML, before, 'fixture: the refresh changed the page');
  assert.equal(t.main.painted.length, paints, 'the refresh repainted the whole page');
  assert.equal(chat(), keptChat, 'an unchanged row was replaced');
  assert.equal(quince(), keptQuince, 'the changed row\'s link was replaced instead of updated');
  assert.equal(DOC.activeElement, keptChat, 'focus moved');
  assert.ok(keptChat.isConnected, 'the focused control left the page');
  assert.ok(DOC.removed < 10, `the refresh removed ${DOC.removed} nodes`);
});

test('M9 the refreshed page equals a fresh render of the same data', async () => {
  const t = await page('#/alerts', { dom: true });
  await visit(t, '#/overview');
  t.srv.w.alerts = [...t.srv.w.alerts, { id: 7, name: 'New Shopify ads', state: 'active_local', lifecycle: 'active_local', unread_ad_count: 1, unread_event_count: 1, query: { version: 2, mode: 'ads', q: 'shopify' } }];
  t.B.back();
  await drain(60);
  const patched = t.main.innerHTML;
  const fresh = await page('#/alerts', { dom: true, w: t.srv.w });
  assert.equal(patched, fresh.main.innerHTML, 'the patched page differs from a fresh render');
  assert.match(patched, /New Shopify ads/);
});

// ============================================================================
// A failed refresh keeps the page; a refusal refuses
// ============================================================================
test('M9 a failed refresh (500) keeps the kept page, with the page\'s own failure notice, never blank', async () => {
  const t = await page('#/alerts');
  await visit(t, '#/overview');
  t.srv.answers['/customer/alerts'] = { status: 500, code: 'INTERNAL', message: 'internal error' };
  t.B.back();
  await drain(60);
  assert.match(t.main.shown(), /ew-work-alert/, 'the failed refresh dropped the kept page');
  assert.match(t.main.shown(), /Alerts are unavailable/, 'the failure is not said');
});

for (const lost of [true, false]) {
  test(`M9 a refusal on the refresh (403${lost ? ', access lost' : ''}) removes the kept page and refuses`, async () => {
    const t = await page('#/alerts');
    await visit(t, '#/overview');
    t.srv.answers['/customer/alerts'] = { status: 403, message: 'You no longer have access to this workspace.', lost };
    t.B.back();
    await drain(80);
    assert.doesNotMatch(t.main.shown(), /ew-work-alert/, 'kept content stayed up under a refusal');
    assert.match(t.main.shown(), /unavailable|no longer have access/i);
    assert.equal(t.E.keptPages.has('#/alerts'), false, 'the refused page is still kept');
  });
}

// ============================================================================
// Sign-out, workspace switch, role change, own write, another person
// ============================================================================
async function backPaintsNothingKept(t, route = '#/overview', shown = OVERVIEW_SHOWN) {
  const reads = holdReads(t.win);
  t.E.go(route);
  assert.doesNotMatch(t.main.shown(), shown, 'a dropped kept page was painted');
  reads.release();
  await drain(60);
}

test('M9 sign-out drops every kept page (the sign-out write, then the reload)', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  assert.ok(t.E.keptPages.size >= 1, 'fixture: pages are kept');
  let reloaded = 0;
  t.win.location.reload = () => { reloaded++; };
  t.win.location.pathname = '/app/';
  t.win.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  await t.win.EWViews.handleAction({ dataset: { action: 'signout' }, closest: () => null, getAttribute: () => null });
  await drain(40);
  assert.equal(t.E.keptPages.size, 0, 'a kept page outlived the sign-out');
  assert.equal(reloaded, 1, 'fixture: the sign-out reloads');
});

test('M9 a session that ends (401) drops every kept page; the same person signing in again sees none', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  t.srv.signedOut = true;
  t.srv.answers['/customer/'] = { status: 401, code: 'SIGNIN_REQUIRED', message: 'Sign in again.' };
  t.E.go('#/overview');
  await drain(80);
  assert.equal(t.E.keptPages.size, 0, 'a kept page outlived the signed-out session');
  delete t.srv.answers['/customer/'];
  t.srv.signedOut = false;
  await t.E.boot();
  await drain(40);
  await visit(t, '#/alerts');
  await backPaintsNothingKept(t);
});

test('M9 a workspace switch drops the kept pages; switching back paints nothing kept', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  assert.ok(t.E.keptPages.has('#/overview'), 'fixture: Overview is kept');
  t.srv.session = { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };
  await t.E.boot();
  await drain(60);
  assert.equal(t.E.current.activeWorkspace, 'ws-b', 'fixture: the tab switched');
  await backPaintsNothingKept(t);
  t.srv.session = { ...HOSTED, workspace_switch_revision: 9 };
  await t.E.boot();
  await drain(60);
  await visit(t, '#/alerts');
  t.srv.w.marker = 'WS-A-AGAIN';
  await backPaintsNothingKept(t);
});

test('M9 a kept page is never shown in another workspace, even if the store were not cleared', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  const kept = t.E.keptPages.get('#/overview');
  assert.ok(kept, 'fixture: Overview is kept');
  t.srv.session = { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 };
  await t.E.boot();
  await drain(60);
  t.E.keptPages.set('#/overview', kept);
  await backPaintsNothingKept(t);
});

test('M9 a role change drops the kept pages (and a page kept for another role never shows)', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  const kept = t.E.keptPages.get('#/overview');
  assert.ok(kept, 'fixture: Overview is kept');
  t.srv.session = { ...HOSTED, role: 'viewer', workspace_context: 'ctx-owner-viewer' };
  await t.E.boot();
  await drain(60);
  assert.equal(t.E.current.sessionRole, 'viewer', 'fixture: the role changed');
  assert.equal(t.E.keptPages.has('#/overview'), false, 'a kept page outlived the role change');
  t.E.keptPages.set('#/overview', kept);
  await backPaintsNothingKept(t);
});

test('M9 the person\'s own write drops the kept pages: Back shows what the write changed, not the page before it', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  await t.E.api('/customer/alerts', { method: 'POST', body: {} });
  t.srv.w.marker = 'AFTER-WRITE';
  assert.equal(t.E.keptPages.size, 0, 'a kept page outlived the write');
  await backPaintsNothingKept(t);
  assert.match(t.main.shown(), /AFTER-WRITE/);
});

test('M9 another person signed in in this tab never sees the kept page', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  t.srv.session = { ...HOSTED, principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other', csrf_token: 'k'.repeat(32) };
  t.srv.w.marker = 'OTHER-PERSON';
  await t.E.boot();
  await drain(60);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  assert.equal(t.E.keptPages.size, 0);
  const reads = holdReads(t.win);
  t.E.go('#/overview');
  assert.doesNotMatch(t.main.shown(), /OWNER-KEPT-MARKER/, 'the owner\'s kept page was painted for another person');
  reads.release();
  await drain(60);
  assert.match(t.main.shown(), /OTHER-PERSON/);
  assert.doesNotMatch(t.main.shown(), /OWNER-KEPT-MARKER/);
});

test('M9 an unowned history entry (another tab\'s or a foreign one) inherits no kept page', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  t.B.entries[t.B.i - 1].state = null;
  const reads = holdReads(t.win);
  t.B.back();
  assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN, 'an unowned entry painted the kept page');
  reads.release();
  await drain(60);
});

test('M9 Search: the kept page brings its Search views back with it (export count, rows) in this epoch', async () => {
  const S = '#/search?mode=ads&q=quincekept';
  const t = await page(S);
  assert.ok(t.E.retained('lastSearch'), 'fixture: Search retained its frame');
  await visit(t, '#/alerts');
  assert.equal(t.E.retained('lastSearch'), null, 'fixture: leaving the page ended the retained frame');
  const reads = holdReads(t.win);
  t.B.back();
  const frame = t.E.retained('lastSearch');
  assert.ok(frame && frame.route === S, 'the kept Search frame is not retained in this epoch');
  assert.ok(t.E.retained('lastSearchTotal'), 'the kept total is not retained');
  reads.release();
  await drain(60);
});

test('M9 a tab keeps at most 6 pages, the most recently shown ones', async () => {
  const t = await page('#/overview');
  for (let i = 1; i <= 8; i++) await visit(t, `#/search?mode=ads&q=capq${i}`);
  await visit(t, '#/alerts');
  const keys = [...t.E.keptPages.keys()];
  assert.ok(keys.length <= 6, `${keys.length} pages kept`);
  assert.ok(keys.includes('#/search?mode=ads&q=capq8') && keys.includes('#/search?mode=ads&q=capq7'), 'the newest pages are kept');
  assert.equal(keys.includes('#/overview'), false, 'the oldest page is dropped first');
});

// ============================================================================
// M9-BACK-FIX1: security r1 and code r1 findings
// ============================================================================
// Customer reads answer when they arrive (as a server would) but are
// delivered only on release, per path.
function holdAtArrival(win) {
  const inner = win.fetch;
  const held = [];
  win.fetch = (url, opts = {}) => {
    if (!/^\/api\/v1\/customer\//.test(url) || (opts.method && opts.method !== 'GET' && !/research-query/.test(url))) return inner(url, opts);
    const answer = inner(url, opts);
    let open;
    const gate = new Promise(res => { open = res; });
    held.push({ url, open });
    return gate.then(() => answer);
  };
  return {
    releaseWhere: re => { for (const h of held.filter(x => re.test(x.url))) { held.splice(held.indexOf(h), 1); h.open(); } },
    release: () => { win.fetch = inner; for (const h of held.splice(0)) h.open(); },
  };
}
// The app's clock (Date.now inside the page), set by the test.
const setClock = (t, ms) => { vm.runInContext('Date', t.win).now = () => ms; };
const MIN = 60 * 1000;

// M9B-SEC-R1-02: the nav waits for the tab-return check, as browser Back does.
test('M9 tab return: while the return check reads the session, the nav paints no kept page; another person signed in elsewhere sees nothing of the first', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  const fire = type => { for (const fn of t.win.docListeners[type] || []) fn({ type }); };
  t.win.document.visibilityState = 'hidden';
  fire('visibilitychange');
  t.srv.session = { ...HOSTED, principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other', csrf_token: 'k'.repeat(32) };
  t.srv.w.marker = 'OTHER-PERSON';
  const inner = t.win.fetch;
  let openSession;
  const sessionGate = new Promise(res => { openSession = res; });
  t.win.fetch = (url, opts = {}) => (/\/session$/.test(url) ? sessionGate.then(() => inner(url, opts)) : inner(url, opts));
  t.win.document.visibilityState = 'visible';
  fire('visibilitychange');
  await drain(5);
  t.E.go('#/overview');
  assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN, 'the nav painted the first person\'s kept page during the return check');
  await drain(20);
  assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN, 'the first person\'s kept page showed while the return check was open');
  openSession();
  await drain(150);
  assert.equal(t.E.current.principal, OTHER, 'fixture: the other person was adopted');
  assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN);
  assert.match(t.main.shown(), /OTHER-PERSON/);
});

// M9B-R1-01: focus stays on the same record after the patch, not the same position.
const A7 = { id: 7, name: 'New Shopify ads', state: 'active_local', lifecycle: 'active_local', unread_ad_count: 5, unread_event_count: 5, query: { version: 2, mode: 'ads', q: 'shopify' } };
async function backWith(change, focusHref) {
  const t = await page('#/alerts', { dom: true });
  await visit(t, '#/overview', 200);
  change(t.srv.w);
  const reads = holdReads(t.win);
  t.B.back();
  const el = t.main.querySelectorAll(`a[href="${focusHref}"]`)[0];
  assert.ok(el, 'fixture: the focus target is on the kept page');
  el.focus();
  reads.release();
  await drain(60);
  const fresh = await page('#/alerts', { dom: true, w: t.srv.w });
  return { t, fresh };
}
const PATCH_CASES = [
  ['a new row above the focused row, whose row also changed', w => { w.alerts = [A7, { ...w.alerts[0], unread_ad_count: 1, unread_event_count: 1 }, w.alerts[1]]; }, '#/alert/4'],
  ['a new row above the focused row', w => { w.alerts = [A7, ...w.alerts]; }, '#/alert/4'],
  ['the row above the focused row removed', w => { w.alerts = [w.alerts[1]]; }, '#/alert/1'],
  ['the focused row removed', w => { w.alerts = [w.alerts[1]]; }, '#/alert/4'],
  ['rows reordered by unread count', w => { w.alerts = [{ ...w.alerts[1], unread_ad_count: 9, unread_event_count: 9 }, { ...w.alerts[0], unread_ad_count: 0, unread_event_count: 0 }]; }, '#/alert/4'],
];
for (const [label, change, href] of PATCH_CASES) {
  test(`M9 patch (${label}): the patched page equals a fresh render, with no old nodes left`, async () => {
    const { t, fresh } = await backWith(change, href);
    assert.equal(t.main.innerHTML, fresh.main.innerHTML);
  });
  test(`M9 patch (${label}): focus stays on the same record, or goes to the page heading when it is gone`, async () => {
    const { t } = await backWith(change, href);
    const now = DOC.activeElement;
    const info = `focused ${href} before; now ${now && now.nodeName} ${now && now.getAttribute && now.getAttribute('href')} connected=${now && now.isConnected}`;
    if (label === 'the focused row removed') {
      assert.ok(now && now.isConnected, info);
      assert.ok(now === t.main.querySelector('h1') || now === t.main, `focus is not on the page heading: ${info}`);
      return;
    }
    assert.equal(now && now.getAttribute('href'), href, info);
    assert.ok(now.isConnected, info);
  });
}
// M9B-R1-03: attributes the new page lacks are removed.
test('M9 patch: an attribute the refreshed page lacks is removed (a paused alert resumed: its unread tag loses data-tag)', async () => {
  const t = await page('#/alerts', { dom: true, w: { ...world(), alerts: world().alerts.map(a => (a.id === 4 ? { ...a, lifecycle: 'paused', state: 'paused' } : a)) } });
  assert.match(t.main.innerHTML, /data-tag="outline"/, 'fixture: the paused alert\'s unread tag has data-tag');
  await visit(t, '#/overview');
  t.srv.w.alerts = world().alerts;
  t.B.back();
  await drain(60);
  const fresh = await page('#/alerts', { dom: true, w: t.srv.w });
  assert.doesNotMatch(fresh.main.innerHTML, /data-tag="outline"/, 'fixture: the resumed alert has no outline tag');
  assert.equal(t.main.innerHTML, fresh.main.innerHTML, 'a stale attribute stayed after the patch');
});

// M9B-R1-03: a 428 is a refusal.
test('M9 a 428 on the refresh removes the kept page and refuses', async () => {
  const t = await page('#/alerts');
  await visit(t, '#/overview');
  t.srv.answers['/customer/alerts'] = { status: 428, code: 'PRECONDITION_REQUIRED', message: 'Reload the workspace.' };
  t.B.back();
  await drain(80);
  assert.doesNotMatch(t.main.shown(), /ew-work-alert/, 'kept content stayed up under a 428');
  assert.equal(t.E.keptPages.has('#/alerts'), false, 'the refused page is still kept');
});

// M9B-R1-03: a page with Retry is never kept.
test('M9 a page with Retry (a panel that failed) is never kept: Back paints the earlier clean page, never the failed one', async () => {
  const t = await page('#/overview');
  await visit(t, '#/alerts');
  t.srv.answers['/customer/saved-items'] = { status: 503, code: 'INTERNAL', message: 'internal error' };
  await visit(t, '#/overview');
  assert.match(t.main.shown(), /data-action="retry"/, 'fixture: Overview shows a panel failure with Retry');
  await visit(t, '#/alerts');
  delete t.srv.answers['/customer/saved-items'];
  const reads = holdReads(t.win);
  t.B.back();
  assert.doesNotMatch(t.main.shown(), /data-action="retry"/, 'a page with Retry was kept and painted');
  reads.release();
  await drain(60);
});

// Security R1-03 (lead option c): a kept page with no successful refresh for 10 minutes is dropped.
// A page opening as new: its loading state (Search: "Searching…").
const OPENING = /class="loading"|Opening Overview|Opening alerts|class="skeleton|Searching…/;
for (const [label, start, other, shown, fail] of [
  ['Overview', '#/overview', '#/alerts', OVERVIEW_SHOWN, null],
  ['Alerts', '#/alerts', '#/overview', /ew-work-alert/, '/customer/alerts'],
  ['Search', '#/search?mode=ads&q=quincekept', '#/alerts', /quincekept/, '/customer/research-query'],
]) {
  test(`M9 ${label}: a kept page with no successful refresh for 10 minutes is dropped; a newer one still shows`, async () => {
    const T0 = 1_800_000_000_000;
    const t = await page(start);
    setClock(t, T0);
    await visit(t, start);
    await visit(t, other);
    let reads;
    if (fail) {
      // A failed refresh keeps the page but does not make it newer.
      t.srv.answers[fail] = { status: 500, code: 'INTERNAL', message: 'internal error' };
      setClock(t, T0 + 9 * MIN);
      await visit(t, start);
      assert.match(t.main.shown(), shown, 'fixture: the failed refresh keeps the 9-minute-old page');
      delete t.srv.answers[fail];
      await visit(t, other);
      setClock(t, T0 + 10 * MIN + 1);
      reads = holdReads(t.win);
      t.E.go(start);
      assert.match(t.main.shown(), OPENING, 'a kept page with no successful refresh for 10 minutes was painted');
      assert.equal(t.E.keptPages.has(start), false, 'the old page is still kept');
      reads.release();
      await drain(60);
      return;
    }
    setClock(t, T0 + 9 * MIN);
    reads = holdReads(t.win);
    t.E.go(start);
    assert.match(t.main.shown(), shown, 'a 9-minute-old kept page is shown');
    assert.doesNotMatch(t.main.shown(), OPENING, 'a 9-minute-old kept page is shown');
    reads.release();
    await drain(60);
    // The 9-minute refresh succeeded: the page is newer again.
    await visit(t, other);
    setClock(t, T0 + 18 * MIN);
    reads = holdReads(t.win);
    t.E.go(start);
    assert.match(t.main.shown(), shown, 'a page refreshed 9 minutes ago was dropped');
    assert.doesNotMatch(t.main.shown(), OPENING, 'a page refreshed 9 minutes ago was dropped');
    reads.release();
    await drain(60);
    await visit(t, other);
    setClock(t, T0 + 28 * MIN + 1);
    reads = holdReads(t.win);
    t.E.go(start);
    assert.match(t.main.shown(), OPENING, 'a kept page with no successful refresh for 10 minutes was painted');
    assert.equal(t.E.keptPages.has(start), false, 'the old page is still kept');
    reads.release();
    await drain(60);
  });
}

// Security R1-04: on Overview, a refusal that is not a boundary drops the
// kept page as soon as the first refused read answers.
for (const [label, answer] of [
  ['403 (no lost-access marker)', { status: 403, message: 'Your role does not allow this.' }],
  ['428', { status: 428, code: 'PRECONDITION_REQUIRED', message: 'Reload the workspace.' }],
]) {
  test(`M9 Overview: a ${label} drops the kept page at the first refused read, not when the page ends`, async () => {
    const t = await page('#/overview');
    await visit(t, '#/alerts');
    t.srv.answers['/customer/'] = answer;
    const h = holdAtArrival(t.win);
    t.B.back();
    assert.match(t.main.shown(), OVERVIEW_SHOWN, 'fixture: the kept page painted');
    await drain(10);
    h.releaseWhere(/\/customer\/overview/);
    await drain(30);
    assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN, 'the kept page stayed up after a refused read answered');
    h.release();
    await drain(80);
    assert.doesNotMatch(t.main.shown(), OVERVIEW_SHOWN);
    assert.equal(t.E.keptPages.has('#/overview'), false);
  });
}

// Security R1-05: a session change in another tab that the tab cannot
// confirm (the session read fails) empties the store.
for (const [label, session] of [
  ['another workspace', { ...HOSTED, active_workspace: 'ws-b', workspace_context: 'ctx-owner-b', workspace_switch_revision: 8 }],
  ['another person', { ...HOSTED, principal: OTHER, email: 'other@example.test', workspace_context: 'ctx-other', csrf_token: 'k'.repeat(32) }],
]) {
  test(`M9 ${label} in another tab, and the session read fails: no kept page is left`, async () => {
    const t = await page('#/overview');
    await visit(t, '#/alerts');
    t.srv.session = session;
    const inner = t.win.fetch;
    t.win.fetch = (url, opts = {}) => (/\/session$/.test(url)
      ? Promise.resolve({ ok: false, status: 500, headers: { get: () => null }, text: async () => '{"ok":false,"error":{"code":"INTERNAL","message":"x"}}' })
      : inner(url, opts));
    t.B.back();
    await drain(120);
    assert.match(t.main.shown(), /could not be loaded/, 'fixture: the tab could not confirm the session');
    assert.equal(t.E.keptPages.size, 0, 'kept pages outlived an unconfirmed session change');
  });
}

// M9B-R2-01 (r2 review): once a kept page's refresh has published, a later
// refused read from an action on that page (Play on a Search card reading a
// record, the alert dialog's Preview) is that action's refusal, not the
// page's: the page stays, as on a page reached without a kept paint.
for (const [route, other, shown] of [['#/alerts', '#/overview', /ew-work-alert/], ['#/overview', '#/alerts', OVERVIEW_SHOWN]]) {
  for (const status of [403, 428]) {
    test(`M9B-R2-01 after the kept ${route} page refreshed, a later ${status} on an action's read keeps the page`, async () => {
      const t = await page(route);
      await visit(t, other);
      t.B.back();
      await drain(80);
      assert.match(t.main.shown(), shown, 'the refreshed kept page is not shown');
      t.srv.answers['/customer/records/r1'] = { status, code: status === 428 ? 'PRECONDITION_REQUIRED' : 'FORBIDDEN', message: 'Refused for this one item.' };
      await t.E.api('/customer/records/r1').catch(() => {});
      await drain(80);
      assert.match(t.main.shown(), shown, 'an action\'s refusal replaced the whole page');
      assert.doesNotMatch(t.main.shown(), /class="loading"|aria-busy="true"/, 'the page went back to its loading state');
    });
  }
}

// M9B-SCR-R2-01 (screen r2, S7): after Back, a failed refresh (500) keeps
// the Search or Alerts page where the person was, so its failure notice
// (with Retry) is pinned in view (sticky at the top of the page), with the
// same words and role="alert"; the kept results are not inside it, so they
// do not move. A refresh that succeeds pins nothing.
const STYLES = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
for (const [label, start, other, fail, says] of [
  ['Search', '#/search?mode=ads&q=quincekept', '#/alerts', '/customer/research-query', /Search didn’t finish[\s\S]*Retry[\s\S]*Showing the earlier results for this scope below; they are not new results\./],
  ['Alerts', '#/alerts', '#/overview', '/customer/alerts', /Alerts are unavailable: internal error Your alerts still exist\.Retry/],
]) {
  test(`M9B-SCR-R2-01 ${label}: a failed refresh over the kept page pins its failure notice in view`, async () => {
    const t = await page(start, { dom: true });
    await visit(t, other, 640);
    t.srv.answers[fail] = { status: 500, code: 'INTERNAL', message: 'internal error' };
    t.win.scrolls.length = 0;
    t.B.back();
    await drain(80);
    const pinned = t.main.querySelectorAll('.ew-pinned');
    assert.equal(pinned.length, 1, 'the failure notice over the kept page is not pinned');
    assert.equal(pinned[0].parentNode, t.main, 'the pinned notice is not a child of the page (sticky needs the page as its box)');
    assert.match(pinned[0].textContent, says, 'the pinned notice lacks the failure words or Retry');
    assert.ok(pinned[0].querySelector('[role="alert"]') || pinned[0].getAttribute('role') === 'alert', 'the notice lost role="alert"');
    assert.ok(pinned[0].querySelector('[data-action="retry"]'), 'no Retry in the pinned notice');
    assert.equal(pinned[0].querySelectorAll('.ew-work-alert, .ew-search-card, #search-results').length, 0, 'kept results moved into the pinned notice');
    assert.match(t.main.innerHTML, label === 'Search' ? /quincekept/ : /ew-work-alert/, 'the kept page is gone');
    assert.equal(t.win.scrolls.filter(y => y !== 640).length, 0, 'the failed refresh moved the scroll');
    assert.ok(/\.ew-pinned\s*\{[^}]*position:\s*sticky;[^}]*top:\s*0;/.test(STYLES), 'no sticky rule for a pinned notice (styles.css)');
    if (label === 'Search') {
      // Retry (an on-screen refresh) that fails again keeps the pin.
      await t.win.EWViews.render();
      await drain(80);
      assert.equal(t.main.querySelectorAll('.ew-pinned').length, 1, 'a failed Retry unpinned the notice over the earlier results');
    }
    delete t.srv.answers[fail];
    await visit(t, other);
    t.B.back();
    await drain(80);
    assert.equal(t.main.querySelectorAll('.ew-pinned').length, 0, 'a refresh that succeeded left a pinned notice');
  });
}

// M9B-SCR-R2-01: the notice put in above the person's place does not move
// the results: the kept page's last block stays where it was on screen. A
// simple layout: each element is 10 px tall in document order (a hidden one
// has no height); the block is found again by its markup, since the patch
// may reuse nodes.
for (const [label, start, other, fail] of [
  ['Search', '#/search?mode=ads&q=quincekept', '#/alerts', '/customer/research-query'],
  ['Alerts', '#/alerts', '#/overview', '/customer/alerts'],
]) {
  test(`M9B-SCR-R2-01 ${label}: the failure notice over the kept page does not move the results`, async () => {
    const t = await page(start, { dom: true });
    await visit(t, other, 640);
    t.srv.answers[fail] = { status: 500, code: 'INTERNAL', message: 'internal error' };
    t.win.scrollBy = (x, y) => { t.win.scrollY += y; };
    const proto = Object.getPrototypeOf(t.main);
    const shown = () => [t.main, ...t.main.all()].filter(n => !n.hasAttribute('hidden'));
    proto.getBoundingClientRect = function () {
      const top = 10 * shown().indexOf(this) - t.win.scrollY;
      return { top, bottom: this === t.main ? 10 * shown().length - t.win.scrollY : top + 10 };
    };
    const kids = () => t.main.childNodes.filter(c => c.nodeType === 1);
    try {
      t.B.back();
      const last = kids().filter(c => !c.hasAttribute('hidden')).at(-1).outerHTML;
      const before = kids().find(c => c.outerHTML === last).getBoundingClientRect().top;
      // A trailing node of the painted page that the refresh does not
      // carry (the screen driver's hidden marker) must not lose the place.
      const mark = new (proto.constructor)(1, 'SPAN');
      mark.setAttribute('hidden', '');
      t.main.appendChild(mark);
      await drain(80);
      assert.equal(t.main.querySelectorAll('.ew-pinned').length, 1, 'fixture: the failure notice is pinned');
      const after = kids().find(c => c.outerHTML === last);
      assert.ok(after, 'the kept page\'s last block is gone');
      assert.equal(after.getBoundingClientRect().top, before, 'the failure notice moved the kept results');
    } finally {
      delete proto.getBoundingClientRect;
    }
  });
}

// ============================================================================
// M9B-R3-02 (NOGAP-TS row 177): Play and Preview refused over a refreshed kept page
// ============================================================================
//    after the kept Alerts / Overview / Search page's refresh published.
const SEARCH_R3 = '#/search?mode=ads&q=quincekept';
const SEARCH_SHOWN = /About these counts/;
const R3_LOADING = /class="loading"|aria-busy="true"|class="skeleton/;
for (const status of [403, 428]) {
  test(`M9B-R3 after the kept Search page refreshed, Play on a card refused ${status} keeps the page`, async () => {
    const t = await page(SEARCH_R3);
    await visit(t, '#/alerts');
    t.B.back();
    await drain(80);
    assert.match(t.main.shown(), SEARCH_SHOWN, 'the refreshed kept Search page is not shown');
    assert.equal(t.E.current.publishedToken, t.E.current.fetchToken, 'the refresh did not publish');
    t.srv.answers['/customer/records/r1'] = { status, code: status === 428 ? 'PRECONDITION_REQUIRED' : 'FORBIDDEN', message: 'Refused for this one item.' };
    // The harness answers every selector with a stub; Search has no player yet.
    const q = t.win.document.querySelector;
    t.win.document.querySelector = sel => (sel === '#search-audio' ? null : q(sel));
    const before = t.srv.calls.length;
    await t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref: 'r1' }, closest: () => null }).catch(() => {});
    await drain(80);
    assert.ok(t.srv.calls.slice(before).some(c => c.path === '/customer/records/r1'), 'Play did not read the record');
    assert.match(t.main.shown(), SEARCH_SHOWN, 'Play\'s refusal replaced the whole Search page');
    assert.doesNotMatch(t.main.shown(), R3_LOADING, 'the Search page went back to its loading state');
    assert.ok(t.E.keptPages.has(SEARCH_R3), 'the kept Search page was dropped');
    assert.ok(t.E.retained('lastSearch'), 'the Search views were cleared');
  });
}
for (const [route, other, shown] of [['#/alerts', '#/overview', /ew-work-alert/], ['#/overview', '#/alerts', OVERVIEW_SHOWN], [SEARCH_R3, '#/alerts', SEARCH_SHOWN]]) {
  test(`M9B-R3 after the kept ${route} page refreshed, the alert Preview (POST research-query) refused 403 keeps the page`, async () => {
    const t = await page(route);
    await visit(t, other);
    t.B.back();
    await drain(80);
    assert.match(t.main.shown(), shown, 'the refreshed kept page is not shown');
    t.srv.answers['/customer/research-query'] = { status: 403, code: 'FORBIDDEN', message: 'Your role does not allow this.', method: 'POST' };
    await t.E.api('/customer/research-query', { method: 'POST', body: { query: { version: 2, mode: 'ads', q: 'x' } } }).catch(() => {});
    await drain(80);
    assert.match(t.main.shown(), shown, 'the Preview\'s refusal replaced the whole page');
    assert.doesNotMatch(t.main.shown(), R3_LOADING, 'the page went back to its loading state');
    assert.ok(t.E.keptPages.has(route), 'the kept page was dropped');
  });
}

// ============================================================================
// M9B-R4-02 (NOGAP-TS row 179): the scroll fix runs only under a failed
// refresh over a kept page, and no other failure notice is pinned
// ============================================================================
// scrollBy is defined and #main's end depends on its content, so a scroll
// fix anywhere else would show as a call.
for (const [label, start, other, read] of [
  ['Search', '#/search?mode=ads&q=quincekept', '#/alerts', '/customer/research-query'],
  ['Alerts', '#/alerts', '#/overview', '/customer/alerts'],
  ['Overview', '#/overview', '#/alerts', '/customer/'],
]) {
  test(`M9B-R4-02 ${label}: no scroll fix and no pinned notice outside a failed refresh over a kept page`, async () => {
    const t = await page(start, { dom: true });
    const by = [];
    t.win.scrollBy = (x, y) => { by.push(y); t.win.scrollY += y; };
    const proto = Object.getPrototypeOf(t.main);
    proto.getBoundingClientRect = function () {
      return { top: 0, bottom: 10 * (this.all ? this.all().length : 1) - t.win.scrollY };
    };
    const pins = () => t.main.querySelectorAll('.ew-pinned').length;
    const check = (what, pinned = 0) => {
      assert.deepEqual(by, [], `${what}: the scroll was moved`);
      assert.equal(pins(), pinned, `${what}: pinned notices`);
      by.length = 0;
    };
    const fail500 = { status: 500, code: 'INTERNAL', message: 'internal error' };
    try {
      await visit(t, other, 640); by.length = 0;
      t.B.back(); await drain(80);
      check('Back, refresh ok');
      await visit(t, other, 640); t.E.keptPages.clear(); by.length = 0;
      t.srv.answers[read] = fail500;
      t.B.back(); await drain(80);
      check('Back with no kept page, refresh 500');
      delete t.srv.answers[read];
      for (const refused of [{ status: 403, code: 'FORBIDDEN', message: 'Refused.' }, { status: 428, code: 'PRECONDITION_REQUIRED', message: 'Reload the workspace.' }]) {
        await visit(t, start, 0); await visit(t, other, 640); by.length = 0;
        t.srv.answers[read] = refused;
        t.B.back(); await drain(80);
        check(`Back, refresh refused ${refused.status}`);
        delete t.srv.answers[read];
      }
      // A pinned failure, then a move to another page: no pin, no scroll fix.
      await visit(t, start, 0); await visit(t, other, 640);
      t.srv.answers[read] = fail500;
      t.B.back(); await drain(80);
      delete t.srv.answers[read]; by.length = 0;
      await visit(t, other, 0);
      check('after a pinned failure, the next page');
    } finally {
      delete proto.getBoundingClientRect;
    }
  });
}
