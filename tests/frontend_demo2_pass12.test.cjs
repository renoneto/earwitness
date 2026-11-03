/* DEMO2 pass 12.
   Owner rule: no route publishes retained or cache-backed workspace data
   unless a fresh request on that same navigation has validated access. A
   401, 403 or 428 removes retained content and shows the normal refusal.
   Built from Astra's round-2 reproductions (demo2-memo-astra-r2.md):
   DEMO2-SEC-R2-01  Search republished its retained frame after a refusal
                    (and painted it before the fresh read answered).
   DEMO2-SEC-R2-02  a kept shared read captured before an invalidation (a
   + DEMO2-R10-01   changed revision, a refusal, a write) was still
                    published after its authorizing read; values and
                    in-flight promises alike.
   DEMO2-SEC-R2-03  a page restored from browser history (pageshow
                    persisted, a same-hash popstate) asked nothing.
   DEMO2-R10-N3     authorizedBy: null counted as permission.
   Only fetch is faked: the real EW.api, apiCall, sharedRead, render and the
   registered window listeners run. Real browser back-forward cache
   eligibility cannot be shown in node; these tests drive the handlers the
   browser calls on a restore.
   Run: node --test tests/frontend_demo2_pass12.test.cjs */
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
    console, URLSearchParams, AbortController, Intl, location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
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
      out = reply(status, { ok: false, error: state.refuseError || { code: status === 401 ? 'SIGNIN_REQUIRED' : 'FORBIDDEN', message: 'You no longer have access to this workspace.', ...(status === 403 ? { access: 'lost' } : {}) } });
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

// -- DEMO2-SEC-R2-01: Search keeps nothing after a refusal ------------------
// Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
for (const status of [401, 403]) {
  for (const [label, back] of [['same query', SEARCH], ['changed query', OTHER]]) {
    test(`SEC-R2-01 Search ${label}: access removed (${status}); nothing retained shows while asking or after the refusal`, async () => {
      const w = fresh();
      const srv = server(data(w));
      const ctx = await app(SEARCH, srv);
      assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show while access holds');
      await go(ctx, '#/shows');
      srv.refuse = status;
      const before = srv.count('/customer/research-query', status);
      const release = srv.hold('/customer/research-query');
      ctx.win.location.hash = back;
      ctx.win.EW.current.route = back;
      const pending = ctx.win.EWViews.render();
      await drain();
      // M9-BACK (owner exception to this rule, brief M9-BACK): the page this
      // tab last rendered for this exact route (same query) is kept and shows
      // at once while its read runs; the refusal below still removes it. Any
      // other route (a changed query) shows nothing retained while asking.
      if (label === 'same query') assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/, 'M9-BACK: the kept page shows while asking');
      else assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the retained Search frame was painted before this navigation was checked');
      release();
      await pending;
      const html = ctx.main.innerHTML;
      assert.ok(srv.count('/customer/research-query', status) > before, `the visit got its ${status}`);
      assert.doesNotMatch(html, /PRIVATE-SHOW/, 'the refused Search still shows the retained frame');
      assert.doesNotMatch(text(html), /Showing the earlier results/, 'a refusal never offers earlier results');
      assert.match(text(html), /Search is unavailable: You no longer have access/, 'the page refuses as usual');
    });
  }
  test(`SEC-R2-01 Search refused (${status}) on the page itself: Retry drops the frame and refuses`, async () => {
    const w = fresh();
    const srv = server(data(w));
    const ctx = await app(SEARCH, srv);
    assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/);
    srv.refuse = status;
    const html = await go(ctx, SEARCH);
    assert.doesNotMatch(html, /PRIVATE-SHOW/, 'the retained frame came back under the refusal');
    assert.match(text(html), /Search is unavailable: You no longer have access/);
    assert.equal(ctx.win.EW.current.lastSearch, null, 'the refusal discarded the retained frame');
  });
}

// M9-BACK-FIX1 (M9B-SEC-R1-01): a refusal that is not a boundary (a 403
// without the lost-access marker, a 428) also removes the kept Search page:
// the settled page and its Retry show nothing from it.
for (const [label, status, error] of [
  ['403 (no lost-access marker)', 403, { code: 'FORBIDDEN', message: 'Your role does not allow this.' }],
  ['428', 428, { code: 'PRECONDITION_REQUIRED', message: 'Reload the workspace.' }]]) {
  test(`M9 Search same query refused (${label}) on return: the kept page is gone once refused, and Retry brings nothing of it back`, async () => {
    const srv = server(data(fresh()));
    const ctx = await app(SEARCH, srv);
    assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show');
    await go(ctx, '#/shows');
    srv.refuse = status;
    srv.refuseError = error;
    const html = await go(ctx, SEARCH);
    assert.ok(srv.count('/customer/research-query', status) >= 1, `fixture: the visit got its ${status}`);
    assert.doesNotMatch(html, /PRIVATE-SHOW/, 'the refused Search still shows the kept page');
    assert.doesNotMatch(text(html), /Showing the earlier results/, 'a refusal offered the kept results');
    assert.match(text(html), /Search is unavailable/, 'the page refuses as usual');
    const retry = await go(ctx, SEARCH);
    assert.doesNotMatch(retry, /PRIVATE-SHOW/, 'Retry brought the kept page back under the refusal');
    assert.doesNotMatch(text(retry), /Showing the earlier results/);
  });
  test(`M9 Search same query, only the research read refused (${label}): the kept page is dropped at that answer and stays gone`, async () => {
    const w = fresh();
    const srv = server(data(w));
    // Only the research query (a read-only POST) is refused; every other read answers.
    const base = srv.fetch;
    srv.fetch = async (url, init = {}) => {
      if (w.refuse && url === '/api/v1/customer/research-query') {
        srv.calls.push({ method: 'POST', path: '/customer/research-query', status });
        if (w.gate) await w.gate;
        return { ok: false, status, headers: { get: () => null }, text: async () => JSON.stringify({ ok: false, error }) };
      }
      return base(url, init);
    };
    const ctx = await app(SEARCH, srv);
    assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/, 'fixture: Search names the private show');
    await go(ctx, '#/shows');
    w.refuse = true;
    let open;
    w.gate = new Promise(res => { open = res; });
    const release = srv.hold('/customer/shows');
    ctx.win.location.hash = SEARCH;
    ctx.win.EW.current.route = SEARCH;
    const pending = ctx.win.EWViews.render();
    await drain();
    assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/, 'fixture: the kept page shows while asking');
    open();
    await drain();
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the kept page stayed up after the research read was refused');
    release();
    await pending;
    await drain();
    assert.ok(srv.count('/customer/research-query', status) >= 1, `fixture: the research read got its ${status}`);
    // The shows read answered in this render, so its name may show in the
    // scope; the kept results may not.
    assert.doesNotMatch(text(ctx.main.innerHTML), /No mentions found/, 'the refused Search still shows the kept results');
    assert.match(text(ctx.main.innerHTML), /Search is unavailable/, 'the page refuses as usual');
    assert.doesNotMatch(text(ctx.main.innerHTML), /Showing the earlier results/, 'a refusal offered the kept results');
    assert.equal(ctx.win.EW.keptPages.has(SEARCH), false, 'the refused page is still kept');
  });
}

test('SEC-R2-01 a refusal on any read discards the retained Search frame', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  assert.ok(ctx.win.EW.current.lastSearch, 'fixture: the frame is retained');
  srv.refuse = 403;
  await go(ctx, '#/shows');
  assert.equal(ctx.win.EW.current.lastSearch, null);
});

test('SEC-R2-01 a transient failure (500) still keeps the earlier results, labeled', async () => {
  const w = fresh();
  const srv = server(data(w));
  // DEMO2-R12-N3: the research read answers a real 500 (not a 404).
  const base = srv.fetch;
  srv.fetch = async (url, init = {}) => {
    if (w.fail && url === '/api/v1/customer/research-query') {
      srv.calls.push({ method: init.method || 'GET', path: '/customer/research-query', status: 500 });
      return { ok: false, status: 500, headers: { get: () => null },
        text: async () => JSON.stringify({ ok: false, error: { code: 'INTERNAL', message: 'internal error' } }) };
    }
    return base(url, init);
  };
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  w.fail = true;
  const html = await go(ctx, SEARCH);
  assert.ok(srv.count('/customer/research-query', 500) >= 1, 'fixture: the research read answered 500');
  assert.match(text(html), /Showing the earlier results for this scope below/);
});

// -- DEMO2-SEC-R2-02 + R10-01: kept reads are bound to their generation -----
test('SEC-R2-02 Astra: a fresh research read at another revision never publishes names kept at the old one', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  assert.match(text(await go(ctx, SEARCH)), /PRIVATE-SHOW/);
  w.revision = 'r2';
  w.shows = [];
  const shows = srv.count('/customer/shows');
  const html = await go(ctx, SEARCH);
  assert.equal(ctx.win.EW.current.displayedRevision, 'r2');
  assert.doesNotMatch(html, /PRIVATE-SHOW/, 'the r2 render published names kept at r1');
  assert.ok(srv.count('/customer/shows') > shows, 'the names were read again for r2');
});

test('SEC-R2-02 a sibling refusal while the authorizing read is pending: the kept names are not published', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  let allow;
  const authorizing = new Promise(res => { allow = res; });
  const out = E.sharedRead('/customer/shows', { authorizedBy: authorizing });
  out.catch(() => {});
  srv.refuse = 403;
  await assert.rejects(E.api('/customer/alerts'), err => err.status === 403);
  allow({ ok: true });
  const got = await out.then(v => v, err => err);
  assert.ok(!(got && got.shows && got.shows.some(s => s.display_title === 'PRIVATE-SHOW')), 'a kept answer survived the refusal');
  assert.equal(got && got.status, 403, 'it asked the server again and was refused');
});

test('R10-01 code review: names changed and invalidated while the authorizing read was open: the new names publish', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  const release = srv.hold('/customer/alerts');
  const own = E.api('/customer/alerts');
  const out = E.sharedRead('/customer/shows', { authorizedBy: own });
  await drain();
  w.shows = [{ id: 'cs-private', display_title: 'NAME-v2' }];
  E.invalidateSharedReads();
  release();
  const got = await out;
  assert.equal(got.shows[0].display_title, 'NAME-v2', 'published after invalidation: ' + got.shows[0].display_title);
});

test('R10-01 an in-flight kept promise invalidated before it settles is not published', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  const release = srv.hold('/customer/shows');
  const first = E.sharedRead('/customer/shows');
  const out = E.sharedRead('/customer/shows', { authorizedBy: { ok: true } });
  await drain();
  E.invalidateSharedReads();
  w.shows = [{ id: 'cs-private', display_title: 'NAME-v2' }];
  release();
  await first;
  const got = await out;
  assert.equal(got.shows[0].display_title, 'NAME-v2', 'the promise kept before the invalidation was published');
});

// -- DEMO2-R10-N3: null is not permission ------------------------------------
test('R10-N3 authorizedBy null, or an authorizing read that resolved to nothing, reads the server', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/overview', srv);
  const E = ctx.win.EW;
  await E.sharedRead('/customer/shows');
  let n = srv.count('/customer/shows');
  await E.sharedRead('/customer/shows', { authorizedBy: null });
  assert.equal(srv.count('/customer/shows'), n + 1, 'authorizedBy: null reused the kept answer');
  n = srv.count('/customer/shows');
  await E.sharedRead('/customer/shows', { authorizedBy: Promise.resolve(null) });
  assert.equal(srv.count('/customer/shows'), n + 1, 'an authorizing read that resolved to null reused the kept answer');
  n = srv.count('/customer/shows');
  await E.sharedRead('/customer/shows', { authorizedBy: { ok: true } });
  assert.equal(srv.count('/customer/shows'), n, 'control: a real authorizing value still reuses (pass 8)');
});

// -- DEMO2-SEC-R2-03: a restored page asks again ----------------------------
// Pass 15: 428 never starts an authority boundary (frontend_demo2_pass15 covers it staying local).
for (const status of [401, 403]) {
  test(`SEC-R2-03 Astra: Shows restored from the back-forward cache (pageshow persisted, then popstate) after access removal (${status}) asks and refuses`, async () => {
    const w = fresh();
    const srv = server(data(w));
    const ctx = await app('#/shows', srv);
    assert.match(await go(ctx, '#/shows'), /PRIVATE-SHOW/);
    srv.refuse = status;
    const before = srv.count('/customer/shows', status);
    const release = srv.hold('/customer/shows');
    fire(ctx, 'pageshow', { persisted: true });
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the restored copy still shows the page before any request');
    fire(ctx, 'popstate', { state: {} });
    release();
    await drain(40);
    assert.ok(srv.count('/customer/shows', status) > before, 'the restore asked nothing');
    assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the restored page kept the private name');
    // Pass 16 (DEMO2-SEC-R5-01): a refusal inside the boundary's own check
    // ends on the boundary's refusal page, with Retry.
    assert.match(text(ctx.main.innerHTML), /Shows are unavailable: You no longer have access/, 'the page refuses');
  });
}

test('SEC-R2-03 a restore that is still allowed reads fresh and shows the page again', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/shows', srv);
  await go(ctx, '#/shows');
  const before = srv.count('/customer/shows');
  fire(ctx, 'pageshow', { persisted: true });
  await drain(40);
  assert.equal(srv.count('/customer/shows'), before + 1, 'one fresh read on restore');
  assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/, 're-rendered after the fresh read');
});

test('SEC-R2-03 an ordinary page load (pageshow not persisted) changes nothing', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/shows', srv);
  await go(ctx, '#/shows');
  const before = srv.calls.length;
  fire(ctx, 'pageshow', { persisted: false });
  await drain();
  assert.equal(srv.calls.length, before);
  assert.match(ctx.main.innerHTML, /PRIVATE-SHOW/);
});

test('SEC-R2-03 a same-hash popstate (history entry restored with its stored state) reads the route again', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app('#/shows', srv);
  await go(ctx, '#/shows');
  srv.refuse = 403;
  const before = srv.count('/customer/shows', 403);
  fire(ctx, 'popstate', { state: {} });
  await drain(40);
  assert.ok(srv.count('/customer/shows', 403) > before, 'the restored entry asked nothing');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
});

test('SEC-R2-03 a restored Search drops its retained frame and closes a retained dialog', async () => {
  const w = fresh();
  const srv = server(data(w));
  const ctx = await app(SEARCH, srv);
  await go(ctx, SEARCH);
  const modal = { open: true, innerHTML: 'PRIVATE-SHOW', close() { this.open = false; }, querySelectorAll: () => [] };
  const mainSel = ctx.win.document.querySelector;
  ctx.win.document.querySelector = sel => (sel === '#modal' ? modal : mainSel(sel));
  srv.refuse = 403;
  const release = srv.hold('/customer/research-query');
  fire(ctx, 'pageshow', { persisted: true });
  assert.equal(modal.open, false, 'the restored dialog stayed open');
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the restored Search frame stayed up before any request');
  await drain();
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/, 'the retained frame was painted while asking');
  release();
  await drain(40);
  assert.doesNotMatch(ctx.main.innerHTML, /PRIVATE-SHOW/);
  assert.match(text(ctx.main.innerHTML), /Search is unavailable/);
});
