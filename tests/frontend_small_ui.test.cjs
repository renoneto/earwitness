/* SMALL-UI (UX quick win 9 and D10; LEDGER-LEAD3-UXQW-1/-2):
   1. Compare has Copy link beside Choose brands. It copies the compare
      route with all five terms; PRIVATE_QUERY stays as it is, so the
      address bar still shows only #/compare.
   2. Plan A Search: the scope line adds "Your plan searches ad excerpts,
      not full transcripts." on every tab, after the tier sentence.
   3. Overview alert rows: a paused or stopped alert carries the Alerts
      page tag, the unread count is its own tag, and with no new matches
      the row says the Alerts page line instead of "No new matches."
   4. Phone header: a Search link before Menu, below 1024 px only, not
      on the Search page, at least 44 px.
   5. Quiet and link buttons are underlined; disabled ones are not.
   Run: node --test tests/frontend_small_ui.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const css = file => fs.readFileSync(path.join(F.APP, file), 'utf8');
const unesc = s => String(s).replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'");
const datasetOf = (html, action) => {
  const m = html.match(new RegExp(`<button[^>]*data-action="${action}"[^>]*>`));
  assert.ok(m, `a ${action} button`);
  const ds = { action };
  for (const [, k, v] of m[0].matchAll(/data-([a-z-]+)="([^"]*)"/g)) ds[k.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = unesc(v);
  return ds;
};

/* -- 1. Compare Copy link -------------------------------------------------- */
const FULL = '#/compare?brands=chatgpt,progressive&from=2026-10-01&to=2026-10-31&compareFrom=2026-09-01&compareTo=2026-09-30';
const HOSTED_TOAST = 'Link copied. It opens this comparison for members of this workspace.';
const LOCAL_TOAST = 'Link copied. It opens this comparison in this workspace on this computer.';
const FAIL_TOAST = 'Copy didn’t work.';

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
async function copyFrom(route, { hosted = false, clipboard = 'ok', exec = true, query = '', dataset = null, at = null } = {}) {
  const { html, win } = await F.render(route, F.compareApi());
  win.EW.current.hosted = hosted;
  // The page's own address, as a browser has it (SUI-SEC-R1-01).
  Object.assign(win.location, { origin: 'http://127.0.0.1:9153', pathname: '/app/', search: query, href: 'http://127.0.0.1:9153/app/' + query + route }, at || {});
  const toasts = [], copied = [], areas = [];
  win.EW.toast = m => toasts.push(m);
  win.navigator = { clipboard: { writeText: async t => { if (clipboard !== 'ok') throw new Error('NotAllowedError'); copied.push(t); } } };
  win.document.body = { append: el => { el.inPage = true; areas.push(el); } };
  win.document.createElement = () => ({ value: '', inPage: false, select() {}, remove() { this.inPage = false; } });
  win.document.execCommand = () => { if (!exec) return false; copied.push(areas[areas.length - 1].value); return true; };
  await win.EWViews.handleAction({ dataset: dataset || datasetOf(html, 'copy-compare-link'), closest: () => null });
  await settle();
  return { html, win, toasts, copied, areas };
}

test('Copy link: a Copy link button sits in the header actions beside Choose brands', async () => {
  const { html } = await F.render(FULL, F.compareApi());
  const actions = (html.match(/<div class="actions lib-head-actions">([\s\S]*?)<\/div><\/header>/) || [])[1];
  assert.ok(actions, 'the header actions render');
  assert.match(actions, /data-action="compare-select"/);
  assert.match(actions, /<button[^>]*data-action="copy-compare-link"[^>]*>Copy link<\/button>/);
});

test('Copy link: it copies the compare route with brands, from, to, compareFrom and compareTo', async () => {
  const { copied, toasts } = await copyFrom(FULL);
  assert.equal(copied.length, 1, 'one copy');
  const url = new URL(copied[0]);
  assert.equal(url.origin + url.pathname, 'http://127.0.0.1:9153/app/', 'an address of this app');
  assert.equal(url.hash, FULL);
  const p = new URLSearchParams(url.hash.split('?')[1]);
  assert.deepEqual([...p.keys()], ['brands', 'from', 'to', 'compareFrom', 'compareTo']);
  assert.deepEqual(toasts, [LOCAL_TOAST]);
});

test('Copy link: the brands are the two compared, also when the route named none', async () => {
  const { copied } = await copyFrom('#/compare');
  const p = new URLSearchParams(new URL(copied[0]).hash.split('?')[1]);
  assert.equal(p.get('brands'), 'chatgpt,progressive');
  for (const k of ['from', 'to', 'compareFrom', 'compareTo']) assert.equal(p.get(k), '', `${k} is named, empty`);
});

test('Copy link: hosted toast; the textarea fallback; the failure toast', async () => {
  assert.deepEqual((await copyFrom(FULL, { hosted: true })).toasts, [HOSTED_TOAST]);
  const fallback = await copyFrom(FULL, { clipboard: 'reject' });
  assert.equal(new URL(fallback.copied[0]).hash, FULL, 'the fallback copies the same link');
  assert.deepEqual(fallback.toasts, [LOCAL_TOAST]);
  const failed = await copyFrom(FULL, { clipboard: 'reject', exec: false });
  assert.deepEqual(failed.copied, []);
  assert.deepEqual(failed.toasts, [FAIL_TOAST]);
});

test('Copy link: brand keys and dates with &, comma, # and a space round-trip through URLSearchParams', async () => {
  // A comma separates brands, so it rides in the dates; the keys carry &, # and a space.
  const keys = ['h&m #1', 'a#b c'];
  const dates = { from: '2026-10-01&x=1', to: '2026-10-31,y #z', compareFrom: 'a b#c', compareTo: '1&2,3' };
  const route = '#/compare?' + new URLSearchParams({ brands: keys.join(','), ...dates }).toString();
  const { copied } = await copyFrom(route);
  assert.equal(copied.length, 1, 'one copy');
  const hash = copied[0].slice(copied[0].indexOf('#'));
  assert.ok(hash.startsWith('#/compare?'), hash);
  const p = new URLSearchParams(hash.slice('#/compare?'.length));
  assert.deepEqual([...p.keys()], ['brands', 'from', 'to', 'compareFrom', 'compareTo'], hash);
  assert.deepEqual(p.get('brands').split(','), keys);
  for (const [k, v] of Object.entries(dates)) assert.equal(p.get(k), v, k);
});

test('Copy link: the fallback textarea is taken out of the page again', async () => {
  for (const exec of [true, false]) {
    const { areas } = await copyFrom(FULL, { clipboard: 'reject', exec });
    assert.equal(areas.length, 1, 'the fallback ran');
    assert.equal(areas[0].inPage, false, `the textarea was left in the page (exec ${exec})`);
  }
});

test('Copy link: a route that is not #/compare?… copies nothing and says nothing', async () => {
  for (const route of ['#/search?q=x', '#/compare', 'http://elsewhere.example/#/compare?brands=a', ' #/compare?brands=a', '']) {
    const { copied, toasts, areas } = await copyFrom(FULL, { dataset: { action: 'copy-compare-link', route } });
    assert.deepEqual(copied, [], route);
    assert.deepEqual(areas, [], route);
    assert.deepEqual(toasts, [], route);
  }
});

test('Copy link: a page address with a query copies the app address without it (SUI-SEC-R1-01)', async () => {
  const cmp = await copyFrom(FULL, { query: '?x=1' });
  assert.equal(cmp.copied[0], 'http://127.0.0.1:9153/app/' + FULL);
  const ref = F.REF(1);
  const read = await copyFrom(FULL, { query: '?x=1', dataset: { action: 'copy-read-link', ref } });
  assert.equal(read.copied[0], 'http://127.0.0.1:9153/app/#/evidence/' + encodeURIComponent(ref));
});

// SUI-R2-01: the app path comes from the page, so a path prefix is kept.
test('Copy link: both copy functions keep the page\'s app path (a /x/app/ prefix)', async () => {
  const at = { pathname: '/x/app/', href: 'http://127.0.0.1:9153/x/app/' + FULL };
  const cmp = await copyFrom(FULL, { at });
  assert.equal(cmp.copied[0], 'http://127.0.0.1:9153/x/app/' + FULL);
  const ref = F.REF(1);
  const read = await copyFrom(FULL, { at, dataset: { action: 'copy-read-link', ref } });
  assert.equal(read.copied[0], 'http://127.0.0.1:9153/x/app/#/evidence/' + encodeURIComponent(ref));
});

// SUI-R2-04: an opaque origin ("null": a sandboxed frame, Firefox file:)
// falls back to the page address without its query and hash.
test('Copy link: where location.origin is "null", both copy functions use the page address without query and hash', async () => {
  const at = { origin: 'null', pathname: '/Users/x/app/index.html', search: '?x=1', href: 'file:///Users/x/app/index.html?x=1#/compare' };
  const cmp = await copyFrom(FULL, { at });
  assert.deepEqual(cmp.copied, ['file:///Users/x/app/index.html' + FULL]);
  assert.deepEqual(cmp.toasts, [LOCAL_TOAST]);
  const ref = F.REF(1);
  const read = await copyFrom(FULL, { at, dataset: { action: 'copy-read-link', ref } });
  assert.deepEqual(read.copied, ['file:///Users/x/app/index.html#/evidence/' + encodeURIComponent(ref)]);
  assert.equal(read.toasts.length, 1, 'the evidence copy says so');
});

test('Copy link: PRIVATE_QUERY is unchanged, so the address bar shows only #/compare', async () => {
  const src = css('app.js');
  assert.ok(src.includes("const PRIVATE_QUERY = { '#/search': null, '#/compare': null, '#/saved': ['q', 'cursor'] };"), 'PRIVATE_QUERY (SEC-R12-02)');
  const { win } = await F.render(FULL, F.compareApi());
  assert.equal(win.EW.publicRoute(FULL), '#/compare');
});

/* -- 2. Plan A scope line -------------------------------------------------- */
const PLAN_LINE = 'Your plan searches ad excerpts, not full transcripts.';
const BASE = { version: 2, mode: 'ads', q: 'ag1', brands: null, shows: null, categories: null, statuses: null, audio: null,
  date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null };
const searchApi = (mode, excerptOnly, extra = {}) => p => {
  if (p === '/customer/brands') return { brands: [] };
  if (p === '/customer/shows') return { shows: [] };
  if (p === '/customer/research-query') {
    return { query: { ...BASE, mode }, selector: null, revision: 'rev-1',
      result: { items: [], total: 0, per_page: 10, total_relation: 'exact', dated_total: 0, undated_total: 0,
        ...(mode === 'ads' ? { break_total: 0, ad_breaks: [] } : {}), excerpt_only: excerptOnly, ...extra },
      coverage: { episodes_in_scope: 38, sources_included: 38, excluded_undated: false }, summary: { week: [] } };
  }
  if (p.startsWith('/customer/filter-values?')) return { items: [] };
  return new Error('unexpected ' + p);
};
const scopeLine = html => F.text((html.match(/<p class="ew-scope-line">([\s\S]*?)<\/p>/) || [])[1] || '');

test('Plan A scope line: Ads tab keeps the tier sentence, then says the plan searches excerpts', async () => {
  const { html } = await F.render('#/search?mode=ads&q=ag1', searchApi('ads', true));
  const line = scopeLine(html);
  assert.ok(line.includes(`Ads come from full-coverage episodes only. ${PLAN_LINE}`), line);
});

test('Plan A scope line: All mentions (plan-limited) says it too', async () => {
  const { html } = await F.render('#/search?mode=mentions&q=ag1', searchApi('mentions', true, { plan_limited: true }));
  assert.ok(scopeLine(html).includes(PLAN_LINE), scopeLine(html));
});

test('Plan A scope line: Plan B says nothing new', async () => {
  const { html } = await F.render('#/search?mode=ads&q=ag1', searchApi('ads', false));
  const line = scopeLine(html);
  assert.ok(line.includes('Ads come from full-coverage episodes only.'), line);
  assert.ok(!html.includes(PLAN_LINE));
});

// SUI-SCR-R2-01: below 600 px the scope span is clipped with an
// ellipsis, so the plan sentence is its own element after it.
const scopeParts = html => {
  const p = (html.match(/<p class="ew-scope-line">([\s\S]*?)<\/p>/) || [])[1] || '';
  return { clipped: (p.match(/<span title="[^"]*">([^<]*)<\/span>/) || [])[1], plan: (p.match(/<span class="ew-plan-scope">([^<]*)<\/span>/) || [])[1], p };
};
for (const [mode, extra] of [['ads', {}], ['episodes', {}], ['mentions', { plan_limited: true }]]) {
  test(`Plan A scope line (${mode}): the plan sentence is its own element after the clipped scope text`, async () => {
    const { html } = await F.render(`#/search?mode=${mode}&q=ag1`, searchApi(mode, true, extra));
    const { clipped, plan, p } = scopeParts(html);
    assert.ok(clipped, 'the clipped scope text renders');
    assert.ok(!clipped.includes(PLAN_LINE), clipped);
    assert.equal(plan, PLAN_LINE, p);
    assert.ok(p.indexOf('<span class="ew-plan-scope">') > p.indexOf('<span title='), 'after the clipped span');
  });
}
test('Plan A scope line: Plan B has no plan element', async () => {
  for (const mode of ['ads', 'mentions', 'episodes']) {
    const { html } = await F.render(`#/search?mode=${mode}&q=ag1`, searchApi(mode, false));
    assert.ok(!html.includes('ew-plan-scope'), mode);
  }
});
test('Plan A scope line: below 600 px the plan sentence takes its own line and is never clipped', () => {
  const s = css('search.css');
  const at = s.indexOf('.ew-scope-line { flex-wrap: nowrap; }');
  assert.ok(at > 0, 'the compact phone scope rule');
  const open = s.lastIndexOf('@media (max-width: 599px) {', at);
  const block = s.slice(open, s.indexOf('\n}\n', at));
  const rule = (block.match(/\n\s*\.ew-scope-line > span\.ew-plan-scope \{([^}]*)\}/) || [])[1] || '';
  assert.match(rule, /flex:\s*1 0 100%/, 'its own line');
  assert.match(rule, /white-space:\s*normal/);
  assert.match(rule, /overflow:\s*visible/);
  assert.match(rule, /text-overflow:\s*clip/);
  // The same specificity as the clipped rule, and later, so it wins.
  assert.ok(block.indexOf('.ew-scope-line > span.ew-plan-scope {') > block.indexOf('.ew-scope-line > span:not(.ew-tag) {'));
  assert.match(block, /\.ew-scope-line:has\(> \.ew-plan-scope\) \{[^}]*flex-wrap:\s*wrap/, 'the line wraps only to hold it');
});

// SUI-R1-02 (lead LEDGER-LEAD3-SUI-2 (a)): Plan A Episodes drops the
// tier words, as the plan-limited Mentions tab does.
const TIER_EPISODES = 'Transcript-only episodes are searched too; they have no ad list.';
test('Plan A Episodes: no tier sentence and no "Full + transcript only" tag', async () => {
  const { html } = await F.render('#/search?mode=episodes&q=ag1', searchApi('episodes', true));
  const { clipped, plan, p } = scopeParts(html);
  assert.equal(clipped, 'All dates, all shows in this collection. 38 episodes in scope.');
  assert.equal(plan, PLAN_LINE);
  assert.ok(!html.includes(TIER_EPISODES));
  assert.ok(!p.includes('Full + transcript only'), p);
  assert.ok(!p.includes('data-tag="tier"'), p);
});
test('Plan A Ads keeps its tier tag; a full-plan Episodes tab keeps its tier sentence and tag', async () => {
  const ads = scopeParts((await F.render('#/search?mode=ads&q=ag1', searchApi('ads', true))).html);
  assert.match(ads.p, /data-tag="tier"/);
  const full = scopeParts((await F.render('#/search?mode=episodes&q=ag1', searchApi('episodes', false))).html);
  assert.equal(full.clipped, 'All dates, all shows in this collection. 38 episodes in scope. ' + TIER_EPISODES);
  assert.match(full.p, /<span class="ew-tag ew-tier ew-tier-full" data-tag="tier">[\s\S]*Full \+ transcript only<\/span>/);
});

test('Plan A scope line: the sentence lives in PLAN_COPY.searchScope', () => {
  assert.match(css('views.js'), /searchScope: 'Your plan searches ad excerpts, not full transcripts\.'/);
});

// SUI-R3-01 (SUI-OWN-2): Plan A All mentions with no words makes no
// request, so nothing is plan-limited; it still carries no tier words,
// because the plan does not search transcripts. Only Ads keeps them.
const route2 = async (win, route) => {
  win.location.hash = route; win.EW.current.route = route;
  await win.EWViews.render();
  await settle(); await settle(); await settle(); await settle();
  return win.document.querySelector('#main').innerHTML;
};
const noWords = async excerpt => {
  const { win } = await F.render('#/search?mode=ads&q=ag1', searchApi('ads', excerpt));
  return route2(win, '#/search?mode=mentions');
};
test('Plan A All mentions with no words (typed or old address): no tier tag, no tier sentence', async () => {
  const html = await noWords(true);
  assert.match(html, /All mentions is not included in your plan\./, 'the Plan A prompt');
  const { clipped, plan, p } = scopeParts(html);
  assert.equal(clipped, 'All dates, all shows in this collection.');
  assert.equal(plan, PLAN_LINE);
  assert.ok(!html.includes(TIER_EPISODES));
  assert.ok(!p.includes('Full + transcript only'), p);
  assert.ok(!p.includes('data-tag="tier"'), p);
});
// Full-plan scope lines, byte for byte as before SUI-R3-01.
const TAG = label => `<span class="ew-tag ew-tier ew-tier-full" data-tag="tier"><span class="ew-tier-bars" aria-hidden="true"><span></span><span></span></span>${label}</span>`;
const LINE = (label, s) => `<p class="ew-scope-line">${TAG(label)}<span title="${s}">${s}</span><a href="/coverage/">What we cover</a></p>`;
const FULL_LINES = {
  ads: LINE('Full coverage', 'All dates, all shows in this collection. 38 episodes in scope. Ads come from full-coverage episodes only.'),
  episodes: LINE('Full + transcript only', 'All dates, all shows in this collection. 38 episodes in scope. ' + TIER_EPISODES),
  mentions: LINE('Full + transcript only', 'All dates, all shows in this collection. 38 episodes in scope. ' + TIER_EPISODES),
};
const fullLine = html => (html.match(/<p class="ew-scope-line">[\s\S]*?<\/p>/) || [])[0];
test('Full plan: the Ads, Episodes and Mentions scope lines are unchanged, byte for byte', async () => {
  for (const mode of ['ads', 'episodes', 'mentions']) {
    const { html } = await F.render(`#/search?mode=${mode}&q=ag1`, searchApi(mode, false));
    assert.equal(fullLine(html), FULL_LINES[mode], mode);
  }
  const html = await noWords(false);
  assert.match(html, /Type words to search all transcripts/);
  assert.equal(fullLine(html), LINE('Full + transcript only', 'All dates, all shows in this collection. ' + TIER_EPISODES), 'mentions, no words');
});

/* -- 3. Overview alert rows ------------------------------------------------ */
const total = (lifecycle, over = {}) => ({ alert_id: 3, alert_name: 'ChatGPT', lifecycle, events: 5, unread: 2, ads: 3, unread_ads: 2,
  new_ads: 0, past_ads: 3, new_unread_ads: 0, latest_past_kind: 'new_finding', latest_past_observed_at: '2026-10-14T09:00:00Z',
  latest_past_after_ref: F.REF(1), query: { version: 2, mode: 'ads', brands: ['chatgpt'] }, ...over });
const ovRow = async t => {
  const { html } = await F.render('#/overview', F.overviewApi({ ov: F.overview({ alert_totals: [t] }) }));
  const row = (html.match(/<li class="ew-ov-alert">[\s\S]*?<\/li>/) || [])[0];
  assert.ok(row, 'the alert row renders');
  return row;
};
const head = row => row.slice(0, row.indexOf('</div>'));
const body = row => F.text((row.match(/<p>([\s\S]*?)<\/p>/) || [])[1] || '').trim();

for (const [lifecycle, label, cls, glyph, line] of [
  ['paused', 'Paused', 'ew-tag-outline', '❙❙', 'Paused. History is kept.'],
  ['stopped', 'Stopped', 'ew-tag-neutral', '■', 'Stopped · read-only. History is kept.']]) {
  test(`Overview: a ${lifecycle} alert has the Alerts page ${label} tag and its own "2 unread" tag`, async () => {
    const h = head(await ovRow(total(lifecycle)));
    assert.match(h, new RegExp(`<span class="ew-tag ${cls} ew-work-state" data-tag="alert-state"><span class="ew-work-glyph" aria-hidden="true">${glyph}</span>${label}</span>`));
    assert.ok(F.text(h).includes('2 unread'), F.text(h));
    assert.ok(!/unread · (paused|stopped)/.test(h), 'the old combined tag is gone');
    assert.ok(!h.includes('New matches'));
  });
  test(`Overview: a ${lifecycle} alert with no new matches says "${line}"`, async () => {
    const text = body(await ovRow(total(lifecycle)));
    assert.ok(text.startsWith(line + ' '), text);
    assert.ok(!text.includes('No new matches.'), text);
    // The rest of the row is the active row's own words (QW7 owns them).
    const active = body(await ovRow(total('active_local')));
    assert.ok(active.startsWith('No new matches. '), active);
    assert.equal(text.slice(line.length), active.slice('No new matches.'.length), 'the past sentence stays');
  });
}

test('Overview: a paused alert with a new match keeps "1 new match" and the Paused tag (SUI-R1-04)', async () => {
  const row = await ovRow(total('paused', { new_ads: 1, new_unread_ads: 1, latest_new_kind: 'new_finding' }));
  const text = body(row);
  assert.ok(text.includes('1 new match'), text);
  assert.ok(!text.includes('History is kept.'), text);
  assert.match(head(row), /data-tag="alert-state"><span class="ew-work-glyph" aria-hidden="true">❙❙<\/span>Paused<\/span>/);
});

test('Overview: a paused alert with no unread shows the Paused tag and no unread tag', async () => {
  const h = head(await ovRow(total('paused', { unread: 0, unread_ads: 0 })));
  assert.ok(h.includes('Paused</span>'));
  assert.ok(!/unread/.test(F.text(h)));
});

test('Overview: an active alert keeps its wording and gets no Paused or Stopped tag', async () => {
  const quiet = await ovRow(total('active_local', { unread: 0, unread_ads: 0 }));
  assert.ok(!/data-tag="alert-state"/.test(head(quiet)), 'no state tag on an active alert');
  assert.ok(body(quiet).startsWith('No new matches.'), body(quiet));
  const fresh = await ovRow(total('active_local', { new_ads: 2, new_unread_ads: 2, latest_new_kind: 'new_finding' }));
  assert.ok(!/data-tag="alert-state"/.test(head(fresh)));
  assert.ok(F.text(head(fresh)).includes('New'), 'the New tag stays');
});

/* -- 4. Phone header Search link ------------------------------------------- */
const SEARCH_LINK = '<a class="button quiet mobile-search" href="#/search">Search</a>';
// SUI-R1-01: the app's boot settles before and after the header, as
// F.render does, so nothing runs after the test ends.
const flushBoot = () => new Promise(r => setTimeout(r, 0));
const headerHtml = async section => {
  const win = F.loadViews();
  for (let i = 0; i < 20; i++) await flushBoot();
  const el = { innerHTML: '' };
  win.document.querySelector = sel => (sel === '#header' ? el : { innerHTML: '', hidden: false, classList: { add() {}, remove() {} } });
  win.EW.header(section);
  for (let i = 0; i < 20; i++) await flushBoot();
  return el.innerHTML;
};
// The section a route draws its header with.
const sectionOf = async route => {
  const { win } = await F.render(route, () => new Error('no data'));
  const sections = [];
  win.EW.header = s => sections.push(s);
  await win.EWViews.render();
  for (let i = 0; i < 20; i++) await flushBoot();
  assert.equal(sections.length > 0, true, `${route} draws a header`);
  return sections[sections.length - 1];
};

test('Phone header: a Search link sits right before Menu', async () => {
  const html = await headerHtml('Overview');
  const at = html.indexOf(SEARCH_LINK), menu = html.indexOf('mobile-menu');
  assert.ok(at > 0, 'the Search link renders');
  assert.ok(at < menu && html.indexOf('<button', at) === at + SEARCH_LINK.length, 'right before Menu');
});

test('Phone header: no Search link on the Search page', async () => {
  assert.ok(!(await headerHtml('Search')).includes('mobile-search'));
});

// SUI-R1-05 (owner: keep): evidence belongs to the Search section, so it
// has no phone Search link either; Overview and Alerts do.
test('Phone header: the Search link is hidden on #/search and #/evidence/…, shown on Overview and Alerts', async () => {
  for (const [route, shown] of [['#/search?q=ag1', false], ['#/evidence/' + encodeURIComponent(F.REF(1)), false],
    ['#/overview', true], ['#/alerts', true]]) {
    const html = await headerHtml(await sectionOf(route));
    assert.equal(html.includes(SEARCH_LINK), shown, route);
  }
});

test('Phone header: the Search link shows below 1024 px only, at least 44 px', () => {
  const s = css('styles.css');
  // The header's phone block: the one that shows Menu.
  const at = s.lastIndexOf('@media (max-width: 1023px)', s.indexOf('.topbar .mobile-menu { display: inline-flex'));
  const phone = s.slice(at, s.indexOf('\n}\n', at));
  const wide = s.slice(0, at);
  assert.match(wide, /\.topbar \.mobile-search\s*\{\s*display:\s*none;/);
  const rule = (phone.match(/\.topbar \.mobile-search\s*\{([^}]*)\}/) || [])[1] || '';
  assert.match(rule, /display:\s*inline-flex/);
  assert.match(rule, /min-height:\s*44px/);
});

// SUI-SCR-R1-01: at 320 px the bar holds wordmark, Search and Menu only
// when its gaps are small.
test('Phone header: the phone bar gap is at most 8 px', () => {
  const s = css('styles.css');
  const at = s.lastIndexOf('@media (max-width: 1023px)', s.indexOf('.topbar .mobile-menu { display: inline-flex'));
  const phone = s.slice(at, s.indexOf('\n}\n', at));
  const rule = (phone.match(/\n\s*\.topbar\s*\{([^}]*)\}/) || [])[1] || '';
  const gap = (rule.match(/(?:^|;)\s*gap:\s*([^;]+);/) || [])[1];
  assert.ok(gap, 'the phone bar sets its own gap');
  const tokens = css('tokens.css');
  const v = gap.trim().replace(/^var\((--[\w-]+)\)$/, (_, name) => (tokens.match(new RegExp(`${name}:\\s*([^;]+);`)) || [])[1] || name);
  assert.match(v, /^\d+px$/, gap);
  assert.ok(parseInt(v, 10) <= 8, `phone bar gap ${gap} = ${v}`);
});

// SUI-R2-03: the phone gap stays inside the phone block; above 1023 px
// the bar keeps its 28 px gap.
test('Desktop header: the .topbar gap stays 28 px above 1023 px', () => {
  const s = css('styles.css');
  const rules = [];
  const stack = [];
  let sel = '', i = 0;
  for (; i < s.length; i++) {
    const ch = s[i];
    if (s.startsWith('/*', i)) { i = s.indexOf('*/', i + 2) + 1; continue; }
    if (ch === '{') { stack.push({ sel: sel.trim(), start: i + 1 }); sel = ''; }
    else if (ch === '}') {
      const top = stack.pop();
      if (top && !top.sel.startsWith('@')) rules.push({ sel: top.sel, body: s.slice(top.start, i), media: stack.map(x => x.sel).filter(x => x.startsWith('@media')) });
      sel = '';
    } else if (ch === ';' && !stack.some(x => !x.sel.startsWith('@'))) sel = '';
    else sel += ch;
  }
  const gaps = rules.filter(r => r.sel.split(',').map(x => x.trim()).includes('.topbar') && /(?:^|;)\s*gap:/.test(r.body));
  const base = gaps.filter(r => !r.media.length);
  assert.equal(base.length, 1, 'one base .topbar gap');
  assert.match(base[0].body, /(?:^|;)\s*gap:\s*28px;/);
  for (const r of gaps.filter(x => x.media.length)) {
    const max = r.media.map(m => +((m.match(/max-width:\s*(\d+)px/) || [])[1] || Infinity));
    assert.ok(Math.min(...max) <= 1023, `a .topbar gap applies above 1023 px: ${r.media.join(' ')} { ${r.body.trim()} }`);
  }
});

/* -- 5. Underlined text-style buttons -------------------------------------- */
test('Quiet and link buttons are underlined; disabled buttons are not', () => {
  const s = css('styles.css');
  const rule = (s.match(/\n\.button\.quiet, \.button\.link \{([^}]*)\}/) || [])[1] || '';
  assert.match(rule, /text-decoration:\s*underline;/);
  assert.match(rule, /text-underline-offset:\s*3px;/);
  assert.match(rule, /text-decoration-thickness:\s*1px;/);
  const quietAt = s.indexOf('\n.button.quiet, .button.link {');
  const disabled = s.indexOf('.button:disabled, .button[aria-disabled=true],');
  assert.ok(disabled > quietAt, 'the disabled rule comes later, so it wins');
  assert.match(s.slice(disabled, s.indexOf('}', disabled)), /text-decoration:\s*none/);
});

// SUI-SCR-R1-02: the phone sheet Close is a bordered button, so no underline.
test('The phone sheet Close button is not underlined', () => {
  const s = css('search.css');
  const rule = (s.match(/\ndialog\.ew-sheet \.dialog-head \.button \{([^}]*)\}/) || [])[1] || '';
  assert.match(rule, /border-color:/, 'the sheet Close rule');
  assert.match(rule, /text-decoration:\s*none;/);
});

/* -- 6. Alert wording: "with brand not identified" (LEDGER-LEAD3-SUI-1) --- */
// The card says "Brand not identified"; the Overview row and the alert
// page header say the same words, never "where we couldn't name the brand".
const OLD_HELD = /couldn(?:'|&#39;|’)t name the brand/;
const heldRow = async n => body(await ovRow(total('active_local', { baseline_held_ads_for_brand_review: n })));
test('Overview: an alert with held ads says "plus N ads with brand not identified" (singular stays "ad")', async () => {
  const one = await heldRow(1);
  assert.ok(one.includes('3 earlier matches, plus 1 ad with brand not identified.'), one);
  const two = await heldRow(2);
  assert.ok(two.includes('3 earlier matches, plus 2 ads with brand not identified.'), two);
  assert.doesNotMatch(one + two, OLD_HELD);
});
const alertHead = async alert => {
  const events = [1, 2, 3].map(i => F.ev(i, 'ep' + i, [i * 10, i * 10 + 5], 'ad' + i, { historical: 1, read_state: 1 }));
  const { html } = await F.render('#/alert/1', F.alertApi({ ...F.ALERT, ...alert }, events));
  const meta = (html.match(/<p class="page-head-meta">([\s\S]*?)<\/p>/) || [])[1];
  assert.ok(meta, 'the alert header meta renders');
  return F.text(meta).trim();
};
test('Alert header: held ads read "· plus N ads with brand not identified" (singular stays "ad")', async () => {
  const one = await alertHead({ baseline_held_for_brand_review: 1, baseline_held_ads_for_brand_review: 1 });
  assert.match(one, /3 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · plus 1 ad with brand not identified/);
  const two = await alertHead({ baseline_held_for_brand_review: 2, baseline_held_ads_for_brand_review: 2 });
  assert.match(two, /3 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · plus 2 ads with brand not identified/);
  assert.doesNotMatch(one + two, OLD_HELD);
});
test('Alert header: an older baseline with held reads only says "· plus N with brand not identified" (no unit)', async () => {
  const old = await alertHead({ baseline_held_for_brand_review: 2 });
  assert.match(old, /3 matches(?: when it was made \([A-Z][a-z]{2} \d{1,2}\))? · plus 2 with brand not identified/);
  assert.doesNotMatch(old, OLD_HELD);
  assert.doesNotMatch(old, /plus 2 ads/);
});
// PUB-2 (lead LEDGER-LEAD3-PUB-2, UX PUB-48 review C1-01): the freeze
// dialog's error names the "final report", as QW7 and the public pages do.
test('Freeze dialog error says "final report", not "report snapshot"', () => {
  const views = css('views.js');
  assert.doesNotMatch(views, /report snapshot/);
  assert.match(views, /The final report could not be read\. Close this and save the report again from the campaign\./);
});
// AP-1 (lead LEDGER-LEAD3-ASKPLAY-2; Meta ASK-PLAY screen review): a
// playing outline Play button kept the outline's dark icon on the blue
// playing fill (~2.1:1). Playing sets the light icon and the blue edge,
// and a hover while playing stays blue, so the icon keeps >= 3:1.
test('AP-1 a playing Play button (outline too) has the light icon on blue, also on hover', () => {
  const s = css('styles.css');
  assert.match(s, /\.ew-play\.is-playing, \.ew-play\[aria-pressed=true\] \{ background: var\(--ew-action\); color: var\(--ew-text-inverse\); border-color: var\(--ew-action\); \}/);
  const hover = s.indexOf('.ew-play-outline.is-playing:hover, .ew-play-outline[aria-pressed=true]:hover { background: var(--ew-action-hover); color: var(--ew-text-inverse); }');
  assert.ok(hover > s.indexOf('.ew-play-outline:hover'), 'the playing hover rule comes after the outline hover rule, so it wins');
});
// SUI-SCR-R5-01 (SMALL-UI screen r5): the Overview Recent ads Play is a link
// (a.ew-play); the global a:hover turned its icon blue on the dark blue
// hover fill (1.27:1). A Play link keeps its button's icon colour on hover:
// light on the filled button, dark on an outline one, light while playing.
test('SUI-SCR-R5-01 a Play link keeps its icon colour on hover (not the global link hover blue)', () => {
  const s = css('styles.css');
  const linkHover = s.indexOf('a.ew-play:hover { color: var(--ew-text-inverse); }');
  assert.ok(linkHover > s.indexOf('a:hover { color: var(--ew-action-hover); }'), 'filled Play link hover keeps the light icon, after the global a:hover');
  assert.ok(s.indexOf('a.ew-play-outline:hover { color: var(--ew-text); }') > linkHover, 'outline Play link hover keeps the dark icon');
  assert.match(s, /\.ew-play-outline\.is-playing:hover, \.ew-play-outline\[aria-pressed=true\]:hover \{ background: var\(--ew-action-hover\); color: var\(--ew-text-inverse\); \}/);
});
