/* PUB-48b row 55 (PUBLIC-WORDING, LEDGER-LEAD3-PUB-4): on a phone the doc-nav
   row scrolls so the current page's item is fully in view AND the row's left
   edge lands on the FIRST item edge that keeps it whole, so no item is cut at
   the left ("s' access" on Privacy at 390 px, PUB48-S1-01). It measures after
   the web fonts load, adds just enough end padding near the row end, and
   places the row again when the width changes (wider or narrower; never on a
   height-only resize, and only once after a burst). public.js runs against a fake row. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'app', 'public', 'public.js'), 'utf8');

// widths: item widths in px, in order (can be swapped later, as a font swap does);
// current: index of the current page; fonts: optional { ready: Promise }.
function run({ widths, current, clientWidth, left = 16, fonts }) {
  const state = { widths: widths.slice(), clientWidth };
  const lefts = () => { const out = []; state.widths.reduce((x, w) => { out.push(x); return x + w; }, 0); return out; };
  const base = () => state.widths.reduce((a, b) => a + b, 0);
  // Like a browser, scrollLeft clamps to [0, scrollWidth - clientWidth], also
  // when the row widens or loses padding; end padding set by public.js widens
  // the scrollable row.
  let pos = 0, pad = 0;
  const clamp = (v) => Math.max(0, Math.min(base() + pad - state.clientWidth, v));
  const row = {
    get clientWidth() { return state.clientWidth; },
    get scrollWidth() { return base() + pad; },
    get scrollLeft() { pos = clamp(pos); return pos; },
    set scrollLeft(v) { pos = clamp(v); },
    style: { set paddingRight(v) { pad = v === '' ? 0 : parseFloat(v); }, get paddingRight() { return pad ? pad + 'px' : ''; } },
  };
  const rect = (i) => { const x = lefts()[i]; return { left: left + x - row.scrollLeft, right: left + x + state.widths[i] - row.scrollLeft }; };
  row.getBoundingClientRect = () => ({ left, right: left + state.clientWidth });
  row.children = state.widths.map((_, i) => ({ getBoundingClientRect: () => rect(i) }));
  const anchor = { closest: (s) => (s === 'ul' ? row : null), getBoundingClientRect: () => rect(current) };
  const document = { querySelector: (s) => (s.includes('.doc-nav a') ? anchor : null), addEventListener() {} };
  if (fonts) document.fonts = fonts;
  const listeners = {};
  const window = { addEventListener: (t, f) => { listeners[t] = f; } };
  // Timers queue until flush(), so the resize debounce can be tested.
  const timers = new Map(); let nextId = 1;
  const setTimeout = (f) => { const id = nextId++; timers.set(id, f); return id; };
  const clearTimeout = (id) => { timers.delete(id); };
  const flush = () => { const fs = [...timers.values()]; timers.clear(); fs.forEach((f) => f()); };
  vm.runInNewContext(SRC, { document, window, setTimeout, clearTimeout });
  // resize(w) fires the event and runs the timers; resize(w, false) leaves them queued.
  const resize = (w, done = true) => { state.clientWidth = w; if (listeners.resize) listeners.resize(); if (done) flush(); };
  return { row, state, lefts, getPad: () => pad, resize, flush, pending: () => timers.size };
}

// What row 55 requires: the first item edge that keeps the current item whole,
// never past its own left edge; end padding only as much as that edge needs.
function expected(widths, current, clientWidth) {
  const lefts = []; widths.reduce((x, w) => { lefts.push(x); return x + w; }, 0);
  const total = widths.reduce((a, b) => a + b, 0);
  const right = lefts[current] + widths[current];
  if (total <= clientWidth || right <= clientWidth) return { scroll: 0, pad: 0 };
  const edge = lefts.find((l) => l >= right - clientWidth);
  const scroll = Math.min(edge, lefts[current]);
  return { scroll, pad: Math.max(0, scroll - (total - clientWidth)) };
}

function check(r, current) {
  const want = expected(r.state.widths, current, r.state.clientWidth);
  assert.equal(r.row.scrollLeft, want.scroll, `scrollLeft is the first fitting item edge (current ${current}, width ${r.state.clientWidth})`);
  assert.equal(r.getPad(), want.pad, `end padding is exactly what that edge needs (current ${current})`);
  const l = r.lefts()[current];
  assert.ok(l - r.row.scrollLeft >= 0 && l + r.state.widths[current] - r.row.scrollLeft <= r.state.clientWidth, 'current item whole');
}

// Measured headless (Chromium, IBM Plex loaded) on Privacy:
// Judges' access, Coverage, Methodology, Privacy, Terms, Pricing.
const ITEMS = [119, 82, 106, 72, 63, 67];
const SIZES = [358, 328, 398]; // row width at 390, 360 and 430 px

test('Privacy at 390 (the reported case): scrolls to the Coverage edge, Privacy whole', () => {
  const r = run({ widths: ITEMS, current: 3, clientWidth: 358 });
  assert.equal(r.row.scrollLeft, 119);
  check(r, 3);
});

test('every doc page at 390, 360 and 430: first fitting edge and exact end padding', () => {
  // Pricing (index 5) has no doc-nav page of its own, so current is 0-4.
  for (const w of SIZES) for (let current = 0; current < 5; current += 1) check(run({ widths: ITEMS, current, clientWidth: w }), current);
});

test('Terms at 430 needs end padding; the padding is exactly the shortfall', () => {
  const r = run({ widths: ITEMS, current: 4, clientWidth: 398 });
  assert.equal(r.row.scrollLeft, 119);
  assert.equal(r.getPad(), 8, 'row 509 px, width 398: last scroll 111, the Coverage edge is at 119');
});

test('waits for the web fonts: nothing moves until fonts.ready, then it uses the new widths', async () => {
  let resolve;
  const fonts = { ready: new Promise((res) => { resolve = res; }) };
  // Fallback widths where Privacy (right edge 410) would need a scroll too.
  const r = run({ widths: [130, 90, 110, 80, 70, 70], current: 3, clientWidth: 358, fonts });
  assert.equal(r.row.scrollLeft, 0, 'no scroll before the fonts load');
  r.state.widths = ITEMS.slice(); // the font swap changes the widths
  resolve();
  await fonts.ready; await null;
  check(r, 3);
});

test('a width change places the row again and clears padding it no longer needs', () => {
  const r = run({ widths: ITEMS, current: 4, clientWidth: 398 });
  assert.equal(r.getPad(), 8);
  r.resize(1200); // the row now fits (desktop)
  assert.equal(r.getPad(), 0, 'padding cleared');
  r.resize(328); // back to a phone
  check(r, 4);
});

test('widening while still a phone moves the row to the right edge, not where the browser clamped it', () => {
  // Terms 390 -> 430: the browser clamps 119 to 111; the right edge is 119 with 8 px padding.
  const a = run({ widths: ITEMS, current: 4, clientWidth: 358 });
  check(a, 4);
  a.resize(398);
  check(a, 4);
  a.resize(418); // 430 -> 450
  check(a, 4);
  // Methodology 320 -> 430: once the item fits from the start, the row goes back to 0.
  const b = run({ widths: ITEMS, current: 2, clientWidth: 288 });
  check(b, 2);
  b.resize(398);
  assert.equal(b.row.scrollLeft, 0);
  check(b, 2);
});

test('a height-only resize changes nothing, and keeps where the reader scrolled the row', () => {
  const r = run({ widths: ITEMS, current: 4, clientWidth: 398 });
  assert.equal(r.getPad(), 8);
  r.row.scrollLeft = 0; // the reader scrolls back to the start
  r.resize(398); // the phone's address bar hides
  assert.equal(r.getPad(), 8, 'padding kept');
  assert.equal(r.row.scrollLeft, 0, 'row not moved back');
});

test('a burst of resizes places the row once, after the last one', () => {
  const r = run({ widths: ITEMS, current: 4, clientWidth: 398 });
  r.resize(1200, false);
  r.resize(800, false);
  assert.equal(r.getPad(), 8, 'nothing moves until the timer fires');
  assert.equal(r.pending(), 1, 'one queued placement');
  r.flush();
  assert.equal(r.getPad(), 0);
});

test('back to the first width after a resize places the row again', () => {
  const r = run({ widths: ITEMS, current: 4, clientWidth: 398 });
  r.resize(1200);
  assert.equal(r.getPad(), 0);
  r.resize(398);
  check(r, 4);
});

test('no scroll when the row fits', () => {
  const r = run({ widths: [80, 80, 80], current: 2, clientWidth: 358 });
  assert.equal(r.row.scrollLeft, 0);
  assert.equal(r.getPad(), 0);
});
