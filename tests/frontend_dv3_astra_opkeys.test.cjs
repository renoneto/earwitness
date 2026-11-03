/* Astra decisions 12 and 14 (operator Ad review keys and rule removal).
   - 14 (UX-13): plain Enter in "Type a brand" saves this read only; the
     every-read rename is Shift+Enter in the field or Shift+A, count first,
     Esc cancels; a pending rename never saves text no longer in the field.
   - 12 (F-13): removing a rule (Remove or the inline Retry) asks the server
     for the reads it changes first; the next press sends that count.
   The loader and page harness are the ones in frontend_dv3_ux_operator.
   Run: node --test tests/frontend_dv3_astra_opkeys.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(names, extra = {}) {
  const el = () => ({ textContent: '', innerHTML: '', classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, removeAttribute() {}, addEventListener() {} });
  const document = { getElementById: el, querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, removeEventListener() {} };
  const store = { getItem: () => null, setItem() {} };
  const ctx = { document, sessionStorage: store, localStorage: store, window: {}, location: { hash: '' },
    history: { replaceState() {} }, console, setTimeout, clearTimeout, URLSearchParams, AbortController,
    fetch: async () => { throw new Error('no network in tests'); }, ...extra };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  }
  return vm.runInContext(`(${names})`, ctx);
}
const onPage = { closest: () => null };
const ITEM = {
  evidence_id: 'e1', show_title: 'The Daily', ep_title: 'Ep 1', pub_date: '2026-10-08', span: [1893.12, 1920],
  raw_brand: 'youtube premium',
  read: { brand: { key: 'youtube premium', display: 'YouTube Premium', confirmed: false }, confidence: 'low', confidence_score: 0.704,
    candidates: [{ brand: 'youtube premium', score: 5.066, evidence: [] }, { brand: 'other name', score: 0.2, evidence: [] }] },
  transcript: [{ start: 1893.12, end: 1900, text: 'Try it.', in_read: true }],
  reports: [],
  media: { kind: 'local', src: '/media/x.mp3', label: 'Local file', source_offset_s: 0 },
};

// ---- The page harness (copy of frontend_dv3_ux_operator.test.cjs page()) ----
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
    || (s === '.ar-play' && /ar-play/.test(b.cls))
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
  return { ...api, show, press, flush, posts, renders, el, json, get buttons() { return buttons; }, button: (ar) => buttons.find((b) => b.dataset.ar === ar) };
}

const ALIAS_ASK = (reads) => ({ ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'Every read heard as “youtube premium” becomes Apple Card: confirm with the count.', details: { raw: 'youtube premium', brand: 'apple card', reads, known_brand: false } } });
const typeEnter = async (h, value, extra = {}) => {
  const input = h.el('ar-brand');
  input.value = value;
  input.on.keydown({ key: 'Enter', preventDefault() {}, ...extra });
  await h.flush(); await h.flush();
};

// ---- Decision 14 (UX-13): Enter = this read; Shift+Enter / Shift+A = every read, count first ----
test('D14 plain Enter in "Type a brand" saves this read only, with no prompt', async () => {
  const h = page({ onPost: () => h.json(200, { ok: true, rules: [], reads: 0 }) });
  await h.show();
  await typeEnter(h, 'Apple Card');
  assert.equal(h.posts.length, 1);
  assert.deepEqual(h.posts[0].body, { action: 'type', brand: 'Apple Card', every_read: false, heard: 'youtube premium' });
  assert.equal(h.el('ar-confirm').hidden, true);
});

test('D14 Shift+Enter in the field asks with the count; Shift+Enter again sends it', async () => {
  const h = page({ onPost: (url, body) => (body.expected_reads === 11 ? h.json(200, { ok: true, rules: [], reads: 11 }) : h.json(409, ALIAS_ASK(11))) });
  await h.show();
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].body.action, 'alias');
  assert.equal('expected_reads' in h.posts[0].body, false, 'the first press only asks');
  assert.equal(h.el('ar-confirm').hidden, false);
  assert.match(h.el('ar-confirm').innerHTML, /This changes 11 reads\. Press Shift\+A again \(or Shift\+Enter in the brand field\) to show every read heard as “youtube premium” as Apple Card; Esc cancels\./);
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.action, 'alias');
  assert.equal(h.posts[1].body.expected_reads, 11);
});

test('D14 edited text after the prompt asks again (no old count is sent)', async () => {
  const h = page({ onPost: () => h.json(409, ALIAS_ASK(11)) });
  await h.show();
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  await typeEnter(h, 'Apple Pay', { shiftKey: true });
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.brand, 'Apple Pay');
  assert.equal('expected_reads' in h.posts[1].body, false);
});

test('D14 plain Enter after a rename prompt saves this read only, never the rename', async () => {
  const h = page({ onPost: (url, body) => (body.action === 'alias' ? h.json(409, ALIAS_ASK(11)) : h.json(200, { ok: true, rules: [] })) });
  await h.show();
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  await typeEnter(h, 'Apple Card');
  assert.equal(h.posts.length, 2);
  assert.deepEqual(h.posts[1].body, { action: 'type', brand: 'Apple Card', every_read: false, heard: 'youtube premium' });
});

test('D14 Shift+Enter on a read with no spoken name sends nothing and says why', async () => {
  const h = page({ item: { ...ITEM, raw_brand: null }, onPost: () => h.json(200, { ok: true, rules: [] }) });
  await h.show();
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  assert.equal(h.posts.length, 0);
  assert.match(h.el('ar-status').textContent, /no spoken name/);
});

test('D14 a pending rename is not reused by Shift+A once the field text changed', async () => {
  const h = page({ onPost: () => h.json(409, ALIAS_ASK(11)) });
  await h.show();
  await typeEnter(h, 'Apple Card', { shiftKey: true });
  assert.equal(h.posts.length, 1);
  h.el('ar-brand').value = 'Apple Pay';
  await h.press('A', { shiftKey: true });
  assert.equal(h.posts.length, 2);
  assert.equal(h.posts[1].body.brand, 'Apple Pay');
  assert.equal('expected_reads' in h.posts[1].body, false);
  // Same text: Shift+A confirms the count shown.
  await h.press('A', { shiftKey: true });
  assert.equal(h.posts[2].body.brand, 'Apple Pay');
  assert.equal(h.posts[2].body.expected_reads, 11);
});

test('D14 the key hints name Enter = this read only and Shift+Enter = every read, count first', () => {
  const { adReviewItemMarkup, adReviewAskWords, AdReview } = load('{ adReviewItemMarkup, adReviewAskWords, AdReview }');
  AdReview.items = [{ evidence_id: 'e1', reasons: [] }]; AdReview.index = 0;
  const html = adReviewItemMarkup(ITEM);
  assert.match(html, /<p class="meta">Enter: this read only · Shift\+Enter: every read heard as “youtube premium” \(shows the count first\) · Esc: back to keys<\/p>/);
  assert.doesNotMatch(html, /Enter: this read, and every read/);
  assert.match(html, /Rules: Shift\+A \(Shift\+Enter in the brand field\), Shift\+G and P show the count first; press the same key again \(Enter for P\) to save\./);
  assert.doesNotMatch(html, /a typed brand that changes other reads/);
  const none = adReviewItemMarkup({ ...ITEM, raw_brand: null });
  assert.match(none, /<p class="meta">Enter: this read only · Esc: back to keys<\/p>/);
  assert.equal(adReviewAskWords('alias', { raw: 'state farm', reads: 110 }, 'Geico'),
    'This changes 110 reads. Press Shift+A again (or Shift+Enter in the brand field) to show every read heard as “state farm” as Geico; Esc cancels.');
});

// ---- Decision 12 (F-13): rule removal shows the count first ----
const REMOVE_ASK = (reads) => ({ ok: false, error: { code: 'CONFIRM_REQUIRED', message: 'Removing this rule changes reads: confirm with the count.', details: { rule: 7, kind: 'alias', reads, known_brand: false } } });
const RULE_URL = '/api/v1/admin/rules/7';

test('D12 the first Remove press only asks the server for the count; the next press sends it', async () => {
  const h = page({ onPost: (url, body) => (body && body.expected_reads === 3 ? h.json(200, { ok: true, message: 'Rule removed.', reads: 3 }) : h.json(409, REMOVE_ASK(3))) });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 1);
  assert.deepEqual([h.posts[0].url, h.posts[0].method, h.posts[0].body], [RULE_URL, 'DELETE', null], 'no count: a dry run');
  assert.equal(remove.textContent, 'Remove: press again');
  assert.equal(remove.inserted.length, 1);
  assert.match(remove.inserted[0], /^<li class="ar-rule-ask notice" role="alert"><span>This changes 3 reads\. Press Remove again to remove the rule; Esc cancels\.<\/span><\/li>$/);
  assert.equal(h.AdReview.removing, false, 'actions come back while it asks');
  assert.equal(h.button('confirm').disabled, false);
  const renders = h.renders.length;
  await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 2);
  assert.deepEqual(h.posts[1].body, { expected_reads: 3 });
  assert.equal(h.renders.length, renders + 1, 'removed: the read and queue reload');
  assert.equal(h.AdReview.lastMessage, 'Rule removed.');
});

test('D12 a count that changed meanwhile asks again with the new count', async () => {
  let reads = 3;
  const h = page({ onPost: (url, body) => (body && body.expected_reads === reads ? h.json(200, { ok: true, message: 'Rule removed.' }) : h.json(409, REMOVE_ASK(reads))) });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  await remove.onclick(); await h.flush();
  reads = 4;
  await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 2);
  assert.deepEqual(h.posts[1].body, { expected_reads: 3 });
  assert.match(remove.inserted.at(-1), /The count changed\. This changes 4 reads\. Press Remove again/);
  await remove.onclick(); await h.flush();
  assert.deepEqual(h.posts[2].body, { expected_reads: 4 });
});

test('D12 Esc cancels the removal prompt (in the panel and with the page key)', async () => {
  const h = page({ onPost: () => h.json(409, REMOVE_ASK(3)) });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  await remove.onclick(); await h.flush();
  h.el('ar-rules').onkeydown({ key: 'Escape' });
  assert.equal(remove.textContent, 'Remove');
  await remove.onclick(); await h.flush();
  assert.equal(h.posts[1].body, null, 'after Esc the next press asks again');
  await h.press('Escape');
  assert.equal(remove.textContent, 'Remove');
  await remove.onclick(); await h.flush();
  assert.equal(h.posts[2].body, null);
});

test('D12 the inline Retry is not pre-armed: it asks for the count first', async () => {
  let n = 0;
  const h = page({ onPost: () => (++n === 1 ? h.json(409, REMOVE_ASK(3)) : n === 2 ? h.json(500, { ok: false, error: 'database is locked' }) : h.json(409, REMOVE_ASK(3))) });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  await remove.onclick(); await h.flush();
  await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 2);
  const errorRow = remove.inserted.at(-1);
  assert.match(errorRow, /Not removed: database is locked\. The rule still applies and no read changed\./);
  assert.doesNotMatch(errorRow, /data-armed/);
  assert.equal(remove.textContent, 'Remove');
  const retry = h.buttons.find((b) => /ar-retry/.test(b.cls));
  await retry.onclick(); await h.flush();
  assert.equal(h.posts.length, 3);
  assert.equal(h.posts[2].body, null, 'Retry sends no count: it asks');
  assert.equal(retry.textContent, 'Remove: press again');
});
