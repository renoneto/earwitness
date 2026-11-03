/* DEMO2 pass 17.
   Red cases: Astra round 6 (demo2-memo-astra-r6.md DEMO2-SEC-R6-01) and
   code review r13 (demo2-code-r13.md DEMO2-R13-01, R13-02, R13-N1).
   Owner rules:
   - A role change is a soft boundary: everything the access boundary does
     (the epoch advances, reads in flight abort, every retained store and
     page-captured cache is cleared, the current route is read again)
     except the "Checking access…" page. It applies to the marked refusal
     (error.reason "role_changed") and to a context-token change that
     differs only in role.
   - The client keys on error.reason, never on the message text.
   - Typed text is not kept across the sign-in redirect. The held notice
     and the signed-out page show the user's own typed free text, escaped
     and read-only, so they can copy it; the leave-page prompt counts it.
   - A save answered under a token whose only change is the role says so.
   Only fetch and DOM nodes are simulated; the real EW.api, apiCall,
   render, handleAction, handleSubmit and builders run.
   Run: node --test tests/frontend_demo2_pass17.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeFormData {
  constructor(form) { this.form = form; }
  all() { return ((this.form && this.form.elements) || []).filter(el => el.name && ((el.type !== 'checkbox' && el.type !== 'radio') || el.checked)).map(el => [el.name, String(el.value ?? '')]); }
  get(k) { const e = this.all().find(([n]) => n === k); return e ? e[1] : null; }
  entries() { return this.all()[Symbol.iterator](); }
}
const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const OWNER = 'oidc:https://i.example:owner';
const HOSTED = { ok: true, authenticated: true, principal: OWNER, email: 'owner@example.test', active_workspace: 'ws', workspaces: ['ws'],
  csrf_token: 's'.repeat(32), workspace_context: 'ctx-owner', workspace_switch_revision: 7, role: 'owner',
  frontend_contract: 'earwitness/customer-ui/2' };
const ROLE_CHANGED = { status: 403, message: 'workspace role changed', reason: 'role_changed' };

function loadPages(route, fetch) {
  const listeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, FormData: FakeFormData, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
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
  return sandbox;
}
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const text = html => html.replace(/<[^>]+>/g, '').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');

// A server whose answers are decided when a request arrives and can be
// held (gate on one path), the session read included. A signed-out
// session answers 401 with the sign-in state.
function server(data, session) {
  const state = { calls: [], gates: {}, answers: {}, session: { ...session }, signedOut: false };
  const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const call = { method: init.method || 'GET', path: p, signal: init.signal };
    state.calls.push(call);
    let out;
    const fixed = state.answers[p] && (!state.answers[p].method || state.answers[p].method === call.method) ? state.answers[p] : null;
    if (p === '/session') {
      call.status = state.signedOut ? 401 : 200;
      out = state.signedOut ? reply(401, { ok: false, error: 'signin', signin: { available: true } }) : reply(200, JSON.parse(JSON.stringify(state.session)));
    } else if (fixed) {
      call.status = fixed.status;
      out = reply(fixed.status, { ok: false, error: { code: fixed.code || 'FORBIDDEN', message: fixed.message,
        ...(fixed.access ? { access: fixed.access } : {}), ...(fixed.reason ? { reason: fixed.reason } : {}) } });
    } else {
      const body = data(p, init);
      if (body === undefined) out = reply(404, { ok: false, error: { code: 'NOT_FOUND', message: 'unexpected ' + p } });
      else {
        call.status = 200;
        const copy = JSON.parse(JSON.stringify(body));
        if (copy && typeof copy === 'object' && !('workspace_context' in copy)) copy.workspace_context = state.session.workspace_context;
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
function node(props = {}) {
  return { dataset: {}, innerHTML: '', textContent: '', value: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
    setAttribute() {}, removeAttribute() {}, hasAttribute: () => false, getAttribute: () => null, focus() {}, addEventListener() {},
    insertAdjacentHTML() {}, querySelector: () => null, querySelectorAll: () => [], closest: () => null, ...props };
}
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
// Fields: [name, value, type, defaultValue]; a text field rendered empty
// unless a default is given (the browser's defaultValue).
function formStub(id, modal, fields, data = {}) {
  return node({ id, dataset: { ...data }, elements: fields.map(([name, value, type = 'text', defaultValue = '']) => ({ name, value, type, defaultValue, dataset: {} })),
    getAttribute: n => (n === 'id' ? id : null), closest: sel => (sel === '#modal' ? modal : null) });
}
async function app(route, srv) {
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = { innerHTML: '' };
  const modal = modalStub();
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : node());
  win.document.querySelectorAll = sel => (/form/.test(sel) ? modal.querySelectorAll(sel) : []);
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  return { win, main, modal };
}
const fire = (ctx, type, event) => { for (const fn of ctx.win.listeners[type] || []) fn(event); };
const unloadPrompted = ctx => { let prompted = false; fire(ctx, 'beforeunload', { preventDefault() { prompted = true; }, set returnValue(v) { prompted = true; } }); return prompted; };

// ============================================================================
// Owner Settings with a pending invitation (Astra's fixture)
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
  const toasts = [];
  E.toast = m => toasts.push(m);
  const html = await go(ctx, '#/settings');
  assert.match(html, /PRIVATE-INVITEE/, 'fixture: the owner sees the pending invitation');
  assert.match(html, /Invite someone/, 'fixture: the owner sees the invitation form');
  assert.ok(E.current.accessReads, 'fixture: the access reads are kept for an in-place save');
  return { state, srv, ctx, E, toasts };
}
// The owner made this member an editor elsewhere: the server's session now
// says editor (with its own token) and a fresh invitations read is refused.
function demote(srv, state) {
  state.role = 'editor';
  srv.session = { ...srv.session, role: 'editor', workspace_context: 'ctx-editor' };
  srv.answers['/workspaces/ws/invites'] = { status: 403, message: 'owner manages membership' };
}
async function refuseRoleChanged(srv, E, answer = ROLE_CHANGED) {
  srv.answers['/customer/alerts'] = answer;
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 403 && err.accessRefused !== true);
  delete srv.answers['/customer/alerts'];
}
const workspaceForm = () => node({ id: 'settings-workspace', getAttribute: n => (n === 'id' ? 'settings-workspace' : null),
  elements: [{ name: 'expected_revision', value: '1', type: 'hidden' }, { name: 'workspace_name', value: 'Acme renamed', type: 'text', defaultValue: 'Acme' }] });
const ownerOnlyGone = (ctx, E, why) => {
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-INVITEE/, `the owner-only invitation was published ${why}`);
  assert.doesNotMatch(ctx.main.innerHTML, /Invite someone/, `the invitation form was published ${why}`);
  for (const field of ['accessReads', 'keepAccessReads']) {
    assert.doesNotMatch(JSON.stringify(E.current[field] || null), /PRIVATE-INVITEE/, `${field} kept the owner-only invitation ${why}`);
  }
};

// ============================================================================
// DEMO2-SEC-R6-01 case A: a held owner invitations read
// ============================================================================
test('SEC-R6-01 A: a role-change refusal aborts the held owner invitations read; released, it publishes nothing and nothing is retained; Settings is read again', async () => {
  const { state, srv, ctx, E } = await ownerSettings();
  const release = srv.hold('/workspaces/ws/invites');
  const before = srv.calls.length;
  const rendering = ctx.win.EWViews.render();
  await drain();
  const held = srv.calls.slice(before).find(c => c.path === '/workspaces/ws/invites');
  assert.ok(held && held.status === 200, 'fixture: the invitations read was answered 200 under the owner role (held)');
  demote(srv, state);
  const epoch = E.authorityEpoch();
  await refuseRoleChanged(srv, E);
  assert.ok(E.authorityEpoch() > epoch, 'the role change did not advance the epoch');
  assert.ok(held.signal && held.signal.aborted, 'the held invitations read was not aborted');
  assert.equal(E.current.accessReads, null, 'the access reads survived the role change');
  assert.doesNotMatch(text(ctx.main.innerHTML), /Checking access/, 'a role change showed "Checking access…"');
  const fresh = srv.count('/workspaces/ws/invites');
  release();
  await rendering;
  await drain(80);
  ownerOnlyGone(ctx, E, 'after the role change');
  assert.ok(srv.count('/workspaces/ws/invites', 403) >= 1 && srv.count('/workspaces/ws/invites') > fresh,
    'the invitations were not read fresh after the role change');
  assert.match(ctx.main.innerHTML, /Workspace access/, 'Settings was not read again and published');
});

// ============================================================================
// DEMO2-SEC-R6-01 case B: the preferences-save handoff
// ============================================================================
test('SEC-R6-01 B: a preferences save hands its access reads to the render; a role-change refusal while its session read is held voids that handoff', async () => {
  const { state, srv, ctx, E } = await ownerSettings();
  const release = srv.hold('/session');
  const sessions = srv.count('/session');
  await ctx.win.EWViews.handleSubmit(workspaceForm());
  await drain(40);
  assert.ok(srv.calls.some(c => c.method === 'PUT' && c.path === '/customer/preferences'), 'fixture: the preferences PUT went out');
  assert.ok(srv.count('/session') > sessions, 'fixture: the in-place render is waiting on its session read (held, owner payload)');
  assert.ok(E.current.renderGiven && E.current.renderGiven.access, 'fixture: the save handed its access reads to the render');
  demote(srv, state);
  await refuseRoleChanged(srv, E);
  assert.equal(E.current.accessReads, null, 'accessReads survived the role change');
  assert.equal(E.current.keepAccessReads, null, 'keepAccessReads survived the role change');
  // Every value stored after the refusal, not only the last one.
  const stored = [];
  let value = E.current.accessReads;
  Object.defineProperty(E.current, 'accessReads', { configurable: true, enumerable: true,
    get: () => value, set: v => { stored.push(JSON.stringify(v)); value = v; } });
  const invites = srv.count('/workspaces/ws/invites');
  release();
  await drain(120);
  assert.ok(!stored.some(v => /PRIVATE-INVITEE/.test(v)), 'the pre-refusal handoff was stored again after the refusal');
  ownerOnlyGone(ctx, E, 'from the pre-refusal handoff');
  assert.ok(srv.count('/workspaces/ws/invites') > invites, 'the invitations were not read again after the role change');
  assert.ok(srv.count('/workspaces/ws/invites', 403) >= 1, 'no fresh invitations read was answered under the new role');
  assert.match(ctx.main.innerHTML, /Workspace access/, 'Settings was not read again and published');
});

test('SEC-R6-01 A (save in flight): while a page save defers the re-render, the role-change refusal itself advances the epoch and aborts the held read', async () => {
  const { state, srv, ctx, E } = await ownerSettings();
  const release = srv.hold('/workspaces/ws/invites');
  const before = srv.calls.length;
  ctx.win.EWViews.render();
  await drain();
  const held = srv.calls.slice(before).find(c => c.path === '/workspaces/ws/invites');
  assert.ok(held && held.status === 200, 'fixture: the invitations read was answered 200 (held)');
  // Another form on the page is saving: the soft boundary's render waits.
  const saving = node({ id: 'settings-display', dataset: { saving: '1' }, getAttribute: n => (n === 'id' ? 'settings-display' : null) });
  const modalForms = ctx.win.document.querySelectorAll;
  ctx.win.document.querySelectorAll = sel => (/data-saving/.test(sel) ? [saving] : modalForms(sel));
  demote(srv, state);
  const epoch = E.authorityEpoch();
  await refuseRoleChanged(srv, E);
  assert.ok(E.authorityEpoch() > epoch, 'the role change advanced no epoch while its render waits');
  assert.ok(held.signal && held.signal.aborted, 'the held invitations read was not aborted while the render waits');
  release();
  await drain(40);
  assert.doesNotMatch(JSON.stringify(E.current.accessReads || null), /PRIVATE-INVITEE/, 'the held answer was stored');
});

test('SEC-R6-01 loop guard: a role-change refusal inside the soft boundary\'s own render ends it on the refusal with Retry, once', async () => {
  const { srv, ctx, E } = await ownerSettings();
  // Refused every time; past a few refusals the server stops answering, so
  // a loop ends (and fails below) instead of starving the test's timers.
  const MEMBERS = '/workspaces/ws/members';
  Object.defineProperty(srv.answers, MEMBERS, { configurable: true, enumerable: true, get() {
    if (srv.count(MEMBERS) > 6 && !srv.gates[MEMBERS]) srv.hold(MEMBERS);
    return ROLE_CHANGED;
  } });
  await refuseRoleChanged(srv, E);
  await drain(80);
  const calls = srv.calls.length;
  await drain(80);
  assert.equal(srv.calls.length, calls, 'the soft boundary kept reading (loop)');
  assert.ok(srv.count('/workspaces/ws/members', 403) <= 2, 'members asked ' + srv.count('/workspaces/ws/members', 403) + ' times');
  assert.match(text(ctx.main.innerHTML), /Settings are unavailable: Your role in this workspace changed\./);
  assert.match(ctx.main.innerHTML, /data-action="retry"/);
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-INVITEE/);
});

// ============================================================================
// Soft boundary: a context token that changes only in role
// ============================================================================
test('SEC-R6-01 token: a session read whose token changes only in role takes the soft boundary: epoch, fresh route, typed entries kept (not a workspace switch)', async () => {
  const { state, srv, ctx, E } = await ownerSettings();
  E.current.dialogDrafts.set('alert-form|{}|#/settings', { basis: '{}', values: { name: 'typed' } });
  E.current.filters = { show: 'kept' };
  demote(srv, state);
  const epoch = E.authorityEpoch();
  await ctx.win.EWViews.reconcileCanonical();
  await drain(80);
  assert.ok(E.authorityEpoch() > epoch, 'the role change did not advance the epoch');
  assert.equal(E.current.workspaceContext, 'ctx-editor', 'the new token was not adopted');
  assert.equal(E.current.activeWorkspace, 'ws');
  assert.ok(E.current.dialogDrafts.has('alert-form|{}|#/settings'), 'a role change dropped kept dialog entries like a workspace switch');
  assert.deepEqual(JSON.parse(JSON.stringify(E.current.filters)), { show: 'kept' }, 'a role change reset the filters like a workspace switch');
  ownerOnlyGone(ctx, E, 'after the role-only token change');
  assert.ok(srv.count('/workspaces/ws/invites', 403) >= 1, 'Settings was not read again');
  assert.doesNotMatch(text(ctx.main.innerHTML), /Checking access/);
});

// ============================================================================
// DEMO2-R13-02: the client keys on the marker, not the message
// ============================================================================
test('R13-02: a 403 marked reason "role_changed" takes the soft boundary whatever its message', async () => {
  const { srv, E } = await ownerSettings();
  const epoch = E.authorityEpoch();
  await refuseRoleChanged(srv, E, { status: 403, message: 'Your role changed.', reason: 'role_changed' });
  assert.ok(E.authorityEpoch() > epoch, 'the marked refusal was not recognised');
  assert.equal(E.current.accessReads, null);
});
test('R13-02: the bare message "workspace role changed" without the marker is an ordinary role refusal', async () => {
  const { srv, E } = await ownerSettings();
  const epoch = E.authorityEpoch();
  await refuseRoleChanged(srv, E, { status: 403, message: 'workspace role changed' });
  assert.equal(E.authorityEpoch(), epoch, 'the message text alone started the soft boundary');
  assert.ok(E.current.accessReads, 'the message text alone cleared the access reads');
});
test('R13-02: the client source compares error.reason, not the message text', () => {
  const src = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
  assert.ok(src.includes("error.reason === 'role_changed'"), 'the client does not compare error.reason');
  assert.ok(!src.includes("'workspace role changed'"), 'the client still matches the server message text');
});

// ============================================================================
// DEMO2-R13-N1: a save answered under a token that changed only in role
// ============================================================================
test('R13-N1: a preferences save answered under a token that changed only in role says the role changed, not "Saved in ws."', async () => {
  const { state, srv, ctx, E, toasts } = await ownerSettings();
  demote(srv, state);
  await ctx.win.EWViews.handleSubmit(workspaceForm());
  await drain(120);
  assert.ok(toasts.some(t => /^Saved\. Your role in this workspace changed\.$/.test(t)), 'no role-change save copy: ' + toasts.join(' | '));
  assert.ok(!toasts.some(t => /^Saved in /.test(t)), 'told "Saved in ws." for a role change: ' + toasts.join(' | '));
  const notice = JSON.stringify(E.current.outcomeNotice || {});
  assert.doesNotMatch(notice, /"saved"/, 'the durable notice says the change went to another workspace');
  ownerOnlyGone(ctx, E, 'after the save under the editor token');
});

// ============================================================================
// DEMO2-R13-01: a signed-out save with a dialog holding typed text
// ============================================================================
const SIGNED_OUT = /You were signed out\. Copy anything you typed below before you sign in again; it won(?:'|&#39;)t be kept\./;
async function alertSignedOut(typed) {
  const shows = [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }];
  const data = (p, init = {}) => {
    if (p === '/customer/shows') return { shows };
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/alerts' || p.startsWith('/customer/alerts?')) return init.method && init.method !== 'GET' ? { alert: { id: 1 } } : { alerts: [] };
    if (p.startsWith('/customer/filter-values')) return { items: [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
    if (p.startsWith('/customer/summary')) return { groups: [] };
    return undefined;
  };
  const srv = server(data, HOSTED);
  const ctx = await app('#/alerts', srv);
  const E = ctx.win.EW;
  E.toast = () => {};
  await go(ctx, '#/alerts');
  await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  assert.equal(ctx.modal.open, true, 'fixture: Create alert opened');
  assert.match(ctx.modal.innerHTML, /PRIVATE-SHOW/, 'fixture: the dialog names the private show');
  ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', typed], ['show', 'cs-private', 'select-one', undefined]], { dirty: '1' })];
  srv.signedOut = true;
  srv.answers['/customer/alerts'] = { method: 'POST', status: 401, code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required' };
  await assert.rejects(E.api('/customer/alerts', { method: 'POST', body: { name: typed } }), err => err.status === 401 && err.signedOut === true);
  await drain(80);
  return { srv, ctx, E };
}

test('R13-01 P3: signed out with a dialog holding typed text: the dialog says so plainly and shows the typed text to copy; no server names; the leave prompt counts it', async () => {
  const { ctx, E } = await alertSignedOut('My typed alert');
  assert.equal(E.current.principal, null, 'fixture: the re-read confirmed the sign-out');
  assert.equal(ctx.modal.open, true, 'the dialog holding typed text closed');
  const said = text(ctx.modal.innerHTML);
  assert.match(ctx.modal.innerHTML, SIGNED_OUT, 'the dialog does not say what happens to the typed text: ' + said);
  assert.doesNotMatch(said, /typed text is kept/, 'the dialog still claims the typed text is kept');
  assert.match(ctx.modal.innerHTML, /<textarea[^>]*readonly[^>]*>My typed alert<\/textarea>/, 'the typed text is not shown read-only to copy');
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW|cs-private/, 'a server value is shown with the typed text');
  assert.match(ctx.main.innerHTML, SIGNED_OUT, 'the signed-out page does not say what happens to the typed text');
  assert.match(ctx.main.innerHTML, /My typed alert/, 'the signed-out page does not show the typed text');
  assert.match(ctx.main.innerHTML, /data-action="signin-start"/, 'fixture: the sign-in state is shown');
  assert.equal(unloadPrompted(ctx), true, 'the leave-page prompt ignores the held typed text');
});

test('R13-01: the typed text is escaped wherever it is shown', async () => {
  const typed = '<img src=x onerror="alert(1)">&amp;';
  const { ctx } = await alertSignedOut(typed);
  for (const [where, html] of [['dialog', ctx.modal.innerHTML], ['page', ctx.main.innerHTML]]) {
    assert.ok(!html.includes('<img src=x'), `the ${where} renders the typed text as markup`);
    assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;amp;'), `the ${where} does not show the escaped typed text`);
  }
});

test('R13-01: the boundary\'s held notice (still signed in) no longer claims the text is kept and shows it to copy', async () => {
  const shows = [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }];
  const srv = server((p, init = {}) => {
    if (p === '/customer/shows') return { shows };
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/alerts') return init.method && init.method !== 'GET' ? { alert: { id: 1 } } : { alerts: [] };
    if (p.startsWith('/customer/filter-values')) return { items: [{ value: 'cs-private', label: 'PRIVATE-SHOW' }] };
    return undefined;
  }, HOSTED);
  const ctx = await app('#/alerts', srv);
  ctx.win.EW.toast = () => {};
  await go(ctx, '#/alerts');
  await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  ctx.modal.forms = [formStub('alert-form', ctx.modal, [['name', 'Held alert']], { dirty: '1' })];
  srv.answers['/customer/alerts'] = { status: 403, message: 'workspace membership required', access: 'lost' };
  await assert.rejects(ctx.win.EW.api('/customer/alerts'));
  const said = text(ctx.modal.innerHTML);
  assert.doesNotMatch(said, /typed text is kept/, 'the held notice claims the text is kept: ' + said);
  assert.match(ctx.modal.innerHTML, /<textarea[^>]*readonly[^>]*>Held alert<\/textarea>/, 'the held notice does not show the typed text');
  assert.doesNotMatch(ctx.modal.innerHTML, /PRIVATE-SHOW/);
});
