/* SCREENED: the Episode page and the Show page episode rows take their
   label from Fresh's one screened definition (screen_check.state:
   no_transcript, not_screened, screened, fully_checked) and fall back to
   the older screening object only when screen_check is null. The label
   never claims more than the state: "no ads found" only when fully
   checked. NEW words (R/ws-app/SCREENED-STRINGS.md) stay off until the
   lead approves them. Loader helpers copied from frontend_dv3_g2.test.cjs.
   Run: node --test tests/frontend_screened.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP = path.join(__dirname, '..', 'app');
const VIEWS = fs.readFileSync(path.join(APP, 'views.js'), 'utf8');

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
async function render(route, api, { src = VIEWS } = {}) {
  const win = loadAt(route);
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  vm.runInContext(src, win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.EW.header = () => {};
  win.EW.api = async (p, o) => { const out = api(p, o); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  return main.innerHTML;
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&rsquo;|’/g, '’').replace(/\s+/g, ' ');

/* The four states, as coverage.episode_screen_state sends them. */
const CHECK = {
  fully_checked: { state: 'fully_checked', screened: true, fully_checked: true, transcript_short_s: 2.0, not_fully_checked: [] },
  screened: { state: 'screened', screened: true, fully_checked: false, transcript_short_s: 41.0, not_fully_checked: ['transcript_short'] },
  not_screened: { state: 'not_screened', screened: false, fully_checked: false, transcript_short_s: 0.0, not_fully_checked: ['not_screened'] },
  no_transcript: { state: 'no_transcript', screened: false, fully_checked: false, transcript_short_s: null, not_fully_checked: ['no_transcript'] },
};
// The older screening object: here it always claims screened with no ads,
// so only the state can make the label say less.
const OLD = { label: 'Screened, no ads found', jev_screened: true, screened_no_ads: true, stopped_partway: false,
  coverage_tier: 'full', transcript_only: false, stage: null };
const STOPPED = { label: 'Stopped partway (transcribe)', jev_screened: false, screened_no_ads: false, stopped_partway: true,
  coverage_tier: 'full', transcript_only: false, stage: 'transcribe' };
const NEW_WORDS = /transcript ends|not every check on this episode finished|so an ad could be missing|so an ad there could be missed/;
const N2 = /not every check on this episode finished/;

const REC = { ref: 'ep:ep_s', source_ref: 'sr1:e5e5', canonical_source_ref: 'sr1:e5e5', record_kind: 'source_publication', relationship: 'current',
  episode: { episode_id: 'ep_s', transcript_version_id: 3, ep_title: 'Screened state', show_title: 'The Long Run', pub_date: '2026-10-09', duration_min: 58 },
  show: { show_identity_id: 'cs-1', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3480 }, selected_bounds: { start_s: 0, end_s: 3480 }, media: {},
  window: { segments: [{ segment_id: 1, start_s: 60, end_s: 70, text: 'Quick break.', text_start: 0, text_end: 12 }], text_complete: true, next_cursor: null } };
const AD = { ref: 'sr1:' + 'a'.repeat(32), evidence_id: 'a1', status: 'finding', span: [60, 70], display_span: [60, 70],
  brand: { key: 'ag1', display: 'AG1', display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'named', source_label: 'Named read', transcript_start_s: 60, current_transcript: true, transcript_version_id: 3, media: null };
function episodeOf({ check, screening = OLD, ads = [] }) {
  const ep = { episode_id: 'ep_s', ads, self_promo: [], current_transcript_version_id: 3, earlier_transcripts: [],
    ad_breaks: ads.map((a, i) => ({ id: 'b' + i, start_s: a.span[0], end_s: a.span[1], reads: [a.evidence_id] })),
    ad_break_counts: { current: ads.length }, screening,
    screened_no_ads: !!(screening && screening.screened_no_ads) && !ads.length };
  if (check !== undefined) ep.screen_check = check;
  return ep;
}
const episodeApi = (episode, rec = REC) => p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_s')) return { record: rec };
  if (p === '/customer/episodes/ep_s') return { episode };
  return new Error('unexpected ' + p);
};
const statusLine = html => (html.match(/<p class="ew-episode-status">[\s\S]*?<\/p>/) || [''])[0];
const adsState = html => (html.match(/<p class="ew-ads-state">[\s\S]*?<\/p>|<div class="ew-notice ew-notice-stopped"[\s\S]*?<\/div>/) || [''])[0];
const aboutAds = html => (html.match(/<dt>Ads<\/dt><dd>([^<]*)<\/dd>/) || ['', ''])[1];

/* -- Episode page --------------------------------------------------------- */

test('fully checked with no ads: the full words, and the only "no ads found"', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.fully_checked })));
  assert.match(statusLine(html), /data-tag="tier"/);
  assert.match(statusLine(html), /Transcript and ads checked\./);
  assert.doesNotMatch(html, /Not fully checked/);
  assert.match(adsState(html), /Screened, no ads found\./);
  assert.equal(aboutAds(html), 'Screened, no ads found');
});

test('screened only: never reads as fully checked, never "no ads found"', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened })));
  const line = statusLine(html);
  assert.match(line, /Not fully checked/);
  assert.match(line, /Ads checked\./);
  assert.doesNotMatch(line, /Transcript and ads checked|Full coverage|data-tag="tier"/);
  assert.doesNotMatch(text(html), /no ads found/i);
  assert.match(adsState(html), /No ads are listed for this episode\./);
  assert.equal(aboutAds(html), 'Screened');
  assert.doesNotMatch(text(html), N2, 'N2 is not approved');
});

test('screened only with ads listed: the badge still says not fully checked', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened, screening: { ...OLD, screened_no_ads: false, label: 'Screened' }, ads: [AD] })));
  assert.match(statusLine(html), /Not fully checked/);
  assert.match(html, /<span class="ew-ads-count">1 ad<\/span>/);
});

test('not screened by the state, although the older record says screened', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.not_screened })));
  assert.match(statusLine(html), /Transcript ready\. Ads not checked yet\./);
  assert.doesNotMatch(statusLine(html), /data-tag="tier"/);
  assert.match(adsState(html), /This episode has not been screened for ads yet, so its ads are unknown for now\./);
  assert.doesNotMatch(text(html), /no ads found|Transcript and ads checked/i);
  assert.equal(aboutAds(html), 'Not screened');
});

test('not screened and stopped partway: the stop is still labelled', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.not_screened, screening: STOPPED })));
  assert.match(statusLine(html), /Processing stopped partway/);
  assert.match(adsState(html), /Processing stopped partway, at transcription\./);
  assert.equal(aboutAds(html), 'Stopped partway · transcription');
});

test('no transcript: never the not-screened "Transcript ready" words', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.no_transcript })));
  const line = statusLine(html);
  assert.match(line, /Ads not checked yet\./);
  assert.doesNotMatch(line, /Transcript ready|Transcript and ads checked/);
  assert.doesNotMatch(text(html), /no ads found/i);
});

test('screen_check null: the older screening logic still runs', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: null })));
  assert.match(statusLine(html), /Transcript and ads checked\./);
  assert.match(adsState(html), /Screened, no ads found\./);
});

/* -- Show page ------------------------------------------------------------ */

const row = (id, check, over = {}) => ({ id, ep_title: id, pub_date: '2026-10-0' + id.slice(1), transcript: true, ads: 0,
  screening: OLD, ...(check === undefined ? {} : { screen_check: check }), ...over });
const SHOW = (episodes, sponsors = []) => ({ show_id: 'cs-1', display_title: 'The Long Run', category_summary: [], artwork_url: null,
  unresolved: false, coverage: { episodes: episodes.length, episodes_with_transcript: episodes.length }, sponsors,
  sponsors_not_counted: {}, episodes });
const showApi = show => p => (p === '/customer/shows/cs-1' ? show : new Error('unexpected ' + p));
const epRowOf = (html, id) => (html.match(new RegExp(`<li class="lib-ep">(?:(?!</li>).)*#/episode/${id}"(?:(?!</li>).)*</li>`)) || [''])[0];
const sponsorEmpty = html => (html.match(/<p class="lib-sponsor-empty">([^<]*)<\/p>/) || ['', ''])[1];

test('Show rows take their word from screen_check.state', async () => {
  const html = await render('#/show/cs-1', showApi(SHOW([
    row('e1', CHECK.fully_checked),
    row('e2', CHECK.screened),
    row('e3', CHECK.screened, { ads: 2, ad_breaks: 2, screening: { ...OLD, screened_no_ads: false } }),
    row('e4', CHECK.not_screened),
    row('e5', CHECK.not_screened, { screening: STOPPED }),
    row('e6', CHECK.no_transcript),
    row('e7', null),
  ])));
  const r = id => epRowOf(html, id);
  assert.match(r('e1'), />Screened · no ads found</);
  assert.doesNotMatch(r('e1'), /Not fully checked/);
  assert.match(r('e2'), />Screened · no ads listed</);
  assert.match(r('e2'), /<span class="lib-row-sub">Not fully checked · transcript ends 41 s early<\/span>/);
  assert.match(r('e3'), />Screened · 2 ads listed</);
  assert.match(r('e3'), /<span class="lib-row-sub">Not fully checked · transcript ends 41 s early<\/span>/);
  assert.match(r('e4'), />Not screened yet · ads unknown</);
  assert.match(r('e5'), />Stopped partway</);
  assert.match(r('e6'), />Metadata only</);
  assert.match(r('e7'), />Screened · no ads found</, 'null falls back to the older logic');
  for (const id of ['e1', 'e4', 'e5', 'e6', 'e7']) assert.doesNotMatch(r(id), NEW_WORDS, 'NEW words only on screened rows');
});

test('Sponsors line: "No ads found" only over fully checked episodes', async () => {
  const all = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.fully_checked), row('e2', CHECK.fully_checked)])));
  assert.equal(sponsorEmpty(all), "No ads found in this show's episodes.");
  const mixed = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.fully_checked), row('e2', CHECK.screened)])));
  assert.doesNotMatch(sponsorEmpty(mixed), /No ads found/);
  const none = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.not_screened), row('e2', CHECK.no_transcript)])));
  assert.equal(sponsorEmpty(none), 'No episodes screened yet.');
});

/* -- NEW words: N1, N3, N4 approved (LEDGER-LEAD3-SCR-STR-1); N2 is not -- */

test('the approved NEW words are on, behind the one flag', async () => {
  assert.ok(VIEWS.includes('const SCREEN_NEW_WORDS = true;'), 'one flag, on');
  assert.equal((VIEWS.match(/TODO\(SCREENED-NEW\)/g) || []).length, 0, 'no TODO left');
  const ep = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened })));
  assert.match(statusLine(ep), /Ads checked\. The transcript ends 41 s before the episode does, so an ad there could be missed\./, 'N1');
  assert.match(adsState(ep), /No ads are listed for this episode\. It is not fully checked, so an ad could be missing\./, 'N3');
  const full = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.fully_checked })));
  assert.doesNotMatch(text(full), NEW_WORDS);
  const show = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.screened), row('e2', CHECK.fully_checked)])));
  assert.match(epRowOf(show, 'e1'), /<span class="lib-row-sub">Not fully checked · transcript ends 41 s early<\/span>/, 'N4');
  assert.doesNotMatch(epRowOf(show, 'e2'), NEW_WORDS);
});

test('N2 is not approved: a screened episode without a short transcript keeps "Ads checked."', async () => {
  const other = { ...CHECK.screened, transcript_short_s: 3.0, not_fully_checked: ['not_indexed'] };
  const ep = await render('#/episode/ep_s', episodeApi(episodeOf({ check: other })));
  assert.match(statusLine(ep), /Not fully checked<\/span><span>Ads checked\.<\/span>/);
  assert.doesNotMatch(text(ep), /not every check on this episode finished|transcript ends/);
  assert.doesNotMatch(VIEWS, /not every check on this episode finished/, 'N2 is not in the code');
  const show = await render('#/show/cs-1', showApi(SHOW([row('e1', other)])));
  assert.match(epRowOf(show, 'e1'), /<span class="lib-row-sub">Not fully checked<\/span>/);
});

test('minutes rule: 120 s or more reads as whole minutes, rounded down', async () => {
  const at = s => ({ ...CHECK.screened, transcript_short_s: s });
  for (const [s, words] of [[119, '119 s'], [119.4, '119 s'], [120, '2 min'], [179.9, '2 min'], [3725.5, '62 min']]) {
    const ep = await render('#/episode/ep_s', episodeApi(episodeOf({ check: at(s) })));
    assert.match(statusLine(ep), new RegExp(`Ads checked\\. The transcript ends ${words} before the episode does, so an ad there could be missed\\.`), `N1 at ${s}`);
    const show = await render('#/show/cs-1', showApi(SHOW([row('e1', at(s))])));
    assert.match(epRowOf(show, 'e1'), new RegExp(`Not fully checked · transcript ends ${words} early<`), `N4 at ${s}`);
  }
});

/* -- Show header: the tier badge never stands on screened-only episodes --- */

test('Show header: "Full coverage" only when an episode is fully checked', async () => {
  const scope = html => (html.match(/<p class="lib-scope">[\s\S]*?<\/p>/) || [''])[0];
  const only = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.screened), row('e2', CHECK.not_screened)])));
  assert.doesNotMatch(scope(only), /Full coverage/);
  const full = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.fully_checked), row('e2', CHECK.screened)])));
  assert.match(scope(full), /Full coverage/);
  const old = await render('#/show/cs-1', showApi(SHOW([row('e1', null)])));
  assert.match(scope(old), /Full coverage/, 'null falls back to the older logic');
});

/* -- SCREENED-FIX1 -------------------------------------------------------- */

// Stopped at fetch: no transcript copy exists, so the state is no_transcript.
const STOP_FETCH = { ...STOPPED, label: 'Stopped partway (fetch)', stage: 'fetch' };

test('no transcript and stopped partway: still labelled as stopped (code M-1)', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.no_transcript, screening: STOP_FETCH })));
  assert.match(statusLine(html), /Processing stopped partway/);
  assert.doesNotMatch(statusLine(html), /Ads not checked yet/);
  assert.match(adsState(html), /Processing stopped partway, at audio download\./);
  assert.doesNotMatch(text(html), /not been screened for ads yet/);
  assert.equal(aboutAds(html), 'Stopped partway · audio download');
  const show = await render('#/show/cs-1', showApi(SHOW([row('e1', CHECK.no_transcript, { transcript: false, screening: STOP_FETCH })])));
  assert.match(epRowOf(show, 'e1'), />Stopped partway</);
  assert.match(epRowOf(show, 'e1'), /stopped at audio download/);
  assert.doesNotMatch(epRowOf(show, 'e1'), /Metadata only/);
});

test('About "Ads" fact with no screening record but a screen_check (code L-2)', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.not_screened, screening: null })));
  assert.equal(aboutAds(html), 'Not screened');
  const done = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened, screening: null })));
  assert.equal(aboutAds(done), 'Screened');
});

test('Episode "Not fully checked" uses the campaign chip style, not the red one (screen 03)', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened })));
  const line = statusLine(html);
  assert.match(line, /<span class="tag amber">Not fully checked<\/span>/);
  assert.doesNotMatch(line, /data-tag="gap"|ew-tag-gap/);
});

/* -- N5: the campaign Coverage card names the not fully checked episodes -- */

const campaignApi = (missing, labels) => p => {
  const targets = [{ id: 't1', scope: 'show_window', show_id: 'cs-1', date_from: '2026-10-01', date_to: '2026-10-31' }];
  const episodes = ['e1', 'e2', 'e3', 'e4', 'e5', 'e6'];
  if (p === '/customer/campaigns/2') return { campaign: { id: 2, mode: 'research', revision: 3, name: 'Field study', targets } };
  if (p === '/customer/campaigns/2/draft') return { draft: { pins: {}, campaign: { revision: 3 }, mode: 'research', placements: [],
    targets: targets.map(t => ({ ...t, evidence: [] })), allocation: {}, show_titles: { 'cs-1': 'The Long Run' },
    coverage_by_placement: { t1: { state: missing.length ? 'incomplete' : 'complete', episodes, missing, failed: [], episode_labels: labels } } } };
  if (p === '/customer/campaigns/2/reports') return { reports: [] };
  return new Error('unexpected ' + p);
};
const coverageP = html => (html.match(/<h2>Coverage<\/h2><p>([\s\S]*?)<\/p>/) || ['', ''])[1];
const epLink = (id, t) => `<a class="" href="#/episode/${id}">${t}</a>`;
const LABELS = { e1: 'One', e2: 'Two <b>&', e3: 'Three', e4: 'Four', e5: 'Five' };

test('N5: 1 episode is named and linked', async () => {
  const p = coverageP(await render('#/campaign/2', campaignApi(['e1'], LABELS)));
  assert.equal(p, `6 episodes in scope. 1 not fully checked: ${epLink('e1', 'One')}.`);
  assert.doesNotMatch(p, /without a full-episode check/);
});

test('N5: 2 or 3 episodes are a " · " list of links, titles escaped (SCR-SCR-R2-01)', async () => {
  const two = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e2'], LABELS)));
  assert.equal(two, `6 episodes in scope. 2 not fully checked: ${epLink('e1', 'One')} · ${epLink('e2', 'Two &lt;b&gt;&amp;')}.`);
  const three = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e2', 'e3'], LABELS)));
  assert.match(three, new RegExp(`3 not fully checked: ${epLink('e1', 'One')} · .* · ${epLink('e3', 'Three')}\\.$`));
});

test('N5: titles that contain commas stay apart, joined with " · " (SCR-SCR-R2-01)', async () => {
  const COMMAS = { e1: '#254 - Rogue AI hacking, bio-weapons, Dean & Hassabis out', e2: '#257 - GPT 6, AI Extinction, Security Incidents', e3: '#256 - Fable 5.1, Gemini 3.8 Flash' };
  const p = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e2', 'e3'], COMMAS)));
  assert.equal(p, `6 episodes in scope. 3 not fully checked: ${epLink('e1', '#254 - Rogue AI hacking, bio-weapons, Dean &amp; Hassabis out')} · `
    + `${epLink('e2', '#257 - GPT 6, AI Extinction, Security Incidents')} · ${epLink('e3', '#256 - Fable 5.1, Gemini 3.8 Flash')}.`);
  assert.doesNotMatch(p, /<\/a>, <a/, 'no ", " between titles');
});

test('N5: more than 3 names the first 3, then the app’s "N more episodes" words', async () => {
  const p = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e3', 'e4', 'e5', 'e6'], LABELS)));
  assert.equal(p, `6 episodes in scope. 5 not fully checked: ${epLink('e1', 'One')} · ${epLink('e3', 'Three')} · ${epLink('e4', 'Four')} and 2 more episodes.`);
  const four = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e3', 'e4', 'e5'], LABELS)));
  assert.match(four, /and 1 more episode\.$/);
});

test('N5: with no titles known, the count alone', async () => {
  const p = coverageP(await render('#/campaign/2', campaignApi(['e6'], LABELS)));
  assert.equal(p, '6 episodes in scope. 1 not fully checked.');
});

/* -- SCREENED-FIX2 -------------------------------------------------------- */

test('N5: titled and untitled mixed; untitled ones count in "N more" (SCR-R2-04)', async () => {
  const p = coverageP(await render('#/campaign/2', campaignApi(['e6', 'e1'], LABELS)));
  assert.equal(p, `6 episodes in scope. 2 not fully checked: ${epLink('e1', 'One')} and 1 more episode.`);
  const five = coverageP(await render('#/campaign/2', campaignApi(['e6', 'e1', 'e2', 'e3', 'e4'], { ...LABELS, e2: undefined })));
  assert.equal(five, `6 episodes in scope. 5 not fully checked: ${epLink('e1', 'One')} · ${epLink('e3', 'Three')} · ${epLink('e4', 'Four')} and 2 more episodes.`);
});

test('N5: a title of only spaces counts as untitled (SCR-R2-05)', async () => {
  const p = coverageP(await render('#/campaign/2', campaignApi(['e1', 'e6', 'e3'], { ...LABELS, e1: '   ' })));
  assert.equal(p, `6 episodes in scope. 3 not fully checked: ${epLink('e3', 'Three')} and 2 more episodes.`);
  assert.doesNotMatch(p, /href="#\/episode\/e1"/);
  const none = coverageP(await render('#/campaign/2', campaignApi(['e1'], { e1: ' \t ' })));
  assert.equal(none, '6 episodes in scope. 1 not fully checked.');
});

test('N1 stays off on the excerpt plan: the line is "Ads checked." only (SCR-R2-01)', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.screened }), { ...REC, disclosure: 'excerpt' }));
  assert.match(statusLine(html), /Not fully checked<\/span><span>Ads checked\.<\/span>/);
  assert.doesNotMatch(statusLine(html), /transcript ends/);
});

test('minutes rule edges on the raw value: 119.6 s reads "120 s", 15.6 s reads "16 s" (SCR-R2-02, lead SCR-STR-2)', async () => {
  const at = s => ({ ...CHECK.screened, transcript_short_s: s });
  for (const [s, words] of [[119.6, '120 s'], [15.6, '16 s']]) {
    const ep = await render('#/episode/ep_s', episodeApi(episodeOf({ check: at(s) })));
    assert.match(statusLine(ep), new RegExp(`The transcript ends ${words} before the episode does`), `N1 at ${s}`);
    const show = await render('#/show/cs-1', showApi(SHOW([row('e1', at(s))])));
    assert.match(epRowOf(show, 'e1'), new RegExp(`Not fully checked · transcript ends ${words} early<`), `N4 at ${s}`);
  }
});

test('N3 stays off a fully checked episode whose ads are not ruled out (SCR-R2-03)', async () => {
  const html = await render('#/episode/ep_s', episodeApi(episodeOf({ check: CHECK.fully_checked, screening: { ...OLD, screened_no_ads: false, label: 'Screened' } })));
  assert.equal(adsState(html), '<p class="ew-ads-state">No ads are listed for this episode.</p>');
  assert.doesNotMatch(text(html), NEW_WORDS);
});

test('N4 stays off the excerpt plan: the Show row sub-line is "Not fully checked" only (SCR-R2-06, lead SCR-STR-3)', async () => {
  const excerpt = await render('#/show/cs-1', showApi({ ...SHOW([row('e1', CHECK.screened)]), excerpt_only: true }));
  assert.match(epRowOf(excerpt, 'e1'), /<span class="lib-row-sub">Not fully checked<\/span>/);
  assert.doesNotMatch(epRowOf(excerpt, 'e1'), /transcript ends/);
  const full = await render('#/show/cs-1', showApi({ ...SHOW([row('e1', CHECK.screened)]), excerpt_only: false }));
  assert.match(epRowOf(full, 'e1'), /<span class="lib-row-sub">Not fully checked · transcript ends 41 s early<\/span>/, 'N4 as approved on the full plan');
});
