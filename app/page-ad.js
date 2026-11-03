/* Redesigned Ad page (#/evidence/{ref}). REDESIGN-BUILD; build source
   docs/landing/build-spec/ad/SPEC.md and AD-DECISIONS.md (63b853fbb); plan ws-app/logs/redesign/PLAN.md.
   Owner: the Ad page builder (this file, app/ad-b2.css, tests/frontend_b2_ad.test.cjs).
   Route switch: ON. false keeps today's page; true draws this one.
   render(token, ref, old): today's evidencePage read path (detail read,
   errors, the sparse and passage records); a current ad record draws
   here, every other record (Plan A excerpt, an earlier version or an
   earlier transcript copy, a mention or a removed ad) keeps today's reader.
   Markup and words: the reference pages ref-{1440,390}-*.html. */
(() => {
  'use strict';
  const ON = true;
  const E = () => window.EW;
  const U = () => window.EWUI;
  const B = () => window.EWB2;
  const X = () => window.EWViews._b2;
  const esc = v => E().esc(v);
  const clock = n => U().clock(n);
  const num = v => (v != null && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
  const fold = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const pct = n => `${Number(n.toFixed(2))}%`;

  /* -- D18 and its guard (Review 2026-10-30, ad/SPEC.md "D18 guard", ported
     exactly; lead ADDR-MISS 10-30). The namer stores no web addresses or
     promo codes, so an empty list is not always a fact: the note shows only
     when both lists are [] (null is unknown) AND the ad's shown words hold
     no web-address-like text and no word "code". Under the guard only the
     both-missing form can show. No ad words shown: nothing is claimed. */
  const WEB_LIKE = /\b[a-z0-9-]+ ?\.(?:co\.uk|com|co|net|org|io|ca)\b|\bdot com\b/i;
  const CODE_WORD = /\bcode\b/i;
  const D18 = 'Web address and promo code: none found in our transcript.';
  function d18(said, adLines) {
    if (!adLines.length) return '';
    const words = adLines.map(l => l.text).join(' ');
    const empty = v => Array.isArray(v) && !v.length;
    return empty(said.web_addresses) && empty(said.promo_codes) && !WEB_LIKE.test(words) && !CODE_WORD.test(words) ? D18 : '';
  }
  // Dates drop the year when it is the current year (Review, 2026-10-30).
  function day(v) {
    const d = X().longDate(v);
    const year = String(new Date().getUTCFullYear());
    return d.endsWith(', ' + year) ? d.slice(0, -(year.length + 2)) : d;
  }
  // Spelled-out tokens ("QU-I-N-C-E.com", "B-R-E-T-T") never break at their hyphens (final recheck minor 11).
  const SPELLED = /\b(?:[A-Z]{1,2}-)+[A-Z]{1,2}\b(?:\.[a-z]{2,})?/g;
  const spell = html => html.split(/(<[^>]*>)/).map(part => (part.startsWith('<') ? part : part.replace(SPELLED, m => `<span class="a-spell">${m}</span>`))).join('');

  /* -- The page's model: every value the markup shows, from the record --- */
  function model(detail, requested, notes) {
    const ui = U(), x = X();
    const ep = detail.episode || {}, show = detail.show || {}, ev = detail.evidence || {};
    const read = detail.read;
    const b = read.brand || {};
    const held = ui.underReview(read);
    const selfPromo = !!read.self_promo;
    const name = !held && !selfPromo && b.display ? ui.brandLabel(b) : '';
    const said = (held ? null : read.said) || {};
    const span = x.adSpan(read) || (num(ev.orig_start_s) != null && num(ev.orig_end_s) != null ? [Number(ev.orig_start_s), Number(ev.orig_end_s)] : null);
    // Lines that overlap the read's stored span are "The ad" (views.js
    // windowHtml's rule); the rest of the window is context, before and after (AD-DECISIONS item 9).
    const lines = x.chunkLines((detail.window || {}).segments);
    const inside = l => span && Number.isFinite(l.start) && Number.isFinite(l.end) && l.end > span[0] && l.start < span[1];
    const adLines = lines.filter(inside);
    const before = span ? lines.filter(l => !inside(l) && Number.isFinite(l.start) && l.start < span[0]) : [];
    const after = span ? lines.filter(l => !inside(l) && !before.includes(l)) : [];
    // Where the brand is first heard: the server's heard_at_s (S1), else the
    // first ad line that says it (brandHeardLine's rule); none: the ad start (UX D19).
    const variant = name ? x.heardAs(read, lines) : null;
    const line = name ? x.brandHeardLine(adLines, said, variant) : null;
    // RSV-R1-06 (App): never before the ad span start, as the server clamps heard_at_s; a line
    // that overlaps the span start reads the span start.
    const heardAt = !name ? null : num(detail.heard_at_s) != null ? Number(detail.heard_at_s)
      : line && Number.isFinite(line.start) ? (span ? Math.max(line.start, span[0]) : line.start) : null;
    const at = heardAt != null ? heardAt : span ? span[0] : null;
    // The clip: Play runs from the lead-in start to the clip end (AD-DECISIONS item 2).
    const media = detail.media || {};
    const local = span ? x.localMedia(media, x.mediaRef(detail, requested), span[0]) : null;
    let clip = null;
    if (local) {
      const playStart = local.offset + (local.at != null ? local.at : Math.max(0, span[0] - local.offset));
      const end = num(media.clip_end_s) != null ? Number(media.clip_end_s) : span[1];
      const length = B().playLength(media, span) ?? (end > playStart ? Math.round(end - playStart) : null);
      if (length) clip = { local, start: playStart, end, length };
    }
    const duration = x.episodeSeconds(ep);
    const length = duration > 0 && span ? Math.max(duration, span[1]) : duration;
    const showName = ep.show_title || show.display_title || '';
    const showId = show.show_identity_id && !show.unresolved ? show.show_identity_id : '';
    // PODS (PODS-STRINGS, PODS-REDESIGN-PORT): a read with two model names names both, no main brand
    // (EWUI.shownNames); `modelOnly`: every name is model-named, so no brand alert is offered (it would never
    // fire) and campaign screens say "Brand not identified" (FUNC-1 decision 2).
    const names = name ? ui.shownNames(read) : '';
    const modelOnly = !!name && !ui.offerBrand(read) && ui.modelNamed(b);
    const campaignName = modelOnly ? '' : name;
    const h1 = selfPromo ? (showName ? `Ad on ${showName}` : 'Ad') : !name ? 'Brand not identified' : showName ? `${names} ad on ${showName}` : `${names} ad`;
    const adding = [held || (!campaignName && !selfPromo) ? 'Ad, brand not identified' : campaignName ? `${campaignName} ad` : 'Ad', showName,
      at != null ? `${clock(at)} into the episode` : ''].filter(Boolean).join(' · ');
    return { detail, requested, read, b, held, selfPromo, name, names, modelOnly, campaignName, said, span, lines, adLines, before, after, variant, heardAt, at, brandLine: line,
      clip, duration, length, ep, show, showName, showId, h1, adding, note: (notes || [])[0] || null,
      episodeId: ev.episode_id || ep.episode_id || '' };
  }

  /* -- Head ---------------------------------------------------------------- */
  function tile(m, phone) {
    const art = m.detail.artwork_url;
    if (typeof art === 'string' && art.startsWith('/') && !art.startsWith('//')) {
      return `<img class="${phone ? 'a-cover--phone' : 'a-cover'}" src="${esc(art)}" alt="" loading="lazy" decoding="async">`;
    }
    // EWB2.cover's initials tile (its tint and letters) in the a-tile size.
    const cover = B().cover({ title: m.showName });
    const t = (/s-initials--t(\d)/.exec(cover) || [])[1] || '0';
    const letters = (/>([^<]*)<\/div>$/.exec(cover) || [])[1] || '';
    return `<div class="${phone ? 'a-tile--phone' : 'a-tile'} s-initials--t${t}" aria-hidden="true">${letters}</div>`;
  }
  function tag(m, phone) {
    if (m.selfPromo) return '<span class="a-chip--neutral"><span aria-hidden="true">↺</span> Show’s own promotion</span>';
    // The logo when it reads as the full name, else the plain name (Review 10-30 logos rule).
    // The shipped client map, as the Overview (screen r1 m1; ad/SPEC.md "Brand logo", AD-DECISIONS item 10).
    // PODS: each shown name, a model-named one with "Named by Muse Spark" after it (one unit, EWUI.brandsHtml).
    const one = n => (B().hasLogo(n) ? B().brandMark(n, { cls: phone ? 's-logo--13' : 's-logo--16' }) : `<span class="s-strong">${esc(U().brandLabel(n))}</span>`);
    // Each " · " rides with the next name's unit, so a wrapped tag line never ends on a stray "·" (review LOW, D41).
    const mark = !m.name ? '' : U().brandsHtml(m.read, one).split(' · <span class="ew-brand-unit">').join(' <span class="ew-brand-unit">· ');
    return `<span class="${phone ? 's-adtag' : 'a-adtag--lg'}">AD</span>${mark}`;
  }
  /* PODS P2/P3: who named a model-named brand and from what (its own channel; an unknown channel gets none),
     under the title, plain text for screen readers. With two names, one tip each, led by its name; "Report a
     mistake" in the last is the one button, which opens the page's own report control (Facts and sources). */
  const REPORT_BTN = `<button type="button" class="a-named__report" data-b2-report>${esc(U().REPORT_LABEL)}</button>`;
  function tips(m) {
    if (!m.name) return '';
    const several = U().shownBrands(m.read).length > 1;
    const tipped = U().shownBrands(m.read).filter(n => U().modelTip(n));
    return tipped.map((n, i) => `<p class="a-named">${several ? `<b>${esc(n.display)}</b>: ` : ''}${esc(U().modelTip(n))} If it’s wrong, use `
      + `${i === tipped.length - 1 ? REPORT_BTN : esc(U().REPORT_LABEL)}.</p>`).join('');
  }
  function epLine(m, phone) {
    const title = m.ep.ep_title || '';
    if (!title && !(m.length > 0)) return '';
    const len = m.length > 0 ? `<span class="a-head__ep-len">${esc(clock(m.length))} long</span>` : '';
    return `<div class="${phone ? 'a-head__ep--phone' : 'a-head__ep'}" title="${esc(title)}">`
      // UX recheck 2 #1: the last word and the "·" stay together (no-break spaces);
      // recheck 3 #2: a spaced dash stays with the word before it ("14.4 - The" -> "14.4&nbsp;- The").
      + (title ? `<span class="a-head__ep-title">Episode: ${esc(keepLast(title).replace(/ ([-–—])(?= )/g, '\u00a0$1'))}${len ? '\u00a0·' : ''}</span>` : '') + len + '</div>';
  }
  function head(m, phone) {
    const date = day(m.ep.pub_date);
    const when = !date ? '' : phone ? `<span class="a-head__date--phone">${esc(date)}</span>` : `<span class="s-meta">Episode published ${esc(date)}</span>`;
    return `<${phone ? 'div class="a-head--phone"' : 'section class="a-head"'}>${tile(m, phone)}`
      + `<div class="${phone ? 'a-head__text--phone' : 'a-head__text'}"><div class="${phone ? 'a-head__chips--phone' : 'a-head__chips'}">${tag(m, phone)}${when}</div>`
      + `<h1 class="${phone ? 'a-h1--phone' : 'a-h1'}" title="${esc(m.h1)}">${esc(m.h1)}</h1>${epLine(m, phone)}${tips(m)}</div>`
      + `</${phone ? 'div' : 'section'}>`;
  }

  /* -- Player (only with a local clip) ------------------------------------- */
  // EV row 73 (QA G2-27): a tick on the seek bar where the brand is first
  // heard, when that is inside what Play plays; SVG attributes (CSP-safe).
  // Screen r1 m3 (Review, ad/SPEC.md "Player"): an image in the page's words; with no brand to name, at the
  // ad start (UX D19). No focus, no clicks (pointer-events: none in ad-b2.css).
  function seekMark(c, m) {
    const at = m.at;
    if (at == null || at < c.start || at > c.end) return '';
    const x = Math.min(996, ((at - c.start) / (c.end - c.start)) * 1000);
    const label = m.heardAt != null ? `${m.name} first heard, ${clock(at)} into the episode` : `The ad starts ${clock(at)} into the episode.`;
    return `<svg class="a-seek__mark" viewBox="0 0 1000 10" preserveAspectRatio="none" role="img" aria-label="${esc(label)}" focusable="false">`
      + `<rect class="a-seek__brand" x="${Number(x.toFixed(1))}" y="0" width="4" height="10"></rect></svg>`;
  }
  const skip = (delta, words, label) => `<button type="button" class="s-btn" data-action="clip-seek" data-clip="ev" data-delta="${delta}" aria-label="${label}">${words}</button>`;
  function player(m, phone) {
    const c = m.clip;
    if (!c) return '';
    const box = ` aria-label="Clip" data-clip-id="ev" data-start="${esc(c.start)}" data-end="${esc(c.end)}" data-offset="${esc(c.local.offset)}"`
      + (c.local.at != null ? ` data-lead="${esc(c.local.at)}"` : '');
    const playBtn = `<button type="button" class="${phone ? 'a-play--phone' : 'a-play'}" data-action="clip-toggle" data-clip="ev" data-b2-play aria-pressed="false">`
      + `<span class="${phone ? 'a-play__disc--phone' : 'a-play__disc'}"><span class="${phone ? 'a-play__tri--phone' : 'a-play__tri'}"></span></span><span>Play ${esc(clock(c.length))}</span></button>`;
    const time = esc(clock(m.at));
    const who = m.name ? U().brandsHtml(m.read, n => esc(U().brandLabel(n))) : '';
    const heard = m.heardAt != null
      ? (phone ? `<span class="s-card__at">${time}</span><span class="s-meta">into the episode, ${who} is first heard</span>`
        : `<div class="a-player__heard-row"><span class="s-panel__time">${time}</span><span class="a-player__heard-text">into the episode, ${who} is first heard</span></div>`)
      : (phone ? `<span class="s-meta">The ad starts</span><span class="s-card__at">${time}</span><span class="s-meta">into the episode.</span>`
        : `<div class="a-player__heard-row"><span class="a-player__heard-text">The ad starts</span><span class="s-panel__time">${time}</span><span class="a-player__heard-text">into the episode.</span></div>`);
    const range = `<input class="a-player__range" type="range" min="0" max="${c.length}" step="1" value="0" data-clip-seek="ev" aria-valuemin="0" aria-valuemax="${c.length}" aria-valuenow="0"`
      + ` aria-valuetext="${esc(X().seekText(0, c.length))}" aria-label="Position in clip, ${esc(clock(c.start))} to ${esc(clock(c.end))}">`;
    const mark = seekMark(c, m);
    const seek = mark ? `<span class="a-seek">${range}${mark}</span>` : range;
    const ends = [`<span class="a-time" data-clip-now>${esc(clock(c.start))}</span>`, `<span class="a-time">${esc(clock(c.end))}</span>`];
    // EV row 95: the error line and the status line the clip engine fills.
    const states = '<p class="ew-clip-error a-clip-error" role="alert" hidden><b>! The clip didn’t load.</b> <span>The words below are still the record.</span> '
      + '<button type="button" class="s-btn" data-action="clip-retry" data-clip="ev">Retry</button></p>'
      + '<p class="ew-clip-status a-clip-status" role="status" aria-live="polite" data-clip-status></p>';
    const audio = X().clipAudio('ev', c.local.src, c.local.at);
    if (phone) {
      return `<section class="a-player--phone"${box}><div class="a-player__row--phone">${playBtn}<div class="a-player__heard--phone">${heard}</div></div>`
        + `<div class="a-player__bar--phone">${ends[0]}${seek}${ends[1]}</div>`
        + `<div class="a-skip--phone">${skip(-5, '−5s', 'Back 5 seconds')}${skip(5, '+5s', 'Forward 5 seconds')}</div>${states}${audio}</section>`;
    }
    return `<section class="a-player"${box}><div class="a-player__row">${playBtn}`
      + `<div class="a-player__heard">${heard}<div class="s-meta">The clip starts a few seconds before the ad.</div></div>`
      + '<label class="a-speed">Speed <select class="a-speed__sel" data-clip-speed="ev"><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option></select></label></div>'
      + `<div class="a-player__bar">${skip(-5, '−5s', 'Back 5 seconds')}${ends[0]}${seek}${ends[1]}${skip(5, '+5s', 'Forward 5 seconds')}</div>${states}${audio}</section>`;
  }

  /* -- What the host said --------------------------------------------------- */
  // Marks as windowHtml() does today (markedText: the search words, the
  // recorded codes and addresses); the design's marks are the look only.
  // UX recheck 2 #4: a line keeps its last two words together, so none ends with one word alone.
  const keepLast = t => String(t).replace(/\s+(\S+)\s*$/, '\u00a0$1');
  const marked = (m, text) => spell(X().markedText(keepLast(text), m.said).replace(/<mark>/g, '<mark class="s-mark">'));
  /* Screen r1 m2 (AD-DECISIONS item 9: windowHtml's brand-first-heard line, views.js lineHtml): that line takes
     the "Brand name first heard" tag and its first mention of the brand is marked, however the page was
     opened. A word the search words already mark stays as it is. */
  const BRAND_TAG = '<span class="a-line__tag">Brand name first heard</span>';
  function markHeard(m, html) {
    const names = [m.name, m.variant].filter(Boolean).map(n => esc(n).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!names.length) return html;
    const re = new RegExp(`(^|[^\\p{L}\\p{N}])(${names.join('|')})(?![\\p{L}\\p{N}])`, 'iu');
    let inMark = false, done = false;
    return html.split(/(<[^>]*>)/).map(part => {
      if (part.startsWith('<')) { if (/^<mark\b/.test(part)) inMark = true; else if (part === '</mark>') inMark = false; return part; }
      if (done || inMark) return part;
      return part.replace(re, (_, pre, w) => { done = true; return `${pre}<mark class="s-mark">${w}</mark>`; });
    }).join('');
  }
  const lineWords = (m, l) => (l === m.brandLine ? BRAND_TAG + markHeard(m, marked(m, l.text)) : marked(m, l.text));
  const timed = l => (Number.isFinite(l.start) && Number.isFinite(l.end) ? ` data-start="${esc(l.start)}" data-end="${esc(l.end)}"` : '');
  const ctxLine = (m, l) => `<div class="a-line--ctx"><span class="a-line__t--ctx">${Number.isFinite(l.start) ? esc(clock(l.start)) : ''}</span><p class="a-line__p--ctx">${marked(m, l.text)}</p></div>`;
  const adLine = (m, l, phone) => `<li class="${phone ? 'a-line--phone' : 'a-line'}" data-segment="${esc(l.id ?? '')}"${timed(l)}>`
    + `<span class="${phone ? 'a-line__t--phone' : 'a-line__t'}">${Number.isFinite(l.start) ? esc(clock(l.start)) : ''}</span><p class="${phone ? 'a-line__p--phone' : 'a-line__p'}">${lineWords(m, l)}</p></li>`;
  function words(m, phone) {
    const none = m.clip ? '' : `<div class="${phone ? 'a-noclip--phone' : 'a-noclip'}"><span class="a-noclip__title">No audio clip for this ad</span>`
      + '<span class="s-meta">The words below are from our transcript.</span></div>';
    const headHtml = phone
      ? `<div class="a-words__head--phone"><h2 class="a-h2--phone" id="ew-said-h">What the host said</h2>${none}</div>`
      : '<div class="a-words__head"><h2 class="a-h2" id="ew-said-h">What the host said</h2><span class="s-grow"></span></div>' + none;
    const list = `<ol class="a-lines"${m.clip ? ' data-clip-lines="ev"' : ''}>${m.adLines.map(l => adLine(m, l, phone)).join('')}</ol>`;
    const ad = `<div class="${phone ? 'a-adblock--phone' : 'a-adblock'}"><div class="${phone ? 'a-adblock__head--phone' : 'a-adblock__head'}"><span class="a-eyebrow">The ad</span></div>${list}</div>`;
    const ctx = phone ? '' : `<div class="a-words__ctx">${m.before.map(l => ctxLine(m, l)).join('')}</div>`;
    const afterHtml = phone || !m.after.length ? '' : `<div class="a-words__after">${m.after.map(l => ctxLine(m, l)).join('')}</div>`;
    // An earlier copy never draws here; the full transcript opens at the ad.
    const open = m.episodeId && m.span ? `<a class="${phone ? 'a-btn--phone' : 's-btn'}" href="#/episode/${esc(encodeURIComponent(m.episodeId))}?at=${esc(m.span[0])}">Open the full transcript</a>` : '';
    const foot = phone
      ? `<div class="a-words__foot--phone"><span class="s-meta">Words from our transcript; they can be wrong.</span>${open}</div>`
      : `<div class="a-words__foot"><span class="a-words__note">Words from our transcript; they can be wrong.</span>${open}</div>`;
    return `<section class="a-card--flush a-words" aria-labelledby="ew-said-h">${headHtml}${ctx}${ad}${afterHtml}${foot}</section>`;
  }
  // Where the ad sat (1440 only): the mark in % of the episode, never under 6 px.
  function strip(m) {
    if (!m.span || !(m.length > 0)) return '';
    const x = (m.span[0] / m.length) * 100, w = ((m.span[1] - m.span[0]) / m.length) * 100;
    return '<section class="a-strip"><div class="a-strip__head"><span class="a-strip__title">Where the ad sat in the episode</span></div>'
      + `<div class="a-strip__track"><svg class="a-strip__mark" aria-hidden="true" focusable="false"><rect x="${pct(x)}" y="0" width="${pct(w)}" height="100%" rx="3"></rect>`
      + `<svg x="${pct(x)}" width="${pct(w)}" overflow="visible"><rect x="50%" y="0" width="6" height="100%" rx="3" transform="translate(-3 0)"></rect></svg></svg></div>`
      + `<div class="a-strip__ends"><span>0:00</span><span>${esc(clock(m.length))}</span></div></section>`;
  }

  /* -- Actions and More ------------------------------------------------------ */
  function actionData(m) {
    const d = m.detail, ep = m.ep, ref = m.requested;
    const campaign = `data-ref="${esc(ref)}" data-episode="${esc(m.episodeId)}" data-show="${esc(m.show.show_identity_id || '')}" data-day="${esc(ep.pub_date || '')}"`
      + ` data-show-name="${esc(m.showName)}" data-episode-name="${esc(ep.ep_title || '')}"`
      + (m.campaignName ? ` data-brand-name="${esc(m.campaignName)}"` : '') + (m.at != null ? ` data-at="${esc(clock(m.at))}"` : '')
      + ` data-adding="${esc(m.adding)}"`;
    const alert = `data-id="${esc(ref)}"` + (m.name && m.b.key ? ` data-brand="${esc(m.b.key)}" data-brand-name="${esc(m.name)}"` : '')
      + (m.showId ? ` data-show="${esc(m.showId)}" data-show-name="${esc(m.showName)}"` : '');
    const tv = X().pinnedTv(d);
    return { campaign, alert, bookmark: `data-episode="${esc(m.episodeId)}" data-ref="${tv ? esc(ref) : ''}" data-tv="${esc(tv)}"` };
  }
  function moreItems(m, phone) {
    const a = actionData(m);
    const cls = phone ? 'a-more__item--phone' : 'a-more__item';
    const item = (act, words, attrs) => `<button type="button" class="${cls}" data-action="${act}" ${attrs}>${esc(words)}</button>`;
    return item('save-excerpt', 'Save this excerpt', `data-ref="${esc(m.requested)}"`)
      + item('report-mistake', m.note ? 'Edit a note' : 'Add a note', `data-id="${esc(m.requested)}"`)
      + (m.episodeId ? item('save-bookmark-episode', 'Bookmark the episode', a.bookmark) : '')
      + (m.modelOnly ? '' : item('alert-evidence', m.name ? `Create an alert for ${m.name}` : 'Create an alert for this show', a.alert));
  }
  const D3 = 'Your campaigns hold the ad buys you’re checking: did each booked read run, with the right code?';
  function actions(m) {
    const a = actionData(m);
    return '<section class="a-actions">'
      + `<button type="button" class="a-btn-primary" data-action="copy-read-link" data-ref="${esc(m.requested)}">Copy link</button>`
      + `<button type="button" class="a-btn-add" data-action="add-to-campaign" ${a.campaign}>Add to my campaign</button>`
      + `<p class="a-actions__note">${esc(D3)}</p>`
      + '<div class="a-more"><button type="button" class="a-more__toggle" data-b2-more aria-expanded="false" aria-controls="a-more-list"><span>' + (m.modelOnly ? 'More: save, add a note' : 'More: save, add a note, create alert') + '</span><span class="a-more__glyph" aria-hidden="true">▾</span></button>'
      + `<div class="a-more__list" id="a-more-list" hidden>${moreItems(m, false)}</div></div></section>`;
  }
  function addCard(m) {
    return `<section class="a-card--phone"><button type="button" class="a-btn-add--phone" data-action="add-to-campaign" ${actionData(m).campaign}>Add to my campaign</button>`
      + `<p class="a-actions__note--phone">${esc(D3)}</p></section>`;
  }
  function actionBar(m) {
    return `<div class="a-actionbar"><div class="a-more--phone" id="a-more-list" hidden>${moreItems(m, true)}</div><div class="a-actionbar__row">`
      + `<button type="button" class="a-btn-primary--phone" data-action="copy-read-link" data-ref="${esc(m.requested)}">Copy link</button>`
      + '<button type="button" class="a-btn-more--phone" data-b2-more aria-expanded="false" aria-controls="a-more-list">More</button></div></div>';
  }
  const noteLine = m => (m.note ? `<p class="s-meta a-yournote">Your note: “${esc(m.note.reason || 'No additional reason supplied.')}” Notes are yours; they do not report a mistake to EarWitness.</p>` : '');

  /* -- Facts and sources ------------------------------------------------------ */
  const SOURCE = 'From our transcript, which can be wrong.';
  const text = v => `<span class="a-fact__value--text">${esc(v)}</span>`;
  const code = v => `<span class="a-chip--code"><b class="a-chip__code">${esc(v)}</b></span>`;
  function factRows(m) {
    const rows = [];
    const said = m.said;
    if (m.selfPromo) {
      rows.push(['Kind of ad', '<span class="a-chip--neutral">Self-promotion</span>', 'The show promoting itself or its network. Not counted for any brand.']);
    } else if (!m.held) {
      // The spoken brand, the address and the code: each its own row from its own field.
      if (said.spoken_brand) {
        const heardName = fold(said.spoken_brand) === fold(m.b.display) || fold(said.spoken_brand) === fold(m.b.key) ? m.name || said.spoken_brand : said.spoken_brand;
        rows.push(['Brand heard in the audio', text(m.heardAt != null ? `${heardName} · named at ${clock(m.heardAt)}` : heardName),
          SOURCE + (m.variant && fold(m.variant) !== fold(heardName) ? ` The transcript also spells it “${m.variant}”.` : '')]);
      }
      const addresses = Array.isArray(said.web_addresses) ? said.web_addresses : [];
      const codes = Array.isArray(said.promo_codes) ? said.promo_codes : [];
      if (addresses.length) rows.push(['Web address read out', addresses.map(code).join(''), SOURCE]);
      if (addresses.length && said.address_brand) {
        // The address owner is compared only with the spoken brand.
        const compared = !!said.spoken_brand;
        const same = compared && fold(said.spoken_brand) === fold(said.address_brand);
        rows.push(['Brand the address points to',
          same ? text(`${said.address_brand} · matches what was heard`) : text(said.address_brand) + (compared ? '<span class="a-chip--differs">≠ Differs from what was heard</span>' : ''),
          !compared ? 'Not compared: no spoken brand was recorded to compare it with.' : !same ? 'We show both facts and don’t pick one.' : '']);
      }
      if (codes.length) rows.push(['Promo code read out', codes.map(code).join(''), SOURCE + (said.code_brand ? ` The code names ${said.code_brand}.` : '')]);
      const note = d18(said, m.adLines);
      if (note) rows.push({ note });
    }
    // With a clip the player shows the times; without one, When (turn 7).
    if (!m.clip && m.span) rows.push(['When', text(`${clock(m.span[0])} → ${clock(m.span[1])} into the episode`), '']);
    const kind = X().READ_KIND_WORDS[m.read.kind] || '';
    if (kind) rows.push(['How the time was found', text(kind), 'Times from the transcript']);
    rows.push(['Audio', text(m.clip ? `Clip, ${clock(m.clip.length)} long` : 'No audio clip for this ad'), '']);
    if (m.showName) rows.push(['Show', text(m.showName), '']);
    const title = m.ep.ep_title || '';
    const date = day(m.ep.pub_date);
    if (title || m.length > 0) {
      rows.push(['Episode', `<span class="a-fact__value--plain">${esc([title, m.length > 0 ? `${clock(m.length)} long` : ''].filter(Boolean).join(' · '))}</span>`,
        date ? `Published ${date}` : '']);
    }
    return rows;
  }
  function factsHtml(m, phone) {
    const held = m.held ? `<p class="${phone ? 'a-facts__note--phone' : 'a-facts__note'}">We heard an ad here but couldn’t name the brand.</p>` : '';
    const rows = factRows(m).map(r => {
      if (r.note) return `<p class="${phone ? 'a-facts__note--phone' : 'a-facts__note'}">${esc(r.note)}</p>`;
      const [label, value, note] = r;
      const n = note ? `<span class="a-fact__note">${esc(note)}</span>` : '';
      return phone ? `<div class="a-fact--phone"><span class="s-meta">${esc(label)}</span>${value}${n}</div>`
        : `<div class="a-fact"><span class="a-fact__label">${esc(label)}</span><div class="a-fact__body">${value}${n}</div></div>`;
    }).join('');
    return `<section class="a-card--flush a-facts ew-ledger" aria-labelledby="a-facts-h"><h2 class="${phone ? 'a-facts__h2--phone' : 'a-facts__h2'}" id="a-facts-h">Facts and sources</h2>`
      + held + rows + `<div class="${phone ? 'a-facts__foot--phone' : 'a-facts__foot'}">${U().reportControl(m.requested)}</div></section>`;
  }
  const SHOWS = ['These words were said at this time in this episode.', 'It doesn’t show how many people heard the ad, what was paid for it, or that it ran as booked.'];
  const shows = phone => (phone
    ? `<section class="a-shows--phone"><h3 class="a-shows__h3">What this shows</h3><p class="a-shows__p">${esc(SHOWS.join(' '))}</p></section>`
    : `<section class="a-shows"><h3 class="a-shows__h3">What this shows</h3>${SHOWS.map(s => `<p class="a-shows__p">${esc(s)}</p>`).join('')}</section>`);

  /* -- The page ------------------------------------------------------------- */
  // Today's backLink() words in the a-back look, naming the origin (Landing Q7):
  // Compare is named too; a self-promotion never names the searched brand
  // (final recheck minor 9) and the phone drops the query.
  function back(here, { phone = false, selfPromo = false } = {}) {
    const c = E().current;
    const origin = String((c.back && c.back.get(c.route)) || '').split('?')[0];
    let html = X().backLink(here).replace('class="ew-back"', 'class="a-back"');
    if (/^#\/compare(\/|$)/.test(origin)) html = html.replace(/←[^<]*/, '← Back to Compare');
    if (phone || selfPromo) html = html.replace(/(← Back to Search): “[^<]*”/, '$1');
    return html;
  }
  const root = (phone, inner) => `<div class="a-page${phone ? ' a-page--phone' : ''}" data-reader="evidence">${inner}</div>`;
  function draw(m, pod) {
    const phone = B().layout() === 'phone';
    const x = X();
    const here = { brandKey: m.b.key, brandName: m.name, showId: m.show.show_identity_id, showName: m.showName, episodeId: m.episodeId, episodeName: m.ep.ep_title };
    const notices = x.requestedPin(m.requested, m.detail).note + x.savedExcerptNotice() + x.savedNoteNotice();
    if (phone) {
      return root(true, `<div class="a-main--phone">${back(here, { phone: true, selfPromo: m.selfPromo })}${notices}<div class="s-stack s-stack--tight">${head(m, true)}${player(m, true)}${words(m, true)}${pod}`
        + `${addCard(m)}${noteLine(m)}${factsHtml(m, true)}${shows(true)}</div></div>${actionBar(m)}`);
    }
    return root(false, `<div class="a-main">${back(here, { selfPromo: m.selfPromo })}${notices}<div class="a-ready">${head(m, false)}<div class="s-results">`
      + `<div class="a-col">${player(m, false)}${words(m, false)}${pod}${strip(m)}</div>`
      + `<aside class="a-side">${actions(m)}${noteLine(m)}${factsHtml(m, false)}${shows(false)}</aside></div></div></div>`);
  }

  // The records this page draws: a current ad on the transcript shown.
  function drawsHere(detail) {
    const x = X();
    const read = detail.read;
    return detail.record_kind === 'evidence' && !detail.historical && !x.excerptView(detail)
      && read && !read.historical && detail.status !== 'mention' && detail.status !== 'rejected'
      && (detail.episode || {}).transcript_is_current !== false;
  }

  // "Other evidence for this ad" (AD-DECISIONS item 11): today's section, below the words.
  async function podFor(detail, requested, signal) {
    const x = X();
    const ab = detail.ad_break, ev = detail.evidence || {};
    const episodeId = ev.episode_id || (detail.episode || {}).episode_id;
    const n = Number(ab && ab.read_count);
    if (!ab || !episodeId || ev.transcript_version_id === null || detail.read.self_promo || !(n > 1)
      || num(ab.index) == null || ab.index < 0 || ab.index >= n) return '';
    const epRead = await E().api('/customer/episodes/' + encodeURIComponent(episodeId), { signal }).then(r => (r && r.episode) || null, () => null);
    if (!epRead) return '';
    const list = x.breakReads(ab, epRead.ads || []);
    if (list.length !== n || !list.some(a => a.evidence_id === ev.id || a.ref === requested)) return '';
    return x.podSection({ group: x.breakGroup(list, ab), index: Number(ab.index) }, ev.id, n);
  }

  async function render(token, ref, old) {
    const x = X();
    const hand = takeHandoff();
    const { signal, live } = x.guardedFetch(token);
    const prefsRead = E().preferences();
    let out;
    try { out = await E().api('/customer/records/' + encodeURIComponent(ref), { signal }); }
    catch (err) {
      if (!live() || (err && err.droppedContext)) return '';
      if (err.status === 400 || err.status === 404) return E().notFound({ kind: 'ad record' });
      return x.readerError('This evidence didn’t load', 'We couldn’t open this passage',
        err.status >= 500 && err.status !== 503 ? 'The service had an error. Nothing has changed. Try again in a moment.'
          : `The service didn’t answer: ${err.message}. Nothing has changed.`, x.backLink());
    }
    await prefsRead;
    if (!live()) return '';
    const detail = out.record || {};
    x.notePlan(detail);
    const c = E().current;
    if (!c.savedExcerpt || c.savedExcerpt.ref !== ref) c.savedExcerpt = null;
    if (!c.savedNote || c.savedNote.ref !== ref) c.savedNote = null;
    if (!detail.canonical_source_ref && !detail.window && !x.excerptView(detail)) {
      // Sparse identifier-only projection: no pins or text leak (today's words).
      return `<div class="ew-reader">${x.backLink()}${E().pageHead('Source record', '')}`
        + x.techDetails(`<p class="small muted">Requested pin <span class="mono">${esc(ref)}</span> · record ${esc(detail.record_id || '')}.</p>`)
        + E().empty('Source text unavailable', 'This source is not disclosed to the current view. The saved record and its identity are retained; no text is substituted.', `<a class="button" href="${esc(E().back())}">Back</a>`)
        + '</div>';
    }
    if (detail.record_kind !== 'evidence') return x.passageArticle(detail, ref, live, signal);
    if (!drawsHere(detail)) return x.evidenceArticle(detail, ref, live, signal);
    const [notes, pod] = await Promise.all([
      E().api('/customer/records/' + encodeURIComponent(ref) + '/mistake').then(r => r.mistakes || r.notes || [], () => []),
      podFor(detail, ref, signal),
    ]);
    if (!live()) return '';
    // RA-R2-02: the hand-off waits for this draw's player (published after this returns), however slow the reads.
    if (hand) setTimeout(() => startHandoff(hand, ref, live), 0);
    return draw(model(detail, ref, notes), pod);
  }

  /* -- Loading: the head's skeleton, the status words, skeleton cards ------ */
  function loading() {
    const phone = B().layout() === 'phone';
    const words = '<div class="s-muted">Loading this ad’s words and audio…</div>';
    if (phone) {
      return root(true, `<div class="a-main--phone">${back({}, { phone: true })}<div class="s-stack s-stack--tight" role="status" aria-busy="true">`
        + '<div class="a-skel__head--phone"><div class="a-skel__tile--phone"></div><div class="s-skel-m__lines"><div class="a-skel__eyebrow--phone"></div><div class="a-skel__h1--phone"></div></div></div>'
        + words + '<div class="a-skel__words--phone"><div class="s-skel__text"></div><div class="s-skel__text"></div><div class="s-skel__text s-skel__text--75"></div><div class="s-skel__text"></div></div></div></div>');
    }
    return root(false, `<div class="a-main">${back({})}<div class="a-loading" role="status" aria-busy="true">`
      + '<div class="a-head"><div class="a-skel__tile"></div><div class="s-stack s-stack--tight"><div class="a-skel__eyebrow"></div><div class="a-skel__h1"></div><div class="a-skel__sub"></div></div></div>'
      + words + '<div class="s-results"><div class="a-skel__words"><div class="a-skel__title"></div><div class="a-skel__line"></div><div class="a-skel__line"></div>'
      + '<div class="a-skel__line--80"></div><div class="a-skel__line"></div><div class="a-skel__line--60"></div></div>'
      + '<div class="s-skel__panel"><div class="a-skel__btn"></div><div class="a-skel__btn--2"></div><div class="s-skel-m__l1"></div></div></div></div></div>');
  }

  /* -- Add to my campaign: today's dialog (campaignAttachDialog: its model,
        its checks and its submit), in AD-DECISIONS item 4's words and the
        a-dialog look. Pure: today's dialog markup in, the page's out. -------- */
  const KEEP_NOTE = 'Adding an ad keeps it with the campaign. It doesn’t mark it as checked.';
  // UX item 6 (REDESIGN-AD-WORDS round 2, the words until "Start a campaign" carries the ad; as Search):
  // FR-11's greyed rows (another member's campaign: disabled, no value).
  const THEIRS_RE = /<label><input type="radio" name="choice" disabled(="")?><span>([\s\S]*?)<\/span><\/label>/g;
  const OWN_RE = /(<label><input type="radio" name="choice" value=[^>]*>)<span>([\s\S]*?)<\/span><\/label>/g;
  const WHY_LOOK = { title: 'a-h2', none: 'a-dialog__none', meta: 's-meta--faint', foot: 'a-dialog__foot', cancel: 'a-btn-cancel', second: 'a-btn-cancel', cta: 's-cta' };
  // UX D35: item 6's heading is the dialog's own (EWB2.addWhy.WORDS.theirsTitle); its line stays above the rows.
  const THEIRS_HEAD = '<div class="a-dialog__none" data-theirs>'
    + '<span class="s-meta--faint">Only the member who made a campaign can add to it. Start a campaign of your own, then add this ad to it.</span></div>';
  function dialogWords(html, { adding = '', phone = false, research = [], cfa = false } = {}) {
    let s = String(html);
    const bodyAt = s.indexOf('<div class="dialog-body">');
    const footAt = s.indexOf('<div class="dialog-footer">');
    const formEnd = s.lastIndexOf('</form>');
    // No form ("The campaigns didn’t load"): only the title takes the button's words.
    if (bodyAt < 0 || formEnd < 0) return s.replace('<h2 id="dialog-title">Add to campaign</h2>', '<h2 id="dialog-title">Add to my campaign</h2>');
    const bodyEnd = (footAt > 0 ? footAt : formEnd) - '</div>'.length;
    let body = s.slice(bodyAt + '<div class="dialog-body">'.length, bodyEnd);
    let foot = footAt > 0 ? s.slice(footAt, formEnd) : '';
    const submit = /<button class="button primary" type="submit">[^<]*<\/button>/.test(foot);
    const theirsOnly = !submit && /<label><input type="radio" name="choice" disabled(="")?><span>/.test(s) && !/name="choice" value=/.test(s);
    // UX D35: item 6 has its own heading; otherwise "Add to my campaign" (the research case's is EWB2.addWhy's).
    const title = theirsOnly ? B().addWhy.WORDS.theirsTitle : B().addWhy.WORDS.noneTitle;
    const top = s.slice(0, bodyAt)
      // A browser writes the boolean back as novalidate="" when innerHTML is read.
      .replace(/<form id="attach-to-campaign" novalidate(="")?>/, (_, v) => `<form id="attach-to-campaign" novalidate${v || ''} class="${phone ? 'a-dialog--phone' : 'a-dialog'}">`)
      .replace(/<div class="dialog-head"><h2 id="dialog-title">[^<]*<\/h2>[\s\S]*<\/div>$/,
        `<h2 class="a-h2" id="dialog-title">${esc(title)}</h2>`
        + (adding ? `<div class="a-dialog__adding"><span class="a-dialog__adding-label">Adding:</span> <b class="s-strong">${esc(adding)}</b></div>` : ''));
    const why = submit ? [] : unlisted(s, research);
    /* Screen r1 m4 (UX final, EWB2.addWhy shared with Search): no campaign of mine covers the episode. "This ad
       can’t be added yet" is the only heading, then the Adding line, two lines, no rows; the exit is primary and
       "Start a campaign with this ad" the second button for a caller who may (never a viewer). */
    if (why.length) {
      const formTag = (/^[\s\S]*?<form id="attach-to-campaign"[^>]*>/.exec(top) || [''])[0];
      const addingHtml = adding ? `<div class="a-dialog__adding"><span class="a-dialog__adding-label">Adding:</span> <b class="s-strong">${esc(adding)}</b></div>` : '';
      return formTag + B().addWhy.html(why, WHY_LOOK, { adding: addingHtml, cfa }) + s.slice(formEnd);
    }
    body = body
      // Rule 5: a row is the name (no "· Episode: {title}", no target words), its status, and the other-member note.
      .replace(OWN_RE, (_, open, t) => `${open}<span>${B().addWhy.row(t)}</span></label>`)
      .replace(THEIRS_RE, (_, d, t) => `<label><input type="radio" name="choice" disabled${d || ''}><span>${B().addWhy.row(t, { theirs: true })}</span></label>`)
      .replace(/<fieldset class="radio-group"><legend>[^<]*<\/legend>/g, '<fieldset class="a-dialog__choices" role="radiogroup" aria-label="Where to keep it"><legend class="a-fact__value">Where to keep it</legend>')
      .replace(/<label><input type="radio" name="choice"([^>]*)>/g, (_, attrs) => `<label class="a-camp${/\schecked\b/.test(attrs) ? ' a-camp--on' : ''}"><input class="a-camp__radio" type="radio" name="choice"${attrs}>`)
      .replace(/<\/fieldset>/g, '<a class="a-camp__new" href="#/campaigns">Start a campaign</a></fieldset>')
      .replace(/<p class="section-note">No (?:campaign placement|research target) covers this episode\.<\/p><p><a class="text-link" href="#\/campaigns">Go to Campaigns<\/a><\/p>/,
        `<div class="a-dialog__none"><span class="a-dialog__none-title">None of your campaigns follows this episode yet.</span><span class="s-meta--faint" data-d3>${esc(D3)}</span></div>`)
      // "Go to Campaigns" is never drawn (spec 407513adc+): the exit is "Start a campaign".
      .replace(/<p><a class="text-link" href="#\/campaigns">Go to Campaigns<\/a><\/p>/g, '')
      .replace(/<p class="section-note">A source reference only points to a passage;[^<]*<\/p>/, `<span class="s-meta--faint">${esc(KEEP_NOTE)}</span>`)
      .replace(/<p class="section-note">/g, '<p class="s-meta">');
    // Nothing to choose: one exit and no note (Review, 2026-10-30). Listed rows with the member's own research
    // campaigns missing the episode: the research words above the rows (UX round 2); only other members' rows:
    // UX item 6's words above them; else the D3 sentence under the list and "Start a campaign".
    if (!submit) {
      body = body.replace(`<span class="s-meta--faint">${esc(KEEP_NOTE)}</span>`, '');
      const head = /<fieldset class="a-dialog__choices"/.test(body) && theirsOnly ? THEIRS_HEAD : '';
      body = head
        ? body.replace('<a class="a-camp__new" href="#/campaigns">Start a campaign</a></fieldset>', '</fieldset>').replace('<fieldset class="a-dialog__choices"', head + '<fieldset class="a-dialog__choices"')
        : body.replace('<a class="a-camp__new" href="#/campaigns">Start a campaign</a></fieldset>', `</fieldset><span class="s-meta--faint" data-d3>${esc(D3)}</span>`);
    }
    foot = '<div class="a-dialog__foot"><button class="a-btn-cancel" type="button" data-action="close-dialog">Cancel</button>'
      + (submit ? '<button class="s-cta" type="submit">Add to my campaign</button>' : theirsOnly ? '<a class="s-cta" href="#/campaigns">Start your own campaign</a>' : '<a class="s-cta" href="#/campaigns">Start a campaign</a>') + '</div>';
    const out = top + body + foot + s.slice(formEnd);
    // Part B (lead 10-30): for a caller who may make campaigns, every "Start a campaign" exit and item 6's
    // "Start your own campaign" become "Start a campaign with this ad" (EWB2.cfa), and item 6's line carries the ad.
    return cfa ? out
      .replace('<a class="s-cta" href="#/campaigns">Start a campaign</a>', CFA_BTN)
      .replace('<a class="s-cta" href="#/campaigns">Start your own campaign</a>', CFA_BTN)
      .replace('<a class="a-camp__new" href="#/campaigns">Start a campaign</a>', `<button class="a-camp__new" type="button" data-b2-cfa>${esc(B().cfa.WORDS.start)}</button>`)
      .replace('Start a campaign of your own, then add this ad to it.', esc(B().cfa.WORDS.theirsLine).replace(/^Only the member who made a campaign can add to it\. /, '')) : out;
  }
  const CFA_BTN = '<button class="s-cta" type="button" data-b2-cfa>Start a campaign with this ad</button>';
  /* A member's open research campaign takes an ad only through a research target that covers the episode
     (today's model, AD-DECISIONS item 4); with nothing to choose the dialog says why (EWB2.addWhy, above). */
  // RA-R1-04 (shared with Search, EWB2.addWhy.list): a listed own row (open, already attached, refused) is never the
  // research case; else the campaigns the dialog does not list (FR-11: a listed name covers the episode).
  const unlisted = (html, list) => B().addWhy.list(html, list);
  let research = [], names = [];
  // Read with the press (hosted only: research is on there, as campaignAttachDialog's researchOn).
  function readResearch() {
    research = []; names = [];
    if (!E().current.hosted) return;
    // The member's own campaigns (can_change); a viewer changes none, so a viewer's are every open one (the viewer line).
    const viewer = E().current.workspaceRole === 'viewer';
    const open = c => c && c.mode === 'research' && c.availability === 'active' && (viewer || c.can_change !== false);
    const asked = adding;
    Promise.resolve().then(() => E().api('/customer/campaigns')).then(r => {
      if (adding === null || adding !== asked) return;
      research = ((r && r.campaigns) || []).filter(open).map(c => ({ id: c.id, name: c.name || `Campaign ${c.id}`, viewer: c.can_change === false }));
      names = ((r && r.campaigns) || []).map(c => c && c.name).filter(Boolean);
      paintWhy();
    }, () => { /* the dialog says what failed */ });
  }
  // The read landed after the restyle: the dialog is drawn again from today's markup with the read in, so
  // both paint orders give the same dialog (UX round 2).
  let rawDialog = null, rawPhone = false;
  function paintWhy() {
    const dlg = document.getElementById && document.getElementById('modal');
    if (!dlg || adding === null || rawDialog === null || !research.length || /data-why/.test(dlg.innerHTML)) return;
    const next = dialogWords(rawDialog, { adding, phone: rawPhone, research, cfa: B().cfa.may(E().current) });
    if (!/data-why/.test(next)) return;
    dlg.innerHTML = next;
    if (observer && observer.takeRecords) observer.takeRecords();
    const exit = dlg.querySelector && dlg.querySelector('.a-dialog__foot .s-cta');
    if (exit && exit.focus) { try { exit.focus(); } catch { /* keep focus */ } }
  }
  // UX task test r2 (as Search): today's save disables the submit and writes "Saving…" (views.js saveIn);
  // while the add runs it reads "Adding…". Today's words come back with the answer.
  function busyStep(btn) {
    if (btn && btn.disabled && btn.textContent === 'Saving…') btn.textContent = 'Adding…';
  }
  function watchBusy(form) {
    const btn = form.querySelector && form.querySelector('button[type="submit"]');
    if (!btn || typeof MutationObserver !== 'function' || (btn.dataset && btn.dataset.b2Busy)) return;
    if (btn.dataset) btn.dataset.b2Busy = '1';
    new MutationObserver(() => busyStep(btn)).observe(btn, { childList: true, characterData: true, subtree: true, attributes: true });
  }
  /* Part B: "Start a campaign with this ad" in the same dialog (EWB2.cfa, shared with Search). The ad is the
     Add button's own data; the dialog keeps the steps done, so "Try again" resumes. */
  let addAd = null, made = null;
  const LOOK = phone => ({ form: phone ? 'a-dialog--phone' : 'a-dialog', title: 'a-h2', meta: 's-meta--faint', field: 'a-dialog__field', label: 'a-fact__value',
    input: 'a-dialog__input', foot: 'a-dialog__foot', cancel: 'a-btn-cancel', cta: 's-cta', error: 'a-dialog__error', link: 'a-camp__new' });
  function paintMade(focusSel) {
    const dlg = document.getElementById && document.getElementById('modal');
    if (!dlg || adding === null || !made) return;
    dlg.innerHTML = B().cfa.html(addAd, made, LOOK(rawPhone));
    if (observer && observer.takeRecords) observer.takeRecords();
    const el = focusSel && dlg.querySelector && dlg.querySelector(focusSel);
    if (el && el.focus) { try { el.focus(); } catch { /* keep focus */ } }
  }
  function startMade() {
    if (!addAd) return;
    made = B().cfa.state(addAd, names);
    paintMade('input[name="name"]');
  }
  // RA-R1-02: today's dialog guard before each write (views.js submitIn's staleDialogForm, which this submit
  // skips): the workspace or displayed epoch moved, or the session changed (authority epoch) since the dialog opened.
  // The form is the one the dialog holds now: each paint draws a new one, so the submitted node is detached.
  const stillCurrent = () => {
    const dlg = document.getElementById && document.getElementById('modal');
    const form = dlg && dlg.querySelector && dlg.querySelector('form#campaign-from-ad');
    const ep = E().current.dialogEpoch;
    return !form || X().staleDialogForm(form) || (ep && ep.authority !== E().authorityEpoch()) ? X().DIALOG_STALE : null;
  };
  async function submitMade(form) {
    if (!made || made.busy || made.held) return;
    const box = form.querySelector && form.querySelector('input[name="name"]');
    if (made.cid == null) made.name = String((box && box.value) || '').trim();
    made.error = made.name ? '' : 'Name the campaign.';
    if (made.error) return paintMade('input[name="name"]');
    const st = made;
    st.busy = true;
    paintMade();
    try { await B().cfa.run((p, o) => E().api(p, o), addAd, st, { guard: stillCurrent }); } finally { st.busy = false; }
    if (made !== st) return;
    // Refused before anything was saved: the dialog closes with the words (as a stale dialog's button action).
    if (st.refused) { E().closeDialog(); E().toast(st.refused); return; }
    paintMade(st.done ? '[role="status"]' : st.held ? '[role="alert"]' : '[type="submit"]');
  }
  // The open dialog takes the words once today's opener has drawn it.
  let adding = null;
  function restyleDialog() {
    const dlg = document.getElementById && document.getElementById('modal');
    const form = dlg && dlg.querySelector && dlg.querySelector('form#attach-to-campaign');
    if (adding === null) return;
    if (!form) {
      // "The campaigns didn’t load": the title only, in place (no rewrite, no observer record).
      const title = dlg && dlg.querySelector && dlg.querySelector('#dialog-title');
      if (title && title.textContent === 'Add to campaign') title.textContent = 'Add to my campaign';
      return;
    }
    if (/\ba-dialog/.test(form.className || '')) return;
    const phone = B().layout() === 'phone';
    rawDialog = dlg.innerHTML; rawPhone = phone;
    dlg.innerHTML = dialogWords(rawDialog, { adding, phone, research, cfa: B().cfa.may(E().current) });
    // This write is not a new dialog: drop its own records, so a miss can never loop.
    if (observer && observer.takeRecords) observer.takeRecords();
    dlg.classList.add('a-modal');
    dlg.classList.toggle('a-modal--phone', phone);
    if (dlg.addEventListener) dlg.addEventListener('close', () => { dlg.classList.remove('a-modal', 'a-modal--phone'); adding = null; rawDialog = null; made = null; }, { once: true });
    const first = dlg.querySelector('input[name="choice"]:checked') || dlg.querySelector('input[name="choice"]:not([disabled])') || dlg.querySelector('button, a[href]');
    if (first && first.focus) { try { first.focus(); } catch { /* keep focus */ } }
  }
  let observer = null;
  function watchDialog() {
    if (observer || typeof MutationObserver !== 'function') return;
    const dlg = document.getElementById && document.getElementById('modal');
    if (!dlg) return;
    observer = new MutationObserver(() => restyleDialog());
    observer.observe(dlg, { childList: true });
    if (dlg.addEventListener) dlg.addEventListener('focusin', keepClear);
  }
  // RA-R2-01: a row that takes focus (arrow keys, Tab) and still ends under the sticky foot after the browser's own
  // scroll (WebKit stops short) is scrolled clear of it, with 8 px to spare. The foot's own buttons never scroll it.
  function keepClear(event) {
    const el = event && event.target;
    const form = el && el.closest && el.closest('#modal.a-modal > form');
    const foot = form && form.querySelector && form.querySelector('.a-dialog__foot');
    if (!foot || foot.contains(el)) return;
    const row = el.closest('label') || el;
    const fix = () => {
      const over = row.getBoundingClientRect().bottom - foot.getBoundingClientRect().top;
      if (over > 0) form.scrollTop += Math.ceil(over) + 8;
    };
    fix();
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fix);
  }

  /* -- Player end state (Landing 2026-10-30, ad/SPEC.md "Player end state"; NOGAP row 28) --
     After the clip ends (the engine pauses it at the clip end, or the file ends first): the
     same Play button reads "Play again", the bar sits at the clip end in episode time, and
     −5s / +5s and Speed stay. A press plays from the clip start. A play, or a seek off the
     end, leaves the state. The button's name is its words, as every b2 Play button (views.js
     clipPresent never labels one). Runs after the engine's own listener (script order). */
  const PLAY_BTN = '.a-play[data-b2-play], .a-play--phone[data-b2-play]';
  const bound = (box, k) => Number(box.dataset[k]);
  const atEnd = (box, audio) => audio.ended || bound(box, 'offset') + (audio.currentTime || 0) >= bound(box, 'end') - 0.05;
  function paintEnd(box, on) {
    const btn = box.querySelector(PLAY_BTN);
    const idle = B().playInner(btn, false);
    btn.innerHTML = on ? idle.replace(/Play \d+(?::\d\d)+(?=<\/span>$)/, 'Play again') : idle;
    if (!on) { delete box.dataset.clipEnded; return; }
    box.dataset.clipEnded = '';
    const range = box.querySelector('[data-clip-seek]');
    if (range) {
      const max = Number(range.max) || 0;
      range.value = String(max);
      if (range.style && range.style.setProperty) range.style.setProperty('--ew-seek', '100%');
      range.setAttribute('aria-valuenow', String(max));
      range.setAttribute('aria-valuetext', X().seekText(max, max));
    }
    const now = box.querySelector('[data-clip-now]');
    if (now) now.textContent = clock(bound(box, 'end'));
  }
  function followEnd(event) {
    const audio = event.target;
    const box = audio && audio.tagName === 'AUDIO' && audio.closest ? audio.closest('[data-clip-id]') : null;
    if (!box || !box.querySelector || !box.querySelector(PLAY_BTN)) return;
    if ((event.type === 'pause' || event.type === 'ended') && !audio.error && atEnd(box, audio)) paintEnd(box, true);
    // Screen r1 M1: WebKit ends the file short of the clip end and then sends one more timeupdate, which the
    // engine paints from currentTime ("23:01", 64 of 66). While the end state holds, the bar keeps the clip end.
    else if ((event.type === 'timeupdate' || event.type === 'seeked') && 'clipEnded' in box.dataset && audio.paused) paintEnd(box, true);
    else if ('clipEnded' in box.dataset && (event.type === 'play' || (event.type === 'seeking' && !atEnd(box, audio)))) paintEnd(box, false);
  }
  if (typeof document !== 'undefined' && document.addEventListener) for (const type of ['pause', 'ended', 'play', 'seeking', 'timeupdate', 'seeked']) document.addEventListener(type, followEnd, true);
  // The press on "Play again": from the clip start (the lead-in), never back to the end.
  function playAgain(btn) {
    const box = btn.closest('[data-clip-id]');
    const audio = box && 'clipEnded' in box.dataset && document.getElementById('clip-' + box.dataset.clipId);
    if (!audio) return;
    audio.currentTime = Math.max(0, bound(box, 'start') - bound(box, 'offset'));
    if (audio.dataset) delete audio.dataset.pausedAt;
  }

  /* -- Walkthrough item 3 (SEARCH-7 7d, Search 74d54cee): a Search clip that was playing when the member opened
     this ad leaves the point it reached (EW.current.clipHandoff = { ref, at: seconds into the episode, t }). Taken
     once, when the page starts drawing, if under 3 s old; once drawn, the page's own player seeks there as the
     engine's clipSeekSet does (media not loaded yet: data-pending-seek, applied on load) and starts through the
     engine's Play, so one player sounds. A point outside the clip, another ad's, or an old one starts nothing. */
  function takeHandoff() {
    const c = E().current, h = c && c.clipHandoff;
    if (c && h !== undefined) c.clipHandoff = null;
    return h && Number.isFinite(h.at) && handoffFresh(h) ? h : null;
  }
  // RA-R2-02: fresh is under 3 s from the Search press (h.t), whenever this page gets to draw its player.
  const handoffFresh = h => Date.now() - Number(h.t) < 3000;
  // Kept until this render's player exists (its page is published), while the route is the same and the
  // hand-off fresh; a newer render or route (live() false) or 3 s from the press drops it.
  function startHandoff(h, ref, live) {
    if (!live() || !handoffFresh(h)) return;
    const audio = document.getElementById && document.getElementById('clip-ev');
    const box = audio && audio.closest ? audio.closest('[data-clip-id]') : null;
    const btn = box && box.querySelector ? box.querySelector(PLAY_BTN) : null;
    if (!btn) { setTimeout(() => startHandoff(h, ref, live), 25); return; }
    const m = /\/customer\/records\/([^/?#]+)\/media/.exec(String(audio.getAttribute('src') || ''));
    if (h.ref !== ref && !(m && decodeURIComponent(m[1]) === h.ref)) return;
    if (h.at < bound(box, 'offset') || h.at >= bound(box, 'end')) return;
    const t = Math.round((h.at - bound(box, 'offset')) * 10) / 10;
    audio.currentTime = t;
    delete audio.dataset.pausedAt;
    if (!audio.readyState) audio.dataset.pendingSeek = String(t);
    btn.click();
  }

  /* -- Events on this page (EWB2.handle, capture; true stops today's) ------- */
  function setMore(btn, open) {
    const list = document.getElementById(btn.getAttribute('aria-controls'));
    if (!list) return;
    btn.setAttribute('aria-expanded', String(open));
    list.hidden = !open;
  }
  // PODS: the tip's Report a mistake opens this ad's own report control and moves focus to it.
  function openReport() {
    const summary = document.querySelector && document.querySelector('.a-facts .report-problem > summary');
    if (!summary) return;
    summary.parentElement.open = true;
    const still = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    if (summary.scrollIntoView) summary.scrollIntoView({ block: 'center', behavior: still ? 'auto' : 'smooth' });
    if (summary.focus) summary.focus({ preventScroll: true });
  }
  const handlers = {
    click(event) {
      const t = event.target;
      if (!t || !t.closest) return false;
      const again = t.closest(PLAY_BTN);
      if (again) playAgain(again);
      const more = t.closest('[data-b2-more]');
      if (more) { setMore(more, more.getAttribute('aria-expanded') !== 'true'); return true; }
      if (t.closest('[data-b2-report]')) { openReport(); return true; }
      // A More item closes the list; today's handler runs the action.
      const item = t.closest('.a-more__item, .a-more__item--phone');
      const toggle = item && document.querySelector && document.querySelector('[data-b2-more][aria-expanded="true"]');
      if (toggle) setMore(toggle, false);
      const add = t.closest('[data-action="add-to-campaign"][data-adding]');
      if (add) {
        adding = add.getAttribute('data-adding') || '';
        const at = k => add.getAttribute(k) || '';
        addAd = { ref: at('data-ref'), episode: at('data-episode'), episodeName: at('data-episode-name'), showName: at('data-show-name'), brand: at('data-brand-name'), at: at('data-at') };
        made = null;
        readResearch(); watchDialog();
      }
      if (t.closest('[data-b2-cfa]')) { startMade(); return true; }
      return false;
    },
    // The Add dialog's submit shows its busy words (today's handler still sends it).
    submit(event) {
      const f = event.target;
      if (f && f.getAttribute && f.getAttribute('id') === 'attach-to-campaign' && f.closest && f.closest('#modal')) watchBusy(f);
      if (f && f.getAttribute && f.getAttribute('id') === 'campaign-from-ad') { if (event.preventDefault) event.preventDefault(); submitMade(f); return true; }
      return false;
    },
    keydown(event) {
      if (event.key !== 'Escape' || !document.querySelector) return false;
      const toggle = document.querySelector('[data-b2-more][aria-expanded="true"]');
      if (!toggle) return false;
      setMore(toggle, false);
      if (toggle.focus) toggle.focus();
      return true;
    },
  };

  /* -- Cut at a word end (Landing 4c6e1c3ac, search/tools/textcut.cjs) -----
     Every clamped or one-line text on the page (the h1, the episode line) is cut
     in the text at a word end, with "…", for the width the page lays out; the CSS
     clamp is only a safety net. The same rule as Search's (page-search.js). */
  // aaecbd611 (recheck 5): no lone "|" or "/" before the "…" either.
  const TRIM = /[\s,;:.…—–|\/-]+$/;
  // c9ea045ca (recheck 4): a short function word before the cut goes too ("…DMT and the…" reads "…DMT…").
  const FN = /\s+(?:the|a|an|and|or|of|with|to|in|on|for|at|by|from|as|&)$/i;
  // Pure: the longest cut of body at a word end that fits; null when even "…" does not.
  function fitText(body, fits) {
    const ends = [0];
    for (let i = 1; i < body.length; i++) if (/\s/.test(body[i]) && !/\s/.test(body[i - 1])) ends.push(i);
    // A cut one word after the source's own "…" ends at that "…" (no "…Download…").
    const at = k => {
      let kept = body.slice(0, ends[k]).replace(TRIM, '');
      for (let g = 0; g < 3 && FN.test(kept); g++) { const t = kept.replace(FN, '').replace(TRIM, ''); if (!t) break; kept = t; }
      const src = k > 0 && /…\s*\S+$/.exec(kept);
      return src ? kept.slice(0, src.index + 1) : kept + '…';
    };
    let lo = 0, hi = ends.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (fits(at(mid))) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best < 0 ? null : at(best);
  }
  // Write a cut back into the element's own text nodes in order; "…" on the last kept node.
  function spreadText(nodes, orig, v) {
    const full = orig.join('');
    const cut = v.endsWith('…') && full.startsWith(v.slice(0, -1));
    let left = (cut ? v.slice(0, -1) : v).length;
    nodes.forEach((t, i) => { const take = Math.max(0, Math.min(orig[i].length, left)); t.data = orig[i].slice(0, take); left -= take; });
    const end = [...nodes].reverse().find(t => t.data.trim()) || nodes[0];
    if (end && cut) end.data = end.data.replace(/\s+$/, '') + '…';
  }
  const CLIPS = 'p, span, div, b, a, h1, h2, h3, button, strong';
  const isClipped = el => {
    const cs = getComputedStyle(el);
    return /hidden|clip/.test(cs.overflow + cs.overflowX) && ((cs.webkitLineClamp && cs.webkitLineClamp !== 'none') || cs.textOverflow === 'ellipsis');
  };
  // The text as drawn, per element, so a resize cuts again from the whole text.
  const ORIG = new WeakMap();
  function restoreText(el) {
    const saved = ORIG.get(el);
    if (saved) { for (const [t, d] of saved) if (el.contains(t)) t.data = d; return; }
    const keep = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) keep.push([t, t.data]);
    ORIG.set(el, keep);
  }
  function fitPlain(el) {
    const over = () => el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
    if (!el.clientWidth || !over()) return;
    const nodes = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) nodes.push(t);
    if (!nodes.length) return;
    const orig = nodes.map(t => t.data);
    const full = orig.join('');
    const best = fitText(full, v => { spreadText(nodes, orig, v); return !over(); });
    if (best === null || best === '…') { nodes.forEach((t, i) => { t.data = orig[i]; }); return; }
    spreadText(nodes, orig, best);
    if (!el.closest('[title]')) el.setAttribute('title', full.replace(/\s+/g, ' ').trim());
  }
  // On every paint of the page, on resize and once the fonts load (text edits are
  // characterData, so the observer below is not woken by its own cuts).
  let fitQueued = false;
  function queueFit() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => {
      fitQueued = false;
      const els = [...document.querySelectorAll('.a-page :is(' + CLIPS + ')')].filter(isClipped);
      for (const el of els) restoreText(el);
      for (const el of els.reverse()) fitPlain(el);
    });
  }
  (function watchFit() {
    if (typeof MutationObserver !== 'function' || typeof requestAnimationFrame !== 'function' || typeof document === 'undefined' || !document.createTreeWalker) return;
    const root = document.getElementById('main') || document.body;
    if (!root) return;
    new MutationObserver(queueFit).observe(root, { childList: true, subtree: true });
    window.addEventListener('resize', queueFit);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
  })();

  window.EWB2Ad = Object.freeze({ dialog: dialogWords, d18, back, fitText, busyStep, keepClear });
  window.EWB2.page('ad', { on: ON, render, loading, handlers });
})();
