/* Astra UX-09, Reno decision 13 (Option A): a changed dialog closed
   without saving keeps its entry for this page session (memory only).
   Reopening the same dialog restores it as an unsaved draft with a notice
   and "Discard it"; an entry typed against an earlier record revision is
   not restored; a save clears it; untouched dialogs close at once.
   The real openers run against a small structural fake DOM.
   Run: node --test tests/frontend_dv3_astra_drafts.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

/* -- fake DOM: enough structure for forms, controls and events ------------ */
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
const KEPT = 'Not saved. Your entry is kept — reopen it to continue.';
const EARLIER = 'Your unsaved entry was for an earlier version of this record, so it was not restored.';
const notice = f => f.querySelector('[data-restored-draft]');

/* -- Save excerpt: the basic contract -------------------------------------- */
test('UX-09 Escape on a changed Save excerpt keeps the entry; reopening restores it as an unsaved draft', async () => {
  const t = await app();
  t.act('save-excerpt', { ref: 'sr1:a' });
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'draft title');
  t.type(f.querySelector('#excerpt-note'), 'my note');
  t.escape();
  assert.equal(t.dom.modal.open, false, 'Escape still closes the dialog');
  assert.deepEqual(t.toasts, [KEPT]);
  assert.equal(t.win.EW.current.dialogDrafts.size, 1);
  t.act('save-excerpt', { ref: 'sr1:a' });
  const g = t.form('save-excerpt');
  assert.notEqual(g, f, 'a fresh form');
  assert.equal(g.querySelector('#excerpt-title').value, 'draft title');
  assert.equal(g.querySelector('#excerpt-note').value, 'my note');
  assert.equal(g.dataset.dirty, '1', 'restored as an unsaved draft');
  const n = notice(g);
  assert.ok(n, 'restored notice shown');
  assert.equal(n.getAttribute('role'), 'status');
  assert.match(n.textContent, /Restored your unsaved entry\. It is not saved yet\./);
  assert.ok(n.querySelector('[data-action="discard-dialog-draft"]'), 'Discard it offered');
  assert.equal(n.querySelector('[data-action="discard-dialog-draft"]').textContent, 'Discard it');
  assert.equal(t.win.EW.current.dialogDrafts.size, 0, 'the entry left memory once restored');
});

test('UX-09 the Close button and Cancel keep the entry the same way', async () => {
  for (const sel of ['.dialog-head [data-action="close-dialog"]', '.dialog-footer [data-action="close-dialog"]']) {
    const t = await app();
    t.act('save-excerpt', { ref: 'sr1:a' });
    t.type(t.form('save-excerpt').querySelector('#excerpt-title'), 'typed');
    assert.ok(t.dom.modal.querySelector(sel), sel);
    await t.act('close-dialog');
    assert.equal(t.dom.modal.open, false);
    assert.deepEqual(t.toasts, [KEPT], sel);
    t.act('save-excerpt', { ref: 'sr1:a' });
    assert.equal(t.form('save-excerpt').querySelector('#excerpt-title').value, 'typed');
  }
});

test('UX-09 an untouched or reverted dialog closes at once: no entry kept, no toast', async () => {
  const t = await app();
  t.act('save-excerpt', { ref: 'sr1:a' });
  t.escape();
  assert.equal(t.dom.modal.open, false);
  t.act('save-excerpt', { ref: 'sr1:a' });
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'x');
  t.type(f.querySelector('#excerpt-title'), '');
  t.escape();
  assert.deepEqual(t.toasts, []);
  assert.equal(t.win.EW.current.dialogDrafts.size, 0);
  t.act('save-excerpt', { ref: 'sr1:a' });
  assert.equal(notice(t.form('save-excerpt')), null);
});

test('UX-09 an entry belongs to its own record: another excerpt does not get it', async () => {
  const t = await app();
  t.act('save-excerpt', { ref: 'sr1:a' });
  t.type(t.form('save-excerpt').querySelector('#excerpt-title'), 'for a');
  t.escape();
  t.act('save-excerpt', { ref: 'sr1:b' });
  assert.equal(t.form('save-excerpt').querySelector('#excerpt-title').value, '');
  assert.equal(notice(t.form('save-excerpt')), null);
  t.escape();
  t.act('save-excerpt', { ref: 'sr1:a' });
  assert.equal(t.form('save-excerpt').querySelector('#excerpt-title').value, 'for a');
});

test('UX-09 Discard it returns the dialog to what it opened with and drops the entry', async () => {
  const t = await app();
  t.act('save-excerpt', { ref: 'sr1:a' });
  t.type(t.form('save-excerpt').querySelector('#excerpt-title'), 'unwanted');
  t.escape();
  t.act('save-excerpt', { ref: 'sr1:a' });
  const g = t.form('save-excerpt');
  const btn = notice(g).querySelector('[data-action="discard-dialog-draft"]');
  await t.win.EWViews.handleAction(btn);
  assert.equal(g.querySelector('#excerpt-title').value, '');
  assert.equal(g.dataset.dirty, undefined, 'no longer a draft');
  assert.equal(notice(g), null, 'notice removed');
  t.toasts.length = 0;
  t.escape();
  assert.deepEqual(t.toasts, [], 'a discarded entry closes silently');
  assert.equal(t.win.EW.current.dialogDrafts.size, 0);
});

test('UX-09 a successful save clears a kept entry for the same dialog', async () => {
  const t = await app();
  t.act('save-excerpt', { ref: 'sr1:a' });
  t.type(t.form('save-excerpt').querySelector('#excerpt-title'), 'kept');
  t.escape();
  const drafts = t.win.EW.current.dialogDrafts;
  const kept = [...drafts];
  // Reopen without the restore, then put the entry back: the save must
  // clear it (no stale entry comes back after saving).
  drafts.clear();
  t.act('save-excerpt', { ref: 'sr1:a' });
  for (const [k, v] of kept) drafts.set(k, v);
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'saved title');
  const posts = [];
  t.win.EW.api = async (p, o) => { posts.push(p); return { item: { id: 9, revision: 1 } }; };
  t.win.EW.render = async () => {};
  await t.win.EWViews.handleSubmit(f);
  assert.deepEqual(posts, ['/customer/saved-items']);
  assert.equal(drafts.size, 0, 'saved: the kept entry is gone');
  t.act('save-excerpt', { ref: 'sr1:a' });
  assert.equal(t.form('save-excerpt').querySelector('#excerpt-title').value, '');
});

test('UX-09 a workspace clear drops kept entries; nothing is written to localStorage', async () => {
  const t = await app();
  const writes = [];
  t.win.localStorage.setItem = (k) => writes.push(k);
  t.act('save-excerpt', { ref: 'sr1:a' });
  t.type(t.form('save-excerpt').querySelector('#excerpt-title'), 'typed');
  t.escape();
  assert.equal(t.win.EW.current.dialogDrafts.size, 1);
  assert.deepEqual(writes, [], 'memory only');
  t.win.EW.clearWorkspaceState();
  assert.equal(t.win.EW.current.dialogDrafts.size, 0);
});

/* -- Personal note: an edit with a revision -------------------------------- */
const noteApi = rev => async p => {
  if (/\/mistake$/.test(p)) return { mistakes: [{ id: 4, revision: rev, reason: 'stored note', status: 'open' }] };
  throw new Error('unexpected ' + p);
};
test('UX-09 Add personal note: a changed note comes back on reopen', async () => {
  const t = await app('#/evidence/sr1:a');
  t.win.EW.api = async p => { if (/\/mistake$/.test(p)) return { mistakes: [] }; throw new Error('unexpected ' + p); };
  await t.act('report-mistake', { id: 'sr1:a' });
  const f = t.form('mistake');
  t.type(f.querySelector('#mistake-reason'), 'wrong brand');
  t.pick(f.querySelector('#mistake-status'), 'acknowledged');
  t.escape();
  assert.deepEqual(t.toasts, [KEPT]);
  await t.act('report-mistake', { id: 'sr1:a' });
  const g = t.form('mistake');
  assert.equal(g.querySelector('#mistake-reason').value, 'wrong brand');
  assert.equal(g.querySelector('#mistake-status').value, 'acknowledged');
  assert.ok(notice(g));
});

test('UX-09 an entry typed against an earlier revision is not restored, and says so', async () => {
  const t = await app('#/evidence/sr1:a');
  t.win.EW.api = noteApi(3);
  await t.act('report-mistake', { id: 'sr1:a' });
  t.type(t.form('mistake').querySelector('#mistake-reason'), 'my edit');
  t.escape();
  // The note changed elsewhere meanwhile.
  t.win.EW.api = noteApi(4);
  t.toasts.length = 0;
  await t.act('report-mistake', { id: 'sr1:a' });
  const g = t.form('mistake');
  assert.equal(g.querySelector('#mistake-reason').value, 'stored note', 'the current record, not the old entry');
  assert.equal(notice(g), null);
  assert.equal(g.dataset.dirty, undefined);
  assert.deepEqual(t.toasts, [EARLIER]);
  assert.equal(t.win.EW.current.dialogDrafts.size, 0, 'the old entry is dropped');
  // Same revision: restored.
  t.type(g.querySelector('#mistake-reason'), 'second edit');
  t.escape();
  await t.act('report-mistake', { id: 'sr1:a' });
  assert.equal(t.form('mistake').querySelector('#mistake-reason').value, 'second edit');
});

/* -- Create alert: a restored Look in rebuilds what it shapes --------------- */
const alertApi = async p => { if (p === '/customer/shows') return { shows: [] }; throw new Error('unexpected ' + p); };
const tracked = f => f.querySelectorAll('#alert-track input[name=event_types]').map(b => `${b.value}${b.checked ? '+' : ''}`);
test('UX-09 Create alert: a restored mode and track choice come back with the matching choices and scope', async () => {
  const t = await app();
  t.win.EW.api = alertApi;
  await t.act('alerts-create');
  const f = t.form('alert-form');
  assert.deepEqual(tracked(f), ['new_text+', 'new_finding+', 'offer_difference+']);
  t.type(f.querySelector('#alert-name'), 'Trial ads');
  t.pick(f.querySelector('#alert-mode'), 'ads');
  assert.deepEqual(tracked(f), ['new_finding+', 'offer_difference+']);
  check(f.querySelector('#alert-track input[value=offer_difference]'), false);
  t.type(f.querySelector('#alert-q'), 'free trial');
  const touched = JSON.parse(f.querySelector('#alert-touched').value);
  t.escape();
  assert.deepEqual(t.toasts, [KEPT]);
  await t.act('alerts-create');
  const g = t.form('alert-form');
  assert.equal(g.querySelector('#alert-name').value, 'Trial ads');
  assert.equal(g.querySelector('#alert-mode').value, 'ads');
  assert.equal(g.querySelector('#alert-q').value, 'free trial');
  assert.deepEqual(tracked(g), ['new_finding+', 'offer_difference'], 'ads choices, the unchecked one stays unchecked');
  assert.deepEqual(JSON.parse(g.querySelector('#alert-touched').value).sort(), touched.sort(), 'touched fields as typed');
  assert.match(g.querySelector('#alert-scope').textContent, /^This alert watches: Ads · matching “free trial”\.$/);
  assert.ok(notice(g));
  // A later edit still records its own touch on top of the restored ones.
  t.type(g.querySelector('#alert-brand'), 'Acme');
  assert.ok(JSON.parse(g.querySelector('#alert-touched').value).includes('brand'));
  assert.ok(JSON.parse(g.querySelector('#alert-touched').value).includes('mode'));
  // Discard it: back to the opening mode and its own choices.
  await t.win.EWViews.handleAction(notice(g).querySelector('[data-action="discard-dialog-draft"]'));
  assert.equal(g.querySelector('#alert-mode').value, 'mentions');
  assert.deepEqual(tracked(g), ['new_text+', 'new_finding+', 'offer_difference+']);
  assert.equal(g.querySelector('#alert-name').value, '');
  assert.deepEqual(JSON.parse(g.querySelector('#alert-touched').value), []);
  assert.equal(g.dataset.dirty, undefined);
  function check(el, on) { t.check(el, on); }
});

/* -- Start a campaign: a restored campaign type rebuilds its fields -------- */
test('UX-09 Start a campaign: a restored Supplied requirements type brings back its message field', async () => {
  const t = await app('#/campaigns');
  t.win.EW.api = async p => { if (/^\/customer\/briefs/.test(p)) return { items: [] }; throw new Error('unexpected ' + p); };
  await t.act('create-campaign');
  const f = t.form('campaign');
  assert.ok(f, 'campaign dialog open');
  t.type(f.querySelector('#campaign-name'), 'Spring push');
  t.check(f.querySelector('input[name=mode][value=supplied_requirements]'), true);
  assert.ok(f.querySelector('#campaign-message'), 'requirement fields shown');
  t.type(f.querySelector('#campaign-message'), 'Say the code');
  t.escape();
  assert.deepEqual(t.toasts, [KEPT]);
  await t.act('create-campaign');
  const g = t.form('campaign');
  assert.equal(g.querySelector('#campaign-name').value, 'Spring push');
  assert.equal(g.querySelector('input[name=mode][value=supplied_requirements]').checked, true);
  assert.ok(g.querySelector('#campaign-message'), 'requirement fields rebuilt');
  assert.equal(g.querySelector('#campaign-message').value, 'Say the code');
  assert.ok(notice(g));
});

/* -- Other dialogs keep their Escape behaviour ----------------------------- */
test('UX-09 the keydown handler still closes every open dialog on Escape', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.match(src, /if \(dlg && dlg\.open\) \{ closeDialogByUser\(\{ escape: true \}\); return; \}/);
  assert.doesNotMatch(src, /localStorage\.setItem\([^)]*[Dd]raft/, 'drafts never go to storage');
});
