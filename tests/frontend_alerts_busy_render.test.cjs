'use strict';
/* Check now busy line (LEDGER-LEAD3-ALERTS-3 D) with the real render: the
   slot and the Check now button belong to the page drawn at query time, so
   the order redraw, gone(), then the lookup is pinned. From code review r1
   of the alert strings.
   Run: node --test tests/frontend_alerts_busy_render.test.cjs */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'); const path = require('node:path'); const vm = require('node:vm');
const APP = path.join(__dirname, '..', 'app');
const FIX = path.join(__dirname, 'fixtures', 'alert-view');
const fixture = n => JSON.parse(fs.readFileSync(path.join(FIX, n + '.json'), 'utf8'));
const AID = 'ma_111111111111111111111111';
const flush = () => new Promise(r => setTimeout(r, 0));
async function page(alertName, { slowRead } = {}) {
  const store = {};
  const sb = { console, URLSearchParams, AbortController, location: { hash: '#/overview' }, history: { replaceState() {} }, scrollTo() {},
    fetch: async url => { if (url === '/api/v1/session') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) }; throw new Error('no net'); },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t } };
  sb.window = sb; sb.globalThis = sb; vm.createContext(sb);
  for (const f of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, f), 'utf8'), sb, { filename: f });
  const win = sb; win.EW.current.route = '#/overview'; win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy; win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flush();
  // Each draw of #main gets its own slot; a write to an old draw's slot is lost.
  let drawn = 0; let mainHtml = '';
  const main = { get innerHTML() { return mainHtml; }, set innerHTML(v) { mainHtml = v; drawn += 1; } };
  const slots = new Map();
  win.document.querySelector = sel => {
    if (sel === '#main') return main;
    if (sel === '#meaning-progress') { if (!mainHtml.includes('id="meaning-progress"')) return null; if (!slots.has(drawn)) slots.set(drawn, { innerHTML: '' }); return slots.get(drawn); }
    if (sel === '[data-action="meaning-check"]') { const m = /<button[^>]*data-action="meaning-check"[^>]*>/.exec(mainHtml); return m ? { disabled: / disabled[ >]/.test(m[0]) } : null; }
    return dummy;
  };
  win.EW.header = () => {}; win.EW.current.meaningAlertsAvailable = true; win.setTimeout = () => 0;
  let alert = fixture(alertName); const gates = [];
  win.EW.api = async (p, o) => {
    if (o && o.method === 'POST') { const e = new Error('busy'); e.status = 429; e.body = { error: { code: 'ALERT_BUSY' } }; return new Promise((_, rej) => gates.push(() => rej(e))); }
    if (p === '/customer/meaning-alerts/' + AID) { if (slowRead && slowRead.on) await new Promise(r => slowRead.hold.push(r)); return alert; }
    if (p === '/customer/alerts') return { alerts: [] };
    if (p.startsWith('/customer/meaning-alerts/runs/')) return new Promise(() => {});
    throw new Error('unexpected ' + p);
  };
  win.location.hash = '#/alert/' + AID; await win.EWViews.render();
  assert.ok(mainHtml.includes('meaning-definition'));
  const click = () => win.EWViews.handleAction({ dataset: { action: 'meaning-check', id: AID }, disabled: false, closest: () => null, getAttribute: () => null });
  return { win, slots, click, latest: () => (slots.get(drawn) || { innerHTML: '' }).innerHTML, gates, setAlert: a => { alert = a; }, html: () => mainHtml };
}
const BUSY = 'Meaning alerts are busy right now. Try again in a minute.';
test('stay on page, Check now on: busy line lands in the redrawn page', async () => {
  const p = await page('alert-active'); const c = p.click(); await flush(); p.gates.shift()(); await c; await flush();
  assert.ok(p.latest().includes(BUSY), p.latest());
});
test('failed banner (Check again): busy line lands in the redrawn page', async () => {
  const p = await page('alert-failed'); const c = p.click(); await flush(); p.gates.shift()(); await c; await flush();
  assert.ok(p.latest().includes(BUSY), p.latest());
});
test('redrawn page shows this alert checking: no busy line', async () => {
  const p = await page('alert-active'); const c = p.click(); await flush(); p.setAlert(fixture('alert-checking')); p.gates.shift()(); await c; await flush();
  assert.equal(p.latest(), '');
});
test('return visit: busy still shows', async () => {
  const p = await page('alert-active'); const c = p.click(); await flush();
  p.win.location.hash = '#/alerts'; await p.win.EWViews.render(); p.win.location.hash = '#/alert/' + AID; await p.win.EWViews.render();
  p.gates.shift()(); await c; await flush();
  assert.ok(p.latest().includes(BUSY), p.latest());
});
test('leave during the redraw: nothing written on the page shown', async () => {
  const slow = { on: false, hold: [] };
  const p = await page('alert-active', { slowRead: slow }); const c = p.click(); await flush();
  slow.on = true; p.gates.shift()(); await flush();
  assert.equal(slow.hold.length, 1, 'redraw read held');
  slow.on = false; p.win.location.hash = '#/alert/ma_222222222222222222222222';
  p.win.EW.api = (orig => async (path, o) => path === '/customer/meaning-alerts/ma_222222222222222222222222' ? { ...fixture('alert-active'), alert_id: 'ma_222222222222222222222222' } : orig(path, o))(p.win.EW.api);
  await p.win.EWViews.render();
  slow.hold.shift()(); await c; await flush();
  assert.ok(p.html().includes('ma_222222222222222222222222'));
  assert.equal(p.latest(), '', 'no busy line on the other alert page');
});
