/* Redesigned Brand page (#/brand/{key}). REDESIGN-BUILD; build source
   docs/landing/build-spec/brand/SPEC.md (aaecbd611, the polish commit on
   c9ea045ca); plan ws-app/logs/redesign/PLAN.md section 5.
   Owner: the Brand page builder (this file, app/brand-b2.css, tests/frontend_b2_brand.test.cjs).
   Route switch: ON. false keeps today's page; true draws this one.
   render(token, id, old): today's reads (the brand read, tracked brands, the
   brand's newest ads as Search's brand filter reads them, its weekly summary) plus the Overview read (the Ads
   checked counts and the S8 range, one source with Overview and Search),
   the summary by show (B1) and the shows directory (tile art). Server
   fields with their fallbacks: S1 heard_at_s (else the ad window start), S2
   artwork_url (else initials), S4 read.media (else Play on today's Search
   player, no length), S5 week cells in distinct ads (else no chart), S6
   ads_with_clips (else the clip line's first sentence), S7 match_excerpt
   (the row quote is Search's), S8 coverage.earliest_episode (else the start
   date is dropped). */
(() => {
  'use strict';
  const ON = true;
  const E = () => window.EW;
  const U = () => window.EWUI;
  const B = () => window.EWB2;
  const X = () => window.EWViews._b2;
  const esc = v => E().esc(v);
  const phone = () => B().layout() === 'phone';
  const NB = '\u00a0';
  const isNum = v => v != null && v !== '' && Number.isFinite(Number(v));
  const num = n => Number(n).toLocaleString('en-US');
  const fold = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const HINT = 'Your Overview is your home page. Added brands show their new ads there.';

  /* -- Words ---------------------------------------------------------------------- */
  const realDay = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && (t => Number.isFinite(t.getTime()) && t.toISOString().slice(0, 10) === v)(new Date(v + 'T12:00:00Z'));
  // Today, for the year words (the tests set it).
  let todayIso = null;
  const thisYear = () => (todayIso ? Number(todayIso.slice(0, 4)) : new Date().getUTCFullYear());
  const yearOf = v => (/^\d{4}-\d{2}-\d{2}/.test(v || '') ? Number(String(v).slice(0, 4)) : null);
  // "{Mon} {D}", with its year when asked, else when not this year (NOGAP SR row 42).
  function dateWords(v, withYear = null) {
    const d = E().date(v);
    const year = yearOf(v);
    const show = withYear === null ? year !== thisYear() : withYear;
    return year && show && /^[A-Z][a-z]{2} \d/.test(d) ? `${d}, ${year}` : d;
  }
  // The same, held together (no-break spaces): a date never splits (R6, R7).
  const day = (v, withYear = null) => dateWords(v, withYear).replace(/ /g, NB);
  const dayName = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || '')); return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : ''; };
  const days = (a, b) => (Date.parse(String(b).slice(0, 10) + 'T00:00:00Z') - Date.parse(String(a).slice(0, 10) + 'T00:00:00Z')) / 86400000;
  // Collection words (Reno, Landing 849c650c4 + 399bed463; replace the date range): "going back to
  // {year}" from the Overview read's S8 coverage.earliest_episode; a start in the current year reads
  // "going back to {Mon D}". No S8: no words (never a fixed string).
  function collectionWords(cov) {
    const first = cov && realDay(cov.earliest_episode) ? cov.earliest_episode : null;
    if (!first) return '';
    return `, going back to ${yearOf(first) === thisYear() ? day(first) : yearOf(first)}`;
  }
  // B1 (computed, never copied): the shows after the tiles, from summary group_by=show.
  // Without per-show counts that cover all {k} shows: review decision 3's words.
  function moreLine(brand, shows, tileIds, cells) {
    const k = (Number(shows) || 0) - tileIds.length;
    if (!(k > 0)) return '';
    const name = brand.display;
    const tiles = new Set(tileIds);
    const rest = Array.isArray(cells) ? cells.filter(c => c && c.key !== 'unknown' && !tiles.has(c.key)) : [];
    const n = c => Number(isNum(c.breaks) ? c.breaks : c.count);
    if (rest.length !== k || !rest.every(c => n(c) > 0)) return k === 1 ? `1 more show has fewer ${name} ads.` : `${num(k)} more shows have fewer ${name} ads.`;
    const a = Math.min(...rest.map(n)), b = Math.max(...rest.map(n));
    const ads = v => `${num(v)} ${name} ${v === 1 ? 'ad' : 'ads'}`;
    if (k === 1) return `1 more show has ${ads(a)}.`;
    return a === b ? `${num(k)} more shows have ${ads(a)} each.` : `${num(k)} more shows have ${num(a)} or ${ads(b)} each.`;
  }
  // Initials: a name that starts with digits is those digits ("99% Invisible" -> "99", RC2 #4);
  // else the base rule. The tint is the base cover's, one per show on every page.
  function tile(name) {
    const html = B().cover({ title: name });
    const tint = (/s-initials--t(\d)/.exec(html) || [0, 0])[1];
    const lead = /^(\d+)/.exec(String(name || '').trim());
    const text = lead ? lead[1] : (/>([^<]*)<\/div>$/.exec(html) || [0, '?'])[1];
    return { tint, text };
  }
  const sameOrigin = url => typeof url === 'string' && /^\/(?!\/)/.test(url);
  function rowCover(show, art, ph) {
    if (sameOrigin(art)) return B().cover({ title: show, artwork_url: art }, { size: ph ? 'sm' : '' });
    const t = tile(show);
    return `<div class="s-initials${ph ? ' s-initials--sm' : ''} s-initials--t${t.tint}" aria-hidden="true">${esc(t.text)}</div>`;
  }

  /* -- Reads ------------------------------------------------------------------------ */
  function guarded(token) {
    const controller = new AbortController();
    E().current.aborters.set(token, controller);
    return { signal: controller.signal, live: () => E().current.fetchToken === token && E().current.aborters.get(token) === controller };
  }
  const qs = (path, params, opts) => {
    const { group_by, ...query } = params;
    const p = new URLSearchParams({ query: JSON.stringify(query) });
    if (group_by) p.set('group_by', group_by);
    return E().api(path + '?' + p.toString(), opts);
  };
  const failed = err => ({ failed: err || new Error('request failed') });
  // Brand names seen on this tab, so the loading state can name the brand (SPEC loading words).
  const NAMES = new Map();
  const remember = b => { if (b && b.key && b.display) NAMES.set(String(b.key), String(b.display)); };

  /* -- One ad (Search's facts, quote, tags and Play) ----------------------------------- */
  function facts(item) {
    const it = item || {};
    const { read, span } = U().resultFacts(it);
    const brand = (read && read.brand) || {};
    const heard = isNum(it.heard_at_s) ? Number(it.heard_at_s) : span ? span[0] : null;
    return { it, read, span, brand, heard, ref: it.ref || '', show: it.show_title || (it.show || {}).display_title || 'Unknown show',
      ep: it.ep_title || it.episode_id || 'Episode', date: dateWords(it.pub_date) };
  }
  // n6: the rows are Search's rows (search/SPEC.md:106, brand/SPEC.md cross #2): one per ad as
  // Search draws it (its rowsOf: result.ad_breaks, the read adAttachItem picks; without ad groups,
  // the ad's first read), in Search's order. Then only named reads: a brand page always names its
  // brand (never held). A known_bleed ad never becomes a row (VISDATA-AVOID item 1: the old Brand
  // page's RECENT-ADS B1 rule, views.js recentPick; Search still shows it).
  function namedAds(items, adBreaks) {
    const list = items || [];
    let picks;
    if (Array.isArray(adBreaks)) {
      const used = new Set();
      picks = [];
      for (const b of adBreaks) {
        let group = list.filter(x => x.ad_break_id === b.id);
        if (!group.length) group = list.filter(x => (b.refs || []).includes(x.ref));
        group = group.filter(x => !used.has(x));
        if (!group.length) continue;
        group.forEach(x => used.add(x));
        picks.push(X().adAttachItem(b, group));
      }
      picks.push(...list.filter(x => !used.has(x)));
    } else {
      const seen = new Set();
      picks = list.filter(it => !it.ad_break_id || (seen.has(it.ad_break_id) ? false : seen.add(it.ad_break_id)));
    }
    return picks.filter(it => {
      const read = it && it.read && !it.read.historical ? it.read : null;
      return !!read && !it.known_bleed && it.record_kind !== 'transcript_passage' && !U().underReview(read) && !!(read.brand || {}).display;
    });
  }
  // Search's quote (S7: the same quote for one ad everywhere): the brand word marked
  // once, the transcript's punctuation, a cut at a word end with "…" (the fit below
  // cuts again for the width shown; the CSS clamp is only the safety net).
  const TRIM = /[\s,;:.…—–|\/-]+$/;
  const FN = /\s+(?:the|a|an|and|or|of|with|to|in|on|for|at|by|from|as|&)$/i;
  // n7 (brand/SPEC.md:233): a cut drops a short last word ("the", "and", "of", "with"…) before the "…".
  const dropShort = kept => {
    for (let g = 0; g < 3 && FN.test(kept); g++) { const t = kept.replace(FN, '').replace(TRIM, ''); if (!t) break; kept = t; }
    return kept;
  };
  function cutText(s, max) {
    if (s.length > max) {
      const part = s.slice(0, max + 1);
      const sp = part.lastIndexOf(' ');
      return dropShort((sp > 0 ? part.slice(0, sp) : s.slice(0, max)).replace(TRIM, '')) + '…';
    }
    return /[.!?…]["”’)]?$/.test(s) ? s : s.replace(TRIM, '') + '…';
  }
  function quoteHtml(f, max, show) {
    const qt = U().quoteText(f.it) || { lead: '', text: '' };
    let text = String(qt.text || '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    const ex = f.it.match_excerpt;
    let lead = ex && Number(ex.start) === 0 ? '' : qt.lead || '';
    if (!lead && /^\p{Ll}/u.test(text)) lead = '…';
    let range = null;
    for (const w of [f.it.brand_spelled, f.brand.display, f.brand.key].filter(w => w && String(w).trim().length > 1)) {
      const r = E().matchRanges(text, [{ text: String(w), whole: true }]).ranges[0];
      if (r) { range = r; break; }
    }
    if (range && range.end > show - 20) {
      let from = Math.max(0, range.start - 50);
      const sp = text.lastIndexOf(' ', from);
      from = sp > 0 ? sp + 1 : 0;
      if (from > 0) { text = text.slice(from); lead = '…'; range = { start: range.start - from, end: range.end - from }; }
    }
    const cut = cutText(text, max);
    const cutAt = cut === text ? cut.length : cut.length - 1;
    const body = cut.slice(0, cutAt);
    const tail = cut.slice(cutAt);
    const html = range && range.end <= body.length
      ? esc(body.slice(0, range.start)) + `<mark class="s-mark${!lead && range.start === 0 ? ' s-mark--lead' : ''}">${esc(body.slice(range.start, range.end))}</mark>` + esc(body.slice(range.end))
      : esc(body);
    return `“${esc(lead)}${html}${tail}”`;
  }
  // Search's tags (recheck 2 cross #1): what was read out, and the spelling line
  // only when the shown quote says it and it differs from the name.
  const quoteSays = (quote, word) => !!E().matchRanges(String(quote || '').replace(/<[^>]+>/g, ''), [{ text: String(word), whole: true }]).ranges[0];
  function tagsHtml(f, quote) {
    const said = (f.read && f.read.said) || {};
    const tags = [
      ...(said.web_addresses || []).map(x => `<span class="s-tag--code-sm">Web address read out <b class="s-code">${esc(x)}</b></span>`),
      ...(said.promo_codes || []).map(x => `<span class="s-tag--code-sm">Promo code read out <b class="s-code">${esc(x)}</b></span>`),
    ];
    const sp = f.it.brand_spelled;
    if (sp && fold(sp) !== fold(f.brand.display) && quoteSays(quote, sp)) tags.push(`<span class="s-meta">The transcript spells it “${esc(sp)}”.</span>`);
    return tags.length ? `<div class="s-panel__tags">${tags.join('')}</div>` : '';
  }
  // "Play {length}" (P6) on today's clip engine, in the reference's look (b-playlen,
  // the phone triangle, the label in a span); a clip whose block is not on the row
  // (S4 missing) plays on today's Search player, no length; no clip: "No audio clip" (UX D50).
  function playHtml(f, id, ph) {
    const read = f.read || {};
    const cls = ph ? 's-play-m b-playlen' : 's-play b-playlen';
    const disc = '<span class="s-play__disc"><span class="s-play__tri s-play__tri--phone"></span></span>';
    if (read.has_clip === true && read.media === undefined && f.span) {
      return `<button type="button" class="${cls}" data-action="search-play" data-ref="${esc(f.ref)}" data-from="${esc(f.span[0])}" data-to="${esc(f.span[1])}" aria-label="Play ${esc(f.brand.display || 'ad')} ad, ${esc(f.show)}">${disc}<span>Play</span></button>`;
    }
    const html = B().play({ ref: f.ref, media: read.media, span: f.span, id, size: ph ? 'phone' : '' });
    if (!/data-b2-play/.test(html)) return html;
    return html
      .replace(/class="s-play(?:-m)?" data-action/, `class="${cls}" data-action`)
      .replace(/<span class="s-play__disc"><span class="s-play__tri[^"]*"><\/span><\/span>/, disc)
      .replace(/(<\/span><\/span>)(Play(?: [\d:]+)?)(<\/button>)/, '$1<span>$2</span>$3');
  }
  const adRoute = ref => '#/evidence/' + encodeURIComponent(ref);

  /* -- Head ---------------------------------------------------------------------------- */
  // Q7 (Landing final): the back link names the origin; "← All brands" only with none.
  function back(v, ph) {
    const c = E().current;
    const origin = String((c.back && c.back.get(c.route)) || '');
    const path = origin.split('?')[0];
    const cls = ph ? 'b-back b-back-m' : 'b-back';
    if (!origin || path === '#/brands') return `<a class="${cls}" href="#/brands">← All brands</a>`;
    const label = /^#\/compare(\/|$)/.test(path) ? 'Back to Compare' : X().backLabel(origin, { brandKey: v.key, brandName: v.display });
    return `<a class="${cls}" href="${esc(origin)}">← ${esc(label)}</a>`;
  }
  // BH: a logo tile only for a brand with a logo (never a plain-name brand); else no tile.
  function logoTile(v, ph) {
    const brand = { key: v.key, display: v.display };
    if (!B().hasLogo(brand)) return '';
    const logo = U().brandLogo(brand);
    return `<div class="${ph ? 'b-logo-tile-m' : 'b-logo-tile'}"><img class="b-logo-tile__img" src="${esc(logo.src)}" alt="${esc(logo.alt)}" loading="lazy" decoding="async" data-brand-logo></div>`;
  }
  function countLine(b, ph) {
    if (!isNum(b.ad_breaks) || !(Number(b.ad_breaks) > 0)) return '';
    const parts = [`${num(b.ad_breaks)} ${b.display} ${Number(b.ad_breaks) === 1 ? 'ad' : 'ads'}`,
      isNum(b.episodes) ? `in ${B().count(b.episodes, 'episode')}` : '', isNum(b.shows) ? `across ${B().count(b.shows, 'show')}` : ''].filter(Boolean).join(' ');
    // "·" stays with the word before it (recheck 4 #3); "last heard {date}" is one unit (R7).
    const heard = !('last_heard' in b) ? '' : b.last_heard ? `last heard ${day(b.last_heard)}` : 'air dates unknown';
    return `<p class="${ph ? 'b-count-m' : 'b-count'}">${esc(parts)}${heard ? `${NB}· <span class="b-nowrap">${esc(heard)}</span>` : ''}</p>`;
  }
  // CV: the chip is one /coverage/ link, counted from the Overview read (Q8: one source).
  function chip(v) {
    const c = v.cov;
    const k = B().checked(c);  // JE-COVERAGE: the Screened counts, else no chip
    if (!k) return '';
    return `<span class="b-checked"><a class="b-checked__link b-coverage" href="/coverage/"><span><b class="b-checked__b">${B().CHECK_MARK}Ads checked</b> in ${esc(B().checkedIn(k))}.</span></a></span>`;
  }
  // D24 "+ Add {brand} to my Overview" (a brand bookmark, today's Track). Q1 (7fb041d1b, fb2db7d1e):
  // added, "✓ Added to my Overview" in the same look, pressed to remove; hover and keyboard focus show
  // "Remove from my Overview" (both words in the markup, CSS shows one); the name stays "Added to my
  // Overview". Another member's: "✓ Tracked on Overview" (UX D42; was "✓ On your Overview"), quiet read-only text, no live region (UX D32; FIX1
  // RB-R1-02, 08). A viewer: no button and no hint (UX D37). A failed read: no button, the note.
  // data-action="track-brand" puts the button on the workspace write-hold list (RB-R1-03); the page's
  // capture handler takes the press, so today's toggle never runs.
  const viewer = () => E().current.workspaceRole === 'viewer';
  // RB-R1-01: D25 only for the member who added the brand (added_by_me): an owner may change
  // every item (can_change) but reads D32 on another member's. A maker who can no longer change
  // it (demoted to viewer) reads D32 too. Without the field: today's can_change rule.
  const addedByMe = item => item.can_change !== false && (!('added_by_me' in item) || item.added_by_me === true);
  function trackHtml(v, ph) {
    const cls = ph ? 'b-track-full-m' : 'b-track b-track-nw';
    if (v.trackedFailed || v.modelOnly) return '';
    const item = v.trackedItem;
    if (item && !addedByMe(item)) return `<span class="b-track-ro"><span aria-hidden="true">✓</span>${NB}Tracked on Overview</span>`;
    if (viewer()) return '';
    const data = `data-b2-track data-action="track-brand" data-brand="${esc(v.key)}" data-name="${esc(v.display)}"`;
    if (item) {
      return `<button type="button" class="${cls} b-track--on" ${data} data-item="${esc(item.id || '')}" data-rev="${esc(String(item.revision ?? ''))}" aria-pressed="true" aria-label="Added to my Overview">`
        + '<span class="b-track__on">✓ Added to my Overview</span><span class="b-track__off" aria-hidden="true">Remove from my Overview</span></button>';
    }
    return `<button type="button" class="${cls}" ${data}>+ Add ${esc(v.display)} to my Overview</button>`;
  }
  const trackFailed = v => (v.trackedFailed && !v.modelOnly ? '<p class="b-track-failed" role="status">We couldn’t check your Overview, so Add is hidden for now. <button type="button" class="b-quiet" data-action="retry">Retry</button></p>' : '');
  function quiet(v, ph) {
    const cls = ph ? 'b-quiet-m' : 'b-quiet';
    const alert = v.noAds ? `Alert me to new ${v.display} ads` : 'Create alert';
    // PODS (UX 2026-10-28): a model_only brand has no Ask and no brand alert (it would never fire).
    const askOn = E().current.askAvailable === true && v.b.ask_button === true && !!v.display && !v.modelOnly;
    const items = [`<a class="${cls}" href="#/compare?brands=${esc(encodeURIComponent(v.key))}">Compare with another brand</a>`,
      ...(v.modelOnly ? [] : [`<button type="button" class="${cls}" data-action="alert-brand" data-id="${esc(v.key)}">${esc(alert)}</button>`]),
      ...(askOn ? [`<a class="${cls}${ph ? ' b-quiet-m--wide' : ''} b-ask" href="#/ask?brand=${esc(encodeURIComponent(v.display))}">What podcasts say about ${esc(v.display)}</a>`] : [])];
    return ph ? `<div class="b-quiet-grid-m">${items.join('')}</div>` : `<div class="b-quiet-row">${items.join('<span class="b-quiet__sep"></span>')}</div>`;
  }
  /* PODS (PODS-STRINGS P1, PODS-REDESIGN-PORT): the heading's name takes "Named by Muse Spark" only when every
     ad of the brand is model-named (model_only; mixed brands are labelled per read), the two one unit. */
  const nameHtml = v => (U().modelOnlyMark(v.b) ? `<span class="ew-brand-unit">${esc(v.display)}${U().modelOnlyMark(v.b)}</span>` : esc(v.display));
  function head(v) {
    const tileHtml = logoTile(v, false);
    return `<section class="b-head${tileHtml ? '' : ' b-head--nologo'}">${tileHtml}<div class="b-head__text"><h1 class="b-h1">${nameHtml(v)}</h1>`
      + (v.noAds ? '' : countLine(v.b, false)) + chip(v) + '</div>'
      + `<div class="b-head__acts">${trackHtml(v, false)}${quiet(v, false)}${viewer() || v.modelOnly ? '' : `<p class="b-hint">${esc(HINT)}</p>`}${trackFailed(v)}</div></section>`;
  }
  function headPhone(v) {
    const tileHtml = logoTile(v, true);
    return `<section class="b-recent"><div class="b-head__row-m${tileHtml ? '' : ' b-head__row-m--nologo'}">${tileHtml}<h1 class="b-h1-m">${nameHtml(v)}</h1></div>`
      + (v.noAds ? '' : countLine(v.b, true))
      + `<div class="b-head__acts-m">${trackHtml(v, true)}${viewer() || v.modelOnly ? '' : `<p class="b-hint-m">${esc(HINT)}</p>`}${quiet(v, true)}${trackFailed(v)}</div></section>`;
  }

  /* -- Top shows -------------------------------------------------------------------------- */
  function tilesHtml(v, ph) {
    const top = (v.b.top_shows || []).slice(0, ph ? 3 : 5);
    if (!top.length) return '';
    const art = new Map((v.shows || []).map(s => [s.id, s.artwork_url]));
    const tiles = top.map(s => {
      const name = s.display_title || 'Unresolved show';
      const url = art.get(s.show_id);
      const t = tile(name);
      const ini = `${ph ? 'b-tile__initials-m' : 'b-tile__initials'} s-initials--t${t.tint}`;
      const pic = sameOrigin(url) ? `<img class="b-tile__art" src="${esc(url)}" alt="" loading="lazy" decoding="async" data-b2-art="${ini}" data-b2-initials="${esc(t.text)}">`
        : `<span class="${ini}" aria-hidden="true">${esc(t.text)}</span>`;
      const n = isNum(s.ad_breaks) ? `<span class="s-meta">${esc(B().count(s.ad_breaks, 'ad'))}</span>` : '';
      return `<button type="button" class="${ph ? 'b-tile-m' : 'b-tile'}" data-b2-go="#/show/${esc(encodeURIComponent(s.show_id))}">${pic}<span class="${ph ? 'b-tile__name-m' : 'b-tile__name'}" title="${esc(name)}">${esc(name)}</span>${n}</button>`;
    }).join('');
    const title = `Shows with the most ${esc(v.display)} ads`;
    if (ph) return `<section class="b-section-m"><h2 class="b-h2-m">${title}</h2><div class="b-tiles-m">${tiles}</div></section>`;
    const more = moreLine(v.b, v.b.shows, top.map(s => s.show_id), v.showCells);
    return `<div class="b-top"><h2 class="b-h2">${title}</h2><div class="b-tiles">${tiles}</div>${more ? `<span class="s-meta--faint b-more">${esc(more)}</span>` : ''}</div>`;
  }

  // Art that fails to load becomes the initials tile (screen r3: no blank tile). CSP-safe,
  // no inline onerror; error does not bubble, so capture (as ui.js does for logos).
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('error', event => {
      const img = event.target;
      if (!img || typeof img.matches !== 'function' || !img.matches('img[data-b2-art]')) return;
      const span = document.createElement('span');
      span.className = img.getAttribute('data-b2-art');
      span.setAttribute('aria-hidden', 'true');
      span.textContent = img.getAttribute('data-b2-initials') || '';
      img.replaceWith(span);
    }, true);
  }

  /* -- Week chart (the Search chart; attributes and classes only) ----------------------------- */
  const G = {
    wide: { w: 560, h: 150, x0: 28, x1: 556, axis: 124, top: 18, bar: 0.58, lbl: 139, tick: 22 },
    phone: { w: 326, h: 132, x0: 24, x1: 322, axis: 110, top: 18, bar: 0.64, lbl: 123, tick: 18 },
  };
  const trim = n => String(Math.round(n * 1000) / 1000);
  function niceStep(max) {
    for (const s of [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000]) if (s * 2 >= max) return s;
    return Math.ceil(max / 2);
  }
  // "Oct 19–21" for a week the data cuts short (RC2 #3), else null.
  function partialDays(cell) {
    const { from, to } = cell.shown || cell.interval || {};
    if (!from || !to || days(from, to) >= 6) return null;
    const a = dayName(from), b = dayName(to);
    return a.split(' ')[0] === b.split(' ')[0] ? `${a}–${b.split(' ')[1]}` : `${a}–${b}`;
  }
  // Recheck 4 (Review): the first and the last week always labelled; middle labels skip
  // (every); a label that would touch the one before it goes (Search's axisPlan).
  function axisPlan(names, slot, every) {
    const last = names.length - 1;
    const on = names.map((_, i) => i === 0 || i === last || i % every === 0);
    const touch = (a, b) => (names[a].length + names[b].length) * 3.5 + 6 > (b - a) * slot;
    const kept = [0];
    for (let i = 1; i <= last; i++) {
      if (!on[i]) continue;
      if (i !== last) { if (touch(kept[kept.length - 1], i)) on[i] = false; else kept.push(i); continue; }
      while (kept.length > 1 && touch(kept[kept.length - 1], i)) on[kept.pop()] = false;
    }
    return on;
  }
  // The weeks the chart draws, in distinct ads (S5): brand_breaks when every cell carries
  // it, else breaks; no unit, no chart. The data's last week ends on the data's last day.
  function chartModel(cells, lastDay) {
    const dated = (cells || []).filter(x => x && x.key !== 'unknown' && x.interval && x.interval.from).map(x => {
      const { from, to } = x.interval;
      return lastDay && to && from <= lastDay && lastDay < to ? { ...x, shown: { from, to: lastDay } } : x;
    });
    const key = dated.length && dated.every(x => isNum(x.brand_breaks)) ? 'brand_breaks' : 'breaks';
    if (!dated.length || !dated.every(x => isNum(x[key]))) return null;
    return { dated, value: x => Number(x[key]) || 0 };
  }
  const weekName = (cell, v) => { const part = partialDays(cell); return `${part ? `${part} (partial week)` : `Week of ${dayName(cell.interval.from)}`}: ${B().count(v, 'ad')}`; };
  // U6: a bar opens Search for the brand in that week (the cell's own week, from/to).
  const weekRoute = (v, cell) => E().searchURL({ mode: 'ads', brands: [v.key], from: cell.interval.from, to: cell.interval.to, page: 1 });
  function chartSvg(v, model, geo, hits) {
    const dated = model.dated;
    const n = dated.length;
    const step = niceStep(Math.max(1, ...dated.map(model.value)));
    const topV = step * 2;
    const span = geo.axis - geo.top;
    const y = val => geo.axis - (val / topV) * span;
    const wk = (geo.x1 - geo.x0) / n;
    const names = dated.map(c => partialDays(c) || dayName(c.interval.from));
    const shown = axisPlan(names, wk, geo === G.phone ? 2 : Math.max(1, Math.ceil(n / 9)));
    let out = `<svg class="s-chart" width="100%" height="${geo.h}" viewBox="0 0 ${geo.w} ${geo.h}" role="group" aria-label="${esc(v.display)} ads heard per week">`
      + `<line class="s-chart__axis" x1="${geo.x0}" x2="${geo.x1}" y1="${geo.axis}" y2="${geo.axis}"></line><text class="s-chart__tick" x="${geo.tick}" y="${geo.axis + 4}">0</text>`
      + [1, 2].map(k => `<line class="s-chart__grid" x1="${geo.x0}" x2="${geo.x1}" y1="${trim(y(step * k))}" y2="${trim(y(step * k))}"></line><text class="s-chart__tick" x="${geo.tick}" y="${trim(y(step * k) + 4)}">${num(step * k)}</text>`).join('')
      + `<line class="s-chart__axis" x1="${geo.x0}" x2="${geo.x0}" y1="${geo.top}" y2="${geo.axis}"></line>`;
    dated.forEach((cell, i) => {
      const val = model.value(cell);
      const cx = geo.x0 + wk * i + wk / 2, bw = wk * geo.bar, h = (val / topV) * span;
      out += `<g class="s-chart__week"><rect class="s-chart__bar" x="${trim(cx - bw / 2)}" y="${trim(geo.axis - h)}" width="${trim(bw)}" height="${trim(h)}" rx="2"></rect>`
        + `<text class="s-chart__val" x="${trim(cx)}" y="${trim(geo.axis - h - 5)}">${num(val)}</text>`
        + `<text class="s-chart__lbl" x="${trim(cx)}" y="${geo.lbl}">${shown[i] ? esc(names[i]) : ''}</text>`
        + (hits ? `<rect class="s-chart__hit" x="${trim(geo.x0 + wk * i)}" y="0" width="${trim(wk)}" height="${geo.h}" tabindex="0" role="button" aria-label="${esc(weekName(cell, val))}" data-search="${esc(weekRoute(v, cell))}"></rect>` : '')
        + '</g>';
    });
    return out + '</svg>';
  }
  // Recheck 3 #6: the last week's end (the data's last day), no range; without it no end.
  function caption(model, lastDay) {
    const last = model.dated[model.dated.length - 1];
    const end = lastDay && last.shown ? last.shown.to : null;
    return `Week starting${end ? `; the last week runs to ${dayName(end).replace(' ', NB)}` : ''}. Counts of ads heard in this collection.`;
  }
  function chartHtml(v, ph) {
    const model = chartModel(v.weekCells, v.lastDay);
    if (!model) return '';
    const title = `<span class="s-chart__title">${esc(v.display)} ads heard, by week</span>`;
    if (!ph) {
      return `<div class="b-chart"><div class="b-chart__head">${title}</div>${chartSvg(v, model, G.wide, true)}`
        + `<div class="b-chart__foot"><span class="b-chart__cap-text">${esc(caption(model, v.lastDay))}</span><span class="b-chart__hint">Click a week to see only its ads.</span></div></div>`;
    }
    const busiest = model.dated.reduce((a, x) => (model.value(x) > model.value(a) ? x : a), model.dated[0]);
    const part = partialDays(busiest);
    const weeks = model.dated.map(cell => `<button type="button" class="b-week-m" aria-label="${esc(weekName(cell, model.value(cell)))}" data-search="${esc(weekRoute(v, cell))}">${esc(partialDays(cell) || dayName(cell.interval.from))}</button>`).join('');
    return `<section class="b-chart-m"><div class="b-chart__head-m">${title}<span>Busiest: ${esc(part || `week of ${dayName(busiest.interval.from)}`)}, ${esc(B().count(model.value(busiest), 'ad'))}</span></div>`
      + chartSvg(v, model, G.phone, false)
      + `<span class="b-chart__cap-m">${esc(caption(model, v.lastDay))}</span><span class="b-chart__hint-m">Tap a week to see only its ads.</span><div class="b-weeks-m">${weeks}</div></section>`;
  }

  /* -- Recent ads ------------------------------------------------------------------------------ */
  // PODS: a model-named read's row says so with P1 alone, no brand name in front (the page is the brand).
  const rowNamed = f => { const n = U().shownBrands(f.read || {}).find(U().modelNamed); return n ? `<span class="b-named">${U().modelMark(n).trim()}</span>` : ''; };
  function rowHtml(item, i) {
    const f = facts(item);
    const quote = quoteHtml(f, 160, 118);
    const time = f.heard == null ? '' : `<div class="b-row__time"><span class="s-row__at">${esc(U().clock(f.heard))}</span><span class="s-meta--faint">into the episode</span></div>`;
    return `<div class="b-row"><div class="b-row__art">${rowCover(f.show, item.artwork_url, false)}</div><div class="s-row__body">`
      + (quote ? `<p class="s-quote">${quote}</p>` : '') + tagsHtml(f, quote) + rowNamed(f)
      + `<div class="b-row__meta"><b class="s-row__name" title="${esc(f.show)}">${esc(f.show)}</b><span class="s-row__epline"><span class="s-row__eptitle" title="${esc(f.ep)}">${esc(f.ep)}</span><span class="s-row__epdate">· ${esc(f.date)}</span></span></div></div>`
      + `<div class="b-row__side">${time}<div class="b-row__actions">${playHtml(f, 'br' + i, false)}<a class="b-open" href="${esc(adRoute(f.ref))}">Open the ad</a></div></div></div>`;
  }
  function phoneRowHtml(item, i) {
    const f = facts(item);
    const quote = quoteHtml(f, 140, 105);
    return `<div class="b-row-m"><div>${rowCover(f.show, item.artwork_url, true)}</div><div class="s-row__body">`
      + (quote ? `<span class="b-row__quote-m">${quote}</span>` : '') + tagsHtml(f, quote) + rowNamed(f)
      + `<span class="b-row__show-m"><b class="b-row__name-m" title="${esc(f.show)}">${esc(f.show)}</b><span class="s-row__epdate">· ${esc(f.date)}</span></span>`
      + (f.heard == null ? '' : `<span class="s-meta"><b class="b-row__num-m">${esc(U().clock(f.heard))}</b> into the episode</span>`)
      + `<div class="b-row__actions-m">${playHtml(f, 'bm' + i, true)}<span class="s-grow"></span><a class="s-btn-m" href="${esc(adRoute(f.ref))}">Open the ad</a></div></div></div>`;
  }
  // The Recent read and "See all": the brand filter alone, newest first (RB-R2-01, RB-R2-02).
  const recentQuery = ads => ({ ...ads, q: '', sort: 'newest', page: 1 });
  const RECENT_PAGES = 3;
  const failedBlock = (what, not) => `<div class="b-failed" role="alert"><p><b>! ${esc(what)} didn’t load.</b> This is not “${esc(not)}”.</p><button type="button" class="s-btn" data-action="retry">Retry</button></div>`;
  function recentHtml(v, ph) {
    const total = isNum(v.b.ad_breaks) ? Number(v.b.ad_breaks) : v.result && isNum(v.result.break_total) ? Number(v.result.break_total) : null;
    const seeAll = `See all ${total === null ? '' : `${num(total)} `}${v.display} ${total === 1 ? 'ad' : 'ads'} in Search`;
    // RB-R2-02: Search's brand filter (Ads only, newest first), the same query as the rows' read; never
    // text words, which Search reads as a filter on what the host said.
    const href = E().searchURL(recentQuery((v.b.drill_down || {}).ads || { mode: 'ads', brands: [v.key] }));
    const clipN = v.result && isNum(v.result.ads_with_clips) ? Number(v.result.ads_with_clips) : 0;
    const clip = `<p class="b-clipline">${esc(B().clipLine(clipN, total || 0))}</p>`;
    const rows = v.rows.slice(0, ph ? 3 : 5);
    // PODS P4: the model-named ads among the rows shown.
    const p4 = U().modelSummary(rows.map(it => U().shownBrands(facts(it).read || {})));
    const body = v.recentFailed ? failedBlock('Recent ads', 'no ads')
      : rows.length ? `${clip}${p4}<div class="s-list">${rows.map(ph ? phoneRowHtml : rowHtml).join('')}</div>`
        : `<p class="b-clipline">The newest ads are in <a class="b-link" href="${esc(href)}">Search</a>.</p>`;
    if (ph) return `<section class="b-section-m"><h2 class="b-h2-m">Recent ${esc(v.display)} ads</h2>${body}<a class="b-seeall-m" href="${esc(href)}">${esc(seeAll)}</a></section>`;
    return `<section class="b-recent"><div class="b-recent__head"><h2 class="b-h2-grow">Recent ${esc(v.display)} ads</h2><a class="s-btn s-btn--xl" href="${esc(href)}">${esc(seeAll)} →</a></div>${body}</section>`;
  }

  /* -- No ads yet (Review call 7: Search's empty state; R4 per width) ------------------------------- */
  function emptyHtml(v, ph) {
    const q = v.display.toLowerCase();
    const c = v.cov;
    const link = ph ? 'b-coverage-link b-coverage-link-m b-coverage' : 'b-coverage-link b-coverage';
    // JE-COVERAGE: "We checked all … and found no ad" only when every episode was checked.
    const k = B().checked(c);
    const checked = k && k.all
      ? `We checked <a class="${link}" href="/coverage/">all ${esc(B().count(c.episodes, 'episode'))} from ${esc(B().count(c.shows, 'show'))}</a>${esc(collectionWords(c))}${collectionWords(c) ? ',' : ''} and found` : 'We found';
    const words = `${checked} no ad that names ${esc(v.display)}. Add it to your Overview${ph ? ` to see new${NB}ads${NB}there.` : ', and its ads will show there as soon as we hear one.'}`;  // UX WORDS D29; "there." never alone
    // Plan A may say words in ads, never words outside ads: no any-mention search there.
    const any = E().excerptOnly() ? '' : E().searchURL({ mode: 'mentions', q, page: 1 });
    if (ph) {
      return `<section class="b-section-m"><h2 class="b-h2-m">No ads found for “${esc(q)}”</h2><p class="b-count-m">${words}</p>`
        + (any ? `<a class="b-anymention-m" href="${esc(any)}">Search any mention of “${esc(q)}”</a>` : '') + '</section>';
    }
    return `<section class="b-empty"><h2 class="b-h2">No ads found for “${esc(q)}”</h2><p class="b-count">${words}</p>`
      + (any ? `<div class="b-empty__actions"><a class="s-btn s-btn--xl" href="${esc(any)}">Search any mention of “${esc(q)}”</a></div>` : '') + '</section>';
  }

  /* -- The page ---------------------------------------------------------------------------------- */
  function pageHtml(v) {
    if (phone()) {
      const rest = v.noAds ? emptyHtml(v, true) : `<div class="b-body-m">${recentHtml(v, true)}${tilesHtml(v, true)}${chartHtml(v, true)}</div>`;
      return `<div class="b-page b-page--phone"><div class="b-main-m">${back(v, true)}<div class="b-body-m">${headPhone(v)}${rest}</div></div></div>`;
    }
    const rest = v.noAds ? emptyHtml(v, false)
      : `<div class="b-results"><section class="b-overview">${tilesHtml(v, false)}${chartHtml(v, false)}</section>${recentHtml(v, false)}</div>`;
    return `<div class="b-page"><div class="b-main">${back(v, false)}<div class="b-body">${head(v)}${rest}</div></div></div>`;
  }
  function loadingHtml(route) {
    const key = (() => { try { return decodeURIComponent(String(route || '').split('?')[0].replace(/^#\/brand\//, '')); } catch { return ''; } })();
    // Q2: "Loading ads…" until the brand's name is known; never the route key.
    const name = NAMES.get(key) || '';
    const words = `<div class="s-muted">${name ? `Loading ${esc(name)}’s ads…` : 'Loading ads…'}</div>`;
    const v = { key, display: name || key };
    const rep = (n, cls) => Array.from({ length: n }, () => `<div class="${cls}"></div>`).join('');
    if (phone()) {
      return `<div class="b-page b-page--phone"><div class="b-main-m">${back(v, true)}<div class="b-recent" role="status">`
        + `<div class="b-head__row-m"><div class="b-skel__lines-m"><div class="b-skel__title-m"></div><div class="b-skel__line-m"></div></div></div>`
        + `<div class="b-section-m">${rep(2, 'b-skel__line-m')}</div>`
        + `<div class="b-section-m"><div class="b-skel__act-m"></div>${rep(2, 'b-skel__line-m')}${rep(2, 'b-skel__act')}</div>`
        + words
        + `<div class="b-section-m"><div class="b-skel__title-m"></div><div class="b-skel__line-m"></div>${rep(3, 'b-skel__row-m')}</div>`
        + `<div class="b-skel__grid-m">${rep(3, 'b-skel__tile-m')}</div></div></div></div>`;
    }
    return `<div class="b-page"><div class="b-main">${back(v, false)}<div class="b-skel" role="status">`
      + `<div class="b-skel__head"><div class="s-stack s-stack--tight"><div class="b-skel__title"></div><div class="b-skel__line"></div></div></div>`
      + `<div class="b-skel__acts">${rep(3, 'b-skel__act')}</div>${words}`
      + `<div class="b-skel__grid">${rep(5, 'b-skel__tile')}<div class="b-skel__chart"></div></div>`
      + `<div class="s-stack s-stack--tight">${rep(3, 'b-skel__row')}</div></div></div></div>`;
  }

  async function render(token, id) {
    const { signal, live } = guarded(token);
    // READ-QUEUE: what the person sees first goes first.
    const detailRead = E().api('/customer/brands/' + encodeURIComponent(id), { signal }).catch(failed);
    const trackedRead = window.EWSaved.trackedBrands(E().api, { signal }).catch(failed);
    const overviewRead = E().api('/customer/overview', { signal }).catch(failed);
    const detail = await detailRead;
    if (!live()) return;
    if (detail && detail.failed) {
      const err = detail.failed;
      if (err.droppedContext) return '';
      return (err.status === 400 || err.status === 404) ? E().notFound({ kind: 'brand' })
        : E().errorBlock('Brand is unavailable: ' + err.message, '<button type="button" class="button" data-action="retry">Retry</button>');
    }
    const b = { ...(detail.brand || {}) };
    const key = b.key || id;
    b.display = b.display || b.brand || id;
    remember({ key, display: b.display });
    const ads = (b.drill_down || {}).ads || null;
    const noAds = isNum(b.ad_breaks) && Number(b.ad_breaks) === 0;
    const reads = !ads || noAds ? [] : [
      // n6: Search's own read (its research-query bundle, ranked as Search ranks it) for "See all"
      // (Search's brand filter), so the rows, their order and their quotes are Search's. RB-R2-01:
      // the brand filter alone; a text q filters on the words said and drops a brand said another way.
      X().apiBundle({ query: recentQuery(ads), selector: null }, [], { signal })
        .then(out => (out && out.result) || {}).catch(failed),
      qs('/customer/summary', { ...ads, group_by: 'week' }, { signal }).catch(() => null),
      phone() ? Promise.resolve(null) : qs('/customer/summary', { ...ads, group_by: 'show' }, { signal }).catch(() => null),
      E().sharedRead('/customer/shows', { signal }).then(r => r.shows || []).catch(() => null),
    ];
    const [tracked, overview, newest, week, byShow, shows] = await Promise.all([trackedRead, overviewRead, ...reads]);
    if (!live()) return;
    for (const r of [tracked, overview, newest]) if (r && r.failed && r.failed.droppedContext) return '';
    if (newest && !newest.failed) E().notePlan(newest, { result: true });
    // VISDATA-AVOID (lead 2026-10-31): Recent rows drop hidden reads before they take N. Search's
    // ranked list (n6) keeps its order, so when hidden rows leave the first page short, the next
    // pages fill the rows (at most RECENT_PAGES pages, never past the brand's last ad).
    let rows = newest && !newest.failed ? namedAds(newest.items, newest.ad_breaks) : [];
    const want = phone() ? 3 : 5;
    for (let page = 2; newest && !newest.failed && rows.length < want && page <= RECENT_PAGES
      && (page - 1) * (Number(newest.per_page) || 10) < (Number(newest.break_total) || 0); page++) {
      const more = await X().apiBundle({ query: { ...recentQuery(ads), page }, selector: null }, [], { signal })
        .then(out => (out && out.result) || null).catch(() => null);
      if (!live()) return;
      if (!more) break;
      const seen = new Set(rows.map(it => it.ref));
      rows = rows.concat(namedAds(more.items, more.ad_breaks).filter(it => !seen.has(it.ref)));
    }
    const ov = overview && !overview.failed ? overview : {};
    const v = {
      b, key, display: b.display, noAds, modelOnly: b.model_only === true,
      cov: ov.coverage || null, newest: ov.newest_episode || null,
      lastDay: ov.newest_episode && realDay(ov.newest_episode.date) ? ov.newest_episode.date : null,
      trackedFailed: !Array.isArray(tracked), trackedItem: Array.isArray(tracked) ? window.EWSaved.trackedBrandFor(tracked, key) : null,
      result: newest && !newest.failed ? newest : null, recentFailed: !!(newest && newest.failed),
      rows,
      weekCells: week && Array.isArray(week.cells) ? week.cells : null,
      showCells: byShow && Array.isArray(byShow.cells) ? byShow.cells : null,
      shows: Array.isArray(shows) ? shows : null,
    };
    queueFit();
    return pageHtml(v);
  }

  /* -- + Add / stop tracking, show tiles, the chart's keyboard -------------------------------------- */
  // UX D36: changed elsewhere says whether the press counted.
  const CHANGED = 'Your Overview changed somewhere else. This page now shows the latest.';
  const NOT_SAVED = 'Your Overview changed somewhere else, so this didn’t save. This page now shows the latest.';
  // RB-R1-05: after the redraw, focus goes to the new button or the read-only text, never the page top.
  async function redraw() {
    await E().render();
    const main = document.querySelector('#main');
    const el = main && main.querySelector ? main.querySelector('[data-b2-track], .b-track-ro') : null;
    if (!el) return;
    if (el.tagName !== 'BUTTON') el.setAttribute('tabindex', '-1');
    el.focus();
  }
  async function track(btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const key = btn.dataset.brand || '', name = btn.dataset.name || key;
    const on = btn.getAttribute('aria-pressed') === 'true';
    btn.setAttribute('aria-busy', 'true');
    btn.disabled = true;
    try {
      if (on) {
        const rev = btn.dataset.rev === undefined || btn.dataset.rev === '' ? undefined : Number(btn.dataset.rev);
        await window.EWSaved.untrackBrand(E().api, { id: btn.dataset.item, revision: rev });
        E().toast(`Removed ${name} from your Overview.`);
      } else {
        const out = await window.EWSaved.trackBrand(E().api, key);
        // The server returned the item another member made: on the Overview, but not by this press.
        E().toast(out && out.item && !addedByMe(out.item) ? CHANGED : `Added ${name} to your Overview.`);
      }
      await redraw();
    } catch (err) {
      if (err && err.droppedContext) return;
      // RB-R1-04: a timeout after the server may have saved it: today's toast and lasting Reload workspace.
      if (X().unconfirmedAction(err)) return;
      if (err && (err.status === 409 || err.status === 412 || err.status === 404)) {
        // Remove of an item already gone counted; anything else did not save.
        E().toast(on && err.status === 404 ? CHANGED : NOT_SAVED);
        await redraw();
        return;
      }
      E().toast(err && err.message ? err.message : 'Your change could not be confirmed.');
    } finally {
      btn.removeAttribute('aria-busy');
      btn.disabled = false;
      // A press that drew nothing new (refused, unconfirmed) keeps the focus on its button.
      if (btn.isConnected && btn.focus) btn.focus();
    }
  }
  const handlers = {
    click(event) {
      const t = event.target;
      if (!t || !t.closest) return false;
      const btn = t.closest('[data-b2-track]');
      if (btn) { if (event.preventDefault) event.preventDefault(); track(btn); return true; }
      const go = t.closest('[data-b2-go]');
      if (go) { if (event.preventDefault) event.preventDefault(); E().go(go.getAttribute('data-b2-go')); return true; }
      return false;
    },
    // U6: Enter and Space on a bar open its week (a click is today's data-search handler).
    keydown(event) {
      const t = event.target;
      if (!t || !t.closest || (event.key !== 'Enter' && event.key !== ' ')) return false;
      const bar = t.closest('.s-chart__hit[data-search]');
      if (!bar) return false;
      if (event.preventDefault) event.preventDefault();
      E().go(bar.getAttribute('data-search'));
      return true;
    },
  };

  /* -- Text cut at a word end (search/tools/textcut.cjs; SPEC rules) ------------------------------- */
  // Every clamped or one-line text is cut in the text, at a word end, with "…", for
  // the width it is shown at; a short last word is dropped; the whole text stays in
  // title; a resize cuts again. A quote keeps its closing mark after the "…".
  function fitText(body, fits) {
    const ends = [0];
    for (let i = 1; i < body.length; i++) if (/\s/.test(body[i]) && !/\s/.test(body[i - 1])) ends.push(i);
    const cut = k => dropShort(body.slice(0, ends[k]).replace(TRIM, '')) + '…';
    let lo = 0, hi = ends.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (fits(cut(mid))) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best < 0 ? null : cut(best);
  }
  // A quote with a marked brand word keeps the mark whole (screen r3, 390: the end cut
  // used to drop it and leave an empty highlight). The end is cut first; when the words
  // before the mark leave no room, they are cut at the front with "…".
  function quoteCut(before, mark, after, fits) {
    if (fits(before, after)) return { before, after };
    const close = /”$/.test(after) ? '”' : '';
    const tail = after.slice(0, after.length - close.length).replace(/…$/, '');
    const endCut = b => {
      const best = fitText(tail, val => fits(b, val + close));
      return best === null ? null : { before: b, after: best + close };
    };
    const kept = endCut(before);
    if (kept) return kept;
    const open = /^“/.test(before) ? '“' : '';
    const body = before.slice(open.length).replace(/^…/, '');
    const starts = [];
    for (let i = 0; i < body.length; i++) if (!/\s/.test(body[i]) && (i === 0 || /\s/.test(body[i - 1]))) starts.push(i);
    starts.push(body.length);
    const front = k => open + '…' + body.slice(starts[k]);
    let lo = 1, hi = starts.length - 1, best = hi;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (fits(front(mid), '…' + close)) { best = mid; hi = mid - 1; } else lo = mid + 1; }
    return endCut(front(best)) || { before: front(best), after: '…' + close };
  }
  const FIT = '.b-page .s-quote, .b-page .b-row__quote-m, .b-page .b-tile__name, .b-page .b-tile__name-m, .b-page .s-row__name, .b-page .s-row__eptitle, .b-page .b-row__name-m';
  const ORIG = new WeakMap();
  function fitOne(el) {
    const nodes = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) nodes.push(t);
    if (!nodes.length) return;
    if (!ORIG.has(el)) ORIG.set(el, nodes.map(t => t.data));
    const orig = ORIG.get(el);
    if (orig.length !== nodes.length) return;
    nodes.forEach((t, i) => { t.data = orig[i]; });
    const over = () => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
    if (!el.clientWidth || !over()) return;
    const mark = el.querySelector('mark');
    const mi = mark && mark.textContent ? nodes.findIndex(t => mark.contains(t)) : -1;
    if (mi > 0 && mi === nodes.length - 2) {
      const put = (b, a) => { nodes.forEach((t, i) => { if (i < mi) t.data = i ? '' : b; }); nodes[mi + 1].data = a; };
      const q = quoteCut(orig.slice(0, mi).join(''), orig[mi], orig[mi + 1], (b, a) => { put(b, a); return !over(); });
      put(q.before, q.after);
      return;
    }
    const full = orig.join('');
    const close = /”$/.test(full) ? '”' : '';
    const set = val => {
      const cut = val.endsWith('…');
      let left = (cut ? val.slice(0, -1) : val).length;
      nodes.forEach((t, i) => { const take = Math.max(0, Math.min(orig[i].length, left)); t.data = orig[i].slice(0, take); left -= take; });
      const end = [...nodes].reverse().find(t => t.data.trim()) || nodes[0];
      end.data = end.data.replace(/\s+$/, '') + (cut ? '…' : '') + close;
    };
    let body = full.slice(0, full.length - close.length);
    if (close) body = body.replace(/…$/, '');
    const best = fitText(body, val => { set(val); return !over(); });
    if (best === null || best === '…') { nodes.forEach((t, i) => { t.data = orig[i]; }); return; }
    set(best);
  }
  function fitAll() {
    if (typeof document === 'undefined' || !document.createTreeWalker || !document.querySelector('.b-page')) return;
    for (const el of document.querySelectorAll(FIT)) fitOne(el);
  }
  let queued = false;
  function queueFit() {
    if (queued || typeof window.requestAnimationFrame !== 'function') return;
    queued = true;
    window.requestAnimationFrame(() => {
      queued = false;
      fitAll();
      // RL1-01: WebKit fires no 'loadingdone'; a face still loading re-fits on the next fonts.ready.
      if (document.fonts && document.fonts.status === 'loading' && document.fonts.ready) document.fonts.ready.then(queueFit);
    });
  }
  if (typeof window.addEventListener === 'function' && typeof window.requestAnimationFrame === 'function') {
    window.addEventListener('resize', queueFit);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
    // RBC-01: a face that loads after fonts.ready re-fits too. Attached once, here at load;
    // the fit changes text only, so it never loads a face and never loops.
    if (document.fonts && typeof document.fonts.addEventListener === 'function') document.fonts.addEventListener('loadingdone', queueFit);
  }

  window.EWB2.page('brand', {
    on: ON,
    render: (token, id) => render(token, id),
    loading: route => loadingHtml(route),
    handlers,
  });
  // For the page's tests (B1 forms, the S8 range words, the word-end cut, the loading name, today).
  // today(iso) sets the date the year words read (tests); today(null) is the clock again.
  const today = iso => { todayIso = iso && realDay(iso) ? iso : null; };
  window.EWBrandB2 = Object.freeze({ moreLine, collectionWords, fitText, quoteCut, cutText, remember, today });
})();
