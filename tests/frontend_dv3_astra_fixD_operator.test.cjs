/* Astra decision-round review fixes (operator Ad review): D-12 an Undo
   whose answer was lost and is pressed again ("already undone") is done:
   it says "Already undone" and reloads the queue with that read first;
   D-03 the Undo line carries the server's measured "N reads changed back".
   Page harness copied from frontend_dv3_astra_undo.test.cjs.
   Run: node --test tests/frontend_dv3_astra_fixD_operator.test.cjs */
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

test('D-12 Undo refused as already undone is done: "Already undone", the read reloads, no Undo left', async () => {
  const GONE = { ok: false, error: { code: 'INVALID_ARGUMENT', message: 'the last save on this read is already undone', status: 400, details: {} } };
  const h = page({ onPost: (url) => (url.endsWith('/undo') ? h.json(400, GONE) : h.json(200, SAVED())) });
  await h.show();
  await h.press('n');
  assert.equal(h.AdReview.items.length, 0, 'the saved read left the queue');
  await undoButton(h).onclick(); await h.flush();
  const html = h.el('view').innerHTML;
  assert.match(html, /id="ar-status"[^>]*>Already undone: not an ad\.</);
  assert.doesNotMatch(html, /Not undone/);
  assert.match(html, /aria-label="Read under review" data-brand="youtube premium"/, 'the read is on screen again');
  assert.equal(h.AdReview.items[h.AdReview.index].evidence_id, 'e1');
  assert.equal(h.AdReview.done.has('e1'), false);
  assert.doesNotMatch(html, /ar-undo|Correct this read/, 'nothing left to undo');
});

test('D-12 any other 400 on Undo still says "Not undone" and keeps the button', async () => {
  const BAD = { ok: false, error: { code: 'INVALID_ARGUMENT', message: 'nothing to undo: no save on this read', status: 400, details: {} } };
  const h = page({ onPost: (url) => (url.endsWith('/undo') ? h.json(400, BAD) : h.json(200, SAVED())) });
  await h.show();
  await h.press('n');
  const undo = undoButton(h);
  await undo.onclick(); await h.flush();
  assert.match(h.el('ar-status').textContent, /^Not undone: nothing to undo/);
  assert.equal(undo.disabled, false);
});

test('D-03 Undo of a bulk save shows the measured count the server returns', () => {
  const ctx = page({ onPost: () => ({}) });
  const run = (code) => vm.runInContext(code, ctx.ctx);
  run(`adReviewUndone({ id: 'a', label: 'every “way fair” read is Wayfair', listed: { evidence_id: 'a', reasons: [] } },
    { corpus_changed: true, reads: 2, message: "The read's brand and judgment are back as they were; 2 rules retired; 2 reads changed back. The labelled example is marked undone in the corrections export." });`);
  assert.match(run('AdReview.lastMessage'), /^Undone: every “way fair” read is Wayfair\. .*; 2 reads changed back\./);
});
