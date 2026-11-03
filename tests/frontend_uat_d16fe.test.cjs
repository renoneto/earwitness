/* d16 round, worker fe: decision 9 (fact row wording), decision 13 (placement
   with no checks), decision 14 tags (alert change rows "New"/"Seen").
   Loader copied from frontend_dv3_ux_alerts.test.cjs.
   Run: node --test tests/frontend_uat_d16fe.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');

function loadAt(route) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController,
    location: { hash: route },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') {
        return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
          csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
          frontend_contract: 'earwitness/customer-ui/2' }) };
      }
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
      createElement: () => ({ dataset: {}, appendChild: () => {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) {
    vm.runInContext(fs.readFileSync(path.join(APP, file), 'utf8'), sandbox, { filename: file });
  }
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
async function renderRoute(route, api) {
  const win = loadAt('#/overview');
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.EW.header = () => {};
  win.EW.api = async (p) => api(p);
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  await flushBoot();
  return main.innerHTML;
}
const visible = html => String(html).replace(/<details class="tech-details">[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ');
function ledger(html) {
  const rows = {};
  for (const m of html.matchAll(/<th scope="row">([^<]*)<\/th><td[^>]*>([\s\S]*?)<\/td><\/tr>/g)) rows[m[1]] = m[2];
  return rows;
}

/* -- Decision 9: the fact row -- */
const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const EV_ID = 'named:ep_g2.named.120000-180000';
const read = (over = {}) => ({ kind: 'named', source_label: 'Named read',
  brand: { key: 'northwind mattress', display: 'Northwind Mattress', display_basis: 'brand_list', suggested: 'northwind mattress', confirmed: false },
  needs_review: false, self_promo: false, self_promo_reason: null, confidence: 'high', display_span: [120, 170], has_clip: false,
  said: { web_addresses: [], promo_codes: [], address_brand: null, code_brand: null, spoken_brand: 'Northwind Mattress' }, ...over });
const record = (over = {}) => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [{ brand: 'northwind mattress', basis: 'suggested', channel: 'spoken' }],
  relationship: 'current', availability: 'current', bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_g2', show_title: 'Quiet Hours', ep_title: 'Why we wake at 3am', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: EV_ID, episode_id: 'ep_g2', orig_start_s: 110, orig_end_s: 180 }, media: { playback: 'remote', alignment: 'unknown' },
  read: read(),
  window: { segments: [{ segment_id: 2, start_s: 120, end_s: 150, text: 'Head to the site to find yours.', text_start: 0, text_end: 31 }],
    text_complete: true, next_cursor: null }, ...over });
const evApi = rec => p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: rec };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  throw new Error('unexpected ' + p);
};

// PLAIN-ADS (DEP7) replaces D9: the ledger has no Brand name review row
// and no Name certainty, whatever the brand's review.
test('D9 the ledger has no Brand name review row and no Name certainty', async () => {
  const html = await renderRoute('#/evidence/' + encodeURIComponent(REF), evApi(record()));
  assert.equal(ledger(html)['Brand name review'], undefined);
  assert.doesNotMatch(html, /Name certainty|review team|Not flagged for review/);
  // PLAIN-ADS-FIX2 (screen R2-02): no address or code was read out, so neither is listed.
  assert.deepEqual(Object.keys(ledger(html)), ['Brand heard in the audio', 'How the time was found']);
});

test('D9 confirmed and needs-review reads read the same, with no review words', async () => {
  const conf = await renderRoute('#/evidence/' + encodeURIComponent(REF),
    evApi(record({ read: read({ brand: { ...read().brand, confirmed: true } }) })));
  const need = await renderRoute('#/evidence/' + encodeURIComponent(REF), evApi(record({ read: read({ needs_review: true }) })));
  for (const html of [conf, need]) {
    assert.equal(ledger(html)['Brand name review'], undefined);
    assert.doesNotMatch(html, /Needs review|review team|Confirmed/);
  }
});

/* -- Decision 13: a placement with no checks entered -- */
const NO_CHECKS = 'No checks entered for this placement. The brief text is not read automatically; add Message, Offer, Code or Length to check them.';
const PID = 'exp:5ba6bde24476', OCC = 'occ:5d1a3855b029', SHOW = 'cs-1a2b3c4d5e6f';
const draft = () => ({
  campaign: { id: 3, revision: 16, name: 'Lumen launch' }, mode: 'supplied_requirements',
  placements: [{ id: PID, scope: 'show_window', show_id: SHOW, date_from: '2026-01-01', date_to: '2026-02-28', expected_count: 1, revision: 1, provenance: {} }],
  targets: [], episode_titles: {},
  allocation: { evaluations: { [PID]: { outcome: 'ambiguous', eligible_ids: [OCC], allocated_elsewhere: [], reasons: [] } }, citations: {} },
  reason_codes: { evaluations: { [PID]: [] }, citations: {} },
  coverage_by_placement: { [PID]: { state: 'complete' } }, comparisons: {},
  assessment_scope: { by_unit: { [PID]: { entered_fields: [], assessed_fields: [], unknown_fields: [] } } },
  pins: { source_scopes: {}, source_scope_state: {}, attachments: {}, attachment_state: {} },
  attachment_pool: [], pool_refs: {}, suggestions: [],
});
const campApi = p => {
  const d = draft();
  const frozen = { captured_at: '2026-10-01T10:00:00Z', campaign_revision: 16, placements: d.placements, targets: [],
    evaluations: d.allocation.evaluations, allocation: { citations: {} }, coverage_by_placement: d.coverage_by_placement,
    comparisons: d.comparisons, assessment_scope: d.assessment_scope, episode_titles: {}, brief_versions: {} };
  const table = {
    '/customer/campaigns/3': { campaign: { id: 3, mode: 'supplied_requirements', revision: 16, name: 'Lumen launch' } },
    '/customer/campaigns/3/draft': { draft: d },
    '/customer/campaigns/3/reports': { reports: [{ report_no: 1, captured_at: '2026-10-01T10:00:00Z', stale: false }] },
    '/customer/campaigns/3/reports/1': { frozen, stale: false },
    ['/customer/shows/' + SHOW]: { display_title: 'Morning Show', episodes: [] },
    '/customer/shows': { shows: [{ id: SHOW, display_title: 'Morning Show' }] },
  };
  if (Object.hasOwn(table, p)) return table[p];
  throw new Error('unexpected ' + p);
};

test('D13 the placement page says what no checks means, in the new words', async () => {
  const html = await renderRoute('#/campaign/3/placement/' + encodeURIComponent(PID), campApi);
  assert.match(html, /<h1/);
  assert.ok(visible(html).includes(NO_CHECKS), visible(html));
  assert.doesNotMatch(html, /nothing here was assessed|No checks were entered/);
});

test('D13 the report page says the same, in the new words', async () => {
  const html = await renderRoute('#/campaign/3/report/1', campApi);
  assert.ok(visible(html).includes(NO_CHECKS), visible(html));
  assert.doesNotMatch(html, /nothing here was assessed|No checks were entered/);
});

test('D13 a placement with entered checks keeps "Checked against the brief"', async () => {
  const api = p => {
    const r = campApi(p);
    if (p === '/customer/campaigns/3/draft') {
      r.draft.assessment_scope.by_unit[PID] = { entered_fields: ['message'], assessed_fields: ['message'], unknown_fields: [] };
    }
    return r;
  };
  const html = await renderRoute('#/campaign/3/placement/' + encodeURIComponent(PID), api);
  assert.match(visible(html), /Checked against the brief: Message\./);
  assert.ok(!visible(html).includes(NO_CHECKS));
});

/* -- Decision 14: New / Seen tags on alert change rows -- */
const ALERT = { id: 1, name: 'Free trial wording', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: 'free trial' },
  current_matches: { count: 2, tracked: 2, tracked_by_kind: { new_finding: 2 }, held_for_brand_review: 0 } };
const SUM = (episode, span) => ({ record_kind: 'evidence', status: 'finding', episode_id: episode, show_title: 'NPR News Now', ep_title: episode,
  pub_date: '2026-10-09', span, text: 'You can try it free trial today.', historical: false,
  read: { brand: { key: 'betterhelp', display: 'BetterHelp', confirmed: false }, needs_review: false, historical: false } });
const ev = (id, episode, span, over = {}) => ({ id, kind: 'new_finding', after_ref: 'sr1:' + id, read_state: 0, historical: false,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM(episode, span), ...over });
const alertHtml = events => renderRoute('#/alert/1', p => {
  // Server shape: unread_ad_count = distinct ad_ids plus one per event with none (customer._ad_tally).
  if (p === '/customer/alerts/1') { const un = events.filter(e => !e.read_state);
    return { alert: { ...ALERT, events, unread_event_count: un.length, unread_ad_count: new Set(un.filter(e => e.ad_id).map(e => e.ad_id)).size + un.filter(e => !e.ad_id).length } }; }
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [] };
  throw new Error('unexpected ' + p);
});
const tagsOf = html => [...html.matchAll(/<span class="ew-tag [^"]*" data-tag="(info|neutral)">([^<]*)<\/span>/g)].map(m => m[2]);

test('D14 an unread change row is tagged New and a read one Seen, with the existing tag component', async () => {
  const html = await alertHtml([ev(1, 'epA', [10, 40]), ev(2, 'epB', [10, 40], { read_state: 1 })]);
  const rows = html.split('<li class="inbox-event').slice(1);
  assert.equal(rows.length, 2);
  const [unreadRow, readRow] = rows.map(r => r.slice(0, r.indexOf('</li>')));
  assert.match(unreadRow, /<span class="ew-tag ew-tag-info" data-tag="info">New<\/span>/);
  assert.match(readRow, /<span class="ew-tag ew-tag-neutral" data-tag="neutral">Seen<\/span>/);
  assert.doesNotMatch(html, />Unread<|>Read<|style=/);
});

test('D14 in an episode group each row carries its own New or Seen tag', async () => {
  const html = await alertHtml([ev(1, 'epA', [10, 40]), ev(2, 'epA', [100, 140], { read_state: 1 })]);
  assert.match(html, /ew-work-event-group/);
  assert.deepEqual(tagsOf(html).filter(t => t === 'New' || t === 'Seen').sort(), ['New', 'Seen']);
});

test('D14 a folded passage that is still unread keeps its host row New', async () => {
  const host = ev(1, 'epA', [10, 100], { read_state: 1 });
  const inside = ev(2, 'epA', [20, 30], { kind: 'new_text' });
  const html = await alertHtml([host, inside]);
  const hostRow = html.split('<li class="inbox-event').slice(1).find(r => r.includes('data-event-id="1"'));
  assert.match(hostRow, />New</, 'host row shows New while its folded passage is unread');
  assert.doesNotMatch(html, />Unread</);
});

test('D14 an event with no summary gets the tag too; Mark all read and the unread count stay', async () => {
  const html = await alertHtml([ev(1, 'epA', [10, 40], { summary: null }), ev(2, 'epB', [10, 40], { read_state: 1, summary: null })]);
  assert.deepEqual(tagsOf(html).filter(t => t === 'New' || t === 'Seen').sort(), ['New', 'Seen']);
  assert.match(html, /<strong>1<\/strong> unread/);
  assert.match(html, /Mark all read/);
});

test('D14 with every change read, every row says Seen and no row says New', async () => {
  const html = await alertHtml([ev(1, 'epA', [10, 40], { read_state: 1 })]);
  assert.ok(tagsOf(html).includes('Seen'));
  assert.ok(!tagsOf(html).includes('New'));
});
