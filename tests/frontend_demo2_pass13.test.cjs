/* DEMO2 pass 13.
   Owner rule (unchanged): nothing retained or cache-backed is shown on a
   navigation, restore or dialog without a fresh successful authority
   check on that navigation. A 401, 403 or 428 removes retained content.
   One mechanism closes the class: the authority epoch (EW.authorityEpoch).
   It advances at every authority boundary: a navigation away from the
   page on screen, a restore (pageshow persisted, a same-hash popstate with
   stored state), any 401/403/428, a workspace change. Retained views,
   kept shared reads, async dialog openers and in-dialog fills carry the
   epoch they were validated under and publish only while it still holds.
   Built from Astra's round-3 reproductions (demo2-memo-astra-r3.md):
   DEMO2-SEC-R3-01  Search republished its old frame after a failed (500,
                    transport) refresh on a new navigation.
   DEMO2-SEC-R3-02  a same-hash history restore kept Search's frame while
                    asking and an open dialog after the refusal.
   DEMO2-SEC-R3-03  a dialog opener started before a persisted restore
                    opened with pre-restore data after the restore refused.
   Only fetch and the dialog element are simulated: the real EW.api,
   apiCall, sharedRead, render, handleAction, openDialog and the registered
   window listeners run.
   Run: node --test tests/frontend_demo2_pass13.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };

function loadPages(route, fetch) {
  const listeners = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
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

// A server whose workspace reads can be refused (every read answers the
// given status) or held open (gate on one path). Only fetch is simulated.
function server(data) {
  const state = { refuse: null, calls: [], gates: {} };
  state.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    const call = { method: init.method || 'GET', path: p };
    state.calls.push(call);
    const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
    if (p === '/session') return reply(200, SESSION);
    // The answer is decided when the request arrives; a held one is
    // delivered later, unchanged.
    let out;
    if (state.refuse) {
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
async function app(route, srv) {
  const win = loadPages(route, srv.fetch);
  for (let i = 0; i < 5; i++) await tick();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.matchMedia = () => ({ matches: false });
  win.EW.header = () => {};
  return { win, main };
}
async function go(ctx, route) {
  ctx.win.location.hash = route;
  ctx.win.EW.current.route = route;
  await ctx.win.EWViews.render();
  return ctx.main.innerHTML;
}
const fire = (ctx, type, event) => { for (const fn of ctx.win.listeners[type] || []) fn(event); };

const fresh = () => ({ revision: 'r1', shows: [{ id: 'cs-private', display_title: 'PRIVATE-SHOW', episodes: 3 }] });
const data = w => (p, init = {}) => {
  if (p === '/customer/shows') return { shows: w.shows };
  if (p === '/customer/brands') return { brands: [] };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  if (p === '/customer/alerts') return { alerts: [] };
  if (p === '/customer/research-query') {
    const body = init.body ? JSON.parse(init.body) : {};
    return { query: body.query, revision: w.revision, coverage: {}, summary: { week: [] },
      result: { items: [], total: 0, per_page: 10, total_relation: 'exact' } };
  }
  return undefined;
};
const SEARCH = '#/search?mode=mentions&q=Muse&shows=cs-private';
const OTHER = '#/search?mode=mentions&q=Other&shows=cs-private';


// A workspace read failure that is not a refusal: an HTTP 500 or a
// transport failure (fetch rejects). /session still answers.
function failable(srv) {
  const base = srv.fetch;
  srv.fetch = async (url, init = {}) => {
    const p = url.replace(/^\/api\/v1/, '');
    if (p === '/session' || !srv.failKind) return base(url, init);
    srv.calls.push({ method: init.method || 'GET', path: p, status: srv.failKind });
    if (srv.failKind === 'transport') throw new TypeError('Failed to fetch');
    return { ok: false, status: 500, headers: { get: () => null }, text: async () => JSON.stringify({ ok: false, error: { code: 'INTERNAL', message: 'The service had an error.' } }) };
  };
  return srv;
}
function modalStub() {
  return { open: false, innerHTML: '', dataset: {}, classList: { add() {}, remove() {} }, setAttribute() {}, removeAttribute() {},
    showModal() { this.open = true; }, close() { this.open = false; }, querySelector: sel => (sel === 'button' ? { focus() {} } : null), querySelectorAll: () => [],
    addEventListener() {}, focus() {} };
}
function withModal(ctx) {
  const modal = modalStub();
  const sel = ctx.win.document.querySelector;
  ctx.win.document.querySelector = s => (s === '#modal' ? modal : sel(s));
  return modal;
}
const SRC = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
const APPSRC = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');

// -- DEMO2-SEC-R3-01: a failed refresh on a new navigation shows nothing old --
for (const kind of [500, 'transport']) {
  for (const [label, back] of [['same query', SEARCH], ['changed query', OTHER]]) {
    test(`SEC-R3-01 Search ${label}: back from another page, every fresh read fails (${kind}); the old frame never returns`, async () => {
      const w = fresh();
      const srv = failable(server(data(w)));
      const ctx = await app(SEARCH, srv);
      assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show');
      await go(ctx, '#/shows');
      srv.failKind = kind;
      const before = srv.calls.length;
      const html = await go(ctx, back);
      assert.ok(srv.calls.slice(before).some(c => c.path === '/customer/research-query' && c.status === kind), 'the visit asked and failed');
      assert.ok(!srv.calls.slice(before).some(c => c.status === 200), 'fixture: no fresh read succeeded');
      if (label === 'same query') {
        // M9-BACK (owner exception to this rule, brief M9-BACK): the page this
        // tab last rendered for this exact route is kept; a failed (not
        // refused) refresh keeps it, labeled as the earlier results under the
        // failure, as on the page itself. A refusal still removes it (pass 12).
        assert.match(html, /PRIVATE-SHOW/, 'M9-BACK: the kept page stays after a failed refresh');
        assert.match(text(html), /Showing the earlier results for this scope below; they are not new results/, 'the kept results are labeled as earlier ones');
      } else {
        assert.doesNotMatch(html, /PRIVATE-SHOW/, 'the old frame was republished after an unvalidated failure');
        assert.doesNotMatch(text(html), /Showing the earlier results/, 'earlier results offered on a new navigation');
      }
      assert.match(text(html), /Search didn’t finish/, 'the failure is told');
    });
  }
}
test('SEC-R3-01 control: a real 500 on the page itself (frame on screen) still keeps the earlier results, labeled', async () => {
  const w = fresh();
  const srv = failable(server(data(w)));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  srv.failKind = 500;
  const html = await go(ctx, SEARCH);
  assert.ok(srv.count('/customer/research-query', 500) >= 1, 'fixture: the retry got a real 500');
  assert.match(text(html), /Showing the earlier results for this scope below/);
});

// -- DEMO2-SEC-R3-02: a same-hash restore is a restore boundary ----------------
// Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
for (const status of [401, 403]) {
  test(`SEC-R3-02 same-hash popstate on Search (${status}): nothing old while asking, nothing after the refusal`, async () => {
    const w = fresh();
    const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/);
    srv.refuse = status;
    const before = srv.count('/customer/research-query', status);
    const release = srv.hold('/customer/research-query');
    fire(ctx, 'popstate', { state: {} });
    await drain();
    assert.ok(srv.calls.some(c => c.path === '/customer/research-query' && c.status === status), 'fixture: the fresh read is pending');
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the retained frame stayed up while the restore asked');
    release();
    await drain(40);
    assert.ok(srv.count('/customer/research-query', status) > before);
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
    assert.match(text(ctx.main.innerHTML), /Search is unavailable/);
  });
  test(`SEC-R3-02 same-hash popstate (${status}) closes the real Create alert dialog before the route reads publish`, async () => {
    const w = fresh();
    const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    const modal = withModal(ctx);
    await go(ctx, SEARCH);
    await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    assert.equal(modal.open, true, 'fixture: the real alert dialog opened');
    assert.match(modal.innerHTML, /PRIVATE-SHOW/, 'fixture: it names the private show');
    srv.refuse = status;
    const release = srv.hold('/customer/research-query');
    fire(ctx, 'popstate', { state: {} });
    assert.equal(modal.open, false, 'the dialog stayed open across the restore while the route asked');
    release();
    await drain(40);
    assert.equal(modal.open, false, 'the dialog is open after the refusal');
    assert.match(text(ctx.main.innerHTML), /Search is unavailable/);
  });
}
test('SEC-R3-02 control: a stateless same-hash popstate (an in-page anchor) is no restore', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  const before = srv.calls.length;
  fire(ctx, 'popstate', { state: null });
  await drain();
  assert.equal(srv.calls.length, before);
  assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/);
});

// -- DEMO2-SEC-R3-03: a pre-restore opener never publishes after it --------
// Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
for (const status of [401, 403]) {
  for (const [label, restore] of [['pageshow persisted', ctx => fire(ctx, 'pageshow', { persisted: true })],
    ['same-hash popstate', ctx => fire(ctx, 'popstate', { state: {} })]]) {
    test(`SEC-R3-03 Create alert started before a restore (${label}, ${status}) never opens with the old answer`, async () => {
      const w = fresh();
      const srv = server(data(w));
      const ctx = await app(SEARCH, srv);
      const modal = withModal(ctx);
      await go(ctx, SEARCH);
      const release = srv.hold('/customer/shows');
      const opening = ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
      await drain();
      assert.equal(modal.open, false, 'fixture: the opener waits on its own shows read');
      assert.ok(srv.count('/customer/shows', 200) >= 2, 'fixture: the held read was answered 200 at arrival');
      srv.refuse = status;
      restore(ctx);
      release();
      await opening;
      await drain(60);
      assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'fixture: the restored page refused');
      assert.equal(modal.open, false, 'the pre-restore opener opened its dialog after the refusal');
      assert.doesNotMatch(modal.innerHTML, /PRIVATE-SHOW/, 'the pre-restore answer was published into the dialog');
    });
  }
  test(`SEC-R3-03 an opener pending across a refusal on another read (${status}) never opens`, async () => {
    const w = fresh();
    const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    const modal = withModal(ctx);
    await go(ctx, SEARCH);
    const release = srv.hold('/customer/shows');
    const opening = ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
    await drain();
    srv.refuse = status;
    await assert.rejects(ctx.win.EW.api('/customer/alerts'), err => err.status === status);
    release();
    await opening;
    await drain();
    assert.equal(modal.open, false, 'the opener published an answer read before the refusal');
  });
}
test('SEC-R3-03 control: with access unchanged the opener still opens', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  const modal = withModal(ctx);
  await go(ctx, SEARCH);
  await ctx.win.EWViews.handleAction({ dataset: { action: 'create-alert' } });
  assert.equal(modal.open, true);
  assert.match(modal.innerHTML, /PRIVATE-SHOW/);
});

// -- The mechanism: every boundary advances the epoch; gates use it ----------
test('authority epoch: navigation away, restores, refusals and a workspace change each advance it; an in-place refresh does not', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  const E = ctx.win.EW;
  withModal(ctx);
  await go(ctx, SEARCH);
  let e = E.authorityEpoch();
  await go(ctx, SEARCH);
  assert.equal(E.authorityEpoch(), e, 'an in-place refresh of the frame on screen is no boundary');
  await go(ctx, OTHER);
  assert.equal(E.authorityEpoch(), e, 'a changed query over the frame on screen is no boundary');
  await go(ctx, '#/shows');
  assert.ok(E.authorityEpoch() > e, 'leaving the page on screen'); e = E.authorityEpoch();
  fire(ctx, 'pageshow', { persisted: true });
  assert.ok(E.authorityEpoch() > e, 'pageshow persisted'); e = E.authorityEpoch();
  await drain(40);
  e = E.authorityEpoch();
  fire(ctx, 'popstate', { state: {} });
  assert.ok(E.authorityEpoch() > e, 'same-hash popstate with stored state'); e = E.authorityEpoch();
  await drain(40);
  e = E.authorityEpoch();
  srv.refuse = 403;
  await assert.rejects(E.api('/customer/alerts'));
  assert.ok(E.authorityEpoch() > e, 'a 403'); e = E.authorityEpoch();
  E.clearWorkspaceState();
  assert.ok(E.authorityEpoch() > e, 'a workspace change');
});

test('openDialog refuses an owner from an earlier authority epoch; a current one opens', async () => {
  const srv = server(data(fresh()));
  const ctx = await app('#/overview', srv);
  const modal = withModal(ctx);
  const E = ctx.win.EW;
  const owner = { epoch: E.authorityEpoch() };
  E.authorityBoundary();
  assert.equal(E.openDialog('Old', '<p>PRIVATE-SHOW</p>', '', '', owner), false);
  assert.equal(modal.open, false);
  assert.doesNotMatch(modal.innerHTML, /PRIVATE-SHOW/);
  assert.equal(E.openDialog('New', '<p>ok</p>', '', '', { epoch: E.authorityEpoch() }), true);
  assert.equal(modal.open, true);
});

test('a kept shared read whose authorizing read settles after a boundary reads the server instead', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  let allow;
  const out = E.sharedRead('/customer/shows', { authorizedBy: new Promise(res => { allow = res; }) });
  E.authorityBoundary();
  w.shows = [{ id: 'cs-private', display_title: 'NAME-v2' }];
  const n = srv.count('/customer/shows');
  allow({ ok: true });
  const got = await out;
  assert.equal(got.shows[0].display_title, 'NAME-v2', 'a kept answer from the earlier epoch was published');
  assert.equal(srv.count('/customer/shows'), n + 1);
});

test('retained views from an earlier epoch read as nothing', async () => {
  const srv = server(data(fresh()));
  const ctx = await app(SEARCH, srv);
  const E = ctx.win.EW;
  await go(ctx, SEARCH);
  for (const name of ['lastSearch', 'lastSearchTotal', 'searchItems']) assert.ok(E.retained(name), `fixture: ${name} retained`);
  E.authorityBoundary();
  for (const name of ['lastSearch', 'lastSearchTotal', 'searchItems']) assert.equal(E.retained(name), null, `${name} survived a boundary`);
});

// -- Enumeration: every publish path of retained data checks the epoch -------
// A small scanner that skips strings, template literals and comments.
function scanArgs(src, open) {
  let i = open + 1, depth = 0, args = [''], stack = [];
  const push = ch => { args[args.length - 1] += ch; };
  while (i < src.length) {
    const ch = src[i];
    if (stack.length && stack[stack.length - 1] === '`') {
      if (ch === '\\') { push(ch + src[i + 1]); i += 2; continue; }
      if (ch === '`') { stack.pop(); push(ch); i++; continue; }
      if (ch === '$' && src[i + 1] === '{') { stack.push('${'); push('${'); i += 2; continue; }
      push(ch); i++; continue;
    }
    if (ch === '/' && src[i + 1] === '/') { while (src[i] !== '\n') i++; continue; }
    if (ch === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 2; continue; }
    if (ch === '\'' || ch === '"') {
      let j = i + 1;
      while (src[j] !== ch) j += src[j] === '\\' ? 2 : 1;
      push(src.slice(i, j + 1)); i = j + 1; continue;
    }
    if (ch === '`') { stack.push('`'); push(ch); i++; continue; }
    if (ch === '}' && stack.length && stack[stack.length - 1] === '${') { stack.pop(); push(ch); i++; continue; }
    if (ch === '{' && stack.length) { stack.push('{'); push(ch); i++; continue; }
    if (ch === '}' && stack.length && stack[stack.length - 1] === '{') { stack.pop(); push(ch); i++; continue; }
    if (!stack.length && (ch === '(' || ch === '[' || ch === '{')) depth++;
    if (!stack.length && (ch === ')' || ch === ']' || ch === '}')) {
      if (depth === 0) return args.map(a => a.trim());
      depth--;
    }
    if (!stack.length && depth === 0 && ch === ',') { args.push(''); i++; continue; }
    push(ch); i++;
  }
  throw new Error('unbalanced call at ' + open);
}
const lineOf = (src, idx) => src.slice(0, idx).split('\n').length;
// The enclosing unit of a call: the nearest named function or `case` label above it.
function enclosing(src, idx) {
  const lines = src.slice(0, idx).split('\n');
  for (let n = lines.length - 1; n >= 0; n--) {
    const m = lines[n].match(/^\s*(?:async\s+)?function\s+(\w+)\s*\(/) || lines[n].match(/^\s*(case\s+'[^']+')/);
    if (m) return { name: m[1], text: lines.slice(n).join('\n'), isFn: !/^case/.test(m[1]) };
  }
  return { name: '(top)', text: src.slice(0, idx), isFn: false };
}
const stripComments = t => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
const hasAwait = t => /\bawait\b/.test(stripComments(t));
const afterLastAwait = t => { const c = stripComments(t); const k = c.lastIndexOf('await'); return k < 0 ? c : c.slice(k); };

test('enumeration: every read of a retained view goes through EW.retained (epoch-checked)', () => {
  const offenders = [];
  for (const [file, src] of [['views.js', SRC], ['app.js', APPSRC]]) {
    src.split('\n').forEach((line, n) => {
      const code = line.replace(/\/\/.*$/, '');
      for (const m of code.matchAll(/\.(lastSearch|lastSearchTotal|searchItems)\b/g)) offenders.push(`${file}:${n + 1}: ${line.trim()}`);
    });
  }
  assert.deepEqual(offenders, [], 'direct retained-view access bypasses the epoch:\n' + offenders.join('\n'));
  assert.match(APPSRC, /function retained\(name\)[\s\S]{0,300}authorityHolds/, 'EW.retained checks the epoch');
  assert.match(APPSRC, /holds = \(\) =>[^\n]*authorityHolds\(epoch\)/, 'a kept shared read checks the epoch');
});

// Openers whose dialog is fixed copy (no server-derived content).
const FIXED_COPY = ['openUnsavedPrompt'];
test('enumeration: every dialog opener after an await passes its owner and checks it after the last await', () => {
  const calls = [...SRC.matchAll(/\bE\.openDialog\(/g)].map(m => m.index + m[0].length - 1);
  assert.ok(calls.length >= 25, 'fixture: found the openers (' + calls.length + ')');
  const problems = [];
  const syncUnits = new Set();
  for (const open of calls) {
    const unit = enclosing(SRC, open);
    const region = SRC.slice(open - unit.text.length, open);
    const args = scanArgs(SRC, open);
    const where = `views.js:${lineOf(SRC, open)} (${unit.name})`;
    if (hasAwait(region)) {
      if (args.length < 5 || !args[4] || /^(''|null|undefined)$/.test(args[4])) problems.push(`${where}: async opener without an owner`);
      if (!/editStale\(|authorityHolds\(/.test(afterLastAwait(region))) problems.push(`${where}: no epoch check after its last await`);
    } else if (unit.isFn) syncUnits.add(unit.name);
  }
  // A synchronous opener must itself be reached synchronously: no caller
  // awaits before calling it (else it would publish post-await data).
  for (const name of syncUnits) {
    if (FIXED_COPY.includes(name)) continue;
    for (const m of SRC.matchAll(new RegExp(`(?<![\\w.])${name}\\(`, 'g'))) {
      if (/function\s+$/.test(SRC.slice(m.index - 20, m.index))) continue;
      const unit = enclosing(SRC, m.index);
      if (unit.name === name) continue;
      const region = SRC.slice(m.index - unit.text.length, m.index);
      if (hasAwait(region) && !/editStale\(|authorityHolds\(/.test(afterLastAwait(region))) {
        problems.push(`views.js:${lineOf(SRC, m.index)} (${unit.name}): calls sync opener ${name} after an await without an epoch check`);
      }
    }
  }
  assert.deepEqual(problems, [], problems.join('\n'));
  // A fixed-copy opener publishes no server-derived content: its body
  // interpolates nothing but buttons.
  for (const name of FIXED_COPY) {
    const at = SRC.indexOf(`function ${name}(`);
    const body = SRC.slice(at, SRC.indexOf('\n  }\n', at));
    const interps = [...body.matchAll(/\$\{([^}]*)\}/g)].map(m => m[1]).filter(x => !/^E\.button\(/.test(x));
    assert.deepEqual(interps, [], `${name} publishes interpolated content`);
  }
  assert.match(APPSRC, /function openDialog\([^)]*\)\s*\{[\s\S]{0,400}authorityHolds\(owner\.epoch\)/, 'openDialog itself refuses a stale owner');
  assert.match(SRC, /function editOwner\(\)\s*\{[^}]*epoch: E\.authorityEpoch\(\)/, 'editOwner captures the epoch');
  assert.match(SRC, /function editStale\(owner\)[\s\S]{0,1200}authorityHolds\(owner\.epoch\)/, 'editStale checks the epoch');
});

test('enumeration: every in-dialog fill that captured its dialog checks it with EW.dialogHolds', () => {
  // Captures named own/mine feed a fill after an await; ownDialog/opened
  // name outcome and submit guards (identity only, no data published).
  const problems = [];
  const caps = [...SRC.matchAll(/const (own|mine) = E\.current\.dialogEpoch[;,]/g)];
  assert.ok(caps.length >= 5, 'fixture: found the in-dialog fills (' + caps.length + ')');
  for (const m of caps) {
    const unit = enclosing(SRC, m.index);
    const body = SRC.slice(m.index - unit.text.length);
    const end = body.indexOf('\n  }\n', unit.text.length);
    const fn = body.slice(0, end < 0 ? undefined : end);
    if (!fn.includes(`E.dialogHolds(${m[1]}`)) problems.push(`views.js:${lineOf(SRC, m.index)} (${unit.name}) fills without E.dialogHolds(${m[1]})`);
    if (new RegExp(`${m[1]} (?:===|!==) E\\.current\\.dialogEpoch`).test(fn)) problems.push(`views.js:${lineOf(SRC, m.index)} (${unit.name}) still compares identity only`);
  }
  assert.deepEqual(problems, [], problems.join('\n'));
  assert.match(APPSRC, /function dialogHolds\(own, epoch\)[^\n]*authorityHolds\(/);
});
