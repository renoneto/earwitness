/* Astra decision 11 (T5 R2-08c): a real Undo in operator Ad review.
   After a save (never after a skip) the result line has "Undo" beside
   "Correct this read", for the last save of this session only. Undo posts
   to /api/v1/admin/review-queue/<id>/undo with the save's label id; on
   success it says what was undone and brings that read back; on
   READ_CHANGED it explains and keeps only "Correct this read".
   Page harness copied from frontend_dv3_ux_operator.test.cjs (plus .ar-undo).
   Run: node --test tests/frontend_dv3_astra_undo.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const onPage = { closest: () => null };
const ITEM = {
  evidence_id: 'e1', show_title: 'The Daily', ep_title: 'Ep 1', pub_date: '2026-10-08', span: [1893.12, 1920],
  raw_brand: 'youtube premium',
  read: { brand: { key: 'youtube premium', display: 'YouTube Premium', confirmed: false }, confidence: 'low', confidence_score: 0.704,
    candidates: [{ brand: 'youtube premium', score: 5.066, evidence: [] }, { brand: 'other name', score: 0.2, evidence: [] }] },
  transcript: [{ start: 1893.12, end: 1900, text: 'Try it.', in_read: true }],
  reports: [],
  media: null,
};
function page({ item = ITEM, onPost, rules, player = null, empty = false } = {}) {
  const posts = [], keydown = [], els = {}, renders = [];
  let buttons = [];
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`)); return m ? m[1] : undefined; };
  const parse = (html, where) => [...String(html).matchAll(/<button\b[^>]*>/g)].map((m) => {
    const tag = m[0], cls = attr(tag, 'class') || '';
    const b = { tag, where, cls, disabled: /\sdisabled(\s|>|=)/.test(tag), textContent: '', hidden: false, title: '',
      dataset: { ar: attr(tag, 'data-ar'), arPick: attr(tag, 'data-ar-pick'), brand: attr(tag, 'data-brand'), rule: attr(tag, 'data-rule'), armed: attr(tag, 'data-armed') },
      inserted: [], setAttribute(k, v) { this[k] = v; }, removeAttribute() {}, focus() {}, click() { this.onclick && this.onclick(); },
      closest(sel) { return sel === 'li' ? { insertAdjacentHTML: (pos, h) => { b.inserted.push(h); buttons = buttons.concat(parse(h, 'inserted')); } } : null; } };
    return b;
  });
  const matches = (b, sel) => sel.split(',').map((s) => s.trim()).some((s) =>
    (s === '.ar-actions button' && b.dataset.ar && b.dataset.ar !== 'replay' && !/ar-play/.test(b.cls))
    || (s === '.ar-candidates button' && b.dataset.arPick)
    || (s === '.ar-remove' && /ar-remove/.test(b.cls))
    || (s === '.ar-rules-reload' && /ar-rules-reload/.test(b.cls))
    || (s === '[data-ar]' && b.dataset.ar) || (s === '[data-ar-pick]' && b.dataset.arPick)
    || (s === '.ar-play' && /ar-play/.test(b.cls)) || (s === '.ar-undo' && /ar-undo/.test(b.cls))
    || ((/^\[data-ar="([^"]+)"\]$/.exec(s) || [])[1] === b.dataset.ar && b.dataset.ar !== undefined));
  const el = (id) => {
    if (id === 'ar-audio') return player;
    if (['ar-clock', 'ar-progress'].includes(id)) return null;
    if (!els[id]) {
      let html = '';
      els[id] = { id, textContent: '', value: '', hidden: false, disabled: false, dataset: {}, isConnected: true,
        get innerHTML() { return html; },
        set innerHTML(v) { html = v; buttons = buttons.filter((b) => b.where !== id).concat(parse(v, id)); },
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, setAttribute() {}, removeAttribute() {},
        addEventListener(type, fn) { (this.on = this.on || {})[type] = fn; }, focus() {}, select() {}, blur() {},
        querySelector: (sel) => { const m = sel.match(/^\[data-ar(-pick)?="([^"]+)"\]$/); return m ? buttons.find((b) => (m[1] ? b.dataset.arPick : b.dataset.ar) === m[2]) || null : null; },
        querySelectorAll: (sel) => buttons.filter((b) => matches(b, sel)) };
    }
    return els[id];
  };
  const json = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const RULES = rules || { rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple Card' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: { 'alias\tapplecard': 7 } } };
  const fetch = async (url, init = {}) => {
    if (init.method === 'POST' || init.method === 'DELETE') { const body = init.body ? JSON.parse(init.body) : null; posts.push({ url, method: init.method, body }); return onPost(url, body, posts.length); }
    if (url.startsWith('/api/v1/admin/review-queue?')) return json(200, { ok: true, items: empty ? [] : [{ evidence_id: (typeof item === 'function' ? item() : item).evidence_id, reasons: [] }], total: empty ? 0 : 1, facets: { shows: [], reasons: [] } });
    if (url.startsWith('/api/v1/admin/review-queue/')) return json(200, { ok: true, item: typeof item === 'function' ? item() : item });
    if (url === '/api/v1/admin/rules') return json(200, { ok: true, ...(typeof RULES === 'function' ? RULES() : RULES) });
    if (url.startsWith('/api/v1/admin/brands')) return json(200, { ok: true, brands: [] });
    throw new Error(`unexpected ${url}`);
  };
  const document = { getElementById: el, querySelector: () => null,
    querySelectorAll: (sel) => buttons.filter((b) => matches(b, sel)),
    addEventListener: (type, fn) => { if (type === 'keydown') keydown.push(fn); }, removeEventListener: (type, fn) => { const i = keydown.indexOf(fn); if (i >= 0) keydown.splice(i, 1); } };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: {}, location: { hash: '#/ad-review' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController, fetch };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  const api = vm.runInContext('({ App, AdReview, Views })', ctx);
  api.App.route = 'ad-review'; api.App.csrf = 't';
  const show = () => api.Views['ad-review']();
  ctx.render = async () => { renders.push(1); await show(); };
  const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };
  const press = async (key, extra = {}) => { const ev = { key, target: onPage, preventDefault() { ev.prevented = true; }, ...extra }; [...keydown].forEach((fn) => fn(ev)); await flush(); await flush(); return ev; };
  return { ...api, ctx, show, press, flush, posts, renders, el, json, get buttons() { return buttons; }, button: (ar) => buttons.find((b) => b.dataset.ar === ar) };
}


const SAVED = (labelId = 41) => ({ ok: true, action: 'not_ad', rules: [{ kind: 'not_ad', key: 'k' }], reads: 0, label_id: labelId, corpus_changed: false });
const undoButton = (h) => h.buttons.find((b) => /ar-undo/.test(b.cls));

test('after a save the result line has Undo beside "Correct this read"; after a skip it has neither', async () => {
  const h = page({ onPost: () => h.json(200, SAVED()) });
  await h.show();
  await h.press('n');
  const html = h.el('view').innerHTML;
  assert.match(html, /<span class="ar-last-fix"><button type="button" class="ar-undo">Undo<\/button><a href="#\/review\/e1">Correct this read<\/a><\/span>/);
  assert.doesNotMatch(html, /<a [^>]*>Undo/, 'the link is never labelled Undo');
  assert.equal(h.AdReview.lastSaved.labelId, 41);
  h.AdReview.lastSaved = null; h.AdReview.lastMessage = 'Skipped. It stays in the queue for later.';
  await h.show();
  assert.doesNotMatch(h.el('view').innerHTML, /ar-undo|Correct this read/);
});

test('Undo is for the last save only: a second save replaces it, a skip removes it', () => {
  const ctx = page({ onPost: () => ({}) });
  const run = (code) => vm.runInContext(code, ctx.ctx);
  run(`AdReview.items = [{ evidence_id: 'a' }, { evidence_id: 'b' }, { evidence_id: 'c' }]; AdReview.index = 0;
    adReviewAdvance('a', 'Saved: not an ad.', { label: 'not an ad', labelId: 1, listed: { evidence_id: 'a' } });
    adReviewAdvance('b', 'Saved: confirmed X.', { label: 'confirmed X', labelId: 2, listed: { evidence_id: 'b' } });`);
  const after = run('adReviewEmptyMarkup()');
  assert.match(after, /<button type="button" class="ar-undo">Undo<\/button><a href="#\/review\/b">/);
  assert.doesNotMatch(after, /#\/review\/a/);
  run(`adReviewAdvance('c', 'Skipped. It stays in the queue for later.', false)`);
  assert.doesNotMatch(run('adReviewEmptyMarkup()'), /ar-undo|Correct this read/);
});

test('Undo posts the label id, says what was undone and reloads that read', async () => {
  const h = page({ onPost: (url) => (url.endsWith('/undo')
    ? h.json(200, { ok: true, action: 'not_ad', label_id: 41, rules_retired: [{ kind: 'not_ad', key: 'k' }], reports_reopened: 0, corpus_changed: false,
      message: "The read's judgment is back as it was; 1 rule retired. The labelled example is marked undone in the corrections export." })
    : h.json(200, SAVED())) });
  await h.show();
  await h.press('n');
  assert.equal(h.AdReview.items.length, 0, 'the saved read left the queue');
  const undo = undoButton(h);
  assert.ok(undo, 'Undo is on the page (empty queue too)');
  await undo.onclick(); await h.flush();
  const post = h.posts[1];
  assert.equal(post.url, '/api/v1/admin/review-queue/e1/undo');
  assert.deepEqual(post.body, { label_id: 41 });
  const html = h.el('view').innerHTML;
  assert.match(html, /<p class="ar-last" id="ar-status" role="status" aria-live="polite">Undone: not an ad\. The read's judgment is back as it was; 1 rule retired\./);
  assert.match(html, /aria-label="Read under review" data-brand="youtube premium"/, 'the undone read is on screen again');
  assert.equal(h.AdReview.items[h.AdReview.index].evidence_id, 'e1');
  assert.equal(h.AdReview.done.has('e1'), false);
  assert.doesNotMatch(html, /ar-undo|Correct this read/, 'nothing left to undo');
});

test('an Undo that changed other reads reloads the queue with the undone read first', () => {
  const ctx = page({ onPost: () => ({}) });
  const run = (code) => vm.runInContext(code, ctx.ctx);
  run(`AdReview.items = [{ evidence_id: 'x' }]; AdReview.index = 1; AdReview.cache.set('x', 1);
    adReviewUndone({ id: 'a', label: 'every “way fair” read is Wayfair', listed: { evidence_id: 'a', reasons: [] } }, { corpus_changed: true, message: 'Done.' });`);
  assert.equal(run('AdReview.items.length'), 0, 'the queue reloads');
  assert.equal(run('AdReview.cache.size'), 0, 'prefetched reads reload');
  assert.equal(run('AdReview.front.evidence_id'), 'a');
  assert.equal(run('AdReview.lastMessage'), 'Undone: every “way fair” read is Wayfair. Done.');
});

test('READ_CHANGED on Undo explains, drops Undo and keeps "Correct this read"', async () => {
  const CHANGED = { ok: false, error: { code: 'READ_CHANGED', message: 'This read changed since the page showed it (its brand or judgment changed after that save). Nothing was undone.', details: {} } };
  const h = page({ onPost: (url) => (url.endsWith('/undo') ? h.json(409, CHANGED) : h.json(200, SAVED())) });
  await h.show();
  await h.press('n');
  await undoButton(h).onclick(); await h.flush();
  const html = h.el('view').innerHTML;
  assert.match(html, /Not undone: This read changed since the page showed it \(its brand or judgment changed after that save\)\. Nothing was undone\. Use Correct this read to change it on Review evidence\./);
  assert.match(html, /<span class="ar-last-fix"><a href="#\/review\/e1">Correct this read<\/a><\/span>/);
  assert.doesNotMatch(html, /ar-undo/);
  assert.equal(h.AdReview.done.has('e1'), true, 'the read stays saved and out of the queue');
});

test('a failed Undo (not READ_CHANGED) keeps the button to try again', async () => {
  let release;
  const h = page({ onPost: (url) => (url.endsWith('/undo') ? new Promise((r) => { release = () => r(h.json(500, { ok: false, error: 'database is locked' })); }) : h.json(200, SAVED())) });
  await h.show();
  await h.press('n');
  const undo = undoButton(h);
  const running = undo.onclick();
  await h.flush();
  assert.equal(undo.disabled, true);
  assert.match(h.el('ar-status').textContent, /Undoing the last save/);
  release(); await running; await h.flush();
  assert.equal(undo.disabled, false);
  assert.match(h.el('ar-status').textContent, /Not undone: database is locked/);
});

test('Undo markup has no inline style', () => {
  const ctx = page({ onPost: () => ({}) });
  const run = (code) => vm.runInContext(code, ctx.ctx);
  run(`AdReview.lastMessage = 'Saved: x.'; AdReview.lastSaved = { id: 'e1', message: 'Saved: x.', label: 'x', labelId: 3 };`);
  assert.doesNotMatch(run('adReviewFixLink()'), /style=/);
});
