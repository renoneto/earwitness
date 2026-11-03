/* DEMO2 pass 15.
   Owner decision: the server marks a lost-access refusal with
   error.access === "lost" (status 403 and code FORBIDDEN unchanged). The
   customer app starts its authority boundary on 401, or on 403 with that
   marker. 428 never starts one; every other 403 stays local to its action
   and shows the server's sentence. The per-request local exceptions of
   pass 14 are gone: the marker replaces them. Everything else of pass 14
   stays (tests/frontend_demo2_pass14.test.cjs).
   Run: node --test tests/frontend_demo2_pass15.test.cjs */
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
      out = reply(status, { ok: false, error: { code: status === 401 ? 'SIGNIN_REQUIRED' : 'FORBIDDEN', message: 'You no longer have access to this workspace.' } });
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
const STATUSES = [401, 403, 428];
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


const ROLE = "Your role in this workspace can't make this change. Ask an owner for editor access.";
async function onSearch() {
  const w = world(); const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/);
  return { w, srv, ctx, E: ctx.win.EW };
}
function still(ctx, E, epoch, what) {
  assert.equal(E.authorityEpoch(), epoch, what + ' started an authority boundary');
  assert.ok(E.retained('searchItems'), what + ' dropped the retained rows');
  assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/, what + ' took the page down');
}

test('a role 403 on Export (no marker) stays local and shows the server sentence', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/research-export'] = { status: 403, message: "Your role in this workspace can't export. Ask an owner for editor access." };
  const toasts = []; E.toast = m => toasts.push(m);
  await ctx.win.EWViews.handleAction({ dataset: { action: 'export-all' } });
  still(ctx, E, epoch, 'a role 403 on Export');
  assert.ok(toasts.some(t => /^Your role in this workspace can't export/.test(t)), toasts.join(' | '));
});
test('a role 403 on the invitations read (no marker) stays local', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/workspaces/ws/invites'] = { status: 403, message: 'owner manages membership' };
  await assert.rejects(E.api('/workspaces/ws/invites'), err => err.status === 403 && /owner manages membership/.test(err.message));
  still(ctx, E, epoch, 'a role 403 on invitations');
});
test('a role 403 on a write (no marker) stays local with the server sentence', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/saved-items'] = { status: 403, message: ROLE };
  await assert.rejects(E.api('/customer/saved-items', { method: 'POST', body: {} }), err => err.status === 403 && err.message === ROLE);
  still(ctx, E, epoch, 'a role 403 on a write');
});
test('a stale-revision 428 on a write stays local', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/saved-items'] = { status: 428, code: 'PRECONDITION_REQUIRED', message: 'expected_revision is required' };
  await assert.rejects(E.api('/customer/saved-items', { method: 'POST', body: {} }), err => err.status === 428);
  still(ctx, E, epoch, 'a 428');
});
test('a 428 on a read stays local too (428 never starts a boundary)', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/alerts'] = { status: 428, code: 'PRECONDITION_REQUIRED', message: 'precondition required' };
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 428);
  still(ctx, E, epoch, 'a read 428');
});
test('a 403 with access "lost" starts the boundary: the page is checked again and retained data goes', async () => {
  const { srv, ctx, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/alerts'] = { status: 403, message: 'workspace membership required', access: 'lost' };
  srv.refuse = 403;
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 403);
  assert.ok(E.authorityEpoch() > epoch, 'a lost-access 403 started no boundary');
  assert.equal(E.retained('searchItems'), null);
  await drain(40);
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
});
test('a 401 starts the boundary', async () => {
  const { srv, E } = await onSearch();
  const epoch = E.authorityEpoch();
  srv.answers['/customer/alerts'] = { status: 401, code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required' };
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 401);
  assert.ok(E.authorityEpoch() > epoch);
});
test('the per-request local exceptions are gone: the marker replaces them', () => {
  for (const f of ['app.js', 'views.js']) {
    const code = fs.readFileSync(path.join(APP, f), 'utf8');
    assert.doesNotMatch(code, /localRefusal/, f + ' still declares local refusals per request');
  }
  const appjs = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
  assert.match(appjs, /function startsBoundary\([^)]*\)\s*\{[\s\S]{0,400}access === 'lost'/, 'the boundary keys on the marker');
});

// -- Pass 15 verification: every client rule keys on the boundary --------
// The kept-read cache and the Search retained frame used their own
// 401/403/428 list. They now follow the same rule: only a refusal that
// started the boundary (401, or 403 marked lost) clears them.
for (const [label, answer] of [
  ['a role 403 (no marker)', { status: 403, message: ROLE }],
  ['a 428', { status: 428, code: 'PRECONDITION_REQUIRED', message: 'precondition required' }]]) {
  test(`${label} on a shared read keeps the other kept answers`, async () => {
    const { srv, E } = await onSearch();
    await E.sharedRead('/customer/shows');
    const n = srv.count('/customer/shows');
    srv.answers['/customer/brands'] = answer;
    await assert.rejects(E.sharedRead('/customer/brands'), err => err.status === answer.status);
    await drain();
    await E.sharedRead('/customer/shows', { authorizedBy: { ok: true } });
    assert.equal(srv.count('/customer/shows'), n, 'the kept answers were cleared by a refusal that is local');
  });
  test(`${label} on the Search read keeps the earlier results, labeled`, async () => {
    const { srv, ctx } = await onSearch();
    srv.answers['/customer/research-query'] = answer;
    const html = await go(ctx, SEARCH);
    assert.match(text(html), /Showing the earlier results for this scope below/);
  });
}
test('control: a lost-access 403 on a shared read clears the kept answers', async () => {
  const { srv, E } = await onSearch();
  await E.sharedRead('/customer/shows');
  const n = srv.count('/customer/shows');
  srv.answers['/customer/brands'] = { status: 403, message: 'workspace membership required', access: 'lost' };
  await assert.rejects(E.sharedRead('/customer/brands'), err => err.status === 403);
  await drain();
  await E.sharedRead('/customer/shows', { authorizedBy: { ok: true } });
  assert.ok(srv.count('/customer/shows') > n, 'a lost-access refusal kept the answers');
});
