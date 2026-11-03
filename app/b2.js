/* Redesign base (REDESIGN-BUILD phase A; ws-app/logs/redesign/PLAN.md).
   window.EWB2: the page registry and route switch, the layout mode, the
   page-first event hook, the redesigned header, and the shared components
   the five pages draw with (Landing's build specs, docs/landing/build-spec).

   - A page module registers itself with EWB2.page(name, { on, render,
     loading, handlers }). With on:true its render replaces today's page for
     its route; with on:false (or no module) today's page renders. The
     switch sits in the page's own file, so a page ships or is pulled alone.
   - render(...args, old) gets today's page function last, for fallbacks.
   - Components return HTML strings; every value is escaped here. */
(() => {
  'use strict';
  const E = () => window.EW;
  const U = () => window.EWUI;
  const esc = v => E().esc(v);

  /* -- Pages and the route switch ------------------------------------------ */
  // Page name -> the views.js page function it replaces.
  const ROUTE_FN = Object.freeze({ overview: 'overviewPage', search: 'searchPage', ad: 'evidencePage', brand: 'brandPage', signin: 'accessSection' });
  const PAGES = {};
  function page(name, spec) {
    if (!Object.hasOwn(ROUTE_FN, name)) throw new Error('unknown page ' + name);
    PAGES[name] = Object.freeze({ ...spec });
  }
  const isOn = name => { const p = PAGES[name]; return !!(p && p.on === true && typeof p.render === 'function'); };
  function pageForPath(path) {
    const p = String(path || '').split('?')[0];
    if (p === '#/overview' || p === '#/') return 'overview';
    if (p === '#/search') return 'search';
    if (p.startsWith('#/evidence/')) return 'ad';
    if (p.startsWith('#/brand/')) return 'brand';
    return null;
  }
  // The redesigned page on screen for a route, or null (today's page).
  function activeFor(route) {
    const name = pageForPath(route);
    return name && isOn(name) ? name : null;
  }
  // The views.js page table with every on page in its place (built once
  // per draw). The sign-in gate is the one accessSection call with
  // {page: true}; it marks this draw's guest header as the page's own.
  let signinShown = false;
  function overlay(V) {
    signinShown = false;
    const out = { ...V };
    for (const [name, fn] of Object.entries(ROUTE_FN)) {
      if (!isOn(name)) continue;
      const old = V[fn];
      out[fn] = name === 'signin'
        ? (...args) => {
          signinShown = !!(args[0] && args[0].page) && !E().current.principal;
          if (signinShown) k4Keep(); // K4: a run kept across the sign-out becomes the one-time note
          return PAGES[name].render(...args, old);
        }
        : (...args) => PAGES[name].render(...args, old);
    }
    return out;
  }
  // An on page's own loading state, painted before its read answers.
  function loading(path, route) {
    const name = activeFor(path);
    const p = name && PAGES[name];
    return p && typeof p.loading === 'function' ? p.loading(route, layout()) : null;
  }

  /* -- Layout: phone below 600 px (AD-DECISIONS item 10), else wide --------- */
  const PHONE_QUERY = '(max-width: 599px)';
  const phoneQuery = typeof window.matchMedia === 'function' ? window.matchMedia(PHONE_QUERY) : null;
  const layout = () => (phoneQuery && phoneQuery.matches ? 'phone' : 'wide');
  // Crossing 600 px draws a redesigned page again in the other markup
  // (both markups at once would repeat ids). The signed-out gate is drawn
  // again on any route, and keeps what was typed and the focused field
  // when the new draw is still the gate (RSI-R1-03).
  const GATE_FIELDS = ['signin-email', 'signin-password-input'];
  function typedInGate() {
    if (!signinShown || typeof document === 'undefined' || !document.querySelector) return null;
    const active = document.activeElement && document.activeElement.id;
    const kept = { focus: GATE_FIELDS.includes(active) ? active : null, values: {} };
    for (const id of GATE_FIELDS) { const f = document.querySelector('#' + id); if (f && f.value) kept.values[id] = f.value; }
    return kept;
  }
  function putBackInGate(kept) {
    if (!kept || !signinShown) return;
    for (const [id, value] of Object.entries(kept.values)) {
      const f = document.querySelector('#' + id);
      if (f && !f.value) f.value = value;
    }
    const f = kept.focus && document.querySelector('#' + kept.focus);
    if (f && f.focus) { try { f.focus(); } catch { /* keep focus */ } }
  }
  if (phoneQuery && typeof phoneQuery.addEventListener === 'function') {
    phoneQuery.addEventListener('change', () => {
      const ew = E();
      if (!ew || typeof ew.render !== 'function' || !(signinShown || activeFor(ew.current.route))) return;
      const kept = typedInGate();
      const drawn = ew.render();
      if (kept && drawn && typeof drawn.then === 'function') drawn.then(() => putBackInGate(kept), () => {});
    });
  }

  /* -- Events: the page on screen first (capture), today's handlers after -- */
  function handle(type, event) {
    const ew = E();
    const name = ew && activeFor(ew.current.route);
    const h = name && PAGES[name].handlers && PAGES[name].handlers[type];
    return typeof h === 'function' && h(event) === true;
  }
  if (typeof document !== 'undefined' && document.addEventListener) {
    for (const type of ['click', 'submit', 'change', 'input', 'keydown']) {
      document.addEventListener(type, event => {
        // K4: Close or Escape on a dialog forgets a kept "Start a campaign" run.
        if (k4Run && ((type === 'keydown' && event.key === 'Escape')
          || (type === 'click' && event.target && event.target.closest && event.target.closest('[data-action="close-dialog"]')))) k4Run = null;
        if (type === 'click' && menuClick(event)) { event.stopImmediatePropagation(); return; }
        if (type === 'keydown' && menuKey(event)) return;
        if (handle(type, event)) event.stopImmediatePropagation();
      }, true);
    }
  }

  /* -- Header: the s-nav look with today's items, order and rules ---------- */
  function navItems() {
    const c = E().current;
    const ask = c.askAvailable === true || c.askPaused === true;
    return ['Overview', 'Search', ...(ask ? ['Ask'] : []), 'Alerts', 'Brands', 'Shows', 'Campaigns', 'Saved'];
  }
  function navLink(n, section) {
    const label = n === 'Ask' ? 'Ask<span class="s-nav__new">New</span>' : esc(n);
    // Landing 407513adc cross #8: data-label reserves the bold width (app-b2.css X8); Ask's New tag keeps it out.
    const reserve = ` data-label="${n === 'Ask' ? '' : esc(n)}"`;
    return section === n
      ? `<a class="s-nav__current" href="#/${n.toLowerCase()}" aria-current="page"${reserve}>${label}</a>`
      : `<a class="s-nav__link" href="#/${n.toLowerCase()}"${reserve}>${label}</a>`;
  }
  function accountLinks(section) {
    const ew = E(), c = ew.current;
    const secondary = ew.connectionsOff() ? ['Settings'] : ['Settings', 'Connections'];
    const signout = c.hosted && c.principal ? '<button type="button" class="s-nav__account-item" data-action="signout">Sign out</button>' : '';
    return secondary.map(n => `<a class="s-nav__account-item" href="#/${n.toLowerCase()}"${section === n ? ' aria-current="page"' : ''}>${esc(n)}</a>`).join('') + signout;
  }
  function avatar() {
    const c = E().current;
    const who = c.principal ? E().signedInName() : '';
    const letter = (String(who).trim()[0] || '·').toUpperCase();
    return esc(letter);
  }
  // The header for a redesigned page (phone: wordmark, Menu, avatar; the
  // drawer holds the items). The avatar opens Settings, Connections and Sign out.
  function header(section) {
    const items = navItems().map(n => navLink(n, section)).join('');
    const brand = `<a class="${layout() === 'phone' ? 's-nav__brand-m' : 's-nav__brand'}" href="#/overview">EarWitness</a>`;
    const account = `<details class="s-nav__account"><summary class="s-nav__avatar" aria-label="Account">${avatar()}</summary>`
      + `<div class="s-nav__account-panel" role="group" aria-label="Account">${accountLinks(section)}</div></details>`;
    if (layout() === 'phone') {
      return brand
        + '<button type="button" class="s-nav__menu" data-b2-menu aria-expanded="false" aria-controls="b2-nav">Menu</button>'
        + account
        + `<nav class="s-nav__drawer" id="b2-nav" aria-label="Primary" hidden>${items}<span class="s-nav__drawer-rule" aria-hidden="true"></span>${accountLinks(section)}</nav>`
        + '<div class="s-nav__scrim" data-b2-menu-close aria-hidden="true" hidden></div>';
    }
    return brand + `<nav class="s-nav__links" aria-label="Primary">${items}</nav>` + account;
  }
  // body.ew-b2--<page> scopes the rules two page files define differently.
  const markPage = name => {
    if (typeof document === 'undefined' || !document.body || !document.body.classList) return;
    for (const n of Object.keys(ROUTE_FN)) document.body.classList.toggle('ew-b2--' + n, n === name);
  };
  const markBody = on => { if (typeof document !== 'undefined' && document.body && document.body.classList) document.body.classList.toggle('ew-b2', on); };
  // E.header asks first: true when it painted the redesigned header. Today's
  // pages get today's header element back (class topbar, shown).
  function paintHeader(section, el) {
    const on = !!activeFor(E().current.route);
    signinShown = false;
    markBody(on);
    markPage(on ? pageForPath(E().current.route) : null);
    if (!el) { k4Paint(el); return false; }
    el.hidden = false;
    if (!on) {
      if (el.classList && el.classList.contains('s-nav')) el.className = 'topbar';
      k4Paint(el); // K4: after the header has its class, so the notice takes its sides
      return false;
    }
    el.className = layout() === 'phone' ? 's-nav s-nav--phone' : 's-nav';
    el.innerHTML = header(section);
    k4Paint(el);
    return true;
  }
  // The guest header (views.js guestHeader): the redesigned sign-in page
  // draws its own header inside the page, so the shell's is hidden there.
  function paintGuestHeader(el) {
    // RSI-R1-04: the gate's mark was set before its session read; a read
    // that found someone (today's chooser) keeps today's header and body.
    if (signinShown && E().current.principal) signinShown = false;
    markBody(signinShown);
    markPage(signinShown ? 'signin' : null);
    if (!el) return;
    if (el.classList && el.classList.contains('s-nav')) el.className = 'topbar';
    el.hidden = signinShown;
  }
  // Phone menu: open moves focus to the first item (A11 row 37); Escape,
  // the scrim and Menu again close it and return focus to Menu.
  function setMenu(open) {
    const btn = document.querySelector('[data-b2-menu]');
    const nav = document.querySelector('#b2-nav');
    const scrim = document.querySelector('[data-b2-menu-close]');
    if (!btn || !nav) return;
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? 'Close menu' : 'Menu';
    nav.hidden = !open;
    if (scrim) scrim.hidden = !open;
    const target = open ? nav.querySelector('a') : btn;
    if (target && target.focus) { try { target.focus(); } catch { /* keep focus */ } }
  }
  function menuClick(event) {
    const t = event.target;
    if (!t || !t.closest) return false;
    if (t.closest('[data-b2-menu]')) { setMenu(t.closest('[data-b2-menu]').getAttribute('aria-expanded') !== 'true'); return true; }
    if (t.closest('[data-b2-menu-close]')) { setMenu(false); return true; }
    return false;
  }
  function menuKey(event) {
    if (event.key !== 'Escape') return false;
    const btn = document.querySelector && document.querySelector('[data-b2-menu][aria-expanded="true"]');
    if (!btn) return false;
    setMenu(false);
    return true;
  }

  /* -- Shared components ------------------------------------------------------ */
  // Brands that always show their name as text, never a logo (review, 2026-10-29).
  const PLAIN_NAME = new Set(['chatgpt', 'charlesschwab', 'schwab', 'growtherapy', 'youtubepremium']);
  const fold = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  function hasLogo(brand) {
    const b = brand && typeof brand === 'object' ? brand : { key: brand };
    if (b.under_review || PLAIN_NAME.has(fold(b.key))) return false;
    return !!U().brandLogo(b);
  }
  // The brand's logo (alt = its name; a failed load becomes the name, ui.js),
  // else its name as text; a held brand reads "Brand not identified".
  function brandMark(brand, { cls = '' } = {}) {
    const b = brand || {};
    if (b.under_review) return 'Brand not identified';
    if (!hasLogo(b)) return esc(b.display || '');
    const logo = U().brandLogo(b);
    return `<img class="${esc(['s-logo', cls].filter(Boolean).join(' '))}" src="${esc(logo.src)}" alt="${esc(logo.alt)}" loading="lazy" decoding="async" data-brand-logo>`;
  }
  // Same tint as EWUI.initialsTile (ew-tint-N), drawn as s-initials--tN.
  function tint(name) {
    let h = 0;
    for (const ch of String(name || '')) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return h % 6;
  }
  function initials(name) {
    return String(name || '').split(/\s+/).filter(w => /^[A-Za-z0-9]/.test(w) && !/^(the|a|an|of)$/i.test(w))
      .slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
  }
  const sameOrigin = url => typeof url === 'string' && url.startsWith('/') && !url.startsWith('//');
  // Show art at the slot's size (object-fit in CSS), else the initials tile.
  function cover(show, { size = '' } = {}) {
    const s = show || {};
    const title = s.title || s.show_title || s.display_title || '';
    if (sameOrigin(s.artwork_url)) {
      return `<img class="s-cover${size ? ' s-cover--' + esc(size) : ''}" src="${esc(s.artwork_url)}" alt="" loading="lazy" decoding="async">`;
    }
    return `<div class="s-initials${size ? ' s-initials--' + esc(size) : ''} s-initials--t${tint(title)}" aria-hidden="true">${esc(initials(title))}</div>`;
  }
  const clock = n => U().clock(n);
  // Seconds Play plays: from where it really starts (the ad start less the
  // 3 s lead-in, never before the clip) to the clip end (AD-DECISIONS item 2).
  // QUOTE-START: from: the episode second Play starts at instead (the quote).
  function playLength(media, span, from = null) {
    const m = media || {};
    const end = Number(m.clip_end_s), clipStart = Number(m.clip_start_s);
    if (m.clip_end_s == null || m.clip_start_s == null || !Number.isFinite(end) || !Number.isFinite(clipStart)) return null;
    const adStart = span && Number.isFinite(Number(span[0])) ? Number(span[0]) : clipStart;
    const start = from != null ? from : Math.max(clipStart, adStart - 3);
    return end > start ? Math.round(end - start) : null;
  }
  // QUOTE-START (UX D43): Search's Play starts at the quoted line, quoteAt
  // (the item's quote_at_s, episode seconds), in file seconds (less the
  // recording offset), when it is 5 s or more after the lead-in start and
  // 10 s or more before the clip end; else null (Play keeps the lead-in).
  function quoteStart(local, quoteAt, end) {
    const q = quoteAt == null || String(quoteAt).trim() === '' ? NaN : Number(quoteAt);
    const e = end == null || String(end).trim() === '' ? NaN : Number(end);
    if (!Number.isFinite(q) || !Number.isFinite(e) || local.at == null) return null;
    const at = q - local.offset;
    return at - local.at >= 5 && q <= e - 10 ? at : null;
  }
  const playLabel = seconds => (seconds == null ? 'Play' : `Play ${clock(seconds)}`);
  const noClip = () => '<span class="s-noclip">No audio clip</span>';
  const PLAY_CLASS = { '': 's-play', card: 's-play s-play--card', lg: 's-play s-play--lg', phone: 's-play-m' };
  // "Play {length}" on today's clip engine (clip-toggle, one audio at a time),
  // with its audio and the bounds the engine reads; no local clip: "No audio clip" (UX D50).
  // quoteAt: Search only (QUOTE-START); the Ad, Brand and Overview pages pass none.
  function play({ ref, media, span, id, size = '', quoteAt = null } = {}) {
    const local = E() && window.EWViews && window.EWViews._b2 ? window.EWViews._b2.localMedia(media, ref, span ? span[0] : null) : null;
    if (!local) return noClip();
    const m = media || {};
    const tri = size === 'phone' ? 's-play__tri s-play__tri--phone' : size ? `s-play__tri s-play__tri--${esc(size)}` : 's-play__tri';
    const disc = size === 'lg' ? 's-play__disc s-play__disc--lg' : 's-play__disc';
    // The engine's bounds (views.js clipBounds): Play starts at the lead-in
    // and runs to the clip end, so the label's length is what plays.
    const end = m.clip_end_s != null ? m.clip_end_s : span && span[1];
    // The lead-in stays data-lead: the seek bar, ±5 s and the replay at the end cover the whole ad.
    const quoted = quoteStart(local, quoteAt, end);
    const bounds = [['start', span && span[0]], ['end', end], ['offset', local.offset], ['lead', local.at]]
      .filter(([, v]) => v != null && String(v).trim() !== '' && Number.isFinite(Number(v))).map(([k, v]) => ` data-${k}="${esc(Number(v))}"`).join('');
    return `<span class="s-clip" data-clip-id="${esc(id)}"${bounds}>`
      + `<button type="button" class="${PLAY_CLASS[size] || 's-play'}" data-action="clip-toggle" data-clip="${esc(id)}" data-b2-play aria-pressed="false">`
      + `<span class="${disc}"><span class="${tri}"></span></span>${esc(playLabel(playLength(media, span, quoted == null ? null : quoted + local.offset)))}</button>`
      + window.EWViews._b2.clipAudio(id, local.src, quoted == null ? local.at : quoted) + '</span>';
  }
  // The clip engine's words for a b2 Play button: Pause with two bars while
  // playing; the button's own idle words otherwise (kept from its first change).
  const IDLE = new WeakMap();
  function playInner(btn, playing) {
    if (!IDLE.has(btn)) IDLE.set(btn, btn.innerHTML);
    const idle = IDLE.get(btn);
    if (!playing) return idle;
    return idle
      .replace(/<span class="s-play__tri([^"]*)"><\/span>/, (_, mods) => {
        const m = /--(card|lg)/.exec(mods);
        const pause = m && m[1] === 'lg' ? 's-play__pause s-play__pause--lg' : 's-play__pause';
        const bar = m ? `s-play__bar s-play__bar--${m[1]}` : 's-play__bar';
        return `<span class="${pause}"><span class="${bar}"></span><span class="${bar}"></span></span>`;
      })
      .replace(/<span class="a-play__tri([^"]*)"><\/span>/, (_, mods) => {
        const bar = /--phone/.test(mods) ? 'a-play__pause-bar a-play__pause-bar--phone' : 'a-play__pause-bar';
        return `<span class="a-play__pause"><span class="${bar}"></span><span class="${bar}"></span></span>`;
      })
      .replace(/Play(?: \d+(?::\d\d)+)?(?=(?:<\/span>)?$)/, 'Pause');
  }
  const at = seconds => `${clock(seconds)} into the episode`;
  const count = (n, one, many = one + 's') => `${Number(n).toLocaleString('en-US')} ${Number(n) === 1 ? one : many}`;
  // UX D6-P (final 2026-10-29): leads with what can be played; {n} counted live.
  function clipLine(n, total) {
    const lead = 'Every ad shows what the host said and when in the episode.';
    if (!n) return lead;
    if (total && n >= total) return `${lead} Every ad also has a clip you can play.`;
    return `${lead} ${n === 1 ? '1 has' : `${Number(n).toLocaleString('en-US')} have`} a clip you can play.`;
  }
  // Cut at a word end with "…" and no space before it; a cut that would land
  // after a sentence's period ends at the period with no ellipsis (R21, X20).
  function cutQuote(text, max) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    if (s.length <= max) return s;
    let cut = s.slice(0, max + 1);
    const sp = cut.lastIndexOf(' ');
    cut = (sp > 0 ? cut.slice(0, sp) : s.slice(0, max)).replace(/[\s,;:–—-]+$/, '');
    return /[.!?]$/.test(cut) ? cut : cut + '…';
  }
  // JE-COVERAGE: the server's Screened counts (DET-PLAN item 6) beside the totals. null without them
  // (an older server): no "checked" claim at all, never the total. "all" only when both pairs are equal.
  function checked(cov) {
    const c = cov || {};
    const n = x => typeof x === 'number' && Number.isFinite(x);
    if (![c.episodes, c.shows, c.screened_episodes, c.screened_shows].every(n)) return null;
    return { episodes: c.episodes, shows: c.shows, n: c.screened_episodes, nShows: c.screened_shows,
      all: c.screened_episodes === c.episodes && c.screened_shows === c.shows };
  }
  // The coverage words, one place (JE-COVERAGE; UX WORDS D44, D45): the chip's mark (none: D44 drops
  // the "✓", which reads as verified, D10), its words
  // after "Ads checked in", and the collection sentence (Overview, Search's empty state).
  const CHECK_MARK = '';
  const checkedIn = k => (k.all ? `all ${count(k.episodes, 'episode')} from ${count(k.shows, 'show')}`
    : `${Number(k.n).toLocaleString('en-US')} of ${count(k.episodes, 'episode')} (${Number(k.nShows).toLocaleString('en-US')} of ${count(k.shows, 'show')})`);
  const adsCheckedOf = k => `Ads checked in ${Number(k.n).toLocaleString('en-US')} of them.`;
  // linked: the escaped "{n} episodes from {n} shows" link; back: ", going back to …" or ''.
  const collectionSentence = (k, linked, back) => (k && k.all ? `We checked ${linked}${back}. `
    : `We hold ${linked}${back}. ${k ? esc(adsCheckedOf(k)) + ' ' : ''}`);
  // "Ads checked in {words}." as one /coverage/ link (build decision 4i / CV); inline: only the
  // counted words. Nothing without the Screened counts.
  function coverageLink(cov, { inline = false } = {}) {
    const k = checked(cov);
    if (!k) return '';
    const words = checkedIn(k);
    if (inline) return `<a class="s-coverage-inline" href="/coverage/">${esc(words)}</a>`;
    return `<a class="s-coverage" href="/coverage/"><span class="s-checked"><b class="s-strong">${CHECK_MARK}Ads checked</b> in ${esc(words)}.</span></a>`;
  }
  const longName = (text, cls) => `<span class="${esc(cls)}" title="${esc(text)}">${esc(text)}</span>`;
  const status = words => `<p class="s-status" role="status">${esc(words)}</p>`;

  /* -- Start a campaign with this ad (REDESIGN-AD-WORDS round 2 part B, lead 10-30; UX words in
        briefs/CAMPAIGN-FROM-AD.md and App's create-step words). In the Add dialog: one name field,
        then "Create and add" makes a research campaign, adds this episode as its first research
        target and keeps the ad on it, through today's customer routes (the server's plan and
        rights checks stand). A failed step says what was saved; "Try again" resumes there and
        never makes a second campaign. Shared by the Ad and Search pages: each passes its look. */
  const CFA_WORDS = Object.freeze({
    start: 'Start a campaign with this ad',
    theirsLine: 'Only the member who made a campaign can add to it. Start your own campaign with this ad in it.',
  });
  // Who sees the button: a hosted member who may make campaigns (owner or editor; a session with no role, an older
  // server, as Export). Never a viewer. Plan A may: the server makes the campaign and its episode target at excerpt level.
  const cfaMay = c => !!(c && c.hosted) && (!c.workspaceRole || c.workspaceRole === 'owner' || c.workspaceRole === 'editor');
  // Never two campaigns with one name in the workspace (any member's): "{name} (2)", "(3)", ….
  function cfaUnique(base, taken) {
    const have = new Set((taken || []).map(n => String(n).trim().toLowerCase()));
    const b = String(base || '').trim();
    if (!b || !have.has(b.toLowerCase())) return b;
    for (let i = 2; ; i++) if (!have.has(`${b} (${i})`.toLowerCase())) return `${b} (${i})`;
  }
  const cfaDefault = ad => (ad && ad.brand ? `${ad.brand} ads` : '');
  const cfaState = (ad, taken) => ({ name: cfaUnique(cfaDefault(ad), taken), cid: null, rev: null, tid: null, failed: null, done: false, busy: false, stale: false });
  // The part of a failed step's line that says what was saved (no "Try again").
  const cfaSaved = st => (st.failed === 'target' ? `We started “${st.name}”, but couldn’t add this episode to it.`
    : st.failed === 'attach' ? `We started “${st.name}” and added this episode, but couldn’t add the ad.` : '');
  function cfaFail(st) {
    // AD-FIX2: a held run (no answer, or the dialog went stale once something was saved) has its own line.
    if (st.held) return st.held;
    if (st.failed === 'create') return 'We couldn’t start the campaign. Nothing was saved. Try again.';
    if (st.failed === 'target') return `We started “${st.name}”, but couldn’t add this episode to it. Try again, or open the campaign to add it.`;
    if (st.failed === 'attach') return `We started “${st.name}” and added this episode, but couldn’t add the ad. Try again.`;
    return '';
  }
  // The create step, its busy and failure states, and the success, as one form in the dialog.
  function cfaHtml(ad, st, k) {
    // Success (UX 10-30): Close is the primary button, "Open “{name}”" a plain link; the line takes focus (tabindex -1).
    if (st.done) {
      return `<form id="campaign-from-ad" novalidate class="${esc(k.form)}" data-cfa><h2 class="${esc(k.title)}" id="dialog-title" role="status" tabindex="-1">${esc(`Started “${st.name}” and added this ad.`)}</h2>`
        + `<div class="${esc(k.foot)}"><a class="${esc(k.link)}" href="#/campaign/${esc(encodeURIComponent(String(st.cid)))}">${esc(`Open “${st.name}”`)}</a>`
        + `<button class="${esc(k.cta)}" type="button" data-action="close-dialog">Close</button></div></form>`;
    }
    const first = [ad.showName, ad.episodeName, ad.at].filter(Boolean).join(' · ');
    const fail = st.error || cfaFail(st);
    // AD-FIX2 (RA-R1-01/02): held, the run may have saved more than it knows: no Try again (no submit at all),
    // the name is fixed, Close, and today's Reload workspace shows what is stored.
    const held = !!st.held;
    return `<form id="campaign-from-ad" novalidate class="${esc(k.form)}" data-cfa><h2 class="${esc(k.title)}" id="dialog-title">${esc(CFA_WORDS.start)}</h2>`
      + (first ? `<span class="${esc(k.meta)}">${esc(`This ad goes in first: ${first}`)}</span>` : '')
      + `<label class="${esc(k.field)}"><span class="${esc(k.label)}">Campaign name</span><input class="${esc(k.input)}" name="name" maxlength="160" autocomplete="off" value="${esc(st.name)}"${st.cid != null || held ? ' readonly' : ''}></label>`
      + (ad.episodeName ? `<span class="${esc(k.meta)}">${esc(`First research target: ${ad.episodeName}`)}</span>` : '')
      // Once the campaign is started something is saved: Cancel reads Close (UX 10-30).
      + `<div class="${esc(k.foot)}"><button class="${esc(k.cancel)}" type="button" data-action="close-dialog">${st.cid != null || held ? 'Close' : 'Cancel'}</button>`
      + (held ? `<button class="${esc(k.cta)}" type="button" data-action="reload-workspace">Reload workspace</button></div>`
        : `<button class="${esc(k.cta)}" type="submit"${st.busy ? ' disabled' : ''}>${st.busy ? 'Creating…' : st.cid != null ? 'Try again' : 'Create and add'}</button></div>`)
      + (fail && !st.busy ? `<p class="${esc(k.error)}" role="alert">${esc(fail)}${st.cid != null ? ` <a href="#/campaign/${esc(encodeURIComponent(String(st.cid)))}">${esc(`Open “${st.name}”`)}</a>` : ''}</p>` : '')
      + '</form>';
  }
  // The three steps, each once: a retry starts at the step that failed, with the campaign's revision read again.
  // AD-FIX2 (Ad review r1): RA-R1-01 a write with no answer (err.outcomeUnknown: a timeout or transport failure)
  // may have committed: the run holds with today's words (views.js UNCONFIRMED) and never sends again (a retry
  // would make "Quince ads (2)" or a second target). RA-R1-02 `guard` (the caller's) answers before each write:
  // null while the dialog is current, else the words to close with; nothing is sent then (st.refused). A
  // workspace 409 or a sign-out 401 at a step refuses the same way with the app's own words. Once something is
  // saved, a refusal holds instead, saying what was saved.
  async function cfaRun(api, ad, st, { guard = null } = {}) {
    if (st.held) return st;
    let step = 'create';
    st.failed = null; st.refused = null;
    const refuse = words => {
      st.failed = step; st.stale = st.cid != null;
      if (st.cid == null) st.refused = words;
      else st.held = `${cfaSaved(st)} Reload the workspace to continue.`;
      return st;
    };
    const stale = () => (guard ? guard() : null);
    try {
      const path = () => '/customer/campaigns/' + encodeURIComponent(String(st.cid));
      if (st.cid == null) {
        const list = await api('/customer/campaigns');
        st.name = cfaUnique(st.name, ((list && list.campaigns) || []).map(c => c && c.name).filter(Boolean));
        if (stale()) return refuse(stale());
        const out = await api('/customer/campaigns', { method: 'POST', body: { campaign: { name: st.name, brand: ad.brand || null, message: '', offer: '', notes: '', mode: 'research' } } });
        st.cid = out.campaign.id; st.name = out.campaign.name || st.name; st.rev = out.campaign.revision; st.stale = false;
      }
      step = 'target';
      if (st.stale) { st.rev = (await api(path())).campaign.revision; st.stale = false; }
      if (st.tid == null) {
        if (stale()) return refuse(stale());
        const t = await api(path() + '/targets', { method: 'POST', body: { target: { scope: 'episode', show_id: null, episode_id: ad.episode, date_from: null, date_to: null, timezone: 'UTC', note: '' }, expected_campaign_revision: st.rev } });
        st.tid = t.id; st.rev = t.campaign_revision;
      }
      step = 'attach';
      if (stale()) return refuse(stale());
      await api(path() + '/targets/' + encodeURIComponent(String(st.tid)) + '/evidence', { method: 'POST', body: { ref: ad.ref, expected_revision: st.rev } });
      st.done = true;
    } catch (err) {
      if (err && err.droppedContext) throw err;
      if (err && (err.workspaceConflict || err.signedOut)) return refuse(err.message);
      st.failed = step;
      st.stale = st.cid != null;
      if (err && err.outcomeUnknown) st.held = window.EWViews._b2.UNCONFIRMED;
    }
    return st;
  }
  /* AD-FIX3 (base(ad); Ad review r2, briefs/DIALOG-STALE-WORDS.md, Search review r2). Kept apart from the AD-FIX2
     lines above (a wrapper at the export), so every branch that already holds those lines merges clean.
     - DIALOG-STALE (Reno via the lead, PACE-4): the guard's own words are (a) "the workspace changed"; when the session
       the dialog opened in changed (signed out, or signed in as someone else, or a new sign-in as the same person: the
       principal or the session's CSRF token moved) the words are (b), and (b) wins when both moved. Nothing is sent.
       Once something is saved, the held line ends "Sign in again to continue." instead.
     - RSEARCH-R2-03: signed in again in another tab, this tab's epoch never moves; the server refuses the write's CSRF
       token (403). That is case (b): refused (or held, once something is saved), never "Try again".
     - RA-R2-03: a held line takes focus (tabindex -1), as the success line. */
  const CFA_SESSION_STALE = 'Your sign-in changed after this was opened. Nothing was changed. Sign in again to continue.';
  const CFA_RELOAD_AS_NOW = 'Reload to continue as the account that is signed in now.';
  const cfaSession = () => { const c = (E() && E().current) || {}; return `${c.principal || ''}\n${c.csrf || ''}`; };
  // K6 (RA-R3-02): the server marks a refused CSRF token error.reason "csrf_invalid" (asgi.py _csrf_refused); the
  // client keys on that code. A 403 with any other reason is never this case.
  const cfaCsrfRefused = err => {
    if (!err || err.status !== 403) return false;
    const e = err.body && err.body.error;
    const reason = e && typeof e === 'object' ? e.reason : undefined;
    if (reason !== undefined) return reason === 'csrf_invalid';
    // Fallback for an older server that sends no reason: match its English message, as before K6.
    return /csrf/i.test(String((e && (e.message || e)) || err.message || ''));
  };
  const cfaOpen = (ad, taken) => ({ ...cfaState(ad, taken), session: cfaSession() });
  const cfaLine = st => (st.refused && !st.held ? st.refused : cfaFail(st));
  function cfaPaint(ad, st, k) {
    const h = cfaHtml(ad, st, k);
    const out = st.held ? h.replace(/(<p class="[^"]*" role="alert")>/, '$1 tabindex="-1">') : h;
    return st.reloadPage ? out.replace('data-action="reload-workspace">Reload workspace</button>', 'data-action="reload-workspace">Reload page</button>') : out;
  }
  async function cfaRunSession(api, ad, st, { guard = null } = {}) {
    let session = false;
    const moved = () => st.session !== undefined && st.session !== cfaSession();
    const check = guard && (() => { const words = guard(); if (!moved()) return words; session = true; return CFA_SESSION_STALE; });
    const send = async (p, o) => {
      const step = k4Step(p, o);
      if (step === 'attach') st.k4attach = 'sent';
      try {
        const out = await api(p, o);
        if (step === 'create') k4Live(st);
        if (step === 'attach') st.k4attach = 'ok';
        return out;
      } catch (err) {
        if (step === 'attach') st.k4attach = k4Outcome(err);
        if (!cfaCsrfRefused(err)) throw err;
        session = true;
        throw Object.assign(new Error(CFA_SESSION_STALE), { status: err.status, body: err.body, signedOut: true });
      }
    };
    await cfaRun(send, ad, st, { guard: check });
    // UX D39 (WORDS.md, 27c35630b): still signed in (another account, a new session, or the CSRF 403): the button
    // reloads under that account, so the words say so; it never signs anyone in. Signed out: today's line.
    // K5 (RD39-01, D39 2026-10-31): the button reads "Reload page" signed out too; the action stays.
    if (session && st.held && st.cid != null) {
      const signedIn = !!((E() && E().current) || {}).principal;
      st.reloadPage = true;
      st.held = `${cfaSaved(st)} ${signedIn ? CFA_RELOAD_AS_NOW : 'Sign in again to continue.'}`;
    }
    return st;
  }
  /* K4 (RA-R3-03; UX D40, WORDS.md 066f6edc1). A campaign started from an ad, then signed out for real (in another
     tab): the next write gets a 401 and the tab goes to the sign-in page, so the dialog never says what was started.
     - While a run has made its campaign, it is kept here (memory only) with the page it opened on. Closing the
       dialog by hand (Close, Escape) or moving to another page forgets it.
     - When the sign-in page draws signed out (overlay), the kept run becomes a one-time note in this browser
       (localStorage): the campaign id, its name, the add state, a hash of the account and when it was written
       (at, ms). Never a token. RKD-03: a note older than 24 hours, or one that looks written in the future (the
       clock moved), is dropped unseen.
     - The add state: "added" only when the attach answered; "not" when the attach never ran or was refused;
       "unknown" when it was sent and its answer is not known (no answer, still on its way): never promoted.
     - The first signed-in page draw (paintHeader) takes the note out of storage. The account that started the
       campaign sees it once, as a notice after the header, until the next page; anyone else's sign-in drops it
       unseen. "Campaigns" opens the campaign, or #/campaigns when that id is gone. Never a toast. */
  const K4_KEY = 'earWitnessCampaignNote';
  const K4_TTL = 24 * 60 * 60 * 1000; // RKD-03: a note lives one day at most
  const K4_LINES = Object.freeze({
    added: n => [`Before you were signed out, we started your campaign “${n}” with this ad. It’s in `, '.'],
    not: n => [`Before you were signed out, we started your campaign “${n}”, but couldn’t add this ad to it. It’s in `, '; add the ad again from its page.'],
    unknown: n => [`Before you were signed out, we started your campaign “${n}”. Open it in `, ' to check whether this ad is in it.'],
  });
  let k4Run = null;   // { st, who, route }: the run whose campaign exists, while its dialog is open
  let k4Shown = null; // the route the notice shows on
  // The account as a short one-way hash (FNV-1a), enough to tell one account from another on this browser.
  const k4Who = principal => {
    let h = 0x811c9dc5;
    for (const ch of String(principal || '')) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, '0');
  };
  const k4Step = (p, o) => ((o && o.method) !== 'POST' ? null : p === '/customer/campaigns' ? 'create' : /\/evidence$/.test(p) ? 'attach' : null);
  // An attach error: refused only on a definite answer; a late write keeps what it says; anything else is unknown.
  const k4Outcome = err => (err && err.lateWrite === 'saved' ? 'ok' : err && err.lateWrite === 'rejected' ? 'refused'
    : err && err.lateWrite ? 'sent' : err && !err.outcomeUnknown && Number(err.status) >= 400 ? 'refused' : 'sent');
  const k4Add = st => (st.done || st.k4attach === 'ok' ? 'added' : !st.k4attach || st.k4attach === 'refused' ? 'not' : 'unknown');
  const k4Store = () => { try { return window.localStorage || null; } catch { return null; } };
  function k4Live(st) {
    const principal = String(st.session || '').split('\n')[0];
    k4Run = principal ? { st, who: k4Who(principal), route: ((E() && E().current) || {}).route || '' } : null;
  }
  // The sign-in page drew, signed out: the kept run becomes the note.
  function k4Keep() {
    const run = k4Run;
    k4Run = null;
    if (!run || run.st.cid == null) return;
    const store = k4Store();
    try { if (store) store.setItem(K4_KEY, JSON.stringify({ v: 1, who: run.who, cid: run.st.cid, name: String(run.st.name || ''), add: k4Add(run.st), at: Date.now() })); } catch { /* no note */ }
  }
  // Signed in: the note, once, for its own account only. Anything else is dropped.
  function k4Take(principal) {
    const store = k4Store();
    let raw = null;
    try { raw = store && store.getItem(K4_KEY); if (raw !== null && store) store.removeItem(K4_KEY); } catch { return null; }
    if (raw === null) return null;
    let n = null;
    try { n = JSON.parse(raw); } catch { return null; }
    if (!n || n.v !== 1 || typeof n.who !== 'string' || n.cid == null || typeof n.name !== 'string' || !Object.hasOwn(K4_LINES, n.add)) return null;
    // RKD-03: too old, written "in the future" (a clock that moved), or no usable time: dropped unseen.
    const age = typeof n.at === 'number' ? Date.now() - n.at : NaN;
    if (!(age >= 0 && age <= K4_TTL)) return null;
    return n.who === k4Who(principal) ? n : null;
  }
  function k4Paint(el) {
    const c = (E() && E().current) || {};
    const route = c.route || '';
    if (k4Shown !== null && k4Shown !== route) {
      const old = document.querySelector && document.querySelector('#b2-note');
      if (old && old.remove) old.remove();
      k4Shown = null;
    }
    if (k4Run && k4Run.route !== route) k4Run = null;
    if (!c.principal || !el || typeof el.insertAdjacentHTML !== 'function') return;
    const n = k4Take(c.principal);
    if (!n) return;
    const [before, after] = K4_LINES[n.add](n.name);
    el.insertAdjacentHTML('afterend', `<div id="b2-note"><div class="notice" role="status"><p>${esc(before)}`
      + `<a href="#/campaign/${esc(encodeURIComponent(String(n.cid)))}">Campaigns</a>${esc(after)}</p></div></div>`);
    // The notice lines up with the header's own sides (100 px wide, 24 px below 1200, 16/12 on the phone). Set
    // through the style property: the app's CSP refuses a style attribute in markup.
    try {
      const box = document.querySelector('#b2-note');
      const cs = typeof getComputedStyle === 'function' ? getComputedStyle(el) : null;
      if (box && box.style && cs) box.style.padding = `12px ${cs.paddingRight} 0 ${cs.paddingLeft}`;
    } catch { /* default sides */ }
    k4Shown = route;
    // The campaign's id gone (deleted, or another workspace): the link opens Campaigns instead.
    Promise.resolve().then(() => E().api('/customer/campaigns/' + encodeURIComponent(String(n.cid))))
      .catch(err => {
        if (!err || err.status !== 404) return;
        const a = document.querySelector && document.querySelector('#b2-note a');
        if (a && a.setAttribute) a.setAttribute('href', '#/campaigns');
      });
  }
  const cfa = Object.freeze({ WORDS: Object.freeze({ ...CFA_WORDS, sessionStale: CFA_SESSION_STALE }), may: cfaMay, uniqueName: cfaUnique, defaultName: cfaDefault,
    state: cfaOpen, failLine: cfaLine, html: cfaPaint, run: cfaRunSession });

  /* -- The Add dialog when the ad can't be added (UX m4, 10-30 ~21:4xZ, final; briefs/REDESIGN-SEARCH-6.md
        "m4 words"). The member's own open research campaigns, none with a research target covering the
        episode: the dialog's only heading is "This ad can’t be added yet" (never "Add to my campaign" above
        it), two lines, the exit as the primary button and, for a caller who may make campaigns, "Start a
        campaign with this ad" (EWB2.cfa) as the second; no campaign rows. Rows elsewhere drop
        "· Episode: {title}". Shared by the Ad and Search pages: each passes its look. */
  const WHY_WORDS = Object.freeze({
    title: 'This ad can’t be added yet',
    // UX D35 (WORDS.md, 10-30): each case its own heading. No campaign: "Add to my campaign"; only other members'
    // campaigns (item 6): "You can’t add to these campaigns"; the research case: the title above.
    noneTitle: 'Add to my campaign',
    theirsTitle: 'You can’t add to these campaigns',
    theirsNote: 'Made by another member · only they can add to it',
  });
  // list: [{ id, name, viewer }], the member's own (a viewer: every open one, viewer true).
  function whyLines(list) {
    const one = list.length === 1 ? list[0] : null;
    const viewer = list.some(c => c && c.viewer);
    return Object.freeze([
      one ? `None of the research targets in “${one.name}” covers this episode.` : 'None of the research targets in your campaigns covers this episode.',
      viewer ? (one ? 'Ask the campaign’s owner to add a research target that covers this episode.'
        : 'Ask the owner of one of these campaigns to add a research target that covers this episode.') // UX D35
        : one ? 'To add it, give the campaign a research target that covers this episode.' : 'To add it, give one of them a research target that covers this episode.',
    ]);
  }
  const whyExit = list => Object.freeze(list.length === 1
    ? { href: '#/campaign/' + encodeURIComponent(String(list[0].id)), text: `Open “${list[0].name}”` }
    : { href: '#/campaigns', text: 'Go to Campaigns' });
  // The dialog's inside in the caller's look k = { title, none, meta, foot, cancel, second, cta }; `adding` is the
  // caller's own "Adding: …" markup. The second button never shows to a viewer.
  function whyHtml(list, k, { adding = '', cfa: may = false } = {}) {
    const exit = whyExit(list);
    const second = may && !list.some(c => c && c.viewer);
    return `<h2 class="${esc(k.title)}" id="dialog-title">${esc(WHY_WORDS.title)}</h2>${adding}`
      + `<div class="${esc(k.none)}" data-why>${whyLines(list).map(t => `<span class="${esc(k.meta)}">${esc(t)}</span>`).join('')}</div>`
      + `<div class="${esc(k.foot)}"><button class="${esc(k.cancel)}" type="button" data-action="close-dialog">Cancel</button>`
      + (second ? `<button class="${esc(k.second)}" type="button" data-b2-cfa>${esc(CFA_WORDS.start)}</button>` : '')
      + `<a class="${esc(k.cta)}" href="${esc(exit.href)}">${esc(exit.text)}</a></div>`;
  }
  // A listed row's words (today's "<b>{name}</b> · {target} · research observation[ · status]", already escaped):
  // the name, the target unless it is the episode, the status, and for another member's row the note.
  function whyRow(inner, { theirs = false } = {}) {
    const s = String(inner);
    const m = /^<b>[\s\S]*?<\/b>/.exec(s);
    if (!m) return s;
    const rest = s.slice(m[0].length)
      .replace(/ · Only the person who made this can change it\.$/, '')
      .replace(/ · research observation(?= · |$)/, '')
      .replace(/ · Episode: [\s\S]*?(?= · already attached$| · can’t take this evidence: |$)/, '');
    return m[0] + rest + (theirs ? ` · ${esc(WHY_WORDS.theirsNote)}` : '');
  }
  // Which of the member's open research campaigns the research case names (SEARCH-7 item 2, RSEARCH-R1-02 /
  // RA-R1-04). A dialog that lists a row of the member's own (a radio with a value: open, already attached, or
  // refused) is never the research case: []. Else every campaign the dialog does not list by name (FR-11's
  // greyed rows of another member's campaign cover the episode, so a listed name is never called missing).
  function whyList(html, list) {
    const s = String(html);
    if (/<input type="radio" name="choice" value=/.test(s)) return Object.freeze([]);
    return Object.freeze((list || []).filter(c => c && !s.includes(`<b>${esc(c.name)}</b>`)));
  }
  const addWhy = Object.freeze({ WORDS: WHY_WORDS, lines: whyLines, exit: whyExit, html: whyHtml, row: whyRow, list: whyList });

  window.EWB2 = Object.freeze({
    cfa,
    addWhy,
    page, isOn, overlay, loading, activeFor, pageForPath, layout, handle, header, paintHeader, paintGuestHeader,
    brandMark, hasLogo, cover, playLength, playLabel, play, playInner, noClip, at, count, clipLine, cutQuote, coverageLink, checked, checkedIn, collectionSentence, CHECK_MARK, longName, status,
    PHONE_QUERY,
  });
})();
