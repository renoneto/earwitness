/* Owner ad review page (BRAND-CONFIRM-ROUTE): app/owner/review.js.
   Loads the real script in a stubbed sandbox with a recording fetch and
   checks what it sends: the owner routes only, Confirm with the brand on
   screen, a typed brand for this read only, Not an ad, Undo of the last
   save, and the session's CSRF token and switch revision on every write.
   Run: node --test tests/frontend_owner_review.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ID = 'named:ep_fx1.named.60000-120000';
const ITEM = {
  evidence_id: ID, episode_id: 'ep_fx1', show_title: '<b>Show</b>', ep_title: 'Ep 1',
  pub_date: '2026-10-21', span: [60, 120], raw_brand: 'quints',
  read: { brand: { key: 'quince', display: 'Quince', confirmed: false } },
  transcript: [{ start: 60, end: 70, text: 'We get it at quince.', in_read: true }],
};

function load({ answers = {} } = {}) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, { id, textContent: '', innerHTML: '', value: '', dataset: {}, querySelectorAll: () => [] });
    return elements.get(id);
  };
  const listeners = {};
  const calls = [];
  const document = { getElementById: el, addEventListener: (type, fn) => { listeners[type] = fn; } };
  const fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method || 'GET', headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : undefined });
    let out = { ok: true };
    if (url === '/api/v1/session') out = { csrf_token: 'tok-1', workspace_switch_revision: 4 };
    else if (url.startsWith('/api/v1/owner/ad-review?')) out = { ok: true, items: [], total: 0, facets: {} };
    else if (url.endsWith('/action')) out = { ok: true, label_id: 7, ...(answers.action || {}) };
    else if (url.endsWith('/undo')) out = { ok: true, message: 'Back as it was.' };
    else if (url.startsWith('/api/v1/owner/ad-review/')) out = { ok: true, item: ITEM };
    return { ok: true, status: 200, json: async () => out };
  };
  const ctx = { document, fetch, location: { search: '' }, URLSearchParams, console, setTimeout };
  vm.createContext(ctx);
  const file = 'app/owner/review.js';
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  return { el, calls, boot: () => listeners.DOMContentLoaded() };
}
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r)); };
async function opened() {
  const page = load();
  await page.boot(); await settle();
  page.el('ar-id').value = ID;
  page.el('ar-open').onsubmit({ preventDefault() {} });
  await settle();
  return page;
}

test('boot reads the session, then the owner queue only', async () => {
  const page = load();
  await page.boot(); await settle();
  assert.deepEqual(page.calls.map(c => c.url), ['/api/v1/session', '/api/v1/owner/ad-review?limit=50']);
  assert.match(page.el('ar-queue').innerHTML, /Nothing waits for review/);
});

test('a record id opens that read; its words are escaped', async () => {
  const page = await opened();
  assert.equal(page.calls.at(-1).url, `/api/v1/owner/ad-review/${encodeURIComponent(ID)}`);
  const html = page.el('ar-read').innerHTML;
  assert.match(html, /&lt;b&gt;Show&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>Show/);
  assert.match(html, /Confirm Quince/);
  assert.match(html, /Type a brand \(this read only\)/);
});

test('Confirm sends the brand on screen with the CSRF token and switch revision', async () => {
  const page = await opened();
  page.el('ar-confirm').onclick();
  await settle();
  const write = page.calls.find(c => c.method === 'POST');
  assert.equal(write.url, `/api/v1/owner/ad-review/${encodeURIComponent(ID)}/action`);
  assert.deepEqual(write.body, { action: 'confirm', brand: 'quince' });
  assert.equal(write.headers['X-CSRF-Token'], 'tok-1');
  assert.equal(write.headers['X-EarWitness-Workspace-Switch-Revision'], '4');
  assert.match(page.el('ar-status').textContent, /Saved: confirmed Quince/);
});

test('a typed brand saves this read only, with the spoken name shown', async () => {
  const page = await opened();
  page.el('ar-brand').value = ' BetterHelp ';
  page.el('ar-type').onsubmit({ preventDefault() {} });
  await settle();
  const write = page.calls.find(c => c.method === 'POST');
  assert.deepEqual(write.body, { action: 'type', brand: 'BetterHelp', heard: 'quints', every_read: false });
});

test('Not an ad, then Undo sends the label of that save', async () => {
  const page = await opened();
  page.el('ar-not-ad').onclick();
  await settle();
  assert.deepEqual(page.calls.find(c => c.method === 'POST').body, { action: 'not_ad' });
  page.el('ar-undo').onclick();
  await settle();
  const undo = page.calls.filter(c => c.method === 'POST')[1];
  assert.equal(undo.url, `/api/v1/owner/ad-review/${encodeURIComponent(ID)}/undo`);
  assert.deepEqual(undo.body, { label_id: 7 });
  assert.match(page.el('ar-status').textContent, /Undone: not an ad/);
});

test('the page calls no operator or customer write route', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'app/owner/review.js'), 'utf8');
  const routes = src.match(/\/api\/v1\/[a-z0-9/-]+/g);
  assert.deepEqual([...new Set(routes)].sort(), ['/api/v1/owner/ad-review', '/api/v1/session']);
});
