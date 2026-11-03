/* Row 1 (d16) frontend B fixtures: Overview, Brands, Brand, Show, Compare,
   Alerts, Campaigns, Saved, Settings. Server-shaped per D/D1-CONTRACT.md:
   the wire names stay (ad_breaks, breaks, break_total, ...) and their
   values are ads; ad_id / unread_ads / ads are the new fields.
   Shared by frontend_uat_d1b.test.cjs and frontend_uat_d1_vocab_b.test.cjs.
   Loader copied from frontend_uat_dec_d1ui.test.cjs. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = process.env.EW_APP_DIR || path.join(__dirname, '..', 'app');
const TIMERS = new Set();
const stopTimers = () => { for (const t of TIMERS) clearTimeout(t); TIMERS.clear(); };

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, clearTimeout, URL, Intl,
    setTimeout: (fn, ms, ...a) => { const t = setTimeout(fn, ms, ...a); TIMERS.add(t); return t; },
    location: { hash: route, href: 'http://127.0.0.1:9153/app/' + route, origin: 'http://127.0.0.1:9153', pathname: '/app/' },
    history: { replaceState() {} },
    scrollTo() {},
    fetch: fetch || (async () => { throw new Error('no network in unit test'); }),
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
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7,
      frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
async function render(route, api) {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  const modal = { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? modal : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  stopTimers();
  return { html: main.innerHTML, win };
}
function loadViews() {
  const win = loadAt('#/settings', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(fs.readFileSync(path.join(APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return win;
}
const text = html => String(html).replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ');

/* -- Brands / Brand / Show -- */
const DIRBRANDS = [
  { key: 'chatgpt', display: 'ChatGPT', brand: 'chatgpt', records: 13, ad_breaks: 7, confirmed: 2, confirmed_ads: 1, shows: 3, last_heard: '2026-10-09' },
  { key: 'progressive', display: 'Progressive', brand: 'progressive', records: 14, ad_breaks: 13, confirmed: 0, confirmed_ads: 0, shows: 5, last_heard: '2026-10-01' },
  { key: 'qvc', display: 'QVC', brand: 'qvc', records: 9, ad_breaks: 7, confirmed: 0, confirmed_ads: 0, shows: 1, last_heard: '2026-10-02' }];
const OLD_DIRBRANDS = DIRBRANDS.map(({ ad_breaks, ...b }) => b);
const dirApi = brands => p => {
  if (p === '/customer/brands' || p.startsWith('/customer/brands?')) return { brands };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
const REF = n => 'sr1:' + String(n).padEnd(32, 'a');
const brandItem = (n, adId, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'confirmed', show_title: 'Show A', ep_title: 'Episode ' + n,
  episode_id: 'e' + n, pub_date: '2026-10-0' + n, audio: 'unavailable', quotation_span: [60, 120], ad_break_id: adId,
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: true }, display_span: [60, 120], display_text: 'Try ChatGPT today ' + n, has_clip: false, said: { web_addresses: [], promo_codes: [] } },
  attribution: [], ...over });
const brandDetail = (over = {}) => ({ brand: { brand: 'chatgpt', key: 'chatgpt', display: 'ChatGPT', detections: 13, ad_breaks: 7,
  confirmed_occurrences: ['sr1:c1', 'sr1:c2'], findings: [], offer_sequence: [{ signature: 'sig1', refs: ['sr1:c1', 'sr1:c2'] }], episodes: 6, shows: 3, last_heard: '2026-10-09',
  drill_down: { ads: { version: 2, mode: 'ads', q: '', brands: ['chatgpt'], shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null }, mentions: {} },
  top_shows: [{ show_id: 'cs-a', display_title: 'Show A', reads: 3, ad_breaks: 2, confirmed: 0, confirmed_ads: 0, last_heard: '2026-10-09' }], reads_without_show: 2, ...over } });
const BRAND_ITEMS = [brandItem(1, 'brk:a'), brandItem(2, 'brk:a'), brandItem(3, 'brk:b')];
const brandApi = (detail = brandDetail(), items = BRAND_ITEMS, search = { total: 13, break_total: 7 }) => p => {
  if (p === '/customer/brands/chatgpt') return detail;
  if (p.startsWith('/customer/search?')) return { items, total_relation: 'exact', ...search };
  if (p.startsWith('/customer/summary?')) return { cells: [], total_relation: 'exact' };
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
const SHOW = { show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null, unresolved: false,
  coverage: { episodes: 2, episodes_with_transcript: 2 },
  sponsors: [{ key: 'ag1', display: 'AG1', episodes: 2, ads: 6, ad_breaks: 4, first_seen: '2026-09-01', last_seen: '2026-10-09' },
    { key: 'zeta', display: 'Zeta', episodes: 1, ads: 1, ad_breaks: 1, first_seen: '2026-09-01', last_seen: '2026-09-01' }],
  sponsors_not_counted: { under_review: 1, unnamed: 2 },
  episodes: [{ id: 'e1', ep_title: 'One', pub_date: '2026-10-09', transcript: true, ads: 7, ad_breaks: 5, confirmed: 2, screening: SCREENED },
    { id: 'e2', ep_title: 'Two', pub_date: '2026-10-02', transcript: true, ads: 1, ad_breaks: 1, confirmed: 0, screening: SCREENED }] };
const showApi = (show = SHOW) => p => (p === '/customer/shows/cs-1' ? show : p.startsWith('/customer/saved-items') ? { items: [] } : new Error('unexpected ' + p));

/* -- Compare -- */
const compareData = (withAds = true) => {
  const part = (count, ads) => ({ count, ...(withAds ? { ads } : {}), refs: ['sr1:x'], coverage: { matched_in_scope: count }, shows: 2, last_heard: '2026-10-09',
    selector: { version: 1, base_query: { version: 2, mode: 'ads' }, clause: { kind: 'compare_cell' } } });
  return { cells: [{ brand: 'chatgpt', a: part(4, 3) }, { brand: 'progressive', a: part(2, 2) }], shared_scale: 4 };
};
const compareApi = (data = compareData(), brands = DIRBRANDS) => (p) => {
  if (p === '/customer/brands') return { brands };
  if (p === '/customer/compare' || p.startsWith('/customer/compare')) return data;
  if (p.startsWith('/customer/saved-items')) return { items: [] };
  return new Error('unexpected ' + p);
};

/* -- Overview -- */
const ovItem = (n, adId, over = {}) => ({ ref: REF(n), record_kind: 'evidence', status: 'finding', show_title: 'Show A', ep_title: 'Episode ' + n,
  episode_id: 'e1', pub_date: '2026-10-09', ad_break_id: adId, text: 'Get ChatGPT plus now.',
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: true }, display_span: [60, 120] }, attribution: [], ...over });
const OV_TOTALS = [{ alert_id: 3, alert_name: 'ChatGPT', lifecycle: 'active_local', events: 5, unread: 4, ads: 3, unread_ads: 2,
  latest_kind: 'new_finding', latest_observed_at: '2026-10-14T09:00:00Z', latest_after_ref: REF(1), query: { version: 2, mode: 'ads', brands: ['chatgpt'] } }];
const overview = (over = {}) => ({ coverage: { episodes: 2, shows: 1, episodes_with_transcript: 2 }, recent_events: [], offer_sequence: [],
  campaign_gaps: [], tracked_campaigns: [], activity: [], alert_totals: OV_TOTALS, alert_lifecycle_counts: { active_local: 1, paused: 0, stopped: 0 }, ...over });
// R2-01/R2-02: Overview "Needs attention" rows, worded as the server sends
// them (placement_linkage_label, _placement_scope_label).
const OV_GAPS = [
  { campaign_id: 6, campaign_name: 'Shopify check', placement_id: 'exp:aaaa00000001', outcome: 'supported', linkage: 'possible', label: 'Ad found',
    placement_label: 'The Long Run · Oct 1 to Oct 31, 2026 · expects 2 ads', placement_href: '#/campaign/6/placement/exp:aaaa00000001' },
  { campaign_id: 6, campaign_name: 'Shopify check', placement_id: 'exp:aaaa00000002', outcome: 'no_observation_in_covered_sources', linkage: 'none',
    label: 'No matching ad in the available transcript', placement_label: 'Episode: Mitrice Richardson Part 2', placement_href: '#/campaign/6/placement/exp:aaaa00000002' },
  { campaign_id: 7, campaign_name: 'Quince launch', placement_id: 'exp:aaaa00000003', outcome: 'no_observation_in_covered_sources', linkage: 'none',
    label: 'No matching ad in the available transcript', placement_label: 'Show window · expects 1 ad', placement_href: '#/campaign/7/placement/exp:aaaa00000003' }];
const OV_TRACKED = [{ id: 6, name: 'Shopify check', state: 'Tracking', brand: 'shopify', brand_display: 'Shopify' }];
const OV_ITEMS = [ovItem(1, 'brk:a'), ovItem(2, 'brk:a'), ovItem(3, 'brk:b'), ovItem(4, 'brk:c'), ovItem(5, 'brk:d')];
const overviewApi = ({ ov = overview(), brands = DIRBRANDS, items = OV_ITEMS } = {}) => p => {
  if (p === '/customer/overview') return ov;
  if (p.startsWith('/customer/saved-items')) return { items: [{ id: 'si-1', type: 'bookmark', revision: 1, target: { kind: 'brand', id: 'chatgpt' } }] };
  if (p === '/customer/brands') return { brands };
  if (p.startsWith('/customer/search')) return { items };
  return new Error('unexpected ' + p);
};

/* -- Alerts -- */
const ALERT = { id: 1, name: 'ChatGPT ads', revision: 1, definition_revision: 1, lifecycle: 'active_local',
  baseline_complete: true, last_successful_at: '2026-10-14T09:00:00Z', query: { version: 2, mode: 'ads', q: '', brands: ['chatgpt'] },
  current_matches: { count: 13, breaks: 7, tracked: 13, tracked_by_kind: { new_finding: 13 }, held_for_brand_review: 0 } };
const SUM = (episode, span, over = {}) => ({ record_kind: 'evidence', status: 'finding', episode_id: episode, show_title: 'NPR News Now', ep_title: 'Ep ' + episode,
  pub_date: '2026-10-09', span, text: 'Try ChatGPT plus today.', historical: false,
  read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: false }, needs_review: false, historical: false }, ...over });
const ev = (id, episode, span, adId, over = {}) => ({ id, kind: 'new_finding', after_ref: REF(id), read_state: 0, historical: false, ad_id: adId,
  observed_at: '2026-10-14T16:27:59+00:00', summary: SUM(episode, span), ...over });
const alertApi = (alert = ALERT, events = []) => p => {
  if (p === '/customer/alerts/1') return { alert: { ...alert, events, unread_event_count: events.filter(e => !e.read_state).length,
    unread_ad_count: new Set(events.filter(e => !e.read_state).map(e => e.ad_id || 'e' + e.id)).size } };
  if (p === '/customer/alerts') return { alerts: [{ ...alert, events: undefined, unread_event_count: 1, unread_ad_count: 1 }] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/brands') return { brands: [{ key: 'chatgpt', display: 'ChatGPT' }] };
  return new Error('unexpected ' + p);
};

/* -- Campaigns -- */
const PID = 'exp:5c876f060783';
const draft = (cov, outcome, codes = [], over = {}) => ({
  campaign: { id: 6, revision: 2, name: 'Shopify check' }, mode: 'supplied_requirements', synthetic: false, synthetic_placements: [],
  placements: [{ id: PID, scope: 'episode', show_id: 's1', episode_id: 's002e1', expected_count: 2, binding: 'eligible', provenance: { binding: 'eligible' } }],
  targets: [], episode_titles: { s002e1: 'Mitrice Richardson Part 2' },
  coverage_by_placement: { [PID]: cov },
  comparisons: { [PID]: [] },
  allocation: { evaluations: { [PID]: { placement_id: PID, outcome, eligible_ids: [], allocated_elsewhere: [], occurrence_order: [],
    reasons: codes.map(c => c.text) } }, citations: {} },
  reason_codes: { evaluations: { [PID]: codes.map(c => ({ code: c.code, field: null })) }, citations: {} },
  assessment_scope: { basis: 'entered_structured_requirements_only', by_unit: { [PID]: { entered_fields: [], assessed_fields: [], unknown_fields: [] } }, limitations: [] },
  pins: { attachments: {}, source_scopes: {}, source_scope_state: {}, attachment_state: {} },
  attachment_pool: [], pool_refs: {}, suggestions: [],
  allowed_actions: ['read', 'edit', 'targets', 'placements', 'freeze', 'reports'],
  ...over,
});
const COMPLETE = n => ({ state: 'complete', missing: [], failed: [], episodes: ['s002e1'], episode_labels: { s002e1: 'Mitrice Richardson Part 2' }, waiting_for_review: n });
const campaignApi = d => p => {
  if (p === '/customer/campaigns/6') return { campaign: { id: 6, mode: 'supplied_requirements', revision: 2, name: 'Shopify check', brand: 'shopify', requirement_provenance: {}, allowed_actions: d.allowed_actions } };
  if (p === '/customer/campaigns/6/draft') return { draft: d };
  if (p === '/customer/campaigns/6/reports') return { reports: [{ report_no: 1, captured_at: '2026-10-01T10:00:00Z', stale: false }] };
  if (p === '/customer/campaigns/6/reports/1') return { stale: false, frozen: { captured_at: '2026-10-01T10:00:00Z', campaign_revision: 2, placements: d.placements, targets: [],
    evaluations: d.allocation.evaluations, allocation: { citations: {} }, coverage_by_placement: d.coverage_by_placement,
    comparisons: d.comparisons, assessment_scope: d.assessment_scope, episode_titles: d.episode_titles, brief_versions: {} } };
  if (p === '/customer/campaigns') return { campaigns: [{ id: 6, name: 'Shopify check', mode: 'supplied_requirements', placement_count: 1, target_count: 0, state: 'Tracking' }] };
  if (p.startsWith('/customer/search')) return { items: [], total: 0 };
  if (p === '/customer/shows') return { shows: [] };
  return new Error('unexpected request ' + p);
};
const UNREVIEWED = () => draft(COMPLETE(2), 'unreviewed_detections_in_scope',
  [{ code: 'unreviewed_detections_in_scope', text: 'no reviewed match yet; reads in scope wait for review' }]);

/* -- Saved / Settings -- */
const SAVED_ITEMS = [{ id: 'si-1', type: 'bookmark', title: 'Mine', revision: 1, availability: 'earlier_copy', availability_reason: 'This is in an earlier copy of the episode. Your bookmark keeps the version you saw.',
  target: { kind: 'episode', id: 'e1', source_ref: 'sr1:a', transcript_version_id: 3 },
  captured: { label: 'Morning brew', show_title: 'Same Title', opens: 'captured_pin' } }];
const savedApi = p => {
  if (p.startsWith('/customer/saved-items')) return { items: SAVED_ITEMS, total: 1, total_relation: 'exact', counts: { excerpt: 0, query: 0, bookmark: 1 }, next_cursor: null };
  return new Error('unexpected ' + p);
};
const settingsApi = p => {
  if (p === '/customer/preferences') return { preferences: { revision: 1, workspace_name: 'Mine' } };
  return {};
};

module.exports = { APP, render, loadViews, loadAt, text, REF,
  DIRBRANDS, OLD_DIRBRANDS, dirApi, brandItem, brandDetail, BRAND_ITEMS, brandApi, SCREENED, SHOW, showApi,
  compareData, compareApi, ovItem, overview, OV_ITEMS, OV_TOTALS, OV_GAPS, OV_TRACKED, overviewApi,
  ALERT, SUM, ev, alertApi, PID, draft, COMPLETE, campaignApi, UNREVIEWED, SAVED_ITEMS, savedApi, settingsApi };
