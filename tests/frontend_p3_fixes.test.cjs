/* P3 dress-rehearsal fixes, customer app (P3-3, P3-5).
   P3-3: an export refused for the viewer's role shows the server's plain
         sentence, never "Export failed: <operation name>".
   P3-5: a confirmed invitation accept opens the invited workspace
         directly; when that switch cannot happen the accepted chooser
         stays the way forward.
   Run: node --test tests/frontend_p3_fixes.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const EXPORT_REFUSAL = "Your role in this workspace can't export. Ask an owner for editor access.";
const INVITE = '#/invite?workspace=ws-b&id=inv-1';

// Boot reads one compatible session per call; revisions advance per read.
const sessionFetch = (revisions = [7]) => {
  let n = 0;
  return async (url) => {
    if (url === '/api/v1/session') {
      const rev = revisions[Math.min(n++, revisions.length - 1)];
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: rev,
        frontend_contract: 'earwitness/customer-ui/2' }) };
    }
    throw new Error('no network in unit test');
  };
};

function loadViews(route, fetch) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch,
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
  const win = sandbox.window;
  win.EW.current.route = route;
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await flush(); };
const httpError = (status, message) => Object.assign(new Error(message), { status });

// ---- P3-3: export refusal wording -------------------------------------------

async function exportWith(err) {
  const win = loadViews('#/search?mode=mentions&q=lumen', sessionFetch());
  await settle();
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.closeDialog = () => {};
  win.document.querySelector = sel => (sel === '#export-format' ? null : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.current.displayedRevision = 'r1';
  win.EW.api = async (p) => { if (p === '/customer/research-export') throw err; throw new Error('unexpected ' + p); };
  await win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null });
  return toasts;
}

test('P3-3 a role refusal on export shows the server sentence alone', async () => {
  const toasts = await exportWith(httpError(403, EXPORT_REFUSAL));
  assert.equal(toasts[toasts.length - 1], EXPORT_REFUSAL, toasts.join(' | '));
  assert.ok(!toasts.some(m => /Export failed|customer_|operation requires/.test(m)), toasts.join(' | '));
});

test('P3-3 other export failures keep their named failure', async () => {
  const toasts = await exportWith(httpError(500, 'Something went wrong on our side. Try again.'));
  assert.equal(toasts[toasts.length - 1], 'Export failed: Something went wrong on our side. Try again.');
});

// ---- P3-5: accept lands in the invited workspace ----------------------------

async function acceptPage(api) {
  const win = loadViews(INVITE, sessionFetch([7, 8]));
  await settle();
  const main = { innerHTML: '' };
  // Every other element is a working stand-in (form errors and notices write to it).
  const el = () => ({ innerHTML: '', textContent: '', hidden: false, dataset: {}, classList: { add() {}, remove() {} },
    setAttribute() {}, removeAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [] });
  win.document.querySelector = sel => (sel === '#main' ? main : el());
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = 'ws-a';
  win.EW.current.workspaces = ['ws-a'];
  const out = { win, main, calls: [], toasts: [], went: null, renders: 0, log: [] };
  win.EW.toast = m => out.toasts.push(m);
  win.EW.go = r => { out.went = r; };
  win.EW.render = () => { out.renders++; out.log.push('render'); };
  win.EW.api = async (p, o = {}) => { out.calls.push({ method: o.method || 'GET', path: p, body: o.body }); out.log.push(p); return api(p, o); };
  // DEMO2M-R1-01: the post-accept session read is boot's own (its request
  // layer, not EW.api); the stub answers it as a compatible session.
  const inner = win.fetch;
  win.fetch = async (url, init = {}) => {
    if (url !== '/api/v1/session' || (init.method && init.method !== 'GET')) return inner(url, init);
    out.calls.push({ method: 'GET', path: '/session' });
    out.log.push('/session');
    const body = { ok: true, csrf_token: 's'.repeat(32), frontend_contract: 'earwitness/customer-ui/2', ...(await api('/session', init)) };
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  };
  win.FormData = class { *entries() { yield ['workspace', 'ws-b']; yield ['invite_id', 'inv-1']; } };
  out.form = {
    getAttribute: () => 'invite-accept', setAttribute() {}, removeAttribute() {},
    querySelector: sel => (sel === '[type=submit]' ? { disabled: false, textContent: 'Accept invitation' } : null),
    querySelectorAll: () => [],
  };
  return out;
}
const SESSION_AFTER = { authenticated: true, principal: 'sam', active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-b'], workspace_context: '', workspace_switch_revision: 7 };

test('P3-5 a confirmed accept switches to the invited workspace and opens it', async () => {
  let switched = false;
  const page = await acceptPage((p) => {
    if (p.endsWith('/accept')) return { ok: true };
    // After the switch, its reconcile read names the invited workspace.
    if (p === '/session') return switched ? { ...SESSION_AFTER, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 8 } : SESSION_AFTER;
    if (p === '/session/workspace') { switched = true; return { active_workspace: 'ws-b' }; }
    throw new Error('unexpected ' + p);
  });
  await page.win.EWViews.handleSubmit(page.form);
  await settle();
  // The last read is the switch's own reconcile (boot's reader).
  assert.deepEqual(page.calls.map(c => `${c.method} ${c.path}`),
    ['POST /workspaces/ws-b/invites/inv-1/accept', 'GET /session', 'POST /session/workspace', 'GET /session']);
  assert.equal(JSON.stringify(page.calls[0].body), '{}');
  assert.equal(page.calls[2].body.workspace_id, 'ws-b');
  assert.equal(page.went, '#/overview', 'lands in the workspace, not the chooser');
  assert.equal(page.win.EW.current.activeWorkspace, 'ws-b');
  assert.equal(page.win.EW.current.inviteAccepted, null, 'the acceptance is settled');
});

test('P3-5 a switch that does not happen keeps the accepted chooser', async () => {
  const page = await acceptPage((p) => {
    if (p.endsWith('/accept')) return { ok: true };
    if (p === '/session') return SESSION_AFTER;
    if (p === '/session/workspace') throw httpError(403, 'workspace membership required');
    throw new Error('unexpected ' + p);
  });
  await page.win.EWViews.handleSubmit(page.form);
  await settle();
  assert.equal(page.went, null);
  assert.deepEqual(JSON.parse(JSON.stringify(page.win.EW.current.inviteAccepted)), { workspace: 'ws-b', id: 'inv-1' });
  assert.equal(page.win.EW.current.inviteError, null, 'the accept is not reported as failed');
  assert.equal(page.log[page.log.length - 1], 'render', 'the accepted page renders after the failed switch: ' + page.log.join(' > '));
});

test('P3-5 an unreadable switch basis keeps the accepted chooser', async () => {
  const page = await acceptPage((p) => {
    if (p.endsWith('/accept')) return { ok: true };
    // The post-accept session (the switch basis) carries no readable revision.
    if (p === '/session') return { ...SESSION_AFTER, workspace_switch_revision: undefined };
    throw new Error('unexpected ' + p);
  });
  page.win.EW.current.workspaceSwitchRevision = undefined;
  await page.win.EWViews.handleSubmit(page.form);
  await settle();
  assert.ok(!page.calls.some(c => c.path === '/session/workspace'), 'no switch without a basis');
  assert.equal(page.went, null);
  assert.equal(page.win.EW.current.inviteAccepted.workspace, 'ws-b');
  assert.equal(page.win.EW.current.inviteError, null, 'the accept is not reported as failed');
  assert.equal(page.log[page.log.length - 1], 'render', page.log.join(' > '));
});

test('P3-5 a switch that throws never turns a confirmed accept into a failure', async () => {
  const page = await acceptPage((p) => {
    if (p.endsWith('/accept')) return { ok: true };
    if (p === '/session') return SESSION_AFTER;
    if (p === '/session/workspace') throw httpError(403, 'workspace membership required');
    throw new Error('unexpected ' + p);
  });
  // The switch's own failure report breaks: the accept still stands.
  page.win.EW.formError = () => { throw new Error('form error broke'); };
  await page.win.EWViews.handleSubmit(page.form);
  await settle();
  assert.equal(page.win.EW.current.inviteError, null);
  assert.equal(page.win.EW.current.inviteAccepted.workspace, 'ws-b');
  assert.equal(page.log[page.log.length - 1], 'render', page.log.join(' > '));
});

test('P3-5 an invited workspace missing from the session keeps the chooser, no switch', async () => {
  const page = await acceptPage((p) => {
    if (p.endsWith('/accept')) return { ok: true };
    if (p === '/session') return { ...SESSION_AFTER, workspaces: ['ws-a'] };
    throw new Error('unexpected ' + p);
  });
  await page.win.EWViews.handleSubmit(page.form);
  await settle();
  assert.ok(!page.calls.some(c => c.path === '/session/workspace'), 'no switch without membership');
  assert.equal(page.went, null);
  assert.equal(page.renders, 1);
  assert.equal(page.win.EW.current.inviteAccepted.workspace, 'ws-b');
});

// ---- P3-3: Export follows the session role -----------------------------------

const searchItem = { ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [0, 10], audio: 'available', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', confirmed: false }, display_span: [0, 10], has_clip: false, needs_review: false } };
const searchBundle = { query: { version: 2, mode: 'ads', q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [searchItem], total: 1, per_page: 10, total_relation: 'exact', dated_total: 1, undated_total: 0,
    break_total: 1, break_dated_total: 1, break_undated_total: 0,
    ad_breaks: [{ id: 'brk:1', refs: ['sr1:a'], read_count: 1, span: [0, 10], brands: [{ key: 'ag1', display: 'AG1', confirmed: false }] }] },
  coverage: { episodes_in_scope: 3 }, summary: { week: [] } };

const episodeBundle = { query: { version: 2, mode: 'episodes', q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items: [{ ref: 'ep1', record_kind: 'episode', episode_id: 'ep1', ep_title: 'Ep one', show_title: 'Show one',
    pub_date: '2026-10-09', matching_passages: 1, indexed_passages: 4 }], total: 1, per_page: 10, total_relation: 'exact',
    dated_total: 1, undated_total: 0 }, coverage: { episodes_in_scope: 3 }, summary: { week: [] } };

async function searchPage(session, mode = 'ads', extra = '', bundleFor = null) {
  const route = `#/search?mode=${mode}&q=sleep${extra}`;
  const win = loadViews(route, sessionFetch());
  await settle();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/brands') return { brands: [] };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/research-query') return bundleFor ? bundleFor : mode === 'episodes' ? episodeBundle : searchBundle;
    throw new Error('unexpected ' + p);
  };
  win.location.hash = route;
  win.EW.current.route = route;
  // The session the last boot adopted (app.js hands it to the next render).
  win.EW.current.adoptedSession = session;
  const render = async () => { await win.EWViews.render(); await settle(); return main.innerHTML; };
  return { win, render };
}
const EXPORTS = /data-action="export-(open|selected)"/;

test('P3-3 a viewer sees no Export controls, and it stays so on the next render', async () => {
  const page = await searchPage({ authenticated: true, role: 'viewer' });
  const first = await page.render();
  assert.match(first, /Showing 1 to 1/, 'the results rendered');
  assert.doesNotMatch(first, EXPORTS);
  assert.doesNotMatch(await page.render(), EXPORTS, 'the role outlives the one-render handoff');
});

test('P3-3 owners, editors and a session with no role keep Export', async () => {
  for (const session of [{ authenticated: true, role: 'owner' }, { authenticated: true, role: 'editor' },
    { authenticated: true }, null]) {
    const html = await (await searchPage(session)).render();
    assert.match(html, /data-action="export-open"/, JSON.stringify(session));
    assert.match(html, /data-action="export-selected"/, JSON.stringify(session));
  }
});

// ---- P3-4: the hosted footer strip carries no local-build line ---------------

async function stripAfterHeader(hosted) {
  const win = loadViews('#/overview', sessionFetch());
  await settle();
  const strip = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#dataset' ? strip : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.current.hosted = hosted;
  win.EW.current.activeWorkspace = hosted ? 'ws-a' : '';
  win.EW.current.dataset = '3 episodes · 3 shows · stored records only';
  win.EW.header('Overview');
  await settle();
  return strip.innerHTML;
}

test('P3-4 a hosted workspace shows no "Local workspace … no live services" strip', async () => {
  assert.equal(await stripAfterHeader(true), '');
});

test('P3-4 the local app keeps its local strip', async () => {
  assert.match(await stripAfterHeader(false), /Local workspace[\s\S]*no live services/);
});

test('P3-4 the app shell says nothing about a local workspace', () => {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  const description = /<meta name="description" content="([^"]*)">/.exec(html)[1];
  assert.doesNotMatch(description, /local/i);
  assert.doesNotMatch(html, /Opening the local workspace/);
});

// ---- P3-3: viewers get no per-row export checkboxes -------------------------

const SELECT = /name="selected_ref"/;

test('P3-3 a viewer gets no row export checkboxes on cards or episode cards', async () => {
  for (const mode of ['ads', 'episodes']) {
    const html = await (await searchPage({ authenticated: true, role: 'viewer' }, mode)).render();
    assert.match(html, /Showing 1 to 1/, mode + ' rendered');
    assert.doesNotMatch(html, SELECT, mode);
  }
});

test('P3-3 owners, editors and a session with no role keep the row checkboxes', async () => {
  for (const mode of ['ads', 'episodes']) {
    for (const session of [{ authenticated: true, role: 'owner' }, { authenticated: true, role: 'editor' }, { authenticated: true }, null]) {
      const html = await (await searchPage(session, mode)).render();
      assert.match(html, SELECT, mode + ' ' + JSON.stringify(session));
    }
  }
});

// ---- P3-4: the shell footer link and noscript text are neutral ---------------

test('P3-4 the footer link and the noscript text say nothing about local data or a local app', () => {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  const footer = /<footer class="footer">([\s\S]*?)<\/footer>/.exec(html)[1];
  assert.match(footer, /<a href="#\/settings">[^<]+<\/a>/, 'the Settings link stays');
  assert.doesNotMatch(footer, /local/i);
  const noscript = /<noscript>([\s\S]*?)<\/noscript>/.exec(html)[1];
  assert.doesNotMatch(noscript, /local/i);
  assert.match(noscript, /JavaScript/);
});

// ---- P3-3 (Sol r1 FIX): the Ads List view has no selection column for viewers -

function uiOnly() {
  const win = loadViews('#/overview', sessionFetch());
  return win.EWUI;
}

test('P3-3 resultRow leaves the checkbox out only when asked; the default keeps it', async () => {
  const ui = uiOnly();
  assert.match(ui.resultRow(searchItem, { index: 0 }), /<span class="ew-row-check"><label class="ew-select"><input type="checkbox" name="selected_ref"/);
  assert.match(ui.resultRow(searchItem, { index: 0, selected: true }), SELECT);
  const bare = ui.resultRow(searchItem, { index: 0, selected: false });
  assert.doesNotMatch(bare, SELECT);
  assert.doesNotMatch(bare, /ew-row-check/);
  assert.match(bare, /class="ew-row-open"/, 'the rest of the row stays');
});

const looseBundle = { ...searchBundle, result: { ...searchBundle.result, ad_breaks: undefined, break_total: undefined } };

test('P3-3 a viewer\'s List view has no select-all, no row checkboxes and no selection column', async () => {
  for (const [name, bundle] of [['by ad', searchBundle], ['plain rows', looseBundle]]) {
    const html = await (await searchPage({ authenticated: true, role: 'viewer' }, 'ads', '&view=list', bundle)).render();
    assert.match(html, /class="ew-rows-head/, name + ' list rendered');
    assert.doesNotMatch(html, SELECT, name);
    assert.doesNotMatch(html, /id="select-page"|ew-rows-all/, name);
    assert.match(html, /class="ew-rows-wrap[^"]* ew-no-select"/, name + ': the wrap drops the column');
  }
});

test('P3-3 owners, editors and a session with no role keep the List view selection', async () => {
  for (const session of [{ authenticated: true, role: 'owner' }, { authenticated: true, role: 'editor' }, { authenticated: true }, null]) {
    for (const bundle of [searchBundle, looseBundle]) {
      const html = await (await searchPage(session, 'ads', '&view=list', bundle)).render();
      assert.match(html, /id="select-page"/, JSON.stringify(session));
      assert.match(html, SELECT, JSON.stringify(session));
      assert.doesNotMatch(html, /ew-no-select/, JSON.stringify(session));
    }
  }
});

test('P3-3 the no-select List view reclaims the check column at every width', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  const rules = [...css.matchAll(/\.ew-no-select \.ew-row[^{]*\{([^}]*)\}/g)].map(m => m[1]);
  assert.ok(rules.length >= 2, 'a wide and a narrow rule');
  for (const body of rules.filter(b => /grid-template-areas/.test(b))) assert.doesNotMatch(body, /check/);
  assert.ok(rules.some(b => /"play time brand where date status open"/.test(b)), 'wide: seven columns');
  assert.ok(rules.some(b => /"play brand open"/.test(b)), 'narrow: no check column');
});

// ---- Review r1: only the role sentence drops the prefix; static strip; public pages -

test('P3-3 other export 403s keep the "Export failed:" prefix, on both export actions', async () => {
  const toasts = await exportWith(httpError(403, 'workspace membership required'));
  assert.equal(toasts[toasts.length - 1], 'Export failed: workspace membership required');
});

test('P3-3 export selected: the role sentence alone, other 403s prefixed', async () => {
  for (const [message, shown] of [[EXPORT_REFUSAL, EXPORT_REFUSAL],
    ['workspace membership required', 'Export failed: workspace membership required']]) {
    const win = loadViews('#/search?mode=mentions&q=lumen', sessionFetch());
    await settle();
    const toasts = [];
    win.EW.toast = m => toasts.push(m);
    win.EW.closeDialog = () => {};
    win.document.querySelector = sel => (sel === '#export-format' ? null : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
    win.EW.current.displayedRevision = 'r1';
    const key = win.EWViews.selectionFor(win.EW.current.route).key;
    win.EW.current.selection = { key, ids: ['sr1:a'], touched: true, revision: 'r1' };
    win.EW.api = async (p) => { if (p === '/customer/research-export') throw httpError(403, message); throw new Error('unexpected ' + p); };
    await win.EWViews.handleAction({ dataset: { action: 'export-selected' }, closest: () => null });
    assert.equal(toasts[toasts.length - 1], shown, toasts.join(' | '));
  }
});

test('P3-4 the shell ships an empty dataset strip (no static local line)', () => {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  assert.match(html, /<div id="dataset" class="dataset-strip" aria-label="Dataset status"><\/div>/);
  assert.match(fs.readFileSync(path.join(APP, 'styles.css'), 'utf8'), /\.dataset-strip:empty \{ display: none; \}/);
});

test('P3-4 the public home and coverage pages say nothing about a local development build', () => {
  for (const page of ['index.html', 'coverage.html']) {
    const html = fs.readFileSync(path.join(APP, 'public', page), 'utf8');
    assert.doesNotMatch(html, /local development build|development build/i, page);
  }
});
