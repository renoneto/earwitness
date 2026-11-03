/* PODS-REDESIGN-PORT, Search (#/search): "Named by Muse Spark" (PODS-STRINGS
   P1) after each model-named brand name on the redesigned Search, P4 under
   the result count, brand-level names labelled only with model_only, short
   and screen-reader labels without P1, no brand alert on a model_only brand.
   Run: node --test tests/frontend_b2_pods_search.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const H = require('./b2_harness.cjs');

const P1 = 'Named by Muse Spark';
const MARK = `<span class="ew-brand-mark">${P1}</span>`;
const BANNED = /\b(confirmed|verified|checked|possible|likely|suggested)\b/i;
const count = (s, re) => (String(s).match(re) || []).length;
const marks = s => count(s, /class="ew-brand-mark">Named by Muse Spark</g);

/* -- Fixtures: a spoken model name, a two-name read, a plain brand ----------- */
const NORD = { key: 'nordvpn', display: 'NordVPN', display_basis: 'model', model_channel: 'spoken' };
const TWO_NAMES = [{ key: 'nordvpn', display: 'NordVPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'LinkedIn', model_channel: 'url' }];
const QUINCE = { key: 'quince', display: 'Quince', display_basis: 'brand_list' };
function item(i, read, o = {}) {
  return {
    ref: `sr1:p${i}`, record_kind: 'evidence', episode_id: `ep${i}`, ad_break_id: `b${i}`,
    show: { display_title: `Show ${i}`, show_identity_id: `show-${i}` }, show_title: `Show ${i}`,
    ep_title: `Episode ${i}`, pub_date: '2026-10-21', text: o.text || 'Go to the link in the show notes for this offer.', quotation_span: [60, 90],
    heard_at_s: 63, artwork_url: null, brand_spelled: o.spelled === undefined ? null : o.spelled,
    read: { has_clip: false, media: null, said: { web_addresses: [], promo_codes: [] }, ...read },
    ...(o.extra || {}),
  };
}
const MODEL = i => item(i, { brand: NORD });
const TWO = i => item(i, { brand: NORD, model_names: TWO_NAMES });
const PLAIN = i => item(i, { brand: QUINCE }, { text: 'Quince makes it easy to build on those everyday staples.' });
const BRANDS = [
  { key: 'quince', display: 'Quince', ad_breaks: 99, episodes: 95 },
  { key: 'nordvpn', display: 'NordVPN', ad_breaks: 3, episodes: 3, model_only: true },
  { key: 'betterhelp', display: 'BetterHelp', ad_breaks: 80, episodes: 70 },
];
const WEEKS = [['2026-10-05', 1], ['2026-10-12', 1], ['2026-10-19', 2]];
const cells = () => WEEKS.map(([from, n]) => ({ key: from, label: from, count: n, breaks: n, interval: { from, to: from } }));
function bundle(query, items) {
  const result = { items, total: items.length, episode_total: items.length, show_total: items.length, per_page: 10, break_total: items.length, break_dated_total: items.length, break_undated_total: 0,
    ad_breaks: items.map(x => ({ id: x.ad_break_id, refs: [x.ref], brands: [x.read.brand], read_count: 1, matching_read_count: 1 })), ads_with_clips: 0 };
  return { query: { ...query }, result, summary: { week: cells() }, revision: 'rev-1', coverage: { episodes_in_scope: 1500 } };
}
const OVERVIEW = { coverage: { episodes: 1500, shows: 796, earliest_episode: '2019-05-30' }, newest_episode: { date: '2026-10-21', has_episodes: true } };
function api(items, { brands = BRANDS } = {}) {
  return (p, o = {}) => {
    if (p === '/customer/brands') return { brands };
    if (p === '/customer/shows') return { shows: [{ id: 'show-1', display_title: 'Show 1', episodes: 1500, screened_episodes: 1500 }] };
    if (p === '/customer/overview') return OVERVIEW;
    if (p === '/customer/research-query') return bundle(o.body.query, typeof items === 'function' ? items(o.body.query) : items);
    return {};
  };
}
const MIXED = [MODEL(1), TWO(2), PLAIN(3), PLAIN(4)];
const page = async (route, items = MIXED, width = 1440, opts) => {
  const out = await H.render(route, api(items, opts), { width });
  return { ...out, text: H.text(out.html) };
};
const WORDS = '#/search?mode=ads&q=offer&view=';
const panelOf = html => (html.match(/<aside class="ew-inspector s-panel"[^]*?<\/aside>/) || [''])[0];
// The element starting at `at` with this tag, to its matching close.
function element(html, at, tag) {
  let depth = 0;
  const re = new RegExp(`<${tag}\\b|</${tag}>`, 'g');
  re.lastIndex = at;
  for (let m; (m = re.exec(html));) {
    depth += m[0] === `</${tag}>` ? -1 : 1;
    if (!depth) return html.slice(at, m.index + m[0].length);
  }
  return html.slice(at);
}
const cardOf = (html, ref) => element(html, html.indexOf(`<article class="s-card`, html.indexOf(`id="row-${ref}"`) - 80), 'article');
const units = html => [...String(html).matchAll(/<span class="ew-brand-unit">([^]*?)<span class="ew-brand-mark">Named by Muse Spark<\/span><\/span>/g)].map(m => m[1].trim());

/* -- P1 on cards and the preview -------------------------------------------- */
test('cards: a model-named brand takes P1 right after its name, in one unit; a plain brand none', async () => {
  const { html } = await page(WORDS + 'cards');
  const one = cardOf(html, 'sr1:p1');
  assert.equal(marks(one), 1, 'one label on the one-name card');
  assert.deepEqual(units(one), ['NordVPN'], 'the name and its label are one unit');
  const plain = cardOf(html, 'sr1:p3');
  assert.match(plain, /alt="Quince"/);
  assert.equal(marks(plain), 0, 'no P1 on a non-model brand');
});

test('cards: two model names each show with their own P1 (logo or plain name), no main brand', async () => {
  const { html } = await page(WORDS + 'cards');
  const two = cardOf(html, 'sr1:p2');
  assert.equal(marks(two), 2, 'each name its own label');
  const u = units(two);
  assert.equal(u.length, 2);
  assert.equal(u[0], 'NordVPN');
  assert.match(u[1], /^<img class="s-logo s-logo--14"[^>]*alt="LinkedIn"[^>]*>$/, 'the logo keeps its logo, the label after it');
});

test('preview: the ids line takes P1 per name; the two-name read lists both', async () => {
  const { html } = await page(WORDS + 'list', [TWO(1), PLAIN(2)]);
  const panel = panelOf(html);
  const ids = (panel.match(/<div class="s-ids">[^]*?<span class="s-grow">/) || [''])[0];
  assert.equal(marks(ids), 2, 'ids line: both names labelled');
  assert.equal(marks(panel), 2, 'the names are labelled once in the preview');
});

test('UX D41: no "Brand heard" chip on a model-named read (one or two names); a plain read keeps it', async () => {
  for (const items of [[MODEL(1), PLAIN(2)], [TWO(1), PLAIN(2)]]) {
    const panel = panelOf((await page(WORDS + 'list', items)).html);
    assert.doesNotMatch(panel, /Brand heard/, 'no chip');
    assert.match(panel, /class="s-ids"[^]*?Named by Muse Spark/, 'the names still show, labelled, in the ids line');
  }
  const plain = panelOf((await page(WORDS + 'list', [PLAIN(1), MODEL(2)])).html);
  assert.match(plain, /<span class="s-tag s-tag--none">Brand heard: <b class="s-chart__title">Quince<\/b><\/span>/);
});

test('preview of a plain brand: no P1 anywhere in the panel', async () => {
  const { html } = await page(WORDS + 'list', [PLAIN(1), MODEL(2)]);
  assert.equal(marks(panelOf(html)), 0);
});

/* -- Short and screen-reader labels ----------------------------------------- */
test('screen-reader and short labels name every brand with no P1 (phone row, Selected, Inside tag)', async () => {
  const phone = await page(WORDS + 'list', [TWO(1)], 390);
  const label = (phone.html.match(/<div class="s-prow"[^>]*aria-label="([^"]*)"/) || [])[1];
  assert.equal(label, 'Open the ad: NordVPN · LinkedIn on Show 1');
  const desk = await page(WORDS + 'list', [TWO(1)]);
  const live = (desk.html.match(/data-inspector-live>([^<]*)</) || [])[1];
  assert.equal(live, 'Selected: NordVPN · LinkedIn · Show 1');
  for (const html of [phone.html, desk.html]) assert.doesNotMatch(String(html.match(/aria-label="[^"]*"/g)), /Muse Spark/);
  // "Inside a/an X ad" (Any mention) lists every name, with no P1.
  const any = await page('#/search?mode=mentions&q=offer&view=list', [TWO(1)]);
  const tag = (any.html.match(/<span class="s-tag s-tag--inside">[^]*?<\/span>/) || [''])[0];
  assert.equal(tag, '<span class="s-tag s-tag--inside">Inside a NordVPN · LinkedIn ad</span>');
});

/* -- P4 --------------------------------------------------------------------- */
test('P4: counted from the rows shown, under the result count (1440 and 390)', async () => {
  for (const width of [1440, 390]) {
    const { html, text } = await page(WORDS + 'list', MIXED, width);
    assert.ok(text.includes('Muse Spark named the brand in 2 of these ads.'), `${width}: 2 of 4`);
    const bar = width === 390 ? html.indexOf('class="s-bar-m"') : html.indexOf('class="s-bar"');
    const p4 = html.indexOf('ew-model-count');
    assert.ok(bar > 0 && p4 > bar && p4 < html.indexOf('id="search-results"'), `${width}: under the count, above the list`);
  }
});

test('P4: singular at one ad; hidden at zero', async () => {
  const one = await page(WORDS + 'list', [MODEL(1), PLAIN(2)]);
  assert.ok(one.text.includes('Muse Spark named the brand in 1 of these ads.'));
  const none = await page(WORDS + 'list', [PLAIN(1), PLAIN(2)]);
  assert.doesNotMatch(none.html, /Muse Spark/);
  assert.doesNotMatch(none.html, /ew-model-count/);
});

test('P4: only rows on screen count (a two-name ad is one ad; a second read of a shown ad is not a row)', async () => {
  const hidden = { ...MODEL(9), ad_break_id: 'b3' };  // the same ad as the plain row b3: rowsOf shows one read
  const { text } = await page(WORDS + 'list', [TWO(1), PLAIN(3), hidden]);
  assert.ok(text.includes('Muse Spark named the brand in 1 of these ads.'));
});

/* -- Brand-level names: P1 only with model_only ------------------------------ */
test('a model_only brand filter: chip and heading take P1; Create alert is hidden', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page('#/search?mode=ads&brands=nordvpn&view=list', [MODEL(1), TWO(2)], width);
    const chip = (html.match(/<span class="s-chip(?:-m)?">[^]*?aria-label="Remove filter: Brand: NordVPN"/) || [''])[0];
    assert.equal(marks(chip), 1, `${width}: chip`);
    assert.deepEqual(units(chip), ['NordVPN'], `${width}: chip name and label one unit`);
    const h1 = (html.match(/<h1 class="s-head__title[^"]*">[^]*?<\/h1>/) || [''])[0];
    assert.equal(marks(h1), 1, `${width}: heading`);
    assert.ok(H.text(h1).startsWith('2 NordVPN Named by Muse Spark ads'), `${width}: ${H.text(h1)}`);
    assert.doesNotMatch(html, /data-action="create-alert"/, `${width}: no brand alert that never fires`);
  }
});

test('a mixed brand filter: no brand-level P1 (chip, heading, links, chart), only per-read labels; Create alert stays', async () => {
  for (const width of [1440, 390]) {
    const { html } = await page('#/search?mode=ads&brands=quince&view=cards', [PLAIN(1), MODEL(2)], width);
    const chip = (html.match(/<span class="s-chip(?:-m)?">[^]*?aria-label="Remove filter: Brand: Quince"/) || [''])[0];
    assert.ok(chip && marks(chip) === 0, `${width}: chip`);
    const h1 = (html.match(/<h1 class="s-head__title[^"]*">[^]*?<\/h1>/) || [''])[0];
    assert.ok(h1 && marks(h1) === 0, `${width}: heading`);
    const head = html.slice(0, html.indexOf('id="search-results"'));
    assert.equal(marks(head.replace(/<p class="[^"]*ew-model-count[^]*?<\/p>/, '')), 0, `${width}: nothing brand-level above the list`);
    assert.match(html, /data-action="create-alert"/, `${width}: Create alert stays`);
  }
});

test('brand lists: the empty state\'s brands and the loading head label a model_only brand only', () => {
  const b = H.boot({ route: '#/search?mode=ads&q=zz' });
  const S = b.win.EWSearchB2;
  const empty = S.emptyHtml({ q: 'zz', mode: 'ads' }, { shows: [], brands: BRANDS });
  const nord = (empty.match(/<a class="s-empty__brand"[^>]*brands=nordvpn[^>]*>[^]*?<\/a>/) || [''])[0];
  assert.equal(marks(nord), 1, 'model_only brand in the list');
  const quince = (empty.match(/<a class="s-empty__brand"[^>]*brands=quince[^>]*>[^]*?<\/a>/) || [''])[0];
  assert.ok(quince && marks(quince) === 0, 'mixed brand: none');
});

/* -- Words ------------------------------------------------------------------ */
test('no verification or confidence word next to the label; P1 word for word', async () => {
  for (const width of [1440, 390]) {
    for (const view of ['list', 'cards']) {
      const { html } = await page(WORDS + view, MIXED, width);
      assert.ok(!/Named by Muse Spark/.test(html) || html.includes(MARK), 'exact P1');
      for (const m of html.matchAll(/Named by Muse Spark/g)) {
        const near = H.text(html.slice(Math.max(0, m.index - 300), m.index + 300));
        assert.doesNotMatch(near, BANNED, `${width} ${view}: ${near}`);
      }
      assert.doesNotMatch(html, /Muse Spark named the brand in \d+ of these ads\.[^<]*\b(confirmed|verified|likely)\b/);
    }
  }
});

/* -- Campaign screens keep "Brand not identified" for a model-named read ----- */
test('Add to my campaign on a model-named read sends no brand name (FUNC-1 decision 2)', async () => {
  const { html } = await page(WORDS + 'list', [MODEL(1), PLAIN(2)]);
  const add = (panelOf(html).match(/<button type="button" class="s-panel__add"[^>]*>/) || [''])[0];
  assert.ok(add, 'the add button');
  assert.doesNotMatch(add, /data-brand-name=/);
  assert.match(add, /data-adding="Ad, brand not identified · Show 1/);
  const plain = await page(WORDS + 'list', [PLAIN(1)]);
  assert.match(panelOf(plain.html), /data-brand-name="Quince"/);
});

test('layout hook: a model-named read\'s names are one group (s-names) in the card and preview; a plain read is unchanged', async () => {
  const { html } = await page(WORDS + 'cards', [TWO(1), PLAIN(2)]);
  const two = cardOf(html, 'sr1:p1');
  assert.match(two, /<span class="s-adtag">AD<\/span><span class="s-names"><span class="ew-brand-unit">/);
  const panel = panelOf(html);
  assert.match(panel, /<div class="s-ids"><span class="s-adtag">AD<\/span><span class="s-names">/);
  assert.doesNotMatch(cardOf(html, 'sr1:p2'), /s-names/);
  const css = require('node:fs').readFileSync(require('node:path').join(H.APP, 'search-b2.css'), 'utf8');
  assert.match(css, /\.s-ids:has\(\.s-names\) \{\s*flex-wrap: wrap;/, 'the row wraps rather than squeeze the names');
});

test('UX D41 item 4: on a model_only filter at 390 the count line stays between "Named by Muse Spark" and "Ads checked"', async () => {
  const { html } = await page('#/search?mode=ads&brands=nordvpn&view=list', [MODEL(1), TWO(2)], 390);
  const label = html.indexOf('Named by Muse Spark');
  const count = html.indexOf('in 2 episodes across 2 shows');
  const checked = html.indexOf('Ads checked');
  assert.ok(label > 0 && count > 0 && checked > 0, `all three drawn: ${label} ${count} ${checked}`);
  assert.ok(label < count && count < checked, `label ${label} < count ${count} < checked ${checked}`);
});

/* RPR-01: a hostile model name is escaped at every print site. */
test('RPR-01: a hostile model name is escaped on cards, list, phone rows, preview, Selected, Inside tag, chips, heading and brand lists', async () => {
  const BAD = { key: 'nordvpn', display: 'Nord<x-pwn>VPN', display_basis: 'model', model_channel: 'spoken' };
  const BAD_TWO = [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', model_channel: 'spoken' }, { key: 'linkedin', display: 'Link<x-pwn>In', model_channel: 'url' }];
  const items = () => [item(1, { brand: BAD, model_names: BAD_TWO }), item(2, { brand: BAD })];
  const brands = [{ key: 'nordvpn', display: 'Nord<x-pwn>VPN', ad_breaks: 3, model_only: true }, { key: 'quince', display: 'Quince', ad_breaks: 99 }];
  const routes = [[WORDS + 'cards', 1440], [WORDS + 'list', 1440], [WORDS + 'list', 390], ['#/search?mode=mentions&q=offer&view=list', 1440],
    ['#/search?mode=ads&brands=nordvpn&view=list', 1440], ['#/search?mode=ads&brands=nordvpn&view=list', 390]];
  let escaped = 0;
  for (const [route, width] of routes) {
    const { html } = await page(route, items(), width, { brands });
    assert.doesNotMatch(html, /<x-pwn/, `${route} ${width}: raw name`);
    escaped += (html.match(/&lt;x-pwn&gt;/g) || []).length;
  }
  assert.ok(escaped > 20, `the names were drawn (escaped ${escaped} times)`);
  const S = H.boot({ route: '#/search?mode=ads&q=zz' }).win.EWSearchB2;
  const empty = S.emptyHtml({ q: 'zz', mode: 'ads' }, { shows: [], brands });
  assert.doesNotMatch(empty, /<x-pwn/);
  assert.match(empty, /Nord&lt;x-pwn&gt;VPN/);
});
