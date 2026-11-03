/* PODS-REDESIGN-PORT, Brand page (#/brand/{key}): the heading takes
   "Named by Muse Spark" (PODS-STRINGS P1) only for a model_only brand;
   Recent rows show P1 with no brand name in front; P4 counts the Recent rows
   shown; a model_only brand hides Track (+ Add), Ask and Create alert.
   Run: node --test tests/frontend_b2_pods_brand.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const BANNED = /\b(confirmed|verified|checked|possible|likely|suggested)\b/i;
const marks = s => (String(s).match(/class="ew-brand-mark">Named by Muse Spark</g) || []).length;

const NORD = { key: 'nordvpn', display: 'NordVPN', display_basis: 'model', model_channel: 'spoken' };
const NORD_URL = { ...NORD, model_channel: 'url' };
const TWO_NAMES = [{ key: 'nordvpn', display: 'NordVPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'LinkedIn', model_channel: 'url' }];
const PLAIN = { key: 'nordvpn', display: 'NordVPN', display_basis: 'brand_list' };
const item = (ref, read, extra = {}) => ({
  ref, record_kind: 'evidence', show_title: 'Show ' + ref, ep_title: 'Episode ' + ref, pub_date: '2026-10-21', ad_break_id: ref,
  heard_at_s: 63, artwork_url: null, read: { has_clip: false, media: null, display_span: [60, 90], ...read }, quotation_span: [60, 90],
  text: 'Protect yourself online with NordVPN today.', ...extra,
});
const ADS_Q = { version: 2, mode: 'ads', q: '', brands: ['nordvpn'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null };
const DETAIL = (o = {}) => ({ brand: {
  brand: 'nordvpn', key: 'nordvpn', display: 'NordVPN', ad_breaks: 4, episodes: 4, shows: 4, last_heard: '2026-10-21',
  top_shows: [], reads_without_show: 0, ask_button: true, detections: 4,
  drill_down: { ads: ADS_Q, mentions: { ...ADS_Q, mode: 'mentions' } },
  coverage: { episodes_in_scope: 1500, sources_included: 1500, excluded_undated: 0 }, ...o } });
const OVERVIEW = { coverage: { episodes: 1500, shows: 796, episodes_with_transcript: 1500, earliest_episode: '2019-05-30', screened_episodes: 1500, screened_shows: 796 }, newest_episode: { date: '2026-10-21', has_episodes: true } };
function api({ detail = DETAIL(), recent = [] } = {}) {
  return p => {
    if (p.startsWith('/customer/research-query')) return { result: { items: recent, total: recent.length, total_relation: 'exact', break_total: recent.length, ads_with_clips: 0 }, summaries: {}, revision: 'rev-1' };
    if (p.startsWith('/customer/brands/')) return detail;
    if (p.startsWith('/customer/overview')) return OVERVIEW;
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    if (p.startsWith('/customer/shows')) return { shows: [] };
    if (p.startsWith('/customer/summary')) return { cells: [] };
    return {};
  };
}
async function page(o = {}, width = 1440) {
  const a = api(o);
  const b = H.boot({ route: '#/settings', width });
  const { win, els } = b;
  await H.flush();
  win.EWBrandB2.today('2026-10-30');
  win.EW.current.hosted = false; win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.current.askAvailable = true;
  win.EW.api = async (p, opts) => { const out = a(p, opts); if (out instanceof Error) throw out; return out; };
  win.location.hash = '#/brand/nordvpn'; win.EW.current.route = '#/brand/nordvpn';
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await H.flush();
  H.stopTimers();
  const html = els['#main'].innerHTML;
  return { html, text: H.text(html) };
}
const h1Of = html => (html.match(/<h1 class="b-h1(?:-m)?">[^]*?<\/h1>/) || [''])[0];
const rowsOf = html => html.match(/<div class="b-row(?:-m)?">/g) || [];
const recentOf = html => { const at = html.search(/<h2 class="b-h2(?:-grow|-m)">Recent /); return at < 0 ? '' : html.slice(at); };
const MODEL_ONLY = () => DETAIL({ model_only: true });
const RECENT_MODEL = () => [item('r1', { brand: NORD }), item('r2', { brand: NORD_URL }), item('r3', { brand: NORD, model_names: TWO_NAMES })];
const RECENT_MIXED = () => [item('m1', { brand: NORD }), item('m2', { brand: PLAIN }), item('m3', { brand: PLAIN })];

test('heading: P1 after the name only with model_only (1440 and 390), one unit', async () => {
  for (const width of [1440, 390]) {
    const only = await page({ detail: MODEL_ONLY(), recent: RECENT_MODEL() }, width);
    assert.equal(marks(h1Of(only.html)), 1, `${width}: model_only heading`);
    assert.match(h1Of(only.html), /<span class="ew-brand-unit">NordVPN <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/);
    const mixed = await page({ recent: RECENT_MIXED() }, width);
    assert.ok(h1Of(mixed.html), `${width}: heading drawn`);
    assert.equal(marks(h1Of(mixed.html)), 0, `${width}: mixed brand heading plain`);
  }
});

test('Recent rows: P1 on each model-named read with no brand name in front; none on a plain read', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ recent: RECENT_MIXED() }, width);
    const rows = recentOf(html).split(/<div class="b-row(?:-m)?">/).slice(1);
    assert.equal(rows.length, 3, `${width}: three rows`);
    assert.equal(marks(rows[0]), 1, `${width}: model-named row`);
    assert.match(rows[0], /<span class="b-named"><span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/, `${width}: the label alone`);
    assert.doesNotMatch(rows[0].replace(/<p class="s-quote[^]*?<\/p>|<span class="b-row__quote-m">[^]*?<\/span>/, ''), /NordVPN[^<]*<\/[^>]*>\s*<span class="ew-brand-mark/, `${width}: no name before it`);
    assert.equal(marks(rows[1]) + marks(rows[2]), 0, `${width}: plain rows`);
  }
});

test('Recent rows: a two-name read is one row with one label', async () => {
  const { html } = await page({ recent: [item('t1', { brand: NORD, model_names: TWO_NAMES })] });
  const rows = recentOf(html).split(/<div class="b-row">/).slice(1);
  assert.equal(rows.length, 1);
  assert.equal(marks(rows[0]), 1);
});

test('P4: counted from the Recent rows shown; singular; hidden at 0', async () => {
  const two = await page({ recent: RECENT_MIXED().concat(item('m4', { brand: NORD_URL })) });
  assert.ok(two.text.includes('Muse Spark named the brand in 2 of these ads.'), two.text);
  const one = await page({ recent: RECENT_MIXED() });
  assert.ok(one.text.includes('Muse Spark named the brand in 1 of these ads.'));
  const none = await page({ recent: [item('p1', { brand: PLAIN }), item('p2', { brand: PLAIN })] });
  assert.doesNotMatch(none.html, /Muse Spark/);
  // The phone shows 3 rows: a fourth model-named read is not counted there.
  const ph = await page({ recent: [item('p1', { brand: PLAIN }), item('p2', { brand: PLAIN }), item('p3', { brand: PLAIN }), item('x4', { brand: NORD })] }, 390);
  assert.equal(rowsOf(ph.html).length, 3);
  assert.doesNotMatch(ph.html, /Muse Spark named the brand/, 'a row not shown is not counted');
});

test('P4 with a known-bleed row in the fixture counts only the rows shown', async () => {
  const bleed = item('kb', { brand: NORD }, { known_bleed: true });
  for (const width of [1440, 390]) {
    const { html, text } = await page({ recent: [bleed, item('m1', { brand: NORD }), item('m2', { brand: PLAIN })] }, width);
    const shownModel = recentOf(html).split(/<div class="b-row(?:-m)?">/).slice(1).filter(r => marks(r) === 1).length;
    const n = Number((/Muse Spark named the brand in (\d+) of these ads\./.exec(text) || [])[1]);
    assert.equal(n, shownModel, `${width}: P4 equals the labelled rows on screen`);
  }
});

test('model_only: Track (+ Add), its hint, Ask and Create alert are hidden; Compare stays', async () => {
  for (const width of [1440, 390]) {
    const { html, text } = await page({ detail: MODEL_ONLY(), recent: RECENT_MODEL() }, width);
    assert.doesNotMatch(html, /data-action="track-brand"/, `${width}: no Track`);
    assert.doesNotMatch(text, /Add NordVPN to my Overview|Added brands show their new ads there/, `${width}: no Add words`);
    assert.doesNotMatch(html, /href="#\/ask\?brand=/, `${width}: no Ask`);
    assert.doesNotMatch(html, /data-action="alert-brand"/, `${width}: no Create alert`);
    assert.match(html, /href="#\/compare\?brands=nordvpn"/, `${width}: Compare stays`);
    const mixed = await page({ recent: RECENT_MIXED() }, width);
    assert.match(mixed.html, /data-action="track-brand"/, `${width}: mixed keeps Track`);
    assert.match(mixed.html, /href="#\/ask\?brand=/, `${width}: mixed keeps Ask`);
    assert.match(mixed.html, /data-action="alert-brand"/, `${width}: mixed keeps Create alert`);
  }
});

test('no verification or confidence word next to the label; short labels keep no P1', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ detail: MODEL_ONLY(), recent: RECENT_MODEL() }, width);
    for (const m of html.matchAll(/Named by Muse Spark/g)) {
      const near = H.text(html.slice(Math.max(0, m.index - 300), m.index + 300)).replace(/Ads checked/g, '');
      assert.doesNotMatch(near, BANNED, `${width}: ${near}`);
    }
    for (const a of html.match(/aria-label="[^"]*"/g) || []) assert.doesNotMatch(a, /Muse Spark/);
    assert.doesNotMatch((html.match(/<a class="b-back[^]*?<\/a>/) || [''])[0], /Muse Spark/);
  }
});

test('UX D41 item 4: on a model_only brand the count line stays between "Named by Muse Spark" and "Ads checked" (1440, 390)', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ detail: MODEL_ONLY(), recent: RECENT_MODEL() }, width);
    const label = html.indexOf('Named by Muse Spark');
    const count = html.search(/<p class="b-count(?:-m)?">4 NordVPN ads in 4 episodes across 4 shows/);
    const checked = html.indexOf('Ads checked');
    assert.ok(label > 0 && count > 0, `${width}: label and count drawn`);
    assert.ok(label < count, `${width}: the heading's label comes before the count line`);
    // The 1440 head has the coverage chip; the phone head has none (count line is then last of the three).
    if (width === 1440) assert.ok(checked > 0, '1440: chip drawn');
    if (checked > 0) assert.ok(count < checked, `${width}: count ${count} < checked ${checked}`);
  }
});

/* RPR-01: a hostile brand name is escaped in the model_only heading and around the Recent rows. */
test('RPR-01: a hostile model_only brand name is escaped (1440, 390)', async () => {
  const BAD = { key: 'nordvpn', display: 'Nord<x-pwn>VPN', display_basis: 'model', model_channel: 'spoken' };
  const BAD_TWO = [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'Link<x-pwn>In', model_channel: 'url' }];
  for (const width of [1440, 390]) {
    const { html } = await page({ detail: DETAIL({ display: 'Nord<x-pwn>VPN', model_only: true }), recent: [item('h1', { brand: BAD }), item('h2', { brand: BAD, model_names: BAD_TWO })] }, width);
    assert.doesNotMatch(html, /<x-pwn/, `${width}: raw name`);
    assert.match(h1Of(html), /<span class="ew-brand-unit">Nord&lt;x-pwn&gt;VPN <span class="ew-brand-mark">/, `${width}: heading escaped`);
  }
});
