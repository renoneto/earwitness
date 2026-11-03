/* WIN3-FIX3 (code r2 W3-R2-01): the brief registration state survives a
   dialog close and reopen. Cancel, Close and Escape keep the typed brief
   title and text as a draft; the reopened form gets the same
   registration state back, so it never registers that text again: in
   flight (nothing sent until it answers), no answer (held, no resend),
   already registered (that brief is reused). The same for Edit
   campaign's Save changes.
   The real openers, close and restore run against the structural fake
   DOM of frontend_dv3_astra_drafts.test.cjs (copied below).
   Run: node --test tests/frontend_win3_reopen.test.cjs */
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
      // WIN3-FIX3: as in a browser, an option's value property is its value attribute.
      if (this.tagName === 'OPTION') { this.attrs.value = String(v); return; }
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
const notice = f => f.querySelector('[data-restored-draft]');

/* -- the server: brief list, stored campaign 5, brief POSTs answered later -- */
const QUINCE = { brief_id: 'quince', current_version: 2, title: 'Quince podcast ads' };
const CAMP5 = { id: 5, revision: 3, mode: 'supplied_requirements', name: 'Judge test: Mint Mobile', brand: 'Mint Mobile',
  message: 'Wireless plans from $15 a month', offer: 'mintmobile.com/legends', notes: '',
  requirement_provenance: { brief_ref: { kind: 'supplied_brief', brief_id: 'quince', version: 2 } } };
// listed: false — the reopened dialog's brief list does not include the
// new brief (a long list, or the brief answered after the reopen read).
function server(t, { listed = true } = {}) {
  const s = { calls: [], briefs: [], added: [] };
  // No page render in these tests: the dialog is the subject.
  s.went = [];
  t.win.EW.go = r => { s.went.push(r); };
  t.win.EW.api = async (p, opts = {}) => {
    const method = opts.method || 'GET';
    s.calls.push([method, p, opts.body]);
    if (method === 'GET' && p === '/customer/briefs?q=&limit=20') return { items: [QUINCE, ...(listed ? s.added : [])] };
    if (method === 'GET' && p === '/customer/campaigns/5') return { campaign: CAMP5 };
    if (method === 'GET' && p === '/customer/briefs/quince/versions/2') return { brief_version: { title: QUINCE.title, provided_text: 'One Quince read.' } };
    if (method === 'POST' && p === '/customer/briefs') {
      return new Promise((res, rej) => s.briefs.push({ rej, res: out => {
        s.added.push({ brief_id: out.brief_id, current_version: out.version, title: opts.body.title }); res(out);
      } }));
    }
    if (method === 'POST' && p === '/customer/campaigns') return { campaign: { id: 9 } };
    if (method === 'PUT' && p === '/customer/campaigns/5') return { campaign: { id: 5 } };
    throw new Error('unexpected request ' + method + ' ' + p);
  };
  return s;
}
const writes = s => s.calls.filter(c => c[0] !== 'GET').map(c => `${c[0]} ${c[1]}`);
const briefPosts = s => writes(s).filter(w => w === 'POST /customer/briefs').length;
const flush = async (n = 20) => { for (let i = 0; i < n; i++) await new Promise(res => setTimeout(res, 0)); };
const MINT = { title: 'Mint Mobile Oct', text: 'One Mint Mobile host read.' };
const MINT_REF = { kind: 'supplied_brief', brief_id: 'mint', version: 1 };
const BUSY = 'This change is already being saved.';
const NO_ANSWER = /Your change could not be confirmed\. Reload the workspace to continue\./;
const noAnswer = () => Object.assign(new TypeError('Failed to fetch'), { outcomeUnknown: true });
const LATER = { timeout: 5000 };

const KINDS = [
  { kind: 'Start campaign', route: '#/campaigns', write: 'POST /customer/campaigns', went: '#/campaign/9',
    open: t => t.act('create-campaign'),
    fill: (t, f) => {
      t.type(f.querySelector('#campaign-name'), 'Judge test: Mint Mobile');
      t.check(f.querySelector('input[name=mode][value=supplied_requirements]'), true);
      t.type(f.querySelector('#campaign-message'), 'Wireless plans from $15 a month');
    } },
  { kind: 'Save changes', route: '#/campaign/5', write: 'PUT /customer/campaigns/5', went: '#/campaign/5',
    open: t => t.act('edit-campaign', { id: '5' }),
    fill: () => {} },
];
// Cancel, Close and Escape each keep the entry (closeDialogByUser).
const CLOSES = {
  Cancel: (t, f) => t.win.EWViews.handleAction(f.querySelector('.dialog-footer [data-action="close-dialog"]')),
  Close: t => t.win.EWViews.handleAction(t.dom.modal.querySelector('.dialog-head [data-action="close-dialog"]')),
  Escape: t => t.escape(),
};

// Open the dialog, fill it, type the brief and press Save brief only.
async function typedAndSaveOnly(k, opts) {
  const t = await app(k.route);
  const s = server(t, opts);
  await k.open(t);
  const f = t.form('campaign');
  assert.ok(f, 'campaign dialog open');
  k.fill(t, f);
  t.type(f.querySelector('#brief-title'), MINT.title);
  t.type(f.querySelector('#brief-text'), MINT.text);
  const saving = t.win.EWViews.handleAction(f.querySelector('[data-action="brief-register"]'));
  await flush();
  assert.equal(briefPosts(s), 1, 'Save brief only sent the brief');
  return { t, s, f, saving };
}
// Close the dialog one way, reopen it: a fresh form with the entry restored.
async function closeAndReopen(k, t, f, how) {
  if (how) await CLOSES[how](t, f);
  assert.equal(t.dom.modal.open, false, 'the dialog closed');
  assert.ok(t.toasts.includes(KEPT), 'the entry is kept');
  await k.open(t);
  await flush();
  const g = t.form('campaign');
  assert.notEqual(g, f, 'a fresh form');
  assert.ok(notice(g), 'restored as an unsaved draft');
  assert.equal(g.querySelector('#brief-title').value, MINT.title);
  assert.equal(g.querySelector('#brief-text').value, MINT.text);
  return g;
}
const sentRef = (s, k) => plain(s.calls.find(c => `${c[0]} ${c[1]}` === k.write)[2]).campaign.requirement_provenance.brief_ref;
const plain = v => JSON.parse(JSON.stringify(v));

for (const k of KINDS) {
  test(`WIN3-FIX3 ${k.kind}: dialog closed (Escape) and reopened while Save brief only's POST runs: nothing sent until it answers, then that brief is used (RV2-C1)`, LATER, async () => {
    const { t, s, f, saving } = await typedAndSaveOnly(k);
    const g = await closeAndReopen(k, t, f, 'Escape');
    t.win.EWViews.handleSubmit(g);
    await flush();
    assert.deepEqual(writes(s), ['POST /customer/briefs'], 'no second registration, no save while it runs');
    assert.ok(t.toasts.includes(BUSY), 'the change is already being saved');
    s.briefs[0].res({ brief_id: 'mint', version: 1 });
    await saving; await flush();
    assert.equal(g.querySelector('#campaign-brief').value, 'mint:1', 'the reopened form shows the saved brief');
    await t.win.EWViews.handleSubmit(g);
    await flush();
    assert.deepEqual(writes(s), ['POST /customer/briefs', k.write], 'one registration in all');
    assert.deepEqual(sentRef(s, k), MINT_REF);
    assert.deepEqual(s.went, [k.went], 'saved');
  });

  test(`WIN3-FIX3 ${k.kind}: Save brief only had no answer; dialog closed (Cancel) and reopened: no blind re-send, the form is held (RV2-C2)`, LATER, async () => {
    const { t, s, f, saving } = await typedAndSaveOnly(k);
    s.briefs[0].rej(noAnswer());
    await saving; await flush();
    const g = await closeAndReopen(k, t, f, 'Cancel');
    t.win.EWViews.handleSubmit(g);
    await flush();
    assert.deepEqual(writes(s), ['POST /customer/briefs'], 'no second registration, no save');
    assert.equal(g.dataset.held, '1', 'held');
    assert.match(g.querySelector('#form-errors').innerHTML, NO_ANSWER);
    await t.win.EWViews.handleAction(g.querySelector('[data-action="brief-register"]'));
    await flush();
    assert.deepEqual(writes(s), ['POST /customer/briefs'], 'Save brief only sends nothing either');
  });

  test(`WIN3-FIX3 ${k.kind}: dialog closed (Escape) while Save brief only's POST runs; it saves; reopened: that brief is chosen and reused (RV2-C1)`, LATER, async () => {
    const { t, s, f, saving } = await typedAndSaveOnly(k);
    await CLOSES.Escape(t, f);
    s.briefs[0].res({ brief_id: 'mint', version: 1 });
    await saving; await flush();
    assert.ok(t.toasts.includes('Brief saved for Judge test: Mint Mobile.'));
    const g = await closeAndReopen(k, t, f, null);
    assert.equal(g.querySelector('#campaign-brief').value, 'mint:1', 'the saved brief is the choice');
    t.win.EWViews.handleSubmit(g);
    await flush();
    assert.deepEqual(writes(s), ['POST /customer/briefs', k.write], 'one registration in all');
    assert.deepEqual(sentRef(s, k), MINT_REF);
    assert.deepEqual(s.went, [k.went], 'saved');
  });

  for (const listed of [true, false]) {
    test(`WIN3-FIX3 ${k.kind}: Save brief only saved; dialog closed (Close) and reopened${listed ? '' : ' (the list lacks the new brief)'}; ${k.kind}: that brief is reused, no second registration (RV2-C3)`, LATER, async () => {
      const { t, s, f, saving } = await typedAndSaveOnly(k, { listed });
      s.briefs[0].res({ brief_id: 'mint', version: 1 });
      await saving; await flush();
      assert.equal(f.querySelector('#campaign-brief').value, 'mint:1');
      const g = await closeAndReopen(k, t, f, 'Close');
      assert.equal(g.querySelector('#campaign-brief').value, 'mint:1', 'the saved brief is the choice');
      t.win.EWViews.handleSubmit(g);
      await flush();
      assert.deepEqual(writes(s), ['POST /customer/briefs', k.write], 'one registration in all');
      assert.deepEqual(sentRef(s, k), MINT_REF);
      assert.deepEqual(s.went, [k.went], 'saved');
    });
  }
}
