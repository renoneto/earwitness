/* d16 round, worker d7: decisions 7, 10, 12 and 14 (time, one Close).
   7: members see three audio states (Clip available, No clip kept
      (transcript only), Audio changed since transcription); rotation,
      remote-only and alignment notes sit under Technical details.
   10: three public jargon lines rewritten; legal and approval lines byte-equal.
   12: no "Operator tools" link in the member footer; /app/operator/ still served.
   14: every timestamp is a <time datetime title> in local time with the UTC
      time on hover; the Add-to-campaign result and load-error dialogs have one Close.
   Loader copied from frontend_dv3_g1-search / frontend_uat_fixes / frontend_uat_library.
   Run: node --test tests/frontend_uat_d7.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const APP = path.join(ROOT, 'app');
const read = f => fs.readFileSync(path.join(APP, f), 'utf8');

function loadAt(route, { fetch } = {}) {
  const store = {};
  const sandbox = {
    console, URLSearchParams, AbortController, URL,
    location: { hash: route, href: 'http://127.0.0.1:9163/app/' + route },
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
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(read(file), sandbox, { filename: file });
  sandbox.window.EW.current.route = route;
  return sandbox.window;
}
const flushBoot = () => new Promise(res => setTimeout(res, 0));
let PREFS = {};
const sessionFetch = async (url) => {
  if (url === '/api/v1/customer/preferences') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1, ...PREFS }, revision: 1 }) };
  }
  if (url === '/api/v1/session') {
    return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true, authenticated: false,
      csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  }
  throw new Error('no network in unit test');
};
/* One page rendered through the real views with a stubbed API. */
async function page(route, api, { modal = null, setup } = {}) {
  const win = loadAt(route, { fetch: sessionFetch });
  win.addEventListener = () => {};
  win.document.addEventListener = () => {};
  const dummy = { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } };
  win.document.querySelector = () => dummy;
  win.document.querySelectorAll = () => [];
  win.EW.current.route = route;
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.current.hosted = false;
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  const dlg = modal || { open: false, innerHTML: '', close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {} } };
  win.document.querySelector = sel => (sel === '#main' ? main : sel === '#modal' ? dlg : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
  win.scrollTo = () => {};
  win.setTimeout = () => {};
  win.EW.header = () => {};
  win.EW.toast = () => {}; win.EW.saveNotice = () => {};
  const calls = [];
  win.EW.api = async (p, opts = {}) => { calls.push([p, opts.method || 'GET']); const out = api(p, opts); if (out instanceof Error) throw out; return out; };
  win.location.hash = route;
  win.EW.current.route = route;
  if (setup) setup(win);
  await win.EWViews.render();
  for (let i = 0; i < 5; i++) await flushBoot();
  return { win, html: main.innerHTML, modal: dlg, calls };
}
const text = html => html.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const TECH = /<details class="tech-details">[\s\S]*?<\/details>/g;
const outside = html => html.replace(TECH, '');
const inside = html => (html.match(TECH) || []).join('\n');

const CLIP = 'Clip available';
const NONE = 'No clip (transcript only)';
const CHANGED = 'Audio changed since transcription';
const NO_CLIP = 'No clip for this ad. The words are below.';
const LEAKS = /Remote recording only|Remote audio only|Rotated|rotated|Timing not checked|unverified|Source audio|authorized recording|prerolls/;

/* -- fixtures ----------------------------------------------------------------- */
const REF = 'sr1:b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2';
const evRecord = media => ({ ref: REF, canonical_source_ref: REF, record_kind: 'evidence', status: 'finding',
  review: { judgment: 'ad', bounds_verified: 0 }, attribution: [], relationship: 'current', availability: 'current',
  bounds_basis: 'full_effective_source', selected_bounds: { start_s: 110, end_s: 180 },
  episode: { episode_id: 'ep_f', show_title: 'Quiet Hours', ep_title: 'T', pub_date: '2026-10-07', fetch_date: '2026-10-09', duration_min: 41.4 },
  show: { show_identity_id: 'cs-quiet', display_title: 'Quiet Hours', unresolved: 0 },
  evidence: { id: 'named:ep_f.named.120000-180000', episode_id: 'ep_f', orig_start_s: 110, orig_end_s: 180 }, media,
  read: { kind: 'named', source_label: 'Named read', brand: { key: 'northwind', display: 'Northwind', display_basis: 'brand_list', confirmed: false },
    needs_review: false, self_promo: false, display_span: [121.5, 170], has_clip: false },
  window: { segments: [{ segment_id: 1, start_s: 120, end_s: 150, text: 'Go.', text_start: 0, text_end: 3 }], text_complete: true, next_cursor: null },
  provenance: { source_key: 'k', version_no: 2, created_at: '2026-10-09T17:30:00Z' } });
const evidencePage = media => page('#/evidence/' + encodeURIComponent(REF), p => {
  if (p === '/customer/records/' + encodeURIComponent(REF)) return { record: evRecord(media) };
  if (p.endsWith('/mistake')) return { mistakes: [] };
  return {};
});
const SCREENED = { label: 'Screened', jev_screened: true, screened_no_ads: false, stopped_partway: false, coverage_tier: 'full', transcript_only: false, stage: null };
const epAd = (id, span, display) => ({ ref: 'sr1:' + id.padEnd(32, '0'), evidence_id: id, status: 'finding', span, display_span: span,
  brand: { key: display.toLowerCase(), display, display_basis: 'brand_list', confirmed: false }, needs_review: false, self_promo: false,
  kind: 'rule', source_label: 'Rule candidate', transcript_start_s: span[0], media: null, transcript_version_id: 36, on_current_transcript: true });
const epRecord = media => ({ ref: 'ep:ep_tv', source_ref: 'sr1:'.padEnd(36, 'e'), canonical_source_ref: 'sr1:'.padEnd(36, 'e'),
  record_kind: 'source_publication', relationship: 'current', bounds_basis: 'full_effective_source',
  episode: { episode_id: 'ep_tv', transcript_version_id: 36, ep_title: 'Michelle Parker', show_title: 'True Crime', pub_date: '2026-09-20',
    duration_min: 55.4, transcript_duration_s: 3324, transcript_is_current: true },
  show: { show_identity_id: 'cs-tc', unresolved: 0 }, effective_bounds: { start_s: 0, end_s: 3324 }, selected_bounds: { start_s: 0, end_s: 3324 }, media,
  window: { segments: [{ segment_id: 1, start_s: 100, end_s: 110, text: 'Lowes has you covered.', text_start: 0, text_end: 22 }], text_complete: true, next_cursor: null } });
const episodePage = (media, ads = []) => page('#/episode/ep_tv', p => {
  if (p === '/customer/records/' + encodeURIComponent('ep:ep_tv')) return { record: epRecord(media) };
  if (p === '/customer/episodes/ep_tv') return { episode: { episode_id: 'ep_tv', ads, self_promo: [], screening: SCREENED, screened_no_ads: false, current_transcript_version_id: 36,
    earlier_transcripts: [], ad_breaks: [], ad_break_counts: { current: 0, earlier: 0, no_copy: 0 } } };
  return {};
});
const searchItem = (over = {}) => ({ ref: 'sr1:a', record_kind: 'evidence', status: 'finding', ep_title: 'Ep one', show_title: 'Show one',
  show: { show_identity_id: 'cs-1', display_title: 'Show one' }, pub_date: '2026-10-09', text: 'sleep better tonight',
  quotation_span: [60, 120], audio: 'unavailable', audio_local: false,
  read: { brand: { key: 'ag1', display: 'AG1', product: 'Greens', confirmed: false }, display_span: [62, 121], has_clip: false, needs_review: false }, ...over });
const searchBundle = items => ({ query: { version: 2, mode: 'ads', q: 'sleep', sort: 'newest', page: 1 }, revision: 'rev-1',
  result: { items, total: items.length, per_page: 10, total_relation: 'exact', dated_total: items.length, undated_total: 0 },
  coverage: { episodes_in_scope: 288 }, summary: { week: [] } });
const searchPage = (items, view = '') => page('#/search?mode=ads&q=sleep' + view, p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') return searchBundle(items);
  return new Error('unexpected ' + p);
});

/* -- decision 7: Search -------------------------------------------------------- */
test('D7 Search: the member audio state is one of three phrases', () => {
  const ui = loadAt('#/search').EWUI;
  assert.equal(ui.audioState({ audio: 'available', audio_local: true }), CLIP);
  assert.equal(ui.audioState({ audio: 'available', audio_local: false }), NONE, 'remote only');
  assert.equal(ui.audioState({ audio: 'unavailable' }), NONE);
  assert.equal(ui.audioState({}), NONE, 'unknown stays no clip kept');
  assert.equal(ui.audioState({ audio: 'mismatch' }), CHANGED);
  const none = { has_clip: false };
  assert.equal(ui.clipNote({ audio: 'available' }, none), NONE);
  assert.equal(ui.clipNote({ audio: 'available' }, undefined), NONE);
  assert.equal(ui.clipNote({ audio: 'mismatch' }, none), CHANGED);
  // Review F7: the slot where nothing plays never says "Clip available".
  assert.equal(ui.clipNote({ audio: 'available', audio_local: true }, none), NONE);
});

test('D7 Search card: the no-clip note uses the three phrases and never the old wording', () => {
  const ui = loadAt('#/search').EWUI;
  const card = item => ui.sourceCard(item);
  assert.match(card(searchItem({ audio: 'available' })), /<span class="ew-noclip">No clip \(transcript only\)<\/span>/);
  assert.match(card(searchItem({ audio: 'mismatch' })), /<span class="ew-noclip">Audio changed since transcription<\/span>/);
  for (const audio of ['available', 'mismatch', 'unavailable']) {
    assert.doesNotMatch(card(searchItem({ audio })), /Remote audio only|Source audio|Local audio/, audio);
  }
});

test('D7 Search inspector and list rows: three states; remote detail only under Technical details', async () => {
  const items = [searchItem({ ref: 'sr1:r', audio: 'available' }), searchItem({ ref: 'sr1:m', audio: 'mismatch' }), searchItem({ ref: 'sr1:u', audio: 'unavailable' })];
  const { html } = await searchPage(items, '&view=list');
  assert.match(html, /title="No clip \(transcript only\)"/);
  assert.match(html, /title="Audio changed since transcription"/);
  assert.doesNotMatch(outside(html), /Remote audio only|Source audio|Local audio/);
  // the first row is inspected: remote only
  assert.match(html, /ew-inspector-noclip">No clip \(transcript only\)\. The passage is on the evidence page\./);
  assert.match(inside(html), /Remote audio only, no local playback/, 'the remote detail is a technical detail');
  assert.doesNotMatch(html.match(/ew-inspector-noclip">[^<]*/)[0], /\bread\b/i, 'no "read" in the audio line');
  const ledger = html.slice(html.indexOf('ew-inspector-head'));
  assert.match(ledger, /Audio<\/th><td[^>]*>No clip \(transcript only\)/);
});

test('D7 Search inspector: a playable result says Clip available; a changed one says so', async () => {
  const playable = await searchPage([searchItem({ audio: 'available', audio_local: true })]);
  assert.match(playable.html, /Audio<\/th><td[^>]*>Clip available/);
  assert.doesNotMatch(playable.html, /A clip can play here/);
  const changed = await searchPage([searchItem({ audio: 'mismatch' })]);
  assert.match(changed.html, /ew-inspector-noclip">Audio changed since transcription\./);
});

/* -- decision 7: Evidence -------------------------------------------------------- */
const LOCAL = { playback: 'local', source_offset_s: 0, alignment: 'verified', timing: 'verified' };
const states = html => [CLIP, NONE, CHANGED].filter(s => outside(html).includes(s));

test('D7 Evidence: a local clip says Clip available; alignment and rotation notes are technical details', async () => {
  const ok = await evidencePage(LOCAL);
  assert.deepEqual(states(ok.html), [CLIP]);
  const unaligned = await evidencePage({ ...LOCAL, alignment: 'unknown', timing: 'unknown' });
  assert.ok(outside(unaligned.html).includes(CLIP));
  assert.doesNotMatch(outside(unaligned.html), LEAKS);
  assert.match(inside(unaligned.html), /Timing not checked against this audio\./);
  const rot = await evidencePage({ ...LOCAL, trust: 'rotated' });
  assert.deepEqual(states(rot.html), [CHANGED], 'a rotated feed means the audio changed');
  assert.doesNotMatch(outside(rot.html), LEAKS);
  assert.match(inside(rot.html), /This feed can change what plays near the start of an episode\. This clip may not match the transcript exactly\./);
});

test('D7 Evidence: remote only, unavailable, mismatch and rotated map to the three states; the player names no reason', async () => {
  const remote = await evidencePage({ playback: 'remote', alignment: 'unknown' });
  assert.deepEqual(states(remote.html), [NONE]);
  assert.doesNotMatch(outside(remote.html), LEAKS, 'remote detail is hidden by default');
  // CLIP design 6.2: no clip, for any reason, is one neutral sentence and no technical details.
  assert.ok(remote.html.includes(NO_CLIP));
  assert.doesNotMatch(remote.html, LEAKS);
  const none = await evidencePage({ playback: 'unavailable', reason: 'no authorized recording proof', alignment: 'unknown' });
  assert.deepEqual(states(none.html), [NONE]);
  assert.doesNotMatch(outside(none.html), LEAKS);
  assert.ok(none.html.includes(NO_CLIP));
  assert.doesNotMatch(none.html, /No recording of this episode/);
  assert.doesNotMatch(none.html, /authorized recording proof/i);
  const bare = await evidencePage({ playback: 'unavailable' });
  assert.deepEqual(states(bare.html), [NONE]);
  const mism = await evidencePage({ playback: 'remote', alignment: 'mismatch' });
  assert.deepEqual(states(mism.html), [CHANGED]);
  assert.doesNotMatch(outside(mism.html), LEAKS);
  const rot = await evidencePage({ playback: 'remote', alignment: 'unknown', trust: 'rotated' });
  assert.deepEqual(states(rot.html), [CHANGED]);
  assert.ok(rot.html.includes(NO_CLIP));
  assert.doesNotMatch(rot.html, LEAKS);
  assert.doesNotMatch(rot.html, /<audio/);
});

test('D7 Evidence: the About record Audio row is a member state, not a technical phrase', async () => {
  for (const [media, want] of [[LOCAL, CLIP], [{ playback: 'remote' }, NONE], [{ playback: 'unavailable' }, NONE], [{ playback: 'remote', trust: 'rotated' }, CHANGED], [{ playback: 'remote', alignment: 'mismatch' }, CHANGED]]) {
    const { html } = await evidencePage(media);
    assert.match(html, new RegExp('<dt>Audio</dt><dd>' + want.replace(/[()]/g, '\\$&') + '</dd>'), JSON.stringify(media));
  }
});

/* -- decision 7: Episode ---------------------------------------------------------- */
test('D7 Episode: remote, rotated and unavailable audio use the three states; notes are technical details', async () => {
  const remote = await episodePage({ playback: 'remote' });
  assert.ok(outside(remote.html).includes(NONE));
  assert.doesNotMatch(outside(remote.html), LEAKS);
  assert.match(inside(remote.html), /Remote recording only/);
  const rot = await episodePage({ playback: 'remote', trust: 'rotated' });
  assert.ok(outside(rot.html).includes(CHANGED));
  assert.doesNotMatch(outside(rot.html), LEAKS);
  assert.match(inside(rot.html), /Rotated audio feed/);
  assert.doesNotMatch(rot.html, /<audio/);
  const local = await episodePage({ playback: 'local', source_offset_s: 0, alignment: 'unknown', trust: 'rotated' });
  assert.ok(outside(local.html).includes(CHANGED));
  assert.doesNotMatch(outside(local.html), /Timing not checked|different creative/);
  assert.match(inside(local.html), /Timing not checked against this audio\./);
  const ok = await episodePage(LOCAL);
  assert.ok(outside(ok.html).includes(CLIP));
});

test('D7 Episode: an ad row with no clip says No clip (transcript only) and no "read"', async () => {
  const withClip = { ...epAd('c1', [100, 160], 'Lowes'), media: { playback: 'local', source_offset_s: 0, alignment: 'verified' } };
  const { html } = await episodePage({ playback: 'remote' }, [withClip, epAd('c2', [300, 360], 'Quince')]);
  const hidden = html.match(/<span class="visually-hidden">No clip[^<]*<\/span>/g) || [];
  assert.deepEqual(hidden, [`<span class="visually-hidden">${NONE}</span>`]);
});

test('D7: audio state sentences carry no "read" and the old member phrases are gone from the sources', () => {
  for (const file of ['ui.js', 'views.js']) {
    const src = read(file);
    assert.doesNotMatch(src, /'Local audio available'/, file);
    assert.doesNotMatch(src, /Source audio does not match this transcript<\/strong>/, file);
  }
  assert.doesNotMatch(read('app.js'), /Source audio unavailable|Source audio does not match/, 'audioBox uses the member states');
});

test('D7: audioBox (app.js) shows the member states', () => {
  const E = loadAt('#/search').EW;
  assert.equal(E.audioBox('available'), '');
  assert.match(E.audioBox('mismatch'), /<strong>Audio changed since transcription<\/strong>/);
  assert.match(E.audioBox('unavailable'), /<strong>No clip \(transcript only\)<\/strong>/);
  assert.doesNotMatch(E.audioBox('mismatch') + E.audioBox('unavailable'), /\bread\b|Remote|Rotated/);
});

/* -- decision 10: public pages ---------------------------------------------------- */
const lines = f => fs.readFileSync(path.join(APP, 'public', f), 'utf8').split('\n');
test('D10: the three jargon lines are rewritten exactly', () => {
  const meth = lines('methodology.html');
  assert.ok(meth.includes('<li>We keep four things separate: the brand we heard, any web address or code, whether it is an ad, and whether it met your brief.</li>'));
  assert.ok(!meth.join('\n').includes('Spoken-brand attribution, URL/code association'));
  const idx = lines('index.html');
  // LAND-1 (direction B2): the illustrative card is gone; the made-up example says so.
  assert.ok(idx.some(l => l.includes('Ferncastle is a made-up brand, for illustration.')));
  assert.ok(!idx.join('\n').includes('Author-created illustrative source'));
  const sentence = 'Where audio, timing or requirements are missing, we say so.';
  assert.ok(idx.some(l => l.startsWith('<p>') && l.includes(sentence)), 'index');
  assert.ok(meth.some(l => l.startsWith('<p>') && l.includes(sentence)), 'methodology');
  for (const bad of ['uncertain alignment', 'Missing audio', 'absent requirements']) {
    assert.ok(!idx.join('\n').includes(bad) && !meth.join('\n').includes(bad), bad);
  }
});

test('D10: legal and approval lines are byte-equal to df64a8e, as updated by the approved PUB-48 rows', () => {
  const meth = lines('methodology.html');
  const idx = lines('index.html');
  const LEGAL_INDEX = [
    '<p>The words, where they were heard, and when we captured them. A timestamp says where something was heard, not that it was delivered as bought.</p>',
  ];
  // LAND-1: the compare examples this caption described are gone; the caption went with them.
  assert.ok(!idx.join('\n').includes('Illustrative transcript examples.'));
  const LEGAL_METH = [
    // PUB-48 (PUBLIC-WORDING rows 9, 10, 49, 48; lead-approved, LEDGER-LEAD3-NEXT-3, QW7-1, UXQW-2)
    '<li>Our own checks on ads are working notes, not accuracy scores.</li>',
    '<li>A file fingerprint shows only that two copies match. It is not a legal or authenticity seal.</li>',
    '<p>Members can report a mistake on any ad. Reports go to the EarWitness team. Only the review team changes what is shown, and a frozen report never changes.</p>',
    '<p>Ads are found in full-coverage episodes. When we can match an ad to a brand, we show its name. Otherwise the ad shows as &ldquo;Brand not identified&rdquo; and doesn&rsquo;t count toward any brand.</p>',
    '<p>These are two separate facts. When they disagree we show both and don&rsquo;t pick one.</p>',
  ];
  for (const l of LEGAL_INDEX) assert.ok(idx.includes(l), l);
  for (const l of LEGAL_METH) assert.ok(meth.includes(l), l);
  // PUB-48b row 52 (lead LEDGER-LEAD3-PUB-4) rewrote the Home sentence to match Methodology row 11.
  assert.ok(idx.some(l => l.includes('A match is something heard in the shows we cover, nothing more.')));
  assert.ok(!idx.join('\n').includes('A matching passage is an observation from the covered collection.'));
  // PUB-48 row 11 rewrote the methodology sentence.
  assert.ok(meth.some(l => l.includes('A match is something heard in the shows we cover, nothing more')));
  assert.ok(meth.some(l => l.includes('<a href="/coverage/">Coverage</a>')));
});

/* -- decision 12: footer ---------------------------------------------------------- */
test('D12: the member footer has no Operator tools link; the operator page is still served', () => {
  const idx = read('index.html');
  assert.doesNotMatch(idx, /Operator tools/);
  assert.doesNotMatch(idx, /href="\/app\/operator/);
  assert.match(idx, /<a href="#\/settings">Collection definitions &amp; settings<\/a>/, 'the other footer link stays');
  assert.ok(fs.existsSync(path.join(APP, 'operator', 'index.html')));
  const server = fs.readFileSync(path.join(ROOT, 'scripts', 'app_server.py'), 'utf8');
  assert.match(server, /path in \("\/app\/operator", "\/app\/operator\/"\)/, 'the local server still routes /app/operator/');
});

/* -- decision 14: time ------------------------------------------------------------ */
async function withZone(zone) {
  PREFS = { time_zone: zone };
  const win = loadAt('#/overview', { fetch: sessionFetch });
  for (let i = 0; i < 20; i++) await flushBoot();
  await win.EW.preferences();
  await win.EW.preferences();
  return win;
}
test('D14: E.stamp renders local time in a <time> with the UTC time on hover', async () => {
  const la = await withZone('America/Los_Angeles');
  assert.equal(la.EW.stamp('2026-10-16T05:10:00Z'),
    '<time datetime="2026-10-16T05:10:00Z" title="Oct 16, 2026, 5:10 AM UTC">Oct 15, 10:10 PM PDT</time>');
  const utc = await withZone('UTC');
  assert.equal(utc.EW.stamp('2026-10-16T05:10:00'), '<time datetime="2026-10-16T05:10:00Z" title="Oct 16, 2026, 5:10 AM UTC">Oct 16, 5:10 AM UTC</time>', 'a zoneless server time is UTC');
  assert.equal(utc.EW.stamp('2026-10-16T07:10:00+02:00'), '<time datetime="2026-10-16T05:10:00Z" title="Oct 16, 2026, 5:10 AM UTC">Oct 16, 5:10 AM UTC</time>');
  assert.equal(utc.EW.stamp(null), 'No successful update');
  assert.doesNotMatch(la.EW.stamp('2026-10-16T05:10:00Z'), /style=/);
  assert.equal(la.EW.stampText('2026-10-16T05:10:00Z'), 'Oct 15, 10:10 PM PDT', 'a plain-text form for comparisons');
});
test('D14: day labels and clock times are <time> elements too; unknown stays plain text', async () => {
  const la = await withZone('America/Los_Angeles');
  assert.equal(la.EW.timeHtml('2026-10-16T05:10:00Z', { day: true }),
    '<time datetime="2026-10-16T05:10:00Z" title="Oct 16, 2026, 5:10 AM UTC">Oct 15</time>');
  assert.equal(la.EW.timeHtml('2026-10-16T05:10:00Z'), la.EW.stamp('2026-10-16T05:10:00Z'));
  assert.equal(la.EW.timeHtml('nonsense'), 'Date unknown');
  assert.equal(la.EW.timeHtml(''), 'Date unknown');
  assert.equal(la.EW.timeHtml('2026-10-16'), 'Oct 16', 'a calendar date is not a timestamp');
  assert.equal(la.EW.clockHtml(new Date('2026-10-16T05:10:00Z')),
    '<time datetime="2026-10-16T05:10:00Z" title="Oct 16, 2026, 5:10 AM UTC">10:10 PM</time>');
});
test('D14: a hostile value cannot add markup through the time element', async () => {
  const win = await withZone('UTC');
  const out = win.EW.timeHtml('2026-10-16T05:10:00Z"><img src=x>');
  assert.doesNotMatch(out, /<img/);
});

const T1 = '2026-10-14T09:00:00Z';
const ev = (n, over = {}) => ({ id: n, kind: 'new_finding', read_state: 0, observed_at: '2026-10-14T09:05:00Z', summary: { episode_id: 'e' + n, show_title: 'S', ep_title: 'E', pub_date: '2026-10-10', span: [10, 20], brand: 'Shopify' }, ...over });
const ALERT = { id: 5, name: 'Shopify', revision: 1, definition_revision: 1, lifecycle: 'active_local', baseline_complete: true,
  last_successful_at: T1, last_attempted_at: T1, last_attempt_result: 'ok', unread_event_count: 1, events: [ev(1)], query: { version: 2, mode: 'ads', q: '', brands: ['shopify'] },
  current_matches: { count: 1, tracked: 1, tracked_by_kind: { new_finding: 1 }, held_for_brand_review: 0 } };
const noRaw = html => assert.doesNotMatch(html, /&lt;time|="[^"]*<time|title="<time/);
const timeEls = html => [...html.matchAll(/<time datetime="([^"]+)" title="([^"]+)">([^<]+)<\/time>/g)];

test('D14: the alert page shows every timestamp as a <time> element, none escaped into text or attributes', async () => {
  const { html } = await page('#/alert/5', p => {
    if (p === '/customer/alerts/5') return { alert: ALERT };
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/brands') return { brands: [{ key: 'shopify', display: 'Shopify' }] };
    return new Error('unexpected ' + p);
  });
  noRaw(html);
  const els = timeEls(html);
  assert.ok(els.length >= 3, 'last checked, latest check, last attempt, found: ' + els.length);
  for (const [, dt, title] of els) {
    assert.match(dt, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
    assert.match(title, / UTC$/);
  }
  assert.doesNotMatch(html, /Checked [A-Z][a-z]{2} \d/, 'no bare timestamp text outside a <time>');
  assert.doesNotMatch(html, /<time[^>]*style=/);
});

test('D14: Overview alert rows show <time> elements', async () => {
  const { html } = await page('#/overview', p => {
    if (p === '/customer/overview') return { coverage: {}, recent_events: [{ alert_id: 1, alert_name: 'One', kind: 'new_text', read_state: 0, observed_at: '2026-10-15T10:00:00Z' }],
      offer_sequence: [], campaign_gaps: [], tracked_campaigns: [], activity: [{ text: 'Saved a search', at: '2026-10-15T11:00:00Z', href: '#/saved' }],
      alert_totals: [{ alert_id: 1, alert_name: 'One', lifecycle: 'active_local', events: 1, unread: 1, query: { version: 2, mode: 'mentions', q: 'x' }, latest_kind: 'new_text', latest_observed_at: '2026-10-15T10:00:00Z' }] };
    if (p.startsWith('/customer/saved-items')) return { items: [] };
    if (p === '/customer/brands') return { brands: [] };
    if (p.startsWith('/customer/search')) return { items: [] };
    return new Error('unexpected ' + p);
  });
  noRaw(html);
  assert.ok(timeEls(html).length >= 2, 'row stamps: ' + timeEls(html).length);
});

test('D14: the evidence record shows its stored time as a <time> element', async () => {
  const { html } = await evidencePage(LOCAL);
  noRaw(html);
  assert.ok(timeEls(html).some(([, dt]) => dt === '2026-10-09T17:30:00Z'), 'stored time');
});

test('D14: no member view puts a stamp inside esc() or an attribute', () => {
  for (const file of ['views.js', 'saved.js']) {
    const src = read(file);
    assert.doesNotMatch(src, /esc\(\s*(?:E|ew\(\))\.(?:stamp|timeHtml|dayHtml|clockHtml)\(/, file);
    assert.doesNotMatch(src, /esc\(\s*E\.formatD(?:ate|ay)\(/, file + ': formatDate/formatDay text must use the <time> form');
    assert.doesNotMatch(src, /="[^"]*\$\{[^}]*E\.(?:stamp|timeHtml|dayHtml)\(/, file + ': inside an attribute');
  }
});

/* -- decision 14: one Close -------------------------------------------------------- */
const closes = html => (html.match(/data-action="close-dialog"[^>]*>Close</g) || []).length;
test('D14: the load-error dialog has exactly one Close', async () => {
  const win = loadAt('#/evidence/x', { fetch: sessionFetch });
  win.addEventListener = () => {};
  const dlg = { innerHTML: '', open: false, close() {}, showModal() {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {} } };
  win.document.querySelector = sel => (sel === '#modal' ? dlg : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  vm.runInContext(read('views.js'), win, { filename: 'views.js' });
  await flushBoot();
  win.EW.api = async () => { const e = new Error('boom'); e.status = 503; throw e; };
  win.EW.header = () => {}; win.EW.toast = () => {};
  win.setTimeout = () => {};
  await win.EWViews.handleAction({ dataset: { action: 'add-to-campaign', ref: 'sr1:x', episode: 'e', show: 's', day: '2026-10-01' }, disabled: false, closest: () => null, getAttribute: () => null });
  for (let i = 0; i < 5; i++) await flushBoot();
  assert.match(dlg.innerHTML, /The campaigns didn’t load/);
  assert.equal(closes(dlg.innerHTML), 1, 'the header Close only');
  assert.match(dlg.innerHTML, /data-action="add-to-campaign"[^>]*>Retry</, 'Retry stays');
});
test('D14: the Add-to-campaign result dialog has exactly one Close', () => {
  const src = read('views.js');
  const at = src.indexOf('async function submitCampaignAttach');
  const body = src.slice(at, src.indexOf('/* Save bookmark', at));
  assert.doesNotMatch(body, /data-action="close-dialog"/, 'no second Close in the footer');
  assert.match(body, /E\.openDialog\('Add to campaign'/);
});
