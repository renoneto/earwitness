/* FIRST-SCREEN (UX audit M1, quick win 4, lead plan LEDGER-LEAD3-UXQW-1):
   1. Overview says what EarWitness is, in one line under its title.
   2. Below it, "Try a search:" and three chips (wayfair, mint mobile,
      betterhelp; lead LEDGER-LEAD3-APP-12, FS-SCR-R1-02), each a real link
      to Search (Ads view) for its query, as the Search box opens it.
   3. Recent ads sits above Alert activity in the same column, so it comes
      first at 1440 and at 390.
   4. The signed-out sign-in page says the same line before "Sign in to
      reach your workspaces.", with the password form and with the provider
      button alone.
   Run: node --test tests/frontend_first_screen.test.cjs */
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const F = require('./frontend_uat_d1b_fixtures.cjs');

const LINE = 'EarWitness finds the ads in podcasts and shows the exact moment, so a brand can check that its ad ran as bought.';
const SIGNIN = LINE + ' Sign in to reach your workspaces.';
const CHIPS = ['wayfair', 'mint mobile', 'betterhelp'];
const text = F.text;
const esc = s => s.replace(/&/g, '&amp;');

test('first screen: one line under the Overview title, then Try a search: and three chips', async () => {
  const { html } = await F.render('#/overview', F.overviewApi());
  const head = html.slice(0, html.indexOf('</div></div>') + 12);
  assert.match(head, /<h1>Overview<\/h1><p>([^<]*)<\/p>/, 'the line sits right under the title');
  assert.equal(head.match(/<h1>Overview<\/h1><p>([^<]*)<\/p>/)[1], LINE);
  const at = text(html);
  const line = at.indexOf(LINE), label = at.indexOf('Try a search:');
  assert.ok(line >= 0 && label > line, 'Try a search: comes below the line');
  const after = at.slice(label + 'Try a search:'.length).trim();
  assert.ok(after.startsWith(CHIPS.join(' ')), `the three chips follow, in order: ${after.slice(0, 40)}`);
  assert.equal(at.split('Try a search:').length, 2, 'one label');
  // Exactly these three chips, in this order (FS-SCR-R1-02: quince and
  // shopify were swapped out; chip text is the brand query only).
  const chips = [...html.matchAll(/<a [^>]*class="[^"]*ew-chip[^"]*"[^>]*>([^<]*)<\/a>/g)].map(m => m[1]);
  assert.deepEqual(chips, CHIPS);
});

test('first screen: each chip is a real link to Search (Ads view) for its query, named by its text', async () => {
  const { html, win } = await F.render('#/overview', F.overviewApi());
  for (const q of CHIPS) {
    const want = win.EW.searchURL({ mode: 'ads', q, page: 1 });
    assert.match(want, /^#\/search\?/);
    assert.match(want, /mode=ads/);
    const links = [...html.matchAll(/<a ([^>]*)>([^<]*)<\/a>/g)].filter(m => m[2] === q);
    assert.equal(links.length, 1, `one link named "${q}"`);
    const attrs = links[0][1];
    assert.ok(attrs.includes(`href="${esc(want)}"`), `"${q}" opens ${want}: ${attrs}`);
    assert.doesNotMatch(attrs, /aria-label|tabindex="-1"|aria-hidden/, 'named by its text and keyboard reachable');
    assert.match(attrs, /class="[^"]*ew-chip/, 'chip style (40 px target)');
  }
  // The route reads back as the Ads view of that query.
  const q = new URLSearchParams(win.EW.searchURL({ mode: 'ads', q: 'mint mobile', page: 1 }).split('?')[1]);
  assert.equal(q.get('mode'), 'ads');
  assert.equal(q.get('q'), 'mint mobile');
});

test('first screen: chip targets stay at least 24 px tall', () => {
  const tokens = fs.readFileSync(path.join(F.APP, 'tokens.css'), 'utf8');
  const target = Number((tokens.match(/--ew-target:\s*(\d+)px/) || [])[1]);
  assert.ok(target >= 24, 'the shared target is at least 24 px');
  const css = fs.readFileSync(path.join(F.APP, 'styles.css'), 'utf8') + fs.readFileSync(path.join(F.APP, 'search.css'), 'utf8');
  assert.match(css, /\.ew-chip[^{]*\{[^}]*min-height:\s*var\(--ew-target\)/);
  for (const m of css.matchAll(/\.ew-ov-try[^{]*\{([^}]*)\}/g)) {
    assert.doesNotMatch(m[1], /(?:^|;|\s)(?:min-)?height:|max-height:/, 'no Overview rule shrinks the chip');
  }
});

test('first screen: Recent ads comes before Alert activity, in the same column (1440 and 390)', async () => {
  const { html } = await F.render('#/overview', F.overviewApi());
  const recent = html.indexOf('ew-ov-recent'), alerts = html.indexOf('<h2>Alert activity</h2>');
  assert.ok(recent > 0 && alerts > 0, 'both cards render');
  assert.ok(recent < alerts, 'Recent ads first');
  const main = html.indexOf('class="ew-ov-main"'), side = html.indexOf('class="ew-ov-side"');
  assert.ok(main < recent && recent < side && alerts < side, 'both in the main column, so the order holds side by side too');
  assert.match(html.slice(recent, alerts), /<h2>Recent ads<\/h2>/, 'its existing heading');
  // No stylesheet reorders the Overview cards.
  const css = fs.readFileSync(path.join(F.APP, 'search.css'), 'utf8');
  for (const m of css.matchAll(/\.ew-ov-[^{]*\{([^}]*)\}/g)) assert.doesNotMatch(m[1], /(?:^|[;\s])order\s*:/);
});

test('first screen: Recent ads keeps its picking (no listed ad, clips first)', async () => {
  const ov = (n, { clip = false, bleed = false } = {}) => F.ovItem(n, 'brk:' + n, {
    ep_title: 'Episode ' + n, ...(bleed ? { known_bleed: true } : {}),
    read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: false }, display_span: [60, 120], has_clip: clip } });
  const items = [ov(1, { clip: true, bleed: true }), ov(2), ov(3, { clip: true }), ov(4, { clip: true }), ov(5, { clip: true })];
  const { html } = await F.render('#/overview', F.overviewApi({ items }));
  const s = html.indexOf('ew-ov-recent');
  const c = text(html.slice(s, html.indexOf('</section>', s)));
  assert.deepEqual([1, 2, 3, 4, 5].filter(n => new RegExp(`Episode ${n}\\b`).test(c)), [3, 4, 5]);
});

/* -- signed-out sign-in page (both branches) -- */
function signedOutPage(signin) {
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, setTimeout, clearTimeout,
    location: { hash: '#/overview', pathname: '/app/', replace() {}, reload() {} },
    history: { replaceState() {} }, scrollTo() {},
    fetch: async (url) => {
      if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
        csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
      throw new Error('no network in unit test');
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t },
    window: {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(F.APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox.window;
  win.addEventListener = () => {};
  win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  vm.runInContext(fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  return async () => {
    for (let i = 0; i < 10; i++) await new Promise(r => setTimeout(r, 0));
    const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
    win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', classList: { add() {}, remove() {} } });
    win.document.querySelectorAll = () => [];
    win.EW.header = () => {}; win.EW.toast = () => {}; win.EW.saveNotice = () => {};
    win.EW.current.hosted = true;
    win.EW.current.principal = null;
    win.EW.current.signin = null;
    win.EW.api = async () => { throw Object.assign(new Error('browser sign-in is required'), { status: 401, body: { error: { code: 'SIGNIN_REQUIRED' }, signin } }); };
    win.location.hash = '#/overview';
    win.EW.current.route = '#/overview';
    await win.EWViews.render();
    for (let i = 0; i < 10; i++) await new Promise(r => setTimeout(r, 0));
    return main.innerHTML;
  };
}

test('sign-in page: the line, then "Sign in to reach your workspaces.", with the password form and with the provider button', async () => {
  const password = await signedOutPage({ available: true, reason: '', login_url: null, methods: ['password'] })();
  const provider = await signedOutPage({ available: true, reason: '', login_url: '/api/v1/session/login', methods: ['oidc'] })();
  const older = await signedOutPage({ available: true, reason: '', login_url: '/api/v1/session/login' })();
  assert.match(password, /id="signin-password"/, 'fixture: the password branch');
  assert.doesNotMatch(provider, /id="signin-password"/, 'fixture: the provider branch');
  for (const [name, html] of [['password', password], ['provider', provider], ['older server', older]]) {
    assert.ok(html.includes(`<p class="section-note">${SIGNIN}</p>`), `${name}: the sentence, exact`);
    assert.equal(html.split('reach your workspaces').length, 2, `${name}: said once`);
  }
});

test('sign-in page: sign-in not set up keeps its own words', async () => {
  const off = await signedOutPage({ available: false, reason: 'not configured', login_url: null, methods: [] })();
  assert.match(off, /Sign-in isn't set up for this site yet\./);
  assert.ok(!off.includes(LINE), 'no new line on the not-set-up state');
});

/* -- BL-033 (lead LEDGER-LEAD3-INV-1; RQ-SCR-R1-04, JS-SCR-R2-08): a Search
   address with words opens with its words when the browser announces the
   arrival. A chip click (the app's own link handling) and an announced
   fragment navigation both open Search (Ads view) with the box holding the
   words, as a page load does. Chromium announces an address typed or
   pasted into the open app, a bookmark and a script's location.hash;
   WebKit announces only location.hash, so its typed, pasted and bookmarked
   addresses in the open tab open fresh, as before (FS-R1-01; real Safari
   unverified). The words still leave the address bar (SEC-R12-02), and
   Back/Forward onto an entry the app does not own still opens Search
   fresh. The browser says which navigation it is through the Navigation
   API (navigationType push/replace/traverse). */
const BOOT_FETCH = async (url) => {
  if (url === '/api/v1/session') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, authenticated: false,
    csrf_token: 's'.repeat(32), workspace_context: 'ctx-ws', workspace_switch_revision: 7, frontend_contract: 'earwitness/customer-ui/2' }) };
  if (url === '/api/v1/customer/preferences') return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ ok: true, workspace_context: 'ctx-ws', preferences: { revision: 1 }, revision: 1 }) };
  throw new Error('no network in unit test');
};
const tick = () => new Promise(r => setTimeout(r, 0));
const drain = async (n = 60) => { for (let i = 0; i < n; i++) await tick(); };
async function liveApp({ navigationApi = true, announceWrites = true } = {}) {
  const listeners = {}, docListeners = {}, navListeners = [];
  const B = { entries: [{ url: '#/overview', state: null }], i: 0 };
  B.cur = () => B.entries[B.i];
  const clone = v => (v == null ? null : JSON.parse(JSON.stringify(v)));
  // FS-R1-02: the signals both real engines send (Chromium and WebKit,
  // first-screen-code-r1 navigate-events.txt): the app's own pushState and
  // replaceState fire navigate (push/replace, hashChange false); a script's
  // location.hash fires navigate (push, hashChange true); Back/Forward fire
  // navigate (traverse) before popstate. A refused history write (embedded
  // previews) is not modelled. announceWrites false: an engine that
  // announces fragment navigations but not history writes (a partial
  // Navigation API; neither engine checked behaves so).
  const navigate = (type, href, hashChange = true) => { for (const fn of navListeners) fn({ navigationType: type, hashChange, destination: { url: 'http://127.0.0.1:9153/app/' + href } }); };
  const location = { href: 'http://127.0.0.1:9153/app/#/overview', pathname: '/app/', replace() {}, reload() {} };
  Object.defineProperty(location, 'hash', { configurable: true, get: () => B.cur().url, set: v => { navigate('push', v); B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: v, state: null }); B.i++; } });
  const sandbox = {
    console, URLSearchParams, AbortController, Intl, URL, setTimeout, clearTimeout, location, scrollTo() {},
    history: { get state() { return clone(B.cur().state); }, get length() { return B.entries.length; },
      pushState(st, ti, u) { const url = u === undefined ? B.cur().url : u; if (announceWrites) navigate('push', url, false); B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url, state: clone(st) }); B.i++; },
      replaceState(st, ti, u) { const url = u === undefined ? B.cur().url : u; if (announceWrites) navigate('replace', url, false); B.entries[B.i] = { url, state: clone(st) }; } },
    fetch: BOOT_FETCH,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
      createElement: () => ({ dataset: {}, appendChild() {} }), createTextNode: t => t },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    window: {},
  };
  if (navigationApi) sandbox.navigation = { addEventListener: (t, fn) => { if (t === 'navigate') navListeners.push(fn); } };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  for (const file of ['app.js', 'ui.js', 'saved.js']) vm.runInContext(fs.readFileSync(path.join(F.APP, file), 'utf8'), sandbox, { filename: file });
  const win = sandbox.window;
  win.document.querySelector = () => ({ innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  vm.runInContext(fs.readFileSync(path.join(F.APP, 'views.js'), 'utf8'), win, { filename: 'views.js' });
  await drain(10);
  const E = win.EW;
  E.current.hosted = false;
  const main = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
  win.document.querySelector = sel => (sel === '#main' ? main : { innerHTML: '', textContent: '', setAttribute() {}, focus() {}, classList: { add() {}, remove() {} } });
  win.document.querySelectorAll = () => [];
  win.matchMedia = () => ({ matches: false });
  E.header = () => {}; E.toast = () => {}; E.saveNotice = () => {};
  const queries = [];
  const overviewApi = F.overviewApi();
  E.api = async (p, o = {}) => {
    if (p === '/customer/research-query') {
      const q = (o.body && o.body.query) || {};
      queries.push(q);
      const items = q.q ? [{ ref: 'sr1:' + 'b'.repeat(32), record_kind: 'evidence', status: 'finding', ep_title: 'Result for ' + q.q, show_title: 'Show', pub_date: '2026-10-21', text: 'about ' + q.q }]
        : [{ ref: 'sr1:' + 'c'.repeat(32), record_kind: 'evidence', status: 'finding', ep_title: 'Unfiltered newest ad', show_title: 'Show', pub_date: '2026-10-21', text: 'anything' }];
      return { query: { ...q }, revision: 'rev-1', result: { items, total: q.q ? 1 : 6617, per_page: 10, total_relation: 'exact' }, coverage: {}, summary: {} };
    }
    if (p === '/customer/shows') return { shows: [] };
    if (p === '/customer/preferences') return { preferences: { revision: 1 }, revision: 1 };
    const out = overviewApi(p, o);
    if (out instanceof Error) throw out;
    return out;
  };
  location.hash = '#/overview';
  B.entries = [{ url: '#/overview', state: null }]; B.i = 0;
  E.current.route = '#/overview';
  await win.EWViews.render();
  await drain();
  const fire = (t, ev) => { for (const fn of listeners[t] || []) fn(ev); };
  return {
    win, E, B, main, queries,
    // A plain click on an in-app link: the app's own link handling.
    async click(href) {
      const anchor = { getAttribute: k => (k === 'href' ? href : null), hasAttribute: () => false, target: '' };
      const ev = { button: 0, target: { closest: sel => (sel === 'a[href^="#/"]' ? anchor : null) }, preventDefault() {} };
      for (const fn of docListeners.click || []) fn(ev);
      await drain();
    },
    // A fragment navigation the browser makes and announces (Chromium:
    // address bar, bookmark, script; WebKit: script): navigate (push), a
    // new entry with no state, then popstate and hashchange.
    async fragment(href) {
      location.hash = href;
      fire('popstate', { state: null });
      fire('hashchange', {});
      await drain();
    },
    // A fragment navigation with no navigate event (WebKit headless: an
    // address typed, pasted or bookmarked into the open tab): a new entry
    // (none for the address already shown), then popstate and hashchange.
    async silent(href) {
      if (B.cur().url !== href) { B.entries = B.entries.slice(0, B.i + 1); B.entries.push({ url: href, state: null }); B.i++; }
      fire('popstate', { state: null });
      fire('hashchange', {});
      await drain();
    },
    // A navigate the app sees no route event for (held, intercepted).
    announce(href) { navigate('push', href); },
    async traverse(d) {
      const old = B.cur().url;
      navigate('traverse', B.entries[B.i + d].url);
      B.i += d;
      fire('popstate', { state: clone(B.cur().state) });
      if (B.cur().url !== old) fire('hashchange', {});
      await drain();
    },
  };
}
const boxValue = html => { const m = String(html).match(/<input id="search-query" name="q" type="search" value="([^"]*)"/); return m ? m[1].replace(/&amp;/g, '&') : null; };
const assertOpened = (t, q, how) => {
  const want = t.E.searchURL({ mode: 'ads', q, page: 1 });
  assert.equal(t.E.current.route, want, `${how}: the route keeps the words`);
  assert.equal(boxValue(t.main.innerHTML), q, `${how}: the box holds the words`);
  const last = t.queries[t.queries.length - 1] || {};
  assert.equal(last.q, q, `${how}: Search read the words`);
  assert.equal(last.mode, 'ads', `${how}: the Ads view`);
  assert.match(text(t.main.innerHTML), new RegExp('Result for ' + q), `${how}: the results for the words`);
  assert.doesNotMatch(text(t.main.innerHTML), /Unfiltered newest ad/, `${how}: not the unfiltered list`);
  assert.equal(t.B.cur().url, '#/search', `${how}: the words leave the address bar (SEC-R12-02)`);
};

for (const q of CHIPS) {
  test(`BL-033: clicking the "${q}" chip on Overview, no reload, opens Search (Ads) with the box holding "${q}"`, async () => {
    const t = await liveApp();
    assert.match(t.main.innerHTML, /Try a search:/, 'fixture: Overview');
    await t.click(t.E.searchURL({ mode: 'ads', q, page: 1 }));
    assertOpened(t, q, 'chip click');
  });
  test(`BL-033: a fragment navigation to the "${q}" chip's address, no reload, opens the same as a page load`, async () => {
    const t = await liveApp();
    await t.fragment(t.E.searchURL({ mode: 'ads', q, page: 1 }));
    assertOpened(t, q, 'fragment navigation');
  });
}

test('BL-033: any Search address with words and filters, arriving by fragment navigation, keeps them (not just the chips)', async () => {
  const t = await liveApp();
  const href = t.E.searchURL({ mode: 'ads', q: 'free shipping', brands: ['quince'], page: 1 });
  await t.fragment(href);
  assert.equal(t.E.current.route, href);
  assert.equal(boxValue(t.main.innerHTML), 'free shipping');
  const last = t.queries[t.queries.length - 1];
  assert.equal(last.q, 'free shipping');
  assert.deepEqual([...last.brands], ['quince']);
  assert.equal(t.B.cur().url, '#/search');
});

test('BL-033 / SEC-R12-02 control: Back onto an entry the app does not own, its URL holding words, still opens Search fresh', async () => {
  const t = await liveApp();
  const href = t.E.searchURL({ mode: 'ads', q: 'PRIVATE-WORDS', page: 1 });
  t.B.entries = [{ url: href, state: null }, { url: '#/overview', state: t.B.cur().state }];
  t.B.i = 1;
  await t.traverse(-1);
  assert.match(t.E.current.route, /^#\/search/, 'fixture: Back reached Search');
  assert.doesNotMatch(t.E.current.route, /PRIVATE-WORDS/, 'Back restored words from an entry the app does not own');
  assert.doesNotMatch(t.main.innerHTML, /PRIVATE-WORDS/);
  assert.equal(t.B.cur().url, '#/search', 'the words leave the address bar');
});

test('BL-033: the same person\'s Back to the search they opened by address keeps the words', async () => {
  const t = await liveApp();
  await t.fragment(t.E.searchURL({ mode: 'ads', q: 'quince', page: 1 }));
  await t.click('#/brands');
  await t.traverse(-1);
  assertOpened(t, 'quince', 'Back to the search');
});

test('BL-033: with no Navigation API the browser cannot say push from Back, so the entry opens fresh, as before', async () => {
  const t = await liveApp({ navigationApi: false });
  await t.fragment(t.E.searchURL({ mode: 'ads', q: 'quince', page: 1 }));
  assert.match(t.E.current.route, /^#\/search/);
  assert.doesNotMatch(t.E.current.route, /quince/);
  assert.equal(t.B.cur().url, '#/search');
});

/* FS-R1-02: each guard on the BL-033 signal, pinned. The words come back
   only for the address the browser's last fragment navigation made, once,
   and the entry still inherits nothing. */
const assertFresh = (t, words, how) => {
  assert.match(t.E.current.route, /^#\/search/, `${how}: fixture: Search opened`);
  assert.ok(!t.E.current.route.includes(words), `${how}: the route lost the words`);
  assert.ok(!String(t.main.innerHTML).includes(words), `${how}: the page shows no words`);
  assert.equal(t.B.cur().url, '#/search', `${how}: the words leave the address bar`);
};

test('BL-033 guard (hash match): a navigate to one address does not make a route event at another address explicit', async () => {
  const t = await liveApp();
  t.announce(t.E.searchURL({ mode: 'ads', q: 'first words', page: 1 }));
  await t.silent(t.E.searchURL({ mode: 'ads', q: 'OTHER-WORDS', page: 1 }));
  assertFresh(t, 'OTHER-WORDS', 'route event at another address');
});

test('BL-033 guard (used once): a later route event at the same address, with no navigate between, opens fresh', async () => {
  // Both engines announce the app's own history writes, and that clears
  // the value too; this pins the route event's own reset for an engine
  // that announces fragment navigations only.
  const t = await liveApp({ announceWrites: false });
  const href = t.E.searchURL({ mode: 'ads', q: 'ONCE-WORDS', page: 1 });
  await t.fragment(href);
  assert.equal(t.E.current.route, href, 'fixture: the first arrival keeps the words');
  await t.click('#/brands');
  assert.equal(t.E.current.route, '#/brands', 'fixture: an in-app move');
  await t.silent(href);
  assertFresh(t, 'ONCE-WORDS', 'second route event');
});

test('BL-033 guard (fresh): an address arrival inherits no Back cache, scroll or focus its route kept (pass 24/26 owner rule)', async () => {
  const t = await liveApp();
  const href = t.E.searchURL({ mode: 'ads', q: 'quince', page: 1 });
  await t.click(href);
  await t.click('#/brands');
  assert.equal(t.E.current.route, '#/brands', 'fixture: another page on screen');
  t.E.current.back.set(href, '#/brand/quince');
  t.E.current.scroll.set(href, 777);
  t.E.current.focus.set(href, { id: 'search-query' });
  await t.fragment(href);
  assertOpened(t, 'quince', 'address arrival');
  assert.equal(t.E.current.back.has(href), false, 'no Back cache inherited');
  assert.equal(t.E.current.scroll.has(href), false, 'no scroll inherited');
  assert.equal(t.E.current.focus.has(href), false, 'no focus inherited');
});

test('BL-033 guard (hashChange): an entry made by pushState is not explicit, even when a route event then lands on its address', async () => {
  const t = await liveApp();
  const href = t.E.searchURL({ mode: 'ads', q: 'PUSHED-WORDS', page: 1 });
  // A script's pushState: navigate (push, hashChange false), no route event.
  t.win.history.pushState(null, '', href);
  await t.silent(href);
  assertFresh(t, 'PUSHED-WORDS', 'route event on a pushState entry');
});

/* FS-R2-01: the two BL-033 layers r2 found unpinned. An address arrival is
   "fresh" (info.fresh): for the route already on screen that is a restore
   boundary, so the page reads again; and every navigate first clears the
   last announced address, so a later Back/Forward onto an entry at that
   address does not count as the arrival. */
test('BL-033 guard (fresh, route on screen): an announced arrival at the Search address on screen reads Search again', async () => {
  const t = await liveApp();
  await t.click('#/search');
  assert.equal(t.E.current.route, '#/search', 'fixture: the plain Search page');
  const before = t.queries.length;
  await t.fragment('#/search');
  assert.equal(t.E.current.route, '#/search');
  assert.ok(t.queries.length > before, 'the arrival at the route on screen did not read Search again');
});

test('BL-033 guard (listener reset): an announced address, then Forward onto an entry at that address, opens fresh', async () => {
  const t = await liveApp();
  const href = t.E.searchURL({ mode: 'ads', q: 'FORWARD-WORDS', page: 1 });
  // An entry at the address ahead, which the app does not own.
  t.B.entries = [t.B.cur(), { url: href, state: null }];
  t.B.i = 0;
  // A push announced for that address whose route event never came (held).
  t.announce(href);
  await t.traverse(+1);
  assertFresh(t, 'FORWARD-WORDS', 'Forward after an announced address');
});

/* -- Overview Recent ads Play (lead LEDGER-LEAD3-APP-6 item 4): a row whose
   ad has a clip carries the Brand page's Play control, its label pattern
   unchanged; a row without a clip has no button and no placeholder.
   VIS-NOV1 #12: that control plays the clip in place (the Search player's
   button), so its name no longer says "on its evidence page". */
const playOf = html => [...String(html).matchAll(/<button type="button" class="ew-play"[^>]*>[\s\S]*?<\/button>/g)].map(m => m[0]);
const recentCardOf = html => { const s = html.indexOf('ew-ov-recent'); return html.slice(s, html.indexOf('</section>', s)); };
const rowsOf = card => card.split('<li>').slice(1);
const clipItem = (n, clip) => F.ovItem(n, 'brk:' + n, { ep_title: 'Episode ' + n, read: { brand: { key: 'chatgpt', display: 'ChatGPT', confirmed: true }, display_span: [60, 120], has_clip: clip } });

test('Recent ads Play: a row with a clip gets the Brand page\'s Play control and label; rows without get none', async () => {
  const items = [clipItem(1, true), clipItem(2, false), clipItem(3, true)];
  const { html } = await F.render('#/overview', F.overviewApi({ items }));
  const rows = rowsOf(recentCardOf(html));
  assert.equal(rows.length, 3, 'fixture: three rows');
  const byEp = n => rows.find(r => r.includes('Episode ' + n) || r.includes(encodeURIComponent(F.REF(n))));
  // The Brand page's control for the same ad (same brand, show and date).
  const brand = await F.render('#/brand/chatgpt', F.brandApi(F.brandDetail(), [clipItem(1, true), clipItem(3, true)]));
  const brandPlays = playOf(brand.html);
  assert.equal(brandPlays.length, 2, 'fixture: the Brand page shows two Play controls');
  for (const [n, i] of [[1, 0], [3, 1]]) {
    const plays = playOf(byEp(n));
    assert.equal(plays.length, 1, `row ${n}: one Play control`);
    assert.equal(plays[0], brandPlays[i], `row ${n}: the Brand page's control, unchanged`);
    assert.ok(plays[0].includes(`data-action="search-play" data-ref="${F.REF(n)}"`), `row ${n}: plays its own ad`);
    assert.match(plays[0], /aria-label="Play ChatGPT ad, Show A, Oct 9, 1:00 to 2:00"/, `row ${n}: the label pattern`);
  }
  // FS-R1-03: Play is the row's first focusable control, before the brand link.
  for (const n of [1, 3]) {
    const row = byEp(n);
    assert.ok(row.startsWith('<div class="ew-ov-row"><button type="button" class="ew-play"'), `row ${n}: Play comes first: ${row.slice(0, 60)}`);
    assert.ok(row.indexOf('class="ew-play"') < row.indexOf('ew-ov-lead'), `row ${n}: Play before the brand link`);
  }
  const none = byEp(2);
  assert.equal(playOf(none).length, 0, 'row without a clip: no Play');
  assert.doesNotMatch(none, /ew-play-none|aria-label="No clip"|—/, 'row without a clip: no placeholder');
});

test('Recent ads Play: an older server with no clip flag shows no Play and no placeholder', async () => {
  const items = [F.ovItem(1, 'brk:1', { ep_title: 'Episode 1' })];
  const { html } = await F.render('#/overview', F.overviewApi({ items }));
  const card = recentCardOf(html);
  assert.match(card, /Episode 1|ChatGPT/, 'fixture: the row shows');
  assert.equal(playOf(card).length, 0);
  assert.doesNotMatch(card, /ew-play-none/);
});
