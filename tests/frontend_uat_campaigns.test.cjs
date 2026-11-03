/* UAT 2026-10-15 campaign findings (frontend half).
   UAT-091: a form error also sits beside its field, so a long dialog shows
   it where the user is; the top message stays and is still announced.
   UAT-092: Add to campaign says before submit which placements can't take
   this read and why (the server's own rule, asked read-only).
   UAT-093: Detach confirms and states its effect like Retain/Reference/
   Move; the Move dialog names show windows by their dates (numbered only
   when two clash, as the frozen Summary does).
   UAT-098: a stale report names only what changed.
   Run: node --test tests/frontend_uat_campaigns.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9170/app/' + route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
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
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };

async function views(route = '#/overview') {
  const win = loadAt(route);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.setTimeout = () => {};
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  return { win, modal, main, toasts };
}
const withApi = (t, api) => {
  t.calls = [];
  t.win.EW.api = async (p, opts = {}) => {
    t.calls.push([opts.method || 'GET', p, opts.body]);
    const out = await api(p, opts);
    if (out instanceof Error) throw out;
    return out;
  };
  return t;
};
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
function formFrom(win, html, id, extra = {}) {
  const hidden = {};
  for (const m of html.matchAll(/<input type="hidden" name="([^"]+)" value="([^"]*)">/g)) hidden[m[1]] = m[2].replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  const entries = { ...hidden, ...extra };
  win.FormData = class { constructor() { this.e = Object.entries(entries); } *entries() { yield* this.e; } get(k) { return entries[k]; } };
  return { getAttribute: k => (k === 'id' ? id : null), dataset: {}, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, elements: [] };
}

/* -- UAT-091: the error sits beside the field ------------------------------ */
// A minimal element tree: enough for formError's own DOM calls.
class El {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = { ...attrs }; this.children = []; this.parentElement = null;
    this.dataset = {}; this.className = attrs.class || ''; this.id = attrs.id || ''; this.focused = false; this.scrolled = null;
    this.textContent = ''; this.innerHTML = '';
  }
  append(...kids) { for (const k of kids) { k.parentElement = this; this.children.push(k); } return this; }
  insertAdjacentElement(where, el) {
    if (where === 'beforeend') { this.append(el); return el; }
    if (where === 'afterend') {
      const sib = this.parentElement.children;
      el.parentElement = this.parentElement;
      sib.splice(sib.indexOf(this) + 1, 0, el);
      return el;
    }
    throw new Error('unexpected position ' + where);
  }
  remove() { if (this.parentElement) { const s = this.parentElement.children; s.splice(s.indexOf(this), 1); this.parentElement = null; } }
  matches(sel) {
    return sel.split(',').map(s => s.trim()).some(s => {
      if (s.startsWith('.')) return this.className.split(/\s+/).includes(s.slice(1));
      if (s.startsWith('#')) return this.id === s.slice(1);
      if (s === '[data-inline-error]') return 'inlineError' in this.dataset;
      if (s === '[aria-invalid="true"]') return this.attrs['aria-invalid'] === 'true';
      const m = /^\[aria-describedby~="([^"]+)"\]$/.exec(s);
      if (m) return String(this.attrs['aria-describedby'] || '').split(/\s+/).includes(m[1]);
      return this.tagName === s.toUpperCase();
    });
  }
  closest(sel) { for (let p = this; p; p = p.parentElement) if (p.matches(sel)) return p; return null; }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() { this.focused = true; }
  scrollIntoView(opts) { this.scrolled = opts || {}; }
}
function placementForm() {
  const form = new El('form', { id: 'placement' });
  const box = new El('div', { id: 'form-errors' });
  const field = (id, tag = 'input') => { const wrap = new El('div', { class: 'od-field' }); const f = new El(tag, { id }); wrap.append(new El('label'), f); return [wrap, f]; };
  const [briefWrap, brief] = field('placement-brief', 'select');
  const [bindingWrap, binding] = field('placement-binding', 'select');
  form.append(box, new El('p', { class: 'section-note' }), briefWrap, bindingWrap);
  return { form, box, brief, binding, briefWrap, bindingWrap };
}
async function withDom() {
  const win = loadAt('#/campaign/1');
  win.document.createElement = tag => new El(tag);
  return win;
}

test('UAT-091 the error appears beside the field it marks, and the top message stays announced', async () => {
  const win = await withDom();
  const { form, box, binding, bindingWrap } = placementForm();
  win.EW.formError('Choose an explicit binding.', binding, form);
  assert.match(box.innerHTML, /<p class="field-error">Choose an explicit binding\.<\/p>/, 'the top message is kept');
  assert.equal(box.getAttribute('role'), 'alert', 'and still announced');
  const inline = bindingWrap.querySelector('[data-inline-error]');
  assert.ok(inline, 'a copy sits inside the field’s own block');
  assert.equal(inline.className, 'field-error');
  assert.equal(inline.textContent, 'Choose an explicit binding.');
  assert.equal(bindingWrap.children.at(-1), inline, 'after the field');
  assert.equal(binding.getAttribute('aria-invalid'), 'true');
  assert.ok(binding.getAttribute('aria-describedby').split(' ').includes(inline.id), 'the field is described by its message');
  assert.ok(binding.focused, 'focus moves to the field');
  assert.equal(inline.scrolled && inline.scrolled.block, 'nearest', 'the message is brought into view with the field');
  assert.equal(inline.getAttribute('role'), null, 'one announcement: the top box is the alert');
});

test('UAT-091 a later error moves the message; an error with no field leaves none beside a field', async () => {
  const win = await withDom();
  const { form, brief, binding, briefWrap, bindingWrap } = placementForm();
  binding.setAttribute('aria-describedby', 'binding-help');
  win.EW.formError('Choose an explicit binding.', binding, form);
  win.EW.formError('Select a registered brief version.', brief, form);
  assert.equal(form.querySelectorAll('[data-inline-error]').length, 1, 'one message at a time');
  assert.equal(briefWrap.querySelector('[data-inline-error]').textContent, 'Select a registered brief version.');
  assert.equal(bindingWrap.querySelector('[data-inline-error]'), null);
  assert.equal(binding.getAttribute('aria-describedby'), 'binding-help', 'its own description is restored');
  assert.equal(binding.getAttribute('aria-invalid'), null);
  win.EW.formError('Reload the campaign before saving placements.', null, form);
  assert.equal(form.querySelectorAll('[data-inline-error]').length, 0);
  assert.equal(brief.getAttribute('aria-describedby'), null);
});

test('UAT-091 a field outside a field block gets its message right after it; a stub field never throws', async () => {
  const win = await withDom();
  const form = new El('form', { id: 'x' });
  const box = new El('div', { id: 'form-errors' });
  const input = new El('input', { id: 'bare' });
  form.append(box, input);
  win.EW.formError('Name it.', input, form);
  assert.equal(form.children[2].textContent, 'Name it.');
  assert.ok('inlineError' in form.children[2].dataset);
  // Forms in other tests hand plain objects: no DOM methods, no crash.
  const stub = { setAttribute() {}, focus() {}, parentElement: null };
  assert.equal(win.EW.formError('Plain.', stub, { querySelector: () => null, querySelectorAll: () => [] }), false);
});

/* -- UAT-092: say before submit why a placement can't take the read -------- */
const REF = 'sr1:dc39c846855a4ef6a58fc873d477389e';
const read = { action: 'add-to-campaign', ref: REF, episode: 's001e0', show: 'cs-49b79532d2e7', day: '2026-09-21' };
const draft9 = () => ({ campaign: { id: 9, revision: 7 }, episode_titles: { s001e0: 'When an Ex Police Chief Becomes a Hitman' },
  placements: [
    { id: 'exp:ep', scope: 'episode', episode_id: 's001e0', date_from: '2026-09-01', date_to: '2026-09-30' },
    { id: 'exp:win', scope: 'show_window', show_id: 'cs-49b79532d2e7', date_from: '2026-09-01', date_to: '2026-09-30' },
    { id: 'exp:far', scope: 'show_window', show_id: 'cs-other', date_from: '2026-09-01', date_to: '2026-09-30' }],
  pins: { attachments: {} } });
const fitApi = (fit) => (p) => {
  if (p === '/customer/campaigns') return { campaigns: [{ id: 9, name: 'UAT supplied', mode: 'supplied_requirements', availability: 'active', revision: 7 }] };
  if (p === '/customer/campaigns/9/draft') return { draft: draft9() };
  if (p === '/customer/campaigns/9/evidence-fit?ref=' + encodeURIComponent(REF)) return typeof fit === 'function' ? fit() : fit;
  throw new Error('unexpected ' + p);
};

test('UAT-092 a placement whose scope refuses the read is listed, disabled, with the reason', async () => {
  const t = withApi(await views(), fitApi({ ref: REF, placements: { 'exp:ep': 'transcript_version', 'exp:win': null, 'exp:far': 'outside_show' } }));
  await t.win.EWViews.handleAction({ dataset: read }); await settle();
  const m = t.modal.innerHTML;
  assert.deepEqual(t.calls.filter(c => c[1].includes('evidence-fit')).map(c => c[0] + ' ' + c[1]),
    ['GET /customer/campaigns/9/evidence-fit?ref=' + encodeURIComponent(REF)], 'asked once for the campaign, read-only');
  assert.match(m, /value="9\|exp:ep" disabled><span><b>UAT supplied<\/b> · Episode: When an Ex Police Chief Becomes a Hitman · Sep 1–Sep 30, 2026 · can’t take this evidence: it comes from a different transcript of this episode than the one the placement was set up on<\/span>/);
  assert.match(m, /value="9\|exp:win" checked autofocus><span><b>UAT supplied<\/b> · Show window 1 · Sep 1–Sep 30, 2026<\/span>/);
  assert.doesNotMatch(m, /exp:far/, 'a placement that does not cover the read stays counted, not listed');
  assert.match(m, /type="submit">Add to placement</);
});

test('UAT-092 the other reasons read plainly; nothing to add means no submit', async () => {
  const t = withApi(await views(), fitApi({ placements: { 'exp:ep': 'outside_bounds', 'exp:win': 'outside_dates' } }));
  await t.win.EWViews.handleAction({ dataset: read }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /value="9\|exp:ep" disabled>[\s\S]*?can’t take this evidence: it falls outside the part of the episode the placement covers</);
  assert.match(m, /value="9\|exp:win" disabled>[\s\S]*?can’t take this evidence: it is outside the placement’s episode, show or dates</);
  assert.doesNotMatch(m, /type="submit"/, 'no placement can take it: no Add button');
  assert.doesNotMatch(m, / checked/);
});

test('UAT-092 when the check fails the placements are offered as before; the server still judges', async () => {
  const t = withApi(await views(), fitApi(() => Object.assign(new Error('service had an error'), { status: 500 })));
  await t.win.EWViews.handleAction({ dataset: read }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /value="9\|exp:ep" checked autofocus>/);
  assert.match(m, /value="9\|exp:win">/);
  assert.doesNotMatch(m, /can’t take this evidence/);
  assert.match(m, /type="submit">Add to placement</);
});

/* -- UAT-093: Detach confirms; Move names windows by dates ------------------ */
const P1 = 'exp:w1', P2 = 'exp:w2', SRC = 'sr1:aaaa';
const twoWindows = () => ({
  campaign: { id: 9, revision: 7, name: 'UAT supplied' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [
    { id: P1, scope: 'show_window', show_id: 'cs-1', date_from: '2026-09-01', date_to: '2026-09-30', revision: 2, binding: 'eligible', provenance: {} },
    { id: P2, scope: 'show_window', show_id: 'cs-1', date_from: '2026-10-01', date_to: '2026-10-31', revision: 1, binding: 'eligible', provenance: {} }],
  targets: [], episode_titles: {}, coverage_by_placement: {}, comparisons: {},
  allocation: { evaluations: {}, citations: {} }, reason_codes: { evaluations: {}, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: {}, limitations: [] },
  pins: { attachments: { [P1]: [SRC], [P2]: [] }, attachment_state: { [SRC]: {} } },
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'] });
const labelItem = { ref: SRC, show_title: 'True Crime', ep_title: 'Hitman', pub_date: '2026-09-21', quotation_span: [698, 764],
  read: { brand: { display: 'Kachava', confirmed: true }, display_span: [698, 764] } };
async function placementPage(draft = twoWindows()) {
  const t = await views();
  withApi(t, p => {
    if (p === '/customer/campaigns/9/draft') return { draft };
    if (p.startsWith('/customer/search?')) return { items: [labelItem] };
    throw new Error('unexpected ' + p);
  });
  const route = '#/campaign/9/placement/' + encodeURIComponent(P1);
  t.win.location.hash = route; t.win.EW.current.route = route;
  await t.win.EWViews.render();
  return t;
}
const dataOf = (html, action) => {
  const tag = new RegExp(`<button[^>]*data-action="${action}"[^>]*>`).exec(html)[0];
  const d = {};
  for (const m of tag.matchAll(/data-([a-z-]+)=(?:"([^"]*)"|'([^']*)')/g)) d[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = (m[2] ?? m[3]).replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  return d;
};

test('UAT-093 Detach asks first and says what it does; nothing is sent until Detach it', async () => {
  const t = await placementPage();
  const d = dataOf(t.main.innerHTML, 'detach-evidence');
  withApi(t, () => ({ campaign_revision: 8 }));
  await t.win.EWViews.handleAction({ dataset: d }); await settle();
  assert.equal(t.calls.length, 0, 'the click alone sends nothing');
  const m = t.modal.innerHTML;
  assert.match(m, /<h2 id="dialog-title">Detach this source<\/h2>/);
  assert.match(m, /<b>Kachava · True Crime · Hitman on Show window · Sep 1–Sep 30, 2026<\/b>/);
  assert.match(visible(m), /Detach it from “Show window · Sep 1–Sep 30, 2026”\. The placement stops holding this source and no longer uses it for its result\. The evidence itself is not deleted; you can add it again with Add to campaign\./);
  assert.match(m, /type="submit">Detach it</);
  assert.match(m, /data-action="close-dialog">Cancel</);
  await t.win.EWViews.handleSubmit(formFrom(t.win, m, 'confirm')); await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(t.calls.filter(c => c[0] !== 'GET'))),
    [['DELETE', '/customer/campaigns/9/placements/exp%3Aw1/evidence', { ref: SRC, expected_revision: 7 }]]);
  assert.ok(t.toasts.includes('Source detached.'));
  assert.ok(t.calls.some(c => c[1] === '/customer/campaigns/9/draft'), 'then the placement is read again');
});

test('UAT-093 Move names show windows by their dates, not "Placement 1 · Show window"', async () => {
  const t = await placementPage();
  const d = dataOf(t.main.innerHTML, 'allocation-request" data-op="move');
  await t.win.EWViews.handleAction({ dataset: d, disabled: false, closest: () => null }); await settle();
  const m = t.modal.innerHTML;
  assert.match(m, /Move it from “Show window · Sep 1–Sep 30, 2026” to the placement you choose\./);
  assert.match(m, /<option value="exp:w2" selected>Show window · Oct 1–Oct 31, 2026<\/option>/);
  assert.doesNotMatch(visible(m), /Placement \d/);
});

test('UAT-093 two windows with the same dates are numbered, as in the frozen Summary', async () => {
  const draft = twoWindows();
  draft.placements[1] = { ...draft.placements[1], date_from: '2026-09-01', date_to: '2026-09-30' };
  const t = await placementPage(draft);
  await t.win.EWViews.handleAction({ dataset: dataOf(t.main.innerHTML, 'allocation-request" data-op="move'), disabled: false, closest: () => null }); await settle();
  assert.match(t.modal.innerHTML, /Move it from “Show window 1 · Sep 1–Sep 30, 2026”/);
  assert.match(t.modal.innerHTML, /<option value="exp:w2" selected>Show window 2 · Sep 1–Sep 30, 2026<\/option>/);
  await t.win.EWViews.handleAction({ dataset: dataOf(t.main.innerHTML, 'detach-evidence') }); await settle();
  assert.match(visible(t.modal.innerHTML), /Detach it from “Show window 1 · Sep 1–Sep 30, 2026”/);
});

/* -- UAT-098: the stale line names only what changed ----------------------- */
test('UAT-098 a research-target change reads as such; a bare revision change says the campaign', async () => {
  const win = loadAt('#/overview');
  assert.equal(win.EW.staleSummary(['targets'], 'changed since capture'), 'research targets changed since capture');
  assert.equal(win.EW.staleSummary(['campaign'], 'changed since capture'), 'the campaign changed since capture');
});
