/* M11-PLAYER (UX audit M11: MKT-06, A11Y-16, A11Y-09, MKT-14; QA G2-17;
   AUDIO-429 A429-R1-04 and A429-SCR-R2-03).

   1. Span: a Search card, list row or inspector plays the span it names
      ("17:25 to 18:00", 0:35), not the clip file's whole window: from 3 s
      before the span start (the evidence player's lead-in), "0:00 of 0:35",
      and it pauses at the span end.
   2. Resume: WebKit puts a clip paused within about 0.3 s of its first
      playing back at its #t= start about 100 ms later, with no seeking
      event (scratch host, headless WebKit: paused at 2.23 s, 2.00 s next;
      the evidence player too, 15.33 -> 15.10). Play then restarted it.
      Every player keeps the place of a pause and Play goes back there; a
      seek while paused (the seek bar, ±5 s, Play from) stays where it went.
   3. The playing line on the evidence transcript scrolls into view while
      the clip plays (smooth, or instant with reduced motion), focus stays,
      and it alone carries aria-current="true".
   4. A429-R1-04: a Search clip that fails after it started playing says so
      in the evidence player's words ("The clip didn’t load."), instead of
      silently going back to Play. A failed start keeps its own toast.
   Run: node --test tests/frontend_m11_player.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };

// Each browser's events from a press to playing (rs = readyState then).
const ORDERS = {
  chromium: [['play', 0], ['waiting', 0], ['loadstart', 0], ['loadedmetadata', 1], ['seeking', 1], ['seeked', 4], ['canplay', 4], ['playing', 4]],
  firefox: [['play', 0], ['waiting', 0], ['loadstart', 0], ['loadedmetadata', 1], ['canplay', 1], ['playing', 1], ['seeking', 1], ['waiting', 1], ['seeked', 4], ['canplay', 4], ['playing', 4]],
  webkit: [['play', 0], ['waiting', 0], ['loadstart', 0], ['loadedmetadata', 1], ['canplay', 4], ['seeking', 4], ['seeked', 4], ['playing', 4]],
};

// -- a small DOM: elements with attributes, classes and children --------
class El {
  constructor(tag, props = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.classes = new Set(); this.dataset = {};
    this.children = []; this.parent = null; this.innerHTML = ''; this.textContent = ''; this.hidden = false; this.listeners = {};
    this.style = { props: {}, setProperty(k, v) { this.props[k] = String(v); }, getPropertyValue(k) { return this.props[k] || ''; } };
    const self = this;
    this.classList = { toggle(c, on) { if (on === undefined) on = !self.classes.has(c); on ? self.classes.add(c) : self.classes.delete(c); return on; },
      add(c) { self.classes.add(c); }, remove(c) { self.classes.delete(c); }, contains: c => self.classes.has(c) };
    Object.assign(this, props);
  }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  removeAttribute(n) { delete this.attrs[n]; }
  hasAttribute(n) { return n in this.attrs; }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } return this; }
  get parentNode() { return this.parent; }
  get nextElementSibling() { const sib = this.parent ? this.parent.children : []; return sib[sib.indexOf(this) + 1] || null; }
  appendChild(k) { this.append(k); return k; }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  matches(sel) { return sel.split(',').some(s => matchOne(this, s.trim())); }
  querySelector(sel) { return this.all().find(e => e.matches(sel)) || null; }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  closest(sel) { for (let e = this; e; e = e.parent) if (e.matches(sel)) return e; return null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
}
const camel = s => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
function matchOne(el, sel) {
  const m = /^([a-z]+)?(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/i.exec(sel);
  if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && el.id !== m[2].slice(1)) return false;
  for (const c of (m[3] || '').split('.').filter(Boolean)) if (!el.classes.has(c)) return false;
  for (const a of (m[4] || '').match(/\[[^\]]+\]/g) || []) {
    const [, name, val] = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(a);
    const v = name.startsWith('data-') ? el.dataset[camel(name.slice(5))] : name === 'id' ? el.id : el.getAttribute(name);
    if (v === undefined || v === null || (val !== undefined && String(v) !== val)) return false;
  }
  return true;
}

// A media element whose play() settles as the browser's does: resolved
// at playing, rejected after a failed load.
class Audio extends El {
  constructor(t) {
    super('audio');
    Object.assign(this, { t, paused: true, ended: false, readyState: 0, error: null, currentTime: 0, plays: 0, waiters: [], src: '' });
  }
  play() {
    this.plays++; this.paused = false; this.ended = false;
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }
  pause() {
    if (this.paused) return;
    this.paused = true;
    for (const w of this.waiters.splice(0)) w.reject(Object.assign(new Error('The play() request was interrupted by a call to pause().'), { name: 'AbortError' }));
    this.t.queue(this, 'pause');
  }
  // The media load algorithm: a playing element is paused (no pause
  // event) and its pending play() rejects with AbortError.
  load() {
    this.error = null; this.readyState = 0;
    if (!this.paused) { this.paused = true; for (const w of this.waiters.splice(0)) w.reject(Object.assign(new Error('The play() request was interrupted by a new load request.'), { name: 'AbortError' })); }
  }
  removeAttribute(n) { if (n === 'src') this.src = ''; super.removeAttribute(n); }
}

async function page() {
  const t = { docListeners: {}, toasts: [], pending: [] };
  const body = new El('body');
  const main = new El('main'); main.id = 'main';
  body.append(main);
  const doc = {
    body,
    addEventListener: (type, fn) => { (t.docListeners[type] = t.docListeners[type] || []).push(fn); },
    querySelector: sel => body.querySelector(sel),
    querySelectorAll: sel => body.querySelectorAll(sel),
    getElementById: id => body.all().find(e => e.id === id) || null,
    createElement: tag => (tag === 'audio' ? new Audio(t) : new El(tag)),
    createTextNode: s => s,
    visibilityState: 'visible', hasFocus: () => true,
  };
  const win = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: '#/search' }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    // A local session; every other read is the test's (EW.api below).
    fetch: async url => {
      if (url !== '/api/v1/session') throw new Error('unexpected ' + url);
      return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
        workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: doc,
  };
  win.window = win; win.globalThis = win;
  vm.createContext(win);
  // Page chrome the boot render paints (header, main, …) is a throwaway
  // node, as in the other frontend loaders; the players answer from the tree.
  const real = doc.querySelector;
  doc.querySelector = sel => real(sel) || (/^#search-audio/.test(sel) ? null : new El('div'));
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.addEventListener = () => {};
  win.EW.current.route = '#/search';
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  win.EW.toast = msg => t.toasts.push(msg);
  t.realApi = win.EW.api;
  win.EW.api = async p => {
    const m = /^\/customer\/records\/(.+)$/.exec(p);
    if (!m) throw new Error('unexpected ' + p);
    return new Promise(resolve => t.pending.push(() => resolve({ record: { media: { playback: 'local', source_offset_s: 0 }, selected_bounds: { start_s: 1320, end_s: 1380 } } })));
  };
  Object.assign(t, { win, doc, body });
  // An event: the document's capture listeners, then the element's own.
  t.fire = (a, type, rs) => {
    if (rs !== undefined) a.readyState = rs;
    if (type === 'ended') { a.ended = true; a.paused = true; }
    if (type === 'error') { a.error = { code: 4 }; a.paused = true; for (const w of a.waiters.splice(0)) w.reject(Object.assign(new Error('not supported'), { name: 'NotSupportedError' })); }
    if (type === 'playing') for (const w of a.waiters.splice(0)) w.resolve();
    const ev = { type, target: a };
    for (const fn of t.docListeners[type] || []) fn(ev);
    for (const fn of a.listeners[type] || []) fn(ev);
  };
  // Browsers fire pause as a task after pause(): queued, sent on flush.
  t.queued = [];
  t.queue = (a, type) => t.queued.push([a, type]);
  t.flush = () => { for (const [a, type] of t.queued.splice(0)) t.fire(a, type); };
  await drain(40); // boot reads the session
  return t;
}

// The evidence player: its box, play button, status, error line and audio.
function evidencePlayer(t, id = 'ev') {
  const box = new El('section', { dataset: { clipId: id, start: '1320', end: '1380', offset: '0', lead: '1317' } });
  box.classes.add('ew-player');
  const btn = new El('button', { dataset: { action: 'clip-toggle', clip: id } });
  btn.classes.add('ew-play'); btn.setAttribute('aria-label', 'Play clip, 22:00 to 23:00');
  btn.innerHTML = '<svg><path d="M3 1.5v9l7.5-4.5z"/></svg>';
  const status = new El('p', { dataset: { clipStatus: '' } });
  const err = new El('p', { hidden: true }); err.classes.add('ew-clip-error');
  const audio = new Audio(t); audio.id = 'clip-' + id; audio.dataset.clip = id;
  box.append(btn, err, status, audio);
  t.body.append(box);
  return { box, btn, status, err, audio };
}
// Search: the card's play and the inspector's play of one ref.
function searchButtons(t, ref) {
  const mk = () => { const b = new El('button', { dataset: { action: 'search-play', ref } }); b.classes.add('ew-play');
    b.setAttribute('aria-label', 'Play clip, 22:00 to 23:00'); b.innerHTML = '<svg><path d="M3 1.5v9l7.5-4.5z"/></svg>'; return b; };
  const card = mk(), inspector = mk();
  t.body.append(new El('article').append(card), new El('aside').append(inspector));
  return [card, inspector];
}

const icon = b => (/<rect/.test(b.innerHTML) ? 'pause' : /<path/.test(b.innerHTML) ? 'play' : 'none');
function look(b) {
  return { icon: icon(b), loading: b.classes.has('is-loading'), busy: b.getAttribute('aria-busy') === 'true',
    pressed: b.getAttribute('aria-pressed') === 'true', label: b.getAttribute('aria-label') };
}
function isLoading(b, what) {
  const l = look(b);
  assert.ok(l.loading, `${what}: no loading look (is-loading) ${JSON.stringify(l)}`);
  assert.ok(l.busy, `${what}: not aria-busy ${JSON.stringify(l)}`);
  assert.notEqual(l.icon, 'pause', `${what}: Pause while loading`);
  assert.equal(l.pressed, false, `${what}: pressed while loading`);
}
function isPlaying(b, what) {
  const l = look(b);
  assert.equal(l.icon, 'pause', `${what}: no Pause icon while playing ${JSON.stringify(l)}`);
  assert.ok(l.pressed, `${what}: not aria-pressed while playing`);
  assert.equal(l.label, 'Pause clip', `${what}: label`);
  assert.ok(!l.loading && !l.busy, `${what}: still loading while playing ${JSON.stringify(l)}`);
}
function isIdle(b, what) {
  const l = look(b);
  assert.equal(l.icon, 'play', `${what}: no Play icon ${JSON.stringify(l)}`);
  assert.ok(!l.pressed && !l.loading && !l.busy, `${what}: not idle ${JSON.stringify(l)}`);
  assert.match(l.label || '', /^Play clip/, `${what}: label`);
}
const searchStatus = t => { const s = t.doc.querySelector('#search-audio-status'); return s ? s.textContent : null; };
const clipToggle = (t, id = 'ev') => t.win.EWViews.handleAction({ dataset: { action: 'clip-toggle', clip: id }, closest: () => null });
const searchPlay = (t, ref) => t.win.EWViews.handleAction({ dataset: { action: 'search-play', ref }, closest: () => null });
const REF = 'sr1:fec09fbf13aa4a1c99d48eb3eb7664fc';

// An audio whose src assignment reloads as a browser does: the position
// goes back to the #t= start of the new source (as the G2-08 tests).
function reloadingAudio(t) {
  const make = t.doc.createElement;
  t.doc.createElement = tag => {
    const el = make(tag);
    if (tag !== 'audio') return el;
    let src = '';
    Object.defineProperty(el, 'src', { get: () => src, set: v => {
      src = String(v); if (!src) return;
      el.readyState = 0;
      const m = /#t=([\d.]+)/.exec(src); el.currentTime = m ? Number(m[1]) : 0;
    } });
    return el;
  };
}
const focused = [];
El.prototype.focus = function () { focused.push(this); };
const press = (t, el) => t.win.EWViews.handleAction(el);
// A Search card's play button and its "0:35 clip" length, as ui.sourceCard draws them.
function searchCard(t, ref, from, to) {
  const b = new El('button', { dataset: { action: 'search-play', ref, from: String(from), to: String(to) } });
  b.classes.add('ew-play'); b.setAttribute('aria-label', 'Play clip');
  b.innerHTML = '<svg><path d="M3 1.5v9l7.5-4.5z"/></svg>';
  const len = new El('span'); len.classes.add('ew-clip-len'); len.textContent = '0:35 clip';
  t.body.append(new El('article').append(b, len));
  return { btn: b, len };
}
// The record read answers bounds 1320-1380 (the clip file's window), recording offset 0.
async function playCard(t, card) {
  const pressed = press(t, card.btn); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  return audio;
}

/* -- 1. Span ------------------------------------------------------------ */

test('span: Search card, list row and inspector play buttons name the span they show (data-from / data-to)', async () => {
  const t = await page();
  const ui = t.win.EWUI;
  const item = { ref: REF, record_kind: 'evidence', status: 'finding', show_title: 'Quiet Hours', ep_title: 'E1', pub_date: '2026-10-07',
    quotation_span: [1320, 1380], read: { brand: { key: 'quince', display: 'Quince' }, display_span: [1045, 1080], has_clip: true } };
  for (const [name, html] of [['card', ui.sourceCard(item, { index: 0 })], ['row', ui.resultRow(item, { index: 0 })]]) {
    const btn = /<button[^>]*data-action="search-play"[^>]*>/.exec(html);
    assert.ok(btn, `${name}: a play button`);
    assert.match(btn[0], / data-from="1045" data-to="1080"/, `${name}: the span it shows: ${btn[0]}`);
  }
  assert.match(ui.sourceCard(item, { index: 0 }), /0:35 clip/, 'the card says the span length');
});

test('span: a card plays its span, not the clip window: from 3 s before its start, "0:00 of 0:35", paused at its end', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1345, 1380);
  const audio = await playCard(t, card);
  assert.match(audio.src, /\/media#t=1342\.0$/, `starts 3 s before the span start, not the window's: ${audio.src}`);
  assert.equal(audio.dataset.begin, '1345');
  assert.equal(audio.dataset.end, '1380');
  // The lead-in reads 0:00 of the span's length.
  audio.currentTime = 1342; t.fire(audio, 'timeupdate');
  assert.equal(card.len.textContent, '0:00 of 0:35');
  audio.currentTime = 1357.2; t.fire(audio, 'timeupdate');
  assert.equal(card.len.textContent, '0:12 of 0:35');
  audio.currentTime = 1380; t.fire(audio, 'timeupdate');
  assert.equal(audio.paused, true, 'it pauses at the span end');
});

test('span: a button with no span (an older render) still plays the record window', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const [btn] = searchButtons(t, REF);
  const pressed = press(t, btn); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  assert.match(audio.src, /\/media#t=1317\.0$/);
  assert.equal(audio.dataset.begin, '1320');
  assert.equal(audio.dataset.end, '1380');
});

test('span: the recording offset applies to the span as to the window (episode seconds -> file seconds)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  t.win.EW.api = async () => new Promise(resolve => t.pending.push(() => resolve({ record: { media: { playback: 'local', source_offset_s: 1300 }, selected_bounds: { start_s: 1320, end_s: 1380 } } })));
  const card = searchCard(t, REF, 1345, 1380);
  const audio = await playCard(t, card);
  assert.match(audio.src, /\/media#t=42\.0$/);
  assert.equal(audio.dataset.begin, '45');
  assert.equal(audio.dataset.end, '80');
});

/* DET-PLAN (LEDGER-LEAD3-DET-1): the Search card plays the read's
   display_span when the record has one; without one (data before
   DET-RI) it plays the read's current span, as before. One helper,
   EWUI.playSpan, decides it for the card and the player (CLIP-STOP
   reuses it for the playback end). */
const recordWith = (t, read) => { t.win.EW.api = async () => new Promise(resolve => t.pending.push(() => resolve({ record: {
  media: { playback: 'local', source_offset_s: 0 }, selected_bounds: { start_s: 1320, end_s: 1380 }, ...(read === undefined ? {} : { read }) } }))); };

test('display_span: the record\'s read has a display_span: the card plays it (not the named span, not the window)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  recordWith(t, { display_span: [1350, 1371] });
  const audio = await playCard(t, searchCard(t, REF, 1345, 1380));
  assert.match(audio.src, /\/media#t=1347\.0$/, `from 3 s before the display span start: ${audio.src}`);
  assert.equal(audio.dataset.begin, '1350');
  assert.equal(audio.dataset.end, '1371');
});

test('display_span: none on the record (before DET-RI): the card plays the read\'s current span, as before', async (tc) => {
  // FIX2 (CS-3): a historical read with no display_span (its evidence row is gone) keeps this rule too.
  for (const read of [undefined, { display_span: [null, null] }, { historical: true, current_ref: 'sr1:' + 'c'.repeat(32) }]) {
    const t = await page();
    tc.after(() => { for (const r of t.pending.splice(0)) r(); });
    recordWith(t, read);
    const audio = await playCard(t, searchCard(t, REF, 1345, 1380));
    assert.equal(`${audio.dataset.begin}-${audio.dataset.end}`, '1345-1380', `the span the card names, read ${JSON.stringify(read)}`);
    // No span on the button (an older render): the record's window.
    const t2 = await page();
    tc.after(() => { for (const r of t2.pending.splice(0)) r(); });
    recordWith(t2, read);
    const [btn] = searchButtons(t2, REF);
    const pressed = press(t2, btn); await drain();
    t2.pending.shift()(); await drain();
    const a2 = t2.doc.querySelector('#search-audio');
    for (const [type, rs] of ORDERS.chromium) t2.fire(a2, type, rs);
    await pressed; await drain();
    assert.equal(`${a2.dataset.begin}-${a2.dataset.end}`, '1320-1380', `the record window, read ${JSON.stringify(read)}`);
  }
});

test('display_span: EWUI.playSpan is the one place: display_span, else the current span; the card and the player both use it', async (tc) => {
  const t = await page();
  const ui = t.win.EWUI;
  assert.equal(typeof ui.playSpan, 'function', 'EWUI.playSpan');
  assert.deepEqual([...ui.playSpan({ display_span: [1350, 1371] }, [1320, 1380])], [1350, 1371]);
  assert.deepEqual([...ui.playSpan({ display_span: [null, null] }, [1320, 1380])], [1320, 1380]);
  assert.deepEqual([...ui.playSpan(null, ['1320', '1380'])], [1320, 1380]);
  assert.equal(ui.playSpan(null, null), null);
  // A card without a display_span names and plays its quotation span.
  const item = { ref: REF, record_kind: 'evidence', status: 'finding', show_title: 'Quiet Hours', ep_title: 'E1', pub_date: '2026-10-07',
    quotation_span: [1320, 1380], read: { brand: { key: 'quince', display: 'Quince' }, display_span: [null, null], has_clip: true } };
  assert.match(/<button[^>]*data-action="search-play"[^>]*>/.exec(ui.sourceCard(item, { index: 0 }))[0], / data-from="1320" data-to="1380"/);
  // The Search player asks the helper (a stand-in answer is what plays).
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const seen = [];
  ui.playSpan = (read, current) => { seen.push([read, current]); return [1300, 1310]; };
  recordWith(t, { display_span: [1350, 1371] });
  const audio = await playCard(t, searchCard(t, REF, 1345, 1380));
  assert.equal(`${audio.dataset.begin}-${audio.dataset.end}`, '1300-1310', 'the Search player plays what EWUI.playSpan says');
  assert.deepEqual(JSON.parse(JSON.stringify(seen[0])), [{ display_span: [1350, 1371] }, [1345, 1380]]);
});

/* -- 2. Resume ---------------------------------------------------------- */

test('resume, Search: Play goes back to where it paused after WebKit moved the paused clip to its #t= start', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1317.23; t.fire(audio, 'timeupdate');
  await press(t, card.btn); t.flush(); await drain();
  assert.equal(audio.paused, true, 'fixture: paused');
  // WebKit, about 100 ms later: the time is the #t= start again, no seeking.
  audio.currentTime = 1317; t.fire(audio, 'timeupdate');
  const plays = audio.plays;
  const resumed = press(t, card.btn); await drain();
  assert.equal(t.pending.length, 0, 'no new record read');
  assert.equal(audio.plays, plays + 1, 'it plays');
  assert.equal(audio.currentTime, 1317.23, 'Play restarted from the #t= start instead of where it paused');
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await resumed; await drain();
});

test('resume, Search: a clip still where it paused is not moved (no seek), and a new clip starts at its own start', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  reloadingAudio(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1330.5; t.fire(audio, 'timeupdate');
  await press(t, card.btn); t.flush(); await drain();
  let sets = 0;
  const desc = { get: () => 1330.5, set: () => { sets++; } };
  Object.defineProperty(audio, 'currentTime', { ...desc, configurable: true });
  const resumed = press(t, card.btn); await drain();
  assert.equal(sets, 0, 'a resume where it paused seeks nowhere');
  delete audio.currentTime; audio.currentTime = 1330.5; audio.readyState = 4;
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4); await resumed; await drain();
  // Pause, then another ad: its own start, not this pause place.
  await press(t, card.btn); t.flush(); await drain();
  const other = searchCard(t, 'sr1:second', 1400, 1440);
  const pressed = press(t, other.btn); await drain();
  t.pending.shift()(); await drain();
  assert.match(audio.src, /second\/media#t=1397\.0$/);
  assert.equal(audio.dataset.pausedAt, undefined, 'the old pause place outlived its source');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  assert.equal(audio.currentTime, 1397, 'the other ad did not start at its own start');
});

test('resume, evidence: Play goes back to where it paused after WebKit moved the paused clip to its #t= start', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1317.33; t.fire(p.audio, 'timeupdate');
  await clipToggle(t); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'fixture: paused');
  p.audio.currentTime = 1317; t.fire(p.audio, 'timeupdate');
  await clipToggle(t); await drain();
  assert.equal(p.audio.paused, false, 'it plays');
  assert.equal(p.audio.currentTime, 1317.33, 'Play restarted from the #t= start instead of where it paused');
});

/* A429-SCR-R2-03, the rapid-press replay: a pause during WebKit's first
   #t= seek (pressed while still loading) leaves the clip stuck: it never
   played (audio.played is empty), and the next play() fires playing, then
   pause and ended where it stands, ~150 ms later (scratch host, headless
   WebKit, 2 of 2 runs; a seek to the same time does not clear it, a seek to
   a new place does). Play then seeks 0.01 s on. A clip that has played is
   not moved (the test above). */
const neverPlayed = a => { a.played = { length: 0 }; };
test('resume, Search: a pause before the clip ever played: Play seeks to a new place (WebKit would end it at once)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  neverPlayed(audio);
  audio.currentTime = 1317; t.fire(audio, 'timeupdate');
  await press(t, card.btn); t.flush(); await drain();
  assert.equal(audio.dataset.pausedAt, '1317', 'fixture: paused at the #t= start');
  const resumed = press(t, card.btn); await drain();
  assert.equal(audio.currentTime, 1317.01, 'Play left the never-played clip where WebKit ends it at once');
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await resumed; await drain();
});

test('resume, evidence: a pause before the clip ever played: Play seeks to a new place (WebKit would end it at once)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
  neverPlayed(p.audio);
  p.audio.currentTime = 1317; t.fire(p.audio, 'timeupdate');
  await clipToggle(t); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'fixture: paused');
  await clipToggle(t); await drain();
  assert.equal(p.audio.paused, false, 'it plays');
  assert.equal(p.audio.currentTime, 1317.01, 'Play left the never-played clip where WebKit ends it at once');
});

test('resume, evidence: a seek while paused (±5 s, Play from) plays from where the seek went, not the pause place', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1330; t.fire(p.audio, 'timeupdate');
  await clipToggle(t); t.flush(); await drain();
  await press(t, { dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null }); await drain();
  assert.equal(p.audio.currentTime, 1335, 'fixture: +5 s');
  await clipToggle(t); await drain();
  assert.equal(p.audio.currentTime, 1335, 'Play went back to the pause place after a seek');
  t.fire(p.audio, 'playing', 4); await drain();
  // Played to the end (paused there): Play still replays from the lead-in (CLIPUI-R9-01).
  p.audio.currentTime = 1380; t.fire(p.audio, 'timeupdate'); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'fixture: paused at the end');
  await clipToggle(t); await drain();
  assert.equal(p.audio.currentTime, 1317, 'at the end Play replays from the lead-in');
});

/* -- 3. The playing line in view --------------------------------------- */

// The evidence transcript: lines at fixed page positions in a 844 px view.
function transcript(t, p, tops) {
  const list = new El('ol'); list.classes.add('transcript'); list.dataset.clipLines = 'ev';
  const lines = tops.map(([start, end, top], i) => {
    const li = new El('li', { dataset: { segment: String(i + 1), start: String(start), end: String(end) } });
    li.scrolls = [];
    li.getBoundingClientRect = () => ({ top: top - t.scrollY, bottom: top - t.scrollY + 52, height: 52 });
    li.scrollIntoView = opts => { li.scrolls.push(opts); t.scrollY = top - 400; };
    list.append(li);
    return li;
  });
  t.body.append(list);
  return lines;
}
function viewport(t, { height = 844, reduce = false } = {}) {
  t.scrollY = 0;
  t.win.innerHeight = height;
  t.win.matchMedia = q => ({ matches: /prefers-reduced-motion: reduce/.test(q) ? reduce : false, media: q });
}
const current = lines => lines.filter(li => li.getAttribute('aria-current') === 'true').map(li => li.dataset.segment);
async function playingEvidence(t) {
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.chromium) t.fire(p.audio, type, rs);
  await drain();
  return p;
}

for (const [width, height] of [[390, 844], [1440, 900]]) {
  test(`line in view (${width}): the playing line off screen scrolls into view, smooth, focus stays; it alone has aria-current`, async () => {
    const t = await page();
    viewport(t, { height });
    const lines = transcript(t, null, [[1316, 1322, 1546], [1322, 1330, 1600], [1330, 1340, 2400]]);
    const p = await playingEvidence(t);
    focused.length = 0;
    p.audio.currentTime = 1318; t.fire(p.audio, 'timeupdate');
    assert.deepEqual(current(lines), ['1']);
    assert.equal(lines[0].scrolls.length, 1, 'the playing line below the view did not scroll into view');
    assert.equal(lines[0].scrolls[0].behavior, 'smooth');
    assert.equal(lines[0].scrolls[0].block, 'center');
    // The same line again, and the next line already in view: no scroll.
    p.audio.currentTime = 1319; t.fire(p.audio, 'timeupdate');
    p.audio.currentTime = 1323; t.fire(p.audio, 'timeupdate');
    assert.deepEqual(current(lines), ['2'], 'aria-current moved to the next line and left the previous one');
    assert.equal(lines[0].scrolls.length + lines[1].scrolls.length, 1, 'scrolled for a line already in view');
    // A long clip: the line after runs off screen, and comes into view.
    p.audio.currentTime = 1331; t.fire(p.audio, 'timeupdate');
    assert.deepEqual(current(lines), ['3']);
    assert.equal(lines[2].scrolls.length, 1, 'a later playing line off screen did not scroll into view');
    assert.deepEqual(focused, [], 'focus moved');
  });
}

test('line in view: with reduced motion the scroll is instant', async () => {
  const t = await page();
  viewport(t, { reduce: true });
  const lines = transcript(t, null, [[1316, 1322, 1546]]);
  const p = await playingEvidence(t);
  p.audio.currentTime = 1318; t.fire(p.audio, 'timeupdate');
  assert.equal(lines[0].scrolls.length, 1);
  assert.equal(lines[0].scrolls[0].behavior, 'auto');
});

test('line in view: a seek while paused moves the highlight but does not scroll the page', async () => {
  const t = await page();
  viewport(t);
  const lines = transcript(t, null, [[1316, 1322, 1546], [1322, 1330, 1600]]);
  const p = evidencePlayer(t);
  p.audio.readyState = 4; p.audio.currentTime = 1323; t.fire(p.audio, 'seeked');
  assert.deepEqual(current(lines), ['2']);
  assert.equal(lines[1].scrolls.length, 0, 'a paused seek scrolled the page');
});

/* -- 4. A429-R1-04: a Search clip that fails mid-play says so ----------- */

test('A429-R1-04: a Search clip that fails after it started playing shows the evidence player\'s words, then Play', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1330; t.fire(audio, 'timeupdate');
  assert.deepEqual(t.toasts, [], 'fixture');
  t.fire(audio, 'error', 4); await drain();
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'a mid-play failure was silent');
  isIdle(card.btn, 'after the failure');
  assert.equal(audio.src, '', 'AUDIO-429: the failed clip stops its loader');
});

test('A429-R1-04: a stall then a failure mid-play says so once; a failed start keeps only its own toast', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  t.fire(audio, 'waiting', 2); t.fire(audio, 'error', 2); await drain();
  assert.deepEqual(t.toasts, ['The clip didn’t load.']);
  // A failed start: the play's rejection speaks, the error event does not.
  t.toasts.length = 0;
  const other = searchCard(t, 'sr1:second', 1400, 1440);
  const pressed = press(t, other.btn); await drain();
  t.pending.shift()(); await drain();
  t.fire(audio, 'play', 0); t.fire(audio, 'error', 0);
  await pressed; await drain();
  assert.deepEqual(t.toasts, ['The clip did not start. Try again, or open the evidence.']);
});

/* WebKit fires no error when a later media read fails mid-play: it asks
   again, stalls, then fires pause and ended where it stands, long before
   the file's end (scratch host, headless WebKit, a 500/404/429 on every
   later read: ended at 0:51 of a 1:08 file, err null). An ended more than
   1 s before the file's end is that failure; an ended at the file's end
   (a span running past a short file) is not. */
test('A429-R1-04: WebKit\'s early ended mid-play (no error event) shows the evidence player\'s words; an end at the file end does not', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1385;
  audio.currentTime = 1351; t.fire(audio, 'timeupdate');
  t.fire(audio, 'stalled', 4);
  audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'WebKit\'s mid-play failure was silent');
  isIdle(card.btn, 'after the failure');
  // A clip file that ends before the span end: its end is no failure.
  const t2 = await page();
  tc.after(() => { for (const r of t2.pending.splice(0)) r(); });
  const card2 = searchCard(t2, REF, 1320, 1380);
  const a2 = await playCard(t2, card2);
  a2.duration = 1379.6;
  a2.currentTime = 1379.6; t2.fire(a2, 'timeupdate');
  a2.paused = true; t2.fire(a2, 'pause'); t2.fire(a2, 'ended'); await drain();
  assert.deepEqual(t2.toasts, [], 'an end at the file end spoke');
  isIdle(card2.btn, 'after the end');
});

/* At that early end WebKit also sets duration to where it stopped (0:51
   of a 1:08 file: duration 68 -> 51 with pause), so the file's length is no
   reference: the span the player plays is (data-end, file seconds). */
test('A429-R1-04: WebKit\'s early ended with duration set to where it stopped still speaks; an end at the span end does not', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1385;
  audio.currentTime = 1351; t.fire(audio, 'timeupdate');
  audio.duration = 1351; audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'WebKit\'s mid-play failure was silent');
  const t2 = await page();
  tc.after(() => { for (const r of t2.pending.splice(0)) r(); });
  const card2 = searchCard(t2, REF, 1320, 1380);
  const a2 = await playCard(t2, card2);
  a2.duration = 1379.5; a2.currentTime = 1379.5; a2.paused = true; t2.fire(a2, 'pause'); t2.fire(a2, 'ended'); await drain();
  assert.deepEqual(t2.toasts, [], 'an end within 1 s of the span end spoke');
});

/* -- M11-PLAYER-FIX1 (M11-R1-01..04, -08; M11-SCR-R1-01/02) ------------
   A refused media read mid-play may fire no error. Headless WebKit stalls
   where its buffer ran out (no error, pause or ended in 93 s) and its
   loader asks again 300-500 times a second; Chromium shows "Loading…"
   about 30 s before its error. One guard for both players (LEDGER-LEAD3-
   M11-1): a playing clip whose time has not moved for 10 s, or that ends
   early, stops (no src, load(), so the loader stops asking), says the
   existing "The clip didn’t load." once and is back to Play. A reader's
   pause, a seek and the normal end never count. */

// A clock the page's timers run on; advance(ms) runs what falls due.
function fakeClock(t) {
  let now = 0, seq = 0;
  const timers = [];
  t.win.setTimeout = (fn, ms = 0) => { const id = ++seq; timers.push({ id, at: now + (Number(ms) || 0), fn }); return id; };
  t.win.clearTimeout = id => { const i = timers.findIndex(x => x.id === id); if (i >= 0) timers.splice(i, 1); };
  return {
    advance(ms) {
      const end = now + ms;
      for (;;) {
        timers.sort((a, b) => a.at - b.at || a.id - b.id);
        const next = timers[0];
        if (!next || next.at > end) break;
        timers.shift(); now = next.at; next.fn();
      }
      now = end;
    },
  };
}
// WebKit's media loader after a refused read: while the element has a
// source it asks again (here every 100 ms); each ask is refused.
function refusedLoader(clock, audio) {
  const l = { asks: 0, run(ms) { for (let i = 0; i < ms; i += 100) { if (audio.src) l.asks++; clock.advance(100); } } };
  return l;
}
const MEDIA = '/api/v1/customer/records/x/media#t=1317.0';

test('FIX1 stall, Search: no progress for 10 s while playing stops the clip (no src), says "The clip didn’t load." once, back to Play; at most 1 read after', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1385; audio.currentTime = 1330; t.fire(audio, 'timeupdate');
  // The next read is refused: WebKit stalls where its buffer ran out.
  t.fire(audio, 'waiting', 2); t.fire(audio, 'stalled', 2);
  const loader = refusedLoader(clock, audio);
  loader.run(9900);
  assert.deepEqual(t.toasts, [], 'it stopped before 10 s without progress');
  assert.ok(audio.src, 'fixture: still loading at 9.9 s');
  loader.run(100);
  assert.equal(audio.src, '', 'a clip stalled 10 s kept its src: the loader goes on asking');
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'the stall was silent');
  isIdle(card.btn, 'after the stall');
  const before = loader.asks;
  loader.run(10000);
  assert.ok(loader.asks - before <= 1, `refused reads in the 10 s after the stop: ${loader.asks - before}`);
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'said more than once');
});

test('FIX1 stall, evidence: no progress for 10 s while playing stops the clip (no src), shows its error line, back to Play; at most 1 read after', async () => {
  const t = await page();
  const clock = fakeClock(t);
  const p = evidencePlayer(t);
  p.audio.src = MEDIA;
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
  p.audio.duration = 1385; p.audio.currentTime = 1330; t.fire(p.audio, 'timeupdate');
  t.fire(p.audio, 'waiting', 2);
  const loader = refusedLoader(clock, p.audio);
  loader.run(9900);
  assert.equal(p.err.hidden, true, 'it stopped before 10 s without progress');
  loader.run(100);
  assert.equal(p.audio.src, '', 'a clip stalled 10 s kept its src: the loader goes on asking');
  assert.equal(p.err.hidden, false, 'the stall showed no error line');
  assert.ok(p.box.classes.has('is-error'));
  isIdle(p.btn, 'after the stall');
  const before = loader.asks;
  loader.run(10000);
  assert.ok(loader.asks - before <= 1, `refused reads in the 10 s after the stop: ${loader.asks - before}`);
  // Retry/Play puts the clip back (AUDIO-429 clipResume).
  await clipToggle(t); await drain();
  assert.equal(p.audio.src, MEDIA, 'Play after the stop did not load the clip again');
  assert.equal(p.err.hidden, true);
});

test('FIX1 stall: a stall that recovers before 10 s says nothing; a seek starts the count again', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1330; t.fire(audio, 'timeupdate');
  t.fire(audio, 'waiting', 2);
  clock.advance(9000);
  audio.readyState = 4; audio.currentTime = 1330.25; t.fire(audio, 'timeupdate');
  clock.advance(9500);
  assert.deepEqual(t.toasts, [], 'a recovered stall spoke');
  assert.ok(audio.src);
  // Evidence: stalled 6 s, then a seek (+5 s): 10 s from the seek, not from the stall.
  const t2 = await page();
  const c2 = fakeClock(t2);
  const p = evidencePlayer(t2);
  p.audio.src = MEDIA;
  await clipToggle(t2); await drain();
  for (const [type, rs] of ORDERS.webkit) t2.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1330; t2.fire(p.audio, 'timeupdate');
  t2.fire(p.audio, 'waiting', 2);
  c2.advance(6000);
  await press(t2, { dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null });
  t2.fire(p.audio, 'seeking', 2);
  c2.advance(6000);
  assert.equal(p.err.hidden, true, 'a seek counted as part of the stall');
  assert.ok(p.audio.src);
  c2.advance(4000);
  assert.equal(p.err.hidden, false, 'no stop 10 s after the seek');
});

test('FIX1 stall: a reader\'s pause longer than 10 s is no stall; Play then resumes with no message', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1330; t.fire(audio, 'timeupdate');
  await press(t, card.btn); t.flush(); await drain();
  assert.equal(audio.paused, true, 'fixture: paused');
  clock.advance(15000);
  assert.deepEqual(t.toasts, [], 'a reader\'s pause counted as a stall');
  assert.ok(audio.src, 'a reader\'s pause stopped the clip');
  const resumed = press(t, card.btn); await drain();
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await resumed; await drain();
  for (let s = 1; s <= 12; s++) { audio.currentTime = 1330 + s; t.fire(audio, 'timeupdate'); clock.advance(1000); }
  assert.deepEqual(t.toasts, [], 'a resumed clip spoke');
  isPlaying(card.btn, 'resumed');
  // Evidence: the same.
  const t2 = await page();
  const c2 = fakeClock(t2);
  const p = evidencePlayer(t2);
  p.audio.src = MEDIA;
  await clipToggle(t2); await drain();
  for (const [type, rs] of ORDERS.webkit) t2.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1330; t2.fire(p.audio, 'timeupdate');
  await clipToggle(t2); t2.flush(); await drain();
  c2.advance(15000);
  assert.equal(p.err.hidden, true, 'a reader\'s pause counted as a stall (evidence)');
  assert.ok(p.audio.src);
});

test('FIX1 stall: the normal end (paused at the span end) is no stall', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1380; t.fire(audio, 'timeupdate'); t.flush(); await drain();
  assert.equal(audio.paused, true, 'fixture: paused at the span end');
  clock.advance(20000);
  assert.deepEqual(t.toasts, []);
  isIdle(card.btn, 'after the end');
});

/* M11-R1-01 (issue a): the early end stops the clip too (no src), on both players. */
test('FIX1 early end, Search: WebKit\'s early ended also stops the clip (no src)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1385; audio.currentTime = 1351; t.fire(audio, 'timeupdate');
  audio.duration = 1351; audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
  assert.deepEqual(t.toasts, ['The clip didn’t load.']);
  assert.equal(audio.src, '', 'the early end kept src: the loader may go on asking');
});

test('FIX1 early end, evidence: an end more than 1 s before the span end stops the clip and shows its error line', async () => {
  const t = await page();
  fakeClock(t);
  const p = evidencePlayer(t);
  p.audio.src = MEDIA;
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
  p.audio.duration = 1385; p.audio.currentTime = 1351; t.fire(p.audio, 'timeupdate');
  p.audio.duration = 1351; p.audio.paused = true; t.fire(p.audio, 'pause'); t.fire(p.audio, 'ended'); await drain();
  assert.equal(p.err.hidden, false, 'the evidence player\'s early end was silent');
  assert.equal(p.audio.src, '', 'the early end kept src');
  isIdle(p.btn, 'after the early end');
});

/* M11-R1-02: the early end is measured against min(span end, file end). */
test('FIX1 early end: a span past the clip file\'s end does not speak at the file\'s normal end (both players)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1370; audio.currentTime = 1369.8; t.fire(audio, 'timeupdate');
  audio.currentTime = 1370; audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
  assert.deepEqual(t.toasts, [], 'the file\'s normal end, 10 s before the span end, spoke');
  const t2 = await page();
  fakeClock(t2);
  const p = evidencePlayer(t2);
  p.audio.src = MEDIA;
  await clipToggle(t2); await drain();
  for (const [type, rs] of ORDERS.webkit) t2.fire(p.audio, type, rs);
  await drain();
  p.audio.duration = 1370; p.audio.currentTime = 1369.8; t2.fire(p.audio, 'timeupdate');
  p.audio.currentTime = 1370; p.audio.paused = true; t2.fire(p.audio, 'pause'); t2.fire(p.audio, 'ended'); await drain();
  assert.equal(p.err.hidden, true, 'the evidence file\'s normal end showed the error line');
});

/* M11-R1-04: the four guards with no test. */
test('FIX1 guard R-03: a pause that comes with the end keeps no pause place (evidence, no read end)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  p.box.dataset.end = '';
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.chromium) t.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1400; p.audio.ended = true; p.audio.paused = true;
  t.fire(p.audio, 'pause'); t.fire(p.audio, 'ended'); await drain();
  assert.equal(p.audio.dataset.pausedAt, undefined, 'the end kept a pause place: Play would seek to the file end and end at once');
});

test('FIX1 guard R-04: a used pause place is dropped, so after a natural end Play does not go back to it', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  p.box.dataset.end = '';
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.chromium) t.fire(p.audio, type, rs);
  await drain();
  p.audio.currentTime = 1330; t.fire(p.audio, 'timeupdate');
  await clipToggle(t); t.flush(); await drain();
  await clipToggle(t); await drain();
  t.fire(p.audio, 'playing', 4); await drain();
  assert.equal(p.audio.dataset.pausedAt, undefined, 'the pause place outlived its use');
  p.audio.currentTime = 1400; p.audio.ended = true; p.audio.paused = true;
  t.fire(p.audio, 'pause'); t.fire(p.audio, 'ended'); await drain();
  await clipToggle(t); await drain();
  assert.notEqual(p.audio.currentTime, 1330, 'Play after the end went back to an old pause place');
});

test('FIX1 guard R-07: the inspector\'s play button names the read\'s display span (data-from / data-to)', async () => {
  const t = await page();
  const item = { ref: REF, record_kind: 'evidence', status: 'finding', show_title: 'Quiet Hours', ep_title: 'E1', pub_date: '2026-10-07',
    quotation_span: [1320, 1380], read: { brand: { key: 'quince', display: 'Quince' }, display_span: [1045, 1080], has_clip: true } };
  const btn = /<button[^>]*data-action="search-play"[^>]*>/.exec(t.win.EWViews.inspectorHtml(item, { q: '' }, 1));
  assert.ok(btn, 'an inspector play button');
  assert.match(btn[0], / data-from="1045" data-to="1080"/);
});

test('FIX1 guard R-09: resultFacts drops a historical read (its span and clip are not the current read\'s)', async () => {
  const t = await page();
  const f = t.win.EWUI.resultFacts({ ref: REF, quotation_span: [1320, 1380], read: { historical: true, display_span: [1045, 1080], has_clip: true } });
  assert.equal(f.read, null);
  assert.deepEqual([...f.span], [1320, 1380]);
  assert.equal(f.playable, false);
});

/* M11-R1-08: a zero-length or reversed display span is missing. */
test('FIX1 playSpan: a zero-length or reversed span (end <= start) is missing', async () => {
  const t = await page();
  const ps = t.win.EWUI.playSpan;
  assert.deepEqual([...ps({ display_span: [1350, 1350] }, [1320, 1380])], [1320, 1380], 'a zero-length display span was kept');
  assert.deepEqual([...ps({ display_span: [1371, 1350] }, [1320, 1380])], [1320, 1380]);
  assert.equal(ps(null, [1320, 1320]), null, 'a zero-length current span was kept');
});

/* M11-R1-03: the Brand page rows that name display_span || quotation_span
   use EWUI.playSpan (libPlay, offer sort, offerFigure, readRow). */
async function brandHtml(items) {
  const t = await page();
  const detail = { brand: { brand: 'acme', key: 'acme', display: 'Acme', detections: 2, ad_breaks: 2, confirmed_occurrences: [], findings: [], offer_sequence: [],
    episodes: 2, shows: 2, last_heard: '2026-10-09',
    drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['acme'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null } } } };
  t.win.EW.api = async p => {
    if (p === '/customer/brands/acme') return detail;
    if (p.startsWith('/customer/search?')) return { items, total: items.length, total_relation: 'exact' };
    if (p.startsWith('/customer/summary?')) return { cells: [], total_relation: 'exact' };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    throw new Error('unexpected ' + p);
  };
  t.win.EW.header = () => {};
  const main = t.doc.querySelector('#main');
  t.win.location.hash = '#/brand/acme'; t.win.EW.current.route = '#/brand/acme';
  await t.win.EWViews.render(); await drain(40);
  return main.innerHTML;
}
test('FIX1 Brand rows: a reversed display span names the quotation span (row time, play label, offer figure, offer order)', async () => {
  const brandRead = (ref, show, display_span, quotation_span, text) => ({ ref: 'sr1:' + ref.repeat(32), record_kind: 'evidence', status: 'finding', ep_title: 'Ep',
    show_title: show, pub_date: '2026-10-09', quotation_span, audio: 'available',
    read: { brand: { key: 'acme', display: 'Acme' }, display_span, has_clip: true, display_basis: 'checked', display_text: text, said: {} } });
  const a = brandRead('a', 'Show A', [500, 10], [10, 70], 'Acme saves you ten');
  const b = brandRead('b', 'Show B', [20, 80], [0, 90], 'Acme saves you twenty');
  const html = await brandHtml([b, a]);
  const rowA = (html.match(/<li class="lib-read">[\s\S]*?<\/li>/g) || []).find(r => /Show A/.test(r));
  assert.ok(rowA, 'row A drawn');
  assert.match(rowA, /<span class="lib-read-time"><span class="ew-time"[^>]*>0:10–1:10<\/span>/, 'readRow: the reversed display span named no time');
  assert.match(rowA, /aria-label="Play Acme ad, Show A, [^"]*, 0:10 to 1:10"/, 'libPlay: the label named no time');
  const earlier = /<figure class="lib-offer-read"><figcaption><b>Earlier ad[\s\S]*?<\/figure>/.exec(html);
  assert.ok(earlier, 'an offer pair');
  assert.match(earlier[0], /Show A/, 'offer sort: the reversed display span sorted by its own start');
  assert.match(earlier[0], /<span class="lib-offer-where"><span class="ew-time"[^>]*>0:10–1:10<\/span>/, 'offerFigure: the reversed display span named no time');
});

test('FIX1 stall: a clip that never starts (play asked, no progress for 10 s) stops and says "The clip didn’t load." once, both players', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  // Not awaited: without the stop its play() never settles.
  press(t, card.btn); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  t.fire(audio, 'play', 0); t.fire(audio, 'waiting', 0);
  clock.advance(10000); await drain();
  assert.equal(audio.src, '', 'a start that never came kept its src');
  assert.deepEqual(t.toasts, ['The clip didn’t load.'], 'not exactly one message');
  isIdle(card.btn, 'after the stop');
  const t2 = await page();
  const c2 = fakeClock(t2);
  const p = evidencePlayer(t2);
  p.audio.src = MEDIA;
  await clipToggle(t2); await drain();
  t2.fire(p.audio, 'play', 0); t2.fire(p.audio, 'waiting', 0);
  c2.advance(10000); await drain();
  assert.equal(p.audio.src, '');
  assert.equal(p.err.hidden, false);
  isIdle(p.btn, 'after the stop');
});

/* -- M11-PLAYER-FIX2 ------------------------------------------------------
   M11-SCR-R2-02: a quick Play, Pause, Play on a fresh clip could start at
   the file's 0:00, before the span (Search 5 of 32, evidence WebKit 5 of
   6): the Pause landed after src was set but before the #t= start applied,
   currentTime was still 0, keepPausePlace kept 0 and the next Play went
   back there. A pause place is never before where playback begins (the
   #t= start: the span start less the lead-in).
   LEDGER-LEAD3-CS-3 (player part): the server caps a historical ref at the
   current read's display span and the historical read block carries it
   (read.display_span, CLIP-STOP-FIX3); the players play that span, so the
   old ref's stored bounds never run past the cap. */
const BEFORE_T = [['play', 0], ['waiting', 0], ['loadstart', 0], ['loadedmetadata', 1]];

test('FIX2 M11-SCR-R2-02, Search: a Pause before the #t= start applies keeps the start, not 0:00; the next Play starts there', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const pressed = press(t, card.btn); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  assert.match(audio.src, /#t=1317\.0$/, 'fixture: the #t= start');
  neverPlayed(audio);
  for (const [type, rs] of BEFORE_T) t.fire(audio, type, rs);
  assert.equal(audio.currentTime, 0, 'fixture: the #t= start has not applied');
  await press(t, card.btn); t.flush(); await drain();
  await pressed.catch(() => {});
  assert.equal(audio.paused, true, 'fixture: paused');
  assert.equal(audio.dataset.pausedAt, '1317', 'the pause place is where playback begins, not the file\'s 0:00');
  // The #t= start lands while paused.
  audio.currentTime = 1317; audio.readyState = 4; t.fire(audio, 'seeking'); t.fire(audio, 'seeked');
  const resumed = press(t, card.btn); await drain();
  assert.equal(t.pending.length, 0, 'fixture: a resume, no new record read');
  assert.ok(audio.currentTime >= 1317 && audio.currentTime < 1318, `Play started at file ${audio.currentTime}, before the span`);
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await resumed; await drain();
});

test('FIX2 M11-SCR-R2-02, evidence: a Pause before the #t= start applies keeps the lead-in start; the next Play starts there', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  neverPlayed(p.audio);
  for (const [type, rs] of BEFORE_T) t.fire(p.audio, type, rs);
  await clipToggle(t); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'fixture: paused');
  assert.equal(p.audio.dataset.pausedAt, '1317', 'the pause place is the lead-in start, not the file\'s 0:00');
  p.audio.currentTime = 1317; p.audio.readyState = 4; t.fire(p.audio, 'seeking'); t.fire(p.audio, 'seeked');
  await clipToggle(t); await drain();
  assert.equal(p.audio.paused, false, 'it plays');
  assert.ok(p.audio.currentTime >= 1317 && p.audio.currentTime < 1318, `Play started at file ${p.audio.currentTime}, before the span`);
});

test('FIX2 M11-SCR-R2-02: a pause place past the start is kept as it was (both players)', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.currentTime = 1330.4; t.fire(audio, 'timeupdate');
  await press(t, card.btn); t.flush(); await drain();
  assert.equal(audio.dataset.pausedAt, '1330.4');
  const t2 = await page();
  const p = evidencePlayer(t2);
  await clipToggle(t2); await drain();
  for (const [type, rs] of ORDERS.webkit) t2.fire(p.audio, type, rs);
  p.audio.currentTime = 1330.4; t2.fire(p.audio, 'timeupdate');
  await clipToggle(t2); t2.flush(); await drain();
  assert.equal(p.audio.dataset.pausedAt, '1330.4');
});

test('FIX2 CS-3, Search: a historical record plays the current read\'s display_span (the server\'s cap), with no early-end message', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  recordWith(t, { historical: true, current_ref: 'sr1:' + 'c'.repeat(32), display_span: [1350, 1371] });
  // The old ref's own span runs past the cap.
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  assert.match(audio.src, /\/media#t=1347\.0$/, `from 3 s before the current display span: ${audio.src}`);
  assert.equal(`${audio.dataset.begin}-${audio.dataset.end}`, '1350-1371', 'the old span, not the current display span');
  // The server sends the clip to its cap (the span end + 0.5 s) only.
  audio.duration = 1371.5; audio.currentTime = 1360; t.fire(audio, 'timeupdate');
  audio.currentTime = 1371.1; t.fire(audio, 'timeupdate'); t.flush(); await drain();
  assert.equal(audio.paused, true, 'paused at the display span end');
  clock.advance(20000); await drain();
  assert.deepEqual(t.toasts, [], 'a message at the display span end');
  assert.ok(audio.src, 'stopped at a normal end');
});

/* M11-R2-01: the guard details no test pinned (code review r2 probes
   PROBE-M, -L, -C and -H, and the Search twin of PROBE-M). */
// The evidence player with its own bounds (episode seconds) and offset.
function evBox(t, { start = '1320', end = '1380', offset = '0', lead = '1317' } = {}) {
  const p = evidencePlayer(t);
  Object.assign(p.box.dataset, { start, end, offset, lead });
  return p;
}
async function evPlaying(t, p, src = MEDIA) {
  p.audio.src = src;
  await clipToggle(t); await drain();
  for (const [type, rs] of ORDERS.webkit) t.fire(p.audio, type, rs);
  await drain();
}

test('FIX2 M11-R2-01 evidence with a recording offset: an end 0.5 s before the span end is silent; 30 s before it stops (tolerance, span end in file seconds)', async () => {
  const t = await page();
  fakeClock(t);
  // Episode 1320-1380 in a clip file that starts at episode 1300 (file 20-80 s, file length 85 s).
  const p = evBox(t, { start: '1320', end: '1380', offset: '1300', lead: '17' });
  await evPlaying(t, p, '/api/v1/customer/records/x/media#t=17.0');
  p.audio.duration = 85; p.audio.currentTime = 79.3; t.fire(p.audio, 'timeupdate');
  p.audio.duration = 79.5; p.audio.currentTime = 79.5; p.audio.paused = true; t.fire(p.audio, 'pause'); t.fire(p.audio, 'ended'); await drain();
  assert.equal(p.err.hidden, true, 'an end within 1 s of the span end spoke');
  assert.ok(p.audio.src, 'an end within 1 s of the span end stopped the clip');
  const t2 = await page();
  fakeClock(t2);
  const p2 = evBox(t2, { start: '1320', end: '1380', offset: '1300', lead: '17' });
  await evPlaying(t2, p2, '/api/v1/customer/records/x/media#t=17.0');
  p2.audio.duration = 85; p2.audio.currentTime = 50; t2.fire(p2.audio, 'timeupdate');
  p2.audio.duration = 50; p2.audio.paused = true; t2.fire(p2.audio, 'pause'); t2.fire(p2.audio, 'ended'); await drain();
  assert.equal(p2.err.hidden, false, 'an early end 30 s before the span end was silent');
  assert.equal(p2.audio.src, '');
});

test('FIX2 M11-R2-01 Search: an end 0.5 s before the span end (data-end) is silent after the full duration was seen; 30 s before it stops', async (tc) => {
  for (const [at, speaks] of [[1379.5, false], [1350, true]]) {
    const t = await page();
    tc.after(() => { for (const r of t.pending.splice(0)) r(); });
    fakeClock(t);
    const audio = await playCard(t, searchCard(t, REF, 1320, 1380));
    audio.duration = 1385; audio.currentTime = at - 0.2; t.fire(audio, 'timeupdate');
    audio.currentTime = at; audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
    assert.deepEqual(t.toasts, speaks ? ['The clip didn’t load.'] : [], `an end at ${at} (span end 1380, file end 1385)`);
    assert.equal(!audio.src, speaks, `src after an end at ${at}`);
  }
});

test('FIX2 M11-R2-01 Search reuses one audio: a new clip does not inherit the old clip\'s file end', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  fakeClock(t);
  const a = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, a);
  audio.duration = 1385; audio.currentTime = 1340; t.fire(audio, 'timeupdate');
  // Clip B: another record whose span runs past its shorter file (60 s).
  const REF2 = 'sr1:' + 'b'.repeat(32);
  const b = searchCard(t, REF2, 1320, 1400);
  const pressed = press(t, b.btn); await drain();
  t.flush(); t.pending.shift()(); await drain();
  audio.duration = NaN; // the load algorithm resets duration on a new source
  assert.ok(/records\/sr1%3Ab+/.test(audio.src), 'fixture: clip B set ' + audio.src);
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  audio.duration = 1370; audio.currentTime = 1369.8; t.fire(audio, 'timeupdate');
  audio.currentTime = 1370; audio.paused = true; t.fire(audio, 'pause'); t.fire(audio, 'ended'); await drain();
  assert.deepEqual(t.toasts, [], 'clip B\'s normal end spoke with clip A\'s file end');
});

test('FIX2 M11-R2-01 only progress restarts the 10 s count: a stall that still fires timeupdate at the same time (every 250 ms) stops at 10 s', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const card = searchCard(t, REF, 1320, 1380);
  const audio = await playCard(t, card);
  audio.duration = 1385; audio.currentTime = 1330; t.fire(audio, 'timeupdate');
  t.fire(audio, 'waiting', 2);
  for (let ms = 0; ms < 10000; ms += 250) { clock.advance(250); if (audio.src) t.fire(audio, 'timeupdate'); }
  assert.equal(audio.src, '', 'same-time timeupdates kept a stalled clip going');
  assert.deepEqual(t.toasts, ['The clip didn’t load.']);
});

test('FIX2 M11-R2-01 the never-started stop gives back the READ-QUEUE place and settles the press', async (tc) => {
  const t = await page();
  tc.after(() => { for (const r of t.pending.splice(0)) r(); });
  const clock = fakeClock(t);
  const q0 = t.win.EW.requestQueue().busy;
  const card = searchCard(t, REF, 1320, 1380);
  let settled = false;
  press(t, card.btn).then(() => { settled = true; }, () => { settled = true; }); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  t.fire(audio, 'play', 0); t.fire(audio, 'waiting', 0);
  const held = t.win.EW.requestQueue().busy;
  clock.advance(10000); await drain();
  assert.equal(audio.src, '');
  assert.equal(held, q0 + 1, `fixture: the press holds a place (${q0} -> ${held})`);
  assert.equal(t.win.EW.requestQueue().busy, q0, 'the stop kept the place');
  assert.ok(settled, 'the press never settled');
  assert.deepEqual(t.toasts, ['The clip didn’t load.']);
});
