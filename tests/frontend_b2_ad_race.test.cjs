/* AD-RACE: on a cold load the boot read can answer before b2.js and the
   page modules (deferred scripts after views.js) have run. A redesigned
   page must still win: today's page is never drawn for a route a
   redesigned page owns, not even briefly. The harness's late mode runs
   b2.js and the page modules only after the boot read and its draw have
   had their turn, then fires DOMContentLoaded.
   Run: node --test tests/frontend_b2_ad_race.test.cjs */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const REF = 'named:one';
const RECORD = {
  ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding', review: { judgment: null, bounds_verified: 0 },
  attribution: [], relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 1316, end_s: 1410 },
  episode: { episode_id: 'ep_p', show_title: 'A Show', ep_title: 'Episode 12', pub_date: '2026-10-21', fetch_date: '2026-10-22', duration_min: 50, transcript_duration_s: 3000 },
  show: { show_identity_id: 'cs-p', display_title: 'A Show', unresolved: 0 },
  evidence: { id: 'named:ep_p.1320', episode_id: 'ep_p', orig_start_s: 1320, orig_end_s: 1400, transcript_version_id: 7 },
  media: { playback: 'unavailable', reason: 'no authorized recording proof' }, heard_at_s: 1322,
  read: { kind: 'named', source_label: 'Named read', needs_review: false, self_promo: false, display_span: [1320, 1400], has_clip: false, candidates: [],
    brand: { key: 'quince', display: 'Quince', display_basis: 'brand_list' }, said: { spoken_brand: 'Quince', web_addresses: [], promo_codes: [] } },
  window: { segments: [{ segment_id: 1, start_s: 1320, end_s: 1330, text: 'Quince has the softest sheets.', text_start: 0 }], text_complete: true, next_cursor: null },
};

// Cold load in the losing order: views.js boots, its read answers and the
// first draw runs, and only then do b2.js and the page modules arrive.
async function coldLoad(route, { pages = {}, api = () => ({}) } = {}) {
  const b = H.boot({ route, late: true, pages });
  const main = b.els['#main'];
  const drawn = [];
  let html = '';
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = String(v); drawn.push(html); } });
  b.win.EW.toast = () => {}; b.win.EW.saveNotice = () => {};
  b.win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  for (let i = 0; i < 30; i++) await H.flush();
  b.loadLate();
  for (let i = 0; i < 30; i++) await H.flush();
  H.stopTimers();
  return { html, drawn, b };
}

test('a cold Ad page never draws today\'s reader when the page modules arrive after the boot read', async () => {
  const { html, drawn, b } = await coldLoad('#/evidence/' + encodeURIComponent(REF), {
    api: p => (p === '/customer/records/' + encodeURIComponent(REF) ? { record: RECORD } : p.endsWith('/mistake') ? { mistakes: [] } : {}),
  });
  assert.ok(/class="[^"]*\ba-page\b/.test(html), 'the redesigned Ad page is on screen');
  assert.ok(b.body.classList.contains('ew-b2--ad'), 'body scoped to the Ad page');
  const reader = drawn.filter(h => /\bew-reader\b/.test(h));
  assert.equal(reader.length, 0, 'today\'s reader was drawn ' + reader.length + ' time(s)');
});

// The same order for the other routes the redesign owns: a stand-in page
// per route shows which page function drew.
for (const [name, route, fn] of [
  ['overview', '#/overview', 'overviewPage'],
  ['search', '#/search?mode=ads&q=offer', 'searchPage'],
  ['brand', '#/brand/quince', 'brandPage'],
  ['ad', '#/evidence/' + encodeURIComponent(REF), 'evidencePage'],
]) {
  test(`${name}: only the redesigned page draws when the modules arrive after the boot read`, async () => {
    const pages = { [name]: { on: true, loading: () => `<div class="race-new">loading ${name}</div>`, render: async () => `<div class="race-new">${name}</div>` } };
    const { html, drawn, b } = await coldLoad(route, { pages });
    assert.ok(html.includes('race-new'), `${name}: the redesigned page is on screen`);
    // Every draw is a loading line or the redesigned page, never another page.
    const other = drawn.filter(h => !h.includes('race-new') && !/^<div class="loading"/.test(h));
    assert.deepEqual(other.map(h => h.slice(0, 80)), [], `${name}: no other page drawn (today's ${fn})`);
    assert.ok(b.body.classList.contains('ew-b2'), 'body marked for the redesign');
  });
}

test('the sign-in gate is the redesigned one when the modules arrive after the boot read', async () => {
  // The hosted gate, signed out: the session read answers 401 with the sign-in block.
  const signin = { available: true, reason: '', login_url: null, methods: ['password'] };
  const fetch = async url => {
    if (url === '/api/v1/session') return { ok: false, status: 401, text: async () => JSON.stringify({ error: { code: 'SIGNIN_REQUIRED' }, signin }) };
    throw new Error('no network in unit test');
  };
  const b = H.boot({ route: '#/overview', late: true, fetch });
  const main = b.els['#main'];
  const drawn = [];
  let html = '';
  Object.defineProperty(main, 'innerHTML', { get: () => html, set: v => { html = String(v); drawn.push(html); } });
  for (let i = 0; i < 30; i++) await H.flush();
  b.loadLate();
  for (let i = 0; i < 30; i++) await H.flush();
  H.stopTimers();
  const pagesDrawn = drawn.filter(h => !/^<div class="loading"/.test(h));
  assert.ok(pagesDrawn.length > 0 && /signin-email/.test(pagesDrawn[pagesDrawn.length - 1]), 'the gate is on screen: ' + pagesDrawn.map(h => h.slice(0, 60)).join(' | '));
  assert.ok(b.body.classList.contains('ew-b2--signin'), 'the redesigned gate is on screen');
  assert.equal(pagesDrawn.length, 1, 'one gate draw, the redesigned one: ' + pagesDrawn.map(h => h.slice(0, 60)).join(' | '));
});

// RAR-01: once the modules have arrived the wait is over for good. A later
// navigation draws straight away, with no "Opening…" line in between.
test('after a cold load, the next navigation draws without the opening wait', async () => {
  const pages = { overview: { on: true, loading: () => '<div class="race-new">loading overview</div>', render: async () => '<div class="race-new">overview</div>' } };
  const { drawn, b } = await coldLoad('#/evidence/' + encodeURIComponent(REF), {
    pages,
    api: p => (p === '/customer/records/' + encodeURIComponent(REF) ? { record: RECORD } : p.endsWith('/mistake') ? { mistakes: [] } : {}),
  });
  const before = drawn.length;
  b.win.location.hash = '#/overview';
  b.win.EW.current.route = '#/overview';
  await b.win.EWViews.render();
  for (let i = 0; i < 20; i++) await H.flush();
  H.stopTimers();
  const after = drawn.slice(before);
  assert.ok(after.some(h => h.includes('>overview<')), 'the next page is drawn: ' + after.map(h => h.slice(0, 60)).join(' | '));
  assert.deepEqual(after.filter(h => /Opening…/.test(h)).map(h => h.slice(0, 80)), [], 'no opening wait after the first draw');
});
