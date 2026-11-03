/* RECENT-ADS (UX audit B1 part 1-2, lead 2026-10-25): Overview › Recent
   ads and every Brand page's Recent ads never list an ad on the known-bleed
   list (the server marks it `known_bleed`: its card window is known to
   hold another brand's ad), with a clip or without. They prefer ads with a
   playable clip; when too few qualify they fill with the next ads without
   a clip, never with a listed ad. Rows keep the server's order (date, then
   clip, then a fixed id: the same on every copy).
   Run: node --test tests/frontend_recent_ads.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const text = F.text;
const card = html => { const s = html.indexOf('ew-ov-recent'); return html.slice(s, html.indexOf('</section>', s)); };
const brandRecent = html => { const s = html.indexOf('id="reads-h"'); return html.slice(s, html.indexOf('</section>', s)); };
// One ad per break; `ep` names the row (its episode title).
const ov = (n, { clip = false, bleed = false } = {}) => F.ovItem(n, 'brk:' + n, {
  ep_title: 'Episode ' + n, ...(bleed ? { known_bleed: true } : {}),
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: false }, display_span: [60, 120], has_clip: clip } });
const br = (n, { clip = false, bleed = false } = {}) => {
  const it = F.brandItem(n, 'brk:' + n, bleed ? { known_bleed: true } : {});
  return { ...it, pub_date: '2026-10-09', read: { ...it.read, has_clip: clip } };
};
const shown = (html, n) => new RegExp(`Episode ${n}\\b`).test(text(html));

test('recent ads: Overview never lists a listed ad, with a clip or without', async () => {
  const items = [ov(1, { clip: true, bleed: true }), ov(2, { bleed: true }), ov(3, { clip: true }), ov(4), ov(5, { clip: true })];
  const { html } = await F.render('#/overview', F.overviewApi({ items }));
  const c = card(html);
  assert.ok(c.length > 0, 'recent ads card');
  assert.ok(!shown(c, 1), 'listed ad with a clip left out');
  assert.ok(!shown(c, 2), 'listed ad without a clip left out');
  assert.ok(shown(c, 3) && shown(c, 4) && shown(c, 5), 'the next three ads fill the card');
});

test('recent ads: Overview prefers ads with a clip, then fills with ads without one', async () => {
  // Server order: 1 (no clip), 2 (clip), 3 (no clip), 4 (clip), 5 (clip).
  const items = [ov(1), ov(2, { clip: true }), ov(3), ov(4, { clip: true }), ov(5, { clip: true })];
  const c = card((await F.render('#/overview', F.overviewApi({ items }))).html);
  assert.deepEqual([1, 2, 3, 4, 5].filter(n => shown(c, n)), [2, 4, 5], 'three clip ads, before any ad without a clip');
  // Too few clip ads: fill with the next ads without one (never a listed ad), in the server's order.
  const few = [ov(1, { clip: true, bleed: true }), ov(2), ov(3, { clip: true }), ov(4, { bleed: true }), ov(5)];
  const c2 = card((await F.render('#/overview', F.overviewApi({ items: few }))).html);
  assert.deepEqual([1, 2, 3, 4, 5].filter(n => shown(c2, n)), [2, 3, 5]);
  const rows = [...c2.matchAll(/Episode (\d)/g)].map(m => m[1]);
  assert.deepEqual([...new Set(rows)], ['2', '3', '5'], 'rows keep the server order');
});

test('recent ads: only listed ads leaves the card pointing to Search, never a listed row', async () => {
  const items = [ov(1, { clip: true, bleed: true }), ov(2, { bleed: true })];
  const c = card((await F.render('#/overview', F.overviewApi({ items }))).html);
  assert.ok(!shown(c, 1) && !shown(c, 2));
  assert.match(text(c), /The newest ads are in Search ?\./);
});

test('recent ads: a Brand page never lists a listed ad and prefers clips', async () => {
  const items = [br(1, { clip: true, bleed: true }), br(2), br(3, { clip: true }), br(4, { bleed: true }), br(5, { clip: true }),
    br(6), br(7, { clip: true }), br(8)];
  const { html } = await F.render('#/brand/chatgpt', F.brandApi(F.brandDetail(), items));
  const r = brandRecent(html);
  assert.ok(r.length > 0, 'recent ads section');
  assert.ok(!shown(r, 1) && !shown(r, 4), 'listed ads left out');
  // Three clip ads (3, 5, 7), then the next two without a clip (2, 6); 8 is the sixth.
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7, 8].filter(n => shown(r, n)), [2, 3, 5, 6, 7]);
});
