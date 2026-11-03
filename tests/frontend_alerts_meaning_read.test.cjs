/* READ-PP for meaning alerts (lead decision 2026-10-27, alerts x READ-PP):
   a meaning row and page show the caller's own New as a word alert's do.
   The Alerts list row carries "N new" (dark tag) and joins New matches
   while active; paused or stopped it is the grey "N unread". The alert page
   marks each match New or Seen and has Mark all read, which posts the
   caller's unread match ids to POST /customer/meaning-alerts/<id>/read.
   No new strings: "Mark all read", "New", "Seen", "N new", "N unread" are
   the word alerts' own. A server without read fields shows neither.
   Loaders copied from frontend_alerts_meaning.test.cjs.
   Run: node --test tests/frontend_alerts_meaning_read.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
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
const canonicalSessionFetch = () => async (url) => {
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function renderRoute(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.EW.current.meaningAlertsAvailable = true;
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
const plain = html => html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
const el = dataset => ({ dataset, disabled: false, closest: () => null, getAttribute: () => null });

const MA = 'ma_' + '1'.repeat(24);
const row = over => Object.assign({ alert_id: MA, kind: 'meaning', sentence: 'A host says they use AG1',
  lifecycle: 'active', banner: 'active', last_checked_at: '2026-10-29T09:00:41.000000Z', match_count: 3,
  unread_event_count: 3, unread_ad_count: 3, created_at: '2026-09-20T12:00:00Z', can_change: true }, over);
const word = over => Object.assign({ id: 1, name: 'Words one', lifecycle: 'active_local', revision: 1,
  definition_revision: 1, query: { mode: 'ads', q: 'x' }, last_successful_at: '2026-10-14T09:00:00Z',
  baseline_complete: true, unread_event_count: 2, unread_ad_count: 2 }, over);
async function alertsList(alerts) {
  const { html } = await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  });
  return html;
}
const meaningLi = html => html.split('<li class="ew-work-alert"').find(s => s.includes(MA));
const wordLi = html => html.split('<li class="ew-work-alert"').find(s => s.includes('#/alert/1"'));
const page = matches => Object.assign(fixture('alert-active'), {
  unread_event_count: matches.filter(m => !m.read_state).length,
  unread_ad_count: matches.filter(m => !m.read_state).length, matches });
const marked = states => fixture('alert-active').matches.map((m, i) => Object.assign(m, { read_state: states[i] }));

test('alerts list: an active meaning row with unread matches says "N new" and is under New matches, as a word row', async () => {
  const html = await alertsList([word(), row()]);
  const m = meaningLi(html), w = wordLi(html);
  assert.match(w, /<span class="ew-tag ew-tag-count ew-work-new">2 new<\/span>/);
  assert.match(m, /<span class="ew-tag ew-tag-count ew-work-new">3 new<\/span>/);
  assert.match(m, /data-alert-groups="all new"/);
  assert.ok(plain(html).includes('New matches · 2'), 'both rows count under New matches');
});

test('alerts list: a meaning row the caller has read has no New and is not under New matches', async () => {
  const html = await alertsList([word({ unread_event_count: 0, unread_ad_count: 0 }),
    row({ unread_event_count: 0, unread_ad_count: 0 })]);
  const m = meaningLi(html);
  assert.doesNotMatch(m, /ew-work-new/);
  assert.match(m, /data-alert-groups="all"/);
  assert.ok(plain(html).includes('New matches · 0'));
});

for (const lifecycle of ['paused', 'stopped']) {
  test(`alerts list: a ${lifecycle} meaning row's unread matches are the grey "N unread", not New matches`, async () => {
    const html = await alertsList([word({ lifecycle, unread_event_count: 2, unread_ad_count: 2 }), row({ lifecycle })]);
    const m = meaningLi(html);
    assert.ok(plain(m).includes('3 unread'), m);
    assert.doesNotMatch(m, /ew-work-new/);
    assert.doesNotMatch(m, /data-alert-groups="[^"]*\bnew\b/);
    assert.ok(plain(wordLi(html)).includes('2 unread'), 'the same as a word row');
  });
}

test('alert page: each match is New or Seen for the caller; Mark all read while any is New', () => {
  const html = loadViews(canonicalSessionFetch()).EWViews.meaningAlertHtml(page(marked([0, 1, 0])));
  const items = html.split('<li class="meaning-match"').slice(1).map(s => plain('<li' + s));
  assert.deepEqual(items.map(t => t.startsWith('New ') ? 'New' : t.startsWith('Seen ') ? 'Seen' : '?'),
    ['New', 'Seen', 'New']);
  assert.match(html, new RegExp(`<button[^>]*data-action="mark-read"[^>]*data-kind="meaning" data-id="${MA}" >Mark all read</button>`));
});

test('alert page: with every match Seen, Mark all read is off', () => {
  const html = loadViews(canonicalSessionFetch()).EWViews.meaningAlertHtml(page(marked([1, 1, 1])));
  assert.match(html, /<button[^>]*data-action="mark-read"[^>]*data-kind="meaning"[^>]*disabled>Mark all read<\/button>/);
  assert.doesNotMatch(plain(html), /\bNew\b/);
});

test('alert page: a server without read fields shows no New, no Seen and no Mark all read', () => {
  const html = loadViews(canonicalSessionFetch()).EWViews.meaningAlertHtml(fixture('alert-active'));
  assert.doesNotMatch(html, /mark-read/);
  assert.doesNotMatch(plain(html), /\b(New|Seen)\b/);
});

test('Mark all read (meaning): posts the caller\'s unread match ids to the meaning read route, then redraws', async () => {
  const win = loadViews(canonicalSessionFetch());
  const alert = page(marked([0, 1, 0]));
  const calls = [];
  win.EW.api = async (p, opts) => {
    calls.push([p, opts && opts.method, opts && opts.body]);
    return p === '/customer/meaning-alerts/' + MA ? alert : { alert_id: MA, marked: 2 };
  };
  let rendered = 0;
  win.EW.render = () => { rendered += 1; };
  await win.EWViews.handleAction(el({ action: 'mark-read', id: MA, kind: 'meaning' }));
  const ids = alert.matches.map(m => m.match_id);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.map(([p, method, body]) => [p, method || null, body || null]))), [
    ['/customer/meaning-alerts/' + MA, null, null],
    ['/customer/meaning-alerts/' + MA + '/read', 'POST', { match_ids: [ids[0], ids[2]] }],
  ]);
  assert.equal(rendered, 1);
});

test('Mark all read (meaning): nothing unread posts nothing', async () => {
  const win = loadViews(canonicalSessionFetch());
  const calls = [];
  win.EW.api = async (p, opts) => { calls.push([p, opts && opts.method]); return page(marked([1, 1, 1])); };
  win.EW.render = () => {};
  await win.EWViews.handleAction(el({ action: 'mark-read', id: MA, kind: 'meaning' }));
  assert.deepEqual(calls.map(c => c[0]), ['/customer/meaning-alerts/' + MA]);
});

/* Copied from tests/frontend_query.test.cjs "F2 mark-read posts the click-time basis…". */
const hostedSession = (over = {}) => ({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
  active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-b', 'ws-proof'], workspace_context: 'ctx-a',
  workspace_switch_revision: 5, frontend_contract: 'earwitness/customer-ui/2', ...over });
const json = (obj, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(obj) });

test('F2 Mark all read (meaning) posts the click-time basis even when a publication lands during its read', async () => {
  let releaseRead;
  const readGate = new Promise(res => { releaseRead = res; });
  const writes = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_switch_revision: 9 }));
    if (url === '/api/v1/customer/meaning-alerts/' + MA) {
      await readGate;
      return json(Object.assign(page(marked([0, 1, 0])), { workspace_context: 'ctx-a' }));
    }
    if (url === '/api/v1/customer/meaning-alerts/' + MA + '/read') {
      writes.push(opts.headers['X-EarWitness-Workspace-Switch-Revision']);
      return json({ alert_id: MA, marked: 2 });
    }
    return json({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  win.EW.render = () => {};
  win.EW.current.displayedWorkspaceSwitchRevision = 9;
  const pending = win.EWViews.handleAction(el({ action: 'mark-read', id: MA, kind: 'meaning' }));
  await flushBoot();
  // Same-context revision advance published while the GET is in flight.
  win.EW.current.workspaceSwitchRevision = 10;
  win.EW.current.displayedWorkspaceSwitchRevision = 10;
  releaseRead();
  await pending;
  assert.deepEqual(writes, ['9'], 'POST carries the click-time basis, never the newer publication');
});
