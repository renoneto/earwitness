/* CLIP-TRANSCRIPT (2b): the playing line is highlighted, and an in-clip
   line's time is a "Play from m:ss" button.

   Design R/ws-app/CLIP-TRANSCRIPT-DESIGN.md, narrowed by the lead GO
   (2026-10-24T03:56Z): evidence and passage pages only (the clip box's
   own .ew-said section); lines get data-start/data-end (+ data-in-clip
   when they overlap the playable range [offset + lead, data-end]) only
   on a page with a local clip box, appended lines too; is-now plus
   aria-current="true" on the playing line (a gap keeps the previous
   line; nothing before the first line or after the clip end; seeked
   updates while paused); the time label of an in-clip line is a native
   button (title "Play from m:ss", as its label) that seeks to
   max(line.start - 0.3, offset + lead) - offset (FIX1 CTR-SCR-R1-04:
   speech often starts just before the line's time), clamped like
   clipSeekBy, then plays through clipToggle when paused; the pressed
   line stays the playing line until its start, so the line before does
   not flash. At 390 its hit area is 44 px tall (::after), with no
   overlap between lines (FIX1 CTR-SCR-R1-01, real headless Chromium).
   No whole-line click, no auto-scroll, no live region, focus never moves.
   Without a clip, and on the excerpt view, the words section is
   byte-identical to launch-readiness a57b9030.
   Run: node --test tests/frontend_clip_transcript.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const os = require('node:os');
const { spawn } = require('node:child_process');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');

/* -- 1. Rendered pages (HTML strings), loader as frontend_clip_player -- */

const flush = () => new Promise(res => setTimeout(res, 0));
const settleAll = async (n = 20) => { for (let i = 0; i < n; i++) await flush(); };
const json = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => null }, text: async () => JSON.stringify(body) });
function sessionFetch() {
  const prefs = { revision: 1 };
  return async (url, init = {}) => {
    const method = init.method || 'GET';
    if (url === '/api/v1/session') return json({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' });
    if (url === '/api/v1/customer/preferences' && method === 'GET') return json({ ok: true, workspace_context: 'ctx-ws', preferences: prefs, revision: 1 });
    if (url === '/api/v1/customer/reader-status') return json({ ok: true, workspace_context: 'ctx-ws', configured: false, verified: false, note: 'n' });
    throw new Error('unexpected ' + method + ' ' + url);
  };
}
function loadViews(route) {
  const store = {};
  const win = {
    console, URLSearchParams, AbortController, Intl, location: { hash: route }, history: { replaceState() {} }, scrollTo() {}, fetch: sessionFetch(),
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
  };
  win.window = win; win.globalThis = win;
  vm.createContext(win);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.EW.current.route = route; win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
async function render(route, api) {
  const win = loadViews(route);
  await settleAll(); await win.EW.preferences();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {}; win.EW.api = async (p, o) => api(p, o);
  win.location.hash = route; win.EW.current.route = route;
  await win.EWViews.render(); await settleAll();
  return main.innerHTML;
}

const REF = 'sr1:c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1';
const LOCAL = { playback: 'local', kind: 'evidence_clip', source_offset_s: 100, alignment: 'verified', timing: 'verified', trust: 'exact' };
/* Read 120-170, recording offset 100: the clip box says data-offset 100,
   data-lead 17 (3 s before the read), data-end 170, so the playable
   range is [117, 170] in episode seconds. */
const SEGMENTS = [
  { segment_id: 1, start_s: 100, end_s: 116, text: 'Before the lead-in.', text_start: 0 },
  { segment_id: 2, start_s: 116, end_s: 124.5, text: 'Thanks to Northwind', text_start: 0 },
  { segment_id: 2, start_s: 116, end_s: 124.5, text: ' for this one.', text_start: 19 },
  { segment_id: 3, start_s: 125, end_s: 150, text: 'Go to northwind.', text_start: 0 },
  { segment_id: 4, start_s: 152, end_s: 169, text: 'Use code QUIET.', text_start: 0 },
  { segment_id: 5, start_s: 170, end_s: 180, text: 'Back to the show.', text_start: 0 },
];
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: null, bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 120, end_s: 170 },
  episode: { episode_id: 'ep_c', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: 'named:ep_c.named.120000-170000', episode_id: 'ep_c', orig_start_s: 120, orig_end_s: 170 }, media: LOCAL,
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [120, 170], has_clip: true },
  window: { segments: SEGMENTS, text_complete: true, next_cursor: null },
  ...over });
const passage = (over = {}) => record({ record_kind: 'passage', status: 'mention', read: null, evidence: null, ...over });
const excerpt = (over = {}) => record({ disclosure: 'excerpt', window: undefined, text: 'Go to northwind. Use code QUIET.',
  excerpt_bounds: { start_s: 120, end_s: 170 }, ...over });
const page = rec => render('#/evidence/' + encodeURIComponent(REF), p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  throw new Error('unexpected ' + p);
});
// The words section ("What was said", or the excerpt).
function said(html) {
  const at = html.indexOf('<section class="ew-card ew-said');
  assert.ok(at >= 0, 'a words section is rendered');
  return html.slice(at, html.indexOf('</section>', at) + '</section>'.length);
}
const lineOf = (html, seg) => {
  const m = new RegExp(`<li[^>]*data-segment="${seg}"[^>]*>[\\s\\S]*?</li>`).exec(html);
  assert.ok(m, `line ${seg} rendered`);
  return m[0];
};

for (const [name, make] of [['evidence', record], ['passage', passage]]) {
  test(`${name} page with a local clip: every line has its times; lines in [offset + lead, end] get data-in-clip and a Play from button`, async () => {
    const words = said(await page(make()));
    assert.match(words, /<ol class="transcript"[^>]* data-clip-lines="ev"/, 'the list names its clip box');
    const expect = { 1: [100, 116, false], 2: [116, 124.5, true], 3: [125, 150, true], 4: [152, 169, true], 5: [170, 180, false] };
    for (const [seg, [start, end, inClip]] of Object.entries(expect)) {
      const li = lineOf(words, seg);
      const open = /<li[^>]*>/.exec(li)[0];
      assert.match(open, new RegExp(` data-start="${start}" data-end="${end}"`), `line ${seg} times: ${open}`);
      const clock = `${Math.floor(start / 60)}:${String(Math.floor(start % 60)).padStart(2, '0')}`;
      if (inClip) {
        assert.match(open, / data-in-clip[ >]/, `line ${seg} in the clip`);
        assert.ok(li.includes(`<button type="button" class="mono ew-line-play" data-action="clip-line" data-clip="ev" aria-label="Play from ${clock}" title="Play from ${clock}">${clock}</button>`), `line ${seg} button: ${li}`);
        assert.doesNotMatch(li, /<span class="mono">/, 'the button replaces the time label');
      } else {
        assert.doesNotMatch(open, /data-in-clip/, `line ${seg} outside the clip`);
        assert.doesNotMatch(li, /<button|clip-line|Play from/, `line ${seg} has no button`);
        assert.ok(li.includes(`<span class="mono">${clock}</span>`), `line ${seg} keeps its time label`);
      }
    }
    assert.equal(words.split('data-action="clip-line"').length - 1, 3, 'one button per in-clip line');
    // The continuation chunk still extends its line; no whole-line action.
    assert.match(lineOf(words, 2), /Thanks to Northwind for this one\./);
    assert.doesNotMatch(words, /<li[^>]*data-action/, 'no whole-line click');
    assert.doesNotMatch(words, /aria-live|role="status"|is-now|aria-current/, 'no live region, nothing highlighted before play');
  });
}

/* CTR-R1-02: a line heard only in the 3 s lead-in (112-118.5: it ends
   after offset + lead = 117 but before the read start, 120) is in the
   clip: the lower bound is the lead-in start, not the read start. */
test('a line that ends inside the lead-in (112-118.5) is in the clip and gets a Play from button', async () => {
  const segments = [
    { segment_id: 1, start_s: 100, end_s: 112, text: 'Before the lead-in.', text_start: 0 },
    { segment_id: 7, start_s: 112, end_s: 118.5, text: 'Just before the read.', text_start: 0 },
    ...SEGMENTS.slice(1),
  ];
  const words = said(await page(record({ window: { segments, text_complete: true, next_cursor: null } })));
  const li = lineOf(words, 7);
  assert.match(/<li[^>]*>/.exec(li)[0], / data-start="112" data-end="118.5" data-in-clip[ >]/, li);
  assert.ok(li.includes('<button type="button" class="mono ew-line-play" data-action="clip-line" data-clip="ev" aria-label="Play from 1:52" title="Play from 1:52">1:52</button>'), li);
  assert.doesNotMatch(lineOf(words, 1), /data-in-clip|<button/, 'line 1 ends at 112 s, before the lead-in');
});

test('the Play from buttons are only in the clip box\'s words section (not the player, facts or side)', async () => {
  const html = await page(record());
  const words = said(html);
  assert.equal(html.split('data-action="clip-line"').length, words.split('data-action="clip-line"').length);
  assert.equal(html.split('data-in-clip').length, words.split('data-in-clip').length);
});

/* Byte-identical to a57b9030: the words section of a page without a local
   clip (every no-clip reason, evidence and passage) and of the excerpt
   view (even with a local clip). Captured from launch-readiness a57b9030
   with these fixtures. */
const GOLDEN = {
  evidence: "<section class=\"ew-card ew-said\" aria-labelledby=\"ew-said-h\"><h2 id=\"ew-said-h\">What was said</h2><div class=\"window-text\" data-window-ref=\"sr1:c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1\" data-cursor=\"\" data-count=\"6\" data-loaded-from=\"100\" data-loaded-to=\"180\"><ol class=\"transcript\"><li class=\"\" data-segment=\"1\"><span class=\"mono\">1:40</span><div><p>Before the lead-in.</p></div></li><li class=\"is-hit\" data-segment=\"2\"><span class=\"mono\">1:56</span><div><p>Thanks to Northwind for this one.</p></div></li><li class=\"is-hit\" data-segment=\"3\"><span class=\"mono\">2:05</span><div><p>Go to northwind.</p></div></li><li class=\"is-hit\" data-segment=\"4\"><span class=\"mono\">2:32</span><div><p>Use code QUIET.</p></div></li><li class=\"\" data-segment=\"5\"><span class=\"mono\">2:50</span><div><p>Back to the show.</p></div></li></ol><p class=\"small muted\">Complete text.</p></div><a class=\"text-link\" href=\"#/episode/ep_c?at=120\">Open in the full transcript</a></section>",
  passage: "<section class=\"ew-card ew-said\" aria-labelledby=\"ew-said-h\"><h2 id=\"ew-said-h\">What was said</h2><div class=\"window-text\" data-window-ref=\"sr1:c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1c1\" data-cursor=\"\" data-count=\"6\" data-loaded-from=\"100\" data-loaded-to=\"180\"><ol class=\"transcript\"><li class=\"\" data-segment=\"1\"><span class=\"mono\">1:40</span><div><p>Before the lead-in.</p></div></li><li class=\"\" data-segment=\"2\"><span class=\"mono\">1:56</span><div><p>Thanks to Northwind for this one.</p></div></li><li class=\"\" data-segment=\"3\"><span class=\"mono\">2:05</span><div><p>Go to northwind.</p></div></li><li class=\"\" data-segment=\"4\"><span class=\"mono\">2:32</span><div><p>Use code QUIET.</p></div></li><li class=\"\" data-segment=\"5\"><span class=\"mono\">2:50</span><div><p>Back to the show.</p></div></li></ol><p class=\"small muted\">Complete interval text. Selected 2:00–2:50 (the whole passage).</p></div><a class=\"text-link\" href=\"#/episode/ep_c?at=120\">Open in the full transcript</a></section>",
  excerpt: "<section class=\"ew-card ew-said ew-excerpt\" aria-labelledby=\"ew-said-h\"><h2 id=\"ew-said-h\">Excerpt</h2><blockquote class=\"ew-excerpt-text\">Go to northwind. Use code QUIET.</blockquote></section>",
};
const NO_CLIP = [
  ['no clip row', {}],
  ['read over 90 s', { playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown' }],
  ['held (unavailable)', { playback: 'unavailable', reason: 'registered media lacks pinned recording proof', alignment: 'unknown' }],
  ['file changed', { playback: 'unavailable', alignment: 'mismatch', reason: 'recording alignment mismatch', trust: 'rotated' }],
  ['audio denied', { playback: 'remote', alignment: 'unknown', reason: 'audio not allowed' }],
];
for (const [why, media] of NO_CLIP) {
  for (const [name, make] of [['evidence', record], ['passage', passage]]) {
    test(`no local clip (${why}), ${name}: the words section is unchanged (no times, no buttons)`, async () => {
      const words = said(await page(make({ media })));
      assert.equal(words, GOLDEN[name]);
      assert.doesNotMatch(words, /data-start=|data-in-clip|data-clip-lines|clip-line|Play from/);
    });
  }
}
test('excerpt view (Plan A), even with a local clip: the words section is unchanged', async () => {
  for (const media of [LOCAL, {}]) {
    const words = said(await page(excerpt({ media })));
    assert.equal(words, GOLDEN.excerpt);
  }
});

/* -- 2. Behaviour on a small DOM, loader as frontend_clip_player_states -- */

const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
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
  get className() { return [...this.classes].join(' '); }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  removeAttribute(n) { delete this.attrs[n]; }
  hasAttribute(n) { return n in this.attrs; }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } return this; }
  get parentNode() { return this.parent; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  appendChild(k) { this.append(k); return k; }
  all() { return this.children.flatMap(c => [c, ...c.all()]); }
  matches(sel) { return sel.split(',').some(s => matchOne(this, s.trim())); }
  querySelector(sel) { return this.all().find(e => e.matches(sel)) || null; }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  closest(sel) { for (let e = this; e; e = e.parent) if (e.matches(sel)) return e; return null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() { this.t && this.t.focused.push(this); }
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
class Audio extends El {
  constructor(t) {
    super('audio');
    Object.assign(this, { t, paused: true, ended: false, readyState: 0, error: null, currentTime: 0, plays: 0, loads: 0, waiters: [], src: 'x' });
  }
  play() { this.plays++; this.paused = false; this.ended = false; return new Promise((resolve, reject) => this.waiters.push({ resolve, reject })); }
  pause() {
    if (this.paused) return;
    this.paused = true;
    for (const w of this.waiters.splice(0)) w.reject(Object.assign(new Error('interrupted'), { name: 'AbortError' }));
  }
  load() { this.loads++; this.error = null; this.readyState = 0; this.currentTime = 0; }
}
async function dom() {
  const t = { docListeners: {}, focused: [] };
  const body = new El('body');
  const doc = {
    body, visibilityState: 'visible', hasFocus: () => true,
    addEventListener: (type, fn) => { (t.docListeners[type] = t.docListeners[type] || []).push(fn); },
    querySelector: sel => body.querySelector(sel), querySelectorAll: sel => body.querySelectorAll(sel),
    getElementById: id => body.all().find(e => e.id === id) || null,
    createElement: tag => Object.assign(new El(tag), { t }), createTextNode: s => s,
  };
  const win = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: '#/search' }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
    fetch: async url => {
      if (url !== '/api/v1/session') throw new Error('unexpected ' + url);
      return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
        workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: doc,
  };
  win.window = win; win.globalThis = win;
  vm.createContext(win);
  const real = doc.querySelector;
  doc.querySelector = sel => real(sel) || (/^#search-audio|^\[data-clip-lines/.test(sel) ? null : new El('div'));
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.addEventListener = () => {};
  win.EW.current.route = '#/search';
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  win.EW.toast = () => {};
  Object.assign(t, { win, doc, body });
  t.fire = (a, type) => {
    if (type === 'playing') for (const w of a.waiters.splice(0)) w.resolve();
    const ev = { type, target: a };
    for (const fn of t.docListeners[type] || []) fn(ev);
    for (const fn of a.listeners[type] || []) fn(ev);
  };
  await drain(40);
  return t;
}
/* The evidence page as rendered: the clip box (offset 100, read 120-170,
   lead 17) and the words section with SEGMENTS' lines (1 and 5 outside
   the clip). withClip false: a page whose list names no clip box. */
function evidencePage(t, { withClip = true, clip = {} } = {}) {
  const box = new El('section', { dataset: { clipId: 'ev', start: '120', end: '170', offset: '100', lead: '17', ...clip } });
  box.classes.add('ew-player');
  const btn = new El('button', { dataset: { action: 'clip-toggle', clip: 'ev' } });
  btn.classes.add('ew-play'); btn.setAttribute('aria-label', 'Play clip, 2:00 to 2:50');
  const status = new El('p', { dataset: { clipStatus: '' } });
  const err = new El('p', { hidden: true }); err.classes.add('ew-clip-error');
  const audio = new Audio(t); audio.id = 'clip-ev'; audio.dataset.clip = 'ev';
  box.append(btn, err, status, audio);
  const section = new El('section'); section.classes.add('ew-said');
  const win = new El('div'); win.classes.add('window-text');
  const list = new El('ol'); list.classes.add('transcript');
  if (withClip) list.dataset.clipLines = 'ev';
  const lines = {};
  for (const [seg, start, end, inClip] of [[1, 100, 116, false], [2, 116, 124.5, true], [3, 125, 150, true], [4, 152, 169, true], [5, 170, 180, false]]) {
    const li = new El('li', { dataset: { segment: String(seg) } });
    if (withClip) { li.dataset.start = String(start); li.dataset.end = String(end); if (inClip) li.dataset.inClip = ''; }
    const time = withClip && inClip ? new El('button', { dataset: { action: 'clip-line', clip: 'ev' } }) : new El('span');
    time.classes.add('mono');
    li.append(time, new El('div'));
    list.append(li);
    lines[seg] = { li, time };
  }
  win.append(list); section.append(win);
  t.body.append(box, section);
  return { box, btn, status, audio, list, lines, win };
}
const at = (t, p, episodeSeconds, type = 'timeupdate') => { p.audio.currentTime = episodeSeconds - 100; t.fire(p.audio, type); };
const now = p => p.list.querySelectorAll('li').filter(li => li.classes.has('is-now') || li.getAttribute('aria-current') !== null);
function playingLine(p, seg, what) {
  const on = now(p);
  assert.deepEqual(on.map(li => li.dataset.segment), seg == null ? [] : [String(seg)], `${what}: highlighted lines`);
  for (const li of on) {
    assert.ok(li.classes.has('is-now'), `${what}: is-now`);
    assert.equal(li.getAttribute('aria-current'), 'true', `${what}: aria-current`);
  }
}
const press = (t, el) => t.win.EWViews.handleAction(el);
const near = (got, want, what = '') => assert.ok(Math.abs(got - want) < 1e-9, `${what}: ${got} != ${want}`);

test('highlight: the playing line gets is-now and aria-current="true"; both move to the next line and clear from the old one', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.audio.paused = false;
  at(t, p, 118); playingLine(p, 2, '118 s');
  at(t, p, 124); playingLine(p, 2, '124 s, same line');
  at(t, p, 126); playingLine(p, 3, '126 s');
  assert.equal(p.lines[2].li.getAttribute('aria-current'), null, 'aria-current cleared from the old line');
  assert.equal(p.lines[2].li.classes.has('is-now'), false, 'is-now cleared from the old line');
  at(t, p, 160); playingLine(p, 4, '160 s');
  // No live region and no focus move with the highlight.
  assert.equal(p.status.textContent, '', 'the live status says nothing per line');
  assert.deepEqual(t.focused, [], 'focus never moves');
});

test('highlight: in a gap between lines the previous line stays highlighted', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.audio.paused = false;
  at(t, p, 124.8); playingLine(p, 2, 'gap 124.5-125');
  at(t, p, 151); playingLine(p, 3, 'gap 150-152');
  at(t, p, 169.5); playingLine(p, 4, 'after the last in-clip line, before the clip end');
});

test('highlight: nothing before the first line, and nothing at or after the clip end (cleared)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  // Lines start at 100 (offset): move the first line so a time before it exists.
  p.lines[1].li.dataset.start = '105';
  p.audio.paused = false;
  at(t, p, 103); playingLine(p, null, 'before the first line');
  at(t, p, 140); playingLine(p, 3, 'fixture');
  at(t, p, 103); playingLine(p, null, 'back before the first line: cleared');
  at(t, p, 140); playingLine(p, 3, 'fixture');
  at(t, p, 170); playingLine(p, null, 'at the clip end');
  assert.equal(p.audio.paused, true, 'the clip stops at its end, as before');
  at(t, p, 175, 'seeked'); playingLine(p, null, 'after the clip end (line 5 is outside the clip)');
});

test('highlight: a seek while paused (seeked) moves it too', async () => {
  const t = await dom();
  const p = evidencePage(t);
  assert.equal(p.audio.paused, true);
  at(t, p, 130, 'seeked'); playingLine(p, 3, 'seeked while paused');
  at(t, p, 155, 'seeked'); playingLine(p, 4, 'seeked again');
  assert.equal(p.audio.plays, 0, 'a seek never plays');
});

test('highlight: a page whose lines name no clip box is untouched', async () => {
  const t = await dom();
  const p = evidencePage(t, { withClip: false });
  p.audio.paused = false;
  at(t, p, 130); at(t, p, 130, 'seeked');
  playingLine(p, null, 'no clip lines');
});

test('Play from (paused): seeks 0.3 s before the line start in the clip file and plays through clipToggle (loading look)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  near(p.audio.currentTime, 24.7, 'line 3 starts at 125 s: 25 s into the file (offset 100), less 0.3 s');
  assert.equal(p.audio.plays, 1, 'it plays');
  assert.ok(p.btn.classes.has('is-loading'), 'the player shows its loading look (clipToggle)');
  assert.equal(p.status.textContent, 'Loading clip…');
  t.fire(p.audio, 'playing'); await drain();
  assert.equal(p.btn.getAttribute('aria-pressed'), 'true');
  assert.deepEqual(t.focused, [], 'focus stays on the pressed button');
});

test('Play from: a line that starts before the lead-in seeks to the lead-in (max(line.start - 0.3, offset + lead))', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[2].time); await drain();
  assert.equal(p.audio.currentTime, 17, 'line 2 starts at 116 s, before 117 s (offset + lead)');
});

test('Play from: never past the clip end (clamped like clipSeekBy)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.lines[4].li.dataset.start = '172';
  // Playing: the seek alone, clamped to the read end (170 s - offset).
  await press(t, p.btn); await drain();
  t.fire(p.audio, 'play'); t.fire(p.audio, 'playing'); await drain();
  await press(t, p.lines[4].time); await drain();
  assert.equal(p.audio.currentTime, 70, 'clamped to the read end (170 s - offset)');
  // Paused: clamped to the read end, then Play there replays from the
  // lead-in start (the replay rule clipToggle keeps).
  p.audio.paused = true;
  await press(t, p.lines[4].time); await drain();
  assert.equal(p.audio.currentTime, 17, 'the replay rule');
});

test('Play from (playing): seeks and keeps playing, no second play or pause', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.btn); await drain();
  t.fire(p.audio, 'play'); t.fire(p.audio, 'playing'); await drain();
  assert.equal(p.audio.plays, 1);
  await press(t, p.lines[4].time); await drain();
  near(p.audio.currentTime, 51.7);
  assert.equal(p.audio.plays, 1, 'no second play');
  assert.equal(p.audio.paused, false, 'still playing');
  // 151.7 s is in the gap after line 3: the pressed line 4 is the playing line already.
  t.fire(p.audio, 'seeked'); playingLine(p, 4, 'after the seek');
});

test('Play from (loading): a pending start is not stopped; it starts from the line', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.btn); await drain();
  // Pending: asked to play, not playing yet; a second clipToggle would stop it.
  p.audio.paused = true;
  await press(t, p.lines[3].time); await drain();
  near(p.audio.currentTime, 24.7);
  assert.ok(p.btn.classes.has('is-loading'), 'still loading, not stopped');
  assert.equal(p.audio.plays, 1, 'no second play');
});

test('Play from (after a failed load): retries the load, then plays from the line', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.audio.error = { code: 4 };
  await press(t, p.lines[3].time); await drain();
  assert.equal(p.audio.loads, 1, 'the load is reset (as Retry)');
  near(p.audio.currentTime, 24.7, 'the seek survives the load');
  assert.equal(p.audio.plays, 1);
});

test('Play from (paused at the read end): plays from the line, not the replay start', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.audio.currentTime = 70; p.audio.ended = true;
  await press(t, p.lines[3].time); await drain();
  near(p.audio.currentTime, 24.7);
  assert.equal(p.audio.plays, 1);
});

/* WebKit (headless, not Safari app; TJG Quince on the scratch host): a
   seek made before the media loads is followed by its own seek to the
   src's #t= start: seeking@29.60 rs1, canplay@2.00, seeking@2.00,
   seeked@2.00, playing@2.00, so Play from on a fresh page played from
   the lead-in. Chromium keeps the seek (seeked@29.60). */
test('Play from before the media loads: a seek the browser then moves to the #t= start is applied again (WebKit order)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  assert.equal(p.audio.readyState, 0);
  await press(t, p.lines[3].time); await drain();
  near(p.audio.currentTime, 24.7);
  // WebKit: metadata in, then its own seek to the #t= start (17 here).
  p.audio.readyState = 4; p.audio.currentTime = 17; t.fire(p.audio, 'seeked');
  near(p.audio.currentTime, 24.7, 'the line start again, not the #t= start');
  playingLine(p, 3, 'the pressed line, not the one before it');
  t.fire(p.audio, 'seeked'); t.fire(p.audio, 'playing');
  // Done once: a later seek is the person's own.
  p.audio.currentTime = 40; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 40, 'applied once only');
});

test('Play from before the media loads, Chromium order (the seek is kept): nothing is applied again later', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  p.audio.readyState = 4; t.fire(p.audio, 'seeked'); t.fire(p.audio, 'playing');
  p.audio.currentTime = 40; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 40);
});

test('Play from before the media loads, then a ±5 s step: the newest seek wins (Play from is not applied again)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  await press(t, { dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null }); await drain();
  near(p.audio.currentTime, 29.7);
  p.audio.readyState = 4; t.fire(p.audio, 'seeked');
  near(p.audio.currentTime, 29.7, 'the step stands');
});

test('Play from on loaded media: nothing is applied again later', async () => {
  const t = await dom();
  const p = evidencePage(t);
  p.audio.readyState = 4;
  await press(t, p.lines[3].time); await drain();
  p.audio.currentTime = 40; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 40);
});

test('Play from (lead-in line, 112-118.5): seeks to the lead-in start (17 s), not the read start', async () => {
  const t = await dom();
  const p = evidencePage(t);
  const li = new El('li', { dataset: { segment: '7', start: '112', end: '118.5', inClip: '' } });
  const time = new El('button', { dataset: { action: 'clip-line', clip: 'ev' } });
  li.append(time, new El('div'));
  p.list.children.splice(1, 0, li); li.parent = p.list;
  await press(t, time); await drain();
  assert.equal(p.audio.currentTime, 17);
});

/* CTR-SCR-R1-04 second part: Play from seeks 0.3 s before the line, into
   the end of the line before it (or the gap after it). The pressed line is
   the playing line from the seek on; at its start the normal rule takes
   over. */
test('Play from: the pressed line is the playing line from the seek until its start (the line before does not flash)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  // Line 2 ends at 124.5, so 124.7 is the gap after it: the rule alone says line 2.
  await press(t, p.lines[3].time); await drain();
  t.fire(p.audio, 'seeked'); playingLine(p, 3, 'seeked at 124.7');
  t.fire(p.audio, 'playing');
  at(t, p, 124.8); playingLine(p, 3, '124.8 s');
  at(t, p, 125); playingLine(p, 3, 'at the line start');
  at(t, p, 130); playingLine(p, 3, '130 s');
  // Done once it starts: a later seek (here not through the player, as
  // the system media controls do) into the same gap follows the rule.
  at(t, p, 124.8, 'seeked'); playingLine(p, 2, 'a later seek into the gap: the line before');
});

test('Play from on a line right after another (its 0.3 s before is inside the line before): the line before is not shown', async () => {
  const t = await dom();
  const p = evidencePage(t);
  // Line 4 moved to start inside line 3 (125-150): 149.7 is in line 3.
  p.lines[4].li.dataset.start = '150';
  await press(t, p.lines[4].time); await drain();
  near(p.audio.currentTime, 49.7);
  t.fire(p.audio, 'seeked'); playingLine(p, 4, 'seeked at 149.7');
  p.audio.paused = false;
  at(t, p, 149.9); playingLine(p, 4, '149.9 s');
  at(t, p, 151); playingLine(p, 4, '151 s');
});

test('Play from, then a seek elsewhere not through the player (system media controls): the rule, not the pressed line', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[4].time); await drain();
  t.fire(p.audio, 'seeked'); playingLine(p, 4, 'seeked at 151.7');
  at(t, p, 130, 'seeked'); playingLine(p, 3, 'moved back to 130 s');
});

test('Play from, then a ±5 s step back: the newest seek wins and the line before plays as usual', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  await press(t, { dataset: { action: 'clip-seek', clip: 'ev', delta: '-5' }, closest: () => null }); await drain();
  near(p.audio.currentTime, 19.7);
  p.audio.paused = false;
  at(t, p, 124.7); playingLine(p, 2, 'played up to 124.7 after the step: the rule (line 2)');
});

/* CTR-R1-01: a seek bar move (drag or keys) made after a Play from before
   the media loads wins over the WebKit re-seek, like a ±5 s step. */
test('Play from before the media loads, then a seek bar move: the seek bar position stands (WebKit order)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  const range = new El('input', { dataset: { clipSeek: 'ev' }, value: '25', max: '50' });
  await press(t, p.lines[3].time); await drain();
  for (const fn of t.docListeners.input) fn({ target: range });
  assert.equal(p.audio.currentTime, 45, 'the seek bar: 120 s + 25 s, 45 s into the file');
  p.audio.readyState = 4; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 45, 'the seek bar move stands');
  // The End key on the seek bar, the same way.
  const t2 = await dom();
  const p2 = evidencePage(t2);
  await press(t2, p2.lines[3].time); await drain();
  for (const fn of t2.docListeners.keydown) fn({ key: 'End', target: range, preventDefault() {} });
  assert.equal(p2.audio.currentTime, 70);
  p2.audio.readyState = 4; t2.fire(p2.audio, 'seeked');
  assert.equal(p2.audio.currentTime, 70, 'the End key stands');
});

/* SEEK-PRELOAD unify (lr-app-seek-preload): ONE pending seek for Play
   from, the seek bar, its keys and the ±5 s steps. The newest wins, it is
   applied again at most once, and a second press or a play on loaded
   media drops it. Every write to currentTime after the load is logged. */
function seekLog(audio) {
  let v = audio.currentTime;
  const log = [];
  Object.defineProperty(audio, 'currentTime', { get: () => v, set: x => { v = x; log.push(x); }, configurable: true });
  return log;
}
const seekRange = () => new El('input', { dataset: { clipSeek: 'ev' }, value: '25', max: '50' });
const BEFORE_PLAY_FROM = [
  ['a seek bar drag', (t, range) => { for (const fn of t.docListeners.input) fn({ target: range }); }],
  ['the End key', (t, range) => { for (const fn of t.docListeners.keydown) fn({ key: 'End', target: range, preventDefault() {} }); }],
  ['a +5 s step', t => press(t, { dataset: { action: 'clip-seek', clip: 'ev', delta: '5' }, closest: () => null })],
];
for (const [what, move] of BEFORE_PLAY_FROM) {
  for (const engine of ['WebKit', 'Chromium']) {
    test(`unify: ${what}, then Play from, before the media loads (${engine} order): Play from wins, applied again at most once`, async () => {
      const t = await dom();
      const p = evidencePage(t);
      await move(t, seekRange()); await drain();
      await press(t, p.lines[3].time); await drain();
      near(p.audio.currentTime, 24.7, 'fixture: Play from seeks');
      const log = seekLog(p.audio);
      p.audio.readyState = 4;
      if (engine === 'WebKit') { p.audio.currentTime = 17; log.length = 0; }
      t.fire(p.audio, 'seeked');
      t.fire(p.audio, 'seeked'); t.fire(p.audio, 'playing');
      near(p.audio.currentTime, 24.7, 'the newest seek (Play from)');
      assert.deepEqual(log, engine === 'WebKit' ? [24.7] : [], 'the seeks applied again after the load');
    });
  }
}

test('unify: Play from before the media loads, then a second press (stop while loading): nothing is applied again', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  await press(t, p.btn); await drain();
  assert.ok(!p.btn.classes.has('is-loading'), 'fixture: stopped while loading');
  // The load goes on: WebKit's own seek to the #t= start.
  p.audio.readyState = 4; p.audio.currentTime = 17; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 17, 'a stopped Play from still moved the media');
});

test('unify: a play on loaded media never applies an older pending seek (a seek bar move before the metadata came in)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  for (const fn of t.docListeners.input) fn({ target: seekRange() });
  assert.equal(p.audio.currentTime, 45, 'fixture');
  p.audio.readyState = 1; // metadata in, no seek event
  await press(t, p.btn); await drain();
  p.audio.readyState = 4; t.fire(p.audio, 'playing');
  p.audio.currentTime = 40; t.fire(p.audio, 'seeked');
  assert.equal(p.audio.currentTime, 40, 'the older seek bar move was applied over a later seek');
});

/* CTR-R2-04: in WebKit one timeupdate at the #t= start comes before the
   re-seek; while a seek is pending the highlight uses it as the time. */
test('CTR-R2-04: a pending seek is the highlight time (WebKit timeupdate at the #t= start before the re-seek)', async () => {
  const t = await dom();
  const p = evidencePage(t);
  await press(t, p.lines[3].time); await drain();
  p.audio.readyState = 4; p.audio.currentTime = 17; t.fire(p.audio, 'timeupdate');
  playingLine(p, 3, 'Play from 2:05: the pressed line, not the lead-in line');
  t.fire(p.audio, 'seeked');
  near(p.audio.currentTime, 24.7);
  playingLine(p, 3, 'after the re-seek');
  // The seek bar the same way: 0:25 of the clip, 145 s, line 3 (125-150).
  const t2 = await dom();
  const q = evidencePage(t2);
  for (const fn of t2.docListeners.input) fn({ target: seekRange() });
  q.audio.readyState = 4; q.audio.currentTime = 17; t2.fire(q.audio, 'timeupdate');
  playingLine(q, 3, 'seek bar at 145 s: line 3, not the lead-in line');
});

/* SPL-R1-01 / SPL-SCR-R1-02: a clip with no lead-in (its file starts at
   the read: offset 100, read 100-170, lead 0, so no #t= start). A seek to
   0 before the first play makes no seek at load (no seeking, no seeked):
   no pending seek may stay, or the highlight freezes on the first line. */
const TO_ZERO = [
  ['Play from on the first line', (t, p) => { Object.assign(p.lines[1].time.dataset, { action: 'clip-line', clip: 'ev' }); return press(t, p.lines[1].time); }],
  ['Home', t => { for (const fn of t.docListeners.keydown) fn({ key: 'Home', target: seekRange(), preventDefault() {} }); }],
  ['-5 s', t => press(t, { dataset: { action: 'clip-seek', clip: 'ev', delta: '-5' }, closest: () => null })],
  ['End, then Play (replays from 0)', t => { for (const fn of t.docListeners.keydown) fn({ key: 'End', target: seekRange(), preventDefault() {} }); }],
];
for (const [what, move] of TO_ZERO) {
  test(`SPL-R1-01: no lead-in, ${what} before the first play: no pending seek stays, the highlight follows the audio`, async () => {
    const t = await dom();
    const p = evidencePage(t, { clip: { start: '100', lead: '0' } });
    await move(t, p); await drain();
    if (p.audio.paused) { await press(t, p.btn); await drain(); }
    assert.equal(p.audio.currentTime, 0, 'fixture: the seek is to 0');
    // The load: no #t= start, so no seek and no seeked.
    p.audio.readyState = 4; t.fire(p.audio, 'playing');
    at(t, p, 101); playingLine(p, 1, '1:41');
    at(t, p, 130); playingLine(p, 3, '2:10: the highlight froze on the first line');
    at(t, p, 160); playingLine(p, 4, '2:40');
    assert.equal(p.audio.dataset.pendingSeek, undefined, 'a pending seek that no seeked will consume');
  });
}

test('appended lines ("Load the next part") get their times, data-in-clip and the button by the same rule', async () => {
  const t = await dom();
  const p = evidencePage(t);
  // The tail ends at line 4 for this test; the next part brings 4's continuation, 4b and 5.
  p.list.children = p.list.children.filter(li => li.dataset.segment !== '5');
  p.win.dataset = { windowRef: REF, cursor: 'c1', count: '4', loadedFrom: '100', loadedTo: '169' };
  t.win.EW.api = async () => ({ record: { ref: REF, window: { text_complete: true, next_cursor: null, segments: [
    { segment_id: 4, start_s: 152, end_s: 169, text: ' And more.', text_start: 15 },
    { segment_id: 6, start_s: 165, end_s: 171, text: 'Last words.', text_start: 0 },
    { segment_id: 5, start_s: 171, end_s: 180, text: 'Back to the show.', text_start: 0 },
  ] } } });
  await press(t, { dataset: { action: 'window-more', ref: REF, cursor: 'c1' }, closest: sel => (sel === '.window-text' ? p.win : null) }); await drain();
  const added = p.list.children.slice(4);
  assert.deepEqual(added.map(li => li.dataset.segment), ['6', '5'], 'continuation joined line 4');
  const [six, five] = added;
  assert.equal(six.dataset.start, '165'); assert.equal(six.dataset.end, '171');
  assert.equal(six.dataset.inClip, '', 'line 6 overlaps the clip');
  assert.match(six.innerHTML, /^<button type="button" class="mono ew-line-play" data-action="clip-line" data-clip="ev" aria-label="Play from 2:45" title="Play from 2:45">2:45<\/button>/);
  assert.equal(five.dataset.start, '171'); assert.equal(five.dataset.end, '180');
  assert.equal(five.dataset.inClip, undefined, 'line 5 starts after the clip end');
  assert.doesNotMatch(five.innerHTML, /<button|clip-line|Play from/);
  assert.match(five.innerHTML, /^<span class="mono">2:51<\/span>/);
  // The appended line highlights like the others.
  p.audio.paused = false;
  at(t, p, 169.5); playingLine(p, 6, 'appended line playing');
});

test('appended lines on a page whose list names no clip box are unchanged', async () => {
  const t = await dom();
  const p = evidencePage(t, { withClip: false });
  p.win.dataset = { windowRef: REF, cursor: 'c1', count: '5' };
  t.win.EW.api = async () => ({ record: { ref: REF, window: { text_complete: true, next_cursor: null, segments: [
    { segment_id: 6, start_s: 181, end_s: 190, text: 'Later.', text_start: 0 }] } } });
  await press(t, { dataset: { action: 'window-more', ref: REF, cursor: 'c1' }, closest: sel => (sel === '.window-text' ? p.win : null) }); await drain();
  const li = p.list.children[5];
  assert.deepEqual(Object.keys(li.dataset), ['segment']);
  assert.equal(li.innerHTML, '<span class="mono">3:01</span><div><p>Later.</p></div>');
});

/* -- 3. Styles -------------------------------------------------------- */

test('styles: the playing line has a left bar and the action-coloured time (not colour alone), no transition', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = sel => { const m = new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}').exec(css); return m ? m[1] : ''; };
  assert.match(rule('.ew-said .transcript li.is-now'), /box-shadow:\s*inset 3px 0 0 var\(--ew-action\)/, 'a 3 px left bar');
  assert.match(rule('.ew-said .transcript li.is-now > .mono'), /color:\s*var\(--ew-action\)/);
  assert.match(rule('.ew-said .transcript li.is-now > .mono'), /font-weight:\s*600/, 'the time is bold too');
  assert.doesNotMatch(css, /is-now[^{]*\{[^}]*transition/, 'no transition');
  assert.match(rule('.ew-said .transcript .ew-line-play'), /background:\s*none/, 'the button looks like the time label');
});

test('styles: hover and the playing line thicken the Play from underline to 2 px (CTR-SCR-R1-02)', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const m = /([^{}]+)\{([^}]*text-decoration-thickness:\s*2px[^}]*)\}/.exec(css);
  assert.ok(m, 'a 2 px underline rule');
  const sels = m[1].split(',').map(x => x.trim());
  assert.ok(sels.includes('.ew-said .transcript .ew-line-play:hover'), sels.join(' | '));
  assert.ok(sels.includes('.ew-said .transcript li.is-now > .ew-line-play'), sels.join(' | '));
});

/* -- 4. Hit area in a real headless Chromium (CTR-SCR-R1-01) ----------
   The evidence page's words section as rendered above, with every app
   stylesheet index.html links, at 390 and 1440. At 390 each Play from
   hit area (the button plus its ::after) is at least 44 px tall and wide,
   hit-testing at its edges finds the button, its look is unchanged, and
   no two lines' hit areas overlap. At 1440 nothing is added. Skips when
   that browser is not installed (a named verification gap). */
const CHROME = process.env.EW_CHROME || path.join(os.homedir(),
  'Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell');
const sleep = ms => new Promise(r => setTimeout(r, ms));
function appCss() {
  const html = fs.readFileSync(path.join(APP, 'index.html'), 'utf8');
  return [...html.matchAll(/<link rel="stylesheet" href="\/app\/([^"]+)"( media="print")?>/g)]
    .filter(m => !m[2]).map(m => fs.readFileSync(path.join(APP, m[1]), 'utf8')).join('\n');
}
async function chrome() {
  const root = process.env.AGENT_SCRATCH || os.tmpdir();
  const dir = fs.mkdtempSync(path.join(root, 'ew-line-play-chrome-'));
  const proc = spawn(CHROME, ['--headless', '--remote-debugging-port=0', `--user-data-dir=${dir}`,
    '--no-first-run', '--hide-scrollbars', 'about:blank'], { stdio: 'ignore' });
  let port;
  for (let i = 0; i < 100 && !port; i++) {
    await sleep(100);
    try { port = fs.readFileSync(path.join(dir, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch {}
  }
  // CTR-R2-01: close waits for the browser to exit (it writes its
  // profile while it shuts down; SIGKILL after 3 s), then removes the
  // folder; a failed setup closes too; the socket open times out.
  const exited = new Promise(r => proc.once('exit', r));
  const close = async () => {
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill();
      let timer;
      await Promise.race([exited, new Promise(r => { timer = setTimeout(() => { proc.kill('SIGKILL'); r(); }, 3000); })]);
      clearTimeout(timer);
      await Promise.race([exited, sleep(1000)]);
    }
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  };
  try {
    if (!port) throw new Error('headless Chromium did not start');
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(list.find(x => x.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('headless Chromium socket did not open in 10 s')), 10000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); });
      ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('headless Chromium socket error')); });
    });
    let id = 0; const pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { const q = pending.get(m.id); pending.delete(m.id); m.error ? q.reject(new Error(JSON.stringify(m.error))) : q.resolve(m.result); }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => { const i = ++id; pending.set(i, { resolve, reject }); ws.send(JSON.stringify({ id: i, method, params })); });
    return { send, pid: proc.pid, dir, close: async () => { ws.close(); await close(); } };
  } catch (e) { await close(); throw e; }
}
// Every Play from button: its box, its hit area (box + ::after) and edge hit tests.
const MEASURE = `(() => {
  const out = [];
  for (const b of document.querySelectorAll('.ew-line-play')) {
    const r = b.getBoundingClientRect(), a = getComputedStyle(b, '::after');
    const px = v => (v === 'auto' ? 0 : parseFloat(v));
    const hit = a.content === 'none' ? { top: r.top, bottom: r.bottom, left: r.left, right: r.right }
      : { top: r.top + px(a.top), bottom: r.bottom - px(a.bottom), left: r.left + px(a.left), right: r.right - px(a.right) };
    const cx = (hit.left + hit.right) / 2, cy = (hit.top + hit.bottom) / 2;
    const finds = (x, y) => document.elementFromPoint(x, y) === b;
    out.push({ text: b.textContent, box: [r.width, r.height], hit, after: a.content,
      edges: [finds(cx, hit.top + 0.5), finds(cx, hit.bottom - 0.5), finds(hit.left + 0.5, cy), finds(hit.right - 0.5, cy)],
      outside: [finds(cx, hit.top - 1.5), finds(cx, hit.bottom + 1.5)] });
  }
  return out;
})()`;
// CTR-R2-01: close() returns once the browser has exited (it writes
// Default/Session Storage while it shuts down), and the profile folder is gone.
test('CTR-R2-01: the headless Chromium helper closes once the browser has exited and leaves no profile folder',
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    const b = await chrome();
    await b.close();
    assert.throws(() => process.kill(b.pid, 0), { code: 'ESRCH' }, 'the browser still ran when close() returned');
    assert.equal(fs.existsSync(b.dir), false, 'the profile folder is left behind');
  });

for (const [name, make] of [['evidence (read lines, 33 px rows)', record], ['passage (plain lines, 27 px rows)', passage]]) {
test(`Play from hit area, ${name}: at least 44 px at 390 with no overlap between lines; unchanged at 1440 (headless Chromium)`,
  { skip: fs.existsSync(CHROME) ? false : `headless Chromium not installed at ${CHROME}` }, async () => {
    // Short lines too: the tightest rows a real transcript has.
    const segments = [
      ...SEGMENTS.slice(0, 4),
      { segment_id: 8, start_s: 150, end_s: 151, text: 'Yes.', text_start: 0 },
      { segment_id: 9, start_s: 151, end_s: 152, text: 'Okay.', text_start: 0 },
      ...SEGMENTS.slice(4),
    ];
    const html = await page(make({ window: { segments, text_complete: true, next_cursor: null } }));
    const b = await chrome();
    try {
      const { send } = b;
      await send('Page.enable');
      const ev = async expr => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.value;
      const { frameTree } = await send('Page.getFrameTree');
      await send('Page.setDocumentContent', { frameId: frameTree.frame.id,
        html: `<!doctype html><html><head><style>${appCss()}</style></head><body><main id="main">${html}</main></body></html>` });
      for (const width of [390, 1440]) {
        await send('Emulation.setDeviceMetricsOverride', { width, height: 2400, deviceScaleFactor: 1, mobile: false });
        await sleep(100);
        const rows = await ev(MEASURE);
        assert.equal(rows.length, 5, `five in-clip lines at ${width}`);
        for (const r of rows) {
          assert.ok(r.box[1] <= 33, `${width}: the button's own box keeps the time label's size: ${JSON.stringify(r)}`);
          if (width === 390) {
            assert.ok(r.hit.bottom - r.hit.top >= 44, `390: hit area height >= 44: ${JSON.stringify(r)}`);
            assert.ok(r.hit.right - r.hit.left >= 44, `390: hit area width >= 44: ${JSON.stringify(r)}`);
            assert.deepEqual(r.edges, [true, true, true, true], `390: hit tests at the hit area's edges find the button: ${JSON.stringify(r)}`);
            assert.deepEqual(r.outside, [false, false], `390: just outside the hit area is not the button: ${JSON.stringify(r)}`);
          } else {
            assert.equal(r.after, 'none', '1440: no ::after');
          }
        }
        for (let i = 1; i < rows.length; i++) {
          assert.ok(rows[i].hit.top >= rows[i - 1].hit.bottom, `${width}: hit areas of ${rows[i - 1].text} and ${rows[i].text} overlap: ${rows[i - 1].hit.bottom} > ${rows[i].hit.top}`);
        }
      }
      // CTR-SCR-R1-02 in the browser: hover and the playing line draw a 2 px underline.
      const thick = await ev(`(() => { const b = document.querySelectorAll('.ew-line-play'); b[1].closest('li').classList.add('is-now');
        return [getComputedStyle(b[0]).textDecorationThickness, getComputedStyle(b[1]).textDecorationThickness]; })()`);
      assert.deepEqual(thick, ['1px', '2px'], 'normal 1 px, playing line 2 px');
      const r0 = await ev(`(() => { const r = document.querySelectorAll('.ew-line-play')[0].getBoundingClientRect(); return [r.left + 5, r.top + 5]; })()`);
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: r0[0], y: r0[1] });
      assert.equal(await ev(`getComputedStyle(document.querySelectorAll('.ew-line-play')[0]).textDecorationThickness`), '2px', 'hover 2 px');
    } finally { await b.close(); }
  });
}
