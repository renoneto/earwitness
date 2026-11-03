/* REDESIGN-BUILD phase A (ws-app/briefs/REDESIGN-BUILD.md, logs/redesign/PLAN.md):
   the shared base the five redesigned pages build on.
   1. Route switch: a page module with on:true replaces today's page for its
      route (Overview, Search, Ad, Brand, the sign-in gate); on:false or no
      module keeps today's page. Its loading state paints before the read.
   2. Header: on a redesigned page the header is the s-nav look with today's
      items and order; other pages keep today's header. body.ew-b2 marks it.
   3. Layout: phone below 600 px (AD-DECISIONS item 10), else wide; a width
      change across 600 px renders a redesigned page again.
   4. Events: the current redesigned page's handlers run first (capture) and
      stop today's handler only when they take the event.
   5. Shared components (brand mark with the plain-name rule, cover, Play
      length and button, no-clip text, clip line, counts, quote cut,
      coverage link) and the clip engine keeping a b2 Play button's words.
   Run: node --test tests/frontend_b2_base.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const H = require('./b2_harness.cjs');

const VIEWS = fs.readFileSync(path.join(H.APP, 'views.js'), 'utf8');
const failAll = () => new Error('no fixture in this test');

/* -- 1. Route switch -------------------------------------------------------- */
const ROUTES = [['overview', '#/overview'], ['search', '#/search?q=quince'], ['ad', '#/evidence/sr1:abc'], ['brand', '#/brand/quince']];
for (const [name, route] of ROUTES) {
  test(`route switch: ${name} on replaces today's page; off keeps it`, async () => {
    const calls = [];
    const spec = on => ({ on, render: async (...args) => { calls.push(args); return `<section id="b2-${name}">new ${name}</section>`; } });
    const on = await H.render(route, failAll, { modules: false, pages: { [name]: spec(true) } });
    assert.match(on.html, new RegExp(`id="b2-${name}"`));
    assert.equal(calls.length, 1);
    assert.equal(typeof calls[0][calls[0].length - 1], 'function', 'the old page function is passed last, for fallbacks');
    const off = await H.render(route, failAll, { modules: false, pages: { [name]: spec(false) } });
    assert.doesNotMatch(off.html, new RegExp(`id="b2-${name}"`));
    assert.equal(calls.length, 1, 'an off page never renders');
  });
}

test('route switch: another route never reaches a page module', async () => {
  let called = 0;
  const pages = { search: { on: true, render: async () => { called++; return '<p>x</p>'; } } };
  await H.render('#/alerts', failAll, { modules: false, pages });
  assert.equal(called, 0);
});

test('route switch: the overlay covers the sign-in gate (accessSection) and keeps the rest of the page table', () => {
  const { win } = H.boot({ modules: false });
  const V = { accessSection: () => 'old', searchPage: () => 'old search', alertsPage: () => 'alerts' };
  assert.equal(win.EWB2.overlay(V).accessSection(), 'old');
  win.EWB2.page('signin', { on: true, render: (opts, old) => (opts && opts.page ? 'new sign-in' : old(opts)) });
  const P = win.EWB2.overlay(V);
  assert.equal(P.accessSection({ page: true }), 'new sign-in');
  assert.equal(P.accessSection({}), 'old', 'a non-page call falls back');
  assert.equal(P.alertsPage(), 'alerts');
  assert.equal(P.searchPage(), 'old search');
  // The hosted gate and the member dispatch both read the overlay.
  assert.match(VIEWS, /html = await P\.accessSection\(\{ page: true \}\)/);
  for (const fn of ['overviewPage', 'searchPage', 'evidencePage', 'brandPage']) assert.match(VIEWS, new RegExp(`await P\\.${fn}\\(`), fn);
});

test('route switch: an unknown page name is refused', () => {
  const { win } = H.boot({ modules: false });
  assert.throws(() => win.EWB2.page('pricing', { on: true, render: () => '' }), /unknown page/);
});

test('loading: an on page paints its own loading state before its read answers', async () => {
  let release, seen = null;
  const pages = { search: { on: true, loading: route => `<div id="b2-loading">Searching for ${route.includes('quince') ? 'quince' : '?'}</div>`,
    render: () => new Promise(res => { release = () => res('<p id="b2-done">done</p>'); }) } };
  const out = await H.render('#/search?q=quince', failAll, { modules: false, pages, whileReading: async main => {
    for (let i = 0; i < 40 && !release; i++) await H.flush();
    seen = main();
    release();
  } });
  assert.match(seen, /id="b2-loading">Searching for quince/);
  assert.match(out.html, /id="b2-done"/);
});

/* -- 2. Header ---------------------------------------------------------------- */
function headerAt(route, { on = true, width = 1440, ask = false, hosted = false } = {}) {
  const pages = { search: { on, render: async () => '' }, overview: { on, render: async () => '' } };
  const b = H.boot({ route, width, modules: false, pages });
  b.win.EW.current.route = route;
  b.win.EW.current.askAvailable = ask;
  if (hosted) { b.win.EW.current.hosted = true; b.win.EW.current.principal = { email: 'reno@example.com' }; }
  b.win.EW.header(route.startsWith('#/search') ? 'Search' : route.startsWith('#/overview') ? 'Overview' : 'Alerts');
  return { header: b.els['#header'], body: b.body };
}

test('header: a redesigned page gets the s-nav look with today\'s items, order and current item', () => {
  const { header, body } = headerAt('#/search?q=quince', { ask: true });
  assert.ok(header.classList.contains('s-nav'));
  assert.ok(!header.classList.contains('topbar'));
  assert.ok(body.classList.contains('ew-b2'));
  const h = header.innerHTML;
  const names = [...h.matchAll(/<a class="s-nav__(?:link|current)"[^>]*>([A-Za-z]+)/g)].map(m => m[1]);
  assert.deepEqual(names, ['Overview', 'Search', 'Ask', 'Alerts', 'Brands', 'Shows', 'Campaigns', 'Saved'], 'unchanged nav: today\'s items and order');
  assert.match(h, /<a class="s-nav__current" href="#\/search" aria-current="page" data-label="Search">Search<\/a>/);
  assert.match(h, /Ask<span class="s-nav__new">New<\/span>/);
  assert.match(h, /href="#\/settings"/);
});

test('header: Ask is hidden when Ask is off; Sign out only for a signed-in hosted person', () => {
  const off = headerAt('#/overview', { ask: false }).header.innerHTML;
  assert.doesNotMatch(off, /href="#\/ask"/);
  assert.doesNotMatch(off, /data-action="signout"/);
  const hosted = headerAt('#/overview', { hosted: true }).header.innerHTML;
  assert.match(hosted, /data-action="signout"/);
});

test('header: today\'s pages keep today\'s header and clear the b2 mark', () => {
  for (const [route, on] of [['#/alerts', true], ['#/search?q=quince', false]]) {
    const { header, body } = headerAt(route, { on });
    assert.ok(header.classList.contains('topbar'), route);
    assert.ok(!header.classList.contains('s-nav'), route);
    assert.match(header.innerHTML, /class="primary-nav"/, route);
    assert.ok(!body.classList.contains('ew-b2'), route);
  }
});

test('header: the phone header has the wordmark, a 44 px Menu button and a closed drawer', () => {
  const h = headerAt('#/search?q=quince', { width: 390 }).header;
  assert.ok(h.classList.contains('s-nav--phone'));
  assert.match(h.innerHTML, /<button type="button" class="s-nav__menu" data-b2-menu aria-expanded="false" aria-controls="b2-nav">Menu<\/button>/);
  assert.match(h.innerHTML, /<nav class="s-nav__drawer" id="b2-nav" aria-label="Primary" hidden>/);
});

/* Search audit (audit-search/CHECK.md, base open issue 1) and Landing 407513adc cross #8 */
const B2CSS = fs.readFileSync(path.join(H.APP, 'app-b2.css'), 'utf8');
test('header (audit): nav items are the reference\'s grey and black, never the link blue', () => {
  assert.match(B2CSS, /body\.ew-b2 a\.s-nav__link \{\s*color: var\(--ew-text-2\);\s*\}/);
  assert.match(B2CSS, /body\.ew-b2 a\.s-nav__link:hover \{\s*color: var\(--ew-text\);\s*\}/);
  assert.match(B2CSS, /body\.ew-b2 a:is\(\.s-nav__brand, \.s-nav__brand-m, \.s-nav__current, \.s-nav__account-item\),\s*body\.ew-b2 a:is\(\.s-nav__brand, \.s-nav__brand-m, \.s-nav__current, \.s-nav__account-item\):hover \{\s*color: var\(--ew-text\);\s*\}/);
});
test('header (audit): the avatar shows no "+" or "−", closed or open (styles.css details markers)', () => {
  assert.match(B2CSS, /\.s-nav__account > summary::before,\s*\.s-nav__account\[open\] > summary::before \{\s*content: none;\s*\}/);
});
test('header (audit): below 1200 px the wide header keeps 24 px sides and its links scroll inside it, never the page', () => {
  const m = /@media \(max-width: 1199px\) \{([^@]*?)\n\}/.exec(B2CSS);
  assert.ok(m, 'a 1199 px block');
  assert.match(m[1], /\.s-nav:not\(\.s-nav--phone\) \{\s*padding: 0 24px;\s*gap: 24px;\s*\}/);
  assert.match(m[1], /\.s-nav__links \{\s*min-width: 0;\s*overflow-x: auto;\s*scrollbar-width: none;\s*\}/);
});
test('header (407513adc cross #8): every nav label reserves its bold width; Ask (with its New tag) is left out', () => {
  const h = headerAt('#/search?q=quince', { ask: true }).header.innerHTML;
  assert.match(h, /<a class="s-nav__link" href="#\/overview" data-label="Overview">Overview<\/a>/);
  assert.match(h, /<a class="s-nav__link" href="#\/ask" data-label="">Ask<span class="s-nav__new">New<\/span><\/a>/);
  assert.match(B2CSS, /\.s-nav__links > \[data-label\]:not\(\[data-label=""\]\)::after \{\n  content: attr\(data-label\);/);
  const phone = headerAt('#/search?q=quince', { width: 390 }).header.innerHTML;
  assert.match(phone, /<nav class="s-nav__drawer"[^>]*><a class="s-nav__link" href="#\/overview" data-label="Overview">/);
});

test('Review 4 (search-60fb65a5): the wordmark and the footer link are 40 px targets (44 px on the phone); the footer link stays today\'s grey', () => {
  assert.match(B2CSS, /\.s-nav__brand \{\s*display: inline-flex;\s*align-items: center;\s*min-height: 40px;\s*\}/);
  assert.match(B2CSS, /\.s-nav__brand-m \{\s*display: inline-flex;\s*align-items: center;\s*min-height: 44px;\s*\}/);
  assert.match(B2CSS, /body\.ew-b2 \.footer a \{\s*display: inline-flex;\s*align-items: center;\s*min-height: 40px;\s*color: var\(--ew-text-2\);\s*\}/);
  assert.match(B2CSS, /@media \(max-width: 599px\) \{\s*body\.ew-b2 \.footer a \{\s*min-height: 44px;\s*\}\s*\}/);
});

/* -- 3. Layout -------------------------------------------------------------- */
test('layout: phone below 600 px, wide from 600 px; a crossing renders a redesigned page again', async () => {
  assert.equal(H.boot({ width: 390, modules: false }).win.EWB2.layout(), 'phone');
  assert.equal(H.boot({ width: 599, modules: false }).win.EWB2.layout(), 'phone');
  assert.equal(H.boot({ width: 600, modules: false }).win.EWB2.layout(), 'wide');
  assert.equal(H.boot({ width: 1440, modules: false }).win.EWB2.layout(), 'wide');
  const b = H.boot({ route: '#/search?q=quince', width: 1440, modules: false, pages: { search: { on: true, render: async () => '' } } });
  const mq = b.media.find(m => /max-width:\s*599px/.test(m.media) && m.listeners.length);
  assert.ok(mq, 'b2.js listens for the 600 px crossing');
  let renders = 0;
  b.win.EW.render = () => { renders++; };
  b.win.EW.current.route = '#/search?q=quince';
  mq.listeners[0]({ matches: true });
  assert.equal(renders, 1);
  b.win.EW.current.route = '#/alerts';
  mq.listeners[0]({ matches: false });
  assert.equal(renders, 1, 'today\'s pages are not rendered again');
});

/* -- 4. Events -------------------------------------------------------------- */
test('events: the current redesigned page takes an event first, in the capture phase', () => {
  const took = [];
  const pages = { search: { on: true, render: async () => '', handlers: { click: ev => { took.push(ev.kind); return ev.kind === 'mine'; } } } };
  const b = H.boot({ route: '#/search?q=quince', modules: false, pages });
  b.win.EW.current.route = '#/search?q=quince';
  for (const type of ['click', 'submit', 'change', 'input', 'keydown']) {
    assert.ok(b.listeners.some(l => l.type === type && l.capture), `${type} capture listener`);
  }
  const click = b.listeners.find(l => l.type === 'click' && l.capture).fn;
  let stopped = 0;
  click({ kind: 'mine', stopImmediatePropagation: () => { stopped++; } });
  click({ kind: 'other', stopImmediatePropagation: () => { stopped++; } });
  assert.deepEqual(took, ['mine', 'other']);
  assert.equal(stopped, 1, 'only a taken event stops today\'s handler');
  b.win.EW.current.route = '#/alerts';
  click({ kind: 'mine', stopImmediatePropagation: () => { stopped++; } });
  assert.equal(took.length, 2, 'another route never reaches the page handler');
});

/* -- 5. Components ---------------------------------------------------------- */
const B = () => H.boot({ modules: false }).win.EWB2;

test('brandMark: a logo where the map has one; plain names for ChatGPT, Schwab, Grow Therapy, YouTube Premium; held brands in plain words', () => {
  const b = B();
  assert.equal(b.brandMark({ key: 'quince', display: 'Quince' }, { cls: 's-logo--16' }),
    '<img class="s-logo s-logo--16" src="/public/img/logo-quince.png" alt="Quince" loading="lazy" decoding="async" data-brand-logo>');
  for (const [key, display] of [['chatgpt', 'ChatGPT'], ['charles schwab', 'Charles Schwab'], ['schwab', 'Schwab'], ['grow therapy', 'Grow Therapy'], ['youtube premium', 'YouTube Premium']]) {
    assert.equal(b.brandMark({ key, display }), display, key);
  }
  assert.equal(b.brandMark({ key: 'wayfair', display: 'Wayfair & Co' }), 'Wayfair &amp; Co');
  assert.equal(b.brandMark({ key: 'quince', display: 'Quince', under_review: true }), 'Brand not identified');
  assert.equal(b.hasLogo({ key: 'quince' }), true);
  assert.equal(b.hasLogo({ key: 'chatgpt' }), false);
});

test('cover: real art fills the slot; no art is the initials tile, tinted from the show name', () => {
  const b = B();
  assert.equal(b.cover({ title: 'Prof G Markets', artwork_url: '/api/v1/customer/shows/cs-9/artwork' }),
    '<img class="s-cover" src="/api/v1/customer/shows/cs-9/artwork" alt="" loading="lazy" decoding="async">');
  assert.match(b.cover({ title: 'Prof G Markets', artwork_url: '/x' }, { size: 'sm' }), /^<img class="s-cover s-cover--sm" /);
  const tile = b.cover({ title: 'Two Judgey Girls', artwork_url: null });
  assert.match(tile, /^<div class="s-initials s-initials--t[0-5]" aria-hidden="true">TJ<\/div>$/);
  assert.equal(tile, b.cover({ title: 'Two Judgey Girls' }), 'the tint is stable');
  assert.match(b.cover({ title: 'The Breakfast Club' }, { size: 'xl' }), /class="s-initials s-initials--xl s-initials--t[0-5]"[^>]*>BC</);
  assert.equal(b.cover({ title: 'x', artwork_url: 'https://cdn.example.com/a.jpg' }).startsWith('<div class="s-initials'), true, 'only same-origin art (CSP)');
});

test('playLength: from where Play really starts (ad start - 3 s, never before the clip) to the clip end', () => {
  const b = B();
  // Two Judgey Girls: ad 22:00, clip file 21:55 -> 23:03: Play starts 21:57, plays 66 s.
  assert.equal(b.playLength({ playback: 'local', clip_start_s: 1315, clip_end_s: 1383 }, [1320, 1380]), 66);
  assert.equal(b.playLabel(66), 'Play 1:06');
  // A clip that starts at the ad: the lead-in is clamped to the clip start.
  assert.equal(b.playLength({ playback: 'local', clip_start_s: 1320, clip_end_s: 1380 }, [1320, 1380]), 60);
  assert.equal(b.playLength({ playback: 'local' }, [1320, 1380]), null, 'no clip bounds: no length');
  assert.equal(b.playLength(null, [1, 2]), null);
});

test('play: the Play button runs on today\'s clip engine and names its length; no clip is the plain No audio clip text', () => {
  const b = B();
  const media = { playback: 'local', clip_start_s: 1315, clip_end_s: 1383, source_offset_s: 0 };
  const html = b.play({ ref: 'sr1:abc', media, span: [1320, 1380], id: 'r0' });
  assert.match(html, /<button type="button" class="s-play" data-action="clip-toggle" data-clip="r0" data-b2-play aria-pressed="false">/);
  assert.match(html, /<span class="s-play__disc"><span class="s-play__tri"><\/span><\/span>Play 1:06<\/button>/);
  assert.match(html, /<audio id="clip-r0" class="ew-clip-audio" preload="none" src="\/api\/v1\/customer\/records\/sr1%3Aabc\/media#t=1317\.0" data-clip="r0"><\/audio>/);
  assert.match(html, /<span class="s-clip" data-clip-id="r0" data-start="1320" data-end="1383" data-offset="0" data-lead="1317">/,
    'the clip box holds the bounds the engine reads: Play runs from the lead-in to the clip end (the label\'s length)');
  assert.match(b.play({ ref: 'sr1:abc', media, span: [1320, 1380], id: 'r1', size: 'phone' }), /class="s-play-m"[^>]*>.*s-play__tri s-play__tri--phone/s);
  assert.equal(b.play({ ref: 'sr1:abc', media: { playback: 'none' }, span: [1, 2], id: 'r2' }), '<span class="s-noclip">No audio clip</span>');
  assert.equal(b.noClip(), '<span class="s-noclip">No audio clip</span>');
  assert.doesNotMatch(html, /1:08/);
});

test('playInner: playing shows Pause with two bars; idle restores the button\'s own words', () => {
  const b = B();
  const btn = H.element();
  btn.innerHTML = '<span class="s-play__disc"><span class="s-play__tri"></span></span>Play 1:06';
  btn.setAttribute('data-b2-play', '');
  assert.equal(b.playInner(btn, true), '<span class="s-play__disc"><span class="s-play__pause"><span class="s-play__bar"></span><span class="s-play__bar"></span></span></span>Pause');
  btn.innerHTML = b.playInner(btn, true);
  assert.equal(b.playInner(btn, false), '<span class="s-play__disc"><span class="s-play__tri"></span></span>Play 1:06');
  const big = H.element();
  big.innerHTML = '<span class="a-play__disc a-play__disc--phone"><span class="a-play__tri a-play__tri--phone"></span></span><span>Play 1:06</span>';
  assert.equal(b.playInner(big, true), '<span class="a-play__disc a-play__disc--phone"><span class="a-play__pause"><span class="a-play__pause-bar a-play__pause-bar--phone"></span><span class="a-play__pause-bar a-play__pause-bar--phone"></span></span></span><span>Pause</span>');
});

test('clip engine: clipPresent keeps a b2 Play button\'s words and never puts the icon-only label on it', () => {
  const { win } = H.boot({ modules: false });
  const { clipPresent } = win.EWViews._b2;
  const box = H.element(); const btn = H.element();
  btn.innerHTML = '<span class="s-play__disc"><span class="s-play__tri"></span></span>Play 1:06';
  btn.setAttribute('data-b2-play', '');
  clipPresent(box, [btn], true);
  assert.match(btn.innerHTML, /s-play__pause/);
  assert.match(btn.innerHTML, /Pause$/);
  assert.equal(btn.getAttribute('aria-pressed'), 'true');
  assert.equal(btn.getAttribute('aria-label'), null, 'the visible words are the name');
  clipPresent(box, [btn], false);
  assert.equal(btn.innerHTML, '<span class="s-play__disc"><span class="s-play__tri"></span></span>Play 1:06');
  // Today's buttons are unchanged.
  const old = H.element(); old.setAttribute('aria-label', 'Play clip, 22:00 to 23:00');
  clipPresent(box, [old], true);
  assert.equal(old.getAttribute('aria-label'), 'Pause clip');
  assert.match(old.innerHTML, /<svg/);
});

test('clipLine: the D6-P words in all four forms; never "of {total}"', () => {
  const b = B();
  const lead = 'Every ad shows what the host said and when in the episode.';
  assert.equal(b.clipLine(8, 99), `${lead} 8 have a clip you can play.`);
  assert.equal(b.clipLine(1, 99), `${lead} 1 has a clip you can play.`);
  assert.equal(b.clipLine(0, 99), lead);
  assert.equal(b.clipLine(13, 13), `${lead} Every ad also has a clip you can play.`);
  assert.doesNotMatch(b.clipLine(8, 99), / of /);
});

test('count, at and dates: "1 ad", "{m:ss} into the episode"', () => {
  const b = B();
  assert.equal(b.count(1, 'ad'), '1 ad');
  assert.equal(b.count(99, 'ad'), '99 ads');
  assert.equal(b.count(1500, 'episode'), '1,500 episodes');
  assert.equal(b.at(1323), '22:03 into the episode');
  assert.equal(b.at(3723), '1:02:03 into the episode');
});

test('cutQuote: cut at a word end, no space before the ellipsis, never after a period', () => {
  const b = B();
  assert.equal(b.cutQuote('short words', 40), 'short words');
  assert.equal(b.cutQuote('one two three four five six', 15), 'one two three…');
  assert.equal(b.cutQuote('versus out layers and more words here', 18), 'versus out layers…');
  assert.equal(b.cutQuote('Go to quince.com. Get free shipping today', 20), 'Go to quince.com.');
  assert.doesNotMatch(b.cutQuote('a b c d e f g h i j k l m n o p', 9), / …$/);
});

test('RJC-02: coverageLink uses singular forms for 1 episode and 1 show', () => {
  const b = B();
  assert.match(b.coverageLink({ episodes: 1, shows: 1, screened_episodes: 1, screened_shows: 1 }), /Ads checked<\/b> in all 1 episode from 1 show\./);
  assert.match(b.coverageLink({ episodes: 2, shows: 1, screened_episodes: 1, screened_shows: 1 }), /Ads checked<\/b> in 1 of 2 episodes \(1 of 1 show\)\./);
});

test('coverageLink: one /coverage/ link, the chip form and the inline form', () => {
  const b = B();
  const cov = { episodes: 1500, shows: 796, screened_episodes: 1500, screened_shows: 796 };
  assert.equal(b.coverageLink(cov), '<a class="s-coverage" href="/coverage/"><span class="s-checked"><b class="s-strong">Ads checked</b> in all 1,500 episodes from 796 shows.</span></a>');
  assert.equal(b.coverageLink(cov, { inline: true }), '<a class="s-coverage-inline" href="/coverage/">all 1,500 episodes from 796 shows</a>');
});

test('longName: the full text stays in title', () => {
  const b = B();
  assert.equal(b.longName('ROI Podcast™ — the #1 & more', 's-row__name'), '<span class="s-row__name" title="ROI Podcast™ — the #1 &amp; more">ROI Podcast™ — the #1 &amp; more</span>');
});

test('views.js exports the internals the page modules use (EWViews._b2)', () => {
  const { win } = H.boot({ modules: false });
  const want = ['adSpan', 'localMedia', 'clipAudio', 'clipBounds', 'clipPresent', 'episodeSeconds', 'longDate', 'backLink', 'backLabel',
    'brandHeardLine', 'heardAs', 'markedText', 'windowHtml', 'factRows', 'READ_KIND_WORDS', 'positionStrip',
    'cellRouteOrNull', 'weekChart', 'recentPick', 'trackButton', 'alertDialog', 'campaignAttachDialog', 'podSection',
    'aboutRecord', 'readRow', 'passwordSigninForm', 'submitPasswordSignin', 'signinMethods', 'stopSearchPlayer'];
  const got = win.EWViews._b2 || {};
  assert.deepEqual(want.filter(k => got[k] === undefined), []);
});
