/* PUB-48: approved public wording on Methodology, Coverage, Home, Pricing,
   Access and Terms (PUBLIC-WORDING rows 2-20, 22, 24-28, 48-51; row 21 is
   superseded by row 51 and row 23 by row 49; LEDGER-LEAD3-NEXT-3, QW7-1,
   QW7-2, PUB-1; row 22 amended "final report", PUB-1 (b)). The exact bytes
   are the methodology_public pins in earwitness/app/public_rows/public_rows.json:
   each row's new line is on its page once and its old line is gone.
   Row 26 moves the Plan A card above Plan B with no word change. Row 27
   renames the Access link "Judges' access" on every public page. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PUBLIC = path.join(__dirname, '..', 'app', 'public');
const read = (name) => fs.readFileSync(path.join(PUBLIC, name), 'utf8');
const ROWS = require(path.join(__dirname, '..', 'earwitness', 'app', 'public_rows', 'public_rows.json')).methodology_public;
const count = (hay, needle) => hay.split(needle).length - 1;

// LAND-1 (Reno picked direction B2, 2026-10-28) rebuilt the home page: rows
// 22, 51 and 53 described sections that no longer exist (Keep, Compare).
// Row 52 stays on the new page, byte for byte. The pins file is App's and is
// unchanged; these rows are skipped only on index.html.
const LAND_SUPERSEDED = new Set([22, 51, 53]);
// POS-1 (Reno's positioning, 2026-10-30): positioning_public rows POS-9 and
// POS-8 replace the Methodology meta (row 2) and lede (row 3); their bytes
// are pinned in tests/frontend_public_positioning.test.cjs.
const POS_SUPERSEDED = new Set([2, 3]);

test('every approved row: the new bytes once, the old bytes gone', () => {
  for (const r of ROWS) {
    if (r.file === 'index.html' && LAND_SUPERSEDED.has(r.row)) {
      assert.equal(count(read('index.html'), r.new), 0, `row ${r.row} is gone from the rebuilt home page`);
      continue;
    }
    if (r.file === 'methodology.html' && POS_SUPERSEDED.has(r.row)) {
      assert.equal(count(read('methodology.html'), r.new), 0, `row ${r.row} is replaced by its POS row`);
      assert.equal(count(read('methodology.html'), r.old), 0, `row ${r.row} old stays gone`);
      continue;
    }
    const html = read(r.file);
    assert.equal(count(html, r.new), 1, `row ${r.row} new (${r.file})`);
    if (!r.new.includes(r.old)) assert.equal(count(html, r.old), 0, `row ${r.row} old (${r.file})`);
  }
});

test('rows 21 and 23 are superseded and row 22 is amended: no "report snapshot" ships', () => {
  assert.doesNotMatch(read('index.html'), /report snapshot/);
  assert.doesNotMatch(read('methodology.html'), /The report goes to the EarWitness team; on its own it changes nothing on screen/);
});

test('row 26: Plan A card first, then Plan B, each card whole', () => {
  const html = read('pricing.html');
  const a = html.indexOf('<article class="plan" id="plan-a"');
  const b = html.indexOf('<article class="plan" id="plan-b"');
  assert.ok(a > 0 && b > a, 'Plan A before Plan B');
  assert.equal(count(html.slice(a, b), '</article>'), 1, 'Plan A card closes before Plan B');
  assert.match(html.slice(a, b), /<p class="plan-tag">If ready in time<\/p>/);
  assert.doesNotMatch(html.slice(b), /plan-tag/);
});

test('row 27-28: the Access link reads "Judges’ access" on every page; no bare "Access" link', () => {
  let links = 0;
  for (const name of fs.readdirSync(PUBLIC).filter((f) => f.endsWith('.html'))) {
    const html = read(name);
    assert.doesNotMatch(html, /href="\/access\/"[^>]*>Access<\/a>/, name);
    links += (html.match(/href="\/access\/"[^>]*>Judges&rsquo; access<\/a>/g) || []).length;
  }
  // LAND-3: the header no longer lists it; every footer and every side nav still does.
  assert.equal(links, 13, '8 footers and 5 side navs');
  assert.match(read('access.html'), /<h1>Judges&rsquo; access<\/h1>/);
});
