/* Hosted Connections: the Agent tokens section (agent access design rev 4,
   section 4; code review AGT-R1-02, AGT-R1-03, AGT-R1-04; Astra
   AGT-SEC-R1-01). Owner, editor, viewer, failed-load and local views; the
   create body; the one-time reveal and its binding to the issuing
   workspace and principal; revoke; the write hold.
   Loader copied from frontend_uat_library.test.cjs.
   Run: node --test tests/frontend_agent_tokens.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const read = f => fs.readFileSync(path.join(APP, f), 'utf8');
const ROUTE = '#/connections';
const SECRET = 'ewt_SENTINEL<b>&"x"';
const URL_ = 'https://agents.example.test/mcp';
const SCOPES = ['research:read', 'transcripts:read', 'watchlists:read', 'campaigns:read', 'notes:read'];
const DEFAULTS = SCOPES.slice(0, 4);
const A = { ctx: 'ctx-A', ws: 'ws-a', principal: 'oidc:https://i.example/:ed-1' };
const B = { ctx: 'ctx-B', ws: 'ws-b', principal: 'oidc:https://i.example/:ed-1' };
const C = { ctx: 'ctx-A', ws: 'ws-a', principal: 'oidc:https://i.example/:other' };

// The signed-in session the app boots from; setContext moves it.
const live = { c: A };
const sessionFetch = async (url) => {
  if (url === '/api/v1/session') {
    const c = live.c;
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: true,
      csrf_token: 's'.repeat(32), principal: c.principal, active_workspace: c.ws, workspaces: [c.ws],
      workspace_context: c.ctx, workspace_switch_revision: 7, role: 'editor',
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: sessionFetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(read(file), sandbox, { filename: file });
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };

function listing(over = {}) {
  return { ok: true, tokens: [], mcp_url: URL_, can_create: true, scopes: SCOPES,
    default_scopes: DEFAULTS, days: [1, 7, 14], default_days: 7, cap: 3, ...over };
}
function token(over = {}) {
  return { id: 'cg_1', member_id: A.principal, member_email: '', own: true, label: 'Claude Code',
    scopes: DEFAULTS, created_at: '2026-10-21T10:00:00+00:00', expires_at: '2026-10-28T10:00:00+00:00',
    revoked_at: null, last_used_at: null, status: 'Awaiting authorization', ...over };
}

async function boot({ hosted = true, ctx = A } = {}) {
  live.c = ctx;
  const win = loadAt(ROUTE);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, toggle() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flush();
  win.EW.current.hosted = hosted;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {}, toggle() {} } });
  win.EW.header = () => {};
  const calls = [];
  const toasts = [];
  win.EW.toast = m => { toasts.push(String(m)); };
  const state = { list: listing(), failList: false, create: null, revoke: null };
  win.EW.api = async (p, o = {}) => {
    calls.push({ path: p, method: o.method || 'GET', body: o.body });
    if (p === '/customer/reader-status') return { configured: false, profile: {} };
    if (p === '/customer/preferences') return { preferences: {} };
    if (p === '/customer/agent-tokens' && (o.method || 'GET') === 'GET') {
      if (state.failList) throw Object.assign(new Error('list failed'), { status: 500 });
      return state.list;
    }
    if (p === '/customer/agent-tokens' && o.method === 'POST') {
      if (state.create instanceof Error) throw state.create;
      return state.create;
    }
    if (p.startsWith('/customer/agent-tokens/') && o.method === 'POST') {
      if (state.revoke instanceof Error) throw state.revoke;
      return { ok: true, revoked: 'x' };
    }
    throw new Error('unexpected request ' + p);
  };
  setContext(win, ctx);
  win.location.hash = ROUTE;
  win.EW.current.route = ROUTE;
  const render = async () => { await win.EWViews.render(); await settle(); return main.innerHTML; };
  return { win, main, calls, toasts, state, render };
}
function setContext(win, c) {
  live.c = c;
  win.EW.current.workspaceContext = c.ctx;
  win.EW.current.activeWorkspace = c.ws;
  win.EW.current.principal = c.principal;
}
const section = html => {
  const m = /<section[^>]*id="agent-tokens"[\s\S]*?<\/section>/.exec(html);
  return m ? m[0] : '';
};

function createForm(entries, checked) {
  const submit = { disabled: false, textContent: 'Create token', setAttribute() {}, focus() {} };
  const errors = [];
  return {
    errors,
    form: {
      dataset: {}, elements: [], isConnected: true,
      getAttribute: k => (k === 'id' ? 'agent-token-create' : null), closest: () => null,
      querySelector: sel => (sel === '[type=submit]' ? submit : sel === '[data-form-errors]' ? { innerHTML: '' } : { focus() {}, setAttribute() {}, removeAttribute() {} }),
      querySelectorAll: sel => (sel === 'input[name=scopes]:checked' ? checked.map(value => ({ value })) : sel === '[type=submit]' ? [submit] : []),
      entries,
    },
  };
}
async function submitCreate(u, { label = 'Claude Code', scopes = DEFAULTS, days = '7' } = {}) {
  const entries = [['label', label], ...scopes.map(s => ['scopes', s]), ['expires_in_days', days]];
  u.win.FormData = class { constructor(f) { this.f = f; } *entries() { yield* this.f.entries; } };
  const errors = [];
  u.win.EW.formError = (message) => { errors.push(message); return false; };
  const { form } = createForm(entries, scopes);
  await u.win.EWViews.handleSubmit(form);
  await settle();
  return errors;
}
function created(over = {}) {
  return { ok: true, id: 'cg_1', token: SECRET, mcp_url: URL_, expires_at: '2026-10-28T10:00:00+00:00', scopes: DEFAULTS, ...over };
}

// -- views ----------------------------------------------------------------
test('editor: the create form ticks every scope but evidence review notes and defaults to 7 days', async () => {
  const u = await boot();
  const html = section(await u.render());
  assert.match(html, /<h2 id="agent-tokens-title">Agent tokens<\/h2>/);
  assert.match(html, /<form id="agent-token-create"/);
  for (const s of DEFAULTS) assert.match(html, new RegExp(`value="${s}" checked`));
  assert.match(html, /value="notes:read" >/);
  assert.match(html, /Evidence review notes/);
  assert.match(html, /Campaigns, including campaign briefs, their notes and assessment reasons/);
  assert.match(html, /<option value="7" selected>7 days<\/option>/);
  assert.match(html, /<h3>Your tokens<\/h3>/);
  assert.match(html, /No agent tokens yet\./);
});

test('viewer: no form, the editors-and-owners note', async () => {
  const u = await boot();
  u.state.list = listing({ can_create: false });
  const html = section(await u.render());
  assert.doesNotMatch(html, /agent-token-create/);
  assert.match(html, /Only editors and owners can create agent tokens\./);
});

test('owner: every member\'s tokens under "Tokens in this workspace", with readable names; expired and revoked rows have no Revoke', async () => {
  const u = await boot();
  u.state.list = listing({ tokens: [
    token({ id: 'cg_own' }),
    token({ id: 'cg_other', own: false, member_id: 'oidc:https://i.example/:ed-2', member_email: 'ed2@example.test', label: 'Laptop' }),
    token({ id: 'cg_noname', own: false, member_id: 'oidc:https://i.example/:ed-3', member_email: '' }),
    token({ id: 'cg_exp', status: 'Expired' }),
    token({ id: 'cg_rev', status: 'Revoked', revoked_at: '2026-10-22T10:00:00+00:00' }),
  ] });
  const html = section(await u.render());
  assert.match(html, /<h3>Tokens in this workspace<\/h3>/);
  assert.doesNotMatch(html, /Your tokens/);
  assert.match(html, /ed2@example\.test/);
  assert.match(html, /ed-3/);
  assert.doesNotMatch(html, /oidc:https:\/\/i\.example\/:ed-3/);
  const row = id => (new RegExp(`<li data-agent-token="${id}">[\\s\\S]*?</li>`).exec(html) || [''])[0];
  assert.match(row('cg_own'), /data-action="agent-token-revoke" data-id="cg_own"/);
  assert.match(row('cg_other'), /data-action="agent-token-revoke"/);
  assert.match(row('cg_exp'), /Expired/);
  assert.doesNotMatch(row('cg_exp'), /agent-token-revoke/);
  assert.doesNotMatch(row('cg_rev'), /agent-token-revoke/);
  assert.match(row('cg_own'), /Not used yet/);
});

test('failed load: a Retry, and the rest of Connections still renders', async () => {
  const u = await boot();
  u.state.failList = true;
  const html = await u.render();
  assert.match(section(html), /Agent tokens could not be loaded\./);
  assert.match(section(html), /data-action="retry"/);
  assert.match(html, /Reader access/);
});

test('local: no section and no token read', async () => {
  const u = await boot({ hosted: false });
  const html = await u.render();
  assert.equal(section(html), '');
  assert.ok(!u.calls.some(c => c.path.startsWith('/customer/agent-tokens')));
});

// -- create ---------------------------------------------------------------
test('create sends exactly {label, scopes, expires_in_days}, shows the escaped command once, and the next render drops it', async () => {
  const u = await boot();
  await u.render();
  u.state.create = created();
  u.state.list = listing({ tokens: [token()] });
  const errors = await submitCreate(u, { label: 'My laptop', scopes: ['research:read', 'notes:read'], days: '14' });
  assert.deepEqual(errors, []);
  const post = u.calls.find(c => c.method === 'POST');
  assert.equal(post.path, '/customer/agent-tokens');
  assert.equal(JSON.stringify(post.body), JSON.stringify({ label: 'My laptop', scopes: ['research:read', 'notes:read'], expires_in_days: 14 }));
  const shown = section(u.main.innerHTML);
  assert.match(shown, /You see the token only once/);
  assert.ok(shown.includes('Bearer ewt_SENTINEL&lt;b&gt;&amp;&quot;x&quot;'), 'escaped token in the command');
  assert.ok(shown.includes(`--scope user earwitness ${URL_}`));
  assert.ok(!shown.includes(SECRET), 'never raw');
  assert.match(shown, /shell history and in ~\/\.claude\.json/);
  const again = section(await u.render());
  assert.ok(!again.includes('SENTINEL'), 'the next render drops it');
});

test('client-side refusals: no scope ticked, or a bad label, sends nothing', async () => {
  const u = await boot();
  await u.render();
  assert.equal((await submitCreate(u, { scopes: [] })).length, 1);
  assert.equal((await submitCreate(u, { label: '<script>' })).length, 1);
  assert.ok(!u.calls.some(c => c.method === 'POST'));
});

// -- AGT-SEC-R1-01 ---------------------------------------------------------
async function createdThenRender(u, before) {
  await u.render();
  u.state.create = created();
  u.state.list = listing({ tokens: [token()] });
  // Hold the follow-up render: capture the reveal without rendering it.
  const realRender = u.win.EWViews.render;
  let rendered = 0;
  if (before) u.win.EW.render = async () => { rendered++; };
  await submitCreate(u);
  if (before) u.win.EW.render = realRender;
  return rendered;
}

test('SEC-R1-01: a failed follow-up render drops the token; a later render in the same context does not show it', async () => {
  const u = await boot();
  await u.render();
  u.state.create = created();
  u.state.failList = true;
  await submitCreate(u);
  assert.ok(!u.main.innerHTML.includes('SENTINEL'));
  u.state.failList = false;
  u.state.list = listing({ tokens: [token()] });
  assert.ok(!(await u.render()).includes('SENTINEL'));
});

test('SEC-R1-01: failed render, then a workspace reset and another workspace: never shown', async () => {
  const u = await boot();
  await u.render();
  u.state.create = created();
  u.state.failList = true;
  await submitCreate(u);
  u.win.EW.clearWorkspaceState();
  setContext(u.win, B);
  u.state.failList = false;
  u.state.list = listing({ can_create: false, tokens: [] });
  assert.ok(!(await u.render()).includes('SENTINEL'));
});

for (const [name, other] of [['another workspace', B], ['another principal', C]]) {
  test(`SEC-R1-01: created in A, rendered first under ${name}: dropped, and not shown back in A`, async () => {
    const u = await boot();
    const rendered = await createdThenRender(u, true);
    assert.equal(rendered, 1, 'the success asked for one render');
    setContext(u.win, other);
    u.state.list = listing({ tokens: [token({ id: 'cg_1' })] });
    assert.ok(!(await u.render()).includes('SENTINEL'));
    setContext(u.win, A);
    assert.ok(!(await u.render()).includes('SENTINEL'), 'the mismatch discarded it');
  });
}

test('SEC-R1-01: the reveal needs the created token in the returned list', async () => {
  const u = await boot();
  await createdThenRender(u, true);
  u.state.list = listing({ tokens: [token({ id: 'cg_someone_else' })] });
  assert.ok(!(await u.render()).includes('SENTINEL'));
});

test('SEC-R1-01 control: same workspace and principal, the token in the list: shown once', async () => {
  const u = await boot();
  await createdThenRender(u, true);
  const html = await u.render();
  assert.ok(html.includes('ewt_SENTINEL'));
  assert.ok(!(await u.render()).includes('SENTINEL'));
});

test('AGT-R2-02: leaving Connections before create finishes: a later Connections render shows no command', async () => {
  const u = await boot();
  await u.render();
  let finish;
  u.state.create = new Promise(res => { finish = res; });
  u.state.list = listing({ tokens: [token()] });
  const realRender = u.win.EW.render;
  u.win.EW.render = async () => {}; // the other page's render is not under test
  const pending = submitCreate(u);
  await settle();
  u.win.location.hash = '#/search';
  u.win.EW.current.route = '#/search';
  finish(created());
  await pending;
  u.win.EW.render = realRender;
  u.win.location.hash = ROUTE;
  u.win.EW.current.route = ROUTE;
  assert.ok(!(await u.render()).includes('SENTINEL'));
});

// -- revoke ---------------------------------------------------------------
test('revoke posts to the encoded path, toasts and re-renders', async () => {
  const u = await boot();
  u.state.list = listing({ tokens: [token({ id: 'cg_a/b' })] });
  await u.render();
  let renders = 0;
  const real = u.win.EW.render;
  u.win.EW.render = async () => { renders++; };
  await u.win.EWViews.handleAction({ dataset: { action: 'agent-token-revoke', id: 'cg_a/b', name: 'Claude Code' } });
  u.win.EW.render = real;
  const post = u.calls.find(c => c.method === 'POST');
  assert.equal(post.path, '/customer/agent-tokens/cg_a%2Fb/revoke');
  assert.match(u.toasts.join('\n'), /Revoked Claude Code/);
  assert.equal(renders, 1);
});

test('revoke with an unknown outcome says it is unconfirmed, never "not revoked" (AGT-R1-03)', async () => {
  const u = await boot();
  await u.render();
  u.state.revoke = Object.assign(new Error('timeout'), { outcomeUnknown: true });
  await u.win.EWViews.handleAction({ dataset: { action: 'agent-token-revoke', id: 'cg_1', name: 'x' } });
  assert.doesNotMatch(u.toasts.join('\n'), /not revoked/);
  assert.ok(u.toasts.length >= 1);
  u.toasts.length = 0;
  u.state.revoke = Object.assign(new Error('refused'), { status: 404 });
  await u.win.EWViews.handleAction({ dataset: { action: 'agent-token-revoke', id: 'cg_1', name: 'x' } });
  assert.match(u.toasts.join('\n'), /not revoked: refused/);
});

test('create and revoke are held during a pending workspace switch', async () => {
  const u = await boot();
  await u.render();
  const seen = [];
  const root = { querySelectorAll: sel => { seen.push(sel); return []; }, querySelector: () => null };
  u.win.EW.current.switchWaiting = 1; // a switch in progress holds every write
  u.win.EWViews.syncWriteControls(root);
  u.win.EW.current.switchWaiting = 0;
  const all = seen.join('\n');
  assert.ok(all.includes('#agent-token-create [type=submit]'));
  assert.ok(all.includes('[data-action="agent-token-revoke"]'));
});
