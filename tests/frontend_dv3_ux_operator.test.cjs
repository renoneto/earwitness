/* Design v3 UX review fixes, operator pages (T5: UX-12, R2-08, R2-09, R2-16).
   Loads the real operator scripts in a stubbed sandbox (same loader as
   frontend_operator.test.cjs) and checks the safety rules: a rule key needs
   Shift and a second press after the count, a failed save never re-enables a
   button that was off for a reason, a read that changed meanwhile cannot be
   saved with one habitual key, and a failed rule removal is shown where the
   operator looks.
   Run: node --test tests/frontend_dv3_ux_operator.test.cjs */
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
const markup = (AdReview, adReviewItemMarkup, item = ITEM) => {
  AdReview.items = [{ evidence_id: item.evidence_id, reasons: [] }]; AdReview.index = 0;
  return adReviewItemMarkup(item);
};

// ---- R2-08a: rule keys need Shift ----
test('R2-08 plain A and G do nothing in Ad review; Shift+A and Shift+G ask for the rules', () => {
  const { adReviewKey } = load('{ adReviewKey }');
  assert.equal(adReviewKey({ key: 'a', target: onPage }), null, 'A is "ad" on Review evidence; here it must not start a rename');
  assert.equal(adReviewKey({ key: 'g', target: onPage }), null);
  assert.equal(adReviewKey({ key: 'A', shiftKey: true, target: onPage }), 'alias');
  assert.equal(adReviewKey({ key: 'G', shiftKey: true, target: onPage }), 'generic');
  assert.equal(adReviewKey({ key: 'C', shiftKey: true, target: onPage }), 'confirm', 'Shift does not change the other keys');
  assert.equal(adReviewKey({ key: 'n', target: onPage }), 'not_ad');
});

test('R2-08 the bar, the type hint and the count prompts name Shift+A and Shift+G', () => {
  const { adReviewItemMarkup, adReviewAskWords, AdReview } = load('{ adReviewItemMarkup, adReviewAskWords, AdReview }');
  const html = markup(AdReview, adReviewItemMarkup);
  assert.match(html, /Rename every “youtube premium” read <kbd>Shift\+A<\/kbd>/);
  assert.match(html, /Generic word “youtube premium” <kbd>Shift\+G<\/kbd>/);
  assert.match(html, /Shift\+Enter: every read heard as “youtube premium” \(shows the count first\)/);
  assert.doesNotMatch(html, /<kbd>A<\/kbd>|<kbd>G<\/kbd>/);
  assert.match(adReviewAskWords('alias', { reads: 11, raw: 'applecard' }, 'Apple Card'), /Press Shift\+A again/);
  assert.match(adReviewAskWords('generic', { reads: 11, word: 'applecard' }), /Press Shift\+G again/);
});

// ---- R2-08d: a visible, honest way back ----
test('R2-08 after a save the result line links to Review evidence; never "Undo"; a skip has no link', () => {
  const { adReviewItemMarkup, adReviewAdvance, adReviewEmptyMarkup, AdReview } = load('{ adReviewItemMarkup, adReviewAdvance, adReviewEmptyMarkup, AdReview }');
  AdReview.items = [{ evidence_id: 'e0' }, { evidence_id: 'e1' }]; AdReview.index = 0;
  adReviewAdvance('e0', 'Saved: not an ad.', true);
  assert.equal(AdReview.index, 1);
  assert.ok(AdReview.done.has('e0'));
  const html = adReviewItemMarkup(ITEM);
  assert.match(html, /<p class="ar-last" id="ar-status" role="status" aria-live="polite">Saved: not an ad\.<\/p>/);
  assert.match(html, /<span class="ar-last-fix"><a href="#\/review\/e0">Correct this read<\/a><\/span>/);
  assert.doesNotMatch(html, /Undo/);
  AdReview.filter = { show: '', brand: '', reason: '' };
  assert.match(adReviewEmptyMarkup(), /<a href="#\/review\/e0">Correct this read<\/a>/, 'also when the save emptied the queue');
  adReviewAdvance('e1', 'Skipped. It stays in the queue for later.', false);
  assert.doesNotMatch(adReviewEmptyMarkup(), /Correct this read/);
  assert.doesNotMatch(adReviewItemMarkup(ITEM), /Correct this read/);
});

// ---- A small page harness: runs Views["ad-review"] against a fake server ----
// Buttons are rebuilt from the markup the page writes, with their disabled
// state, so a test sees what the operator would see.
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

// ---- UX-12e: a failed save restores, it never re-enables what was off for a reason ----
test('UX-12e after a failed save, Confirm (no brand) and Rename (no spoken name) stay disabled', async () => {
  const h = page({ item: { ...ITEM, raw_brand: null, read: { brand: {}, candidates: [] } }, onPost: () => h.json(500, { ok: false, error: 'boom' }) });
  await h.show();
  assert.equal(h.button('confirm').disabled, true);
  assert.equal(h.button('alias').disabled, true);
  await h.press('n');
  assert.equal(h.posts.length, 1, 'N sent one save');
  assert.match(h.el('ar-status').textContent, /Not saved: boom/);
  assert.equal(h.button('confirm').disabled, true, 'Confirm has no brand to confirm');
  assert.equal(h.button('alias').disabled, true, 'Rename has no spoken name');
  assert.equal(h.button('generic').disabled, true);
  assert.equal(h.button('not_ad').disabled, false, 'what was enabled is enabled again');
});

// ---- R2-09b: a read that changed meanwhile ----
const CHANGED = { error: { code: 'READ_CHANGED', message: 'This read changed since the page showed it (brand). Nothing was saved; look again.', details: { brand: 'other name', display: 'Other Name', candidates: [] } } };
test('R2-09b a changed read shows old -> new, and its save keys stay off until "Review changed read"', async () => {
  let fresh = false;
  const after = { ...ITEM, read: { ...ITEM.read, brand: { key: 'other name', display: 'Other Name', confirmed: true } } };
  const h = page({ item: () => (fresh ? after : ITEM), onPost: () => { fresh = true; return h.json(409, { ok: false, ...CHANGED }); } });
  await h.show();
  await h.press('c');
  assert.equal(h.posts.length, 1);
  const html = h.el('view').innerHTML;
  assert.match(html, /<div class="notice ar-changed" role="alert"><p><strong>This read changed while you were looking\. Nothing was saved\.<\/strong><\/p>/);
  assert.match(html, /<dt>Brand<\/dt><dd>YouTube Premium → Other Name \(confirmed\)<\/dd>/);
  assert.doesNotMatch(html, /<dt>Spoken name<\/dt>/, 'only the fields that differ');
  assert.match(html, /<button type="button" class="primary" data-ar="seen">Review changed read <kbd>Shift\+Enter<\/kbd><\/button>/);
  assert.match(html, /<button type="button" data-ar="confirm" disabled>Confirm Other Name <kbd>C<\/kbd>/, 'Confirm is not primary and is off');
  for (const key of ['c', 'n', '1', 'A']) {
    const ev = await h.press(key, key === 'A' ? { shiftKey: true } : {});
    assert.equal(h.posts.length, 1, `${key} saved nothing`);
    assert.equal(ev.prevented, true);
  }
  assert.match(h.el('ar-status').textContent, /This read changed — press Review changed read first\./);
  await h.press('Enter');
  assert.notEqual(h.AdReview.changed, null, 'a stray Enter does not dismiss the old -> new block');
  await h.press('Enter', { shiftKey: true });
  assert.equal(h.AdReview.changed, null);
  const back = h.el('view').innerHTML;
  assert.doesNotMatch(back, /ar-changed|data-ar="seen"/);
  assert.match(back, /class="primary" data-ar="confirm" >Confirm Other Name <kbd>C<\/kbd>/);
  await h.press('c');
  assert.equal(h.posts.length, 2, 'after the review, C confirms the brand now on screen');
  assert.equal(h.posts[1].body.brand, 'other name');
});

test('R2-09b Enter does nothing on an unchanged read', async () => {
  const h = page({ onPost: () => h.json(200, { ok: true, rules: [] }) });
  await h.show();
  const ev = await h.press('Enter');
  assert.equal(ev.prevented, undefined);
  assert.equal(h.posts.length, 0);
});

// ---- R2-09a: a failed rule removal is shown in the Rules panel ----
test('R2-09a a failed removal shows "Not removed" with a Retry in the panel and keeps the status', async () => {
  const h = page({ onPost: (url) => h.json(500, { ok: false, error: 'database is locked' }) });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  remove.onclick(); await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].method, 'DELETE');
  assert.equal(remove.inserted.length, 1);
  assert.match(remove.inserted[0], /<li class="ar-rule-error" role="alert"><span>Not removed: database is locked\. The rule still applies and no read changed\.<\/span><button type="button" class="ar-remove ar-retry" data-rule="7">Retry<\/button><\/li>/);
  assert.match(h.el('ar-status').textContent, /Not removed: database is locked/);
  assert.equal(h.button('confirm').disabled, false, 'controls come back');
  assert.equal(h.AdReview.removing, false);
});

test('R2-09a an already-removed rule reloads the list, open, with a note', async () => {
  let gone = false;
  const empty = { rules: { brand_aliases: [], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: {} } };
  const full = { rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple Card' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: { 'alias\tapplecard': 7 } } };
  const h = page({ rules: () => (gone ? empty : full), onPost: () => { gone = true; return h.json(400, { ok: false, error: 'unknown rule' }); } });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  remove.onclick(); await remove.onclick(); await h.flush(); await h.flush();
  const rules = h.el('ar-rules').innerHTML;
  assert.match(rules, /<details class="ar-rules" open>/);
  assert.match(rules, /<div class="ar-rules-body"><p class="ar-rule-error" role="alert">That rule was already removed, maybe in another tab\. The list now shows the current rules\.<\/p>/);
  assert.match(rules, /No rules yet\./);
  assert.equal(h.buttons.filter((b) => b.dataset.rule === '7').length, 0, 'the removed rule is gone from the list');
});

test('R2-09a the rule error row helper', () => {
  const { adReviewRuleErrorMarkup } = load('{ adReviewRuleErrorMarkup }');
  const row = adReviewRuleErrorMarkup('bad <thing>', 12);
  assert.match(row, /role="alert"/);
  assert.match(row, /Not removed: bad &lt;thing&gt;\./);
  assert.match(row, /data-rule="12">Retry<\/button>/, 'not pre-armed: Retry asks for the count first (decision 12)');
});

// ---- UX-12b / 12-E: Self-promotion shows the count first ----
test('UX-12b the self-promotion prompt names the count, the brand and where it applies', () => {
  const { adReviewAskWords } = load('{ adReviewAskWords }');
  assert.equal(adReviewAskWords('self_promo', { reads: 2, brand: 'everydollar', network: 'Show B', exact: false, known_brand: false }),
    'This changes 2 reads. Press Enter again to save “everydollar” as self-promotion on shows with “Show B”; Esc cancels.');
  assert.match(adReviewAskWords('self_promo', { reads: 1, brand: 'everydollar', network: 'The Daily', exact: true }), /This changes 1 read\. .* on this show;/);
});

test('UX-12b Enter in the self-promotion form asks first; Enter again sends the count; edited text asks again', async () => {
  const PROMO = { error: { code: 'CONFIRM_REQUIRED', message: 'confirm', details: { brand: 'youtube premium', network: 'Show B', exact: false, reads: 2, known_brand: false } } };
  const h = page({ onPost: (url, body) => (body.expected_reads === 2 ? h.json(200, { ok: true, rules: [], reads: 2 }) : h.json(409, { ok: false, ...PROMO })) });
  await h.show();
  const network = h.el('ar-network'), enter = async () => { network.on.keydown({ key: 'Enter', preventDefault() {} }); await h.flush(); };
  network.value = 'Show B';
  await enter();
  assert.equal(h.posts.length, 1);
  assert.equal('expected_reads' in h.posts[0].body, false, 'the first press only asks');
  assert.match(h.el('ar-confirm').innerHTML, /This changes 2 reads\. Press Enter again to save “youtube premium” as self-promotion on shows with “Show B”/);
  network.value = 'Show';
  await enter();
  assert.equal(h.posts.length, 2);
  assert.equal('expected_reads' in h.posts[1].body, false, 'changed text: a fresh count, not the old one');
  network.value = 'Show B';
  await enter();
  await enter();
  assert.equal(h.posts.length, 4);
  assert.deepEqual([h.posts[3].body.network, h.posts[3].body.expected_reads], ['Show B', 2]);
});

// ---- UX-12a: two labelled groups in a docked bar ----
test('UX-12a the bar has a "This read" group and a "Rules" group, inside one dock with the prompt and status', () => {
  const { adReviewItemMarkup, AdReview } = load('{ adReviewItemMarkup, AdReview }');
  const html = markup(AdReview, adReviewItemMarkup);
  assert.doesNotMatch(html, /style=/);
  const dock = html.indexOf('<div class="ar-dock">');
  assert.ok(dock > 0, 'there is a dock');
  for (const part of ['id="ar-status"', 'id="ar-confirm"', 'class="ar-actions"', 'class="meta ar-foot"']) assert.ok(html.indexOf(part) > dock, `${part} sits in the dock`);
  const read = html.indexOf('<div class="ar-act-group" role="group" aria-labelledby="ar-act-read"><span class="ar-act-label" id="ar-act-read">This read</span>');
  const rules = html.indexOf('<div class="ar-act-group ar-act-rules" role="group" aria-labelledby="ar-act-rules"><span class="ar-act-label" id="ar-act-rules">Rules · change other reads, count shown first</span>');
  assert.ok(read > dock && rules > read, 'This read first, then Rules');
  const readPart = html.slice(read, rules), rulesPart = html.slice(rules, html.indexOf('class="meta ar-foot"'));
  for (const a of ['confirm', 'type', 'not_ad', 'skip', 'play']) assert.match(readPart, new RegExp(`data-ar="${a}"`), a);
  for (const a of ['alias', 'generic', 'self_promo', 'cancel']) { assert.match(rulesPart, new RegExp(`data-ar="${a}"`), a); assert.doesNotMatch(readPart, new RegExp(`data-ar="${a}"`), a); }
  assert.match(html, /This read: saves at once\. Rules: Shift\+A \(Shift\+Enter in the brand field\), Shift\+G and P show the count first/);
});

// ---- UX-12d: Ad review controls after "Audio failed to load" ----
test('UX-12d after the audio fails to load, play and replay are off with the reason, and Space/R do nothing', async () => {
  const listeners = {};
  const player = { paused: true, ended: false, error: null, duration: NaN, currentTime: 0, isConnected: true, playbackRate: 1, plays: 0,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }, pause() {}, play() { this.plays++; return Promise.resolve(); } };
  const h = page({ onPost: () => h.json(200, { ok: true, rules: [] }), player });
  await h.show();
  player.error = { code: 4 };
  (listeners.error || []).forEach((fn) => fn());
  for (const ar of ['play', 'replay']) {
    const b = h.buttons.filter((x) => x.dataset.ar === ar);
    assert.ok(b.length >= 1, ar);
    b.forEach((x) => { assert.equal(x.disabled, true, `${ar} is off`); assert.equal(x.title, 'Audio failed to load. Judge the read on the transcript.'); });
  }
  await h.press(' '); await h.press('r');
  assert.equal(player.plays, 0, 'Space and R do nothing on a failed player');
});

// ================= Review evidence (UX-12c, UX-12d, R2-16) =================
// A generic fake element for the Review evidence wiring tests.
function fakeEl(id, extra = {}) {
  const attrs = {};
  return { id, textContent: '', innerHTML: '', value: '', hidden: false, disabled: false, checked: false, dataset: {}, title: '',
    children: [], isConnected: true, readyState: 0, duration: NaN, currentTime: 0, paused: true, error: null, options: [],
    classList: { set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); }, toggle(c, on) { if (on ?? !this.set.has(c)) this.set.add(c); else this.set.delete(c); }, contains(c) { return this.set.has(c); } },
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute(k) { return k in attrs ? attrs[k] : null; }, removeAttribute(k) { delete attrs[k]; },
    addEventListener() {}, append(...kids) { this.children.push(...kids); }, focus() {}, pause() {}, load() {},
    play() { return Promise.resolve(); }, add() {}, get src() { return attrs.src || ''; }, set src(v) { attrs.src = v; }, ...extra };
}
function reviewSandbox() {
  const els = {};
  const document = { getElementById: (id) => (els[id] = els[id] || fakeEl(id)), querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, removeEventListener() {}, createElement: (tag) => fakeEl(tag) };
  const store = { getItem: () => null, setItem() {} };
  const posts = [];
  const ctx = { document, sessionStorage: store, localStorage: store, window: {}, location: { hash: '' }, history: { replaceState() {} },
    console, setTimeout, clearTimeout, URLSearchParams, AbortController, Option: function Option(t, v) { this.text = t; this.value = v; },
    fetch: async (url, init = {}) => { posts.push({ url, init }); return { ok: true, status: 200, json: async () => ({ ok: true }) }; } };
  vm.createContext(ctx);
  for (const file of ['app/operator/app.js', 'app/operator/views.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), ctx, { filename: file });
  ctx.wireForm = () => {}; ctx.render = async () => {};
  return { ctx, els, posts, get: (names) => vm.runInContext(`(${names})`, ctx) };
}
const A6 = { kind: 'named', source_label: 'Named read', brand: { key: 'simplisafe', display: 'SimpliSafe', suggested: 'simply safe', confirmed: false },
  needs_review: true, confidence: 'low', confidence_score: 0.31, candidates: [{ brand: 'simply safe', score: 2 }, { brand: 'ring', score: 1 }] };

test('UX-12c brand candidates are one radio list with one "Use selected brand" button, off until a different brand is picked', () => {
  const { brandChoiceMarkup } = load('{ brandChoiceMarkup }');
  const html = brandChoiceMarkup(A6, 3);
  assert.doesNotMatch(html, /Use this brand|data-use-brand/);
  assert.match(html, /<fieldset class="brand-candidates"><legend class="meta">The namer's brand candidates for this read\. Using one makes it the confirmed brand; the change is kept in this read's association history\.<\/legend>/);
  assert.match(html, /<label class="brand-cand"><input type="radio" name="brand-candidate" value="simply safe" > <span>simply safe<\/span> <span class="meta">score 2<\/span> <span class="meta">\(namer's pick\)<\/span><\/label>/);
  assert.match(html, /value="ring" >/);
  assert.doesNotMatch(html, /checked/, 'the current brand is not a candidate: nothing is checked');
  assert.match(html, /<\/fieldset><button type="button" id="use-brand" disabled>Use selected brand<\/button>/);
  const current = brandChoiceMarkup({ ...A6, brand: { key: 'ring', display: 'Ring', confirmed: true } }, 3);
  assert.match(current, /value="ring" checked> <span>ring<\/span> <span class="meta">score 1<\/span> <span class="meta">\(on this read now\)<\/span>/);
  assert.match(current, /value="simply safe" >/);
  assert.match(brandChoiceMarkup({ ...A6, candidates: [] }, 3), /The namer listed no brand candidates for this read\./);
});

test('UX-12c the button turns on only for a different brand and sends the picked one with the revision', async () => {
  const s = reviewSandbox();
  const radios = ['simply safe', 'ring'].map((v) => fakeEl('r', { value: v, checked: v === 'ring' }));
  const use = s.els['use-brand'] = fakeEl('use-brand', { disabled: true });
  s.els['brand-choice'] = fakeEl('brand-choice', { dataset: { revision: '3' } });
  s.ctx.document.querySelectorAll = (sel) => (sel === 'input[name="brand-candidate"]' ? radios : []);
  s.get('wireBrandChoice')('e1', 'ring');
  radios[1].onchange(); assert.equal(use.disabled, true, 'the brand already on the read');
  radios[1].checked = false; radios[0].checked = true; radios[0].onchange();
  assert.equal(use.disabled, false);
  await use.onclick();
  const sent = s.posts.find((p) => p.url === '/api/v1/evidence/e1/brand-choice');
  assert.ok(sent);
  assert.deepEqual(JSON.parse(sent.init.body), { brand: 'simply safe' });
  assert.equal(sent.init.headers['If-Match'], '3');
});

test('UX-12d the Review evidence player stays hidden until Play interval sets a source, and hides again on error', async () => {
  const { audioMarkup } = load('{ audioMarkup }');
  assert.match(audioMarkup(), /<audio id="audio" controls preload="none" hidden><\/audio>/);
  const s = reviewSandbox();
  s.els['audio-rate'] = fakeEl('audio-rate', { options: [{ value: '1' }] });
  s.get('App').settings = { playback_speed: 1 };
  s.get('wireAudio')({ kind: 'local', src: '/media/x.mp3', label: 'Local file', trust: 'exact', source_offset_s: 0 }, 10, 20, null, 'exact');
  const player = s.els.audio;
  assert.equal(player.hidden, true, 'no source yet: no dead 0:00 control');
  await s.els['play-evidence'].onclick();
  assert.equal(player.getAttribute('src'), '/media/x.mp3');
  assert.equal(player.hidden, false);
  player.onerror();
  assert.equal(player.hidden, true);
  assert.match(s.els['audio-status'].textContent, /Audio failed to load/);
});

test('R2-16 transcript rows that overlap the passage under review are marked (shape and words)', () => {
  const s = reviewSandbox();
  s.els.segments = fakeEl('segments');
  s.get('wireTranscript')([{ start: 0, end: 5, text: 'a' }, { start: 5, end: 10, text: 'b' }, { start: 10, end: 15, text: 'c' }], 'ep', 'tv', [6, 11]);
  const rows = s.els.segments.children;
  assert.deepEqual(rows.map((li) => li.classList.contains('in-span')), [false, true, true]);
  assert.match(rows[1].children[0].innerHTML, /<span class="visually-hidden"> \(in the passage under review\)<\/span>/);
  assert.doesNotMatch(rows[0].children[0].innerHTML, /passage under review/);
  const none = reviewSandbox();
  none.els.segments = fakeEl('segments');
  none.get('wireTranscript')([{ start: 0, end: 5, text: 'a' }], 'ep', 'tv', [null, 11]);
  assert.equal(none.els.segments.children[0].classList.contains('in-span'), false, 'an unknown bound marks nothing');
});

test('R2-16 the excerpt is labelled with its interval and spoken brand; the brand panel sits in the right column', () => {
  const { reviewExcerptHead } = load('{ reviewExcerptHead }');
  assert.equal(reviewExcerptHead({ orig_start_s: 62, orig_end_s: 118.5, attrs: { brand_spoken: 'simply <safe>' } }, 62, 118.5),
    '<p class="meta review-excerpt-head">Passage under review · 1:02.0–1:58.5 · spoken brand “simply &lt;safe&gt;”</p>');
  assert.equal(reviewExcerptHead({ orig_start_s: null, orig_end_s: 10, attrs: {} }, null, 10), '<p class="meta review-excerpt-head">Passage under review · unknown–0:10.0</p>');
  const src = fs.readFileSync(path.join(__dirname, '..', 'app/operator/views.js'), 'utf8');
  assert.match(src, /<div class="review-side">\$\{brandChoiceMarkup\(evidence\.read, evidence\.assoc_revision\)\}\$\{sourceLegend\(\)\}<\/div>/);
  assert.match(src, /<details class="panel" open><summary>Transcript<\/summary>/);
  assert.match(src, /wireTranscript\(transcript\.segments, evidence\.episode_id, evidence\.transcript_version_id, \[effLo, effHi\]\)/);
});

// ---- F-12: a changed read pending: no Enter path and no key sends ----
test('F-12 while a changed read is pending, Enter in Type and Self-promotion and the keys P, T, Shift+G send nothing', async () => {
  let fresh = false;
  const after = { ...ITEM, read: { ...ITEM.read, brand: { key: 'other name', display: 'Other Name', confirmed: true } } };
  const h = page({ item: () => (fresh ? after : ITEM), onPost: () => { fresh = true; return h.json(409, { ok: false, ...CHANGED }); } });
  await h.show();
  await h.press('c');
  assert.equal(h.posts.length, 1);
  assert.notEqual(h.AdReview.changed, null);
  const brand = h.el('ar-brand'), network = h.el('ar-network');
  const enter = async (box, extra = {}) => { box.on.keydown({ key: 'Enter', preventDefault() {}, ...extra }); await h.flush(); };
  brand.value = 'Typed Brand';
  await enter(brand);
  await enter(brand, { shiftKey: true });
  network.value = 'Show B';
  await enter(network);
  await h.el('ar-promo').on.submit({ preventDefault() {} });
  for (const [key, extra] of [['p', {}], ['t', {}], ['G', { shiftKey: true }], ['A', { shiftKey: true }]]) {
    const ev = await h.press(key, extra);
    assert.equal(ev.prevented, true, `${key} is swallowed`);
  }
  assert.equal(h.posts.length, 1, 'nothing was sent after the READ_CHANGED refusal');
  assert.match(h.el('ar-status').textContent, /This read changed — press Review changed read first\./);
});

// ---- F-14: "That rule was already removed" reloads the read too ----
test('F-14 an already-removed rule clears the cache and items, re-renders, and says "reloaded" only after the reload worked', async () => {
  let gone = false, itemLoads = 0, rulesFail = false;
  const empty = { rules: { brand_aliases: [], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: {} } };
  const full = { rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple Card' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: { 'alias\tapplecard': 7 } } };
  const h = page({ item: () => { itemLoads++; return ITEM; }, rules: () => { if (rulesFail) throw new Error('down'); return gone ? empty : full; },
    onPost: () => { gone = true; return h.json(400, { ok: false, error: 'unknown rule' }); } });
  await h.show(); await h.flush();
  const loadsBefore = itemLoads, rendersBefore = h.renders.length;
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  remove.onclick(); await remove.onclick(); await h.flush(); await h.flush();
  assert.equal(h.renders.length, rendersBefore + 1, 'the read is rendered again');
  assert.ok(itemLoads > loadsBefore, 'the read is fetched again, not taken from the prefetch cache');
  assert.match(h.el('ar-rules').innerHTML, /No rules yet\./);
  assert.match(h.el('ar-status').textContent, /already removed.*reloaded/);
  // The rules reload fails: no "reloaded" claim.
  gone = false; rulesFail = false;
  const h2 = page({ rules: () => { if (rulesFail) throw new Error('down'); return full; }, onPost: () => { rulesFail = true; return h2.json(400, { ok: false, error: 'unknown rule' }); } });
  await h2.show(); await h2.flush();
  const r2 = h2.buttons.find((b) => b.dataset.rule === '7');
  r2.onclick(); await r2.onclick(); await h2.flush(); await h2.flush();
  assert.doesNotMatch(h2.el('ar-status').textContent, /are reloaded/);
  assert.match(h2.el('ar-status').textContent, /could not be reloaded/);
});

// ---- F-15a: only the button, or Shift+Enter, dismisses the old -> new block ----
test('F-15a a plain Enter never dismisses a changed read; adReviewKey maps only Shift+Enter to "seen"', () => {
  const { adReviewKey } = load('{ adReviewKey }');
  assert.equal(adReviewKey({ key: 'Enter', target: onPage }), null);
  assert.equal(adReviewKey({ key: 'Enter', shiftKey: true, target: onPage }), 'seen');
});

test('F-15a the "Review changed read" button works with a click', async () => {
  let fresh = false;
  const after = { ...ITEM, read: { ...ITEM.read, brand: { key: 'other name', display: 'Other Name', confirmed: true } } };
  const h = page({ item: () => (fresh ? after : ITEM), onPost: () => { fresh = true; return h.json(409, { ok: false, ...CHANGED }); } });
  await h.show();
  await h.press('c');
  await h.press('Enter');
  assert.notEqual(h.AdReview.changed, null);
  h.button('seen').click(); await h.flush();
  assert.equal(h.AdReview.changed, null);
});

// ---- F-15b: a failed save does not revive a dead player ----
test('F-15b a save that fails after the audio died leaves play and replay off', async () => {
  const listeners = {};
  const player = { paused: true, ended: false, error: null, duration: NaN, currentTime: 0, isConnected: true, playbackRate: 1,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }, pause() {}, play() { return Promise.resolve(); } };
  const h = page({ player, onPost: () => { player.error = { code: 4 }; (listeners.error || []).forEach((fn) => fn()); return h.json(500, { ok: false, error: 'boom' }); } });
  await h.show();
  await h.press('n');
  assert.match(h.el('ar-status').textContent, /Not saved: boom/);
  for (const ar of ['play', 'replay']) {
    const b = h.buttons.filter((x) => x.dataset.ar === ar);
    assert.ok(b.length >= 1, ar);
    b.forEach((x) => assert.equal(x.disabled, true, `${ar} stays off`));
  }
  assert.equal(h.button('not_ad').disabled, false, 'the other buttons come back');
});

// ---- F-15c: the excerpt label names the interval the text was cut at ----
test('F-15c the excerpt label shows the original interval and names corrected bounds that differ', () => {
  const { reviewExcerptHead } = load('{ reviewExcerptHead }');
  const ev = { orig_start_s: 62, orig_end_s: 118.5, attrs: {} };
  assert.equal(reviewExcerptHead(ev, 62, 118.5), '<p class="meta review-excerpt-head">Passage under review · 1:02.0–1:58.5</p>', 'no correction: just the interval');
  assert.equal(reviewExcerptHead(ev, 60, 125),
    '<p class="meta review-excerpt-head">Passage under review · original interval 1:02.0–1:58.5 (corrected bounds 1:00.0–2:05.0; the text is from the original interval)</p>');
  assert.doesNotMatch(reviewExcerptHead(ev, 60, 125), /Passage under review · 1:00\.0/, 'the corrected times never head the original text');
});

// ---- Astra UX-14: the Rules panel says when it could not load; a removal with no answer says so ----
test('UX-14 when the rules fail to load, the panel stays with "Rules could not load" and Retry', async () => {
  const h = page({ rules: () => { throw new Error('down'); }, onPost: () => h.json(200, { ok: true }) });
  await h.show(); await h.flush();
  const html = h.el('ar-rules').innerHTML;
  assert.match(html, /<p class="error-box" role="alert">Rules could not load/);
  assert.match(html, /id="ar-rules-retry">Retry</);
  assert.match(html, /Rules already saved still apply/);
});

test('UX-14 Retry loads the rules again and replaces the error', async () => {
  let down = true;
  const ok = { rules: { brand_aliases: [{ raw: 'applecard', display: 'Apple Card' }], stoplist: [], self_promo: [], not_ads: [], new_brands: [], labels: 1, rule_ids: { 'alias\tapplecard': 7 } } };
  const h = page({ rules: () => { if (down) throw new Error('down'); return ok; }, onPost: () => h.json(200, { ok: true }) });
  await h.show(); await h.flush();
  assert.match(h.el('ar-rules').innerHTML, /Rules could not load/);
  down = false;
  h.el('ar-rules-retry').onclick(); await h.flush();
  assert.match(h.el('ar-rules').innerHTML, /Rules so far · 1/);
  assert.doesNotMatch(h.el('ar-rules').innerHTML, /Rules could not load/);
});

test('UX-14 the empty-queue page shows the same rules error', async () => {
  const h = page({ empty: true, rules: () => { throw new Error('down'); }, onPost: () => h.json(200, { ok: true }) });
  await h.show(); await h.flush();
  assert.match(h.el('ar-rules').innerHTML, /<p class="error-box" role="alert">Rules could not load/);
  assert.match(h.el('ar-rules').innerHTML, /id="ar-rules-retry">Retry</);
});

test('UX-14 a removal that gets no answer says the list may be out of date and offers Reload', async () => {
  const h = page({ onPost: () => { throw new Error('network'); } });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  remove.onclick(); await remove.onclick(); await h.flush();
  assert.equal(h.posts.length, 1);
  assert.equal(remove.inserted.length, 1);
  assert.match(remove.inserted[0], /may or may not be removed/);
  assert.match(remove.inserted[0], /this list may be out of date/);
  assert.match(remove.inserted[0], /class="ar-rules-reload">Reload rules</);
  assert.doesNotMatch(remove.inserted[0], /no read changed/);
  assert.match(h.el('ar-status').textContent, /No answer from the server/);
  assert.doesNotMatch(h.el('ar-status').textContent, /still applies/);
  assert.equal(h.AdReview.removing, false);
});

test('UX-14 Reload rules draws the current list again, open', async () => {
  const h = page({ onPost: () => { throw new Error('network'); } });
  await h.show(); await h.flush();
  const remove = h.buttons.find((b) => b.dataset.rule === '7');
  remove.onclick(); await remove.onclick(); await h.flush();
  const reload = h.buttons.find((b) => /ar-rules-reload/.test(b.cls));
  assert.ok(reload, 'the Reload button is wired');
  const before = h.renders.length;
  reload.onclick(); await h.flush(); await h.flush();
  assert.ok(h.renders.length > before, 'the page was drawn again');
  assert.match(h.el('ar-rules').innerHTML, /<details class="ar-rules" open>/);
});

test('UX-14 an answered failure keeps today\'s wording; the helper default is unchanged', () => {
  const { adReviewRuleErrorMarkup } = load('{ adReviewRuleErrorMarkup }');
  const answered = adReviewRuleErrorMarkup('db', 3, { answered: true });
  assert.equal(answered, adReviewRuleErrorMarkup('db', 3));
  assert.match(answered, /The rule still applies and no read changed\./);
  const silent = adReviewRuleErrorMarkup('db', 3, { answered: false });
  assert.match(silent, /role="alert"/);
  assert.match(silent, /may or may not be removed/);
});
