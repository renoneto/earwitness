/* UAT walkthrough fix round, alerts: VL-01/VL-19/VS-12/F-8 (brand-only
   alerts default to Ads; one total per alert card; preview copy),
   VL-08, VL-14, VL-16, VL-17, V-17 (alert scope line), V-22, V-23,
   VS-22/F-15, F-16 copy, VL-36, "Brand not identified".
   The dialog cases run the real opener against the structural fake DOM
   of frontend_dv3_astra_drafts.test.cjs (copied).
   Run: node --test tests/frontend_uat_alerts.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

/* -- fake DOM (copied from frontend_dv3_astra_drafts.test.cjs) ------------- */
const VOID = new Set(['input', 'br', 'hr', 'img', 'meta']);
const unesc = s => String(s).replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const kebab = k => String(k).replace(/[A-Z]/g, c => '-' + c.toLowerCase());
function parseAttrs(s) {
  const out = {};
  for (const m of String(s).matchAll(/([a-zA-Z_:][-a-zA-Z0-9_:]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
    out[m[1].toLowerCase()] = unesc(m[2] ?? m[3] ?? m[4] ?? '');
  }
  return out;
}
function makeDom() {
  const docListeners = {};
  class El {
    constructor(tag, attrs = {}) {
      this.tagName = tag.toUpperCase();
      this.attrs = { ...attrs };
      this.children = [];
      this.parentElement = null;
      this.listeners = {};
      this.text = '';
      this._html = '';
      this.focused = false;
      const self = this;
      this.dataset = new Proxy({}, {
        get: (_, k) => self.attrs['data-' + kebab(k)],
        set: (_, k, v) => { self.attrs['data-' + kebab(k)] = String(v); return true; },
        deleteProperty: (_, k) => { delete self.attrs['data-' + kebab(k)]; return true; },
        has: (_, k) => ('data-' + kebab(k)) in self.attrs,
      });
      this.classList = { add: c => { self.attrs.class = `${self.attrs.class || ''} ${c}`.trim(); },
        remove: c => { self.attrs.class = (self.attrs.class || '').split(/\s+/).filter(x => x !== c).join(' '); },
        contains: c => (self.attrs.class || '').split(/\s+/).includes(c) };
      if (tag === 'input') {
        this.defaultValue = attrs.value ?? (['checkbox', 'radio'].includes(attrs.type) ? 'on' : '');
        this.value = this.defaultValue;
        this.defaultChecked = 'checked' in attrs;
        this._checked = this.defaultChecked;
      }
    }
    get checked() { return this._checked; }
    set checked(on) {
      // A radio group has one checked member.
      if (on && this.type === 'radio' && this.form) for (const r of this.form.elements) if (r !== this && r.type === 'radio' && r.name === this.name) r._checked = false;
      this._checked = !!on;
    }
    get id() { return this.attrs.id || ''; }
    get name() { return this.attrs.name || ''; }
    get type() {
      if (this.tagName === 'INPUT') return this.attrs.type || 'text';
      if (this.tagName === 'BUTTON') return this.attrs.type || 'submit';
      if (this.tagName === 'SELECT') return 'select-one';
      if (this.tagName === 'TEXTAREA') return 'textarea';
      return undefined;
    }
    get disabled() { return 'disabled' in this.attrs; }
    set disabled(v) { if (v) this.attrs.disabled = ''; else delete this.attrs.disabled; }
    get options() { return this.tagName === 'SELECT' ? this.descendants().filter(e => e.tagName === 'OPTION') : undefined; }
    get value() {
      if (this.tagName === 'SELECT') { const o = this.options; const s = o.find(x => x.selected) || o[0]; return s ? s.value : ''; }
      if (this.tagName === 'OPTION') return 'value' in this.attrs ? this.attrs.value : this.text;
      return this._value;
    }
    set value(v) {
      if (this.tagName === 'SELECT') { for (const o of this.options) o.selected = o.value === String(v); return; }
      this._value = String(v);
    }
    get form() { return this.closest('form'); }
    get elements() {
      return this.descendants().filter(e => ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(e.tagName));
    }
    get isConnected() { let p = this; while (p.parentElement) p = p.parentElement; return p.tagName === '#ROOT' || p.tagName === 'DIALOG' || p.tagName === 'MAIN'; }
    get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
    set textContent(t) { this.children = []; this.text = String(t); }
    get innerHTML() { return this._html; }
    set innerHTML(html) {
      this._html = String(html);
      this.children = [];
      this.text = '';
      for (const c of parse(this._html)) this.appendChild(c);
    }
    insertAdjacentHTML(where, html) {
      const nodes = parse(html);
      const p = this.parentElement;
      if (where !== 'beforebegin' || !p) throw new Error('fake DOM: only beforebegin');
      const i = p.children.indexOf(this);
      for (const n of nodes) n.parentElement = p;
      p.children.splice(i, 0, ...nodes);
    }
    appendChild(c) { if (c.parentElement) c.remove(); c.parentElement = this; this.children.push(c); return c; }
    remove() { const p = this.parentElement; if (p) { p.children = p.children.filter(x => x !== this); this.parentElement = null; } }
    contains(o) { for (let p = o; p; p = p.parentElement) if (p === this) return true; return false; }
    descendants() { const out = []; for (const c of this.children) out.push(c, ...c.descendants()); return out; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    removeAttribute(k) { delete this.attrs[k]; }
    hasAttribute(k) { return k in this.attrs; }
    focus() { dom.active = this; }
    matches(sel) { return String(sel).split(',').some(s => matchChain(this, s.trim().split(/\s+/))); }
    closest(sel) { for (let p = this; p; p = p.parentElement) if (p.matches && p.matches(sel)) return p; return null; }
    querySelectorAll(sel) { return this.descendants().filter(e => e.matches(sel)); }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    dispatchEvent(ev) {
      ev.target = this;
      for (let p = this; p; p = ev.bubbles ? p.parentElement : null) for (const fn of (p.listeners[ev.type] || [])) fn(ev);
      if (ev.bubbles && this.isConnected) for (const fn of (docListeners[ev.type] || [])) fn(ev);
      return true;
    }
    reset() {
      for (const e of this.elements) {
        if (e.tagName === 'INPUT') { e.value = e.defaultValue; e.checked = e.defaultChecked; }
        if (e.tagName === 'TEXTAREA') e.value = e.defaultValue;
        if (e.tagName === 'SELECT') for (const o of e.options) o.selected = o.defaultSelected;
      }
    }
  }
  function matchSimple(el, s) {
    if (!el.attrs) return false;
    let rest = s;
    const not = [];
    rest = rest.replace(/:not\(([^)]*)\)/g, (_, inner) => { not.push(inner); return ''; });
    let checked = false;
    rest = rest.replace(/:checked/g, () => { checked = true; return ''; });
    const attrs = [];
    rest = rest.replace(/\[([^\]=]+)(?:=("?)([^"\]]*)\2)?\]/g, (_, k, q, v) => { attrs.push([k, v]); return ''; });
    const m = rest.match(/^([a-zA-Z0-9]*)((?:[#.][-\w]+)*)$/);
    if (!m) throw new Error('fake DOM: unsupported selector ' + s);
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    for (const part of m[2].match(/[#.][-\w]+/g) || []) {
      if (part[0] === '#' && el.id !== part.slice(1)) return false;
      if (part[0] === '.' && !el.classList.contains(part.slice(1))) return false;
    }
    for (const [k, v] of attrs) {
      const have = k === 'type' ? el.type : el.getAttribute(k);
      if (have === null || have === undefined) return false;
      if (v !== undefined && String(have) !== v) return false;
    }
    if (checked && !el.checked) return false;
    return !not.some(n => matchSimple(el, n));
  }
  function matchChain(el, parts) {
    if (!matchSimple(el, parts[parts.length - 1])) return false;
    if (parts.length === 1) return true;
    for (let p = el.parentElement; p; p = p.parentElement) if (matchChain(p, parts.slice(0, -1))) return true;
    return false;
  }
  function parse(html) {
    const root = new El('#frag');
    const stack = [root];
    for (const m of String(html).matchAll(/<(\/?)([a-zA-Z0-9]+)\b([^>]*)>|([^<]+)/g)) {
      const top = stack[stack.length - 1];
      if (m[4] !== undefined) { top.text += unesc(m[4]); continue; }
      const tag = m[2].toLowerCase();
      if (m[1]) {
        const i = stack.map(e => e.tagName).lastIndexOf(tag.toUpperCase());
        if (i > 0) stack.length = i;
        continue;
      }
      const el = new El(tag, parseAttrs(m[3]));
      top.appendChild(el);
      if (!VOID.has(tag)) stack.push(el);
    }
    // Finalise form controls from their parsed content.
    for (const el of root.descendants()) {
      if (el.tagName === 'TEXTAREA') { el.defaultValue = el.text; el._value = el.text; }
      if (el.tagName === 'OPTION') { el.defaultSelected = 'selected' in el.attrs; el.selected = el.defaultSelected; }
    }
    const kids = root.children;
    for (const k of kids) k.parentElement = null;
    return kids;
  }
  const docRoot = new El('#root');
  const modal = new El('dialog', { id: 'modal' });
  modal.open = false;
  modal.showModal = () => { modal.open = true; };
  modal.close = () => { modal.open = false; };
  const main = new El('main', { id: 'main' });
  docRoot.appendChild(main);
  docRoot.appendChild(modal);
  const dom = { El, parse, modal, main, docRoot, docListeners, active: null };
  dom.document = {
    get activeElement() { return dom.active; },
    querySelector: sel => (sel === '#modal' ? modal : sel === '#main' ? main : docRoot.querySelector(sel)),
    querySelectorAll: sel => docRoot.querySelectorAll(sel),
    addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
    createElement: tag => new El(tag), createTextNode: t => t,
  };
  return dom;
}

/* -- app loader ------------------------------------------------------------ */
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
class FakeEvent { constructor(type, o = {}) { this.type = type; this.bubbles = !!o.bubbles; } preventDefault() {} }
class FakeFormData {
  constructor(form) {
    this.e = [];
    for (const el of form.elements) {
      if (!el.name || el.disabled || ['submit', 'button', 'reset'].includes(el.type)) continue;
      if ((el.type === 'checkbox' || el.type === 'radio') && !el.checked) continue;
      this.e.push([el.name, el.value]);
    }
  }
  *entries() { yield* this.e; }
  get(k) { const hit = this.e.find(([n]) => n === k); return hit ? hit[1] : null; }
}
async function app(route = '#/search?mode=ads&q=sleep') {
  const dom = makeDom();
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, setTimeout, clearTimeout,
    Event: FakeEvent, FormData: FakeFormData,
    location: { hash: route, href: 'http://127.0.0.1/' + route },
    history: { replaceState() {} },
    scrollTo() {}, scrollY: 0,
    fetch: sessionFetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: dom.document,
    addEventListener() {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  // No page chrome or page reads in these tests: the dialogs are the subject.
  sandbox.EW.header = () => {};
  sandbox.EW.api = async p => { throw Object.assign(new Error('no page read in unit test: ' + p), { status: 503 }); };
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  await new Promise(res => setTimeout(res, 0));
  const win = sandbox;
  win.EW.current.route = route;
  win.EW.header = () => {};
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  const escape = () => { for (const fn of dom.docListeners.keydown || []) fn({ key: 'Escape', preventDefault() {} }); };
  const act = (action, dataset = {}) => win.EWViews.handleAction({ dataset: { action, ...dataset }, disabled: false, closest: () => null, getAttribute: () => null });
  const form = id => dom.modal.querySelector('#' + id);
  const type = (el, value) => { el.value = value; el.dispatchEvent(new FakeEvent('input', { bubbles: true })); };
  const pick = (el, value) => { el.value = value; el.dispatchEvent(new FakeEvent('change', { bubbles: true })); };
  const check = (el, on) => { el.checked = on; el.dispatchEvent(new FakeEvent('change', { bubbles: true })); };
  return { win, dom, toasts, escape, act, form, type, pick, check };
}

/* -- page renderer (as frontend_dv3_ux_alerts.test.cjs) -------------------- */
function loadPages(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, location: { hash: route }, history: { replaceState() {} }, scrollTo() {},
    fetch: sessionFetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  sandbox.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  return sandbox;
}
async function pageHtml(route, api, before) {
  const win = loadPages(route);
  await new Promise(res => setTimeout(res, 0));
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async p => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  if (before) before(win);
  await win.EWViews.render();
  return { win, html: main.innerHTML, rerender: async () => { await win.EWViews.render(); return main.innerHTML; } };
}
const T1 = '2026-10-14T09:00:00Z';
const ALERT = { id: 5, name: 'Shopify', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, unread_event_count: 0, events: [], query: { version: 2, mode: 'ads', q: '', brands: ['shopify'] },
  current_matches: { count: 1, tracked: 1, tracked_by_kind: { new_finding: 1 }, held_for_brand_review: 0 } };
const alertApi = (alert, brands = [{ key: 'shopify', display: 'Shopify' }]) => p => {
  if (p === '/customer/alerts/' + alert.id) return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands };
  throw new Error('unexpected request ' + p);
};
const alertPage = (alert, before) => pageHtml('#/alert/' + alert.id, alertApi(alert), before);
const asideOf = html => html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>'));
const bigNumbers = html => [...asideOf(html).matchAll(/<p class="ew-work-big">([^<]*)<\/p>/g)].map(m => m[1]);

/* -- dialog helpers --------------------------------------------------------- */
const BRANDS = [{ key: 'shopify', display: 'Shopify' }, { key: 'kachava', display: 'Ka’Chava' }];
const dialogApi = (alert, previewResult) => async p => {
  if (alert && p === '/customer/alerts/' + alert.id + '?summaries=0') return { alert };
  if (p === '/customer/shows') return { shows: [{ id: 'cs-1', display_title: 'Casefile' }] };
  if (p === '/customer/brands') return { brands: BRANDS };
  if (p === '/customer/research-query' && previewResult) return { result: previewResult };
  throw new Error('unexpected ' + p);
};
async function openCreate(previewResult) {
  const t = await app('#/alerts');
  t.win.EW.api = dialogApi(null, previewResult);
  await t.act('alerts-create');
  return { t, f: t.form('alert-form') };
}
async function openEdit(alert) {
  const t = await app('#/alert/' + alert.id);
  t.win.EW.api = dialogApi(alert);
  await t.act('edit-alert', { id: String(alert.id) });
  return { t, f: t.form('alert-form') };
}
// The query the submit would send: the shared alertScopeFromDraft path.
const draftQuery = (t, f) => {
  const fd = new t.win.FormData(f);
  return t.win.EWViews.alertScopeFromDraft({ mode: fd.get('mode'), q: fd.get('q'), brand: fd.get('brand'), show: fd.get('show'),
    status: fd.get('status'), audio: fd.get('audio'), from: fd.get('from'), to: fd.get('to') },
  fd.get('initial_query'), fd.get('initial_selector'), fd.get('touched_fields')).query;
};
const tracks = f => f.querySelectorAll('#alert-track input[name=event_types]').map(b => b.value);
const modeNote = f => (f.querySelector('#alert-mode-note') || { textContent: '' }).textContent;

/* -- VL-01: brand-only alerts default to Ads -------------------------------- */
test('VL-01 a brand with no words submits mode ads; the select and the note say so', async () => {
  const { t, f } = await openCreate();
  t.type(f.querySelector('#alert-name'), 'Shopify');
  t.type(f.querySelector('#alert-brand'), 'shopify');
  assert.equal(draftQuery(t, f).mode, 'ads');
  assert.equal(JSON.stringify(draftQuery(t, f).brands), '["shopify"]');
  assert.equal(f.querySelector('#alert-mode').value, 'ads', 'Look in shows Ads');
  assert.equal(modeNote(f), 'With no words, a brand alert watches ads named Shopify.');
  assert.deepEqual(tracks(f), ['new_finding', 'offer_difference'], 'the Ads change types');
  assert.match(f.querySelector('#alert-scope').textContent, /^This alert watches: Ads · brand Shopify\.$/);
});

test('VL-01 words plus a brand keep All mentions; clearing the words goes back to Ads', async () => {
  const { t, f } = await openCreate();
  t.type(f.querySelector('#alert-brand'), 'shopify');
  t.type(f.querySelector('#alert-q'), 'promo code');
  assert.equal(draftQuery(t, f).mode, 'mentions');
  assert.equal(f.querySelector('#alert-mode').value, 'mentions');
  assert.equal(modeNote(f), '');
  assert.deepEqual(tracks(f), ['new_text', 'new_finding', 'offer_difference']);
  t.type(f.querySelector('#alert-q'), '');
  assert.equal(draftQuery(t, f).mode, 'ads');
});

test('VL-01 an explicit Look in choice wins over the default', async () => {
  const { t, f } = await openCreate();
  t.pick(f.querySelector('#alert-mode'), 'mentions');
  t.type(f.querySelector('#alert-brand'), 'shopify');
  assert.equal(draftQuery(t, f).mode, 'mentions');
  assert.equal(f.querySelector('#alert-mode').value, 'mentions');
});

test('VL-01 a stored brand-only All mentions alert is never rewritten silently', async () => {
  const stored = { id: 9, name: 'Old Shopify', revision: 3, event_types: ['new_text', 'new_finding'],
    query: { version: 2, mode: 'mentions', q: '', brands: ['shopify'] } };
  const { t, f } = await openEdit(stored);
  assert.equal(f.querySelector('#alert-mode').value, 'mentions');
  t.type(f.querySelector('#alert-name'), 'Old Shopify renamed');
  assert.equal(draftQuery(t, f).mode, 'mentions', 'a name edit keeps the stored mode');
  const again = await openEdit(stored);
  again.t.type(again.f.querySelector('#alert-brand'), 'kachava');
  assert.equal(draftQuery(again.t, again.f).mode, 'ads', 'touching the brand applies the default');
  assert.equal(modeNote(again.f), 'With no words, a brand alert watches ads named Ka’Chava.');
});

test('VL-01 pure: alertScopeFromDraft applies the default only on a brand or words touch', () => {
  const win = loadPages('#/alerts');
  const run = (vals, initial, touched) => win.EWViews.alertScopeFromDraft(vals, JSON.stringify(initial), 'null', JSON.stringify(touched)).query.mode;
  const base = { version: 2, mode: 'mentions', q: '', brands: null };
  assert.equal(run({ mode: 'mentions', q: '', brand: 'acme' }, base, ['brand']), 'ads');
  assert.equal(run({ mode: 'mentions', q: 'x', brand: 'acme' }, base, ['brand', 'q']), 'mentions');
  assert.equal(run({ mode: 'mentions', q: '', brand: 'acme' }, base, ['brand', 'mode']), 'mentions');
  assert.equal(run({ mode: 'mentions', q: '', brand: '' }, { ...base, brands: ['acme'] }, ['name']), 'mentions');
  assert.equal(run({ mode: 'episodes', q: '', brand: 'acme' }, { ...base, mode: 'episodes' }, ['brand']), 'episodes', 'only All mentions moves');
});

/* -- VL-01 / VS-12 / F-8: one total per alert card -------------------------- */
test('VL-01 / DEMO-SOL-R1-10 a card with count 691 and tracked 692 shows Search\'s number and the ads apart', async () => {
  const { html } = await alertPage({ ...ALERT, query: { version: 2, mode: 'mentions', q: '', brands: ['shopify'] },
    current_matches: { count: 691, tracked: 692, breaks: 1, tracked_by_kind: { new_text: 691, new_finding: 1 }, held_for_brand_review: 0 } });
  assert.deepEqual(bigNumbers(html), ['691']);
  assert.match(asideOf(html), /In stored records: 691 matching passages, as Search counts them\. It also watches 1 ad in the same scope\./);
  assert.doesNotMatch(asideOf(html), /692|records? match|This alert tracks|ad records/);
});

test('F-8 an ads alert counts ads; the held line stays', async () => {
  const { html } = await alertPage({ ...ALERT, current_matches: { count: 48, tracked: 40, breaks: 34, tracked_by_kind: { new_finding: 40 }, held_for_brand_review: 14 } });
  assert.deepEqual(bigNumbers(html), ['34']);
  assert.match(asideOf(html), /In stored records: 34 ads\./);
  assert.match(asideOf(html), /14 more ads have Brand not identified and aren’t counted here\./);
});

// PLAIN-ADS (DEP7): held ads have "Brand not identified" and aren't counted here (singular and plural).
test('DEMO2 pass 6: one held ad (singular); several (plural)', async () => {
  const one = await alertPage({ ...ALERT, current_matches: { count: 3, tracked: 2, breaks: 2, tracked_by_kind: { new_finding: 2 }, held_for_brand_review: 1 } });
  assert.match(asideOf(one.html), /1 more ad has Brand not identified and isn’t counted here\./);
  assert.doesNotMatch(asideOf(one.html), /ads have|aren’t|brand review|confirmed/);
  const many = await alertPage({ ...ALERT, current_matches: { count: 3, tracked: 1, breaks: 1, tracked_by_kind: { new_finding: 1 }, held_for_brand_review: 2 } });
  assert.match(asideOf(many.html), /2 more ads have Brand not identified and aren’t counted here\./);
});

test('F-8 an older server without tracked says what the search count counts', async () => {
  const mentions = await alertPage({ ...ALERT, query: { version: 2, mode: 'mentions', q: 'promo code' }, current_matches: { count: 5 } });
  assert.deepEqual(bigNumbers(mentions.html), ['5']);
  assert.match(asideOf(mentions.html), /passages match this alert’s search today\./);
  const ads = await alertPage({ ...ALERT, current_matches: { count: 1 } });
  assert.deepEqual(bigNumbers(ads.html), [], 'no ad count from this server: no number');
  assert.match(asideOf(ads.html), /Ads match this alert’s search today; this server gives no ad count\./);
  assert.doesNotMatch(asideOf(ads.html), /\b1\b|\breads?\b/);
  assert.doesNotMatch(asideOf(ads.html) + asideOf(mentions.html), /records? match/);
});

/* -- VL-19: preview copy ---------------------------------------------------- */
test('VL-19 the preview says what it counts, never "shown"; mentions adds the ads note', async () => {
  const items = Array.from({ length: 5 }, () => ({ record_kind: 'transcript_passage' }));
  const { t, f } = await openCreate({ total: 5, total_relation: 'exact', items });
  t.type(f.querySelector('#alert-q'), 'promo code');
  await t.act('alert-preview');
  const text = f.querySelector('#alert-preview').textContent;
  assert.match(text, /^Preview: 5 matching passages\. Ads that match the same words are tracked too\./);
  assert.doesNotMatch(text, /shown/);
  const ads = await openCreate({ total: 1, break_total: 1, total_relation: 'exact', items: [{ record_kind: 'evidence' }] });
  ads.t.type(ads.f.querySelector('#alert-brand'), 'shopify');
  await ads.t.act('alert-preview');
  const adsText = ads.f.querySelector('#alert-preview').textContent;
  assert.match(adsText, /^Preview: 1 matching ad\./);
  assert.doesNotMatch(adsText, /tracked too|shown/);
  const capped = await openCreate({ total: 50, total_relation: 'lower_bound', items });
  await capped.t.act('alert-preview');
  assert.match(capped.f.querySelector('#alert-preview').textContent, /^Preview: at least 50 matching passages\./, 'a capped count stays capped');
});

/* -- F-15 / VS-22 / V-23: the dialog --------------------------------------- */
test('F-15 the summary is pinned in the footer above the buttons; option "Ads"; plain copy', async () => {
  const { f } = await openCreate();
  const footer = f.querySelector('.dialog-footer');
  const scope = footer.querySelector('#alert-scope');
  assert.ok(scope, 'the summary sits in the footer');
  assert.equal(footer.children[0], scope, 'first row, above the buttons');
  const options = f.querySelector('#alert-mode').options.map(o => o.textContent);
  assert.deepEqual(options, ['All mentions', 'Ads', 'Episodes']);
  assert.equal(f.querySelector('#alert-preview').textContent, 'Preview matches to see what this alert would catch.');
  assert.match(f.textContent, /New matches show on the alert.s page\./);
  assert.doesNotMatch(f.textContent, /inbox|Ad records/);
});

test('V-23 the footer summary takes a full row (CSS)', () => {
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  assert.match(css, /\.dialog-footer > \.ew-alert-scope \{[^}]*flex: 1 1 100%/);
});

/* -- V-17 / VL-08: scope words, not keys ------------------------------------ */
test('V-17 the dialog scope line names the brand from the loaded brands; VL-08 labels status ("Ads", PLAIN-ADS) and audio', async () => {
  const stored = { id: 4, name: 'K', revision: 1, event_types: ['new_finding'],
    query: { version: 2, mode: 'ads', q: '', brands: ['kachava'], statuses: ['finding'], audio: ['available'] } };
  const { f } = await openEdit(stored);
  assert.equal(f.querySelector('#alert-scope').textContent, 'This alert watches: Ads · brand Ka’Chava · status Ads · audio Recording known.');
});

test('VL-08 the hub and the alert page label status and audio values', async () => {
  const query = { version: 2, mode: 'ads', q: '', brands: ['shopify'], statuses: ['finding'], audio: ['available'] };
  const { html } = await pageHtml('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [{ ...ALERT, query }] };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: BRANDS };
    throw new Error('unexpected ' + p);
  });
  assert.match(html, /Ads · brand Shopify · All shows · status: Ads · audio: Recording known/);
  assert.doesNotMatch(html, /· finding|· available/);
  const page = await alertPage({ ...ALERT, query });
  assert.match(page.html, /status: Ads · audio: Recording known/);
});

/* -- VL-14 ------------------------------------------------------------------ */
const SUM = { record_kind: 'evidence', status: 'finding', episode_id: 'ep1', show_title: 'S', ep_title: 'E', pub_date: '2026-10-09',
  span: [10, 40], text: 'words', read: { brand: { key: 'shopify', display: 'Shopify', confirmed: true }, needs_review: false, historical: false } };
const ev = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: false,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM, ...over });
test('VL-14 imported matches say when they were added; an unchecked revision heads "Last check (earlier settings)"', async () => {
  const { html } = await alertPage({ ...ALERT, unread_event_count: 1, events: [ev(1, { historical: true })] });
  assert.match(html, /Earlier matches — already in the collection when this alert was created\. Added on [^<]+\./);
  assert.doesNotMatch(html, /Earlier matches included/);
  const unchecked = await alertPage({ ...ALERT, definition_revision: 2, last_successful_definition_revision: 1, unread_event_count: 1,
    events: [ev(1, { definition_revision: 1, checked_at: T1 })] });
  assert.match(unchecked.html, /<h3 class="ew-alert-subhead">Last check \(earlier settings\) · /);
  assert.doesNotMatch(unchecked.html, /Latest check ·/);
  const checked = await alertPage({ ...ALERT, last_successful_definition_revision: 1, unread_event_count: 1, events: [ev(1, { definition_revision: 1, checked_at: T1 })] });
  assert.match(checked.html, /<h3 class="ew-alert-subhead">Latest check · /);
});

/* -- VL-16: Overview ranks active alerts first ------------------------------ */
test('VL-16 a paused alert with newer events sorts after the active one on Overview', async () => {
  const total = (id, name, lifecycle, at) => ({ alert_id: id, alert_name: name, lifecycle, events: 3, unread: 3,
    query: { version: 2, mode: 'mentions', q: 'x' }, latest_kind: 'new_text', latest_observed_at: at });
  const { html } = await pageHtml('#/overview', p => {
    if (p === '/customer/overview') return { coverage: {}, recent_events: [{ alert_id: 1, alert_name: 'Paused one', kind: 'new_text', read_state: 0, observed_at: '2026-10-15T10:00:00Z' }], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [],
      alert_totals: [total(1, 'Paused one', 'paused', '2026-10-15T10:00:00Z'), total(2, 'Stopped one', 'stopped', '2026-10-15T09:00:00Z'),
        total(3, 'Active one', 'active_local', '2026-10-14T10:00:00Z')] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    if (p === '/customer/brands') return { brands: [] };
    if (p.startsWith('/customer/search')) return { items: [] };
    throw new Error('unexpected ' + p);
  });
  const at = n => html.indexOf('>' + n + '<');
  assert.ok(at('Active one') > 0 && at('Active one') < at('Paused one') && at('Paused one') < at('Stopped one'));
});

/* -- VL-17 / F-16 / V-22: banner and empty states --------------------------- */
test('VL-17 the "Saved" banner shows once, only on an active alert', async () => {
  const saved = win => { win.EW.current.savedAlert = '5'; };
  const first = await alertPage(ALERT, saved);
  assert.match(first.html, /Saved\. New matches show here after/);
  assert.doesNotMatch(await first.rerender(), /Saved\. New matches show here after/, 'cleared after the first render');
  const stopped = await alertPage({ ...ALERT, lifecycle: 'stopped' }, saved);
  assert.doesNotMatch(stopped.html, /Saved\. New matches show here after/);
});

test('VL-17 / F-16 empty states never point at a disabled Check local records', async () => {
  const active = await alertPage(ALERT);
  assert.match(active.html, /Current matches are your starting point\. Press Check local records later to see new ones\./);
  assert.doesNotMatch(active.html, /alongside|starting baseline/);
  const stopped = await alertPage({ ...ALERT, lifecycle: 'stopped' });
  assert.match(stopped.html, /Stopped — no further checks\. History is kept\./);
  assert.doesNotMatch(stopped.html, /Press Check local records/);
  const paused = await alertPage({ ...ALERT, lifecycle: 'paused' });
  assert.doesNotMatch(paused.html, /Press Check local records/);
});

/* -- "Brand not identified" on an alert change ------------------------------ */
test('VS-01 an alert change whose read has no brand says "Brand not identified"; neutral leads stay', async () => {
  const none = { ...SUM, read: { brand: { key: null, display: null, confirmed: false }, needs_review: false, historical: false } };
  const { html } = await alertPage({ ...ALERT, unread_event_count: 3, events: [ev(1, { summary: none }),
    ev(2, { summary: { ...SUM, episode_id: 'ep2', read: null } }), ev(3, { summary: { ...SUM, episode_id: 'ep3', read: { ...SUM.read, historical: true } } })] });
  assert.match(html, /<strong>Brand not identified<\/strong>/);
  assert.doesNotMatch(html, /Brand not named/);
  assert.match(html, /<strong>Ad record<\/strong>/);
  assert.match(html, /<strong>Earlier version<\/strong>/);
});

/* -- VL-36: hub columns line up across rows --------------------------------- */
test('VL-36 hub rows share one column grid on wide screens (subgrid), phone rule unchanged', () => {
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  assert.match(css, /\.ew-work-alerts \{[^}]*display: grid;[^}]*grid-template-columns: minmax\(0, 1fr\) 170px 240px auto/);
  assert.match(css, /\.ew-work-alert \{[^}]*grid-column: 1 \/ -1;[^}]*grid-template-columns: subgrid/);
  const narrow = css.slice(css.indexOf('@media (max-width: 1199px)'));
  assert.match(narrow, /\.ew-work-alerts \{ display: block; \}/);
});

/* -- DEMO2 (Sol DEMO-SOL-R1-05, 10, 12): seeded history is history ----------- */
const pastEv = (id, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:p' + id, read_state: 1, historical: 1,
  ad_id: 'ad' + id, observed_at: '2026-10-14T16:27:59+00:00', ...over });
const MUSE_PAST = [...Array.from({ length: 20 }, (_, i) => pastEv(i + 1, { kind: 'new_text', ad_id: null })),
  pastEv(21), pastEv(22), pastEv(23)];
const MUSE = { ...ALERT, id: 7, name: 'Meta’s Muse mentions', query: { version: 2, mode: 'mentions', q: '"Meta\'s Muse"' },
  unread_event_count: 0, unread_ad_count: 0, events: MUSE_PAST,
  current_matches: { count: 20, tracked: 23, breaks: 3, tracked_by_kind: { new_text: 20, new_finding: 3 }, held_for_brand_review: 0 } };
const QUINCE = { ...ALERT, id: 8, name: 'Quince possible ads', unread_event_count: 0, unread_ad_count: 0,
  events: Array.from({ length: 4 }, (_, i) => pastEv(100 + i)) };

test('DEMO-SOL-R1-05 hub: read history shows past matches, never new; New matches · 0 is shown', async () => {
  const { html } = await pageHtml('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [MUSE, QUINCE] };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: BRANDS };
    throw new Error('unexpected ' + p);
  });
  assert.match(html, /data-tab="new" aria-pressed="false"[^>]*>New matches · 0</);
  assert.doesNotMatch(html, /\d+ new<|ew-work-new|unread/);
  assert.match(html, /4 matches/);
  assert.match(html, /23 matches when it was made \(Oct 14\) · 20 passages, 3 ads/); // RJW-01: dated by the imported matches
});

test('DEMO-SOL-R1-05 detail: Current matches, In stored records, 0 unread, history worded as history', async () => {
  const { html } = await alertPage({ ...QUINCE, current_matches: { count: 4, tracked: 4, breaks: 4, tracked_by_kind: { new_finding: 4 }, held_for_brand_review: 0 } });
  assert.match(html, /<h3>Current matches<\/h3>/);
  assert.doesNotMatch(html, /Matching today|Tracked today/);
  assert.match(html, /In stored records: 4 ads\./);
  assert.match(html, /<strong>0<\/strong> unread/);
  assert.match(html, /4 matches/);
  assert.match(html, /Earlier matches — already in the collection when this alert was created\./);
  assert.doesNotMatch(html, /data-tag="info"[^>]*>New</, 'no New badge on a read historical row');
});

test('DEMO-SOL-R1-10 a mentions alert counts its current matches as Search does; ads are apart', async () => {
  const { html } = await alertPage(MUSE);
  const aside = html.slice(html.indexOf('<h3>Current matches</h3>'), html.indexOf('<h3>Check local records</h3>'));
  assert.deepEqual([...aside.matchAll(/<p class="ew-work-big">([^<]*)<\/p>/g)].map(m => m[1]), ['20']);
  assert.match(aside, /In stored records: 20 matching passages, as Search counts them\./);
  assert.match(aside, /It also watches 3 ads in the same scope\./);
  assert.match(html, /23 matches when it was made \(Oct 14\) · 20 passages, 3 ads/); // RJW-01: dated by the imported matches
});

test('DEMO-SOL-R1-12 phone: Changes come before the summary; desktop keeps two columns', async () => {
  const { html } = await alertPage({ ...QUINCE });
  assert.ok(html.indexOf('<h2>Changes</h2>') < html.indexOf('<aside'), 'Changes first in the DOM');
  const css = fs.readFileSync(path.join(APP, 'work.css'), 'utf8');
  assert.match(css, /\.ew-alert-split\s*\{\s*grid-template-areas:\s*"main aside";/);
  const narrow = css.slice(css.indexOf('@media (max-width: 1023px)'));
  assert.match(narrow.slice(0, narrow.indexOf('\n}')), /\.ew-alert-split\s*\{\s*grid-template-areas:\s*"main" "aside";/);
});

test('DEMO2-R1-06 only historical events count as past matches', async () => {
  const mixed = { ...QUINCE, id: 9, name: 'Mixed', unread_event_count: 1, unread_ad_count: 1,
    events: [pastEv(201), pastEv(202), { ...pastEv(203), historical: 0, read_state: 0 }] };
  const { html } = await pageHtml('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts: [mixed] };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: BRANDS };
    throw new Error('unexpected ' + p);
  });
  assert.match(html, /2 matches/);
  assert.doesNotMatch(html, /3 matches/);
  assert.match(html, /1 new</);
});

/* -- JE-WORDS (UX D48, ws-ux/redesign/WORDS.md): the past count was taken once,
   when the alert was made, so it never reads as today's count. {date} is the
   alert's created_at in the app's day form (EW.formatDay); no created date (or
   one that is not a timestamp): "{n} matches" with no date phrase. ---------- */
const MADE = '2026-10-14T12:00:00+00:00';
const listApi = alerts => p => {
  if (p === '/customer/alerts') return { alerts };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: BRANDS };
  throw new Error('unexpected ' + p);
};
const pastLines = html => [...html.matchAll(/<span class="ew-work-meta ew-work-past">([^<]*)<\/span>/g)].map(m => m[1]).sort();

test('JE-WORDS D48 Alerts list: "{n} matches when it was made ({date})", singular, no date phrase without a created date', async () => {
  const { html } = await pageHtml('#/alerts', listApi([
    { ...QUINCE, created_at: MADE },
    { ...QUINCE, id: 10, name: 'One past', created_at: MADE, events: [pastEv(300)] },
    { ...QUINCE, id: 11, name: 'No date', events: [pastEv(301, { observed_at: null }), pastEv(302, { observed_at: null })] },
    { ...QUINCE, id: 12, name: 'Bad date', created_at: 'not a date', events: [303, 304, 305].map(i => pastEv(i, { observed_at: 'not a date' })) },
    { ...MUSE, id: 13, created_at: MADE },
    { ...QUINCE, id: 14, name: 'Broken stamp', created_at: '2026-13-45T25:61:00Z', events: [5, 6, 7, 8, 9].map(i => pastEv(400 + i, { observed_at: '2026-13-45T25:61:00Z' })) }]));
  assert.deepEqual(pastLines(html), [
    '1 match when it was made (Oct 14)',
    '2 matches',
    '23 matches when it was made (Oct 14) · 20 passages, 3 ads',
    '3 matches',
    '4 matches when it was made (Oct 14)',
    '5 matches']);
  assert.doesNotMatch(html, /earlier match|1 matches|\(\)|Date unknown|Invalid date/);
});

// RJW-01 (lead 11-01): the count is taken when the earlier matches are imported, which can be after
// the alert was made, so the date is the earliest imported match's; created_at only when no match has one.
test('RJW-01: the D48 date is the earliest imported match, not created_at', async () => {
  const { html } = await pageHtml('#/alerts', listApi([
    { ...QUINCE, id: 20, name: 'Imported later', created_at: '2026-10-10T09:00:00+00:00',
      events: [pastEv(500, { observed_at: '2026-10-16T08:00:00+00:00' }), pastEv(501, { observed_at: '2026-10-14T12:00:00+00:00' })] },
    { ...QUINCE, id: 21, name: 'No import time', created_at: '2026-10-10T09:00:00+00:00', events: [pastEv(502, { observed_at: null })] }]));
  assert.deepEqual(pastLines(html).sort(), ['1 match when it was made (Oct 10)', '2 matches when it was made (Oct 14)']);
  // RJF-02: a live (not imported) match older than the import never dates the line; an empty or broken
  // stamp beside a valid one is skipped.
  const mixed = (await pageHtml('#/alerts', listApi([
    { ...QUINCE, id: 22, name: 'Live older', created_at: '2026-10-10T09:00:00+00:00',
      events: [pastEv(510, { historical: 0, observed_at: '2026-10-11T12:00:00+00:00' }), pastEv(511, { observed_at: '2026-10-16T12:00:00+00:00' })] },
    { ...QUINCE, id: 23, name: 'Mixed stamps', created_at: '2026-10-10T09:00:00+00:00',
      events: [pastEv(514, { observed_at: '2026-10-15T12:00:00+00:00' }), pastEv(512, { observed_at: '' }), pastEv(513, { observed_at: '2026-13-45T25:61:00Z' })] }]))).html;
  assert.deepEqual(pastLines(mixed).sort(), ['1 match when it was made (Oct 16)', '3 matches when it was made (Oct 15)']);
});

test('JE-WORDS D48 the alert page header says the same words', async () => {
  const dated = (await alertPage({ ...QUINCE, created_at: MADE })).html;
  assert.match(dated, /<p class="page-head-meta">[^]*?<span>4 matches when it was made \(Oct 14\)<\/span>/);
  const one = (await alertPage({ ...QUINCE, created_at: MADE, events: [pastEv(300)] })).html;
  assert.match(one, /<span>1 match when it was made \(Oct 14\)<\/span>/);
  const undated = (await alertPage({ ...QUINCE, events: QUINCE.events.map(e => ({ ...e, observed_at: null })) })).html;
  assert.match(undated, /<span>4 matches<\/span>/);
  // RJW-03: the singular with no created date is "1 match", whole span.
  const oneUndated = (await alertPage({ ...QUINCE, events: [pastEv(300, { observed_at: null })] })).html;
  assert.match(oneUndated, /<span>1 match<\/span>/);
  const meta = h => /<p class="page-head-meta">([^]*?)<\/p>/.exec(h)[1];
  assert.doesNotMatch(meta(dated) + meta(one) + meta(undated), /earlier|when it was made \(\)/);
});
