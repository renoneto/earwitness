/* CLIP-PLAYER-FIX (DEP7): the clip play button shows loading, playing,
   paused, ended and error, on every player, in the order each browser
   fires its media events.

   Reno (host, b7f7e7c6 + DEP6 clips): "After I press play the button
   never changes, so I can't tell whether it is loading or playing."
   Reproduced on a scratch host at 97f60a2b in headless Chromium, Firefox
   and WebKit: the Search play (card and inspector) only toggled a class
   (the icon stayed Play, no aria-pressed, no loading state, nothing for
   the record read before play); the evidence player's loading look was
   the idle look (owner decision (a)).

   Lead spec (2026-10-24, supersedes decision (a)'s "Play look while
   loading"; keeps its second press while loading stops it):
     play -> LOADING (spinner on the button, aria-busy, "Loading clip…")
          -> PAUSE icon while playing -> PLAY at the end or on pause;
     a failed load -> the error state (evidence: the clipError line with
     Retry; Search: the existing toast) and the Play look.

   The media event orders below are the ones the real browsers fired for
   the TJG Quince clip (repro logs, CLIP-PLAYER-FIX report).
   Run: node --test tests/frontend_clip_player_states.test.cjs */
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
  load() { this.error = null; this.readyState = 0; }
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

/* -- the evidence (and episode) clip player --------------------------- */

for (const [browser, order] of Object.entries(ORDERS)) {
  test(`evidence player, ${browser} order: press -> loading -> Pause while playing -> Play on pause and at the end`, async () => {
    const t = await page();
    const p = evidencePlayer(t);
    await clipToggle(t); await drain();
    isLoading(p.btn, 'after the press, before any event');
    assert.equal(p.status.textContent, 'Loading clip…');
    // Loading until the first playing (Firefox fires one at readyState 1,
    // then waiting: the look follows the media from there).
    let played = false;
    for (const [type, rs] of order) {
      t.fire(p.audio, type, rs);
      played = played || type === 'playing';
      if (!played) isLoading(p.btn, `after ${type}`);
    }
    await drain();
    isPlaying(p.btn, 'playing');
    assert.equal(p.status.textContent, '', 'the status still says loading');
    await clipToggle(t); t.flush(); await drain();
    isIdle(p.btn, 'paused');
    assert.equal(p.audio.paused, true);
    // Play again, then the clip reaches its end.
    await clipToggle(t); await drain();
    isLoading(p.btn, 'second play');
    t.fire(p.audio, 'play', 4); t.fire(p.audio, 'playing', 4); await drain();
    isPlaying(p.btn, 'second play');
    t.fire(p.audio, 'ended', 4); await drain();
    isIdle(p.btn, 'ended');
  });
}

test('evidence player: a stall while playing shows loading again, then Pause', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t);
  for (const [type, rs] of ORDERS.chromium) t.fire(p.audio, type, rs);
  await drain();
  isPlaying(p.btn, 'fixture');
  t.fire(p.audio, 'waiting', 2);
  isLoading(p.btn, 'starved');
  t.fire(p.audio, 'playing', 4);
  isPlaying(p.btn, 'resumed');
});

test('evidence player: a failed load shows the error line with Retry and the Play look, never loading', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'waiting', 0);
  t.fire(p.audio, 'error', 0); await drain();
  assert.equal(p.err.hidden, false, 'the error line is hidden');
  assert.ok(p.box.classes.has('is-error'), 'the box is not in its error state');
  isIdle(p.btn, 'error');
  assert.equal(p.status.textContent, '', 'the status still says loading');
});

test('evidence player: a second press while loading stops it (decision (a) kept): nothing plays, Play look', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0);
  isLoading(p.btn, 'fixture');
  await clipToggle(t); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'still asked to play');
  isIdle(p.btn, 'stopped while loading');
});

/* -- Search: the card's and the inspector's play ---------------------- */

for (const [browser, order] of Object.entries(ORDERS)) {
  test(`Search play, ${browser} order: loading from the press (record read included) -> Pause while playing -> Play on pause`, async () => {
    const t = await page();
    const btns = searchButtons(t, REF);
    const [other] = searchButtons(t, 'sr1:other');
    const pressed = searchPlay(t, REF);
    await drain();
    for (const b of btns) isLoading(b, 'during the record read');
    assert.equal(searchStatus(t), 'Loading clip…');
    isIdle(other, 'another ad');
    t.pending.shift()(); await drain();
    const audio = t.doc.querySelector('#search-audio');
    assert.ok(audio && audio.plays === 1, 'fixture: play asked for');
    let played = false;
    for (const [type, rs] of order) {
      t.fire(audio, type, rs);
      played = played || type === 'playing';
      if (!played) for (const b of btns) isLoading(b, `after ${type}`);
    }
    await pressed; await drain();
    for (const b of btns) isPlaying(b, 'playing');
    assert.equal(searchStatus(t), '', 'the status still says loading');
    isIdle(other, 'another ad while this plays');
    await searchPlay(t, REF); t.flush(); await drain();
    for (const b of btns) isIdle(b, 'paused');
  });
}

test('Search play: the read end pauses it and the button goes back to Play; ended too', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.webkit) t.fire(audio, type, rs);
  await pressed; await drain();
  isPlaying(btns[0], 'fixture');
  audio.currentTime = 1381; t.fire(audio, 'timeupdate'); t.flush(); await drain();
  for (const b of btns) isIdle(b, 'at the read end');
  const again = searchPlay(t, REF); await drain(); t.pending.shift()(); await drain();
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4); await drain();
  await again;
  isPlaying(btns[1], 'replay');
  t.fire(audio, 'ended', 4); await drain();
  for (const b of btns) isIdle(b, 'ended');
});

test('Search play: a failed load goes back to Play and says so (toast), never stuck loading', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  t.fire(audio, 'play', 0); t.fire(audio, 'waiting', 0); t.fire(audio, 'error', 0);
  await pressed; await drain();
  for (const b of btns) isIdle(b, 'error');
  assert.equal(searchStatus(t), '', 'the status still says loading');
  assert.ok(t.toasts.some(m => /did not start|could not be loaded/.test(m)), 'no failure message: ' + JSON.stringify(t.toasts));
});

/* -- AUDIO-429: a failed load stops its clip ---------------------------
   WebKit's media loader asks again at once after a refused (429) Range
   read, and the app cannot see those reads. On a clip's error event each
   player drops the clip's src and resets the element (load()), so the
   loader stops asking; the existing try-again state shows (evidence: the
   error line with Retry; Search: the Play look, a new press reads again). */
const MEDIA_SRC = '/api/v1/customer/records/sr1%3Afec09fbf13aa4a1c99d48eb3eb7664fc/media#t=1317.0';
function countLoads(audio) {
  const load = audio.load.bind(audio);
  audio.loads = 0;
  audio.load = () => { audio.loads += 1; load(); };
}

test('AUDIO-429: evidence player: an error stops the clip (no src, reset), shows Retry; Retry restores it and plays', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  p.audio.src = MEDIA_SRC; p.audio.setAttribute('src', MEDIA_SRC);
  countLoads(p.audio);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'waiting', 0);
  t.fire(p.audio, 'error', 0); await drain();
  assert.equal(p.audio.src, '', 'the failed clip keeps its src: the loader keeps asking');
  assert.ok(p.audio.loads >= 1, 'the failed clip was not reset (load())');
  assert.equal(p.err.hidden, false, 'the error line with Retry is hidden');
  isIdle(p.btn, 'stopped after the error');
  const plays = p.audio.plays;
  await t.win.EWViews.handleAction({ dataset: { action: 'clip-retry', clip: 'ev' }, closest: () => null }); await drain();
  assert.equal(p.audio.src, MEDIA_SRC, 'Retry did not restore the clip');
  assert.equal(p.audio.plays, plays + 1, 'Retry did not play');
  assert.equal(p.err.hidden, true, 'the error line stays after Retry');
  t.fire(p.audio, 'playing', 4); await drain();
  isPlaying(p.btn, 'after Retry');
});

test('AUDIO-429: evidence player: Play after a stopped clip restores it too', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  p.audio.src = MEDIA_SRC; p.audio.setAttribute('src', MEDIA_SRC);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'error', 0); await drain();
  assert.equal(p.audio.src, '');
  await clipToggle(t); await drain();
  assert.equal(p.audio.src, MEDIA_SRC, 'Play did not restore the clip');
  assert.equal(p.err.hidden, true, 'the error line stays after Play');
  isLoading(p.btn, 'play after the stop');
});

test('AUDIO-429: Search player: an error stops the clip (no src, reset), Play look and the message; a new press reads and plays', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  countLoads(audio);
  assert.ok(audio.src, 'fixture: a src');
  t.fire(audio, 'play', 0); t.fire(audio, 'waiting', 0); t.fire(audio, 'error', 0);
  await pressed; await drain();
  assert.equal(audio.src, '', 'the failed clip keeps its src: the loader keeps asking');
  assert.ok(audio.loads >= 1, 'the failed clip was not reset (load())');
  for (const b of btns) isIdle(b, 'stopped after the error');
  assert.ok(t.toasts.some(m => /did not start/.test(m)), 'no try-again message: ' + JSON.stringify(t.toasts));
  const again = searchPlay(t, REF); await drain();
  assert.equal(t.pending.length, 1, 'a new press did not read the record again');
  t.pending.shift()(); await drain();
  assert.match(audio.src, /\/media#t=/, 'a new press did not set the clip again');
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await again; await drain();
  for (const b of btns) isPlaying(b, 'played again');
});

test('Search play: a second press while loading stops it: nothing plays, Play look', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  t.fire(audio, 'play', 0);
  isLoading(btns[0], 'fixture');
  await searchPlay(t, REF); t.flush(); await pressed.catch(() => {}); await drain();
  assert.equal(audio.paused, true);
  for (const b of btns) isIdle(b, 'stopped while loading');
});

test('Search play: a second press during the record read stops it: Play look, and the read starts nothing', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const first = searchPlay(t, REF); await drain();
  for (const b of btns) isLoading(b, 'fixture: reading');
  const second = searchPlay(t, REF); await drain();
  const reads = t.pending.length;
  for (const b of btns) isIdle(b, 'stopped during the read');
  for (const r of t.pending.splice(0)) r();
  await first; await second; await drain();
  assert.equal(reads, 1, 'the second press read the record again');
  const audio = t.doc.querySelector('#search-audio');
  assert.ok(!audio || audio.plays === 0, 'the stopped press played when its read answered');
  for (const b of btns) isIdle(b, 'after the read answered');
});

test('Search play: pressing another ad moves the loading look to it; the first goes back to Play', async () => {
  const t = await page();
  const a = searchButtons(t, REF), b = searchButtons(t, 'sr1:second');
  const first = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await first; await drain();
  isPlaying(a[0], 'fixture');
  const second = searchPlay(t, 'sr1:second'); await drain();
  t.flush(); await drain();
  for (const x of a) isIdle(x, 'the first ad');
  for (const x of b) isLoading(x, 'the second ad');
  t.pending.shift()(); await drain();
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await second; await drain();
  for (const x of b) isPlaying(x, 'the second ad');
  for (const x of a) isIdle(x, 'the first ad');
});

test('Search play: buttons a repaint draws while it loads or plays take its look; after it stops they stay Play', async () => {
  const t = await page();
  searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  const repaint = async () => { const [fresh] = searchButtons(t, REF); await t.win.EWViews.render(); await drain(); return fresh; };
  isLoading(await repaint(), 'repainted during the record read');
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  isPlaying(await repaint(), 'repainted while playing');
  await searchPlay(t, REF); t.flush(); await drain();
  isIdle(await repaint(), 'repainted after the pause');
});

test('Search play: a teardown during the record read (another person or workspace) leaves repainted buttons on Play', async () => {
  const t = await page();
  searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.win.EW.stopAudio();
  const [fresh] = searchButtons(t, REF);
  await t.win.EWViews.render(); await drain();
  isIdle(fresh, 'repainted after the teardown');
  t.pending.shift()(); await pressed; await drain();
  isIdle(fresh, 'after the old read answered');
});

/* -- the loading look is visible: a spinner, not the Play icon -------- */

test('styles: a loading play button shows a spinner instead of its icon', () => {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  const rule = sel => { const at = css.indexOf(sel + ' {'); return at < 0 ? '' : css.slice(at, css.indexOf('}', at)); };
  assert.match(rule('.ew-play.is-loading svg'), /display\s*:\s*none/, 'the icon stays visible while loading');
  assert.match(rule('.ew-play.is-loading::before'), /animation\s*:\s*ew-spin/, 'no spinner on a loading play button');
});

/* -- (2a) the seek bar: elapsed and total for the clip's span; a slider
   the keyboard drives in 5 s steps, Home/End to the clip's start and
   end; drag or click seeks; always inside the clip's bounds ---------- */

// The evidence player's seek bar as readerPlayer draws it for the clip
// 22:00-23:00 (start 1320, end 1380): a 60 s span.
function seekBar(t, p) {
  const range = new El('input', { type: 'range', min: '0', max: '60', value: '0', dataset: { clipSeek: 'ev' } });
  for (const [k, v] of Object.entries({ 'aria-valuemin': '0', 'aria-valuemax': '60', 'aria-valuenow': '0', 'aria-valuetext': '0:00 of 1:00' })) range.setAttribute(k, v);
  const elapsed = new El('span', { dataset: { clipElapsed: '' }, textContent: '0:00' });
  p.box.append(elapsed, range);
  return { range, elapsed };
}
const key = (t, target, k) => {
  const ev = { type: 'keydown', key: k, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const fn of t.docListeners.keydown || []) fn(ev);
  return ev;
};
const slide = (t, target, value, type = 'input') => { target.value = String(value); for (const fn of t.docListeners[type] || []) fn({ type, target }); };

test('(2a) seek bar: ArrowRight/Left move 5 s, Home/End go to the clip start/end, inside the clip bounds', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  p.audio.currentTime = 1330;
  let ev = key(t, range, 'ArrowRight');
  assert.equal(p.audio.currentTime, 1335, 'ArrowRight is +5 s');
  assert.ok(ev.defaultPrevented, 'the native 1-step move also ran');
  key(t, range, 'ArrowLeft');
  assert.equal(p.audio.currentTime, 1330, 'ArrowLeft is -5 s');
  key(t, range, 'ArrowUp'); assert.equal(p.audio.currentTime, 1335, 'ArrowUp is +5 s');
  key(t, range, 'ArrowDown'); assert.equal(p.audio.currentTime, 1330, 'ArrowDown is -5 s');
  ev = key(t, range, 'Home');
  assert.equal(p.audio.currentTime, 1320, 'Home is the clip start');
  assert.ok(ev.defaultPrevented);
  ev = key(t, range, 'End');
  assert.equal(p.audio.currentTime, 1380, 'End is the clip end');
  key(t, range, 'ArrowRight');
  assert.equal(p.audio.currentTime, 1380, 'past the clip end');
  p.audio.currentTime = 1318;
  key(t, range, 'ArrowLeft');
  assert.equal(p.audio.currentTime, 1317, 'before where playback begins (the lead-in start)');
  // Other keys on it, and the keys elsewhere, are left alone.
  p.audio.currentTime = 1330;
  ev = key(t, range, 'Tab');
  assert.equal(ev.defaultPrevented, false); assert.equal(p.audio.currentTime, 1330);
  ev = key(t, new El('input', { type: 'text', dataset: {} }), 'ArrowRight');
  assert.equal(ev.defaultPrevented, false); assert.equal(p.audio.currentTime, 1330);
});

test('(2a) seek bar: dragging or clicking seeks inside the clip (value in seconds of its span)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  slide(t, range, 30, 'input');
  assert.equal(p.audio.currentTime, 1350, 'a drag to 0:30 of 1:00');
  slide(t, range, 60, 'change');
  assert.equal(p.audio.currentTime, 1380, 'a click at the end');
  slide(t, range, 0, 'change');
  assert.equal(p.audio.currentTime, 1320, 'a click at the start');
});

test('(2a) seek bar: playback moves the slider, its elapsed time and its spoken value', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range, elapsed } = seekBar(t, p);
  p.audio.currentTime = 1332.4; p.audio.paused = false;
  t.fire(p.audio, 'timeupdate');
  assert.equal(range.value, '12');
  assert.equal(range.getAttribute('aria-valuenow'), '12');
  assert.equal(range.getAttribute('aria-valuetext'), '0:12 of 1:00');
  assert.equal(elapsed.textContent, '0:12');
  p.audio.currentTime = 1318; // in the lead-in: the clip has not begun
  t.fire(p.audio, 'timeupdate');
  assert.equal(range.value, '0'); assert.equal(elapsed.textContent, '0:00');
});

/* -- CLIP-PLAYER-FIX2: code review r1 and screen review r1 ------------ */

// WCAG 2.x relative luminance and contrast of two #rrggbb colours.
const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const lum = rgb => { const c = rgb.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m); return (x + 0.05) / (y + 0.05); };
// color-mix(in srgb, fg P%, transparent) painted over bg.
const over = (fg, bg, p) => fg.map((v, i) => Math.round(v * p + bg[i] * (1 - p)));
const token = (css, name) => { const m = new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i').exec(css); assert.ok(m, 'token ' + name); return hex(m[1]); };

test('screen R1-01: the spinner ring is at least 3:1 against its button in every state (normal, hover, outline, outline hover, playing)', () => {
  const css = fs.readFileSync(path.join(APP, 'styles.css'), 'utf8');
  const tokens = fs.readFileSync(path.join(APP, 'tokens.css'), 'utf8');
  const at = css.indexOf('.ew-play.is-loading::before {');
  const rule = css.slice(at, css.indexOf('}', at));
  const m = /color-mix\(in srgb,\s*currentColor\s+(\d+)%,\s*transparent\)/.exec(rule);
  assert.ok(m, 'the ring track is not a currentColor mix: ' + rule);
  const p = Number(m[1]) / 100;
  const t = n => token(tokens, n);
  const states = {
    'normal (inverse)': [t('ew-text-inverse'), t('ew-inverse')],
    'hover (action pressed)': [t('ew-text-inverse'), t('ew-action-pressed')],
    'playing colour (action)': [t('ew-text-inverse'), t('ew-action')],
    'outline on sheet': [t('ew-text'), t('ew-sheet')],
    'outline hover': [t('ew-text'), t('ew-hover')],
  };
  const ratios = {};
  for (const [name, [fg, bg]] of Object.entries(states)) {
    const track = over(fg, bg, p);
    ratios[name] = +contrast(track, bg).toFixed(2);
    // The arc (currentColor) must stay apart from the track, or the ring no longer reads as turning.
    assert.ok(contrast(fg, track) >= 2, `${name}: arc and track too close (${contrast(fg, track).toFixed(2)})`);
  }
  for (const [name, r] of Object.entries(ratios)) assert.ok(r >= 3, `${name}: track ${r}:1 under 3:1 (all: ${JSON.stringify(ratios)})`);
});

test('screen R1-02/03: the clip seek bar is a 44 px touch target and styled the same in every engine (track, thumb, blue fill)', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  const rules = sel => { const out = []; let at = 0; while ((at = css.indexOf(sel, at)) >= 0) { const end = css.indexOf('}', at); out.push(css.slice(at, end)); at = end; } return out.join('\n'); };
  const base = rules('.ew-player-progress input[type=range] {');
  assert.match(base, /min-height\s*:\s*44px/, 'the seek bar is not 44 px tall');
  assert.match(base, /appearance\s*:\s*none/, 'the native look (no WebKit thumb colour) is kept');
  assert.match(rules('.ew-player-progress input[type=range]::-webkit-slider-runnable-track'), /var\(--ew-seek/, 'no played fill on the WebKit/Chromium track');
  assert.match(rules('.ew-player-progress input[type=range]::-webkit-slider-thumb'), /background\s*:\s*var\(--ew-action\)/, 'the WebKit thumb is not blue');
  assert.match(rules('.ew-player-progress input[type=range]::-moz-range-progress'), /background\s*:\s*var\(--ew-action\)/, 'no played fill in Firefox');
  assert.match(rules('.ew-player-progress input[type=range]::-moz-range-thumb'), /background\s*:\s*var\(--ew-action\)/, 'the Firefox thumb is not blue');
});

test('screen R1-03: the seek bar fill follows playback, a drag and a click (--ew-seek)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  p.audio.currentTime = 1332; p.audio.paused = false;
  t.fire(p.audio, 'timeupdate');
  assert.equal(range.style.getPropertyValue('--ew-seek'), '20%', 'playback did not move the fill');
  slide(t, range, 45, 'input');
  assert.equal(range.style.getPropertyValue('--ew-seek'), '75%', 'a drag did not move the fill');
  slide(t, range, 60, 'change');
  assert.equal(range.style.getPropertyValue('--ew-seek'), '100%', 'a click did not move the fill');
});

test('code R1-01: a teardown during the record read clears "Loading clip…" as well as the buttons', async () => {
  const t = await page();
  searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  assert.equal(searchStatus(t), 'Loading clip…', 'fixture');
  t.win.EW.stopAudio();
  const [fresh] = searchButtons(t, REF);
  await t.win.EWViews.render(); await drain();
  isIdle(fresh, 'repainted after the teardown');
  assert.equal(searchStatus(t), '', 'the status still says loading after the teardown');
  t.pending.shift()(); await pressed; await drain();
  assert.equal(searchStatus(t), '', 'the status says loading after the old read answered');
});

test('code R1-01: a read that ends after a workspace move (no repaint) clears the buttons and "Loading clip…"', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.win.EW.current.activeWorkspace = 'another-ws';
  t.pending.shift()(); await pressed; await drain();
  for (const b of btns) isIdle(b, 'after the read of a moved workspace');
  assert.equal(searchStatus(t), '', 'the status still says loading');
  const audio = t.doc.querySelector('#search-audio');
  assert.ok(!audio || audio.plays === 0, 'it played in another workspace');
});

test('code R1-03: one clip at a time: an evidence play stops the Search player', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const search = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(search, type, rs);
  await pressed; await drain();
  isPlaying(btns[0], 'fixture');
  await clipToggle(t); t.flush(); await drain();
  assert.equal(search.paused, true, 'the Search clip keeps playing under the evidence clip');
  for (const b of btns) isIdle(b, 'the Search buttons after the evidence press');
  isLoading(p.btn, 'the evidence press');
});

test('code R1-03: one clip at a time: an evidence play during the Search record read starts nothing there', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  await clipToggle(t); await drain();
  for (const b of btns) isIdle(b, 'the Search buttons after the evidence press');
  t.pending.splice(0).forEach(r => r()); await pressed; await drain();
  const search = t.doc.querySelector('#search-audio');
  assert.ok(!search || search.plays === 0, 'the Search read started its clip after the evidence press');
  assert.equal(p.audio.paused, false, 'the evidence clip was stopped');
});

test('code R1-03: one clip at a time: a Search play stops the evidence clip', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  searchButtons(t, REF);
  await clipToggle(t);
  for (const [type, rs] of ORDERS.chromium) t.fire(p.audio, type, rs);
  await drain();
  isPlaying(p.btn, 'fixture');
  const pressed = searchPlay(t, REF); t.flush(); await drain();
  assert.equal(p.audio.paused, true, 'the evidence clip keeps playing under the Search clip');
  isIdle(p.btn, 'the evidence button after the Search press');
  t.pending.shift()(); await drain();
  const search = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(search, type, rs);
  await pressed; await drain();
  assert.equal(search.paused, false);
});

test('code R1-04: pressing another ad pauses the playing one at the press, so no Pause button is lost while it still sounds', async () => {
  const t = await page();
  const a = searchButtons(t, REF), b = searchButtons(t, 'sr1:second');
  const first = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await first; await drain();
  isPlaying(a[0], 'fixture');
  const second = searchPlay(t, 'sr1:second'); await drain();
  // During B's record read: A is silent, so its Play look is true.
  assert.equal(audio.paused, true, 'A still plays during B\'s record read while its button shows Play');
  for (const x of a) isIdle(x, 'A during B\'s read');
  for (const x of b) isLoading(x, 'B during its read');
  t.flush(); await drain();
  for (const x of b) isLoading(x, 'B after A\'s pause event');
  t.pending.shift()(); await drain();
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await second; await drain();
  for (const x of b) isPlaying(x, 'B');
});

/* CLP-R1-02 (Firefox headless, scratch host, 36d21090): a Search card
   press whose record read is aborted by an authority boundary (a second
   route render starting while the page's own render has not painted:
   renderRoute's "left the page" boundary) showed the spinner for a few
   ms, then Play: no play() call, no media event, no message. The read
   failed with droppedContext while the same person, workspace and play
   still held. Now: one automatic retry of the read; if that is dropped
   too, the existing "did not start" message. Never a silent stop. */
const dropped = () => Object.assign(new Error('Access changed while this read was in flight; its response was dropped.'), { status: 0, droppedContext: true, droppedAuthority: true });
const RECORD = { record: { media: { playback: 'local', source_offset_s: 0 }, selected_bounds: { start_s: 1320, end_s: 1380 } } };

test('code R1-02: a record read dropped by a boundary while the same person, workspace and play hold is read again and plays', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  let reads = 0;
  t.win.EW.api = async () => { reads += 1; if (reads === 1) throw dropped(); return RECORD; };
  const pressed = searchPlay(t, REF); await drain();
  const audio = t.doc.querySelector('#search-audio');
  assert.equal(reads, 2, 'the dropped read was not tried again');
  assert.ok(audio && audio.plays === 1, 'nothing played after the dropped read (the silent stop)');
  for (const b of btns) isLoading(b, 'while the retried read starts the clip');
  for (const [type, rs] of ORDERS.firefox) t.fire(audio, type, rs);
  await pressed; await drain();
  for (const b of btns) isPlaying(b, 'after the retry');
  assert.deepEqual(t.toasts, []);
});

// PLAIN-ADS-FIX3 (code PA-R3-04): a stub that always drops stops after 5
// reads (a plain error), and the test has a time bound, so an uncapped
// retry fails here instead of hanging the suite.
const STUB_CAP = 5;
const capped = () => Object.assign(new Error('stub: more than 5 record reads'), { status: 500 });
test('code R1-02: a record read dropped twice says the clip did not start and goes back to Play, never silent', { timeout: 5000 }, async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  let reads = 0;
  t.win.EW.api = async () => { reads += 1; if (reads > STUB_CAP) throw capped(); throw dropped(); };
  await searchPlay(t, REF); await drain();
  assert.equal(reads, 2, 'more than one retry, or none');
  for (const b of btns) isIdle(b, 'after two dropped reads');
  assert.equal(searchStatus(t), '');
  assert.ok(t.toasts.some(m => /did not start/.test(m)), 'a silent stop: ' + JSON.stringify(t.toasts));
});

test('code R1-02: a dropped read after a teardown (another person or workspace) is not retried and says nothing', async () => {
  const t = await page();
  searchButtons(t, REF);
  let reads = 0;
  t.win.EW.api = async () => { reads += 1; t.win.EW.stopAudio(); throw dropped(); };
  await searchPlay(t, REF); await drain();
  assert.equal(reads, 1, 'a read of a torn-down play was retried');
  assert.deepEqual(t.toasts, [], 'the previous play spoke after the teardown');
});

test('code R1-08: before the first play, the seek keys move the slider, its spoken value and the elapsed time with the position', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range, elapsed } = seekBar(t, p);
  // No timeupdate ever fires here: the media is not loaded.
  key(t, range, 'End');
  assert.equal(p.audio.currentTime, 1380, 'fixture: End seeks');
  assert.equal(range.getAttribute('aria-valuenow'), '60', 'End: the slider still says 0');
  assert.equal(range.getAttribute('aria-valuetext'), '1:00 of 1:00');
  assert.equal(range.value, '60');
  assert.equal(elapsed.textContent, '1:00');
  p.audio.currentTime = 1330;
  key(t, range, 'ArrowRight');
  assert.equal(range.getAttribute('aria-valuenow'), '15', 'ArrowRight from 0:10');
  assert.equal(range.style.getPropertyValue('--ew-seek'), '25%');
  key(t, range, 'Home');
  assert.equal(range.getAttribute('aria-valuetext'), '0:00 of 1:00', 'Home');
  // The ±5 s buttons paint too.
  await t.win.EWViews.handleAction({ dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null });
  assert.equal(range.getAttribute('aria-valuenow'), '5', '+5s button');
  assert.equal(elapsed.textContent, '0:05');
});

test('code R1-09: A plays, B is pressed, A is pressed during B\'s read, then B again: B reads and plays (no swallowed press)', async () => {
  const t = await page();
  const a = searchButtons(t, REF), b = searchButtons(t, 'sr1:second');
  const first = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await first; await drain();
  isPlaying(a[0], 'fixture: A plays');
  const pressB = searchPlay(t, 'sr1:second'); await drain();
  const pressA = searchPlay(t, REF); t.flush(); await drain();
  const readsBefore = t.pending.length;
  const pressB2 = searchPlay(t, 'sr1:second'); await drain();
  assert.equal(t.pending.length, readsBefore + 1, 'the press on B was swallowed: no read');
  for (const x of b) isLoading(x, 'B after its press');
  for (const r of t.pending.splice(0)) r();
  await drain();
  assert.equal(audio.dataset.ref, 'sr1:second', 'B did not start');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await Promise.all([pressB, pressA, pressB2].map(x => x.catch(() => {}))); await drain();
  for (const x of b) isPlaying(x, 'B');
  for (const x of a) isIdle(x, 'A');
});

/* -- CLIP-PLAYER-FIX2 polish (screen R1-04, R1-05, R1-06) ------------- */

/* screen R1-04 (the kept empty status line) is replaced by screen R2-02 below:
   "Loading clip…" takes the place of "Clip available", so the card neither
   moves nor keeps an empty band. */

test('screen R1-05: the Search card shows elapsed while its clip plays ("0:12 of 1:00"), and "1:00 clip" again on pause', async () => {
  const t = await page();
  const [card] = searchButtons(t, REF);
  const len = new El('span', { textContent: '1:00 clip' }); len.classes.add('ew-clip-len');
  card.parent.append(len);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  // The clip is 22:00-23:00; the audio starts 3 s before (lead-in).
  audio.currentTime = 1332.4; t.fire(audio, 'timeupdate');
  assert.equal(len.textContent, '0:12 of 1:00', 'no elapsed time on the card while it plays');
  audio.currentTime = 1318; t.fire(audio, 'timeupdate');
  assert.equal(len.textContent, '0:00 of 1:00', 'the lead-in counts as the start');
  await searchPlay(t, REF); t.flush(); await drain();
  assert.equal(len.textContent, '1:00 clip', 'the length is not back after the pause');
});

test('screen R1-06: the Search play button is a 44 px target on a phone (the clip length keeps its 40 px + gap offset, so it clears the button)', () => {
  const css = fs.readFileSync(path.join(APP, 'search.css'), 'utf8');
  assert.match(css, /@media \(max-width: 599px\) \{\s*\.ew-play:not\(\.ew-play-lg\) \{ width: 44px; height: 44px; \}/, 'no 44 px play button under 600 px');
});

/* -- CLIP-PLAYER-FIX3 (screen r2 CLP-SCR-R2-01, code r2 CLP-R2-01/02/04) -- */

// The evidence player's big clock (episode time), as readerPlayer draws it.
function bigClock(p) { const c = new El('span', { dataset: { clipNow: '' }, textContent: '22:00' }); p.box.append(c); return c; }

test('screen R2-01: before the first play, a seek moves the big clock with the seek bar (22:15 with 0:15)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  const now = bigClock(p);
  key(t, range, 'Home');
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  assert.equal(range.getAttribute('aria-valuetext'), '0:15 of 1:00', 'fixture: the bar reads 0:15');
  assert.equal(now.textContent, '22:15', 'the big clock stayed while the bar moved (two times disagree)');
  await t.win.EWViews.handleAction({ dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null });
  assert.equal(now.textContent, '22:20', '+5s button');
  slide(t, range, 30, 'change');
  assert.equal(now.textContent, '22:30', 'a click on the bar');
  // While it plays, timeupdate still moves it; the lead-in reads as the start.
  p.audio.currentTime = 1342; t.fire(p.audio, 'timeupdate');
  assert.equal(now.textContent, '22:22');
  p.audio.currentTime = 1318; t.fire(p.audio, 'timeupdate');
  assert.equal(now.textContent, '22:00', 'the lead-in reads as the clip start');
});

test('code R2-01: before the first play, arrow steps start from the clip start (0:05, 0:10, 0:15), not the lead-in', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  // Unloaded media: currentTime is 0, not the #t= start.
  const said = [];
  for (let i = 0; i < 3; i++) { key(t, range, 'ArrowRight'); said.push([p.audio.currentTime, range.getAttribute('aria-valuetext')]); }
  assert.deepEqual(said, [[1325, '0:05 of 1:00'], [1330, '0:10 of 1:00'], [1335, '0:15 of 1:00']]);
  // Back from an unloaded player still stops at the lead-in start.
  const t2 = await page();
  const r = evidencePlayer(t2);
  const bar = seekBar(t2, r).range;
  key(t2, bar, 'ArrowLeft');
  assert.equal(r.audio.currentTime, 1317, 'ArrowLeft before play floors at the lead-in');
  // The +5s button before play, too.
  const t3 = await page();
  const s = evidencePlayer(t3);
  seekBar(t3, s);
  await t3.win.EWViews.handleAction({ dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null });
  assert.equal(s.audio.currentTime, 1325, '+5s before play');
});

/* CLP-R2-02 (Firefox headless, b3f751d4, 2 of 3 natural re-render runs):
   the retried record read went out with the render's own two reads and
   met the session's 2-request cap: 429, "Too many requests".
   PLAIN-ADS PA-R2-01 (after the merge with the plain-ads branch): a 429
   is retried by the shared read retry in E.api only (Retry-After, at most
   2 retries, at most 3 s of waiting). The clip keeps its one retry of a
   dropped read. The press's reads carry its signal: a second press, a
   teardown or the page left aborts the read and its wait. Before, both
   loops ran: 9 record reads over 8 s per press, and a teardown did not
   stop the inner retries. These tests run the real E.api over a stubbed
   fetch, never an E.api stub. Still failing: a message, never a silent
   stop. */
// A virtual clock for the page's timers: the waits run only on advance().
function timers(t) {
  const c = { now: 0, q: [], seq: 0, waited: [] };
  t.win.setTimeout = (fn, ms = 0) => { const id = ++c.seq; c.q.push({ id, at: c.now + (Number(ms) || 0), fn }); return id; };
  t.win.clearTimeout = id => { c.q = c.q.filter(x => x.id !== id); };
  c.advance = async ms => {
    const end = c.now + ms;
    await drain();
    for (;;) {
      c.q.sort((a, b) => a.at - b.at);
      const n = c.q[0];
      if (!n || n.at > end) break;
      c.q.shift(); c.now = n.at; n.fn(); await drain();
    }
    c.now = end; await drain();
  };
  return c;
}
const httpReply = (status, body, headers = {}) => ({ ok: status < 400, status,
  headers: { get: k => (Object.hasOwn(headers, k) ? headers[k] : null) }, text: async () => JSON.stringify(body) });
const http429 = (secs = '1') => httpReply(429, { ok: false, error: { code: 'RATE_LIMITED', message: 'slow down and retry' } }, secs === null ? {} : { 'Retry-After': secs });
// The real E.api over a stubbed fetch, its timers on the virtual clock.
// `answer(n)` is the n-th record read's reply; 'hang' never answers. Each
// read's time and whether its signal was aborted are kept.
function realReads(t, answer) {
  const clock = timers(t);
  const reads = [];
  t.win.EW.api = t.realApi;
  t.win.EW.requestTimeout.set = (fn, ms) => t.win.setTimeout(fn, ms);
  t.win.EW.requestTimeout.clear = id => t.win.clearTimeout(id);
  const session = t.win.fetch;
  t.win.fetch = (url, init = {}) => {
    if (!/^\/api\/v1\/customer\/records\//.test(url)) return session(url, init);
    const read = { at: clock.now, aborted: false };
    reads.push(read);
    const out = answer(reads.length);
    return new Promise((resolve, reject) => {
      const sig = init.signal;
      const stop = () => { read.aborted = true; reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' })); };
      if (sig && sig.aborted) return stop();
      if (sig) sig.addEventListener('abort', stop, { once: true });
      if (out !== 'hang') resolve(out === 'record' ? httpReply(200, RECORD) : out);
    });
  };
  return { clock, reads, at: () => reads.map(r => r.at) };
}

test('PA-R2-01: a persistent 429 makes 3 record reads per press (0, 1, 2 s), then the message: one retry loop, not two', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const r = realReads(t, () => http429('1'));
  searchPlay(t, REF);
  await r.clock.advance(2100);
  assert.deepEqual(r.at(), [0, 1000, 2000], 'record reads per press');
  assert.ok(t.toasts.length === 1 && /Too many requests/.test(t.toasts[0]), 'no message by 2.1 s: ' + JSON.stringify(t.toasts));
  await r.clock.advance(10000);
  assert.deepEqual(r.at(), [0, 1000, 2000], 'read again after the message');
  for (const b of btns) isIdle(b, 'after the last 429');
  assert.equal(searchStatus(t), '');
  assert.equal(t.toasts.length, 1);
  // A Retry-After past the 3 s budget, or none: no wait, the message at once.
  for (const secs of ['5', null]) {
    const t2 = await page();
    searchButtons(t2, REF);
    const r2 = realReads(t2, () => http429(secs));
    searchPlay(t2, REF);
    await r2.clock.advance(10000);
    assert.deepEqual(r2.at(), [0], `Retry-After ${secs}: waited`);
    assert.ok(t2.toasts.length === 1 && /Too many requests/.test(t2.toasts[0]), `Retry-After ${secs}: ` + JSON.stringify(t2.toasts));
  }
});

test('PA-R2-01: dropped, then 429 throughout: the drop\'s one retry, then the shared retries: 4 reads within 3 s, then the message', async () => {
  const t = await page();
  searchButtons(t, REF);
  const r = realReads(t, n => (n === 1 ? 'hang' : http429('1')));
  searchPlay(t, REF);
  await r.clock.advance(100);
  t.win.EW.authorityBoundary();
  await r.clock.advance(3000);
  assert.deepEqual(r.at(), [0, 100, 1100, 2100], 'record reads per press');
  assert.ok(t.toasts.length === 1 && /Too many requests/.test(t.toasts[0]), JSON.stringify(t.toasts));
  await r.clock.advance(10000);
  assert.equal(r.reads.length, 4);
});

// PLAIN-ADS-FIX3 (code PA-R3-01): the drop's retry shares the press's 429
// budget, so a drop on a later try still ends within 4 reads and 3 s
// (before: 5 or 6 reads, the message at 3.05 or 4.05 s).
test('FIX3 PA-R3-01: a drop on the 2nd or 3rd try shares the 429 budget: at most 4 reads within 3 s, then the message', async () => {
  for (const dropAt of [2, 3]) {
    const t = await page();
    searchButtons(t, REF);
    const r = realReads(t, n => (n === dropAt ? 'hang' : http429('1')));
    searchPlay(t, REF);
    await r.clock.advance((dropAt - 1) * 1000 + 50);
    t.win.EW.authorityBoundary();
    await r.clock.advance(3000);
    assert.ok(r.reads.length <= 4, `drop on try ${dropAt}: ${JSON.stringify(r.at())}`);
    assert.ok(Math.max(...r.at()) <= 3000, `drop on try ${dropAt}: a read after 3 s: ${JSON.stringify(r.at())}`);
    assert.ok(t.toasts.length === 1 && /Too many requests/.test(t.toasts[0]), `drop on try ${dropAt}: ` + JSON.stringify(t.toasts));
    const n = r.reads.length;
    await r.clock.advance(10000);
    assert.equal(r.reads.length, n, `drop on try ${dropAt}: read again after the message`);
  }
});

test('PA-R2-01: a 429 waits Retry-After in the shared retry, then the clip plays', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const r = realReads(t, n => (n === 1 ? http429('1') : 'record'));
  const pressed = searchPlay(t, REF);
  await r.clock.advance(999);
  assert.deepEqual(r.at(), [0], 'the next read did not wait for Retry-After');
  for (const b of btns) isLoading(b, 'while it waits');
  await r.clock.advance(1);
  assert.deepEqual(r.at(), [0, 1000]);
  const audio = t.doc.querySelector('#search-audio');
  assert.ok(audio && audio.plays === 1, 'nothing played after the wait');
  for (const [type, rs] of ORDERS.firefox) t.fire(audio, type, rs);
  await pressed; await drain();
  for (const b of btns) isPlaying(b, 'after the wait');
  assert.deepEqual(t.toasts, []);
  // Dropped, then 429, then the record: the drop's retry waits in the shared retry.
  const t2 = await page();
  searchButtons(t2, REF);
  const r2 = realReads(t2, n => (n === 1 ? 'hang' : n === 2 ? http429('1') : 'record'));
  searchPlay(t2, REF);
  await r2.clock.advance(100);
  t2.win.EW.authorityBoundary();
  await r2.clock.advance(1000);
  assert.deepEqual(r2.at(), [0, 100, 1100]);
  const a2 = t2.doc.querySelector('#search-audio');
  assert.ok(a2 && a2.plays === 1, 'nothing played after drop + 429');
  assert.deepEqual(t2.toasts, []);
});

test('PA-R2-01: a second press, a teardown or leaving the page aborts the read and its wait: no further read, nothing said', async () => {
  const ends = {
    press: t => searchPlay(t, REF),
    teardown: t => t.win.EW.stopAudio(),
    leave: t => { t.win.EW.current.route = '#/overview'; },
  };
  for (const [end, act] of Object.entries(ends)) {
    // During the Retry-After wait.
    const t = await page();
    const btns = searchButtons(t, REF);
    const r = realReads(t, () => http429('2'));
    const pressed = searchPlay(t, REF);
    await r.clock.advance(300);
    await act(t);
    await r.clock.advance(200);
    assert.equal(r.clock.q.length, 0, `${end}: the wait still holds a timer`);
    await r.clock.advance(10000); await pressed;
    assert.deepEqual(r.at(), [0], `${end}: read again after the play ended`);
    for (const b of btns) isIdle(b, end);
    assert.equal(searchStatus(t), '');
    assert.deepEqual(t.toasts, [], `${end}: spoke after the play ended`);
    assert.equal(t.doc.querySelector('#search-audio'), null, `${end}: an audio element was made`);
    // During the read itself: the play ends (nothing said, nothing starts).
    // READ-QUEUE (G2-02): the request itself is not aborted. The server
    // finishes a request whatever the browser does (asgi.py stops one only
    // while reading its body) and counts it until then, so the browser
    // keeps its place until it answers instead of freeing it at once.
    const u = await page();
    searchButtons(u, REF);
    const ru = realReads(u, () => 'hang');
    const p2 = searchPlay(u, REF);
    await ru.clock.advance(300);
    await act(u);
    await ru.clock.advance(10000); await p2;
    assert.equal(ru.reads.length, 1, `${end}: read again`);
    assert.equal(ru.reads[0].aborted, false, `${end}: the read in flight was aborted while the server still counts it`);
    assert.deepEqual(u.toasts, [], `${end}: spoke after the play ended`);
    assert.equal(u.doc.querySelector('#search-audio'), null, `${end}: an audio element was made`);
  }
});

/* CLP-R2-04 (code note): a retry never starts the clip once the person
   has left the page where they pressed play: like a teardown, silent. */
test('code R2-04: a dropped read is not retried once the person left the page; nothing starts, nothing is said', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  let reads = 0;
  t.win.EW.api = async () => { reads += 1; t.win.EW.current.route = '#/overview'; throw dropped(); };
  await searchPlay(t, REF); await drain();
  assert.equal(reads, 1, 'retried after the page was left');
  assert.equal(t.doc.querySelector('#search-audio'), null, 'the clip started on another page');
  for (const b of btns) isIdle(b, 'after leaving');
  assert.deepEqual(t.toasts, []);
});

test('code R2-04: a retry that answers after the person left the page (or during its 429 wait) starts nothing and says nothing', async () => {
  const t = await page();
  searchButtons(t, REF);
  let reads = 0;
  t.win.EW.api = async () => { reads += 1; if (reads === 1) throw dropped(); t.win.EW.current.route = '#/saved'; return RECORD; };
  searchPlay(t, REF); await drain();
  assert.equal(reads, 2, 'fixture: the retry ran');
  assert.equal(t.doc.querySelector('#search-audio'), null, 'the retried read started the clip on another page');
  assert.deepEqual(t.toasts, []);
  // A query change on the same page is not leaving it.
  const t2 = await page();
  searchButtons(t2, REF);
  let reads2 = 0;
  t2.win.EW.api = async () => { reads2 += 1; if (reads2 === 1) { t2.win.EW.current.route = '#/search?q=quince'; throw dropped(); } return RECORD; };
  searchPlay(t2, REF); await drain();
  const a2 = t2.doc.querySelector('#search-audio');
  assert.ok(a2 && a2.plays === 1, 'a Search query change stopped the retry');
  // Left during a 429 wait: the PA-R2-01 tests (real E.api).
});

/* -- CLIP-PLAYER-FIX3 polish (screen r2 R2-02, R2-03, R2-04) ----------- */

test('screen R2-04: the Search card says "Loading…" beside the spinner while it loads, then elapsed, then its length', async () => {
  const t = await page();
  const [card] = searchButtons(t, REF);
  const len = new El('span', { textContent: '1:00 clip' }); len.classes.add('ew-clip-len');
  card.parent.append(len);
  const pressed = searchPlay(t, REF); await drain();
  assert.equal(len.textContent, 'Loading…', 'no visible loading word on the card');
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  assert.equal(len.textContent, 'Loading…', 'the word went before the clip played');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  audio.currentTime = 1332.4; t.fire(audio, 'timeupdate');
  assert.equal(len.textContent, '0:12 of 1:00');
  await searchPlay(t, REF); t.flush(); await drain();
  assert.equal(len.textContent, '1:00 clip');
  // A failed read puts the length back too. (FIX3 PA-R3-04: capped stub.)
  let failedReads = 0;
  t.win.EW.api = async () => { failedReads += 1; if (failedReads > STUB_CAP) throw capped(); throw dropped(); };
  await searchPlay(t, 'sr1:other'); await drain();
  assert.equal(len.textContent, '1:00 clip');
  let fail;
  t.win.EW.api = () => new Promise((_, reject) => { fail = () => reject(Object.assign(new Error('Server error'), { status: 500 })); });
  // G2-08: Play on the paused clip resumes with no read; at its end Play reads afresh.
  audio.currentTime = 1381;
  const p2 = searchPlay(t, REF); await drain();
  assert.equal(len.textContent, 'Loading…');
  fail(); await p2; await drain();
  assert.equal(len.textContent, '1:00 clip', 'a failed read left "Loading…"');
});

test('screen R2-03: the seek bar\'s unplayed track is the 3:1 control colour in every engine', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  for (const sel of ['::-webkit-slider-runnable-track', '::-moz-range-track']) {
    const at = css.indexOf('.ew-player-progress input[type=range]' + sel);
    assert.ok(at >= 0, sel);
    const rule = css.slice(at, css.indexOf('}', at));
    assert.match(rule, /var\(--ew-control\)\s*;/, `${sel}: the unplayed track is not --ew-control`);
    assert.doesNotMatch(rule, /--ew-border-strong/, sel);
  }
});

test('screen R2-02: the evidence player shows "Loading clip…" in the place of "Clip available" (no empty band, same 20 px line)', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  assert.equal(css.indexOf('.ew-player .ew-clip-status:empty {'), -1, 'the empty status still keeps a line (the empty band)');
  assert.match(css, /\.ew-player\.ew-clip\.is-loading \.ew-audio-note \{ display: none; \}/, '"Clip available" is not swapped out while loading');
  assert.match(css, /\.ew-player \.ew-clip-status \{[^}]*line-height: 20px;/, 'the status line is not the 20 px of "Clip available" (the card moves)');
  assert.match(css, /\.ew-player \.ew-clip-status \{[^}]*margin-top: 0;/, 'the status takes the p + p 12 px top margin that "Clip available" has not (the lines below move 12 px)');
});

/* -- SEEK-PRELOAD (DEP7 candidate): a seek-bar move before the first play
   survives WebKit's own seek to the #t= start ---------------------------

   WebKit (headless, not Safari app; lr-app-clip-transcript's probe on
   a57b9030): 3 x ArrowRight on a fresh player, then Play: WebKit
   seeked@2.00 playing@2.00 (its own seek to the src's #t=2.0, the
   lead-in), Chromium seeked@20.00 playing@20.00. The seek is applied
   again once, at the first seeked that lands elsewhere; the newest seek
   wins; a second press (stop while loading) or a teardown drops it. */

// WebKit after a press on a player sought before the media loaded: its
// own seek to the #t= start (the lead-in, 1317 here), then playing. A
// seek applied again at that seeked fires its own seeked first.
function webkitLoad(t, p) {
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadstart', 0); t.fire(p.audio, 'loadedmetadata', 1);
  p.audio.currentTime = 1317; t.fire(p.audio, 'canplay', 4); t.fire(p.audio, 'seeking', 4);
  const again = [];
  t.fire(p.audio, 'seeked', 4); again.push(p.audio.currentTime);
  t.fire(p.audio, 'seeked', 4); t.fire(p.audio, 'timeupdate', 4); t.fire(p.audio, 'playing', 4);
  return again[0];
}
// Chromium keeps the seek: seeked lands where the person put the bar.
function chromiumLoad(t, p) {
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1); t.fire(p.audio, 'seeking', 1);
  t.fire(p.audio, 'seeked', 4); t.fire(p.audio, 'canplay', 4); t.fire(p.audio, 'timeupdate', 4); t.fire(p.audio, 'playing', 4);
}
const seekStep = (t, delta) => t.win.EWViews.handleAction({ dataset: { action: 'clip-seek', clip: 'ev', delta: String(delta) }, closest: () => null });
// Every seek-bar move (and the ±5 s buttons) before the first play: what it does, where it lands.
const PRELOAD_MOVES = [
  ['3 x ArrowRight', (t, range) => { for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight'); }, 1335, '0:15 of 1:00'],
  ['Home', (t, range) => key(t, range, 'Home'), 1320, '0:00 of 1:00'],
  ['a drag to 0:30', (t, range) => slide(t, range, 30, 'input'), 1350, '0:30 of 1:00'],
  ['a click at 0:45', (t, range) => slide(t, range, 45, 'change'), 1365, '0:45 of 1:00'],
  ['the +5s button', t => seekStep(t, 5), 1325, '0:05 of 1:00'],
];

for (const [what, move, at, said] of PRELOAD_MOVES) {
  test(`SEEK-PRELOAD: ${what} before the first play, WebKit order: it plays from there, not from the #t= start`, async () => {
    const t = await page();
    const p = evidencePlayer(t);
    const { range } = seekBar(t, p);
    assert.equal(p.audio.readyState, 0, 'fixture: the media is not loaded');
    await move(t, range);
    assert.equal(p.audio.currentTime, at, 'fixture: the move seeks');
    assert.equal(range.getAttribute('aria-valuetext'), said, 'CLP-R1-08: the bar is drawn after the seek before play');
    await clipToggle(t); await drain();
    const again = webkitLoad(t, p); await drain();
    assert.equal(again, at, `WebKit's own seek to the #t= start (1317) won: the move was lost`);
    assert.equal(p.audio.currentTime, at);
    assert.equal(range.getAttribute('aria-valuetext'), said, 'the bar after the play');
    // Once: a later seek is the person's own (or the clip's).
    p.audio.currentTime = 1340; t.fire(p.audio, 'seeked', 4);
    assert.equal(p.audio.currentTime, 1340, 'the pre-play seek was applied again a second time');
  });
}

// End then Play: the play is at the read's end, so it replays from the
// lead-in start (CLIPUI-R9-01), as Chromium does; the pending End is not
// applied again over that newer seek (it would play at the end and stop).
for (const [engine, load] of [['WebKit', webkitLoad], ['Chromium', chromiumLoad]]) {
  test(`SEEK-PRELOAD: End before the first play, ${engine} order: Play replays from the lead-in start, the End is not applied again`, async () => {
    const t = await page();
    const p = evidencePlayer(t);
    const { range } = seekBar(t, p);
    key(t, range, 'End');
    assert.equal(p.audio.currentTime, 1380, 'fixture: End seeks');
    assert.equal(range.getAttribute('aria-valuetext'), '1:00 of 1:00', 'CLP-R1-08: the bar is drawn after the seek before play');
    await clipToggle(t); await drain();
    load(t, p); await drain();
    assert.equal(p.audio.currentTime, 1317, 'the older End won over the replay from the lead-in');
    isPlaying(p.btn, `${engine} order`);
    assert.equal(range.getAttribute('aria-valuetext'), '0:00 of 1:00', 'the bar after the play');
  });
}

// SPL-R1-02 / SPL-SCR-R1-01: WebKit's timeupdate at the #t= start paints
// the lead-in; the re-seek at the seeked repaints the place at once, not
// at the next timeupdate (WebKit headless: 10-23 frames at 0:00 / 22:00).
test('SPL-R1-02: WebKit order: the re-seek repaints the bar, elapsed and big clock at once (0:15 / 22:15, before the next timeupdate)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range, elapsed } = seekBar(t, p);
  const now = bigClock(p);
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1);
  p.audio.currentTime = 1317; t.fire(p.audio, 'canplay', 4); t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'timeupdate', 4);
  assert.deepEqual([range.getAttribute('aria-valuetext'), elapsed.textContent, now.textContent], ['0:00 of 1:00', '0:00', '22:00'], 'fixture: the timeupdate at the #t= start paints the lead-in');
  t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1335, 'fixture: the move is applied again');
  assert.deepEqual([range.value, range.getAttribute('aria-valuenow'), range.getAttribute('aria-valuetext'), elapsed.textContent, now.textContent],
    ['15', '15', '0:15 of 1:00', '0:15', '22:15'], 'the player shows the lead-in until the next timeupdate');
});

test('SEEK-PRELOAD: Chromium order (the seek is kept): nothing is applied again later', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  await clipToggle(t); await drain();
  chromiumLoad(t, p); await drain();
  assert.equal(p.audio.currentTime, 1335);
  isPlaying(p.btn, 'Chromium order');
  p.audio.currentTime = 1340; t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1340, 'a kept seek was applied again later');
});

test('SEEK-PRELOAD: two moves before the first play: the newest wins (End, then a drag to 0:30; Play from 0:30)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  key(t, range, 'End');
  slide(t, range, 30, 'input');
  await clipToggle(t); await drain();
  assert.equal(webkitLoad(t, p), 1350, 'not the newest move');
  // and the other way round: a drag, then Home.
  const t2 = await page();
  const q = evidencePlayer(t2);
  const bar = seekBar(t2, q).range;
  slide(t2, bar, 30, 'input');
  key(t2, bar, 'Home');
  await seekStep(t2, 5);
  await clipToggle(t2); await drain();
  assert.equal(webkitLoad(t2, q), 1325, 'Home then +5s: 0:05');
});

test('SEEK-PRELOAD: a second press while loading stops the clip and drops the pending seek', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  await clipToggle(t); await drain();
  await clipToggle(t); t.flush(); await drain();
  isIdle(p.btn, 'stopped while loading');
  // The load goes on: WebKit's own seek to the #t= start.
  t.fire(p.audio, 'loadedmetadata', 1); p.audio.currentTime = 1317; t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1317, 'a stopped play still moved the media');
  assert.equal(p.audio.dataset.pendingSeek, undefined);
});

test('SEEK-PRELOAD: a torn-down player (its audio removed) never seeks again', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  key(t, range, 'End');
  await clipToggle(t); await drain();
  p.audio.pause(); p.audio.remove();
  p.audio.currentTime = 1317; t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1317);
});

test('SEEK-PRELOAD: a move on loaded media replaces a pending pre-load seek (the newest wins: End after the metadata, not the older 0:15)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1);
  key(t, range, 'End');
  assert.equal(p.audio.currentTime, 1380, 'fixture: End on loaded media');
  t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1380, 'the older pre-load seek (1335) was applied over the newer End');
});

test('SEEK-PRELOAD: a move on loaded media is not kept (nothing applied again later)', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  await clipToggle(t); await drain();
  chromiumLoad(t, p); await drain();
  await clipToggle(t); t.flush(); await drain();
  key(t, range, 'End');
  assert.equal(p.audio.currentTime, 1380);
  assert.equal(p.audio.dataset.pendingSeek, undefined, 'a seek on loaded media was kept');
  p.audio.currentTime = 1340; t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1340);
});

/* -- LOADING-GAP (SPL-R2-01 / SPL-SCR-R2-01, DEP7) ----------------------
   WebKit headless (not the Safari app), lr-app-loading-gap probe at
   93098859: when the seek before the first play lands on the #t= start
   (Play from on the first in-clip line, clamped to the lead-in; -5 s to
   the floor, then Play), WebKit fires seeked but no playing, and its
   play() promise never settles:
     play(), waiting@17.00 rs0, loadedmetadata@17.00 rs1, seeking@17.00 rs1,
     canplay@17.00 rs4, seeking@17.00 rs4, seeked@17.00 rs4
   while currentTime runs 17 -> 21.7 (Play from 1:56: 5 of 5 runs stuck;
   -5 s then Play: 3 of 5). The player kept "Loading clip…", aria-busy and
   the spinner while the clip played: Reno's "the button never changes".
   Rule: a loading clip whose time runs (two timeupdates in a row, the
   time moved on, not paused, readyState >= 3, no seek between them) is
   playing: once, as at playing. A seek's own jump is no proof; neither is
   a timeupdate at a standing time. */

// The lead-in floor here is 1317 (data-lead), the #t= start.
function firstLine(t, p, start = 1316) {
  const li = new El('li', { dataset: { start: String(start), segment: '2' } });
  const b = new El('button', { dataset: { action: 'clip-line', clip: 'ev' } });
  li.append(b); p.box.append(li);
  return () => t.win.EWViews.handleAction(b);
}
// WebKit's events after the press when the pre-load seek is the #t= start: no playing.
function webkitNoPlaying(t, p) {
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'waiting', 0); t.fire(p.audio, 'loadedmetadata', 1); t.fire(p.audio, 'seeking', 1);
  t.fire(p.audio, 'canplay', 4); t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'seeked', 4);
}
const tu = (t, p, at) => { p.audio.currentTime = at; t.fire(p.audio, 'timeupdate', 4); };
// Writes to the player's status line and its button's icon, counted.
function writes(el, prop) {
  let v = el[prop], n = 0;
  Object.defineProperty(el, prop, { configurable: true, get: () => v, set: x => { v = x; n++; } });
  return () => n;
}
const LOAD_GAP_MOVES = [
  ['Play from the first in-clip line (clamped to the lead-in)', async (t, p) => { await firstLine(t, p)(); }],
  ['-5 s to the lead-in floor, then Play', async (t, p) => { await seekStep(t, -5); await clipToggle(t); }],
];
for (const [what, move] of LOAD_GAP_MOVES) {
  test(`LOADING-GAP: ${what}, WebKit order with no playing: once the time runs the player shows Pause, once`, async () => {
    const t = await page();
    const p = evidencePlayer(t);
    await move(t, p); await drain();
    assert.equal(p.audio.currentTime, 1317, 'fixture: the seek lands on the #t= start');
    isLoading(p.btn, 'after the press');
    webkitNoPlaying(t, p); await drain();
    isLoading(p.btn, 'after seeked: nothing has played yet');
    tu(t, p, 1317); await drain();
    isLoading(p.btn, 'a timeupdate at a standing time is no proof of playing');
    tu(t, p, 1317.25); await drain();
    isPlaying(p.btn, 'the time runs (17.00 -> 17.25) with no playing event');
    assert.equal(p.status.textContent, '', 'still says "Loading clip…" while the clip plays');
    assert.equal(p.box.classes.has('is-loading'), false, 'the box is still is-loading');
    assert.equal(p.box.getAttribute('aria-busy'), null, 'the box is still aria-busy');
    // Once: later timeupdates leave the status and the button alone.
    const said = writes(p.status, 'textContent'), drawn = writes(p.btn, 'innerHTML');
    tu(t, p, 1317.5); tu(t, p, 1317.75); tu(t, p, 1318); await drain();
    assert.equal(said(), 0, 'the status was written again (a second announcement)');
    assert.equal(drawn(), 0, 'the button was drawn again');
    isPlaying(p.btn, 'still playing');
    // The play has started: a tab departure leaves it playing (DEMO2 pass 26b decision 1).
    t.win.EW.current.hosted = true;
    t.doc.visibilityState = 'hidden';
    for (const fn of t.docListeners.visibilitychange || []) fn({ type: 'visibilitychange' });
    await drain();
    assert.equal(p.audio.paused, false, 'a playing clip was paused at the tab departure (its start was still taken as pending)');
    t.doc.visibilityState = 'visible';
    t.win.EW.current.hosted = false;
  });
}

test('LOADING-GAP: the second press after the time ran stops the clip: Play look, paused', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await firstLine(t, p)(); await drain();
  webkitNoPlaying(t, p); tu(t, p, 1317); tu(t, p, 1317.25); await drain();
  isPlaying(p.btn, 'fixture: running');
  await clipToggle(t); t.flush(); await drain();
  isIdle(p.btn, 'second press');
  assert.equal(p.audio.paused, true, 'the second press did not stop it');
  assert.equal(p.status.textContent, '');
  // Paused: its time standing or moving (a seek) never shows Pause.
  tu(t, p, 1320); tu(t, p, 1321); await drain();
  isIdle(p.btn, 'timeupdates while paused');
});

test('LOADING-GAP: a loading clip that is paused never shows Pause, however its time moves', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1); t.fire(p.audio, 'canplay', 4);
  p.audio.paused = true; // the engine has not started it (no pause event yet)
  tu(t, p, 1317); tu(t, p, 1317.25); tu(t, p, 1317.5); await drain();
  isLoading(p.btn, 'paused with moving time');
});

test('LOADING-GAP: a seek is no proof of playing: a move on loaded media while loading keeps the loading look until playing', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1); t.fire(p.audio, 'canplay', 4);
  tu(t, p, 1317); await drain();
  key(t, range, 'ArrowRight'); // loaded media: from the read start 1320 to 1325; the seek's own timeupdate comes before seeked
  assert.equal(p.audio.currentTime, 1325, 'fixture: the move seeks');
  t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'timeupdate', 4); await drain();
  isLoading(p.btn, 'the seek\'s timeupdate (the time jumped) was taken for playback');
  t.fire(p.audio, 'seeked', 4); t.fire(p.audio, 'timeupdate', 4); await drain();
  isLoading(p.btn, 'a timeupdate at the seek\'s landing place');
  t.fire(p.audio, 'playing', 4); await drain();
  isPlaying(p.btn, 'playing');
});

test('LOADING-GAP: WebKit\'s re-seek (a pre-load move away from the #t= start) is no proof either: loading until playing', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const { range } = seekBar(t, p);
  for (let i = 0; i < 3; i++) key(t, range, 'ArrowRight');
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1);
  p.audio.currentTime = 1317; t.fire(p.audio, 'canplay', 4); t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'timeupdate', 4);
  t.fire(p.audio, 'seeked', 4);
  assert.equal(p.audio.currentTime, 1335, 'fixture: the move is applied again');
  t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'timeupdate', 4); await drain();
  isLoading(p.btn, 'the re-seek\'s jump (1317 -> 1335) was taken for playback');
  t.fire(p.audio, 'seeked', 4); t.fire(p.audio, 'playing', 4); await drain();
  isPlaying(p.btn, 'playing');
});

/* -- LOADING-GAP-FIX1 (LGP-R1-01 / LGP-SCR-R1-01, LGP-R1-02, LGP-SCR-R1-02) --
   WebKit headless (not the Safari app), code and screen review r1 at
   fbd7f32f: in a run where WebKit loses playing at the start, it delivers
   it late, after the person pauses and then seeks the paused clip:
     ... seeked@17.00, pause@18.73, rejected AbortError,
     seeking@20.00, seeked@20.00, playing@20.00 (paused: true)
   The player took any playing as playing: Pause, aria-pressed="true" and
   "Pause clip" on a paused, silent clip (Home on the bar 3 of 3 gap runs,
   +5 s 3 of 3). Rule: a playing while paused is no proof of playing. */
const LATE_PLAYING_SEEKS = [
  ['Home on the seek bar', (t, range) => key(t, range, 'Home'), 1320],
  ['+5 s', (t) => seekStep(t, 5), 1325], // from the read start 1320 (the time 1317.25 is in the lead-in)
];
for (const [what, seek, at] of LATE_PLAYING_SEEKS) {
  test(`LOADING-GAP-FIX1: WebKit's lost playing, delivered after pause then ${what}, shows Play on the paused clip; a press then plays it as a normal Play`, async () => {
    const t = await page();
    const p = evidencePlayer(t);
    const { range } = seekBar(t, p);
    await firstLine(t, p)(); await drain();
    webkitNoPlaying(t, p); tu(t, p, 1317); tu(t, p, 1317.25); await drain();
    isPlaying(p.btn, 'fixture: the time ran with no playing');
    await clipToggle(t); t.flush(); await drain();
    isIdle(p.btn, 'fixture: the second press paused it');
    await seek(t, range); await drain();
    assert.equal(p.audio.currentTime, at, 'fixture: the seek on loaded media');
    t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'seeked', 4);
    assert.equal(p.audio.paused, true, 'fixture: still paused');
    t.fire(p.audio, 'playing', 4); await drain();
    isIdle(p.btn, 'the late playing on a paused clip');
    assert.equal(p.btn.getAttribute('aria-pressed'), 'false', 'aria-pressed on a paused clip');
    assert.equal(p.box.classes.has('is-playing'), false, 'the box is-playing on a paused clip');
    assert.equal(p.status.textContent, '', 'the status');
    // A press is a normal Play: it asks to play, loads, then shows Pause at playing.
    const plays = p.audio.plays;
    await clipToggle(t); await drain();
    assert.equal(p.audio.plays, plays + 1, 'the press did not ask to play');
    assert.equal(p.audio.paused, false, 'the press did not start it');
    isLoading(p.btn, 'the press');
    t.fire(p.audio, 'play', 4); t.fire(p.audio, 'playing', 4); await drain();
    isPlaying(p.btn, 'playing after the press');
  });
}

test('LOADING-GAP-FIX1: Search play: a playing that arrives while its clip is paused shows Play; a press then plays it as a normal Play', async () => {
  const t = await page();
  const btns = searchButtons(t, REF);
  const pressed = searchPlay(t, REF); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.webkit) t.fire(audio, type, rs);
  await pressed; await drain();
  for (const b of btns) isPlaying(b, 'fixture');
  await searchPlay(t, REF); t.flush(); await drain();
  for (const b of btns) isIdle(b, 'fixture: paused');
  const said = searchStatus(t);
  t.fire(audio, 'playing', 4); await drain();
  assert.equal(audio.paused, true, 'fixture: still paused');
  for (const b of btns) isIdle(b, 'the late playing on a paused clip');
  assert.equal(searchStatus(t), said, 'the status changed');
  const replay = searchPlay(t, REF); await drain();
  for (const b of btns) isLoading(b, 'the press');
  t.pending.shift()(); await drain();
  assert.equal(audio.plays, 2, 'the press did not ask to play');
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await replay; await drain();
  for (const b of btns) isPlaying(b, 'playing after the press');
});

// LGP-R1-02: the !(readyState >= 3) form, pinned: an element with no
// readyState is never taken as running.
test('LOADING-GAP-FIX1: an element with no readyState never counts as running: forward timeupdates keep the loading look', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0);
  isLoading(p.btn, 'fixture');
  p.audio.readyState = undefined;
  for (const at of [1317, 1317.25, 1317.5]) { p.audio.currentTime = at; t.fire(p.audio, 'timeupdate'); }
  await drain();
  assert.equal(p.audio.paused, false, 'fixture: not paused');
  isLoading(p.btn, 'no readyState');
});

// LGP-SCR-R1-02: the seek landing is the first sample, so in WebKit's
// order with no playing the first timeupdate past it shows Pause (~250 ms
// sooner than a second timeupdate).
test('LOADING-GAP-FIX1: WebKit order with no playing: the first timeupdate past the seek landing shows Pause', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await firstLine(t, p)(); await drain();
  webkitNoPlaying(t, p); await drain();
  isLoading(p.btn, 'after seeked');
  tu(t, p, 1317.09); await drain();
  isPlaying(p.btn, 'the first timeupdate past the landing (17.00 -> 17.09)');
  assert.equal(p.status.textContent, '');
});

test('LOADING-GAP-FIX1: a seek landing on a paused loading clip is no sample: timeupdates while paused keep loading', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  await clipToggle(t); await drain();
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadedmetadata', 1); t.fire(p.audio, 'canplay', 4);
  p.audio.paused = true; // the engine has not started it (no pause event yet)
  p.audio.currentTime = 1317; t.fire(p.audio, 'seeking', 4); t.fire(p.audio, 'seeked', 4);
  tu(t, p, 1317.25); await drain();
  isLoading(p.btn, 'paused');
});

/* -- READ-QUEUE-FIX1 RQ-R1-01: clip audio is counted -------------------- */
// The server counts the <audio> media request like any other (asgi.py
// RateLimiter). A press keeps a place in the client queue from its press
// until the audio first loads or fails, so a session never has more than
// 2 requests at the server; a press still goes ahead of queued reads.
// The real E.api over a fetch that holds '/slow/' reads until released.
function queuedNet(t, { holdRecords = false, record = RECORD } = {}) {
  const held = [], sent = [], records = [], timers = [];
  t.win.EW.api = t.realApi;
  // Timeouts never fire here; a test may run one (timers).
  t.win.EW.requestTimeout.set = (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; };
  t.win.EW.requestTimeout.clear = () => {};
  const session = t.win.fetch;
  t.win.fetch = (url, init = {}) => {
    if (url === '/api/v1/session') return session(url, init);
    sent.push(url.replace('/api/v1', ''));
    if (/\/records\//.test(url)) return holdRecords ? new Promise(res => records.push(() => res(httpReply(200, record)))) : Promise.resolve(httpReply(200, record));
    if (/\/slow\//.test(url)) return new Promise(res => held.push(() => res(httpReply(200, { ok: true }))));
    return Promise.resolve(httpReply(200, { ok: true }));
  };
  const queue = () => ({ ...t.win.EW.requestQueue() });
  return { held, sent, records, timers, queue, read: p => t.win.EW.api(p) };
}

for (const first of ['canplay', 'loadeddata', 'error']) {
  test(`RQ-R1-01 Search play: the press's place is kept for its audio until ${first}; a page read waits`, async () => {
    const t = await page();
    searchButtons(t, REF);
    const n = queuedNet(t, { holdRecords: true });
    const slow = n.read('/customer/slow/1');
    await drain();
    const pressed = searchPlay(t, REF);
    await drain();
    // A page read asked while the record read runs waits for a place.
    const next = n.read('/customer/q/next');
    await drain();
    assert.deepEqual(n.queue(), { limit: 2, busy: 2, queued: 1 }, 'fixture');
    n.records.shift()(); await drain();
    const audio = t.doc.querySelector('#search-audio');
    assert.ok(audio && audio.plays === 1, 'fixture: the record read answered and play was asked for at once');
    assert.equal(n.queue().busy, 2, 'the audio request is not counted: ' + JSON.stringify(n.queue()));
    assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), [], 'a third request reached the server while the audio loads');
    t.fire(audio, 'loadstart', 0); t.fire(audio, 'loadedmetadata', 1); await drain();
    assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), [], 'the place was given back before the audio loaded');
    t.fire(audio, first, first === 'error' ? 0 : 4); await drain();
    assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), ['/customer/q/next'], `the place was not given back at ${first}`);
    await next;
    if (first !== 'error') { t.fire(audio, 'playing', 4); }
    await pressed; await drain();
    n.held.splice(0).forEach(r => r()); await slow; await drain();
    assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
  });
}

test('RQ-R1-01 evidence player: a press waits for a place ahead of queued reads and keeps it until the audio loads', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const n = queuedNet(t);
  const busy = [n.read('/customer/slow/1'), n.read('/customer/slow/2')];
  await drain();
  const queued = n.read('/customer/q/3');
  await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 2, queued: 1 }, 'fixture');
  await clipToggle(t); await drain();
  assert.equal(p.audio.plays, 0, 'the audio was asked for while 2 requests were at the server');
  isLoading(p.btn, 'waiting for a place');
  n.held.shift()(); await drain();
  assert.equal(p.audio.plays, 1, 'the press did not get the free place');
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/3'), [], 'the queued read went ahead of the press');
  t.fire(p.audio, 'play', 0); t.fire(p.audio, 'loadstart', 0); await drain();
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/3'), [], 'the place was given back before the audio loaded');
  t.fire(p.audio, 'loadeddata', 2); await drain();
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/3'), ['/customer/q/3'], 'the place was not given back once the audio loaded');
  await queued;
  t.fire(p.audio, 'playing', 4); await drain();
  isPlaying(p.btn, 'playing');
  n.held.splice(0).forEach(r => r()); await Promise.all(busy); await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
  // Loaded media plays at once: nothing more to load, no place asked for.
  await clipToggle(t); t.flush(); await drain();
  const more = [n.read('/customer/slow/3'), n.read('/customer/slow/4')];
  await drain();
  await clipToggle(t); await drain();
  assert.equal(p.audio.plays, 2, 'loaded media waited for a place');
  n.held.splice(0).forEach(r => r()); await Promise.all(more);
});

test('RQ-R1-01 evidence player: a second press while the press waits for a place stops it; the place goes to the page', async () => {
  const t = await page();
  const p = evidencePlayer(t);
  const n = queuedNet(t);
  const busy = [n.read('/customer/slow/1'), n.read('/customer/slow/2')];
  await drain();
  await clipToggle(t); await drain();
  const queued = n.read('/customer/q/3');
  await clipToggle(t); t.flush(); await drain();
  isIdle(p.btn, 'stopped while waiting');
  n.held.shift()(); await drain();
  assert.equal(p.audio.plays, 0, 'a stopped press played');
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/3'), ['/customer/q/3'], 'the stopped press kept the place');
  await queued;
  n.held.splice(0).forEach(r => r()); await Promise.all(busy); await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
});

/* JUDGE-SMALL G2-08 (QA-full F21, all 4 judges): on a Search card, Pause
   then Play restarted the clip: paused at 5.1 s, at 3.1 s right after Play
   (the clip begins at 2.0). The second Play reloaded the source (#t= its
   start). The evidence player resumes. Now Play on the clip the search
   player paused resumes it where it stopped: no record read, no reload.
   Another ad, or a clip at its end, still loads fresh. */
// An audio whose src assignment reloads as a browser does: the position
// goes back to the #t= start of the new source.
function reloadingAudio(t) {
  const make = t.doc.createElement;
  t.doc.createElement = tag => {
    const el = make(tag);
    if (tag !== 'audio') return el;
    let src = '';
    el.srcSets = 0;
    Object.defineProperty(el, 'src', { get: () => src, set: v => {
      src = String(v); if (!src) return;
      el.srcSets += 1; el.readyState = 0;
      const m = /#t=([\d.]+)/.exec(src); el.currentTime = m ? Number(m[1]) : 0;
    } });
    return el;
  };
}
// JUDGE-SMALL-FIX1 (JS-R1-02): a record read left waiting keeps the
// player's read watch (a 100 ms timer) alive, so a failing G2-08 test
// stalled the file to the runner timeout. Each G2-08 test answers every
// read still waiting when it ends: a regression is a red line, at once.
const answerReadsAtEnd = (tc, t) => tc.after(() => { for (const r of t.pending.splice(0)) r(); });
async function playingAt(t, ref, at) {
  const pressed = searchPlay(t, ref); await drain();
  t.pending.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressed; await drain();
  audio.currentTime = at; t.fire(audio, 'timeupdate');
  return audio;
}

test('G2-08: Pause then Play on a Search card resumes where it paused: no new read, no reload, position kept', async (tc) => {
  const t = await page();
  answerReadsAtEnd(tc, t);
  reloadingAudio(t);
  const btns = searchButtons(t, REF);
  const audio = await playingAt(t, REF, 1325.1);
  for (const b of btns) isPlaying(b, 'fixture');
  await searchPlay(t, REF); t.flush(); await drain();
  for (const b of btns) isIdle(b, 'paused');
  const paused = audio.currentTime, sets = audio.srcSets, plays = audio.plays;
  const resumed = searchPlay(t, REF); await drain();
  // Any read answers at once, so a failing run ends instead of waiting.
  const reads = t.pending.length;
  for (const r of t.pending.splice(0)) r();
  await drain();
  assert.equal(reads, 0, 'Play after Pause read the record again');
  assert.equal(audio.srcSets, sets, 'Play after Pause reloaded the source');
  assert.equal(audio.plays, plays + 1, 'Play after Pause did not ask the audio to play');
  for (const b of btns) isLoading(b, 'the resume press, before playing');
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  await resumed; await drain();
  assert.ok(audio.currentTime >= paused, `resumed at ${audio.currentTime}, paused at ${paused}`);
  for (const b of btns) isPlaying(b, 'resumed');
  // A second press still pauses it.
  await searchPlay(t, REF); t.flush(); await drain();
  assert.equal(audio.paused, true);
  for (const b of btns) isIdle(b, 'paused again');
});

test('G2-08: after a pause, another ad still reads its record and loads fresh from its own start', async (tc) => {
  const t = await page();
  answerReadsAtEnd(tc, t);
  reloadingAudio(t);
  const a = searchButtons(t, REF), b = searchButtons(t, 'sr1:second');
  const audio = await playingAt(t, REF, 1325.1);
  await searchPlay(t, REF); t.flush(); await drain();
  const sets = audio.srcSets;
  const pressB = searchPlay(t, 'sr1:second'); await drain();
  assert.equal(t.pending.length, 1, 'another ad did not read its record');
  for (const x of b) isLoading(x, 'B during its read');
  for (const x of a) isIdle(x, 'A during B\'s read');
  t.pending.shift()(); await drain();
  assert.equal(audio.srcSets, sets + 1, 'another ad did not load its own source');
  assert.match(audio.src, /sr1%3Asecond\/media#t=1317\.0$/);
  assert.equal(audio.currentTime, 1317, 'another ad did not start from its own start');
  for (const [type, rs] of ORDERS.chromium) t.fire(audio, type, rs);
  await pressB; await drain();
  for (const x of b) isPlaying(x, 'B');
  for (const x of a) isIdle(x, 'A');
});

test('G2-08: a clip paused at its end (or ended, or failed) plays again from its start: a fresh load', async (tc) => {
  for (const how of ['end', 'ended', 'error']) {
    const t = await page();
    answerReadsAtEnd(tc, t);
    reloadingAudio(t);
    const btns = searchButtons(t, REF);
    const audio = await playingAt(t, REF, 1340);
    if (how === 'end') { audio.currentTime = 1381; t.fire(audio, 'timeupdate'); t.flush(); }
    else t.fire(audio, how, 4);
    await drain();
    for (const b of btns) isIdle(b, `${how}: fixture`);
    const sets = audio.srcSets;
    const again = searchPlay(t, REF); await drain();
    assert.equal(t.pending.length, 1, `${how}: no fresh read`);
    t.pending.shift()(); await drain();
    assert.equal(audio.srcSets, sets + 1, `${how}: no fresh load`);
    assert.equal(audio.currentTime, 1317, `${how}: not from the clip start`);
    t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
    await again.catch(() => {}); await drain();
    for (const b of btns) isPlaying(b, `${how}: replay`);
  }
});

test('G2-08: Play on the paused clip while another ad\'s record read runs resumes it; that read starts nothing', async (tc) => {
  const t = await page();
  answerReadsAtEnd(tc, t);
  reloadingAudio(t);
  const a = searchButtons(t, REF), b = searchButtons(t, 'sr1:second');
  const audio = await playingAt(t, REF, 1325.1);
  const pressB = searchPlay(t, 'sr1:second'); await drain();
  t.flush(); await drain();
  assert.equal(audio.paused, true, 'fixture: A paused by the press on B');
  const sets = audio.srcSets;
  const pressA = searchPlay(t, REF); await drain();
  for (const x of b) isIdle(x, 'B after the press on A');
  for (const x of a) isLoading(x, 'A resuming');
  for (const r of t.pending.splice(0)) r();
  await drain();
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  const settle = p => Promise.race([p.catch(() => {}), new Promise(res => setTimeout(res, 500))]);
  await Promise.all([pressB, pressA].map(settle)); await drain();
  assert.equal(audio.srcSets, sets, 'B\'s read loaded its source after the press on A');
  assert.equal(audio.dataset.ref, REF);
  assert.ok(audio.currentTime >= 1325.1, `A resumed at ${audio.currentTime}`);
  assert.equal(audio.paused, false);
  for (const x of a) isPlaying(x, 'A');
  for (const x of b) isIdle(x, 'B');
  assert.deepEqual(t.toasts, []);
});

test('G2-08: the resume keeps one clip at a time: another playing clip pauses, and the other ad\'s read in flight is aborted', async (tc) => {
  const t = await page();
  answerReadsAtEnd(tc, t);
  reloadingAudio(t);
  const signals = [];
  const api = t.win.EW.api;
  t.win.EW.api = (p, o = {}) => { signals.push(o.signal); return api(p, o); };
  searchButtons(t, REF); searchButtons(t, 'sr1:second');
  const audio = await playingAt(t, REF, 1325.1);
  const pressB = searchPlay(t, 'sr1:second'); await drain();
  t.flush(); await drain();
  assert.equal(audio.paused, true, 'fixture: A paused by the press on B');
  const bRead = signals[signals.length - 1];
  assert.ok(bRead && bRead.aborted === false, 'fixture: B\'s read in flight');
  // Another clip on the page (an inline player) is playing.
  const other = t.doc.createElement('audio');
  other.dataset.clip = 'inline';
  t.body.append(other);
  other.play().catch(() => {});
  const pressA = searchPlay(t, REF); await drain();
  assert.equal(other.paused, true, 'the resume left another clip playing');
  assert.equal(bRead.aborted, true, 'the resume left B\'s record read running');
  for (const r of t.pending.splice(0)) r();
  t.fire(audio, 'play', 4); t.fire(audio, 'playing', 4);
  const settle = p => Promise.race([p.catch(() => {}), new Promise(res => setTimeout(res, 500))]);
  await Promise.all([pressB, pressA].map(settle)); await drain();
  assert.equal(audio.paused, false);
  assert.ok(audio.currentTime >= 1325.1, `A resumed at ${audio.currentTime}`);
});

/* -- READ-QUEUE-FIX2 ------------------------------------------------------ */
// RQ-R2-01: #search-audio is one element, reused by every Search press in
// the tab. A new src on a used element makes the browser fire abort and
// emptied for the previous source (HTML media load algorithm), after the
// press's own code ran. Those end the old load, not this press's: each
// press keeps its place until its own audio first loads or fails.
const REFS = [REF, 'sr1:' + 'b'.repeat(32), 'sr1:' + 'c'.repeat(32)];
test('RQ-R2-01 Search: the 2nd and 3rd press in a tab keep their place through the old source\'s abort and emptied, until their own audio loads', async () => {
  const t = await page();
  for (const ref of REFS) searchButtons(t, ref);
  const n = queuedNet(t, { holdRecords: true });
  const slow = n.read('/customer/slow/1');
  await drain();
  for (const [i, ref] of REFS.entries()) {
    const nth = `press ${i + 1}`, path = '/customer/q/' + i;
    const pressed = searchPlay(t, ref);
    await drain();
    const next = n.read(path);
    await drain();
    assert.deepEqual(n.queue(), { limit: 2, busy: 2, queued: 1 }, `${nth}: fixture`);
    n.records.shift()(); await drain();
    const audio = t.doc.querySelector('#search-audio');
    assert.equal(audio.dataset.ref, ref, `${nth}: fixture: its source is set`);
    // As in browsers, a new source empties the element at once (readyState 0).
    if (i === 0) {
      let src = audio.src;
      Object.defineProperty(audio, 'src', { get: () => src, set(v) { src = String(v); this.readyState = 0; this.error = null; } });
    }
    // The used element ends the previous source's load.
    if (i > 0) { t.fire(audio, 'abort', 0); t.fire(audio, 'emptied', 0); await drain(); }
    t.fire(audio, 'loadstart', 0); t.fire(audio, 'loadedmetadata', 1); await drain();
    assert.deepEqual(n.sent.filter(u => u === path), [], `${nth}: the place was given back before its audio loaded (3 requests at the server)`);
    t.fire(audio, 'loadeddata', 2); await drain();
    assert.deepEqual(n.sent.filter(u => u === path), [path], `${nth}: the place was not given back once its audio loaded`);
    await next;
    t.fire(audio, 'canplay', 4); t.fire(audio, 'playing', 4);
    await pressed; await drain();
    assert.equal(audio.paused, false, `${nth}: not playing`);
  }
  n.held.splice(0).forEach(r => r()); await slow; await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
});

// RQ-R2-01, the other half: a press on another ad while the first one's
// audio still loads moves the element off the first source; its abort
// and emptied free the first press's place at once (not at the cap).
test('RQ-R2-01 Search: a new source frees the place of the load it replaced; its own place is kept', async () => {
  const t = await page();
  for (const ref of REFS.slice(0, 2)) searchButtons(t, ref);
  const n = queuedNet(t, { holdRecords: true });
  searchPlay(t, REFS[0]); await drain();
  n.records.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  let src = audio.src;
  Object.defineProperty(audio, 'src', { get: () => src, set(v) { src = String(v); this.readyState = 0; this.error = null; } });
  audio.networkState = 2; t.fire(audio, 'loadstart', 0); await drain();   // NETWORK_LOADING
  assert.equal(n.queue().busy, 1, 'fixture: the first press holds its place while its audio loads');
  const second = searchPlay(t, REFS[1]); await drain();
  assert.equal(n.queue().busy, 2, 'fixture: the second press reads its record');
  n.records.shift()(); await drain();
  assert.equal(audio.dataset.ref, REFS[1], 'fixture: the second source is set');
  t.fire(audio, 'abort', 0); t.fire(audio, 'emptied', 0); await drain();
  assert.equal(n.queue().busy, 1, 'the replaced load kept its place: ' + JSON.stringify(n.queue()));
  t.fire(audio, 'loadstart', 0); t.fire(audio, 'loadeddata', 2); await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
  t.fire(audio, 'canplay', 4); t.fire(audio, 'playing', 4); await second;
});

// RQ-R2-02: an evidence press that waits for a place plays nothing once
// the page was left or its player is gone (removed media still plays, and
// nothing on the next page could stop it). The place goes on.
for (const [what, leave] of [
  ['the page was left', t => { t.win.EW.current.route = '#/overview'; }],
  ['its player is gone', (t, p) => { p.box.remove(); p.audio.isConnected = false; }],
]) {
  test(`RQ-R2-02 evidence player: a press waiting for a place plays nothing once ${what}; the place goes on`, async () => {
    const t = await page();
    t.win.EW.current.route = '#/evidence/ev';
    const p = evidencePlayer(t);
    const n = queuedNet(t);
    const busy = [n.read('/customer/slow/1'), n.read('/customer/slow/2')];
    await drain();
    const pressed = clipToggle(t); await drain();
    const queued = n.read('/customer/q/3');
    await drain();
    assert.equal(p.audio.plays, 0, 'fixture: the press waits for a place');
    leave(t, p);
    n.held.shift()(); await drain();
    assert.equal(p.audio.plays, 0, `the clip was asked to play after ${what}`);
    assert.deepEqual(n.sent.filter(u => u === '/customer/q/3'), ['/customer/q/3'], 'the dropped press kept the place');
    await queued; await pressed;
    n.held.splice(0).forEach(r => r()); await Promise.all(busy); await drain();
    assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
  });
}

// RQ-R2-03: the place rules the review found untested (its mutants A02,
// A09, A10, A17).
test('RQ-R2-03 Search: a press whose audio never answers gives its place back at the read timeout', async () => {
  const t = await page();
  searchButtons(t, REF);
  const n = queuedNet(t, { holdRecords: true });
  const slow = n.read('/customer/slow/1');
  await drain();
  searchPlay(t, REF); await drain();
  const next = n.read('/customer/q/next'); await drain();
  const from = n.timers.length;
  n.records.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  assert.equal(audio.plays, 1, 'fixture: play was asked for');
  const cap = n.timers.slice(from).filter(x => x.ms === t.win.EW.requestTimeout.readMs);
  assert.equal(cap.length, 1, 'no read-timeout cap on the audio\'s place');
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), [], 'fixture: the place is held');
  cap[0].fn(); await drain();
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), ['/customer/q/next'], 'the place was not given back at the cap');
  await next;
  n.held.splice(0).forEach(r => r()); await slow; await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
});

test('RQ-R2-03 Search: a record read answered but dropped (the workspace moved) gives its place back, read again or not', async () => {
  const t = await page();
  searchButtons(t, REF);
  const n = queuedNet(t, { holdRecords: true });
  searchPlay(t, REF); await drain();
  assert.equal(n.queue().busy, 1, 'fixture');
  t.win.EW.current.workspaceContext = 'ctx-moved-1';
  n.records.shift()(); await drain();
  assert.equal(n.records.length, 1, 'fixture: read once more (CLP-R1-02)');
  assert.equal(n.queue().busy, 1, 'the dropped answer kept its place: ' + JSON.stringify(n.queue()));
  t.win.EW.current.workspaceContext = 'ctx-moved-2';
  n.records.shift()(); await drain();
  assert.equal(t.doc.querySelector('#search-audio'), null, 'fixture: nothing played');
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
});

test('RQ-R2-03 Search: a record with no local clip gives the place back', async () => {
  const t = await page();
  searchButtons(t, REF);
  const n = queuedNet(t, { record: { record: { media: { playback: 'none' }, selected_bounds: { start_s: 1320, end_s: 1380 } } } });
  await searchPlay(t, REF); await drain();
  assert.ok(t.toasts.some(m => /No clip can play here/.test(m)), 'fixture: ' + JSON.stringify(t.toasts));
  assert.deepEqual(n.queue(), { limit: 2, busy: 0, queued: 0 });
});

test('RQ-R2-03 Search: a press whose start is held by a tab departure gives its place back while it waits', async () => {
  const t = await page();
  searchButtons(t, REF);
  const n = queuedNet(t, { holdRecords: true });
  const slow = n.read('/customer/slow/1');
  await drain();
  // The departure lands as the record read answers, before the start.
  const api = t.win.EW.api;
  t.win.EW.api = async (p, o) => {
    const out = await api(p, o);
    if (/\/records\//.test(p)) {
      t.win.EW.current.hosted = true;
      t.doc.visibilityState = 'hidden';
      for (const fn of t.docListeners.visibilitychange || []) fn({ type: 'visibilitychange' });
    }
    return out;
  };
  searchPlay(t, REF); await drain();
  const next = n.read('/customer/q/next'); await drain();
  assert.deepEqual(n.queue(), { limit: 2, busy: 2, queued: 1 }, 'fixture');
  n.records.shift()(); await drain();
  const audio = t.doc.querySelector('#search-audio');
  assert.ok(audio && audio.dataset.ref === REF, 'fixture: its source is set');
  assert.equal(audio.plays, 0, 'fixture: the start is held');
  assert.deepEqual(n.sent.filter(u => u === '/customer/q/next'), ['/customer/q/next'], 'the held start kept its place');
  assert.deepEqual(n.queue(), { limit: 2, busy: 1, queued: 0 }, 'only the slow read is at the server (the page read was answered)');
  // (Answers now wait for the tab's return read: nothing more to await here.)
  void next; void slow;
});
