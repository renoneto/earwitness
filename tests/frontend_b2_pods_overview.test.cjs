/* PODS-REDESIGN-PORT, Overview (#/overview): "Named by Muse Spark"
   (PODS-STRINGS P1) after the hero card's model-named brand and on Recent
   ads rows (desktop and phone, each name its own); "Tracked brands" only
   with model_only; model_only brands are not in "Brands to add". The Try
   chips (VISDATA-AVOID's hunk) are not touched.
   Run: node --test tests/frontend_b2_pods_overview.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const BANNED = /\b(confirmed|verified|checked|possible|likely|suggested)\b/i;
const marks = s => (String(s).match(/class="ew-brand-mark">Named by Muse Spark</g) || []).length;
const FEATURED = 'sr1:4b1a7687b1cc4e449c18fb5245939b07';
const NORD = { key: 'nordvpn', display: 'NordVPN', display_basis: 'model', model_channel: 'spoken' };
const TWO_NAMES = [{ key: 'nordvpn', display: 'NordVPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'LinkedIn', model_channel: 'url' }];
const QUINCE = { key: 'quince', display: 'Quince' };
const item = (ref, read) => ({
  ref, record_kind: 'evidence', show_title: 'Show ' + ref, ep_title: 'Episode ' + ref, pub_date: '2026-10-21', ad_break_id: ref,
  heard_at_s: 63, artwork_url: null, read: { has_clip: false, media: null, display_span: [60, 90], ...read }, quotation_span: [60, 90],
});
const BRANDS = [
  { key: 'nordvpn', display: 'NordVPN', ad_breaks: 900, model_only: true },
  { key: 'quince', display: 'Quince', ad_breaks: 400 }, { key: 'betterhelp', display: 'BetterHelp', ad_breaks: 300 },
  { key: 'mintmobile', display: 'Mint Mobile', ad_breaks: 200 }, { key: 'shopify', display: 'Shopify', ad_breaks: 100 },
];
const OVERVIEW = { coverage: { episodes: 1500, shows: 796, episodes_with_transcript: 1500, earliest_episode: '2019-05-30' },
  newest_episode: { date: '2026-10-21', has_episodes: true }, recent_events: [], offer_sequence: [], alert_totals: [], activity: [],
  tracked_campaigns: [], campaign_gaps: [], alert_lifecycle_counts: {} };
const queryOf = p => { const m = /[?&]query=([^&]+)/.exec(p); return m ? JSON.parse(decodeURIComponent(m[1])) : null; };
function api({ recent, feat = [], tracked = [] }) {
  return p => {
    const q = queryOf(p);
    if (p.startsWith('/customer/overview')) return OVERVIEW;
    if (p.startsWith('/customer/saved-items')) return { items: tracked };
    if (p.startsWith('/customer/brands')) return { brands: BRANDS };
    if (p.startsWith('/customer/shows')) return { shows: [] };
    if (p.startsWith('/customer/search')) {
      const items = q && Array.isArray(q.ids) ? feat.filter(it => q.ids.includes(it.ref)) : recent;
      return { items, total: items.length };
    }
    return {};
  };
}
const page = async (o, width = 1440) => { const out = await H.render('#/overview', api(o), { width, pages: {} }); return { ...out, text: H.text(out.html) }; };
const HERO = () => item(FEATURED, { brand: QUINCE });
const MIXED = () => [HERO(), item('r1', { brand: NORD }), item('r2', { brand: NORD, model_names: TWO_NAMES }), item('r3', { brand: QUINCE })];
const recentRows = html => { const at = html.search(/<h2 class="o-h2(?:-grow|-m)">Recent ads/); return html.slice(at).split(/<div class="o-row(?:-m)?">/).slice(1); };
const featOf = html => (html.match(/<article class="o-feat(?:-m)?">[^]*?<\/article>/) || [''])[0];
const sideOf = (html, title) => { const at = html.indexOf(`<h2 class="o-side__title">${title}</h2>`); return at < 0 ? '' : html.slice(at, html.indexOf('</section>', at)); };

test('Recent ads: P1 after each model-named name, each name its own (1440 and 390); none on a plain row', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ recent: MIXED(), feat: [HERO()] }, width);
    const rows = recentRows(html);
    assert.equal(rows.length, 3, `${width}: three rows`);
    assert.equal(marks(rows[0]), 1, `${width}: one name`);
    assert.match(rows[0], /<span class="ew-brand-unit">[^]*?NordVPN[^]*? <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/);
    assert.equal(marks(rows[1]), 2, `${width}: two names, two labels`);
    assert.match(rows[1], /alt="LinkedIn"[^>]*> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/, `${width}: LinkedIn logo then label`);
    assert.equal(marks(rows[2]), 0, `${width}: plain row`);
  }
});

test('hero card: the featured ad\'s model-named brand gets P1 (1440 and 390)', async () => {
  for (const width of [1440, 390]) {
    const feat = item(FEATURED, { brand: NORD });
    const { html } = await page({ recent: [feat], feat: [feat] }, width);
    const card = featOf(html);
    assert.ok(card, `${width}: hero card`);
    assert.equal(marks(card), 1, `${width}: hero label`);
    const plain = item(FEATURED, { brand: QUINCE });
    const p = await page({ recent: [plain], feat: [plain] }, width);
    assert.equal(marks(featOf(p.html)), 0, `${width}: plain hero`);
  }
});

test('Brands to add: a model_only brand is never offered (+ Add would save nothing)', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ recent: MIXED(), feat: [HERO()] }, width);
    const side = sideOf(html, 'Brands to add');
    assert.ok(side, `${width}: card drawn`);
    assert.doesNotMatch(side, /data-brand="nordvpn"/, `${width}: no + Add NordVPN`);
    assert.match(side, /data-brand="quince"/, `${width}: the next brand fills its place`);
    assert.equal(marks(side), 0);
  }
});

test('Tracked brands: P1 only on a model_only brand', async () => {
  const tracked = [{ id: 't1', revision: 1, target: { kind: 'brand', id: 'nordvpn' }, title: 'NordVPN' }, { id: 't2', revision: 1, target: { kind: 'brand', id: 'quince' }, title: 'Quince' }];
  for (const width of [1440, 390]) {
    const { html } = await page({ recent: MIXED(), feat: [HERO()], tracked }, width);
    const side = sideOf(html, 'Tracked brands');
    const rows = side.split('<div class="o-tracked__row">').slice(1);
    assert.equal(rows.length, 2, `${width}: two rows`);
    assert.equal(marks(rows[0]), 1, `${width}: model_only labelled`);
    assert.match(rows[0], /<span class="ew-brand-unit"><span class="o-tracked__name"[^>]*>NordVPN<\/span> <span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/);
    assert.equal(marks(rows[1]), 0, `${width}: mixed brand plain`);
  }
});

test('Try chips stay as they are (VISDATA\'s hunk), no label in screen-reader text, no banned words', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page({ recent: MIXED(), feat: [HERO()] }, width);
    assert.equal(marks((html.match(/<div class="o-try"><span class="s-muted">Try[^]*?<\/div>/) || [''])[0]), 0);
    for (const a of html.match(/aria-label="[^"]*"|<span class="visually-hidden">[^<]*<\/span>/g) || []) assert.doesNotMatch(a, /Muse Spark/);
    for (const m of html.matchAll(/Named by Muse Spark/g)) assert.doesNotMatch(H.text(html.slice(Math.max(0, m.index - 300), m.index + 300)), BANNED);
  }
});

test('layout hooks: a model-named phone row puts its names on their own line; a desktop row may wrap (screen: no clip, no overlap)', async () => {
  const ph = await page({ recent: MIXED(), feat: [HERO()] }, 390);
  const rows = recentRows(ph.html);
  assert.match(rows[0], /<div class="s-row__body"><span class="o-row__names-m">/, 'names line first');
  assert.doesNotMatch((rows[0].match(/<span class="o-row__line-m">[^]*?<\/span><\/span>/) || [''])[0], /Muse Spark/, 'the clipped line carries no label');
  assert.doesNotMatch(rows[2], /o-row__names-m/, 'a plain row is as before');
  const desk = await page({ recent: MIXED(), feat: [HERO()] }, 1440);
  const d = recentRows(desk.html);
  assert.match(d[0], /<div class="o-row__meta o-row__meta--named">/);
  assert.match(d[2], /<div class="o-row__meta">/);
});

/* RPR-01: a hostile model name is escaped on the hero card, Recent rows and Tracked brands. */
test('RPR-01: a hostile model name is escaped on the hero, Recent rows (both widths) and Tracked brands', async () => {
  const BAD = { key: 'nordvpn', display: 'Nord<x-pwn>VPN', display_basis: 'model', model_channel: 'spoken' };
  const BAD_TWO = [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', model_channel: 'spoken' }, { key: 'linkup', display: 'Link<x-pwn>In', model_channel: 'url' }];
  const hero = item(FEATURED, { brand: BAD });
  const tracked = [{ id: 't1', revision: 1, target: { kind: 'brand', id: 'nordvpn' }, title: 'Nord<x-pwn>VPN' }];
  for (const width of [1440, 390]) {
    const out = await H.render('#/overview', (p, o) => {
      if (p.startsWith('/customer/brands')) return { brands: [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', ad_breaks: 9, model_only: true }, ...BRANDS.slice(1)] };
      return api({ recent: [hero, item('x1', { brand: BAD }), item('x2', { brand: BAD, model_names: BAD_TWO })], feat: [hero], tracked })(p, o);
    }, { width, pages: {} });
    assert.doesNotMatch(out.html, /<x-pwn/, `${width}: raw name`);
    assert.ok((out.html.match(/Nord&lt;x-pwn&gt;VPN/g) || []).length >= 3, `${width}: hero, rows and tracked draw it escaped`);
    assert.match(out.html, /Link&lt;x-pwn&gt;In/, `${width}: second name escaped`);
  }
});
