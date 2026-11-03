/* JE-WORDS: the judge's-eye public lines (UX WORDS.md D46, D47, D49, D51, D52;
   lead JE-WORDS 2026-11-01). Each je_public pin in
   earwitness/app/public_rows/public_rows.json is on its page once, and its old
   line is gone. Run: node --test tests/frontend_public_je.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PUB = path.join(__dirname, '..', 'app', 'public');
const read = name => fs.readFileSync(path.join(PUB, name), 'utf8');
const count = (hay, needle) => hay.split(needle).length - 1;
const ROWS = require(path.join(__dirname, '..', 'earwitness', 'app', 'public_rows', 'public_rows.json')).je_public;

test('je pins: each new line once on its page, each old line gone', () => {
  assert.equal(ROWS.length, 8);
  for (const r of ROWS) {
    const html = read(r.file);
    assert.equal(count(html, r.new), 1, `${r.row} new (${r.file})`);
    assert.equal(count(html, r.old), 0, `${r.row} old (${r.file})`);
  }
});

test('no public page still promises that every unsupported claim is removed (D47)', () => {
  for (const name of fs.readdirSync(PUB).filter(f => f.endsWith('.html'))) {
    assert.doesNotMatch(read(name), /removes any claim/, name);
  }
});

test('methodology: Compare no longer speaks of passages or wording (D51)', () => {
  const html = read('methodology.html');
  assert.doesNotMatch(html, /Different wording does not prove/);
  assert.doesNotMatch(html, /Put two passages/);
});

test('home tour: the shots are the re-shot screens; no alt text says "oldest first" (review je-r1 F2)', () => {
  const html = read('index.html');
  assert.doesNotMatch(html, /oldest first/i);
  for (const name of ['shot-search.jpg', 'shot-evidence.jpg']) {
    assert.ok(ROWS.some(r => r.new.startsWith(`<img src="/public/img/${name}" alt="`)), `${name} alt pinned`);
  }
});

test('home count (LANDING-COUNT): both lines give the same episodes, shows and "as of" date, in the dated form', () => {
  const html = read('index.html');
  const NUM = String.raw`\d{1,3}(?:,\d{3})*`;
  const MON = String.raw`(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}`;
  const lede = html.match(new RegExp(String.raw`any phrase in (${NUM}) episodes from (${NUM}) shows, as of (${MON})\.`, 'g')) || [];
  const section = html.match(new RegExp(String.raw`<p class="section-lede">(${NUM}) episodes from (${NUM}) shows, as of (${MON})\. EarWitness covers`, 'g')) || [];
  assert.equal(lede.length, 1, 'lede in the dated form, once');
  assert.equal(section.length, 1, 'Shows section in the dated form, once');
  const parts = s => s.match(new RegExp(`(${NUM}) episodes from (${NUM}) shows[^]*?(${MON})`)).slice(1);
  assert.deepEqual(parts(lede[0]), parts(section[0]), 'same count and date in both lines');
  assert.doesNotMatch(html, /1,500 episodes from 796 shows/, 'the old count is gone');
  assert.doesNotMatch(html, /shows we transcribe/, 'Review 11-02: the lede ends "..., as of {date}."');
  // No other undated collection count on the page.
  assert.equal((html.match(/episodes from \d/g) || []).length, 2);
});
