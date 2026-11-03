/* Redesign (b2) test harness: boots app.js, ui.js, saved.js, views.js,
   b2.js and the page modules in a vm, with a small fake DOM, a fixture API
   and a chosen window width. Page builders write their render tests on it:

     const H = require('./b2_harness.cjs');
     const { html, header, win } = await H.render('#/search?q=quince', api, { width: 1440 });

   - api(path, opts) returns the fixture body, or an Error to throw.
   - width < 600 is the phone layout (EWB2.layout() === 'phone').
   - pages: extra page specs to register before the render, e.g.
     { search: { on: true, render: async () => '<p>x</p>' } }.
   - modules: false skips the shipped page modules (base tests only).
   - whileReading: async (main) => {...} runs while the read is pending.
   - late: true runs b2.js and the page modules only when the returned
     loadLate() is called, then fires DOMContentLoaded (deferred scripts
     that arrive after the boot read, AD-RACE).
   - fetch: replaces the fake network (the boot's own session read).
   Run a page's tests: node --test tests/frontend_b2_<page>.test.cjs */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const PAGE_MODULES = ['page-search.js', 'page-ad.js', 'page-overview.js', 'page-brand.js', 'page-signin.js'];
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };
const flush = () => new Promise(res => setTimeout(res, 0));

// A class list on a Set, enough for toggle/contains/add/remove.
function classList(initial = '') {
  const set = new Set(String(initial).split(/\s+/).filter(Boolean));
  return {
    add: (...c) => c.forEach(x => set.add(x)), remove: (...c) => c.forEach(x => set.delete(x)),
    contains: c => set.has(c), toggle: (c, on) => { const want = on === undefined ? !set.has(c) : !!on; if (want) set.add(c); else set.delete(c); return want; },
    toString: () => [...set].join(' '), get value() { return [...set].join(' '); },
  };
}
function element(id = '', cls = '') {
  const el = { id, innerHTML: '', textContent: '', hidden: false, dataset: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; }, hasAttribute(k) { return k in this.attrs; },
    focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {}, getBoundingClientRect: () => ({ bottom: 0, top: 0 }) };
  el.classList = classList(cls);
  Object.defineProperty(el, 'className', { get: () => el.classList.value, set: v => { el.classList = classList(v); } });
  return el;
}

function boot({ route = '#/overview', width = 1440, modules = true, pages = {}, late = false, fetch = null } = {}) {
  const store = {};
  const listeners = [];
  const media = [];
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, URL, Intl, WeakMap,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route, href: 'http://127.0.0.1:9153/app/' + route, origin: 'http://127.0.0.1:9153', pathname: '/app/' },
    history: { replaceState() {}, pushState() {} },
    scrollTo() {}, scrollBy() {},
    innerWidth: width,
    matchMedia: q => {
      const m = /max-width:\s*(\d+)px/.exec(q);
      const mq = { media: q, matches: m ? width <= Number(m[1]) : false, listeners: [],
        addEventListener(type, fn) { this.listeners.push(fn); }, removeEventListener() {} };
      media.push(mq);
      return mq;
    },
    fetch: async url => {
      if (url === '/api/v1/customer/preferences') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
          workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    window: {},
  };
  const els = { '#main': element('main'), '#header': element('header', 'topbar'), '#modal': element('modal'), '#dataset': element('dataset') };
  els['#modal'].open = false; els['#modal'].close = () => {}; els['#modal'].showModal = () => {};
  const body = element('body');
  sandbox.document = {
    body,
    querySelector: sel => els[sel] || null,
    querySelectorAll: () => [],
    getElementById: id => els['#' + id] || null,
    addEventListener: (type, fn, opts) => listeners.push({ type, fn, capture: opts === true || !!(opts && opts.capture) }),
    createElement: () => element(), createTextNode: t => t,
  };
  if (late) sandbox.document.readyState = 'interactive';
  if (fetch) sandbox.fetch = fetch;
  sandbox.addEventListener = () => {};
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const run = file => vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  for (const file of ['app.js', 'ui.js', 'saved.js', 'views.js']) run(file);
  const loadLate = () => {
    run('b2.js');
    if (modules) for (const file of PAGE_MODULES) if (fs.existsSync(path.join(APP, file))) run(file);
    for (const [name, spec] of Object.entries(pages)) sandbox.EWB2.page(name, spec);
    if (!late) return;
    sandbox.document.readyState = 'interactive';
    for (const l of listeners.filter(x => x.type === 'DOMContentLoaded')) l.fn({ type: 'DOMContentLoaded' });
  };
  if (!late) { loadLate(); sandbox.EW.current.route = route; }
  return { win: sandbox, els, body, listeners, media, loadLate };
}

async function render(route, api, opts = {}) {
  const b = boot({ ...opts, route: '#/settings' });
  const { win, els } = b;
  await flush();
  win.EW.current.hosted = false;
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  const done = win.EWViews.render();
  // whileReading(main): runs while the route's read is pending; main() is the page so far.
  if (opts.whileReading) await opts.whileReading(() => els['#main'].innerHTML);
  await done;
  for (let i = 0; i < 20; i++) await flush();
  stopTimers();
  return { html: els['#main'].innerHTML, header: els['#header'], body: b.body, win, listeners: b.listeners, media: b.media };
}

const text = html => String(html).replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim();

module.exports = { APP, boot, render, text, flush, element, stopTimers };
