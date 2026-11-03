/* POS-1: Reno's positioning words (approved 2026-10-30 via Review and the
   lead; UX SUMMARY.md recommended set) on the public pages.
   1. Each positioning_public pin in earwitness/app/public_rows/public_rows.json
      is on its page once, and its old line is gone.
   2. Plan A keeps "Ad intelligence" and its no-transcript line (lead: a Plan A
      line may say "words in ads" and no more).
   The icon tags are pinned in tests/frontend_public_icons.test.cjs.
   Run: node --test tests/frontend_public_positioning.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PUB = path.join(__dirname, '..', 'app', 'public');
const read = name => fs.readFileSync(path.join(PUB, name), 'utf8');
const count = (hay, needle) => hay.split(needle).length - 1;
const ROWS = require(path.join(__dirname, '..', 'earwitness', 'app', 'public_rows', 'public_rows.json')).positioning_public;

test('positioning pins: each new line once on its page, each old line gone', () => {
  assert.equal(ROWS.length, 10);
  for (const r of ROWS) {
    const html = read(r.file);
    assert.equal(count(html, r.new), 1, `${r.row} new (${r.file})`);
    if (r.old) assert.equal(count(html, r.old), 0, `${r.row} old (${r.file})`);
  }
});

test('home: the new compare-and-follow step sits in the tour, before the campaign check', () => {
  const html = read('index.html');
  const tour = html.slice(html.indexOf('<ol class="steps">'), html.indexOf('</ol>', html.indexOf('<ol class="steps">')));
  const titles = [...tour.matchAll(/<h3>([^<]+)<\/h3>/g)].map(m => m[1]);
  assert.deepEqual(titles, ['Search any brand or phrase', 'Open the moment', 'Compare and follow', 'Check what you bought', 'Ask a question']);
});

test('Judges\u2019 access: "Where to start" opens with the "Try these first" line, before the pilot limits', () => {
  const html = read('access.html');
  const at = html.indexOf('<h2>Where to start</h2>\n<p>Try these first: ');
  assert.ok(at > html.indexOf('<h2>Sign in with your login</h2>'), 'after sign-in');
  assert.ok(at < html.indexOf("<h2>What the judges' pilot does not include</h2>"), 'before the pilot limits');
  assert.equal(count(html, '<h2>Where to start</h2>'), 1);
});

test('Plan A keeps its approved lines; only Plan B names full transcripts', () => {
  const html = read('pricing.html');
  const a = html.slice(html.indexOf('id="plan-a"'), html.indexOf('id="plan-b"'));
  const b = html.slice(html.indexOf('id="plan-b"'));
  assert.match(a, /<h2 id="plan-a-title">Ad intelligence<\/h2>\n<p class="plan-key">No full transcripts and no transcript search\.<\/p>/);
  assert.doesNotMatch(a, /any word|any phrase|mentions/i);
  assert.match(b, /<p class="plan-key">Ads, plus full transcripts: search any word or phrase\.<\/p>/);
});
