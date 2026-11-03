/* UAT walkthrough fixes, Saved area: B-4 (earlier copy), B-5 / VL-27
   (say what was saved), V-29 (filter count), V-20 (confirm dialog label
   and danger style). Loads the real app scripts in a stubbed sandbox.
   Run: node --test tests/frontend_uat_saved.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, Intl,
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
const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });

function loadViews(fetch, route) {
  const win = loadAt(route, { fetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}

async function savedHtml(route, items, extra = {}) {
  const win = loadAt(route);
  const api = async () => ({ items, total: items.length, total_relation: 'exact', counts: { excerpt: 0, query: 0, bookmark: items.length }, next_cursor: null, ...extra });
  return win.EWSaved.render({ api, esc: win.EW.esc, link: win.EW.link, scope: {}, signal: null });
}

const EARLIER = 'This read is in an earlier copy of the episode. Your bookmark keeps the version you saw.';
const CHANGED = 'This episode has a newer transcript. Your bookmark keeps the version you saw.';

// -- B-4 -----------------------------------------------------------------
test('B-4 an earlier-copy bookmark says Earlier copy, prints its reason as is, and has its own legend', async () => {
  const html = await savedHtml('#/saved?tab=bookmarks', [
    { id: 'si-1', type: 'bookmark', title: 'Mine', revision: 1, availability: 'earlier_copy', availability_reason: EARLIER,
      target: { kind: 'episode', id: 'e1', source_ref: 'sr1:a', transcript_version_id: 3 },
      captured: { label: 'Morning brew', show_title: 'Same Title', opens: 'captured_pin' } },
  ]);
  assert.match(html, /data-tag="neutral">Earlier copy</);
  assert.doesNotMatch(html, /Changed since saved/, 'nothing changed after saving');
  assert.match(html, new RegExp(`<p class="saved-why">${EARLIER.replace(/\./g, '\\.')}</p>`));
  assert.doesNotMatch(html, /No source text is substituted/);
  assert.match(html, /Earlier copy: you saved an ad from an earlier transcript of the episode\. Nothing changed after you saved\./);
});

test('B-4 a changed bookmark keeps Changed since saved and its legend, the reason unpadded', async () => {
  const html = await savedHtml('#/saved?tab=bookmarks', [
    { id: 'si-2', type: 'bookmark', title: 'Mine', revision: 1, availability: 'source_changed', availability_reason: CHANGED,
      target: { kind: 'episode', id: 'e1', source_ref: 'sr1:a', transcript_version_id: 3 }, captured: {} },
  ]);
  assert.match(html, /data-tag="info">Changed since saved</);
  assert.match(html, /Changed since saved: the source was updated after you saved\./);
  assert.doesNotMatch(html, /Earlier copy:/);
  assert.match(html, /<p class="saved-why">This episode has a newer transcript\. Your bookmark keeps the version you saw\.<\/p>/);
});

// -- B-5 / VL-27 ------------------------------------------------------------
test('B-5 bookmark rows name what was saved, not the user title again', async () => {
  const html = await savedHtml('#/saved?tab=bookmarks', [
    { id: 'si-3', type: 'bookmark', title: 'UAT show bookmark', revision: 1, availability: 'current',
      target: { kind: 'show', id: 'sh-1' }, captured: { label: 'Crime and Coffee Couple', opens: 'current_profile' } },
    { id: 'si-4', type: 'bookmark', title: 'My pin', revision: 1, availability: 'current',
      target: { kind: 'episode', id: 'e1', source_ref: 'sr1:a', transcript_version_id: 3 },
      captured: { label: 'Morning brew', show_title: 'Same Title', opens: 'captured_pin' } },
    { id: 'si-5', type: 'bookmark', title: 'Old pin', revision: 1, availability: 'current',
      target: { kind: 'episode', id: 'e1', source_ref: 'sr1:b', transcript_version_id: 3 }, captured: { opens: 'captured_pin' } },
  ]);
  assert.match(html, /Show bookmark · Crime and Coffee Couple \(opens the current profile\)/);
  assert.doesNotMatch(html, /Show bookmark · UAT show bookmark/);
  assert.match(html, /Pinned episode · Same Title · Morning brew \(opens the transcript version you saved\)/);
  assert.match(html, /Pinned episode · opens the transcript version you saved/, 'no captured name: the old copy');
});

function noteWin(note) {
  const fetch = async (url) => {
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/mistakes/5') return json({ ok: true, workspace_context: 'ctx-ws', note });
    throw new Error('unexpected ' + url);
  };
  return fetch;
}
async function notePageHtml(note) {
  const win = loadViews(noteWin(note), '#/note/5');
  await settleAll();
  const main = { innerHTML: '', setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [], insertAdjacentHTML() {} };
  win.document.querySelector = sel => (sel === '#main' ? main : null);
  win.EW.header = () => {};
  win.location.hash = '#/note/5'; win.EW.current.route = '#/note/5';
  await win.EWViews.render(); await settleAll();
  return main.innerHTML;
}
const NOTE = { id: 5, reason: 'Check this', note_status: 'open', revision: 1, created_at: '2026-10-15T10:00:00Z', delivery_context: 'local_only', ref: 'sr1:abc' };

test('B-5 the note page names the read: brand, show, episode, date and time, then the link', async () => {
  const html = await notePageHtml({ ...NOTE, source_label: { record_kind: 'evidence', show_title: 'Same Title', ep_title: 'Morning brew', pub_date: '2026-03-10',
    span: [10, 20], brand_display: 'Lumen', brand_confirmed: false, brand_under_review: false, needs_review: false } });
  assert.match(html, /<dt>Source<\/dt><dd>Lumen · Same Title · Morning brew · Mar 10 · 0:10–0:20 <a [^>]*href="#\/evidence\/sr1%3Aabc"[^>]*>Open the source →<\/a>/);
});

test('B-5 the note page never names a brand under review, and says when none is named', async () => {
  const hidden = await notePageHtml({ ...NOTE, source_label: { record_kind: 'evidence', show_title: 'Same Title', ep_title: 'Morning brew', pub_date: null,
    span: [10, 20], brand_display: null, brand_confirmed: false, brand_under_review: true, needs_review: true } });
  assert.match(hidden, /<dd>Brand not identified · Same Title · Morning brew · 0:10–0:20 <a /);
  const unnamed = await notePageHtml({ ...NOTE, source_label: { record_kind: 'evidence', show_title: 'Same Title', ep_title: 'Morning brew', pub_date: null,
    span: [null, null], brand_display: null, brand_confirmed: false, brand_under_review: false, needs_review: false } });
  assert.match(unnamed, /<dd>Brand not identified · Same Title · Morning brew <a /);
  const legacy = await notePageHtml({ ...NOTE });
  assert.match(legacy, /<dt>Source<\/dt><dd><a [^>]*>Open the source →<\/a>/, 'no label: the link alone');
});

// -- V-29 ------------------------------------------------------------------
test('V-29 the filter count reads 1 saved item matches / 2 saved items match', async () => {
  const item = n => ({ id: 'si-' + n, type: 'excerpt', title: 'x ' + n, revision: 1, availability: 'current', captured: { text: 't' } });
  const one = await savedHtml('#/saved?tab=excerpts&q=mvai', [item(1)]);
  assert.match(one, /<p class="saved-count">1 saved item matches “mvai”<\/p>/);
  const two = await savedHtml('#/saved?tab=excerpts&q=mvai', [item(1), item(2)]);
  assert.match(two, /<p class="saved-count">2 saved items match “mvai”<\/p>/);
});

// -- V-20 ------------------------------------------------------------------
function dialogWin() {
  const win = loadViews(async () => { throw new Error('no network'); }, '#/saved');
  const modal = { innerHTML: '', open: true, querySelector: () => null, setAttribute() {} };
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {}, contains: () => false } };
  win.document.querySelector = sel => (sel === '#modal' ? modal : dummy);
  win.EW.openDialog = () => {};
  win.EW.current.activeWorkspace = 'ws';
  return { win, modal };
}
const el = dataset => ({ dataset, disabled: false, closest: () => null, getAttribute: () => null });

test('V-20 Saved Remove names the item and confirms with a red Remove', async () => {
  const html = await savedHtml('#/saved?tab=excerpts', [{ id: 'si-9', type: 'excerpt', title: 'UAT remove <b>', revision: 4, availability: 'current', captured: { text: 't' } }]);
  assert.match(html, /data-action="delete-saved"[^>]*data-title="UAT remove &lt;b&gt;"/);
  const { win, modal } = dialogWin();
  await win.EWViews.handleAction(el({ action: 'delete-saved', id: 'si-9', rev: '4', title: 'UAT remove <b>' }));
  assert.match(modal.innerHTML, /<h2 id="dialog-title">Remove “UAT remove &lt;b&gt;”\?<\/h2>/);
  assert.match(modal.innerHTML, /<button class="button danger armed" type="submit" autofocus>Remove<\/button>/);
  assert.doesNotMatch(modal.innerHTML, />Confirm</);
});

test('V-20 Remove member and Stop alert get the danger style; other confirmations stay primary Confirm', async () => {
  const { win, modal } = dialogWin();
  await win.EWViews.handleAction(el({ action: 'member-remove', member: 'm-1', name: 'Ana' }));
  assert.match(modal.innerHTML, /<button class="button danger armed" type="submit" autofocus>Remove<\/button>/);
  // Stop alert opens on Cancel (LEDGER-LEAD3-ALERTS-3 B), like Delete alert.
  await win.EWViews.handleAction(el({ action: 'stop-alert', id: '3', rev: '2' }));
  assert.match(modal.innerHTML, /<button class="button" type="button" data-action="close-dialog" autofocus>Cancel<\/button>/);
  assert.match(modal.innerHTML, /<button class="button danger armed" type="submit">Stop alert<\/button>/);
  await win.EWViews.handleAction(el({ action: 'backfill-alert', id: '3' }));
  assert.match(modal.innerHTML, /<button class="button primary" type="submit" autofocus>Confirm<\/button>/);
});

test('V-20 a failed freeze restores the button label it had', async () => {
  const win = loadViews();
  win.EW.saveNotice = () => {};
  win.EW.formError = () => {};
  win.EW.api = async () => { throw Object.assign(new Error('boom'), { status: 500 }); };
  win.FormData = class { *entries() { yield ['confirm_action', 'freeze-report']; yield ['confirm_id', '2']; yield ['freeze_pins', '{}']; yield ['freeze_revision', '2']; } };
  const submitBtn = { disabled: false, textContent: 'Freeze report' };
  const form = { getAttribute: () => 'confirm', querySelector: sel => (sel === '[type=submit]' ? submitBtn : null), querySelectorAll: () => [] };
  await win.EWViews.handleSubmit(form);
  assert.equal(submitBtn.textContent, 'Freeze report');
  assert.equal(submitBtn.disabled, false);
});
