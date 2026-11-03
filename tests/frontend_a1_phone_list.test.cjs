/* A1 (Sol POLISH-1): the owner/editor List view on phones.
   At the phone breakpoint the selectable List row uses the viewer's stacked
   composition with a dedicated 40 px checkbox column in front: brand,
   Show/Episode and the time/date/status line stack in the central flexible
   column. No Show/Episode track may collapse to 0. Desktop and the viewer's
   layout stay as they were.
   The test resolves the cascade of search.css for a given viewport width
   (source order and specificity, @media min/max-width), so a later
   unconditional rule that overrides the phone rule is caught.
   Run: node --test tests/frontend_a1_phone_list.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP = path.join(__dirname, '..', 'app');
const CSS = fs.readFileSync(path.join(APP, 'search.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const TOKENS = fs.readFileSync(path.join(APP, 'tokens.css'), 'utf8');

// Flat rule list: { media, selectors, decls, order }.
function parse(css, media = null, out = []) {
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf('{', i);
    if (open < 0) break;
    const prelude = css.slice(i, open).trim();
    let depth = 1, j = open + 1;
    while (j < css.length && depth) { if (css[j] === '{') depth++; else if (css[j] === '}') depth--; j++; }
    const body = css.slice(open + 1, j - 1);
    if (prelude.startsWith('@media')) parse(body, prelude.slice(6).trim(), out);
    else if (!prelude.startsWith('@')) {
      const decls = {};
      for (const d of body.split(';')) {
        const k = d.indexOf(':');
        if (k > 0) decls[d.slice(0, k).trim()] = d.slice(k + 1).trim().replace(/\s+/g, ' ');
      }
      out.push({ media, selectors: prelude.split(',').map(s => s.trim()), decls, order: out.length });
    }
    i = j;
  }
  return out;
}
const RULES = parse(CSS);

// Screen media at a viewport width; pointer: fine (the headless check).
function mediaMatches(media, width) {
  if (media == null) return true;
  return media.split(',').some(part => {
    if (/\bprint\b/.test(part)) return false;
    const feats = [...part.matchAll(/\(([^)]*)\)/g)].map(m => m[1].split(':').map(s => s.trim()));
    return feats.every(([f, v]) => f === 'max-width' ? width <= parseFloat(v)
      : f === 'min-width' ? width >= parseFloat(v)
      : f === 'pointer' ? v === 'fine' : false);
  });
}
// Selectors of the form ".a .b.c .ew-row" (descendant combinators, classes only).
function matches(selector, chain) {
  const parts = selector.split(/\s+/);
  if (!parts.every(p => /^(\.[\w-]+)+$/.test(p))) return false;
  const want = parts.map(p => p.slice(1).split('.'));
  const self = want.pop();
  if (!self.every(c => chain[chain.length - 1].includes(c))) return false;
  let k = chain.length - 2;
  for (let w = want.length - 1; w >= 0; w--) {
    while (k >= 0 && !want[w].every(c => chain[k].includes(c))) k--;
    if (k < 0) return false;
    k--;
  }
  return true;
}
const specificity = s => (s.match(/\.[\w-]+/g) || []).length;
function computed(chain, width, prop) {
  let best = null;
  for (const r of RULES) {
    if (!(prop in r.decls) || !mediaMatches(r.media, width)) continue;
    for (const s of r.selectors) {
      if (!matches(s, chain)) continue;
      const sp = specificity(s);
      if (!best || sp > best.sp || (sp === best.sp && r.order >= best.order)) best = { sp, order: r.order, value: r.decls[prop] };
    }
  }
  return best && best.value;
}
const vars = Object.fromEntries([...TOKENS.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
const resolve = v => v.replace(/var\((--[\w-]+)\)/g, (_, n) => vars[n] || _);
// Split a track list on top-level spaces: "40px minmax(0, 1fr) 40px" -> 3 tracks.
function tracks(v) {
  const out = []; let depth = 0, cur = '';
  for (const ch of resolve(v)) {
    if (ch === '(') depth++; if (ch === ')') depth--;
    if (ch === ' ' && !depth) { if (cur) out.push(cur); cur = ''; } else cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}
const areas = v => [...v.matchAll(/"([^"]*)"/g)].map(m => m[1].trim().split(/\s+/));

const OWNER_ROW = [['ew-rows-wrap'], ['ew-rows'], ['ew-row']];
const VIEWER_ROW = [['ew-rows-wrap', 'ew-no-select'], ['ew-rows'], ['ew-row']];
const OWNER_HEAD = [['ew-rows-wrap'], ['ew-rows-head']];
const grid = (chain, w) => ({ cols: tracks(computed(chain, w, 'grid-template-columns')), areas: areas(computed(chain, w, 'grid-template-areas')) });

test('A1 phone: the selectable List row stacks behind a 40 px checkbox column', () => {
  for (const w of [390, 600, 899]) {
    const { cols, areas: rows } = grid(OWNER_ROW, w);
    for (const r of rows) assert.equal(r.length, cols.length, `${w}px: every area row has one name per track (${cols.join(' | ')})`);
    assert.deepEqual(cols, ['40px', '40px', 'minmax(0, 1fr)', '40px'], `${w}px tracks`);
    assert.deepEqual(rows, [['check', 'play', 'brand', 'open'], ['check', 'play', 'where', 'open'], ['check', 'play', 'meta', 'open']], `${w}px areas`);
  }
});

test('A1 phone: Show/Episode sits in the flexible central column, never a zero-width track', () => {
  for (const w of [390, 899]) {
    const { cols, areas: rows } = grid(OWNER_ROW, w);
    for (const name of ['where', 'meta', 'brand']) {
      const at = rows.map(r => r.indexOf(name)).filter(x => x >= 0);
      assert.ok(at.length, `${w}px: ${name} has an area`);
      for (const c of at) assert.match(cols[c] || '', /fr\)?$/, `${w}px: ${name} track is flexible, got ${cols[c]}`);
    }
    assert.ok(cols.every(c => !/^0(px)?$/.test(c)), `${w}px: no 0 track`);
  }
});

test('A1 phone: "Select all on this page" stays a head row above the list', () => {
  assert.equal(computed(OWNER_HEAD, 390, 'display'), 'flex');
});

test('A1 desktop and viewer layouts do not change', () => {
  assert.deepEqual(grid(OWNER_ROW, 1440).cols, tracks('40px 40px 132px minmax(110px, 1fr) minmax(0, 1.1fr) 68px 116px 40px'));
  assert.deepEqual(grid(OWNER_ROW, 1440).areas, [['check', 'play', 'time', 'brand', 'where', 'date', 'status', 'open']]);
  assert.deepEqual(grid(OWNER_ROW, 1024).cols, tracks('40px 40px 132px minmax(110px, 1fr) minmax(0, 1.1fr) 68px 116px 40px'));
  assert.deepEqual(grid(OWNER_ROW, 900).cols, tracks('40px 40px 132px minmax(110px, 1fr) minmax(0, 1.1fr) 68px 116px 40px'));
  assert.deepEqual(grid(VIEWER_ROW, 1440).cols, tracks('40px 132px minmax(110px, 1fr) minmax(0, 1.1fr) 68px 116px 40px'));
  assert.deepEqual(grid(VIEWER_ROW, 1440).areas, [['play', 'time', 'brand', 'where', 'date', 'status', 'open']]);
  assert.deepEqual(grid(VIEWER_ROW, 390).cols, ['40px', 'minmax(0, 1fr)', '40px']);
  assert.deepEqual(grid(VIEWER_ROW, 390).areas, [['play', 'brand', 'open'], ['play', 'where', 'open'], ['play', 'meta', 'open']]);
});
