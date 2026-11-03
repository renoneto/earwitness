/* Review rounds 6+ on the Ad review action guard (AU-02/AU-03): the real
   Ad review page (harness of frontend_dv3_astra_opkeys.test.cjs, with the
   Suggested brands panel added) and a small panel harness.
   Run: node --test tests/frontend_review_fixes_adreview.test.cjs */
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
  media: { kind: 'local', src: '/media/x.mp3', label: 'Local file', source_offset_s: 0 },
};

// ---- The page harness: frontend_dv3_astra_opkeys.test.cjs page(), plus the Suggested brands panel ----
const SUGGESTIONS = [{ id: 9, key: 'other', state: 'pending', status: 'pending', verdict: 'brand', company: 'Other', ads: 1, shows: [] }];
function page({ item = ITEM, onPost, rules, player = null, empty = false } = {}) {
  const posts = [], keydown = [], els = {}, renders = [];
  let buttons = [];
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`)); return m ? m[1] : undefined; };
  const parse = (html, where) => [...String(html).matchAll(/<button\b[^>]*>/g)].map((m) => {
    const tag = m[0], cls = attr(tag, 'class') || '';
    const b = { tag, where, cls, disabled: /\sdisabled(\s|>|=)/.test(tag), textContent: '', hidden: false, title: '',
      dataset: { acting: /\sdata-acting[\s>=]/.test(tag) ? '' : undefined, ar: attr(tag, 'data-ar'), arPick: attr(tag, 'data-ar-pick'), brand: attr(tag, 'data-brand'), rule: attr(tag, 'data-rule'), armed: attr(tag, 'data-armed'),
        sg: attr(tag, 'data-sg'), id: attr(tag, 'data-id'), verdict: attr(tag, 'data-verdict'), company: attr(tag, 'data-company'), status: attr(tag, 'data-status'), fetchDate: attr(tag, 'data-fetch-date'), heardCorrect: attr(tag, 'data-heard-correct') },
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
    || (s === '.ar-undo' && /ar-undo/.test(b.cls))
    || (s === '#ar-suggest button[data-sg]' && b.where === 'ar-suggest' && b.dataset.sg)
    || (s === 'button[data-sg]' && b.dataset.sg)
    || (s === '.ar-remove[data-acting]' && /ar-remove/.test(b.cls) && b.dataset.acting !== undefined)
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
        querySelector: (sel) => { if (sel === '[data-acting]') return /\sdata-acting[\s>=]/.test(html) ? {} : null; const m = sel.match(/^\[data-ar(-pick)?="([^"]+)"\]$/); return m ? buttons.find((b) => (m[1] ? b.dataset.arPick : b.dataset.ar) === m[2]) || null : null; },
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
    if (url === '/api/v1/admin/brand-suggestions') return json(200, { ok: true, suggestions: SUGGESTIONS });
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
  const api = vm.runInContext('({ App, AdReview, Views, adReviewUndo })', ctx);
  api.App.route = 'ad-review'; api.App.csrf = 't';
  const show = () => api.Views['ad-review']();
  ctx.render = async () => { renders.push(1); await show(); };
  const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };
  const press = async (key, extra = {}) => { const ev = { key, target: onPage, preventDefault() { ev.prevented = true; }, ...extra }; [...keydown].forEach((fn) => fn(ev)); await flush(); await flush(); return ev; };
  return { ...api, show, press, flush, posts, renders, el, json, get buttons() { return buttons; }, button: (ar) => buttons.find((b) => b.dataset.ar === ar) };
}


/* POSTs held until the test answers them. */
function held() {
  const waiting = [];
  const onPost = (url) => new Promise((resolve) => waiting.push({ url, resolve }));
  const answer = (url, status, body) => { const h = waiting.find((x) => x.url.startsWith(url)); waiting.splice(waiting.indexOf(h), 1); h.resolve({ ok: status < 400, status, json: async () => body }); };
  return { onPost, answer };
}
const ACTION_URL = '/api/v1/admin/review-queue/e1/action';
const DISMISS_URL = '/api/v1/admin/brand-suggestions/9/dismiss';
const sgButton = (p, sg) => p.buttons.find((b) => b.where === 'ar-suggest' && b.dataset.sg === sg);

test('R6-04 the review save first: suggestion actions are disabled and refused until it ends', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  assert.ok(sgButton(p, 'dismiss') && !sgButton(p, 'dismiss').disabled, 'the panel is ready');
  p.button('not_ad').click(); await p.flush();
  assert.ok(sgButton(p, 'dismiss').disabled, 'Not this is disabled while the save runs');
  sgButton(p, 'dismiss').onclick(); await p.flush();
  assert.deepEqual(p.posts.map((x) => x.url), [ACTION_URL]);
  h.answer(ACTION_URL, 200, { ok: true, label_id: 3, reads: 0, rules: [] });
  await p.flush(); await p.flush();
  assert.equal(p.AdReview.acting, false);
});

test('R6-04 a suggestion action first: the review save is disabled and refused until it ends', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  sgButton(p, 'dismiss').onclick(); await p.flush();
  assert.ok(p.button('not_ad').disabled, 'Not an ad is disabled while the suggestion action runs');
  p.button('not_ad').onclick(); await p.flush();
  await p.press('n');
  assert.deepEqual(p.posts.map((x) => x.url), [DISMISS_URL]);
  h.answer(DISMISS_URL, 200, { ok: true, state: 'dismissed' });
  await p.flush(); await p.flush();
  assert.ok(!p.button('not_ad').disabled, 'enabled again once it ended');
  p.button('not_ad').click(); await p.flush();
  assert.deepEqual(p.posts.map((x) => x.url), [DISMISS_URL, ACTION_URL]);
});

test('R6-03 Filter is refused, and says why, while a guarded action runs', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  const filters = p.el('ar-filters');
  sgButton(p, 'dismiss').onclick(); await p.flush();
  const before = JSON.stringify(p.AdReview.filter), renders = p.renders.length;
  p.el('ar-show').value = 'Show B';
  await filters.on.submit({ preventDefault() {} }); await p.flush();
  assert.equal(JSON.stringify(p.AdReview.filter), before, 'the filter is not changed under the running action');
  assert.equal(p.renders.length, renders);
  assert.match(p.el('ar-status').textContent, /finish/, 'it says why');
  h.answer(DISMISS_URL, 200, { ok: true, state: 'dismissed' });
  await p.flush();
});

/* The small panel harness of review round 6 (R6-01, R6-02). */
function panel({ suggestions = [], deferGet = false } = {}) {
  const els = {};
  const el = (id) => els[id] ||= { id, innerHTML: '', textContent: '', disabled: false, dataset: {},
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {}, append() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } };
  const store = { getItem: () => null, setItem() {} };
  const extra = { list: [] };
  let html = '', buttons = [];
  const doc = { getElementById: el, querySelector: () => null, addEventListener() {}, activeElement: null,
    querySelectorAll: (sel) => (sel.includes('.ar-undo') ? [...extra.list, ...buttons] : []) };
  const ctx = { document: doc, sessionStorage: store, localStorage: store, window: { scrollTo() {}, scrollY: 0 },
    location: { hash: '#/ad-review' }, history: { replaceState() {} }, setTimeout, clearTimeout, AbortController, URLSearchParams, console };
  vm.createContext(ctx);
  for (const f of ['app/operator/app.js', 'app/operator/views.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', f), 'utf8'), ctx, { filename: f });
  const g = (e) => vm.runInContext(e, ctx);
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\sdata-${name}="([^"]*)"`)); return m ? m[1] : undefined; };
  const host = el('ar-suggest');
  Object.defineProperty(host, 'innerHTML', { get: () => html, set: (v) => {
    html = v;
    buttons = [...String(v).matchAll(/<button\b[^>]*>[^<]*<\/button>/g)].map((m) => ({ tag: m[0], disabled: /\sdisabled(\s|>|=)/.test(m[0]),
      dataset: { sg: attr(m[0], 'sg'), id: attr(m[0], 'id'), verdict: attr(m[0], 'verdict'), status: attr(m[0], 'status') } }));
  } });
  host.querySelectorAll = (sel) => (sel === 'button[data-sg]' ? buttons.filter((b) => b.dataset.sg) : []);
  host.querySelector = (sel) => (sel === '[data-acting]' && /\sdata-acting[\s>=]/.test(html) ? {} : null);
  const waiting = []; let getGate = null; const mode = { get: 'ok' };
  ctx.fetch = (url, init = {}) => {
    if (['POST', 'PUT', 'DELETE'].includes(init.method))
      return new Promise((resolve) => waiting.push({ url, answer: (status, body) => resolve({ ok: status < 400, status, json: async () => body }) }));
    if (url === '/api/v1/admin/brand-suggestions') {
      if (mode.get === 'fail') return Promise.resolve({ ok: false, status: 503, json: async () => ({ ok: false, error: 'list down' }) });
      const reply = { ok: true, status: 200, json: async () => ({ suggestions }) };
      return deferGet ? new Promise((r) => { getGate = () => r(reply); }) : Promise.resolve(reply);
    }
    return Promise.reject(new Error(`unexpected ${url}`));
  };
  const answer = (url, status, body) => { const w = waiting.find((x) => x.url === url); waiting.splice(waiting.indexOf(w), 1); w.answer(status, body); };
  g('App').csrf = 'x'; g('App').routeHash = '#/ad-review'; g('App').route = 'ad-review';
  g('Views')['ad-review'] = async () => {};
  const A = g('AdReview');
  A.lastSaved = { id: 'e1', label: 'x', labelId: 4, listed: { evidence_id: 'e1' } };
  A.items = [{ evidence_id: 'e2' }]; A.index = 0;
  return { g, el, extra, answer, host, mode, button: (sg, id) => buttons.find((b) => b.dataset.sg === sg && b.dataset.id === String(id)), openGet: () => getGate() };
}
const SG9 = { id: 9, key: 'other', state: 'pending', status: 'pending', verdict: 'brand', company: 'Other', ads: 1, shows: [] };
const UNDO = '/api/v1/admin/review-queue/e1/undo';
const settle = () => new Promise((r) => setTimeout(r, 0));

test('R6-01 a Suggested brands panel drawn while Undo runs is usable again after Undo fails', async () => {
  const p = panel({ suggestions: [SG9], deferGet: true });
  const load = p.g('loadAdReviewSuggest')();
  const undo = p.g('adReviewUndo')({ disabled: false }, { on: false });
  p.openGet(); await load;
  assert.equal(p.button('dismiss', 9).disabled, true, 'drawn disabled while Undo runs');
  p.answer(UNDO, 500, { ok: false, error: 'server busy' });
  await undo; await settle();
  assert.equal(p.button('dismiss', 9).disabled, false, 'Not this is usable after the Undo failed');
});

test('R6-01 Rules Remove buttons drawn while an action runs are disabled, and usable after it', async () => {
  const p = panel();
  const markup = (acting) => { p.g('AdReview').acting = acting; return p.g('adReviewRulesMarkup')({ rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple Card' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: { 'alias\tapplecard': 7 } } }); };
  assert.match(markup(true), /class="ar-remove"[^>]*disabled/);
  assert.doesNotMatch(markup(false), /class="ar-remove"[^>]*disabled/);
});

test('R6-02 a Play button that went dead during Undo stays off after it', async () => {
  const p = panel();
  const play = { disabled: false, title: '', dataset: { ar: 'play' } };
  const undoButton = { disabled: false, dataset: {} };
  p.extra.list = [play, undoButton];
  const undo = p.g('adReviewUndo')(undoButton, { on: false });
  play.disabled = true; play.title = 'Audio failed to load.';  // the player's error handler
  p.answer(UNDO, 500, { ok: false, error: 'server busy' });
  await undo; await settle();
  assert.equal(play.disabled, true, 'dead audio: Play stays off');
});

test('R7-01 a Suggested brands load error stays on screen after an unrelated action ends', async () => {
  const p = panel({ suggestions: [SG9] });
  await p.g('loadAdReviewSuggest')();
  p.mode.get = 'fail';  // Not this succeeds, its reload of the list fails
  const press = p.g('adReviewSuggestPress')(p.button('dismiss', 9));
  await settle();
  p.answer('/api/v1/admin/brand-suggestions/9/dismiss', 200, { ok: true, state: 'dismissed' });
  await press; await settle();
  assert.match(p.host.innerHTML, /could not load/, 'precondition: error state shown');
  const undo = p.g('adReviewUndo')({ disabled: false }, { on: false });
  await settle();
  p.answer(UNDO, 500, { ok: false, error: 'server busy' });
  await undo; await settle();
  assert.match(p.host.innerHTML, /could not load/, 'the load error and its Retry are still shown');
});

test('R7-02 Shift+Enter (Review changed read) is refused while a suggestion action runs', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  p.button('not_ad').click(); await p.flush();
  h.answer(ACTION_URL, 409, { ok: false, error: { code: 'READ_CHANGED', message: 'changed' } });
  await p.flush(); await p.flush();
  await p.show(); await p.flush();
  assert.ok(p.AdReview.changed && p.button('seen'), 'precondition: changed read shown');
  sgButton(p, 'dismiss').onclick(); await p.flush();
  await p.press('Enter', { shiftKey: true });
  assert.notEqual(p.AdReview.changed, null, 'not half-applied while the action runs');
  h.answer(DISMISS_URL, 200, { ok: true, state: 'dismissed' });
  await p.flush(); await p.flush();
});

test('R7-02 the Skip key on the could-not-load page is refused while a suggestion action runs', async () => {
  const h = held();
  let calls = 0;
  const p = page({ onPost: h.onPost, item: () => { calls += 1; if (calls > 1) throw new Error('detail down'); return ITEM; } });
  await p.show(); await p.flush();
  assert.ok(p.button('retry'), 'precondition: error page');
  sgButton(p, 'dismiss').onclick(); await p.flush();
  await p.press('s');
  assert.equal(p.AdReview.index, 0, 'S is refused while the action runs');
  h.answer(DISMISS_URL, 200, { ok: true, state: 'dismissed' });
  await p.flush(); await p.flush();
});

test('R7-03 a Remove drawn while Undo runs is usable after Undo fails and sends its DELETE', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  p.AdReview.lastSaved = { id: 'e1', label: 'x', labelId: 4, listed: { evidence_id: 'e1' } };
  const undo = p.adReviewUndo({ disabled: false }, { on: false }); await p.flush();
  await p.AdReview.reloadRules({ open: true }); await p.flush();
  const removeDrawn = p.buttons.find((b) => /ar-remove/.test(b.cls));
  assert.ok(removeDrawn.disabled, 'drawn disabled while Undo runs');
  h.answer('/api/v1/admin/review-queue/e1/undo', 500, { ok: false, error: 'server busy' });
  await undo; await p.flush();
  const remove = p.buttons.find((b) => /ar-remove/.test(b.cls));
  assert.equal(remove.disabled, false, 'Remove is usable after Undo failed');
  remove.onclick(); await p.flush();
  assert.ok(p.posts.some((x) => x.method === 'DELETE' && x.url === '/api/v1/admin/rules/7'), 'its press sends the count request');
});

test('R8-01 an unrelated action ending while a suggestion list reload runs does not redraw the old list', async () => {
  const S9 = { ...SG9 }, S10 = { ...SG9, id: 10, key: 'acme', company: 'Acme' };
  const p = panel({ suggestions: [S9, S10], deferGet: true });
  const load = p.g('loadAdReviewSuggest')(); p.openGet(); await load;
  const press = p.g('adReviewSuggestPress')(p.button('dismiss', 9)); await settle();
  p.answer('/api/v1/admin/brand-suggestions/9/dismiss', 200, { ok: true, state: 'dismissed' });
  await settle(); await settle();  // the action ended; its list reload is still running
  const undo = p.g('adReviewUndo')({ disabled: false }, { on: false }); await settle();
  p.answer(UNDO, 500, { ok: false, error: 'server busy' });
  await undo; await settle();
  assert.notEqual(p.button('dismiss', 9)?.disabled, false, 'the just-dismissed row is not offered again');
  S9.state = 'dismissed'; S9.status = 'dismissed'; p.openGet(); await press; await settle();
  assert.match(p.host.innerHTML, /Not this: left unconfirmed/, 'the reload draws the answer');
});

test('R8-02 Enter in the brand field while a suggestion action runs is refused and says why', async () => {
  const h = held();
  const p = page({ onPost: h.onPost });
  await p.show(); await p.flush();
  sgButton(p, 'dismiss').onclick(); await p.flush();
  const input = p.el('ar-brand'); input.value = 'Acme';
  p.el('ar-status').textContent = 'before';
  input.on.keydown({ key: 'Enter', shiftKey: false, preventDefault() {} }); await p.flush();
  assert.deepEqual(p.posts.map((x) => x.url), [DISMISS_URL], 'nothing saved');
  assert.match(p.el('ar-status').textContent, /Wait for the current change to finish/, 'the refusal says why');
  h.answer(DISMISS_URL, 200, { ok: true, state: 'dismissed' }); await p.flush(); await p.flush();
});
