/* DEMO2 pass 14.
   Owner rule (unchanged): nothing retained or cache-backed is shown on a
   navigation, restore or dialog without a fresh successful authority check
   on that navigation; an access refusal removes retained content.
   Owner decisions for the mechanism (coordinator, pass 14):
   1. The epoch is enforced at the one network layer (EW apiCall, the only
      fetch): a read is stamped at send, aborted at a boundary, and dropped
      (never resolved to its caller) when it answers under another epoch.
   2. A boundary replaces the page with "Checking access…", runs the
      route's fresh reads again, closes every dialog (typed entries kept,
      a held notice without server values) and clears every retained store
      (one registry, offerState included).
   3. A restore waits for saves in progress, then runs the boundary.
   4. Only access refusals start a boundary: role/feature refusals of one
      action (Export role, invites on Settings) stay local.
   5. dialogHolds has behavioural tests.
   Red cases: Astra round 4 (demo2-memo-astra-r4.md, six scenarios x
   401/403/428 = 18) and code review r11 (demo2-code-r11.md R11-01..04).
   Only fetch and DOM nodes are simulated; the real EW.api, apiCall,
   sharedRead, render, handleAction, openDialog, listeners and builders run.
   Run: node --test tests/frontend_demo2_pass14.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');


// FormData over a stub form's elements (name/value/type/checked).
class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name && ((el.type !== 'checkbox' && el.type !== 'radio') || el.checked)).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}
const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };

function loadPages(route, fetch) {
  const listeners = {};
  const docListeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
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
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');

// A server whose workspace reads can be refused (every read answers the
// given status) or held open (gate on one path). Only fetch is simulated.
function server(data) {
  const state = { refuse: null, calls: [], gates: {}, answers: {} };
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const call = { method: init.method || 'GET', path: p, signal: init.signal };
    state.calls.push(call);
    const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
    if (p === '/session') return reply(200, SESSION);
    // The answer is decided when the request arrives; a held one is
    // delivered later, unchanged.
    let out;
    const fixed = state.answers[p];
    if (fixed) {
      call.status = fixed.status;
      out = reply(fixed.status, { ok: false, error: { code: fixed.code || 'FORBIDDEN', message: fixed.message, ...(fixed.access ? { access: fixed.access } : {}) } });
    } else if (state.refuse && !(init.method && init.method !== 'GET' && state.writesPass)) {
      const status = state.refuse;
      call.status = status;
      out = reply(status, { ok: false, error: { code: status === 401 ? 'SIGNIN_REQUIRED' : 'FORBIDDEN', message: 'You no longer have access to this workspace.', ...(status === 403 ? { access: 'lost' } : {}) } });
    } else {
      const body = data(p, init);
      if (body === undefined) out = reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
      else { call.status = 200; out = reply(200, JSON.parse(JSON.stringify(body))); }
    }
    if (state.gates[p]) await state.gates[p];
    return out;
  };
  state.count = (p, status) => state.calls.filter(c => c.path === p && (status === undefined || c.status === status)).length;
  state.hold = p => { let open; state.gates[p] = new Promise(res => { open = res; }); return () => { delete state.gates[p]; open(); }; };
  return state;
}
async function go(ctx, route) {
  ctx.win.location.hash = route;
  ctx.win.EW.current.route = route;
  await ctx.win.EWViews.render();
  await drain(30);
  return ctx.main.innerHTML;
}



const SEARCH = '#/search?mode=mentions&q=Muse&shows=cs-private';
// Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
const STATUSES = [401, 403];
const STATUS_WORDS = { 401: 'signed out', 403: 'access removed', 428: 'precondition refused' };

function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, ...props };
}
// A dialog element whose forms can be attached (selectors on data-* only).
function modalStub() {
  const m = node({ open: false, forms: [], showModal() { this.open = true; }, close() { this.open = false; },
    querySelector: sel => (sel === 'button' ? { focus() {} } : null) });
  m.querySelectorAll = sel => {
    if (!/form/.test(sel)) return [];
    return m.forms.filter(f => sel.split(',').some(part => {
      const want = part.match(/data-([a-z]+)="1"/);
      return !want || (f.dataset && f.dataset[want[1]] === '1');
    }));
  };
  return m;
}
function formStub(id, modal, fields, data = {}) {
  return node({ id, dataset: { ...data }, elements: fields.map(([name, value, type = 'text']) => ({ name, value, type, dataset: {} })),
    getAttribute: n => (n === 'id' ? id : null), closest: sel => (sel === '#modal' ? modal : null) });
}
async function app(route, srv, extra = {}) {
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = { innerHTML: '' };
  const modal = modalStub();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal
    : extra[sel] ? extra[sel] : node());
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  return { win, main, modal };
}
const fire = (ctx, type, event) => { for (const fn of ctx.win.listeners[type] || []) fn(event); };
const fireDoc = (ctx, type, event) => { for (const fn of ctx.win.docListeners[type] || []) fn(event); };
const settle = (ms) => new Promise(res => setTimeout(res, ms));

const research = w => (init = {}) => {
  const body = init.body ? JSON.parse(init.body) : {};
  return { query: body.query, revision: 'r1', coverage: {}, summary: { week: [] },
    result: { items: [], total: w.total || 0, per_page: 10, total_relation: 'exact' } };
};
const world = () => ({ total: 0, shows: [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }] });
const data = w => (p, init = {}) => {
  if (p === '/customer/shows') return { shows: w.shows };
  if (p === '/customer/brands') return { brands: [] };
  if (p.startsWith('/customer/saved-items')) return init.method && init.method !== 'GET' ? { item: { id: 1 } } : { items: w.saved || [], next_cursor: null };
  if (p.startsWith('/customer/archive') || p.startsWith('/customer/mistakes')) return { items: [], next_cursor: null };
  if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return { alerts: [] };
  if (p === '/customer/research-query') return research(w)(init);
  if (p.startsWith('/customer/filter-values')) return { items: w.lookup || [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
  if (p.startsWith('/customer/brands/')) return w.brand;
  if (p.startsWith('/customer/search')) return { items: w.offers || [], total: (w.offers || []).length, total_relation: 'exact' };
  if (p.startsWith('/customer/summary')) return { groups: [] };
  return undefined;
};
async function refuseOther(ctx, srv, status) {
  srv.refuse = status;
  await assert.rejects(ctx.win.EW.api('/customer/alerts'), err => err.status === status);
}

// ============================================================================
// Astra round 4: six scenarios x 401/403/428
// ============================================================================
for (const status of STATUSES) {
  // R4-01 (1): the protected page and an open dialog after a refusal.
  test(`SEC-R4-01 refusal (${status}): the protected Search page and the open Create alert dialog no longer show the private name`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show');
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    assert.equal(ctx.modal.open, true, 'fixture: Create alert opened');
    assert.match(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'fixture: the dialog names the private show');
    await refuseOther(ctx, srv, status);
    await drain(40);
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the protected page still shows the name after the refusal');
    assert.ok(!ctx.modal.open || !/PRIVATE-SHOW/.test(ctx.modal.innerHTML), 'the open dialog still shows the name after the refusal');
  });

  // R4-01 (2): a synchronous opener from old page data after a refusal.
  test(`SEC-R4-01 refusal (${status}): a Saved item's Remove action from the old page opens nothing`, async () => {
    const w = world();
    w.saved = [{ id: 7, revision: 3, type: 'query', title: 'PRIVATE-SAVED-TITLE', query: { version: 2, mode: 'mentions', q: 'x' }, selector: null, created_at: '2026-01-02T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' }];
    const srv = server(data(w));
    const ctx = await app('#/saved?tab=queries', srv);
    const html = await go(ctx, '#/saved?tab=queries');
    const m = html.match(/data-action="delete-saved"[^>]*data-id="([^"]*)" data-rev="([^"]*)" data-title="([^"]*)"/);
    assert.ok(m, 'fixture: the Saved page renders the Remove action');
    assert.equal(m[3], 'PRIVATE-SAVED-TITLE');
    await refuseOther(ctx, srv, status);
    await drain(40);
    const before = srv.calls.length;
    await ctx.win.EWViews.handleAction({ dataset: { action: 'delete-saved', id: m[1], rev: m[2], title: m[3] } });
    assert.ok(!ctx.modal.open || !/PRIVATE-SAVED-TITLE/.test(ctx.modal.innerHTML), 'a Remove dialog opened with the old title after the refusal');
    assert.equal(srv.calls.length, before, 'fixture: nothing was asked');
  });

  // R4-01 (3): the brand page's retained offer comparison.
  test(`SEC-R4-01 refusal (${status}): the offer comparison publishes nothing from offerState`, async () => {
    const w = world();
    w.brand = { brand: { key: 'acme', brand: 'Acme', display: 'Acme', drill_down: { ads: { version: 2, mode: 'ads', brands: ['acme'] } },
      confirmed_occurrences: [{}, {}, {}], findings: [] } };
    w.offers = [1, 2, 3].map(n => ({ ref: 'r' + n, status: 'confirmed', pub_date: `2026-01-0${n}`, show_title: 'Show', ep_title: 'Ep',
      read: { display_text: `PRIVATE-OFFER-${n} use code SAVE${n}`, display_span: [n, n + 1], display_basis: 'checked', brand: {} } }));
    const srv = server(data(w));
    const body = node();
    const ctx = await app('#/brand/acme', srv, { '#offer-pair-body': body });
    await go(ctx, '#/brand/acme');
    fireDoc(ctx, 'change', { target: node({ id: 'offer-pair', value: '1' }) });
    assert.match(body.innerHTML, /PRIVATE-OFFER-2/, 'fixture: the pair select publishes the second offer');
    body.innerHTML = '';
    await refuseOther(ctx, srv, status);
    await drain(40);
    const before = srv.calls.length;
    fireDoc(ctx, 'change', { target: node({ id: 'offer-pair', value: '1' }) });
    fireDoc(ctx, 'change', { target: node({ id: 'offer-pair', value: '0' }) });
    assert.doesNotMatch(body.innerHTML, /PRIVATE-OFFER/, 'the retained offers were published after the refusal');
    assert.equal(srv.calls.length, before);
  });

  // R4-02: a page build started before a refusal.
  test(`SEC-R4-02 refusal (${status}) while Search is building: its earlier successful answer never publishes`, async () => {
    const w = world(); w.total = 314159;
    const srv = server(data(w));
    const ctx = await app('#/shows', srv);
    await go(ctx, '#/shows');
    const release = srv.hold('/customer/research-query');
    ctx.win.location.hash = SEARCH;
    ctx.win.EW.current.route = SEARCH;
    const pending = ctx.win.EWViews.render();
    await drain();
    assert.equal(srv.count('/customer/research-query', 200), 1, 'fixture: the research answer was decided (200) before the refusal');
    assert.doesNotMatch(ctx.main.innerHTML, /314,?159/);
    const epoch = ctx.win.EW.authorityEpoch();
    await refuseOther(ctx, srv, status);
    assert.ok(ctx.win.EW.authorityEpoch() > epoch, 'the refusal is a boundary');
    release();
    await pending;
    await drain(40);
    assert.doesNotMatch(ctx.main.innerHTML, /314,?159/, 'the earlier answer was published after the refusal');
  });

  // R4-03 (1): the Filters show lookup.
  test(`SEC-R4-03 refusal (${status}): a Filters show lookup sent before it never fills the list`, async () => {
    const w = world(); w.lookup = [{ value: 'cs-x', label: 'PRIVATE-LOOKUP' }];
    const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'filters' }, closest: () => null });
    const list = node();
    const fieldset = node({ querySelector: sel => (sel === '.check-list' ? list : /init_/.test(sel) ? { value: '[]' } : null) });
    const input = node({ value: 'private', dataset: { lookup: 'shows' },
      closest: sel => (sel === '[data-lookup]' ? input : sel === 'fieldset[data-group]' ? fieldset : sel === '#modal' ? ctx.modal : null) });
    const path = '/customer/filter-values?field=shows&q=private&limit=50';
    const release = srv.hold(path);
    fireDoc(ctx, 'input', { target: input });
    await settle(360);
    assert.equal(srv.count(path, 200), 1, 'fixture: the lookup was answered 200 before the refusal');
    await refuseOther(ctx, srv, status);
    release();
    await drain(40);
    assert.doesNotMatch(list.innerHTML, /PRIVATE-LOOKUP/, 'the pre-refusal lookup filled the list');
  });

  // R4-03 (2): the alert preview.
  test(`SEC-R4-03 refusal (${status}): an alert preview asked before it never publishes its count`, async () => {
    const w = world();
    const srv = server(data(w));
    const box = node({ dataset: {} });
    const form = node({ elements: [['mode', 'mentions'], ['q', 'Muse'], ['brand', ''], ['show', ''], ['status', ''], ['audio', ''], ['from', ''], ['to', ''],
      ['initial_query', JSON.stringify({ version: 2, mode: 'mentions', q: 'Muse' })], ['initial_selector', 'null'], ['touched_fields', '[]']]
      .map(([name, value]) => ({ name, value, type: 'text' })) });
    const ctx = await app(SEARCH, srv, { '#alert-form': form, '#alert-preview': box });
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    assert.equal(ctx.modal.open, true);
    w.total = 271828;
    const release = srv.hold('/customer/research-query');
    const previewing = ctx.win.EWViews.handleAction({ dataset: { action: 'alert-preview' } });
    await drain();
    assert.equal(box.dataset.previewState, 'pending', 'fixture: the preview is pending');
    await refuseOther(ctx, srv, status);
    release();
    await previewing;
    await drain(20);
    assert.doesNotMatch(box.innerHTML, /271828|271,828/, 'the pre-refusal preview count was published');
  });
}

// ============================================================================
// Code review r11
// ============================================================================
for (const status of STATUSES) {
  test(`R11-01 PROBE-A (${status}): Create alert with typed entries over a navigation that is refused: no server values stay, the entry is kept`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'My typed alert'], ['show', 'cs-private', 'select-one']], { dirty: '1' })];
    srv.refuse = status;
    await go(ctx, '#/shows');
    assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'the dialog kept server-derived names over the refused page');
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
    if (ctx.modal.open) assert.match(text(ctx.modal.innerHTML), /Your access changed\. Reopen this dialog in this tab to continue with your typed text\./);
    const kept = [...ctx.win.EW.current.dialogDrafts].find(([k]) => k.startsWith('alert-form|'));
    assert.ok(kept, 'the typed entry was not kept');
    assert.equal(kept[1].values.name, 'My typed alert');
  });
  test(`R11-01 PROBE-B (${status}): a clean Create alert dialog closes on a refusal without navigation`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    srv.refuse = status;
    await assert.rejects(ctx.win.EW.api('/customer/shows'));
    await drain(40);
    assert.equal(ctx.modal.open, false, 'the dialog stayed open after the refusal');
  });
  test(`R11-01 (${status}): a dialog whose save is in flight becomes the held notice; its late outcome is a toast, not painted`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'Saving alert']], { saving: '1' })];
    const own = ctx.win.EW.current.dialogEpoch;
    await refuseOther(ctx, srv, status);
    await drain(40);
    assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/);
    assert.notEqual(ctx.win.EW.current.dialogEpoch, own, 'the dialog keeps its identity, so a late outcome would paint into it');
  });
}

test('R11-02 a same-hash restore waits for a save in progress, then runs the boundary', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app('#/shows', srv);
  assert.match(await go(ctx, '#/shows'), /PRIVATE-SHOW/);
  const release = srv.hold('/customer/saved-items');
  const saving = ctx.win.EW.api('/customer/saved-items', { method: 'POST', body: {} });
  await drain();
  const epoch = ctx.win.EW.authorityEpoch();
  const reads = srv.count('/customer/shows');
  fire(ctx, 'popstate', { state: {} });
  await drain();
  assert.equal(ctx.win.EW.authorityEpoch(), epoch, 'the boundary ran while the save was in progress');
  release();
  assert.ok(await saving, 'the save completed with its own outcome');
  await drain(40);
  assert.ok(ctx.win.EW.authorityEpoch() > epoch, 'the boundary never ran after the save');
  assert.ok(srv.count('/customer/shows') > reads, 'the route was not read again');
});
test('R11-02 a same-hash restore keeps a dialog\'s typed entry for reopening', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'Typed before restore']], { dirty: '1' })];
  fire(ctx, 'popstate', { state: {} });
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/);
  const kept = [...ctx.win.EW.current.dialogDrafts].find(([k]) => k.startsWith('alert-form|'));
  assert.ok(kept && kept[1].values.name === 'Typed before restore', 'the typed entry was lost at the restore');
});

test('R11-03 the Export role refusal stays local: the page, its retained rows and the epoch stay', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  const E = ctx.win.EW;
  const epoch = E.authorityEpoch();
  srv.answers['/customer/research-export'] = { status: 403, message: "Your role in this workspace can't export. Ask an owner for editor access." };
  const toasts = [];
  E.toast = m => toasts.push(m);
  await ctx.win.EWViews.handleAction({ dataset: { action: 'export-all' } });
  assert.equal(E.authorityEpoch(), epoch, 'a role refusal started an authority boundary');
  assert.ok(E.retained('searchItems'), 'a role refusal dropped the retained rows');
  assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/);
  assert.ok(toasts.some(t => /^Your role in this workspace/.test(t)), 'the role refusal is told');
});
test('R11-03 a lost-access 403 (marked) on the same Export action is a boundary', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  const E = ctx.win.EW;
  const epoch = E.authorityEpoch();
  srv.answers['/customer/research-export'] = { status: 403, message: 'workspace membership required', access: 'lost' };
  E.toast = () => {};
  await ctx.win.EWViews.handleAction({ dataset: { action: 'export-all' } });
  assert.ok(E.authorityEpoch() > epoch);
  assert.equal(E.retained('searchItems'), null);
});
// Pass 15: the per-request localRefusal option is gone (the server's
// access marker replaces it); frontend_demo2_pass15 covers local refusals.
test('R11-04 dialogHolds: the same open dialog holds within its epoch and not after a boundary', async () => {
  const srv = server(data(world()));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  assert.equal(E.openDialog('D', '<p>x</p>', '', '', { epoch: E.authorityEpoch() }), true);
  const own = E.current.dialogEpoch, at = E.authorityEpoch();
  assert.equal(E.dialogHolds(own, at), true);
  assert.equal(E.dialogHolds(own), true);
  E.authorityBoundary();
  assert.equal(E.dialogHolds(own, at), false, 'a fill started before a boundary still holds');
  assert.equal(E.dialogHolds(own), false, 'a dialog opened before a boundary still holds');
  assert.equal(E.dialogHolds(own, E.authorityEpoch()), true, 'a fill started after the boundary in the same dialog holds');
});
// ============================================================================
// The network gate (decision 1)
// ============================================================================
test('gate: a read sent before a boundary is aborted and dropped, never resolved with data', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  const release = srv.hold('/customer/brands');
  const read = E.api('/customer/brands');
  await drain();
  const call = srv.calls.find(c => c.path === '/customer/brands');
  E.authorityBoundary();
  assert.equal(call.signal && call.signal.aborted, true, 'the read in flight was not aborted at the boundary');
  release();
  await assert.rejects(read, err => err.droppedContext === true && err.droppedAuthority === true);
});
test('gate: a read that answers after a boundary is dropped even when the transport ignores the abort', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  const release = srv.hold('/customer/shows');
  const read = E.api('/customer/shows');
  await drain();
  E.current.authorityEpoch += 1;
  release();
  await assert.rejects(read, err => err.droppedAuthority === true);
});
test('gate: a write sent before a boundary is neither aborted nor dropped: its committed outcome is reported', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  const release = srv.hold('/customer/saved-items');
  const write = E.api('/customer/saved-items', { method: 'POST', body: {} });
  await drain();
  const call = srv.calls.find(c => c.path === '/customer/saved-items');
  E.authorityBoundary();
  assert.ok(!(call.signal && call.signal.aborted), 'a write was aborted');
  release();
  const out = await write;
  assert.equal(out.item.id, 1);
});
test('gate: a refusal inside the boundary\'s own check refuses once and settles (no loop)', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  srv.refuse = 403;
  await go(ctx, SEARCH);
  await drain(80);
  const n = srv.count('/customer/research-query', 403);
  assert.ok(n >= 1 && n <= 2, 'research asked ' + n + ' times');
  assert.match(text(ctx.main.innerHTML), /Search is unavailable/);
});
test('gate: no customer code calls fetch, XHR or another transport outside the one wrapper', () => {
  const files = ['app.js', 'views.js', 'saved.js', 'ui.js'];
  const strip = t => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`\\])\/\/.*$/gm, '$1');
  const hits = [];
  for (const f of files) {
    const code = strip(fs.readFileSync(path.join(APP, f), 'utf8'));
    for (const re of [/\bfetch\s*\(/g, /XMLHttpRequest/g, /\bEventSource\b/g, /\bWebSocket\b/g, /sendBeacon/g, /\bimport\s*\(/g, /\.fetch\b/g]) {
      for (const m of code.matchAll(re)) hits.push({ f, at: m.index, what: m[0], code });
    }
  }
  const outside = hits.filter(h => !(h.f === 'app.js' && h.what.startsWith('fetch')
    && /function timedFetch\(/.test(h.code.slice(Math.max(0, h.at - 400), h.at))));
  assert.deepEqual(outside.map(h => `${h.f}: ${h.what}`), [], 'a transport outside the wrapper');
  assert.equal(hits.filter(h => h.f === 'app.js' && h.what.startsWith('fetch')).length, 1, 'exactly one fetch, in timedFetch');
  const app = strip(fs.readFileSync(path.join(APP, 'app.js'), 'utf8'));
  assert.deepEqual([...app.matchAll(/\btimedFetch\(/g)].length, 2, 'timedFetch: its definition and the one call in apiCall');
  assert.match(app, /async function apiCall\([\s\S]*?const gated = [\s\S]*?timedFetch\(/, 'apiCall gates before timedFetch');
});

// ============================================================================
// One registry of retained stores (decision 2)
// ============================================================================
// Module-level names that hold no server-derived data, with the reason.
const NOT_RETAINED_MODULE = {
  'app.js': { current: 'state object; its fields are classified below', notices: 'fixed copy', writeWaiters: 'write-settle callbacks',
    nativeSetTimeout: 'timer function', nativeClearTimeout: 'timer function', requestTimeout: 'timeout config',
    retainedStores: 'the registry itself', slots: 'request places: counts, timings and waiting callbacks (READ-QUEUE)', inflightReads: 'abort controllers of reads in flight (aborted at a boundary)', inflightSends: 'abort controllers of counted reads sent (READ-QUEUE)',
    researchLane: 'one research query in flight per tab: a promise that settles with no value, and its counter (SPEED-R1)',
    labelCache: 'session workspace labels (session data, not workspace content)', toastQueue: 'transient toasts', fieldErrorSeq: 'counter', activeWs: 'session locator',
    sessionReadsOpen: 'counter', sessionIdleWaiters: 'callbacks', loadIdentityNoted: 'flag', historyLocked: 'flag' },
  'views.js': { pickerSeq: 'counter', inviteEmailErrorSeq: 'counter', savesInFlight: 'counter', saveWaiters: 'callbacks', settleWaiters: 'callbacks',
    canonicalReconcile: 'session reconcile promise', reconcileConflicts: 'session conflicts', signedOutFollowUp: 'sign-out flow promise', renderSignedOut: 'sign-out flow promise (RR3-01)',
    draftStash: 'user-typed page entries (kept on purpose)', renderingAfterSave: 'form reference', renderAfterSaves: 'flag', deferredBasis: 'render basis ids',
    deferredRender: 'render promise', lookupTimer: 'timer (cleared at a boundary)', modalEl: 'the dialog element', bootSettled: 'flag', bootDone: 'boot promise', pagesReady: 'flag (page modules loaded)', pagesLoaded: 'page-modules-loaded promise',
    typedOwner: 'principal and workspace ids the typed entries belong to', roleBoundaryWaiting: 'boundary promise',
    identityGen: 'counter', ambientOwner: 'stamp of the operation running synchronously (generation and principal id)',
    tabReturn: 'tab-return gate: departure counters, a retry timer and the held answers\' verdict (no server data)',
    clipWatches: 'per clip element (WeakMap, gone with the element at a teardown): its stall timer, last time, longest duration and src (M11-PLAYER-FIX1)',
    searchReading: 'the Search play whose record read runs: its ref and audioEpoch; inert once a teardown moves audioEpoch (CLIP-PLAYER-FIX)',
    searchShown: 'the search player\'s look (a ref and loading/playing) for repainted buttons; once that play is over (a teardown, a newer play, a dropped read), the next repaint or the read\'s end sets it idle and clears "Loading clip…" (CLIP-PLAYER-FIX, FIX2 CLP-R1-01)',
    signedOutNote: 'flag: this page load follows a sign-out in this tab (JUDGE-SMALL G1-16)',
    keptPaint: 'the render token, route and loading label of a kept page painted at once; its first refused read drops it (M9-BACK-FIX1); no server data',
    explicitHash: 'the address the browser\'s last fragment navigation made (already in the address bar); every navigate event replaces it and the next route event consumes it (BL-033)',
    meaningPolls: 'meaning-alert run ids being polled (no server data); each loop ends at an authority boundary',
    drawing: 'render tokens still drawing (no server data); each leaves when its render ends (TABRETURN-LOWS-FIX1)' },
  'saved.js': {}, 'ui.js': {},
};
// Module-level stores and the registry entry that clears each.
const REGISTERED_MODULE = { 'app.js': { sharedReads: 'sharedReads', prefCache: 'preferences', keptPages: 'keptPages' }, 'views.js': { offerState: 'offerState', agentTokenReveal: 'agentTokenReveal', exportBusy: 'exportBusy', exportBrands: 'exportBrands', ask: 'ask' } };
// current fields that hold no server-derived workspace content.
const NOT_RETAINED_CURRENT = (
  // navigation and scroll
  'route scroll focus back filters compare searchTimer toastTimer fetchToken aborters framePath searchInspect selection searchPrefNote autoRetriedRoute rateLimitedRead droppedTokens campaignLinkageFilter cohortRefresh filtersLastGroup focusChooser offRouteBox recordLeftWs freshEntry '
  // authority and publication bookkeeping
  + 'paintedToken supersededRender unpaintedFlow audioEpoch authorityEpoch pageEpoch recheckPending recheckToken recheckRun refusedToken refusedRead roleRecheckPending roleRecheckToken publishedContext publishedGeneration publishedReadFailed publishedRoute publishedToken publishedWorkspace displayedRevision displayedWorkspaceSwitchRevision renderGiven '
  // session, admission and switch (ordered by their own revision)
  + 'hosted principal identity signin csrf sessionCsrf sessionRole roleChange workspaceContext activeWorkspace workspaceGeneration workspaceNames workspaceRole workspaces workspaceSwitchRevision adoptedSession bootAdmitted bootRefused bootRetry refusedWorkspace pilotFeatures askAvailable askPaused meaningAlertsAvailable briefCheckLiveAvailable excerptOnly excerptOnlyContext queuedServerContext reconcileForToken reconcileSeq sessionSeq sessionChangeSeq adoptedSessionTurn switchCommitMark switchOp switchPending switchSettling switchWaiting writeFence fenceReleasedOp writeCommits writesInFlight pendingWorkspace inviteAccepted '
  // dialogs (identity) and user-typed drafts (kept on purpose)
  + 'dialogEpoch dialogOpenSeq dialogOpener dialogRoute dialogWorkspace dialogWorkspaceName dialogDrafts signedOutHeld dirtyForm dirtyWorkspace promptDrafts leaveDrafts leaveTarget staleDraft alertDraft conflict '
  // transient notices and in-flight invite sends (no record content)
  + 'lastToast toastLow outcomeNotice inviteAlert inviteError inviteSendOff inviteSends settingsSaved brands shows'
).split(/\s+/).filter(Boolean);

test('registry: every module-level cache is registered or classified as holding no server data', () => {
  const problems = [];
  for (const f of ['app.js', 'views.js', 'saved.js', 'ui.js']) {
    const code = fs.readFileSync(path.join(APP, f), 'utf8');
    for (const m of code.matchAll(/^  (?:let|const|var) ([A-Za-z_$][\w$]*)\s*=\s*(.*)$/gm)) {
      const [, name, init] = m;
      if (/^[A-Z0-9_$]+$/.test(name)) continue;
      if (/^(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)/.test(init)) continue;
      if (/^(?:window\.|E\.|document\.|\(\(|new Set\(\[')/.test(init) && !/new Map|new Set\(\)/.test(init)) continue;
      if (name in (REGISTERED_MODULE[f] || {}) || name in (NOT_RETAINED_MODULE[f] || {})) continue;
      problems.push(`${f}: ${name} = ${init.slice(0, 60)}`);
    }
  }
  assert.deepEqual(problems, [], 'unregistered module-level state:\n' + problems.join('\n'));
});
test('registry: each module-level store listed as registered is in the live registry', async () => {
  const ctx = await app('#/overview', server(data(world())));
  const names = ctx.win.EW.retainedStoreList().map(st => st.name);
  for (const f of Object.keys(REGISTERED_MODULE)) for (const [v, store] of Object.entries(REGISTERED_MODULE[f])) {
    assert.ok(names.includes(store), `${f}: ${v} is not registered (expected store ${store})`);
  }
});
test('registry: every current field is cleared by a registered store or classified as holding no server data', async () => {
  const srv = server(data(world()));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  const registered = E.retainedStoreList();
  const names = registered.map(s => s.name);
  for (const n of ['sharedReads', 'retainedViews', 'datasetStrip', 'workspaceViews', 'preferences', 'offerState']) assert.ok(names.includes(n), 'not registered: ' + n);
  const regFields = new Set(registered.flatMap(s => s.fields).concat(['directory']));
  const fields = new Set(Object.keys(E.current));
  for (const f of ['app.js', 'views.js', 'saved.js', 'ui.js']) {
    const code = fs.readFileSync(path.join(APP, f), 'utf8');
    for (const m of code.matchAll(/(?:E\.)?\bcurrent\.([A-Za-z_]\w*)\s*=[^=]/g)) fields.add(m[1]);
  }
  const unknown = [...fields].filter(x => !regFields.has(x) && !NOT_RETAINED_CURRENT.includes(x));
  assert.deepEqual(unknown, [], 'unclassified current fields: ' + unknown.join(' '));
});
test('registry: a refusal clears every registered field and store', async () => {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  const E = ctx.win.EW;
  const fields = E.retainedStoreList().flatMap(s => s.fields);
  for (const f of fields) E.current[f] = { sentinel: true };
  E.current.directory = { sentinel: true };
  await refuseOther(ctx, srv, 403);
  for (const f of fields) assert.ok(!(E.current[f] && E.current[f].sentinel), f + ' survived the refusal');
  assert.ok(!E.current.directory.sentinel, 'directory survived the refusal');
});

// A refusal while a page save is in flight: the boundary's render waits
// for the save (deferred), so the boundary itself must take the protected
// page down at once (kills the "page not replaced" mutant).
for (const status of STATUSES) {
  test(`boundary (${status}) with a page save in flight: the protected page is replaced at once, before the deferred render`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app('#/shows', srv);
    assert.match(await go(ctx, '#/shows'), /PRIVATE-SHOW/);
    const busy = node({ dataset: { saving: '1' }, elements: [], getAttribute: n => (n === 'id' ? 'show-note' : null) });
    ctx.win.document.querySelectorAll = sel => (/data-saving/.test(sel) ? [busy] : []);
    srv.refuse = status;
    await assert.rejects(ctx.win.EW.api('/customer/alerts'));
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the protected page stayed up while the render waited for the save');
    assert.match(text(ctx.main.innerHTML), /Checking access/);
  });
}
