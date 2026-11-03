/* VIS-NOV1 (lead LEDGER-LEAD3-VIS-1; visual audit 2026-10-27,
   R/audit-20261027/visual-audit.md):
   #12 Overview and Brand-page Recent ads Play plays the clip in place
       (one tap), through the Search player; its name says what it plays.
   #14 While the evidence clip loads or plays, its player stays on screen
       (sticky), so centring the playing line never scrolls Pause away.
   #16 Several loose words are never said as a phrase the ad says.
   #23 An evidence page is in the section the person came from (Back's
       origin): Overview from Overview, Search from Search or with none.
   Run: node --test tests/frontend_vis_nov1.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const APP = F.APP;
const tick = () => new Promise(res => setTimeout(res, 0));
const drain = async (n = 20) => { for (let i = 0; i < n; i++) await tick(); };
const attrsOf = tag => Object.fromEntries([...tag.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
const unesc = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/* -- #12 Recent ads Play ------------------------------------------------ */
const clipItem = (n, clip) => F.ovItem(n, 'brk:' + n, { ep_title: 'Episode ' + n,
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: true }, display_span: [60, 120], has_clip: clip } });
const recentCardOf = html => { const s = html.indexOf('ew-ov-recent'); return html.slice(s, html.indexOf('</section>', s)); };
const playsOf = html => [...String(html).matchAll(/<(a|button)\b[^>]*class="ew-play[^"]*"[^>]*>/g)].map(m => ({ tag: m[1], attrs: attrsOf(m[0]), raw: m[0] }));

function isPlayInPlace(p, n, where) {
  assert.equal(p.tag, 'button', `${where} row ${n}: Play is a button that plays here, not a link away: ${p.raw}`);
  assert.equal(p.attrs.type, 'button');
  assert.equal(p.attrs['data-action'], 'search-play', `${where} row ${n}: the Search player plays it`);
  assert.equal(unesc(p.attrs['data-ref']), F.REF(n), `${where} row ${n}: its own ad`);
  assert.equal(p.attrs['data-from'], '60', `${where} row ${n}: the span its name says`);
  assert.equal(p.attrs['data-to'], '120');
  assert.equal(p.attrs.href, undefined);
  assert.match(p.attrs['aria-label'], /^Play ChatGPT ad, Show A, Oct 9, 1:00 to 2:00$/, `${where} row ${n}: says it plays, and what`);
}

test('#12 Overview Recent ads: Play plays its clip in place (a button), named for what it plays', async () => {
  const { html } = await F.render('#/overview', F.overviewApi({ items: [clipItem(1, true), clipItem(2, false), clipItem(3, true)] }));
  const rows = recentCardOf(html).split('<li>').slice(1);
  assert.equal(rows.length, 3, 'fixture: three rows');
  for (const n of [1, 3]) {
    const row = rows.find(r => r.includes('Episode ' + n));
    const plays = playsOf(row);
    assert.equal(plays.length, 1, `row ${n}: one Play`);
    isPlayInPlace(plays[0], n, 'Overview');
    // Play first; the brand link and Open evidence still open the evidence page.
    assert.ok(row.startsWith('<div class="ew-ov-row"><button'), `row ${n}: Play comes first`);
    assert.equal([...row.matchAll(new RegExp(`href="#/evidence/${encodeURIComponent(F.REF(n)).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`, 'g'))].length, 2,
      `row ${n}: the brand link and Open evidence still open the evidence page`);
  }
  assert.equal(playsOf(rows.find(r => r.includes('Episode 2'))).length, 0, 'no clip: no Play');
});

test('#12 Brand page Recent ads: the same Play in place', async () => {
  const { html } = await F.render('#/brand/chatgpt', F.brandApi(F.brandDetail(), [clipItem(1, true), clipItem(3, true)]));
  const plays = playsOf(html);
  assert.equal(plays.length, 2, 'fixture: two Play controls');
  plays.forEach((p, i) => isPlayInPlace(p, [1, 3][i], 'Brand'));
});

/* A small DOM and media element (as frontend_clip_player_states): one
   press on the rendered row's Play reads that record and starts the
   Search player's audio at the row's span, with no route change. */
class El {
  constructor(tag, props = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.classes = new Set(); this.dataset = {};
    this.children = []; this.parent = null; this.innerHTML = ''; this.textContent = ''; this.listeners = {};
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
  appendChild(k) { this.append(k); return k; }
  get nextElementSibling() { const sib = this.parent ? this.parent.children : []; return sib[sib.indexOf(this) + 1] || null; }
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
class Audio extends El {
  constructor() { super('audio'); Object.assign(this, { paused: true, ended: false, readyState: 0, error: null, currentTime: 0, plays: 0, src: '' }); }
  play() { this.plays++; this.paused = false; return new Promise(() => {}); }
  pause() { this.paused = true; }
  load() {}
  removeAttribute(n) { if (n === 'src') this.src = ''; super.removeAttribute(n); }
}
async function playerPage(route) {
  const t = { reads: [], timers: new Set() };
  const body = new El('body');
  const doc = {
    body, addEventListener() {},
    querySelector: sel => body.querySelector(sel),
    querySelectorAll: sel => body.querySelectorAll(sel),
    getElementById: id => body.all().find(e => e.id === id) || null,
    createElement: tag => (tag === 'audio' ? new Audio() : new El(tag)),
    createTextNode: s => s, visibilityState: 'visible', hasFocus: () => true,
  };
  const win = {
    console, URLSearchParams, AbortController, Intl, clearTimeout,
    // Tracked, so the test ends with no player timer left running.
    setTimeout: (fn, ms, ...a) => { const id = setTimeout(fn, ms, ...a); t.timers.add(id); return id; },
    location: { hash: route }, history: { replaceState() {}, pushState() {}, state: null }, scrollTo() {},
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
  const real = doc.querySelector;
  doc.querySelector = sel => real(sel) || (/^#search-audio/.test(sel) ? null : new El('div'));
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), win, { filename: file });
  win.addEventListener = () => {};
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  win.EW.toast = () => {};
  win.EW.api = async p => {
    t.reads.push(p);
    if (!/^\/customer\/records\//.test(p)) throw new Error('unexpected ' + p);
    return { record: { media: { playback: 'local', source_offset_s: 0 }, selected_bounds: { start_s: 60, end_s: 120 } } };
  };
  await drain(40);
  return Object.assign(t, { win, body });
}

test('#12 one press on the Overview row\'s Play reads its record and starts its clip, on Overview', async () => {
  const { html } = await F.render('#/overview', F.overviewApi({ items: [clipItem(1, true)] }));
  const [play] = playsOf(recentCardOf(html));
  assert.ok(play, 'fixture: the row has a Play');
  const t = await playerPage('#/overview');
  const btn = new El(play.tag);
  for (const [k, v] of Object.entries(play.attrs)) if (k.startsWith('data-')) btn.dataset[camel(k.slice(5))] = unesc(v);
  btn.classes.add('ew-play');
  btn.setAttribute('aria-label', unesc(play.attrs['aria-label'] || ''));
  t.body.append(new El('li').append(btn));
  // A link's press has no action: the dispatcher would leave it to the browser (a route change).
  assert.ok(btn.dataset.action, 'the Play carries an action, so the press plays here');
  // The boot render's own page reads are not this press's.
  t.reads.length = 0;
  // Not awaited: the press waits on play(), which this media never settles.
  t.win.EWViews.handleAction(btn);
  await drain();
  assert.deepEqual(t.reads, ['/customer/records/' + encodeURIComponent(F.REF(1))], 'one record read, for this row');
  const audio = t.body.querySelector('#search-audio');
  assert.ok(audio, 'the Search player\'s audio');
  assert.equal(audio.src, '/api/v1/customer/records/' + encodeURIComponent(F.REF(1)) + '/media#t=57.0', 'its clip, 3 s before its read');
  assert.equal(audio.plays, 1, 'playing after one press');
  assert.equal(t.win.EW.current.route, '#/overview', 'still on Overview');
  for (const id of t.timers) clearTimeout(id);
});

/* -- #14 the evidence player stays on screen while it plays -------------- */
test('#14 a loading or playing evidence clip player is sticky inside the reader, on its sheet', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8');
  const rules = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(m => ({ sel: m[1].trim(), body: m[2] }));
  const sticky = rules.filter(r => /position:\s*sticky/.test(r.body));
  for (const state of ['is-playing', 'is-loading']) {
    const r = sticky.find(x => x.sel.split(',').map(s => s.trim()).includes(`.ew-reader > .ew-player.ew-clip.${state}`));
    assert.ok(r, `a sticky rule for .ew-reader > .ew-player.ew-clip.${state}: ${sticky.map(x => x.sel).join(' | ')}`);
    assert.match(r.body, /top:\s*0/, 'at the top of the screen');
    // VN1-R1-03: above the positioned transcript controls (the phone "Play
    // from" buttons), so a z-index of 0 or below does not hold.
    assert.match(r.body, /z-index:\s*[1-9]\d*\s*(;|$)/, 'above the transcript it scrolls over');
  }
  // Not sticky while idle: reading the page is not covered.
  assert.ok(!sticky.some(r => r.sel.split(',').map(s => s.trim()).some(s => /\.ew-player(\.ew-clip)?$/.test(s))), 'an idle player scrolls with the page');
});

/* VN1-R1-01: "Jump to the brand name" lands its line (and its focus ring)
   below the stuck player while the clip loads or plays; idle, unchanged.
   VN1-R2-01: by the page's scroll padding (any focus move) or the line's
   own margin, never both (that would land it twice as low). */
const stickySelectors = () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  return [...css.matchAll(/([^{}]+)\{([^{}]*position:\s*sticky[^{}]*)\}/g)].flatMap(m => m[1].split(',').map(s => s.trim()));
};
async function jumpWith(state, style = {}, rootStyle = {}) {
  const win = F.loadViews();
  await drain();
  const line = { tabIndex: 0, style, scrolled: null, focused: null, scrollIntoView(o) { this.scrolled = { ...o }; }, focus(o) { this.focused = { ...o }; } };
  const player = { getBoundingClientRect: () => ({ top: 0, bottom: 232.4, height: 232.4 }) };
  const stuck = state && stickySelectors().filter(s => s.endsWith('.' + state));
  win.matchMedia = () => ({ matches: true });
  // The player the sticky rule finds is stuck (a screen tall enough, VN1-R3-01).
  win.getComputedStyle = () => ({ position: 'sticky' });
  win.document.documentElement = { style: rootStyle };
  win.document.querySelector = sel => {
    if (sel === '.ew-said [data-segment="43"]') return line;
    // The player is found only through the rule that makes it sticky.
    if (stuck && stuck.length && stuck.every(s => sel.split(',').map(x => x.trim()).includes(s))) return player;
    return null;
  };
  await win.EWViews.handleAction({ dataset: { action: 'jump-words', segment: '43' } });
  return line;
}
// The focus ring is 2 px wide, 2 px out (tokens.css).
const RING = 4;
// How far below the top a block: 'start' scroll lands the line.
const px = v => parseFloat(v) || 0;
const landsAt = line => px(line.style.scrollMarginTop) + px(line.rootStyle.scrollPaddingTop);

test('VN1-R1-01 while the clip plays or loads, the jumped-to line lands below the stuck player, ring and all', async () => {
  for (const state of ['is-playing', 'is-loading']) {
    const rootStyle = {};
    const line = Object.assign(await jumpWith(state, {}, rootStyle), { rootStyle });
    assert.equal(line.scrolled.block, 'start', state);
    const at = landsAt(line);
    assert.ok(at >= 232.4 + RING, `${state}: lands ${at} px down, clear of the 232.4 px player and the ring`);
    assert.ok(at <= 232.4 + 24, `${state}: and close under it, not twice (margin ${line.style.scrollMarginTop}, page padding ${rootStyle.scrollPaddingTop})`);
    assert.deepEqual(line.focused, { preventScroll: true });
    assert.equal(line.tabIndex, -1);
  }
});

test('VN1-R1-01 idle: the jump is unchanged (top of the screen, no margin)', async () => {
  // Even after playback left the page a padding.
  const rootStyle = { scrollPaddingTop: '241px' };
  const line = Object.assign(await jumpWith(null, {}, rootStyle), { rootStyle });
  assert.deepEqual(line.scrolled, { block: 'start', behavior: 'auto' });
  assert.equal(landsAt(line), 0, `no margin or page padding when idle: ${JSON.stringify([line.style.scrollMarginTop, rootStyle.scrollPaddingTop])}`);
});

/* VN1-R2-01: while the clip loads or plays, any focus move (Tab, Shift+Tab)
   to a control under the stuck player scrolls it into view below it: the
   page's scroll padding is the player's height + 8 px. Idle, or once the
   page is left, there is none.
   VN1-R3-01: only while the player is actually stuck (a screen tall enough).
   VN1-R3-03: none on the new page after any render, the same page's too.
   VN1-R4-01: a resize (a phone turned) re-measures it. */
const sessionFetch = async url => {
  if (url === '/api/v1/customer/preferences') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  if (url === '/api/v1/session') return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  throw new Error('no network in unit test');
};
async function evidenceClip() {
  const win = F.loadAt('#/settings', { fetch: sessionFetch });
  const on = {}, winOn = {};
  win.addEventListener = (type, fn) => { (winOn[type] = winOn[type] || []).push(fn); };
  win.document.addEventListener = (type, fn) => { (on[type] = on[type] || []).push(fn); };
  win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await drain();
  win.EW.current.hosted = false;
  win.EW.header = () => {}; win.EW.toast = () => {};
  const root = { style: {} };
  win.document.documentElement = root;
  // The evidence page's player: found only through the rules that make it
  // sticky, for the state it is in, and only while its page is on screen.
  const cls = new Set();
  const box = { dataset: { clipId: 'ev' }, setAttribute() {}, removeAttribute() {}, querySelector: () => null,
    classList: { add: c => cls.add(c), remove: c => cls.delete(c), toggle: (c, v) => (v ? cls.add(c) : cls.delete(c)), contains: c => cls.has(c) },
    getBoundingClientRect: () => ({ top: 0, bottom: 232.4, height: 232.4 }) };
  const audio = { tagName: 'AUDIO', dataset: { clip: 'ev' }, paused: true, ended: false, currentTime: 0, readyState: 0, closest: () => box,
    play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } };
  const sticky = stickySelectors();
  // view.position: what the screen's CSS makes of the player the rule finds
  // ('static' where the screen is too short for it to stick).
  const view = { shown: true, position: 'sticky' };
  win.getComputedStyle = el => ({ position: el === box ? view.position : 'static' });
  let html = '';
  const main = { get innerHTML() { return html; }, set innerHTML(v) { html = v; view.shown = false; }, setAttribute() {}, focus() {}, querySelector: () => null, querySelectorAll: () => [] };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => {
    if (sel === '#main') return main;
    if (sel === '#modal') return modal;
    const parts = String(sel).split(',').map(s => s.trim());
    if (view.shown && parts.some(s => sticky.includes(s) && cls.has(s.slice(s.lastIndexOf('.') + 1)))) return box;
    return null;
  };
  win.document.querySelectorAll = sel => (sel === 'audio[data-clip]' ? [audio] : []);
  win.document.getElementById = id => (id === 'clip-ev' ? audio : null);
  const fire = (type, over = {}) => { Object.assign(audio, over); for (const fn of on[type] || []) fn({ type, target: audio }); };
  const resize = () => { for (const fn of winOn.resize || []) fn({ type: 'resize' }); };
  return { win, root, fire, view, winOn, resize };
}

test('VN1-R2-01 the page scroll padding is the stuck player\'s height + 8 px while its clip loads or plays; none when it stops', async () => {
  const p = await evidenceClip();
  assert.ok(!p.root.style.scrollPaddingTop, 'idle: none');
  p.fire('play', { paused: false });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'loading: ceil(232.4) + 8');
  p.fire('playing', { paused: false, readyState: 4 });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'playing');
  p.fire('pause', { paused: true });
  assert.ok(!p.root.style.scrollPaddingTop, `paused: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  p.fire('play', { paused: false });
  p.fire('playing', { paused: false, readyState: 4 });
  p.fire('ended', { paused: true, ended: true });
  assert.ok(!p.root.style.scrollPaddingTop, `ended: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
});

test('VN1-R2-01 leaving the evidence page while its clip plays leaves no scroll padding on the next page', async () => {
  const p = await evidenceClip();
  p.fire('play', { paused: false });
  p.fire('playing', { paused: false, readyState: 4 });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'playing');
  const api = F.overviewApi();
  p.win.EW.api = async (q, o) => { const out = api(q, o); if (out instanceof Error) throw out; return out; };
  p.win.location.hash = '#/overview';
  p.win.EW.current.route = '#/overview';
  await p.win.EWViews.render();
  await drain();
  assert.ok(!p.root.style.scrollPaddingTop, `Overview: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
});

/* VN1-R3-01: in phone landscape (844x390) the stuck player covered 94% of
   the screen. It sticks only where the screen is tall enough to leave at
   least 2 transcript lines and a focus ring below it: measured with the
   branch's CSS (logs/vis-nov1-fix3/measure-*.jsonl), player + 8 px + 2 lines
   + ring needs 434 px at 844-1440 wide, 568 px at 360-430 wide and 618 px at
   320 wide. Elsewhere it scrolls away, as before #14. */
const PLAYER_SELS = ['.ew-reader > .ew-player.ew-clip.is-loading', '.ew-reader > .ew-player.ew-clip.is-playing'];
test('VN1-R3-01 the evidence player sticks only on a screen tall enough for 2 lines below it (never in phone landscape)', () => {
  const css = fs.readFileSync(path.join(APP, 'evidence.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = [...css.matchAll(/@media\s*\(\s*min-height:\s*(\d+)px\s*\)\s*\{((?:[^{}]*\{[^{}]*\})*)[^{}]*\}/g)];
  const stickyIn = body => [...body.matchAll(/([^{}]+)\{([^{}]*position:\s*sticky[^{}]*)\}/g)].flatMap(m => m[1].split(',').map(x => x.trim()));
  const holding = blocks.filter(b => PLAYER_SELS.every(sel => stickyIn(b[2]).includes(sel)));
  assert.equal(holding.length, 1, `one min-height query holds the sticky player rule: ${blocks.map(b => b[0].slice(0, 60)).join(' | ')}`);
  const min = Number(holding[0][1]);
  assert.ok(min >= 618, `min-height ${min}: room for the tallest measured player (394 px at 320 wide) + 8 + 2 lines + ring`);
  assert.ok(min <= 844, `min-height ${min}: still sticks on a 390x844 phone held upright`);
  // Outside the query it never sticks.
  const rest = css.replace(holding[0][0], '');
  assert.deepEqual(stickyIn(rest).filter(sel => PLAYER_SELS.includes(sel)), [], 'no sticky player rule outside the height query');
});

test('VN1-R3-01 a player that is not stuck (a short screen) gives the page no scroll padding while its clip loads or plays', async () => {
  const p = await evidenceClip();
  p.view.position = 'static';
  p.fire('play', { paused: false });
  assert.ok(!p.root.style.scrollPaddingTop, `loading, not stuck: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  p.fire('playing', { paused: false, readyState: 4 });
  assert.ok(!p.root.style.scrollPaddingTop, `playing, not stuck: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  // Stuck (a tall screen), then not (turned to landscape): the next state
  // change clears what the stuck player set.
  p.view.position = 'sticky';
  p.fire('pause', { paused: true });
  p.fire('play', { paused: false });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'loading, stuck');
  p.view.position = 'static';
  p.fire('playing', { paused: false, readyState: 4 });
  assert.ok(!p.root.style.scrollPaddingTop, `playing, no longer stuck: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  // A jump re-measures the playing player directly: stuck, then not.
  const find = p.win.document.querySelector;
  const line = { tabIndex: 0, style: {}, scrollIntoView() {}, focus() {} };
  p.win.document.querySelector = sel => (sel === '.ew-said [data-segment="43"]' ? line : find(sel));
  p.win.matchMedia = () => ({ matches: true });
  p.view.position = 'sticky';
  await p.win.EWViews.handleAction({ dataset: { action: 'jump-words', segment: '43' } });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'jump, stuck');
  p.view.position = 'static';
  await p.win.EWViews.handleAction({ dataset: { action: 'jump-words', segment: '43' } });
  assert.ok(!p.root.style.scrollPaddingTop, `jump, no longer stuck: cleared (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
});

/* VN1-R3-03: a render of the page on screen (in place) replaces its player
   with a new, idle one; the old audio's pause never reaches the page. */
test('VN1-R3-03 re-rendering the page on screen while its clip plays leaves no scroll padding on the new, idle page', async () => {
  const p = await evidenceClip();
  p.fire('play', { paused: false });
  p.fire('playing', { paused: false, readyState: 4 });
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'playing');
  const api = F.overviewApi();
  p.win.EW.api = async (q, o) => { const out = api(q, o); if (out instanceof Error) throw out; return out; };
  p.win.location.hash = '#/overview';
  p.win.EW.current.route = '#/overview';
  p.win.EW.current.framePath = '#/overview'; // this page is the one on screen
  await p.win.EWViews.render();
  await drain();
  assert.equal(p.view.shown, false, 'the page was replaced');
  assert.ok(!p.root.style.scrollPaddingTop, `in-place render: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
});

/* VN1-R4-01: the media query switches the player's stickiness when the
   phone turns, but the padding was measured only on a clip state change,
   a jump or a render: turned while playing, the portrait padding stayed
   on the landscape screen (focus and the "now" line at or below its
   bottom edge), and a landscape start left none under the player once
   upright. A resize re-measures, from one listener for the page. */
test('VN1-R4-01 a resize while the clip plays re-measures the player: padded once it sticks, cleared once it no longer does; one listener, never per render', async () => {
  const p = await evidenceClip();
  assert.equal((p.winOn.resize || []).length, 1, 'one resize listener for the page');
  p.view.position = 'static'; // played in landscape: not stuck
  p.fire('play', { paused: false });
  p.fire('playing', { paused: false, readyState: 4 });
  assert.ok(!p.root.style.scrollPaddingTop, `playing, not stuck: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  p.view.position = 'sticky'; // turned upright: stuck
  p.resize();
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'resized to a sticky height: ceil(232.4) + 8');
  p.view.position = 'static'; // turned back to landscape
  p.resize();
  assert.ok(!p.root.style.scrollPaddingTop, `resized below the sticky height: cleared (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
  p.view.position = 'sticky';
  p.resize();
  assert.equal(p.root.style.scrollPaddingTop, '241px', 'upright again');
  // Renders add no listener, and on the next page a resize sets none.
  const api = F.overviewApi();
  p.win.EW.api = async (q, o) => { const out = api(q, o); if (out instanceof Error) throw out; return out; };
  p.win.location.hash = '#/overview';
  p.win.EW.current.route = '#/overview';
  await p.win.EWViews.render();
  await p.win.EWViews.render();
  await drain();
  assert.equal(p.winOn.resize.length, 1, `still one resize listener after renders (${p.winOn.resize.length})`);
  p.resize();
  assert.ok(!p.root.style.scrollPaddingTop, `Overview, resized: none (${JSON.stringify(p.root.style.scrollPaddingTop)})`);
});

/* -- #16 loose words are not a phrase ------------------------------------ */
const sREF = n => 'sr1:' + String(n).padEnd(32, 'a');
const adItem = n => ({ ref: sREF(n), record_kind: 'evidence', status: 'finding', review: null, show_title: 'Show A',
  ep_title: 'Episode ' + n, episode_id: 'e' + n, pub_date: '2026-10-09', audio: 'unavailable', quotation_span: [60, 120],
  show: { show_identity_id: 'cs-a' }, read: { brand: { key: 'quince', display: 'Quince', confirmed: false }, display_span: [60, 120], has_clip: false, needs_review: false },
  attribution: [{ brand: 'quince', basis: 'suggested', channel: 'spoken' }], ad_break_id: 'brk:' + n, match_basis: 'passage' });
function searchApi(q, count) {
  const items = Array.from({ length: count }, (_, i) => adItem(i + 1));
  const breaks = items.map(it => ({ id: it.ad_break_id, episode_id: it.episode_id, transcript_version_id: 7, on_current_transcript: true,
    span: it.quotation_span, read_count: 1, matching_read_count: 1, refs: [it.ref], pub_date: '2026-10-09', group_id: it.ad_break_id,
    group_ad_count: 1, brands: [{ key: 'quince', display: 'Quince', confirmed: false }] }));
  const bundle = { query: { version: 2, mode: 'ads', q, sort: 'newest', page: 1 }, revision: 'rev-1', selector: null,
    coverage: { episodes_in_scope: 62 }, result: { items, total: count, dated_total: count, undated_total: 0, total_relation: 'exact', per_page: 10,
      ad_breaks: breaks, break_total: count, break_dated_total: count, break_undated_total: 0 }, summary: { week: [] } };
  return p => {
    if (p === '/customer/research-query') return bundle;
    if (p === '/customer/brands') return { brands: [{ key: 'quince', display: 'Quince', ad_breaks: 92, shows: 37, last_heard: '2026-10-19' }] };
    if (p === '/customer/shows') return { shows: [] };
    if (p.startsWith('/customer/filter-values?') || p.startsWith('/customer/saved-items')) return { items: [] };
    return new Error('unexpected ' + p);
  };
}
const notBrand = async (q, count) => {
  const { html } = await F.render(`#/search?mode=ads&q=${encodeURIComponent(q)}`, searchApi(q, count));
  return F.text((html.match(/<p class="ew-brand-jump ew-not-brand">[\s\S]*?<\/p>/) || [''])[0]).replace(/\s+/g, ' ').trim();
};

test('#16 several loose words: the ads have the words, in any order, never "says" the phrase', async () => {
  assert.equal(await notBrand('brand not identified', 1),
    'No brand called “brand not identified” was heard in ads in this collection. This ad has all of these words, in any order, in its words or its episode title. Browse brands · What we cover');
  assert.equal(await notBrand('brand not identified', 3),
    'No brand called “brand not identified” was heard in ads in this collection. These 3 ads have all of these words, in any order, in their words or their episode title. Browse brands · What we cover');
  assert.equal(await notBrand('use code', 1),
    'No brand called “use code” was heard in ads in this collection. This ad has both words, in any order, in its words or its episode title. Browse brands · What we cover');
});

test('#16 one word is still said as the ad says it', async () => {
  assert.equal(await notBrand('nike', 1),
    'No brand called “nike” was heard in ads in this collection. This ad says “nike” in its words or its episode title. Browse brands · What we cover');
  assert.equal(await notBrand('nike', 2),
    'No brand called “nike” was heard in ads in this collection. These 2 ads say “nike” in their words or their episode title. Browse brands · What we cover');
});

/* -- #23 the evidence page's section follows where the person came from --- */
async function evidenceSections(origin) {
  const route = '#/evidence/' + encodeURIComponent(sREF(1));
  const win = F.loadAt('#/settings', { fetch: async url => {
    if (url === '/api/v1/session') {
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false, csrf_token: 's'.repeat(32),
        workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
    }
    throw new Error('no network in unit test');
  } });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await drain();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  const sections = [];
  win.EW.header = s => sections.push(s);
  win.EW.toast = () => {};
  // The record read fails: the page still says where the person is.
  win.EW.api = async () => { throw new Error('no record in this test'); };
  if (origin) win.EW.current.back.set(route, origin);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await drain();
  return sections;
}

test('#23 evidence opened from Overview highlights Overview, never Search', async () => {
  const s = await evidenceSections('#/overview');
  assert.ok(s.length, 'the header is drawn');
  assert.deepEqual([...new Set(s)], ['Overview']);
});

test('#23 evidence opened from Search (or with no origin) keeps Search', async () => {
  assert.deepEqual([...new Set(await evidenceSections('#/search?mode=ads&q=quince'))], ['Search']);
  assert.deepEqual([...new Set(await evidenceSections(null))], ['Search']);
  // Another evidence page as the origin says nothing about the section: Search.
  assert.deepEqual([...new Set(await evidenceSections('#/evidence/' + encodeURIComponent(sREF(2))))], ['Search']);
});

test('#23 evidence opened from a Brand, an episode, an alert or Saved shows that section', async () => {
  assert.deepEqual([...new Set(await evidenceSections('#/brand/quince'))], ['Brands']);
  assert.deepEqual([...new Set(await evidenceSections('#/episode/e1?at=60'))], ['Shows']);
  assert.deepEqual([...new Set(await evidenceSections('#/alert/3'))], ['Alerts']);
  assert.deepEqual([...new Set(await evidenceSections('#/saved'))], ['Saved']);
  // VN1-R1-02: real origins carry a query (a Saved tab); the section is the path's.
  assert.deepEqual([...new Set(await evidenceSections('#/saved?tab=excerpts'))], ['Saved']);
  assert.deepEqual([...new Set(await evidenceSections('#/saved?tab=queries'))], ['Saved']);
});
