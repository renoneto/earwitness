/* EXPORT-UX (lead-approved A+B): honest progress and a clear cap.
   A. An export waits up to 150 s (other writes keep 30 s). While it waits
      a status line names how many results it holds, and Export all,
      Export selected and the phone Export… stay disabled; every outcome
      (success, 4xx, 5xx, timeout, a person or workspace switch) releases
      them. A timeout says only what the server guarantees.
   B. Export all over 150 results sends nothing and says why. The count is
      the displayed result total (the server's unit).
   Only fetch, timers and DOM nodes are simulated; the real EW.api,
   handleAction and syncWriteControls run.
   Run: node --test tests/frontend_export_ux.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const CAP = 'Exports are limited to 150 results. Narrow the search or select the results you need.';
const WAITING = n => `Preparing your export of ${n} results. Large exports can take up to 2 minutes.`;
// Ads tab (EUX-R1-01): the cap counts pieces of evidence; the page counts ads.
const WAITING_ADS = (n, ads) => `Preparing your export of ${n} pieces of evidence from ${ads} ads. Large exports can take up to 2 minutes.`;
const CAP_ADS = 'Exports are limited to 150 pieces of evidence. Narrow the search or select the ads you need.';
const CAP_ADS_HOLDS = (n, ads) => `This export holds ${n} pieces of evidence from ${ads} ads. ${CAP_ADS}`;
const TIMED_OUT = 'The export is still being prepared. If it finishes, it will appear in Saved › Reports. It is not saved if the collection, your workspace or your access changes first.';
const ADS = '#/search?mode=ads&q=quince';
const SESSION = { ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws',
  workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' };
const reply = (status, body) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
const flush = () => new Promise(res => setTimeout(res, 0));
const settle = async (n = 15) => { for (let i = 0; i < n; i++) await flush(); };

function loadViews(route, fetch) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: route, pathname: '/app/' }, history: { replaceState() {} }, scrollTo() {}, fetch,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  sandbox.EW.current.route = route;
  sandbox.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  sandbox.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), sandbox, { filename: 'views.js' });
  return sandbox;
}

// The search page's export controls and its status line, as DOM stand-ins.
const control = action => ({ dataset: { action }, disabled: false, title: '', closest: () => null,
  setAttribute(k) { if (k === 'disabled') this.disabled = true; }, removeAttribute(k) { if (k === 'disabled') this.disabled = false; } });
const camel = s => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
function matches(el, selector) {
  return selector.split(',').some(raw => {
    const part = raw.trim();
    let m = /^\[data-action="([^"]+)"\]$/.exec(part);
    if (m) return el.dataset.action === m[1];
    m = /^\[data-([a-z-]+)\]$/.exec(part);
    return !!m && camel(m[1]) in el.dataset;
  });
}

async function page({ total = 92, relation = '', ads = null, route = '#/search?mode=mentions&q=quince' } = {}) {
  const exports = [];
  const fetch = async (url, init = {}) => {
    if (url === '/api/v1/session') return reply(200, SESSION);
    if (url === '/api/v1/customer/research-export') {
      return new Promise((resolve, reject) => exports.push({ body: JSON.parse(init.body), resolve, reject }));
    }
    if (url === '/api/v1/customer/hang') return new Promise(() => {});
    return reply(200, { ok: true });
  };
  const win = loadViews(route, fetch);
  await settle();
  const controls = [control('export-all'), control('export-selected'), control('export-open')];
  const status = { textContent: '' };
  const all = sel => controls.filter(el => matches(el, sel));
  const main = { innerHTML: '', querySelectorAll: all, insertAdjacentHTML(_at, html) { this.innerHTML = html + this.innerHTML; } };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#export-status' ? status
    : sel === '#export-format' || sel === '#modal' ? null : all(sel)[0] || dummy);
  win.document.querySelectorAll = all;
  const toasts = [];
  win.EW.toast = m => toasts.push(m);
  win.EW.closeDialog = () => {};
  win.EW.current.displayedRevision = 'r1';
  win.EW.retain('lastSearchTotal', { route: win.EW.current.route, total, relation, ads });
  const timers = [];
  win.EW.requestTimeout.set = (fn, ms) => { const h = { fn, ms, cleared: false }; timers.push(h); return h; };
  win.EW.requestTimeout.clear = h => { if (h) h.cleared = true; };
  const held = () => controls.map(c => c.disabled);
  const run = (action = 'export-all') => win.EWViews.handleAction({ dataset: { action }, closest: () => null });
  return { win, main, controls, status, toasts, exports, timers, held, run, pending: () => timers.filter(h => !h.cleared) };
}
const assertReleased = (t, label) => {
  assert.deepEqual(t.held(), [false, false, false], `${label}: every export control is enabled again`);
  assert.equal(t.status.textContent, '', `${label}: the status line is cleared`);
};

test('EUX1 the status line names the count and the controls hold while the export waits', async () => {
  const t = await page({ total: 92 });
  const done = t.run(); await settle();
  assert.equal(t.exports.length, 1);
  assert.equal(t.status.textContent, WAITING(92));
  assert.deepEqual(t.held(), [true, true, true], 'Export all, Export selected and Export… are disabled');
  assert.ok(!t.toasts.some(m => /Preparing/.test(m)), 'no passing toast for the wait: ' + t.toasts.join(' | '));
  // A re-render meanwhile (fresh enabled controls) is held again.
  t.controls.forEach(c => { c.disabled = false; delete c.dataset.exportHeld; });
  t.win.EWViews.syncWriteControls();
  assert.deepEqual(t.held(), [true, true, true], 'a publication during the export keeps the hold');
  assert.equal(t.status.textContent, WAITING(92));
  // A second click while one runs sends nothing.
  await t.run(); await settle();
  assert.equal(t.exports.length, 1, 'one export at a time');
  t.exports[0].resolve(reply(200, { ok: true, export_id: 4, total: 92, item_count: 92, ad_count: 80 }));
  await done; await settle();
  assertReleased(t, 'success');
  assert.equal(t.toasts[t.toasts.length - 1], 'Exported 92 passages.');
});

test('EUX2 export calls wait 150 s; other writes keep 30 s', async () => {
  const t = await page();
  t.run(); await settle();
  assert.deepEqual(t.pending().map(h => h.ms), [150000]);
  const other = t.win.EW.api('/customer/hang', { method: 'PUT', body: {} }).catch(() => null);
  await settle();
  assert.deepEqual(t.pending().map(h => h.ms), [150000, 30000]);
  t.pending()[1].fn(); await other;
});

for (const [label, status, body, toast] of [
  ['4xx (the server cap)', 422, { ok: false, error: { code: 'EXPORT_TOO_LARGE', message: CAP, status: 422, details: {} } }, CAP],
  ['4xx (changed view)', 409, { ok: false, error: { code: 'VIEW_CHANGED', message: 'export view changed', status: 409 } },
    'The collection changed; refresh the results, then export again.'],
  ['5xx', 500, { ok: false, error: { code: 'INTERNAL', message: 'internal error', status: 500 } }, null],
]) {
  test(`EUX3 a ${label} answer releases the controls`, async () => {
    const t = await page();
    const done = t.run(); await settle();
    assert.deepEqual(t.held(), [true, true, true]);
    t.exports[0].resolve(reply(status, body));
    await done; await settle();
    assertReleased(t, label);
    if (toast) assert.equal(t.toasts[t.toasts.length - 1], toast);
  });
}

test('EUX4 a timeout releases the controls and says only what is guaranteed', async () => {
  const t = await page();
  const done = t.run(); await settle();
  t.pending()[0].fn();
  await done; await settle();
  assertReleased(t, 'timeout');
  // DEMO2-SCR-R1-04: the full sentence is the page notice; the toast points to it.
  assert.equal(t.toasts[t.toasts.length - 1], 'The export is still being prepared. See the note above.');
  assert.ok(!t.toasts.some(m => /could not be confirmed|failed/i.test(m)), t.toasts.join(' | '));
});

test('EUX5 a workspace switch mid-export releases the controls; the late answer holds nothing', async () => {
  const t = await page();
  const done = t.run(); await settle();
  assert.deepEqual(t.held(), [true, true, true]);
  t.win.EW.current.workspaceContext = 'ctx-other';
  t.win.EW.clearWorkspaceState();
  t.win.EWViews.syncWriteControls();
  assertReleased(t, 'workspace switch');
  t.exports[0].resolve(reply(200, { ok: true, export_id: 9, total: 3, item_count: 3 }));
  await done; await settle();
  assertReleased(t, 'late answer after the switch');
});

test('EUX6 a person switch mid-export releases the controls', async () => {
  const t = await page();
  t.win.EW.current.principal = 'sam';
  t.win.EWViews.settleIdentity();
  const done = t.run(); await settle();
  assert.deepEqual(t.held(), [true, true, true]);
  t.win.EW.current.principal = 'kim';
  t.win.EWViews.settleIdentity();
  t.win.EWViews.syncWriteControls();
  assertReleased(t, 'person switch');
  // A new export by the next person is not released by the old answer.
  t.win.EW.current.displayedRevision = 'r2';
  t.win.EW.retain('lastSearchTotal', { route: t.win.EW.current.route, total: 5, relation: '' });
  const next = t.run(); await settle();
  assert.equal(t.exports.length, 2);
  // The next person's route lost the previous search terms: the Ads tab.
  assert.equal(t.win.EW.searchCriteria().mode, 'ads');
  assert.equal(t.status.textContent, 'Preparing your export of 5 pieces of evidence. Large exports can take up to 2 minutes.');
  t.exports[0].resolve(reply(500, { ok: false, error: { code: 'INTERNAL', message: 'x' } }));
  await done; await settle();
  assert.deepEqual(t.held(), [true, true, true], 'the old answer leaves the new export held');
  t.exports[1].resolve(reply(200, { ok: true, export_id: 10, total: 5, item_count: 5 }));
  await next; await settle();
  assertReleased(t, 'the new export');
});

for (const [total, sends] of [[149, true], [150, true], [151, false]]) {
  test(`EUX7 Export all with ${total} results ${sends ? 'sends' : 'sends nothing and says why'}`, async () => {
    const t = await page({ total });
    const done = t.run(); await settle();
    assert.equal(t.exports.length, sends ? 1 : 0);
    if (sends) {
      assert.equal(t.status.textContent, WAITING(total));
      t.exports[0].resolve(reply(200, { ok: true, export_id: 1, total, item_count: total }));
    }
    await done; await settle();
    assertReleased(t, String(total));
    if (!sends) assert.equal(t.toasts[t.toasts.length - 1], CAP);
  });
}

test('EUX8 Plan A excerpt level: ads at and over the cap, mentions (no results) never capped', async () => {
  for (const [total, sends] of [[150, true], [151, false]]) {
    const t = await page({ total, ads: 100, route: ADS });
    t.win.EW.current.excerptOnly = true;
    const done = t.run(); await settle();
    assert.equal(t.exports.length, sends ? 1 : 0, String(total));
    if (sends) assert.equal(t.status.textContent, WAITING_ADS(total, 100));
    else assert.equal(t.toasts[t.toasts.length - 1], CAP_ADS_HOLDS(total, 100));
    if (sends) t.exports[0].resolve(reply(200, { ok: true, export_id: 1, total, item_count: total }));
    await done; await settle();
    assertReleased(t, `excerpt ${total}`);
  }
  const t = await page({ total: 0 });
  t.win.EW.current.excerptOnly = true;
  const done = t.run(); await settle();
  assert.equal(t.exports.length, 1, 'a plan-limited mentions export still runs (it holds no results)');
  assert.equal(t.status.textContent, WAITING(0));
  t.exports[0].resolve(reply(200, { ok: true, plan_limited: true, export_id: 2, total: 0, item_count: 0 }));
  await done; await settle();
  assertReleased(t, 'plan-limited');
});

test('EUX9 Export selected is never capped and names the selected count', async () => {
  const t = await page({ total: 400 });
  t.win.EWViews.setSelection(t.win.EW.current.route, ['sr1:a', 'sr1:b'], 'r1');
  const done = t.run('export-selected'); await settle();
  assert.equal(t.exports.length, 1);
  assert.equal(t.exports[0].body.scope, 'selected');
  assert.equal(t.status.textContent, WAITING(2));
  assert.deepEqual(t.held(), [true, true, true]);
  t.exports[0].resolve(reply(200, { ok: true, export_id: 3, total: 2, item_count: 2, ad_count: 2 }));
  await done; await settle();
  // Export selected has a selection, so it is enabled again too.
  assertReleased(t, 'selected');
});

test('EUX10 one cap constant, mirrored from the server', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  assert.deepEqual([...src.matchAll(/\bEXPORT_RESULT_CAP\s*=\s*(\d+)/g)].map(m => m[1]), ['150']);
  const py = fs.readFileSync(path.join(__dirname, '..', 'earwitness', 'app', 'customer_query.py'), 'utf8');
  assert.deepEqual([...py.matchAll(/^EXPORT_RESULT_CAP\s*=\s*(\d+)/gm)].map(m => m[1]), ['150']);
});

test('EUX11 Ads tab: more evidence than ads; the status line names both and the success counts the same ads', async () => {
  const t = await page({ total: 140, ads: 120, route: ADS });
  const done = t.run(); await settle();
  assert.equal(t.exports.length, 1);
  assert.equal(t.status.textContent, WAITING_ADS(140, 120));
  t.exports[0].resolve(reply(200, { ok: true, export_id: 4, total: 140, item_count: 140, ad_count: 120 }));
  await done; await settle();
  assertReleased(t, 'ads success');
  assert.equal(t.toasts[t.toasts.length - 1], 'Exported 120 ads.');
});

test('EUX12 Ads tab: the cap counts evidence, and the refusal says so beside the ads shown', async () => {
  const over = await page({ total: 180, ads: 120, route: ADS });
  await over.run(); await settle();
  assert.equal(over.exports.length, 0, '120 ads but 180 pieces of evidence: nothing is sent');
  assert.equal(over.toasts[over.toasts.length - 1], CAP_ADS_HOLDS(180, 120));
  const at = await page({ total: 150, ads: 149, route: ADS });
  const done = at.run(); await settle();
  assert.equal(at.exports.length, 1);
  at.exports[0].resolve(reply(422, { ok: false, error: { code: 'EXPORT_TOO_LARGE', message: CAP_ADS, status: 422 } }));
  await done; await settle();
  assert.equal(at.toasts[at.toasts.length - 1], CAP_ADS, 'the server refusal reads as sent');
});

test('EUX13 a person switch A to B to A mid-export releases the controls', async () => {
  const t = await page();
  t.win.EW.current.principal = 'sam';
  t.win.EWViews.settleIdentity();
  const done = t.run(); await settle();
  t.win.EW.current.principal = 'kim';
  t.win.EWViews.settleIdentity();
  t.win.EW.current.principal = 'sam';
  t.win.EWViews.settleIdentity();
  t.win.EWViews.syncWriteControls();
  assertReleased(t, 'A to B to A');
  t.exports[0].resolve(reply(200, { ok: true, export_id: 9, total: 3, item_count: 3 }));
  await done; await settle();
  assertReleased(t, 'late answer after A to B to A');
});

test('EUX14 a context that moved without a cleanup holds nothing', async () => {
  const t = await page();
  const done = t.run(); await settle();
  t.win.EW.current.workspaceContext = 'ctx-other';
  t.win.EWViews.syncWriteControls();
  assertReleased(t, 'moved context');
  t.exports[0].resolve(reply(200, { ok: true, export_id: 9, total: 3, item_count: 3 }));
  await done; await settle();
});

test('EUX15 Export selected stays disabled when the selection is emptied and re-checked mid-export', async () => {
  const t = await page();
  const route = t.win.EW.current.route;
  t.win.EWViews.setSelection(route, ['sr1:a'], 'r1');
  const done = t.run('export-selected'); await settle();
  t.win.EWViews.setSelection(route, [], 'r1');
  assert.equal(t.controls[1].disabled, true);
  t.win.EWViews.setSelection(route, ['sr1:b'], 'r1');
  assert.equal(t.controls[1].disabled, true, 're-checked mid-export: still held');
  t.exports[0].resolve(reply(200, { ok: true, export_id: 5, total: 1, item_count: 1 }));
  await done; await settle();
  assertReleased(t, 'after the export');
});

test('EUX16 a switch that waits behind an export says so', async () => {
  const t = await page();
  const done = t.run(); await settle();
  t.win.EW.current.switchWaiting = 1;
  t.win.EWViews.syncWriteControls();
  assert.match(t.main.innerHTML, /Finishing your export before switching workspace…/);
  t.win.EW.current.switchWaiting = 0;
  t.exports[0].resolve(reply(200, { ok: true, export_id: 5, total: 1, item_count: 1 }));
  await done; await settle();
  t.main.innerHTML = '';
  t.win.EW.current.switchWaiting = 1;
  t.win.EWViews.syncWriteControls();
  assert.match(t.main.innerHTML, /Finishing your change before switching workspace…/, 'no export: the usual words');
  t.win.EW.current.switchWaiting = 0;
});

// ---- code review r2 ---------------------------------------------------------

test('EUX17 Ads tab: the Export dialog counts pieces of evidence beside the ads shown', async () => {
  const t = await page({ total: 1234, ads: 120, route: ADS });
  let body = '';
  t.win.EW.openDialog = (_title, html) => { body = html; };
  await t.win.EWViews.handleAction({ dataset: { action: 'export-open' }, closest: () => null });
  assert.match(body, /All matches <span class="meta" id="export-all-count">\(1,234 pieces of evidence from 120 ads\)<\/span>/);
  const low = await page({ total: 180, ads: 120, relation: 'at least ', route: ADS });
  low.win.EW.openDialog = (_title, html) => { body = html; };
  await low.win.EWViews.handleAction({ dataset: { action: 'export-open' }, closest: () => null });
  assert.match(body, /All matches <span class="meta" id="export-all-count">\(at least 180 pieces of evidence from at least 120 ads\)<\/span>/);
  const other = await page({ total: 12 });
  other.win.EW.openDialog = (_title, html) => { body = html; };
  await other.win.EWViews.handleAction({ dataset: { action: 'export-open' }, closest: () => null });
  assert.match(body, /All matches <span class="meta" id="export-all-count">\(12 results\)<\/span>/);
});

test('EUX18 a lower-bound count says "at least" in the status line and the cap toast', async () => {
  const over = await page({ total: 180, ads: 120, relation: 'at least ', route: ADS });
  await over.run(); await settle();
  assert.equal(over.exports.length, 0);
  assert.equal(over.toasts[over.toasts.length - 1],
    `This export holds at least 180 pieces of evidence from at least 120 ads. ${CAP_ADS}`);
  const under = await page({ total: 40, ads: 30, relation: 'at least ', route: ADS });
  const done = under.run(); await settle();
  assert.equal(under.status.textContent,
    'Preparing your export of at least 40 pieces of evidence from at least 30 ads. Large exports can take up to 2 minutes.');
  under.exports[0].resolve(reply(409, { ok: false, error: { code: 'VIEW_CHANGED', message: 'partial', status: 409 } }));
  await done; await settle();
  const results = await page({ total: 151, relation: 'at least ' });
  await results.run(); await settle();
  assert.equal(results.toasts[results.toasts.length - 1], CAP);
});

test('EUX19 Ads tab: left-out rows are named as pieces of evidence in the success toast', async () => {
  const t = await page({ route: ADS });
  const text = t.win.EWViews.exportedText;
  assert.equal(text({ total: 5, ad_count: 4, withheld: 1 }, 'ads'),
    'Exported 4 ads. 1 piece of evidence containing transcript text was left out.');
  assert.equal(text({ total: 5, ad_count: 4, withheld: 3 }, 'ads'),
    'Exported 4 ads. 3 pieces of evidence containing transcript text were left out.');
  assert.equal(text({ total: 5, withheld: 2 }, 'mentions'),
    'Exported 5 passages. 2 results containing transcript text were left out.');
});

test('EUX20 the workspace chooser says it waits for the export', async () => {
  const hosted = (over = {}) => ({ ok: true, authenticated: true, principal: 'sam', csrf_token: 'c'.repeat(32),
    active_workspace: 'ws-a', workspaces: ['ws-a', 'ws-b'], workspace_context: 'ctx-a',
    workspace_switch_revision: 5, frontend_contract: 'earwitness/customer-ui/2', ...over });
  const exports = []; let switched = false;
  const fetch = async (url, init = {}) => {
    if (url === '/api/v1/session/workspace') { switched = true; return reply(200, hosted({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 })); }
    if (url === '/api/v1/session') return reply(200, switched ? hosted({ active_workspace: 'ws-b', workspace_context: 'ctx-b', workspace_switch_revision: 6 }) : hosted());
    if (url === '/api/v1/customer/research-export') return new Promise(res => exports.push(res));
    return reply(200, { ok: true, workspace_context: 'ctx-a', preferences: { workspace_name: 'Alpha', revision: 1 } });
  };
  const win = loadViews(ADS, fetch); await settle(30);
  const main = { innerHTML: '', querySelectorAll: () => [], insertAdjacentHTML() {} };
  const status = { textContent: '' };
  const choice = { dataset: {}, getAttribute: a => (a === 'id' ? 'workspace-choice' : null), closest: () => null, contains: () => false,
    querySelector: s => (s === '[data-switch-status]' ? status : s === '[type=submit]' ? { disabled: false, textContent: 'Use workspace' } : null),
    querySelectorAll: () => [], elements: [] };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.document.querySelectorAll = () => [];
  win.EW.header = () => {}; win.scrollTo = () => {}; win.EW.toast = () => {}; win.EW.saveNotice = () => {}; win.EW.closeDialog = () => {};
  win.EW.render = () => {};
  win.FormData = class { *entries() { yield ['workspace_id', 'ws-b']; } };
  win.EW.current.displayedRevision = 'r1';
  win.EW.current.displayedWorkspaceSwitchRevision = 5;
  win.EW.retain('lastSearchTotal', { route: win.EW.current.route, total: 3, relation: '', ads: 2 });
  const done = win.EWViews.handleAction({ dataset: { action: 'export-all' }, closest: () => null }); await settle();
  assert.equal(exports.length, 1, 'the export is in flight');
  const pc = win.EWViews.handleSubmit(choice); await settle(5);
  assert.equal(status.textContent, 'Finishing your export…');
  exports[0](reply(200, { ok: true, export_id: 1, total: 3, item_count: 3, ad_count: 2 }));
  await done; await pc; await settle(20);
});

test('EUX21 singular forms: 1 piece of evidence from 1 ad, and 1 result', async () => {
  let body = '';
  const ads = await page({ total: 1, ads: 1, route: ADS });
  ads.win.EW.openDialog = (_title, html) => { body = html; };
  await ads.win.EWViews.handleAction({ dataset: { action: 'export-open' }, closest: () => null });
  assert.match(body, /All matches <span class="meta" id="export-all-count">\(1 piece of evidence from 1 ad\)<\/span>/);
  const doneAds = ads.run(); await settle();
  assert.equal(ads.status.textContent, 'Preparing your export of 1 piece of evidence from 1 ad. Large exports can take up to 2 minutes.');
  ads.exports[0].resolve(reply(200, { ok: true, export_id: 1, total: 1, item_count: 1, ad_count: 1, withheld: 1 }));
  await doneAds; await settle();
  assert.equal(ads.toasts[ads.toasts.length - 1], 'Exported 1 ad. 1 piece of evidence containing transcript text was left out.');
  const other = await page({ total: 1 });
  other.win.EW.openDialog = (_title, html) => { body = html; };
  await other.win.EWViews.handleAction({ dataset: { action: 'export-open' }, closest: () => null });
  assert.match(body, /All matches <span class="meta" id="export-all-count">\(1 result\)<\/span>/);
  const done = other.run(); await settle();
  assert.equal(other.status.textContent, 'Preparing your export of 1 result. Large exports can take up to 2 minutes.');
  other.exports[0].resolve(reply(200, { ok: true, export_id: 2, total: 1, item_count: 1, withheld: 1 }));
  await done; await settle();
  assert.equal(other.toasts[other.toasts.length - 1], 'Exported 1 passage. 1 result containing transcript text was left out.');
});
