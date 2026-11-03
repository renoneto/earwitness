/* REDESIGN-BUILD phase A: the redesign stylesheets agree and never restyle
   today's pages.
   - app/app-b2.css holds the --b2-* tokens, the s-* rules the page files
     share (Landing keeps them byte-identical; `.s-noclip` is pinned here)
     and the element rules, scoped to body.ew-b2.
   - A page file never repeats a base selector, and no selector is defined
     in two page files: every stylesheet loads on every page, so a clash
     (Search's and Ad's .s-panel__time, .s-skel__panel) must be scoped under
     its page root.
   - No bare element rule (*, html, body, a, button) and nothing off-origin.
   Run: node --test tests/frontend_b2_css.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const APP = path.join(__dirname, '..', 'app');
const PAGES = ['search', 'ad', 'overview', 'brand', 'signin'];
const read = f => fs.readFileSync(path.join(APP, f), 'utf8');
const strip = css => css.replace(/\/\*[\s\S]*?\*\//g, '');
// Top-level rules only (a page file may add @media blocks; their rules are scoped by the query).
function rules(css) {
  const out = [];
  let depth = 0, start = 0, sel = '';
  const s = strip(css);
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (c === '{') { if (depth === 0) { sel = s.slice(start, i).trim(); start = i + 1; } depth++; }
    else if (c === '}') {
      depth--;
      if (depth === 0) { if (!sel.startsWith('@')) out.push({ sel: sel.replace(/\s+/g, ' '), body: s.slice(start, i).replace(/\s+/g, ' ').trim() }); start = i + 1; }
    }
  }
  return out;
}
// A selector list split on its top-level commas (never inside :is(...) or [...]).
function splitList(sel) {
  const out = []; let depth = 0, cur = '';
  for (const c of sel) {
    if (c === '(' || c === '[') depth++;
    if (c === ')' || c === ']') depth--;
    if (c === ',' && depth === 0) { out.push(cur.trim()); cur = ''; } else cur += c;
  }
  return [...out, cur.trim()].filter(Boolean);
}
const selectors = css => rules(css).flatMap(r => splitList(r.sel));

const B2_VARS = ['--b2-ink-on-dark-dim', '--b2-dark-hover', '--b2-export-hover', '--b2-play-hover', '--b2-chip-x-hover', '--b2-bar-dim',
  '--b2-bar-dim-hover', '--b2-tint-0', '--b2-tint-1', '--b2-tint-2', '--b2-tint-3', '--b2-tint-4', '--b2-tint-5', '--b2-card-lift',
  '--b2-page-clear', '--b2-dark-band', '--b2-ink-on-dark', '--b2-card-on-dark', '--b2-card-on-dark-m'];

test('base: app-b2.css defines the --b2 tokens once, in :root', () => {
  const css = strip(read('app-b2.css'));
  for (const v of B2_VARS) assert.equal(css.split(v + ':').length - 1, 1, v);
  const root = rules(read('app-b2.css')).find(r => r.sel === ':root');
  assert.ok(root, ':root block');
  for (const v of B2_VARS) assert.ok(root.body.includes(v + ':'), v);
});

test('base: the shared no-clip rule is Landing\'s byte-identical U2 rule', () => {
  const r = rules(read('app-b2.css')).find(x => x.sel === '.s-noclip');
  assert.ok(r, '.s-noclip');
  assert.equal(r.body, 'font: 400 13px/40px var(--ew-sans); color: var(--ew-text-3); display: inline-block; height: 40px; white-space: nowrap;');
});

test('base: the phone Menu is 44 px (Search, Overview, Brand; Ad R14)', () => {
  const r = rules(read('app-b2.css')).find(x => x.sel === '.s-nav__menu');
  assert.ok(r, '.s-nav__menu');
  assert.match(r.body, /height: 44px/);
  assert.match(r.body, /min-height: 44px/);
});

test('base: element rules are scoped to body.ew-b2, so today\'s pages keep styles.css', () => {
  for (const f of ['app-b2.css', ...PAGES.map(p => `${p}-b2.css`)]) {
    const bare = selectors(read(f)).filter(s => !/^body\.ew-b2(?:--[a-z]+)?(?=$|[\s:.[>+~])/.test(s)
      && /^(\*|html|body|a|button|input|h[1-6]|p|ul|li)(?=$|[\s:.[>+~])/.test(s));
    assert.deepEqual(bare, [], f);
  }
  const sels = selectors(read('app-b2.css'));
  assert.ok(sels.includes('body.ew-b2'), 'body.ew-b2 rule');
  assert.ok(sels.some(s => /^body\.ew-b2 \*$/.test(s)), 'box sizing on b2 pages only');
});

test('page files: none repeats a base selector, and no selector is defined in two page files', () => {
  const base = new Set(selectors(read('app-b2.css')));
  const owner = new Map();
  const repeats = [], clashes = [];
  for (const p of PAGES) {
    const f = `${p}-b2.css`;
    assert.ok(fs.existsSync(path.join(APP, f)), f);
    for (const s of new Set(selectors(read(f)))) {
      if (base.has(s)) repeats.push(`${f}: ${s}`);
      if (owner.has(s)) clashes.push(`${s} in ${owner.get(s)} and ${f}`);
      else owner.set(s, f);
    }
  }
  assert.deepEqual(repeats, [], 'base selectors repeated in a page file');
  assert.deepEqual(clashes, [], 'scope a clash under its page root');
});

test('all redesign stylesheets: comments close where they should, same-origin only, no @import', () => {
  for (const f of ['app-b2.css', ...PAGES.map(p => `${p}-b2.css`)]) {
    const css = strip(read(f));
    assert.doesNotMatch(css, /\*\/|\/\*/, `${f}: a comment ends early (a "*/" inside it) or never ends`);
    assert.doesNotMatch(css, /@import/, f);
    assert.doesNotMatch(css, /url\(\s*['"]?(?:https?:)?\/\//i, f);
  }
});

test('page modules and stylesheets exist as files the base loads (stubs until each page is built)', () => {
  for (const p of PAGES) {
    assert.ok(fs.existsSync(path.join(APP, `page-${p}.js`)), `page-${p}.js`);
    assert.ok(fs.existsSync(path.join(APP, `${p}-b2.css`)), `${p}-b2.css`);
  }
  assert.ok(fs.existsSync(path.join(APP, 'b2.js')));
});
