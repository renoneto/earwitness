/* Console noise on two operator pages (review finding L4):
   1. Data & Settings must not request /api/v1/connections (the local server
      has no such route; it answered 404 and the console showed an error).
   2. Ad review must not fetch the remote audio before the operator presses
      play; pressing play (button, Space, R or the native control) still plays.
   Page harness copied from frontend_dv3_astra_fixD_operator.test.cjs.
   Run: node --test tests/frontend_console_noise.test.cjs */
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
  media: { kind: 'remote_full', label: 'Remote stream, unverified', src: 'https://dts.podtrac.com/redirect.mp3/ep.mp3', source_offset_s: 0 },
};
function page({ item = ITEM, onPost, rules, player = null, empty = false } = {}) {
  const posts = [], keydown = [], els = {}, renders = [];
  let buttons = [];
  const attr = (tag, name) => { const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`)); return m ? m[1] : undefined; };
  const parse = (html, where) => [...String(html).matchAll(/<button\b[^>]*>/g)].map((m) => {
    const tag = m[0], cls = attr(tag, 'class') || '';
    const b = { tag, where, cls, disabled: /\sdisabled(\s|>|=)/.test(tag), textContent: '', hidden: false, title: '',
      dataset: { ar: attr(tag, 'data-ar'), arPick: attr(tag, 'data-ar-pick'), brand: attr(tag, 'data-brand'), rule: attr(tag, 'data-rule'), armed: attr(tag, 'data-armed') },
      inserted: [], classList: { toggle() {}, add() {}, remove() {} }, setAttribute(k, v) { this[k] = v; }, removeAttribute() {}, focus() {}, click() { this.onclick && this.onclick(); },
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

const mkPlayer = () => {
  const listeners = {}, p = { paused: true, ended: false, error: null, duration: NaN, currentTime: 0, isConnected: true, playbackRate: 1, plays: 0, srcSets: [],
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }, pause() {}, play() { this.plays++; this.paused = false; return Promise.resolve(); },
    emit(type) { (listeners[type] || []).forEach((fn) => fn()); } };
  Object.defineProperty(p, 'src', { get() { return this._src || ''; }, set(v) { this._src = v; this.srcSets.push(v); } });
  return p;
};

test('L4 Ad review markup carries no remote audio source and does not preload', async () => {
  const h = page({ onPost: () => h.json(200, {}), player: mkPlayer() });
  await h.show();
  const html = h.el('view').innerHTML;
  assert.match(html, /<audio id="ar-audio" controls preload="none"[^>]*><\/audio>/);
  assert.doesNotMatch(html, /<audio[^>]*\ssrc=/, 'no src attribute before play');
  assert.doesNotMatch(html, /podtrac|preload="auto"/, 'the remote URL is not in the page before play');
  assert.match(html, /Remote stream, unverified/);
});

test('L4 no audio source is set on load; the first play press sets it and then plays once metadata arrives', async () => {
  const player = mkPlayer();
  const h = page({ onPost: () => h.json(200, {}), player });
  await h.show();
  assert.deepEqual(player.srcSets, [], 'nothing fetched on page load');
  await h.press(' ');
  assert.deepEqual(player.srcSets, ['https://dts.podtrac.com/redirect.mp3/ep.mp3']);
  player.emit('loadedmetadata'); await h.flush();
  assert.equal(player.plays, 1, 'playback starts when the stream is ready');
  await h.press(' ');
  assert.deepEqual(player.srcSets.length, 1, 'the source is set once');
});

test('L4 the Play button and R also load the source on first use', async () => {
  const a = mkPlayer(), ha = page({ onPost: () => ha.json(200, {}), player: a });
  await ha.show(); ha.button('play').onclick && ha.button('play').onclick();
  await ha.press('r');
  assert.equal(a.srcSets.length, 1);
});

test('L4 the native control starts the same lazy load on its play event', async () => {
  const player = mkPlayer();
  const h = page({ onPost: () => h.json(200, {}), player });
  await h.show();
  player.emit('play');
  assert.equal(player.srcSets.length, 1);
});

test('L4 Data & Settings does not request /api/v1/connections and says what the panel means locally', async () => {
  const urls = [], box = { innerHTML: '', textContent: '' }, btn = () => ({ disabled: false, onclick: null });
  const els = { 'hosted-conn-state': box, 'hosted-conn-refresh': btn(), 'hosted-conn-revoke': btn() };
  const ctx = { document: { getElementById: (id) => els[id] || null, addEventListener() {}, removeEventListener() {}, querySelector: () => null, querySelectorAll: () => [] }, console, setTimeout, clearTimeout, URLSearchParams, AbortController,
    fetch: async (url) => { urls.push(url); return { ok: false, status: 404, json: async () => ({}) }; }, location: { hash: '#/data' }, window: {}, sessionStorage: { getItem: () => null, setItem() {} }, localStorage: { getItem: () => null, setItem() {} } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  vm.runInContext('wireHostedConnections()', ctx);
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(urls.filter((u) => /connections/.test(u)), [], 'no request the local server cannot answer');
  assert.match(box.innerHTML, /hosted service/i);
  assert.match(box.innerHTML, /not part of this local app|no hosted connection/i);
  assert.doesNotMatch(box.innerHTML, /Backend not reachable/);
});

test('L4 the operator source never calls the hosted connections route', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'app/operator/views.js'), 'utf8');
  assert.doesNotMatch(src, /\/api\/v1\/connections/);
});
