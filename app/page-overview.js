/* Redesigned Overview (#/overview, #/). REDESIGN-BUILD; build source
   docs/landing/build-spec/overview/SPEC.md (aaecbd611, the polish commit on
   c9ea045ca; the hero words and Plan A hero from 399bed463); plan
   ws-app/logs/redesign/PLAN.md section 4.
   Owner: the Overview builder (this file, app/overview-b2.css, tests/frontend_b2_overview.test.cjs).
   Route switch: ON. false keeps today's page; true draws this one.
   render(token, old): today's reads (overview, tracked brands, the newest
   ads), plus the configured featured record by ref, the brand and show
   directories (Q6 picks). Server fields with their fallbacks: S1 heard_at_s
   (else the ad window start), S2 artwork_url (else initials), S4 read.media
   (else Play on today's Search player, no length), S8
   coverage.earliest_episode (else no "going back to"). */
(() => {
  'use strict';
  const ON = true;
  const E = () => window.EW;
  const U = () => window.EWUI;
  const B = () => window.EWB2;
  const esc = v => E().esc(v);
  const phone = () => B().layout() === 'phone';
  // The one reviewed record whose quote the featured card shows (review decision 1):
  // Two Judgey Girls, Quince, 21:57 (the spec's "sr1:4b1a7687", in full). A SourceRef1 is
  // minted per database, so the card also takes the record by its evidence id; on other
  // data neither matches and the card falls back with no quote (SPEC Data 3).
  const FEATURED = Object.freeze({ ref: 'sr1:4b1a7687b1cc4e449c18fb5245939b07', evidence: 'named:c9bef68dcc5f5738525d052bf9d01cd54891bf873e20f1ae048a736fefec3a5d' });
  const isFeatured = it => !!it && (it.ref === FEATURED.ref || it.evidence_id === FEATURED.evidence);
  // The Try chips: the one list (Q6; VISDATA-AVOID item 2). A change is one line here plus its pin
  // (tests/frontend_b2_overview.test.cjs reads this list). Each entry: key, label and, for a logo chip,
  // its logo class and the logo's pixel size (app/public/img/logo-<key>.png), so the chip keeps its
  // width while the logo loads. No logo: a text chip. phrase: a quoted phrase chip.
  // mint mobile became ziprecruiter (Reno, 10-31 ~16:12Z) and shopify became raycon (Reno, 11-01 ~05:10Z):
  // text chips; their files are square icons.
  const TRY = Object.freeze([
    { key: 'quince', label: 'Quince', logo: { cls: 's-logo--13', size: [192, 56] } },
    { key: 'betterhelp', label: 'BetterHelp', logo: { cls: 's-logo--14', size: [408, 56] } },
    { key: 'ziprecruiter', label: 'ZipRecruiter' },
    { key: 'raycon', label: 'Raycon' },
    { key: 'vpn', label: 'VPN', phrase: true },
  ]);
  const QUOTE_MAX = 200;
  const D3 = 'Your campaigns hold the ad buys you’re checking: did each booked read run, with the right code?';
  // The hero words (H2), and Plan A's (Reno via Review; Landing 849c650c4 "Plan A hero"): words in ads only.
  const HERO = Object.freeze({
    full: { h1: 'Hear what hosts say about any brand, word or competitor.', search: 'search any phrase' },
    planA: { h1: 'Hear what hosts say in ads about any brand, word or competitor.', search: 'search the words in them' },
  });
  const NB = ' ';
  const fold = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const isNum = v => v != null && v !== '' && Number.isFinite(Number(v));

  /* -- Reads (views.js guardedFetch and apiQS, on the shell's own state) ----- */
  function guarded(token) {
    const controller = new AbortController();
    E().current.aborters.set(token, controller);
    return { signal: controller.signal, live: () => E().current.fetchToken === token && E().current.aborters.get(token) === controller };
  }
  const search = (query, opts) => E().api('/customer/search?' + new URLSearchParams({ query: JSON.stringify(query) }).toString(), opts);
  const ADS = { version: 2, mode: 'ads', q: '', brands: null, shows: null, categories: null, statuses: null, audio: null, date_status: 'all', from: null, to: null, sort: 'newest', page: 1, ids: null };
  const failed = err => ({ failed: err || new Error('request failed') });
  // OVERVIEW-2 (screen r2 N2, n2): the brand and show directories are the page's last and slowest reads
  // (the review host answered /customer/shows in 22-25 s); the client stops a read at 20 s
  // (requestTimeout.readMs). A directory read stopped that way is read once more; a second failure shows
  // the part's failure words, never a missing part.
  const dirRead = (path, opts) => E().sharedRead(path, opts).catch(err => {
    if (!err || !err.timedOut || (opts.signal && opts.signal.aborted)) throw err;
    return E().sharedRead(path, opts);
  });

  /* -- Words ------------------------------------------------------------------ */
  const realDay = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && (() => { const t = new Date(v + 'T12:00:00Z'); return Number.isFinite(t.getTime()) && t.toISOString().slice(0, 10) === v; })();
  // "{Mon} {D}", with its year when not this year (NOGAP SR row 42); month and day never split (K5).
  function day(v) {
    const d = E().date(v);
    const year = /^\d{4}-\d{2}-\d{2}/.test(v || '') ? Number(String(v).slice(0, 4)) : null;
    const out = year && /^[A-Z][a-z]{2} \d/.test(d) && year !== new Date().getUTCFullYear() ? `${d}, ${year}` : d;
    return out.replace(/ /g, NB);
  }
  // Collection words (Reno via Review; Landing 849c650c4 + 399bed463): "going back to {year}", the earliest
  // episode's year from S8; a start in the current year reads "going back to {Mon D}". No S8: no start named.
  // No date range and never a length of time.
  function goingBack(cov, today = new Date()) {
    const first = cov && realDay(cov.earliest_episode) ? cov.earliest_episode : null;
    if (!first) return '';
    const year = Number(first.slice(0, 4));
    return `, going back to ${year === today.getUTCFullYear() ? E().date(first).replace(/ /g, NB) : year}`;
  }
  const at = s => U().clock(s);
  const route = ref => '#/evidence/' + encodeURIComponent(ref);
  const searchRoute = q => E().searchURL({ mode: 'ads', q, page: 1 });
  const failedBlock = (what, not) => `<div class="o-failed" role="alert"><p><b>! ${esc(what)} didn’t load.</b> This is not “${esc(not)}”.</p><button type="button" class="s-btn" data-action="retry">Retry</button></div>`;
  const tintOf = name => (/s-initials--t(\d)/.exec(B().cover({ title: name })) || [0, 0])[1];

  /* -- One ad's facts ----------------------------------------------------------- */
  function facts(item) {
    const it = item || {};
    const { read, span } = U().resultFacts(it);
    const brand = (read && read.brand) || {};
    const heard = isNum(it.heard_at_s) ? Number(it.heard_at_s) : span ? span[0] : null;
    return { it, read, span, brand, heard, ref: it.ref || '', show: it.show_title || (it.show || {}).display_title || 'Unknown show',
      ep: it.ep_title || it.episode_id || 'Episode', date: day(it.pub_date) };
  }
  // PLAIN-ADS-FIX3: only ads whose brand shows; one row per ad; never a known_bleed ad (RECENT-ADS).
  function namedAds(items) {
    const seen = new Set();
    return (items || []).filter(it => {
      const read = it.read && !it.read.historical ? it.read : null;
      if (!read || it.known_bleed || it.record_kind === 'transcript_passage' || U().underReview(read) || !(read.brand || {}).display) return false;
      if (!it.ad_break_id) return true;
      if (seen.has(it.ad_break_id)) return false;
      seen.add(it.ad_break_id);
      return true;
    });
  }
  // Play {length} inline on today's clip engine (review decision 7). A clip with
  // no media block (server without S4) plays on today's Search player, no length.
  function playHtml(f, id, cls) {
    const read = f.read || {};
    if (read.has_clip === true && read.media === undefined && f.span) {
      return `<button type="button" class="${cls}" data-action="search-play" data-ref="${esc(f.ref)}" data-from="${esc(f.span[0])}" data-to="${esc(f.span[1])}" aria-label="Play ${esc(f.brand.display || 'ad')} ad, ${esc(f.show)}">`
        + `<span class="${cls === 'o-play-l o-playlen' ? 'o-play-l__disc' : 's-play__disc'}"><span class="${cls === 'o-play-l o-playlen' ? 'o-play-l__tri' : 's-play__tri'}"></span></span><span>Play</span></button>`;
    }
    return B().play({ ref: f.ref, media: read.media, span: f.span, id, size: cls === 's-play-m' ? 'phone' : '' });
  }
  // The featured card's own Play look (o-play-l / o-play-m) on the same engine;
  // s-play__tri stays on the triangle so the engine's Pause draws its two bars.
  function featPlay(f) {
    const ph = phone();
    const cls = ph ? 'o-play-m o-playlen' : 'o-play-l o-playlen';
    const read = f.read || {};
    if (read.has_clip === true && read.media === undefined && f.span) return playHtml(f, 'ov-feat', cls);
    const html = B().play({ ref: f.ref, media: read.media, span: f.span, id: 'ov-feat', size: '' });
    if (!/data-b2-play/.test(html)) return html;
    return html
      .replace('class="s-play" data-action', `class="${cls}" data-action`)
      .replace('<span class="s-play__disc"><span class="s-play__tri"></span></span>',
        ph ? '<span class="s-play__disc s-play__disc--lg"><span class="s-play__tri s-play__tri--lg"></span></span>'
          : '<span class="o-play-l__disc"><span class="s-play__tri o-play-l__tri"></span></span>')
      .replace(/(<\/span><\/span>)(Play(?: [\d:]+)?)(<\/button>)/, '$1<span>$2</span>$3');
  }
  const mark = (b, cls) => (B().hasLogo(b) ? B().brandMark(b, { cls }) : '');
  /* PODS (PODS-STRINGS P1, PODS-REDESIGN-PORT): a model-named read shows each of its names through `one`, each
     with "Named by Muse Spark" after it, the two one unit (EWUI.brandsHtml); any other read draws as before. A
     brand-level row (Tracked brands) takes the label only when every read of the brand is model-named. */
  const modelRead = f => U().shownBrands(f.read || {}).some(U().modelNamed);
  const onlyUnit = (b, html) => (U().modelOnlyMark(b) ? `<span class="ew-brand-unit">${html}${U().modelOnlyMark(b)}</span>` : html);
  // The quote with the brand word marked once (the configured record only).
  function quoteHtml(f) {
    const qt = U().quoteText(f.it);
    if (!qt || !qt.text) return '';
    // The reviewed excerpt's length (the reference's two sentences, 158 characters): the base cut, at a
    // sentence end with no "…" or a word end with one (R21, X20), so the Ask card stays on the first screen (U3).
    const cut = B().cutQuote(qt.text, QUOTE_MAX);
    // Whole sentences when one ends inside the limit, after its first half (no "…" then).
    const ends = [...cut.matchAll(/[.!?](?=\s)/g)].map(m => m.index + 1).filter(i => i >= QUOTE_MAX / 2);
    const text = /…$/.test(cut) && ends.length ? cut.slice(0, ends[ends.length - 1]) : cut;
    const ex = f.it.match_excerpt;
    // Screen r1 m7 (X20, ad/SPEC.md:194): "…" only where the shown text is cut. An excerpt that starts at a
    // sentence (the record's own text before it ends a sentence, as TJG's "…false staples. And the one thing")
    // is not cut: no "…", as the reference's "“And the one thing…”". Text before it the record lacks may be cut.
    const before = ex && Number.isInteger(Number(ex.start)) && Number(ex.start) <= String(f.it.text || '').length
      ? String(f.it.text || '').slice(0, Number(ex.start)) : null;
    const whole = before !== null && (!before.trim() || /[.!?]["”’)]?\s*$/.test(before));
    const lead = ex && whole ? '' : qt.lead || '';
    let range = null;
    for (const w of [f.it.brand_spelled, f.brand.display, f.brand.key].filter(w => w && String(w).trim().length > 1)) {
      const r = E().matchRanges(text, [{ text: String(w), whole: true }]).ranges[0];
      if (r) { range = r; break; }
    }
    const body = range ? esc(text.slice(0, range.start)) + `<mark class="s-mark${!lead && range.start === 0 ? ' s-mark--lead' : ''}">${esc(text.slice(range.start, range.end))}</mark>` + esc(text.slice(range.end)) : esc(text);
    return `“${esc(lead)}${body}”`;
  }

  /* -- Parts ---------------------------------------------------------------------- */
  function hero(v) {
    const ph = phone();
    const cov = v.cov;
    const link = ph ? 'o-link-tap-m' : 'o-link';
    // JE-COVERAGE: "We checked …" only when every episode was checked; else "We hold …" and the
    // Screened count (B().collectionSentence); no checked claim without the count.
    const counted = isNum(cov.episodes) && isNum(cov.shows)
      ? B().collectionSentence(B().checked(cov), `<a class="${link} o-coverage" href="/coverage/">${esc(B().count(cov.episodes, 'episode'))} from ${esc(B().count(cov.shows, 'show'))}</a>`, goingBack(cov)) : '';
    const hw = v.planA ? HERO.planA : HERO.full;
    const words = { h1: hw.h1,
      lede: `${counted}Find ads, ${hw.search}, <a class="${link}" href="#/compare">compare brands</a>, or <a class="${link}" href="#/alerts">create an alert</a>.` };
    const box = ph
      ? `<form id="overview-search" class="s-search" role="search"><label class="visually-hidden" for="overview-search-query">Search what podcasts say</label><input id="overview-search-query" class="o-searchbox__input-m" name="q" type="search" placeholder="Search a brand, show or phrase" autocomplete="off"><button class="s-search__go-m" type="submit">Search</button></form>`
      : `<form id="overview-search" class="o-searchbox" role="search"><label class="visually-hidden" for="overview-search-query">Search what podcasts say</label><input id="overview-search-query" class="o-searchbox__input" name="q" type="search" placeholder="Search a brand, show or phrase" autocomplete="off"><button class="o-searchbox__btn" type="submit">Search</button></form>`;
    // D38 (WORDS.md, 92d9cbef2): with a brand added, "Add another brand…"; with none, U4/X21. Words: D52.
    const trackLine = (v.tracked || []).length ? 'Add another brand to follow its new ads.' : 'Add a brand to your Overview to follow its new ads.';
    const track = `<div class="o-track"><p class="o-track__text">${trackLine}</p><a class="o-browse-btn" href="#/brands">Choose a brand</a></div>`;
    if (ph) return `<div class="s-stack s-stack--tight"><h1 class="o-h1-m">${esc(words.h1)}</h1><p class="o-lede-m">${words.lede}</p>${box}${track}</div>`;
    return `<div class="o-hero__text"><h1 class="o-h1">${esc(words.h1)}</h1><p class="o-lede">${words.lede}</p>${box}${tryChips(v)}${track}</div>`;
  }
  function tryChips(v) {
    const known = new Map((v.brands || []).map(b => [fold(b.display || b.key), b]));
    const chips = TRY.map(({ key, label, logo, phrase }) => {
      const b = (!phrase && known.get(fold(label))) || { key, display: label };
      const brand = { key: b.key || key, display: b.display || label };
      const href = esc(searchRoute((phrase ? label : brand.display).toLowerCase()));
      if (phrase) return `<a class="o-try__phrase" href="${href}">“${esc(label)}”</a>`;
      if (logo && B().hasLogo(brand)) {
        const [w, h] = logo.size;
        return `<a class="o-try__chip" href="${href}">${B().brandMark(brand, { cls: logo.cls }).replace('<img ', `<img width="${w}" height="${h}" `)}</a>`;
      }
      return `<a class="o-try__phrase" href="${href}">${esc(brand.display)}</a>`;
    });
    return `<div class="o-try"><span class="s-muted">Try</span>${chips.join('')}</div>`;
  }
  function featured(v) {
    const ph = phone();
    if (v.featFailed && !v.feat) return ph ? `<article class="o-feat-m"><span class="o-eyebrow">Hear a real ad</span>${failedBlock('This ad', 'no ad')}</article>`
      : `<article class="o-feat"><span class="o-eyebrow">Hear a real ad</span>${failedBlock('This ad', 'no ad')}</article>`;
    if (!v.feat) return '';
    const f = facts(v.feat);
    const quote = isFeatured(f.it) ? quoteHtml(f) : '';
    const one = b => (B().hasLogo(b) ? B().brandMark(b, { cls: 's-logo--15' }) : `<b class="s-strong">${esc(b.display || '')}</b>`);
    const brand = modelRead(f) ? U().brandsHtml(f.read, one) : one(f.brand);
    const line = `<div class="o-feat__line"><span class="s-adtag">AD</span>${brand}<span class="s-grow"></span>`
      + (f.heard == null ? '' : `<span class="s-card__at">${esc(at(f.heard))}</span><span class="s-meta--faint">into the episode</span>`) + '</div>';
    const meta = `<div class="o-feat__meta"><span class="o-feat__ep" title="${esc(f.ep)}">${esc(f.ep)}</span><span class="o-row__date">${NB}·${NB}${esc(f.date)}</span></div>`;
    if (ph) {
      const tile = /^\/(?!\/)/.test(f.it.artwork_url || '') ? `<img class="o-feat__cover-m" src="${esc(f.it.artwork_url)}" alt="" loading="lazy" decoding="async">`
        : `<div class="o-feat__initials-m s-initials--t${tintOf(f.show)}" aria-hidden="true">${esc(/>([^<]*)<\/div>$/.exec(B().cover({ title: f.show }))[1])}</div>`;
      return `<article class="o-feat-m"><span class="o-eyebrow">Hear a real ad</span><div class="o-feat__show-m">${tile}<div class="o-shrink"><div class="o-feat__name-m" title="${esc(f.show)}">${esc(f.show)}</div>${meta}</div></div>`
        + line + (quote ? `<p class="o-feat__quote-m">${quote}</p>` : '')
        + `<div class="o-ask__head">${featPlay(f)}<span class="s-grow"></span><a class="o-feat__open-m" href="${esc(route(f.ref))}">Open the ad</a></div></article>`;
    }
    return `<article class="o-feat"><span class="o-eyebrow">Hear a real ad</span><div class="o-feat__show">${B().cover({ title: f.show, artwork_url: f.it.artwork_url }, { size: 'xl' })}`
      + `<div class="o-shrink"><div class="o-feat__name" title="${esc(f.show)}">${esc(f.show)}</div>${meta}</div></div>`
      + line + (quote ? `<p class="o-feat__quote">${quote}</p>` : '')
      + `<div class="o-feat__actions">${featPlay(f)}<a class="o-open-l" href="${esc(route(f.ref))}">Open the ad</a></div></article>`;
  }
  function askCard() {
    if (E().current.askAvailable !== true) return '';
    const head = '<div class="o-ask__head"><span class="o-ask__eyebrow">Ask</span><span class="s-nav__new">New</span></div>';
    const text = '<p class="o-ask__text o-asktext">Answers come from the episodes, and every claim links to its passage.</p>';
    return phone()
      ? `<section class="o-ask-m">${head}<h2 class="o-ask__title-m">Ask about the ads in plain words</h2>${text}<a class="o-ask__btn-m" href="#/ask">Ask a question</a></section>`
      : `<section class="o-ask">${head}<h2 class="o-ask__title">Ask about the ads in plain words</h2>${text}<a class="o-ask__btn" href="#/ask">Ask a question</a></section>`;
  }
  // Q6: 9 shows with art, most ads first; shows without art fill in only when fewer have art (initials, F1).
  function strip(v) {
    const known = isNum(v.cov.shows);
    const head = words => `<div class="o-strip__head"><h2 class="o-h2">${esc(known ? `${B().count(v.cov.shows, 'show')} in the collection` : words)}</h2><span class="s-muted">A few of them</span></div>`;
    // S1 (OVERVIEW-3): still reading, the strip keeps its full height: the real grid with 9 cover and name
    // blocks (filled in place when its read answers); a failure sits over the same grid, hidden.
    const skel = '<div class="o-strip__item"><div class="o-strip__skel" aria-hidden="true"></div><div class="o-strip__skelname" aria-hidden="true"><div class="o-strip__skelbar"></div></div></div>'.repeat(9);
    if (v.shows === undefined) return `<section class="o-strip" data-o-slot="strip">${head('In the collection')}<div class="o-strip__grid" aria-busy="true">${skel}</div></section>`;
    if (v.showsFailed) return `<section class="o-strip">${head('Shows in the collection')}<div class="o-strip__hold"><div class="o-strip__grid" aria-hidden="true">${skel}</div>${failedBlock('Shows', 'no shows')}</div></section>`;
    if (!Array.isArray(v.shows) || !v.shows.length) return '';
    const art = s => /^\/(?!\/)/.test(s.artwork_url || '');
    const byAds = (a, b) => (Number(b.ad_breaks) || 0) - (Number(a.ad_breaks) || 0);
    const pick = [...v.shows.filter(art).sort(byAds), ...v.shows.filter(s => !art(s)).sort(byAds)].slice(0, 9);
    const items = pick.map(s => {
      const name = s.display_title || s.title || '';
      const img = art(s) ? `<img class="o-strip__img" src="${esc(s.artwork_url)}" alt="" loading="lazy" decoding="async">`
        : `<div class="o-strip__initials s-initials--t${tintOf(name)}" aria-hidden="true">${esc(/>([^<]*)<\/div>$/.exec(B().cover({ title: name }))[1])}</div>`;
      return `<div class="o-strip__item">${img}<span class="o-strip__name" title="${esc(name)}">${esc(name)}</span></div>`;
    }).join('');
    return `<section class="o-strip">${head('Shows in the collection')}<div class="o-strip__grid">${items}</div></section>`;
  }
  function rowHtml(item, i) {
    const f = facts(item);
    const time = f.heard == null ? '' : `<span class="s-row__at">${esc(at(f.heard))}</span><span class="s-meta--faint">into the episode</span>`;
    const open = cls => `<a class="${cls}" href="${esc(route(f.ref))}">Open the ad</a>`;
    if (phone()) {
      // M9 (screen r1 m9): a plain-text brand takes at most half the line, so "on {show}" keeps room.
      // PODS: model-named names (each with its label) take their own line, whole, above "on {show}" (the line clips).
      const named = modelRead(f) ? `<span class="o-row__names-m">${U().brandsHtml(f.read, b => (B().hasLogo(b) ? mark(b, 'o-row__logo-m') : `<span class="o-row__name-m">${esc(b.display)}</span>`))}</span>` : '';
      const who = (named ? '' : B().hasLogo(f.brand) ? mark(f.brand, 'o-row__logo-m') : `<span class="o-row__brand-m" title="${esc(f.brand.display)}">${esc(f.brand.display)}</span> `) + `on <b class="o-b">${esc(f.show)}</b>`;
      return `<div class="o-row-m"><div>${B().cover({ title: f.show, artwork_url: f.it.artwork_url }, { size: 'sm' })}</div><div class="s-row__body">${named}`
        + `<span class="o-row__line-m"><span class="o-row__who-m" title="${esc(f.show)}">${who}</span><span class="o-row__date">· ${esc(f.date)}</span></span>`
        + (f.heard == null ? '' : `<span class="o-row__when-m"><b class="o-b o-b--num">${esc(at(f.heard))}</b> into the episode</span>`)
        + `<div class="o-try">${playHtml(f, 'ov-r' + i, 's-play-m')}<span class="s-grow"></span>${open('s-btn-m')}</div></div></div>`;
    }
    const one = b => (B().hasLogo(b) ? mark(b, 'o-row__logo') : `<span class="o-row__brand">${esc(b.display)}</span>`);
    const brand = modelRead(f) ? U().brandsHtml(f.read, one) : one(f.brand);
    return `<div class="o-row"><div class="o-row__art">${B().cover({ title: f.show, artwork_url: f.it.artwork_url })}</div><div class="s-row__body">`
      + `<div class="o-row__meta${modelRead(f) ? ' o-row__meta--named' : ''}">${brand}<span class="o-row__on">on</span><b class="o-row__show" title="${esc(f.show)}">${esc(f.show)}</b></div>`
      + `<div class="o-row__meta o-row__epline"><span class="s-row__ep" title="${esc(f.ep)}">${esc(f.ep)}</span><span class="o-row__date">· ${esc(f.date)}</span></div></div>`
      + `<div class="o-row__side"><div class="o-row__time">${time}</div><div class="o-row__actions">${playHtml(f, 'ov-r' + i, 's-play')}${open('o-open')}</div></div></div>`;
  }
  function recent(v) {
    const ph = phone();
    const all = E().searchURL({ mode: 'ads', sort: 'newest', page: 1 });
    const head = ph ? `<div class="o-recent__head-m"><h2 class="o-h2-m">Recent ads</h2><a class="o-seeall-m" href="${esc(all)}">See all</a></div>`
      : `<div class="o-recent__head"><h2 class="o-h2-grow">Recent ads</h2><a class="o-seeall" href="${esc(all)}">See all in Search →</a></div>`;
    const body = v.recentFailed ? failedBlock('Recent ads', 'no ads')
      : v.rows.length ? `<div class="s-list">${v.rows.map(rowHtml).join('')}</div>`
        : `<p class="o-note">${v.anyAds ? `The newest ads are in <a class="o-link" href="${esc(all)}">Search</a>.` : 'No ads found.'}</p>`;
    return ph ? `<section class="s-stack o-side--10">${head}${body}</section>` : `<section class="o-recent">${head}${body}</section>`;
  }
  function brandsCard(v) {
    const ph = phone();
    const added = v.tracked || [];
    const info = key => (v.brands || []).find(b => fold(b.key || b.brand) === fold(key)) || null;
    let title, rows;
    if (v.trackedFailed) { title = 'Tracked brands'; rows = failedBlock('Tracked brands', 'no brands'); }
    else if (added.length) {
      title = 'Tracked brands';
      rows = '<div class="o-tracked">' + added.map(it => {
        const key = (it.target || {}).id || '';
        const b = info(key) || { key, display: it.title || key };
        const name = b.display || b.brand || key;
        const n = b.ad_breaks != null && isNum(b.ad_breaks) ? `<span class="s-meta">${esc(B().count(b.ad_breaks, 'ad'))}</span>` : '';
        const label = B().hasLogo({ key: b.key || key, display: name }) ? B().brandMark({ key: b.key || key, display: name }, { cls: 's-logo--14' }) : `<span class="o-tracked__name" title="${esc(name)}">${esc(name)}</span>`;
        return `<div class="o-tracked__row"><a class="o-tracked__link" href="#/brand/${esc(encodeURIComponent(key))}">${onlyUnit(b, label)}</a><span class="s-grow"></span>${n}</div>`;
      }).join('') + '</div>' + (v.brandsFailed ? failedBlock('Ad counts', 'no ads') : '');
    } else if (v.brandsFailed) {
      title = 'Brands to add'; rows = failedBlock('Brands to add', 'no brands');
    } else {
      title = 'Brands to add';
      // PODS: a model_only brand is not offered (+ Add would save nothing: the server answers "unknown brand").
      const picks = (v.brands || []).filter(b => b && b.key && !b.under_review && b.model_only !== true).sort((a, b) => (Number(b.ad_breaks) || 0) - (Number(a.ad_breaks) || 0)).slice(0, 3);
      rows = picks.length ? '<div class="o-tracked">' + picks.map((b, i) => {
        const name = b.display || b.key;
        const label = B().hasLogo(b) ? B().brandMark(b, { cls: 's-logo--' + (14 + i) }) : `<span class="o-tracked__name" title="${esc(name)}">${esc(name)}</span>`;
        return `<div class="o-tracked__row">${label}<span class="s-grow"></span><button type="button" class="${ph ? 'o-btn-m' : 's-btn'}" data-b2-add data-brand="${esc(b.key)}" data-name="${esc(name)}">+ Add<span class="visually-hidden"> ${esc(name)}</span></button></div>`;
      }).join('') + '</div>' : '';
    }
    // OVERVIEW-2 L1: still reading the brands, the card waits in place (fill() draws it again).
    const wait = v.brands === undefined && !v.trackedFailed ? ' data-o-slot="brands" aria-busy="true"' : '';
    return `<section class="s-stack${ph ? ' o-side--10' : ' s-stack--tight'}"${wait}><h2 class="o-side__title">${title}</h2>${rows}<div class="o-side-cta"><a class="o-browse-btn" href="#/brands">Choose a brand</a></div></section>`;
  }
  function sideCard(title, body, action, words) {
    const ph = phone();
    return `<section class="s-stack ${ph ? 'o-side--8' : 'o-side--10'}"><h2 class="o-side__title">${title}</h2>${body}`
      + `<button type="button" class="${ph ? 'o-btn-m o-btn-m--start' : 's-btn o-btn--start'}" data-action="${action}">${words}</button></section>`;
  }
  const listRow = (href, name, meta) => `<div class="o-tracked__row"><a class="o-tracked__link" href="${esc(href)}">${esc(name)}</a><span class="s-grow"></span>${meta ? `<span class="s-meta">${esc(meta)}</span>` : ''}</div>`;
  function campaignsCard(v) {
    if (v.overviewFailed) return sideCard('Your campaigns', failedBlock('Your campaigns', 'no campaigns'), 'create-campaign', 'Start a campaign');
    // VL-10, R1-07: read-only and archived campaigns are not tracked here.
    const live = (v.d.tracked_campaigns || []).filter(c => c.state !== 'Read-only' && c.state !== 'Archived');
    const body = live.length ? `<div class="o-tracked">${live.slice(0, 3).map(c => listRow('#/campaign/' + c.id, c.name || 'Campaign', c.state || 'Draft')).join('')}</div>`
      : `<p class="o-note">${esc(D3)}</p>`;
    return sideCard('Your campaigns', body, 'create-campaign', 'Start a campaign');
  }
  function alertsCard(v) {
    if (v.overviewFailed) return sideCard('Alert activity', failedBlock('Alert activity', 'no alerts'), 'overview-alert', 'Create alert');
    const d = v.d;
    const totals = Array.isArray(d.alert_totals) ? d.alert_totals : [];
    const rank = t => ({ active_local: 0, paused: 1, stopped: 2 })[t.lifecycle] ?? 1;
    const rows = [...totals].sort((a, b) => rank(a) - rank(b)).slice(0, 3).map(t => {
      const n = Number(isNum(t.new_ads) ? t.new_ads : 0);
      return listRow('#/alert/' + encodeURIComponent(t.alert_id), t.alert_name || 'Alert', n ? `${n.toLocaleString('en-US')} new ${n === 1 ? 'match' : 'matches'}` : '');
    });
    const lc = d.alert_lifecycle_counts || {};
    const set = (Number(lc.active_local) || 0) + (Number(lc.paused) || 0) > 0 || totals.length > 0 || (d.recent_events || []).length > 0;
    const body = rows.length ? `<div class="o-tracked">${rows.join('')}</div>`
      : set ? '<p class="o-note">No recent alert matches. Alerts keep checking the stored collection.</p>'
        : '<p class="o-note">No alerts yet. Create one to hear when a brand or show gets a new ad.</p>';
    return sideCard('Alert activity', body, 'overview-alert', 'Create alert');
  }
  function pageHtml(v) {
    if (phone()) {
      return `<div class="o-page o-page--phone"><div class="o-main-m">${hero(v)}${featured(v)}${recent(v)}${askCard()}${brandsCard(v)}${campaignsCard(v)}${alertsCard(v)}</div></div>`;
    }
    return `<div class="o-page"><div class="o-main"><section class="o-hero">${hero(v)}<div class="o-hero__side">${featured(v)}${askCard()}</div></section>`
      + strip(v)
      + `<div class="o-lower">${recent(v)}<aside class="o-aside">${brandsCard(v)}${campaignsCard(v)}${alertsCard(v)}</aside></div></div></div>`;
  }

  /* -- Render ------------------------------------------------------------------------ */
  async function render(token) {
    const { signal, live } = guarded(token);
    // READ-QUEUE: what the person sees first goes first.
    const overviewRead = E().api('/customer/overview', { signal }).catch(failed);
    const trackedRead = window.EWSaved.trackedBrands(E().api, { signal }).catch(failed);
    const recentRead = search(ADS, { signal }).catch(failed);
    const featRead = search({ ...ADS, ids: [FEATURED.ref] }, { signal }).catch(failed);
    const [data, tracked, ads, feat] = await Promise.all([overviewRead, trackedRead, recentRead, featRead]);
    if (!live()) return;
    for (const r of [data, tracked, ads, feat]) if (r && r.failed && r.failed.droppedContext) return '';
    // The ads read carries the plan signal (excerpt_only), as on Search.
    if (ads && !ads.failed) E().notePlan(ads, { result: true });
    // OVERVIEW-2 L1: the page draws now; the directories (the last and slowest reads) fill their parts in
    // place when they answer. Answers already kept (sharedRead) are in the first draw.
    const got = {};
    const late = {
      brands: dirRead('/customer/brands', { signal, authorizedBy: data }).then(r => r.brands || [], failed).then(r => { got.brands = r; }),
      shows: phone() ? null : dirRead('/customer/shows', { signal, authorizedBy: data }).then(r => r.shows || [], failed).then(r => { got.shows = r; }),
    };
    await Promise.race([Promise.all([late.brands, late.shows]), new Promise(res => setTimeout(res, 0))]);
    if (!live()) return;
    const dir = name => (got[name] === undefined ? undefined : got[name].failed ? null : got[name]);
    const dirFailed = name => !!(got[name] && got[name].failed);
    const overviewFailed = !!(data && data.failed);
    const d = overviewFailed ? {} : data;
    const named = ads && !ads.failed ? namedAds(ads.items) : [];
    const configured = (feat && !feat.failed ? namedAds(feat.items).find(isFeatured) : null) || named.find(isFeatured) || null;
    // No configured record: the first recent ad with a clip, else the newest (SPEC Data 3).
    const clip = it => !!(it.read && it.read.has_clip);
    const pick = configured || named.find(clip) || named[0] || null;
    const v = {
      d, overviewFailed, cov: d.coverage || {}, planA: E().excerptOnly(),
      feat: pick, featFailed: !pick && !!(ads && ads.failed),
      rows: named.filter(it => !pick || it.ref !== pick.ref).slice(0, 4),
      recentFailed: !!(ads && ads.failed), anyAds: !!(ads && (ads.items || []).length),
      tracked: Array.isArray(tracked) ? tracked : [], trackedFailed: !Array.isArray(tracked),
      brands: dir('brands'), brandsFailed: dirFailed('brands'),
      shows: phone() ? null : dir('shows'), showsFailed: dirFailed('shows'),
    };
    // Screen check: a cover is the browser's own request, outside the client's queue, but the server counts it
    // (2 per session). While a directory read is out, covers wait (data-o-src), so they never meet those reads
    // there (429 after 8 s: a broken cover); they load once both answered, as before L1.
    const waiting = () => Object.keys(late).some(name => late[name] && got[name] === undefined);
    const hold = html => (waiting() ? html.replace(/(<img\b[^>]*?) src="(\/api\/[^"]*)"/g, '$1 data-o-src="$2"') : html);
    for (const [name, part] of [['brands', brandsCard], ['shows', strip]]) {
      if (late[name] && got[name] === undefined) {
        late[name].then(() => {
          const r = got[name];
          if (r && r.failed && r.failed.droppedContext) return;
          afterDraw(token, live, () => {
            const el = document.querySelector(`[data-o-slot="${name === 'shows' ? 'strip' : name}"]`);
            if (!el) return;
            const out = hold(part({ ...v, [name]: dir(name), [name + 'Failed']: dirFailed(name) }));
            el.outerHTML = out;
            if (/data-action="retry"/.test(out)) E().current.publishedReadFailed = true;
            queueFit();
          });
        });
      }
    }
    if (waiting()) {
      Promise.all([late.brands, late.shows]).then(() => afterDraw(token, live, () => {
        for (const img of document.querySelectorAll('img[data-o-src]')) { img.src = img.getAttribute('data-o-src'); img.removeAttribute('data-o-src'); }
        // OVERVIEW-3 (ROV-R2-01): the early draw is not kept for Back; the whole page is, once both parts are in.
        E().keepFilledPage(token, pageHtml({ ...v, brands: dir('brands'), brandsFailed: dirFailed('brands'), shows: v.shows === null ? null : dir('shows'), showsFailed: dirFailed('shows') }));
      }));
    }
    queueFit();
    return hold(pageHtml(v));
  }
  // Runs fn once this render's page is on screen (the shell publishes right after render resolves), in the
  // order asked; a newer render (another page, Retry, a resize across 600 px) drops it.
  function afterDraw(token, live, fn) {
    let tries = 0;
    const go = () => {
      if (!live()) return;
      if (E().current.publishedToken !== token) { if (tries++ < 100) setTimeout(go, 100); return; }
      fn();
    };
    setTimeout(go, 0);
  }

  /* -- + Add ---------------------------------------------------------------------- */
  async function add(btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const key = btn.dataset.brand || '', name = btn.dataset.name || key;
    btn.setAttribute('aria-busy', 'true');
    btn.disabled = true;
    try {
      await window.EWSaved.trackBrand(E().api, key);
      E().toast(`Tracking ${name}. It is in Saved, under Bookmarks.`);
      E().render();
    } catch (err) {
      if (err && err.droppedContext) return;
      if (err && (err.status === 409 || err.status === 412 || err.status === 404)) { E().toast('Tracking changed elsewhere. The page now shows the current state.'); E().render(); return; }
      E().toast(err && err.message ? err.message : `Your change could not be confirmed.`);
    } finally {
      btn.removeAttribute('aria-busy');
      btn.disabled = false;
    }
  }
  const handlers = {
    click(event) {
      const t = event.target;
      const btn = t && t.closest && t.closest('[data-b2-add]');
      if (!btn) return false;
      if (event.preventDefault) event.preventDefault();
      add(btn);
      return true;
    },
  };

  /* -- Text cut at a word end (search/tools/textcut.cjs; SPEC rules) --------------- */
  // Every clamped or one-line text is cut in the text, at a word end, with "…",
  // for the width it is shown at (recheck 5 #3); a short last word is dropped
  // (recheck 4 #4); the whole text stays in title; a resize cuts again.
  const TRIM = /[\s,;:.…—–-]+$/;
  const FN = /\s+(?:the|a|an|and|or|of|with|to|in|on|for|at|by|from|as|&)$/i;
  function fitText(body, fits) {
    const ends = [0];
    for (let i = 1; i < body.length; i++) if (/\s/.test(body[i]) && !/\s/.test(body[i - 1])) ends.push(i);
    const cut = k => {
      let kept = body.slice(0, ends[k]).replace(TRIM, '');
      for (let g = 0; g < 3 && FN.test(kept); g++) { const t = kept.replace(FN, '').replace(TRIM, ''); if (!t) break; kept = t; }
      return kept + '…';
    };
    let lo = 0, hi = ends.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (fits(cut(mid))) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best < 0 ? null : cut(best);
  }
  const FIT = '.o-page .o-row__show, .o-page .s-row__ep, .o-page .o-feat__name, .o-page .o-feat__name-m, .o-page .o-feat__ep, .o-page .o-strip__name, .o-page .o-tracked__name, .o-page .o-feat__quote, .o-page .o-row__who-m';
  const ORIG = new WeakMap();
  // The phone's "{brand} on {show}" cuts inside the show name (K2), so the brand and "on" stay.
  const cutRoot = el => (el.classList.contains('o-row__who-m') && el.querySelector('b.o-b')) || el;
  // A long plain brand name can leave the show no room: then the whole line is cut at a word end.
  // N12 (screen r3 n12; review n12-fit-r1): with a plain brand, a cut show must keep 60 px (SPEC M9, measured as
  // verify.cjs: who.right - show.left), else the brand gives way. The brand is first cut inside its own cap.
  const SHOW_MIN = 60;
  const texts = root => { const a = [], tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); for (let t = tw.nextNode(); t; t = tw.nextNode()) a.push(t); return a; };
  function fitOne(el) {
    const root = cutRoot(el);
    if (root === el) { fitIn(el, el); return; }
    // Every fit starts again from the whole text (RN12-04: saved before any cut, a whole-line cut before included).
    const nodes = texts(el);
    if (!ORIG.has(el)) ORIG.set(el, nodes.map(t => t.data));
    const saved = ORIG.get(el);
    if (nodes.length === saved.length) nodes.forEach((t, i) => { t.data = saved[i]; });
    const brand = el.querySelector('.o-row__brand-m');
    if (brand) fitIn(brand, brand);
    const whole = texts(root).map(t => t.data).join('');
    const keeps = () => texts(root).map(t => t.data).join('') === whole || el.getBoundingClientRect().right - root.getBoundingClientRect().left >= SHOW_MIN;
    const fits = () => fitIn(el, root) && (!brand || keeps());
    if (fits()) return;
    const bt = brand && texts(brand);
    if (bt && bt.length === 1) {
      const capped = bt[0].data;
      const best = fitText(ORIG.get(brand).join(''), v => { bt[0].data = v; return fits(); });
      if (best !== null && best !== '…') { bt[0].data = best; fits(); return; }
      bt[0].data = capped;
    }
    // No brand cut gives the show 60 px: keep the show as it fits, short; else cut the whole line (RN12-03).
    if (!fitIn(el, root)) fitIn(el, el);
  }
  // true when the text fits (as is or cut); false when no word-end cut fits.
  function fitIn(el, root) {
    const nodes = [];
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) nodes.push(t);
    if (!nodes.length) return true;
    if (!ORIG.has(root)) ORIG.set(root, nodes.map(t => t.data));
    const orig = ORIG.get(root);
    if (orig.length !== nodes.length) return true;
    nodes.forEach((t, i) => { t.data = orig[i]; });
    const over = () => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
    if (!el.clientWidth || !over()) return true;
    // A quote keeps its closing mark after the "…".
    const full = orig.join('');
    const close = /”$/.test(full) ? '”' : '';
    const set = v => {
      const cut = v.endsWith('…');
      let left = (cut ? v.slice(0, -1) : v).length;
      nodes.forEach((t, i) => { const take = Math.max(0, Math.min(orig[i].length, left)); t.data = orig[i].slice(0, take); left -= take; });
      const end = [...nodes].reverse().find(t => t.data.trim()) || nodes[0];
      end.data = end.data.replace(/\s+$/, '') + (cut ? '…' : '') + close;
    };
    const best = fitText(full.slice(0, full.length - close.length), v => { set(v); return !over(); });
    if (best === null || best === '…') { nodes.forEach((t, i) => { t.data = orig[i]; }); return false; }
    set(best);
    return true;
  }
  function fitAll() {
    if (typeof document === 'undefined' || !document.createTreeWalker || !document.querySelector('.o-page')) return;
    for (const el of document.querySelectorAll(FIT)) fitOne(el);
  }
  let queued = false;
  function queueFit() {
    if (queued || typeof window.requestAnimationFrame !== 'function') return;
    queued = true;
    window.requestAnimationFrame(() => {
      queued = false;
      fitAll();
      // K3 (RL1-06, as Brand's RL1-01): WebKit fires no 'loadingdone'; a face still loading re-fits on the next fonts.ready.
      if (document.fonts && document.fonts.status === 'loading' && document.fonts.ready) document.fonts.ready.then(queueFit);
    });
  }
  if (typeof window.addEventListener === 'function' && typeof window.requestAnimationFrame === 'function') {
    window.addEventListener('resize', queueFit);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
    // K3 (RL1-06, as Brand's RBC-01): a face that loads after fonts.ready re-fits too. Attached once, here at load;
    // the fit changes text only, so it never loads a face and never loops.
    if (document.fonts && typeof document.fonts.addEventListener === 'function') document.fonts.addEventListener('loadingdone', queueFit);
  }

  window.EWB2.page('overview', {
    on: ON,
    render: token => render(token),
    handlers,
  });
  // For the page's tests (the word-end cut and the S8 collection words).
  window.EWOverviewB2 = Object.freeze({ fitText, goingBack });
})();
