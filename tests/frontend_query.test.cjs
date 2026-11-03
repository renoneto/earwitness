/* Customer Query2 URL wire + Saved/UI module checks.
   Loads the real app scripts in a stubbed browser sandbox and asserts
   canonical round-trips, selector separation and request construction.
   Run: node --test tests/frontend_query.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const logs = [];
  const store = {};
  const sandbox = {
    console,
    URLSearchParams,
    AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
    localStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    },
    document: {
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }),
      createTextNode: t => t,
    },
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

function EW(route) { return loadAt(route).EW; }
function plain(v) { return JSON.parse(JSON.stringify(v)); }

const flushBoot = () => new Promise(res => setTimeout(res, 0));
// Canonical session fixture: the compatible session read behind
// E.boot(). Tests that render member or guest pages admission-first
// boot through this; refusal/closed-boot behavior has its own tests.
const canonicalSessionFetch = (session = {}, revisions = null) => {
  let n = 0;
  return async (url) => {
    if (url === '/api/v1/session') {
      const rev = revisions ? revisions[Math.min(n++, revisions.length - 1)] : 7;
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: rev,
        frontend_contract: 'earwitness/customer-ui/2', ...session }) };
    }
    throw new Error('no network in unit test');
  };
};

function loadViews(fetch, route = '#/overview') {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  // views.js boots on load; a rejecting fetch lands on the offline
  // branch, which only needs a dummy mount element.
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  vm.runInContext(src, win, { filename: 'views.js' });
  return win;
}

test('legacy empty ids stay exact-empty, absent ids stay null', () => {
  assert.deepEqual(plain(EW('#/search?mode=mentions&ids=').searchCriteria().ids), []);
  assert.deepEqual(plain(EW('#/search?mode=mentions&ids=__none__').searchCriteria().ids), []);
  assert.equal(EW('#/search?mode=mentions').searchCriteria().ids, null);
});

test('none-brand parses to empty set, shows stay null', () => {
  const q = EW('#/search?mode=mentions&brand=__none__').searchCriteria();
  assert.deepEqual(plain(Array.from(q.brands)), []);
  assert.equal(q.shows, null);
});

test('criteria exclude the selector; scope carries both', () => {
  const sel = { version: 1, scope_mode: 'counted_cell', base_query: { version: 2, mode: 'ads' }, clause: { kind: 'chart_interval' } };
  const route = '#/search?mode=ads&' + 'selector=' + encodeURIComponent(JSON.stringify(sel));
  const E = EW(route);
  assert.equal('selector' in E.searchCriteria(), false);
  assert.deepEqual(plain(E.searchSelector()), sel);
  assert.deepEqual(E.searchScope().query.mode, 'ads');
  assert.deepEqual(plain(E.searchScope().selector), sel);
});

test('malformed selector is a visible error, never a silent widening', () => {
  assert.throws(() => EW('#/search?mode=ads&selector=%7Bnope').searchSelector(), /unreadable/);
});

test('canonical emission: repeated plural keys, null absent, empty __none__', () => {
  const E = EW('#/search');
  const url = E.searchURL({ version: 2, mode: 'ads', q: '', brands: ['a', 'b'], shows: null, categories: [], statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: [] });
  assert.match(url, /brands=a&brands=b/);
  assert.match(url, /categories=__none__/);
  assert.match(url, /ids=__none__/);
  assert.doesNotMatch(url, /shows=/);
  assert.doesNotMatch(url, /date_status=/);
  const back = EW(url.replace('#', '#')).searchCriteria();
  assert.deepEqual(plain(back.brands), ['a', 'b']);
  assert.deepEqual(plain(back.categories), []);
  assert.deepEqual(plain(back.ids), []);
});

test('source card separates kind from review, never invents brands', () => {
  const E = loadAt('#/search').EWUI;
  const html = E.sourceCard({ ref: 'sr1:x', record_kind: 'evidence', status: 'finding', ep_title: 'Ep', show_title: 'S', pub_date: '2026-01-01', text: 'great cola taste', audio: 'available', audio_local: false }, { query: 'cola' });
  // dv3 G1: the kind is the heading: an ad read with no named brand says
  // so (never a guess); a transcript passage is a Mention.
  assert.match(html, /Brand not identified/);
  // dv3-ux UX-04: a mention is titled by where it was said; "Mention" is its kind tag.
  const mention = E.sourceCard({ ref: 'sr1:m', record_kind: 'transcript_passage', status: 'mention', text: 'cola', show_title: 'S', ep_title: 'Ep' });
  assert.match(mention, /data-tag="outline">Mention</);
  assert.match(mention, /<h3 class="ew-result-title ew-mention-title od-clamp-2">S · Ep · /);
  assert.doesNotMatch(html, /Possible ad|Confirmed ad/, 'PLAIN-ADS: an ad card carries no review state');
  assert.doesNotMatch(html, /Reviewed by EarWitness/);
  assert.match(html, /<details class="report-problem"><summary>Report a mistake<\/summary><form class="report-form" data-report-ref="sr1:x">/);
  assert.match(html, /<mark>cola<\/mark>/);
  // d16 decision 7: remote only, or no read at all, is "No clip (transcript only)";
  // the remote detail is not a member state.
  assert.match(html, /No clip \(transcript only\)/);
  assert.doesNotMatch(html, /Remote audio only/);
  assert.doesNotMatch(html, /data-action="search-play"/);
});

test('operation states render without fetching', () => {
  const E = loadAt('#/search').EWUI;
  const calls = [];
  const el = { setAttribute: (k, v) => calls.push([k, v]), closest: () => null, innerHTML: '' };
  E.operationState(el, { phase: 'saving', message: 'Saving…' });
  assert.deepEqual(calls[0], ['data-phase', 'saving']);
  assert.match(el.innerHTML, /Saving/);
});

test('saved requests carry typed targets only, no authority fields', () => {
  const S = loadAt('#/saved').EWSaved;
  const excerpt = S.createRequest('excerpt', { title: 'T', note: '', ref: 'sr1:x', start_s: 1, end_s: 2 });
  assert.deepEqual(plain(excerpt), { type: 'excerpt', title: 'T', note: '', target: { ref: 'sr1:x', start_s: 1, end_s: 2 } });
  const query = S.createRequest('query', { title: 'Q', query: { version: 2, mode: 'ads' }, selector: { version: 1 } });
  assert.equal(query.query.mode, 'ads');
  const bookmark = S.createRequest('bookmark', { title: 'B', kind: 'episode', id: 'e1', source_ref: 'sr1:y', transcript_version_id: 3 });
  assert.deepEqual(plain(bookmark.target), { kind: 'episode', id: 'e1', source_ref: 'sr1:y', transcript_version_id: 3 });
  assert.throws(() => S.createRequest('excerpt', { title: 'T' }), /source reference/);
});

test('saved updates refuse source replacement, keep revisions', () => {
  const S = loadAt('#/saved').EWSaved;
  const item = { id: 1, type: 'excerpt', title: 'T', revision: 4, target: { ref: 'sr1:x' } };
  assert.throws(() => S.updateRequest(item, { title: 'T2', ref: 'sr1:other' }), /pinned source/);
  const ok = S.updateRequest(item, { title: 'T2', note: 'n' });
  assert.equal(ok.expected_revision, 4);
  assert.equal(ok.item.title, 'T2');
});

test('escaping preserves consumer text with angle brackets and quotes', () => {
  const E = EW('#/search');
  assert.equal(E.esc('a>b'), 'a&gt;b');
  assert.equal(E.esc('5 < 6 & "quoted" \'it\''), '5 &lt; 6 &amp; &quot;quoted&quot; &#39;it&#39;');
  assert.equal(E.esc('<script>alert(1)</script>'), '&lt;script&gt;alert(1)&lt;/script&gt;');
});

function deferredBody(obj) {
  let release;
  const gate = new Promise(res => { release = res; });
  const fetch = async () => {
    await gate;
    return { ok: true, text: async () => JSON.stringify(obj) };
  };
  return { fetch, release: () => release() };
}

test('late workspace response is dropped even when it echoes the dispatch context', async () => {
  const d = deferredBody({ ok: true, workspace_context: 'ctx-old', items: [] });
  const win = loadAt('#/search', { fetch: d.fetch });
  win.EW.current.workspaceContext = 'ctx-old';
  const pending = win.EW.api('/customer/search');
  win.EW.current.workspaceContext = 'ctx-new';
  d.release();
  await assert.rejects(pending, /workspace changed/i);
});

test('explicit workspaceContext pin is honored against a moved display', async () => {
  const d = deferredBody({ ok: true, workspace_context: 'ctx-pinned', items: [] });
  const win = loadAt('#/search', { fetch: d.fetch });
  win.EW.current.workspaceContext = '';
  const pending = win.EW.api('/customer/search', { workspaceContext: 'ctx-pinned' });
  win.EW.current.workspaceContext = 'ctx-other';
  d.release();
  await assert.rejects(pending, /workspace changed/i);
});

test('session adoption learns context only when the display has not moved', async () => {
  // B2 admission rule: adoption requires a compatible contract marker;
  // the fixture carries it, keeping this a timing/drop test only.
  const d = deferredBody({ ok: true, csrf_token: 'c', workspace_context: 'ctx-1', frontend_contract: 'earwitness/customer-ui/2' });
  const win = loadAt('#/overview', { fetch: d.fetch });
  const pending = win.EW.api('/session');
  d.release();
  await pending;
  assert.equal(win.EW.current.workspaceContext, 'ctx-1');

  const d2 = deferredBody({ ok: true, workspace_context: 'ctx-old' });
  const win2 = loadAt('#/overview', { fetch: d2.fetch });
  win2.EW.current.workspaceContext = 'ctx-old';
  const pending2 = win2.EW.api('/session/workspace', { method: 'POST', body: {} });
  win2.EW.current.workspaceContext = 'ctx-new';
  d2.release();
  await assert.rejects(pending2, /workspace changed/i);
  assert.equal(win2.EW.current.workspaceContext, 'ctx-new');
});

function deferredStatus(ok, status, obj) {
  let release;
  const gate = new Promise(res => { release = res; });
  const fetch = async () => {
    await gate;
    return { ok, status, text: async () => JSON.stringify(obj) };
  };
  return { fetch, release: () => release() };
}

test('dropped boot signin never changes signin state', async () => {
  const d = deferredStatus(false, 401, { ok: false, error: 'stale session', signin: { url: 'https://obsolete-signin' }, workspace_context: 'A' });
  const win = loadAt('#/overview', { fetch: d.fetch });
  win.EW.current.workspaceContext = 'A';
  const pending = win.EW.boot();
  win.EW.current.workspaceContext = 'B';
  d.release();
  await assert.rejects(pending, /workspace changed/i);
  assert.equal(win.EW.current.signin, undefined);
  assert.equal(win.EW.current.hosted, undefined);
});

test('context cleared mid-flight drops late success', async () => {
  const d = deferredBody({ ok: true, workspace_context: 'A', items: [1] });
  const win = loadAt('#/search', { fetch: d.fetch });
  win.EW.current.workspaceContext = 'A';
  const pending = win.EW.api('/customer/search');
  win.EW.current.workspaceContext = '';
  d.release();
  await assert.rejects(pending, /workspace changed/i);
});

test('context cleared mid-flight drops late failure as stale, not as live error', async () => {
  const d = deferredStatus(false, 500, { ok: false, error: 'boom', workspace_context: 'A' });
  const win = loadAt('#/search', { fetch: d.fetch });
  win.EW.current.workspaceContext = 'A';
  const pending = win.EW.api('/customer/search');
  win.EW.current.workspaceContext = '';
  d.release();
  await assert.rejects(pending, /workspace changed/i);
});

test('live signin failure still records signin state', async () => {
  const d = deferredStatus(false, 401, { ok: false, error: 'needs signin', signin: { url: 'https://live-signin' } });
  const win = loadAt('#/overview', { fetch: d.fetch });
  const pending = win.EW.boot();
  d.release();
  await pending;
  assert.equal(win.EW.current.hosted, true);
  assert.equal(JSON.stringify(win.EW.current.signin), JSON.stringify({ url: 'https://live-signin' }));
});

const assertIds = (actual, expected) => assert.equal(JSON.stringify(actual), JSON.stringify(expected));
test('export selection survives page and sort within one scope, clears on scope change', () => {
  const win = loadViews();
  const V = win.EWViews;
  win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute() {}, removeAttribute() {}, classList: { add() {}, remove() {} } });
  const scope = (route) => { win.EW.current.route = route; };
  win.EW.current.workspaceContext = 'W';
  win.EW.current.displayedRevision = 'rev-1';
  scope('#/search?mode=mentions&q=lumen&page=1');
  V.setSelection(win.EW.current.route, ['ref-1'], 'rev-1');
  // Page and sort navigation within the same view keeps refs.
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&page=2').ids, ['ref-1']);
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&page=2&sort=oldest').ids, ['ref-1']);
  // Criteria, selector, workspace or cohort change starts empty.
  assertIds(V.selectionFor('#/search?mode=mentions&q=other&page=1').ids, []);
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&selector=%7B%7D&page=1').ids, []);
  win.EW.current.displayedRevision = 'rev-2';
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&page=2').ids, []);
  win.EW.current.displayedRevision = 'rev-1';
  win.EW.current.workspaceContext = 'W2';
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&page=2').ids, []);
  win.EW.current.workspaceContext = 'W';
  assertIds(V.selectionFor('#/search?mode=mentions&q=lumen&page=2').ids, ['ref-1']);
});

test('page checkbox merges accumulate across pages; uncheck removes only its page ref', () => {
  const win = loadViews();
  const V = win.EWViews;
  win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute() {}, removeAttribute() {}, classList: { add() {}, remove() {} } });
  win.EW.current.route = '#/search?mode=mentions&q=lumen&page=1';
  win.EW.current.workspaceContext = 'W';
  win.EW.current.displayedRevision = 'rev-1';
  // Page 1: check first ref.
  V.setSelection(win.EW.current.route, V.mergePageSelection(win.EW.current.route,
    [{ value: 'ref-1', checked: true }, { value: 'ref-2', checked: false }]), 'rev-1');
  assertIds(win.EW.current.selection.ids, ['ref-1']);
  // Page 2: check one more; page-1 ref persists, total is 2 not 1.
  win.EW.current.route = '#/search?mode=mentions&q=lumen&page=2';
  V.setSelection(win.EW.current.route, V.mergePageSelection(win.EW.current.route,
    [{ value: 'ref-3', checked: true }]), 'rev-1');
  assertIds(win.EW.current.selection.ids, ['ref-1', 'ref-3']);
  // Back on page 1: unchecking removes only that page's ref.
  win.EW.current.route = '#/search?mode=mentions&q=lumen&page=1';
  V.setSelection(win.EW.current.route, V.mergePageSelection(win.EW.current.route,
    [{ value: 'ref-1', checked: false }, { value: 'ref-2', checked: false }]), 'rev-1');
  assertIds(win.EW.current.selection.ids, ['ref-3']);
});

test('boot contract gate refuses unknown markers but keeps special admission', () => {
  const E = EW('#/overview');
  assert.equal(E.FRONTEND_CONTRACT, 'earwitness/customer-ui/2');
  assert.throws(() => E.requireContract({ authenticated: true, active_workspace: 'w', frontend_contract: null }), /different version of the app/);
  assert.throws(() => E.requireContract({ workspace_context: 'ctx', frontend_contract: 'earwitness/customer-ui/1' }), /different version of the app/);
  E.requireContract({ authenticated: true, active_workspace: 'w', frontend_contract: 'earwitness/customer-ui/2' });
  E.requireContract({ authenticated: true, active_workspace: '' });
  E.requireContract({ signin: { available: true } });
  E.requireContract(null);
});

test('saved excerpt renders the real capture, provenance and availability', async () => {
  const win = loadAt('#/saved?tab=excerpts');
  win.EW.current.route = '#/saved?tab=excerpts';
  const item = { id: 1, type: 'excerpt', title: 'T', note: '', revision: 2,
    captured: { text: 'This episode is brought to you by Lumen', source_ref: 'sr1:abc',
      labels: { show_title: 'Shop', ep_title: 'Ep', pub_date: '2026-01-01' } },
    availability: 'source_changed', availability_reason: 'a newer publication exists; saved pin retained' };
  const api = async () => ({ items: [item], total: 1, total_relation: 'exact', next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.match(html, /This episode is brought to you by Lumen/);
  assert.match(html, /Changed since saved/);
  assert.match(html, /a newer publication exists/);
  assert.match(html, /Shop/);
  assert.match(html, /#\/evidence\/sr1%3Aabc/);
  assert.doesNotMatch(html, /#\/note\/new/);
});

test('saved excerpt ignores alias fields instead of blanking as success', async () => {
  const win = loadAt('#/saved?tab=excerpts');
  win.EW.current.route = '#/saved?tab=excerpts';
  const item = { id: 2, type: 'excerpt', title: 'T2', revision: 1,
    captured_text: 'WRONG', text: 'WRONG', availability: 'current' };
  const api = async () => ({ items: [item], total: 1, total_relation: 'exact', next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.doesNotMatch(html, /WRONG/);
  assert.match(html, /Saved quotation unavailable/);
});

test('archive renders reports and exports with server download paths', async () => {
  const win = loadAt('#/saved?tab=reports');
  win.EW.current.route = '#/saved?tab=reports';
  const report = { id: 2, kind: 'report', title: 'Fixture supplied', campaign_id: 3, report_no: 1,
    captured_at: '2026-10-07T08:40:49+00:00', pins: {}, count: 5, count_units: 'items',
    format: 'html', digest: 'aa', download_path: '/customer/campaigns/3/reports/1/html',
    stale: true, stale_reasons: ['campaign changed'], limitations: [] };
  const exp = { id: 9, kind: 'export', title: 'Research CSV', campaign_id: null, report_no: null,
    captured_at: '2026-10-07T08:41:00+00:00', pins: {}, count: 3, count_units: 'matching records',
    format: 'csv', digest: 'bb', download_path: '/customer/exports/9',
    stale: false, stale_reasons: [], limitations: [] };
  const api = async (path) => {
    assert.match(path, /^\/customer\/archive/);
    assert.doesNotMatch(path, /kind=report/);
    return { items: [report, exp], total: 2, total_relation: 'exact', next_cursor: null };
  };
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.match(html, /Fixture supplied/);
  assert.match(html, /Research CSV/);
  assert.match(html, /\/api\/v1\/customer\/campaigns\/3\/reports\/1\/html\?download=1/);
  assert.match(html, /\/api\/v1\/customer\/exports\/9/);
  assert.doesNotMatch(html, /campaign\/null/);
  assert.doesNotMatch(html, /undefined/);
});

test('AA-01 archive rows that cannot be exported offer no open or download link', async () => {
  const win = loadAt('#/saved?tab=reports');
  win.EW.current.route = '#/saved?tab=reports';
  const report = { id: 2, kind: 'report', title: 'Fixture supplied', campaign_id: 3, report_no: 1,
    captured_at: '2026-10-07T08:40:49+00:00', pins: {}, count: 5, count_units: 'items',
    format: 'html', digest: 'aa', download_path: '/customer/campaigns/3/reports/1/html',
    stale: false, stale_reasons: [], limitations: [], exportable: false };
  const exp = { id: 9, kind: 'export', title: 'Research CSV', campaign_id: null, report_no: null,
    captured_at: '2026-10-07T08:41:00+00:00', pins: {}, count: 3, count_units: 'matching records',
    format: 'csv', digest: 'bb', download_path: '/customer/exports/9',
    stale: false, stale_reasons: [], limitations: [], exportable: false };
  const api = async () => ({ items: [report, exp], total: 2, total_relation: 'exact', next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.match(html, /Fixture supplied/);
  assert.match(html, /Research CSV/);
  assert.doesNotMatch(html, /reports\/1\/html/);
  assert.doesNotMatch(html, /\/api\/v1\/customer\/exports\/9/);
  assert.doesNotMatch(html, /#\/campaign\/3\/report\/1/);
  assert.equal((html.match(/You can see this in history but not open or download it\./g) || []).length, 2);
});

test('same-title shows gain distinct source-ID qualifiers, titles untouched', () => {
  const V = loadViews().EWViews;
  const out = plain(V.decorateOptions('shows', [
    { value: 'cs-3a8532d96b80', label: 'Same Title' },
    { value: 'cs-4155f31d4b19', label: 'Same Title' },
  ]));
  assert.equal(out[0].label, 'Same Title');
  assert.equal(out[1].label, 'Same Title');
  assert.ok(out[0].qualifier && out[1].qualifier);
  assert.notEqual(out[0].qualifier, out[1].qualifier);
  const unique = plain(V.decorateOptions('shows', [{ value: 'cs-9', label: 'Only One' }]));
  assert.equal(unique[0].qualifier || '', '');
});

test('add research target opens its dialog from the route campaign', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.EW.current.route = '#/campaign/2';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/2') return { campaign: { id: 2, revision: 5 } };
    if (path === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + path);
  };
  // Browsers provide timers for toast/dialog focus; the sandbox does not.
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'add-target' }, disabled: false, closest: () => null });
  assert.equal(modal.open, true);
  assert.match(modal.innerHTML, /Add research target/);
  assert.match(modal.innerHTML, /<form id="target"/);
});

test('saved-excerpt confirmation carries id, revision and links', () => {
  const win = loadViews();
  win.EW.current.savedExcerpt = null;
  assert.equal(win.EWViews.savedExcerptNotice(), '');
  win.EW.current.savedExcerpt = { id: 41, revision: 7, ref: 'sr1:abc' };
  const html = win.EWViews.savedExcerptNotice();
  assert.match(html, /41/);
  assert.match(html, /revision 7/);
  assert.match(html, /#\/saved\?tab=excerpts/);
  assert.match(html, /#\/evidence\/sr1%3Aabc/);
});

test('stale freeze 412 preserves confirmation and offers review or discard, never auto-refetch', async () => {
  const win = loadViews();
  let closes = 0, renders = 0, apiCalls = 0;
  win.EW.closeDialog = () => { closes++; };
  win.EW.render = () => { renders++; };
  win.EW.saveNotice = () => {};
  win.EW.api = async () => { apiCalls++; throw Object.assign(new Error('expected revision 2, current is 3'), { status: 412 }); };
  const shown = { cid: '2', pins: { t1: { q: 'x' } }, revision: 2, mode: 'research', placements: 0, targets: 1 };
  win.EW.current.displayedDraft = shown;
  win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '2']; yield ['freeze_pins', JSON.stringify(shown.pins)]; yield ['freeze_revision', '2']; } };
  const notices = [];
  win.document.createElement = () => ({ dataset: {}, setAttribute() {}, appendChild() {} });
  const submitBtn = { disabled: false, textContent: 'Confirm' };
  const footer = { innerHTML: '<button>Confirm</button>' };
  const form = {
    getAttribute: () => 'confirm',
    querySelector: sel => (sel === '[type=submit]' ? submitBtn : sel === '.dialog-footer' ? footer : null),
    querySelectorAll: () => [],
    insertBefore: node => { notices.push(node); },
    firstChild: null,
  };
  await win.EWViews.handleSubmit(form);
  assert.equal(apiCalls, 1);
  // No automatic recovery: dialog stays open, nothing refetches.
  assert.equal(closes, 0);
  assert.equal(renders, 0);
  // Durable 412 message names the displayed revision and blocks resubmit.
  assert.equal(notices.length, 1);
  assert.match(notices[0].innerHTML, /<strong>This report was not saved\.<\/strong> The campaign changed after you started saving it/);
  assert.match(notices[0].innerHTML, /revision 2/);
  assert.match(notices[0].innerHTML, /current is 3/);
  assert.equal(submitBtn.disabled, true);
  // Explicit choices only: review (refresh) or discard.
  assert.match(footer.innerHTML, /data-action="freeze-review"/);
  assert.match(footer.innerHTML, /Review current draft/);
  assert.match(footer.innerHTML, /Discard/);
  // Displayed pins and draft basis are untouched by the failure.
  assert.deepEqual(win.EW.current.displayedDraft, shown);
  // The explicit review path is the only refresh: close, then render.
  await win.EWViews.handleAction({ dataset: { action: 'freeze-review' }, disabled: false, closest: () => null });
  assert.equal(closes, 1);
  assert.equal(renders, 1);
});

// FREEZE r2 (code 2 / security 3): only the disclosure refusal (error.refusal
// "disclosure") gets the rights message; every other refusal the general one.
const FREEZE_RIGHTS = "This report can't be saved as a final report. The rights for these shows don't allow saving these ads in a report. Nothing was saved.";
const FREEZE_GENERAL = "This report can't be saved as a final report right now. Nothing was saved.";
const FREEZE_REFUSALS = [
  ['disclosure', { code: 'FORBIDDEN', message: 'source disclosure denied', status: 403, details: {}, refusal: 'disclosure' }, FREEZE_RIGHTS],
  ['disclosure-transcript', { code: 'FORBIDDEN', message: 'source disclosure denied', status: 403, details: { reason: 'transcript_export' }, refusal: 'disclosure' }, FREEZE_RIGHTS],
  ['role', { code: 'FORBIDDEN', message: "Your role in this workspace can't make this change. Ask an owner for editor access.", status: 403, details: {} }, FREEZE_GENERAL],
  ['unmarked', { code: 'FORBIDDEN', message: 'source disclosure denied', status: 403, details: {} }, FREEZE_GENERAL],
];

test('FREEZE: a refused freeze shows a plain message, never the raw refusal', async () => {
  // UX audit B3 / MKT-01: the dialog said "source disclosure denied".
  for (const [kind, error, copy] of FREEZE_REFUSALS) {
    const win = loadViews();
    win.EW.saveNotice = () => {};
    win.EW.api = async () => { throw Object.assign(new Error(error.message), { status: 403, details: error.details, serverMessage: true, body: { ok: false, error } }); };
    win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '2']; yield ['freeze_pins', '{}']; yield ['freeze_revision', '2']; } };
    const errors = [];
    win.EW.formError = (message) => { errors.push(String(message)); };
    const submitBtn = { disabled: false, textContent: 'Confirm' };
    const FOOT = '<button class="button" type="button" data-action="close-dialog">Cancel</button><button class="button primary" type="submit">Confirm</button>';
    const footer = { innerHTML: FOOT };
    const question = { removed: false, remove() { this.removed = true; } };
    const headClose = { hidden: false };
    const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} }, querySelector: () => null, querySelectorAll: () => [] };
    win.document.querySelector = sel => (sel === '#modal .dialog-head [data-action="close-dialog"]' ? headClose : dummy);
    const form = {
      getAttribute: () => 'confirm',
      querySelector: sel => (sel === '[type=submit]' ? submitBtn : sel === '.dialog-footer' ? footer
        : sel === '#form-errors + p' ? question : null),
      querySelectorAll: () => [],
      insertBefore: () => {},
      firstChild: null,
    };
    await win.EWViews.handleSubmit(form);
    assert.deepEqual(errors, [copy], kind);
    if (copy === FREEZE_RIGHTS) {
      // FREEZE r3 (screen r1 MED): a rights refusal fails the same way every
      // time, so Confirm goes and Cancel becomes Close.
      assert.doesNotMatch(footer.innerHTML, /type="submit"|Confirm|Cancel/, kind);
      assert.match(footer.innerHTML, /data-action="close-dialog"[^>]*>Close<\/button>/, kind);
      assert.equal(submitBtn.disabled, true, kind);
      // FREEZE r4: a finished outcome. The question "Freeze … now?" goes and
      // the footer's Close is the one visible Close.
      assert.equal(question.removed, true, kind);
      assert.equal(headClose.hidden, true, kind);
    } else {
      // A general refusal may pass on retry: Confirm and the question stay.
      assert.equal(footer.innerHTML, FOOT, kind);
      assert.equal(submitBtn.disabled, false, kind);
      assert.equal(question.removed, false, kind);
      assert.equal(headClose.hidden, false, kind);
    }
  }
});

test('FREEZE r3: the confirmation says what a frozen report is in plain words', async () => {
  for (const shown of [{ cid: '2', pins: {}, revision: 2, mode: 'research', targets: 1 },
                       { cid: '2', pins: {}, revision: 2, mode: 'supplied_requirements', placements: 2 }]) {
    const win = loadViews();
    const modal = { innerHTML: '', setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
    win.EW.openDialog = () => {};
    const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} }, querySelector: () => null, querySelectorAll: () => [] };
    win.document.querySelector = sel => (sel === '#modal' ? modal : dummy);
    win.EW.current.displayedDraft = shown;
    await win.EWViews.handleAction({ dataset: { action: 'freeze-report', id: '2' }, disabled: false, closest: () => null });
    assert.match(modal.innerHTML, /<h2 id="dialog-title">Save a final report<\/h2>/, shown.mode);
    assert.match(modal.innerHTML, /Save a final report of [^<]* now\? It is a saved copy that won(&#39;|')t change\. If the campaign changes later, the report is marked Stale\./, shown.mode);
    assert.doesNotMatch(modal.innerHTML, /immutable|bytes/i, shown.mode);
  }
});

for (const [kind, error, copy] of FREEZE_REFUSALS) {
  test(`FREEZE r2 ${kind}: a refusal whose dialog was closed is a plain toast, never the raw text`, async () => {
    const t = await otherDialogWin(() => json({ ok: false, error }, false, 403));
    const form = inlineForm('confirm');
    form.closest = s => (s === '#modal' ? t.modal : null);
    form.insertBefore = () => {};
    t.win.FormData = class { entries() { return Object.entries({ confirm_action: 'freeze-report', confirm_id: '1', freeze_pins: '{}', freeze_revision: '1' })[Symbol.iterator](); } };
    const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
    t.modal.open = false; // the user pressed Close; nothing else opened
    t.release(); await p; await settleAll();
    assert.deepEqual(t.toasts, [copy], t.toasts.join(' | '));
    assert.equal(form.box.innerHTML, '', 'nothing painted into the invisible form');
  });
}

test('legacy campaign renders admitted read-only detail without calling draft', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path === '/customer/campaigns/1') return { campaign: { id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: ['no recorded brief type'], allowed_actions: ['read', 'stored_reports'] } };
    if (path === '/customer/campaigns/1/reports') return { reports: [{ report_no: 1, stale: false, captured_at: '2024-01-01T00:00:00Z' }] };
    throw Object.assign(new Error('must not fetch draft for legacy rows: ' + path), { status: 500 });
  };
  win.location.hash = '#/campaign/1';
  await win.EWViews.render();
  assert.ok(!calls.some(c => c === '/customer/campaigns/1/draft'), 'draft endpoint must not be called for legacy rows');
  assert.match(main.innerHTML, /This campaign was created before campaign types existed\. It(?:'|&#39;)s read-only until the workspace owner reviews it\./);
  assert.match(main.innerHTML, /Old stored/);
  assert.match(main.innerHTML, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">no recorded brief type/, 'server reasons kept, folded away');
  assert.match(main.innerHTML, /Report 1/);
  assert.doesNotMatch(main.innerHTML, /This record is not available/);
  assert.doesNotMatch(main.innerHTML, /data-action="(edit-campaign|add-target|add-placement|freeze-report)"/);
});

test('legacy campaign detail shows each review reason as its own paragraph', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/1') return { campaign: { id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: ["the placement's brief is free text", 'the campaign has no <b>registered</b> brief'], allowed_actions: ['read', 'stored_reports'] } };
    if (path === '/customer/campaigns/1/reports') return { reports: [] };
    throw Object.assign(new Error('unexpected ' + path), { status: 500 });
  };
  win.location.hash = '#/campaign/1';
  await win.EWViews.render();
  assert.match(main.innerHTML, /<p class="small muted">the placement(?:'|&#39;)s brief is free text<\/p><p class="small muted">the campaign has no &lt;b&gt;registered&lt;\/b&gt; brief<\/p>/);
  assert.doesNotMatch(main.innerHTML, /free text the campaign/);
});

test('active campaign with a failing draft still surfaces the real draft error', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Live', availability: 'active', allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'] } };
    throw Object.assign(new Error('gone'), { status: 400 });
  };
  win.location.hash = '#/campaign/2';
  await win.EWViews.render();
  assert.ok(calls.includes('/customer/campaigns/2/draft'), 'normal rows still consult the draft endpoint');
  assert.match(main.innerHTML, /We can(?:'|&#39;)t find this campaign/);
});

test('complete reassembled window hides the wire-cap bound note', async () => {
  // Boundary mirror of the observed 118830-char reassembly: a first
  // chunk crossing the 64KiB wire cap plus its exact continuation.
  const REF = 'sr1:562f6108c5214cd28f771d1d0a6aaaaf:w:1200.0:1210.0';
  const first = 'a'.repeat(70000);
  const late = 'b'.repeat(48830);
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/records/' + encodeURIComponent(REF)) {
      return { record: { ref: REF, record_kind: 'evidence', status: 'finding', review: null,
        attribution: [{ brand: 'Lumen', basis: 'spoken' }], relationship: 'current', availability: 'current',
        bounds_basis: 'source bounds', selected_bounds: { start_s: 1200, end_s: 1210 },
        episode: { show_title: 'Morning brew', ep_title: 'Episode', pub_date: '2026-01-01' },
        evidence: {}, media: {},
        window: { segments: [{ text: first }], text_complete: true, next_cursor: null, truncation_reason: 'byte_cap' } } };
    }
    if (path === '/customer/records/' + encodeURIComponent(REF) + '/mistake') return { mistakes: [] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/evidence/' + encodeURIComponent(REF);
  await win.EWViews.render();
  assert.match(main.innerHTML, /Complete text\./);
  assert.doesNotMatch(main.innerHTML, /Window bound reached/);
  assert.equal(main.innerHTML.split(first).length - 1, 1);
});

test('record media with the audited rotated trust renders the neutral no-clip sentence, no player', async () => {
  // Follow-up K: a remote episode audited "rotated" (2026-09-24) keeps
  // its caveat on the record page; the audit never adds a player.
  const REF = 'sr1:562f6108c5214cd28f771d1d0a6aaaaf:w:10.0:20.0';
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/records/' + encodeURIComponent(REF)) {
      return { record: { ref: REF, record_kind: 'evidence', status: 'finding', review: null,
        attribution: [], relationship: 'current', availability: 'current',
        bounds_basis: 'source bounds', selected_bounds: { start_s: 10, end_s: 20 },
        episode: { show_title: 'Show', ep_title: 'Episode', pub_date: '2026-01-01' },
        evidence: {}, media: { playback: 'remote', alignment: 'unknown', timing: 'unknown', trust: 'rotated' },
        window: { segments: [{ text: 'body' }], text_complete: true, next_cursor: null } } };
    }
    if (path === '/customer/records/' + encodeURIComponent(REF) + '/mistake') return { mistakes: [] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/evidence/' + encodeURIComponent(REF);
  await win.EWViews.render();
  // CLIP design 6.2: no clip for any reason gives one neutral sentence, no reason.
  assert.ok(main.innerHTML.includes('No clip for this ad. The words are below.'));
  assert.doesNotMatch(main.innerHTML, /Rotated audio feed|Remote recording only/);
  assert.doesNotMatch(main.innerHTML, /<audio/);
});

// Mocked-DOM continuation wiring and exact baseline copy tests were
// removed per tests policy: Main actual-DOM/state proof is the
// evidence for those surfaces. Genuine behavior regressions stay.

test('edit alert with named event_types array preserves the actual subset', async () => {
  // F12 root-DTO mirror: alert3 saved with offer_difference unchecked
  // (defrev3). Reopening the editor must show the real subset from
  // the named tracker_dto array, so a name-only edit reloads and
  // resubmits the actual types instead of all-checked.
  const win = loadViews();
  const stubEl = () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; } });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector(sel) { return sel === '#alert-form' ? null : stubEl(); } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/alerts/a3?summaries=0') return { alert: { id: 'a3', name: 'Offer watch', revision: 4,
      definition_revision: 3, baseline_complete: true, last_successful_definition_revision: 2,
      query: { version: 2, mode: 'mentions', q: 'lumen' },
      event_types: ['new_text', 'new_finding'] } };
    if (path === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'a3' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /value="new_text" checked/);
  assert.match(modal.innerHTML, /value="new_finding" checked/);
  assert.match(modal.innerHTML, /value="offer_difference"/);
  assert.doesNotMatch(modal.innerHTML, /value="offer_difference" checked/);
});

test('edit alert without the known event_types field checks nothing, never all', async () => {
  // No raw-blob alias and no default-all when the named field is
  // absent: the editor opens with nothing checked so a save cannot
  // silently widen tracking.
  const win = loadViews();
  const stubEl = () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; } });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector(sel) { return sel === '#alert-form' ? null : stubEl(); } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/alerts/a3?summaries=0') return { alert: { id: 'a3', name: 'Offer watch', revision: 4,
      definition_revision: 3, baseline_complete: true, last_successful_definition_revision: 2,
      query: { version: 2, mode: 'mentions', q: 'lumen' } } };
    if (path === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'a3' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /value="offer_difference"/);
  assert.doesNotMatch(modal.innerHTML, /checked/);
});

test('research campaign editor exposes no message/offer requirement fields', async () => {
  // Locked design9: research keeps name/brand/notes plus
  // targets/evidence — Message/Offer belong to supplied mode only,
  // never as disabled placeholders.
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study', brand: 'Lumen', notes: 'scope notes' } };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '2' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /id="campaign-name"/);
  assert.match(modal.innerHTML, /id="campaign-brand"/);
  assert.match(modal.innerHTML, /id="campaign-notes"/);
  assert.doesNotMatch(modal.innerHTML, /campaign-message/);
  assert.doesNotMatch(modal.innerHTML, /campaign-offer/);
  assert.doesNotMatch(modal.innerHTML, /campaign-brief/);
});

test('supplied campaign editor keeps message/offer/brief requirement fields', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  const BID = 'brief-4f42641762de4eec';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/3') return { campaign: { id: 3, mode: 'supplied_requirements', revision: 7, name: 'Fixture supplied', message: 'Say this', offer: 'CODE', requirement_provenance: { brief_id: BID, version: 1, digest: 'd1', source_label: 'Fixture brief' } } };
    if (path.startsWith('/customer/briefs?q=')) return { items: [{ brief_id: BID, current_version: 2, version: 2, title: 'Fixture brief' }] };
    if (path === `/customer/briefs/${BID}/versions/1`) return { brief_version: { brief_id: BID, version: 1, title: 'Fixture brief', digest: 'd1' } };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '3' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /id="campaign-message"/);
  assert.match(modal.innerHTML, /id="campaign-offer"/);
  assert.match(modal.innerHTML, /id="campaign-brief"/);
});

test('create campaign shows requirement fields only for the supplied type choice', async () => {
  const win = loadViews();
  let modeValue = 'research';
  let changeFn = null;
  const formStub = {
    addEventListener(ev, fn) { if (ev === 'change') changeFn = fn; },
    querySelector(sel) { return sel === 'input[name=mode]:checked' ? { value: modeValue } : null; },
  };
  const hosts = {};
  const hostFor = () => ({ innerHTML: '' });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; },
    querySelector(sel) {
      if (sel === '#campaign') return formStub;
      if (sel === '#brief-picker' || sel === '#requirement-fields') return (hosts[sel] = hosts[sel] || hostFor());
      return { focus() {} };
    } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (path) => {
    if (path.startsWith('/customer/briefs')) return { items: [] };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'create-campaign' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /id="requirement-fields"/);
  assert.equal(hosts['#requirement-fields'].innerHTML, '', 'research default shows no requirement fields');
  assert.equal(hosts['#brief-picker'].innerHTML, '', 'research default shows no brief picker');
  modeValue = 'supplied_requirements';
  changeFn({ target: { name: 'mode' } });
  assert.match(hosts['#requirement-fields'].innerHTML, /id="campaign-message"/);
  assert.match(hosts['#requirement-fields'].innerHTML, /id="campaign-offer"/);
  assert.match(hosts['#brief-picker'].innerHTML, /id="campaign-brief"/);
  modeValue = 'research';
  changeFn({ target: { name: 'mode' } });
  assert.equal(hosts['#requirement-fields'].innerHTML, '', 'switching back clears requirement fields');
});

test('supplied detail surfaces all six authoritative outcomes with reasons and ids', async () => {
  // F16: draft.allocation.evaluations carries the six real outcomes;
  // detail cards show each outcome with its server reasons, eligible
  // ids and elsewhere ids instead of reducing them to one label.
  const OUTCOMES = ['supported', 'contradicted', 'pending', 'ambiguous', 'no_observation_in_covered_sources', 'incomplete_coverage'];
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const placements = OUTCOMES.map((o, i) => ({ id: 'exp:p' + i, scope: 'show_window', show_id: 's1', expected_count: 1 }));
  placements.push({ id: 'exp:cite', scope: 'show_window', show_id: 's1', expected_count: 1 });
  const evaluations = {};
  OUTCOMES.forEach((o, i) => {
    evaluations['exp:p' + i] = { outcome: o, reasons: [`reason for ${o}`], eligible_ids: [`occ:eligible-${i}`], allocated_elsewhere: i === 0 ? ['occ:moved-away'] : [] };
  });
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/3') return { campaign: { id: 3, mode: 'supplied_requirements', revision: 7, name: 'Supplied six', requirement_provenance: { brief_id: 'b1', version: 1 } } };
    if (path === '/customer/campaigns/3/draft') return { draft: { pins: {}, campaign: { revision: 7 }, mode: 'supplied_requirements',
      placements, targets: [], assessment_scope: {},
      allocation: { evaluations, citations: { 'exp:cite': { binding: 'citation_only', reasons: ['citation_only placements never allocate'] } } },
      coverage_by_placement: Object.fromEntries(placements.map(p => [p.id, { state: 'complete' }])) } };
    if (path === '/customer/campaigns/3/reports') return { reports: [] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/campaign/3';
  await win.EWViews.render();
  // Each outcome shows distinctly in plain words; its raw token stays in Technical details.
  const shown = OUTCOMES.map(o => {
    const m = new RegExp(`Requirement outcome: ([^<]+)\\.</p>(?:(?!Requirement outcome:)[\\s\\S])*?Outcome <span class="mono">${o}</span>`).exec(main.innerHTML);
    assert.ok(m, `outcome ${o} is shown, raw token in details`);
    assert.doesNotMatch(m[1], /_/, `outcome ${o} reads as plain words`);
    return m[1];
  });
  assert.equal(new Set(shown).size, OUTCOMES.length, 'each outcome is shown distinctly');
  assert.match(main.innerHTML, /Requirement outcome: No match found in the sources we checked\./);
  assert.match(main.innerHTML, /reason for contradicted/);
  assert.match(main.innerHTML, /occ:eligible-2/);
  assert.match(main.innerHTML, /occ:moved-away/);
  assert.match(main.innerHTML, /Citation only/);
});

test('research detail carries no requirement verdict', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study' } };
    if (path === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research',
      placements: [], targets: [{ id: 't1', scope: 'episode', episode_id: 'e1' }], allocation: {}, coverage_by_placement: {} } };
    if (path === '/customer/campaigns/2/reports') return { reports: [] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/campaign/2';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Research targets/);
  assert.doesNotMatch(main.innerHTML, /Requirement outcome:/);
  assert.doesNotMatch(main.innerHTML, /Citation only/);
});

test('name-only alert save resubmits the full initial query verbatim', async () => {
  // F11: brands [] must stay [], multi shows must stay multi, and
  // dates/sort/ids must round-trip when only the name is touched.
  const win = loadViews();
  win.document.querySelector = () => ({ innerHTML: '', textContent: '' });
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.EW.toast = () => {};
  win.EW.render = () => {};
  let went = null;
  win.EW.go = (r) => { went = r; };
  const bodies = [];
  win.EW.api = async (path, opts) => {
    bodies.push([path, opts && opts.body]);
    return { id: '5' };
  };
  const initialQuery = { version: 2, mode: 'mentions', q: '', brands: [],
    shows: ['s1', 's2'], categories: ['c1'], statuses: null, audio: null,
    date_status: 'all', from: '2026-01-01', to: null, sort: 'oldest',
    page: 1, ids: null };
  win.FormData = class { *entries() {
    yield ['name', 'Watch']; yield ['q', '']; yield ['mode', 'mentions'];
    yield ['brand', '']; yield ['show', '']; yield ['status', '']; yield ['audio', ''];
    yield ['from', '2026-01-01']; yield ['to', ''];
    yield ['event_types', 'new_text'];
    yield ['initial_query', JSON.stringify(initialQuery)];
    yield ['initial_selector', 'null']; yield ['touched_fields', '[]'];
  } };
  const form = {
    getAttribute: () => 'alert-form',
    querySelector: () => ({ disabled: false, textContent: 'Save' }),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  assert.equal(bodies.length, 1);
  assert.deepEqual(plain(bodies[0][1].query), initialQuery);
  assert.deepEqual(plain(bodies[0][1].selector), null);
  assert.equal(went, '#/alert/5');
});

test('touched alert brand overwrites only brands', async () => {
  const win = loadViews();
  win.document.querySelector = () => ({ innerHTML: '', textContent: '' });
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.EW.toast = () => {};
  win.EW.render = () => {};
  win.EW.go = () => {};
  const bodies = [];
  win.EW.api = async (path, opts) => {
    bodies.push([path, opts && opts.body]);
    return { id: '6' };
  };
  const initialQuery = { version: 2, mode: 'mentions', q: '', brands: [],
    shows: ['s1', 's2'], categories: null, statuses: null, audio: null,
    date_status: 'all', from: null, to: null, sort: 'newest',
    page: 1, ids: null };
  win.FormData = class { *entries() {
    yield ['name', 'Watch']; yield ['q', '']; yield ['mode', 'mentions'];
    yield ['brand', 'Lumen']; yield ['show', '']; yield ['status', '']; yield ['audio', ''];
    yield ['from', '']; yield ['to', ''];
    yield ['event_types', 'new_text'];
    yield ['initial_query', JSON.stringify(initialQuery)];
    yield ['initial_selector', 'null']; yield ['touched_fields', '["brand"]'];
  } };
  const form = {
    getAttribute: () => 'alert-form',
    querySelector: () => ({ disabled: false, textContent: 'Save' }),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  assert.deepEqual(plain(bodies[0][1].query.brands), ['Lumen']);
  assert.deepEqual(plain(bodies[0][1].query.shows), ['s1', 's2']);
});

test('saved scope distinguishes exact-empty brands from unrestricted', async () => {
  // Alert6 case: persisted brands [] must read exact-empty, never
  // All brands; a null brands predicate keeps the unrestricted label.
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  let brands = [];
  win.EW.api = async (path) => {
    if (path === '/customer/alerts/a6') return { alert: { id: 'a6', name: 'Empty scope', revision: 1,
      lifecycle: 'active_local', definition_revision: 1, baseline_complete: true,
      last_successful_definition_revision: 1, last_successful_at: '2026-10-07T00:00:00Z',
      unread_event_count: 0, events: [], current_matches: { count: 0 },
      query: { version: 2, mode: 'mentions', q: '', brands, shows: null } } };
    if (path === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/alert/a6';
  await win.EWViews.render();
  assert.match(main.innerHTML, /exact-empty \(matches nothing\)/);
  assert.doesNotMatch(main.innerHTML, /All brands/);
  brands = null;
  await win.EWViews.render();
  assert.match(main.innerHTML, /All brands/);
});

test('create watching text marks retained exact-empty brand sets', async () => {
  // Dialog scope line must visibly retain an explicit [] instead of
  // omitting it as unrestricted scope.
  const win = loadViews();
  const stubEl = () => ({ focus() {}, addEventListener() {}, appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; } });
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector(sel) { return sel === '#alert-form' ? null : stubEl(); } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/alerts/a6?summaries=0') return { alert: { id: 'a6', name: 'Empty scope', revision: 1,
      event_types: ['new_text'],
      query: { version: 2, mode: 'mentions', q: '', brands: [], shows: null } } };
    if (path === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'a6' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /This alert watches:/);
  assert.match(modal.innerHTML, /brand exact-empty \(matches nothing\)/);
});

test('supplied editor shows stored brief text and add-text controls', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  const BID = 'brief-4f42641762de4eec';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/3') return { campaign: { id: 3, mode: 'supplied_requirements', revision: 7, name: 'Fixture supplied', requirement_provenance: { brief_id: BID, version: 1, digest: 'd1', source_label: 'Fixture brief' } } };
    if (path.startsWith('/customer/briefs?q=')) return { items: [{ brief_id: BID, current_version: 2, version: 2, title: 'Fixture brief' }] };
    if (path === `/customer/briefs/${BID}/versions/1`) return { brief_version: { brief_id: BID, version: 1, title: 'Fixture brief', digest: 'd1', provided_text: 'Say lumen clearly' } };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '3' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, new RegExp(`value="${BID}:1" selected`));
  assert.match(modal.innerHTML, /Say lumen clearly/);
  assert.match(modal.innerHTML, /id="brief-add"/);
  assert.match(modal.innerHTML, /id="brief-text"/);
  assert.match(modal.innerHTML, /data-action="brief-register"/);
});

test('edit dialog brief registration appends and selects the new version', async () => {
  const win = loadViews();
  const statusBox = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#brief-status' ? statusBox : { innerHTML: '', textContent: '' });
  win.document.createElement = (tag) => ({ tagName: String(tag), dataset: {} });
  const added = [];
  const select = { value: '', appendChild(o) { added.push(o); return o; } };
  const form = { querySelector: sel => {
    if (sel === '#brief-title') return { value: 'New brief' };
    if (sel === '#brief-text') return { value: 'Say lumen loudly' };
    if (sel === '#campaign-brief') return select;
    return null;
  } };
  const posts = [];
  win.EW.api = async (path, opts) => {
    posts.push([path, opts && opts.body]);
    if (path === '/customer/briefs') return { brief_id: 'brief-9', version: 1 };
    throw new Error('unexpected request ' + path);
  };
  const el = { dataset: { action: 'brief-register' }, disabled: false, closest: () => form };
  await win.EWViews.handleAction(el);
  assert.equal(posts.length, 1);
  assert.deepEqual(plain(posts[0][1]), { title: 'New brief', provided_text: 'Say lumen loudly' });
  assert.equal(added.length, 1);
  assert.equal(added[0].value, 'brief-9:1');
  assert.equal(select.value, 'brief-9:1');
  assert.match(statusBox.innerHTML, /New brief/, 'named by its title, never its internal id');
  assert.doesNotMatch(statusBox.innerHTML, /brief-9/);
});

test('supplied detail shows the registered brief original text', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const BID = 'brief-4f42641762de4eec';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/3') return { campaign: { id: 3, mode: 'supplied_requirements', revision: 7, name: 'Fixture supplied', requirement_provenance: { brief_id: BID, version: 1, digest: 'd1', source_label: 'Fixture brief' } } };
    if (path === '/customer/campaigns/3/draft') return { draft: { pins: {}, campaign: { revision: 7 }, mode: 'supplied_requirements',
      placements: [], targets: [], assessment_scope: {}, allocation: {}, coverage_by_placement: {} } };
    if (path === '/customer/campaigns/3/reports') return { reports: [] };
    if (path === `/customer/briefs/${BID}/versions/1`) return { brief_version: { brief_id: BID, version: 1, title: 'Fixture brief', digest: 'd1', provided_text: 'Say lumen clearly' } };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/campaign/3';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Registered brief/);
  assert.match(main.innerHTML, /Say lumen clearly/);
});

test('reference allocation addresses the holding unit revision', () => {
  const win = loadViews();
  const body = plain(win.EWViews.allocationBody({ action: 'reference', ref: 'sr1:x', from: 'exp:a', to: 'exp:b', crev: 7, frev: 2, trev: 9 }));
  assert.equal(body.to_target_id, 'exp:b');
  assert.equal(body.expected_from_revision, 2, 'reference carries the holding from-revision');
  assert.equal(body.expected_to_revision, 2, 'reference carries the holding revision, not the destination revision');
});

test('allocation 409 keeps choice and renders reasoned conflict', async () => {
  const win = loadViews();
  const errBox = { innerHTML: '', setAttribute() {}, querySelector: () => ({ focus() {} }) };
  const submitBtn = { disabled: false, textContent: 'Confirm move' };
  const form = {
    getAttribute: () => 'allocation-confirm',
    querySelector: sel => {
      if (sel === '#form-errors') return errBox;
      if (sel === '[type=submit]') return submitBtn;
      return null;
    },
    querySelectorAll: () => [],
  };
  win.document.querySelector = () => ({ innerHTML: '', textContent: '' });
  win.EW.saveNotice = () => {};
  let renders = 0, closed = 0;
  win.EW.render = () => { renders++; };
  win.EW.closeDialog = () => { closed++; };
  const bodies = [];
  win.EW.api = async (path, opts) => {
    bodies.push([path, opts && opts.body]);
    throw Object.assign(new Error('stale basis'), { status: 409,
      body: { ok: false, conflict: { action: 'move', ref: 'sr1:x', from_target_id: 'exp:a', to_target_id: 'exp:b',
        expected: { campaign_revision: 7, from_revision: 2, to_revision: 2 },
        current: { campaign_revision: 8, from_revision: 3, to_revision: 2 },
        units: { from: { unit_id: 'exp:a', kind: 'placement', bound: true, credit_eligible: true },
                 to: { unit_id: 'exp:b', kind: 'placement', bound: false, credit_eligible: false } },
        allowed_operations: ['retain', 'move', 'reference'] } } });
  };
  win.FormData = class { *entries() {
    yield ['campaign_id', '3']; yield ['allocation_action', 'move']; yield ['allocation_ref', 'sr1:x'];
    yield ['from_target_id', 'exp:a']; yield ['to_target_id', 'exp:b'];
    yield ['expected_campaign_revision', '7']; yield ['expected_from_revision', '2'];
    yield ['units_json', JSON.stringify({ 'exp:a': 2, 'exp:b': 2 })];
  } };
  await win.EWViews.handleSubmit(form);
  assert.equal(bodies.length, 1, 'exactly one attempted request, no replay');
  assert.deepEqual(plain(bodies[0][1]), { action: 'move', ref: 'sr1:x', from_target_id: 'exp:a', to_target_id: 'exp:b',
    expected_campaign_revision: 7, expected_from_revision: 2, expected_to_revision: 2 });
  assert.equal(closed, 0, 'dialog stays open on conflict');
  assert.equal(renders, 0, 'no rebase or replay on conflict');
  assert.equal(submitBtn.disabled, true, 'stale Confirm cannot resubmit before reload');
});

test('saved query label marks exact-empty sets and retained scope', () => {
  const win = loadViews();
  const label = win.EWSaved.describeQuery({ mode: 'mentions', q: '', brands: [], shows: ['s1', 's2'],
    categories: null, statuses: [], audio: null, date_status: 'all',
    from: null, to: null, sort: 'oldest', ids: [] });
  assert.match(label, /brands: exact-empty \(matches nothing\)/);
  assert.match(label, /shows: s1, s2/);
  assert.match(label, /status: exact-empty \(matches nothing\)/);
  assert.match(label, /exact-empty ids/);
  assert.match(label, /sort: oldest/);
  const open = win.EWSaved.describeQuery({ mode: 'mentions', brands: null, shows: null });
  assert.doesNotMatch(open, /exact-empty/);
});

test('saved filter round-trips spaces and literal plus signs', async () => {
  const win = loadAt('#/saved?tab=queries&q=Exact+empty');
  win.EW.current.route = '#/saved?tab=queries&q=Exact+empty';
  const paths = [];
  const api = async (path) => { paths.push(path); return { items: [{ id: 7, type: 'query', title: 'Exact empty saved scope acceptance', revision: 1, query: { version: 2, mode: 'mentions', q: '', brands: [], shows: null } }], total: 1, total_relation: 'exact', next_cursor: null }; };
  const render = () => win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  const html = await render();
  assert.match(html, /value="Exact empty"/);
  assert.match(html, /Exact empty saved scope acceptance/);
  assert.ok(paths.some(p => /[?&]q=Exact(\+|%20)empty/.test(p)), 'decoded filter sent to the list read');
  win.EW.current.route = '#/saved?tab=queries&q=a%2Bb&cursor=c1';
  const api2 = async (path) => { paths.push(path); return { items: [{ id: 9, type: 'query', title: 'a+b records', revision: 1, query: { mode: 'mentions' } }], total: 1, total_relation: 'exact', next_cursor: null }; };
  const html2 = await win.EWSaved.render({ api: api2, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.match(html2, /value="a\+b"/);
  assert.match(html2, /a\+b records/);
  assert.ok(paths.some(p => /[?&]q=a%2Bb/.test(p)), 'literal plus preserved, not space');
  assert.ok(paths.some(p => /[?&]cursor=c1/.test(p)), 'cursor cohort binding preserved');
});

test('saved item titles reuse the real destination, never the bare hub', async () => {
  const win = loadAt('#/saved?tab=queries');
  win.EW.current.route = '#/saved?tab=queries';
  const items = [
    { id: 7, type: 'query', title: 'Exact empty saved scope acceptance', revision: 1,
      query: { version: 2, mode: 'mentions', q: '', brands: [], shows: null } },
    { id: 8, type: 'query', title: 'Broken', revision: 1, query: { mode: 'nope' } },
  ];
  const api = async () => ({ items, total: 2, total_relation: 'exact', next_cursor: null });
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link,
    scope: {}, workspaceContext: '', signal: null, isCurrent: true });
  assert.doesNotMatch(html, /href="#\/saved"/);
  const title = html.match(/<a class="" href="([^"]+)">Exact empty saved scope acceptance<\/a>/);
  assert.ok(title, 'exact-empty title links somewhere real');
  // F-24: the title is the one link to the current results.
  assert.doesNotMatch(html, /Open current results/, 'no second Open link to the same destination');
  assert.match(title[1], /brands=__none__/);
  assert.match(html, /brands: exact-empty \(matches nothing\)/);
  assert.match(html, /<span>Broken<\/span>/);
});

test('supplied editor shows admitted brief v1 selected, never none or latest', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  const BID = 'brief-4f42641762de4eec';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/3') return { campaign: { id: 3, mode: 'supplied_requirements', revision: 7, name: 'Fixture supplied', requirement_provenance: { brief_id: BID, version: 1, digest: 'd1', source_label: 'Fixture brief' } } };
    if (path.startsWith('/customer/briefs?q=')) return { items: [{ brief_id: BID, current_version: 2, version: 2, title: 'Fixture brief' }] };
    if (path === `/customer/briefs/${BID}/versions/1`) return { brief_version: { brief_id: BID, version: 1, title: 'Fixture brief', digest: 'd1' } };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '3' }, disabled: false, closest: () => null });
  // VL-09: named by title and version; the id sits in Technical details.
  assert.match(modal.innerHTML, /Registered brief: “Fixture brief” version 1\./);
  assert.match(modal.innerHTML, new RegExp(`Brief <span class="mono">${BID}</span> v1`));
  assert.match(modal.innerHTML, new RegExp(`value="${BID}:1" selected`));
  assert.doesNotMatch(modal.innerHTML, /none v\?/);
  assert.doesNotMatch(modal.innerHTML, new RegExp(`value="${BID}:2" selected`));
});

test('target show select qualifies duplicate titles with source ids', async () => {
  const win = loadViews();
  const modal = { innerHTML: '', open: false, setAttribute() {}, showModal() { this.open = true; }, close() { this.open = false; }, querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.current.route = '#/campaign/2';
  win.EW.api = async (path) => {
    if (path === '/customer/campaigns/2') return { campaign: { id: 2, revision: 5 } };
    if (path === '/customer/shows') return { shows: [
      { id: 's-morning-1', display_title: 'Morning brew' },
      { id: 's-morning-2', display_title: 'Morning brew' },
      { id: 's-evening', display_title: 'Evening blend' } ] };
    if (path.startsWith('/customer/shows/')) return { episodes: [] };
    throw new Error('unexpected request ' + path);
  };
  await win.EWViews.handleAction({ dataset: { action: 'add-target' }, disabled: false, closest: () => null });
  assert.match(modal.innerHTML, /Morning brew \(s-morning-1\)/);
  assert.match(modal.innerHTML, /Morning brew \(s-morning-2\)/);
  assert.match(modal.innerHTML, /<option value="s-evening">Evening blend<\/option>/);
  assert.match(modal.innerHTML, /<option value="s-morning-1">/);
  assert.match(modal.innerHTML, /<option value="s-morning-2">/);
});

test('attribution qualification follows admitted assoc basis, never kind or status', () => {
  const win = loadViews();
  const V = win.EWViews;
  assert.equal(V.attributionConfirmed({ attribution: [{ brand: 'Lumen', basis: 'spoken' }] }), false);
  assert.equal(V.attributionConfirmed({ attribution: [{ brand: 'Lumen', basis: 'confirmed' }] }), true);
  assert.equal(V.attributionConfirmed({ attribution: [] }), false);
  assert.equal(V.attributionConfirmed({}), false);
});

// PLAIN-ADS-FIX3 (screen R3-02): the row has no quote, and only an ad
// whose brand shows is listed.
test('overview recent rows carry the brand, no quote and a specific evidence link', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (path) => {
    if (path === '/customer/overview') return { coverage: { episodes: 1, shows: 1, episodes_with_transcript: 1 }, recent_events: [], offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [] };
    if (path.startsWith('/customer/search')) return { items: [{ ref: 'ev:1@v1', ep_title: 'Morning brew ep', episode_id: 'e1', show_title: 'Morning brew', pub_date: '2026-03-10', text: 'This episode is brought to you by Lumen. ' + 'x'.repeat(200), record_kind: 'evidence', status: 'finding',
      read: { brand: { key: 'lumen', display: 'Lumen', confirmed: false } } }] };
    throw new Error('unexpected request ' + path);
  };
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Recent ads/);
  assert.match(main.innerHTML, /Morning brew ep/);
  assert.doesNotMatch(main.innerHTML, /Possible ad/, 'PLAIN-ADS: no status on the row');
  assert.doesNotMatch(main.innerHTML, /This episode is brought to you by Lumen/);
  assert.match(main.innerHTML, />Lumen<\/span><\/strong>/);
  assert.match(main.innerHTML, /#\/evidence\/ev%3A1%40v1/);
  assert.match(main.innerHTML, /Open evidence →/);
});

test('async dialog replacement restores focus from body, never steals moved focus', () => {
  const win = loadViews();
  const V = win.EWViews;
  let focused = null;
  const control = { focus() { focused = 'control'; } };
  const dlg = { open: true, querySelector: () => control };
  win.document.body = {};
  win.document.activeElement = win.document.body;
  V.restoreDialogFocus(dlg);
  assert.equal(focused, 'control');
  focused = null;
  win.document.activeElement = { tagName: 'INPUT' };
  V.restoreDialogFocus(dlg);
  assert.equal(focused, null);
  focused = null;
  V.restoreDialogFocus({ open: false, querySelector: () => control });
  assert.equal(focused, null);
});

test('saved stale 412 with structured error shows conflict, keeps the draft', async () => {
  const win = loadViews();
  let renders = 0;
  const toasts = [];
  win.EW.render = () => { renders++; };
  win.EW.toast = (m) => { toasts.push(m); };
  win.EW.saveNotice = () => {};
  win.EW.api = async () => {
    throw Object.assign(new Error('stale revision; current is 2'), { status: 412,
      body: { ok: false, error: { code: 'CONFLICT', message: 'stale revision; current is 2', status: 412, details: { current_revision: 2 } } } });
  };
  win.FormData = class { *entries() { yield ['item_id', 'si-410f0505504744a1']; yield ['expected_revision', '1']; yield ['title', 'Browser unsaved conflicting title']; yield ['note', '']; } };
  const errors = { innerHTML: '', focus() {} };
  const submitBtn = { disabled: false, textContent: 'Save changes' };
  const form = {
    getAttribute: () => 'edit-saved',
    querySelector: sel => (sel === '[type=submit]' ? submitBtn : sel === '#form-errors' ? errors : sel === '#edit-saved-title' ? { focus() {} } : null),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  // No TypeError alert, no rebase, no render: the conflict box names
  // the stored revision and the verbatim draft is stashed for review.
  assert.ok(!toasts.some(t => /replace is not a function/i.test(t)));
  assert.match(errors.innerHTML, /Current server revision is 2/);
  assert.match(errors.innerHTML, /Reload and restore my entries/);
  assert.equal(win.EW.current.staleDraft.rev, '2');
  assert.equal(win.EW.current.staleDraft.formId, 'edit-saved');
  assert.equal(win.EW.current.staleDraft.values.title, 'Browser unsaved conflicting title');
  assert.equal(renders, 0);
});

test('note save records durable id/revision on the owning pin', async () => {
  const PIN = 'sr1:492eddaadd5749e6afa3671062abbfd2:w:0.0:10.0';
  const win = loadViews();
  let renders = 0;
  win.EW.render = () => { renders++; };
  win.EW.saveNotice = () => {};
  win.EW.toast = () => {};
  win.EW.api = async () => ({ ok: true, id: 12, revision: 2, aggregate_rev: 1 });
  win.FormData = class { *entries() { yield ['ref', PIN]; yield ['reason', 'Needs review']; yield ['status', 'open']; } };
  const form = {
    getAttribute: () => 'mistake',
    querySelector: sel => (sel === '[type=submit]' ? { disabled: false, textContent: 'Save' } : sel === '#form-errors' ? { innerHTML: '' } : sel === '#mistake-reason' ? { focus() {} } : null),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  assert.equal(win.EW.current.savedNote.id, 12);
  assert.equal(win.EW.current.savedNote.revision, 2);
  assert.equal(win.EW.current.savedNote.ref, PIN);
  assert.equal(renders, 1);
});

test('reader shows durable note outcome with note link; other pins clear it', async () => {
  const PIN = 'sr1:492eddaadd5749e6afa3671062abbfd2:w:0.0:10.0';
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const record = ref => ({ ref, record_kind: 'evidence', status: 'finding', review: null, attribution: [],
    relationship: 'current', availability: 'current', bounds_basis: 'source bounds',
    selected_bounds: { start_s: 0, end_s: 10 }, episode: { show_title: 'S', ep_title: 'E', pub_date: '' },
    evidence: {}, media: {}, window: { segments: [{ text: 'quoted text' }], text_complete: true, next_cursor: null } });
  win.EW.api = async (path) => {
    if (path === '/customer/records/' + encodeURIComponent(PIN)) return { record: record(PIN) };
    if (path === '/customer/records/' + encodeURIComponent(PIN) + '/mistake') return { mistakes: [] };
    if (path === '/customer/records/other/mistake') return { mistakes: [] };
    if (path === '/customer/records/other') return { record: record('other') };
    throw new Error('unexpected request ' + path);
  };
  win.EW.current.savedNote = { id: 12, revision: 2, ref: PIN };
  win.location.hash = '#/evidence/' + encodeURIComponent(PIN);
  await win.EWViews.render();
  assert.match(main.innerHTML, /Note saved\./);
  assert.match(main.innerHTML, /#\/note\/12/);
  assert.match(main.innerHTML, /revision 2/);
  win.location.hash = '#/evidence/other';
  await win.EWViews.render();
  assert.doesNotMatch(main.innerHTML, /Note saved\./);
});

test('signed no-member invite entry bypasses chooser with no customer reads', async () => {
  // L25: the only read is the invitee's read-only preflight.
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = '';
  win.EW.current.workspaces = [];
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path === '/workspaces/ws/invites/inv-1/preflight') return { invitation: { state: 'pending', role: 'viewer', workspace_name: 'Acme' } };
    throw new Error('no reads allowed: ' + path);
  };
  win.location.hash = '#/invite?workspace=ws&id=inv-1';
  await win.EWViews.render();
  assert.deepEqual([...calls], ['/workspaces/ws/invites/inv-1/preflight']);
  assert.match(main.innerHTML, /Workspace invitation/);
  assert.match(main.innerHTML, /Acme/);
  assert.match(main.innerHTML, /name="invite_id" value="inv-1"/);
  assert.match(main.innerHTML, /Accept invitation/);
  assert.doesNotMatch(main.innerHTML, /Use workspace/);
});

test('anonymous invite entry keeps sign-in with no customer reads', async () => {
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = null;
  win.EW.current.activeWorkspace = '';
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path === '/session') return { signin: { available: true } };
    throw new Error('no reads allowed: ' + path);
  };
  win.location.hash = '#/invite?workspace=ws&id=inv-1';
  await win.EWViews.render();
  assert.ok(calls.every(c => !c.startsWith('/customer/')));
  assert.match(main.innerHTML, /Sign in/);
});

test('invite accept posts exact empty body and surfaces durable result', async () => {
  const win = loadViews();
  win.document.querySelector = () => ({ innerHTML: '', textContent: '' });
  win.EW.saveNotice = () => {};
  let renders = 0;
  win.EW.render = () => { renders++; };
  const posts = [];
  win.EW.api = async (path, opts) => {
    posts.push([path, opts]);
    if (path === '/session') return { authenticated: true, principal: 'sam', active_workspace: '', workspaces: ['ws'], workspace_context: '' };
    return { ok: true };
  };
  win.FormData = class { *entries() { yield ['workspace', 'ws']; yield ['invite_id', 'inv-1']; } };
  const form = {
    getAttribute: () => 'invite-accept',
    querySelector: sel => (sel === '[type=submit]' ? { disabled: false, textContent: 'Accept invitation' } : sel === '#form-errors' ? { innerHTML: '' } : null),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  const accept = posts.find(([p]) => p.includes('/accept'));
  assert.ok(accept);
  assert.equal(accept[1].method, 'POST');
  assert.equal(JSON.stringify(accept[1].body), '{}');
  assert.ok(!('verified_email' in (accept[1].body || {})));
  assert.equal(win.EW.current.inviteAccepted.workspace, 'ws');
  assert.equal(win.EW.current.inviteAccepted.id, 'inv-1');
  assert.equal(renders, 1);
});

test('accepted invite shows lightweight chooser with no management reads', async () => {
  // Post-accept page: durable result plus a chooser built only from
  // the refreshed session workspaces — no session/members/shares/
  // billing reads, no Remove/Invite/Create-share controls.
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = ['ws'];
  win.EW.current.inviteAccepted = { workspace: 'ws', id: 'inv-1' };
  const calls = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('no reads allowed: ' + path); };
  win.location.hash = '#/invite?workspace=ws&id=inv-1';
  await win.EWViews.render();
  assert.equal(calls.length, 0, 'accepted page issues no reads at all');
  assert.match(main.innerHTML, /Invitation accepted/);
  assert.match(main.innerHTML, /Use workspace/);
  assert.match(main.innerHTML, /value="ws" selected/);
  assert.doesNotMatch(main.innerHTML, /<h2>Members<\/h2>/);
  assert.doesNotMatch(main.innerHTML, /member-remove/);
  assert.doesNotMatch(main.innerHTML, /Create share/);
  assert.doesNotMatch(main.innerHTML, /Queue delivery/);
});

test('entry workspace choice switches and exits even when locator already matches', async () => {
  // The session locator can already equal the explicit choice while
  // membership is stale: the choice still posts the switch and
  // leaves the entry for the member app. The stubbed commit advances
  // the canonical revision behind the POST, as the CAS would.
  const win = loadViews(canonicalSessionFetch({}, [7, 8]));
  win.document.querySelector = () => ({ innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.EW.toast = () => {};
  win.EW.current.activeWorkspace = 'ws';
  const posts = [];
  let went = null;
  win.EW.go = (r) => { went = r; };
  win.EW.render = () => {};
  win.EW.api = async (path) => {
    posts.push(path);
    if (path === '/session/workspace') return { active_workspace: 'ws' };
    throw new Error('unexpected ' + path);
  };
  win.FormData = class { *entries() { yield ['workspace_id', 'ws']; yield ['from_entry', 'invite']; } };
  const form = {
    getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace' }),
    querySelectorAll: () => [],
  };
  await win.EWViews.handleSubmit(form);
  assert.ok(posts.includes('/session/workspace'), 'explicit switch still posts');
  assert.equal(went, '#/overview', 'entry exits to the member app');
});

test('offline check leaves a durable tracker error with retry enabled', async () => {
  const win = loadViews();
  const box = { textContent: '', innerHTML: '', dataset: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); } };
  win.document.querySelector = sel => (sel === '#alert-progress' ? box : { innerHTML: '', textContent: '' });
  const toasts = [];
  win.EW.toast = (m) => { toasts.push(m); };
  let renders = 0;
  win.EW.render = () => { renders++; };
  win.EW.api = async () => { throw new Error('Failed to fetch'); };
  const btn = { dataset: { action: 'check-alert', id: 'a3' }, disabled: false, closest: () => null };
  await win.EWViews.handleAction(btn);
  assert.match(box.innerHTML, /Check failed/);
  assert.match(box.innerHTML, /unchanged/);
  assert.equal(box.attrs.role, 'alert');
  assert.equal(toasts.length, 0, 'durable inline error, never toast-only');
  assert.equal(renders, 0, 'stored query/baseline/history retained for retry');
  assert.equal(btn.disabled, false, 'retry stays enabled');
});

test('dropped check applies no tracker feedback at all', async () => {
  const win = loadViews();
  const box = { textContent: '', innerHTML: '', dataset: {}, attrs: {},
    setAttribute(k, v) { this.attrs[k] = String(v); } };
  win.document.querySelector = sel => (sel === '#alert-progress' ? box : { innerHTML: '', textContent: '' });
  const toasts = [];
  win.EW.toast = (m) => { toasts.push(m); };
  let renders = 0;
  win.EW.render = () => { renders++; };
  win.EW.api = async () => { throw Object.assign(new Error('late'), { droppedContext: true }); };
  await win.EWViews.handleAction({ dataset: { action: 'check-alert', id: 'a3' }, disabled: false, closest: () => null });
  assert.equal(box.innerHTML, '', 'no error written from old context');
  assert.equal(toasts.length, 0, 'no stale toast from old context');
  assert.equal(renders, 0, 'no state mutation from old context');
});

test('removed saved-searches popup actions stay inert with no fallback', async () => {
  // Duplicate popup cutover: neither the old dialog action nor its
  // open action may read, navigate, or reopen — the canonical Saved
  // queries hub is the only path, with no alias or broadening shim.
  const win = loadViews();
  const calls = [];
  let went = null;
  win.EW.api = async (path) => { calls.push(path); throw new Error('no reads: ' + path); };
  win.EW.go = (r) => { went = r; };
  await win.EWViews.handleAction({ dataset: { action: 'saved-searches' }, disabled: false, closest: () => null });
  await win.EWViews.handleAction({ dataset: { action: 'open-search', query: '{}' }, disabled: false, closest: () => null });
  assert.equal(calls.length, 0, 'no reads from removed actions');
  assert.equal(went, null, 'no fallback navigation from removed actions');
});

test('shared evidence renders granted fields only, never member controls', async () => {
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = '';
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    return { ok: true, title: 'Shared title', text: 'granted excerpt', evidence_id: 'ev-9' };
  };
  win.location.hash = '#/shared?workspace=ws&share_id=s1&type=evidence&id=ev-9';
  await win.EWViews.render();
  assert.ok(calls.every(c => !c.startsWith('/customer/')));
  assert.match(main.innerHTML, /Shared title/);
  assert.match(main.innerHTML, /granted excerpt/);
  assert.doesNotMatch(main.innerHTML, /data-action="(save-excerpt|save-bookmark|alert-evidence|report-mistake)"/);
  assert.doesNotMatch(main.innerHTML, /<audio/);
});

test('non-member shared entry with session workspace locator stays wordmark-only', async () => {
  // Exact-share guest entry where the session carries
  // active_workspace=ws but no workspaces entry or marker: the grant
  // renders, chrome stays wordmark-only, and no dataset or general
  // customer read fires (the backend 403 is never requested).
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  const headerEl = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? headerEl : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  let headerCalls = 0;
  win.EW.header = () => { headerCalls++; };
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = [];
  win.EW.current.workspaceContext = null;
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path.startsWith('/shared/')) return { ok: true, title: 'Shared title', text: 'granted excerpt', evidence_id: 'fixture-ad' };
    throw Object.assign(new Error('denied'), { status: 403 });
  };
  win.location.hash = '#/shared?workspace=ws&share_id=s1&type=evidence&id=fixture-ad';
  await win.EWViews.render();
  assert.ok(calls.every(c => !c.startsWith('/customer/')), 'no customer reads on exact guest entry');
  assert.equal(headerCalls, 0, 'member header never renders on guest entry');
  assert.match(headerEl.innerHTML, /EarWitness/);
  assert.doesNotMatch(headerEl.innerHTML, /Menu/);
  assert.match(main.innerHTML, /granted excerpt/);
});

test('guest entry clears member strip/footer and member render restores them', async () => {
  // Warm member→guest navigation: the exact entry hides the stale
  // dataset strip (resetting its cache) and member-only footer links
  // with zero generic reads; the next member render restores chrome.
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  const strip = { innerHTML: '255 episodes · 255 shows' };
  const footLinks = [{ hidden: false }, { hidden: false }];
  const dummy = { innerHTML: '', textContent: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#dataset' ? strip : dummy);
  win.document.querySelectorAll = sel => (/\.footer a/.test(sel) ? footLinks : []);
  win.scrollTo = () => {};
  let headerCalls = 0;
  win.EW.header = () => { headerCalls++; };
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = [];
  win.EW.current.workspaceContext = null;
  win.EW.current.dataset = '255 episodes · 255 shows · stored records only';
  const calls = [];
  win.EW.api = async (path) => {
    calls.push(path);
    if (path.startsWith('/shared/')) return { ok: true, title: 'Shared title', text: 'granted excerpt', evidence_id: 'fixture-ad' };
    throw Object.assign(new Error('denied'), { status: 403 });
  };
  win.location.hash = '#/shared?workspace=ws&share_id=s1&type=evidence&id=fixture-ad';
  await win.EWViews.render();
  assert.ok(calls.every(c => !c.startsWith('/customer/')), 'no customer reads on exact guest entry');
  assert.equal(headerCalls, 0, 'member header never renders on guest entry');
  assert.equal(strip.innerHTML, '', 'stale strip cleared on entry');
  assert.equal(win.EW.current.dataset, null, 'strip cache reset on entry');
  assert.ok(footLinks.every(a => a.hidden === true), 'member footer links hidden on entry');
  assert.match(main.innerHTML, /granted excerpt/);
  // Explicit workspace entry admits the member: marker issued.
  win.EW.current.workspaces = ['ws'];
  win.EW.current.workspaceContext = 'ctx-member';
  win.location.hash = '#/brands';
  await win.EWViews.render();
  assert.equal(headerCalls, 1, 'member chrome returns after explicit entry');
  assert.ok(footLinks.every(a => a.hidden === false), 'member footer links restored');
});

test('accessSection rethrows dropped signin, keeps genuine errors', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = null;
  win.EW.current.activeWorkspace = '';
  win.EW.api = async () => { throw Object.assign(new Error('late'), { droppedContext: true, body: { signin: { available: true } } }); };
  win.location.hash = '#/overview';
  await assert.rejects(win.EWViews.render(), /late/);
  win.EW.api = async () => { throw new Error('down'); };
  await win.EWViews.render();
  assert.match(main.innerHTML, /Sign in/);
});

test('superseded or dropped strip reads apply nothing; genuine errors keep fallback', async () => {
  const win = loadAt('#/overview');
  const strip = { innerHTML: 'OLD' };
  win.document.querySelector = sel => (sel === '#dataset' ? strip : { innerHTML: '', textContent: '' });
  // The strip reads counts in the local app only; a hosted workspace has no strip (P3-4).
  win.EW.current.hosted = false;
  const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  const okBody = () => ({ ok: true, status: 200, text: async () => JSON.stringify({ coverage: { episodes: 255, shows: 255 } }) });
  // Superseded success: navigation moves before completion.
  let release;
  win.fetch = () => new Promise(res => { release = res; });
  win.EW.current.route = '#/overview';
  win.EW.current.fetchToken = 7;
  win.EW.header('Overview');
  win.EW.current.fetchToken = 8;
  win.EW.current.route = '#/shared?workspace=ws&share_id=s1&type=evidence&id=e1';
  release(okBody());
  await flush();
  assert.equal(strip.innerHTML, 'OLD', 'late success writes no DOM');
  assert.equal(win.EW.current.dataset || null, null, 'late success writes no cache');
  // Dropped failure applies nothing either.
  win.fetch = async () => { throw Object.assign(new Error('late'), { droppedContext: true }); };
  win.EW.header('Overview');
  await flush();
  assert.equal(strip.innerHTML, 'OLD', 'dropped failure writes no DOM');
  assert.equal(win.EW.current.dataset || null, null, 'dropped failure writes no cache');
  // Genuine failure keeps the named unavailable feedback.
  win.fetch = async () => ({ ok: false, status: 500, text: async () => JSON.stringify({ error: 'bad' }) });
  win.EW.header('Overview');
  await flush();
  assert.equal(win.EW.current.dataset, 'Stored records only');
  assert.match(strip.innerHTML, /Stored records only/);
});

test('no-member wordmark exit shows refusal with zero member reads', async () => {
  // Revoked-guest counterexample: the stale locator alone never
  // admits ordinary routes — no overview/search reads, no member
  // chrome, lightweight refusal instead.
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  const headerEl = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#header' ? headerEl : { innerHTML: '', textContent: '' });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  let headerCalls = 0;
  win.EW.header = () => { headerCalls++; };
  win.EW.current.hosted = true;
  win.EW.current.principal = 'oidc:https://i.example:u-guest-final';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = [];
  win.EW.current.workspaceContext = null;
  const calls = [];
  win.EW.api = async (path) => { calls.push(path); throw Object.assign(new Error('denied'), { status: 403 }); };
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.ok(calls.every(c => !c.startsWith('/customer/')), 'no member reads without a marker');
  assert.equal(headerCalls, 0, 'no member chrome without a marker');
  assert.match(main.innerHTML, /Workspace membership required/);
  assert.match(headerEl.innerHTML, /EarWitness/);
  assert.doesNotMatch(headerEl.innerHTML, /Menu/);
});

test('pending acceptance keeps ordinary routes on the chooser until explicit choice', async () => {
  // Even with a marker present, a newly accepted invite gains no
  // general reads via the wordmark; the explicit choice settles it.
  const win = loadViews(canonicalSessionFetch({}, [7, 8]));
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = ['ws'];
  win.EW.current.workspaceContext = 'ctx-fresh';
  win.EW.current.inviteAccepted = { workspace: 'ws', id: 'inv-1' };
  const calls = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('no reads allowed: ' + path); };
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.equal(calls.length, 0, 'pending acceptance issues no reads on ordinary routes');
  assert.match(main.innerHTML, /Invitation accepted/);
  assert.match(main.innerHTML, /Use workspace/);
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.EW.toast = () => {};
  let went = null;
  win.EW.go = (r) => { went = r; };
  win.EW.render = () => {};
  win.EW.api = async (path) => {
    if (path === '/session/workspace') return { active_workspace: 'ws' };
    throw new Error('unexpected ' + path);
  };
  win.FormData = class { *entries() { yield ['workspace_id', 'ws']; yield ['from_entry', 'invite']; } };
  await win.EWViews.handleSubmit({
    getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace' }),
    querySelectorAll: () => [],
  });
  assert.equal(went, '#/overview', 'explicit choice exits the entry');
  assert.equal(win.EW.current.inviteAccepted, null, 'explicit choice settles the acceptance');
});

test('revoked share stays a named refusal with no member fallback', async () => {
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = '';
  win.EW.api = async () => { throw Object.assign(new Error('share is revoked'), { status: 403 }); };
  win.location.hash = '#/shared?workspace=ws&share_id=s1&type=evidence&id=ev-9';
  await win.EWViews.render();
  // L25 (Sol ui-direction-r1): the named reason in product words.
  assert.match(main.innerHTML, /This shared link was revoked/);
  assert.match(main.innerHTML, /This shared record is no longer available\./);
  assert.doesNotMatch(main.innerHTML, /Use workspace/);
});

test('unsupported share type names the limit without any reads', async () => {
  const win = loadViews();
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'sam';
  win.EW.current.activeWorkspace = '';
  const calls = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('no reads allowed: ' + path); };
  win.location.hash = '#/shared?workspace=ws&share_id=s1&type=campaign&id=3';
  await win.EWViews.render();
  assert.equal(calls.length, 0);
  // L25: a link the guest route cannot open gets the uniform refusal.
  assert.match(main.innerHTML, /This link isn’t available/);
});

test('marker discovery allows exact entry, stays strict elsewhere', () => {
  const win = loadViews();
  const E = win.EW;
  const session = { authenticated: true, active_workspace: 'ws', frontend_contract: null };
  E.current.route = '#/invite?workspace=ws&id=inv-1';
  E.requireContract(session);
  E.current.route = '#/shared?workspace=ws&share_id=s1&type=evidence&id=ev-9';
  E.requireContract(session);
  E.current.route = '#/overview';
  assert.throws(() => E.requireContract(session), /different version of the app/);
  E.current.route = '#/invite?workspace=ws&id=inv-1';
  assert.throws(() => E.requireContract({ authenticated: true, active_workspace: 'ws', frontend_contract: 'other/9' }), /different version of the app/);
});

test('owner entry links use the session workspace locator', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.current.hosted = true;
  win.EW.current.principal = 'owner';
  win.EW.current.activeWorkspace = 'ws';
  win.EW.current.workspaces = ['ws'];
  win.EW.current.workspaceContext = 'ctx-owner';
  win.EW.current.createdInvite = { workspace: 'ws', id: 'inv-9' };
  win.EW.current.createdShare = { workspace: 'ws', share_id: 'sh-7', type: 'evidence', id: 'ev-9' };
  win.EW.api = async (path) => {
    if (path === '/session') return { authenticated: true, principal: 'owner', active_workspace: 'ws', workspaces: ['ws'], frontend_contract: 'earwitness/customer-ui/2' };
    if (path === '/billing/status') return null;
    return { members: [], shares: [] };
  };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /#\/invite\?workspace=ws&amp;id=inv-9/);
  assert.match(main.innerHTML, /#\/shared\?workspace=ws&amp;share_id=sh-7&amp;type=evidence&amp;id=ev-9/);
});

test('shows directory qualifies duplicate titles, keeps titles ids and grid', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async () => ({ shows: [
    { id: 'cs-3a8532d96b80', display_title: 'Same Title', episodes: 1 },
    { id: 'cs-4155f31d4b19', display_title: 'Same Title', episodes: 1 },
    { id: 'cs-9', display_title: 'Morning brew', episodes: 3 } ] });
  win.location.hash = '#/shows';
  await win.EWViews.render();
  // Both cards keep title, exact href and grid placement.
  assert.equal(main.innerHTML.split('Same Title').length - 1, 2);
  assert.match(main.innerHTML, /#\/show\/cs-3a8532d96b80/);
  assert.match(main.innerHTML, /#\/show\/cs-4155f31d4b19/);
  assert.match(main.innerHTML, /Same Title<\/a><\/h2><span class="lib-card-sub">cs-3a853…<\/span>/);
  assert.match(main.innerHTML, /Same Title<\/a><\/h2><span class="lib-card-sub">cs-4155f…<\/span>/);
  // Unique titles gain no qualifier.
  assert.doesNotMatch(main.innerHTML, /Morning brew[^]*cs-/);
  assert.match(main.innerHTML, /Morning brew<\/a><\/h2><\/div><\/div><dl class="ew-facts"><dt>Episodes<\/dt><dd>3<\/dd>/);
});

test('metadata-only episode claims no transcript availability or pin', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async () => ({ record: { source_ref: 'sr1:3757aa4a116b43d3b0873d9fecc1c5b8', ref: 'ep:e3',
    episode: { transcript_version_id: null, ep_title: 'No date yet', show_title: 'Same Title', pub_date: '' },
    media: {}, window: { segments: [], text_complete: true, next_cursor: null }, relationship: 'current' } });
  win.location.hash = '#/episode/e3';
  await win.EWViews.render();
  assert.doesNotMatch(main.innerHTML, /data-transcript="available"/);
  assert.match(main.innerHTML, /data-transcript="unknown"/);
  assert.doesNotMatch(main.innerHTML, /Complete interval text/);
  assert.doesNotMatch(main.innerHTML, /Displayed transcript pin/);
  assert.doesNotMatch(main.innerHTML, /0:00–0:00/);
  assert.match(main.innerHTML, /Transcript unknown/);
  assert.match(main.innerHTML, /Transcript version unknown/);
  assert.match(main.innerHTML, /publication record, not a transcript/);
  assert.match(main.innerHTML, /data-ref=""/);
});

// Follow-up M: the episode page reads the record by `ep:<id>`, but the
// media route accepts only SourceRef1 (`sr1:`) refs, so a local-audio
// player built from the requested ref 400s. Fixture shape is the real
// record_window answer for a local-audio episode.
async function renderEpisodeWith(record, id) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async () => ({ record });
  win.location.hash = '#/episode/' + id;
  await win.EWViews.render();
  return main.innerHTML;
}

test('episode page local audio player uses the sr1 media ref, never ep:', async () => {
  const SR = 'sr1:562f6108c5214cd28f771d1d0a6aaaaf';
  const html = await renderEpisodeWith({
    ref: 'ep:upload:pilot-acquired', source_ref: SR, canonical_source_ref: SR,
    record_kind: 'source_publication', relationship: 'current',
    episode: { episode_id: 'upload:pilot-acquired', id: 'upload:pilot-acquired', transcript_version_id: 7, ep_title: 'E', show_title: 'S', pub_date: '2026-01-01' },
    selected_bounds: { start_s: 30, end_s: 60 }, effective_bounds: { start_s: 30, end_s: 60 },
    media: { playback: 'local', alignment: 'verified', timing: 'verified', trust: 'exact', source_offset_s: 0,
      src: '/api/v1/customer/records/' + encodeURIComponent(SR) + '/media' },
    window: { segments: [{ start: 30, end: 60, text: 'body' }], text_complete: true, next_cursor: null } }, 'upload:pilot-acquired');
  const src = /<audio[^>]* src="([^"#]+)/.exec(html);
  assert.ok(src, 'local audio renders a player');
  assert.equal(src[1], '/api/v1/customer/records/' + encodeURIComponent(SR) + '/media');
  assert.doesNotMatch(html, /records\/ep%3A/);
  assert.match(html, /#t=27\.0/);
});

test('episode page without local audio still renders its caveat and no player', async () => {
  const SR = 'sr1:562f6108c5214cd28f771d1d0a6aaaaf';
  const base = { ref: 'ep:e9', source_ref: SR, canonical_source_ref: SR, relationship: 'current',
    episode: { transcript_version_id: 7, ep_title: 'E', show_title: 'S', pub_date: '2026-01-01' },
    window: { segments: [{ start: 0, end: 5, text: 'body' }], text_complete: true, next_cursor: null } };
  const remote = await renderEpisodeWith({ ...base, media: { playback: 'remote', alignment: 'unknown', timing: 'unknown' } }, 'e9');
  assert.match(remote, /Remote recording only/);
  assert.doesNotMatch(remote, /<audio/);
  const rotated = await renderEpisodeWith({ ...base, media: { playback: 'remote', trust: 'rotated' } }, 'e9');
  assert.match(rotated, /Rotated audio feed/);
  assert.doesNotMatch(rotated, /<audio/);
});

test('retained empty transcript stays distinct from unknown', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async () => ({ record: { source_ref: 'sr1:abc', ref: 'ep:e4',
    episode: { transcript_version_id: 5, ep_title: 'E4', show_title: 'S', pub_date: '' },
    media: {}, window: { segments: [], text_complete: true, next_cursor: null }, relationship: 'current' } });
  win.location.hash = '#/episode/e4';
  await win.EWViews.render();
  assert.match(main.innerHTML, /data-transcript="available"/);
  assert.match(main.innerHTML, /Transcript has no text/);
  assert.doesNotMatch(main.innerHTML, /Transcript unknown/);
});

test('unreadable saved queries show repair, never an unrestricted fallback', () => {
  const S = loadAt('#/saved').EWSaved;
  const badMode = S.queryState({ query: { mode: 'bogus' }, selector: null });
  assert.equal(badMode.ok, false);
  assert.match(badMode.reason, /unreadable/);
  const badSelector = S.queryState({ query: { version: 2, mode: 'ads' }, selector: 'garbage' });
  assert.equal(badSelector.ok, false);
  const good = S.queryState({ query: { version: 2, mode: 'ads', q: '' }, selector: null });
  assert.equal(good.ok, true);
  assert.match(good.route, /^#\/search\?/);
});

test('pre-boot navigation holds dispatch until session adoption, then renders latest route', async () => {
  // Mirror of the observed bootstrap race: immediate Saved navigation
  // while the initial session boot is still in flight. No customer
  // read may dispatch before the boot session (CSRF carrier) settles;
  // the latest route renders once, after adoption.
  let releaseSession;
  const sessionGate = new Promise(res => { releaseSession = res; });
  const sessionFetches = [];
  const fetchStub = async (url) => {
    if (url === '/api/v1/session') {
      sessionFetches.push(url);
      await sessionGate;
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-local', frontend_contract: 'earwitness/customer-ui/2' }) };
    }
    throw new Error('no network in unit test');
  };
  const win = loadViews(fetchStub);
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const reads = [];
  win.EW.api = async (path) => {
    reads.push(path);
    if (path.startsWith('/customer/mistakes')) return { notes: [], total: 0, total_relation: 'exact', next_cursor: null };
    throw new Error('unexpected read ' + path);
  };
  win.location.hash = '#/saved?tab=notes';
  const pending = win.EWViews.render();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.equal(reads.length, 0, 'no customer read dispatches before boot adoption');
  assert.equal(win.EW.current.csrf, '', 'no CSRF before boot adoption');
  assert.deepEqual(sessionFetches, ['/api/v1/session'], 'exactly one boot session read, no second boot');
  releaseSession();
  await pending;
  for (let i = 0; i < 200 && !/No standalone notes yet/.test(main.innerHTML); i++) await Promise.resolve();
  assert.ok(reads.some(p => p.startsWith('/customer/mistakes')), 'latest route renders after boot');
  assert.equal(win.EW.current.csrf.length, 32, 'CSRF adopted from the boot session');
  assert.match(main.innerHTML, /No standalone notes yet/);
});

test('B1 fenced switch holds workspace mutations before dispatch', async () => {
  const calls = [];
  const win = loadAt('#/overview', { fetch: async (url, opts = {}) => { calls.push([url, opts.method || 'GET']); throw new Error('no network'); } });
  win.EW.current.writeFence = { to: 'B' };
  await assert.rejects(win.EW.api('/customer/alerts', { method: 'POST', body: {} }), /could not be confirmed/);
  await assert.rejects(win.EW.api('/customer/x', { method: 'DELETE' }), /could not be confirmed/);
  assert.equal(calls.length, 0, 'fenced mutations never reach fetch');
  await assert.rejects(win.EW.api('/session'), /no network/);
  await assert.rejects(win.EW.api('/session/workspace', { method: 'POST', body: {} }), /no network/);
  await assert.rejects(win.EW.api('/session/login', { method: 'POST', body: {} }), /no network/);
  await assert.rejects(win.EW.api('/session/logout', { method: 'POST', body: {} }), /no network/);
  assert.deepEqual(calls.map(c => c[0]),
    ['/api/v1/session', '/api/v1/session/workspace', '/api/v1/session/login', '/api/v1/session/logout']);
});

test('B2 incompatible session marker is refused before adoption', async () => {
  const win = loadAt('#/overview', { fetch: async () => ({ ok: true, status: 200,
    text: async () => JSON.stringify({ ok: true, csrf_token: 'z', workspace_context: 'ctx-bad', frontend_contract: 'other/9' }) }) });
  await assert.rejects(win.EW.api('/session'), /different version of the app/);
  assert.equal(win.EW.current.workspaceContext || '', '', 'refused marker adopts no context');
  assert.equal(win.EW.current.csrf, '', 'refused marker sets no token');
});

test('B3 canonical audio survives URL emission and Saved reopen', () => {
  const E = EW('#/search');
  const base = { version: 2, mode: 'ads', q: '', brands: null, shows: null, categories: null,
    statuses: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null };
  const round = (audio) => {
    const url = E.searchURL({ ...base, audio });
    E.current.route = url;
    return E.searchCriteria().audio;
  };
  assert.match(E.searchURL({ ...base, audio: ['mismatch'] }), /audio=mismatch/);
  assert.deepEqual(plain(round(['mismatch'])), ['mismatch']);
  assert.deepEqual(plain(round([])), [], 'exact-empty stays distinct from unrestricted');
  assert.deepEqual(plain(round(['available', 'unavailable'])), ['available', 'unavailable']);
  assert.equal(round(null), null);
  const st = loadAt('#/saved').EWSaved.queryState({ query: { version: 2, mode: 'ads', q: '', audio: ['mismatch'] }, selector: null });
  assert.equal(st.ok, true);
  assert.match(st.route, /audio=mismatch/);
});

test('B4 name-only edit keeps the live cell clause; touched membership drops it', () => {
  const V = loadViews().EWViews;
  const sel = { version: 1, scope_mode: 'counted_cell', base_query: { version: 2, mode: 'ads' },
    clause: { kind: 'chart_interval' }, count_units: 'distinct_results',
    view_revision: 'rev-old', workspace_context: 'ctx-old' };
  const base = { version: 2, mode: 'ads', q: '', brands: null, shows: null, categories: null,
    statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', ids: null };
  const kept = V.alertScopeFromDraft({}, JSON.stringify(base), JSON.stringify(sel), '[]');
  assert.equal(kept.selector.scope_mode, 'live_definition', 'tracker saving needs live authority');
  assert.deepEqual(plain(kept.selector.base_query), sel.base_query);
  assert.deepEqual(plain(kept.selector.clause), sel.clause);
  assert.equal(kept.selector.count_units, 'distinct_results');
  assert.equal(kept.selector.view_revision, null, 'counted revision intentionally not carried');
  assert.equal(kept.selector.workspace_context, null, 'counted context intentionally not carried');
  const dropped = V.alertScopeFromDraft({ brand: 'Lumen' }, JSON.stringify(base), JSON.stringify(sel), '["brand"]');
  assert.equal(dropped.selector, null);
  assert.deepEqual(plain(dropped.query.brands), ['Lumen']);
});

test('B6 direct checkbox clears its own Any/Clear draft flags', () => {
  const win = loadViews();
  const legend = { textContent: 'Brands · Any' };
  const fieldset = { dataset: { draftAny: '1', draftEmpty: '' },
    querySelector: (sel) => (sel === 'legend' ? legend : null),
    querySelectorAll: () => [{}] };
  const other = { dataset: { draftAny: '', draftEmpty: '1' } };
  const target = { type: 'checkbox', value: 'lumen',
    closest: (sel) => (sel === 'fieldset[data-group]' ? fieldset : null) };
  win.EWViews.onChange({ target });
  assert.equal(fieldset.dataset.draftAny, '', 'direct edit supersedes prior Any');
  assert.equal(fieldset.dataset.draftEmpty, '', 'direct edit supersedes prior Clear');
  assert.match(legend.textContent, /1 selected/, 'legend recounts checked boxes');
  assert.equal(other.dataset.draftEmpty, '1', 'other groups keep draft state');
});

test('B5 unreadable selector propagates instead of broadening', () => {
  const win = loadViews();
  win.EW.current.route = '#/search?mode=ads&selector=%7Bnope';
  assert.throws(() => win.EWViews.mergeCriteria({ page: 1 }), /unreadable/);
  assert.throws(() => win.EWViews.cellRoute({ count: 3 }), /no stored record list; its exact records are unavailable/);
  assert.throws(() => win.EWViews.cellRoute({ selector: { version: 1 } }), /unreadable/);
  const sel = { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'chart_interval' } };
  assert.match(win.EWViews.cellRoute({ selector: sel }), /selector=/);
});

test('B7 superseded target read opens no editor; ordinary failure keeps the empty field', async () => {
  const opener = async (api) => {
    const win = loadViews();
    let opened = false;
    win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute: () => {},
      open: false, showModal() { opened = true; }, close() {}, querySelector: () => ({ focus() {} }) });
    win.setTimeout = (fn) => { fn(); return 0; };
    win.clearTimeout = () => {};
    win.EW.api = api;
    win.EW.current.route = '#/campaign/2/target/t1';
    win.EW.current.workspaceContext = 'B';
    await win.EWViews.handleAction({ dataset: { action: 'edit-target-note', id: 't1', rev: '3' }, disabled: false, closest: () => null });
    return opened;
  };
  assert.equal(await opener(async () => { throw Object.assign(new Error('late'), { droppedContext: true }); }),
    false, 'dropped read builds no form');
  assert.equal(await opener(async () => { throw new Error('gone'); }), true, 'ordinary failure still opens the editor');
});

test('Sol1 route transition closes the route-owned dialog', async () => {
  const win = loadViews();
  let closed = 0;
  const modal = { innerHTML: '', textContent: '', setAttribute() {}, open: false,
    showModal() { this.open = true; }, close() { this.open = false; closed++; },
    querySelector() { return { focus() {} }; } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', textContent: '' });
  win.setTimeout = (fn) => { fn(); return 0; };
  win.clearTimeout = () => {};
  win.EW.header = () => {};
  win.EW.current.route = '#/campaign/3/placement/exp%3Aa';
  win.EW.openDialog('Move source binding', '', '', '');
  assert.equal(modal.open, true);
  assert.equal(win.EW.current.dialogRoute, '#/campaign/3/placement/exp%3Aa');
  win.EW.api = async () => { throw new Error('no reads in this transition test'); };
  win.location.hash = '#/search?mode=ads';
  await win.EWViews.render();
  assert.equal(closed, 1, 'stale dialog closes on transition');
  assert.equal(modal.open, false);
});

test('Sol2 failed refresh keeps the last successful frame as retained basis', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const rec = { ref: 'sr1:kept', record_kind: 'evidence', status: 'finding', ep_title: 'Kept episode',
    show_title: 'Kept show', pub_date: '2026-01-01', text: 'retained passage text', audio: 'available', audio_local: false };
  const bundle = { query: { version: 2, mode: 'ads', q: 'kept', sort: 'newest', page: 1 }, revision: 'rev-1',
    result: { items: [rec], total: 1, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  let fail = false;
  win.EW.api = async (path) => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') {
      if (fail) throw new Error('Failed to fetch');
      return bundle;
    }
    throw new Error('unexpected ' + path);
  };
  win.location.hash = '#/search?mode=ads&q=kept';
  await win.EWViews.render();
  assert.match(main.innerHTML, /retained passage text/);
  assert.ok(win.EW.current.lastSearch && win.EW.current.lastSearch.route === '#/search?mode=ads&q=kept');
  fail = true;
  await win.EWViews.render();
  assert.match(main.innerHTML, /Failed to fetch/);
  assert.match(main.innerHTML, /Showing the earlier results for this scope below; they are not new results\./);
  assert.match(main.innerHTML, /retained passage text/, 'prior results stay visible on failure');
});

test('notes tab renders the admitted server total, not the page size', async () => {
  const note = { id: 'n1', reason: 'wrong brand spoken', delivery_context: 'local_only', ref: 'sr1:x' };
  const api = async (path) => {
    assert.match(path, /\/customer\/mistakes/);
    return { notes: [note], total: 3, total_relation: 'exact', collection_revision: 7, next_cursor: null };
  };
  const win = loadAt('#/saved?tab=notes');
  const html = await win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link });
  // The count lives in the Notes tab (design v3); an exact relation needs no word.
  assert.match(html, /Standalone notes <span class="ew-tab-count">3<\/span>/);
  assert.doesNotMatch(html, /Notes <span class="ew-tab-count">1<\/span>/);
  // A non-exact total is never shown as exact; no relation, no count.
  const relApi = (rel) => async () => ({ notes: [note], total: 3, ...(rel ? { total_relation: rel } : {}), next_cursor: null });
  const lower = await win.EWSaved.render({ api: relApi('lower_bound'), esc: win.EW.esc, link: win.EW.link });
  assert.match(lower, /Standalone notes <span class="ew-tab-count"><span aria-hidden="true">3\+<\/span><span class="visually-hidden">at least 3<\/span><\/span>/);
  assert.doesNotMatch(lower, /Notes <span class="ew-tab-count">3<\/span>/);
  const none = await win.EWSaved.render({ api: relApi(null), esc: win.EW.esc, link: win.EW.link });
  assert.match(none, />Standalone notes<\/a>/, 'no relation: no count');
});


test('B1 boot adopting a changed workspace context reconciles stale origins', async () => {
  const fetch = async () => ({ ok: true, text: async () => JSON.stringify({ ok: true, csrf_token: 'c',
    workspace_context: 'ctx-B', frontend_contract: 'earwitness/customer-ui/2' }) });
  const win = loadAt('#/overview', { fetch });
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.staleDraft = { formId: 'alert', values: {}, rev: '1' };
  win.EW.current.lastSearch = { route: '#/search', html: 'old frame' };
  win.EW.current.displayedDraft = { cid: '1' };
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceContext, 'ctx-B', 'canonical session is adopted');
  assert.equal(win.EW.current.staleDraft, null, 'old-workspace draft reconciled away');
  assert.equal(win.EW.current.lastSearch, null, 'old-workspace cache reconciled away');
  assert.equal(win.EW.current.displayedDraft, null, 'old-workspace draft reconciled away');
});

test('B1 boot adopting the same context keeps settled origins', async () => {
  const fetch = async () => ({ ok: true, text: async () => JSON.stringify({ ok: true, csrf_token: 'c',
    workspace_context: 'ctx-A', frontend_contract: 'earwitness/customer-ui/2' }) });
  const win = loadAt('#/overview', { fetch });
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.staleDraft = { formId: 'alert', values: {}, rev: '1' };
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceContext, 'ctx-A');
  assert.deepEqual(plain(win.EW.current.staleDraft), { formId: 'alert', values: {}, rev: '1' });
});

test('B1 switch-save refuses a held draft from another workspace', async () => {
  const win = loadViews();
  const submitted = [];
  let toasted = '';
  win.EW.toast = (m) => { toasted = m; };
  win.EW.closeDialog = () => {};
  const dirty = { isConnected: true, requestSubmit: () => submitted.push(1) };
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.dirtyWorkspace = 'ctx-A';
  win.EW.current.dirtyForm = dirty;
  await win.EWViews.handleAction({ dataset: { action: 'switch-save' } });
  assert.deepEqual(submitted, [1], 'same-origin draft replays');
  win.EW.current.workspaceContext = 'ctx-B';
  win.EW.current.dirtyWorkspace = 'ctx-A';
  win.EW.current.dirtyForm = dirty;
  await win.EWViews.handleAction({ dataset: { action: 'switch-save' } });
  assert.deepEqual(submitted, [1], 'other-origin draft never replays into the new workspace');
  assert.match(toasted, /origin workspace/);
  assert.equal(win.EW.current.dirtyForm, null);
});

test('B1 doSave paints nothing for a superseded save response', async () => {
  const win = loadViews();
  let errors = 0;
  win.EW.render = () => {};
  win.EW.saveNotice = () => {};
  win.EW.toast = () => {};
  win.EW.formError = () => { errors++; return false; };
  win.EW.api = async () => { throw Object.assign(
    new Error('The workspace changed while this read was in flight; its response was dropped.'),
    { status: 0, droppedContext: true }); };
  win.FormData = class { *entries() { yield ['ref', 'sr1:x']; yield ['reason', 'Needs review']; yield ['status', 'open']; } };
  const submit = { disabled: false, textContent: 'Save' };
  const form = {
    getAttribute: () => 'mistake',
    querySelector: sel => (sel === '[type=submit]' ? submit : sel === '#form-errors' ? { innerHTML: '' } : null),
    querySelectorAll: () => [],
    closest: () => null,
  };
  await win.EWViews.handleSubmit(form);
  assert.equal(errors, 0, 'dropped save paints no form error on the new workspace');
  assert.equal(submit.disabled, false, 'submit re-enables');
});

test('B1 switch reconcile refuses a contract mismatch and holds the fence', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  let sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-b' });
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1
        ? body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-a', workspace_switch_revision: 4,
            frontend_contract: 'earwitness/customer-ui/2' })
        : body({ ok: true, authenticated: false, csrf_token: 'd'.repeat(32),
            workspace_context: 'ctx-b', frontend_contract: 'other/9' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.workspaceContext = 'ctx-a';
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.match(String(win.EW.current.bootRefused), /different version of the app/, 'refusal recorded for the render gate');
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-b', 'fence keeps holding workspace writes');
  assert.ok(toasts.some(m => /^Switched to ws-b\. This page needs a different version of the app\. Reload after updating\./.test(m)),
    'committed-then-refused switch reported truthfully');
});

test('B2 switch contract rejection invalidates admission; later navigation issues no reads', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  let sessions = 0;
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-contract-proof', frontend_contract: null });
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1
        ? body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-a', workspace_switch_revision: 4,
            frontend_contract: 'earwitness/customer-ui/2' })
        : body({ ok: true, authenticated: false, csrf_token: 'd'.repeat(32),
            workspace_context: 'ctx-proof', frontend_contract: null });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  assert.equal(win.EW.current.bootAdmitted, true, 'initial compatible boot admits');
  win.EW.toast = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.workspaceContext = 'ctx-a';
  win.EW.current.pendingWorkspace = 'ws-contract-proof';
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(win.EW.current.bootRefused, 'switch rejection recorded');
  assert.equal(win.EW.current.bootAdmitted, false, 'switch rejection invalidates ordinary admission');
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-contract-proof', 'fence still held');
  calls.length = 0;
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.ok(calls.every(u => !u.includes('/customer/')), 'later navigation issues no general reads');
  assert.match(main.innerHTML, /different version of the app/, 'refusal re-shown instead of Opening');
});

test('B1 reconcile with an expired session holds the fence and clears retained context', async () => {
  const body = (obj, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(obj) });
  let sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-b' });
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1
        ? body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-a', workspace_switch_revision: 4,
            frontend_contract: 'earwitness/customer-ui/2' })
        : { ok: false, status: 401, text: async () => JSON.stringify({ ok: false, error: 'expired',
            signin: { url: 'https://signin' }, workspace_context: 'ctx-a' }) };
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.workspaceContext = 'ctx-a';
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-b', 'no fresh admission: fence held');
  assert.equal(win.EW.current.workspaceContext, '', 'retained context cleared on signin');
  assert.equal(win.EW.current.bootAdmitted, false, 'a signin outcome grants no admission');
  assert.ok(toasts.some(m => /could not be confirmed/.test(m)), 'unconfirmed session reported truthfully');
});

test('B1 reconcile holds the fence when the session grants no fresh context', async () => {
  // Same retained context but a context-less reconcile read: retained
  // fields alone never release the fence.
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  let sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-a' });
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1
        ? body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-a', workspace_switch_revision: 5,
            frontend_contract: 'earwitness/customer-ui/2' })
        : body({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'd'.repeat(32),
            active_workspace: '', workspaces: [], workspace_switch_revision: 5,
            frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.workspaceContext = 'ctx-a';
  win.EW.current.pendingWorkspace = 'ws-a';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-a', 'no fresh context: fence held');
  assert.equal(win.EW.current.bootAdmitted, false, 'no admission without a fresh grant');
  assert.ok(toasts.some(m => /could not be confirmed/.test(m)));
});

test('B1 an older switch completion never releases a newer fence', async () => {
  let releasePost;
  const gate = new Promise(res => { releasePost = res; });
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') { await gate; return body({ ok: true, active_workspace: 'ws-b' }); }
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
      workspace_context: 'ctx-c', workspace_switch_revision: 5,
      frontend_contract: 'earwitness/customer-ui/2' });
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.workspaceContext = 'ctx-a';
  win.EW.current.workspaceSwitchRevision = 4;
  win.EW.current.pendingWorkspace = 'ws-b';
  const pending = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await flushBoot();
  // B's fence is set and its POST is outstanding: a newer overlapping
  // switch takes fence ownership while B is in flight.
  win.EW.current.switchOp = (win.EW.current.switchOp || 0) + 1;
  const newerOp = win.EW.current.switchOp;
  win.EW.current.writeFence = { to: 'ws-c', op: newerOp };
  win.EW.current.pendingWorkspace = 'ws-c';
  releasePost();
  await pending;
  assert.deepEqual(plain(win.EW.current.writeFence), { to: 'ws-c', op: newerOp }, 'newer fence intact');
  assert.deepEqual(toasts, [], 'older completion stays silent');
});

test('B2 incompatible switch POST invalidates admission before escaping', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const good = { ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
    active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-proof'], workspace_context: 'ctx-a',
    workspace_switch_revision: 6, frontend_contract: 'earwitness/customer-ui/2' };
  const badPost = { ok: true, authenticated: true, principal: 'sam', csrf_token: 'd'.repeat(32),
    active_workspace: 'ws-proof', workspaces: ['ws-a', 'ws-proof'], workspace_context: 'ctx-proof',
    frontend_contract: null };
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session/workspace') return body(badPost);
    if (url === '/api/v1/session') return body(good);
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  assert.equal(win.EW.current.bootAdmitted, true, 'initial compatible boot admits');
  win.EW.toast = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-proof';
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.bootAdmitted, false, 'POST refusal invalidates before escaping');
  assert.match(String(win.EW.current.bootRefused), /different version of the app/);
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-proof', 'fence held');
  calls.length = 0;
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.ok(calls.every(u => !u.includes('/customer/')), 'later navigation issues no general reads');
  assert.match(main.innerHTML, /different version of the app/, 'refusal re-shown');
});

test('B2 failed initial boot stays closed over later navigation', async () => {
  const win = loadViews();
  await flushBoot();
  assert.ok(win.EW.current.bootRefused, 'transport failure recorded');
  assert.equal(win.EW.current.bootAdmitted || false, false, 'no admission without success');
  const calls = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('must not dispatch'); };
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.equal(calls.length, 0, 'no reads without admission');
  assert.match(main.innerHTML, /unreachable/);
});

test('B7 campaign editor stops when the workspace moves during dependent reads', async () => {
  let releaseBriefs;
  const briefGate = new Promise(res => { releaseBriefs = res; });
  let versionCalls = 0;
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
      workspace_context: 'ctx-A', frontend_contract: 'earwitness/customer-ui/2' });
    if (url.includes('/customer/campaigns/c1') && !url.includes('/draft')) return body({ campaign: { id: 'c1',
      revision: 2, mode: 'supplied_requirements',
      requirement_provenance: { brief_id: 'b1', version: 1, source_label: 'S', digest: 'd' } } });
    if (url.includes('/customer/briefs?q=')) { await briefGate; return body({ items: [] }); }
    if (url.includes('/versions/')) { versionCalls++; return body({ brief_version: {} }); }
    return body({});
  };
  const win = loadViews(fetch);
  await flushBoot();
  let opened = 0;
  win.EW.openDialog = () => { opened++; };
  win.EW.toast = () => {};
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.route = '#/campaign/c1';
  const pending = win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: 'c1' } });
  await flushBoot();
  win.EW.current.workspaceContext = 'ctx-B';
  releaseBriefs();
  await pending;
  assert.equal(opened, 0, 'no late dialog for the old workspace');
  assert.equal(versionCalls, 0, 'no further requests after the move');
});

test('B7 campaign editor stops when the route moves during dependent reads', async () => {
  let releaseBriefs;
  const briefGate = new Promise(res => { releaseBriefs = res; });
  let opened = 0;
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
      workspace_context: 'ctx-A', frontend_contract: 'earwitness/customer-ui/2' });
    if (url.includes('/customer/campaigns/c1') && !url.includes('/draft')) return body({ campaign: { id: 'c1',
      revision: 2, mode: 'supplied_requirements',
      requirement_provenance: { brief_id: 'b1', version: 1, source_label: 'S', digest: 'd' } } });
    if (url.includes('/customer/briefs?q=')) { await briefGate; return body({ items: [] }); }
    return body({});
  };
  const win = loadViews(fetch);
  await flushBoot();
  win.EW.openDialog = () => { opened++; };
  win.EW.toast = () => {};
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.route = '#/campaign/c1';
  const pending = win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: 'c1' } });
  await flushBoot();
  win.EW.current.route = '#/campaign/c9';
  releaseBriefs();
  await pending;
  assert.equal(opened, 0, 'no late dialog on another route');
});

test('Sol2 changed query keeps task layout and names pending versus retained basis while loading', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const rec = { ref: 'sr1:old', record_kind: 'evidence', status: 'finding', ep_title: 'Old episode',
    show_title: 'Old show', pub_date: '2026-01-01', text: 'old basis passage', audio: 'available', audio_local: false };
  const bundleFor = q => ({ query: { version: 2, mode: 'mentions', q, sort: 'newest', page: 1 }, revision: 'rev-old',
    result: { items: [rec], total: 1, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } });
  let release;
  const gate = new Promise(res => { release = res; });
  let pendingCalls = 0;
  win.EW.api = async (path, opts) => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') {
      const q = opts && opts.body && opts.body.query && opts.body.query.q;
      if (q === 'lumen cafe') { pendingCalls++; await gate; return bundleFor(q); }
      return bundleFor(q || 'lumen');
    }
    throw new Error('unexpected ' + path);
  };
  win.location.hash = '#/search?mode=mentions&q=lumen';
  await win.EWViews.render();
  assert.match(main.innerHTML, /old basis passage/);
  assert.ok(win.EW.current.lastSearch && win.EW.current.lastSearch.q === 'lumen', 'applied basis cached with its query');
  win.location.hash = '#/search?mode=mentions&q=lumen+cafe&sort=newest';
  const loading = win.EWViews.render();
  await flushBoot();
  assert.equal(pendingCalls, 1, 'pending query dispatched');
  assert.doesNotMatch(main.innerHTML, /^<div class="loading"/, 'no blank-only swap while loading');
  assert.match(main.innerHTML, /Loading results for/, 'pending query named');
  assert.match(main.innerHTML, /lumen cafe/, 'pending draft query visible');
  assert.match(main.innerHTML, /Showing the earlier results for [^<]* below; they are not new results\./, 'old frame labeled as earlier, not new results');
  assert.doesNotMatch(main.innerHTML.split('</p>')[0], /revision|retained basis/, 'no raw revision in the loading line');
  assert.match(main.innerHTML, /old basis passage/, 'prior rows stay visible while loading');
  assert.match(main.innerHTML, /Create alert/, 'task controls retained while loading');
  release();
  await loading;
  assert.match(main.innerHTML, /rev-old/, 'pending frame replaces the basis on success');
});

test('Sol2 changed query failure keeps prior cohort and never mislabels it as new', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const rec = { ref: 'sr1:one', record_kind: 'evidence', status: 'finding', ep_title: 'One episode',
    show_title: 'One show', pub_date: '2026-03-10', text: 'one-row passage', audio: 'available', audio_local: false };
  const bundle = { query: { version: 2, mode: 'mentions', q: 'lumen cafe', sort: 'newest', page: 1 }, revision: 'rev-1',
    result: { items: [rec], total: 1, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  let fail = false;
  win.EW.api = async (path) => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') {
      if (fail) throw new Error('Failed to fetch');
      return bundle;
    }
    throw new Error('unexpected ' + path);
  };
  win.location.hash = '#/search?mode=mentions&q=lumen+cafe&sort=newest';
  await win.EWViews.render();
  assert.match(main.innerHTML, /one-row passage/);
  fail = true;
  win.location.hash = '#/search?mode=mentions&q=uncommitted+replacement+query&sort=newest';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Search is unavailable: Failed to fetch/, 'pending failure reason shown');
  assert.match(main.innerHTML, /Retry/, 'explicit retry offered');
  assert.match(main.innerHTML, /one-row passage/, 'prior successful row kept, not discarded');
  assert.match(main.innerHTML, /Showing the earlier results for [^<]*lumen cafe[^<]* below; they are not a result for/, 'old frame labeled as earlier results');
  assert.doesNotMatch(main.innerHTML, /\(revision |retained basis/, 'no raw revision in the failure line');
  assert.match(main.innerHTML, /lumen cafe/, 'applied old basis named');
  assert.match(main.innerHTML, /uncommitted replacement query/, 'pending draft named, not hidden');
  assert.match(main.innerHTML, /not a result for/, 'old rows never mislabeled as new results');
});

test('B1 overlapping switch refuses the second POST before any fence or dispatch', async () => {
  let releasePost;
  const gate = new Promise(res => { releasePost = res; });
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const posts = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') {
      posts.push(JSON.parse(opts.body).workspace_id);
      await gate;
      return body({ ok: true, active_workspace: 'ws-b' });
    }
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1
        ? body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-a', workspace_switch_revision: 4,
            frontend_contract: 'earwitness/customer-ui/2' })
        : body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
            workspace_context: 'ctx-b', workspace_switch_revision: 5,
            frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const first = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await flushBoot();
  assert.deepEqual(posts, ['ws-b'], 'first switch dispatched');
  const fenceBefore = plain(win.EW.current.writeFence);
  win.EW.current.pendingWorkspace = 'ws-c';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(plain(win.EW.current.writeFence), fenceBefore, 'refused overlap changes no fence or state');
  assert.deepEqual(posts, ['ws-b'], 'refused overlap dispatches no second POST');
  assert.ok(toasts.includes('A workspace switch is still being confirmed.'), 'overlap refused truthfully');
  await assert.rejects(win.EW.api('/session/workspace', { method: 'POST', body: { workspace_id: 'ws-c' } }),
    /already in progress/, 'api backstop refuses the overlapping POST');
  assert.deepEqual(posts, ['ws-b'], 'backstop dispatches nothing');
  releasePost();
  await first;
  assert.equal(win.EW.current.writeFence, null, 'settled switch still reconciles after the overlap');
  assert.equal(win.EW.current.switchPending || false, false, 'pending flag released');
});

test('B2 refused admission blocks ordinary dispatch but passes session and exact-guest ops', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const good = { ok: true, authenticated: false, csrf_token: 'c'.repeat(32), workspace_context: 'ctx-a',
    frontend_contract: 'earwitness/customer-ui/2' };
  const bad = { ok: true, authenticated: false, csrf_token: 'd'.repeat(32), workspace_context: 'ctx-b',
    frontend_contract: 'other/9' };
  let sessions = 0;
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') { sessions++; return body(sessions === 1 ? good : bad); }
    if (url.startsWith('/api/v1/shared/')) return body({ ok: true, title: 'Shared', text: 'granted' });
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  assert.equal(win.EW.current.bootAdmitted, true, 'compatible boot admits');
  await assert.rejects(win.EW.boot(), /different version of the app/, 'refusing refresh fails');
  assert.equal(win.EW.current.bootAdmitted, false, 'refusal invalidates admission');
  calls.length = 0;
  const refused = await win.EW.api('/customer/overview').then(() => null, (err) => err);
  assert.ok(refused && refused.refusedAdmission, 'ordinary reads refuse at dispatch');
  assert.ok(calls.every(u => !u.includes('/customer/')), 'refused reads never reach fetch');
  const memWrite = await win.EW.api('/customer/alerts', { method: 'POST', body: {} }).then(() => null, (err) => err);
  assert.ok(memWrite && memWrite.refusedAdmission, 'ordinary writes refuse at dispatch');
  calls.length = 0;
  await assert.rejects(win.EW.api('/session'), /different version of the app/, 'canonical session read still dispatches');
  assert.ok(calls.some(u => u.endsWith('/session')), 'recovery read reaches fetch');
  win.EW.current.hosted = true;
  win.EW.current.route = '#/shared?workspace=ws&share_id=s1&type=evidence&id=e1';
  const shared = await win.EW.api('/shared/evidence/e1?workspace=ws&share_id=s1');
  assert.equal(shared.text, 'granted', 'exact-entry named read passes');
  const guestOrdinary = await win.EW.api('/customer/overview').then(() => null, (err) => err);
  assert.ok(guestOrdinary && guestOrdinary.refusedAdmission, 'exact route gains no general reads');
});

test('B2 mid-render refusal publishes no ordinary controls or member chrome', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const hostedGood = { ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
    active_workspace: 'ws', workspaces: ['ws'], workspace_context: 'ctx-a',
    frontend_contract: 'earwitness/customer-ui/2' };
  let sessions = 0;
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') {
      sessions++;
      return sessions === 1 ? body({ ...hostedGood }) : body({ ...hostedGood, frontend_contract: 'other/9' });
    }
    if (url === '/api/v1/customer/preferences') return body({ preferences: {} });
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  assert.equal(win.EW.current.hosted, true, 'hosted boot reaches the access refresh path');
  calls.length = 0;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /different version of the app/, 'refusal published instead of stale forms');
  assert.doesNotMatch(main.innerHTML, /Workspace display name/, 'ordinary controls never publish');
  assert.ok(calls.every(u => !u.includes('/customer/overview')), 'no member chrome read after refusal');
});

test('B7 add-target captures owner before its preliminary read', async () => {
  let releaseCamp;
  const gate = new Promise(res => { releaseCamp = res; });
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
      workspace_context: 'ctx-A', frontend_contract: 'earwitness/customer-ui/2' });
    if (url.includes('/customer/campaigns/1') && !url.includes('/draft')) {
      await gate;
      return body({ campaign: { id: '1', revision: 3 } });
    }
    if (url.includes('/customer/shows')) return body({ shows: [] });
    return body({});
  };
  const win = loadViews(fetch);
  await flushBoot();
  let opened = 0;
  win.EW.openDialog = () => { opened++; };
  win.EW.toast = () => {};
  win.EW.current.workspaceContext = 'ctx-A';
  win.EW.current.route = '#/campaign/1';
  const pending = win.EWViews.handleAction({ dataset: { action: 'add-target', id: '1' } });
  await flushBoot();
  win.EW.current.route = '#/campaign/2';
  releaseCamp();
  await pending;
  assert.equal(opened, 0, 'route move during the preliminary read opens nothing');
});

test('B1 switch POST carries the adopted initiating revision', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const bodies = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') {
      bodies.push(JSON.parse(opts.body));
      return body({ ok: true, active_workspace: 'ws-b' });
    }
    if (url === '/api/v1/session') {
      sessions++;
      return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
        workspace_context: sessions === 1 ? 'ctx-a' : 'ctx-b',
        workspace_switch_revision: sessions === 1 ? 12 : 13,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  win.EW.toast = () => {};
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(bodies, [{ workspace_id: 'ws-b', expected_workspace_switch_revision: 12 }]);
  assert.equal(win.EW.current.writeFence, null, 'authoritative advance releases');
});

test('B1 reconcile holds when the revision is unchanged', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-b' });
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
      workspace_context: 'ctx-a', workspace_switch_revision: 12,
      frontend_contract: 'earwitness/customer-ui/2' });
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-b', 'unchanged revision retains the fence');
  assert.match(String(win.EW.current.bootRefused), /could not be confirmed/);
  assert.ok(toasts.some(m => /could not be confirmed/.test(m)));
});

test('B1 recovery replays the initiating revision and reconciles the winner on 409', async () => {
  const R = 9;
  const body = (obj, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(obj) });
  const postBodies = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') {
      postBodies.push(JSON.parse(opts.body));
      if (postBodies.length === 1) throw new Error('Failed to fetch');
      return { ok: false, status: 409, text: async () => JSON.stringify({ ok: false, error: 'stale',
        workspace_switch_revision: R + 1 }) };
    }
    if (url === '/api/v1/session') {
      sessions++;
      return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
        workspace_context: sessions === 1 ? 'ctx-a' : 'ctx-b',
        workspace_switch_revision: sessions === 1 ? R : R + 1,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.to, 'ws-b', 'ambiguous outcome holds the fence');
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(postBodies.map(b => b.expected_workspace_switch_revision), [R, R],
    'recovery consumes the same initiating revision, never a rebased one');
  assert.equal(win.EW.current.writeFence, null, 'actual winner reconciled and released');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'canonical winner adopted');
  assert.ok(toasts.some(m => /^The workspace was changed in another tab or window\. This tab now shows /.test(m)), 'reconciliation reported truthfully');
});

test('B1 switch without an ordering basis refuses before dispatch', async () => {
  const win = loadViews();
  await flushBoot();
  const calls = [];
  const toasts = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('must not dispatch'); };
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-x';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(calls.length, 0, 'no selection POST without a basis');
  assert.ok(!win.EW.current.writeFence, 'no fence without a basis');
  assert.ok(toasts.some(m => /The workspace session could not be read\. Reload the workspace to continue\./.test(m)));
});

test('B1 missing/malformed revision invalidates the retained basis and refuses selection', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  let sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') {
      sessions++;
      const base = { ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
        workspace_context: 'ctx-a', frontend_contract: 'earwitness/customer-ui/2' };
      if (sessions === 1) return body({ ...base, workspace_switch_revision: 9 });
      if (sessions === 2) return body(base); // field missing
      return body({ ...base, workspace_switch_revision: 'nine' }); // malformed
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  assert.equal(win.EW.current.workspaceSwitchRevision, 9, 'valid basis adopted');
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceSwitchRevision, undefined, 'missing field invalidates the retained basis');
  const calls = [];
  const toasts = [];
  win.EW.api = async (path) => { calls.push(path); throw new Error('must not dispatch'); };
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-x';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(calls.length, 0, 'no selection POST on an invalidated basis');
  assert.ok(!win.EW.current.writeFence, 'no fence on an invalidated basis');
  assert.ok(toasts.some(m => /The workspace session could not be read\. Reload the workspace to continue\./.test(m)));
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceSwitchRevision, undefined, 'malformed field never restores a basis');
});

test('B1 a refresh adopting beyond the fence revision releases it; the next choice orders on the adopted revision', async () => {
  const R = 9;
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const postBodies = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') {
      postBodies.push(JSON.parse(opts.body));
      if (postBodies.length === 1) throw new Error('Failed to fetch');
      return { ok: false, status: 409, text: async () => JSON.stringify({ ok: false, error: 'stale',
        workspace_switch_revision: R + 1 }) };
    }
    if (url === '/api/v1/session') {
      sessions++;
      return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32),
        workspace_context: sessions <= 2 ? 'ctx-a' : 'ctx-b',
        workspace_switch_revision: sessions === 1 ? R : R + 1,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.rev, R, 'ambiguous outcome retains the initiating revision');
  // An ordinary canonical refresh adopts r+1: a revision beyond the
  // fence's initiating one orders the uncertain switch, so the fence
  // releases on this adopt path too.
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceSwitchRevision, R + 1, 'refresh adopts the advance');
  assert.equal(win.EW.current.writeFence, null, 'adoption beyond fence.rev releases the fence');
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(postBodies.map(b => b.expected_workspace_switch_revision), [R, R + 1],
    'with no uncertain switch left, the next choice orders on the adopted revision');
  void toasts;
});

test('B1 displayed form posts its displayed epoch; canonical change holds the old action', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const writes = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') {
      sessions++;
      const won = sessions >= 3;
      return body({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
        active_workspace: won ? 'ws-b' : 'ws-a', workspaces: ['ws-a', 'ws-b'],
        workspace_context: won ? 'ctx-b' : 'ctx-a',
        workspace_switch_revision: won ? 10 : 9,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    if (url === '/api/v1/customer/preferences') {
      if (opts.method === 'PUT') {
        writes.push(opts.headers && opts.headers['X-EarWitness-Workspace-Switch-Revision']);
        return body({ ok: true });
      }
      return body({ preferences: { workspace_name: 'Alpha workspace', revision: 3 } });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Alpha workspace/, 'A form published from A data');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 9, 'publication owns the initiating epoch');
  await win.EW.api('/customer/preferences', { method: 'PUT',
    body: { workspace_name: 'Alpha workspace', expected_revision: 3 } });
  assert.equal(writes.length, 1, 'displayed action dispatches');
  assert.equal(writes[0], '9', 'write carries the displayed epoch, never a newer global read');
  // A canonical refresh adopts B/r10: the displayed A basis is held, never upgraded.
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceSwitchRevision, 10, 'refresh adopts the advance');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, undefined, 'canonical change invalidates the old basis');
  const refused = await win.EW.api('/customer/preferences', { method: 'PUT',
    body: { workspace_name: 'Alpha workspace', expected_revision: 3 } })
    .then(() => null, (err) => err);
  assert.ok(refused && refused.epochRefused, 'old action held after the canonical move');
  assert.equal(writes.length, 1, 'held action never reaches the network with any epoch');
});

test('B1 stale view build never publishes old-workspace controls as the new workspace', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const writes = [];
  let sessions = 0;
  let prefsCalls = 0;
  let releasePrefs;
  const prefsGate = new Promise(res => { releasePrefs = res; });
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') {
      sessions++;
      const won = sessions >= 2;
      return body({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
        active_workspace: won ? 'ws-b' : 'ws-a', workspaces: ['ws-a', 'ws-b'],
        workspace_context: won ? 'ctx-b' : 'ctx-a',
        workspace_switch_revision: won ? 10 : 9,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    if (url === '/api/v1/customer/preferences') {
      if (opts.method === 'PUT') {
        writes.push(opts.headers && opts.headers['X-EarWitness-Workspace-Switch-Revision']);
        return body({ ok: true });
      }
      prefsCalls++;
      if (prefsCalls === 1) await prefsGate;
      return body({ preferences: { workspace_name: prefsCalls === 1 ? 'Alpha workspace' : 'Beta workspace',
        revision: 3 } });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.location.hash = '#/settings';
  const pending = win.EWViews.render();
  // The canonical refresh adopts B/r10 while the A preferences load is still in flight.
  await win.EW.boot();
  assert.equal(win.EW.current.workspaceSwitchRevision, 10, 'refresh adopts the advance mid-build');
  releasePrefs();
  await pending;
  assert.equal(prefsCalls, 2, 'stale build discarded and the actual new view refetched');
  assert.match(main.innerHTML, /Beta workspace/, 'new-workspace controls published');
  assert.doesNotMatch(main.innerHTML, /Alpha workspace/, 'old-workspace controls never published as new');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 10, 'publication owns the new epoch');
  assert.equal(writes.length, 0, 'no A values ever posted with any epoch');
});

test('B1 fenced same-target choice enters the retained-basis reconcile path, not a no-op', async () => {
  const R = 9;
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const postBodies = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') {
      postBodies.push(JSON.parse(opts.body));
      if (postBodies.length === 1) throw new Error('Failed to fetch');
      return { ok: false, status: 409, text: async () => JSON.stringify({ ok: false, error: 'stale',
        workspace_switch_revision: R + 1 }) };
    }
    if (url === '/api/v1/session') {
      sessions++;
      const won = sessions >= 2;
      return body({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
        active_workspace: won ? 'ws-b' : 'ws-a', workspaces: ['ws-a', 'ws-b'],
        workspace_context: won ? 'ctx-b' : 'ctx-a',
        workspace_switch_revision: won ? R + 1 : R,
        frontend_contract: 'earwitness/customer-ui/2' });
    }
    return body({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.rev, R, 'ambiguous outcome retains the initiating revision');
  // A fenced same-target choice, before any canonical read proved the
  // outcome, enters the retained-basis CAS path instead of a no-op.
  win.EW.current.activeWorkspace = 'ws-b';
  win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit({
    getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace' }),
    querySelectorAll: () => [],
  });
  assert.deepEqual(postBodies.map(b => b.expected_workspace_switch_revision), [R, R],
    'fenced same-target choice POSTs the retained basis, never the newer adopted one');
  assert.equal(win.EW.current.writeFence, null, 'stale same-target POST 409 reconciles the winner and releases');
  assert.ok(!toasts.some(m => /Already in that workspace/.test(m)), 'no no-op toast while uncertain');
  assert.ok(toasts.some(m => m === 'Switched to ws-b.'), 'terminal reconcile names the chosen winner');
  // Once a canonical read proved the switch and a view was published at
  // that revision, the same choice is a no-op.
  win.EW.current.displayedWorkspaceSwitchRevision = win.EW.current.workspaceSwitchRevision;
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit({
    getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace' }),
    querySelectorAll: () => [],
  });
  assert.equal(postBodies.length, 2, 'no POST without uncertainty');
  assert.ok(toasts.includes('Already in that workspace.'));
});

/* -- Fence scope, pinned click basis, server-moved reconcile, refusal exits -- */
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flushBoot(); };
const hostedSession = (over = {}) => ({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
  active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-b', 'ws-proof'], workspace_context: 'ctx-a',
  workspace_switch_revision: 5, frontend_contract: 'earwitness/customer-ui/2', ...over });
const json = (obj, ok = true, status = 200) => ({ ok, status, text: async () => JSON.stringify(obj) });
const mountMain = (win) => {
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '',
    open: false, setAttribute() {}, close() {}, classList: { add() {}, remove() {}, toggle() {} } });
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  win.EW.header = () => {};
  return main;
};

test('F1 fence passes the read-only research query and exact guest ops, still holds ordinary writes', async () => {
  const calls = [];
  const win = loadAt('#/overview', { fetch: async (url, opts = {}) => {
    calls.push({ url, method: opts.method || 'GET', headers: opts.headers || {} });
    return json({ ok: true });
  } });
  win.EW.current.hosted = true;
  win.EW.current.displayedWorkspaceSwitchRevision = 5;
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  await win.EW.api('/customer/research-query', { method: 'POST', body: { query: { version: 2 } } });
  assert.equal(calls.length, 1, 'research query dispatches under the fence');
  assert.equal(calls[0].headers['X-EarWitness-Workspace-Switch-Revision'], undefined, 'research query carries no epoch');
  const held = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, (err) => err);
  assert.ok(held && held.writeFenced, 'ordinary PUT refused under the fence');
  assert.equal(calls.length, 1, 'refused PUT never reaches fetch');
  win.EW.current.route = '#/invite?workspace=ws-b&id=inv-1';
  await win.EW.api('/workspaces/ws-b/invites/inv-1/accept', { method: 'POST', body: {} });
  assert.equal(calls[1].url, '/api/v1/workspaces/ws-b/invites/inv-1/accept', 'exact invite accept dispatches under the fence');
  assert.equal(calls[1].headers['X-EarWitness-Workspace-Switch-Revision'], undefined, 'exact guest op carries no epoch');
  win.EW.current.route = '#/overview';
  const offEntry = await win.EW.api('/workspaces/ws-b/invites/inv-1/accept', { method: 'POST', body: {} }).then(() => null, (err) => err);
  assert.ok(offEntry && offEntry.writeFenced, 'accept outside the exact entry route stays fenced');
  assert.equal(calls.length, 2);
});

test('F2 mark-read posts the click-time basis even when a publication lands during its read', async () => {
  let releaseRead;
  const readGate = new Promise(res => { releaseRead = res; });
  const writes = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_switch_revision: 9 }));
    if (url === '/api/v1/customer/alerts/7?summaries=0') {
      await readGate;
      return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
    }
    if (url === '/api/v1/customer/alerts/7/read') {
      writes.push(opts.headers['X-EarWitness-Workspace-Switch-Revision']);
      return json({ ok: true });
    }
    return json({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  win.EW.render = () => {};
  win.EW.current.displayedWorkspaceSwitchRevision = 9;
  const pending = win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } });
  await flushBoot();
  // Same-context revision advance published while the GET is in flight.
  win.EW.current.workspaceSwitchRevision = 10;
  win.EW.current.displayedWorkspaceSwitchRevision = 10;
  releaseRead();
  await pending;
  assert.deepEqual(writes, ['9'], 'POST carries the click-time basis, never the newer publication');
  // A pinned missing basis refuses exactly like the dispatch-time read.
  const refused = await win.EW.api('/customer/alerts/7/read', { method: 'POST', body: {}, displayedRevision: undefined })
    .then(() => null, (err) => err);
  assert.ok(refused && refused.epochRefused, 'pinned invalid basis refuses before fetch');
  assert.equal(writes.length, 1);
});

test('F3 lost switch reply: dropped reads reconcile once via the canonical session and recover', async () => {
  let committed = false;
  const calls = [];
  const fetch = async (url, opts = {}) => {
    calls.push(url);
    if (url === '/api/v1/session/workspace') {
      committed = true; // the server commits, then the reply is lost
      throw new Error('Failed to fetch');
    }
    if (url === '/api/v1/session') {
      return json(committed
        ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })
        : hostedSession());
    }
    if (url === '/api/v1/customer/preferences') {
      return json({ preferences: { workspace_name: committed ? 'Beta workspace' : 'Alpha workspace', revision: 1 },
        workspace_context: committed ? 'ctx-b' : 'ctx-a' });
    }
    return json({ ok: true, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const painted = [];
  Object.defineProperty(main, 'innerHTML', { get() { return this._h || ''; },
    set(v) { this._h = v; painted.push({ html: v, ctx: win.EW.current.workspaceContext }); } });
  win.EW.toast = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Alpha workspace/, 'A settings published');
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll();
  const sessionReads = calls.filter(u => u === '/api/v1/session').length;
  assert.equal(win.EW.current.writeFence, null, 'canonical revision beyond fence.rev released the fence');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'canonical workspace adopted');
  assert.equal(win.EW.current.activeWorkspace, 'ws-b');
  assert.equal(win.EW.current.bootAdmitted, true);
  assert.match(main.innerHTML, /Beta workspace/, 'current route re-rendered under the canonical workspace');
  assert.doesNotMatch(main.innerHTML, /response was dropped/, 'no dead-end dropped-read error remains');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 6, 'basis re-established only by publication');
  assert.ok(painted.every(p => !/Beta workspace/.test(p.html) || p.ctx === 'ctx-b'), 'B data never painted under the A display');
  // Later navigation works without further reconciles.
  win.location.hash = '#/settings';
  await win.EWViews.render();
  await settleAll();
  assert.match(main.innerHTML, /Beta workspace/);
  assert.ok(calls.filter(u => u === '/api/v1/session').length - sessionReads <= 1, 'no reconcile loop (only the access refresh)');
});

test('F3 a server that keeps disagreeing reconciles once per render generation, never loops', async () => {
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ preferences: { workspace_name: 'X', revision: 1 }, workspace_context: 'ctx-b' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const sessions = () => calls.filter(u => u === '/api/v1/session').length;
  win.location.hash = '#/settings';
  await win.EWViews.render();
  await settleAll();
  const afterFirst = sessions();
  await win.EWViews.render();
  await settleAll();
  assert.equal(sessions() - afterFirst, 1, 'a new render re-checks exactly once');
  await settleAll(40);
  assert.equal(sessions() - afterFirst, 1, 'and never loops while idle');
  assert.equal(win.EW.current.workspaceContext, 'ctx-a', 'dropped response never adopted');
  assert.doesNotMatch(main.innerHTML, /name="workspace_name" value="X"/, 'dropped response never published');
});

test('F4 refused 200 switch settles the fence on its committed revision and offers exits', async () => {
  const posts = [];
  let sessions = 0;
  const calls = [];
  const fetch = async (url, opts = {}) => {
    calls.push(url);
    if (url === '/api/v1/session/workspace') {
      const b = JSON.parse(opts.body);
      posts.push(b);
      if (b.workspace_id === 'ws-proof') {
        return json(hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof',
          workspace_switch_revision: 6, frontend_contract: null }));
      }
      return json(hostedSession({ active_workspace: 'ws-a', workspace_context: 'ctx-a2', workspace_switch_revision: 7 }));
    }
    if (url === '/api/v1/session') {
      sessions++;
      return json(sessions === 1 ? hostedSession()
        : hostedSession({ workspace_context: 'ctx-a2', workspace_switch_revision: 7 }));
    }
    return json({ ok: true });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  let choice = 'ws-proof';
  win.FormData = class { *entries() { yield ['workspace_id', choice]; } };
  const form = { getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace', innerHTML: '', setAttribute() {}, focus() {} }),
    querySelectorAll: () => [] };
  calls.length = 0;
  await win.EWViews.handleSubmit(form);
  assert.deepEqual(posts.map(p => p.expected_workspace_switch_revision), [5]);
  assert.equal(win.EW.current.bootAdmitted, false, 'refused contract closes ordinary admission');
  assert.equal(win.EW.current.writeFence, null, 'known committed outcome releases the fence');
  assert.equal(win.EW.current.workspaceSwitchRevision, 6, 'committed revision adopted');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, undefined, 'displayed basis stays invalid');
  assert.equal(win.EW.current.activeWorkspace, 'ws-proof');
  assert.match(main.innerHTML, /different version of the app/, 'refusal shown');
  assert.match(main.innerHTML, /id="workspace-choice"/, 'refusal offers the chooser');
  assert.match(main.innerHTML, /data-action="signout"/, 'refusal offers Sign out');
  assert.ok(calls.every(u => !u.includes('/customer/')), 'no ordinary reads under the refusal');
  const read = await win.EW.api('/customer/overview').then(() => null, (err) => err);
  assert.ok(read && read.refusedAdmission, 'ordinary reads stay refused');
  const write = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, (err) => err);
  assert.ok(write && write.refusedAdmission, 'ordinary writes stay refused');
  choice = 'ws-a';
  await win.EWViews.handleSubmit(form);
  assert.deepEqual(posts.map(p => p.expected_workspace_switch_revision), [5, 6],
    'leaving orders on the committed revision, never the stale fence basis');
  assert.equal(win.EW.current.writeFence, null);
  assert.equal(win.EW.current.bootAdmitted, true, 'compatible workspace re-admits');
  assert.equal(win.EW.current.workspaceContext, 'ctx-a2');
  assert.ok(toasts.includes('Switched to ws-a.'), 'switch names the workspace');
  assert.ok(!toasts.includes('Saved.'), 'a switch never toasts Saved.');
});

test('F4 reload into a refused workspace keeps the chooser and Sign out usable', async () => {
  const posts = [];
  let sessions = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace' || url === '/api/v1/session/logout') {
      posts.push({ url, csrf: opts.headers['X-CSRF-Token'], body: JSON.parse(opts.body) });
      if (url === '/api/v1/session/logout') return json({ ok: true });
      return json(hostedSession({ workspace_context: 'ctx-a', workspace_switch_revision: 7 }));
    }
    if (url === '/api/v1/session') {
      sessions++;
      return json(sessions === 1
        ? hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof', workspace_switch_revision: 6,
            frontend_contract: 'other/9', csrf_token: 'r'.repeat(32) })
        : hostedSession({ workspace_switch_revision: 7 }));
    }
    throw new Error('no ordinary reads: ' + url);
  };
  let win = loadViews(fetch);
  await flushBoot();
  const refusal = win.document.querySelector('#main').innerHTML;
  assert.match(refusal, /different version of the app/);
  assert.match(refusal, /id="workspace-choice"/, 'boot refusal offers the chooser');
  assert.match(refusal, /data-action="signout"/, 'boot refusal offers Sign out');
  assert.equal(win.EW.current.csrf, '', 'refused session adopts no ordinary token');
  assert.equal(win.EW.current.workspaceContext || '', '', 'refused session adopts no context');
  let reloaded = false;
  win.location.reload = () => { reloaded = true; };
  await win.EWViews.handleAction({ dataset: { action: 'signout' } });
  assert.equal(posts[0].csrf, 'r'.repeat(32), 'Sign out carries the session token');
  assert.ok(reloaded);
  assert.equal(win.EW.current.sessionCsrf, '', 'sign-out clears the exit token');
  // The sign-out reloads; a fresh load of the same refused session
  // then leaves through the chooser.
  sessions = 0;
  win = loadViews(fetch);
  await flushBoot();
  mountMain(win);
  win.EW.toast = () => {};
  win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-a']; } };
  await win.EWViews.handleSubmit({ getAttribute: () => 'workspace-choice',
    querySelector: () => ({ disabled: false, textContent: 'Use workspace' }), querySelectorAll: () => [] });
  assert.equal(posts[1].url, '/api/v1/session/workspace');
  assert.equal(posts[1].body.expected_workspace_switch_revision, 6, 'chooser orders on the refused session revision');
  assert.equal(posts[1].csrf, 'r'.repeat(32), 'switch carries the session token');
  assert.equal(win.EW.current.bootAdmitted, true, 'compatible choice re-admits');
  assert.equal(win.EW.current.workspaceContext, 'ctx-a');
});

/* -- Held drafts, write conflicts, sign-out, switch copy, fenced controls -- */
const fakeForm = (id = 'settings-workspace') => {
  const submit = { disabled: false, textContent: 'Save workspace name', setAttribute() {}, focus() {} };
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  return { submit, box, dataset: { dirty: '1' }, elements: [{ type: 'text' }], getAttribute: () => id, closest: () => null,
    querySelector: sel => (sel === '#form-errors' ? box : sel === '[type=submit]' ? submit : { value: '' }),
    querySelectorAll: sel => (sel === '[type=submit]' ? [submit] : []) };
};
const hostedFixture = (handler) => {
  const state = { ws: 'a', rev: 5, calls: [] };
  const sessionFor = () => hostedSession({ active_workspace: 'ws-' + state.ws, workspace_context: 'ctx-' + state.ws,
    workspace_switch_revision: state.rev });
  const fetch = async (url, opts = {}) => {
    state.calls.push((opts.method || 'GET') + ' ' + url);
    const custom = handler && await handler(url, opts, state);
    if (custom) return custom;
    if (url === '/api/v1/session') return json(sessionFor());
    if (url === '/api/v1/customer/preferences') {
      return json({ preferences: { workspace_name: state.ws === 'a' ? 'Alpha workspace' : 'Beta workspace', revision: 1 },
        workspace_context: 'ctx-' + state.ws });
    }
    return json({ ok: true, workspace_context: 'ctx-' + state.ws });
  };
  return { state, fetch };
};
const viewsWith = async (fetch) => {
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const modal = { open: true, closed: 0, close() { this.closed++; this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#modal' ? modal : base(sel));
  const toasts = [];
  win.EW.toast = (m) => toasts.push(m);
  win.EW.saveNotice = () => {};
  return { win, main, modal, toasts };
};

test('UA another-tab change with a dirty draft adopts canonical state but keeps the draft, never re-rendering', async () => {
  const { state, fetch } = hostedFixture();
  const { win, main, modal, toasts } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Alpha workspace/);
  const draft = fakeForm();
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) && win.document._drafts ? win.document._drafts : []);
  win.document._drafts = [draft];
  modal.open = true;
  state.ws = 'b'; state.rev = 6; // another tab switched the session
  const before = main.innerHTML;
  await win.EW.api('/customer/overview').catch(() => {});
  await settleAll();
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'canonical session adopted');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, undefined, 'displayed basis invalidated');
  assert.equal(main.innerHTML, before, 'view with a draft is not re-rendered');
  assert.equal(modal.closed, 0, 'a dialog holding the draft stays open');
  assert.equal(draft.submit.disabled, true, 'draft submit disabled');
  assert.match(draft.box.innerHTML, /changed in another tab or window\. Nothing was saved; your entries are kept here\. Reload the workspace to continue\./);
  assert.match(draft.box.innerHTML, /data-action="reload-workspace"[^>]*>Reload workspace</);
  const reload = win.EWViews.handleAction({ dataset: { action: 'reload-workspace' } });
  win.document._drafts = null;
  await reload;
  assert.match(main.innerHTML, /Beta workspace/, 'explicit reload re-renders under the canonical workspace');
  assert.ok(toasts.includes('Your unsaved entry in ws-a was not applied.'), 'dropped draft named by the workspace it was typed in');
});

test('UB1 a 409 workspace conflict holds the form, re-reads the session once, never retries', async () => {
  const { state, fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      st.ws = 'b'; st.rev = 6;
      return json({ ok: false, workspace_switch_revision: 6, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'workspace switch revision changed',
        details: { current_revision: 6, workspace_switch_revision: 6 } } }, false, 409);
    }
    return null;
  });
  const { win, main } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = fakeForm();
  form.dataset = {};
  win.document.querySelectorAll = sel => (/data-held/.test(sel) && form.dataset.held === '1' ? [form] : []);
  win.FormData = class { *entries() { yield ['workspace_name', 'Alpha 2']; yield ['expected_revision', '1']; } };
  state.calls.length = 0;
  const before = main.innerHTML;
  await win.EWViews.handleSubmit(form);
  await settleAll();
  assert.deepEqual(state.calls.filter(c => c.startsWith('PUT')), ['PUT /api/v1/customer/preferences'], 'no retry, no rebase');
  assert.deepEqual(state.calls.filter(c => c === 'GET /api/v1/session'), ['GET /api/v1/session'], 'exactly one canonical re-read');
  assert.equal(form.submit.disabled, true, 'Save disabled');
  assert.doesNotMatch(form.box.innerHTML, /workspace switch revision changed/, 'raw server text never shown');
  assert.match(form.box.innerHTML, /Nothing was saved; your entries are kept here/);
  assert.match(form.box.innerHTML, /data-action="reload-workspace"/);
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, undefined, 'stale basis invalidated');
  assert.equal(main.innerHTML, before, 'the held draft is not re-rendered away');
});

test('UB2 a 401 sign-in write keeps the draft with a Sign in step that shows the sign-in state', async () => {
  let signedOut = false;
  const { state, fetch } = hostedFixture(async (url, opts) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      signedOut = true;
      return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'browser sign-in is required' } }, false, 401);
    }
    if (url === '/api/v1/session' && signedOut) return json({ ok: false, error: 'signin', signin: { available: true } }, false, 401);
    return null;
  });
  const { win, main } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = fakeForm();
  win.FormData = class { *entries() { yield ['workspace_name', 'Alpha 2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(form);
  assert.equal(form.submit.disabled, true, 'Save disabled');
  assert.match(form.box.innerHTML, /You are signed out\. Nothing was saved; your entries stay here until you choose Sign in, which clears them\./);
  assert.doesNotMatch(form.box.innerHTML, /browser sign-in is required/);
  assert.match(form.box.innerHTML, /data-action="show-signin"[^>]*>Sign in</);
  await win.EWViews.handleAction({ dataset: { action: 'show-signin' } });
  await settleAll();
  assert.match(main.innerHTML, /data-action="signin-start"/, 'sign-in state shown');
  void state;
});

test('UB3 switch busy copy says Switching workspace and success names the workspace, never Saved.', async () => {
  let release;
  const gate = new Promise(res => { release = res; });
  const { state, fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { await gate; st.ws = 'b'; st.rev = 6; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    return null;
  });
  const { win, toasts } = await viewsWith(fetch);
  const notices = [];
  win.EW.saveNotice = (m) => notices.push(m);
  win.EW.closeDialog = () => {};
  const form = fakeForm('workspace-choice');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const pending = win.EWViews.handleSubmit(form);
  await settleAll(5);
  assert.equal(form.submit.textContent, 'Switching workspace…');
  assert.ok(notices.includes('Switching workspace…') && !notices.includes('Saving…'));
  const held = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  assert.equal(held && held.message, 'This change is held while the workspace switch is confirmed. Nothing was saved.', 'UB5 pending copy');
  release();
  await pending;
  assert.ok(toasts.includes('Switched to ws-b.'));
  assert.ok(!toasts.includes('Saved.'));
  void state;
});

// final17: Settings reads ride the server's per-session read bucket
// (burst 40, 60/min); /session is the only read outside it. Two
// save+reload cycles must stay well inside it, and the page must not
// ask for the session twice in a row.
test('F17b Settings save then reload, twice: one session read per load, a save re-reads only preferences', async () => {
  const { state, fetch } = hostedFixture(async (url, opts) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') return json({ ok: true, workspace_context: 'ctx-a' });
    return null;
  });
  // A reload: boot and the first render of #/settings, as the browser does.
  const pageLoad = async () => {
    const win = loadViews(fetch, '#/settings');
    const main = mountMain(win);
    win.EW.toast = () => {};
    win.EW.saveNotice = () => {};
    await settleAll();
    return { win, main };
  };
  const phases = [];
  const phase = async (fn) => { state.calls.length = 0; const out = await fn(); phases.push(state.calls.slice()); return out; };
  const save = (win) => phase(async () => {
    win.FormData = class { *entries() { yield ['workspace_name', 'Alpha 2']; yield ['expected_revision', '1']; } };
    await win.EWViews.handleSubmit(fakeForm('settings-workspace'));
    await settleAll();
  });
  const first = await phase(pageLoad);
  assert.match(first.main.innerHTML, /id="settings-name"/);
  await save(first.win);
  const second = await phase(pageLoad);
  await save(second.win);
  await phase(pageLoad);
  const load = ['GET /api/v1/session', 'GET /api/v1/customer/preferences', 'GET /api/v1/workspaces/ws-a/members',
    'GET /api/v1/workspaces/ws-a/invites', 'GET /api/v1/shares', 'GET /api/v1/billing/status'];
  const saved = ['PUT /api/v1/customer/preferences', 'GET /api/v1/customer/preferences', 'GET /api/v1/session'];
  assert.deepEqual(phases, [load, saved, load, saved, load], 'boot session reused by its render; a save keeps the access reads');
  const limited = phases.flat().filter(c => !c.endsWith(' /api/v1/session')).length;
  // 3 loads x 5 + 2 saves x 2 = 19 (was 3 x 5 + 2 x 6 = 27, plus 3 repeated session reads).
  assert.equal(limited, 19);
});

test('F17c a switch into a workspace reads the session once before Settings shows', async () => {
  const { state, fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') {
      st.ws = 'b'; st.rev = 6;
      return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }));
    }
    return null;
  });
  const { win, main, toasts } = await viewsWith(fetch);
  win.EW.closeDialog = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  await settleAll();
  assert.match(main.innerHTML, /Alpha workspace/);
  state.calls.length = 0;
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(fakeForm('workspace-choice'));
  await settleAll();
  assert.ok(toasts.includes('Switched to ws-b.'), toasts.join(' | '));
  assert.match(main.innerHTML, /Beta workspace/, 'Settings shows the new workspace');
  const after = state.calls.slice(state.calls.indexOf('POST /api/v1/session/workspace') + 1);
  // The reconcile read is the one fresh adoption the fence needs;
  // Settings shows that same adoption instead of asking again.
  assert.deepEqual(after.filter(c => c === 'GET /api/v1/session'), ['GET /api/v1/session']);
  assert.equal(win.EW.current.writeFence, null, 'the fence released on the reconcile read');
  assert.equal(win.EW.current.workspaceSwitchRevision, 6);
});

// The real server sends the current switch revision with a 409, at the
// top level and in error.details. It is never adopted as the displayed
// basis nor used to resend the write: only a canonical re-read and a
// new publication move the basis.
test('REV 409 never rebases onto the server revision', async () => {
  for (const moves of [false, true]) {
    const { state, fetch } = hostedFixture(async (url, opts, st) => {
      if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
        if (moves) { st.ws = 'b'; st.rev = 9; }
        return json({ ok: false, workspace_switch_revision: 9, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'workspace switch revision changed',
          status: 409, details: { workspace_switch_revision: 9 } } }, false, 409);
      }
      return null;
    });
    const { win } = await viewsWith(fetch);
    win.location.hash = '#/settings';
    await win.EWViews.render();
    const shown = win.EW.current.displayedWorkspaceSwitchRevision;
    assert.equal(shown, 5);
    const form = fakeForm();
    form.dataset = {};
    win.document.querySelectorAll = sel => (/data-held/.test(sel) && form.dataset.held === '1' ? [form] : []);
    win.FormData = class { *entries() { yield ['workspace_name', 'Alpha 2']; yield ['expected_revision', '1']; } };
    state.calls.length = 0;
    const seen = [];
    const api = win.EW.api;
    win.EW.api = async (...a) => { try { return await api(...a); } finally { seen.push(win.EW.current.displayedWorkspaceSwitchRevision); } };
    await win.EWViews.handleSubmit(form);
    seen.push(win.EW.current.displayedWorkspaceSwitchRevision);
    await settleAll();
    assert.deepEqual(state.calls.filter(c => !c.startsWith('GET')), ['PUT /api/v1/customer/preferences'], 'exactly one write, never resent');
    assert.ok(seen.every(r => r !== 9), 'the 409 reply never becomes the displayed basis: ' + seen.join(','));
    assert.ok([shown, undefined].includes(seen[0]), 'right after the 409 the basis is kept or invalidated, never rebased');
    assert.equal(form.submit.disabled, true, 'the form holds; nothing retries');
    if (!moves) assert.notEqual(win.EW.current.displayedWorkspaceSwitchRevision, 9);
  }
});

test('UB4 a reconcile that lands elsewhere names the winner; UB7 discard copy names the origin', async () => {
  let posts = 0;
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') {
      posts++;
      if (posts === 1) { st.ws = 'b'; st.rev = 6; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
      st.ws = 'c'; st.rev = 8;
      return json({ ok: false, workspace_switch_revision: 8, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'stale', details: { workspace_switch_revision: 8 } } }, false, 409);
    }
    return null;
  });
  const { win, toasts } = await viewsWith(fetch);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(toasts.includes('Switched to ws-b. Unsaved changes were discarded. Nothing was saved to ws-a.'), toasts.join(' | '));
  win.EW.current.pendingWorkspace = 'ws-a';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(toasts.includes('The workspace was changed in another tab or window. This tab now shows ws-c. Unsaved changes were discarded. Nothing was saved to ws-b.'), toasts.join(' | '));
});

test('UB5/UB6 a lost switch reply reports plainly and the fence says how to confirm', async () => {
  const { fetch } = hostedFixture(async (url) => {
    if (url === '/api/v1/session/workspace') throw new TypeError('Failed to fetch');
    return null;
  });
  const { win, toasts } = await viewsWith(fetch);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(toasts.includes('The workspace switch could not be confirmed. Choose the workspace again to confirm it. Nothing was saved.'), toasts.join(' | '));
  assert.ok(!toasts.some(m => /response was lost|may have been applied/.test(m)), 'no internal lost-reply wording');
  assert.ok(!toasts.some(m => /Failed to fetch/.test(m)));
  const held = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  assert.equal(held && held.message, 'The workspace switch could not be confirmed. Choose the workspace again to confirm it. Nothing was saved.');
  const form = fakeForm('workspace-choice');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /could not be confirmed\. Choose the workspace again to confirm it\. Nothing was saved\./, 'chooser form shows the one unconfirmed-switch copy');
  assert.doesNotMatch(form.box.innerHTML, /response was lost|may have been applied|below/);
});

test('UB8 contract refusal copy points to the exits on the page', () => {
  const E = EW('#/overview');
  assert.throws(() => E.requireContract({ authenticated: true, active_workspace: 'w', frontend_contract: null }),
    (err) => err.message === 'This workspace needs a different version of the app. Choose another workspace or sign out.');
  assert.throws(() => E.requireContract({ authenticated: true, active_workspace: 'w', frontend_contract: 'x' }), (err) => !/Reload after the service update/.test(err.message));
});

test('UB9 while fenced, published workspace write controls render disabled with the reason', async () => {
  const { fetch } = hostedFixture();
  const { win, main } = await viewsWith(fetch);
  const held = [];
  let selector = '';
  let banner = '';
  main.querySelectorAll = (sel) => {
    if (/data-fence/.test(sel)) return [];
    selector = sel; const b = { disabled: false, title: '', dataset: {} }; held.push(b); return [b];
  };
  main.insertAdjacentHTML = (pos, html) => { banner = html; };
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Alpha workspace/, 'reads still publish under the fence');
  for (const f of ['settings-workspace', 'settings-display', 'invite-create', 'share-create']) assert.ok(selector.includes(`#${f} [type=submit]`), f);
  assert.ok(selector.includes('[data-action="member-remove"]'));
  assert.ok(!selector.includes('workspace-choice'), 'the chooser stays usable');
  assert.equal(held[0].disabled, true);
  assert.match(held[0].title, /could not be confirmed/);
  assert.match(banner, /could not be confirmed/, 'reason shown on the page');
  win.EW.current.writeFence = null;
  held.length = 0;
  await win.EWViews.render();
  assert.equal(held.length, 0, 'no holding without a fence');
});

/* -- Review fixes: reconcile retry/queue, refused copy, action copy, holds -- */
const fenceRoot = () => {
  // Minimal published view: workspace write controls, a chooser submit,
  // and banner insertion, enough for the fence-hold sync.
  const mk = () => ({ disabled: false, title: '', dataset: {}, closest: () => null });
  const root = { innerHTML: '', writes: [mk(), mk()], chooser: mk(), banners: [] };
  root.querySelectorAll = (sel) => {
    if (sel === '[data-fence-banner]') return [...root.banners];
    if (sel === '[data-fence-held]') return root.writes.filter(w => w.dataset.fenceHeld);
    if (/data-action="pause-alert"/.test(sel)) return root.writes;
    return [];
  };
  root.insertAdjacentHTML = (pos, html) => {
    const b = { html, remove() { root.banners = root.banners.filter(x => x !== b); } };
    root.banners.push(b);
  };
  return root;
};

test('R1a a failed reconcile read records nothing; the next render reconciles, and failures never loop', async () => {
  let moved = false, failing = 0, sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') {
      sessions++;
      if (failing > 0) { failing--; throw new TypeError('Failed to fetch'); }
      return json(moved ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    }
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: moved ? 'Beta workspace' : 'Alpha workspace', revision: 1 }, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  moved = true; failing = 1;
  const before = sessions;
  await win.EWViews.render();
  await settleAll();
  assert.equal(sessions - before, 1, 'one reconcile attempt; its failure triggers no render loop');
  assert.equal(win.EW.current.serverContextKey, undefined, 'a failed read records no key');
  assert.equal(win.EW.current.workspaceContext, 'ctx-a');
  await win.EWViews.handleAction({ dataset: { action: 'retry' } });
  await settleAll();
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'Retry reconciles again and recovers');
  assert.match(main.innerHTML, /Beta workspace/);
});

test('R1b a read dropped while the switch is pending is queued, then reconciles once the lost reply settles', async () => {
  let committed = false, release;
  const gate = new Promise(r => { release = r; });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') { committed = true; await gate; throw new TypeError('Failed to fetch'); }
    if (url === '/api/v1/session') return json(committed ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: committed ? 'Beta workspace' : 'Alpha workspace', revision: 1 }, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
    return json({ ok: true, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.closeDialog = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  win.EW.current.pendingWorkspace = 'ws-b';
  const pending = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(3);
  // A doSwitchWorkspace-style path renders nothing after the lost reply:
  // stub the discard's own trailing render so only the queue can recover.
  const realRender = win.EW.render;
  await realRender();
  await settleAll();
  assert.equal(win.EW.current.serverContextKey, undefined, 'nothing consumed while the switch is pending');
  win.EW.render = () => {};
  release();
  await pending;
  win.EW.render = realRender;
  await settleAll();
  assert.equal(win.EW.current.writeFence, null, 'queued reconcile released the fence on the advanced revision');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.match(main.innerHTML, /Beta workspace/, 'page recovers to the canonical workspace');
  assert.ok(toasts.includes('Switched to ws-b.'), 'the stale "choose again" advice is superseded');
});

test('R2 committed-then-refused switches say where the user is, on every path', async () => {
  const refusedReply = () => json(hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof', workspace_switch_revision: 6, frontend_contract: null }));
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return refusedReply();
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const refusal = 'This workspace needs a different version of the app. Choose another workspace or sign out.';
  let win = loadViews(fetch);
  await flushBoot();
  mountMain(win);
  let toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-proof';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(toasts, [`Switched to ws-proof. ${refusal} Unsaved changes were discarded. Nothing was saved to ws-a.`]);
  win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-proof']; } };
  await win.EWViews.handleSubmit(fakeForm('workspace-choice'));
  // The refusal page names where the user is; no success-sounding toast repeats it.
  assert.deepEqual(toasts, [], 'form path: no toast for a committed-but-refused switch');
  assert.match(main.innerHTML, /Current workspace: ws-proof\./, 'form path names the committed workspace on the page');
  assert.match(main.innerHTML, /data-action="signout"/);
});

test('R3 a 409 on a button action says nothing changed and the page shows the current workspace', async () => {
  let moved = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(moved ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
    if (url === '/api/v1/customer/alerts/7/read') { moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  mountMain(win);
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.location.hash = '#/overview';
  await win.EWViews.render();
  const err = await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).then(() => null, e => e);
  await settleAll();
  assert.equal(err && err.message, 'This workspace changed in another tab or window. Nothing was changed.', 'immediate copy claims only what is known');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'the page did move to the current workspace');
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed; the page now shows the current workspace.'),
    'outcome copy follows the actual re-render');
});

test('R4 Sign in from a held draft announces that the entry was not kept', async () => {
  const { fetch } = hostedFixture(async (url) => (url === '/api/v1/session'
    ? json({ ok: false, error: 'signin', signin: { available: true } }, false, 401) : null));
  const { win, toasts } = await viewsWith(fetch);
  const form = fakeForm();
  form.dataset = { held: '1' };
  win.document.querySelectorAll = sel => (/data-held/.test(sel) ? [form] : []);
  await win.EWViews.handleAction({ dataset: { action: 'show-signin' } });
  assert.ok(toasts.includes('Your unsaved entry was not kept after signing out.'));
});

test('R5 query, filter and chooser inputs never count as durable drafts', async () => {
  const win = loadAt('#/overview', { fetch: canonicalSessionFetch() });
  win.addEventListener = () => {};
  const listeners = {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.document.addEventListener = (t, f) => { (listeners[t] = listeners[t] || []).push(f); };
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const fire = (id) => {
    const form = { dataset: {}, getAttribute: () => id };
    // Other input listeners (lookups) need a fuller DOM; only markDirty's effect is asserted.
    for (const f of listeners.input || []) { try { f({ target: { closest: () => form, matches: () => false } }); } catch { /* not markDirty */ } }
    return form.dataset.dirty;
  };
  for (const id of ['overview-search', 'saved-filter', 'workspace-choice', 'compare-select', 'search', 'filters']) {
    assert.equal(fire(id), undefined, id + ' is not a draft');
  }
  assert.equal(fire('settings-workspace'), '1', 'durable forms still mark dirty');
});

test('R6 fence holds refresh without a re-render: released on a held-draft reconcile, re-held after a lost reply', async () => {
  // Lost reply on the doSwitchWorkspace path: the published view had no
  // fence; the settled fence must now show held controls and wording.
  const { fetch } = hostedFixture(async (url) => {
    if (url === '/api/v1/session/workspace') throw new TypeError('Failed to fetch');
    return null;
  });
  const { win } = await viewsWith(fetch);
  const root = fenceRoot();
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#main' ? root : base(sel));
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(fakeForm('workspace-choice'));
  assert.ok(win.EW.current.writeFence, 'lost reply keeps the fence');
  assert.ok(root.writes.every(w => w.disabled), 'write controls held after the lost reply');
  assert.match(root.writes[0].title, /could not be confirmed/, 'settled wording, not the pending one');
  assert.equal(root.banners.length, 1);
  assert.match(root.banners[0].html, /could not be confirmed/);
  // Held-draft reconcile: the fence releases with no re-render.
  win.EW.current.writeFence = { to: 'ws-b', op: win.EW.current.switchOp, rev: 5 };
  const draft = fakeForm();
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [draft] : []);
  // Server now proves the switch: revision beyond the fence.
  win.EW.boot = async () => { win.EW.current.workspaceContext = 'ctx-b'; win.EW.current.workspaceSwitchRevision = 6;
    win.EW.current.activeWorkspace = 'ws-b'; return { admitted: true, fresh: true, context: 'ctx-b' }; };
  await win.EWViews.reconcileCanonical();
  assert.equal(win.EW.current.writeFence, null, 'fence released');
  assert.ok(root.writes.every(w => !w.disabled && !w.title), 'stale holds cleared without a re-render');
  assert.equal(root.banners.length, 0, 'stale banner removed');
  assert.equal(draft.submit.disabled, true, 'the held draft itself stays held');
});

test('R7 fenced holds actually disable alert, campaign, saved-item and export controls', async () => {
  const { fetch } = hostedFixture();
  const { win, main } = await viewsWith(fetch);
  // Simulated page: one control per action; the hold selector matches
  // a control exactly when it names that action (or the inline form).
  const names = ['pause-alert', 'check-alert', 'mark-read', 'create-alert', 'create-campaign', 'edit-campaign',
    'attach-pool-evidence', 'freeze-report', 'save-search', 'delete-saved', 'edit-saved', 'export-all'];
  const controls = names.map(a => ({ key: `[data-action="${a}"]`, disabled: false, title: '', dataset: {} }))
    .concat([{ key: '#attach-evidence [type=submit]', disabled: false, title: '', dataset: {} },
      { key: '[data-action="retry"]', disabled: false, title: '', dataset: {} }]);
  main.querySelectorAll = (sel) => (/data-fence/.test(sel) ? controls.filter(c => c.dataset.fenceHeld)
    : controls.filter(c => sel.split(', ').includes(c.key)));
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  for (const c of controls.slice(0, -1)) {
    assert.equal(c.disabled, true, c.key + ' disabled');
    assert.match(c.title, /could not be confirmed/, c.key + ' gives the reason');
  }
  assert.equal(controls[controls.length - 1].disabled, false, 'non-write controls stay usable');
});

test('R8 a settling switch is never cleared or overlapped by another switch attempt', async () => {
  const { fetch } = hostedFixture(async (url) => {
    if (url === '/api/v1/session/workspace') throw new TypeError('Failed to fetch');
    return null;
  });
  const { win } = await viewsWith(fetch);
  win.EW.render = () => {};
  win.EW.closeDialog = () => {};
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.current.switchSettling = 1; // another switch is still settling
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(toasts.includes('A workspace switch is still being confirmed.'), 'the overlapping attempt is refused');
  assert.equal(win.EW.current.switchSettling, 1, 'the settling switch keeps its state');
  win.EW.current.writeFence = { to: 'ws-x', op: 9, rev: 5 }; // that switch's fence
  const held = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  assert.equal(held && held.message, 'This change is held while the workspace switch is confirmed. Nothing was saved.');
});

test('R9 a held form without its own error box still shows the reason and action', async () => {
  const { fetch } = hostedFixture(async (url, opts) => (url === '/api/v1/customer/preferences' && opts.method === 'PUT'
    ? json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409) : null));
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = fakeForm();
  let inserted = null;
  form.querySelector = sel => (sel === '[type=submit]' ? form.submit : sel === '[data-held-notice]' ? inserted : null);
  form.insertAdjacentHTML = () => { inserted = { innerHTML: '', setAttribute() {}, focus() {} }; };
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(form);
  assert.equal(form.submit.disabled, true);
  assert.ok(inserted, 'a notice box was added beside the fields');
  assert.match(inserted.innerHTML, /Nothing was saved; your entries are kept here/);
  assert.match(inserted.innerHTML, /data-action="reload-workspace"/);
});

test('R10 the refused-session exit token clears on login/logout and on a compatible session', async () => {
  let compatible = false;
  const win = loadAt('#/overview', { fetch: async (url) => (url === '/api/v1/session'
    ? json(compatible ? hostedSession({ csrf_token: 'n'.repeat(32) }) : hostedSession({ frontend_contract: 'other/9', csrf_token: 'r'.repeat(32) }))
    : json({ ok: true })) });
  await win.EW.boot().catch(() => {});
  assert.equal(win.EW.current.sessionCsrf, 'r'.repeat(32), 'refused session keeps its exit token');
  compatible = true;
  await win.EW.boot();
  assert.equal(win.EW.current.sessionCsrf, '', 'compatible session supplies csrf; exit token cleared');
  win.EW.current.sessionCsrf = 'r'.repeat(32);
  await win.EW.api('/session/login', { method: 'POST', body: {} });
  assert.equal(win.EW.current.sessionCsrf, '', 'login clears it');
  win.EW.current.sessionCsrf = 'r'.repeat(32);
  await win.EW.api('/session/logout', { method: 'POST', body: {} });
  assert.equal(win.EW.current.sessionCsrf, '', 'logout clears it');
});

/* -- Review round 3: action outcomes, sign-out path, phantom drafts, overlap -- */
const markReadFixture = (onRead, sessionFor) => async (url) => {
  if (url === '/api/v1/session') return sessionFor();
  if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
  if (url === '/api/v1/customer/alerts/7/read') return onRead();
  return json({ ok: true, workspace_context: 'ctx-a' });
};
const actionWin = async (fetch) => {
  const win = loadViews(fetch);
  await flushBoot();
  const main = mountMain(win);
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.location.hash = '#/overview';
  await win.EWViews.render();
  return { win, main, toasts };
};

test('S1 a 401 on a button action says nothing changed and moves to the sign-in state', async () => {
  let out = false;
  const { win, main } = await actionWin(markReadFixture(
    () => { out = true; return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401); },
    () => (out ? json({ ok: false, error: 'signin', signin: { available: true } }, false, 401) : json(hostedSession()))));
  const err = await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).then(() => null, e => e);
  await settleAll();
  assert.equal(err && err.message, 'You are signed out. Nothing was changed.');
  assert.match(main.innerHTML, /data-action="signin-start"/, 'the sign-in state is shown');
});

test('S1b (pass 16, DEMO2-R12-01) a 401 on a button action with a visible page draft runs the access boundary: the old page goes, no form on it is held, and the sign-in state renders', async () => {
  let out = false;
  const { win, main } = await actionWin(markReadFixture(
    () => { out = true; return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401); },
    () => (out ? json({ ok: false, error: 'signin', signin: { available: true } }, false, 401) : json(hostedSession()))));
  win.FormData = class { *entries() { yield ['workspace_name', 'typed']; } };
  const draft = fakeForm();
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [draft] : []);
  const before = main.innerHTML;
  const epoch = win.EW.authorityEpoch();
  await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
  await settleAll();
  assert.ok(win.EW.authorityEpoch() > epoch, 'the 401 started no boundary');
  assert.notEqual(main.innerHTML, before, 'the old page stayed up');
  assert.match(main.innerHTML, /data-action="signin-start"/, 'the sign-in state is shown');
  assert.equal(draft.submit.disabled, false, 'a form on the old page was held instead');
  assert.doesNotMatch(draft.box.innerHTML, /You are signed out/);
});

test('S2 a button 409 with a held draft reports the hold and puts Reload workspace on the page', async () => {
  let moved = false;
  const { win, main, toasts } = await actionWin(markReadFixture(
    () => { moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); },
    () => json(moved ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession())));
  const draft = fakeForm();
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [draft] : []);
  main.innerHTML = 'A-PAGE';
  await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
  await settleAll();
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed. Reload the workspace to continue.'));
  assert.ok(!toasts.some(m => /page now shows the current workspace/.test(m)), 'never claims a re-render that did not happen');
  assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*data-action="reload-workspace"/, 'Reload control visible in #main');
  assert.match(main.innerHTML, /A-PAGE/, 'the held view itself was not re-rendered');
});

test('S2b a button 409 whose reconcile read fails says so and offers a Retry that reconciles', async () => {
  let fail = false, moved = false;
  const { win, main, toasts } = await actionWin(async (url) => {
    if (url === '/api/v1/session') {
      if (fail) { fail = false; throw new TypeError('Failed to fetch'); }
      return json(moved ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    }
    if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
    if (url === '/api/v1/customer/alerts/7/read') { fail = true; moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  });
  main.innerHTML = 'A-PAGE';
  await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
  await settleAll();
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed. The current workspace could not be loaded; try again.'));
  assert.ok(!toasts.some(m => /page now shows the current workspace/.test(m)));
  assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*data-action="reconcile-retry"/, 'Retry visible in #main');
  await win.EWViews.handleAction({ dataset: { action: 'reconcile-retry' } });
  await settleAll();
  assert.equal(win.EW.current.workspaceContext, 'ctx-b', 'Retry re-ran the reconcile');
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed; the page now shows the current workspace.'));
});

test('S3 dialog drafts end on close and on save; a closed dialog never counts as a draft', async () => {
  const { fetch } = hostedFixture();
  const { win, main, modal } = await viewsWith(fetch);
  // Closing the dialog clears the flags of the forms inside it.
  const inDialog = fakeForm('alert-form');
  inDialog.dataset = { dirty: '1', held: '1' };
  modal.open = true;
  modal.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [inDialog] : []);
  win.EW.closeDialog();
  assert.deepEqual(plain(inDialog.dataset), {}, 'close clears dirty and held');
  // A dirty form inside a closed dialog is excluded everywhere.
  const phantom = fakeForm('alert-form');
  phantom.closest = sel => (sel === '#modal' ? { open: false } : null);
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [phantom] : []);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  // A successful save clears the saved form's flags.
  const saved = fakeForm('settings-workspace');
  saved.dataset = { dirty: '1' };
  win.FormData = class { *entries() { yield ['workspace_name', 'Ok']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(saved);
  assert.equal(saved.dataset.dirty, undefined, 'saved form is no longer a draft');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const chooser = fakeForm('workspace-choice');
  chooser.dataset = {};
  let unsavedPrompt = false;
  const realOpen = win.EW.openDialog;
  win.EW.openDialog = (title) => { if (/Unsaved/.test(title)) unsavedPrompt = true; };
  await win.EWViews.handleSubmit(chooser);
  win.EW.openDialog = realOpen;
  assert.equal(unsavedPrompt, false, 'chooser ignores a closed dialog draft');
  assert.equal(phantom.submit.disabled, false, 'no notice or hold inside an invisible dialog');
  void main;
});

test('S4 a switch submitted during an in-flight canonical read waits for it and orders on its result', async () => {
  let moved = false, gateOn = false, release;
  const gate = new Promise(r => { release = r; });
  const posts = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') {
      if (gateOn) { gateOn = false; await gate; }
      return json(moved ? hostedSession({ active_workspace: posts.length ? 'ws-c' : 'ws-b',
        workspace_context: posts.length ? 'ctx-c' : 'ctx-b', workspace_switch_revision: posts.length ? 7 : 6 }) : hostedSession());
    }
    if (url === '/api/v1/session/workspace') {
      const b = JSON.parse(opts.body);
      posts.push(b);
      if (b.expected_workspace_switch_revision !== 6) return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'stale' } }, false, 409);
      return json(hostedSession({ active_workspace: 'ws-c', workspace_context: 'ctx-c', workspace_switch_revision: 7 }));
    }
    return json({ ok: true, workspace_context: moved ? (posts.length ? 'ctx-c' : 'ctx-b') : 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  mountMain(win);
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.saveNotice = () => {};
  win.EW.closeDialog = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  // An uncertain earlier switch left a fence at rev 5; the server is now at 6.
  win.EW.current.switchOp = 1;
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  moved = true; gateOn = true;
  const rc = win.EWViews.reconcileCanonical();
  await settleAll(2);
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-c']; } };
  const sub = win.EWViews.handleSubmit(fakeForm('workspace-choice'));
  await settleAll(3);
  assert.equal(posts.length, 0, 'the switch waits for the in-flight canonical read');
  release();
  await rc; await sub; await settleAll();
  assert.deepEqual(posts.map(p => p.expected_workspace_switch_revision), [6], 'ordered on the adopted revision, no lost 409');
  assert.equal(win.EW.current.writeFence, null, 'no fence held after the adopted revision passed it');
  assert.equal(win.EW.current.workspaceContext, 'ctx-c');
  assert.ok(toasts.includes('Switched to ws-c.'));
});

test('S5 fenced copy offers the chooser on every route, in the banner and in form errors', async () => {
  const { fetch } = hostedFixture(async (url) => (url === '/api/v1/session/workspace' ? Promise.reject(new TypeError('Failed to fetch')) : null));
  const { win, main } = await viewsWith(fetch);
  let banner = '';
  main.querySelectorAll = () => [];
  main.insertAdjacentHTML = (pos, html) => { banner = html; };
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.match(banner, /could not be confirmed\. Choose the workspace again to confirm it\./, 'banner says what to do even with no write controls');
  assert.match(banner, /data-action="choose-workspace"[^>]*>Choose workspace</, 'banner carries a real Choose workspace control');
  assert.doesNotMatch(banner, /below|Open Settings/);
  win.EW.current.writeFence = null;
  const form = fakeForm('workspace-choice');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /Choose the workspace again to confirm it\./, 'on the chooser itself the copy carries no direction');
  assert.doesNotMatch(form.box.innerHTML, /below|Open Settings|choose-workspace/);
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(banner, /data-action="choose-workspace"/, 'Settings banner offers the chooser control too');
  assert.doesNotMatch(banner, /below|Open Settings/);
});

test('S6 releasing a fence hold restores the control own tooltip', async () => {
  const { fetch } = hostedFixture();
  const { win } = await viewsWith(fetch);
  const root = fenceRoot();
  root.writes[0].title = 'Pause checks for this alert';
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#main' ? root : base(sel));
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  await win.EWViews.render(); // publication applies the hold to #main
  win.EW.current.writeFence = null;
  win.EW.boot = async () => ({ admitted: true });
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [fakeForm()] : []);
  await win.EWViews.reconcileCanonical(); // held-draft path re-syncs without a render
  assert.equal(root.writes[0].title, 'Pause checks for this alert');
  assert.equal(root.writes[0].disabled, false);
});

test('S7 a held notice never nests alert roles', async () => {
  const { fetch } = hostedFixture(async (url, opts) => (url === '/api/v1/customer/preferences' && opts.method === 'PUT'
    ? json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409) : null));
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = fakeForm();
  const roles = [];
  form.box.setAttribute = (k, v) => roles.push([k, v]);
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(form);
  assert.doesNotMatch(form.box.innerHTML, /role="alert"/, 'inner notice carries no alert role');
  assert.ok(roles.some(([k, v]) => k === 'role' && v === 'alert'), 'the box is the single alert region');
});

test('S8 a refused 200 that cannot be ordered never claims another switch won', async () => {
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return json(hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof', workspace_switch_revision: 'six', frontend_contract: null }));
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch);
  await flushBoot();
  mountMain(win);
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-proof';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.ok(!toasts.some(m => /Another switch already selected/.test(m)), toasts.join(' | '));
  assert.ok(toasts.some(m => m.startsWith('The switch to ws-proof could not be confirmed. This workspace needs a different version of the app.')), toasts.join(' | '));
});

/* -- Ported reviewer reproductions: each asserts the correct behaviour so
      a later change cannot silently reopen it. Names carry their origin. -- */
// Reviewer form stub: no editable elements unless asked (a confirmation),
// stable error box and submit.
const reviewerForm = (id, { dirty = false, entries = false } = {}) => {
  const submit = { disabled: false, textContent: 'Save', setAttribute() {}, focus() {} };
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  return { submit, box, dataset: dirty ? { dirty: '1' } : {}, elements: entries ? [{ type: 'text' }] : [{ type: 'hidden' }],
    getAttribute: () => id, closest: () => null,
    querySelector: sel => (sel === '#form-errors' ? box : sel === '[type=submit]' ? submit : null),
    querySelectorAll: sel => (sel === '[type=submit]' ? [submit] : []) };
};
const sessB = (rev = 6) => hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: rev });

test('REV fe10-h1: a transient reconcile failure does not strand Settings; later renders recover', async () => {
  let moved = false, failNext = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') {
      if (failNext) { failNext = false; throw new TypeError('Failed to fetch'); }
      return json(moved ? sessB() : hostedSession());
    }
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: moved ? 'Beta' : 'Alpha', revision: 1 }, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.EW.toast = () => {};
  win.location.hash = '#/settings';
  await win.EWViews.render();
  moved = true; failNext = true;
  await win.EWViews.render(); await settleAll();
  for (let i = 0; i < 3; i++) { await win.EWViews.render(); await settleAll(); }
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.match(main.innerHTML, /Beta/);
});

test('REV fe10-h1b: a read dropped during a pending switch whose reply is lost recovers once settled', async () => {
  let committed = false, release;
  const gate = new Promise(r => { release = r; });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') { committed = true; await gate; throw new TypeError('Failed to fetch'); }
    if (url === '/api/v1/session') return json(committed ? sessB() : hostedSession());
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: committed ? 'Beta' : 'Alpha', revision: 1 }, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
    return json({ ok: true, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.closeDialog = () => {};
  win.location.hash = '#/settings'; await win.EWViews.render();
  win.EW.current.pendingWorkspace = 'ws-b';
  const p = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(3);
  await win.EWViews.render(); await settleAll();
  release(); await p; await settleAll();
  for (let i = 0; i < 3; i++) { await win.EWViews.render(); await settleAll(); }
  assert.equal(win.EW.current.writeFence, null);
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.match(main.innerHTML, /Beta/);
  assert.ok(toasts.includes('Switched to ws-b.'));
});

test('REV fe10-h2: discard into a refused workspace names the committed switch; a button 409 re-renders under B', async () => {
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return json(hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof', workspace_switch_revision: 6, frontend_contract: null }));
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-proof';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  assert.deepEqual(toasts, ['Switched to ws-proof. This workspace needs a different version of the app. Choose another workspace or sign out. Unsaved changes were discarded. Nothing was saved to ws-a.']);
  assert.equal(win.EW.current.writeFence, null);
  assert.equal(win.EW.current.workspaceSwitchRevision, 6);
  assert.match(main.innerHTML, /workspace-choice/);
  assert.match(main.innerHTML, /data-action="signout"/);
  // H3: a button 409.
  let moved = false;
  const w3 = loadViews(async (url) => {
    if (url === '/api/v1/session') return json(moved ? sessB() : hostedSession());
    if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
    if (url === '/api/v1/customer/alerts/7/read') { moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  });
  await flushBoot(); mountMain(w3);
  const t3 = []; w3.EW.toast = m => t3.push(m);
  w3.location.hash = '#/overview'; await w3.EWViews.render();
  const err = await w3.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).then(() => null, e => e);
  await settleAll();
  assert.equal(err.message, 'This workspace changed in another tab or window. Nothing was changed.');
  assert.equal(w3.EW.current.workspaceContext, 'ctx-b');
  assert.equal(w3.EW.current.displayedWorkspaceSwitchRevision, 6, 're-rendered under B');
  assert.ok(t3.includes('This workspace changed in another tab or window. Nothing was changed; the page now shows the current workspace.'));
});

test('REV fe11-a: button 401 and 409 copy matches the actual outcome (A signed-out, B held draft, C failed read)', async () => {
  // A: the write says 401 but the session read is still signed in: the
  // copy claims nothing more, and the canonical session wins.
  {
    const { win } = await actionWin(markReadFixture(
      () => json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' }, signin: { available: true } }, false, 401),
      () => json(hostedSession())));
    const err = await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).then(() => null, e => e);
    await settleAll();
    assert.equal(err.message, 'You are signed out. Nothing was changed.');
    assert.equal(win.EW.current.principal, 'sam', 'a still-valid session is not signed out');
  }
  // B: held draft — no claim of a re-render; Reload workspace in #main.
  {
    let moved = false;
    const { win, main, toasts } = await actionWin(markReadFixture(
      () => { moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); },
      () => json(moved ? sessB() : hostedSession())));
    const draft = reviewerForm('alert-edit', { dirty: true, entries: true });
    win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) ? [draft] : []);
    main.innerHTML = 'OLD-A-PAGE';
    await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
    await settleAll();
    assert.equal(win.EW.current.workspaceContext, 'ctx-b');
    assert.equal(draft.dataset.held, '1');
    assert.match(draft.box.innerHTML, /your entries are kept here/);
    assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*reload-workspace[\s\S]*OLD-A-PAGE/);
    assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed. Reload the workspace to continue.'));
  }
  // C: failed reconcile read — says so, offers Retry, basis untouched.
  {
    let fail = false;
    const { win, main, toasts } = await actionWin(async (url) => {
      if (url === '/api/v1/session') { if (fail) throw new TypeError('Failed to fetch'); return json(hostedSession()); }
      if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: 'ctx-a' });
      if (url === '/api/v1/customer/alerts/7/read') { fail = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
      return json({ ok: true, workspace_context: 'ctx-a' });
    });
    main.innerHTML = 'OLD-A-PAGE';
    await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
    await settleAll();
    assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*reconcile-retry/);
    assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 5);
    assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed. The current workspace could not be loaded; try again.'));
  }
});

test('REV fe11-b: a switch submitted during an in-flight reconcile orders on its adopted revision and never loses its 409', async () => {
  let moved = false, gateOn = false, release;
  const gate = new Promise(r => { release = r; });
  const posts = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') { if (gateOn) await gate; return json(moved ? sessB() : hostedSession()); }
    if (url === '/api/v1/session/workspace') { posts.push(JSON.parse(opts.body)); return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'stale' } }, false, 409); }
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); mountMain(win);
  win.EW.toast = () => {}; win.EW.saveNotice = () => {}; win.EW.closeDialog = () => {};
  win.location.hash = '#/settings'; await win.EWViews.render();
  moved = true; gateOn = true;
  const rc = win.EWViews.reconcileCanonical();
  await settleAll(2);
  const form = reviewerForm('workspace-choice');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-c']; } };
  const sub = win.EWViews.handleSubmit(form);
  await settleAll(3);
  release(); await rc; await sub; await settleAll();
  assert.deepEqual(posts.map(p => p.expected_workspace_switch_revision), [6], 'ordered on the adopted revision');
  assert.equal(win.EW.current.writeFence && win.EW.current.writeFence.rev, 6, 'no fence left at the stale revision');
  assert.match(form.box.innerHTML, /could not be confirmed/, 'the 409 outcome is reported, not dropped');
});

test('REV fe12-h1-D18: a failed confirming read is not a refusal; Retry and the same-target choice both re-confirm', async () => {
  let sessionFail = false, posts = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') { posts++; sessionFail = true; return json(sessB(5 + posts)); }
    if (url === '/api/v1/session') { if (sessionFail) { sessionFail = false; throw new TypeError('Failed to fetch'); } return json(posts ? sessB(5 + posts) : hostedSession()); }
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: 'Beta', revision: 1 }, workspace_context: 'ctx-b' });
    return json({ ok: true, workspace_context: posts ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(reviewerForm('workspace-choice'));
  await settleAll();
  const copy = 'Switched to ws-b, but the workspace could not be loaded. Retry.';
  assert.ok(toasts.includes(copy));
  assert.doesNotMatch(main.innerHTML, /Failed to fetch/, 'no raw transport error');
  assert.match(main.innerHTML, /data-action="retry-session"/, 'working Retry offered');
  // Same-target choice re-confirms (POST at the current revision), never a no-op.
  toasts.length = 0;
  await win.EWViews.handleSubmit(reviewerForm('workspace-choice'));
  await settleAll();
  assert.equal(posts, 2, 'same-target choice POSTed again');
  assert.ok(!toasts.includes('Already in that workspace.'));
  // Retry re-runs the canonical read and renders.
  win.location.hash = '#/settings';
  await win.EWViews.handleAction({ dataset: { action: 'retry-session' } });
  await settleAll();
  assert.equal(win.EW.current.bootAdmitted, true);
  assert.equal(win.EW.current.bootRefused, null);
  assert.match(main.innerHTML, /Beta/, 'workspace loaded after Retry');
});

test('REV fe12-h1-D1: a confirmation with no entries never claims kept or unsaved entries', async () => {
  let moved = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(moved ? sessB() : hostedSession());
    if (/lifecycle/.test(url)) { moved = true; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); mountMain(win);
  win.location.hash = '#/alert/7'; win.EW.current.route = '#/alert/7';
  await win.EWViews.render();
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  const form = reviewerForm('confirm');
  win.document.querySelectorAll = sel => (/data-held/.test(sel) && form.dataset.held ? [form] : []);
  win.FormData = class { *entries() { yield ['confirm_action', 'stop-alert']; yield ['confirm_id', '7']; yield ['expected_revision', '3']; } };
  await win.EWViews.handleSubmit(form);
  await settleAll();
  await win.EWViews.handleAction({ dataset: { action: 'reload-workspace' } });
  assert.equal(form.dataset.held, undefined, 'not held as a draft');
  assert.ok(!toasts.some(m => /entries|unsaved entry/i.test(m)), toasts.join(' | '));
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed; the page now shows the current workspace.'));
});

test('REV fe12-h2: no second switch starts while the first is settling; the first ends in a render and a toast', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let booted = false, gated = false, releaseRead;
  const readGate = new Promise(r => { releaseRead = r; });
  let posts = 0;
  const fetch = async (url, opts) => {
    if (url === '/api/v1/session/workspace') {
      const body = JSON.parse(opts.body); posts++;
      if (body.expected_workspace_switch_revision !== server.rev) return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT' } }, false, 409);
      server = { ws: body.workspace_id, ctx: 'ctx-' + body.workspace_id.slice(3), rev: server.rev + 1 };
      return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    }
    if (url === '/api/v1/session') {
      if (booted && !gated) { gated = true; await readGate; }
      booted = true;
      return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    }
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: 'DATA-' + server.ws, revision: 1 }, workspace_context: server.ctx });
    return json({ ok: true, workspace_context: server.ctx });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.closeDialog = () => {};
  main.innerHTML = 'PAGE-WS-A';
  win.EW.current.pendingWorkspace = 'ws-b';
  const p1 = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll();
  assert.equal(win.EW.current.switchSettling, 1, 'first switch is settling');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-c']; } };
  const f2 = reviewerForm('workspace-choice');
  await win.EWViews.handleSubmit(f2);
  assert.equal(posts, 1, 'no second POST while settling');
  assert.match(f2.box.innerHTML, /A workspace switch is still being confirmed\./);
  releaseRead(); await p1; await settleAll();
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.notEqual(main.innerHTML, 'PAGE-WS-A', 'never left on the stale page');
  assert.match(main.innerHTML, /DATA-ws-b/);
  assert.ok(toasts.some(m => /^Switched to ws-b\./.test(m)), 'the first switch reports its outcome');
});

test('REV fe12-h3: a sign-out follow-up that finds a refused contract keeps the refusal exits', async () => {
  let out401 = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(out401 ? hostedSession({ frontend_contract: null, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    if (/check$/.test(url)) { out401 = true; return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' }, signin: { available: true } }, false, 401); }
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.location.hash = '#/alert/7'; await win.EWViews.render();
  win.EW.toast = () => {}; win.EW.closeDialog = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'check-alert', id: '7' } });
  await settleAll();
  assert.equal(win.EW.current.principal, 'sam');
  assert.match(main.innerHTML, /workspace-choice/);
  assert.match(main.innerHTML, /data-action="signout"/);
  // The explicit Sign in action keeps them too.
  await win.EWViews.handleAction({ dataset: { action: 'show-signin' } });
  assert.equal(win.EW.current.principal, 'sam');
  assert.match(main.innerHTML, /data-action="signout"/);
});

/* -- Round 4 items without a reviewer harness -- */
test('T5 (pass 16, DEMO2-R12-01) a 401 seals a dialog holding a live draft; a re-read that lands in another workspace drops its kept entry and says so', async () => {
  let out = false;
  const { win } = await actionWin(markReadFixture(
    () => { out = true; return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401); },
    () => json(out ? sessB() : hostedSession())));
  win.FormData = class { *entries() { yield ['name', 'typed']; } };
  const draft = reviewerForm('alert-form', { dirty: true, entries: true });
  const modal = { open: true, innerHTML: 'PRIVATE', closed: 0, close() { this.closed++; this.open = false; },
    querySelectorAll: sel => (/form/.test(sel) ? [draft] : []) };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#modal' ? modal : base(sel));
  draft.closest = sel => (sel === '#modal' ? modal : null);
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) && draft.dataset.dirty ? [draft] : []);
  const pending = win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).catch(() => {});
  await flushBoot();
  await settleAll(2);
  await pending;
  await settleAll();
  // The re-read found a signed-in session in ws-b: the kept entry belonged
  // to ws-a, so the dialog closes and the drop is told.
  assert.equal(win.EW.current.activeWorkspace, 'ws-b');
  assert.equal(modal.open, false, 'the held dialog stayed over another workspace');
  assert.doesNotMatch(modal.innerHTML, /PRIVATE/, 'the dialog kept server-derived content');
  assert.equal(win.EW.current.dialogDrafts.size, 0, 'an entry typed in ws-a was kept for ws-b');
  assert.match(JSON.stringify(win.EW.current.outcomeNotice || {}), /Your unsaved entry in .* was not applied\./);
  assert.doesNotMatch(draft.box.innerHTML, /signed out/);
});

test('T6 a 409 overlapping this tab own switch waits for it and never blames another tab', async () => {
  let committed = false, releaseRead, gated = false, sessionReads = 0;
  const readGate = new Promise(r => { releaseRead = r; });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') { committed = true; return json(sessB()); }
    if (url === '/api/v1/session') {
      sessionReads++;
      if (committed && !gated) { gated = true; await readGate; }
      return json(committed ? sessB() : hostedSession());
    }
    if (url === '/api/v1/customer/alerts/7?summaries=0') return json({ alert: { id: 7, events: [{ id: 'e1', read_state: false }] }, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
    if (url === '/api/v1/customer/alerts/7/read') return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409);
    return json({ ok: true, workspace_context: committed ? 'ctx-b' : 'ctx-a' });
  };
  const { win, toasts } = await actionWin(fetch);
  win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(4);
  assert.equal(win.EW.current.switchSettling, 1);
  win.EW.current.displayedWorkspaceSwitchRevision = 5; // the old view is still on screen
  const err = await win.EWViews.handleAction({ dataset: { action: 'mark-read', id: '7' } }).then(() => null, e => e);
  await settleAll(4);
  assert.equal(err.message, 'This workspace changed. Nothing was changed.', 'neutral while this tab switches');
  const readsWhileSettling = sessionReads;
  releaseRead(); await sw; await settleAll();
  assert.ok(sessionReads > readsWhileSettling, 'the conflict reconcile ran only after the switch settled');
  assert.ok(!toasts.some(m => /another tab or window/.test(m)), toasts.join(' | '));
  assert.ok(toasts.includes('This workspace changed. Nothing was changed; the page now shows the current workspace.'));
});

test('T8 the switch busy status sits beside the chooser in a polite live region', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url) => {
    if (url === '/api/v1/session/workspace') { await gate; return json(sessB()); }
    return null;
  });
  const { win, main } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /<form id="workspace-choice"[\s\S]*role="status" aria-live="polite" data-switch-status/, 'chooser carries its status region');
  const status = { textContent: '' };
  const form = reviewerForm('workspace-choice');
  const q = form.querySelector;
  form.querySelector = sel => (sel === '[data-switch-status]' ? status : q(sel));
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const p = win.EWViews.handleSubmit(form);
  await settleAll(3);
  assert.equal(status.textContent, 'Switching workspace…', 'busy status beside the chooser');
  release(); await p;
  assert.equal(status.textContent, '');
});

test('T9 a refusal clears the previous workspace header, menu and counts strip', async () => {
  const { fetch } = hostedFixture();
  const { win, main } = await viewsWith(fetch);
  const header = { innerHTML: '<span class="mobile-section">Settings</span><button class="mobile-menu">Menu</button>' };
  const strip = { innerHTML: '<span>Local workspace</span>0 episodes · 0 shows' };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#header' ? header : sel === '#dataset' ? strip : base(sel));
  win.EW.current.bootRefused = 'This workspace needs a different version of the app. Choose another workspace or sign out.';
  win.EW.current.bootAdmitted = false;
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.match(main.innerHTML, /different version of the app/);
  assert.doesNotMatch(header.innerHTML, /Menu|mobile-section/, 'workspace header cleared to the wordmark');
  assert.equal(strip.innerHTML, '', 'counts strip cleared');
});

test('T2 the chooser is held while a switch settles and released after', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { st.ws = 'b'; st.rev = 6; return json(sessB()); }
    if (url === '/api/v1/session' && st.ws === 'b') { await gate; return null; }
    return null;
  });
  const { win } = await viewsWith(fetch);
  const chooser = { disabled: false, title: 'Use workspace', dataset: {}, closest: () => null };
  const root = { innerHTML: '', insertAdjacentHTML() {},
    querySelectorAll: sel => (sel === '#workspace-choice [type=submit]' ? [chooser]
      : sel === '[data-fence-held]' ? (chooser.dataset.fenceHeld ? [chooser] : []) : []) };
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#main' ? root : base(sel));
  win.EW.render = () => {}; win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const p = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(4);
  assert.equal(chooser.disabled, true, 'chooser held while settling');
  assert.equal(chooser.title, 'A workspace switch is still being confirmed.');
  release(); await p;
  assert.equal(chooser.disabled, false, 'released after');
  assert.equal(chooser.title, 'Use workspace', 'own tooltip restored');
});

/* -- Round 5: fe13 reviewer reproductions and the two general rules -- */
// Reviewer-style open dialog wrapping one form; the form counts as held
// or dirty only while the dialog is open.
const dialogWith = (win, form) => {
  const dlg = { open: true, innerHTML: '', close() { this.open = false; }, setAttribute() {},
    querySelector: (s) => (/data-held/.test(s) && form.dataset.held ? form : null),
    querySelectorAll: (s) => (form.dataset.dirty || form.dataset.held ? [form] : []) };
  form.closest = s => (s === '#modal' ? dlg : null);
  return dlg;
};

test('REV fe13-h1-freeze: a workspace 409 on the freeze confirmation closes it; stale pins never go out under a newer epoch', async () => {
  let server = { rev: 5 }, first = true;
  const writes = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_switch_revision: server.rev }));
    if (url.endsWith('/reports') && opts.method === 'POST') {
      writes.push(opts.headers['X-EarWitness-Workspace-Switch-Revision']);
      if (first) { first = false; server.rev = 6; return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409); }
      return json({ ok: true, report_no: 9, workspace_context: 'ctx-a' });
    }
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const form = reviewerForm('confirm');
  form.insertBefore = () => {};
  const dlg = dialogWith(win, form);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {};
  win.location.hash = '#/campaign/1'; win.EW.current.route = '#/campaign/1';
  await win.EWViews.render(); await settleAll();
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '1']; yield ['freeze_pins', '{}']; yield ['freeze_revision', '3']; } };
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.dialogEpoch = { rev: 5 }; // as openDialog records it
  await win.EWViews.handleSubmit(form); await settleAll(40);
  assert.equal(dlg.open, false, 'the no-entry confirmation closed on the workspace 409');
  if (dlg.open && !form.submit.disabled) await win.EWViews.handleSubmit(form);
  // Even a confirmation still on screen refuses under the newer epoch.
  dlg.open = true;
  await win.EWViews.handleSubmit(form); await settleAll();
  assert.deepEqual(writes, ['5'], 'never re-sent with header 6');
  assert.equal(form.submit.disabled, true, 'Confirm disabled on the stale dialog');
  assert.match(form.box.innerHTML, /changed after this was opened\. Nothing was changed\. Reload the workspace/);
});

test('REV fe13-h2-heldclose: closing a held dialog announces the discard and shows the canonical route', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
      return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409);
    }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Name ' + server.ws, revision: 1 } });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const form = reviewerForm('settings-workspace', { dirty: true, entries: true });
  const dlg = dialogWith(win, form);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s) && dlg.open && (form.dataset.dirty || form.dataset.held) ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  main.innerHTML = 'PAGE-OF-WS-A';
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(form); await settleAll(40);
  assert.equal(dlg.open, true, 'held while the user decides');
  assert.equal(main.innerHTML, 'PAGE-OF-WS-A');
  await win.EWViews.handleAction({ dataset: { action: 'close-dialog' } }); await settleAll();
  assert.ok(toasts.includes('Your unsaved entry in ws-a was not applied.'), 'names the workspace the entry was typed in');
  assert.notEqual(main.innerHTML, 'PAGE-OF-WS-A', 'the older page is not left under the new workspace');
  assert.match(main.innerHTML, /Name ws-b/);
});

test('REV fe13-h3-signout (pass 16, DEMO2-R12-01): a sign-out re-read that finds a signed-in session in another workspace makes no sign-out claim and tells the entry was not applied', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
      return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401);
    }
    return json({ ok: true, workspace_context: server.ctx, preferences: { revision: 1 } });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const form = reviewerForm('settings-workspace', { dirty: true, entries: true });
  const dlg = dialogWith(win, form);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s) && dlg.open && (form.dataset.dirty || form.dataset.held) ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(form); await settleAll(40);
  assert.equal(win.EW.current.principal, 'sam');
  assert.equal(win.EW.current.activeWorkspace, 'ws-b');
  assert.doesNotMatch(dlg.innerHTML, /signed out|show-signin/, 'no false sign-out claim');
  assert.equal(dlg.open, false, 'the held dialog stayed over another workspace');
  assert.equal(win.EW.current.dialogDrafts.size, 0, 'an entry typed in ws-a was kept for ws-b');
  assert.match(JSON.stringify(win.EW.current.outcomeNotice || {}), /Your unsaved entry in .* was not applied\./);
});

test('REV fe13-h4-failrecon: a dropped read plus a failed reconcile never strands a route; it ends in unavailable + Retry or the canonical workspace', async () => {
  for (const route of ['#/settings', '#/overview', '#/alerts', '#/search', '#/saved']) {
    let moved = false, failNext = false;
    const fetch = async (url) => {
      if (url === '/api/v1/session') {
        if (failNext) { failNext = false; return json({ ok: false, error: 'boom' }, false, 500); }
        return json(moved ? sessB() : hostedSession());
      }
      return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a', preferences: { revision: 1 } });
    };
    const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
    win.EW.toast = () => {};
    win.location.hash = route; win.EW.current.route = route;
    await win.EWViews.render(); await settleAll();
    moved = true; failNext = true;
    await win.EWViews.render(); await settleAll(40);
    assert.doesNotMatch(main.innerHTML, /Opening…|response was dropped/, route + ' never stranded');
    if (win.EW.current.workspaceContext !== 'ctx-b') {
      // Its only re-check failed: unavailable with a working Retry.
      assert.match(main.innerHTML, /The current workspace could not be loaded\. Try again\./, route);
      assert.match(main.innerHTML, /data-action="retry-workspace"/, route + ' offers Retry');
    } // else a later read of the same render (Search) re-checked after the failure and recovered
    assert.equal(win.EW.current.publishedWorkspace === 'ws-a' && win.EW.current.workspaceContext === 'ctx-b', false, route + ' never shows A under B');
    await win.EWViews.handleAction({ dataset: { action: 'retry-workspace' } }); await settleAll(40);
    assert.equal(win.EW.current.workspaceContext, 'ctx-b', route + ': Retry reconciles');
  }
});

test('U1 a persistently disagreeing server still ends the route with a way forward, without looping', async () => {
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-b' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.EW.toast = () => {};
  const sessions = () => calls.filter(u => u === '/api/v1/session').length;
  win.location.hash = '#/overview';
  await win.EWViews.render(); await settleAll();
  const afterFirst = sessions();
  await win.EWViews.render(); await settleAll();
  assert.equal(sessions() - afterFirst, 1, 'one reconcile per render generation, never more');
  await settleAll(40);
  assert.equal(sessions() - afterFirst, 1, 'no loop while idle');
  assert.doesNotMatch(main.innerHTML, /Opening…/);
  assert.match(main.innerHTML, /data-action="retry-workspace"/);
});

test('U2 publication closes, and announces, a non-held dialog opened under an older epoch', async () => {
  const { fetch } = hostedFixture();
  const { win, modal, toasts } = await viewsWith(fetch);
  modal.querySelector = () => null;
  modal.querySelectorAll = () => [];
  modal.open = true;
  win.EW.current.dialogEpoch = { rev: 4 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal(modal.open, false, 'stale dialog closed at publication');
  assert.ok(toasts.includes('The workspace changed, so the open dialog was closed. Nothing was changed.'));
});

test('U3 any write form in a stale dialog refuses before dispatch, held with Reload', async () => {
  const calls = [];
  const { fetch } = hostedFixture(async (url, opts) => { if (opts.method === 'POST') calls.push(url); return null; });
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('save-search', { entries: true });
  dialogWith(win, form);
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.dialogEpoch = { rev: 4 }; // opened before the current publication (5)
  win.FormData = class { *entries() { yield ['name', 'My search']; } };
  await win.EWViews.handleSubmit(form);
  assert.equal(calls.length, 0, 'nothing dispatched');
  assert.equal(form.submit.disabled, true);
  assert.match(form.box.innerHTML, /your entries are kept here[\s\S]*reload-workspace/);
});

test('U5 a workspace 409 in the allocation flow uses the workspace copy, not an empty allocation conflict', async () => {
  const { fetch } = hostedFixture(async (url) => (/\/allocation$/.test(url)
    ? json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409) : null));
  const { win, toasts } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('allocation-confirm');
  const dlg = dialogWith(win, form);
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#modal' ? dlg : base(sel));
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.FormData = class { *entries() { yield ['allocation_action', 'retain']; yield ['allocation_ref', 'r1']; yield ['campaign_id', '1'];
    yield ['from_target_id', 'p1']; yield ['expected_campaign_revision', '2']; yield ['expected_from_revision', '3']; yield ['units_json', '{}']; } };
  await win.EWViews.handleSubmit(form); await settleAll();
  assert.doesNotMatch(form.box.innerHTML, /\?/, 'no empty "rev ? → current ?" conflict');
  assert.equal(dlg.open, false, 'the no-entry confirmation closed');
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed.'));
});

test('U6 the workspace-required page always offers Sign out, and the chooser when workspaces exist', async () => {
  for (const spaces of [[], ['ws-a', 'ws-b']]) {
    const win = loadViews(canonicalSessionFetch());
    await flushBoot();
    const main = mountMain(win);
    win.EW.current.hosted = true;
    win.EW.current.principal = 'sam';
    win.EW.current.activeWorkspace = 'ws-x';
    win.EW.current.workspaces = spaces;
    win.EW.current.workspaceContext = '';
    win.location.hash = '#/overview';
    await win.EWViews.render();
    assert.match(main.innerHTML, /Workspace required/);
    assert.match(main.innerHTML, /data-action="signout"/, spaces.length + ' workspaces: Sign out offered');
    assert.equal(/id="workspace-choice"/.test(main.innerHTML), spaces.length > 0);
  }
});

test('U7 a local (loopback) contract refusal offers an honest Reload', async () => {
  const win = loadViews(async (url) => (url === '/api/v1/session'
    ? json({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32), workspace_context: 'ctx-l', frontend_contract: 'other/9' })
    : json({ ok: true })));
  await flushBoot();
  const page = win.document.querySelector('#main').innerHTML;
  assert.match(page, /This page needs a different version of the app\. Reload after updating\./);
  assert.match(page, /data-action="reload-page"/);
  assert.doesNotMatch(page, /retry-session/);
});

test('U8 Retry on the refusal page waits for a settling switch before its read', async () => {
  let releaseRead, gated = false, sessionReads = 0;
  const readGate = new Promise(r => { releaseRead = r; });
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { st.ws = 'b'; st.rev = 6; return json(sessB()); }
    if (url === '/api/v1/session') {
      sessionReads++;
      if (st.ws === 'b' && !gated) { gated = true; await readGate; }
    }
    return null;
  });
  const { win } = await viewsWith(fetch);
  win.EW.render = () => {}; win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(4);
  const before = sessionReads;
  const retry = win.EWViews.handleAction({ dataset: { action: 'retry-session' } });
  await settleAll(4);
  assert.equal(sessionReads, before, 'Retry has not read while the switch settles');
  releaseRead(); await sw; await retry;
  assert.ok(sessionReads > before, 'Retry read after the switch settled');
});

test('U9 a refusal after an earlier failed read never shows Retry', async () => {
  let refuse = false, fail = true;
  const win = loadViews(async (url) => {
    if (url === '/api/v1/session') {
      if (fail) { fail = false; throw new TypeError('Failed to fetch'); }
      return json(refuse ? hostedSession({ frontend_contract: 'other/9' }) : hostedSession());
    }
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  await flushBoot();
  assert.equal(win.EW.current.bootRetry, true, 'the failed read offers Retry');
  const main = mountMain(win);
  win.EW.toast = () => {};
  refuse = true;
  await win.EWViews.handleAction({ dataset: { action: 'retry-session' } });
  assert.match(main.innerHTML, /different version of the app/);
  assert.doesNotMatch(main.innerHTML, /retry-session/, 'a refusal never offers Retry');
  assert.match(main.innerHTML, /data-action="signout"/, 'its exits instead');
});

/* -- Round 6: fe14 reviewer reproductions and hardening -- */
test('REV fe14-h1-paint: late reads from before a published view never replace that good, current page with an error; while they hold both request places the same page may show its own loading state, then the good page', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const gates = [];
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (url.startsWith('/api/v1/slow/')) { await new Promise(r => gates.push(r)); return json({ ok: true, workspace_context: 'ctx-b' }); }
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: 'Alpha', revision: 1 }, workspace_context: server.ctx });
    return json({ ok: true, workspace_context: server.ctx });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const reads = [1, 2, 3].map(i => win.EW.api('/slow/' + i).catch(e => e));
  await settleAll(2);
  server = { ws: 'ws-a', ctx: 'ctx-a', rev: 7 }; // another tab: A→B(6)→A(7)
  // READ-QUEUE (G2-02): at most 2 requests in flight per tab, so the
  // re-check's own reads may wait for a late read to free a place; the
  // page meanwhile is this same Settings page loading, never an error.
  // READ-QUEUE-FIX1 (RQ-R1-05): renamed to say so. The original check
  // (Alpha after every late read) fails with the queue: the re-check
  // render shows "Loading members and invitations" while late reads hold
  // both places. Restoring it would change that render, outside this fix.
  for (let i = 0; i < 3; i++) {
    while (!gates.length) await settleAll(5);
    gates.shift()(); await settleAll(40);
    assert.match(main.innerHTML, /Alpha|<h1>Settings<\/h1>[\s\S]*Loading members and invitations/, 'read ' + (i + 1) + ': the current page stays');
    assert.doesNotMatch(main.innerHTML, /could not be loaded/);
  }
  await Promise.all(reads); await settleAll(40);
  assert.match(main.innerHTML, /Alpha/, 'the current page, once the late reads are done');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 7);
});

test('REV fe14-h2-closeall: closing a dialog never re-renders over a draft still held on the page', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (url === '/api/v1/customer/preferences') return json({ preferences: { workspace_name: server.ws === 'ws-a' ? 'Alpha' : 'Beta', revision: 1 }, workspace_context: server.ctx });
    return json({ ok: true, workspace_context: server.ctx });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const confirmForm = reviewerForm('confirm');
  const dlg = dialogWith(win, confirmForm);
  const mainDraft = reviewerForm('invite-create', { dirty: true, entries: true });
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s)
    ? [mainDraft, confirmForm].filter(f => (f.dataset.dirty === '1' || f.dataset.held === '1') && (f === mainDraft || dlg.open)) : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  main.innerHTML = 'SETTINGS-A with typed invite draft';
  win.EW.current.dialogWorkspace = 'ctx-a'; win.EW.current.dialogEpoch = { rev: 5 }; win.EW.current.dialogRoute = '#/settings';
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
  await win.EW.api('/customer/anything').catch(() => {}); await settleAll(40);
  assert.equal(mainDraft.dataset.held, '1');
  win.FormData = class { *entries() { yield ['x', '1']; } };
  await win.EWViews.handleSubmit(confirmForm); await settleAll();
  assert.equal(confirmForm.dataset.held, '1', 'the stale confirmation is held');
  await win.EWViews.handleAction({ dataset: { action: 'close-dialog' } }); await settleAll(40);
  assert.equal(dlg.open, false);
  assert.equal(main.innerHTML, 'SETTINGS-A with typed invite draft', 'the held page draft is not wiped');
  assert.equal(mainDraft.dataset.held, '1');
  assert.match(mainDraft.box.innerHTML, /your entries are kept here[\s\S]*reload-workspace/, 'its notice and Reload stay');
  assert.ok(!toasts.some(m => /unsaved entry/.test(m)), 'no discard claimed: nothing was discarded');
});

test('V1 (pass 16, DEMO2-R12-01) a 401 on a dialog form with a page draft beside it: the boundary keeps the dialog entry and takes the page down; the sign-in state renders', async () => {
  const { fetch } = hostedFixture(async (url, opts) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401);
    if (url === '/api/v1/session' && opts.method !== 'POST' && win && win.__out) return json({ ok: false, error: 'signin', signin: { available: true } }, false, 401);
    return null;
  });
  let win = null;
  let main = null;
  ({ win, main } = await viewsWith(fetch));
  win.location.hash = '#/settings';
  await win.EWViews.render();
  win.__out = true;
  const dialogForm = reviewerForm('settings-workspace', { dirty: true, entries: true });
  const dlg = dialogWith(win, dialogForm);
  const pageDraft = reviewerForm('invite-create', { dirty: true, entries: true });
  const base = win.document.querySelector;
  win.document.querySelector = sel => (sel === '#modal' ? dlg : base(sel));
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s)
    ? [pageDraft, dialogForm].filter(f => (f.dataset.dirty === '1' || f.dataset.held === '1') && (f === pageDraft || dlg.open)) : []);
  win.EW.current.dialogWorkspace = 'ctx-a'; win.EW.current.dialogEpoch = { rev: 5 };
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(dialogForm); await settleAll(40);
  const kept = [...win.EW.current.dialogDrafts].find(([k]) => k.startsWith('settings-workspace'));
  assert.ok(kept, 'the dialog entry was not kept');
  assert.equal(kept[1].values.workspace_name, 'A2');
  assert.equal(dlg.open, true, 'the dialog with typed entries closed');
  assert.match(dlg.innerHTML, /data-access-held/, 'the dialog shows the held notice');
  // DEMO2 pass 17 (R13-01): the re-read confirmed the sign-out, so the
  // notice says plainly that nothing typed here is kept across sign-in.
  assert.match(dlg.innerHTML, /You were signed out\./, 'the held notice does not say the user was signed out');
  assert.doesNotMatch(dlg.innerHTML, /typed text is kept/);
  assert.equal(pageDraft.dataset.held, undefined, 'a form on the old page was held instead');
  assert.match(main.innerHTML, /data-action="signin-start"/, 'the sign-in state is shown');
});

test('V3 brief registration from a stale dialog is refused before dispatch', async () => {
  const posts = [];
  const { fetch } = hostedFixture(async (url, opts) => { if (opts.method === 'POST') posts.push(url); return null; });
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('campaign', { entries: true });
  dialogWith(win, form);
  const fields = { '#brief-title': { value: 'Brief' }, '#brief-text': { value: 'Text' } };
  const q = form.querySelector;
  form.querySelector = sel => fields[sel] || q(sel);
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.dialogEpoch = { rev: 4 }; // opened before the current publication
  await win.EWViews.handleAction({ dataset: { action: 'brief-register' }, closest: () => form, disabled: false });
  assert.deepEqual(posts, [], 'no brief POST from the stale dialog');
  assert.match(form.box.innerHTML, /reload-workspace/);
});

test('V4 (pass 16, DEMO2-R12-01) a 401 whose re-read is still signed in: the boundary ran, no form on the old page is held, and the page is read again', async () => {
  const { state, fetch } = hostedFixture(async (url, opts) => (url === '/api/v1/customer/preferences' && opts.method === 'PUT'
    ? json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401) : null));
  const { win, main } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const confirm = reviewerForm('confirm');
  confirm.dataset = { held: '1' }; // e.g. a stale-dialog hold
  const form = reviewerForm('settings-workspace', { entries: true });
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s) ? [confirm, form].filter(f => f.dataset.held === '1') : []);
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const epoch = win.EW.authorityEpoch();
  const reads = state.calls.filter(c => c === 'GET /api/v1/customer/preferences').length;
  await win.EWViews.handleSubmit(form); await settleAll(40);
  assert.ok(win.EW.authorityEpoch() > epoch, 'the 401 started no boundary');
  assert.equal(confirm.box.innerHTML, '', 'a form on the old page was given a notice');
  assert.ok(state.calls.filter(c => c === 'GET /api/v1/customer/preferences').length > reads, 'the page was not read again');
  assert.doesNotMatch(main.innerHTML, /Checking access/, 'the page stayed on the boundary');
});

test('V5 a dropped late result on a freeze write never shows the read-in-flight copy', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts) => {
    if (url.endsWith('/reports') && opts.method === 'POST') { await gate; return json({ ok: true, report_no: 1, workspace_context: 'ctx-a' }); }
    return null;
  });
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/campaign/1';
  win.EW.current.route = '#/campaign/1';
  await win.EWViews.render();
  const form = reviewerForm('confirm');
  form.insertBefore = () => {};
  win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '1']; yield ['freeze_pins', '{}']; yield ['freeze_revision', '3']; } };
  const p = win.EWViews.handleSubmit(form);
  await settleAll(3);
  win.EW.current.workspaceContext = 'ctx-moved'; // the display moved while the write was out
  release(); await p;
  assert.doesNotMatch(form.box.innerHTML, /in flight/);
});

/* -- Round 7: sequence rule, fe15 reproductions, committed writes -- */
test('REV fe15-h1-hang: after late reads from an older window, a later switch is still re-checked on the next render', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const gates = []; let sessionReads = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { sessionReads++; return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev })); }
    if (url.startsWith('/api/v1/slow/')) { await new Promise(r => gates.push(r)); return json({ ok: true, workspace_context: 'ctx-b' }); }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Name ' + server.ws, revision: 1 } });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  [1, 2].forEach(i => win.EW.api('/slow/' + i).catch(() => {}));
  await settleAll(2);
  server = { ws: 'ws-a', ctx: 'ctx-a', rev: 7 }; // another tab A→B(6)→A(7)
  gates.shift()(); await settleAll(40);
  gates.shift()(); await settleAll(40);
  assert.match(main.innerHTML, /Name ws-a/);
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 8 }; // and then to B again
  const before = sessionReads;
  for (const r of ['#/overview', '#/brands', '#/settings', '#/overview']) {
    win.location.hash = r; win.EW.current.route = r;
    await win.EWViews.render(); await settleAll(40);
    assert.doesNotMatch(main.innerHTML, /Opening…|could not be loaded|response was dropped/, r);
    assert.equal(win.EW.current.workspaceContext, 'ctx-b', r);
  }
  assert.ok(sessionReads > before, 'the canonical session was re-read');
});

test('REV fe15-h2-close-stale: closing a held dialog over a stale page holds the page draft with Reload and names its workspace', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  const sent = [];
  const fetch = async (url, opts = {}) => {
    if (opts.method && opts.method !== 'GET') sent.push(opts.method + ' ' + url);
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
      return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409);
    }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: server.ws === 'ws-a' ? 'Alpha' : 'Beta', revision: 1 } });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const dform = reviewerForm('settings-workspace', { dirty: true, entries: true });
  const dlg = dialogWith(win, dform);
  const page = reviewerForm('invite-create', { entries: true });
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s)
    ? [page, dform].filter(f => (f.dataset.dirty === '1' || f.dataset.held === '1') && (f === page || dlg.open)) : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  main.innerHTML = 'SETTINGS-OF-WS-A';
  win.EW.current.dialogWorkspace = 'ctx-a'; win.EW.current.dialogEpoch = { rev: 5 }; win.EW.current.dialogRoute = '#/settings';
  win.EW.current.dialogWorkspaceName = 'ws-a';
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  await win.EWViews.handleSubmit(dform); await settleAll(40);
  page.dataset.dirty = '1'; // typed on the stale page
  await win.EWViews.handleAction({ dataset: { action: 'close-dialog' } }); await settleAll(40);
  assert.equal(page.dataset.held, '1', 'the stale page draft is held');
  assert.match(page.box.innerHTML, /Nothing was saved; your entries are kept here[\s\S]*reload-workspace/);
  assert.ok(toasts.includes('Your unsaved entry in ws-a was not applied.'), 'names the workspace it was typed in');
  win.FormData = class { *entries() { yield ['member', 'bob']; yield ['role', 'reader']; } };
  sent.length = 0;
  const notice = page.box.innerHTML;
  await win.EWViews.handleSubmit(page); await settleAll(20);
  assert.deepEqual(sent, [], 'the held page form sends nothing');
  assert.equal(page.box.innerHTML, notice, 'its notice stays');
});

test('W1 two dropped reads of one render join one reconcile; a reconcile that changed nothing never re-renders', async () => {
  let sessions = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { sessions++; return json(hostedSession()); }
    return json({ ok: true, workspace_context: 'ctx-b' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.EW.toast = () => {};
  await settleAll(40);
  const before = sessions, token = win.EW.current.fetchToken;
  // Overview issues several reads; every one is dropped (server says B).
  win.location.hash = '#/overview';
  await win.EWViews.render(); await settleAll(40);
  assert.equal(sessions - before, 1, 'one canonical read for one render generation');
  assert.equal(win.EW.current.fetchToken, token + 1, 'unchanged canonical state starts no further render');
  assert.match(main.innerHTML, /retry-workspace/, 'the route still ends with a way forward');
});

test('W2 a successful write is never dropped as late: it reports where it committed and renders nothing of it', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') { await gate; return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  const { win, modal, toasts } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('settings-workspace', { dirty: true, entries: true });
  dialogWith(win, form);
  win.EW.current.dialogWorkspace = 'ctx-a';
  let rendered = 0;
  win.EW.render = () => { rendered++; };
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const p = win.EWViews.handleSubmit(form);
  await settleAll(3);
  win.EW.current.workspaceContext = 'ctx-b'; // the view moved on meanwhile
  release(); await p;
  assert.ok(toasts.includes('Saved in ws-a.'), toasts.join(' | '));
  assert.ok(!toasts.some(m => /Nothing was changed|in flight/.test(m)));
  assert.equal(rendered, 0, 'nothing of it rendered into the current view');
  assert.equal(form.dataset.dirty, undefined, 'no longer a draft');
  void modal;
});

test('W2b a committed freeze after the view moved reports where it committed and never navigates there', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts) => {
    if (url.endsWith('/reports') && opts.method === 'POST') { await gate; return json({ ok: true, report_no: 3, workspace_context: 'ctx-a' }); }
    return null;
  });
  const { win, toasts } = await viewsWith(fetch);
  win.location.hash = '#/campaign/1'; win.EW.current.route = '#/campaign/1';
  await win.EWViews.render();
  const form = reviewerForm('confirm');
  form.insertBefore = () => {};
  let navigated = null;
  win.EW.go = (r) => { navigated = r; };
  win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '1']; yield ['freeze_pins', '{}']; yield ['freeze_revision', '3']; } };
  const p = win.EWViews.handleSubmit(form);
  await settleAll(3);
  win.EW.current.workspaceContext = 'ctx-b';
  release(); await p;
  assert.ok(toasts.includes('Saved in ws-a.'));
  assert.equal(navigated, null);
});

test('W3 a read queued during a settling switch keeps its render ownership and sequence', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { st.ws = 'b'; st.rev = 6; return json(sessB()); }
    if (url === '/api/v1/session' && st.ws === 'b') { await gate; return null; }
    if (url === '/api/v1/customer/late') return json({ ok: true, workspace_context: 'ctx-z' });
    return null;
  });
  const { win } = await viewsWith(fetch);
  win.EW.render = () => {}; win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(4);
  const token = win.EW.current.fetchToken, seq = win.EW.current.reconcileSeq || 0;
  await win.EW.api('/customer/late').catch(() => {});
  await settleAll(4);
  const list = win.EW.current.queuedServerContext;
  assert.ok(list && list.length, 'queued while settling');
  const q = list[list.length - 1];
  assert.equal(q.origin.token, token, 'route ownership carried');
  assert.equal(q.origin.seq, seq, 'dispatch sequence carried');
  release(); await sw;
});

test('W4 (pass 16, DEMO2-R12-01) a 401 whose re-read is signed in, same workspace and revision: the page is read again in place; the follow-up adds no sign-out claim', async () => {
  const { state, fetch } = hostedFixture(async (url, opts) => (url === '/api/v1/customer/preferences' && opts.method === 'PUT'
    ? json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'x' } }, false, 401) : null));
  const { win, main, toasts } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('settings-workspace', { entries: true });
  win.document.querySelectorAll = (s) => (/data-dirty|data-held/.test(s) && form.dataset.held === '1' ? [form] : []);
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const reads = state.calls.filter(c => c === 'GET /api/v1/customer/preferences').length;
  await win.EWViews.handleSubmit(form); await settleAll(40);
  assert.equal(win.EW.current.activeWorkspace, 'ws-a');
  assert.ok(win.EW.current.principal, 'the re-read signed the session out');
  assert.ok(state.calls.filter(c => c === 'GET /api/v1/customer/preferences').length > reads, 'the page was not read again');
  assert.doesNotMatch(main.innerHTML, /signin-start|Checking access/);
  assert.ok(!toasts.some(t => /workspace changed/i.test(t)), toasts.join(' | '));
});

test('W5 an unconfirmed switch is described plainly', async () => {
  const body = (obj) => ({ ok: true, status: 200, text: async () => JSON.stringify(obj) });
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return body({ ok: true, active_workspace: 'ws-b' });
    if (url === '/api/v1/session') return body({ ok: true, authenticated: false, csrf_token: 'c'.repeat(32), workspace_context: 'ctx-a', workspace_switch_revision: 5, frontend_contract: 'earwitness/customer-ui/2' });
    return body({ ok: true });
  };
  const win = loadViews(fetch); await flushBoot(); mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  const all = toasts.join(' ') + ' ' + win.EW.current.bootRefused;
  assert.match(all, /The workspace switch could not be confirmed\./);
  assert.doesNotMatch(all, /stays held|unconfirmed|revision \d|admitted/i);
});

test('W6 a selection change never lifts a fence hold on Export selected', async () => {
  const win = loadViews(canonicalSessionFetch()); await flushBoot();
  const calls = [];
  const btn = { textContent: '', dataset: { fenceHeld: '1' }, removeAttribute: (a) => calls.push('remove ' + a), setAttribute: (a) => calls.push('set ' + a) };
  win.document.querySelector = sel => (sel === '[data-action="export-selected"]' ? btn : null);
  win.EWViews.setSelection('#/search', ['ev-1'], 'r1');
  assert.ok(!calls.includes('remove disabled'), 'still held');
  btn.dataset = {};
  win.EWViews.setSelection('#/search', ['ev-1'], 'r1');
  assert.ok(calls.includes('remove disabled'), 'enabled once the hold is gone');
});

/* -- Round 8: fe16 reproductions; late write outcomes; generation coverage -- */
const lateWriteWin = async (writePart, writeBody, sessionExtra = {}) => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gate; const sent = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ ...sessionExtra, active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method && opts.method !== 'GET' && url.includes(writePart)) {
      sent.push(url);
      await new Promise(r => { gate = r; });
      return typeof writeBody === 'function' ? writeBody() : json({ ok: true, ...writeBody });
    }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m);
  const hrefs = [];
  win.location = { hash: '#/settings', set href(v) { hrefs.push(v); }, get href() { return ''; } };
  win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const moveToB = async () => { server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; await win.EW.boot(); await settleAll(5); };
  return { win, main, toasts, hrefs, sent, moveToB, release: () => gate() };
};

test('REV fe16-h1-attrib: a successful write whose display moved is announced once as information; no caller acts on its payload', async () => {
  const mk = (action, ds = {}) => ({ dataset: { action, ...ds }, closest: () => null, setAttribute() {} });
  const cases = [
    ['pause-alert', '/lifecycle', {}, mk('pause-alert', { id: 'al1', rev: '3', lifecycle: 'active_local' })],
    ['check-alert', '/check', { emitted: 4 }, mk('check-alert', { id: 'al1' })],
    ['share-revoke', '/shares/', {}, mk('share-revoke', { share: 'sh1' })],
    ['billing-portal', '/billing/portal', { portal_url: 'https://billing.example/A-portal' }, mk('billing-portal')],
  ];
  for (const [name, part, body, el] of cases) {
    const t = await lateWriteWin(part, body);
    const p = t.win.EWViews.handleAction(el).catch(e => t.toasts.push('THROW ' + e.message));
    await settleAll(5);
    await t.moveToB();
    t.release(); await p; await settleAll(20);
    assert.equal(t.sent.length, 1, name);
    assert.deepEqual(t.toasts, ['Saved in ws-a.'], name + ': information only, never "failed", never A payload copy');
    assert.deepEqual(t.hrefs, [], name + ': no navigation from a late outcome');
  }
});

// UAT-093: Detach now writes from its confirmation, so its late success
// is checked through the confirm form (the click only opens it).
test('REV fe16-h1-attrib: a late Detach confirmation is announced once as information, with no navigation', async () => {
  const t = await lateWriteWin('/evidence', {});
  const values = { confirm_action: 'detach-evidence', confirm_id: '1', detach_pid: '2', detach_ref: 'r1', expected_revision: '1' };
  t.win.FormData = class { entries() { return Object.entries(values); } };
  const submit = { textContent: 'Detach it', disabled: false };
  const form = { dataset: {}, getAttribute: a => (a === 'id' ? 'confirm' : null), closest: () => null,
    querySelector: s => (s === '[type=submit]' ? submit : null), querySelectorAll: () => [submit], elements: [{ type: 'hidden' }] };
  const p = t.win.EWViews.handleSubmit(form).catch(e => t.toasts.push('THROW ' + e.message));
  await settleAll(5);
  await t.moveToB();
  t.release(); await p; await settleAll(20);
  assert.equal(t.sent.length, 1);
  assert.deepEqual(t.toasts, ['Saved in ws-a.'], 'information only, never "failed"');
  assert.deepEqual(t.hrefs, [], 'no navigation from a late outcome');
});

test('REV fe16-h2-dialog: a late success never closes or clears a dialog opened since', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gate;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method === 'POST' && url.includes('/invites')) { await new Promise(r => { gate = r; }); return json({ ok: true, invite: { id: 'i1' } }); }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  win.FormData = class { constructor(f) { this.f = f; } entries() { return Object.entries(this.f._values); } };
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const mkForm = (id, values, dlg) => { const submit = { textContent: 'Send', disabled: false };
    return { _values: values, dataset: { dirty: '1' }, getAttribute: a => (a === 'id' ? id : null), closest: s => (s === '#modal' ? dlg : null),
      querySelector: s => (s === '[type=submit]' ? submit : null), querySelectorAll: () => [submit], elements: [{ type: 'text', disabled: false }], submit }; };
  const dlg = { open: true, forms: [], close() { this.open = false; }, querySelectorAll() { return this.forms.filter(f => f.dataset.dirty || f.dataset.held); }, querySelector: () => null, setAttribute() {}, showModal() { this.open = true; } };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const c = win.EW.current;
  c.dialogWorkspace = 'ctx-a'; c.dialogEpoch = { rev: 5 };
  const formA = mkForm('invite-create', { member: 'pat@example.com', role: 'viewer' }, dlg); dlg.forms = [formA];
  const p = win.EWViews.handleSubmit(formA); await settleAll(5);
  dlg.open = false; dlg.forms = [];
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; await win.EW.boot(); await win.EWViews.render(); await settleAll(10);
  c.dialogWorkspace = 'ctx-b'; c.dialogEpoch = { rev: c.displayedWorkspaceSwitchRevision };
  const formB = mkForm('invite-create', { member: 'bob-typed-in-B', role: 'viewer' }, dlg); dlg.forms = [formB]; dlg.open = true;
  gate(); await p; await settleAll(10);
  assert.equal(dlg.open, true, 'the B dialog stays open');
  assert.equal(formB.dataset.dirty, '1', 'its draft is untouched');
  assert.deepEqual(toasts, ['Saved in ws-a.']);
  assert.equal(formA.dataset.dirty, undefined, 'the dispatching A form is saved, not a draft');
});

test('REV fe16-h3-inconsistent: an action read on a published page that keeps disagreeing tells the user, once per generation', async () => {
  let sessionReads = 0, readCtx = 'ctx-a';
  const fetch = async (url) => {
    if (url === '/api/v1/session') { sessionReads++; return json(hostedSession()); }
    if (url.startsWith('/api/v1/customer/alerts/')) return json({ ok: true, workspace_context: 'ctx-b', alert: { id: 'al1', revision: 1, query: {}, events: [] } });
    return json({ ok: true, workspace_context: readCtx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {};
  win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  for (let i = 0; i < 3; i++) {
    const before = sessionReads;
    main.innerHTML = 'PUBLISHED-SETTINGS';
    await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'al1' }, closest: () => null }).catch(() => {});
    await settleAll(20);
    assert.ok(sessionReads - before <= (i === 0 ? 1 : 0), 'at most one re-check for this page generation');
    assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*could not be loaded[\s\S]*retry-workspace[\s\S]*PUBLISHED-SETTINGS/, 'told, page kept');
  }
  readCtx = 'ctx-b';
  for (let i = 0; i < 2; i++) {
    const before = sessionReads;
    await win.EWViews.render(); await settleAll(30);
    assert.equal(sessionReads - before, 1, 'one re-check per render');
    assert.match(main.innerHTML, /could not be loaded[\s\S]*retry-workspace/);
  }
});

test('REV fe16-h4-join: a read that finds an older reconcile in flight waits for it, then re-checks itself', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let sessGate = null, sessionReads = 0, holdSession = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') {
      sessionReads++;
      const snap = { ...server };
      if (holdSession) { holdSession = false; await new Promise(r => { sessGate = r; }); }
      return json(hostedSession({ active_workspace: snap.ws, workspace_context: snap.ctx, workspace_switch_revision: snap.rev }));
    }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Name ' + server.ws, revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {};
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  holdSession = true;
  win.EWViews.reconcileCanonical(); // served at A, delivered late
  await settleAll(3);
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
  const before = sessionReads;
  const r = win.EWViews.render();
  await settleAll(10);
  sessGate(); await r; await settleAll(30);
  assert.ok(sessionReads - before >= 1, 'a fresh re-check after the older one (Settings also reads its access section)');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.match(main.innerHTML, /Name ws-b/);
});

test('REV fe16-h5-token-limit: a failed re-check does not cover its page; the next dropped read re-checks and recovers', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 }, failNextSession = false, sessionReads = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { sessionReads++; if (failNextSession) { failNextSession = false; throw new TypeError('network'); }
      return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev })); }
    if (url.startsWith('/api/v1/customer/alerts/')) return json({ ok: true, workspace_context: server.ctx, alert: { id: 'al1', revision: 1, query: {}, events: [] } });
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Name ' + server.ws, revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {};
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
  failNextSession = true;
  let before = sessionReads;
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'al1' }, closest: () => null }).catch(() => {});
  await settleAll(20);
  assert.equal(sessionReads - before, 1);
  assert.match(main.innerHTML, /could not be loaded[\s\S]*retry-workspace/, 'the failed re-check is reported');
  before = sessionReads;
  await win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: 'al1' }, closest: () => null }).catch(() => {});
  await settleAll(20);
  assert.ok(sessionReads - before >= 1, 'a second re-check runs');
  assert.equal(win.EW.current.workspaceContext, 'ctx-b');
  assert.equal(win.EW.current.displayedWorkspaceSwitchRevision, 6);
  assert.match(main.innerHTML, /Name ws-b/);
});

test('X5 a failed write that arrives after the display moved is announced: rejected vs unknown', async () => {
  for (const [label, respond, copy] of [
    ['4xx', () => json({ ok: false, error: { code: 'VALIDATION', message: 'bad' } }, false, 422), 'Your change in ws-a was not saved.'],
    ['5xx', () => json({ ok: false, error: 'boom' }, false, 503), 'Your change in ws-a could not be confirmed.'],
    ['transport', () => { throw new TypeError('Failed to fetch'); }, 'Your change in ws-a could not be confirmed.'],
  ]) {
    const t = await lateWriteWin('/lifecycle', respond);
    const p = t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', id: 'al1', rev: '3', lifecycle: 'active_local' }, closest: () => null })
      .catch(e => t.toasts.push('THROW ' + e.message));
    await settleAll(5);
    await t.moveToB();
    t.release(); await p; await settleAll(20);
    assert.deepEqual(t.toasts, [copy], label);
  }
});

test('X6 several reads queued during a settling switch each keep their own ownership', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { st.ws = 'b'; st.rev = 6; return json(sessB()); }
    if (url === '/api/v1/session' && st.ws === 'b') { await gate; return null; }
    if (url.startsWith('/api/v1/customer/late')) return json({ ok: true, workspace_context: 'ctx-z' });
    return null;
  });
  const { win } = await viewsWith(fetch);
  win.EW.render = () => {}; win.EW.closeDialog = () => {};
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(4);
  const t1 = win.EW.current.fetchToken;
  await win.EW.api('/customer/late1').catch(() => {});
  win.EW.current.fetchToken += 1; // a newer render dispatches the second read
  await win.EW.api('/customer/late2').catch(() => {});
  await settleAll(4);
  const list = win.EW.current.queuedServerContext;
  assert.deepEqual(plain(list.map(q => q.origin.token)), [t1, t1 + 1], 'neither ownership overwritten');
  release(); await sw;
});

test('X7 an exact guest entry never reconciles the member session or paints the member unavailable state', async () => {
  let sessionReads = 0;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { sessionReads++; return json(hostedSession()); }
    if (url.startsWith('/api/v1/shared/')) return json({ ok: true, title: 'Shared', text: 'granted', workspace_context: 'ctx-share' });
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot(); const main = mountMain(win);
  win.EW.toast = () => {};
  win.EW.current.route = '#/shared?workspace=ws&share_id=s1&type=evidence&id=e1';
  win.location.hash = win.EW.current.route;
  const before = sessionReads;
  await win.EW.api('/shared/evidence/e1?workspace=ws&share_id=s1').catch(() => {});
  await settleAll(20);
  assert.equal(sessionReads, before, 'no member session reconcile from a guest read');
  assert.doesNotMatch(main.innerHTML, /could not be loaded/);
});

/* -- Round 9: in-flight write tracking and late outcomes on forms -- */
const inlineForm = (id = 'settings-workspace') => {
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const submit = { type: 'submit', textContent: 'Save', disabled: false };
  return { box, submit, dataset: { dirty: '1' }, getAttribute: a => (a === 'id' ? id : null), closest: () => null,
    querySelector: s => (s === '[type=submit]' ? submit : s === '#form-errors' ? box : null),
    querySelectorAll: s => (s === '[type=submit]' ? [submit] : []), elements: [{ tagName: 'INPUT', type: 'text', disabled: false }, submit] };
};
const heldLateWin = async (respond) => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gate; const sent = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method === 'PUT' && url.includes('/customer/preferences')) { sent.push(url); await new Promise(r => { gate = r; }); return respond(); }
    if (url.includes('/customer/probe')) return json({ ok: true, workspace_context: server.ctx });
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const form = inlineForm();
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.EW.render = () => {};
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; // another tab switched
  await win.EW.api('/customer/probe').catch(() => {}); await settleAll(30);
  gate(); await p; await settleAll(10);
  return { win, form, toasts, sent };
};

test('REV fe17-c1: a late commit on a form whose basis moved says it was saved, with Reload, and never resubmits', async () => {
  const { win, form, toasts, sent } = await heldLateWin(() => json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 2 } }));
  assert.ok(toasts.includes('Saved in ws-a.'));
  assert.match(form.box.innerHTML, /Saved in ws-a\. Reload the workspace to continue\.[\s\S]*reload-workspace/);
  assert.doesNotMatch(form.box.innerHTML, /kept here|ordering basis/);
  assert.equal(form.submit.disabled, true, 'the basis is still invalid');
  assert.equal(form.dataset.dirty, undefined, 'saved, so no longer a draft');
  const before = sent.length;
  await win.EWViews.handleSubmit(form); await settleAll(5);
  assert.equal(sent.length, before, 'never resubmitted');
  // A later Reload announces no lost entry: it was saved.
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) ? [form] : []);
  toasts.length = 0;
  await win.EWViews.handleAction({ dataset: { action: 'reload-workspace' } });
  assert.ok(!toasts.some(m => /not applied/.test(m)), toasts.join(' | '));
});

test('REV fe17-c2: a late rejection on a held form keeps it held, says it was not saved, and offers Reload', async () => {
  const { win, form, toasts, sent } = await heldLateWin(() => json({ ok: false, error: { code: 'STALE', message: 'stale' } }, false, 412));
  assert.ok(toasts.includes('Your change in ws-a was not saved.'));
  assert.match(form.box.innerHTML, /Your change in ws-a was not saved\. Reload the workspace to continue\.[\s\S]*reload-workspace/);
  assert.equal(form.submit.disabled, true);
  assert.equal(form.dataset.held, '1');
  const before = sent.length;
  await win.EWViews.handleSubmit(form); await settleAll(5);
  assert.equal(sent.length, before);
  assert.doesNotMatch(form.box.innerHTML, /ordering basis/);
});

test('REV fe17-c3: Discard-and-switch waits for an in-flight write; no outcome is lost and no false "Nothing was saved"', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gateW; const order = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') { order.push('switch'); server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method === 'POST' && url.includes('/check')) { await new Promise(r => { gateW = r; }); order.push('check'); return json({ ok: true, emitted: 2, workspace_context: 'ctx-a' }); }
    return json({ ok: true, workspace_context: server.ctx });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {};
  const log = []; win.EW.toast = m => log.push(m);
  win.EW.current.route = '#/alerts'; win.location.hash = '#/alerts';
  await win.EWViews.render(); await settleAll();
  win.EW.closeDialog = () => {};
  const pc = win.EWViews.handleAction({ dataset: { action: 'check-alert', id: 'al1' }, closest: () => null, setAttribute() {} });
  await settleAll(5);
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(10);
  assert.deepEqual(order, [], 'the switch waits while the write is in flight');
  gateW(); await pc; await sw; await settleAll(20);
  assert.deepEqual(order, ['check', 'switch']);
  assert.ok(log.includes('2 new matches.'), log.join(' | '));
  assert.ok(log.some(m => /^Switched to ws-b\. Unsaved changes were discarded\.$/.test(m)), log.join(' | '));
  assert.ok(!log.some(m => /Nothing was saved to ws-a/.test(m)));
});

test('REV fe17-c4: a draft submitted before Discard commits first; the switch copy stays truthful', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gateW; const order = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') { order.push('switch'); server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method === 'PUT' && url.includes('/customer/preferences')) { await new Promise(r => { gateW = r; }); order.push('write@' + opts.headers['X-EarWitness-Workspace-Switch-Revision']); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const form = inlineForm();
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const log = []; win.EW.toast = m => log.push(m);
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.EW.closeDialog = () => {};
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  win.EW.current.pendingWorkspace = 'ws-b';
  const sw = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(10);
  gateW(); await p; await sw; await settleAll(20);
  assert.deepEqual(order, ['write@5', 'switch'], 'commit settles before the switch dispatches');
  assert.ok(!log.some(m => /Nothing was saved to ws-a/.test(m)), log.join(' | '));
});

test('REV fe17-c5: a form mid-save is not a draft; the chooser waits for it and Save-in-current never sends it twice', async () => {
  const sent = []; const gates = []; let switched = false;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') { switched = true; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return json(switched ? hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hostedSession());
    if (opts.method === 'POST' && url.endsWith('/invites')) { sent.push(opts.body); await new Promise(r => gates.push(r)); return json({ ok: true, workspace_context: 'ctx-a', invite_id: 'i' + sent.length }); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => ({ focus() {} }), querySelectorAll: () => [] };
  const form = inlineForm('invite-create');
  form.isConnected = true; form.contains = () => false;
  form.requestSubmit = () => win.EWViews.handleSubmit(form);
  const status = { textContent: '' };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? status : s === '[type=submit]' ? { disabled: false, textContent: 'Use workspace' } : null),
    querySelectorAll: () => [], elements: [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const log = []; win.EW.toast = m => log.push(m); win.EW.saveNotice = () => {};
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.EW.render = () => {};
  win.FormData = class { constructor(f) { this.f = f; } *entries() { if (this.f === choice) yield ['workspace_id', 'ws-b']; else { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } } };
  const p1 = win.EWViews.handleSubmit(form); await settleAll(5);
  assert.equal(form.dataset.saving, '1', 'marked mid-save');
  const pc = win.EWViews.handleSubmit(choice); await settleAll(5);
  assert.equal(status.textContent, 'Finishing your change…', 'chooser waits, saying so');
  assert.doesNotMatch(modal.innerHTML, /Unsaved changes/, 'a saving form is not a draft');
  // Even if a prompt referenced the saving form, Save-in-current would not resubmit it.
  win.EW.current.dirtyForm = form; win.EW.current.dirtyWorkspace = 'ctx-a';
  await win.EWViews.handleAction({ dataset: { action: 'switch-save' } }); await settleAll(5);
  assert.equal(sent.length, 1, 'no duplicate invite');
  for (const g of gates) g();
  await p1; await pc; await settleAll(10);
  assert.equal(sent.length, 1);
  assert.equal(status.textContent, '');
  assert.ok(log.some(m => /^Switched to ws-b\./.test(m)), 'the switch proceeds once the write settled');
});

test('REV fe17-c6: a late commit of an allocation confirmation never claims kept entries or announces a lost draft', async () => {
  let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
  let gate; const sent = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
    if (opts.method === 'POST' && url.includes('/allocation')) { sent.push(url); await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', action: 'move', binding: 'moved' }); }
    if (url.includes('/customer/probe')) return json({ ok: true, workspace_context: server.ctx });
    return json({ ok: true, workspace_context: server.ctx, campaign: { revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const form = inlineForm('allocation-confirm');
  form.elements = [{ type: 'radio', disabled: false }, form.submit];
  const dlg = { open: true, close() { this.open = false; }, querySelectorAll: (q) => (/held|dirty/.test(q) && (form.dataset.held || form.dataset.dirty) ? [form] : []), querySelector: (q) => (/held/.test(q) && form.dataset.held ? form : null), setAttribute() {}, showModal() { this.open = true; } };
  form.closest = s => (s === '#modal' ? dlg : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : null);
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.route = '#/campaign/1'; win.location.hash = '#/campaign/1';
  await win.EWViews.render(); await settleAll();
  const c = win.EW.current; c.dialogWorkspace = 'ctx-a'; c.dialogEpoch = { rev: c.displayedWorkspaceSwitchRevision }; c.dialogRoute = '#/campaign/1';
  win.FormData = class { *entries() { for (const kv of [['campaign_id', '1'], ['allocation_action', 'move'], ['allocation_ref', 'r1'], ['from_target_id', 'p1'], ['to_target_id', 'p2'], ['expected_campaign_revision', '1'], ['expected_from_revision', '1'], ['units_json', '{"p2":1}']]) yield kv; } };
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 };
  await win.EW.api('/customer/probe').catch(() => {}); await settleAll(30);
  gate(); await p; await settleAll(10);
  assert.ok(toasts.includes('Saved in ws-a.'));
  assert.doesNotMatch(form.box.innerHTML, /kept here/);
  const before = sent.length;
  await win.EWViews.handleSubmit(form); await settleAll(5);
  assert.equal(sent.length, before, 'never re-dispatched');
  dlg.open = true;
  win.EW.render = async () => {};
  await win.EWViews.handleAction({ dataset: { action: 'close-dialog' } }); await settleAll(3);
  assert.ok(!toasts.some(m => /not applied/.test(m)), toasts.join(' | '));
});

test('Y1 an outcome interrupting a late-write notice shows at once, and the notice follows: nothing is lost', async () => {
  const win = loadAt('#/overview');
  const el = { textContent: '' };
  win.document.querySelector = sel => (sel === '#toast' ? el : null);
  const timers = [];
  win.setTimeout = (fn) => { timers.push(fn); return timers.length; };
  win.clearTimeout = () => {};
  win.EW.toast('Saved in ws-a.', { low: true });
  win.EW.toast('Switched to ws-b.');
  assert.equal(el.textContent, 'Switched to ws-b.', 'the outcome is shown immediately');
  timers.pop()();
  assert.equal(el.textContent, 'Saved in ws-a.', 'the interrupted notice follows');
  timers.pop()();
  assert.equal(el.textContent, '');
});

test('Y2 dropped-read marks are kept per render, so an older drop never unmarks the current render', async () => {
  const fetch = async (url) => (url === '/api/v1/session' ? json(hostedSession()) : json({ ok: true, workspace_context: 'ctx-b' }));
  const win = loadViews(fetch); await flushBoot(); mountMain(win);
  win.EW.toast = () => {};
  win.EW.current.fetchToken = 40;
  win.EW.api('/customer/one').catch(() => {});
  win.EW.current.fetchToken = 41;
  win.EW.api('/customer/two').catch(() => {});
  await settleAll(3);
  const marks = win.EW.current.droppedTokens;
  assert.ok(marks.has(40) && marks.has(41), 'both renders stay marked');
});

test('Y3 guest invite acceptance is never treated as a late member write', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const win = loadAt('#/invite?workspace=w&id=i', { fetch: async () => { await gate; return json({ ok: true }); } });
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.hosted = true;
  win.EW.current.workspaceContext = 'ctx-a';
  const p = win.EW.api('/workspaces/w/invites/i/accept', { method: 'POST', body: {} }).then(() => null, e => e);
  win.EW.current.workspaceContext = 'ctx-b';
  release();
  const err = await p;
  assert.ok(err && err.droppedContext && !err.lateWrite, 'a plain superseded result, not a member-write outcome');
  assert.deepEqual(toasts, []);
});

test('Y4 local mode late-write copy names no workspace', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const win = loadAt('#/overview', { fetch: async () => { await gate; return json({ ok: true }); } });
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.workspaceContext = 'ctx-local';
  const p = win.EW.api('/customer/alerts/1/check', { method: 'POST' }).then(() => null, e => e);
  win.EW.current.workspaceContext = 'ctx-other';
  release();
  const err = await p;
  assert.equal(err.message, 'Saved.');
  assert.deepEqual(toasts, ['Saved.']);
});

test('Y5 an out-of-date write never shows the internal "ordering basis" wording and offers Reload', async () => {
  const { fetch } = hostedFixture();
  const { win, main } = await viewsWith(fetch);
  main.insertAdjacentHTML = (p, h) => { main.innerHTML = h + main.innerHTML; };
  win.EW.current.displayedWorkspaceSwitchRevision = undefined;
  const err = await win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  assert.equal(err.message, 'Nothing was saved. Reload the workspace to continue.');
  assert.doesNotMatch(err.message, /ordering basis/);
  assert.match(main.innerHTML, /data-reconcile-notice[\s\S]*reload-workspace/);
});

/* -- Round 10: draft decisions after saves settle; saving forms are held drafts -- */
for (const STATUS of [412, 422]) {
  test(`REV fe18-r1-${STATUS}: a save that fails while the chooser waits is prompted, never switched away`, async () => {
    let gateW; const order = [];
    let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
    const fetch = async (url, opts = {}) => {
      if (url === '/api/v1/session/workspace') { order.push('SWITCH-POST'); server = { ws: 'ws-b', ctx: 'ctx-b', rev: 6 }; return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
      if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
      if (opts.method === 'PUT') { order.push('PUT'); await new Promise(r => { gateW = r; });
        return json({ ok: false, workspace_context: 'ctx-a', error: { code: 'X', message: 'Name rejected by server', details: { current_revision: 2 } } }, false, STATUS); }
      return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
    };
    const win = loadViews(fetch); await settleAll();
    win.setTimeout = setTimeout; // openDialog focuses on a timer
    const main = { innerHTML: '', querySelectorAll: () => [] };
    const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => ({ focus() {} }), querySelectorAll: () => [] };
    const form = inlineForm();
    form.isConnected = true; form.contains = () => false;
    const status = { textContent: '' };
    const chooseBtn = { disabled: false, textContent: 'Use workspace' };
    const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
      querySelector: s => (s === '[data-switch-status]' ? status : s === '[type=submit]' ? chooseBtn : null), querySelectorAll: () => [], elements: [] };
    win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
    win.document.querySelectorAll = s => (/data-dirty/.test(s) && form.dataset.dirty === '1' ? [form] : []);
    win.EW.header = () => {}; win.scrollTo = () => {};
    const log = []; win.EW.toast = m => log.push(m);
    win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
    await win.EWViews.render(); await settleAll();
    win.FormData = class { constructor(f) { this.f = f; } *entries() { if (this.f === choice) yield ['workspace_id', 'ws-b']; else { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } } };
    const p1 = win.EWViews.handleSubmit(form); await settleAll(5);
    const pc = win.EWViews.handleSubmit(choice); await settleAll(5);
    assert.equal(status.textContent, 'Finishing your change…');
    assert.equal(chooseBtn.disabled, true, 'Choose is disabled while waiting');
    gateW(); await p1; await pc; await settleAll(20);
    assert.deepEqual(order, ['PUT'], 'no switch dispatched over the failed draft');
    assert.match(modal.innerHTML, /Unsaved changes/, 'Save/Discard/Cancel offered');
    assert.equal(form.dataset.dirty, '1');
    assert.equal(win.EW.current.activeWorkspace, 'ws-a');
    assert.equal(chooseBtn.disabled, false);
  });
}

for (const W2 of [412, 409]) {
  test(`REV fe18-r2-${W2}: a reconcile never re-renders over a form whose save is in flight; its outcome lands in the form`, async () => {
    let gateW; let server = { ws: 'ws-a', ctx: 'ctx-a', rev: 5 };
    const fetch = async (url, opts = {}) => {
      if (url === '/api/v1/session') return json(hostedSession({ active_workspace: server.ws, workspace_context: server.ctx, workspace_switch_revision: server.rev }));
      if (opts.method === 'PUT') { await new Promise(r => { gateW = r; });
        return W2 === 409 ? json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409)
          : json({ ok: false, error: { code: 'STALE', message: 'stale', details: { current_revision: 2 } } }, false, W2); }
      if (opts.method === 'POST' && url.includes('/lifecycle')) return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409);
      return json({ ok: true, workspace_context: server.ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
    };
    const win = loadViews(fetch); await settleAll();
    const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
    const form = inlineForm();
    win.document.querySelector = sel => (sel === '#main' ? main : null);
    win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
    win.EW.header = () => {}; win.scrollTo = () => {};
    const log = []; win.EW.toast = m => log.push(m);
    win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
    await win.EWViews.render(); await settleAll();
    main.innerHTML = 'SETTINGS-PAGE-WITH-FORM';
    win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
    const p = win.EWViews.handleSubmit(form); await settleAll(5);
    server = { ws: 'ws-a', ctx: 'ctx-a', rev: 7 }; // another tab: A→B→A
    await win.EW.api('/customer/alerts/a1/lifecycle', { method: 'POST', body: {} }).catch(() => {});
    await settleAll(20);
    assert.match(main.innerHTML, /SETTINGS-PAGE-WITH-FORM/, 'the page holding the saving form is not replaced');
    assert.equal(form.dataset.held, '1', 'the saving form with entries is held');
    gateW(); await p; await settleAll(20);
    assert.match(form.box.innerHTML, W2 === 409 ? /your entries are kept here/ : /Your entries are preserved/, 'the outcome is painted into the still-attached form');
    assert.equal(form.submit.disabled, true, 'it stays held');
    assert.equal(form.dataset.dirty, '1', 'its entries are still a draft');
  });
}

test('Z1 Discard covers only the drafts it showed: a save that failed meanwhile is asked about again', async () => {
  let gateW; const order = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') { order.push('SWITCH-POST'); return json(sessB()); }
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'PUT') { await new Promise(r => { gateW = r; }); return json({ ok: false, error: { code: 'X', message: 'bad' } }, false, 422); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  win.setTimeout = setTimeout; // openDialog focuses on a timer
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => ({ focus() {} }), querySelectorAll: () => [] };
  const shown = inlineForm('invite-create');
  const saving = inlineForm('settings-workspace');
  saving.dataset = {};
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = s => (/data-dirty/.test(s) ? [shown, saving].filter(f => f.dataset.dirty === '1') : []);
  win.EW.header = () => {}; win.scrollTo = () => {};
  const log = []; win.EW.toast = m => log.push(m);
  win.EW.current.route = '#/settings'; win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.FormData = class { *entries() { yield ['workspace_name', 'A2']; yield ['expected_revision', '1']; } };
  const p = win.EWViews.handleSubmit(saving); await settleAll(5); // its save is in flight
  win.EW.current.pendingWorkspace = 'ws-b';
  win.EW.current.promptDrafts = [shown]; // the prompt showed only this draft
  saving.dataset.dirty = '1'; // still holds entries while saving
  const d = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(5);
  gateW(); await p; await d; await settleAll(10);
  assert.deepEqual(order, [], 'no switch over a draft the user never chose to discard');
  assert.match(modal.innerHTML, /Unsaved changes/);
});

test('Z4 toasts: ordinary messages replace each other at once and are never dropped; notices queue, capped, behind them', async () => {
  const win = loadAt('#/overview');
  const el = { textContent: '' };
  const seen = [];
  Object.defineProperty(el, 'textContent', { get() { return this._t || ''; }, set(v) { this._t = v; if (v) seen.push(v); } });
  win.document.querySelector = sel => (sel === '#toast' ? el : null);
  let pending = null;
  win.setTimeout = (fn) => { pending = fn; return 1; };
  win.clearTimeout = () => { pending = null; };
  const drain = () => { while (pending) { const f = pending; pending = null; f(); } };
  // Ordinary flow: one immediate message; a newer one replaces it.
  win.EW.toast('Saved.');
  win.EW.toast("Saved. These are proposals only; nobody's access changes.");
  assert.equal(el.textContent, "Saved. These are proposals only; nobody's access changes.");
  drain();
  assert.deepEqual(seen, ['Saved.', "Saved. These are proposals only; nobody's access changes."], 'no stale message shown after the newer one');
  // Notices queue behind (capped at five, oldest dropped); ordinary messages are never dropped.
  seen.length = 0;
  win.EW.toast('X');
  for (let i = 0; i < 8; i++) win.EW.toast('n' + i, { low: true });
  win.EW.toast('Signed out.');
  win.EW.toast('n7', { low: true }); // not an immediate repeat of the last submitted message
  drain();
  assert.deepEqual(seen, ['X', 'Signed out.', 'n4', 'n5', 'n6', 'n7', 'n7']);
  // Only an immediate repeat is dropped.
  seen.length = 0;
  win.EW.toast('Same.'); win.EW.toast('Same.');
  drain();
  assert.deepEqual(seen, ['Same.']);
});

test('Z5 a switch whose session gave no ordering basis says so plainly, with Reload', async () => {
  const { fetch } = hostedFixture();
  const { win, main } = await viewsWith(fetch);
  main.insertAdjacentHTML = (p, h) => { main.innerHTML = h + main.innerHTML; };
  win.EW.current.workspaceSwitchRevision = undefined;
  const form = reviewerForm('workspace-choice');
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /The workspace session could not be read\. Reload the workspace to continue\./);
  assert.doesNotMatch(form.box.innerHTML + main.innerHTML, /ordering basis/);
  assert.match(main.innerHTML, /reload-workspace/);
});

/* -- Round 11: one toast per ordinary save; saving forms are never released -- */
test('REV fe19-h1: an ordinary save on an unchanged page shows exactly one toast, immediately', async () => {
  const fetch = async (url) => (url === '/api/v1/session' ? json(hostedSession())
    : json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 }, id: 9 }));
  const win = loadViews(fetch); await settleAll();
  win.setTimeout = setTimeout; win.clearTimeout = clearTimeout;
  const shown = [];
  const toastEl = {};
  Object.defineProperty(toastEl, 'textContent', { get() { return this._t || ''; }, set(v) { this._t = v; if (v) shown.push(v); } });
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const modal = { open: false, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#toast' ? toastEl : sel === '#modal' ? modal : null);
  const timers = [];
  win.setTimeout = (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; };
  win.clearTimeout = (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); };
  win.EW.header = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const form = inlineForm();
  win.FormData = class { entries() { return Object.entries({ workspace_name: 'Alpha 2', expected_revision: '1' })[Symbol.iterator](); } };
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  await win.EWViews.handleSubmit(form); await settleAll();
  assert.deepEqual(shown, ['Saved.'], 'one message, shown at once');
  while (timers.length) timers.shift().fn();
  assert.deepEqual(shown, ['Saved.'], 'nothing queued behind it');
});

test('REV fe19-h2: a sign-out follow-up never releases a form whose save is in flight; no duplicate submit', async () => {
  const sent = []; let gate;
  const fetch = async (url, opts = {}) => {
    sent.push((opts.method || 'GET') + ' ' + url);
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'PUT') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', revision: 2 }); }
    if (opts.method === 'POST' && url.includes('/check')) return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'sign in' } }, false, 401);
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const modal = { open: false, close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const form = inlineForm();
  win.FormData = class { entries() { return Object.entries({ workspace_name: 'Alpha 2', expected_revision: '1' })[Symbol.iterator](); } };
  win.document.querySelectorAll = s => (/data-dirty|data-held/.test(s) && (form.dataset.dirty === '1' || form.dataset.held === '1') ? [form] : []);
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  await win.EW.api('/customer/alerts/1/check', { method: 'POST', body: {} }).catch(() => {});
  await settleAll();
  assert.equal(form.submit.disabled, true, 'still saving: never re-enabled');
  assert.doesNotMatch(form.box.innerHTML, /Try again/, 'no "try again" while it may still commit');
  form.submit.disabled = false; // even if something re-enabled it…
  await win.EWViews.handleSubmit(form); await settleAll(5);
  assert.equal(sent.filter(x => x.startsWith('PUT')).length, 1, '…a second submit is refused while saving');
  gate(); await p; await settleAll();
});

test('Z6 a held form whose save then fails (5xx, 428, transport) keeps a plain notice with Reload', async () => {
  for (const [label, respond, copy] of [
    ['500', () => json({ ok: false, error: 'boom' }, false, 500), /could not be confirmed\. Reload the workspace/],
    ['428', () => json({ ok: false, error: { code: 'PRECONDITION_REQUIRED', message: 'x' } }, false, 428), /was not saved\. Reload the workspace/],
    ['transport', () => { throw new TypeError('Failed to fetch'); }, /could not be confirmed\. Reload the workspace/],
  ]) {
    let gate;
    const fetch = async (url, opts = {}) => {
      if (url === '/api/v1/session') return json(hostedSession());
      if (opts.method === 'PUT') { await new Promise(r => { gate = r; }); return respond(); }
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
    };
    const win = loadViews(fetch); await settleAll();
    const main = { innerHTML: '', querySelectorAll: () => [] };
    win.document.querySelector = sel => (sel === '#main' ? main : null);
    win.EW.toast = () => {}; win.EW.saveNotice = () => {}; win.EW.header = () => {};
    win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
    await win.EWViews.render(); await settleAll();
    const form = inlineForm();
    win.FormData = class { entries() { return Object.entries({ workspace_name: 'Alpha 2', expected_revision: '1' })[Symbol.iterator](); } };
    const p = win.EWViews.handleSubmit(form); await settleAll(5);
    form.dataset.held = '1'; // held while saving (its basis moved)
    gate(); await p; await settleAll();
    assert.match(form.box.innerHTML, copy, label);
    assert.match(form.box.innerHTML, /data-action="reload-workspace"/, label + ' keeps Reload');
    assert.doesNotMatch(form.box.innerHTML, /Failed to fetch|Request failed/, label);
  }
});

/* -- Round 13: switch waits until quiet; new writes refuse while it waits -- */
const switchGapWin = async (lifecycleRespond) => {
  const log = []; let sessGate = null, holdSession = false, writeGate = null;
  const server = { rev: 5 };
  let win;
  const fetch = async (url, opts = {}) => {
    const m = (opts.method || 'GET') + ' ' + url;
    log.push('start ' + m);
    if (url === '/api/v1/session') {
      if (holdSession) { holdSession = false; await new Promise(r => { sessGate = r; }); }
      return json(hostedSession({ workspace_switch_revision: server.rev, active_workspace: server.ws || 'ws-a', workspace_context: server.ctx || 'ctx-a' }));
    }
    if (url === '/api/v1/session/workspace') {
      server.rev++; server.ws = 'ws-b'; server.ctx = 'ctx-b';
      log.push('switch POST while writesInFlight=' + (win.EW.current.writesInFlight || 0));
      return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: server.rev }));
    }
    if (url.includes('/lifecycle')) { await new Promise(r => { writeGate = r; }); return lifecycleRespond(); }
    return json({ ok: true, workspace_context: server.ctx || 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  return { win, log, toasts, hold: () => { holdSession = true; }, releaseSession: () => sessGate && sessGate(), releaseWrite: () => writeGate && writeGate() };
};

test('REV fe20-h1: a write clicked while a switch waits for a reconcile is refused before dispatch; the switch never races it', async () => {
  const t = await switchGapWin(() => json({ ok: true, workspace_context: 'ctx-a' }));
  t.hold();
  const rc = t.win.EWViews.reconcileCanonical();
  await settleAll(5);
  t.win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const sw = t.win.EWViews.handleSubmit(reviewerForm('workspace-choice')); await settleAll(5);
  const act = t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', lifecycle: 'active_local', rev: '1' }, closest: () => null });
  await settleAll(5);
  t.releaseSession(); await settleAll(10);
  t.releaseWrite(); await act; await sw; await rc; await settleAll();
  assert.ok(!t.log.some(l => /lifecycle/.test(l)), 'the write never reached the network');
  assert.ok(t.log.includes('switch POST while writesInFlight=0'), t.log.join(' | '));
  assert.ok(t.toasts.some(m => /A workspace switch is in progress\. Nothing was saved\./.test(m)), t.toasts.join(' | '));
  assert.ok(t.toasts.includes('Switched to ws-b.'));
});

test('REV fe20-h1c: the switch outcome is never overwritten by a racing write\'s conflict copy', async () => {
  const t = await switchGapWin(() => json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409));
  t.hold();
  const rc = t.win.EWViews.reconcileCanonical();
  await settleAll(5);
  t.win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const sw = t.win.EWViews.handleSubmit(reviewerForm('workspace-choice')); await settleAll(5);
  const act = t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', lifecycle: 'active_local', rev: '1' }, closest: () => null });
  await settleAll(5);
  t.releaseSession(); await settleAll(10);
  await sw; await settleAll();
  t.releaseWrite(); await act; await rc; await settleAll();
  assert.equal(t.toasts[t.toasts.length - 1], 'Switched to ws-b.', 'the switch outcome is the last word: ' + t.toasts.join(' | '));
  assert.ok(!t.toasts.some(m => /workspace changed|another tab/i.test(m)), t.toasts.join(' | '));
});

test('REV fe20-h2: a successful inline save never closes a dialog opened meanwhile, nor its draft', async () => {
  let gate;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'PUT') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', revision: 2 }); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const f2 = inlineForm('edit-saved');
  const modal = { open: false, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty/.test(s) ? [f2] : []) };
  f2.closest = s => (s === '#modal' ? modal : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.EW.toast = () => {}; win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const form = inlineForm();
  win.FormData = class { entries() { return Object.entries({ workspace_name: 'Alpha 2', expected_revision: '1' })[Symbol.iterator](); } };
  win.document.querySelectorAll = () => [];
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  modal.open = true; // the user opens an edit dialog and types
  gate(); await p; await settleAll();
  assert.equal(modal.open, true, 'the other dialog stays open');
  assert.equal(f2.dataset.dirty, '1', 'its draft is untouched');
});

test('AA1 Discard-and-switch also waits until quiet and refuses writes clicked meanwhile', async () => {
  const t = await switchGapWin(() => json({ ok: true, workspace_context: 'ctx-a' }));
  t.win.EW.closeDialog = () => {};
  t.hold();
  const rc = t.win.EWViews.reconcileCanonical();
  await settleAll(5);
  t.win.EW.current.pendingWorkspace = 'ws-b';
  const sw = t.win.EWViews.handleAction({ dataset: { action: 'switch-discard' } }); await settleAll(5);
  const act = t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', lifecycle: 'active_local', rev: '1' }, closest: () => null });
  await settleAll(5);
  t.releaseSession(); await settleAll(10);
  t.releaseWrite(); await act; await sw; await rc; await settleAll();
  assert.ok(!t.log.some(l => /lifecycle/.test(l)), 'refused before dispatch');
  assert.ok(t.log.includes('switch POST while writesInFlight=0'));
  assert.equal(t.win.EW.current.switchWaiting, 0, 'the wait is released');
  const after = await t.win.EW.api('/customer/x', { method: 'POST', body: {} }).then(() => 'sent', e => e.message);
  assert.notEqual(after, 'A workspace switch is in progress. Nothing was saved.', 'writes are allowed again afterwards');
});

test('AA2 Save-in-current waits for an in-flight reconcile before submitting the draft', async () => {
  let sessGate = null, holdSession = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { if (holdSession) { holdSession = false; await new Promise(r => { sessGate = r; }); } return json(hostedSession()); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  mountMain(win);
  win.EW.toast = () => {}; win.EW.closeDialog = () => {};
  const form = inlineForm();
  form.isConnected = true;
  let submitted = 0;
  form.requestSubmit = () => { submitted++; };
  holdSession = true;
  const rc = win.EWViews.reconcileCanonical();
  await settleAll(3);
  win.EW.current.dirtyForm = form; win.EW.current.dirtyWorkspace = win.EW.current.workspaceContext || '';
  const ss = win.EWViews.handleAction({ dataset: { action: 'switch-save' } });
  await settleAll(5);
  assert.equal(submitted, 0, 'not submitted while the re-read is in flight');
  sessGate(); await rc; await ss;
  assert.equal(submitted, 1, 'submitted once quiet');
});

test('AA3 an export that meets a workspace-switch 409 gets the workspace copy, not the collection copy', async () => {
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url.includes('/research-export')) return json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409);
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await settleAll();
  mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m);
  win.EW.current.route = '#/search'; win.location.hash = '#/search';
  win.EW.current.displayedRevision = 'r1';
  win.EW.current.displayedWorkspaceSwitchRevision = 5;
  await win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null });
  assert.ok(toasts.includes('This workspace changed in another tab or window. Nothing was changed.'), toasts.join(' | '));
  assert.ok(!toasts.some(m => /collection changed/.test(m)));
});

/* -- Round 14: an async success closes only the dialog it came from -- */
const otherDialogWin = async (respond) => {
  let gate;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && /allocation|reports|lifecycle/.test(url)) { await new Promise(r => { gate = r; }); return respond(url); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const f2 = inlineForm('campaign-form');
  const state = { modalForms: [] };
  const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty|data-held/.test(s) ? state.modalForms : []) };
  f2.closest = s => (s === '#modal' ? modal : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
  const gone = []; win.EW.go = r => gone.push(r);
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  modal.open = true;
  win.EW.current.dialogEpoch = { rev: 5 };
  win.document.querySelectorAll = () => [];
  // The user closes the confirmation, opens another dialog and types.
  const openOther = () => { modal.open = false; win.EW.current.dialogEpoch = { rev: 5 }; modal.open = true; state.modalForms = [f2]; };
  return { win, main, modal, f2, toasts, gone, openOther, release: () => gate() };
};
for (const kind of ['allocation', 'freeze']) {
  test(`REV fe21-h1-${kind}: a success never closes a dialog opened meanwhile, nor silently drops its draft`, async () => {
    const t = await otherDialogWin(() => json({ ok: true, workspace_context: 'ctx-a', action: 'move', binding: 'kept', report_no: 1 }));
    const form = inlineForm(kind === 'allocation' ? 'allocation-confirm' : 'confirm');
    form.closest = s => (s === '#modal' ? t.modal : null);
    form.insertBefore = () => {};
    const vals = kind === 'allocation'
      ? { campaign_id: '1', allocation_action: 'move', allocation_ref: 'r', from_target_id: 'p1', to_target_id: 'p2', expected_campaign_revision: '1', expected_from_revision: '1', units_json: '{}' }
      : { confirm_action: 'freeze-report', confirm_id: '1', freeze_pins: '{}', freeze_revision: '1' };
    t.win.FormData = class { entries() { return Object.entries(vals)[Symbol.iterator](); } };
    const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
    t.openOther();
    t.release(); await p; await settleAll();
    assert.equal(t.modal.open, true, 'the other dialog stays open');
    assert.equal(t.f2.dataset.dirty, '1', 'its draft is untouched');
  });
}

test('AB1 a freeze that succeeds after its own dialog was replaced never navigates; it says where the report is', async () => {
  const t = await otherDialogWin(() => json({ ok: true, workspace_context: 'ctx-a', report_no: 4 }));
  const form = inlineForm('confirm');
  form.closest = s => (s === '#modal' ? t.modal : null);
  t.win.FormData = class { entries() { return Object.entries({ confirm_action: 'freeze-report', confirm_id: '7', freeze_pins: '{}', freeze_revision: '1' })[Symbol.iterator](); } };
  const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
  t.openOther();
  t.release(); await p; await settleAll();
  assert.deepEqual(t.gone, [], 'no navigation over the other draft');
  assert.ok(t.toasts.includes('Report 4 saved. Open it from the campaign.'), 'self-contained: ' + t.toasts.join(' | '));
  assert.equal(t.modal.open, true);
  assert.equal(t.f2.dataset.dirty, '1');
  // Without another dirty dialog the freeze still opens its report.
  const t2 = await otherDialogWin(() => json({ ok: true, workspace_context: 'ctx-a', report_no: 5 }));
  const form2 = inlineForm('confirm');
  form2.closest = s => (s === '#modal' ? t2.modal : null);
  t2.win.FormData = class { entries() { return Object.entries({ confirm_action: 'freeze-report', confirm_id: '7', freeze_pins: '{}', freeze_revision: '1' })[Symbol.iterator](); } };
  const p2 = t2.win.EWViews.handleSubmit(form2); await settleAll(5);
  t2.release(); await p2; await settleAll();
  assert.deepEqual(t2.gone, ['#/campaign/7/report/5']);
  assert.equal(t2.modal.open, false, 'its own dialog closed');
});

test('AB2 a workspace 409 on a confirmation never closes a dialog opened during the await', async () => {
  const t = await otherDialogWin(() => json({ ok: false, error: { code: 'WORKSPACE_SWITCH_CONFLICT', message: 'x' } }, false, 409));
  const form = reviewerForm('confirm');
  form.closest = s => (s === '#modal' ? t.modal : null);
  t.win.EW.current.route = '#/alert/7'; t.win.location.hash = '#/alert/7';
  t.win.FormData = class { entries() { return Object.entries({ confirm_action: 'stop-alert', confirm_id: '7', expected_revision: '3' })[Symbol.iterator](); } };
  const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
  t.openOther();
  t.release(); await p; await settleAll();
  assert.equal(t.modal.open, true, 'the other dialog stays open');
  assert.equal(t.f2.dataset.dirty, '1');
});

test('AB3 Sign in never closes a dialog opened during its session re-read', async () => {
  let sessGate = null, hold = false;
  const fetch = async (url) => {
    if (url === '/api/v1/session') { if (hold) { hold = false; await new Promise(r => { sessGate = r; }); } return json(hostedSession()); }
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [] };
  const modal = { open: false, close() { this.open = false; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.EW.toast = () => {}; win.EW.header = () => {};
  hold = true;
  const p = win.EWViews.handleAction({ dataset: { action: 'show-signin' } });
  await settleAll(3);
  modal.open = true; win.EW.current.dialogEpoch = { rev: 5 }; // opened during the read
  sessGate(); await p;
  assert.equal(modal.open, true);
});

/* -- Round 15: dialogs closed or replaced mid-flight (two general rules) -- */
for (const [kind, resp, copy] of [
  ['freeze-500', () => json({ ok: false, error: 'boom' }, false, 500), 'Your change in ws-a could not be confirmed.'],
  ['freeze-412', () => json({ ok: false, error: { code: 'STALE', message: 'stale', details: { current_revision: 3 } } }, false, 412), 'Your change in ws-a was not saved: it changed meanwhile.'],
  ['alloc-500', () => json({ ok: false, error: 'boom' }, false, 500), 'Your change in ws-a could not be confirmed.'],
  ['alloc-409', () => json({ ok: false, error: 'conflict', conflict: { reason: 'x' } }, false, 409), 'Your change in ws-a was not saved: it changed meanwhile.'],
  ['stop-500', () => json({ ok: false, error: 'boom' }, false, 500), 'Your change in ws-a could not be confirmed.'],
]) {
  test(`REV fe22-${kind}: a failure whose own dialog the user closed mid-flight is told as a toast`, async () => {
    const t = await otherDialogWin(resp);
    const isAlloc = kind.startsWith('alloc'), isStop = kind.startsWith('stop');
    const form = isStop ? reviewerForm('confirm') : inlineForm(isAlloc ? 'allocation-confirm' : 'confirm');
    form.closest = s => (s === '#modal' ? t.modal : null);
    form.insertBefore = () => {};
    if (isStop) { t.win.EW.current.route = '#/alert/7'; t.win.location.hash = '#/alert/7'; }
    const vals = isAlloc
      ? { campaign_id: '1', allocation_action: 'move', allocation_ref: 'r', from_target_id: 'p1', to_target_id: 'p2', expected_campaign_revision: '1', expected_from_revision: '1', units_json: '{}' }
      : isStop ? { confirm_action: 'stop-alert', confirm_id: '7', expected_revision: '3' }
        : { confirm_action: 'freeze-report', confirm_id: '1', freeze_pins: '{}', freeze_revision: '1' };
    t.win.FormData = class { entries() { return Object.entries(vals)[Symbol.iterator](); } };
    const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
    t.modal.open = false; // the user pressed Close; nothing else opened
    t.release(); await p; await settleAll();
    assert.ok(t.toasts.includes(copy), t.toasts.join(' | '));
    assert.equal(form.box.innerHTML, '', 'nothing painted into the invisible form');
  });
}

test('REV fe22-alloc-ok: a normal allocation still closes its own dialog and reports', async () => {
  const t = await otherDialogWin(() => json({ ok: true, workspace_context: 'ctx-a', action: 'move', binding: 'kept' }));
  const form = inlineForm('allocation-confirm');
  form.closest = s => (s === '#modal' ? t.modal : null);
  t.win.FormData = class { entries() { return Object.entries({ campaign_id: '1', allocation_action: 'move', allocation_ref: 'r', from_target_id: 'p1', to_target_id: 'p2', expected_campaign_revision: '1', expected_from_revision: '1', units_json: '{}' })[Symbol.iterator](); } };
  const p = t.win.EWViews.handleSubmit(form); await settleAll(5);
  t.release(); await p; await settleAll();
  assert.equal(t.modal.open, false);
  assert.ok(t.toasts.some(m => /Source moved to the chosen placement\./.test(m)), t.toasts.join('|'));
});

test('REV fe22-r22b: a success that navigates never closes another dialog holding unsaved entries', async () => {
  let gate;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && /\/customer\/campaigns$/.test(url)) { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', campaign: { id: 9 } }); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; } };
  const f2 = inlineForm('save-search');
  const state = { modalForms: [] };
  const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty|data-held|data-saving/.test(s) ? state.modalForms : []) };
  f2.closest = s => (s === '#modal' ? modal : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.history.pushState = (st, ti, dest) => { win.location.hash = dest; };
  win.location.hash = '#/campaigns'; win.EW.current.route = '#/campaigns';
  await win.EWViews.render(); await settleAll();
  win.EW.current.dialogEpoch = { rev: 5 }; win.EW.current.dialogRoute = '#/campaigns'; win.EW.current.dialogWorkspace = 'ctx-a';
  win.document.querySelectorAll = () => [];
  const form = inlineForm('campaign');
  form.closest = s => (s === '#modal' ? modal : null);
  win.FormData = class { entries() { return Object.entries({ name: 'C1', mode: 'research', brand: '' })[Symbol.iterator](); } };
  const p = win.EWViews.handleSubmit(form); await settleAll(5);
  modal.open = false; win.EW.current.dialogEpoch = { rev: 5 }; win.EW.current.dialogRoute = '#/campaigns'; modal.open = true; state.modalForms = [f2];
  gate(); await p; await settleAll();
  // Its own dialog is gone: the success never navigates the user away
  // from what they are doing now; it says where to find the campaign.
  assert.equal(win.EW.current.route, '#/campaigns', 'no navigation away from the other dialog');
  assert.equal(modal.open, true, 'the other dialog with unsaved entries stays open');
  assert.equal(f2.dataset.dirty, '1');
  assert.ok(toasts.includes('Campaign saved. Find it in Campaigns.'), toasts.join(' | '));
});

test('AC1 an epoch change never closes a dialog whose save is in flight, nor says nothing was changed', async () => {
  const { fetch } = hostedFixture();
  const { win, modal, toasts } = await viewsWith(fetch);
  const f = inlineForm('save-search');
  f.dataset = { saving: '1', dirty: '1' };
  modal.open = true;
  modal.querySelector = () => null;
  modal.querySelectorAll = (s) => (/data-dirty|data-held|data-saving/.test(s) ? [f] : []);
  win.EW.current.dialogEpoch = { rev: 4 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal(modal.open, true, 'kept: its own outcome will report');
  assert.ok(!toasts.some(m => /Nothing was changed/.test(m)), toasts.join(' | '));
});

test('AC2 an epoch change holds a dialog draft visibly instead of discarding it', async () => {
  const { fetch } = hostedFixture();
  const { win, modal, toasts } = await viewsWith(fetch);
  const f = inlineForm('save-search');
  modal.open = true;
  modal.querySelector = () => null;
  modal.querySelectorAll = (s) => (/data-dirty|data-held|data-saving/.test(s) ? [f] : []);
  win.EW.current.dialogEpoch = { rev: 4 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal(modal.open, true);
  assert.equal(f.dataset.held, '1');
  assert.match(f.box.innerHTML, /your entries are kept here[\s\S]*reload-workspace/);
  assert.ok(!toasts.some(m => /not applied|Nothing was changed/.test(m)), toasts.join(' | '));
});

test('AC3 a route change keeps a dialog with unsaved entries open, with its own identity', async () => {
  const { fetch } = hostedFixture();
  const { win, modal } = await viewsWith(fetch);
  const f = inlineForm('save-search');
  modal.open = true;
  modal.querySelectorAll = (s) => (/data-dirty|data-held|data-saving/.test(s) ? [f] : []);
  win.EW.current.dialogRoute = '#/search';
  win.EW.current.dialogEpoch = { rev: 5 };
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal(modal.open, true, 'kept open over the new route');
  assert.equal(win.EW.current.dialogRoute, '#/search', 'its own route identity is kept');
  // An empty dialog still closes on route change, as before.
  modal.querySelectorAll = () => [];
  win.location.hash = '#/overview';
  await win.EWViews.render();
  assert.equal(modal.open, false);
});

/* -- Round 16: route-kept dialogs never submit off-route; pinned ids -- */
const keptConfirmWin = async (firstResp) => {
  let gate = null; const posts = []; let n = 0;
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && /lifecycle/.test(url)) {
      posts.push({ url, body: opts.body }); n++;
      if (n === 1) { await new Promise(r => { gate = r; }); return firstResp(); }
      return json({ ok: true, workspace_context: 'ctx-a' });
    }
    return json({ ok: true, workspace_context: 'ctx-a', alert: { id: 8, name: 'x', revision: 3, events: [] } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {} };
  const form = reviewerForm('confirm');
  const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty|data-held|data-saving/.test(s)
      && (form.dataset.saving === '1' || form.dataset.held === '1' || form.dataset.dirty === '1') ? [form] : []) };
  form.closest = s => (s === '#modal' ? modal : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = () => [];
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.location.hash = '#/alert/7';
  await win.EWViews.render(); await settleAll();
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.dialogRoute = '#/alert/7'; win.EW.current.dialogWorkspace = win.EW.current.workspaceContext;
  modal.open = true;
  win.FormData = class { entries() { return Object.entries({ confirm_action: 'stop-alert', confirm_id: '7', expected_revision: '3' })[Symbol.iterator](); } };
  return { win, form, modal, toasts, posts, release: () => gate() };
};
for (const dest of ['#/alert/8', '#/alerts']) {
  test(`REV fe23-h1 ${dest}: a stop confirmation kept over another route never sends for the wrong alert, nor silently does nothing`, async () => {
    const t = await keptConfirmWin(() => json({ ok: false, error: 'boom' }, false, 500));
    const p = t.win.EWViews.handleSubmit(t.form); await settleAll(5);
    t.win.location.hash = dest;
    await t.win.EWViews.render(); await settleAll();
    assert.equal(t.modal.open, true, 'kept open (its save is in flight)');
    t.release(); await p; await settleAll();
    await t.win.EWViews.handleSubmit(t.form); await settleAll();
    assert.equal(t.posts.length, 1, 'no second POST off-route');
    assert.ok(!t.posts.some(x => /alerts\/8\//.test(x.url)), 'never the other alert');
    assert.match(t.form.box.innerHTML, /This form belongs to alert 7\. Return to it to continue, or close it\./);
    assert.match(t.form.box.innerHTML, /href="#\/alert\/7"/, 'a real link back to its page');
    // Back on its own page, the pinned alert id is used.
    t.win.location.hash = '#/alert/7';
    await t.win.EWViews.render(); await settleAll();
    t.win.EW.current.dialogEpoch = { rev: t.win.EW.current.displayedWorkspaceSwitchRevision };
    await t.win.EWViews.handleSubmit(t.form); await settleAll();
    assert.equal(t.posts.length, 2);
    assert.match(t.posts[1].url, /alerts\/7\/lifecycle/);
  });
}

test('REV fe23-h2: a dirty dialog kept over another route cannot submit there', async () => {
  const posts = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method && opts.method !== 'GET') { posts.push(opts.method + ' ' + url); return json({ ok: true, workspace_context: 'ctx-a', item: { id: 1 } }); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {} };
  const form = inlineForm('save-search');
  const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty|data-held|data-saving/.test(s)
      && (form.dataset.saving === '1' || form.dataset.held === '1' || form.dataset.dirty === '1') ? [form] : []) };
  form.closest = s => (s === '#modal' ? modal : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = () => [];
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.location.hash = '#/search?mode=mentions&q=cola';
  await win.EWViews.render(); await settleAll();
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.dialogRoute = win.location.hash; win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.pendingSearch = win.EW.searchScope();
  win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  assert.equal(modal.open, true, 'kept (it holds entries)');
  win.FormData = class { entries() { return Object.entries({ name: 'My cola' })[Symbol.iterator](); } };
  await win.EWViews.handleSubmit(form); await settleAll();
  assert.deepEqual(posts.filter(x => !/research-query/.test(x)), [], 'no write sent off-route (the page read is not a write)');
  assert.match(form.box.innerHTML, /This form belongs to the search page\. Return to it to continue, or close it\./);
  assert.match(form.box.innerHTML, /href="#\/search\?mode=mentions&amp;q=cola"/);
});

for (const outcome of ['ok', 'fail']) {
  test(`REV fe23-h3-${outcome}: a brief outcome whose campaign dialog was closed is told as a toast`, async () => {
    let gate;
    const fetch = async (url, opts = {}) => {
      if (url === '/api/v1/session') return json(hostedSession());
      if (opts.method === 'POST' && /briefs/.test(url)) {
        await new Promise(r => { gate = r; });
        return outcome === 'ok' ? json({ ok: true, workspace_context: 'ctx-a', brief_id: 'b1', version: 1 })
          : json({ ok: false, error: 'boom' }, false, 500);
      }
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
    };
    const win = loadViews(fetch); await settleAll();
    const main = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {} };
    const box = { innerHTML: '' };
    const form = inlineForm('campaign');
    const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
    form.closest = s => (s === '#modal' ? modal : null);
    form.querySelector = s => (s === '#brief-title' ? { value: 'T' } : s === '#brief-text' ? { value: 'X' } : s === '[name=name]' ? { value: 'Spring push' } : null);
    win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : sel === '#brief-status' ? box : null);
    win.document.querySelectorAll = () => [];
    const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {}; win.EW.header = () => {};
    win.location.hash = '#/campaigns';
    await win.EWViews.render(); await settleAll();
    win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
    win.EW.current.dialogRoute = '#/campaigns'; win.EW.current.dialogWorkspace = 'ctx-a';
    const el = { dataset: { action: 'brief-register' }, disabled: false, closest: s => (s === 'form' ? form : null) };
    const p = win.EWViews.handleAction(el); await settleAll(5);
    modal.open = false;
    gate(); await p; await settleAll();
    assert.deepEqual(toasts, [outcome === 'ok' ? 'Brief saved for Spring push.' : 'The brief for Spring push could not be confirmed.']);
    assert.equal(box.innerHTML, '', 'nothing painted into the closed dialog');
  });
}

test('AD1 a 5xx brief failure in an open dialog says "could not be confirmed", never "not saved"', async () => {
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && /briefs/.test(url)) return json({ ok: false, error: 'boom' }, false, 503);
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await settleAll();
  const box = { innerHTML: '' };
  const form = inlineForm('campaign');
  const modal = { open: true, close() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  form.closest = s => (s === '#modal' ? modal : null);
  form.querySelector = s => (s === '#brief-title' ? { value: 'T' } : s === '#brief-text' ? { value: 'X' } : null);
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#brief-status' ? box : { innerHTML: '' });
  win.EW.toast = () => {};
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.dialogRoute = win.EW.current.route; win.EW.current.dialogWorkspace = win.EW.current.workspaceContext || '';
  await win.EWViews.handleAction({ dataset: { action: 'brief-register' }, disabled: false, closest: s => (s === 'form' ? form : null) });
  assert.match(box.innerHTML, /could not be confirmed/);
  assert.doesNotMatch(box.innerHTML, /not saved/);
});

test('AD2 an alert or bookmark saved after its dialog was closed never navigates; it says where to find it', async () => {
  for (const [id, vals, copy] of [
    ['alert-form', { name: 'A1', event_types: 'new_match', initial_query: '{"version":2,"mode":"mentions"}' }, 'Alert saved. Find it in Alerts.'],
    ['save-bookmark', { title: 'B1', kind: 'show', id: 's1' }, 'Bookmark saved. Find it in Saved.'],
  ]) {
    let gate;
    const fetch = async (url, opts = {}) => {
      if (url === '/api/v1/session') return json(hostedSession());
      if (opts.method === 'POST') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', id: 4, item: { id: 4 } }); }
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
    };
    const win = loadViews(fetch); await settleAll();
    const modal = { open: true, close() { this.open = false; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
    mountMain(win);
    const base = win.document.querySelector;
    win.document.querySelector = sel => (sel === '#modal' ? modal : base(sel));
    const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
    const gone = []; win.EW.go = r => gone.push(r);
    win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
    win.EW.current.dialogRoute = win.EW.current.route; win.EW.current.dialogWorkspace = win.EW.current.workspaceContext || '';
    const form = inlineForm(id);
    form.closest = s => (s === '#modal' ? modal : null);
    win.FormData = class { entries() { return Object.entries(vals)[Symbol.iterator](); } };
    const p = win.EWViews.handleSubmit(form); await settleAll(5);
    modal.open = false; // the user closed it while it saved
    gate(); await p; await settleAll();
    assert.deepEqual(gone, [], id + ': no navigation');
    assert.ok(toasts.includes(copy), id + ': ' + toasts.join(' | '));
  }
});

test('AD3 a fenced error inside a dialog tells the user to close it before opening Settings', async () => {
  const { fetch } = hostedFixture();
  const { win, modal } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  win.location.hash = '#/overview'; win.EW.current.route = '#/overview';
  const form = reviewerForm('save-search', { entries: true });
  dialogWith(win, form);
  win.EW.current.dialogRoute = '#/overview'; win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.pendingSearch = { query: { version: 2, mode: 'mentions' }, selector: null };
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.FormData = class { *entries() { yield ['name', 'S']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /Close this form, then <a href="#\/settings">open Settings<\/a> to choose the workspace again/);
  void modal;
});

/* -- Delta 17: late openers, off-route notice clearing, Settings copy -- */
const lateOpenerWin = async () => {
  let gate = null;
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/shows') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a', shows: [] }); }
    return json({ ok: true, workspace_context: 'ctx-a', brands: [] });
  };
  const win = loadViews(fetch); await settleAll();
  win.setTimeout = () => {};
  const forms = [];
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: (s) => (modal.open && /data-dirty|data-held|data-saving/.test(s) ? forms : []) };
  const main = { innerHTML: '', querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = () => [];
  win.EW.toast = () => {}; win.EW.header = () => {};
  const opened = [];
  const real = win.EW.openDialog;
  win.EW.openDialog = (...a) => { opened.push(a[0]); return real(...a); };
  return { win, modal, forms, opened, release: () => gate && gate() };
};

test('AE1 a delayed opener never replaces a dialog the user opened since, with or without entries', async () => {
  for (const typed of [true, false]) {
    const t = await lateOpenerWin();
    const p = t.win.EWViews.handleAction({ dataset: { action: 'create-alert' }, closest: () => null }); // waits on a fetch
    await settleAll(3);
    t.win.EWViews.handleAction({ dataset: { action: 'save-search' }, closest: () => null }); // opens at once
    if (typed) { const f = inlineForm('save-search'); t.forms.push(f); }
    t.release(); await p; await settleAll();
    assert.deepEqual(t.opened, ['Save this search'], (typed ? 'typed' : 'empty') + ': the late Create alert is dropped');
    assert.equal(t.modal.open, true);
  }
});

test('AE1b the Filters late fill never overwrites a dialog opened since', async () => {
  let gate = null, armed = false, held = null;
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (armed && url.includes('/customer/filter-values')) {
      if (!held) held = new Promise(r => { gate = r; });
      await held;
      return json({ ok: true, workspace_context: 'ctx-a', items: [] });
    }
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await settleAll();
  win.setTimeout = () => {};
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#modal' ? modal : { innerHTML: '', querySelectorAll: () => [] });
  win.EW.toast = () => {};
  win.location.hash = '#/search'; win.EW.current.route = '#/search';
  armed = true;
  const p = win.EWViews.handleAction({ dataset: { action: 'filters' }, closest: () => null });
  await settleAll(3);
  assert.ok(gate, 'the Filters lookups are in flight');
  win.EW.openDialog('Save this search', '<form id="save-search">TYPED</form>', '', '');
  modal.innerHTML = '<form id="save-search">TYPED</form>';
  if (gate) gate();
  await p; await settleAll();
  assert.match(modal.innerHTML, /TYPED/, 'the other dialog is untouched');
  assert.doesNotMatch(modal.innerHTML, /Apply filters/);
});

test('AE2 the off-route notice clears once the dialog is back on its own page', async () => {
  const { fetch } = hostedFixture();
  const { win, modal } = await viewsWith(fetch);
  const form = inlineForm('save-search');
  modal.open = true;
  modal.querySelectorAll = (s) => (/data-dirty|data-held|data-saving/.test(s) ? [form] : []);
  form.closest = s => (s === '#modal' ? modal : null);
  win.EW.current.dialogRoute = '#/search';
  win.EW.current.dialogEpoch = { rev: 5 };
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.location.hash = '#/settings';
  await win.EWViews.render();
  win.FormData = class { *entries() { yield ['name', 'S']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /This form belongs to the search page/);
  win.location.hash = '#/search';
  await win.EWViews.render();
  assert.equal(form.box.innerHTML, '', 'cleared on return');
});

test('AE3 on Settings, the in-dialog fence copy points to this page, not to Settings', async () => {
  const { fetch } = hostedFixture();
  const { win } = await viewsWith(fetch);
  win.location.hash = '#/settings';
  await win.EWViews.render();
  const form = reviewerForm('save-search', { entries: true });
  dialogWith(win, form);
  win.EW.current.dialogRoute = '#/settings'; win.EW.current.dialogWorkspace = 'ctx-a';
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.pendingSearch = { query: { version: 2, mode: 'mentions' }, selector: null };
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.FormData = class { *entries() { yield ['name', 'S']; } };
  await win.EWViews.handleSubmit(form);
  assert.match(form.box.innerHTML, /Close this form, then choose the workspace again on this page to confirm it\./);
  assert.doesNotMatch(form.box.innerHTML, /open Settings|below/);
});

test('REV fe25-held2: brief-register on a held dialog form never replaces its notice; returning keeps it', async () => {
  const writes = [];
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method && opts.method !== 'GET' && !/research-query/.test(url)) writes.push(url);
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(fetch); await settleAll();
  const main = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null, insertAdjacentHTML() {} };
  const form = reviewerForm('campaign', { dirty: true, entries: true });
  form.dataset.held = '1';
  form.submit.disabled = true;
  form.box.innerHTML = '<div class="notice warn"><p>HELD changed</p>Reload workspace</div>';
  const held = form.box.innerHTML;
  const modal = { open: true, close() { this.open = false; }, showModal() {}, setAttribute() {}, querySelector: () => null,
    querySelectorAll: (s) => (modal.open && /data-dirty|data-held|data-saving/.test(s) ? [form] : []) };
  form.closest = s => (s === '#modal' ? modal : s === 'form' ? form : null);
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = () => [];
  win.EW.toast = () => {}; win.EW.saveNotice = () => {}; win.EW.header = () => {};
  win.location.hash = '#/campaigns';
  await win.EWViews.render(); await settleAll();
  win.EW.current.dialogEpoch = { rev: win.EW.current.displayedWorkspaceSwitchRevision };
  win.EW.current.dialogRoute = win.location.hash; win.EW.current.dialogWorkspace = 'ctx-a';
  win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  assert.equal(modal.open, true, 'kept (it holds entries)');
  await win.EWViews.handleAction({ dataset: { action: 'brief-register' }, closest: s => (s === 'form' ? form : null) });
  assert.equal(form.box.innerHTML, held, 'held notice not replaced off-route');
  win.location.hash = '#/campaigns';
  await win.EWViews.render(); await settleAll();
  assert.equal(form.box.innerHTML, held, 'held notice survives the return');
  assert.equal(form.dataset.held, '1');
  assert.equal(form.submit.disabled, true);
  assert.deepEqual(writes, []);
});

/* -- Rendered-UI round: holds, durable notices, chooser access, chrome -- */
// A published #main with two workspace write controls (a held form's Save
// and Settings' display form Save), the chooser submit, and real banner /
// notice insertion and removal.
const uiRoot = () => {
  const mk = (title = '') => ({ disabled: false, title, dataset: {}, closest: () => null });
  const root = { innerHTML: '', save: mk(), display: mk('Own tip'), chooser: mk(), inserted: [] };
  const writes = () => [root.save, root.display];
  root.querySelectorAll = (sel) => {
    if (sel === '[data-fence-banner]') return root.inserted.filter(b => /data-fence-banner/.test(b.html));
    if (sel === '[data-fence-held]') return writes().filter(w => w.dataset.fenceHeld);
    if (sel === '#workspace-choice [type=submit]') return [root.chooser];
    if (sel.includes('#settings-display [type=submit]')) return writes();
    return [];
  };
  root.querySelector = (sel) => (sel === '[data-outcome-notice]' ? root.inserted.find(b => /data-outcome-notice/.test(b.html)) || null : null);
  root.insertAdjacentHTML = (pos, html) => {
    const b = { html, remove() { root.inserted = root.inserted.filter(x => x !== b); } };
    root.inserted.unshift(b);
  };
  root.top = () => root.inserted.map(b => b.html).join('\n');
  return root;
};
const uiWin = async (fetch) => {
  const win = loadViews(fetch); await settleAll();
  const root = uiRoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const pick = { focused: 0, scrolled: 0, focus() { this.focused++; }, scrollIntoView() { this.scrolled++; } };
  const headerEl = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? root : sel === '#modal' ? modal : sel === '#header' ? headerEl
    : sel === '#ws-pick' ? (win.EW.current.route === '#/settings' && /ws-pick/.test(root.innerHTML) ? pick : null) : null);
  win.document.querySelectorAll = () => [];
  win.scrollTo = () => {};
  let headers = 0;
  win.EW.header = () => { headers++; };
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  return { win, root, modal, pick, headerEl, toasts, headers: () => headers };
};
// A session that moves to ws-b once `moved()` says so.
const movingSession = (moved, extra = {}) => async (url, opts = {}) => {
  if (url === '/api/v1/session/workspace') return json(sessB());
  if (url === '/api/v1/session') return json(moved() ? sessB() : hostedSession(extra));
  return json({ ok: true, workspace_context: moved() ? 'ctx-b' : 'ctx-a', preferences: { workspace_name: moved() ? 'Beta' : 'Alpha', revision: 1 } });
};

test('UI1 while a switch waits, write controls hold with the reason and the page says so at the top', async () => {
  let switched = false, gate = null;
  const base = movingSession(() => switched);
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') switched = true;
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return base(url, opts);
  };
  const { win, root } = await uiWin(fetch);
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  assert.equal(root.display.disabled, false, 'enabled on a valid published basis');
  const write = win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).catch(() => {});
  await settleAll(3);
  const status = { textContent: '' };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? status : s === '[type=submit]' ? { disabled: false } : null),
    querySelectorAll: () => [], elements: [] };
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const sw = win.EWViews.handleSubmit(choice);
  await settleAll(5);
  assert.equal(status.textContent, 'Finishing your change…', 'the chooser says it waits');
  for (const c of [root.save, root.display]) {
    assert.equal(c.disabled, true, 'every workspace write control holds while the switch waits');
    assert.equal(c.title, 'A workspace switch is in progress.');
  }
  assert.match(root.top(), /role="status" data-fence-banner><p>Finishing your change before switching workspace…/, 'page-level status at the top of #main');
  gate(); await write; await sw; await settleAll(20);
  assert.equal(win.EW.current.switchWaiting, 0);
  assert.equal(root.display.disabled, false, 'released once the switch is done and the new view published');
  assert.equal(root.display.title, 'Own tip', 'own tooltip restored');
  assert.doesNotMatch(root.top(), /Finishing your change/);
});

test('UI2 the fence banner carries a real Choose workspace control that lands on the chooser', async () => {
  const { win, root, pick } = await uiWin(movingSession(() => false));
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  win.location.hash = '#/overview'; win.EW.current.route = '#/overview';
  await win.EWViews.render(); await settleAll();
  assert.match(root.top(), /The workspace switch could not be confirmed\. Choose the workspace again to confirm it\. Nothing was saved\./);
  assert.match(root.top(), /data-action="choose-workspace"[^>]*>Choose workspace</);
  assert.doesNotMatch(root.top(), /below|response was lost|may have been applied/);
  win.history.pushState = (st, t, url) => { win.location.hash = url; };
  await win.EWViews.handleAction({ dataset: { action: 'choose-workspace' } });
  await settleAll();
  assert.equal(win.EW.current.route, '#/settings', 'navigates to Settings');
  assert.ok(pick.scrolled >= 1 && pick.focused >= 1, 'the chooser select is scrolled into view and focused');
  const before = pick.focused;
  await win.EWViews.handleAction({ dataset: { action: 'choose-workspace' } });
  assert.equal(pick.focused, before + 1, 'on Settings it focuses the chooser in place');
});

test('UI3 another-tab move: a durable notice survives the re-render it caused and leaves only on Dismiss', async () => {
  let moved = false;
  const { win, root, toasts } = await uiWin(movingSession(() => moved));
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  moved = true;
  await win.EW.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(win.EW.current.activeWorkspace, 'ws-b');
  assert.match(root.innerHTML, /Beta/, 'the reconcile re-rendered under ws-b');
  const text = 'The workspace was changed in another tab or window. This tab now shows ws-b.';
  assert.match(root.top(), new RegExp(`role="status" data-outcome-notice><p>${text.replace(/\./g, '\\.')}</p>`));
  assert.match(root.top(), /data-action="dismiss-notice"[^>]*>Dismiss</);
  void toasts;
  await win.EWViews.render(); await settleAll();
  assert.equal((root.top().match(/data-outcome-notice/g) || []).length, 1, 'still one notice after another render');
  await win.EWViews.handleAction({ dataset: { action: 'dismiss-notice' } });
  assert.doesNotMatch(root.top(), /data-outcome-notice/);
  await win.EWViews.render(); await settleAll();
  assert.doesNotMatch(root.top(), /data-outcome-notice/, 'dismissed for good');
});

test('UI3b a write committed in A while the tab moved is told durably: saved to A, now shows B', async () => {
  let moved = false, gate = null;
  const base = movingSession(() => moved);
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return base(url, opts);
  };
  const { win, root } = await uiWin(fetch);
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const write = win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  await settleAll(3);
  moved = true;
  await win.EW.api('/customer/overview').catch(() => {});
  await settleAll(20);
  gate();
  const err = await write; await settleAll(10);
  assert.equal(err && err.lateWrite, 'saved');
  assert.match(root.top(), /Your change was saved to ws-a\. The workspace was changed in another tab or window\. This tab now shows ws-b\./);
});

test('UI3c switch-discard keeps a durable record of the discard and of a commit in its window', async () => {
  let gate = null;
  const { fetch } = hostedFixture(async (url, opts, st) => {
    if (url === '/api/v1/session/workspace') { st.ws = 'b'; st.rev = 6; return json(sessB()); }
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') { await new Promise(r => { gate = r; }); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  const { win, root, toasts } = await uiWin(fetch);
  win.EW.closeDialog = () => {};
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  win.EW.current.pendingWorkspace = 'ws-b';
  await win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(10);
  assert.ok(toasts.includes('Switched to ws-b. Unsaved changes were discarded. Nothing was saved to ws-a.'), toasts.join(' | '));
  assert.match(root.top(), /data-outcome-notice><p>Unsaved changes were discarded\. Nothing was saved to ws-a\.<\/p>/, 'durable after the render');
  // Back to A, with a save in flight that commits inside the discard window.
  const write = win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).catch(() => {});
  await settleAll(3);
  win.EW.current.pendingWorkspace = 'ws-a';
  const d = win.EWViews.handleAction({ dataset: { action: 'switch-discard' } });
  await settleAll(3);
  gate(); await write; await d; await settleAll(10);
  assert.doesNotMatch(root.top(), /Nothing was saved to ws-b/, 'never claims nothing was saved across a commit');
  assert.match(root.top(), /Your change was saved to ws-b\. This tab now shows ws-b\. Unsaved changes were discarded\./);
});

test('UI4 a record route reopened after the tab left its workspace names the cause; a delayed opener says so too', async () => {
  // notFound copy: dataset revision only where it is genuine.
  const E = EW('#/episode/e1');
  assert.match(E.notFound(), /different local dataset revision/, 'local mode unchanged');
  E.current.hosted = true; E.current.activeWorkspace = 'ws-b'; E.current.recordLeftWs = 'ws-a';
  assert.match(E.notFound(), /This record belongs to another workspace\. This tab now shows ws-b\./);
  assert.doesNotMatch(E.notFound(), /dataset revision/);
  // Rendered: published in ws-a, reconciled to ws-b, re-rendered there.
  let moved = false;
  const base = movingSession(() => moved);
  const fetch = async (url, opts = {}) => {
    if (url.startsWith('/api/v1/customer/records/')) return json({ ok: false, error: { code: 'NOT_FOUND', message: 'missing' } }, false, 404);
    return base(url, opts);
  };
  const t = await uiWin(fetch);
  t.win.location.hash = '#/episode/e1'; t.win.EW.current.route = '#/episode/e1';
  await t.win.EWViews.render(); await settleAll();
  assert.match(t.root.innerHTML, /We can(?:'|&#39;)t find this episode<\/h1><p>It may have been removed, or the link may be wrong\./, 'hosted, same workspace: plain not-found copy');
  assert.doesNotMatch(t.root.innerHTML, /dataset revision/);
  moved = true;
  await t.win.EW.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(t.win.EW.current.activeWorkspace, 'ws-b');
  assert.match(t.root.innerHTML, /This record belongs to another workspace\. This tab now shows ws-b\./);
  assert.doesNotMatch(t.root.innerHTML, /dataset revision/);
  // A delayed opener whose read lands after the tab left its workspace.
  let gate = null, moved2 = false;
  const base2 = movingSession(() => moved2);
  const fetch2 = async (url, opts = {}) => {
    if (url === '/api/v1/customer/alerts/7?summaries=0') { await new Promise(r => { gate = r; }); return json({ alert: { id: 7 }, workspace_context: 'ctx-a' }); }
    return base2(url, opts);
  };
  const u = await uiWin(fetch2);
  u.win.location.hash = '#/alerts'; u.win.EW.current.route = '#/alerts';
  await u.win.EWViews.render(); await settleAll();
  const opener = u.win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: '7' } });
  await settleAll(3);
  moved2 = true;
  await u.win.EW.api('/customer/overview').catch(() => {});
  await settleAll(20);
  gate(); await opener; await settleAll(5);
  assert.equal(u.modal.open, false, 'the late opener never opens');
  assert.match(u.root.top(), /This record belongs to another workspace\. The workspace was changed in another tab or window\. This tab now shows ws-b\./);
  assert.doesNotMatch(u.root.top() + u.root.innerHTML, /dataset revision/);
});

test('UI5 held states disable every workspace write control with the matching reason, by precedence', async () => {
  let moved = false;
  const { win, root } = await uiWin(movingSession(() => moved));
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  // A control disabled for its own reason, matched by the same selector.
  const other = { disabled: true, title: 'Choose a plan first', dataset: {}, closest: () => null };
  const baseQsa = root.querySelectorAll;
  root.querySelectorAll = (sel) => (sel.includes('#settings-display [type=submit]') ? [...baseQsa(sel), other] : baseQsa(sel));
  const sync = () => win.EWViews.syncWriteControls(root);
  const reasons = () => [root.save.title, root.display.title];
  assert.deepEqual([root.save.disabled, root.display.disabled], [false, false]);
  // Stale basis (after a 409 or a held reconcile).
  win.EW.current.displayedWorkspaceSwitchRevision = undefined; sync();
  assert.deepEqual(reasons(), Array(2).fill('This page is out of date. Reload the workspace to make changes.'));
  assert.equal(root.display.disabled, true, 'the display form\'s Save holds with the name form');
  // Signed out outranks stale.
  win.EW.current.principal = null; sync();
  assert.deepEqual(reasons(), Array(2).fill('You are signed out. Sign in to make changes.'));
  // Fence outranks signed out; a waiting switch outranks the fence.
  win.EW.current.writeFence = { to: 'ws-b', op: 1, rev: 5 }; sync();
  assert.match(root.save.title, /could not be confirmed/);
  win.EW.current.switchWaiting = 1; sync();
  assert.deepEqual(reasons(), Array(2).fill('A workspace switch is in progress.'));
  // Nothing held: everything re-enables, own tooltips back.
  win.EW.current.switchWaiting = 0; win.EW.current.writeFence = null; win.EW.current.principal = 'sam';
  win.EW.current.displayedWorkspaceSwitchRevision = 5; sync();
  assert.deepEqual([root.save.disabled, root.display.disabled], [false, false]);
  assert.equal(root.display.title, 'Own tip');
  assert.equal(other.disabled, true, 'never touched');
  assert.equal(other.title, 'Choose a plan first');
  // Local mode: no session basis, no holds.
  win.EW.current.hosted = false; win.EW.current.displayedWorkspaceSwitchRevision = undefined; sync();
  assert.equal(root.save.disabled, false, 'local mode unaffected');
});

test('UI5b a 409 held reconcile disables the other forms on the page; a fresh publication re-enables them', async () => {
  let moved = false;
  const { win, root } = await uiWin(movingSession(() => moved));
  win.location.hash = '#/settings'; win.EW.current.route = '#/settings';
  await win.EWViews.render(); await settleAll();
  const draft = fakeForm();
  win.document.querySelectorAll = sel => (/data-dirty|data-held/.test(sel) && win.document._drafts ? win.document._drafts : []);
  win.document._drafts = [draft];
  moved = true;
  await win.EW.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(draft.submit.disabled, true, 'the held draft');
  assert.equal(root.display.disabled, true, 'another form on the page holds too');
  assert.equal(root.display.title, 'This page is out of date. Reload the workspace to make changes.');
  win.document._drafts = null;
  await win.EWViews.handleAction({ dataset: { action: 'reload-workspace' } });
  await settleAll();
  assert.equal(root.display.disabled, false, 're-enabled by the fresh publication');
  assert.equal(draft.submit.disabled, true, 'a held form submit is never re-enabled here');
});

test('UI6 an off-route dialog renders its submit and write buttons disabled until it is back on its page', async () => {
  const { fetch } = hostedFixture();
  const { win, modal } = await viewsWith(fetch);
  const form = inlineForm('save-search');
  const submit = { disabled: false, title: '', dataset: {}, closest: s => (s === 'form[data-held="1"]' && form.dataset.held ? form : null) };
  const tagged = () => [submit].filter(b => b.dataset.offRouteHeld);
  modal.open = true;
  modal.querySelectorAll = (s) => (/data-dirty|data-held|data-saving/.test(s) && !/off-route/.test(s) ? [form]
    : s === '[data-off-route-held]' ? tagged() : /\[type=submit\]/.test(s) ? [submit] : []);
  form.closest = s => (s === '#modal' ? modal : null);
  win.EW.current.dialogRoute = '#/search';
  win.EW.current.dialogEpoch = { rev: 5 };
  win.EW.current.dialogWorkspace = 'ctx-a';
  win.location.hash = '#/settings';
  await win.EWViews.render();
  assert.equal(modal.open, true, 'kept (live work)');
  assert.equal(submit.disabled, true);
  assert.equal(submit.title, 'Return to the search page to continue.');
  win.location.hash = '#/search';
  await win.EWViews.render();
  assert.equal(submit.disabled, false, 're-enabled on return');
  assert.equal(submit.title, '');
});

test('UI7 the no-workspace pages say what to do and hide member navigation and counts', async () => {
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession({ active_workspace: '', workspace_context: null }));
    return json({ ok: true });
  };
  const t = await uiWin(fetch);
  t.win.location.hash = '#/overview'; t.win.EW.current.route = '#/overview';
  await t.win.EWViews.render(); await settleAll();
  assert.match(t.root.innerHTML, /role="status">Choose a workspace to continue\./);
  assert.equal(t.headers(), 0, 'no member header');
  assert.match(t.headerEl.innerHTML, /wordmark/);
  assert.doesNotMatch(t.headerEl.innerHTML, /primary-nav/);
  // Signed in, active workspace locator but no member marker: workspaceRequiredPage.
  const u = await uiWin(async (url) => (url === '/api/v1/session' ? json(hostedSession({ workspace_context: null, frontend_contract: null, active_workspace: '' })) : json({ ok: true })));
  u.win.EW.current.activeWorkspace = 'ws-a'; u.win.EW.current.workspaceContext = '';
  u.win.location.hash = '#/overview'; u.win.EW.current.route = '#/overview';
  await u.win.EWViews.render(); await settleAll();
  assert.match(u.root.innerHTML, /Workspace required/);
  assert.match(u.root.innerHTML, /role="status">Choose a workspace to continue\./);
  assert.equal(u.headers(), 0);
});

test('UI8 the refusal chooser preselects another workspace; an unconfirmed switch keeps the chosen one', async () => {
  const fetch = async (url) => {
    if (url === '/api/v1/session/workspace') return json(hostedSession({ active_workspace: 'ws-proof', workspace_context: 'ctx-proof', workspace_switch_revision: 6, frontend_contract: null }));
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const win = loadViews(fetch); await flushBoot();
  const main = mountMain(win);
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-proof']; } };
  await win.EWViews.handleSubmit(fakeForm('workspace-choice'));
  await settleAll();
  assert.match(main.innerHTML, /<option value="ws-a" selected>/, 'another workspace is preselected');
  assert.doesNotMatch(main.innerHTML, /<option value="ws-proof" selected>/);
  assert.equal((main.innerHTML.match(/needs a different version of the app/g) || []).length, 1, 'the refusal text appears once on the page');
  assert.deepEqual(toasts, [], 'no toast repeats it');
  // Unconfirmed (not a refusal of the workspace): the re-confirm keeps it.
  win.EW.current.refusedWorkspace = '';
  win.EW.current.bootRefused = win.EW.notices.fenceUncertain;
  win.EW.current.bootAdmitted = false;
  win.EW.current.activeWorkspace = 'ws-b';
  await win.EWViews.render();
  assert.match(main.innerHTML, /<option value="ws-b" selected>/);
});

test('UI9 narrow screens keep the page end clear of a showing toast', () => {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  assert.match(css, /@media\(max-width:760px\)\{body:has\(#toast:not\(:empty\)\)\{padding-bottom:\d+px\}\}/);
});

test('UI10 Settings access shows a readable identity and folds contract and context into Technical details', async () => {
  for (const [extra, name] of [[{ principal: 'oidc:https://i.example:u-owner', subject: 'u-owner' }, 'u-owner'],
    [{ principal: 'oidc:https://i.example:u-owner', email: 'owner@example.com' }, 'owner@example.com'],
    [{ principal: 'oidc:https://i.example:u-owner' }, 'u-owner']]) {
    const fetch = async (url) => (url === '/api/v1/session' ? json(hostedSession(extra))
      : json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } }));
    const t = await uiWin(fetch);
    t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
    await t.win.EWViews.render(); await settleAll();
    assert.match(t.root.innerHTML, new RegExp(`Signed in as ${name.replace('.', '\\.')}\\.`));
    assert.doesNotMatch(t.root.innerHTML, /oidc:/, 'never the raw provider URI');
    assert.match(t.root.innerHTML, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Contract earwitness\/customer-ui\/2 · workspace context/);
  }
});

test('UI11 Search folds the cohort revision into Technical details', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {}; win.EW.header = () => {};
  const bundle = { query: { version: 2, mode: 'ads', q: 'kept', sort: 'newest', page: 1 }, revision: 'ea28a4e5188d77',
    result: { items: [], total: 0, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  win.EW.api = async (p) => (p === '/customer/brands' ? { brands: [] } : p === '/customer/shows' ? { shows: [] } : bundle);
  win.location.hash = '#/search?mode=ads&q=kept';
  await win.EWViews.render();
  assert.match(main.innerHTML, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Snapshot <span class="mono">ea28a4e5188d<\/span>/);
});

test('UI12 signed out: plain sign-in copy, a clear Sign in action, and no member navigation', async () => {
  for (const available of [false, true]) {
    const fetch = async (url) => (url === '/api/v1/session'
      ? json({ ok: false, error: 'sign in', signin: { available } }, false, 401) : json({ ok: true }));
    const t = await uiWin(fetch);
    t.win.location.hash = '#/overview'; t.win.EW.current.route = '#/overview';
    await t.win.EWViews.render(); await settleAll();
    if (available) {
      assert.match(t.root.innerHTML, /data-action="signin-start"[^>]*>Sign in</);
    } else {
      assert.match(t.root.innerHTML, /Sign-in isn't set up for this site yet\. Ask the workspace owner for access\./);
      assert.match(t.root.innerHTML, /<details class="tech-details"><summary>Technical details<\/summary>[^]*Browser sign-in is not configured/);
      assert.doesNotMatch(t.root.innerHTML.replace(/<details[^]*<\/details>/, ''), /Owner mode|not configured/);
    }
    assert.equal(t.headers(), 0, 'no member header while signed out');
    assert.doesNotMatch(t.headerEl.innerHTML, /primary-nav/);
  }
});

test('UI13 in-app navigation away from a typed page draft asks Leave this page? with Save, Discard and Stay', async () => {
  const listeners = {};
  const win = loadAt('#/settings', { fetch: movingSession(() => false) });
  win.addEventListener = (type, fn) => { listeners[type] = fn; };
  const docListeners = {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.addEventListener = (type, fn) => { (docListeners[type] = docListeners[type] || []).push(fn); };
  win.EW.current.route = '#/settings';
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const root = uiRoot();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? root : sel === '#modal' ? modal : null);
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {}; win.setTimeout = () => {};
  win.history.pushState = (st, t, url) => { win.location.hash = url; };
  win.location.hash = '#/settings';
  await win.EWViews.render(); await settleAll();
  const draft = inlineForm('settings-workspace');
  let drafts = [draft];
  win.document.querySelectorAll = s => (/data-dirty/.test(s) ? drafts.filter(f => f.dataset.dirty === '1') : []);
  const went = [];
  const go = win.EW.go;
  win.EW.go = (r) => { went.push(r); return go(r); };
  const click = (href) => {
    const a = { getAttribute: () => href, hasAttribute: () => false, target: '' };
    const ev = { target: { closest: s => (s === 'a[href^="#/"]' ? a : null) }, button: 0, preventDefault() {} };
    for (const fn of docListeners.click) fn(ev);
  };
  click('#/overview');
  assert.equal(modal.open, true, 'the prompt opens');
  assert.match(modal.innerHTML, /Leave this page\?/);
  assert.match(modal.innerHTML, /data-action="leave-save"[^>]*>Save</);
  assert.match(modal.innerHTML, /data-action="leave-discard"[^>]*>Discard</);
  assert.match(modal.innerHTML, /data-action="leave-stay"[^>]*>Stay</);
  assert.deepEqual(went, [], 'no navigation yet');
  assert.equal(win.EW.current.route, '#/settings');
  await win.EWViews.handleAction({ dataset: { action: 'leave-stay' } });
  assert.equal(modal.open, false);
  assert.equal(draft.dataset.dirty, '1', 'Stay keeps the draft');
  // (Browser Back/Forward and typed hashes: REV ui12-* model the history stack.)
  click('#/alerts');
  assert.equal(modal.open, true);
  // Discard navigates and drops the draft.
  await win.EWViews.handleAction({ dataset: { action: 'leave-discard' } });
  assert.deepEqual(went, ['#/alerts']);
  assert.equal(draft.dataset.dirty, undefined);
  // Non-durable forms and open dialogs never ask.
  drafts = [Object.assign(inlineForm('search'), {})];
  went.length = 0; win.EW.current.route = '#/settings';
  click('#/overview');
  assert.deepEqual(went, ['#/overview'], 'a search form is not a draft');
  drafts = [inlineForm('settings-workspace')]; went.length = 0; modal.open = true; win.EW.current.route = '#/settings';
  click('#/alerts');
  assert.deepEqual(went, ['#/alerts'], 'an open dialog (Rule 1) is never prompted over');
});

test('UI14 customer-facing copy carries no leftover internal terms', () => {
  const E = EW('#/episode/e1');
  E.current.hosted = true; E.current.recordLeftWs = '';
  assert.match(E.notFound(), /This record isn(?:'|&#39;)t available in this workspace\. It may have been removed or changed\./);
  assert.doesNotMatch(E.notFound(), /dataset revision/, 'hosted mode has no local dataset revisions');
  const views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  for (const old of ['no stored selector', 'retained basis (revision', 'they are the retained basis', 'one read cohort',
    'same basis or refuse', '<dt>Bounds basis</dt>', '· bounds basis', 'A source reference is an untrusted selector', 'The frozen basis is unreadable',
    'needs the current revision']) {
    assert.ok(!views.includes(old), 'reworded: ' + old);
  }
});

/* -- Review ui12/ui13: navigation against a realistic browser history -- */
// Browser model (ported from review ui13): entries {url, state, doc}, a
// position, tasks queued like the browser's (user Back/Forward/typed
// hash, history.go), popstate then a queued hashchange for hash entries,
// and leaving the app when traversal reaches a non-app document.
const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));
function makeBrowser(entries, i) {
  const B = { entries: entries.map(e => ({ doc: 'app', state: null, ...e })), i, q: [], left: null, log: [], L: {}, goCalls: [], writes: [], steps: 0 };
  B.cur = () => B.entries[B.i];
  B.location = { get hash() { return B.cur().url; }, set hash(v) { B.navigate(v); } };
  B.history = {
    get state() { return clone(B.cur().state); },
    get length() { return B.entries.length; },
    pushState(st, t, url) { const u = url === undefined ? B.cur().url : url; B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: u, state: clone(st), doc: 'app' }); B.i++; B.log.push('push ' + u); },
    replaceState(st, t, url) { const e = B.cur(); B.entries[B.i] = { url: url === undefined ? e.url : url, state: clone(st), doc: e.doc }; B.log.push('replace ' + B.entries[B.i].url); },
    go(d) { B.goCalls.push(d); B.q.push(() => B.traverse(d, 'app')); },
    back() { this.go(-1); }, forward() { this.go(1); },
  };
  B.fireHash = (oldUrl) => { if (oldUrl !== B.cur().url) B.q.push(() => { if (!B.left && B.L.hashchange) B.L.hashchange(); }); };
  B.traverse = (d, src) => {
    const j = B.i + d;
    B.log.push(`${src} go(${d}) ${B.i}->${j}`);
    if (d === 0 || j < 0 || j >= B.entries.length) return;
    if (B.entries[j].doc !== 'app') { B.i = j; B.left = B.entries[j].url; return; }
    const old = B.cur().url; B.i = j;
    if (B.L.popstate) B.L.popstate();
    B.fireHash(old);
  };
  // A typed hash or an in-page anchor: a new entry, popstate, then hashchange.
  B.navigate = (url) => {
    const old = B.cur().url;
    B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url, state: null, doc: 'app' }); B.i++;
    if (B.L.popstate) B.L.popstate();
    B.fireHash(old);
  };
  B.userBack = () => B.q.push(() => B.traverse(-1, 'user'));
  B.userFwd = () => B.q.push(() => B.traverse(1, 'user'));
  B.userTyped = (url) => B.q.push(() => B.navigate(url));
  B.run = async (max = 400) => { while (B.q.length) { if (++B.steps > max) throw new Error('LOOP: ' + JSON.stringify(B.log.slice(-12))); B.q.shift()(); await settleAll(10); } };
  B.urls = () => B.entries.map(e => e.url);
  return B;
}
// A rendered Settings page carries a fresh name form with the server
// value; typing marks it dirty, as markDirty does.
const pageForm = (value, revision = '1', id = 'settings-workspace', fieldName = 'workspace_name') => {
  const box = { innerHTML: '', setAttribute() {}, focus() {} };
  const field = { name: fieldName, type: 'text', value, defaultValue: value };
  const rev = { name: 'expected_revision', type: 'hidden', value: revision, defaultValue: revision };
  const submit = { type: 'submit', textContent: 'Save workspace name', disabled: false };
  return { box, field, rev, submit, dataset: {}, elements: [rev, field], getAttribute: a => (a === 'id' ? id : null), closest: () => null,
    querySelector: s => (s === '#form-errors' ? box : s === '[type=submit]' ? submit : null), querySelectorAll: s => (s === '[type=submit]' ? [submit] : []),
    reset() { field.value = field.defaultValue; rev.value = rev.defaultValue; } };
};
async function modelWin(entries, i, { moved = () => false, server = null } = {}) {
  const B = makeBrowser(entries, i);
  const store = {};
  const base = movingSession(moved);
  const fetch = async (url, opts = {}) => {
    if (opts.method && opts.method !== 'GET') B.writes.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
    const own = server && await server(url, opts);
    return own || base(url, opts);
  };
  const win = { console, URLSearchParams, AbortController, location: B.location, history: B.history, scrollTo() {}, fetch,
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {} };
  win.window = win; win.globalThis = win;
  vm.createContext(win);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  const docL = {};
  win.addEventListener = (type, fn) => { B.L[type] = fn; };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.addEventListener = (type, fn) => { (docL[type] = docL[type] || []).push(fn); };
  win.EW.current.route = B.location.hash;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const root = uiRoot();
  let html = '';
  Object.defineProperty(root, 'innerHTML', { get: () => html, set: (v) => {
    html = v;
    const m = v.match(/id="settings-workspace"><input type="hidden" name="expected_revision" value="([^"]*)"><div class="od-field"><label for="settings-name">Workspace display name<\/label><input id="settings-name" name="workspace_name" value="([^"]*)"/);
    // The placement page's inline attach form (a campaign route).
    const a = v.match(/<form id="attach-evidence"[^]*?name="expected_revision" value="([^"]*)"/);
    root.form = m ? pageForm(m[2], m[1]) : a ? pageForm('', a[1], 'attach-evidence', 'ref') : null;
    root.renders = (root.renders || 0) + 1;
  } });
  const baseQ = root.querySelector;
  root.querySelector = s => (root.form && s === '#' + root.form.getAttribute('id') ? root.form : baseQ(s));
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? root : sel === '#modal' ? modal : null);
  win.document.querySelectorAll = s => (/data-dirty/.test(s) && root.form && root.form.dataset.dirty === '1' ? [root.form] : []);
  win.EW.header = () => {}; win.EW.toast = () => {}; win.setTimeout = () => {};
  win.FormData = class { constructor(f) { this.f = f; } *entries() { for (const e of this.f.elements) if (e.name) yield [e.name, e.value]; } };
  await win.EWViews.render(); await settleAll();
  const type = (v) => { root.form.field.value = v; root.form.dataset.dirty = '1'; };
  const click = async (href) => {
    const a = { getAttribute: () => href, hasAttribute: () => false, target: '' };
    for (const fn of docL.click) fn({ target: { closest: s => (s === 'a[href^="#/"]' ? a : null) }, button: 0, preventDefault() {} });
    await settleAll(20); await B.run();
  };
  const act = async (action) => { await win.EWViews.handleAction({ dataset: { action } }); await settleAll(10); await B.run(); };
  const escape = async () => { for (const fn of docL.keydown || []) fn({ key: 'Escape' }); await B.run(); };
  const E = win.EW;
  // URL and page agree; the app never moved the history itself.
  const coherent = () => {
    if (!B.left) assert.equal(B.cur().url, E.current.route, 'URL and page agree: ' + B.urls().join(' '));
    assert.deepEqual(B.goCalls, [], 'the app never traverses history');
  };
  const restoredWith = (v) => {
    assert.ok(root.form, 'Settings rendered');
    assert.equal(root.form.field.value, v, 'entry refilled');
    assert.equal(root.form.dataset.dirty, '1', 'marked as a draft');
    assert.match(root.form.box.innerHTML, /Your unsaved entry was restored\. (Save it or discard it|This record changed since you typed it; saving will show the differences first)\./);
    assert.match(root.form.box.innerHTML, /data-action="discard-restored"[^>]*>Discard</);
  };
  return { B, win, E, root, modal, type, click, act, escape, coherent, restoredWith };
}

// Rewritten for the stash/restore design (review ui13): Back/Forward can
// no longer be cancelled, so the invariant "no silent draft loss on
// Back/Forward" is asserted as: history entries intact, the earlier page
// is reached, and the draft is restored on return.
test('REV ui12-R1: Back then Stay keeps every history entry and the draft; a later Back prompts again; Discard reaches the earlier page', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2);
  t.type('Draft name');
  t.B.userBack(); await t.B.run();
  assert.equal(t.modal.open, false, 'Back is not cancellable: no prompt');
  assert.equal(t.E.current.route, '#/alerts', 'the earlier page is reached');
  assert.deepEqual(t.B.urls(), ['https://ext', '#/alerts', '#/settings'], 'entries intact');
  t.coherent();
  t.B.userFwd(); await t.B.run();
  assert.equal(t.E.current.route, '#/settings');
  t.restoredWith('Draft name');
  assert.deepEqual(t.B.writes, [], 'restoring never submits');
  t.coherent();
});

// Rewritten for the stash/restore design (review ui13); see R1.
test('REV ui12-R2: a typed hash then Stay keeps the draft; Back prompts instead of re-rendering; earlier pages stay reachable', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2);
  t.type('Typed then left');
  t.B.userTyped('#/campaigns'); await t.B.run();
  assert.equal(t.E.current.route, '#/campaigns', 'a typed hash proceeds');
  assert.deepEqual(t.B.urls(), ['https://ext', '#/alerts', '#/settings', '#/campaigns']);
  t.coherent();
  t.B.userBack(); await t.B.run();
  t.restoredWith('Typed then left');
  t.B.userBack(); await t.B.run();
  assert.equal(t.E.current.route, '#/alerts', 'earlier pages stay reachable');
  assert.equal(t.B.left, null);
  t.coherent();
});

test('UI15 a double Back and a Forward never lose entries or the draft; URL and page always agree', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/overview' }, { url: '#/alerts' }, { url: '#/settings' }], 3);
  t.type('Twice back');
  t.B.userBack(); t.B.userBack(); await t.B.run();
  assert.equal(t.E.current.route, '#/overview');
  t.coherent();
  t.B.userFwd(); await t.B.run(); t.coherent();
  t.B.userFwd(); await t.B.run(); t.coherent();
  t.restoredWith('Twice back');
  // Discard on the restored entry returns to the rendered values.
  await t.win.EWViews.handleAction({ dataset: { action: 'discard-restored' }, closest: s => (s === 'form' ? t.root.form : null) });
  assert.equal(t.root.form.field.value, 'Alpha');
  assert.equal(t.root.form.dataset.dirty, undefined);
  assert.equal(t.root.form.box.innerHTML, '');
  assert.deepEqual(t.B.urls(), ['https://ext', '#/overview', '#/alerts', '#/settings']);
});

test('UI16 cancellable navigation: links and Choose workspace ask first; Stay does nothing; any close ends the prompt; Discard proceeds inside the app', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1);
  t.type('Kept');
  await t.click('#/alerts');
  assert.equal(t.modal.open, true, 'a link asks');
  assert.deepEqual(t.B.urls(), ['https://ext', '#/settings'], 'intercepted before the hash changes');
  await t.act('leave-stay');
  assert.equal(t.E.current.route, '#/settings');
  assert.equal(t.root.form.field.value, 'Kept', 'Stay does nothing');
  // Escape and a code-driven close both end the prompt.
  await t.click('#/alerts'); await t.escape();
  assert.equal(t.E.current.leaveTarget, null, 'Escape clears the destination');
  await t.click('#/alerts'); t.E.closeDialog();
  assert.equal(t.E.current.leaveTarget, null, 'a code-driven close clears it too');
  // Choose workspace is cancellable too.
  t.E.current.writeFence = { to: 'ws-b', op: 1, rev: 5 };
  t.E.current.route = '#/settings';
  t.root.innerHTML = 'no chooser here'; t.root.form = pageForm('Alpha'); t.type('Kept');
  await t.act('choose-workspace');
  assert.equal(t.modal.open, true, 'Choose workspace asks about the draft');
  await t.act('leave-stay');
  assert.equal(t.E.current.focusChooser, false, 'Stay cancels the chooser hop');
  t.E.current.writeFence = null;
  await t.click('#/alerts');
  await t.act('leave-discard');
  assert.equal(t.E.current.route, '#/alerts', 'Discard proceeds with E.go');
  assert.equal(t.B.left, null, 'never leaves the app');
  assert.deepEqual(t.B.urls(), ['https://ext', '#/settings', '#/alerts']);
  t.coherent();
  // A discarded draft is not restored on Back.
  t.B.userBack(); await t.B.run();
  assert.equal(t.root.form.field.value, 'Alpha');
  assert.equal(t.root.form.dataset.dirty, undefined);
});

test('UI18 an entry before the app, the skip link, and a workspace change', async () => {
  // Back past the first app entry leaves the app; the app adds no traversal (beforeunload guards drafts).
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1);
  t.type('Unload guard');
  t.B.userBack(); await t.B.run();
  assert.equal(t.B.left, 'https://ext');
  assert.deepEqual(t.B.goCalls, []);
  // The skip link (#main) is an in-page anchor, never a route.
  const u = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1);
  u.type('Skip link');
  const renders = u.root.renders;
  u.B.userTyped('#main'); await u.B.run();
  assert.equal(u.E.current.route, '#/settings', 'no route change');
  assert.equal(u.root.renders, renders, 'no render');
  assert.equal(u.modal.open, false, 'no prompt');
  assert.equal(u.root.form.field.value, 'Skip link', 'the draft is untouched');
  u.B.userBack(); await u.B.run();
  assert.equal(u.root.renders, renders, 'back from #main to the same route renders nothing');
  assert.equal(u.root.form.field.value, 'Skip link');
  // A workspace change drops a stashed draft, saying so; never restored into B.
  let moved = false;
  const w = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { moved: () => moved });
  w.type('Typed in A');
  w.B.userBack(); await w.B.run();
  moved = true;
  await w.E.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(w.E.current.activeWorkspace, 'ws-b');
  assert.match(w.root.top(), /Your unsaved entry in ws-a was not applied\./);
  w.B.userFwd(); await w.B.run();
  assert.equal(w.E.current.route, '#/settings');
  assert.equal(w.root.form.field.value, 'Beta', 'never restored into another workspace: ws-b values as rendered');
  assert.equal(w.root.form.dataset.dirty, undefined);
});

test('UI19 the stash is per route and capped at ten', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings?n=0' }], 1);
  for (let n = 0; n <= 10; n++) {
    t.type('draft ' + n);
    t.B.userTyped(`#/settings?n=${n + 1}`); await t.B.run();
  }
  for (let n = 10; n >= 0; n--) {
    t.B.userBack(); await t.B.run();
    assert.equal(t.E.current.route, `#/settings?n=${n}`);
    if (n >= 1) t.restoredWith('draft ' + n);
    else assert.equal(t.root.form.field.value, 'Alpha', 'the oldest of eleven was dropped');
  }
  assert.match(t.root.top(), /Your unsaved entry on the settings page was not kept\./, 'the eviction is announced');
  t.coherent();
});

test('UI20 fuzzed Back/Forward/typed/link/Stay/Discard/Escape sequences never loop, lose entries or split URL from page', async () => {
  const pages = ['#/overview', '#/alerts', '#/settings', '#/campaigns', '#/brands'];
  let seed = 7; const rnd = n => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n; };
  for (let run = 0; run < 25; run++) {
    const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: pages[rnd(5)] }], 1);
    const seq = [];
    for (let k = 0; k < 12 && !t.B.left; k++) {
      const a = rnd(9);
      const before = t.B.urls().length;
      if (a === 0) { seq.push('back'); t.B.userBack(); await t.B.run(); }
      else if (a === 1) { seq.push('fwd'); t.B.userFwd(); await t.B.run(); }
      else if (a === 2) { const p = pages[rnd(5)]; seq.push('typed ' + p); t.B.userTyped(p); await t.B.run(); }
      else if (a === 3) { const p = pages[rnd(5)]; seq.push('link ' + p); await t.click(p); }
      else if (a === 4 && t.root.form) { seq.push('type'); t.type('v' + k); }
      else if (a === 5 && t.modal.open) { seq.push('stay'); await t.act('leave-stay'); }
      else if (a === 6 && t.modal.open) { seq.push('discard'); await t.act('leave-discard'); }
      else if (a === 7 && t.modal.open) { seq.push('escape'); await t.escape(); }
      else if (a === 8) { seq.push('back,back'); t.B.userBack(); t.B.userBack(); await t.B.run(); }
      if (t.B.left) { assert.ok(/back/.test(seq[seq.length - 1]), 'only a user Back leaves the app: ' + seq.join(' > ')); break; }
      assert.ok(t.B.urls().length >= before || /typed|link|discard/.test(seq[seq.length - 1]), 'no entry destroyed: ' + seq.join(' > '));
      if (!t.modal.open) assert.equal(t.B.cur().url, t.E.current.route, 'URL and page agree: ' + seq.join(' > '));
      assert.deepEqual(t.B.goCalls, []);
      if (!t.modal.open) assert.equal(t.E.current.leaveTarget || null, null, 'no stale destination: ' + seq.join(' > '));
    }
  }
});

test('UI21 a restored entry keeps the revision it was typed against: saving over a newer record conflicts, never overwrites', async () => {
  let stored = { name: 'Alpha', revision: 1 };
  const server = async (url, opts = {}) => {
    if (url !== '/api/v1/customer/preferences') return null;
    if (opts.method === 'PUT') {
      const body = JSON.parse(opts.body);
      if (body.expected_revision !== stored.revision) {
        return json({ ok: false, error: { code: 'STALE_REVISION', message: 'stale', details: { current_revision: stored.revision } } }, false, 412);
      }
      stored = { name: body.workspace_name, revision: stored.revision + 1 };
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: stored.name, revision: stored.revision } });
    }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: stored.name, revision: stored.revision } });
  };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { server });
  assert.equal(t.root.form.rev.value, '1');
  t.type('Mine');
  t.B.userBack(); await t.B.run();
  stored = { name: 'Theirs', revision: 2 }; // saved elsewhere meanwhile
  t.B.userFwd(); await t.B.run();
  assert.equal(t.root.form.field.value, 'Mine', 'entry restored');
  assert.equal(t.root.form.rev.value, '1', 'with the revision it was typed against, not the fresh 2');
  assert.match(t.root.form.box.innerHTML, /Your unsaved entry was restored\. This record changed since you typed it; saving will show the differences first\./);
  const epoch = t.E.current.displayedWorkspaceSwitchRevision;
  await t.win.EWViews.handleSubmit(t.root.form);
  await settleAll();
  const put = t.B.writes.find(w => w.url === '/api/v1/customer/preferences');
  assert.equal(put.body.expected_revision, 1, 'the save carries the typed-against revision');
  assert.equal(epoch, 5, 'the epoch header basis stays the current displayed one');
  assert.deepEqual(stored, { name: 'Theirs', revision: 2 }, 'no overwrite');
  assert.match(t.root.form.box.innerHTML, /This record changed while you were editing\./, 'the existing conflict UI');
  assert.match(t.root.form.box.innerHTML, /Reload and restore my entries/);
  // An unchanged record restores with the plain notice.
  const u = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2);
  u.type('Same'); u.B.userBack(); await u.B.run(); u.B.userFwd(); await u.B.run();
  assert.match(u.root.form.box.innerHTML, /Your unsaved entry was restored\. Save it or discard it\./);
});

test('UI22 a stashed entry restores across a context rotation within the same workspace', async () => {
  let rotated = false;
  const server = async (url) => {
    const ctx = rotated ? 'ctx-a2' : 'ctx-a';
    if (url === '/api/v1/session') return json(hostedSession({ workspace_context: ctx, workspace_switch_revision: rotated ? 6 : 5 }));
    return json({ ok: true, workspace_context: ctx, preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { server });
  t.type('Across rotation');
  t.B.userBack(); await t.B.run();
  rotated = true;
  await t.E.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(t.E.current.workspaceContext, 'ctx-a2', 'context rotated');
  assert.equal(t.E.current.activeWorkspace, 'ws-a', 'same workspace');
  assert.doesNotMatch(t.root.top(), /was not applied/, 'nothing dropped');
  t.B.userFwd(); await t.B.run();
  t.restoredWith('Across rotation');
});

test('REV ui14-X1: a held A draft is never restored into B after another tab switched; its Save never reaches B', async () => {
  let moved = false;
  const w = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { moved: () => moved });
  w.type('Typed in A');
  moved = true; // another tab switches to ws-b while this page is held
  await w.E.api('/customer/overview').catch(() => {});
  await settleAll(20);
  assert.equal(w.E.current.activeWorkspace, 'ws-b');
  assert.equal(w.root.form.dataset.held, '1', 'held, not re-rendered');
  w.B.userBack(); await w.B.run();
  assert.match(w.root.top(), /Your unsaved entry in ws-a was not applied\./, 'the drop is announced');
  w.B.userFwd(); await w.B.run();
  assert.equal(w.E.current.route, '#/settings');
  assert.notEqual(w.root.form.field.value, 'Typed in A', 'never restored into ws-b');
  assert.equal(w.root.form.dataset.dirty, undefined);
  await w.win.EWViews.handleSubmit(w.root.form); await settleAll();
  assert.ok(!w.B.writes.some(x => x.body && x.body.workspace_name === 'Typed in A'), 'the A entry never reaches B: ' + JSON.stringify(w.B.writes));
});

test('REV ui14-X2: a failed read on return keeps the stash; the render after Retry restores it', async () => {
  let fail = false;
  const server = async (url) => (url === '/api/v1/customer/preferences' && fail ? json({ ok: false, error: { code: 'X', message: 'down' } }, false, 500) : null);
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { server });
  t.type('Precious');
  t.B.userBack(); await t.B.run();
  fail = true;
  t.B.userFwd(); await t.B.run();
  assert.equal(t.root.form, null, 'the error page has no form');
  fail = false;
  await t.act('retry');
  t.restoredWith('Precious');
});

test('UI23 workspace generations: A -> B -> A drops an A draft; a signed-out follow-up into B never restores it', async () => {
  // A -> B -> A by another tab while held: same name, later generation.
  let ws = 'a';
  const server = async (url) => {
    if (url === '/api/v1/session') return json(ws === 'a' ? hostedSession({ workspace_switch_revision: ws === 'a' && server.back ? 7 : 5 }) : sessB());
    return null;
  };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { server, moved: () => ws === 'b' });
  t.type('Before the round trip');
  ws = 'b'; await t.E.api('/customer/overview').catch(() => {}); await settleAll(20);
  // Back to A (a canonical re-read; the held page is not re-rendered).
  ws = 'a'; server.back = true; await t.win.EWViews.reconcileCanonical(); await settleAll(20);
  assert.equal(t.E.current.activeWorkspace, 'ws-a');
  t.B.userBack(); await t.B.run();
  t.B.userFwd(); await t.B.run();
  assert.notEqual(t.root.form.field.value, 'Before the round trip', 'a later generation of A never gets it back');
  assert.match(t.root.top(), /Your unsaved entry in ws-a was not applied\./);
  // Signed-out follow-up (401 on a write) whose re-read lands in B: held, then Back/Forward.
  let inB = false;
  const server2 = async (url, opts = {}) => {
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') { inB = true; return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'sign in' } }, false, 401); }
    return null;
  };
  const u = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2, { server: server2, moved: () => inB });
  u.type('Typed before 401');
  await u.win.EWViews.handleSubmit(u.root.form); await settleAll(20);
  assert.equal(u.E.current.activeWorkspace, 'ws-b', 'the follow-up re-read moved to ws-b');
  u.B.userBack(); await u.B.run();
  u.B.userFwd(); await u.B.run();
  assert.notEqual(u.root.form.field.value, 'Typed before 401', 'never restored into ws-b');
  assert.match(u.root.top(), /Your unsaved entry in ws-a was not applied\./);
});

/* -- DEMO2 pass 17 (code review r13: DEMO2-R13-01, R13-03) --
   A Save answered 401 takes the access boundary. Owner decision: typed
   text is not kept across the sign-in redirect; the signed-out page says
   so plainly and shows the user's own typed text, escaped and read-only,
   to copy. Still signed in, or signed in again in the same tab without a
   reload, the entry is refilled; a dropped one is told. */
const SIGNED_OUT_COPY = /You were signed out\. Copy anything you typed below before you sign in again; it won(?:'|&#39;)t be kept\./;
const signOutServer = (state) => async (url, opts = {}) => {
  if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
    state.out = state.signOut;
    return json({ ok: false, error: { code: 'SIGNIN_REQUIRED', message: 'sign in' } }, false, 401);
  }
  if (url === '/api/v1/session' && state.out) return json({ ok: false, error: 'signin', signin: { available: true } }, false, 401);
  if (url === '/api/v1/session' && state.session) return json(state.session);
  return null;
};
const unloadPrompt = (t) => { let prompted = false; t.B.L.beforeunload({ preventDefault() { prompted = true; } }); return prompted; };

test('R13-03 (W4, V4) a 401 Save whose re-read is still signed in: the typed entry is refilled with the restored notice; nothing else is written', async () => {
  const state = { signOut: false };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server: signOutServer(state) });
  t.type('Typed before 401');
  await t.win.EWViews.handleSubmit(t.root.form); await settleAll(40);
  assert.ok(t.E.current.principal, 'fixture: the re-read is still signed in');
  t.restoredWith('Typed before 401');
  assert.equal(t.B.writes.length, 1, 'refilling submitted the entry');
  assert.doesNotMatch(t.root.innerHTML + t.root.top(), /signed out/i, 'a sign-out claim while still signed in');
});

test('R13-01 P1 / R13-03 (S1b, V1) a 401 Save whose re-read is signed out: the sign-in page says the typed text is not kept and shows it to copy; no server value; the leave prompt counts it', async () => {
  const state = { signOut: true };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server: signOutServer(state) });
  t.type('Typed before 401');
  await t.win.EWViews.handleSubmit(t.root.form); await settleAll(40);
  assert.equal(t.E.current.principal, null, 'fixture: the re-read is signed out');
  const shown = t.root.innerHTML + t.root.top();
  assert.match(shown, /data-action="signin-start"/, 'fixture: the sign-in state is shown');
  assert.match(shown, SIGNED_OUT_COPY, 'the signed-out page does not say what happens to the typed text');
  assert.match(shown, /<textarea[^>]*readonly[^>]*>Typed before 401<\/textarea>/, 'the typed text is not shown read-only to copy');
  assert.doesNotMatch(shown, /Alpha/, 'a server value is shown on the signed-out page');
  assert.doesNotMatch(shown, /was not applied/, 'told "not applied" while signed out');
  assert.equal(unloadPrompt(t), true, 'the leave-page prompt ignores the held typed text');
});

test('R13-01 P2 signed out, then signed in again in the same tab without a reload: the entry is refilled', async () => {
  const state = { signOut: true };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server: signOutServer(state) });
  t.type('Typed before 401');
  await t.win.EWViews.handleSubmit(t.root.form); await settleAll(40);
  assert.equal(t.E.current.principal, null, 'fixture: signed out');
  state.out = false; state.signOut = false;
  await t.E.boot(); await t.win.EWViews.render(); await settleAll(20);
  assert.equal(t.E.current.activeWorkspace, 'ws-a');
  t.restoredWith('Typed before 401');
  assert.doesNotMatch(t.root.top(), /was not applied/);
  assert.doesNotMatch(t.root.innerHTML + t.root.top(), SIGNED_OUT_COPY, 'the signed-out copy outlived the sign-in');
});

// DEMO2 pass 18 owner decision: only the same person is told, in another
// workspace; another person is never shown the previous person's workspace.
test('R13-01 P2 signed out, then signed in again into another workspace or as another person: the entry is not refilled; the same person is told, another person is not', async () => {
  for (const session of [hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }), hostedSession({ principal: 'kim' })]) {
    const samePerson = session.principal === 'sam';
    const state = { signOut: true };
    const moved = () => !!(state.session && state.session.active_workspace === 'ws-b');
    const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server: signOutServer(state), moved });
    t.type('Typed before 401');
    await t.win.EWViews.handleSubmit(t.root.form); await settleAll(40);
    state.out = false; state.signOut = false; state.session = session;
    await t.E.boot(); await t.win.EWViews.render(); await settleAll(20);
    assert.ok(t.root.form, 'Settings rendered');
    assert.notEqual(t.root.form.field.value, 'Typed before 401', `refilled for ${session.principal} in ${session.active_workspace}`);
    if (samePerson) assert.match(t.root.top(), /Your unsaved entry in ws-a was not applied\./, 'the dropped entry was not told');
    else assert.doesNotMatch(t.root.top() + t.root.innerHTML, /was not applied|ws-a was/, 'another person was told about the previous person\'s entry');
  }
});

/* -- DEMO2 pass 18 (code review r14: DEMO2-R14-01, R14-N2) -- */
test('R14-N2 signed out, then another person signs in and lands on the chooser: the typed text is gone and the leave-page prompt stops counting it', async () => {
  const state = { signOut: true };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server: signOutServer(state) });
  t.type('Typed before 401');
  await t.win.EWViews.handleSubmit(t.root.form); await settleAll(40);
  assert.equal(unloadPrompt(t), true, 'fixture: the leave prompt counts the held typed text');
  state.out = false; state.signOut = false;
  state.session = hostedSession({ principal: 'kim', active_workspace: null, workspace_context: null, workspaces: ['ws-a'] });
  await t.E.boot(); await t.win.EWViews.render(); await settleAll(20);
  assert.match(t.root.innerHTML, /Signed in as kim/, 'fixture: the chooser is shown');
  assert.equal(t.E.current.signedOutHeld, null, 'the held typed text survived the identity change');
  assert.doesNotMatch(t.root.innerHTML + t.root.top(), /Typed before 401/, 'the previous typed text is shown');
  assert.equal(unloadPrompt(t), false, 'the leave-page prompt still counts the previous person\'s typed text');
  assert.doesNotMatch(t.root.top() + t.root.innerHTML + JSON.stringify(t.E.current.outcomeNotice || null), /was not applied/, 'another person was told about the previous person\'s entry');
});

test('R14-01 a Settings save whose answer shows only a role change: the form is saved and clean, not restored as an unsaved draft', async () => {
  const state = { role: 'owner', ctx: 'ctx-a', name: 'Alpha', rev: 1 };
  const server = async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ role: state.role, workspace_context: state.ctx }));
    if (url === '/api/v1/customer/preferences' && opts.method === 'PUT') {
      // The role changes at the server as the save commits.
      state.role = 'editor'; state.ctx = 'ctx-a-editor';
      state.name = JSON.parse(opts.body || '{}').workspace_name; state.rev = 2;
      return json({ ok: true, workspace_context: state.ctx, preferences: { workspace_name: state.name, revision: state.rev } });
    }
    if (url.startsWith('/api/v1/session/')) return null;
    return json({ ok: true, workspace_context: state.ctx, preferences: { workspace_name: state.name, revision: state.rev } });
  };
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1, { server });
  const toasts = []; t.E.toast = m => toasts.push(m);
  t.type('Acme renamed');
  await t.win.EWViews.handleSubmit(t.root.form); await settleAll(80);
  assert.equal(t.B.writes.length, 1, 'fixture: one save');
  assert.ok(toasts.includes('Saved. Your role in this workspace changed.'), 'no role-change save copy: ' + toasts.join(' | '));
  assert.ok(t.root.form, 'Settings rendered again');
  assert.doesNotMatch(t.root.form.box.innerHTML, /unsaved entry was restored/, 'the saved entry came back as an unsaved draft');
  assert.notEqual(t.root.form.dataset.dirty, '1', 'the saved form is marked unsaved');
  assert.equal(t.root.form.rev.value, '2', 'the form carries the revision from before the save');
  assert.equal(t.root.form.field.value, 'Acme renamed');
  assert.equal(unloadPrompt(t), false, 'the leave-page prompt counts the saved entry');
});

test('UI24 a campaign route (placement attach form) stashes and restores like any page', async () => {
  const draft = { campaign: { id: 3, name: 'Spring push', revision: 4 }, pins: {}, placements: [{ id: 'p1', scope: 'show', show_id: 's1', expected_count: 1 }],
    allocation: { evaluations: {}, citations: {} }, coverage_by_placement: {}, episode_titles: {} };
  const server = async (url) => (url === '/api/v1/customer/campaigns/3/draft' ? json({ ok: true, workspace_context: 'ctx-a', draft }) : null);
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/campaigns' }, { url: '#/campaign/3/placement/p1' }], 2, { server });
  assert.equal(t.root.form && t.root.form.getAttribute('id'), 'attach-evidence', 'placement page rendered');
  assert.equal(t.root.form.rev.value, '4');
  t.type('evidence:ev-9');
  t.B.userBack(); await t.B.run();
  assert.equal(t.E.current.route, '#/campaigns');
  t.B.userFwd(); await t.B.run();
  t.restoredWith('evidence:ev-9');
  assert.equal(t.root.form.rev.value, '4');
  assert.deepEqual(t.B.writes, []);
});

test('UI25 our own navigation handlers (search submit, mode, sort) ask before leaving a page draft', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1);
  t.type('Keep me');
  const go = t.E.go; const went = []; t.E.go = (r) => { went.push(r); };
  t.win.FormData = class { *entries() { yield ['q', 'cola']; } };
  await t.win.EWViews.handleSubmit({ getAttribute: () => 'overview-search', dataset: {} });
  assert.equal(t.modal.open, true, 'search submit asks');
  assert.deepEqual(went, []);
  await t.act('leave-stay');
  const sort = { id: 'search-sort', value: 'oldest' };
  t.win.EWViews.onChange({ target: sort });
  assert.equal(t.modal.open, true, 'sort asks');
  assert.equal(sort.value, 'newest', 'Stay keeps the applied sort shown');
  await t.act('leave-stay');
  t.E.go = go;
});

test('REV ui15-X7: eleven placement drafts left with Back: the evicted first one is announced, never silent', async () => {
  const placements = Array.from({ length: 11 }, (_, k) => ({ id: 'p' + (k + 1), scope: 'show', show_id: 's1', expected_count: 1 }));
  const draft = { campaign: { id: 3, name: 'Spring push', revision: 4 }, pins: {}, placements,
    allocation: { evaluations: {}, citations: {} }, coverage_by_placement: {}, episode_titles: {} };
  const server = async (url) => (url === '/api/v1/customer/campaigns/3/draft' ? json({ ok: true, workspace_context: 'ctx-a', draft }) : null);
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/campaigns' }], 1, { server });
  for (let k = 1; k <= 11; k++) {
    await t.click('#/campaign/3/placement/p' + k);
    assert.equal(t.root.form && t.root.form.getAttribute('id'), 'attach-evidence');
    t.type('evidence:ev-' + k);
    t.B.userBack(); await t.B.run();
    assert.equal(t.E.current.route, '#/campaigns');
  }
  assert.match(t.root.top(), /Your unsaved entry on campaign 3 was not kept\./, 'the eviction is announced on the page');
  await t.click('#/campaign/3/placement/p11');
  t.restoredWith('evidence:ev-11');
});

test('REV ui15-X8: a stashed draft for this workspace still warns on tab close', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/alerts' }, { url: '#/settings' }], 2);
  t.type('Stashed then closed'); t.root.form.contains = () => false;
  let ev = { prevented: false, preventDefault() { this.prevented = true; }, returnValue: undefined };
  t.B.L.beforeunload(ev); assert.equal(ev.prevented, true, 'on-page draft guarded');
  t.B.userBack(); await t.B.run();
  assert.equal(t.E.current.route, '#/alerts');
  ev = { prevented: false, preventDefault() { this.prevented = true; }, returnValue: undefined };
  t.B.L.beforeunload(ev);
  assert.equal(ev.prevented, true, 'a stashed draft still warns on unload');
  // Restored, then discarded: nothing left to warn about.
  t.B.userFwd(); await t.B.run();
  await t.win.EWViews.handleAction({ dataset: { action: 'discard-restored' }, closest: s => (s === 'form' ? t.root.form : null) });
  ev = { prevented: false, preventDefault() { this.prevented = true; }, returnValue: undefined };
  t.B.L.beforeunload(ev);
  assert.equal(ev.prevented, false);
});

test('UI26 several stash entries dropped at once are counted, not overwritten', async () => {
  let moved = false;
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings?n=1' }], 1, { moved: () => moved });
  t.type('one');
  t.B.userTyped('#/settings?n=2'); await t.B.run();
  t.type('two');
  t.B.userTyped('#/alerts'); await t.B.run();
  moved = true;
  await t.win.EWViews.reconcileCanonical(); await settleAll(20);
  assert.equal(t.E.current.activeWorkspace, 'ws-b');
  assert.match(t.root.top(), /2 unsaved entries were not applied\./);
});

test('UI17 a context rotation within the same workspace is never reported as another workspace', async () => {
  let gate = null;
  const base = movingSession(() => false);
  const fetch = async (url, opts = {}) => {
    if (url === '/api/v1/customer/alerts/7?summaries=0') { await new Promise(r => { gate = r; }); return json({ alert: { id: 7 }, workspace_context: 'ctx-a' }); }
    return base(url, opts);
  };
  const u = await uiWin(fetch);
  u.win.location.hash = '#/alerts'; u.win.EW.current.route = '#/alerts';
  await u.win.EWViews.render(); await settleAll();
  const opener = u.win.EWViews.handleAction({ dataset: { action: 'edit-alert', id: '7' } });
  await settleAll(3);
  u.win.EW.current.workspaceContext = 'ctx-a-rotated'; // same workspace, new context
  gate(); await opener; await settleAll(5);
  assert.equal(u.modal.open, false, 'the stale opener still opens nothing');
  assert.equal(u.win.EW.current.activeWorkspace, 'ws-a');
  assert.equal(u.win.EW.current.outcomeNotice, undefined, 'no "another workspace" notice');
  assert.doesNotMatch(u.root.top(), /another workspace/);
});

/* -- Site pass: on-demand episodes, legacy campaigns, favicon, leave Save, action copy -- */
// A dialog whose picker controls are real enough to fill and read.
const pickerModal = (prefix) => {
  const el = (id) => ({ id, innerHTML: '', value: '', disabled: false, dataset: {} });
  const parts = { show: el(prefix + '-show'), episode: el(prefix + '-episode'), showsStatus: el(prefix + '-shows-status'), episodesStatus: el(prefix + '-episodes-status') };
  const byId = { [`#${prefix}-show`]: parts.show, [`#${prefix}-episode`]: parts.episode, [`#${prefix}-shows-status`]: parts.showsStatus, [`#${prefix}-episodes-status`]: parts.episodesStatus };
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: s => byId[s] || null, querySelectorAll: () => [] };
  return { modal, parts };
};
const showsWin = async (prefix, respond) => {
  const calls = [];
  const fetch = async (url, opts = {}) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    const own = respond && await respond(url, opts);
    if (own) return own;
    if (url === '/api/v1/customer/campaigns/3') return json({ ok: true, workspace_context: 'ctx-a', campaign: { id: 3, revision: 4, mode: 'research' } });
    if (url === '/api/v1/customer/campaigns/3/draft') return json({ ok: true, workspace_context: 'ctx-a', draft: { campaign: { id: 3, revision: 4 }, placements: [], pins: {} } });
    if (url === '/api/v1/customer/shows') return json({ ok: true, workspace_context: 'ctx-a', shows: Array.from({ length: 255 }, (_, k) => ({ id: 's' + k, display_title: 'Show ' + k })) });
    if (url.startsWith('/api/v1/customer/shows/')) return json({ ok: true, workspace_context: 'ctx-a', episodes: [{ id: 'e2', ep_title: 'Second' }, { id: 'e1', ep_title: 'First', pub_date: '2026-01-02' }] });
    return json({ ok: true, workspace_context: 'ctx-a', items: [] });
  };
  const win = loadViews(fetch); await settleAll();
  const { modal, parts } = pickerModal(prefix);
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? { innerHTML: '', querySelectorAll: () => [] } : null);
  win.document.querySelectorAll = () => [];
  win.setTimeout = () => {}; win.EW.toast = () => {}; win.EW.header = () => {};
  win.EW.current.route = '#/campaign/3'; win.location.hash = '#/campaign/3';
  return { win, modal, parts, calls, perShow: () => calls.filter(u => /\/customer\/shows\/./.test(u)) };
};

test('SITE1 Add research target sends no per-show reads until a show is picked, then exactly one; a failed read says so with Retry', async () => {
  let limited = true;
  const t = await showsWin('target', async (url) => (url === '/api/v1/customer/shows/s7' && limited
    ? json({ ok: false, error: { code: 'RATE_LIMITED', message: 'slow down' } }, false, 429) : null));
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } });
  await settleAll();
  assert.equal(t.modal.open, true, 'the dialog opened');
  assert.deepEqual(t.perShow(), [], 'no per-show request on open, with 255 shows');
  assert.match(t.modal.innerHTML, /id="target-episode" name="episode_id" disabled><option value="">Choose a show first/);
  t.parts.show.value = 's7';
  t.win.EWViews.onChange({ target: t.parts.show });
  await settleAll();
  assert.deepEqual(t.perShow(), ['/api/v1/customer/shows/s7'], 'exactly one read, for the picked show');
  assert.match(t.parts.episodesStatus.innerHTML, /Too many requests right now\. Wait a moment, then try again\./);
  assert.match(t.parts.episodesStatus.innerHTML, /data-action="picker-episodes-retry"[^>]*>Retry</);
  assert.equal(t.parts.episode.disabled, true, 'never a silently partial list');
  limited = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'picker-episodes-retry', prefix: 'target' } });
  await settleAll();
  assert.equal(t.parts.episode.disabled, false);
  assert.match(t.parts.episode.innerHTML, /<option value="e1">First \(2026-01-02\)<\/option><option value="e2">Second<\/option>/);
  assert.equal(t.parts.episodesStatus.innerHTML, '');
});

test('SITE1b the placement dialog lists episodes on demand too, keeping a stored choice', async () => {
  const t = await showsWin('placement', async (url) => (url === '/api/v1/customer/campaigns/3/draft'
    ? json({ ok: true, workspace_context: 'ctx-a', draft: { campaign: { id: 3, revision: 4 }, pins: {}, placements: [{ id: 'p1', scope: 'episode', show_id: 's3', episode_id: 'e1', revision: 2 }] } }) : null));
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } });
  await settleAll();
  assert.equal(t.modal.open, true);
  assert.deepEqual(t.perShow(), ['/api/v1/customer/shows/s3'], 'only the stored show, never all 255');
  assert.match(t.parts.episode.innerHTML, /<option value="e1" selected>First/);
});

test('SITE2 legacy campaigns: the list sends no draft request and says it plainly; coded server errors never reach users', async () => {
  const calls = [];
  const fetch = async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/campaigns') return json({ ok: true, workspace_context: 'ctx-a', campaigns: [
      { id: 1, name: 'Old stored', mode: null, state: 'Draft', availability: 'legacy_review_required', placement_count: 2, target_count: 0 },
      { id: 2, name: 'Typed', mode: 'research', state: 'Draft', availability: 'active', placement_count: 0, target_count: 1 },
      { id: 4, name: 'Odd', mode: 'research', state: 'Draft', availability: 'legacy_review_required', placement_count: 0, target_count: 0 }] });
    if (url === '/api/v1/customer/campaigns/1/draft') return json({ ok: false, error: 'legacy_review_required: legacy campaign mode requires owner disposition' }, false, 400);
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const t = await uiWin(fetch);
  t.win.location.hash = '#/campaigns'; t.win.EW.current.route = '#/campaigns';
  await t.win.EWViews.render(); await settleAll();
  assert.deepEqual(calls.filter(u => /\/draft$/.test(u)), [], 'no draft request for any row, legacy or not');
  const plainCopy = /This campaign was created before campaign types existed\. It(?:'|&#39;)s read-only until the workspace owner reviews it\./g;
  assert.equal((t.root.innerHTML.match(plainCopy) || []).length, 2, 'the legacy row and a typed row the server calls legacy both read plainly');
  assert.doesNotMatch(t.root.innerHTML, /legacy_review_required|owner disposition/, 'no raw server code or text');
  assert.match(t.root.innerHTML, /1 research target/);
  // Any coded server error is mapped before it can surface anywhere.
  assert.equal(t.win.EW.plainServerText('legacy_review_required: x'), t.win.EW.LEGACY_CAMPAIGN);
  // Only listed codes are mapped: any other text (a field name) stays as sent.
  assert.equal(t.win.EW.plainServerText('some_code_here: Something went wrong.'), 'some_code_here: Something went wrong.');
  assert.equal(t.win.EW.plainServerText('date_to: must not precede date_from'), 'date_to: must not precede date_from');
  assert.equal(t.win.EW.plainServerText('slow down', 'RATE_LIMITED'), 'slow down');
  assert.equal(t.win.EW.plainServerText('anything', 'legacy_review_required'), t.win.EW.LEGACY_CAMPAIGN);
  assert.equal(t.win.EW.plainServerText('Plain words stay.'), 'Plain words stay.');
  const err = await t.win.EW.api('/customer/campaigns/1/draft').then(() => null, e => e);
  assert.equal(err && err.message, t.win.EW.LEGACY_CAMPAIGN);
});

test('SITE3 no page declares a data: icon; the page policy (default-src \'self\') would block it', () => {
  // /favicon.ico is answered empty (204) by both servers instead.
  const pages = ['index.html', ...fs.readdirSync(path.join(APP, 'public')).filter(f => f.endsWith('.html')).map(f => 'public/' + f), 'operator/index.html'];
  for (const page of pages) {
    const html = fs.readFileSync(path.join(APP, page), 'utf8');
    assert.doesNotMatch(html, /<link[^>]*rel="icon"[^>]*href="data:/, page);
  }
});

test('SITE4 the leave prompt offers Save only when the draft could be saved, and says why not', async () => {
  const t = await modelWin([{ url: 'https://ext', doc: 'ext' }, { url: '#/settings' }], 1);
  t.type('Draft');
  await t.click('#/alerts');
  assert.match(t.modal.innerHTML, /data-action="leave-save"/, 'a submittable draft offers Save');
  await t.act('leave-stay');
  const cases = [
    [() => { t.E.current.writeFence = { to: 'ws-b', op: 1, rev: 5 }; }, /could not be confirmed/],
    [() => { t.E.current.switchWaiting = 1; }, /A workspace switch is in progress\./],
    [() => { t.E.current.principal = null; }, /You are signed out\./],
    [() => { t.E.current.displayedWorkspaceSwitchRevision = undefined; }, /This page is out of date\./],
    [() => { t.root.form.dataset.held = '1'; }, /This page is out of date\./],
  ];
  for (const [set, why] of cases) {
    const saved = { fence: t.E.current.writeFence, wait: t.E.current.switchWaiting, who: t.E.current.principal, rev: t.E.current.displayedWorkspaceSwitchRevision };
    set();
    await t.click('#/alerts');
    assert.equal(t.modal.open, true);
    assert.doesNotMatch(t.modal.innerHTML, /data-action="leave-save"/, String(why));
    assert.match(t.modal.innerHTML, /data-action="leave-discard"/);
    assert.match(t.modal.innerHTML, /data-action="leave-stay"/);
    assert.match(t.modal.innerHTML, new RegExp("Saving isn(?:'|&#39;)t possible right now\\. [^<]*" + why.source));
    await t.act('leave-stay');
    Object.assign(t.E.current, { writeFence: saved.fence, switchWaiting: saved.wait, principal: saved.who, displayedWorkspaceSwitchRevision: saved.rev });
    delete t.root.form.dataset.held;
  }
});

test('SITE5 alert lifecycle and member actions say what happened, never a generic Saved', async () => {
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  };
  const t = await uiWin(fetch);
  t.win.location.hash = '#/alerts'; t.win.EW.current.route = '#/alerts';
  await t.win.EWViews.render(); await settleAll();
  t.win.EW.render = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', id: '7', lifecycle: 'active_local', rev: '3' } });
  await t.win.EWViews.handleAction({ dataset: { action: 'pause-alert', id: '7', lifecycle: 'paused', rev: '4' } });
  // Member removal is confirmed first (SITE18), then says what happened.
  t.win.setTimeout = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'member-remove', member: 'pat' } });
  t.win.FormData = class { *entries() { yield ['confirm_action', 'member-remove']; yield ['confirm_id', 'pat']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('confirm')); await settleAll();
  const form = reviewerForm('confirm');
  t.win.FormData = class { *entries() { yield ['confirm_action', 'stop-alert']; yield ['confirm_id', '7']; yield ['expected_revision', '5']; } };
  await t.win.EWViews.handleSubmit(form); await settleAll();
  assert.deepEqual(t.toasts, ['Alert paused. Its history is kept.', 'Alert resumed.', 'Member removed.', 'Alert stopped.']);
  assert.ok(!t.toasts.includes('Saved.'));
});

/* -- Site pass 2: stored show kept, held pickers, checkout, rows and reports that failed to load -- */
// FormData as a browser builds it from rendered dialog markup: enabled
// controls only; a select submits its selected (else first) option.
const markupEntries = (html) => {
  const out = [];
  const nameOf = a => (/\bname="([^"]*)"/.exec(a) || [])[1];
  for (const [, a] of html.matchAll(/<input\b([^>]*)>/g)) {
    if (nameOf(a) && !/\sdisabled\b/.test(a)) out.push([nameOf(a), (/\bvalue="([^"]*)"/.exec(a) || [, ''])[1]]);
  }
  for (const [, a, body] of html.matchAll(/<select\b([^>]*)>(.*?)<\/select>/g)) {
    if (!nameOf(a) || /\sdisabled\b/.test(a)) continue;
    const opts = [...body.matchAll(/<option value="([^"]*)"([^>]*)>/g)];
    const pick = opts.find(o => /\bselected\b/.test(o[2])) || opts[0];
    if (pick) out.push([nameOf(a), pick[1]]);
  }
  return out;
};
const storedPlacement = (over = {}) => ({ id: 'p1', scope: 'episode', show_id: 's3', episode_id: 'e1', revision: 2, binding: 'eligible', expected_count: 1,
  provenance: { brief_ref: { brief_id: 'b1', version: 1 } }, ...over });
const placementDraft = (placements) => json({ ok: true, workspace_context: 'ctx-a', draft: { campaign: { id: 3, revision: 4 }, pins: {}, placements } });

test('SITE6 editing a placement while the show list fails keeps and submits its stored show; Retry re-selects it', async () => {
  let showsDown = true;
  const writes = [];
  const t = await showsWin('placement', async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/draft') return placementDraft([storedPlacement()]);
    if (url === '/api/v1/customer/shows' && showsDown) return json({ ok: false, error: { code: 'RATE_LIMITED', message: 'slow down' } }, false, 429);
    if (opts.method === 'PUT') { writes.push(JSON.parse(opts.body)); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  t.win.EW.render = () => {};
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } });
  await settleAll();
  assert.match(t.modal.innerHTML, /<select id="placement-show" name="show_id"><option value="">—<\/option><option value="s3" selected>s3<\/option><\/select>/,
    'the stored show stays a selected, enabled choice');
  assert.match(t.modal.innerHTML, /Too many requests right now\./);
  assert.equal(t.parts.show.dataset.stored, 's3');
  t.win.FormData = class { *entries() { yield* markupEntries(t.modal.innerHTML); } };
  await t.win.EWViews.handleSubmit(reviewerForm('placement'));
  await settleAll();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].placement.show_id, 's3', 'the PUT never clears the stored show');
  assert.equal(writes[0].placement.episode_id, 'e1');
  showsDown = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'picker-shows-retry', prefix: 'placement' } });
  await settleAll();
  assert.match(t.parts.show.innerHTML, /<option value="s3" selected>Show 3<\/option>/, 'Retry re-selects the stored show');
  assert.equal((t.parts.show.innerHTML.match(/ selected/g) || []).length, 1);
});

test('SITE7 a target or new placement whose show list never loaded refuses submit with a plain message', async () => {
  const writes = [];
  for (const prefix of ['target', 'placement']) {
    const t = await showsWin(prefix, async (url, opts) => {
      if (url === '/api/v1/customer/shows') return json({ ok: false, error: 'boom' }, false, 500);
      if (opts.method === 'POST') { writes.push(url); return json({ ok: true, workspace_context: 'ctx-a' }); }
      return null;
    });
    t.win.EW.render = () => {};
    await t.win.EWViews.handleAction({ dataset: { action: prefix === 'target' ? 'add-target' : 'add-placement', id: '3' } });
    await settleAll();
    assert.match(t.modal.innerHTML, new RegExp(`<select id="${prefix}-show" name="show_id" disabled>`));
    // The live control mirrors the markup: disabled, nothing stored.
    Object.assign(t.parts.show, { disabled: true, setAttribute() {}, focus() {} });
    const form = reviewerForm(prefix);
    const base = form.querySelector;
    form.querySelector = s => (s === `#${prefix}-show` ? t.parts.show : base(s));
    const typed = prefix === 'target' ? [['scope', 'show_window']] : [['brief_ref', 'b1:1'], ['binding', 'eligible'], ['expected_count', '1']];
    t.win.FormData = class { *entries() { yield* markupEntries(t.modal.innerHTML).filter(([k]) => !typed.some(([n]) => n === k)); yield* typed; } };
    await t.win.EWViews.handleSubmit(form);
    await settleAll();
    assert.match(form.box.innerHTML, /The shows could not be loaded\. Retry, then choose a show\./, prefix);
  }
  assert.deepEqual(writes, [], 'nothing is sent without a show');
});

test('B-3 a new show-window placement with no show chosen refuses submit and sends nothing', async () => {
  const writes = [];
  const t = await showsWin('placement', async (url, opts) => {
    if (opts.method === 'POST') { writes.push(url); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  t.win.EW.render = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'add-placement', id: '3' } });
  await settleAll();
  const form = reviewerForm('placement');
  const typed = [['scope', 'show_window'], ['show_id', ''], ['brief_ref', 'b1:1'], ['binding', 'eligible'], ['expected_count', '1'], ['expected_campaign_revision', '4']];
  t.win.FormData = class { *entries() { yield* markupEntries(t.modal.innerHTML).filter(([k]) => !typed.some(([n]) => n === k)); yield* typed; } };
  await t.win.EWViews.handleSubmit(form);
  await settleAll();
  assert.deepEqual(writes, [], 'nothing is sent without a show');
  assert.match(form.box.innerHTML, /Choose the show for this window\./);
});

test('SITE8 editing a stored show-window placement lists that show\'s episodes', async () => {
  const t = await showsWin('placement', async (url) => (url === '/api/v1/customer/campaigns/3/draft'
    ? placementDraft([storedPlacement({ scope: 'show_window', episode_id: null })]) : null));
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } });
  await settleAll();
  assert.deepEqual(t.perShow(), ['/api/v1/customer/shows/s3'], 'one read, for the stored show');
  assert.equal(t.parts.episode.disabled, false, 'never left on "Choose a show first"');
  assert.match(t.parts.episode.innerHTML, /<option value="">—<\/option><option value="e1">First \(2026-01-02\)<\/option><option value="e2">Second<\/option>/);
  assert.doesNotMatch(t.parts.episode.innerHTML, / selected/);
});

test('SITE9 a dialog held over a workspace change starts no picker read and is never filled', async () => {
  const gates = {};
  const t = await showsWin('target', async (url) => {
    if (url === '/api/v1/customer/shows/s1') { await new Promise(r => { gates.s1 = r; }); return json({ ok: true, workspace_context: 'ctx-a', episodes: [{ id: 'X1', ep_title: 'From s1' }] }); }
    return null;
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } });
  await settleAll();
  const opened = t.win.EW.current.displayedWorkspaceSwitchRevision;
  // Typed into, then a read is in flight when the workspace changes.
  const form = { dataset: { dirty: '1' } };
  t.parts.show.form = t.parts.episode.form = form;
  t.parts.show.value = 's1'; t.win.EWViews.onChange({ target: t.parts.show });
  await settleAll();
  form.dataset.held = '1';
  gates.s1(); await settleAll();
  assert.doesNotMatch(t.parts.episode.innerHTML, /X1/, 'a read started before the hold is dropped');
  const reads = () => t.calls.filter(u => /\/customer\/shows/.test(u)).length;
  for (const [name, hold] of [
    ['held form', () => { form.dataset.held = '1'; }],
    ['moved revision', () => { delete form.dataset.held; t.win.EW.current.displayedWorkspaceSwitchRevision = (opened ?? 0) + 1; }],
  ]) {
    hold();
    const before = reads(), episodes = t.parts.episode.innerHTML, shows = t.parts.show.innerHTML;
    t.parts.show.value = 's2'; t.win.EWViews.onChange({ target: t.parts.show });
    await t.win.EWViews.handleAction({ dataset: { action: 'picker-shows-retry', prefix: 'target' } });
    await settleAll();
    assert.equal(reads(), before, `${name}: no shows or episodes request`);
    assert.equal(t.parts.episode.innerHTML, episodes, `${name}: no options filled`);
    assert.equal(t.parts.show.innerHTML, shows, `${name}: no show options filled`);
    assert.match(t.parts.episodesStatus.innerHTML, /This page is out of date\. Reload the workspace to make changes\./, name);
    assert.match(t.parts.showsStatus.innerHTML, /This page is out of date\./, name);
  }
});

test('SITE10 checkout says "Opening checkout…" only when the reply carries its address', async () => {
  let reply = { ok: true, workspace_context: 'ctx-a' };
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/billing/checkout') return json(reply);
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  t.win.EW.render = () => {};
  t.win.FormData = class { *entries() { yield ['price_id', 'price_1']; } };
  const form = reviewerForm('billing-checkout');
  await t.win.EWViews.handleSubmit(form); await settleAll();
  assert.deepEqual(t.toasts, [], 'no checkout claim without an address');
  assert.match(form.box.innerHTML, /Checkout isn(?:'|&#39;)t available right now\./);
  assert.equal(t.win.location.href, undefined);
  reply = { ok: true, workspace_context: 'ctx-a', checkout_url: 'https://pay.example/c1' };
  await t.win.EWViews.handleSubmit(reviewerForm('billing-checkout')); await settleAll();
  assert.deepEqual(t.toasts, ['Opening checkout…']);
  assert.equal(t.win.location.href, 'https://pay.example/c1');
});

test('SITE11 the campaigns list renders every row from the one list reply: counts and state, no per-row draft reads', async () => {
  const calls = [];
  const t = await uiWin(async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/campaigns') return json({ ok: true, workspace_context: 'ctx-a', campaigns: [
      { id: 2, name: 'Typed', mode: 'research', state: 'Draft', availability: 'active', placement_count: 0, target_count: 2 },
      { id: 3, name: 'Supplied work', mode: 'supplied_requirements', state: 'Tracking', availability: 'active', placement_count: 3, target_count: 0 },
      { id: 5, name: 'Empty', mode: 'research', state: 'Draft', availability: 'active', placement_count: 0, target_count: 0 },
      { id: 6, name: 'Older server', mode: 'supplied_requirements', state: 'Draft' }] });
    throw new Error('unexpected request ' + url);
  });
  t.win.location.hash = '#/campaigns'; t.win.EW.current.route = '#/campaigns';
  await t.win.EWViews.render(); await settleAll();
  assert.equal(calls.filter(u => u === '/api/v1/customer/campaigns').length, 1, 'one list read');
  assert.deepEqual(calls.filter(u => /\/draft$/.test(u)), [], 'no per-row draft reads');
  const row = name => new RegExp(`<li[^>]*>(?:(?!</li>).)*${name}(?:(?!</li>).)*</li>`).exec(t.root.innerHTML)[0];
  assert.match(row('Typed'), /2 research targets/);
  assert.match(row('Supplied work'), /3 placements/);
  assert.match(row('Supplied work'), />Tracking</);
  assert.match(row('Empty'), /Add a research target \(episode or show window\)/);
  // A reply without counts never reads as an empty scope.
  assert.doesNotMatch(row('Older server'), /No scope yet|Add a show or episode placement/);
  assert.ok(t.root.innerHTML.indexOf('Supplied work') < t.root.innerHTML.indexOf('Typed'), 'tracking rows first');
  assert.doesNotMatch(t.root.innerHTML, /Couldn(?:'|&#39;)t load this campaign/);
});

const reportsFail = async (campaign, draft) => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/campaigns/1') return { campaign };
    if (p === '/customer/campaigns/1/draft' && draft) return { draft };
    // A stored report whose digest cannot be verified.
    if (p === '/customer/campaigns/1/reports') throw Object.assign(new Error('view changed'), { status: 409, code: 'VIEW_CHANGED' });
    throw new Error('unexpected request ' + p);
  };
  win.location.hash = '#/campaign/1';
  await win.EWViews.render();
  return main.innerHTML;
};
test('SITE12 a report history that failed to load is told, never "no reports" (legacy and active pages)', async () => {
  const unreadable = /<p class="field-error">The stored reports couldn(?:'|&#39;)t be loaded\.<\/p>/;
  const legacy = await reportsFail({ id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: [] });
  assert.match(legacy, unreadable);
  assert.doesNotMatch(legacy, /No stored reports\./);
  assert.doesNotMatch(legacy, /stay readable/, 'no readable claim when the list failed');
  assert.match(legacy, /data-action="create-campaign"[^>]*>Start a new campaign</, 'a real control to the new-campaign flow (SITE20)');
  const active = await reportsFail({ id: 1, mode: 'research', revision: 3, name: 'Field study' },
    { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], targets: [{ id: 't1', scope: 'episode', episode_id: 'e1' }], allocation: {}, coverage_by_placement: {} });
  assert.match(active, unreadable);
  assert.doesNotMatch(active, /No frozen reports yet/);
});

test('K-notes a read-only legacy campaign shows its stored notes, escaped; empty notes show no Notes section', async () => {
  const base = { id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: [] };
  const withNotes = await reportsFail({ ...base, notes: 'Q3 plan: <b>call</b> the buyer & confirm' });
  assert.match(withNotes, /<h2>Notes<\/h2>/);
  assert.match(withNotes, /Q3 plan: &lt;b&gt;call&lt;\/b&gt; the buyer &amp; confirm/);
  assert.doesNotMatch(withNotes, /<b>call<\/b>/, 'notes are escaped');
  assert.doesNotMatch(withNotes, /<textarea|name="notes"/, 'read-only: no edit field');
  for (const notes of ['', undefined, null]) {
    const without = await reportsFail({ ...base, notes });
    assert.doesNotMatch(without, /<h2>Notes<\/h2>/);
  }
});

const reportsPartial = async (campaign, draft, reports = [
  { report_no: 1, captured_at: '2026-01-01T00:00:00Z', stale: false, stale_reasons: [], available: false, unavailable_reason: 'unverifiable' },
  { report_no: 2, captured_at: '2026-01-02T00:00:00Z', stale: false, stale_reasons: [], available: true }]) => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => {
    if (p === '/customer/campaigns/1') return { campaign };
    if (p === '/customer/campaigns/1/draft' && draft) return { draft };
    // By default report 1's stored copy failed verification; report 2 is fine.
    if (p === '/customer/campaigns/1/reports') return { reports };
    throw new Error('unexpected request ' + p);
  };
  win.location.hash = '#/campaign/1';
  await win.EWViews.render();
  return main.innerHTML;
};
test('SITE12b an unverifiable stored report is listed plainly with no link; the others still open (legacy and active pages)', async () => {
  const legacy = await reportsPartial({ id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: [] });
  const active = await reportsPartial({ id: 1, mode: 'research', revision: 3, name: 'Field study' },
    { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], targets: [{ id: 't1', scope: 'episode', episode_id: 'e1' }], allocation: {}, coverage_by_placement: {} });
  for (const html of [legacy, active]) {
    assert.match(html, /Report 1 — can(?:'|&#39;)t be opened: its stored copy couldn(?:'|&#39;)t be verified\./);
    assert.doesNotMatch(html, /#\/campaign\/1\/report\/1["']/, 'no link to the unverifiable report');
    assert.match(html, /#\/campaign\/1\/report\/2["']/, 'the verified report still opens');
    assert.doesNotMatch(html, /couldn(?:'|&#39;)t be loaded|unverifiable/, 'the list is not a failure; no internal code');
  }
});

test('F17a a legacy campaign claims its reports stay readable only when one actually opens', async () => {
  const legacyCamp = { id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: [] };
  const allUnverifiable = await reportsPartial(legacyCamp, null, [
    { report_no: 1, captured_at: '2026-01-01T00:00:00Z', stale: false, stale_reasons: [], available: false, unavailable_reason: 'unverifiable' }]);
  assert.match(allUnverifiable, /Report 1 — can(?:'|&#39;)t be opened/);
  assert.doesNotMatch(allUnverifiable, /stay readable/, 'no readable claim when no listed report opens');
  assert.match(allUnverifiable, /For current work, start a new campaign\./);
  assert.doesNotMatch(await reportsPartial(legacyCamp, null, []), /stay readable/, 'no claim over an empty list');
  assert.match(await reportsPartial(legacyCamp), /Its stored reports below stay readable\./, 'kept when one report opens');
});

test('SITE13 rapid show changes: a slow reply for an earlier pick is dropped (reviewer RV1)', async () => {
  const gates = {};
  const t = await showsWin('target', async (url) => {
    if (url === '/api/v1/customer/shows/s1') { await new Promise(r => { gates.s1 = r; }); return json({ ok: true, workspace_context: 'ctx-a', episodes: [{ id: 'X1', ep_title: 'From s1' }] }); }
    if (url === '/api/v1/customer/shows/s2') return json({ ok: true, workspace_context: 'ctx-a', episodes: [{ id: 'X2', ep_title: 'From s2' }] });
    return null;
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } }); await settleAll();
  t.parts.show.value = 's1'; t.win.EWViews.onChange({ target: t.parts.show }); await settleAll();
  t.parts.show.value = 's2'; t.win.EWViews.onChange({ target: t.parts.show }); await settleAll();
  gates.s1(); await settleAll();
  assert.match(t.parts.episode.innerHTML, /X2/);
  assert.doesNotMatch(t.parts.episode.innerHTML, /X1/);
});

test('SITE14 an episode reply that lands after the dialog was closed and reopened is dropped (reviewer RV2)', async () => {
  const gates = {};
  const t = await showsWin('target', async (url) => {
    if (url === '/api/v1/customer/shows/s1') { await new Promise(r => { gates.s1 = r; }); return json({ ok: true, workspace_context: 'ctx-a', episodes: [{ id: 'X1', ep_title: 'From s1' }] }); }
    return null;
  });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } }); await settleAll();
  t.parts.show.value = 's1'; t.win.EWViews.onChange({ target: t.parts.show }); await settleAll();
  await t.win.EWViews.handleAction({ dataset: { action: 'close-dialog' } });
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } }); await settleAll();
  assert.equal(t.modal.open, true, 'a new dialog is open');
  const before = t.parts.episode.innerHTML;
  gates.s1(); await settleAll();
  assert.equal(t.parts.episode.innerHTML, before);
  assert.doesNotMatch(t.parts.episode.innerHTML, /X1/);
});

test('SITE15 a stored show or episode missing from the loaded lists stays selected and is submitted', async () => {
  let showsDown = true;
  const writes = [];
  const t = await showsWin('placement', async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/draft') return placementDraft([storedPlacement({ show_id: 'gone9', episode_id: 'gone-ep' })]);
    if (url === '/api/v1/customer/shows' && showsDown) return json({ ok: false, error: 'boom' }, false, 500);
    if (opts.method === 'PUT') { writes.push(JSON.parse(opts.body)); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  t.win.EW.render = () => {};
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } });
  await settleAll();
  assert.match(t.parts.episode.innerHTML, /<option value="gone-ep" selected>gone-ep<\/option>/, 'the stored episode survives a list without it');
  showsDown = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'picker-shows-retry', prefix: 'placement' } });
  await settleAll();
  assert.match(t.parts.show.innerHTML, /<option value="gone9" selected>/, 'the stored show survives a list without it');
  const html = t.modal.innerHTML
    .replace(/<select id="placement-show"[^>]*>.*?<\/select>/, `<select id="placement-show" name="show_id">${t.parts.show.innerHTML}</select>`)
    .replace(/<select id="placement-episode"[^>]*>.*?<\/select>/, `<select id="placement-episode" name="episode_id">${t.parts.episode.innerHTML}</select>`);
  t.win.FormData = class { *entries() { yield* markupEntries(html); } };
  await t.win.EWViews.handleSubmit(reviewerForm('placement'));
  await settleAll();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].placement.show_id, 'gone9');
  assert.equal(writes[0].placement.episode_id, 'gone-ep');
});

test('SITE16 checkout without an address gives its button back', async () => {
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  t.win.EW.render = () => {};
  t.win.FormData = class { *entries() { yield ['price_id', 'price_1']; } };
  const form = reviewerForm('billing-checkout');
  form.submit.textContent = 'Upgrade';
  await t.win.EWViews.handleSubmit(form); await settleAll();
  assert.equal(form.submit.disabled, false);
  assert.equal(form.submit.textContent, 'Upgrade');
  assert.match(form.box.innerHTML, /Checkout isn(?:'|&#39;)t available right now\./);
});

test('SITE17 the picker loads under a published numeric revision after an ordinary re-render', async () => {
  const t = await showsWin('target', async () => null);
  t.win.EW.current.displayedWorkspaceSwitchRevision = 7;
  await t.win.EWViews.handleAction({ dataset: { action: 'add-target' } }); await settleAll();
  t.win.EW.current.displayedWorkspaceSwitchRevision = 7;
  t.parts.show.value = 's1'; t.win.EWViews.onChange({ target: t.parts.show }); await settleAll();
  assert.deepEqual(t.perShow(), ['/api/v1/customer/shows/s1']);
  assert.match(t.parts.episode.innerHTML, /value="e1"/);
  assert.equal(t.parts.episode.disabled, false);
});

/* -- UX pass 7: failed reads never become editable blanks or empty claims; sibling drafts survive a save; plain copy -- */
// A hosted member window with a dialog and page the tests can read.
const uxWin = async (respond, route, session = {}) => {
  const calls = [];
  const fetch = async (url, opts = {}) => {
    calls.push((opts.method || 'GET') + ' ' + url + (opts.body ? ' ' + opts.body : ''));
    if (url === '/api/v1/session') return json(hostedSession(session));
    const own = respond && await respond(url, opts);
    if (own) return own;
    return json({ ok: true, workspace_context: 'ctx-a', items: [] });
  };
  const win = loadViews(fetch); await settleAll();
  const modal = { open: false, innerHTML: '', close() { this.open = false; }, showModal() { this.open = true; }, setAttribute() {},
    querySelector: () => null, querySelectorAll: () => [] };
  const main = { innerHTML: '', querySelectorAll: () => [], querySelector: () => null };
  win.document.querySelector = sel => (sel === '#modal' ? modal : sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.setTimeout = () => {}; win.EW.header = () => {}; win.scrollTo = () => {};
  const toasts = []; win.EW.toast = m => toasts.push(m); win.EW.saveNotice = () => {};
  win.EW.current.route = route; win.location.hash = route;
  return { win, modal, main, calls, toasts, writes: () => calls.filter(c => !c.startsWith('GET ')) };
};
const down = (status = 500) => json({ ok: false, error: status === 429 ? { code: 'RATE_LIMITED', message: 'slow down' } : 'boom' }, false, status);

test('UX1 a target note whose read failed, or whose target is gone, opens nothing editable; Retry loads the stored note', async () => {
  let mode = 'limited';
  const t = await uxWin(async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/targets' && opts.method === 'GET') {
      if (mode === 'limited') return down(429);
      if (mode === 'gone') return json({ ok: true, workspace_context: 'ctx-a', targets: [] });
      return json({ ok: true, workspace_context: 'ctx-a', targets: [{ id: 't1', note: 'Stored note', revision: 2 }] });
    }
    if (opts.method === 'PUT') return json({ ok: true, workspace_context: 'ctx-a' });
    return null;
  }, '#/campaign/3/target/t1');
  t.win.EW.render = () => {};
  for (const [m, copy] of [['limited', /Too many requests right now\./], ['gone', /This target could not be found\./]]) {
    mode = m;
    await t.win.EWViews.handleAction({ dataset: { action: 'edit-target-note', rev: '2' } }); await settleAll();
    assert.equal(t.modal.open, true, m);
    assert.doesNotMatch(t.modal.innerHTML, /<textarea|<form/, m + ': no field to save over the stored note');
    assert.match(t.modal.innerHTML, copy, m);
    assert.match(t.modal.innerHTML, /data-action="edit-target-note"[^>]*data-rev="2"[^>]*>Retry</, m);
  }
  mode = 'ok';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-target-note', rev: '2' } }); await settleAll();
  assert.match(t.modal.innerHTML, /<textarea id="target-note-text" name="note"[^>]*>Stored note<\/textarea>/);
  assert.deepEqual(t.writes(), [], 'nothing was written');
});

test('UX2 saving one Settings form keeps what was typed in the others, on its original revision', async () => {
  let rev = 1;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/preferences') {
      if (opts.method === 'PUT') rev++;
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: rev, role_matrix: { viewer: 'read' } } });
    }
    return json({ ok: true, workspace_context: 'ctx-a', members: [], shares: [] });
  });
  // Rendered Settings forms, rebuilt from markup on every render.
  let forms = {}, html = '';
  Object.defineProperty(t.root, 'innerHTML', { get: () => html, set: (v) => {
    html = v; forms = {};
    const ws = v.match(/<form id="settings-workspace"><input type="hidden" name="expected_revision" value="([^"]*)">.*?name="workspace_name" value="([^"]*)"/);
    if (ws) forms['settings-workspace'] = pageForm(ws[2], ws[1]);
    const display = v.match(/<form id="settings-display"[^>]*><input type="hidden" name="expected_revision" value="([^"]*)">/);
    if (display) forms['settings-display'] = pageForm('Reno', display[1], 'settings-display', 'display_name');
  } });
  const baseQ = t.root.querySelector;
  t.root.querySelector = s => forms[s.replace(/^#/, '')] || baseQ(s);
  t.win.document.querySelectorAll = s => (/data-dirty/.test(s) ? Object.values(forms).filter(f => f.dataset.dirty === '1') : []);
  t.win.FormData = class { constructor(f) { this.f = f; } *entries() { for (const e of this.f.elements) if (e.name) yield [e.name, e.value]; } };
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  const name = forms['settings-workspace'], display = forms['settings-display'];
  assert.ok(name && display, 'both forms rendered');
  name.field.value = 'Typed name'; name.dataset.dirty = '1';
  display.field.value = 'comment'; display.dataset.dirty = '1';
  await t.win.EWViews.handleSubmit(display); await settleAll();
  assert.equal(rev, 2, 'the display form saved');
  const after = forms['settings-workspace'];
  assert.notEqual(after, name, 'the page re-rendered');
  assert.equal(after.field.value, 'Typed name', 'the other form keeps its typed entry');
  assert.equal(after.rev.value, '1', 'on the revision it was typed against: saving it conflicts, never overwrites');
  assert.equal(after.dataset.dirty, '1');
  assert.match(after.box.innerHTML, /Your unsaved entry was restored\./);
  assert.equal(forms['settings-display'].dataset.dirty, undefined, 'the saved form is fresh');
});

test('UX3 a failed brief list keeps the stored brief selectable and submitted, says so with Retry, never "No registered briefs"', async () => {
  let briefsDown = true;
  const writes = [];
  const t = await showsWin('placement', async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/draft') return placementDraft([storedPlacement()]);
    if (url.startsWith('/api/v1/customer/briefs?')) return briefsDown ? down(429)
      : json({ ok: true, workspace_context: 'ctx-a', items: [{ brief_id: 'b1', title: 'Brief One', current_version: 1 }, { brief_id: 'b2', title: 'Two', current_version: 3 }] });
    if (opts.method === 'PUT') { writes.push(JSON.parse(opts.body)); return json({ ok: true, workspace_context: 'ctx-a' }); }
    return null;
  });
  t.win.EW.render = () => {};
  t.win.EW.current.route = '#/campaign/3/placement/p1'; t.win.location.hash = '#/campaign/3/placement/p1';
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-placement' } }); await settleAll();
  assert.match(t.modal.innerHTML, /<select id="placement-brief" name="brief_ref" required data-unloaded="1"><option value="b1:1" selected>b1 v1<\/option><\/select>/);
  assert.match(t.modal.innerHTML, /Too many requests right now\./);
  assert.match(t.modal.innerHTML, /data-action="picker-briefs-retry"[^>]*data-prefix="placement"[^>]*>Retry</);
  assert.doesNotMatch(t.modal.innerHTML, /No registered briefs/);
  t.win.FormData = class { *entries() { yield* markupEntries(t.modal.innerHTML); } };
  await t.win.EWViews.handleSubmit(reviewerForm('placement')); await settleAll();
  assert.equal(writes.length, 1, 'the edit saves');
  assert.deepEqual(plain(writes[0].placement.provenance.brief_ref), { kind: 'supplied_brief', brief_id: 'b1', version: 1 });
  // Retry fills the list and keeps the choice.
  const select = { innerHTML: '', value: 'b1:1', dataset: { unloaded: '1' }, selectedOptions: [{ textContent: 'b1 v1' }] };
  const status = { innerHTML: 'x' };
  const base = t.modal.querySelector;
  t.modal.querySelector = s => (s === '#placement-brief' ? select : s === '#placement-briefs-status' ? status : base(s));
  briefsDown = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'picker-briefs-retry', prefix: 'placement' } }); await settleAll();
  assert.equal(select.innerHTML, '<option value="b1:1" selected>Brief One v1</option><option value="b2:3">Two v3</option>');
  assert.equal(select.dataset.unloaded, undefined);
  assert.equal(status.innerHTML, '');
  // A new placement with no stored brief: plain words, nothing sent.
  briefsDown = true;
  await t.win.EWViews.handleAction({ dataset: { action: 'close-dialog' } });
  t.win.EW.current.route = '#/campaign/3'; t.win.location.hash = '#/campaign/3';
  await t.win.EWViews.handleAction({ dataset: { action: 'add-placement' } }); await settleAll();
  assert.match(t.modal.innerHTML, /<option value="">Briefs unavailable<\/option>/);
  const form = reviewerForm('placement');
  const fq = form.querySelector;
  form.querySelector = s => (s === '#placement-brief' ? { dataset: { unloaded: '1' }, setAttribute() {}, focus() {} } : fq(s));
  t.win.FormData = class { *entries() { yield ['campaign_id', '3']; yield ['show_id', 's1']; yield ['expected_count', '1']; yield ['binding', 'eligible']; yield ['expected_campaign_revision', '4']; yield ['brief_ref', '']; } };
  await t.win.EWViews.handleSubmit(form); await settleAll();
  assert.match(form.box.innerHTML, /The briefs could not be loaded\. Retry, then choose a brief\./);
  assert.equal(writes.length, 1);
});

test('UX3b the campaign editor keeps a stored brief when the list fails; creation says the list failed', async () => {
  const t = await uxWin(async (url) => {
    if (url === '/api/v1/customer/campaigns/3') return json({ ok: true, workspace_context: 'ctx-a', campaign: { id: 3, revision: 2, mode: 'supplied_requirements', name: 'C',
      requirement_provenance: { brief_id: 'b1', version: 1, source_label: 'Spring read' } } });
    if (url === '/api/v1/customer/briefs/b1/versions/1') return json({ ok: true, workspace_context: 'ctx-a', brief_version: { title: 'Spring read' } });
    if (url.startsWith('/api/v1/customer/briefs?')) return down();
    return null;
  }, '#/campaign/3');
  await t.win.EWViews.handleAction({ dataset: { action: 'edit-campaign', id: '3' } }); await settleAll();
  assert.match(t.modal.innerHTML, /<label for="campaign-brief">Brief version<\/label><select id="campaign-brief" name="brief_ref" data-unloaded="1"><option value="b1:1" selected>Spring read v1<\/option><\/select>/);
  assert.match(t.modal.innerHTML, /The briefs could not be loaded\. Try again\./);
  assert.doesNotMatch(t.modal.innerHTML, /No registered briefs|SuppliedBriefRef/);
  // Creation: the supplied picker says the list failed.
  const host = { innerHTML: '' };
  const form = { addEventListener() {}, querySelector: s => (s === 'input[name=mode]:checked' ? { value: 'supplied_requirements' } : null) };
  t.modal.querySelector = s => (s === '#campaign' ? form : s === '#brief-picker' ? host : null);
  await t.win.EWViews.handleAction({ dataset: { action: 'close-dialog' } });
  await t.win.EWViews.handleAction({ dataset: { action: 'create-campaign' } }); await settleAll();
  assert.match(host.innerHTML, /<option value="">Briefs unavailable<\/option>/);
  assert.match(host.innerHTML, /data-action="picker-briefs-retry"[^>]*data-prefix="campaign"/);
  assert.doesNotMatch(host.innerHTML, /No registered briefs yet/);
});

test('UX4 Settings tells failed member, share and billing reads with Retry, never as empty lists or unconnected billing', async () => {
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/preferences') return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1, workspace_name: 'Alpha' } });
    if (/\/members$|\/shares$|\/billing\/status$/.test(url)) return down();
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  const html = t.root.innerHTML;
  for (const lie of ['No members listed.', 'No shares.', 'Billing is unconnected']) assert.ok(!html.includes(lie), lie);
  for (const told of ['The member list could not be loaded. Try again.', 'The share list could not be loaded. Try again.', 'The billing status could not be loaded. Try again.']) assert.ok(html.includes(told), told);
  assert.equal((html.match(/data-action="retry"[^>]*>Retry</g) || []).length, 3);
  assert.match(html, /data-action="billing-portal"[^>]*>Manage subscription</, 'still reachable');
});

test('UX5 a report list that failed offers Retry; the legacy page links to a new campaign', async () => {
  const legacy = await reportsFail({ id: 1, mode: null, revision: 1, name: 'Old stored', availability: 'legacy_review_required', reasons: [] });
  assert.match(legacy, /The stored reports couldn(?:'|&#39;)t be loaded\.<\/p><div class="actions"><button type="button" class="button " data-action="retry" >Retry<\/button>/);
  assert.match(legacy, /data-action="create-campaign"[^>]*>Start a new campaign</);
  const active = await reportsFail({ id: 1, mode: 'research', revision: 3, name: 'Field study' },
    { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [], targets: [{ id: 't1', scope: 'episode', episode_id: 'e1' }], allocation: {}, coverage_by_placement: {} });
  assert.match(active, /couldn(?:'|&#39;)t be loaded\.<\/p><div class="actions">[^]*?data-action="retry"/);
});

test('UX6 a failed notes, brands or filter read is told with Retry; never a new note, an empty brand list or "no values"', async () => {
  let notesDown = true;
  const t = await uxWin(async (url, opts) => {
    if (url.endsWith('/mistake') && opts.method === 'GET') return notesDown ? down() : json({ ok: true, workspace_context: 'ctx-a', mistakes: [{ id: 5, revision: 3, status: 'open', reason: 'Kept' }] });
    if (url === '/api/v1/customer/brands' || url.startsWith('/api/v1/customer/filter-values')) return down();
    return null;
  }, '#/evidence/sr1%3Ax');
  await t.win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: 'sr1:x' } }); await settleAll();
  assert.doesNotMatch(t.modal.innerHTML, /<form|<textarea/, 'not offered as a new note (a POST would duplicate it)');
  assert.match(t.modal.innerHTML, /Your existing note could not be loaded\. Try again\./);
  assert.match(t.modal.innerHTML, /data-action="report-mistake"[^>]*data-id="sr1:x"[^>]*>Retry</);
  notesDown = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: 'sr1:x' } }); await settleAll();
  assert.match(t.modal.innerHTML, /name="mistake_id" value="5"/, 'Retry edits the existing note');
  await t.win.EWViews.handleAction({ dataset: { action: 'compare-select' } }); await settleAll();
  assert.doesNotMatch(t.modal.innerHTML, /No brands in stored sources/);
  assert.match(t.modal.innerHTML, /The brands could not be loaded\. Try again\./);
  assert.match(t.modal.innerHTML, /data-action="compare-select"[^>]*>Retry</);
  t.win.EW.current.route = '#/search'; t.win.location.hash = '#/search';
  await t.win.EWViews.handleAction({ dataset: { action: 'filters' } }); await settleAll();
  assert.equal((t.modal.innerHTML.match(/These values couldn(?:'|&#39;)t be loaded\./g) || []).length, 3);
  assert.doesNotMatch(t.modal.innerHTML, /No values in the authorized collection/);
  assert.deepEqual(t.writes(), []);
  // Search scope chips whose names failed to load say IDs are shown.
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {}; win.EW.header = () => {};
  const bundle = { query: { version: 2, mode: 'ads', q: 'kept', brands: ['acme'], sort: 'newest', page: 1 }, revision: 'r1',
    result: { items: [], total: 0, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  win.EW.api = async (p) => { if (p === '/customer/brands' || p === '/customer/shows') throw Object.assign(new Error('boom'), { status: 500 }); return bundle; };
  win.location.hash = '#/search?mode=ads&q=kept&brands=acme';
  await win.EWViews.render();
  assert.match(main.innerHTML, /Couldn(?:'|&#39;)t load the name of brand “acme”, so its ID is shown\./);
});

test('UX7 the campaigns list paints nothing and reads nothing more after navigation', async () => {
  const calls = [];
  let release;
  const t = await uiWin(async (url) => {
    calls.push(url);
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/campaigns') {
      await new Promise(r => { release = r; });
      return json({ ok: true, workspace_context: 'ctx-a', campaigns: [{ id: 1, name: 'One', mode: 'research', placement_count: 0, target_count: 1 }] });
    }
    throw new Error('unexpected request ' + url);
  });
  t.win.location.hash = '#/campaigns'; t.win.EW.current.route = '#/campaigns';
  const rendering = t.win.EWViews.render(); await settleAll();
  t.win.EW.current.fetchToken++; // the user navigated away
  release(); await rendering; await settleAll();
  assert.ok(!calls.some(u => /\/draft$/.test(u)), 'no row reads');
  assert.doesNotMatch(t.root.innerHTML, /1 research target/);
});

test('UX8 a created invite stays named and is sent from its listed row; shares choose a kind; Remove asks first', async () => {
  let invited = false, delivery = 'not_queued';
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && url.endsWith('/invites')) { invited = true; return json({ ok: true, workspace_context: 'ctx-a', invite_id: 'inv-42', member: 'pat@example.com', role: 'viewer' }); }
    if ((opts.method || 'GET') === 'GET' && url === '/api/v1/workspaces/ws-a/invites') return json({ ok: true, invites: invited ? [{ id: 'inv-42', member: 'pat@example.com', role: 'viewer', created_at: '2026-10-08T10:00:00+00:00', delivery }] : [] });
    if (opts.method === 'POST' && url.endsWith('/send')) { delivery = 'sent'; return json({ ok: true, workspace_context: 'ctx-a', invite_id: 'inv-42', message_id: 17, status: 'sent', unresolved: false, reason: '' }); }
    if (opts.method === 'POST' && url === '/api/v1/shares') return json({ ok: true, workspace_context: 'ctx-a', share_id: 'sh-1' });
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'pat', role: 'viewer' }], shares: [] });
  });
  const sent = [];
  const fetch = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => { if (opts.method && opts.method !== 'GET') sent.push(opts.method + ' ' + url + ' ' + (opts.body || '')); return fetch(url, opts); };
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('invite-create')); await settleAll();
  // L25: creating says no email went; the invite ID stays in its row's Technical details.
  assert.match(t.root.innerHTML, /Invitation created for pat@example\.com\. No email has been sent yet\.<\/p>/);
  assert.match(t.root.innerHTML, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Invite <span class="mono">inv-42<\/span>/, 'the invite ID stays visible');
  assert.doesNotMatch(t.root.innerHTML, /id="invite-deliver"|id="invite-id"/, 'no manual invite-ID field');
  // Sending is one click on the listed row: no ID to copy or type.
  assert.match(t.root.innerHTML, /<h3>Pending invitations<\/h3>[\s\S]*data-action="invite-send" data-invite="inv-42">Send invite<\/button>/);
  await t.win.EWViews.handleAction({ dataset: { action: 'invite-send', invite: 'inv-42' }, closest: () => null, setAttribute() {} }); await settleAll();
  assert.ok(sent.some(x => x.startsWith('POST /api/v1/workspaces/ws-a/invites/inv-42/send')), sent.join(' | '));
  assert.ok(t.toasts.includes('Email send recorded. Inbox delivery is not guaranteed.'), t.toasts.join(' | '));
  assert.ok(!t.toasts.some(m => /outbox|\(invite /.test(m)));
  assert.match(t.root.innerHTML, /data-tag="confirmed">[^]*?Sent<\/span>/);
  // Shares: a fixed list of kinds plus the item's ID, never "type:id".
  assert.match(t.root.innerHTML, /<select id="share-type" name="object_type"><option value="evidence">Evidence page<\/option><option value="episode">Episode<\/option>/);
  assert.doesNotMatch(t.root.innerHTML, /type:id/);
  t.win.FormData = class { *entries() { yield ['object_type', 'episode']; yield ['object_id', 'ep-7']; yield ['audience', 'bob']; yield ['fields', '']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('share-create')); await settleAll();
  const share = sent.find(s => s.startsWith('POST /api/v1/shares '));
  assert.ok(share, sent.join(' | '));
  assert.equal(JSON.parse(share.slice('POST /api/v1/shares '.length)).object_type, 'episode');
  assert.equal(JSON.parse(share.slice('POST /api/v1/shares '.length)).object_id, 'ep-7');
  // Remove asks first; only Confirm deletes.
  t.win.setTimeout = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'member-remove', member: 'pat' } }); await settleAll();
  assert.equal(t.modal.open, true);
  assert.match(t.modal.innerHTML, /<form id="confirm"[^]*Remove pat from this workspace\?[^]*name="confirm_action" value="member-remove"/);
  assert.ok(!sent.some(s => s.startsWith('DELETE')), 'nothing removed before Confirm');
  t.win.FormData = class { *entries() { yield ['confirm_action', 'member-remove']; yield ['confirm_id', 'pat']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('confirm')); await settleAll();
  assert.deepEqual(sent.filter(s => s.startsWith('DELETE')), ['DELETE /api/v1/workspaces/ws-a/members/pat ']);
});

test('UX9 customer copy: no internal terms; status-only failures read plainly while server field messages stay', async () => {
  const views = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  const app = fs.readFileSync(path.join(APP, 'app.js'), 'utf8');
  for (const old of ['SuppliedBriefRef', 'recorded: binding', '(outbox ', 'Brief saved as', 'operator-owned', 'Binding never proves fulfillment',
    'Proposals change no permission or grant', 'linkage is computed, never attached', '¢', 'Personal note · revision', '· revision ${t.revision',
    'It was prepared from revision', 'Object (type:id)', '(invite ${', 'Last successful baseline used definition revision']) {
    assert.ok(!views.includes(old), 'reworded: ' + old);
  }
  assert.ok(!app.includes('Request failed ('), 'no "Request failed (status)"');
  let reply = null;
  const E = loadAt('#/overview', { fetch: async () => reply }).EW;
  const failWith = async (status, body) => { reply = { ok: false, status, text: async () => JSON.stringify(body) }; return E.api('/customer/x').then(() => null, e => e.message); };
  assert.equal(await failWith(400, { ok: false }), "That didn't work. Check the entries and try again.");
  assert.equal(await failWith(403, { ok: false }), "You don't have permission to do that.");
  assert.equal(await failWith(404, { ok: false }), 'That item no longer exists.');
  assert.equal(await failWith(429, { ok: false }), 'Too many requests right now. Wait a moment, then try again.');
  assert.equal(await failWith(503, { ok: false }), 'Something went wrong on our side. Try again.');
  assert.equal(await failWith(400, { ok: false, error: { code: 'BAD_INPUT', message: 'date_to: must not precede date_from' } }), 'date_to: must not precede date_from');
  // A conflict keeps the stored revision, under Technical details.
  assert.match(E.conflictBlock(4), /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Current server revision is 4\.<\/p><\/details>/);
  assert.doesNotMatch(E.conflictBlock(4).replace(/<details[^]*<\/details>/, ''), /revision/);
});

test('UX10 plans read as names and prices; a target page names its show and episode, IDs under Technical details', async () => {
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    // Since BY2 the plan picker renders only when the server says in-app
    // checkout is on (`checkout: true`); checkout stays off at launch, and
    // frontend_purchase_billing covers the off case. This test checks the
    // picker's labels, so it turns checkout on.
    if (url === '/api/v1/billing/status') return json({ ok: true, workspace_context: 'ctx-a', configured: true, checkout: true, entitlement: { state: 'past_due', allowed: false },
      prices: [{ price_id: 'price_1Abc', amount_cents: 999, interval: 'month', trial_days: 0 }, { price_id: 'price_2Def', amount_cents: 9900, interval: 'year', trial_days: 14 }] });
    if (url === '/api/v1/customer/campaigns/3') return json({ ok: true, workspace_context: 'ctx-a', campaign: { id: 3, name: 'Study' } });
    if (url === '/api/v1/customer/campaigns/3/targets') return json({ ok: true, workspace_context: 'ctx-a', targets: [{ id: 't1', scope: 'episode', show_id: 's3', episode_id: 'e1', revision: 4 }] });
    if (url === '/api/v1/customer/shows/s3') return json({ ok: true, workspace_context: 'ctx-a', show_id: 's3', display_title: 'Show Three', episodes: [{ id: 'e1', ep_title: 'Pilot' }] });
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  assert.match(t.root.innerHTML, /<option value="price_1Abc">\$9\.99 \/ month<\/option><option value="price_2Def">\$99\.00 \/ year · 14-day free trial<\/option>/);
  t.win.location.hash = '#/campaign/3/target/t1'; t.win.EW.current.route = '#/campaign/3/target/t1';
  await t.win.EWViews.render(); await settleAll();
  const page = t.root.innerHTML;
  assert.match(page, /Episode: Pilot/);
  assert.match(page, /<dt>Scope<\/dt><dd>One episode · Show Three · Pilot<\/dd>/);
  const visible = page.replace(/<details class="tech-details">[^]*?<\/details>/g, '').replace(/<[^>]+>/g, ' ');
  assert.doesNotMatch(visible, /revision|\bs3\b|\be1\b/, 'no revision or raw IDs on the reading path');
  assert.match(page, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Target <span class="mono">t1<\/span> · revision 4/);
});

test('UX11 Settings: the stored Viewer role reads out (never a form), unique ids, and a checkout button restored from the back-forward cache works', async () => {
  for (const [viewer, shown] of [['comment', 'Comment'], ['read', 'Read only'], [undefined, 'Not set']]) {
    const t = await uiWin(async (url) => (url === '/api/v1/session' ? json(hostedSession())
      : json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1, role_matrix: viewer ? { viewer } : {} }, members: [], shares: [] })));
    t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
    await t.win.EWViews.render(); await settleAll();
    assert.doesNotMatch(t.root.innerHTML, /<select id="role-viewer"/, 'no role form');
    assert.match(t.root.innerHTML, new RegExp(`<dt>Viewer role</dt><dd>${shown}</dd>`), String(viewer));
    const ids = [...t.root.innerHTML.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]);
    assert.deepEqual(ids.filter((id, i) => ids.indexOf(id) !== i), [], 'no duplicate ids');
  }
  const listeners = {};
  const win = loadAt('#/settings', { fetch: canonicalSessionFetch() });
  win.addEventListener = (type, fn) => { listeners[type] = fn; };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await settleAll();
  const busy = { disabled: true, textContent: 'Starting checkout…' };
  win.document.querySelectorAll = s => (s === '#billing-checkout [type=submit]' ? [busy] : []);
  assert.equal(typeof listeners.pageshow, 'function');
  listeners.pageshow({ persisted: false });
  assert.equal(busy.disabled, true, 'an ordinary load changes nothing');
  listeners.pageshow({ persisted: true });
  assert.equal(busy.disabled, false);
  assert.equal(busy.textContent, 'Upgrade');
});

/* -- UX pass 7b: in-flight sibling saves, the real mistakes shape, stash merge, plain failure toasts, scoped invite links, retries that keep choices -- */
// Settings with its name and display forms rebuilt from markup on every
// render; a replaced form is detached (isConnected false).
const settingsForms = async (respond, { savingVisible = true } = {}) => {
  const t = await uiWin(respond);
  const st = { forms: {}, html: '', renders: 0, withDisplay: true };
  Object.defineProperty(t.root, 'innerHTML', { get: () => st.html, set: (v) => {
    st.html = v;
    for (const f of Object.values(st.forms)) f.isConnected = false;
    st.forms = {};
    const ws = v.match(/<form id="settings-workspace"><input type="hidden" name="expected_revision" value="([^"]*)">.*?name="workspace_name" value="([^"]*)"/);
    if (ws) st.forms['settings-workspace'] = Object.assign(pageForm(ws[2], ws[1]), { isConnected: true });
    const display = st.withDisplay && v.match(/<form id="settings-display"[^>]*><input type="hidden" name="expected_revision" value="([^"]*)">/);
    if (display) st.forms['settings-display'] = Object.assign(pageForm('Reno', display[1], 'settings-display', 'display_name'), { isConnected: true });
    if (ws) st.renders++;
  } });
  const baseQ = t.root.querySelector;
  t.root.querySelector = s => st.forms[s.replace(/^#/, '')] || baseQ(s);
  t.win.document.querySelectorAll = s => (/data-dirty/.test(s) ? Object.values(st.forms).filter(f => f.dataset.dirty === '1')
    : /data-saving/.test(s) && savingVisible ? Object.values(st.forms).filter(f => f.dataset.saving === '1') : []);
  t.win.FormData = class { constructor(f) { this.f = f; } *entries() { for (const e of this.f.elements) if (e.name) yield [e.name, e.value]; } };
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  return { ...t, st };
};
const slowDisplayServer = (state) => async (url, opts = {}) => {
  if (url === '/api/v1/session') return json(hostedSession());
  if (url === '/api/v1/customer/preferences') {
    if (opts.method === 'PUT') {
      if (JSON.parse(opts.body).display_name) {
        await new Promise(r => { state.release = r; });
        return json({ ok: false, error: { code: 'CONFLICT', message: 'stale revision', details: { current_revision: state.rev } } }, false, 412);
      }
      state.rev++;
    }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: state.rev, role_matrix: { viewer: 'read' } } });
  }
  return json({ ok: true, workspace_context: 'ctx-a', members: [], shares: [] });
};

test('UX12 a sibling save never re-renders over a save still in flight; one render follows once all settle, entries kept', async () => {
  const state = { rev: 1 };
  const t = await settingsForms(slowDisplayServer(state));
  const name = t.st.forms['settings-workspace'], display = t.st.forms['settings-display'];
  name.field.value = 'Typed name'; name.dataset.dirty = '1';
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const displaySave = t.win.EWViews.handleSubmit(display); await settleAll();
  const before = t.st.renders;
  await t.win.EWViews.handleSubmit(name); await settleAll();
  assert.equal(state.rev, 2, 'the name saved');
  assert.equal(t.st.forms['settings-display'], display, 'the display form is not replaced while it saves');
  assert.equal(display.field.value, 'comment');
  assert.equal(t.st.renders, before, 'the re-render waits');
  state.release(); await displaySave; await settleAll();
  assert.equal(t.st.renders, before + 1, 'exactly one render once every save settled');
  const live = t.st.forms['settings-display'];
  assert.equal(live.field.value, 'comment', 'the typed entry survives the 412');
  assert.equal(live.rev.value, '1', 'on its original revision: never rebased');
  assert.equal(live.dataset.dirty, '1');
  assert.match(live.box.innerHTML, /This record changed since you typed it; saving will show the differences first\./);
  assert.equal(t.st.forms['settings-workspace'].rev.value, '2', 'the saved name form shows the new revision');
  assert.equal(t.st.forms['settings-workspace'].dataset.dirty, undefined);
});

// Leaves Settings and comes back while a save is in flight: the real
// path by which a page form is replaced mid-save.
const awayAndBack = async (t) => {
  t.win.location.hash = '#/overview'; t.win.EW.current.route = '#/overview';
  await t.win.EWViews.render(); await settleAll();
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
};
// The save counter is back to zero exactly when a deferred render can
// run: a slow sibling save plus an ordinary one render once both settle.
const assertSavesSettle = async (t, state) => {
  const live = t.st.forms['settings-display'];
  live.field.value = 'comment'; live.dataset.dirty = '1';
  const slow = t.win.EWViews.handleSubmit(live); await settleAll();
  const name = t.st.forms['settings-workspace']; name.field.value = 'X'; name.dataset.dirty = '1';
  const before = t.st.renders;
  await t.win.EWViews.handleSubmit(name); await settleAll();
  assert.equal(t.st.renders, before, 'deferred while the slow save is in flight');
  state.release(); await slow; await settleAll(40);
  assert.equal(t.st.renders, before + 1, 'the save count returned to zero: the deferred render ran');
};

test('UX12b safety net (away and back mid-save): an untouched live form gets the outcome; the original is released', async () => {
  const state = { rev: 1 };
  const t = await settingsForms(slowDisplayServer(state));
  const display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const displaySave = t.win.EWViews.handleSubmit(display); await settleAll();
  await awayAndBack(t);
  const live = t.st.forms['settings-display'];
  assert.ok(live && live !== display, 'replaced while saving');
  state.release(); await displaySave; await settleAll();
  assert.equal(live.field.value, 'comment', 'the user\'s entry, on the live form');
  assert.equal(live.rev.value, '1', 'on its original revision');
  assert.equal(live.dataset.dirty, '1');
  assert.match(live.box.innerHTML, /This record changed while you were editing\./);
  assert.equal(display.box.innerHTML, '', 'nothing painted into the detached form');
  assert.equal(display.dataset.saving, undefined, 'the original form is released');
  await assertSavesSettle(t, state);
});

test('UX12c safety net never overwrites entries typed into the live form; a toast tells the outcome (reviewer R7b-2/R7b-3)', async () => {
  const state = { rev: 1 };
  const t = await settingsForms(slowDisplayServer(state));
  const display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const displaySave = t.win.EWViews.handleSubmit(display); await settleAll();
  await awayAndBack(t);
  const live = t.st.forms['settings-display'];
  live.field.value = 'admin'; live.dataset.dirty = '1';
  state.release(); await displaySave; await settleAll();
  assert.equal(live.field.value, 'admin', 'the newer entry stays');
  assert.equal(live.box.innerHTML, '', 'nothing painted over it');
  assert.ok(t.toasts.includes('Your change in ws-a was not saved: it changed meanwhile.'), t.toasts.join(' | '));
  assert.equal(display.dataset.saving, undefined, 'the original form is released');
  live.reset(); delete live.dataset.dirty;
  await assertSavesSettle(t, state);
});

test('UX13 Add personal note finds the existing note in the real {mistakes} reply and edits it, never a duplicate', async () => {
  const t = await uxWin(async (url, opts) => {
    if (url.endsWith('/mistake') && opts.method === 'GET') return json({ ok: true, workspace_context: 'ctx-a', mistakes: [{ id: 5, revision: 3, status: 'open', reason: 'Kept' }] });
    if (opts.method === 'PUT' || opts.method === 'POST') return json({ ok: true, workspace_context: 'ctx-a', id: 5, revision: 4 });
    return null;
  }, '#/evidence/sr1%3Ax');
  t.win.EW.render = () => {};
  await t.win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: 'sr1:x' } }); await settleAll();
  assert.match(t.modal.innerHTML, /Current note: open · Kept/);
  assert.match(t.modal.innerHTML, /name="mistake_id" value="5"/);
  t.win.FormData = class { *entries() { yield* markupEntries(t.modal.innerHTML); yield ['reason', 'Edited']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('mistake')); await settleAll();
  assert.deepEqual(t.writes().map(w => w.split(' ').slice(0, 2).join(' ')), ['PUT /api/v1/customer/mistakes/5']);
});

test('UX14 a same-page stash keeps items still waiting for forms not on the page', async () => {
  const t = await settingsForms(async (url) => (url === '/api/v1/session' ? json(hostedSession())
    : json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 }, members: [], shares: [] })));
  t.st.forms['settings-workspace'].field.value = 'N1'; t.st.forms['settings-workspace'].dataset.dirty = '1';
  t.st.forms['settings-display'].field.value = 'comment'; t.st.forms['settings-display'].dataset.dirty = '1';
  t.st.withDisplay = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.equal(t.st.forms['settings-workspace'].field.value, 'N1');
  assert.equal(t.st.forms['settings-display'], undefined, 'display is waiting for its form');
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  t.st.withDisplay = true;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.equal(t.st.forms['settings-display'].field.value, 'comment', 'the waiting item was not dropped');
  assert.equal(t.st.forms['settings-display'].dataset.dirty, '1');
  assert.equal(t.st.forms['settings-workspace'].field.value, 'N1');
});

test('UX15 a failure toast adds a reason only when the server gave one', async () => {
  // A status-only 400 keeps its plain reason as its own sentence (UX21).
  for (const [body, expected] of [[{ ok: false }, 'Your change in ws-a was not saved. Check the entries and try again.'],
    [{ ok: false, error: { code: 'BAD_INPUT', message: 'reason: too long' } }, 'Your change in ws-a was not saved: reason: too long.']]) {
    let gate;
    const t = await uxWin(async (url, opts) => {
      if (opts.method === 'POST') { await new Promise(r => { gate = r; }); return json(body, false, 400); }
      return null;
    }, '#/evidence/sr1%3Ax');
    t.win.EW.openDialog('Add personal note', '', '', '');
    const form = reviewerForm('mistake');
    form.closest = s => (s === '#modal' ? t.modal : null);
    t.win.FormData = class { *entries() { yield ['ref', 'sr1:x']; yield ['reason', 'Wrong brand']; yield ['status', 'open']; } };
    const saving = t.win.EWViews.handleSubmit(form); await settleAll();
    t.modal.open = false; // the user closed the dialog meanwhile
    gate(); await saving; await settleAll();
    assert.deepEqual(t.toasts, [expected]);
  }
});

test('UX16 invite and share entry links stay in their workspace; a real switch and a sign-out clear them', async () => {
  let ws = 'a', signedOut = false;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session/workspace') { ws = 'b'; return json(sessB()); }
    if (url === '/api/v1/session') return signedOut ? json({ ok: false, error: 'sign in', signin: { available: true } }, false, 401) : json(ws === 'b' ? sessB() : hostedSession());
    if (opts.method === 'POST' && url.endsWith('/invites')) return json({ ok: true, workspace_context: 'ctx-a', invite_id: 'inv-42', member: 'pat' });
    if (opts.method === 'POST' && url === '/api/v1/shares') return json({ ok: true, workspace_context: 'ctx-a', share_id: 'sh-7' });
    return json({ ok: true, workspace_context: 'ctx-' + ws, preferences: { revision: 1 }, members: [], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('invite-create')); await settleAll();
  t.win.FormData = class { *entries() { yield ['object_type', 'evidence']; yield ['object_id', 'ev-9']; yield ['audience', 'bob']; } };
  await t.win.EWViews.handleSubmit(reviewerForm('share-create')); await settleAll();
  assert.match(t.root.innerHTML, /inv-42/);
  assert.match(t.root.innerHTML, /share_id=sh-7/);
  // A real switch to ws-b through the chooser.
  t.win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? { textContent: '' } : s === '[type=submit]' ? { disabled: false, textContent: 'Use workspace' } : null),
    querySelectorAll: () => [], elements: [] };
  await t.win.EWViews.handleSubmit(choice); await settleAll(40);
  assert.equal(t.win.EW.current.activeWorkspace, 'ws-b');
  assert.equal(t.win.EW.current.createdInvite, null);
  assert.equal(t.win.EW.current.createdShare, null);
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  assert.doesNotMatch(t.root.innerHTML, /inv-42|sh-7|Entry link/, 'nothing from ws-a shows in ws-b');
  // Signing out (a 401 session read) clears them too.
  t.win.EW.current.createdInvite = { workspace: 'ws-b', id: 'inv-77', member: 'kim' };
  signedOut = true;
  await t.win.EW.boot().catch(() => {}); await settleAll();
  assert.equal(t.win.EW.current.createdInvite, null);
});

test('UX17 Filters Retry keeps unapplied choices; the alert dialog tells a failed show list with Retry', async () => {
  let showsDown = true;
  const t = await uxWin(async (url) => {
    if (url.includes('field=brands')) return down();
    if (url.includes('field=shows')) return json({ ok: true, workspace_context: 'ctx-a', items: [{ value: 's1', label: 'One' }, { value: 's2', label: 'Two' }] });
    if (url === '/api/v1/customer/shows') return showsDown ? down() : json({ ok: true, workspace_context: 'ctx-a', shows: [{ id: 's1', display_title: 'One' }, { id: 's9', display_title: 'Nine' }] });
    return null;
  }, '#/search');
  // The open Filters dialog, as the user left it: s2 checked, nothing
  // applied yet (PLAIN-ADS: the dialog has no status group).
  const checked = { shows: ['s2'] };
  const draft = { getAttribute: () => 'filters', elements: [],
    querySelector: s => (/^\[name=init_/.test(s) ? { value: 'null' } : /^fieldset/.test(s) ? { dataset: {} } : null),
    querySelectorAll: s => { const m = /input\[name=(\w+)\]:checked/.exec(s); return m ? (checked[m[1]] || []).map(value => ({ value })) : []; } };
  t.win.FormData = class { *entries() { yield ['sort', 'oldest']; } };
  await t.win.EWViews.handleAction({ dataset: { action: 'filters-retry' }, closest: s => (s === 'form' ? draft : null) }); await settleAll();
  assert.match(t.modal.innerHTML, /<input type="checkbox" name="shows" value="s2" checked>/);
  assert.doesNotMatch(t.modal.innerHTML, /name="statuses"/);
  assert.match(t.modal.innerHTML, /name="sort" value="oldest" checked/);
  assert.match(t.modal.innerHTML, /data-action="filters-retry"[^>]*>Retry</, 'the failed group keeps its Retry');
  // Alert dialog: a failed show list is told, the choice kept, Retry refills.
  t.win.EW.current.route = '#/overview'; t.win.location.hash = '#/overview';
  await t.win.EWViews.handleAction({ dataset: { action: 'alert-brand', id: 'acme' } }); await settleAll();
  assert.match(t.modal.innerHTML, /<div id="alert-shows-status" role="status"><p class="field-error">The shows could not be loaded\. Try again\.<\/p>/);
  assert.match(t.modal.innerHTML, /data-action="alert-shows-retry"[^>]*>Retry</);
  const select = { innerHTML: '', value: '' }, status = { innerHTML: 'x' };
  t.modal.querySelector = s => (s === '#alert-show' ? select : s === '#alert-shows-status' ? status : null);
  showsDown = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'alert-shows-retry' } }); await settleAll();
  assert.match(select.innerHTML, /<option value="" selected>All shows<\/option><option value="s1" >One<\/option><option value="s9" >Nine<\/option>/);
  assert.equal(status.innerHTML, '');
});

/* -- UX pass 7c: the save count never leaks; deferred renders respect a moved display and say they wait -- */
test('UX18 a replaced form that saved is released, so later deferred renders still run (reviewer R7b-1)', async () => {
  const state = { rev: 1 };
  let visible = false;
  const t = await settingsForms(slowDisplayServer(state));
  t.win.document.querySelectorAll = s => (/data-dirty/.test(s) ? Object.values(t.st.forms).filter(f => f.dataset.dirty === '1')
    : /data-saving/.test(s) && visible ? Object.values(t.st.forms).filter(f => f.dataset.saving === '1') : []);
  const display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const s1 = t.win.EWViews.handleSubmit(display); await settleAll();
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.notEqual(t.st.forms['settings-display'], display);
  state.release(); await s1; await settleAll();
  assert.equal(display.dataset.saving, undefined, 'never left marked saving');
  visible = true;
  await assertSavesSettle(t, state);
});

test('UX19 a deferred render never runs after the display moved; held drafts stay on screen (reviewer R7b-4)', async () => {
  const state = { rev: 1 }; let moved = false;
  const base = slowDisplayServer(state);
  const server = async (url, opts = {}) => {
    if (moved && url === '/api/v1/session') return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }));
    const r = await base(url, opts);
    if (!moved) return r;
    const body = JSON.parse(await r.text());
    if (body.workspace_context) body.workspace_context = 'ctx-b';
    return json(body, r.ok, r.status);
  };
  const t = await settingsForms(server);
  const name = t.st.forms['settings-workspace'], display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const s1 = t.win.EWViews.handleSubmit(display); await settleAll();
  name.field.value = 'N'; name.dataset.dirty = '1';
  await t.win.EWViews.handleSubmit(name); await settleAll();
  const before = t.st.renders;
  moved = true;
  await t.win.EWViews.reconcileCanonical(); await settleAll();
  state.release(); await s1; await settleAll(40);
  assert.equal(t.st.renders, before, 'no render over the moved display');
  assert.equal(t.st.forms['settings-display'], display, 'the held draft stays on screen until Reload');
  assert.equal(display.dataset.held, '1');
  assert.equal(display.field.value, 'comment');
});

test('UX20 a re-render waiting for another save says so', async () => {
  const state = { rev: 1 };
  const t = await settingsForms(slowDisplayServer(state));
  const notes = []; t.win.EW.saveNotice = (m) => notes.push(m);
  const display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  const s1 = t.win.EWViews.handleSubmit(display); await settleAll();
  const before = t.st.renders;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.equal(t.st.renders, before, 'the display form is not re-rendered away mid-save');
  assert.equal(notes[notes.length - 1], 'Waiting for your other change to finish…');
  state.release(); await s1; await settleAll(40);
  assert.equal(t.st.renders, before + 1, 'the waiting render runs once the save settles');
});

test('UX21 failure toasts keep code-mapped guidance and the status reason (400, 403, 404, 422, 429)', async () => {
  for (const [status, body, expected] of [
    [400, { ok: false, error: { code: 'legacy_review_required' } }, "Your change in ws-a was not saved: This campaign was created before campaign types existed. It's read-only until the workspace owner reviews it."],
    [403, { ok: false }, "Your change in ws-a was not saved. You don't have permission to do that."],
    [429, { ok: false }, 'Your change in ws-a was not saved. Too many requests right now. Wait a moment, then try again.'],
    [404, { ok: false }, 'Your change in ws-a was not saved. That item no longer exists.'],
    [400, { ok: false }, 'Your change in ws-a was not saved. Check the entries and try again.'],
    [422, { ok: false }, 'Your change in ws-a was not saved. Check the entries and try again.']]) {
    let gate;
    const t = await uxWin(async (url, opts) => {
      if (opts.method === 'POST') { await new Promise(r => { gate = r; }); return json(body, false, status); }
      return null;
    }, '#/evidence/sr1%3Ax');
    t.win.EW.openDialog('Add personal note', '', '', '');
    const form = reviewerForm('mistake');
    form.closest = s => (s === '#modal' ? t.modal : null);
    t.win.FormData = class { *entries() { yield ['ref', 'sr1:x']; yield ['reason', 'Wrong brand']; yield ['status', 'open']; } };
    const saving = t.win.EWViews.handleSubmit(form); await settleAll();
    t.modal.open = false;
    gate(); await saving; await settleAll();
    assert.deepEqual(t.toasts, [expected], String(status));
  }
});

/* -- UX pass 7d: a required render is never lost behind an older deferral -- */
test('UX22 a render required by another tab\'s move is never deferred behind an older deferral (reviewer R7c-1)', async () => {
  const state = { rev: 1 }; let moved = false;
  const base = slowDisplayServer(state);
  const server = async (url, opts = {}) => {
    if (moved && url === '/api/v1/session') return json(hostedSession({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }));
    const r = await base(url, opts);
    if (!moved) return r;
    const body = JSON.parse(await r.text());
    if (body.workspace_context) body.workspace_context = 'ctx-b';
    if (body.preferences) body.preferences.workspace_name = 'Beta';
    return json(body, r.ok, r.status);
  };
  const t = await settingsForms(server);
  const name = t.st.forms['settings-workspace'], display = t.st.forms['settings-display'];
  // display is submitted untouched (prefilled values): saving, never dirty.
  const s1 = t.win.EWViews.handleSubmit(display); await settleAll();
  name.field.value = 'N'; name.dataset.dirty = '1';
  await t.win.EWViews.handleSubmit(name); await settleAll(); // deferred, basis ctx-a
  moved = true;
  await t.win.EWViews.reconcileCanonical(); await settleAll(40);
  assert.match(t.st.html, /Beta/, 'the page shows the current workspace right away');
  state.release(); await s1; await settleAll(40);
  assert.match(t.st.html, /Beta/, 'and still does once the saves settle');
  assert.equal(t.win.EW.current.workspaceContext, 'ctx-b');
});

/* -- Workspace display names; pending invitations on Settings -- */
const WS_NAMES = { 'ws-a': 'Acme & Co', 'ws-b': 'Brand Team', 'ws-proof': 'Proof Lab' };

test('NAMES1 the session names workspaces: names for people, IDs for requests, IDs when unnamed', async () => {
  const win = loadViews(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: { 'ws-a': 'Acme & Co', 'ws-b': '  ' } }));
    throw new Error('no network in unit test');
  });
  await settleAll();
  const E = win.EW;
  assert.deepEqual(plain(E.current.workspaces), ['ws-a', 'ws-b', 'ws-proof']);
  assert.equal(E.workspaceName('ws-a'), 'Acme & Co');
  assert.equal(E.workspaceName('ws-b'), 'ws-b', 'a blank name falls back to the ID');
  assert.equal(E.workspaceName('ws-proof'), 'ws-proof', 'an unnamed workspace keeps its ID');
  assert.equal(E.workspaceName('toString'), 'toString', 'never an inherited property');
  assert.equal(E.workspaceName('the previous workspace'), 'the previous workspace');
});

test('NAMES2 the Settings chooser lists names and keeps IDs as the chosen values', async () => {
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [], shares: [], invites: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  const html = t.root.innerHTML;
  assert.match(html, /<option value="ws-a" selected>Acme &amp; Co<\/option>/);
  assert.match(html, /<option value="ws-b"\s*>Brand Team<\/option>/);
  assert.doesNotMatch(html, /<option[^>]*>ws-[ab]<\/option>/, 'no bare IDs in the chooser');
  assert.match(html, /id="settings-name" name="workspace_name" value="Acme &amp; Co"/, 'the display name field starts from the workspace name');
});

test('NAMES3 late-write toasts name the workspace a change went to', async () => {
  for (const [kind, reply, copy] of [
    ['saved', () => json({ ok: true }), 'Saved in Acme & Co.'],
    ['rejected', () => json({ ok: false, error: { code: 'VALIDATION', message: 'bad' } }, false, 422), 'Your change in Acme & Co was not saved.'],
  ]) {
    const t = await lateWriteWin('/shares/', reply, { workspace_names: WS_NAMES });
    const el = { dataset: { action: 'share-revoke', share: 'sh1' }, closest: () => null, setAttribute() {} };
    const p = t.win.EWViews.handleAction(el).catch(e => t.toasts.push('THROW ' + e.message));
    await settleAll(5);
    await t.moveToB();
    t.release(); await p; await settleAll(20);
    assert.ok(t.toasts.includes(copy), kind + ': ' + t.toasts.join(' | '));
    assert.ok(!t.toasts.some(m => /ws-a/.test(m)), kind + ': no workspace ID in the copy');
  }
});

test('NAMES4 switch copy names the workspace switched to', async () => {
  let moved = false;
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session/workspace') { moved = true; return json(hostedSession({ workspace_names: WS_NAMES, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return json(hostedSession(moved ? { workspace_names: WS_NAMES, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 } : { workspace_names: WS_NAMES }));
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a', preferences: { revision: 1 }, members: [], shares: [], invites: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  t.win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? { textContent: '' } : s === '[type=submit]' ? { disabled: false, textContent: 'Use workspace' } : null),
    querySelectorAll: () => [], elements: [] };
  await t.win.EWViews.handleSubmit(choice); await settleAll(40);
  assert.equal(t.win.EW.current.activeWorkspace, 'ws-b');
  assert.ok(t.toasts.includes('Switched to Brand Team.'), t.toasts.join(' | '));
});

test('NAMES5 two workspaces with the same name carry their IDs in the chooser, toasts and notices', async () => {
  const DUP = { 'ws-a': 'Marketing', 'ws-b': ' marketing', 'ws-proof': 'Proof Lab' };
  let moved = false;
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session/workspace') { moved = true; return json(hostedSession({ workspace_names: DUP, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return json(hostedSession(moved ? { workspace_names: DUP, active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 } : { workspace_names: DUP }));
    return json({ ok: true, workspace_context: moved ? 'ctx-b' : 'ctx-a', preferences: { revision: 1 }, members: [], shares: [], invites: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  const html = t.root.innerHTML;
  assert.match(html, /<option value="ws-a" selected>Marketing \(ws-a\)<\/option>/);
  assert.match(html, /<option value="ws-b"\s*>marketing \(ws-b\)<\/option>/);
  assert.match(html, /<option value="ws-proof"\s*>Proof Lab<\/option>/, 'a unique name stays plain');
  assert.match(html, /id="settings-name" name="workspace_name" value="Marketing"/, 'the name field edits the name, never the ID');
  t.win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? { textContent: '' } : s === '[type=submit]' ? { disabled: false, textContent: 'Use workspace' } : null),
    querySelectorAll: () => [], elements: [] };
  await t.win.EWViews.handleSubmit(choice); await settleAll(40);
  assert.ok(t.toasts.includes('Switched to marketing (ws-b).'), t.toasts.join(' | '));
  const late = await lateWriteWin('/shares/', () => json({ ok: true }), { workspace_names: DUP });
  const p = late.win.EWViews.handleAction({ dataset: { action: 'share-revoke', share: 'sh1' }, closest: () => null, setAttribute() {} }).catch(e => late.toasts.push('THROW ' + e.message));
  await settleAll(5);
  await late.moveToB();
  late.release(); await p; await settleAll(20);
  assert.ok(late.toasts.includes('Saved in Marketing (ws-a).'), late.toasts.join(' | '));
});

test('NAMES6 a name that copies another workspace\'s label, or differs only in inner spaces, still reads apart', async () => {
  const t = await uiWin(async () => json(hostedSession({ workspace_names: WS_NAMES })));
  const name = (names, id) => { t.win.EW.current.workspaceNames = names; t.win.EW.current.workspaces = Object.keys(names); return t.win.EW.workspaceName(id); };
  const COPY = { 'ws-a': 'Marketing', 'ws-b': 'Marketing', 'ws-x': 'Marketing (ws-a)' };
  assert.deepEqual(['ws-a', 'ws-b', 'ws-x'].map(w => name(COPY, w)), ['Marketing (ws-a)', 'Marketing (ws-b)', 'Marketing (ws-a) (ws-x)']);
  const SPACES = { 'ws-a': 'Mar keting', 'ws-b': 'Mar   keting', 'ws-c': 'Sales' };
  assert.deepEqual(['ws-a', 'ws-b', 'ws-c'].map(w => name(SPACES, w)), ['Mar keting (ws-a)', 'Mar   keting (ws-b)', 'Sales']);
  assert.equal(name({ 'ws-a': 'ws-b' }, 'ws-b'), 'ws-b', 'an unnamed workspace keeps its bare ID');
  assert.equal(t.win.EW.workspaceName('ws-a', true), 'ws-b', 'bare keeps the name a field edits');
  // Each name copies the label before it: every label still reads apart.
  const CHAIN = { 'ws-0': 'M', 'ws-1': 'M' };
  for (let i = 2, prev = 'M (ws-0)'; i < 200; prev += ` (ws-${i})`, i++) CHAIN['ws-' + i] = prev;
  const labels = Object.keys(CHAIN).map(w => name(CHAIN, w).replace(/\s+/g, ' ').toLowerCase());
  assert.equal(new Set(labels).size, 200);
});

const invitesWin = async (invitesReply) => {
  const sent = [];
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') return invitesReply();
    if (opts.method === 'POST' && url.endsWith('/send')) {
      sent.push({ url, headers: opts.headers || {} });
      return json({ ok: true, workspace_context: 'ctx-a', invite_id: 'inv-42', message_id: 3, status: 'sent', unresolved: false, reason: '' });
    }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  return { ...t, sent };
};

test('INV1 Settings lists pending invitations; each is sent from its row in the displayed workspace without copying an ID', async () => {
  const t = await invitesWin(() => json({ ok: true, invites: [
    { id: 'inv-42', member: 'pat@example.com', role: 'viewer', created_at: '2026-10-08T10:00:00+00:00', delivery: 'not_queued' },
    { id: 'inv-43', member: 'kim@example.com', role: 'editor', created_at: '2026-10-08T11:00:00+00:00', delivery: 'pending' }] }));
  const html = t.root.innerHTML;
  assert.match(html, /<h3>Pending invitations<\/h3>/);
  // L25: the email-send state in words, then the row's one send action.
  assert.match(html, /pat@example\.com · Viewer<\/span><span class="actions"><span class="ew-tag[^"]*" data-tag="neutral">Not sent<\/span><button class="button" type="button" data-action="invite-send" data-invite="inv-42">Send invite<\/button>/);
  assert.match(html, /kim@example\.com · Editor<\/span><span class="actions"><span[^>]*data-tag="pending">[^]*?Pending<\/span><button[^>]*data-invite="inv-43">Send invite<\/button>/);
  // The invite ID is only in Technical details.
  const visible = html.replace(/<details class="tech-details">[\s\S]*?<\/details>/g, '');
  assert.doesNotMatch(visible.replace(/data-invite(-row)?="inv-4\d"/g, ''), /inv-4[23]/);
  await t.win.EWViews.handleAction({ dataset: { action: 'invite-send', invite: 'inv-42' }, closest: () => null, setAttribute() {} });
  await settleAll();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].url, '/api/v1/workspaces/ws-a/invites/inv-42/send');
  assert.equal(t.sent[0].headers['X-EarWitness-Workspace-Switch-Revision'], '5', 'the write carries the displayed epoch');
  assert.ok(t.toasts.includes('Email send recorded. Inbox delivery is not guaranteed.'), t.toasts.join(' | '));
});

test('INV2 roles that cannot invite see no pending list; a failed read says so with Retry', async () => {
  const hidden = await invitesWin(() => json({ ok: false, error: { code: 'FORBIDDEN', message: 'owner manages membership' } }, false, 403));
  assert.doesNotMatch(hidden.root.innerHTML, /Pending invitations|Invite someone|could not be loaded/);
  const empty = await invitesWin(() => json({ ok: true, invites: [] }));
  assert.match(empty.root.innerHTML, /<h3>Pending invitations<\/h3><p class="section-note">No pending invitations\.<\/p>/);
  const down = await invitesWin(() => json({ ok: false, error: { code: 'INTERNAL', message: 'internal error' } }, false, 500));
  assert.match(down.root.innerHTML, /Pending invitations could not be loaded\.[\s\S]*data-action="retry"/);
  assert.doesNotMatch(down.root.innerHTML, /No pending invitations/);
});

// Fake request timers: nothing fires until the test says so.
const fakeRequestTimers = (win) => {
  const timers = [];
  win.EW.requestTimeout.set = (fn, ms) => { const h = { fn, ms, cleared: false }; timers.push(h); return h; };
  win.EW.requestTimeout.clear = (h) => { if (h) h.cleared = true; };
  return { timers, pending: () => timers.filter(h => !h.cleared) };
};

test('TO1 a save with no reply times out as unconfirmed: one request, saves in flight released, the entry held, never retried', async () => {
  let posts = 0;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && url.endsWith('/invites')) { posts++; return new Promise(() => {}); }
    if (url === '/api/v1/customer/hang') return new Promise(() => {});
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [], shares: [] });
  });
  const clock = fakeRequestTimers(t.win);
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  const form = reviewerForm('invite-create', { entries: true });
  const saving = t.win.EWViews.handleSubmit(form); await settleAll();
  assert.equal(t.win.EW.current.writesInFlight, 1);
  assert.deepEqual(clock.pending().map(h => h.ms), [30000], 'a write waits 30 s');
  clock.pending()[0].fn();
  await saving; await settleAll();
  assert.equal(t.win.EW.current.writesInFlight, 0, 'the in-flight count is released');
  await t.win.EW.whenWritesSettled();
  assert.notEqual(form.dataset.saving, '1', 'the form is no longer saving');
  assert.equal(posts, 1, 'never retried');
  assert.match(form.box.innerHTML, /Your change could not be confirmed\. Reload the workspace to continue\./);
  assert.match(form.box.innerHTML, /data-action="reload-workspace"/);
  assert.doesNotMatch(form.box.innerHTML, /not saved|try again/i, 'never told as a failure to retry');
  assert.equal(form.submit.disabled, true, 'no resubmit from the held form');
  assert.equal(form.dataset.held, '1', 'the entry stays on screen');
  // Reload never claims the unconfirmed change was not applied.
  t.win.document.querySelectorAll = sel => (/data-held/.test(sel) ? [form] : []);
  await t.win.EWViews.handleAction({ dataset: { action: 'reload-workspace' } }); await settleAll();
  t.win.document.querySelectorAll = () => [];
  assert.ok(t.toasts.some(m => /could not be confirmed\. The page now shows what is saved\./.test(m)), t.toasts.join(' | '));
  assert.ok(!t.toasts.some(m => /was not applied/.test(m)), t.toasts.join(' | '));
  // The raw API outcome: unknown, never a status the views read as a refusal.
  const direct = t.win.EW.api('/customer/hang', { method: 'PUT', body: {} }).then(() => null, e => e);
  await settleAll();
  clock.pending()[0].fn();
  const err = await direct;
  assert.equal(err.status, undefined);
  assert.equal(err.outcomeUnknown, true);
  assert.equal(err.timedOut, true);
});

test('TO2 a read with no reply times out as a plain failure with Retry', async () => {
  let hang = true;
  const t = await uiWin(async (url) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/campaigns') {
      if (hang) return new Promise(() => {});
      return json({ ok: true, workspace_context: 'ctx-a', campaigns: [{ id: 2, name: 'Typed', mode: 'research', placement_count: 0, target_count: 1 }] });
    }
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  const clock = fakeRequestTimers(t.win);
  t.win.location.hash = '#/campaigns'; t.win.EW.current.route = '#/campaigns';
  const rendering = t.win.EWViews.render(); await settleAll();
  assert.deepEqual(clock.pending().map(h => h.ms), [20000], 'a read waits 20 s');
  clock.pending()[0].fn();
  await rendering; await settleAll();
  assert.match(t.root.innerHTML, /Campaigns are unavailable: The server took too long to respond\./);
  assert.match(t.root.innerHTML, /data-action="retry"[^>]*>Retry</);
  hang = false;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.match(t.root.innerHTML, /1 research target/);
  assert.deepEqual(clock.pending(), [], 'a settled request leaves no timer behind');
});

test('TO3 with timeouts on, navigation still aborts a read as before and replies still arrive', async () => {
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (url === '/api/v1/customer/slow') return new Promise((_, reject) => opts.signal.addEventListener('abort', () => reject(opts.signal.reason)));
    return json({ ok: true, workspace_context: 'ctx-a', value: 7 });
  });
  const clock = fakeRequestTimers(t.win);
  const ctl = new AbortController();
  const slow = t.win.EW.api('/customer/slow', { signal: ctl.signal }).then(() => null, e => e);
  await settleAll(2);
  ctl.abort();
  const err = await slow;
  assert.equal(err && err.name, 'AbortError');
  assert.ok(!err.timedOut);
  const out = await t.win.EW.api('/customer/fast');
  assert.equal(out.value, 7);
  // READ-QUEUE (G2-02): the left read is not aborted (the server still
  // counts it); only its own request timeout is left, and it ends it.
  assert.deepEqual(clock.pending().map(h => h.ms), [20000], 'only the left read\'s own timeout');
  clock.pending()[0].fn(); await settleAll(2);
  assert.deepEqual(clock.pending(), [], 'every timer cleared');
});

test('NE1 a network error on a save or a button write is unconfirmed, never a failure to retry; reads keep Retry', async () => {
  const sent = [];
  let readDown = true;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method && opts.method !== 'GET') { sent.push(opts.method + ' ' + url); throw new TypeError('Failed to fetch'); }
    if (url === '/api/v1/customer/campaigns') {
      if (readDown) throw new TypeError('Failed to fetch');
      return json({ ok: true, workspace_context: 'ctx-a', campaigns: [] });
    }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  // A form save: held with Reload, entries kept, submit disabled.
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  const form = reviewerForm('invite-create', { entries: true });
  await t.win.EWViews.handleSubmit(form); await settleAll();
  assert.match(form.box.innerHTML, /Your change could not be confirmed\. Reload the workspace to continue\./);
  assert.match(form.box.innerHTML, /data-action="reload-workspace"/);
  assert.equal(form.submit.disabled, true);
  assert.equal(form.dataset.held, '1');
  assert.equal(t.win.EW.current.writesInFlight, 0);
  // The API outcome itself: unknown, status undefined.
  const err = await t.win.EW.api('/customer/preferences', { method: 'PUT', body: {} }).then(() => null, e => e);
  assert.equal(err.status, undefined);
  assert.equal(err.outcomeUnknown, true);
  assert.equal(err.message, 'Your change could not be confirmed.');
  // A button write: plain copy with Reload on the page, never "failed".
  await t.win.EWViews.handleAction({ dataset: { action: 'share-revoke', share: 'sh-1' } }); await settleAll();
  assert.ok(t.toasts.includes('Your change could not be confirmed. Reload the workspace to continue.'), t.toasts.join(' | '));
  assert.match(t.root.top(), /data-reconcile-notice[^]*Your change could not be confirmed\.[^]*data-action="reload-workspace"/);
  assert.ok(!t.toasts.some(m => /failed|Failed to fetch|try again/i.test(m)), t.toasts.join(' | '));
  // Each write was sent exactly once: nothing retried.
  assert.deepEqual(sent, ['POST /api/v1/workspaces/ws-a/invites', 'PUT /api/v1/customer/preferences', 'POST /api/v1/shares/sh-1']);
  // A read with a network error keeps the plain failure with Retry.
  t.win.location.hash = '#/campaigns'; t.win.EW.current.route = '#/campaigns';
  await t.win.EWViews.render(); await settleAll();
  assert.match(t.root.innerHTML, /Campaigns are unavailable: Failed to fetch/);
  assert.match(t.root.innerHTML, /data-action="retry"[^>]*>Retry</);
});

test('NE2 a brief registration with a network error is not offered again blind', async () => {
  let posts = 0;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession());
    if (opts.method === 'POST' && url === '/api/v1/customer/briefs') { posts++; throw new TypeError('Failed to fetch'); }
    return json({ ok: true, workspace_context: 'ctx-a' });
  });
  const box = { innerHTML: '' };
  const fields = { '[name=name]': { value: 'Spring push' }, '#brief-title': { value: 'Brief' }, '#brief-text': { value: 'Say lumen.' } };
  const form = { dataset: {}, closest: () => null, querySelector: sel => fields[sel] || null };
  const orig = t.win.document.querySelector;
  t.win.document.querySelector = sel => (sel === '#brief-status' ? box : orig(sel));
  const el = { dataset: { action: 'brief-register' }, disabled: false, closest: () => form };
  await t.win.EWViews.handleAction(el); await settleAll();
  assert.equal(posts, 1);
  assert.match(box.innerHTML, /The brief for Spring push could not be confirmed\./);
  assert.equal(el.disabled, true, 'Register stays off: the brief may already be saved');
});

test('INV3 a network error while sending an invitation is unconfirmed: one request, never a resend', async () => {
  let posts = 0;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') return json({ ok: true, invites: [
      { id: 'inv-42', member: 'pat@example.com', role: 'viewer', created_at: '2026-10-08T10:00:00+00:00', delivery: 'not_queued' }] });
    if (opts.method === 'POST' && url.endsWith('/send')) { posts++; throw new TypeError('Failed to fetch'); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  await t.win.EWViews.handleAction({ dataset: { action: 'invite-send', invite: 'inv-42' } }); await settleAll();
  assert.equal(posts, 1);
  // L25: the row says the send is uncertain and offers no resend.
  assert.ok(t.toasts.includes('EarWitness could not confirm whether the email was accepted. Do not send this invitation again.'), JSON.stringify(t.toasts));
  assert.ok(!t.toasts.some(x => /failed|try again/i.test(x)), JSON.stringify(t.toasts));
  assert.match(t.root.innerHTML, /data-tag="uncertain">[^]*?Send uncertain<\/span>/);
  assert.doesNotMatch(t.root.innerHTML, /data-action="invite-send"/);
});

/* -- Follow-up C: the safety net's page and revision guards, each deferral guard on its own, a truthful reload toast -- */
// Settings server whose revision really moves: a PUT on a stale
// expected_revision gets a 412 naming the current one. `hold.display`,
// `hold.name` and `hold.get` gate the next request of that kind.
const revState = () => ({ rev: 1, name: 'Alpha', puts: [], hold: {}, release: {} });
const revServer = (state) => async (url, opts = {}) => {
  if (url === '/api/v1/session') return json(hostedSession());
  if (url === '/api/v1/customer/preferences') {
    const kind = opts.method === 'PUT' ? (JSON.parse(opts.body).display_name ? 'display' : 'name') : 'get';
    if (state.hold[kind]) { state.hold[kind] = false; await new Promise(r => { state.release[kind] = r; }); }
    if (opts.method === 'PUT') {
      const body = JSON.parse(opts.body);
      state.puts.push({ kind, rev: body.expected_revision });
      if (body.expected_revision !== state.rev) {
        return json({ ok: false, error: { code: 'CONFLICT', message: 'stale revision', details: { current_revision: state.rev } } }, false, 412);
      }
      state.rev++;
      if (body.workspace_name) state.name = body.workspace_name;
    }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: state.name, revision: state.rev, role_matrix: { viewer: 'read' } } });
  }
  return json({ ok: true, workspace_context: 'ctx-a', members: [], shares: [] });
};
// The display form typed into and sent; its PUT waits for release and,
// with the server moved meanwhile, gets a 412.
const displayInFlight = async (t, state) => {
  const display = t.st.forms['settings-display'];
  display.field.value = 'comment'; display.dataset.dirty = '1';
  state.hold.display = true;
  const saving = t.win.EWViews.handleSubmit(display); await settleAll();
  state.rev = 2; // another tab saved meanwhile
  return { display, saving };
};
const WAITING = 'Waiting for your other change to finish…';

test('FC1 safety net refills on the ORIGINAL revision: the next Save 412s because the server moved', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { display, saving } = await displayInFlight(t, state);
  await awayAndBack(t);
  const live = t.st.forms['settings-display'];
  assert.ok(live && live !== display, 'replaced while saving');
  assert.equal(live.rev.value, '2', 'the live form was rendered on the moved revision');
  state.release.display(); await saving; await settleAll();
  assert.equal(live.field.value, 'comment', 'the entry, on the live form');
  assert.equal(live.rev.value, '1', 'on its original revision, never the live one');
  await t.win.EWViews.handleSubmit(live); await settleAll();
  assert.deepEqual(state.puts.map(p => p.rev), [1, 1], 'the next Save still sends the original revision');
  assert.equal(state.rev, 2, 'the newer server state is never overwritten');
  assert.match(live.box.innerHTML, /Reload and restore my entries/);
});

test('FC2 a late outcome never refills a same-id form on another page (as two placement pages would show)', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  t.win.location.hash = '#/overview'; t.win.EW.current.route = '#/overview';
  await t.win.EWViews.render(); await settleAll();
  // The page now shown has an untouched form with the same id.
  const other = Object.assign(pageForm('Reno', '7', 'settings-display', 'display_name'), { isConnected: true });
  t.st.forms['settings-display'] = other;
  state.release.display(); await saving; await settleAll();
  assert.equal(other.field.value, 'Reno', 'the other page\'s form is left alone');
  assert.equal(other.rev.value, '7');
  assert.equal(other.dataset.dirty, undefined);
  assert.equal(other.box.innerHTML, '');
  assert.ok(t.toasts.includes('Your change in ws-a was not saved: it changed meanwhile.'), t.toasts.join(' | '));
});

test('FC3 safety net never overwrites a live form whose text was edited, even before it was marked dirty', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  await awayAndBack(t);
  const live = t.st.forms['settings-display'];
  live.field.value = 'admin'; // typed; no input event has marked it yet
  state.release.display(); await saving; await settleAll();
  assert.equal(live.field.value, 'admin', 'the newer entry stays');
  assert.equal(live.rev.value, '2');
  assert.equal(live.box.innerHTML, '');
  assert.ok(t.toasts.includes('Your change in ws-a was not saved: it changed meanwhile.'), t.toasts.join(' | '));
});

// A render requested while the display save is in flight: did it run now?
const renderRequestedNow = async (t) => {
  const notes = []; t.win.EW.saveNotice = (m) => notes.push(m);
  const token = t.win.EW.current.fetchToken;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  return { ranNow: t.win.EW.current.fetchToken !== token, waited: notes.includes(WAITING) };
};

test('FC4 a cleared displayed epoch alone makes the render run now, never deferred', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  assert.equal(t.win.EW.current.hosted, true);
  assert.equal(t.win.EW.current.workspaceContext, t.win.EW.current.publishedContext, 'contexts still agree');
  t.win.EW.current.displayedWorkspaceSwitchRevision = undefined;
  assert.deepEqual(await renderRequestedNow(t), { ranNow: true, waited: false });
  state.release.display(); await saving; await settleAll();
});

test('FC5 an adopted context alone (epoch still displayed) makes the render run now, never deferred', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  assert.notEqual(t.win.EW.current.displayedWorkspaceSwitchRevision, undefined, 'the epoch is still displayed');
  t.win.EW.current.workspaceContext = 'ctx-b';
  assert.deepEqual(await renderRequestedNow(t), { ranNow: true, waited: false });
  state.release.display(); await saving; await settleAll();
});

test('FC6 a deferred render is skipped when only the publication moved (no held draft)', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { display, saving } = await displayInFlight(t, state);
  assert.deepEqual(await renderRequestedNow(t), { ranNow: false, waited: true });
  const before = t.st.renders;
  t.win.EW.current.publishedGeneration += 1;
  state.release.display(); await saving; await settleAll(40);
  assert.equal(display.dataset.held, undefined, 'no held draft is involved');
  assert.equal(t.st.renders, before, 'never rendered over the moved display');
});

test('FC7 a deferred render is skipped while a draft is held (display unchanged)', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { display, saving } = await displayInFlight(t, state);
  assert.deepEqual(await renderRequestedNow(t), { ranNow: false, waited: true });
  const before = t.st.renders;
  display.dataset.held = '1'; // its basis moved while it saved
  state.release.display(); await saving; await settleAll(40);
  assert.equal(t.st.renders, before, 'the held draft is never re-rendered away');
  assert.equal(t.st.forms['settings-display'], display);
  assert.equal(display.field.value, 'comment');
});

test('FC8 the latest deferral basis replaces an older one, so the render still runs', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  assert.deepEqual(await renderRequestedNow(t), { ranNow: false, waited: true });
  t.win.EW.current.publishedGeneration += 1;
  assert.deepEqual(await renderRequestedNow(t), { ranNow: false, waited: true });
  const before = t.st.renders;
  state.release.display(); await saving; await settleAll(40);
  assert.equal(t.st.renders, before + 1, 'rendered against the latest basis');
});

test('FC9 "Reload and restore my entries" waits for a render deferred by another save before saying values loaded', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  t.win.document.getElementById = id => t.st.forms[id] || null;
  const notes = []; t.win.EW.saveNotice = (m) => notes.push(m);
  const display = t.st.forms['settings-display'], name = t.st.forms['settings-workspace'];
  state.rev = 2; // another tab saved
  display.field.value = 'comment'; display.dataset.dirty = '1';
  await t.win.EWViews.handleSubmit(display); await settleAll();
  assert.match(display.box.innerHTML, /Reload and restore my entries/);
  name.field.value = 'N'; name.dataset.dirty = '1'; name.rev.value = '2';
  state.hold.name = true;
  const nameSave = t.win.EWViews.handleSubmit(name); await settleAll();
  const before = t.st.renders;
  const reload = t.win.EWViews.handleAction({ dataset: { action: 'reload-stale-draft' }, closest: s => (s === 'form' ? display : null) });
  await settleAll();
  assert.equal(t.st.renders, before, 'the reload render waits for the name save');
  assert.equal(notes[notes.length - 1], WAITING, 'and says so');
  assert.ok(!t.toasts.some(m => /Current values loaded/.test(m)), 'no claim before the values are loaded: ' + t.toasts.join(' | '));
  state.release.name(); await nameSave; await reload; await settleAll(40);
  assert.ok(t.st.renders > before, 'the page was re-read');
  const live = t.st.forms['settings-display'];
  assert.notEqual(live, display);
  assert.equal(live.field.value, 'comment', 'entries restored on the re-read form');
  assert.ok(t.toasts.includes('Current values loaded and your entries restored. Review, then save again.'), t.toasts.join(' | '));
});

/* -- Follow-up D: no raw internal ids outside Technical details; note status; rate-limited page reads -- */
const D_PID1 = 'exp:016083066067', D_PID2 = 'exp:5ba6bde24476', D_OCC = 'occ:5d1a3855b029', D_PIPE = 'pipe:obs:a00205124adf:r1';
const D_SR = 'sr1:1245f9be101b44779e1e9af0', D_SR2 = 'sr1:aa11bb22cc33dd44', D_SHOW = 'cs-1a2b3c4d5e6f';
// Raw internal ids a customer must never read outside a Technical details disclosure.
const D_RAW_ID = /\b(?:occ|pipe|sr1|exp|share|oidc|evidence):[A-Za-z0-9]|\bcs-[0-9a-f]{6,}/g;
// Only the folded Technical details disclosure is excluded; an open
// <details> (Source & provenance) is customer-visible text.
const dVisible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ');
const dLeaks = html => dVisible(html).match(D_RAW_ID) || [];
const dDraft = () => ({
  campaign: { id: 3, revision: 16, name: 'Lumen launch' }, mode: 'supplied_requirements',
  placements: [
    { id: D_PID1, scope: 'episode', show_id: D_SHOW, episode_id: 'e1', date_from: '2026-01-01', date_to: '2026-01-31', expected_count: 1, revision: 2,
      requirements: { message: 'Say lumen clearly' },
      provenance: { source_scope: { source_ref: D_SR, bounds: { start_s: 0, end_s: 2531.5 }, bounds_basis: 'full_effective_source' } } },
    { id: D_PID2, scope: 'show_window', show_id: D_SHOW, date_from: '2026-01-01', date_to: '2026-02-28', expected_count: 1, revision: 1, provenance: {} },
  ],
  targets: [], episode_titles: { e1: 'Morning brew' },
  allocation: {
    evaluations: {
      [D_PID1]: { outcome: 'supported', reasons: [], eligible_ids: [D_OCC], allocated_elsewhere: [] },
      [D_PID2]: { outcome: 'ambiguous', eligible_ids: [], allocated_elsewhere: [D_OCC], reasons: [
        `allocation competition: ['${D_OCC}'] already allocated; identity/evidence uncertain`,
        `contradicted by verified evidence: ['${D_PIPE}']`, `${D_OCC}: message: mismatch`, 'failed fetches: []', `${D_OCC}: strange new reason`] },
    },
    citations: {},
  },
  reason_codes: { evaluations: { [D_PID1]: [], [D_PID2]: [{ code: 'allocation_competition', field: null }, { code: 'contradicted', field: null },
    { code: 'requirement_mismatch', field: 'message' }, { code: 'no_failed_fetches', field: null }, { code: 'other', field: null }] }, citations: {} },
  coverage_by_placement: { [D_PID1]: { state: 'complete' }, [D_PID2]: { state: 'complete' } },
  comparisons: { [D_PID1]: [{ field: 'message', expected: 'Say lumen clearly', observed: 'Say lumen clearly', state: 'supported' }] },
  assessment_scope: { by_unit: { [D_PID1]: { entered_fields: ['message'], assessed_fields: ['message'], unknown_fields: [] },
    [D_PID2]: { entered_fields: [], assessed_fields: [], unknown_fields: [] } } },
  pins: { source_scopes: { [D_PID1]: { source_ref: D_SR, bounds: { start_s: 0, end_s: 2531.5 }, bounds_basis: 'full_effective_source' } },
    source_scope_state: { [D_PID1]: D_SR2 }, attachments: { [D_PID1]: [D_SR2] }, attachment_state: { [D_SR2]: { evidence_revision: 3 } } },
  attachment_pool: ['ev-9'], pool_refs: { 'ev-9': 'sr1:99887766554433' }, suggestions: ['ev-9'],
});
const dTarget = { id: 'ct-52bc50b9d74f4b98', scope: 'episode', show_id: D_SHOW, episode_id: 'e1', revision: 1,
  source_scope: { source_ref: D_SR, bounds: { start_s: 0, end_s: 2531.5 }, bounds_basis: 'full_effective_source' } };
const dShares = [{ id: 'share:512cd1edf2e0', object_type: 'evidence', object_id: 'fixture-ad', object_title: 'Morning brew',
  audience: 'oidc:https://i.example:u-guest-visual', audience_email: '', expires_at: '2099-01-01T00:00:00Z', revoked: 0, revision: 1 }];
const dMembers = [{ member: 'oidc:https://i.example:u-viewer', role: 'viewer', revoked: 0, email: '' },
  { member: 'oidc:https://i.example:u-owner', role: 'owner', revoked: 0, email: 'owner@example.com' }];
// One realistic customer workspace, routed by request path.
const dRoutes = () => {
  const ok = body => json({ ok: true, workspace_context: 'ctx-a', ...body });
  const draft = dDraft();
  const frozen = { captured_at: '2026-10-01T10:00:00Z', campaign_revision: 16, placements: draft.placements, targets: [],
    evaluations: draft.allocation.evaluations, allocation: { citations: {} }, coverage_by_placement: draft.coverage_by_placement,
    comparisons: draft.comparisons, assessment_scope: draft.assessment_scope, episode_titles: draft.episode_titles,
    brief_versions: { campaign: { brief_id: 'brief-4f42641762de4eec', version: 1, digest: 'abc123', provided_text: 'Say lumen clearly.' },
      [D_PID2]: { brief_id: 'brief-other', version: 2, digest: 'def456' } } };
  const record = { record_kind: 'evidence', ref: D_SR, canonical_source_ref: D_SR2, status: 'finding',
    window: { segments: [{ text: 'Say lumen clearly.' }], text_complete: true },
    evidence: { episode_id: 'e1', orig_start_s: 10, orig_end_s: 20 }, episode: { ep_title: 'Morning brew', show_title: 'Morning Show', pub_date: '2026-01-05' }, media: {} };
  const table = {
    '/api/v1/customer/campaigns/3': { campaign: { id: 3, mode: 'supplied_requirements', revision: 16, name: 'Lumen launch',
      requirement_provenance: { brief_id: 'brief-4f42641762de4eec', version: 1, source_label: 'Lumen brief' } } },
    '/api/v1/customer/campaigns/3/draft': { draft },
    '/api/v1/customer/campaigns/3/reports': { reports: [{ report_no: 1, captured_at: '2026-10-01T10:00:00Z', stale: false }] },
    '/api/v1/customer/campaigns/3/reports/1': { frozen, stale: false },
    '/api/v1/customer/briefs/brief-4f42641762de4eec/versions/1': { brief_version: { provided_text: 'Say lumen clearly.' } },
    '/api/v1/customer/campaigns/2': { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study' } },
    '/api/v1/customer/campaigns/2/draft': { draft: { campaign: { id: 2, revision: 3 }, mode: 'research', placements: [], targets: [dTarget],
      allocation: {}, coverage_by_placement: {}, episode_titles: { e1: 'Morning brew' } } },
    '/api/v1/customer/campaigns/2/reports': { reports: [] },
    '/api/v1/customer/campaigns/2/targets': { targets: [dTarget] },
    ['/api/v1/customer/shows/' + D_SHOW]: { display_title: 'Morning Show', episodes: [{ id: 'e1', ep_title: 'Morning brew' }] },
    ['/api/v1/customer/records/' + encodeURIComponent(D_SR)]: { record },
    ['/api/v1/customer/records/' + encodeURIComponent(D_SR) + '/mistake']: { mistakes: [{ id: 5, revision: 3, reason: 'F15 first note', ref: D_SR }] },
    '/api/v1/customer/mistakes/5': { note: { id: 5, revision: 3, reason: 'F15 first note', ref: D_SR, delivery_context: 'received' } },
    '/api/v1/customer/brands/lumen': { brand: { brand: 'lumen', display: 'Lumen', detections: 2, confirmed_occurrences: [D_SR], findings: [],
      offer_sequence: [{ signature: '9f8e7d6c5b4a3a2b', refs: [D_SR] }] } },
    '/api/v1/customer/creatives/cr1': { creative: { id: 'cr1', members: [{ observation_id: D_PIPE, confirmed: true, evidence_refs: [{ ref: D_SR, excerpt: 'Say lumen clearly.' }] }] } },
    '/api/v1/workspaces/ws-a/members': { members: dMembers },
    '/api/v1/workspaces/ws-a/invites': { invites: [{ id: 'inv-1', member: 'oidc:https://i.example:u-invited', role: 'viewer', delivery: 'not_queued' }] },
    '/api/v1/shares': { shares: dShares },
    '/api/v1/billing/status': { configured: false },
    '/api/v1/customer/preferences': { preferences: { workspace_name: 'Alpha', revision: 1, role_matrix: { viewer: 'read' } } },
  };
  return async (url) => {
    const [p, q] = url.split('?');
    if (p === '/api/v1/customer/mistakes' && q) return ok({ notes: [{ id: 5, reason: 'F15 first note', ref: D_SR, delivery_context: 'received' }], total: 1, total_relation: 'exact', next_cursor: null });
    return Object.hasOwn(table, p) ? ok(table[p]) : null;
  };
};
const dRender = async (t, route) => {
  t.win.location.hash = route; t.win.EW.current.route = route;
  await t.win.EWViews.render(); await settleAll(40);
  return t.main.innerHTML;
};

test('D1 the main pages, rendered from realistic data, show no raw internal id outside Technical details', async () => {
  const t = await uxWin(dRoutes(), '#/settings');
  const pages = ['#/settings', '#/campaign/3', '#/campaign/3/placement/' + encodeURIComponent(D_PID1),
    '#/campaign/3/placement/' + encodeURIComponent(D_PID2), '#/campaign/3/report/1', '#/campaign/2',
    '#/campaign/2/target/ct-52bc50b9d74f4b98', '#/evidence/' + encodeURIComponent(D_SR), '#/note/5', '#/saved?tab=notes',
    '#/brand/lumen', '#/creative/cr1'];
  for (const route of pages) {
    const html = await dRender(t, route);
    assert.match(html, /<h1/, route + ' rendered its page');
    assert.doesNotMatch(html, /notice error/, route + ' rendered without a read failure');
    assert.deepEqual(dLeaks(html), [], route + ' shows no raw id outside Technical details');
  }
  // The ids are still on the page for support, folded away.
  const placement = await dRender(t, '#/campaign/3/placement/' + encodeURIComponent(D_PID1));
  assert.match(placement, /<details class="tech-details">[\s\S]*sr1:1245f9be101b44779e1e9af0[\s\S]*<\/details>/);
  assert.match(dVisible(placement), /The whole episode as published when this was saved from 0:00\.0 to 42:11\.5\./);
  const report = await dRender(t, '#/campaign/3/report/1');
  assert.match(dVisible(report), /Episode: Morning brew · Jan 1–Jan 31, 2026\s+Ad found\s+Coverage complete\s+Checked against the brief: Message\./, 'the scope facts sit inside that placement card');
  assert.match(dVisible(report), /No checks entered for this placement\. The brief text is not read automatically; add Message, Offer, Code or Length to check them\./);
});

test('D2 Settings names people readably and shares in plain words; the Remove prompt never shows the raw principal', async () => {
  // JE-SETTINGS: Remove is the owner's; a member name follows the rule
  // (display, else the email's local part, else "Member"), never the id.
  const t = await uxWin(dRoutes(), '#/settings', { role: 'owner' });
  const html = await dRender(t, '#/settings');
  const text = dVisible(html);
  assert.match(text, /Member · Viewer/, 'Member when neither a display name nor an email is known');
  assert.match(text, /owner · Owner/, 'the email\'s local part when the server gives one');
  assert.doesNotMatch(text, /u-viewer · Viewer/, 'never the subject');
  assert.match(text, /u-invited · Viewer/, 'pending invitations too');
  assert.match(text, /Evidence page · Morning brew · shared with u-guest-visual · Active/);
  assert.doesNotMatch(text, /oidc:|share:|fixture-ad/);
  assert.match(html, /<details class="tech-details"><summary>Technical details<\/summary><p class="small muted">Share <span class="mono">share:512cd1edf2e0<\/span> · evidence <span class="mono">fixture-ad<\/span>/);
  assert.match(html, /Member <span class="mono">oidc:https:\/\/i\.example:u-viewer<\/span>/);
  const button = /data-action="member-remove" data-member="([^"]*)" data-name="([^"]*)"/.exec(html);
  assert.ok(button, 'the Remove button carries the readable name');
  await t.win.EWViews.handleAction({ dataset: { action: 'member-remove', member: button[1], name: button[2] } }); await settleAll();
  assert.match(t.modal.innerHTML, /Remove Member from this workspace\?/);
  assert.deepEqual(dLeaks(t.modal.innerHTML), [], 'the prompt shows no raw principal');
  assert.match(t.modal.innerHTML, /name="confirm_id" value="oidc:https:\/\/i\.example:u-viewer"/, 'the removal still targets the exact principal');
  assert.equal(t.win.EW.personName('pat@example.com'), 'pat@example.com');
  assert.equal(t.win.EW.personName('Pat: marketing'), 'Pat: marketing', 'typed names are never cut');
  assert.equal(t.win.EW.personName('oidc:https://i.example:8443:u-1', ''), 'u-1');
  assert.deepEqual(t.writes(), [], 'nothing was written');
});

test('D3 campaign reasons read as plain copy by stable code; raw reasons and ids sit in Technical details', async () => {
  const t = await uxWin(dRoutes(), '#/campaign/3');
  const html = await dRender(t, '#/campaign/3');
  const text = dVisible(html);
  assert.match(text, /Another placement already claims this occurrence, and the evidence can(?:'|&#39;)t tell them apart\./);
  assert.match(text, /Verified evidence contradicts this match\./);
  assert.match(text, /Doesn(?:'|&#39;)t match the brief: message\./);
  assert.match(text, /Another condition affects this result\. The exact reason is in Technical details\./, 'an unknown code reads generically');
  assert.doesNotMatch(text, /failed fetches|Some sources in this window couldn/, 'an empty-list code adds no line');
  assert.match(text, /1 matching occurrence can count here\./);
  assert.match(text, /1 matching occurrence already counts for another placement\./);
  assert.match(html, /<details class="tech-details">[\s\S]*allocation competition: \[&#39;occ:5d1a3855b029&#39;\][\s\S]*<\/details>/);
  assert.deepEqual(dLeaks(html), []);
});

test('D3b without server reason codes, prose is never parsed: every reason reads generically, raw text in details', async () => {
  const routes = dRoutes();
  const t = await uxWin(async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/draft') { const draft = dDraft(); delete draft.reason_codes; return json({ ok: true, workspace_context: 'ctx-a', draft }); }
    return routes(url, opts);
  }, '#/campaign/3');
  const html = await dRender(t, '#/campaign/3');
  const text = dVisible(html);
  assert.equal((text.match(/Another condition affects this result\./g) || []).length, 1, 'one generic line, deduplicated');
  assert.doesNotMatch(text, /Verified evidence contradicts/, 'no copy guessed from prose');
  assert.match(html, /<details class="tech-details">[\s\S]*contradicted by verified evidence[\s\S]*<\/details>/);
  assert.deepEqual(dLeaks(html), []);
});

test('D4 editing a note whose stored record has no status shows the same "open" fallback as the evidence page', async () => {
  const t = await uxWin(dRoutes(), '#/evidence/' + encodeURIComponent(D_SR));
  const page = await dRender(t, '#/evidence/' + encodeURIComponent(D_SR));
  // PLAIN-ADS-FIX4 (screen R4-05): the page's note line names no state;
  // the edit form keeps the "open" fallback.
  assert.match(page, /Your note: “/);
  assert.doesNotMatch(page, /Your note \(/);
  await t.win.EWViews.handleAction({ dataset: { action: 'report-mistake', id: D_SR } }); await settleAll();
  assert.match(t.modal.innerHTML, /Current note: open · F15 first note/);
  assert.doesNotMatch(t.modal.innerHTML, /Current note: {2}·|Current note: ·/, 'never an empty status');
  assert.match(t.modal.innerHTML, /name="mistake_id" value="5"/);
  assert.match(t.modal.innerHTML, /<option value="open">Open<\/option>/);
});

// OVERVIEW-429: a read retries a 429 itself when Retry-After fits its 3 s
// budget; these page-level checks use longer waits so the read gives up at once.
const dLimited = (retryAfter) => {
  const headers = { get: k => (String(k).toLowerCase() === 'retry-after' && retryAfter !== undefined ? String(retryAfter) : null) };
  return { ok: false, status: 429, headers, text: async () => JSON.stringify({ error: { code: 'RATE_LIMITED', message: 'slow down and retry', status: 429, details: {} } }) };
};

test('D5 a rate-limited page read renders at once with its heading, plain copy and Retry; Retry-After retries once, bounded', async () => {
  const routes = dRoutes();
  let limit = 1, drafts = 0, retryAfter = 5;
  const t = await uxWin(async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/draft') { drafts++; if (limit > 0) { limit--; return dLimited(retryAfter); } }
    return routes(url, opts);
  }, '#/campaign/3');
  const clock = fakeRequestTimers(t.win);
  const html = await dRender(t, '#/campaign/3');
  assert.match(html, /<h1>Campaign<\/h1>/, 'the page heading renders with the failure');
  assert.match(html, /Too many requests right now\. Wait a moment, then try again\./);
  assert.doesNotMatch(html, /slow down/, 'never the server wording');
  assert.match(html, /data-action="retry"/);
  assert.match(html, /Trying again automatically in 5 seconds\./);
  const retries = clock.pending().filter(h => h.ms === 5000);
  assert.equal(retries.length, 1, 'one automatic retry after Retry-After');
  assert.equal(drafts, 1);
  retries[0].cleared = true; retries[0].fn(); await settleAll(40);
  assert.equal(drafts, 2, 'the retry re-read the page once');
  assert.match(t.main.innerHTML, /<h1>Lumen launch<\/h1>/, 'and published it');
  // Limited again after its automatic retry: no second automatic retry.
  limit = 2;
  await dRender(t, '#/campaign/3');
  const auto = clock.pending().filter(h => h.ms === 5000);
  assert.equal(auto.length, 1); auto[0].cleared = true; auto[0].fn(); await settleAll(40);
  assert.equal(drafts, 4);
  assert.match(t.main.innerHTML, /Too many requests right now/);
  assert.doesNotMatch(t.main.innerHTML, /Trying again automatically/, 'one automatic retry per page, then Retry only');
  assert.equal(clock.pending().filter(h => h.ms === 5000).length, 0);
  // A Retry-After beyond 10 s, or none at all, never retries on its own.
  for (const ra of [30, undefined]) {
    limit = 1; retryAfter = ra; t.win.EW.current.autoRetriedRoute = null;
    const before = clock.timers.length;
    const page = await dRender(t, '#/campaign/3');
    assert.match(page, /<h1>Campaign<\/h1>[\s\S]*Too many requests right now/);
    assert.doesNotMatch(page, /Trying again automatically/);
    assert.deepEqual(clock.timers.slice(before).filter(h => !h.cleared && h.ms !== 20000).map(h => h.ms), [], String(ra));
  }
  // A retry that is no longer current (the user navigated) does nothing.
  limit = 1; retryAfter = 4; t.win.EW.current.autoRetriedRoute = null;
  await dRender(t, '#/campaign/3');
  const stale = clock.pending().find(h => h.ms === 4000);
  await dRender(t, '#/campaign/2');
  const count = drafts; stale.fn(); await settleAll(40);
  assert.equal(drafts, count, 'a superseded automatic retry never re-reads');
  assert.match(t.main.innerHTML, /<h1>Field study<\/h1>/);
});

test('D6 a rate-limited write is reported, never retried on its own', async () => {
  let posts = 0;
  const t = await uxWin(async (url, opts) => {
    if (opts.method === 'POST' && url.endsWith('/mistake')) { posts++; return dLimited(1); }
    return null;
  }, '#/evidence/' + encodeURIComponent(D_SR));
  const clock = fakeRequestTimers(t.win);
  t.win.EW.render = () => {};
  t.win.EW.openDialog('Add personal note', '', '', '');
  const form = reviewerForm('mistake');
  form.closest = s => (s === '#modal' ? t.modal : null);
  t.win.FormData = class { *entries() { yield ['ref', D_SR]; yield ['reason', 'Wrong brand']; yield ['status', 'open']; } };
  await t.win.EWViews.handleSubmit(form); await settleAll(40);
  assert.equal(posts, 1);
  assert.equal(t.win.EW.current.rateLimitedRead, undefined, 'a write never arms a page retry');
  for (const h of clock.pending()) { if (h.ms !== 30000) { h.cleared = true; h.fn(); } }
  await settleAll(40);
  assert.equal(posts, 1, 'never retried');
  assert.ok(t.toasts.concat(form.box ? [form.box.innerHTML] : []).some(m => /Too many requests right now/.test(m)), t.toasts.join(' | '));
});

test('D8 the automatic retry of a rate-limited read never re-renders over a held unconfirmed entry', async () => {
  let invReads = 0, limited = true;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') { invReads++; if (limited) { limited = false; return dLimited(4); } return json({ ok: true, invites: [] }); }
    if (opts.method === 'POST' && url.endsWith('/invites')) return new Promise(() => {});
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  const clock = fakeRequestTimers(t.win);
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll(40);
  assert.match(t.root.innerHTML, /Trying again automatically in 4 seconds\./);
  const auto = clock.pending().filter(h => h.ms === 4000);
  assert.equal(auto.length, 1);
  // An invite whose save never answers is held as unconfirmed.
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  const form = reviewerForm('invite-create', { entries: true });
  const saving = t.win.EWViews.handleSubmit(form); await settleAll();
  clock.pending().filter(h => h.ms === 30000)[0].fn(); await saving; await settleAll();
  assert.equal(form.dataset.held, '1');
  t.win.document.querySelectorAll = sel => (/data-held|data-dirty/.test(sel) ? [form] : []);
  const token = t.win.EW.current.fetchToken, reads = invReads;
  const note = { textContent: 'Trying again automatically in 4 seconds.' };
  const find = t.root.querySelector;
  t.root.querySelector = sel => (sel === '[data-auto-retry]' ? note : find(sel));
  auto[0].cleared = true; auto[0].fn(); await settleAll(40);
  assert.equal(t.win.EW.current.fetchToken, token, 'no render over the held entry');
  assert.equal(invReads, reads, 'the page is not re-read');
  assert.equal(form.dataset.held, '1', 'the entry stays held with its notice');
  assert.match(note.textContent, /^Not trying again automatically while an unconfirmed entry is on this page\. Use Retry when ready\.$/, 'the note stops promising a retry');
});

test('D8b Retry over a held unconfirmed page entry says it was dropped, never replays it; a held dialog stays, plain Retry is silent', async () => {
  let invReads = 0, limited = true, posts = 0;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') { invReads++; if (limited) { limited = false; return dLimited(4); } return json({ ok: true, invites: [] }); }
    if (opts.method === 'POST' && url.endsWith('/invites')) { posts++; return new Promise(() => {}); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  const clock = fakeRequestTimers(t.win);
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll(40);
  const auto = clock.pending().filter(h => h.ms === 4000);
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  const form = reviewerForm('invite-create', { entries: true });
  const saving = t.win.EWViews.handleSubmit(form); await settleAll();
  clock.pending().filter(h => h.ms === 30000)[0].fn(); await saving; await settleAll();
  assert.equal(form.dataset.held, '1');
  t.win.document.querySelectorAll = sel => (/data-held|data-dirty/.test(sel) ? [form] : []);
  auto[0].cleared = true; auto[0].fn(); await settleAll(40);
  // Held in an open dialog: the render keeps the dialog, so Retry
  // re-reads the page and leaves the entry and its notice alone.
  t.modal.open = true; form.closest = s => (s === '#modal' ? t.modal : null);
  let reads = invReads;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
  assert.equal(invReads, reads + 1, 'Retry re-reads the page');
  assert.equal(form.dataset.held, '1');
  assert.deepEqual(t.toasts, [], 'nothing dropped, nothing said');
  // Held on the page: the re-read replaces it, so its loss is said.
  t.modal.open = false; form.closest = () => null;
  reads = invReads;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
  t.win.document.querySelectorAll = () => [];
  assert.equal(invReads, reads + 1, 'Retry re-reads the page');
  assert.equal(posts, 1, 'the held write is never replayed');
  assert.ok(t.toasts.some(m => /could not be confirmed\. The page now shows what is saved\./.test(m)), t.toasts.join(' | ') || 'the held entry was dropped silently');
  assert.match(t.root.top(), /data-outcome-notice[\s\S]*could not be confirmed\. The page now shows what is saved\./, 'the durable notice says so too');
  // Plain Retry, nothing held: re-reads, says nothing new.
  const toasts = t.toasts.length;
  reads = invReads;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
  assert.equal(invReads, reads + 1);
  assert.equal(t.toasts.length, toasts);
});

test('D8c Retry deferred behind another page save is skipped over a held entry: it says the page was not reloaded, never that the entry was dropped', async () => {
  const state = revState();
  const t = await settingsForms(revServer(state));
  const { saving } = await displayInFlight(t, state);
  // The name form holds an unconfirmed entry on the page.
  const name = t.st.forms['settings-workspace'];
  name.field.value = 'Typed name';
  Object.assign(name.dataset, { dirty: '1', held: '1', unconfirmed: '1' });
  name.box.innerHTML = 'Your change could not be confirmed. Reload the workspace to continue.';
  const notes = []; t.win.EW.saveNotice = (m) => notes.push(m);
  const before = t.st.renders;
  const retry = t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll();
  assert.ok(notes.includes(WAITING), 'the render waits for the other save');
  assert.deepEqual(t.toasts, [], 'nothing said while it waits');
  assert.doesNotMatch(t.root.top(), /data-outcome-notice/);
  state.release.display(); await saving; await retry; await settleAll(40);
  assert.equal(t.st.renders, before, 'the deferred render was skipped over the held entry');
  assert.equal(t.st.forms['settings-workspace'], name);
  assert.equal(name.dataset.held, '1', 'the entry stays held');
  assert.equal(name.field.value, 'Typed name');
  assert.match(name.box.innerHTML, /could not be confirmed\. Reload the workspace to continue\./, 'its notice stays');
  assert.ok(!t.toasts.some(m => /could not be confirmed|was not applied/.test(m)), t.toasts.join(' | '));
  assert.ok(t.toasts.includes('The page was not reloaded because an entry on it is on hold. Use the action in its notice to continue.'), t.toasts.join(' | ') || 'the skipped Retry said nothing');
  assert.doesNotMatch(t.root.top(), /data-outcome-notice/, 'no notice claims the entry was dropped');
});

test('D8d Retry over a held unconfirmed page entry whose re-read fails again (429, 500) never says the page shows what is saved', async () => {
  for (const reread of ['429', '500', 'ok']) {
    let invReads = 0, posts = 0;
    const t = await uiWin(async (url, opts = {}) => {
      if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
      if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') {
        invReads++;
        // First read limited; the Retry re-read fails again or loads.
        if (invReads === 1 || (invReads === 2 && reread === '429')) return dLimited(4);
        if (invReads === 2 && reread === '500') return json({ ok: false, error: { code: 'INTERNAL', message: 'boom' } }, false, 500);
        return json({ ok: true, invites: [] });
      }
      if (opts.method === 'POST' && url.endsWith('/invites')) { posts++; return new Promise(() => {}); }
      return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
    });
    const clock = fakeRequestTimers(t.win);
    t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
    await t.win.EWViews.render(); await settleAll(40);
    t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
    const form = reviewerForm('invite-create', { entries: true });
    form.closest = () => null;
    const saving = t.win.EWViews.handleSubmit(form); await settleAll();
    clock.pending().filter(h => h.ms === 30000)[0].fn(); await saving; await settleAll();
    assert.equal(form.dataset.held, '1', reread);
    assert.equal(form.dataset.unconfirmed, '1', reread);
    t.win.document.querySelectorAll = sel => (/data-held|data-dirty/.test(sel) ? [form] : []);
    await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
    t.win.document.querySelectorAll = () => [];
    assert.equal(invReads, 2, reread + ': Retry re-read the page once');
    assert.equal(posts, 1, reread + ': the held write is never replayed');
    const said = t.toasts.join(' | ');
    if (reread === 'ok') {
      assert.match(said, /could not be confirmed\. The page now shows what is saved\./, said);
      assert.match(t.root.top(), /data-outcome-notice[\s\S]*could not be confirmed\. The page now shows what is saved\./);
      continue;
    }
    assert.match(t.root.innerHTML, /data-action="retry"/, reread + ': the failed re-read offers Retry');
    assert.doesNotMatch(said, /The page now shows what is saved/, reread + ': ' + said);
    assert.ok(t.toasts.some(m => /^Your change in .+ could not be confirmed, and the page could not be reloaded\.$/.test(m)), reread + ': ' + (said || 'the held entry was dropped silently'));
    assert.doesNotMatch(t.root.top(), /The page now shows what is saved/, reread + ': the durable notice never claims it');
    assert.match(t.root.top(), /data-outcome-notice[\s\S]*could not be confirmed, and the page could not be reloaded\./, reread + ': the durable notice says so too');
  }
});

test('D8e Retry over a held unconfirmed page entry whose re-read never publishes (superseded) says the page could not be reloaded', async () => {
  let invReads = 0, posts = 0, superseded = null;
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') return json(hostedSession({ workspace_names: WS_NAMES }));
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') {
      invReads++;
      // The Retry re-read is superseded by a newer render that never lands.
      if (invReads === 2) { superseded = t.win.EWViews.render(); return json({ ok: true, invites: [] }); }
      if (invReads > 2) return new Promise(() => {});
      return json({ ok: true, invites: [] });
    }
    if (opts.method === 'POST' && url.endsWith('/invites')) { posts++; return new Promise(() => {}); }
    return json({ ok: true, workspace_context: 'ctx-a', preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  const clock = fakeRequestTimers(t.win);
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll(40);
  assert.doesNotMatch(t.root.innerHTML, /data-action="retry"/, 'the page loaded whole');
  t.win.FormData = class { *entries() { yield ['member', 'pat@example.com']; yield ['role', 'viewer']; } };
  const form = reviewerForm('invite-create', { entries: true });
  const saving = t.win.EWViews.handleSubmit(form); await settleAll();
  clock.pending().filter(h => h.ms === 30000)[0].fn(); await saving; await settleAll();
  assert.equal(form.dataset.held, '1');
  t.win.document.querySelectorAll = sel => (/data-held|data-dirty/.test(sel) ? [form] : []);
  const published = t.win.EW.current.publishedToken;
  await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
  t.win.document.querySelectorAll = () => [];
  assert.ok(superseded, 'a newer render superseded the Retry re-read');
  assert.equal(t.win.EW.current.publishedToken, published, 'no render published');
  assert.equal(posts, 1, 'the held write is never replayed');
  const said = t.toasts.join(' | ');
  assert.doesNotMatch(said, /The page now shows what is saved/, said);
  assert.ok(t.toasts.some(m => /^Your change in .+ could not be confirmed, and the page could not be reloaded\.$/.test(m)), said || 'the held entry was dropped silently');
  assert.doesNotMatch(t.root.top(), /The page now shows what is saved/, 'the durable notice never claims it');
});

test('D8f Retry over a held page entry: a partial names notice is not a failed read; a failed page read still is', async () => {
  for (const reread of ['names', 'page']) {
    const t = await uiWin(async (url) => (url === '/api/v1/session' ? json(hostedSession({ workspace_names: WS_NAMES })) : json({ ok: true, workspace_context: 'ctx-a' })));
    const bundle = { query: { version: 2, mode: 'ads', q: 'kept', brands: ['acme'], sort: 'newest', page: 1 }, revision: 'r1',
      result: { items: [], total: 0, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
    let pageDown = false;
    t.win.EW.api = async (p) => {
      if (pageDown || p === '/customer/brands' || p === '/customer/shows') throw Object.assign(new Error('boom'), { status: 500 });
      return bundle;
    };
    t.win.location.hash = '#/search?mode=ads&q=kept&brands=acme'; t.win.EW.current.route = t.win.location.hash;
    await t.win.EWViews.render(); await settleAll(40);
    assert.match(t.root.innerHTML, /so its ID is shown\./, reread);
    assert.equal((t.root.innerHTML.match(/data-action="retry"/g) || []).length, 1, reread + ': its only Retry is the names notice');
    const form = reviewerForm('note-edit', { entries: true });
    Object.assign(form.dataset, { held: '1', unconfirmed: '1' });
    t.win.document.querySelectorAll = sel => (/data-held|data-dirty/.test(sel) ? [form] : []);
    pageDown = reread === 'page';
    await t.win.EWViews.handleAction({ dataset: { action: 'retry' } }); await settleAll(40);
    t.win.document.querySelectorAll = () => [];
    const said = t.toasts.join(' | ');
    if (reread === 'names') {
      assert.match(t.root.innerHTML, /so its ID is shown\./, 'names still fall back to IDs');
      assert.ok(t.toasts.some(m => /could not be confirmed\. The page now shows what is saved\.$/.test(m)), said || 'the held entry was dropped silently');
      continue;
    }
    assert.match(t.root.innerHTML, /Search is unavailable/, 'the re-read failed');
    assert.doesNotMatch(said, /The page now shows what is saved/, said);
    assert.ok(t.toasts.some(m => /could not be confirmed, and the page could not be reloaded\.$/.test(m)), said || 'the held entry was dropped silently');
  }
});

test('D7 outcome, linkage and state labels read as plain words; raw tokens and server wording stay in Technical details', async () => {
  const routes = dRoutes();
  const SNAKE = /\b[a-z]+_[a-z_]+\b/g;
  const t = await uxWin(async (url, opts) => {
    if (url === '/api/v1/customer/campaigns/3/reports/1') {
      const draft = dDraft();
      return json({ ok: true, workspace_context: 'ctx-a', stale: true, stale_reasons: ['allocation', 'attachments:' + D_SR2, 'coverage_sources'],
        frozen: { captured_at: '2026-10-01T10:00:00Z', placements: draft.placements, targets: [], evaluations: { [D_PID1]: { outcome: 'no_observation_in_covered_sources', reasons: [] } },
          allocation: { citations: {} }, coverage_by_placement: draft.coverage_by_placement, episode_titles: draft.episode_titles,
          comparisons: { [D_PID1]: [{ field: 'message', expected: 'Say lumen', observed: 'Say lumen', state: 'unknown', reason: 'no copy-field verification recorded; observed values stay unknown' },
            { field: '(attachment)', expected: null, observed: null, state: 'unknown', reason: `${D_SR2}: attachment_unreadable` }] } } });
    }
    if (url === '/api/v1/customer/campaigns/3/reports') return json({ ok: true, workspace_context: 'ctx-a', reports: [{ report_no: 1, captured_at: '2026-10-01T10:00:00Z', stale: true, stale_reasons: ['brief', 'attachments:' + D_SR2] }] });
    if (url === '/api/v1/billing/status') return json({ ok: true, workspace_context: 'ctx-a', configured: true, entitlement: { state: 'past_due', allowed: true } });
    return routes(url, opts);
  }, '#/campaign/3/report/1');
  const report = await dRender(t, '#/campaign/3/report/1');
  const text = dVisible(report);
  assert.match(text, /Requirement outcome: No match found in the sources we checked\./);
  assert.match(text, /This report is stale:\s*which sources count where, attached sources, coverage changed since capture/);
  assert.match(text, /Attached source/);
  assert.match(text, /This couldn(?:'|&#39;)t be verified from the stored record\./);
  assert.match(report, /<details class="tech-details">[\s\S]*no copy-field verification recorded[\s\S]*<\/details>/, 'server wording kept in details');
  assert.deepEqual(text.match(SNAKE) || [], [], 'report: no snake_case token outside details');
  assert.deepEqual(dLeaks(report), []);
  for (const route of ['#/campaign/3', '#/campaign/3/placement/' + encodeURIComponent(D_PID2), '#/evidence/' + encodeURIComponent(D_SR), '#/settings']) {
    const html = await dRender(t, route);
    assert.deepEqual(dVisible(html).match(SNAKE) || [], [], route + ': no snake_case token outside details');
  }
  const camp = dVisible(await dRender(t, '#/campaign/3'));
  assert.match(camp, /Stale: the brief, attached sources changed/);
  assert.match(camp, /Requirement outcome: Supported by the stored evidence\./);
  assert.match(dVisible(await dRender(t, '#/settings')), /Status: Payment overdue\./);
  const evPage = await dRender(t, '#/evidence/' + encodeURIComponent(D_SR));
  assert.doesNotMatch(dVisible(evPage), /Relationship/);
  assert.match(evPage, /<details class="tech-details">[\s\S]*Version: Current version/);
});

/* -- A6: pages show what we know (validation plan A6; gaps 7, 8, 11, 12) -- */
async function a6Render(route, api) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const paths = [];
  win.EW.api = async (path, opts) => { paths.push(path); return api(path, opts); };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  return { html: main.innerHTML, paths, win };
}
const A6_SR = 'sr1:a6a6a6a6a6a6a6a6a6a6a6a6a6a6a6a6';
const a6Read = (over = {}) => ({ kind: 'named', source_label: 'Named read',
  brand: { key: 'simplisafe', display: 'SimpliSafe', display_basis: 'brand_list', suggested: 'simply safe', confirmed: false },
  needs_review: true, self_promo: false, self_promo_reason: null, confidence: 'low', confidence_score: 0.31,
  display_span: [62, 118.5], has_clip: false, candidates: [{ brand: 'simply safe', score: 2 }, { brand: 'ring', score: 1 }], ...over });

test('A6 search cards show the brand display spelling, time range and a needs-review badge', async () => {
  const base = { record_kind: 'evidence', status: 'finding', ep_title: 'Episode', show_title: 'Show',
    pub_date: '2026-10-02', text: 'passage', audio: 'none', quotation_span: [60, 120] };
  const items = [
    { ...base, ref: A6_SR + ':1', read: a6Read() },
    { ...base, ref: A6_SR + ':2', read: a6Read({ brand: { key: 'ag1', display: 'AG1', display_basis: 'brand_list', confirmed: true }, display_span: [180, 240] }) },
    { ...base, ref: A6_SR + ':3', quotation_span: [300, 330] },
  ];
  const bundle = { query: { version: 2, mode: 'ads', q: '', sort: 'newest', page: 1 }, revision: 'r',
    result: { items, total: 3, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } };
  const { html } = await a6Render('#/search?mode=ads', path => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') return bundle;
    throw new Error('unexpected ' + path);
  });
  assert.match(html, /SimpliSafe/);
  assert.match(html, /1:02–1:59/, 'display span of the read, whole seconds');
  assert.match(html, /AG1/);
  assert.match(html, /3:00–4:00/);
  assert.match(html, /5:00–5:30/, 'a record without a read block shows its quotation span');
  // PLAIN-ADS (DEP7): no needs-review badge on a card or the inspector.
  assert.doesNotMatch(html, /Needs review/);
});

test('A6 evidence page separates words, says no review state and shows the read facts', async () => {
  const ref = A6_SR + ':w:60.0:120.0';
  const record = { ref, record_kind: 'evidence', status: 'finding', review: { judgment: null, revision: 0 },
    attribution: [{ brand: 'simply safe', basis: 'suggested' }], relationship: 'current', availability: 'current',
    bounds_basis: 'source bounds', selected_bounds: { start_s: 60, end_s: 120 },
    episode: { show_title: 'Show', ep_title: 'Episode', pub_date: '2026-10-02' }, evidence: { episode_id: 'ep_fx2' }, media: {},
    read: a6Read(),
    window: { segments: [
      { segment_id: 1, start_s: 60, end_s: 62, text: 'Quince has', text_start: 0, text_end: 10 },
      { segment_id: 2, start_s: 62, end_s: 64, text: 'everything', text_start: 0, text_end: 10 },
      { segment_id: 2, start_s: 62, end_s: 64, text: 'thing', text_start: 10, text_end: 15 },
      { segment_id: 3, start_s: 64, end_s: 66, text: ' at home.', text_start: 0, text_end: 9 }],
    text_complete: true, next_cursor: null } };
  const { html } = await a6Render('#/evidence/' + encodeURIComponent(ref), path => {
    if (path === '/customer/records/' + encodeURIComponent(ref)) return { record };
    if (path.endsWith('/mistake')) return { mistakes: [] };
    throw new Error('unexpected ' + path);
  });
  assert.match(html, /<p>Quince has<\/p>[\s\S]*<p>everythingthing<\/p>[\s\S]*<p> at home\.<\/p>/, 'one line per segment, so words never run together; a continuation chunk is not split');
  assert.doesNotMatch(html, /hasevery/);
  assert.doesNotMatch(html, /object Object/i);
  assert.doesNotMatch(html, /<dt>Review<\/dt>|Needs review/);
  assert.match(html, /SimpliSafe/);
  assert.match(html, /Found automatically/);
  assert.doesNotMatch(html, /\bring\b/, 'other brand candidates are for the operator only');
});

test('A6 a historical evidence page points to the current read', async () => {
  const ref = A6_SR + ':w:60.0:120.0';
  const record = { ref, record_kind: 'evidence', status: 'finding', review: { judgment: 'ad', revision: 2 },
    attribution: [], relationship: 'historical', historical: true, availability: 'current',
    episode: { show_title: 'Show', ep_title: 'Episode', pub_date: '2026-10-02' }, evidence: {}, media: {},
    read: { historical: true, current_ref: 'sr1:current' },
    window: { segments: [], text_complete: true, next_cursor: null } };
  const { html } = await a6Render('#/evidence/' + encodeURIComponent(ref), path => {
    if (path === '/customer/records/' + encodeURIComponent(ref)) return { record };
    if (path.endsWith('/mistake')) return { mistakes: [] };
    throw new Error('unexpected ' + path);
  });
  assert.doesNotMatch(html, /<dt>Review<\/dt>/);
  assert.match(html, /href="#\/evidence\/sr1%3Acurrent"/);
});

const A6_EP_SR = 'sr1:e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6';
const a6EpisodeRecord = (segments, next = null) => ({ ref: 'ep:ep_fx1', source_ref: A6_EP_SR, canonical_source_ref: A6_EP_SR,
  record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_fx1', id: 'ep_fx1', transcript_version_id: 7, ep_title: 'Episode one', show_title: 'Show', pub_date: '2026-10-01' },
  effective_bounds: { start_s: 0, end_s: 1200 }, selected_bounds: { start_s: 0, end_s: 1200 }, media: {},
  window: { segments, text_complete: !next, next_cursor: next } });
const a6Seg = (id, start, text) => ({ segment_id: id, start_s: start, end_s: start + 2, text, text_start: 0, text_end: text.length });
const a6Episode = (over = {}) => ({ episode_id: 'ep_fx1', ep_title: 'Episode one', show_title: 'Show', pub_date: '2026-10-01',
  ads: [{ ref: A6_SR + ':q', evidence_id: 'e1', status: 'finding', span: [60, 120], display_span: [62, 118.5],
    brand: { key: 'quince', display: 'Quince', display_basis: 'brand_list', confirmed: false }, needs_review: true,
    self_promo: false, kind: 'named', source_label: 'Named read', transcript_start_s: 62,
    media: { playback: 'local', kind: 'evidence_clip', source_offset_s: 45, offset_s: 15, src: '/api/v1/customer/records/x/media' } }],
  self_promo: [{ ref: A6_SR + ':p', evidence_id: 'e2', status: 'finding', span: [900, 960], display_span: [900, 960],
    brand: { key: 'fixture show', display: 'fixture show', display_basis: 'none', confirmed: false }, needs_review: false,
    self_promo: true, self_promo_reason: 'own-platform:patreon', kind: 'named', source_label: 'Named read', transcript_start_s: 900, media: null }],
  screening: { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, stage: null },
  screened_no_ads: false, ...over });

test('A6 episode page lists its ads with time, brand, clip and a jump into the transcript', async () => {
  const record = a6EpisodeRecord([a6Seg(1, 60, 'Quince has'), a6Seg(2, 62, 'everything')]);
  const { html, paths } = await a6Render('#/episode/ep_fx1', path => {
    if (path === '/customer/records/' + encodeURIComponent('ep:ep_fx1')) return { record };
    if (path === '/customer/episodes/ep_fx1') return { episode: a6Episode() };
    throw new Error('unexpected ' + path);
  });
  assert.ok(paths.includes('/customer/episodes/ep_fx1'));
  assert.match(html, /Ads in this episode/);
  assert.match(html, /Quince/);
  assert.match(html, /1:02–1:59/);
  assert.doesNotMatch(html, /Needs review|Possible ad/, 'PLAIN-ADS: no review state on the ad row');
  assert.equal((html.match(/Report a mistake/g) || []).length, 2, 'every ad and self-promo read can be reported');
  assert.match(html, new RegExp('<audio[^>]*src="/api/v1/customer/records/' + encodeURIComponent(A6_SR + ':q').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '/media#t=14\\.0"'));
  assert.match(html, /href="#\/episode\/ep_fx1\?at=62"[^>]*>Jump to transcript/);
  assert.match(html, /href="#\/evidence\/sr1%3Aa6a6[^"]*%3Aq"/);
  assert.match(html, /Show’s own promotion/);
  assert.match(html, /Points to the show&#39;s own patreon page|Points to the show's own patreon page/);
  assert.match(html, /<span class="visually-hidden">No clip \(transcript only\)<\/span>/, 'one read has a clip, so the other says it has none');
  assert.match(html, /<ol class="transcript"/, 'one line per segment');
  assert.match(html, /1:00<\/span><div><p>Quince has<\/p>/);
  assert.doesNotMatch(html, /Quince haseverything/);
});

test('A6 episode page states: screened with no ads, stopped partway, not screened', async () => {
  const record = a6EpisodeRecord([a6Seg(1, 0, 'Hello')]);
  const page = async episode => (await a6Render('#/episode/ep_fx1', path => {
    if (path === '/customer/records/' + encodeURIComponent('ep:ep_fx1')) return { record };
    if (path === '/customer/episodes/ep_fx1') return { episode };
    throw new Error('unexpected ' + path);
  })).html;
  const quiet = await page(a6Episode({ ads: [], self_promo: [], screened_no_ads: true,
    screening: { label: 'Screened, no ads found', jev_screened: true, screened_no_ads: true, stopped_partway: false, stage: null } }));
  assert.match(quiet, /Screened, no ads found/);
  const stopped = await page(a6Episode({ ads: [], self_promo: [],
    screening: { label: 'Stopped partway (name)', jev_screened: false, screened_no_ads: false, stopped_partway: true, stage: 'name' } }));
  assert.match(stopped, /Processing stopped partway, at brand naming/);
  assert.doesNotMatch(stopped, /no ads found/i);
  const unknown = await page(a6Episode({ ads: [], self_promo: [], screening: null }));
  assert.match(unknown, /not been screened for ads/);
  assert.doesNotMatch(unknown, /no ads found/i);
  const tier = await page(a6Episode({ ads: [], self_promo: [],
    screening: { label: 'Not screened for ads (transcript only)', jev_screened: false, screened_no_ads: false,
      stopped_partway: false, stage: null, coverage_tier: 'transcript', transcript_only: true } }));
  assert.match(tier, /Not screened for ads \(transcript only\)/);
  assert.match(tier, /transcript is searchable/);
  assert.doesNotMatch(tier, /no ads found|ads are unknown/i);
});

test('A6 a failed episode ads read is an error with Retry, never an empty list', async () => {
  const record = a6EpisodeRecord([a6Seg(1, 0, 'Hello')]);
  const { html } = await a6Render('#/episode/ep_fx1', path => {
    if (path === '/customer/records/' + encodeURIComponent('ep:ep_fx1')) return { record };
    if (path === '/customer/episodes/ep_fx1') throw Object.assign(new Error('boom'), { status: 500 });
    throw new Error('unexpected ' + path);
  });
  assert.match(html, /Ads for this episode are unavailable: boom/);
  assert.match(html, /data-action="retry"/);
  assert.doesNotMatch(html, /no ads/i);
  assert.match(html, /Hello/, 'the transcript still renders');
});

test('A6 jump to transcript reads the window from the ad and marks the line', async () => {
  const first = a6EpisodeRecord([a6Seg(1, 0, 'Hello'), a6Seg(2, 2, 'there')], 'cursor-1');
  const later = a6EpisodeRecord([a6Seg(40, 895, 'Before'), a6Seg(41, 899, 'Join our Patreon')], null);
  const { html, paths } = await a6Render('#/episode/ep_fx1?at=900', path => {
    if (path === '/customer/records/' + encodeURIComponent('ep:ep_fx1')) return { record: first };
    if (path.startsWith('/customer/records/' + encodeURIComponent('ep:ep_fx1') + '?')) return { record: later };
    if (path === '/customer/episodes/ep_fx1') return { episode: a6Episode() };
    throw new Error('unexpected ' + path);
  });
  const windowRead = paths.find(p => p.includes('?start_s='));
  assert.ok(windowRead, paths.join(' | '));
  assert.match(windowRead, /start_s=870&end_s=1200/);
  assert.match(html, /<li[^>]*data-jump-target[^>]*>[\s\S]*Join our Patreon/);
  assert.match(html, /Transcript from 14:30 /, '30 s of lead-in before the ad');
  assert.match(html, /href="#\/episode\/ep_fx1"[^>]*>Start from the beginning/);
});

test('A6 show page has a sponsor table; a missing table is said, never an empty one', async () => {
  const detail = { show_id: 'cs-1', display_title: 'Show', episodes: [{ id: 'ep_fx2', ep_title: 'E2', pub_date: '2026-10-02' }],
    coverage: { episodes: 1, episodes_with_transcript: 1 }, category_summary: [],
    sponsors: [{ key: 'simplisafe', display: 'SimpliSafe', display_basis: 'brand_list', episodes: 3, ads: 4, ad_breaks: 2, first_seen: '2026-09-01', last_seen: '2026-10-02' }] };
  const page = async d => (await a6Render('#/show/cs-1', path => {
    if (path === '/customer/shows/cs-1') return d;
    throw new Error('unexpected ' + path);
  })).html;
  const html = await page(detail);
  assert.match(html, /<h2 id="sponsors-h">Sponsors heard<\/h2>/);
  assert.match(html, /Brands heard in this show's ads\. Self-promotion is not counted\./);
  assert.match(html, /href="#\/brand\/simplisafe"[^>]*>SimpliSafe<\/a><span class="lib-row-sub">3 episodes · Sep 1 to Oct 2<\/span><\/td><td class="ew-num">2 ads<\/td>/);
  assert.doesNotMatch(html, /\b4 reads?\b/, 'the read count is never shown');
  const { sponsors, ...older } = detail;
  void sponsors;
  assert.match(await page(older), /sponsor list is unavailable/);
  assert.match(await page({ ...detail, sponsors: [] }), /No named sponsors found\./);
});

function loadOperator(fetch) {
  const sandbox = { console, URLSearchParams, setTimeout, clearTimeout,
    sessionStorage: { getItem: () => null, setItem() {} }, localStorage: { getItem: () => null, setItem() {} },
    location: { hash: '#/review/e1' }, fetch,
    document: { addEventListener() {}, getElementById: () => ({ textContent: '' }), querySelectorAll: () => [] } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  for (const file of ['operator/app.js', 'operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  return sandbox;
}

test('A6 operator review lists the other brand candidates with one brand choice', async () => {
  const sent = [];
  const op = loadOperator(async (url, init) => { sent.push({ url, init }); return { ok: true, status: 200, json: async () => ({ ok: true, revision: 4, read: a6Read({ brand: { key: 'ring', display: 'Ring', confirmed: true } }) }) }; });
  const markup = vm.runInContext('brandChoiceMarkup', op)(a6Read(), 3);
  assert.match(markup, /SimpliSafe/);
  assert.match(markup, /Needs review/i);
  assert.match(markup, /<input type="radio" name="brand-candidate" value="ring"/);
  assert.match(markup, /id="use-brand" disabled>Use selected brand</);
  assert.match(markup, /value="simply safe"/);
  assert.match(markup, /namer&#39;s pick|namer's pick/);
  assert.equal(vm.runInContext('brandChoiceMarkup', op)({ historical: true, current_ref: 'x' }, 3), '');
  const out = await vm.runInContext('chooseBrand', op)('e1', 'ring', 3);
  assert.equal(out.read.brand.key, 'ring');
  assert.equal(sent[0].url, '/api/v1/evidence/e1/brand-choice');
  assert.equal(sent[0].init.method, 'POST');
  assert.equal(sent[0].init.headers['If-Match'], '3');
  assert.deepEqual(JSON.parse(sent[0].init.body), { brand: 'ring' });
});

test('A6 a scope that contradicts the chart selection is a clear message, never an empty list', async () => {
  const { html } = await a6Render('#/search?mode=ads&self_promo=only', path => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') throw Object.assign(new Error('request self_promo contradicts the selector scope'), { status: 400 });
    throw new Error('unexpected ' + path);
  });
  assert.match(html, /This search conflicts with your chart selection/);
  assert.match(html, /disagree about self-promotion/);
  assert.match(html, /data-action="remove-selector"/);
  assert.doesNotMatch(html, /No (results|detections)/i);
  const win = loadViews(canonicalSessionFetch());
  assert.equal(win.EWViews.scopeConflictText('request date scope differs from the selector scope'),
    'The chart selection covers other dates than this search.');
  assert.equal(win.EWViews.scopeConflictText('Failed to fetch'), null);
});

/* -- Admin review (2026-10-11): read times, badges, "Report a mistake" -- */
test('read times are whole seconds, hours as h:mm:ss', () => {
  const W = loadAt('#/search');
  assert.equal(W.EWUI.timeRange([4, 60]), '0:04–1:00');
  assert.equal(W.EWUI.timeRange([3845.4, 3901.6]), '1:04:05–1:05:02');
  assert.equal(W.EWUI.clock(59.5), '1:00');
  assert.equal(W.EWUI.timeRange([null, 3]), '');
});

test('no status badge for any ad state, for members and the operator alike (PLAIN-ADS)', () => {
  const W = loadAt('#/search');
  for (const hosted of [true, false]) {
    W.EW.current.hosted = hosted;
    for (const st of ['finding', 'confirmed', 'unresolved', 'mention']) assert.equal(W.EWUI.statusBadge(st), '', st);
  }
});

const reportForm = (reason = 'not_an_ad', note = ' it is a show segment ') => {
  const box = { outerHTML: '<details>' };
  const status = { textContent: '' }, submit = { disabled: false };
  return { box, status, submit, dataset: { reportRef: 'sr1:abc:1' }, classList: { contains: c => c === 'report-form' },
    closest: s => (s === '.report-problem' ? box : null),
    querySelector: s => s === '[role=status]' ? status : s === '[type=submit]' ? submit
      : s === 'input[name=reason]:checked' ? { value: reason } : s === 'textarea[name=note]' ? { value: note } : null };
};

test('Report a mistake sends the reason and note, then thanks the member', async () => {
  const win = loadViews(canonicalSessionFetch()); await flushBoot();
  const sent = [];
  win.EW.api = async (path, opts) => { sent.push([path, opts]); return { report: { id: 1, status: 'received' } }; };
  const form = reportForm();
  await win.EWViews.sendReport(form);
  assert.equal(sent[0][0], '/customer/records/' + encodeURIComponent('sr1:abc:1') + '/report');
  assert.equal(sent[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0][1].body)), { reason: 'not_an_ad', note: 'it is a show segment' });
  assert.match(form.box.outerHTML, /Your report was sent to the EarWitness team\./);
  assert.match(form.box.outerHTML, /<span>Nothing on this page changes for now\.<\/span>/);
  assert.doesNotMatch(form.box.outerHTML, /waits for review/);
});

test('Report a mistake says when a member sent too many reports, and keeps the form', async () => {
  const win = loadViews(canonicalSessionFetch()); await flushBoot();
  win.EW.api = async () => { const e = new Error('too many reports'); e.status = 429; throw e; };
  const form = reportForm('wrong_brand', '');
  await win.EWViews.sendReport(form);
  assert.equal(form.box.outerHTML, '<details>');
  assert.match(form.status.textContent, /many reports in the last hour/);
  assert.equal(form.submit.disabled, false);
});

/* -- Option A (2026-10-13): the namer's product / network next to the brand -- */
test('the brand label adds product and network with a middle dot, and nothing when absent', () => {
  const U = loadAt('#/search').EWUI;
  assert.equal(U.brandLabel({ display: 'Apple', product: 'Apple Watch' }), 'Apple · Apple Watch');
  assert.equal(U.brandLabel({ display: 'Pivot', network: 'Vox Media | New York Magazine' }), 'Pivot · Vox Media | New York Magazine');
  assert.equal(U.brandLabel({ display: 'Apple', product: 'Apple Watch', network: 'Vox' }), 'Apple · Apple Watch · Vox');
  assert.equal(U.brandLabel({ display: 'Apple' }), 'Apple');
  assert.equal(U.brandLabel({ display: 'Apple', product: null, network: '' }), 'Apple');
  assert.equal(U.brandLabel({}), '');
  assert.equal(U.brandLabel(null), '');
});

test('result cards show the brand label, escaped', () => {
  const U = loadAt('#/search').EWUI;
  const facts = brand => U.readFacts({ read: { brand } });
  assert.match(facts({ display: 'Apple', product: 'Apple Watch' }), /<strong>Apple · Apple Watch<\/strong>/);
  assert.match(facts({ display: 'Pivot', network: 'Vox Media | New York Magazine' }), /<strong>Pivot · Vox Media \| New York Magazine<\/strong>/);
  assert.match(facts({ display: 'Apple' }), /<strong>Apple<\/strong>/);
  const html = facts({ display: 'A&B', product: '<img src=x onerror=alert(1)>', network: '"Net"' });
  assert.ok(!html.includes('<img'), html);
  assert.match(html, /A&amp;B · &lt;img src=x onerror=alert\(1\)&gt; · (&quot;|")Net(&quot;|")/);
});

test('alerts hub Pause and Resume send the displayed revision and the toggled lifecycle', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {};
  const posts = [];
  win.EW.api = async (path, opts) => {
    if (path === '/customer/alerts') return { alerts: [
      { id: 11, name: 'Active one', revision: 4, definition_revision: 9, lifecycle: 'active_local', unread_event_count: 0, query: { version: 2, mode: 'mentions', q: 'a' } },
      { id: 12, name: 'Paused one', revision: 6, definition_revision: 2, lifecycle: 'paused', unread_event_count: 0, query: { version: 2, mode: 'mentions', q: 'b' } },
    ] };
    if (path === '/customer/shows') return { shows: [] };
    if (path.endsWith('/lifecycle')) { posts.push({ path, body: opts.body }); return { ok: true }; }
    throw new Error('unexpected ' + path);
  };
  win.location.hash = '#/alerts';
  await win.EWViews.render();
  const hubHtml = main.innerHTML;
  // Click exactly what the hub rendered: the button's own data-* attributes.
  const click = async (id) => {
    const tag = hubHtml.match(new RegExp(`<button[^>]*data-action="pause-alert"[^>]*data-id="${id}"[^>]*>`));
    assert.ok(tag, 'hub renders a pause-alert button for alert ' + id);
    const dataset = { action: 'pause-alert' };
    for (const [, k, v] of tag[0].matchAll(/data-([a-z-]+)="([^"]*)"/g)) dataset[k] = v;
    await win.EWViews.handleAction({ dataset });
  };
  await click(11);
  await click(12);
  assert.deepEqual(plain(posts.map(p => p.path)), ['/customer/alerts/11/lifecycle', '/customer/alerts/12/lifecycle']);
  assert.deepEqual(plain(posts[0].body), { lifecycle: 'paused', expected_revision: 4 });
  assert.deepEqual(plain(posts[1].body), { lifecycle: 'active_local', expected_revision: 6 });
});

test('Search Episodes tab cards open the transcript and show passage counts, not evidence', async () => {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '' });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  const episode = { ref: 'ep:e42', record_kind: 'episode', evidence_id: null, episode_id: 'e42', show_title: 'Morning brew',
    ep_title: 'Cola special', pub_date: '2026-03-10', text: '', match_basis: 'transcript', matching_passages: 3,
    indexed_passages: 120, status: '', review: null, attribution: [], audio: 'available', audio_local: false };
  const evidence = { ref: 'sr1:ad', record_kind: 'evidence', status: 'finding', ep_title: 'Ad episode', show_title: 'Ad show',
    pub_date: '2026-01-01', text: 'plain evidence passage', audio: 'available', audio_local: false };
  const bundleFor = (mode, items) => ({ query: { version: 2, mode, q: 'cola', sort: 'newest', page: 1 }, revision: 'rev-1',
    result: { items, total: items.length, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: { week: [] } });
  let rows = [episode];
  win.EW.api = async (path) => {
    if (path === '/customer/brands') return { brands: [] };
    if (path === '/customer/shows') return { shows: [] };
    if (path === '/customer/research-query') return bundleFor('episodes', rows);
    throw new Error('unexpected ' + path);
  };
  win.location.hash = '#/search?mode=episodes&q=cola';
  await win.EWViews.render();
  const html = main.innerHTML;
  assert.match(html, /href="#\/episode\/e42"/);
  assert.match(html, /Open transcript/);
  assert.match(html, /3 matching passages · 120 indexed passages/);
  assert.match(html, /<input type="checkbox" name="selected_ref" value="ep:e42"/, 'an episode can still be selected for export');
  assert.doesNotMatch(html, /#\/evidence\/ep:/);
  assert.doesNotMatch(html, /#\/evidence\/ep%3A/);
  assert.doesNotMatch(html, /View evidence|Open evidence/);
  // Non-episode rows keep the evidence card.
  rows = [evidence];
  win.location.hash = '#/search?mode=episodes&q=cola+x';
  await win.EWViews.render();
  assert.match(main.innerHTML, /plain evidence passage/);
  assert.match(main.innerHTML, /Open evidence/);
});

/* -- Brand not identified (Reno, 2026-10-13): a below-threshold name waits for
   review; customers see the ad with "Brand not identified", never the guess. -- */
const UNDER_REVIEW = { key: null, display: null, display_basis: 'none', suggested: null, confirmed: false, under_review: true };
const hiddenRead = (over = {}) => a6Read({ brand: { ...UNDER_REVIEW }, candidates: undefined, ...over });

test('a search card says Brand not identified for a read whose brand waits for review', () => {
  const U = loadAt('#/search').EWUI;
  const html = U.readFacts({ read: hiddenRead(), quotation_span: [60, 120] });
  assert.match(html, /Brand not identified/);
  assert.equal((html.match(/Needs review/g) || []).length, 0, 'one label, not two');
  assert.equal(U.brandLabel(UNDER_REVIEW), 'Brand not identified');
});

test('the episode page and the evidence page say Brand not identified, not Not named', async () => {
  const ad = { ...a6Episode().ads[0], brand: { ...UNDER_REVIEW } };
  const record = a6EpisodeRecord([a6Seg(1, 60, 'Quince has')]);
  const ep = await a6Render('#/episode/ep_fx1', path => {
    if (path === '/customer/records/' + encodeURIComponent('ep:ep_fx1')) return { record };
    if (path === '/customer/episodes/ep_fx1') return { episode: a6Episode({ ads: [ad] }) };
    throw new Error('unexpected ' + path);
  });
  assert.match(ep.html, /Brand not identified/);
  assert.doesNotMatch(ep.html, /Not named/);
  const ref = A6_SR + ':w:60.0:120.0';
  const evidence = { ref, record_kind: 'evidence', status: 'finding', review: { judgment: null, revision: 0 },
    attribution: [], relationship: 'current', availability: 'current', bounds_basis: 'source bounds',
    selected_bounds: { start_s: 60, end_s: 120 }, episode: { show_title: 'Show', ep_title: 'Episode', pub_date: '2026-10-02' },
    evidence: { episode_id: 'ep_fx2' }, media: {}, read: hiddenRead(),
    window: { segments: [{ segment_id: 1, start_s: 60, end_s: 62, text: 'Quince has', text_start: 0, text_end: 10 }],
      text_complete: true, next_cursor: null } };
  const page = await a6Render('#/evidence/' + encodeURIComponent(ref), path => {
    if (path.endsWith('/mistake')) return { mistakes: [] };
    if (path.startsWith('/customer/records/')) return { record: evidence };
    throw new Error('unexpected ' + path);
  });
  assert.match(page.html, /<h1[^>]*>(<span[^>]*>[\s\S]*?)?Brand not identified/);
  // PLAIN-ADS-FIX1 (screen R1-04): no name fact rows on a held ad.
  assert.doesNotMatch(page.html, /<th scope="row">Brand heard in the audio<\/th>/);
  assert.match(page.html, /We found this ad but haven’t matched it to a brand, so it doesn’t count toward any brand\./);
  assert.doesNotMatch(page.html, /Not named|namer's guess|Brand confidence|Name certainty/);
});

/* -- Design v3 (Exhibit) shared builders in EWUI: escaping, accessible
   names, and no invented values when a field is missing. -- */
const XSS = '<img src=x onerror=alert(1)>"&';
function dsUI() { return loadAt('#/overview').EWUI; }
function noRawXss(html) {
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /style=/, 'no inline styles (CSP style-src self)');
}

test('EWUI.tag: kinds carry words, an icon only as decoration, escaped labels', () => {
  const U = dsUI();
  const confirmed = U.tag('confirmed', 'Confirmed');
  assert.match(confirmed, /class="ew-tag ew-tag-confirmed" data-tag="confirmed"/);
  assert.match(confirmed, /<svg[^>]*aria-hidden="true"/);
  assert.match(confirmed, />Confirmed<\/span>$/);
  const review = U.tag('review');
  assert.match(review, /data-tag="review"/);
  assert.match(review, /Brand not identified<\/span>$/, 'review default label is the product wording');
  assert.match(U.tag('code', 'Code', { value: 'OFFER20' }), /Code <b>OFFER20<\/b>/);
  assert.equal(U.tag('code', 'Code', { value: '' }), '', 'a code tag without its code is not drawn');
  const evil = U.tag('neutral', XSS);
  noRawXss(evil);
  assert.match(evil, /&lt;img src=x onerror=alert\(1\)&gt;&quot;&amp;/);
  assert.match(U.tag('nonsense', 'Plain'), /ew-tag-neutral/, 'unknown kinds fall back to neutral');
  assert.match(U.tierTag('full'), /Full coverage/);
  assert.match(U.tierTag('transcript'), /Transcript only/);
  assert.equal(U.tierTag(undefined), '', 'unknown tier says nothing rather than guessing');
});

test('EWUI.playButton and timeRangeHtml: accessible names from real times only', () => {
  const U = dsUI();
  const play = U.playButton({ span: [31, 62] });
  assert.match(play, /<button type="button" class="ew-play" aria-label="Play clip, 0:31 to 1:02">/);
  assert.match(play, /<svg[^>]*aria-hidden="true"/);
  assert.match(U.playButton({ span: [31, 62], playing: true }), /class="ew-play is-playing" aria-label="Pause clip"/);
  assert.match(U.playButton({ label: XSS }), /aria-label="&lt;img/);
  assert.match(U.playButton({ span: [1, 2], size: 'lg', outline: true, attrs: { action: 'play', ref: 'a"b' } }),
    /class="ew-play ew-play-lg ew-play-outline"[^>]* data-action="play" data-ref="a&quot;b"/);
  assert.match(U.playButton({}), /aria-label="Play clip"/, 'no times: no invented range in the name');
  const range = U.timeRangeHtml([31, 62]);
  assert.match(range, /<span class="ew-time" data-print="full-time" data-start="00:00:31" data-end="00:01:02">0:31–1:02<\/span>/);
  assert.equal(U.timeRangeHtml(null), '');
  assert.equal(U.timeRangeHtml([31, null]), '');
  assert.equal(U.timeRangeHtml(['x', 4]), '');
});

test('EWUI.ledger: each fact with its source; a missing value says so, never blank or invented', () => {
  const U = dsUI();
  const html = U.ledger([
    { label: 'Brand heard in the audio', value: 'Northwind', source: 'Transcript · 47:05' },
    { label: 'Web address read out', value: 'restwell.com/notes', mono: true },
    { label: 'Promo code read out', value: null, missing: 'None heard' },
    { label: 'Brand the address points to', value: undefined },
    { label: 'Differs', value: XSS, uncertain: true, note: 'We show both and don’t pick one.' },
  ]);
  noRawXss(html);
  assert.match(html, /<table class="ew-ledger">/);
  assert.match(html, /<th scope="row">Brand heard in the audio<\/th><td>Northwind<div class="ew-ledger-source">Transcript · 47:05<\/div><\/td>/);
  assert.match(html, /<td class="ew-ledger-value-mono">restwell\.com\/notes<\/td>/);
  assert.match(html, /<td class="ew-ledger-missing">None heard<\/td>/);
  assert.match(html, /Brand the address points to<\/th><td class="ew-ledger-missing">Not known<\/td>/);
  assert.match(html, /<tr class="is-uncertain">/);
  assert.doesNotMatch(html, /undefined|null|NaN/);
  assert.equal(U.ledger([]), '');
});

test('EWUI.uncertainBlock and citationChip: escaped, named, no clip means a link to evidence', () => {
  const U = dsUI();
  const block = U.uncertainBlock({ subject: XSS, reason: 'No episode mentions it.' });
  noRawXss(block);
  assert.match(block, /class="ew-uncertain"/);
  assert.match(block, /data-tag="uncertain"/);
  assert.equal(U.uncertainBlock({ subject: 'x' }), '', 'Uncertain is always followed by its reason');
  const chip = U.citationChip({ span: [1122, 1191], show: 'The Long Run', episode: 'Recovery weeks', attrs: { action: 'cite' } });
  assert.match(chip, /^<button type="button" class="ew-cite" aria-label="Play citation: 18:42 to 19:51, The Long Run, Recovery weeks" data-action="cite">/);
  const noClip = U.citationChip({ span: [14, 41], show: 'Common Ground', clip: false, href: '#/evidence/a b' });
  assert.match(noClip, /^<a class="ew-cite ew-cite-noclip" href="#\/evidence\/a b"/);
  assert.match(noClip, /no clip, opens evidence/);
  noRawXss(U.citationChip({ span: [1, 2], show: XSS }));
  assert.doesNotMatch(U.citationChip({ show: 'S' }), /–|NaN/, 'no times: no range drawn');
});

test('EWUI.podBar: segments sized only from the given timestamps; no bar without them', () => {
  const U = dsUI();
  const spots = [
    { span: [1060, 1092], label: 'Squarespace' },
    { span: [1092, 1122], under_review: true, label: 'GuessedName' },
    { span: [1122, 1191], label: 'AG1' },
  ];
  const html = U.podBar({ spots, playing: 2 });
  noRawXss(html);
  assert.match(html, /role="group" aria-label="Ad, 3 pieces of evidence"/);
  // widths are percentages of the pod's own span (1060–1191 = 131 s)
  assert.match(html, /ew-pod-w-24-4|data-w="24.4"/);
  assert.match(html, /data-w="22.9"/);
  assert.match(html, /aria-label="Evidence, Brand not identified, 18:12 to 18:42"/);
  assert.doesNotMatch(html, /GuessedName/, 'an under-review spot never shows a guessed name');
  assert.match(html, /aria-label="Evidence, AG1, 18:42 to 19:51, playing"/);
  assert.match(html, /aria-label="Evidence, Squarespace, 17:40 to 18:12"/);
  const unnamed = U.podBar({ spots: [{ span: [0, 30], label: 'A' }, { span: [30, 60] }] });
  assert.match(unnamed, /Brand not identified/);
  assert.equal(U.podBar({ spots: [{ span: [0, 30], label: 'A' }] }), '', 'one spot: no pod bar');
  assert.equal(U.podBar({ spots: [{ span: [0, 30], label: 'A' }, { span: [30, null], label: 'B' }] }), '',
    'a spot without both times: no bar rather than an invented width');
  assert.equal(U.podBar({ spots: [{ span: [30, 0], label: 'A' }, { span: [30, 60], label: 'B' }] }), '');
  assert.equal(U.podBar({}), '');
  const unclear = U.podBar({ spots: [{ span: [0, 30], label: 'A', unclear_end: true }, { span: [30, 60], label: 'B' }] });
  assert.match(unclear, /is-unclear-end/);
  assert.match(unclear, /ends about 0:30, boundary not clear/);
});

test('EWUI.podBar widths reach the page through classes, not inline styles', () => {
  const U = dsUI();
  const html = U.podBar({ spots: [{ span: [0, 25], label: 'A' }, { span: [25, 100], label: 'B' }] });
  assert.doesNotMatch(html, /style=/);
  assert.match(html, /data-w="25"/);
  assert.match(html, /data-w="75"/);
});

test('EWUI.previewCard, initialsTile and state blocks: escaped, honest states', () => {
  const U = dsUI();
  const idle = U.previewCard({ title: 'Preview', sub: XSS, state: 'idle' });
  noRawXss(idle);
  assert.match(idle, /<section class="ew-preview" aria-live="polite"/);
  const failed = U.previewCard({ state: 'failed' });
  assert.match(failed, /role="alert"/);
  assert.match(failed, /not “nothing would match”/);
  const results = U.previewCard({ state: 'results', items: [{ reason: 'Taste complaint', quote: XSS, span: [10, 20], show: 'S', episode: 'E' },
    { reason: 'Maybe', uncertain: true, why: 'Sarcasm' }] });
  noRawXss(results);
  assert.match(results, /data-tag="uncertain"/);
  assert.match(results, /Sarcasm/);
  assert.doesNotMatch(results, /undefined|NaN/);
  const tile = U.initialsTile('The Long Run');
  assert.match(tile, /^<span class="ew-tile ew-tint-\d" aria-hidden="true">LR<\/span>$/);
  assert.equal(U.initialsTile('The Long Run'), tile, 'tint is stable for a name');
  assert.match(U.initialsTile('', { size: 'lg' }), /ew-tile-lg[^>]*><\/span>$/);  // plain tile (APV-R1-05)
  noRawXss(U.initialsTile(XSS));
  const empty = U.emptyState({ title: XSS, body: 'Try a wider range.', actions: '<a class="button" href="#/search">Search</a>' });
  noRawXss(empty.replace('<a class="button" href="#/search">Search</a>', ''));
  assert.match(empty, /class="ew-state"/);
  const err = U.errorState({ title: 'Search didn’t finish', body: 'Your filters are kept.' });
  assert.match(err, /class="ew-state ew-state-error" role="alert"/);
  const loading = U.loadingState({ label: 'Loading results', lines: 3 });
  assert.match(loading, /role="status" aria-busy="true" aria-label="Loading results"/);
  assert.equal((loading.match(/ew-skeleton"/g) || []).length, 3);
});

test('EWUI.brandHtml: Brand · Product, under review replaces the name, nothing invented', () => {
  const U = dsUI();
  assert.match(U.brandHtml({ display: 'Apple', product: 'Apple Watch' }),
    /<span class="ew-brand">Apple <span class="ew-brand-sep">·<\/span> <span class="ew-brand-product">Apple Watch<\/span><\/span>/);
  assert.equal(U.brandHtml({ display: 'AG1', product: 'AG1' }), '<span class="ew-brand">AG1</span>', 'product hidden when it repeats the brand');
  const ur = U.brandHtml({ display: 'Guess', product: 'P', under_review: true });
  assert.equal(ur, '<span class="ew-brand-none">Brand not identified</span>');
  assert.doesNotMatch(ur, /Guess|>P</);
  assert.equal(U.brandHtml({}), '');
  noRawXss(U.brandHtml({ display: XSS }));
});

/* -- Design v3 backend helpers: Track a brand + Settings preferences ------ */
const jsonFetch = (routes, calls = []) => async (url, init = {}) => {
  calls.push({ url, method: init.method || 'GET', body: init.body ? JSON.parse(init.body) : null });
  const out = routes(url, init);
  if (out instanceof Error) throw out;
  const [status, body] = Array.isArray(out) ? out : [200, out];
  return { ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) };
};
const brandItem = (id, key, rev = 1) => ({ id, type: 'bookmark', title: key, revision: rev, availability: 'current',
  target: { kind: 'brand', id: key }, captured: { record_kind: 'brand', label: key, opens: 'current_profile' } });

test('tracked brands page the saved bookmarks and keep only brand targets', async () => {
  const calls = [];
  const win = loadAt('#/brands', { fetch: jsonFetch(url => {
    if (url === '/api/v1/customer/saved-items?type=bookmark&limit=20') {
      return { ok: true, items: [brandItem('si-1', 'quince'), { id: 'si-2', type: 'bookmark', target: { kind: 'show', id: 'sh-1' } }], next_cursor: 'c2' };
    }
    if (url === '/api/v1/customer/saved-items?type=bookmark&limit=20&cursor=c2') return { ok: true, items: [brandItem('si-3', 'lumen')], next_cursor: null };
    throw new Error('unexpected ' + url);
  }, calls) });
  const items = await win.EWSaved.trackedBrands(win.EW.api);
  assert.deepEqual(plain(items.map(i => i.id)), ['si-1', 'si-3']);
  assert.equal(calls.length, 2);
  assert.equal(win.EWSaved.trackedBrandFor(items, 'lumen').id, 'si-3');
  assert.equal(win.EWSaved.trackedBrandFor(items, 'ring'), null);
  assert.equal(win.EWSaved.trackedBrandFor([{ ...brandItem('si-9', null) }], null), null, 'a redacted item matches nothing');
});

test('tracked brands surface a failed list instead of an empty one', async () => {
  const win = loadAt('#/brands', { fetch: jsonFetch(() => [500, { ok: false, error: 'boom' }]) });
  await assert.rejects(() => win.EWSaved.trackedBrands(win.EW.api));
});

test('track sends only the brand target; untrack sends the displayed revision', async () => {
  const calls = [];
  const win = loadAt('#/brand/quince', { fetch: jsonFetch((url, init) => {
    if (url === '/api/v1/customer/saved-items' && init.method === 'POST') return { ok: true, created: true, item: brandItem('si-1', 'quince') };
    if (url === '/api/v1/customer/saved-items/si-1' && init.method === 'DELETE') return { ok: true, removed_id: 'si-1' };
    throw new Error('unexpected ' + url);
  }, calls) });
  const out = await win.EWSaved.trackBrand(win.EW.api, 'quince');
  assert.equal(out.item.id, 'si-1');
  assert.deepEqual(calls[0].body, { type: 'bookmark', target: { kind: 'brand', id: 'quince' }, note: '' });
  await win.EWSaved.untrackBrand(win.EW.api, out.item);
  assert.equal(calls[1].method, 'DELETE');
  assert.deepEqual(calls[1].body, { expected_revision: 1 });
  await assert.rejects(() => win.EWSaved.trackBrand(win.EW.api, ''), /brand/);
  await assert.rejects(() => win.EWSaved.untrackBrand(win.EW.api, { id: 'si-1' }), /revision/i);
  assert.equal(calls.length, 2, 'refused before any request');
});

test('a brand bookmark needs no title and links to its brand page', () => {
  const S = loadAt('#/saved').EWSaved;
  assert.equal(S.checkDraft('bookmark', { kind: 'brand', id: 'quince' }), '');
  assert.deepEqual(plain(S.createRequest('bookmark', { kind: 'brand', id: 'quince', title: 'ignored' })),
    { type: 'bookmark', target: { kind: 'brand', id: 'quince' }, note: '' });
  assert.match(S.checkDraft('bookmark', { kind: 'brand' }), /brand/);
});

test('the saved hub shows a tracked brand, and an unavailable one without a name', async () => {
  const win = loadAt('#/saved?tab=bookmarks', { fetch: jsonFetch(url => {
    if (url.startsWith('/api/v1/customer/saved-items?')) {
      return { ok: true, items: [brandItem('si-1', 'quince'),
        { id: 'si-2', type: 'bookmark', title: 'Tracked brand unavailable', revision: 2, availability: 'unavailable',
          availability_reason: 'this brand is not available to you now', target: { kind: 'brand', id: null }, captured: { record_kind: 'brand', opens: 'current_profile' } }],
      total: 2, total_relation: 'exact', counts: { excerpt: 0, query: 0, bookmark: 2 }, next_cursor: null };
    }
    throw new Error('unexpected ' + url);
  }) });
  const html = await win.EWSaved.render({ api: win.EW.api, esc: win.EW.esc, link: win.EW.link, scope: {}, workspaceContext: '', signal: null, isCurrent: () => true });
  assert.match(html, /href="#\/brand\/quince"/);
  assert.match(html, /Tracked brand/);
  assert.doesNotMatch(html, /#\/brand\/(null|undefined|")/);
});

const prefsWin = (prefs, calls = [], route = '#/search') => loadAt(route, { fetch: jsonFetch((url, init) => {
  if (url === '/api/v1/customer/preferences' && (init.method || 'GET') === 'GET') return typeof prefs === 'function' ? prefs() : { ok: true, preferences: prefs };
  if (url === '/api/v1/customer/preferences' && init.method === 'PUT') return { ok: true, preferences: { ...JSON.parse(init.body), expected_revision: undefined }, revision: 9 };
  throw new Error('unexpected ' + url);
}, calls) });

test('preferences are read once per workspace and resolve {} on failure', async () => {
  const calls = [];
  const win = prefsWin({ search_tab: 'episodes', revision: 3 }, calls);
  const E = win.EW;
  E.current.workspaceContext = 'ctx-a';
  assert.equal((await E.preferences()).search_tab, 'episodes');
  await E.preferences();
  assert.equal(calls.length, 1, 'cached');
  E.current.workspaceContext = 'ctx-b';
  await E.preferences();
  assert.equal(calls.length, 2, 'a workspace switch reads again');
  E.invalidatePreferences();
  await E.preferences();
  assert.equal(calls.length, 3, 'invalidated');
  const bad = prefsWin(() => [500, { ok: false, error: 'down' }]).EW;
  assert.deepEqual(plain(await bad.preferences()), {});
  const offline = prefsWin(() => new Error('offline')).EW;
  assert.deepEqual(plain(await offline.preferences()), {});
});

test('saving preferences refreshes the cache from the server answer', async () => {
  const calls = [];
  const E = prefsWin({ results_view: 'cards', revision: 3 }, calls).EW;
  await E.preferences();
  const out = await E.savePreferences({ results_view: 'list' }, 3);
  assert.equal(out.revision, 9);
  assert.deepEqual(calls[1].body, { results_view: 'list', expected_revision: 3 });
  assert.equal(E.prefResultsView(), 'list');
  assert.equal((await E.preferences()).revision, 9);
  assert.equal(calls.length, 2, 'no extra read after a save');
});

test('preference helpers fall back to the plain defaults', () => {
  const E = EW('#/search');
  assert.equal(E.prefSearchTab({}), 'ads');
  assert.equal(E.prefSearchTab({ search_tab: 'episodes' }), 'episodes');
  assert.equal(E.prefSearchTab({ search_tab: 'brands' }), 'ads');
  assert.equal(E.prefResultsView({}), 'cards');
  assert.equal(E.prefResultsView({ results_view: 'list' }), 'list');
  assert.equal(E.prefSearchTab(), 'ads', 'nothing read yet');
});

test('the default date range becomes a Query2 from/to patch', () => {
  const E = EW('#/search');
  const now = new Date('2026-10-14T12:00:00Z');
  assert.deepEqual(plain(E.prefDateRange({ default_range: '30d', time_zone: 'UTC' }, now)), { from: '2026-09-15', to: null });
  assert.deepEqual(plain(E.prefDateRange({ default_range: '7d', time_zone: 'UTC' }, now)), { from: '2026-10-08', to: null });
  assert.deepEqual(plain(E.prefDateRange({ default_range: '90d', time_zone: 'UTC' }, now)), { from: '2026-07-17', to: null });
  assert.deepEqual(plain(E.prefDateRange({ default_range: 'all' }, now)), { from: null, to: null });
  assert.deepEqual(plain(E.prefDateRange({}, now)), {});
  assert.deepEqual(plain(E.prefDateRange({ default_range: '14d' }, now)), {});
  // "Today" is the day in the chosen time zone.
  const late = new Date('2026-10-14T23:30:00Z');
  assert.deepEqual(plain(E.prefDateRange({ default_range: '7d', time_zone: 'Asia/Tokyo' }, late)), { from: '2026-10-09', to: null });
  const url = E.searchURL({ mode: 'ads', ...E.prefDateRange({ default_range: '7d', time_zone: 'UTC' }, now) });
  assert.match(url, /from=2026-10-08/);
});

test('formatDate keeps calendar dates and shows times in the chosen zone', () => {
  const E = EW('#/search');
  assert.equal(E.formatDate('2026-03-10', { time_zone: 'Asia/Tokyo' }), E.date('2026-03-10'));
  assert.equal(E.formatDate(null), 'Date unknown');
  assert.equal(E.formatDate(''), 'Date unknown');
  assert.equal(E.formatDate('not a date'), 'Date unknown');
  assert.equal(E.formatDate('2026-10-14T09:42:00+00:00', { time_zone: 'UTC' }), 'Oct 14, 9:42 AM UTC');
  assert.match(E.formatDate('2026-10-14T09:42:00+00:00', { time_zone: 'Europe/London' }), /^Oct 14, 10:42 AM (BST|GMT\+1)$/);
  assert.match(E.formatDate('2026-10-14T23:42:00Z', { time_zone: 'Asia/Tokyo' }), /^Oct 15, 8:42 AM/);
  assert.equal(E.formatDate('2026-10-14T09:42:00', { time_zone: 'UTC' }), 'Oct 14, 9:42 AM UTC', 'a zoneless server time is UTC');
  assert.equal(E.formatDate('2026-10-14T09:42:00Z', { time_zone: 'Not/AZone' }), 'Oct 14, 9:42 AM UTC', 'an unknown zone falls back to UTC');
});

test('clipStart always starts 3 seconds early, whatever the stored preference', () => {
  const E = EW('#/search');
  assert.equal(E.clipStart(60, { clip_lead_in: true }), 57);
  assert.equal(E.clipStart(1.5, { clip_lead_in: true }), 0);
  assert.equal(E.clipStart(60, { clip_lead_in: false }), 57);
  assert.equal(E.clipStart(60, {}), 57);
  assert.equal(E.clipStart(null, { clip_lead_in: true }), null, 'unknown stays unknown');
  assert.equal(E.clipStart('x', { clip_lead_in: true }), null);
});

/* -- DEMO2M-R3-01 / R3-03 (merged-head review round 3): per-person page
   state never outlives a person change. One clearPersonState() runs on
   sign-out and when another person is adopted; a next person with no
   workspace drops the previous person's context, so the workspace
   cleanup runs too. A guard classifies every EW.current field. -- */
const OTHER_PERSON = { principal: 'other', email: 'other@example.test', workspace_context: 'ctx-other' };
async function inviteSendUncertain() {
  let who = 'sam', delivery = 'not_queued';
  // The context every answer carries: the adopted session's.
  const ctx = () => (who === 'sam' || who === 'out' ? 'ctx-a' : who.workspace_context ?? 'ctx-a');
  const t = await uiWin(async (url, opts = {}) => {
    if (url === '/api/v1/session') {
      if (who === 'out') return json({ ok: false, error: 'signin', signin: { available: true } }, false, 401);
      return json(hostedSession({ workspace_names: WS_NAMES, ...(who === 'sam' ? {} : who) }));
    }
    if (url === '/api/v1/workspaces/ws-a/invites' && (opts.method || 'GET') === 'GET') return json({ ok: true, workspace_context: ctx(), invites: [
      { id: 'inv-42', member: 'pat@example.com', role: 'viewer', created_at: '2026-10-08T10:00:00+00:00', delivery }] });
    if (opts.method === 'POST' && url.endsWith('/send')) return json({ ok: false, error: { code: 'TIMEOUT', message: 'gateway timeout' } }, false, 504);
    return json({ ok: true, workspace_context: ctx(), preferences: { revision: 1 }, members: [{ member: 'sam', role: 'owner' }], shares: [] });
  });
  t.win.location.hash = '#/settings'; t.win.EW.current.route = '#/settings';
  await t.win.EWViews.render(); await settleAll();
  await t.win.EWViews.handleAction({ dataset: { action: 'invite-send', invite: 'inv-42' } }); await settleAll();
  assert.match(t.root.innerHTML, /Send uncertain/, 'fixture: the owner\'s send is uncertain');
  t.as = async next => { who = next; await t.win.EW.boot().catch(() => {}); await t.win.EWViews.render(); await settleAll(); };
  return t;
}
const notSentRow = /data-tag="neutral">Not sent<\/span><button class="button" type="button" data-action="invite-send" data-invite="inv-42">Send invite<\/button>/;
test('DEMO2M-R3-01: another person adopted in the same workspace: the row shows the server state with Send invite, not the previous person\'s "Send uncertain"', async () => {
  const t = await inviteSendUncertain();
  await t.as(OTHER_PERSON);
  assert.equal(t.win.EW.current.principal, 'other', 'fixture: the other person is adopted');
  assert.doesNotMatch(t.root.innerHTML, /Send uncertain|could not confirm whether the email/, 'the other person sees the previous person\'s send outcome');
  assert.match(t.root.innerHTML, notSentRow, 'the other person is not offered Send invite');
});
test('DEMO2M-R3-01: signed out, then another person: no previous send outcome', async () => {
  const t = await inviteSendUncertain();
  await t.as('out');
  assert.equal(t.win.EW.current.principal, null, 'fixture: signed out');
  assert.equal(t.win.EW.current.inviteSends, null, 'sign-out kept the previous person\'s send outcomes');
  await t.as(OTHER_PERSON);
  assert.match(t.root.innerHTML, notSentRow, 'the other person is not offered Send invite');
});
test('DEMO2M-R3-01 control: the same person, re-read: their own "Send uncertain" stays (no resend offered)', async () => {
  const t = await inviteSendUncertain();
  await t.as({});
  assert.match(t.root.innerHTML, /Send uncertain/);
  assert.doesNotMatch(t.root.innerHTML, /data-action="invite-send"/);
});
test('DEMO2M-R3-03: another person with no workspace: the previous person\'s context goes and the workspace cleanup runs', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  E.current.savedNote = { sentinel: true };
  await t.as({ ...OTHER_PERSON, active_workspace: '', workspaces: [], workspace_context: '' });
  assert.equal(E.current.principal, 'other', 'fixture: the other person is adopted');
  assert.equal(E.current.workspaceContext || '', '', 'the previous person\'s workspace context was kept');
  assert.ok(!(E.current.savedNote && E.current.savedNote.sentinel), 'the previous person\'s retained workspace view was kept');
});
test('DEMO2M-R3-03: signed out: the previous person\'s retained views go too', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  E.current.savedNote = { sentinel: true };
  await t.as('out');
  assert.equal(E.current.principal, null, 'fixture: signed out');
  assert.ok(!(E.current.savedNote && E.current.savedNote.sentinel), 'a retained workspace view survived sign-out');
});
test('DEMO2M-R3-03 control: the same person with the same context keeps their retained views', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  E.current.savedNote = { sentinel: true };
  await E.boot();
  assert.ok(E.current.savedNote && E.current.savedNote.sentinel, 'a same-person re-read cleared retained views');
});

// The guard: every EW.current field is classified as one person's,
// one workspace's, or global (the tab's own bookkeeping). A new field
// fails here until someone decides which it is.
const CURRENT_FIELDS = {
  // One person's. `clear`: clearPersonState() resets it on sign-out and
  // when another person is adopted. `adopt`: replaced or cleared at every
  // session adoption (boot, Settings, the invitation).
  person: {
    clear: 'inviteSends inviteAlert inviteAccepted inviteError createdInvite createdShare searchInspect searchPrefNote outcomeNotice',
    adopt: 'principal identity signin csrf sessionCsrf sessionRole workspaces workspaceNames adoptedSession signedOutHeld',
  },
  // One workspace's: cleared by clearWorkspaceState or a registered
  // store when the adopted context moves, or keyed by the context.
  workspace: 'workspaceContext activeWorkspace workspaceRole workspaceSwitchRevision displayedWorkspaceSwitchRevision displayedRevision '
    + 'dataset directory filters back scroll focus pendingSearch displayedAlert displayedDraft savedAlert savedExcerpt savedNote '
    + 'invitePreflight accessReads keepAccessReads lastSearch lastSearchTotal searchItems staleDraft dirtyForm dirtyWorkspace dialogDrafts '
    + 'dialogWorkspace dialogWorkspaceName promptDrafts leaveDrafts leaveTarget selection settingsSaved inviteSendOff '
    + 'campaignLinkageFilter cohortRefresh refusedWorkspace roleChange pilotFeatures askAvailable askPaused meaningAlertsAvailable briefCheckLiveAvailable recordLeftWs '
    // Plan A excerpt view: the plan level is the workspace's (keyed by its context).
    + 'excerptOnly excerptOnlyContext',
  // The tab's own bookkeeping: counters, epochs, timers, routing, flags.
  global: 'route fetchToken searchTimer toastTimer aborters framePath autoRetriedRoute rateLimitedRead refusedRead droppedTokens filtersLastGroup focusChooser '
    + 'offRouteBox freshEntry paintedToken supersededRender unpaintedFlow audioEpoch authorityEpoch pageEpoch recheckPending recheckToken recheckRun '
    + 'refusedToken roleRecheckPending roleRecheckToken publishedContext publishedGeneration publishedReadFailed publishedRoute publishedToken '
    + 'publishedWorkspace renderGiven hosted bootAdmitted bootRefused bootRetry queuedServerContext reconcileForToken reconcileSeq sessionSeq '
    + 'sessionChangeSeq adoptedSessionTurn switchCommitMark switchOp switchPending switchSettling switchWaiting writeFence fenceReleasedOp '
    + 'writeCommits writesInFlight pendingWorkspace workspaceGeneration dialogEpoch dialogOpenSeq dialogOpener dialogRoute lastToast toastLow',
};
const words = s => s.split(/\s+/).filter(Boolean);
test('DEMO2M-R3 guard: every EW.current field is classified as person, workspace or global, exactly once', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  const classes = { 'person (clear)': words(CURRENT_FIELDS.person.clear), 'person (adopt)': words(CURRENT_FIELDS.person.adopt),
    workspace: words(CURRENT_FIELDS.workspace), global: words(CURRENT_FIELDS.global) };
  const seen = new Map();
  for (const [cls, keys] of Object.entries(classes)) for (const k of keys) {
    assert.ok(!seen.has(k), `${k} is classified twice (${seen.get(k)} and ${cls})`);
    seen.set(k, cls);
  }
  const fields = new Set(Object.keys(E.current));
  for (const st of E.retainedStoreList()) for (const f of st.fields) fields.add(f);
  for (const f of ['app.js', 'views.js', 'saved.js', 'ui.js']) {
    const code = fs.readFileSync(path.join(APP, f), 'utf8');
    for (const m of code.matchAll(/(?:E\.)?\bcurrent\.([A-Za-z_]\w*)\s*=[^=]/g)) fields.add(m[1]);
    for (const m of code.matchAll(/\bE\.current\.([A-Za-z_]\w*)/g)) fields.add(m[1]);
  }
  const unclassified = [...fields].filter(k => !seen.has(k)).sort();
  assert.deepEqual(unclassified, [], 'unclassified EW.current fields (person, workspace or global?): ' + unclassified.join(' '));
});
test('Plan A guard: a workspace or person change never carries the excerpt level over', async () => {
  for (const clear of ['clearWorkspaceState', 'clearPersonState']) {
    const t = await inviteSendUncertain();
    const E = t.win.EW;
    E.current.workspaceContext = 'ctx-same';
    E.current.excerptOnly = true;
    E.current.excerptOnlyContext = 'ctx-same';
    assert.equal(E.excerptOnly ? E.excerptOnly() : (E.current.excerptOnly === true), true, 'precondition');
    if (clear === 'clearWorkspaceState') E.clearWorkspaceState({ boundary: false }); else E.clearPersonState();
    // Even if the next workspace or person gets the same context string,
    // the previous excerpt level is gone.
    E.current.workspaceContext = 'ctx-same';
    assert.notEqual(E.current.excerptOnly, true, `${clear} keeps excerptOnly`);
    assert.ok(!E.current.excerptOnlyContext, `${clear} keeps excerptOnlyContext`);
  }
});
test('DEMO2M-R3 guard: clearPersonState resets every person (clear) field', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  assert.equal(typeof E.clearPersonState, 'function', 'no EW.clearPersonState');
  const keys = words(CURRENT_FIELDS.person.clear);
  for (const k of keys) E.current[k] = { sentinel: k };
  E.clearPersonState();
  const kept = keys.filter(k => E.current[k] && E.current[k].sentinel === k);
  assert.deepEqual(kept, [], 'clearPersonState kept: ' + kept.join(' '));
});

/* -- DEMO2M-R4-01 / R4-02 (merged-head review round 4): on every
   person-change path (another person directly, or signed out and then
   another person) the workspace cleanup runs too, and the guard proves
   it by behaviour: a marker in every person and workspace field is gone
   afterwards. -- */
const PASSAGE_REF = 'sr1:r4r4r4r4r4r4r4r4r4r4r4r4r4r4r4r4';
async function searchedThenSettings() {
  const t = await inviteSendUncertain();
  const passage = { ref: PASSAGE_REF, canonical_source_ref: PASSAGE_REF, record_kind: 'transcript_passage', status: 'finding', review: { judgment: null, bounds_verified: 0 },
    attribution: [], relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 240, end_s: 250 },
    episode: { episode_id: 'ep_r4', show_title: 'Up First', ep_title: 'Tuesday', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 14 },
    show: { show_identity_id: 'cs-upfirst', display_title: 'Up First', unresolved: 0 }, media: { playback: 'remote', alignment: 'unknown' },
    window: { segments: [{ segment_id: 50, start_s: 240, end_s: 250, text: 'Our secretword sponsor today.', text_start: 0, text_end: 29 }], text_complete: true, next_cursor: null } };
  const inner = t.win.fetch;
  t.win.fetch = async (url, opts = {}) => (url === '/api/v1/customer/records/' + encodeURIComponent(PASSAGE_REF)
    ? json({ ok: true, record: passage, workspace_context: t.win.EW.current.workspaceContext })
    : inner(url, opts));
  t.go = async route => { t.win.location.hash = route; t.win.EW.current.route = route; await t.win.EWViews.render(); await settleAll(); };
  await t.go('#/search?q=secretword');
  assert.equal((t.win.EW.current.filters || {}).q, 'secretword', 'fixture: the owner searched');
  await t.go('#/settings');
  return t;
}
const passageClean = async (t, why) => {
  await t.go('#/evidence/' + encodeURIComponent(PASSAGE_REF));
  assert.match(t.root.innerHTML, /data-action="alert-evidence"[^>]*data-kind="passage"/, `fixture (${why}): the passage page rendered`);
  assert.doesNotMatch(t.root.innerHTML, /data-q="secretword"/, `${why}: Create alert carries the previous person's search words`);
  assert.doesNotMatch(t.root.innerHTML, /<mark>secretword<\/mark>/, `${why}: the previous person's search words are marked`);
};
test('DEMO2M-R4-01: signed out, then another person: the previous person\'s search words are not kept, marked or prefilled', async () => {
  const t = await searchedThenSettings();
  await t.as('out');
  assert.equal(t.win.EW.current.principal, null, 'fixture: signed out');
  await t.as(OTHER_PERSON);
  assert.equal(t.win.EW.current.principal, 'other', 'fixture: the other person is adopted');
  assert.equal((t.win.EW.current.filters || {}).q || '', '', 'the previous person\'s search words survived');
  await passageClean(t, 'signed out, then another person');
});
test('DEMO2M-R4-01 control: another person directly: the search words go too', async () => {
  const t = await searchedThenSettings();
  await t.as(OTHER_PERSON);
  assert.equal((t.win.EW.current.filters || {}).q || '', '');
  await passageClean(t, 'another person directly');
});
test('DEMO2M-R4-01 control: the same person back after signing out keeps their search words', async () => {
  const t = await searchedThenSettings();
  await t.as('out');
  await t.as({});
  assert.equal(t.win.EW.current.principal, 'sam');
  assert.equal((t.win.EW.current.filters || {}).q, 'secretword', 'the same person lost their own search words');
});
test('DEMO2M-R4 control: the same person re-read with no workspace context keeps their retained views and context', async () => {
  const t = await inviteSendUncertain();
  const E = t.win.EW;
  E.current.savedNote = { sentinel: true };
  await t.as({ workspace_context: '' });
  assert.equal(E.current.principal, 'sam');
  assert.equal(E.current.workspaceContext, 'ctx-a', 'the same person\'s context was dropped');
  assert.ok(E.current.savedNote && E.current.savedNote.sentinel, 'the same person lost their retained views');
});

// Person and workspace fields that may keep a marker across a person
// change, each for a stated reason. Everything else must lose it.
const SWEEP_KEPT = {
  selection: 'keyed by the workspace context and revision; the context hashes the principal, so it never matches for another person',
  settingsSaved: 'keyed by the workspace context, which hashes the principal',
  inviteSendOff: 'a workspace fact from the server (sending is off), not a person\'s action',
  campaignLinkageFilter: 'a per-campaign view setting, no record data',
  roleChange: 'two workspace context strings, compared only with the current context (which hashes the principal)',
  cohortRefresh: 'a one-shot "refresh the cohort" flag for the next Search render, no data',
  displayedRevision: 'the workspace dataset\'s revision id, no person data; every Search render replaces it',
  dialogWorkspace: 'the context a dialog opened in, compared only with the current context',
  dialogWorkspaceName: 'a workspace id, read only beside held dialog drafts, which a person change drops',
};
const typeOf = v => Object.prototype.toString.call(v).slice(8, -1);
// A marker that fits the field's type: an entry in a Map or Set, a
// property on an object, or a replaced value.
function plant(E, k) {
  const v = E.current[k];
  switch (typeOf(v)) {
    case 'Map': v.set('r4-marker', 'R4-MARKER'); return () => typeOf(E.current[k]) === 'Map' && E.current[k].has('r4-marker');
    case 'Set': v.add('r4-marker'); return () => typeOf(E.current[k]) === 'Set' && E.current[k].has('r4-marker');
    case 'Array': v.push('R4-MARKER'); return () => Array.isArray(E.current[k]) && E.current[k].includes('R4-MARKER');
    case 'Object': v.r4Marker = true; return () => !!E.current[k] && E.current[k].r4Marker === true;
    case 'String': E.current[k] = 'R4-MARKER'; return () => E.current[k] === 'R4-MARKER';
    case 'Number': E.current[k] = 987654; return () => E.current[k] === 987654;
    default: E.current[k] = { r4Marker: true }; return () => !!E.current[k] && E.current[k].r4Marker === true;
  }
}
for (const [label, path] of [['another person directly', [OTHER_PERSON]], ['signed out, then another person', ['out', OTHER_PERSON]]]) {
  test(`DEMO2M-R4-02 sweep (${label}): every person and workspace field loses its marker, except the named workspace-keyed ones`, async () => {
    const t = await searchedThenSettings();
    const E = t.win.EW;
    const keys = [...words(CURRENT_FIELDS.person.clear), ...words(CURRENT_FIELDS.workspace)];
    const planted = keys.map(k => [k, plant(E, k)]);
    for (const step of path) await t.as(step);
    assert.equal(E.current.principal, 'other', 'fixture: the other person is adopted');
    const kept = planted.filter(([k, still]) => still() && !(k in SWEEP_KEPT)).map(([k]) => k);
    assert.deepEqual(kept, [], `${label}: the previous person's state survived in: ${kept.join(' ')}`);
  });
}
// Whatever a field is classified as: after a person change, no EW.current
// field anywhere holds the previous person's search words. (A per-person
// field filed as global still fails here.)
function holding(value, needle, seen = new Set(), depth = 0) {
  if (value == null || depth > 6) return false;
  if (typeof value === 'string') return value.includes(needle);
  if (typeof value !== 'object' || seen.has(value)) return false;
  seen.add(value);
  const t = typeOf(value);
  const parts = t === 'Map' ? [...value.keys(), ...value.values()] : t === 'Set' || Array.isArray(value) ? [...value]
    : Object.keys(value).filter(k => k !== 'ownerDocument' && k !== 'parentNode').map(k => { try { return value[k]; } catch { return null; } });
  return parts.some(p => holding(p, needle, seen, depth + 1));
}
for (const [label, path] of [['another person directly', [OTHER_PERSON]], ['signed out, then another person', ['out', OTHER_PERSON]]]) {
  test(`DEMO2M-R4-02 sweep (${label}): no EW.current field, in any class, keeps the previous person's search words`, async () => {
    const t = await searchedThenSettings();
    const E = t.win.EW;
    const before = Object.keys(E.current).filter(k => holding(E.current[k], 'secretword'));
    assert.ok(before.includes('filters') && before.includes('searchInspect'), 'fixture: the search words are held: ' + before.join(' '));
    for (const step of path) await t.as(step);
    assert.equal(E.current.principal, 'other', 'fixture: the other person is adopted');
    const kept = Object.keys(E.current).filter(k => holding(E.current[k], 'secretword'));
    assert.deepEqual(kept, [], `${label}: the previous person's search words are kept in: ${kept.join(' ')}`);
  });
}
