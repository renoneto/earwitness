/* Astra review 2026-10-18, app UI findings AU-01..AU-04. Each test replays
   the finding's sequence on the production scripts in a VM with mocked
   requests (no server, no network).
   Run: node --test tests/frontend_review_fixes_ui.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');

/* The operator app (app.js + views.js) over a stub DOM: every element is a
   plain object kept by id, so handlers set on it can be called. */
function operator(hash = '#/overview') {
  const els = {};
  const el = (id) => els[id] ||= { id, innerHTML: '', textContent: '', disabled: false, dataset: {},
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {}, append() {},
    classList: { add() {}, remove() {}, toggle() {} } };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document: { getElementById: el, querySelectorAll: () => [], querySelector: () => null, addEventListener() {} },
    sessionStorage: store, localStorage: store, window: { scrollTo() {}, scrollY: 0 }, location: { hash },
    history: { replaceState() {} }, setTimeout, clearTimeout, AbortController, URLSearchParams, console,
    fetch: async () => { throw new Error('network forbidden'); } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, file), 'utf8'), ctx, { filename: file });
  }
  return { ctx, el, get: (expression) => vm.runInContext(expression, ctx) };
}

/* AU-04 */
test('AU-04 a failed creatives load shows the error and Retry, never "No creatives yet."', async () => {
  const t = operator('#/creatives');
  t.get('App').csrf = 'test';
  let fail = true;
  t.ctx.fetch = async (url) => {
    assert.equal(url, '/api/v1/creatives?limit=50');
    if (fail) return { ok: false, status: 503, json: async () => ({ ok: false, error: 'Service <b>unavailable</b>' }) };
    return { ok: true, status: 200, json: async () => ({ creatives: [] }) };
  };
  await t.get('render')();
  const failed = t.el('view').innerHTML;
  assert.doesNotMatch(failed, /No creatives yet\./);
  assert.match(failed, /role="alert">Service &lt;b&gt;unavailable&lt;\/b&gt;</);
  assert.match(failed, /<button id="retry">Retry<\/button>/);
  fail = false;
  await t.el('retry').onclick();
  assert.match(t.el('view').innerHTML, /No creatives yet\./);
  assert.doesNotMatch(t.el('view').innerHTML, /role="alert"/);
});

/* The Suggested brands panel host (id ar-suggest) over the operator app:
   buttons parsed from its markup, POSTs held until the test resolves them. */
const SG = (over = {}) => ({ id: 1, key: 'century', state: 'pending', status: 'pending', verdict: 'brand', company: 'Sentry',
  official_domain: 'sentry.io', heard_correct: false, fetch_date: '2026-10-18T00:00:00+00:00', confidence: 0.92,
  evidence: 'Sentry is an error tracking company.', self_promo: null, ads: 2, shows: ['Show A'], reads_not_saying: 0,
  excerpt: 'sponsored by Century dot io', rules: [], decided_by: null, ...over });
function suggestPanel(list, hash = '#/ad-review') {
  const t = operator(hash);
  let html = '', buttons = [];
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\sdata-${name}="([^"]*)"`)); return m ? m[1] : undefined; };
  const host = t.el('ar-suggest');
  Object.defineProperty(host, 'innerHTML', { get: () => html, set: (v) => {
    html = v;
    buttons = [...String(v).matchAll(/<button\b[^>]*>[^<]*<\/button>/g)].map((m) => {
      const tag = m[0];
      return { tag, disabled: /\sdisabled(\s|>|=)/.test(tag), dataset: { sg: attr(tag, 'sg'), id: attr(tag, 'id'),
        verdict: attr(tag, 'verdict'), company: attr(tag, 'company'), heardCorrect: attr(tag, 'heard-correct'),
        fetchDate: attr(tag, 'fetch-date'), status: attr(tag, 'status') } };
    });
  } });
  host.querySelectorAll = (sel) => (sel === 'button[data-sg]' ? buttons.filter((b) => b.dataset.sg) : []);
  const posts = [], held = [];
  t.ctx.fetch = (url, init = {}) => {
    if (init.method === 'POST' || init.method === 'DELETE') {
      posts.push(url);
      return new Promise((resolve) => held.push({ url, answer: (body) => resolve({ ok: true, status: 200, json: async () => body }) }));
    }
    if (url === '/api/v1/admin/brand-suggestions') return Promise.resolve({ ok: true, status: 200, json: async () => ({ suggestions: list }) });
    return Promise.reject(new Error(`unexpected ${url}`));
  };
  t.get('App').csrf = 't';
  t.get('AdReview').reloadRules = async () => true;
  const button = (sg, id) => buttons.find((b) => b.dataset.sg === sg && b.dataset.id === String(id));
  const press = (sg, id) => t.get('adReviewSuggestPress')(button(sg, id));
  const answer = async (url, body) => { const h = held.find((x) => x.url === url); held.splice(held.indexOf(h), 1); h.answer(body); };
  return { ...t, host, posts, button, press, answer };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/* AU-02 / AU-03: one read-changing action at a time on Ad review (the
   last-save Undo, a suggestion's Confirm / Not this / Undo, a rule Remove,
   the review save). The page is drawn by a stand-in that shows the queue's
   current read, so the test sees what the operator would see. */
function adReviewPage(list = [SG({ id: 9, key: 'other', company: 'Other' })], hash = '#/ad-review') {
  const p = suggestPanel(list, hash);
  p.get('App').routeHash = '#/ad-review';
  p.get('App').route = 'ad-review';
  p.get('Views')['ad-review'] = async () => {
    const AdReview = p.get('AdReview');
    p.el('view').innerHTML = `read ${AdReview.items[AdReview.index]?.evidence_id}`;
  };
  const AdReview = p.get('AdReview');
  AdReview.lastSaved = { id: 'e1', label: 'confirmed Sentry', labelId: 4, listed: { evidence_id: 'e1' } };
  AdReview.items = [{ evidence_id: 'e2' }]; AdReview.index = 0;
  p.el('view').innerHTML = 'read e2';
  const undoButton = { disabled: false };
  const undo = () => p.get('adReviewUndo')(undoButton, { on: false });
  const dismiss = () => p.press('dismiss', 9);
  return { ...p, undo, dismiss, undoButton };
}
const UNDO_URL = '/api/v1/admin/review-queue/e1/undo';
const DISMISS_URL = '/api/v1/admin/brand-suggestions/9/dismiss';
const ANSWERED_UNDO = { ok: true, corpus_changed: false, message: 'Undone.' };

test('AU-02 while Undo runs, Not this is refused; Undo then draws the undone read', async () => {
  const p = adReviewPage();
  await p.get('loadAdReviewSuggest')();
  const undo = p.undo();
  p.dismiss(); await settle();
  assert.deepEqual(p.posts, [UNDO_URL], 'the suggestion action sent nothing');
  p.get('drawAdReviewSuggest')();
  assert.ok(p.button('dismiss', 9).disabled, 'suggestion actions are disabled while Undo runs');
  await p.answer(UNDO_URL, ANSWERED_UNDO);
  await undo; await settle();
  assert.equal(p.el('view').innerHTML, 'read e1', 'the undone read is shown');
  assert.equal(p.get('App.mutations'), 0);
});

test('AU-02 reversed: while Not this runs, Undo is refused; after it ends Undo works', async () => {
  const p = adReviewPage();
  await p.get('loadAdReviewSuggest')();
  const dismiss = p.dismiss();
  p.undo(); await settle();
  assert.deepEqual(p.posts, [DISMISS_URL], 'Undo sent nothing while Not this ran');
  await p.answer(DISMISS_URL, { ok: true, state: 'dismissed' });
  await dismiss; await settle();
  assert.ok(!p.button('dismiss', 9).disabled, 'the panel is enabled again');
  const undo = p.undo();
  await p.answer(UNDO_URL, ANSWERED_UNDO);
  await undo; await settle();
  assert.equal(p.el('view').innerHTML, 'read e1');
});

test('AU-03 while one suggestion action runs, the other rows are disabled and a second press sends nothing', async () => {
  const p = suggestPanel([SG(), SG({ id: 2, key: 'other', company: 'Other' })]);
  await p.get('loadAdReviewSuggest')();
  const first = p.press('confirm', 1);
  for (const [sg, id] of [['confirm', 1], ['dismiss', 1], ['confirm', 2], ['dismiss', 2]]) assert.ok(p.button(sg, id).disabled, `${sg} ${id} disabled`);
  p.press('dismiss', 2); p.press('confirm', 1); await settle();
  assert.deepEqual(p.posts, ['/api/v1/admin/brand-suggestions/1/confirm']);
  await p.answer('/api/v1/admin/brand-suggestions/1/confirm', { ok: true, state: 'confirmed', reads: 0 });
  await first; await settle();
  assert.ok(!p.button('dismiss', 2).disabled, 'enabled once it ended');
});

test('AU-02 a rule Remove and a suggestion action never run together', async () => {
  const p = adReviewPage();
  await p.get('loadAdReviewSuggest')();
  const remove = { dataset: { rule: '5' }, textContent: 'Remove', disabled: false, classList: { contains: () => false }, closest: () => null };
  const query = p.ctx.document.querySelectorAll;
  p.ctx.document.querySelectorAll = (sel) => (sel === '.ar-remove' ? [remove] : query(sel));
  p.get('wireAdReviewRules')();
  const RULE = '/api/v1/admin/rules/5';
  const counting = remove.onclick();
  p.dismiss(); await settle();
  assert.deepEqual(p.posts, [RULE], 'no suggestion action while the removal is counted');
  await p.answer(RULE, { ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'count', details: { reads: 2 } } });
  await counting; await settle();
  const dismiss = p.dismiss();
  remove.onclick(); await settle();
  assert.deepEqual(p.posts, [RULE, DISMISS_URL], 'no removal while the suggestion action runs');
  await p.answer(DISMISS_URL, { ok: true, state: 'dismissed' });
  await dismiss;
});

/* AU-01: the customer app's Save excerpt dialog, through the fake DOM and
   real openers of frontend_dv3_astra_drafts.test.cjs (its setup, loaded as is). */
function draftsHarness() {
  const source = fs.readFileSync(path.join(__dirname, 'frontend_dv3_astra_drafts.test.cjs'), 'utf8');
  const context = { require, console, __dirname, setTimeout, clearTimeout, URLSearchParams, URL, AbortController };
  vm.createContext(context);
  vm.runInContext(source.slice(0, source.indexOf('\ntest(')) + '\nthis.result = { app };', context);
  return context.result.app;
}
const ENTRIES = ['#excerpt-title', '#excerpt-note', '#excerpt-start', '#excerpt-end'];

test('AU-01 Save excerpt: entries cannot change while the save runs; the saved entry is what was typed', async () => {
  const t = await draftsHarness()();
  await t.act('save-excerpt', { ref: 'sr1:a' });
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'first title');
  let resolve, body;
  t.win.EW.api = (p, options) => { body = options.body; return new Promise((r) => { resolve = r; }); };
  t.win.EW.render = async () => {};
  const saving = t.win.EWViews.handleSubmit(f);
  assert.ok(resolve, 'the save was sent');
  for (const sel of ENTRIES) assert.equal(f.querySelector(sel).disabled, true, `${sel} is read-only while saving`);
  resolve({ item: { id: 9, revision: 1 } });
  await saving;
  assert.equal(body.title, 'first title');
  assert.equal(body.note, '');
  assert.equal(t.dom.modal.open, false);
  assert.equal(t.win.EW.current.dialogDrafts.size, 0);
});

test('AU-01 Save excerpt: a failed save gives the entries back as they were, editable', async () => {
  const t = await draftsHarness()();
  await t.act('save-excerpt', { ref: 'sr1:a' });
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'first title');
  t.type(f.querySelector('#excerpt-note'), 'a note');
  f.querySelector('#excerpt-end').disabled = true;  // a control that was already off stays off
  let reject;
  t.win.EW.api = () => new Promise((_r, j) => { reject = j; });
  const saving = t.win.EWViews.handleSubmit(f);
  assert.equal(f.querySelector('#excerpt-note').disabled, true);
  reject(Object.assign(new Error('Service unavailable'), { status: 503 }));
  await saving;
  assert.equal(t.dom.modal.open, true, 'the dialog stays open');
  for (const sel of ['#excerpt-title', '#excerpt-note', '#excerpt-start']) assert.equal(f.querySelector(sel).disabled, false, `${sel} editable again`);
  assert.equal(f.querySelector('#excerpt-end').disabled, true);
  assert.equal(f.querySelector('#excerpt-title').value, 'first title');
  assert.equal(f.querySelector('#excerpt-note').value, 'a note');
});

/* R1-03 (review round 1): the entries stay read-only until the success
   follow-up (its page redraw) has run, then come back. */
test('R1-03 entries stay read-only through the success redraw and come back after', async () => {
  const t = await draftsHarness()();
  await t.act('save-excerpt', { ref: 'sr1:a' });
  const f = t.form('save-excerpt');
  t.type(f.querySelector('#excerpt-title'), 'first title');
  let resolve;
  t.win.EW.api = () => new Promise((r) => { resolve = r; });
  const during = [];
  // Save excerpt's success starts E.render() without awaiting it: sample at its start.
  t.win.EW.render = async () => { during.push(f.querySelector('#excerpt-note').disabled); };
  const saving = t.win.EWViews.handleSubmit(f);
  resolve({ item: { id: 9, revision: 1 } });
  await saving;
  assert.deepEqual(during, [true], 'read-only while the success redraw starts');
  assert.equal(f.querySelector('#excerpt-note').disabled, false, 'given back once the save is done');
});

/* R5-03 (review round 5, a base bug): a failed Undo on the empty-queue page,
   which has no status line, still says why. */
test('R5-03 a failed Undo on the empty-queue page shows its reason', async () => {
  const p = adReviewPage([]);
  const byId = p.ctx.document.getElementById;
  p.ctx.document.getElementById = (id) => (id === 'ar-status' ? null : byId(id));
  const dialog = p.el('modal'); dialog.open = false; dialog.showModal = () => { dialog.open = true; };
  const undo = p.undo();
  await p.answer(UNDO_URL, { ok: false, error: 'server busy' });
  await undo;
  assert.ok(dialog.open, 'a dialog opens');
  assert.match(p.el('modal-body').innerHTML, /server busy/);
});
