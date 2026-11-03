/* Row 1 (d16) vocab guard, frontend B: Overview, Brands, Brand, Show,
   Compare, Alerts, Campaigns, Saved, Settings rendered from fixtures.
   Customers see "ad" as the unit; "read", "detection", "ad break",
   "pod" and "spot" never appear in visible text or in aria-label/title
   attributes, apart from the verb phrases below (D/D1-CONTRACT.md;
   same regex and allowlist as frontend_uat_d1_vocab.test.cjs).
   Server-supplied strings in the fixtures are kept free of these words,
   so a hit is always the page's own copy.
   Run: node --test tests/frontend_uat_d1_vocab_b.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const F = require('./frontend_uat_d1b_fixtures.cjs');

// Same BANNED regex, ALLOW list, decode and Ad-break label as d1fa's
// tests/frontend_uat_d1_vocab.test.cjs (keep the two in step). These pages
// are not the Evidence page, so Technical details are never spared here.
const BANNED = /\b(reads?|detections?|ad breaks?|pods?|spots?)\b/i;
// Verb phrases, each a full phrase (the verb "read", never the unit).
const ALLOW = [
  'read-only', // a view that cannot change anything
  'Mark all read', // alerts: mark events as seen
  'Web address read out', // fact row: the address was spoken
  'Promo code read out', // fact row: the code was spoken
  'the brand’s code or address was read out', // name certainty gloss
  'not read automatically', // row 13 copy
  'alerts read it', // coverage tier: alerts scan the transcript
];
const decode = s => s.replace(/&nbsp;/g, ' ').replace(/&middot;/g, '·').replace(/&rsquo;/g, '’').replace(/&ldquo;|&rdquo;/g, '"')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
// "Ad break · <range> · N ads" over N >= 2 ads is the one allowed label;
// on Search, "Ad break · N brands" over N >= 2 brands (NOGAP-AB, DET-1).
const AD_BREAK_LABEL = /<h3 class="ew-adbreak-h">Ad break [\s\S]*?\s(\d+) (?:ads|brands)<\/h3>/g;
function surfaces(html) {
  let h = String(html).replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/g, ' ');
  h = h.replace(AD_BREAK_LABEL, (m, n) => (Number(n) >= 2 ? ' ' : m));
  const attrs = [...h.matchAll(/\s(?:aria-label|title)="([^"]*)"/g)].map(m => m[1]);
  const meta = [...h.matchAll(/<meta name="description" content="([^"]*)"/g)].map(m => m[1]);
  const visible = h.replace(/<[^>]+>/g, ' ');
  return [visible, ...attrs, ...meta].map(decode);
}
function hits(html) {
  const out = [];
  for (let s of surfaces(html)) {
    s = s.replace(/\s+/g, ' ');
    for (const phrase of ALLOW) s = s.split(new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi')).join(' ');
    const re = new RegExp(BANNED.source, 'gi');
    for (const m of s.matchAll(re)) out.push(s.slice(Math.max(0, m.index - 50), m.index + 40).trim());
  }
  return out;
}
const guard = async (name, route, api) => {
  const { html } = await F.render(route, api);
  assert.ok(html && html.length > 200, `${name} rendered`);
  assert.deepEqual(hits(html), [], `${name}: banned unit words`);
};

const cleanCodes = [{ code: 'unreviewed_detections_in_scope', text: 'no reviewed match yet; ads in scope wait for review' }];
const unreviewed = () => F.draft(F.COMPLETE(2), 'unreviewed_detections_in_scope', cleanCodes);
const events = [F.ev(1, 'epA', [10, 40], 'brk:a', { read_state: 1 }), F.ev(2, 'epA', [30, 60], 'brk:a'), F.ev(3, 'epB', [10, 40], 'brk:c', { summary: null })];

test('vocab b: Overview', () => guard('Overview', '#/overview', F.overviewApi()));
// R2-01/R2-02: campaign gaps nested under a tracked campaign and as stray rows.
// WIN3: Overview lists the placements the campaign page's "Needs
// attention" filter lists, so a placement with an ad found is not there;
// its row here waits for review instead.
const OV_ATTENTION = F.OV_GAPS.map(g => (g.linkage === 'possible'
  ? { ...g, outcome: 'unreviewed_detections_in_scope', linkage: 'unreviewed', label: '2 ads waiting for review' } : g));
test('vocab b: Overview, campaign gaps', async () => {
  const api = F.overviewApi({ ov: F.overview({ campaign_gaps: OV_ATTENTION, tracked_campaigns: F.OV_TRACKED }) });
  const { html } = await F.render('#/overview', api);
  const t = F.text(html);
  assert.match(t, /2 ads waiting for review/);
  assert.match(t, /No matching ad in the available transcript/);
  assert.match(t, /expects 2 ads/);
  assert.match(t, /expects 1 ad\b/);
  await guard('Overview gaps', '#/overview', api);
});
test('vocab b: the guard catches the older gap wording', async () => {
  const old = OV_ATTENTION.map(g => ({ ...g, label: g.linkage === 'possible' ? 'Possible detection' : 'No matching detection in the available transcript',
    placement_label: g.placement_label.replace(/expects (\d+) ads?/, (m, n) => `expects ${n} read${n === '1' ? '' : 's'}`) }));
  const { html } = await F.render('#/overview', F.overviewApi({ ov: F.overview({ campaign_gaps: old, tracked_campaigns: F.OV_TRACKED }) }));
  assert.ok(hits(html).length >= 4, 'old server labels would be caught');
});
test('vocab b: Overview, older server', () => guard('Overview old', '#/overview', F.overviewApi({ brands: F.OLD_DIRBRANDS, ov: F.overview({ alert_totals: null, recent_events: [] }) })));
test('vocab b: Overview, first use', () => guard('Overview first use', '#/overview', p => (p.startsWith('/customer/saved-items') ? { items: [] }
  : F.overviewApi({ ov: F.overview({ alert_totals: [], alert_lifecycle_counts: {} }), items: [] })(p))));
test('vocab b: Brands', () => guard('Brands', '#/brands', F.dirApi(F.DIRBRANDS)));
test('vocab b: Brands, older server', () => guard('Brands old', '#/brands', F.dirApi(F.OLD_DIRBRANDS)));
test('vocab b: Brand', () => guard('Brand', '#/brand/chatgpt', F.brandApi()));
test('vocab b: Brand, no ads yet', () => guard('Brand empty', '#/brand/chatgpt', F.brandApi(F.brandDetail({ offer_sequence: [] }), [], { total: 0, break_total: 0 })));
test('vocab b: Show', () => guard('Show', '#/show/cs-1', F.showApi()));
test('vocab b: Compare', () => guard('Compare', '#/compare?brands=chatgpt,progressive', F.compareApi()));
test('vocab b: Compare, all zero', () => guard('Compare zero', '#/compare?brands=chatgpt,progressive', F.compareApi({ cells: [{ brand: 'chatgpt', a: { count: 0, ads: 0 } }], shared_scale: 0 })));
test('vocab b: Alerts list', () => guard('Alerts', '#/alerts', F.alertApi(F.ALERT, events)));
test('vocab b: Alert', () => guard('Alert', '#/alert/1', F.alertApi(F.ALERT, events)));
test('vocab b: Alert, words', () => guard('Alert words', '#/alert/1', F.alertApi({ ...F.ALERT, query: { version: 2, mode: 'mentions', q: 'chatgpt' },
  current_matches: { count: 16, tracked: 34, breaks: 7, tracked_by_kind: { new_text: 16, new_finding: 18 }, held_for_brand_review: 2 } }, events)));
test('vocab b: Campaigns list', () => guard('Campaigns', '#/campaigns', F.campaignApi(unreviewed())));
test('vocab b: Campaign', () => guard('Campaign', '#/campaign/6', F.campaignApi(unreviewed())));
test('vocab b: Campaign, results', () => guard('Campaign results', '#/campaign/6', F.campaignApi(F.draft(F.COMPLETE(0), 'supported'))));
test('vocab b: Placement', () => guard('Placement', '#/campaign/6/placement/' + encodeURIComponent(F.PID), F.campaignApi(unreviewed())));
test('vocab b: Report', () => guard('Report', '#/campaign/6/report/1', F.campaignApi(F.draft(F.COMPLETE(0), 'no_observation_in_covered_sources'))));
test('vocab b: Saved', () => guard('Saved', '#/saved?tab=bookmarks', F.savedApi));
test('vocab b: Settings', () => guard('Settings', '#/settings', F.settingsApi));

test('vocab b: the guard catches the words it bans and lets the verbs through', () => {
  assert.equal(hits('<p>3 reads heard</p>').length, 1);
  assert.equal(hits('<a aria-label="Play the read">x</a>').length, 1);
  assert.equal(hits('<p>Possible detections</p>').length, 1);
  assert.equal(hits('<p>2 ad breaks</p>').length, 1);
  assert.deepEqual(hits('<p>Read-only campaign. Mark all read. Web address read out. The brief text is not read automatically.</p>'), []);
  assert.equal(hits('<h3 class="ew-adbreak-h">Ad break · 1:00 · 1 ads</h3>').length, 1, 'never over one ad');
  assert.deepEqual(hits('<h3 class="ew-adbreak-h">Ad break · 1:00 · 2 ads</h3>'), []);
  assert.equal(hits('<details class="tech-details"><p>read 5</p></details>').length, 1, 'Technical details are spared only on the Evidence page');
});
