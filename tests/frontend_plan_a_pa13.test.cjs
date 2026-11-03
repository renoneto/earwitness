/* Plan A excerpt view, PA fix round 13: an alert's stored mode must not
   change silently. Dialog cases run the real opener and submit against the
   structural fake DOM of frontend_uat_alerts.test.cjs (copied).
   Run: node --test tests/frontend_plan_a_pa13.test.cjs */
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

/* -- PA13 ------------------------------------------------------------------- */
const mkAlert = (mode, extra = {}) => ({ id: 5, name: 'Everything Shopify', revision: 3, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, event_types: mode === 'ads' ? ['new_finding'] : ['new_text'], delivery_json: '{}',
  query: { version: 2, mode, q: '', brands: ['shopify'], ...extra } });
const dialogApi = alert => async p => {
  if (alert && p === '/customer/alerts/' + alert.id + '?summaries=0') return { alert };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'shopify', display: 'Shopify' }] };
  throw new Error('unexpected ' + p);
};
async function open(alert, { excerpt }) {
  const t = await app(alert ? '#/alert/' + alert.id : '#/alerts');
  if (excerpt) { t.win.EW.current.excerptOnly = true; t.win.EW.current.excerptOnlyContext = t.win.EW.current.workspaceContext || ''; }
  const calls = [];
  const api = dialogApi(alert);
  t.win.EW.api = async (p, o = {}) => { if (o.method) { calls.push({ path: p, ...o }); return { id: 5 }; } return api(p, o); };
  t.win.EW.go = () => {};
  await t.act(alert ? 'edit-alert' : 'alerts-create', alert ? { id: String(alert.id) } : {});
  return { t, f: t.form('alert-form'), calls };
}
const save = async ({ t, f }) => { await t.win.EWViews.handleSubmit(f); await new Promise(r => setTimeout(r, 0)); };
const FIXED = 'Look in: All mentions (not included in your plan)';
const fixedLine = f => { const el = f.querySelector('#alert-mode-fixed'); return el ? el.textContent : null; };

test('PA13 excerpt level: a stored mentions alert keeps its mode on a rename-only save', async () => {
  const d = await open(mkAlert('mentions'), { excerpt: true });
  assert.ok(d.f, 'dialog opened');
  d.t.type(d.f.querySelector('#alert-name'), 'Renamed');
  await save(d);
  assert.equal(d.calls.length, 1, 'one save request');
  assert.equal(d.calls[0].method, 'PUT');
  assert.equal(d.calls[0].body.query.mode, 'mentions');
  assert.equal(d.calls[0].body.name, 'Renamed');
});
test('PA13 excerpt level: a stored mentions alert shows the fixed Look in line, not the select', async () => {
  const d = await open(mkAlert('mentions'), { excerpt: true });
  assert.equal(d.f.querySelector('#alert-mode'), null, 'no select');
  assert.equal(fixedLine(d.f), FIXED);
});
test('PA13 excerpt level: editing the brand does not flip a stored mentions alert to ads', async () => {
  const d = await open(mkAlert('mentions'), { excerpt: true });
  d.t.type(d.f.querySelector('#alert-brand'), 'kachava');
  await save(d);
  assert.equal(d.calls[0].body.query.mode, 'mentions');
});
test('PA13 excerpt level: a new alert offers only Ads and Episodes and saves ads', async () => {
  const d = await open(null, { excerpt: true });
  const opts = d.f.querySelector('#alert-mode').options.map(o => o.value);
  assert.deepEqual(opts, ['ads', 'episodes']);
  assert.equal(fixedLine(d.f), null);
  d.t.type(d.f.querySelector('#alert-name'), 'New one');
  await save(d);
  assert.equal(d.calls[0].method, 'POST');
  assert.equal(d.calls[0].body.query.mode, 'ads');
});
test('PA13 excerpt level: a stored ads alert still shows the select', async () => {
  const d = await open(mkAlert('ads'), { excerpt: true });
  assert.deepEqual(d.f.querySelector('#alert-mode').options.map(o => o.value), ['ads', 'episodes']);
  assert.equal(fixedLine(d.f), null);
});
test('PA13 Plan B control: a mentions alert keeps the select with All mentions and saves mentions', async () => {
  const d = await open(mkAlert('mentions'), { excerpt: false });
  assert.deepEqual(d.f.querySelector('#alert-mode').options.map(o => o.value), ['mentions', 'ads', 'episodes']);
  assert.equal(d.f.querySelector('#alert-mode').value, 'mentions');
  assert.equal(fixedLine(d.f), null);
  d.t.type(d.f.querySelector('#alert-name'), 'Renamed');
  await save(d);
  assert.equal(d.calls[0].body.query.mode, 'mentions');
});
