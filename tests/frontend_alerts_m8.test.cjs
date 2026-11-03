/* M8 (UX audit M8 row, lead LEDGER-LEAD3-M8-1), word and meaning alerts
   alike: the approved Stop and Delete dialog strings byte for byte, Delete
   only for the maker (can_change / can_delete), Edit hidden on stopped
   alerts (list row and alert page), and "Alert deleted." as the Alerts
   list opens after a delete (the toast, as "Alert stopped."; a page notice
   would need a new EW.current field the registry guards do not list).
   Run: node --test tests/frontend_alerts_m8.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
const fixture = name => JSON.parse(fs.readFileSync(path.join(FIX, name + '.json'), 'utf8'));
const clone = o => JSON.parse(JSON.stringify(o));

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
async function renderRoute(route, api, { available = true } = {}) {
  const win = loadViews(canonicalSessionFetch());
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.EW.current.meaningAlertsAvailable = available;
  win.location.hash = route;
  await win.EWViews.render();
  return { win, html: main.innerHTML };
}
function views(available = true) {
  const win = loadViews(canonicalSessionFetch());
  win.EW.current.meaningAlertsAvailable = available;
  return win.EWViews;
}
const plain = html => html.replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'");

const STOP = { title: 'Stop this alert?', body: "You can't restart it. Its matches stay here.", button: 'Stop alert' };
const DEL = { button: 'Delete alert', title: 'Delete this alert?',
  body: 'This removes the alert and its matches for everyone in the workspace. You can\'t undo it.', after: 'Alert deleted.' };
const base = { revision: 1, definition_revision: 1, query: { mode: 'ads', q: 'x' }, last_successful_at: '2026-10-14T09:00:00Z',
  baseline_complete: true, unread_event_count: 0, events: [], delivery_json: '{}' };

function dialogWin() {
  const win = loadViews(canonicalSessionFetch());
  const modal = { innerHTML: '', open: true, querySelector: () => null, setAttribute() {} };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, contains: () => false } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : dummy);
  win.EW.openDialog = () => {};
  return { win, modal };
}
const el = dataset => ({ dataset, disabled: false, closest: () => null, getAttribute: () => null });

for (const kind of ['words', 'meaning']) {
  test(`stop dialog (${kind}): the approved title, body and danger button`, async () => {
    const { win, modal } = dialogWin();
    await win.EWViews.handleAction(el({ action: 'stop-alert', id: kind === 'words' ? '3' : 'ma_' + '1'.repeat(24), rev: '2', kind }));
    assert.match(modal.innerHTML, /<h2 id="dialog-title">Stop this alert\?<\/h2>/);
    assert.ok(modal.innerHTML.includes('<p>You can&#39;t restart it. Its matches stay here.</p>') || modal.innerHTML.includes("<p>You can't restart it. Its matches stay here.</p>"), modal.innerHTML);
    // Cancel takes the focus, like Delete (LEDGER-LEAD3-ALERTS-3 B; V-20 in tests/frontend_uat_saved).
    assert.match(modal.innerHTML, /<button class="button" type="button" data-action="close-dialog" autofocus>Cancel<\/button>/);
    assert.match(modal.innerHTML, /<button class="button danger armed" type="submit">Stop alert<\/button>/);
    assert.equal((modal.innerHTML.match(/autofocus/g) || []).length, 1);
    assert.doesNotMatch(modal.innerHTML, /Stopping retains history/);
  });

  test(`delete dialog (${kind}): the approved title, body and danger button`, async () => {
    const { win, modal } = dialogWin();
    await win.EWViews.handleAction(el({ action: 'delete-alert', id: kind === 'words' ? '3' : 'ma_' + '1'.repeat(24), kind }));
    assert.match(modal.innerHTML, /<h2 id="dialog-title">Delete this alert\?<\/h2>/);
    assert.ok(plain(modal.innerHTML).includes(DEL.body), modal.innerHTML);
    // Cancel takes the focus (CR-SCR-R1-03 cancelFirst): Enter never deletes.
    assert.match(modal.innerHTML, /<button class="button" type="button" data-action="close-dialog" autofocus>Cancel<\/button>/);
    assert.match(modal.innerHTML, /<button class="button danger armed" type="submit">Delete alert<\/button>/);
    assert.match(modal.innerHTML, new RegExp(`name="confirm_action" value="${kind === 'words' ? 'delete-alert' : 'delete-meaning-alert'}"`));
  });
}

test('delete submit (words): DELETE /customer/alerts/<id>, then the Alerts list with "Alert deleted."', async () => {
  const win = loadViews(canonicalSessionFetch());
  const calls = [];
  win.EW.api = async (p, opts) => { calls.push([p, opts && opts.method]); return { ok: true }; };
  win.EW.closeDialog = () => {};
  win.EW.saveNotice = () => {};
  const toasts = [];
  win.EW.toast = t => toasts.push(t);
  const went = [];
  win.EW.go = r => went.push(r);
  win.EW.render = () => {};
  win.FormData = class { *entries() { yield ['confirm_action', 'delete-alert']; yield ['confirm_id', '7']; } };
  const submitBtn = { disabled: false, textContent: 'Delete alert' };
  const form = { getAttribute: () => 'confirm', querySelector: sel => (sel === '[type=submit]' ? submitBtn : null), querySelectorAll: () => [] };
  await win.EWViews.handleSubmit(form);
  assert.deepEqual(calls, [['/customer/alerts/7', 'DELETE']]);
  assert.deepEqual(went, ['#/alerts']);
  assert.deepEqual(toasts, ['Alert deleted.']);
});

test('delete submit (meaning): DELETE /customer/meaning-alerts/<id>', async () => {
  const win = loadViews(canonicalSessionFetch());
  const calls = [];
  win.EW.api = async (p, opts) => { calls.push([p, opts && opts.method]); return { ok: true }; };
  win.EW.closeDialog = () => {};
  win.EW.saveNotice = () => {};
  win.EW.toast = () => {};
  win.EW.go = () => {};
  win.EW.render = () => {};
  const id = 'ma_' + '1'.repeat(24);
  win.FormData = class { *entries() { yield ['confirm_action', 'delete-meaning-alert']; yield ['confirm_id', id]; } };
  const form = { getAttribute: () => 'confirm', querySelector: () => ({ disabled: false, textContent: 'Delete alert' }), querySelectorAll: () => [] };
  await win.EWViews.handleSubmit(form);
  assert.deepEqual(calls, [['/customer/meaning-alerts/' + id, 'DELETE']]);
});

test('Edit is hidden on a stopped word alert, on the list row and the alert page', async () => {
  const alerts = [{ ...base, id: 1, name: 'Live', lifecycle: 'active_local', can_change: true },
                  { ...base, id: 2, name: 'Done', lifecycle: 'stopped', can_change: true }];
  const list = (await renderRoute('#/alerts', p => {
    if (p === '/customer/alerts') return { alerts };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  })).html;
  const row = name => { const i = list.indexOf('>' + name + '<'); const s = list.lastIndexOf('<li', i); return list.slice(s, list.indexOf('</li>', i)); };
  assert.match(row('Live'), /data-action="edit-alert"/);
  assert.doesNotMatch(row('Done'), /data-action="edit-alert"/);
  const page = (await renderRoute('#/alert/2', p => {
    if (p === '/customer/alerts/2') return { alert: alerts[1] };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  })).html;
  assert.doesNotMatch(page, /data-action="edit-alert"/);
  assert.match(page, /data-action="delete-alert"[^>]*>Delete alert</, 'a stopped alert can still be deleted by its maker');
});

test('Delete alert on the word alert page only for the maker (can_change)', async () => {
  const page = async alert => (await renderRoute('#/alert/5', p => {
    if (p === '/customer/alerts/5') return { alert };
    if (p === '/customer/shows') return { shows: [] };
    throw new Error('unexpected request ' + p);
  })).html;
  assert.match(await page({ ...base, id: 5, name: 'Mine', lifecycle: 'active_local', can_change: true }), /data-action="delete-alert"[^>]*data-id="5"[^>]*>Delete alert</);
  assert.doesNotMatch(await page({ ...base, id: 5, name: 'Theirs', lifecycle: 'active_local', can_change: false }), /delete-alert/);
});

test('Delete alert on the meaning alert page only for the maker (can_delete)', () => {
  const win = loadViews(canonicalSessionFetch());
  const V = win.EWViews;
  const alert = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'alert-view', 'alert-active.json'), 'utf8'));
  assert.match(V.meaningAlertHtml(alert), /data-action="delete-alert"[^>]*data-kind="meaning"[^>]*>Delete alert</);
  assert.match(V.meaningAlertHtml({ ...alert, lifecycle: 'stopped', banner: 'stopped' }), /delete-alert/);
  assert.doesNotMatch(V.meaningAlertHtml({ ...alert, can_delete: false, can_change: false }), /delete-alert/);
});

test('the approved M8 strings are in views.js byte for byte', () => {
  const src = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');
  for (const s of [...Object.values(STOP), ...Object.values(DEL)]) assert.ok(src.includes(s), s);
});
