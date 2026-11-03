/* DEMO2 pass 16.
   Red cases: code review r12 (demo2-code-r12.md DEMO2-R12-01, R12-02) and
   Astra round 5 (demo2-memo-astra-r5.md DEMO2-SEC-R5-01, R5-02).
   Owner rules:
   - A 401 on an ordinary write (an expired session when the user presses
     Save) runs the same access boundary as any other lost-access refusal;
     typed entries are kept the way the boundary keeps them.
   - Every lost-access refusal (401, or 403 marked access "lost"), wherever
     it arrives, including inside the boundary's own check, advances the
     epoch, aborts reads in flight and keeps the boundary up.
   - The hosted context token includes the caller's workspace role. A
     "workspace role changed" refusal (unmarked) clears every retained
     store; role-dependent views read fresh on their next render. It does
     not show "Checking access…".
   Only fetch and DOM nodes are simulated; the real EW.api, apiCall,
   sharedRead, render, handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass16.test.cjs */
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
const OWNER = 'oidc:https://i.example:owner';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };

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

// A server whose workspace reads can be refused (every request answers
// the given status), answered per path, or held open (gate on one path).
// The session is mutable; customer answers carry its context, as the
// hosted server's do. Only fetch is simulated.
function server(data, session = SESSION) {
  const state = { refuse: null, calls: [], gates: {}, answers: {}, session: { ...session } };
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const call = { method: init.method || 'GET', path: p, signal: init.signal };
    state.calls.push(call);
    const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
    if (p === '/session') return reply(200, state.session);
    // The answer is decided when the request arrives; a held one is
    // delivered later, unchanged.
    let out;
    const fixed = state.answers[p];
    if (fixed) {
      call.status = fixed.status;
      out = reply(fixed.status, { ok: false, error: { code: fixed.code || 'FORBIDDEN', message: fixed.message, ...(fixed.access ? { access: fixed.access } : {}), ...(fixed.reason ? { reason: fixed.reason } : {}) } });
    } else if (state.refuse) {
      const status = state.refuse;
      call.status = status;
      out = reply(status, { ok: false, error: { code: status === 401 ? 'SIGNIN_REQUIRED' : 'FORBIDDEN', message: 'You no longer have access to this workspace.', ...(status === 403 ? { access: 'lost' } : {}) } });
    } else {
      const body = data(p, init);
      if (body === undefined) out = reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
      else {
        call.status = 200;
        const copy = JSON.parse(JSON.stringify(body));
        if (p.startsWith('/customer/') && copy && typeof copy === 'object' && !('workspace_context' in copy)) copy.workspace_context = state.session.workspace_context;
        out = reply(200, copy);
      }
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
const STATUSES = [401, 403];
const LOST = { 401: { status: 401, code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required' },
  403: { status: 403, message: 'workspace membership required', access: 'lost' } };

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
  // The dialog's forms are in the document (heldDrafts and the switch
  // guard find them there).
  win.document.querySelectorAll = sel => (/form/.test(sel) ? modal.querySelectorAll(sel) : []);
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  return { win, main, modal };
}
const fire = (ctx, type, event) => { for (const fn of ctx.win.listeners[type] || []) fn(event); };

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
  if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return init.method && init.method !== 'GET' ? { alert: { id: 1 } } : { alerts: [] };
  if (p === '/customer/research-query') return research(w)(init);
  if (p.startsWith('/customer/filter-values')) return { items: w.lookup || [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
  if (p.startsWith('/customer/summary')) return { groups: [] };
  return undefined;
};

// ============================================================================
// DEMO2-R12-01: a 401 on an ordinary write runs the access boundary
// ============================================================================
async function alertOpen() {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show');
  await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  assert.equal(ctx.modal.open, true, 'fixture: Create alert opened');
  assert.match(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'fixture: the dialog names the private show');
  return { w, srv, ctx, E: ctx.win.EW };
}
async function signedOutSave(srv, E) {
  srv.refuse = 401;
  await assert.rejects(E.api('/customer/alerts', { method: 'POST', body: { name: 'x' } }),
    err => err.status === 401 && err.signedOut === true);
}

test('R12-01 P1c: Save answered 401 while Create alert holds typed entries: no server names stay on the page or in the dialog; the entry is kept', async () => {
  const { srv, ctx, E } = await alertOpen();
  ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'My typed alert'], ['show', 'cs-private', 'select-one']], { dirty: '1' })];
  const epoch = E.authorityEpoch();
  await signedOutSave(srv, E);
  assert.ok(E.authorityEpoch() > epoch, 'fixture: the 401 advanced the epoch');
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'the dialog kept server-derived names after the signed-out write');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the protected page stayed up after the signed-out write');
  await drain(60);
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'the dialog shows server-derived names once the sign-out is followed');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the page shows server-derived names once the sign-out is followed');
  const kept = [...E.current.dialogDrafts].find(([k]) => k.startsWith('alert-form|'));
  assert.ok(kept, 'the typed entry was not kept');
  assert.equal(kept[1].values.name, 'My typed alert');
});

test('R12-01 P1: Save answered 401 with a clean dialog: it closes and is emptied', async () => {
  const { srv, ctx, E } = await alertOpen();
  await signedOutSave(srv, E);
  await drain(60);
  assert.equal(ctx.modal.open, false, 'the dialog stayed open');
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'the closed dialog still holds the private name');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
});

// ============================================================================
// DEMO2-R12-02: a dialog the boundary closes is emptied
// ============================================================================
for (const status of STATUSES) {
  test(`R12-02 (${status}): a clean dialog the boundary closes holds no server names`, async () => {
    const { srv, ctx, E } = await alertOpen();
    srv.refuse = status;
    await assert.rejects(E.api('/customer/alerts'), err => err.status === status);
    assert.equal(ctx.modal.open, false, 'the dialog stayed open');
    assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'the closed dialog still holds the private name');
    await drain(40);
    assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/);
  });
}

// ============================================================================
// DEMO2-SEC-R5-01: a refusal during the boundary's own check
// ============================================================================
async function heldInRecheck(status, trigger) {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  const E = ctx.win.EW;
  assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search rendered');
  // The next research answer is decided (200, total 314159) and held.
  w.total = 314159;
  const release = srv.hold('/customer/research-query');
  // The boundary's own check: the names read is refused.
  srv.answers['/customer/shows'] = LOST[status];
  const before = srv.calls.length;
  await trigger(ctx, srv, E);
  await drain();
  const research = srv.calls.slice(before).find(c => c.path === '/customer/research-query');
  assert.ok(research && research.status === 200, 'fixture: the check asked research and it was answered 200 (held)');
  const refused = srv.calls.slice(before).find(c => c.path === '/customer/shows');
  assert.ok(refused && refused.status === status, 'fixture: the check\'s names read was refused');
  assert.equal(E.current.recheckToken, E.current.fetchToken, 'fixture: the refusal belongs to the boundary\'s own check');
  assert.equal(E.current.refusedToken, E.current.fetchToken, 'fixture: the refusal answered the check');
  assert.doesNotMatch(ctx.main.innerHTML, /314,?159/);
  return { srv, ctx, E, release, research, after: () => srv.calls.length };
}
const triggers = {
  'a sibling refusal': async (ctx, srv, E) => {
    srv.answers['/customer/alerts'] = LOST[403];
    await assert.rejects(E.api('/customer/alerts'), err => err.status === 403);
  },
  'a restore': async ctx => { fire(ctx, 'pageshow', { persisted: true }); },
};
for (const status of STATUSES) {
  for (const [label, trigger] of Object.entries(triggers)) {
    test(`SEC-R5-01 (${status}, after ${label}): a refusal inside the boundary's check advances the epoch, aborts the held Search read, and nothing from it publishes or is retained`, async () => {
      const { srv, ctx, E, release, research } = await heldInRecheck(status, trigger);
      const epoch = E.authorityEpoch();
      const reads = srv.calls.length;
      // Nothing more is needed to show the defect: the refusal is in.
      assert.ok(research.signal && research.signal.aborted, 'the held research read was not aborted by the refusal in the check');
      release();
      await drain(60);
      assert.doesNotMatch(ctx.main.innerHTML, /314,?159/, 'the held research answer from before the refusal was published');
      assert.equal(E.retained('lastSearch'), null, 'the held answer entered the retained Search frame');
      assert.equal(E.current.lastSearch, null, 'the held answer is stored in the retained Search frame');
      assert.equal(E.retained('lastSearchTotal'), null, 'the held total was retained');
      assert.ok(E.authorityEpoch() >= epoch, 'the epoch went back');
      assert.equal(srv.calls.length, reads, 'the refusal in the check started another automatic render');
      assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
      assert.equal(E.current.pageEpoch, null, 'the refused check left a usable page epoch');
    });
  }
  test(`SEC-R5-01 (${status}): the refusal inside the check itself advances the epoch`, async () => {
    const w = world(); const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    const E = ctx.win.EW;
    await go(ctx, SEARCH);
    const release = srv.hold('/customer/shows');
    srv.answers['/customer/shows'] = LOST[status];
    srv.answers['/customer/alerts'] = LOST[403];
    await assert.rejects(E.api('/customer/alerts'), err => err.status === 403);
    await drain();
    const epoch = E.authorityEpoch();
    release();
    await drain(60);
    assert.ok(E.authorityEpoch() > epoch, 'a lost-access refusal inside the check did not advance the epoch');
    assert.match(text(ctx.main.innerHTML), /Search is unavailable/, 'the check did not end on the refusal');
  });
}

// ============================================================================
// DEMO2-SEC-R5-02: owner becomes editor; Settings' owner-only invitations
// ============================================================================
const INVITE = { id: 'invite:1', member: 'PRIVATE-INVITEE@example.test', role: 'viewer', created_at: '2026-10-20T10:00:00+00:00',
  delivery: 'not_queued', unresolved: false };
function settingsData(state) {
  return (p, init = {}) => {
    const method = init.method || 'GET';
    if (p === '/customer/preferences') return method === 'PUT' ? { ok: true, preferences: { revision: 2, workspace_name: 'Acme' } } : { preferences: { revision: 1, workspace_name: 'Acme' } };
    if (p === '/workspaces/ws/members') return { members: [{ member: OWNER, role: state.role, email: 'owner@example.test' }] };
    if (p === '/workspaces/ws/invites') return { invites: [INVITE] };
    if (p === '/shares') return { shares: [] };
    if (p === '/billing/status') return { configured: false };
    if (p === '/customer/alerts') return { alerts: [] };
    return undefined;
  };
}
async function ownerSettings() {
  const state = { role: 'owner' };
  const srv = server(settingsData(state), HOSTED);
  const ctx = await app('#/settings', srv);
  const E = ctx.win.EW;
  E.toast = () => {};
  const html = await go(ctx, '#/settings');
  assert.match(html, /PRIVATE-INVITEE/, 'fixture: the owner sees the pending invitation');
  assert.match(html, /Invite someone/, 'fixture: the owner sees the invitation form');
  assert.ok(E.current.accessReads, 'fixture: the access reads are kept for an in-place save');
  return { state, srv, ctx, E };
}
function demote(srv, state, { context } = {}) {
  state.role = 'editor';
  srv.session = { ...srv.session, role: 'editor', ...(context ? { workspace_context: context } : {}) };
  srv.answers['/workspaces/ws/invites'] = { status: 403, message: 'owner manages membership' };
}
const workspaceForm = () => node({ id: 'settings-workspace', getAttribute: n => (n === 'id' ? 'settings-workspace' : null),
  elements: [{ name: 'expected_revision', value: '1', type: 'hidden' }, { name: 'workspace_name', value: 'Acme renamed', type: 'text' }] });
async function savePreferences(ctx, srv) {
  await ctx.win.EWViews.handleSubmit(workspaceForm());
  await drain(80);
  assert.ok(srv.calls.some(c => c.method === 'PUT' && c.path === '/customer/preferences'), 'fixture: the preferences PUT went out');
}

test('SEC-R5-02 Astra: after a "workspace role changed" refusal, a preferences save reads the invitations fresh and the refusal hides them', async () => {
  const { state, srv, ctx, E } = await ownerSettings();
  demote(srv, state);
  srv.answers['/customer/alerts'] = { status: 403, message: 'workspace role changed', reason: 'role_changed' };
  const epoch = E.authorityEpoch();
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 403 && err.accessRefused !== true);
  // DEMO2 pass 17 (owner decision): a role change is a soft boundary: the
  // epoch advances, but there is no "Checking access…" page.
  assert.ok(E.authorityEpoch() > epoch, 'a role change did not advance the epoch');
  assert.doesNotMatch(text(ctx.main.innerHTML), /Checking access/);
  assert.equal(E.current.accessReads, null, 'the role-changed refusal kept the owner-only access reads');
  delete srv.answers['/customer/alerts'];
  const invites = srv.count('/workspaces/ws/invites');
  await savePreferences(ctx, srv);
  assert.ok(srv.count('/workspaces/ws/invites') > invites, 'the invitations were not read again');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-INVITEE/, 'the owner-only invitation list was published to an editor');
  assert.doesNotMatch(ctx.main.innerHTML, /Invite someone/, 'the invitation form was published to an editor');
});

test('SEC-R5-02 control (pass 17: soft boundary): a role-changed refusal clears every retained store and reads the page again without "Checking access…"', async () => {
  const { srv, ctx, E } = await ownerSettings();
  E.current.dataset = { cached: true };
  E.current.lastSearch = { html: 'x', epoch: E.authorityEpoch() };
  srv.answers['/customer/alerts'] = { status: 403, message: 'workspace role changed', reason: 'role_changed' };
  await assert.rejects(E.api('/customer/alerts'));
  for (const store of E.retainedStoreList()) for (const field of store.fields) {
    assert.equal(E.current[field], null, `${store.name}.${field} survived a role-changed refusal`);
  }
  assert.doesNotMatch(text(ctx.main.innerHTML), /Checking access/, 'a role change showed "Checking access…"');
  await drain(60);
  assert.match(ctx.main.innerHTML, /Workspace access/, 'the page was not read again');
});

test('SEC-R5-02 control: another unmarked role 403 keeps the retained stores (only the role_changed refusal clears them)', async () => {
  const { srv, E } = await ownerSettings();
  srv.answers['/customer/alerts'] = { status: 403, message: "Your role in this workspace can't make this change. Ask an owner for editor access." };
  await assert.rejects(E.api('/customer/alerts'));
  assert.ok(E.current.accessReads, 'a role 403 of one action cleared the access reads');
});

test('SEC-R5-02 token: the server\'s context token changes with the role; a preferences save then reads the invitations fresh and the refusal hides them', async () => {
  const { state, srv, ctx } = await ownerSettings();
  // No refusal reaches the page first: only the token says the role moved.
  demote(srv, state, { context: 'ctx-editor' });
  const invites = srv.count('/workspaces/ws/invites');
  await savePreferences(ctx, srv);
  await drain(80);
  assert.ok(srv.count('/workspaces/ws/invites') > invites, 'the invitations were not read again');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-INVITEE/, 'the owner-only invitation list was published under the editor token');
  assert.doesNotMatch(ctx.main.innerHTML, /Invite someone/);
  assert.equal(ctx.win.EW.current.workspaceContext, 'ctx-editor', 'the new token was not adopted');
});

// ============================================================================
// DEMO2-R12-N4: the held-notice copy says what happens
// ============================================================================
test('R12-N4: a dialog whose typed text is not refilled says so plainly, without claiming it closed', async () => {
  const { srv, ctx, E } = await alertOpen();
  ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'kept']], { dirty: '1' }),
    formStub('not-a-draft-form', ctx.modal, [['note', 'typed']], { dirty: '1' })];
  srv.refuse = 403;
  await assert.rejects(E.api('/customer/alerts'));
  assert.equal(ctx.modal.open, true, 'fixture: the held notice is shown in the open dialog');
  const said = text(ctx.modal.innerHTML);
  assert.doesNotMatch(said, /This dialog closed/, 'the notice says the dialog closed while it is open');
  assert.doesNotMatch(said, /could not be kept/, 'the notice says the text could not be kept while it is held in memory');
  assert.match(said, /Your access changed\./);
  assert.match(said, /will not be filled in again/);
});
