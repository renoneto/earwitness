/* EarWitness shared customer rendering grammar (EWUI).
   Cohesive source-card, directory-card and operation-state rendering.
   No data store, no API calls, no boot on load: pure HTML builders and
   one inline-state helper over the caller's element. */
(() => {
  'use strict';

  function ew() { return window.EW; }

  function initials(name) {
    return String(name || '?').replace(/[^A-Za-z ]/g, '').split(' ')
      .filter(Boolean).slice(0, 2).map(x => x[0]).join('') || '?';
  }

  /* Separate server record_kind from status: the kind names what the row
     is (Evidence record / Transcript passage / Episode). Customers never
     see a review state (PLAIN-ADS): every ad status reads "Ad". */
  function kindLabel(kind) {
    if (kind === 'transcript_passage') return 'Transcript passage';
    if (kind === 'episode') return 'Episode';
    return 'Evidence record';
  }

  function statusLabel(status) {
    return status === 'mention' ? 'Mention' : 'Ad';
  }

  /* The three audio states a member sees (decision 7). The remote detail
     is a technical detail (audioDetail), never the visible state. */
  const AUDIO_CLIP = 'Clip available';
  const AUDIO_NONE = 'No clip (transcript only)';
  const AUDIO_CHANGED = 'Audio changed since transcription';
  // A rotated feed (audio_trust, as views.js isRotated) changed too (review F2).
  const rotated = item => item.audio_trust === 'rotated';
  function audioState(item) {
    if (item.audio === 'mismatch' || rotated(item)) return AUDIO_CHANGED;
    if (item.audio_local) return AUDIO_CLIP;
    return AUDIO_NONE;
  }
  // What the API knows beyond the member state, for Technical details.
  function audioDetail(item) {
    if (item.audio === 'mismatch') return 'Source audio mismatch';
    if (rotated(item)) return 'Rotated audio feed: prerolls rotated since transcription';
    if (item.audio_local) return '';
    if (item.audio === 'available') return 'Remote audio only, no local playback';
    return 'Source audio unavailable';
  }

  /* What the load knows about one read (validation plan A6): the brand
     in its display spelling, the time range (the read's display span,
     else the record's quotation span), and a needs-review badge while the
     brand is the namer's unchecked guess. A confirmed brand clears it. */
  function needsReview(read) {
    return !!(read && read.needs_review && !(read.brand || {}).confirmed);
  }
  /* A brand held for review (Reno, 2026-10-13): the namer's name was
     below the threshold and waits inside EarWitness. The server sends no
     guess; customers read "Brand not identified" (PLAIN-ADS, lead
     2026-10-24), a fact, not a doubt. */
  const UNDER_REVIEW_LABEL = 'Brand not identified';
  function underReview(read) {
    return !!(read && read.brand && read.brand.under_review);
  }
  /* A held ad's brand as plain text, like an ad with no brand name
     (PLAIN-ADS-FIX1, screen R1-02): never the amber "pending" tag. */
  const HELD_BRAND_HTML = `<span class="ew-brand-none">${UNDER_REVIEW_LABEL}</span>`;
  function heldBrand() { return HELD_BRAND_HTML; }
  /* A name shown as heard but not confirmed (UX-02). Internal only:
     customers see no name flag (PLAIN-ADS). */
  function nameUnconfirmed(read) {
    const b = (read && read.brand) || {};
    return !!(b.display && !b.confirmed && !b.under_review && !needsReview(read));
  }
  /* A shown name carries no review flag (PLAIN-ADS: as far as the
     customer knows every ad is an ad). Kept as no-ops for callers. */
  function nameFlag() { return ''; }
  /* PODS (lead PODS-5; UX PODS-STRINGS P1-P4): a brand Muse Spark named
     in Meta's pods batch (brand display_basis "model") says who named it,
     in plain text right after the name (P1). The ad page adds its
     channel's tip (P2 spoken, P3 url; an unknown channel gets none), and
     an ad list adds P4, counted from the model-named ads it shows. Each
     takes one brand, not one read, so a read showing two names can carry
     two. Any other basis adds nothing. */
  const MODEL_MARK = 'Named by Muse Spark';
  const MODEL_TIPS = {
    spoken: 'Muse Spark, Meta’s AI model, named this brand from the words spoken in the ad’s transcript and found it on our brand list.',
    url: 'Muse Spark, Meta’s AI model, named this brand from a web address in the ad’s transcript and found it on our brand list.',
  };
  function modelNamed(brand) {
    const b = brand || {};
    return b.display_basis === 'model' && !!b.display && !b.under_review;
  }
  function modelMark(brand) {
    return modelNamed(brand) ? ` <span class="ew-brand-mark">${MODEL_MARK}</span>` : '';
  }
  /* Every name Muse Spark gave a read (read.model_names, the server's
     order; read.brand is the first), else its one brand as before. A list
     of one, or a brand that is not model-named, changes nothing; no name
     is promoted over another. */
  function shownBrands(holder) {
    const h = holder || {};
    const list = Array.isArray(h.model_names) ? h.model_names : [];
    if (list.length < 2 || !modelNamed(h.brand)) return [h.brand];
    return list.map(n => ({ key: n.key, display: n.display, display_basis: 'model', model_channel: n.model_channel }));
  }
  // Each shown name through `one`, each with its own label right after it;
  // a model name and its label form one unit, so the label never starts a
  // line alone (PAV-R1-03).
  function brandsHtml(holder, one) {
    const keep = (b, html) => (modelNamed(b) ? `<span class="ew-brand-unit">${html}</span>` : html);
    return shownBrands(holder).map(b => keep(b, one(b) + modelMark(b))).join(' · ');
  }
  const shownNames = holder => shownBrands(holder).map(brandLabel).join(' · ');
  // PODS (UX 2026-10-28): the name a brand alert may be made on, never a
  // model-named one (such an alert never fires for them); none: no offer.
  const offerBrand = holder => shownBrands(holder).find(n => n && !modelNamed(n));
  // A brand-level row (Brands directory, Compare) whose every visible read is model-named.
  function modelOnlyMark(row) {
    return row && row.model_only === true ? ` <span class="ew-brand-mark">${MODEL_MARK}</span>` : '';
  }
  // The tip's words; the page adds "If it's wrong, use Report a mistake." with its own control.
  function modelTip(brand) {
    const channel = modelNamed(brand) ? brand.model_channel : null;
    return channel === 'spoken' || channel === 'url' ? MODEL_TIPS[channel] : '';
  }
  // P4 over the ads a list shows, each ad given as the brands its shown rows name.
  function modelSummary(ads) {
    const n = (ads || []).filter(brands => (brands || []).some(modelNamed)).length;
    return n ? `<p class="section-note ew-model-count">Muse Spark named the brand in ${n} of these ads.</p>` : '';
  }
  function nameFlagWords() { return ''; }
  /* Whole-second clock for read times: 0:04, 12:30, 1:04:01 (an ad's
     time needs no tenths). */
  function clock(n) {
    const v = Math.round(Number(n));
    if (!Number.isFinite(v) || v < 0) return '0:00';
    const h = Math.floor(v / 3600), m = Math.floor(v / 60) % 60, sec = String(v % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
  }
  function timeRange(span) {
    if (!Array.isArray(span)) return '';
    const s = Number(span[0]), e = Number(span[1]);
    if (span[0] == null || span[1] == null || !Number.isFinite(s) || !Number.isFinite(e)) return '';
    return `${clock(s)}–${clock(e)}`;
  }
  /* No review-state tag on customer pages (PLAIN-ADS): the server's
     status stays internal. Kept as a no-op for callers. */
  function statusBadge() { return ''; }
  /* "Report a mistake" on an ad (members only; the server refuses share
     guests and reader credentials). It goes to the owner's queue and
     changes nothing shown meanwhile. wrong_time stays a server reason;
     the page offers these three. */
  // The control's name, kept here with the control (UX-08 source guard): other
  // pages that point to it (the held line, SEARCH-CLARITY) read it from EWUI.
  const REPORT_LABEL = 'Report a mistake';
  const REPORT_REASONS = [['not_an_ad', 'Not an ad'], ['wrong_brand', 'Wrong brand'], ['other', 'Other']];
  function reportControl(ref) {
    const E = ew();
    if (!ref) return '';
    return `<details class="report-problem"><summary>Report a mistake</summary>`
      + `<form class="report-form" data-report-ref="${E.esc(ref)}"><fieldset class="report-reasons"><legend class="small">What is wrong?</legend>`
      + REPORT_REASONS.map(([v, l]) => `<label class="small"><input type="radio" name="reason" value="${v}" required> ${l}</label>`).join('')
      + `</fieldset><label class="small report-note"><span>Details <span class="report-optional">(optional)</span></span><textarea name="note" rows="2" maxlength="1000"></textarea></label>`
      + `<div class="actions"><button type="submit" class="button primary">Send report</button><span class="report-hint">Goes to EarWitness.</span></div><p class="small muted" role="status"></p></form></details>`;
  }
  /* Self-promo reasons arrive as "<rule>:<value>" from the pipeline;
     pages say them in words. Unknown forms pass through unchanged. */
  const PROMO_REASONS = {
    'own-name': v => `Names the show itself (${v})`,
    'own-domain': v => `Points to the show's own website (${v})`,
    'own-platform': v => `Points to the show's own ${v} page`,
    'own-cta': v => `Asks listeners to go to the show's own ${v}`,
    phrase: v => `Says “${v}”`,
    network: v => `Self-promotion for ${v}`,
    'own-call': v => `Says “${v}”`,
  };
  function selfPromoReason(reason) {
    // The ad rules prefix their rule set ("R1:own-name:…", ADRULES).
    const text = String(reason || '').replace(/^R\d+[a-z]?:(?=[a-z-]+:)/, '');
    const at = text.indexOf(':');
    const rule = at > 0 ? text.slice(0, at) : '';
    return Object.hasOwn(PROMO_REASONS, rule) && text.slice(at + 1) ? PROMO_REASONS[rule](text.slice(at + 1)) : text;
  }
  /* A read's brand as text (escape it to render): "Brand · Product ·
     Network" when the namer gave its product or network (Option A),
     else the brand alone. Links and filters keep using the brand key. */
  function brandLabel(brand) {
    const b = brand || {};
    if (b.under_review) return UNDER_REVIEW_LABEL;
    return [b.display, b.product, b.network].filter(Boolean).join(' · ');
  }
  function readFacts(item) {
    const E = ew();
    const read = item.read && !item.read.historical ? item.read : null;
    const range = timeRange(playSpan(read, item.quotation_span));
    const brand = read && read.brand && read.brand.display && brandLabel(read.brand);
    return (underReview(read) ? HELD_BRAND_HTML
      : brand ? `<span class="small"><strong>${E.esc(brand)}</strong></span>` : '')
      + (range ? `<span class="small mono">${E.esc(range)}</span>` : '')
      + (read && read.self_promo ? E.tag('Self-promotion') : '');
  }

  /* What a result row knows about its read (design v3, G1): the current
     read (never a historical one), its time span (the read's display span,
     else the record's quotation span) and whether a clip can play here
     (a kept clip or a local recording). Nothing is guessed. */
  function resultFacts(item) {
    const read = item.read && !item.read.historical ? item.read : null;
    const span = playSpan(read, item.quotation_span);
    const playable = !!(span && ((read && read.has_clip) || item.audio_local));
    return { read, span, playable };
  }
  /* What to say when no clip plays: one of the three member audio states,
     never "Clip available" in a slot where nothing plays (review F7). */
  function clipNote(item) {
    const state = audioState(item);
    return state === AUDIO_CLIP ? AUDIO_NONE : state;
  }
  // "Show · Episode · Oct 9": the show links to its page when the API
  // names its identity; a missing date says so.
  function resultWhere(item, { showLink = true, date = true } = {}) {
    const E = ew();
    const showName = item.show_title || ((item.show || {}).display_title) || 'Unknown show';
    const showId = (item.show || {}).show_identity_id;
    const show = showLink && showId ? E.link('#/show/' + encodeURIComponent(showId), esc(showName)) : esc(showName);
    return `${show} · ${esc(item.ep_title || item.episode_id || 'Episode')}${date ? ` · ${esc(E.date(item.pub_date))}` : ''}`;
  }
  /* The heading of a result: Brand · Product, the held tag in place of a
     held brand (no guessed name), "Mention" for a transcript passage, and
     "Brand not identified" when the read names none. */
  function resultHeading(item, read) {
    // A mention is titled by where it was said (UX-04); the kind is a tag.
    if (item.record_kind === 'transcript_passage') return `<h3 class="ew-result-title ew-mention-title od-clamp-2">${resultWhere(item)}</h3>`;
    if (underReview(read)) return `<h3 class="ew-result-title">${HELD_BRAND_HTML}</h3>`;
    const brand = read && read.brand ? brandHtml(read.brand) : '';
    return `<h3 class="ew-result-title">${brand ? brandsHtml(read, brandHtml) : '<span class="ew-brand-none">Brand not identified</span>'}</h3>`;
  }
  // A result's only tag is a show's own promotion (PLAIN-ADS: no review state).
  function resultTags(item, read) {
    return read && read.self_promo ? tag('self-promo') : '';
  }
  function selectBox(ref, selectedIds, label = 'Select this record for export') {
    const was = Array.isArray(selectedIds) && selectedIds.includes(ref);
    return `<label class="ew-select"><input type="checkbox" name="selected_ref" value="${esc(ref)}"${was ? ' checked' : ''} aria-label="${esc(label)}"></label>`;
  }

  /* Canonical result card (Search, design v3). item is one B1 result row;
     query marks matched terms. selected adds the export checkbox bound to
     the row ref; index adds the "Show in panel" control the inspector uses
     at 1200 px and up (inspected marks the card shown there). mode
     'mentions' names ad reads among mentions. The only customer action on
     an ad is Report a mistake. compact is kept for older callers. */
  function sourceCard(item, { query = '', compact = false, selected = false, selectedIds = null, mode = '', index = null, inspected = false } = {}) {
    const E = ew();
    const ref = item.ref || '';
    const href = '#/evidence/' + encodeURIComponent(ref);
    const { read, span, playable } = resultFacts(item);
    const range = timeRangeHtml(span);
    const inspect = index == null ? ''
      : `<button type="button" class="ew-inspect${range ? '' : ' ew-inspect-text'}" data-action="inspect" data-index="${esc(index)}" aria-pressed="${inspected ? 'true' : 'false'}" aria-label="Preview this result" title="Preview this result">`
        + inspectLabel(range || 'Show in panel') + '</button>';
    const clip = playable
      ? playButton({ span, attrs: { action: 'search-play', ref, from: span[0], to: span[1] } }) + `<span class="ew-clip-len">${clock(span[1] - span[0])} clip</span>`
      : `<span class="ew-noclip">${esc(clipNote(item))}</span>`;
    const passage = item.record_kind === 'transcript_passage';
    const whereTitle = `${item.show_title || ((item.show || {}).display_title) || 'Unknown show'} · ${item.ep_title || item.episode_id || 'Episode'} · ${E.date(item.pub_date)}`;
    // A mention's heading names where it was said; its meta row carries
    // the kind. Ads keep show · episode · date in the meta row.
    const where = passage ? tag('outline', 'Mention') : `<span class="ew-result-where-text" title="${esc(whereTitle)}">${resultWhere(item, { date: false })}</span><span class="ew-result-date">${esc(E.date(item.pub_date))}</span>`;
    const quote = quoteText(item);
    return `<article class="ew-result ew-search-card${compact ? ' compact' : ''}${inspected ? ' is-selected' : ''}" id="row-${esc(ref)}"${index == null ? '' : ` data-index="${esc(index)}"`}>`
      + `<div class="ew-result-meta"><span class="ew-result-where">${selected !== false ? selectBox(ref, selectedIds) : ''}${where}</span>`
      + (range ? `<span class="ew-result-time">${range}</span>` : '') + inspect + `</div>`
      + `<div class="ew-result-head">${resultHeading(item, read)}${resultTags(item, read, mode)}</div>`
      + (quote ? `<p class="ew-result-quote od-clamp-3">“${quote.lead}${E.highlight(quote.text, query)}”</p>` : '')
      // Ads tab: an ad found only by its show or episode title says so.
      + (mode === 'ads' && item.match_basis === 'title' ? `<p class="ew-result-basis">${E.basisLine(item)}</p>` : '')
      + `<div class="ew-result-actions">${clip}<span class="ew-spacer"></span>`
      + (item.record_kind === 'evidence' ? reportControl(ref) : '')
      + `<a class="button ew-view" href="${esc(href)}">Open evidence</a></div></article>`;
  }
  /* The inspect button's text: the time (or label) at rest, "Shown in
     panel →" while pressed at 1200 px and up (CSS follows aria-pressed;
     below 1200 there is no panel and the time stays). html is escaped. */
  function inspectLabel(html) {
    return `<span class="ew-inspect-time">${html}</span><span class="ew-inspect-shown">Shown in panel →</span>`;
  }
  /* The quote a card or inspector shows: the matched passage when the
     server found the match past the opening (match_excerpt, UX-04), led
     by an ellipsis; else the record's own text. */
  function quoteText(item) {
    const ex = item && item.match_excerpt;
    if (ex && typeof ex.text === 'string' && ex.text) return { lead: '…', text: ex.text };
    return item && item.text ? { lead: '', text: item.text } : null;
  }

  /* Compact list row (Search Ads tab, List view): select, play or "—",
     time, Brand · Product (the held tag instead of a guessed name), show ·
     episode, date, Report a mistake, open. No review state (PLAIN-ADS). */
  // selected: false leaves the export checkbox out (a viewer's list).
  function resultRow(item, { selectedIds = null, index = null, inspected = false, selected = true } = {}) {
    const E = ew();
    const ref = item.ref || '';
    const { read, span, playable } = resultFacts(item);
    const t = span ? `${clock(span[0])}–${clock(span[1])}` : '';
    const review = underReview(read);
    const named = !review && read && read.brand && brandHtml(read.brand);
    const brand = review ? HELD_BRAND_HTML : named
      ? brandsHtml(read, b => `<span class="ew-row-brand-name">${brandHtml(b)}</span>`)
      : '<span class="ew-brand-none">Brand not identified</span>';
    const showName = item.show_title || ((item.show || {}).display_title) || 'Unknown show';
    const epName = item.ep_title || item.episode_id || 'Episode';
    // An ad found only by its show or episode title says so (Ads tab).
    const where = `<span class="ew-row-show">${esc(showName)}</span><span class="ew-row-ep">${esc(epName)}</span>`
      + (item.match_basis === 'title' ? E.basisLine(item) : '');
    // The narrow Date column says "Unknown", not "Date unknown".
    const day = E.date(item.pub_date) === 'Date unknown' ? 'Unknown' : E.date(item.pub_date);
    const several = shownBrands(read).length > 1;
    const name = review ? UNDER_REVIEW_LABEL : read && read.brand && read.brand.display ? (several ? shownBrands(read).map(b => b.display).join(' · ') : read.brand.display) : 'ad';
    return `<li class="ew-row${inspected ? ' is-selected' : ''}" id="row-${esc(ref)}"${index == null ? '' : ` data-index="${esc(index)}"`}>`
      + (selected ? `<span class="ew-row-check">${selectBox(ref, selectedIds)}</span>` : '')
      + `<span class="ew-row-play">${playable ? playButton({ span, attrs: { action: 'search-play', ref, from: span[0], to: span[1] } }) : `<span class="ew-play-none" title="${esc(clipNote(item))}">—</span>`}</span>`
      + `<span class="ew-row-brand">${brand}</span>`
      + `<span class="ew-row-where" title="${esc(showName + ' · ' + epName)}">${where}</span>`
      + `<span class="ew-row-meta"><span class="ew-row-time">${index == null ? `<span class="ew-time">${esc(t)}</span>`
        : `<button type="button" class="ew-inspect ew-time" data-action="inspect" data-index="${esc(index)}" aria-pressed="${inspected ? 'true' : 'false'}" aria-label="Preview ${esc(name)}${t ? ', at ' + esc(t) : ''}" title="Preview this result">${esc(t || 'Show')}</button>`
          + `<span class="ew-row-time-text ew-time">${esc(t)}</span>`}</span>`
      + `<span class="ew-row-date">${esc(day)}</span>`
      + `<span class="ew-row-status ew-row-report">${item.record_kind === 'evidence' ? reportControl(ref) : ''}</span></span>`
      + `<a class="ew-row-open" href="${esc('#/evidence/' + encodeURIComponent(ref))}" aria-label="Open evidence: ${esc(name)}${t ? ', ' + esc(t) : ''}">›</a></li>`;
  }

  // Category slugs from the feeds ("truecrime") read as labels; an unknown
  // slug is title-cased, never shown raw.
  const CATEGORY_LABELS = { truecrime: 'True crime', comedy: 'Comedy', business: 'Business', technology: 'Technology' };
  function categoryLabel(slug) {
    const s = String(slug ?? '').trim();
    return CATEGORY_LABELS[s.toLowerCase()] || s.split(/[\s_-]+/).filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
  }
  /* Directory card (design v3 framed card). kind is 'brand' or 'show'.
     Shows: artwork (same-origin cached bytes) over an initials tile; a
     failed image removes itself (CSP-safe, no inline onerror) and the
     tile remains. Only facts the API returns are listed. */
  function directoryCard(entity, { kind = 'brand', qualifiers = null } = {}) {
    const E = ew();
    const isBrand = kind === 'brands' || kind === 'brand';
    const id = isBrand ? (entity.key || entity.brand) : (entity.id);
    const name = isBrand
      ? (entity.display || entity.brand || id)
      : (entity.display_title || entity.id);
    const href = `#/${isBrand ? 'brand' : 'show'}/${encodeURIComponent(id)}`;
    if (isBrand) {
      return `<article class="ew-card lib-card"><h2 class="lib-card-name"><a href="${href}">${E.esc(name)}</a></h2>`
        // Row 1: the brand's ads; an older server's read count is not shown.
        + (entity.ad_breaks != null && entity.ad_breaks !== '' && Number.isFinite(Number(entity.ad_breaks)) ? `<dl class="ew-facts"><dt>Ads</dt><dd>${E.esc(String(entity.ad_breaks))}</dd></dl>` : '') + `</article>`;
    }
    const qualifier = (qualifiers && qualifiers[id]) || '';
    const cats = Array.isArray(entity.category_summary) ? entity.category_summary.filter(Boolean).map(categoryLabel) : [];
    const sub = [cats.join(' · '), entity.unresolved ? 'unresolved identity' : '', qualifier].filter(Boolean).join(' · ');
    const tile = initialsTile(name);
    const art = entity.artwork_url
      ? `<span class="lib-art">${tile}<img class="show-art" src="${E.esc(entity.artwork_url)}" alt="" loading="lazy"></span>` : tile;
    return `<article class="ew-card lib-card"><div class="lib-card-id">${art}<div class="lib-card-text">`
      + `<h2 class="lib-card-name"><a href="${href}">${E.esc(name)}</a></h2>${sub ? `<span class="lib-card-sub">${E.esc(sub)}</span>` : ''}</div></div>`
      + `<dl class="ew-facts"><dt>Episodes</dt><dd>${E.esc(String(entity.episodes ?? 0))}</dd>`
      + (entity.ads_with_clips != null && entity.ads_with_clips !== '' && Number.isFinite(Number(entity.ads_with_clips)) ? `<dt>Ads with clips</dt><dd>${E.esc(String(entity.ads_with_clips))}</dd>` : '')
      + `</dl></article>`;
  }

  /* Durable inline operation state on the caller's element. phase is one
     of loading/saving/saved/error/empty/partial/conflict; message is
     the human-readable detail. Saving disables the nearest submit. */
  function operationState(element, { phase = 'loading', message = '' } = {}) {
    if (!element) return;
    const labels = {
      loading: 'Loading…', saving: 'Saving…', saved: 'Saved.',
      error: 'Error', empty: 'Nothing here yet.',
      partial: 'Partial results', conflict: 'Changed elsewhere',
    };
    element.setAttribute('data-phase', phase);
    element.setAttribute('role', phase === 'error' || phase === 'conflict' ? 'alert' : 'status');
    element.innerHTML = `<span class="op-phase">${labels[phase] || phase}</span>`
      + (message ? `<span class="op-message">${ew().esc(message)}</span>` : '');
    const form = element.closest ? element.closest('form') : null;
    const submit = form ? form.querySelector('[type=submit]') : null;
    if (submit) submit.disabled = (phase === 'saving' || phase === 'loading');
  }

  /* -- Design v3 (Exhibit) shared builders --------------------------------
     Pure HTML strings for the Design System components. Every text is
     escaped; nothing is drawn from a missing value (unknown stays
     unknown); no inline styles (CSP). Classes live in styles.css; the
     catalogue is .tmp-design-v3/foundation/CATALOGUE.md. */
  function esc(v) { return ew().esc(v); }
  function attrList(attrs) {
    // { action: 'play', ref: 'x' } -> data-action="play" data-ref="x"
    return Object.entries(attrs || {}).filter(([, v]) => v !== undefined && v !== null && v !== false)
      .map(([k, v]) => ` data-${String(k).replace(/[^a-z0-9-]/gi, '')}="${esc(v)}"`).join('');
  }
  function spanOf(span) {
    if (!Array.isArray(span) || span[0] == null || span[1] == null) return null;
    const s = Number(span[0]), e = Number(span[1]);
    return Number.isFinite(s) && Number.isFinite(e) && s >= 0 && e >= s ? [s, e] : null;
  }
  /* The span a read's clip plays and its control names (M11; DET-PLAN,
     LEDGER-LEAD3-DET-1): the read's display_span when it has one, else
     `current`, the read's current span (the quotation span, the span a
     button names, or the record's window), as before DET-RI. The one
     place for the cards, the Search player and the evidence page
     (CLIP-STOP ends playback here too). A zero-length or reversed span
     (end <= start) is missing (M11-R1-08). */
  function playSpan(read, current) {
    const ok = span => { const t = spanOf(span); return t && t[1] > t[0] ? t : null; };
    return ok(read && read.display_span) || ok(current);
  }
  function fullClock(n) {
    const v = Math.round(Number(n));
    const p = x => String(x).padStart(2, '0');
    return `${p(Math.floor(v / 3600))}:${p(Math.floor(v / 60) % 60)}:${p(v % 60)}`;
  }
  const ICONS = {
    play: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M3 1.5v9l7.5-4.5z" fill="currentColor"/></svg>',
    pause: '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><rect x="2.5" y="1.5" width="2.5" height="9" fill="currentColor"/><rect x="7" y="1.5" width="2.5" height="9" fill="currentColor"/></svg>',
    check: '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false"><path d="M1.5 5.2l2.3 2.3L8.5 2.8" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
    review: '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false"><circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-dasharray="2 1.4"/></svg>',
  };
  /* Trust/status tag. kind: confirmed | review | uncertain | gap | neutral |
     not-screened | pending | self-promo | code | info | outline | ink |
     words | meaning. A code tag needs its value. */
  const TAG_KINDS = {
    confirmed: ['ew-tag-confirmed', ICONS.check, 'Confirmed'],
    review: ['ew-tag-review', ICONS.review, UNDER_REVIEW_LABEL],
    uncertain: ['ew-tag-uncertain', '<b class="ew-tag-icon" aria-hidden="true">?</b>', 'Uncertain'],
    gap: ['ew-tag-gap', '<b class="ew-tag-icon" aria-hidden="true">!</b>', ''],
    neutral: ['ew-tag-neutral', '', ''],
    'not-screened': ['ew-tag-neutral', '<span class="ew-shape-square" aria-hidden="true"></span>', 'Not screened'],
    pending: ['ew-tag-pending', '<span class="ew-shape-ring" aria-hidden="true"></span>', 'Not screened yet'],
    'self-promo': ['ew-tag-neutral', '<span aria-hidden="true">↺</span>', 'Show’s own promotion'],
    code: ['ew-tag-code', '', 'Code heard'],
    info: ['ew-tag-info', '', ''],
    outline: ['ew-tag-outline', '', ''],
    ink: ['ew-tag-ink', '', ''],
    words: ['ew-tag-words', '', 'Words'],
    brand: ['ew-tag-words', '', 'Brand'],
    show: ['ew-tag-words', '', 'Show'],
    meaning: ['ew-tag-meaning', '', 'Meaning'],
  };
  function tag(kind, label, { value } = {}) {
    const k = Object.hasOwn(TAG_KINDS, kind) ? kind : 'neutral';
    const [cls, icon, fallback] = TAG_KINDS[k];
    const text = label == null || label === '' ? fallback : label;
    if (k === 'code' && (value == null || value === '')) return '';
    if (!text && k !== 'code') return '';
    const val = k === 'code' ? ` <b>${esc(value)}</b>` : '';
    return `<span class="ew-tag ${cls}" data-tag="${k}">${icon}${esc(text)}${val}</span>`;
  }
  /* Coverage tier: 'full' (transcript and ad list) or 'transcript' only. */
  function tierTag(tier) {
    if (tier !== 'full' && tier !== 'transcript') return '';
    const label = tier === 'full' ? 'Full coverage' : 'Transcript only';
    return `<span class="ew-tag ew-tier ew-tier-${tier}" data-tag="tier"><span class="ew-tier-bars" aria-hidden="true"><span></span><span></span></span>${label}</span>`;
  }
  /* Brand · Product (product hidden when it repeats the brand). A brand
     under review replaces the whole label: no guessed name or product. */
  function brandHtml(brand) {
    const b = brand || {};
    if (b.under_review) return HELD_BRAND_HTML;
    if (!b.display) return '';
    const product = b.product && String(b.product).toLowerCase() !== String(b.display).toLowerCase() ? b.product : '';
    const rest = [product, b.network].filter(Boolean).join(' · ');
    return `<span class="ew-brand">${esc(b.display)}${rest ? ` <span class="ew-brand-sep">·</span> <span class="ew-brand-product">${esc(rest)}</span>` : ''}</span>`;
  }
  /* LOGOS: the 17 approved brand logos (Landing LAND-1, app/public/img),
     keyed by the canonical brand key as letters and digits (views.js
     fold: "at&t" is att, "charles schwab" is charlesschwab) -> [file key,
     brand name]. Wayfair, Zocdoc and CarMax have no clean logo; they and
     every other brand keep the plain name. */
  const BRAND_LOGOS = Object.freeze(Object.fromEntries(Object.entries({
    att: ['att', 'AT&T'], betterhelp: ['betterhelp', 'BetterHelp'], carvana: ['carvana', 'Carvana'],
    chase: ['chase', 'Chase'], chatgpt: ['chatgpt', 'ChatGPT'], draftkings: ['draftkings', 'DraftKings'],
    growtherapy: ['growtherapy', 'Grow Therapy'], linkedin: ['linkedin', 'LinkedIn'], mintmobile: ['mintmobile', 'Mint Mobile'],
    ondeck: ['ondeck', 'OnDeck'], plaid: ['plaid', 'Plaid'], progressive: ['progressive', 'Progressive'],
    quince: ['quince', 'Quince'], charlesschwab: ['schwab', 'Charles Schwab'], shopify: ['shopify', 'Shopify'],
    simplisafe: ['simplisafe', 'SimpliSafe'], youtubepremium: ['youtube', 'YouTube Premium'],
  }).map(([k, v]) => [k, Object.freeze(v)])));
  /* Raw keys the server's display list merges into one of the 17 (with
     named ads the page gets the merged key; without, the raw key). Keys
     only: the canon stays 17 brands (tests/test_logos_keys.py). */
  const BRAND_LOGO_ALIASES = Object.freeze({
    schwab: 'charlesschwab', choiceology: 'charlesschwab', simplysafe: 'simplisafe', betterhu: 'betterhelp', chatchipt: 'chatgpt',
    // DET additions (display-brands-det.json, Nov 3 line).
    carvano: 'carvana', chachipt: 'chatgpt', chatchipyt: 'chatgpt', edmobile: 'mintmobile', midmobile: 'mintmobile',
    growththerapy: 'growtherapy', ondec: 'ondeck', ondect: 'ondeck', quance: 'quince', quins: 'quince', quints: 'quince',
    simplysave: 'simplisafe',
  });
  const foldKey = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  /* A brand (its API key) or a canonical key -> {src, alt}, else null.
     Never the display name or typed text; never a brand under review. */
  function brandLogo(brand) {
    const b = brand && typeof brand === 'object' ? brand : { key: brand };
    if (b.under_review || typeof b.key !== 'string') return null;
    const raw = foldKey(b.key);
    const key = Object.hasOwn(BRAND_LOGO_ALIASES, raw) ? BRAND_LOGO_ALIASES[raw] : raw;
    if (!Object.hasOwn(BRAND_LOGOS, key)) return null;
    const [file, alt] = BRAND_LOGOS[key];
    return { src: `/public/img/logo-${file}.png`, alt };
  }
  /* The logo in place of the brand name (alt is the name, so screen
     readers and copy read it once); no logo: the plain name. */
  function brandLogoHtml(brand) {
    const b = brand || {};
    if (b.under_review) return HELD_BRAND_HTML;
    const logo = brandLogo(b);
    if (!logo) return b.display ? esc(b.display) : '';
    return `<img class="ew-brand-logo" src="${esc(logo.src)}" alt="${esc(logo.alt)}" loading="lazy" decoding="async" data-brand-logo>`;
  }
  // A logo that fails to load becomes its name in the same spot (CSP-safe,
  // no inline onerror; error does not bubble, so capture).
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('error', event => {
      const img = event.target;
      if (img && typeof img.matches === 'function' && img.matches('img[data-brand-logo]')) img.replaceWith(document.createTextNode(img.alt));
    }, true);
  }
  /* Time range in mono, printable in full (hh:mm:ss). Missing: ''. */
  function timeRangeHtml(span) {
    const t = spanOf(span);
    if (!t) return '';
    return `<span class="ew-time" data-print="full-time" data-start="${fullClock(t[0])}" data-end="${fullClock(t[1])}">${clock(t[0])}–${clock(t[1])}</span>`;
  }
  /* Round inverse play button. The accessible name comes from label, else
     from real times ("Play clip, 0:31 to 1:02"), else "Play clip". */
  function playButton({ span = null, label = '', playing = false, size = '', outline = false, attrs = null } = {}) {
    const t = spanOf(span);
    const name = label || (playing ? 'Pause clip' : t ? `Play clip, ${clock(t[0])} to ${clock(t[1])}` : 'Play clip');
    const cls = ['ew-play', size === 'lg' ? 'ew-play-lg' : '', outline ? 'ew-play-outline' : '', playing ? 'is-playing' : ''].filter(Boolean).join(' ');
    return `<button type="button" class="${cls}" aria-label="${esc(name)}"${attrList(attrs)}>${playing ? ICONS.pause : ICONS.play}</button>`;
  }
  /* Fact ledger ("Each fact and its source"). rows: [{ label, value,
     mono, source, missing, note, uncertain }]. A missing value states it
     (row.missing, else "Not known"); it is never blank or guessed. */
  function ledger(rows) {
    if (!Array.isArray(rows) || !rows.length) return '';
    const body = rows.map(r => {
      const has = r.value !== null && r.value !== undefined && r.value !== '';
      const cls = !has ? ' class="ew-ledger-missing"' : r.mono ? ' class="ew-ledger-value-mono"' : '';
      const value = has ? esc(r.value) : esc(r.missing || 'Not known');
      const source = has && r.source ? `<div class="ew-ledger-source">${esc(r.source)}</div>` : '';
      const note = r.note ? `<div class="ew-ledger-note">${esc(r.note)}</div>` : '';
      return `<tr${r.uncertain ? ' class="is-uncertain"' : ''}><th scope="row">${esc(r.label)}</th><td${cls}>${value}${source}${note}</td></tr>`;
    }).join('');
    return `<table class="ew-ledger"><tbody>${body}</tbody></table>`;
  }
  /* Uncertain block: what is uncertain and why. No reason, no block. */
  function uncertainBlock({ subject = '', reason = '' } = {}) {
    if (!reason) return '';
    return `<div class="ew-uncertain">${tag('uncertain')}`
      + (subject ? `<p class="ew-uncertain-subject">${esc(subject)}</p>` : '')
      + `<p class="ew-uncertain-reason">${esc(reason)}</p></div>`;
  }
  /* Citation chip: a button that plays inline; without a kept clip it is a
     dashed link that opens the evidence page. */
  function citationChip({ span = null, show = '', episode = '', clip = true, playing = false, href = '', attrs = null } = {}) {
    const t = spanOf(span);
    const range = t ? `${clock(t[0])}–${clock(t[1])}` : '';
    const where = [show, episode].filter(Boolean);
    const text = (range ? `<span class="ew-time">${range}</span>` : '') + where.map(w => ` · ${esc(w)}`).join('');
    if (!clip) {
      return `<a class="ew-cite ew-cite-noclip" href="${esc(href)}"${attrList(attrs)}><span aria-hidden="true">—</span> ${text} · no clip, opens evidence</a>`;
    }
    const name = ['Play citation: ' + (t ? `${clock(t[0])} to ${clock(t[1])}` : 'clip'), ...where].join(', ');
    return `<button type="button" class="ew-cite${playing ? ' is-playing' : ''}" aria-label="${esc(name)}"${attrList(attrs)}>`
      + `<span class="ew-cite-dot" aria-hidden="true">${playing ? ICONS.pause : ICONS.play}</span>${text}</button>`;
  }
  /* The bar of one ad's evidence, from its timestamps only (row 1: the
     server groups evidence into ads). spots: [{ span:[s,e], label,
     under_review, unclear_end }]. Needs 2+ pieces that all have both
     times; otherwise no bar (one piece is an ordinary ad row). Widths are
     each piece's share of the ad's own span: data-w (exact, 0.1 %) and a
     rounded ew-pw-N class (CSP: no inline style). playing: spot index. */
  function podBar({ spots = [], playing = -1, attrs = null } = {}) {
    if (!Array.isArray(spots) || spots.length < 2) return '';
    const spans = spots.map(s => spanOf(s && s.span));
    if (spans.some(t => !t || t[1] <= t[0])) return '';
    const start = Math.min(...spans.map(t => t[0])), end = Math.max(...spans.map(t => t[1]));
    if (!(end > start)) return '';
    const n = spots.length;
    // Evidence that starts before earlier evidence ends overlaps it; the
    // bar lays the pieces side by side, so it says so instead of implying
    // an order (B-2).
    let maxEnd = -Infinity, maxAt = -1;
    const body = spots.map((s, i) => {
      const [a, b] = spans[i];
      const overlaps = i > 0 && a < maxEnd ? maxAt + 1 : 0;
      if (b > maxEnd) { maxEnd = b; maxAt = i; }
      const w = Math.round(((b - a) / (end - start)) * 1000) / 10;
      const review = !!s.under_review;
      const name = review ? UNDER_REVIEW_LABEL : (s.label || 'Brand not identified');
      const unclearStart = !!(s.unclear_start || (i > 0 && spots[i - 1].unclear_end));
      const when = s.unclear_end ? `${unclearStart ? `starts about ${clock(a)}, ` : ''}ends about ${clock(b)}, boundary not clear`
        : unclearStart ? `starts about ${clock(a)}, boundary not clear` : `${clock(a)} to ${clock(b)}`;
      const isPlaying = i === playing;
      const cls = ['ew-pod-spot', `ew-pw-${Math.round(w)}`, review ? 'is-review' : !s.label ? 'is-unnamed' : '',
        s.unclear_end ? 'is-unclear-end' : '', overlaps ? 'is-overlap' : '', isPlaying ? 'is-playing' : ''].filter(Boolean).join(' ');
      // Overlapping evidence has no known order (B-2/R1-16, D-1): none is numbered.
      const shown = `${unclearStart ? '~' : ''}${clock(a)}–${s.unclear_end ? '~' : ''}${clock(b)}${overlaps ? ' · overlaps other evidence' : ''}`;
      return `<button type="button" class="${cls}" data-w="${w}" data-spot="${i}" aria-label="${esc(`Evidence, ${name}, ${when}${overlaps ? ', overlaps other evidence' : ''}${isPlaying ? ', playing' : ''}`)}"${attrList(attrs)}>`
        + `<span class="ew-pod-label">${isPlaying ? '❙❙' : '▸'} ${esc(name)}${isPlaying ? ' · playing' : ''}</span>`
        + `<span class="ew-time">${shown}</span></button>`;
    }).join('');
    return `<div class="ew-pod-bar" role="group" aria-label="Ad, ${n} pieces of evidence">${body}</div>`;
  }
  /* Preview card (plug-in point for meaning-alert previews). state: idle |
     loading | none | failed | results. items: [{ reason, quote, span,
     show, episode, uncertain, why }]. */
  function previewCard({ title = 'Preview', sub = '', state = 'idle', items = [], message = '' } = {}) {
    let body;
    if (state === 'loading') {
      body = `<div class="ew-preview-body" aria-busy="true"><span>${esc(message || 'Checking recent episodes…')}</span><span class="ew-skeleton"></span><span class="ew-skeleton"></span></div>`;
    } else if (state === 'failed') {
      body = `<div class="ew-preview-body" role="alert"><b class="ew-save-state" data-state="failed">! Preview didn’t run</b><span>${esc(message || 'This is not “nothing would match”.')}</span></div>`;
    } else if (state === 'none') {
      body = `<div class="ew-preview-body"><b>Nothing would have been flagged</b>${message ? `<span>${esc(message)}</span>` : ''}</div>`;
    } else if (state === 'results' && Array.isArray(items) && items.length) {
      body = `<ol class="ew-preview-list">${items.map(m => {
        const t = spanOf(m.span);
        const src = [m.show, m.episode].filter(Boolean).map(esc).join(' · ');
        return `<li><div class="ew-result-head">${m.reason ? `<b>${esc(m.reason)}</b>` : ''}${m.uncertain ? tag('uncertain') : ''}</div>`
          + (m.uncertain && m.why ? `<span class="ew-uncertain-reason">${esc(m.why)}</span>` : '')
          + (m.quote ? `<p class="ew-preview-quote">“${esc(m.quote)}”</p>` : '')
          + (t || src ? `<div class="ew-preview-src">${t ? playButton({ span: m.span }) + timeRangeHtml(m.span) : ''}${src ? `<span>${src}</span>` : ''}</div>` : '')
          + '</li>';
      }).join('')}</ol>`;
    } else {
      body = `<div class="ew-preview-body">${esc(message || 'Write a sentence and press Preview to see what it would have flagged. Nothing is saved by previewing.')}</div>`;
    }
    return `<section class="ew-preview" aria-live="polite"><div class="ew-preview-head"><h2>${esc(title)}</h2>`
      + `${sub ? `<span class="ew-preview-sub">${esc(sub)}</span>` : ''}</div>${body}</section>`;
  }
  /* Initials tile: 2 letters, one of six tints chosen by the name (stable).
     Each word's first letter or digit in any script, after any leading
     punctuation or symbol ("#SistersInLaw" is S, "小Lin说" is 小); a name
     with none gets a plain tile (APV-R1-05). Two letters of a joining
     script (Arabic, Syriac, N'Ko, Mongolian, Adlam, Mandaic) would join into
     what reads as a word, so U+200C ZERO WIDTH NON-JOINER keeps them apart;
     the tile is aria-hidden, so it is never read (APV-C-08, APF3-01). */
  function initialsTile(name, { size = '' } = {}) {
    const text = String(name || '');
    let h = 0;
    for (const ch of text) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    const ini = text.split(/\s+/).map(w => w.replace(/^[^\p{L}\p{N}]+/u, ''))
      .filter(w => w && !/^(the|a|an|of)$/i.test(w.replace(/[^\p{L}\p{N}]+$/u, '')))
      .slice(0, 2).map(w => String.fromCodePoint(w.codePointAt(0)).toUpperCase());
    const apart = ini.length === 2 && ini.every(c => /^(?=\p{L})[\p{Script=Arabic}\p{Script=Syriac}\p{Script=Nko}\p{Script=Mongolian}\p{Script=Adlam}\p{Script=Mandaic}]$/u.test(c));
    const cls = ['ew-tile', size === 'lg' ? 'ew-tile-lg' : size === 'sm' ? 'ew-tile-sm' : '', `ew-tint-${h % 6}`].filter(Boolean).join(' ');
    return `<span class="${cls}" aria-hidden="true">${esc(ini.join(apart ? '\u200C' : ''))}</span>`;
  }
  /* Empty (reason + next step), error (never "no results"), loading. The
     actions argument is caller-built, already-escaped HTML. */
  function emptyState({ title = '', body = '', actions = '', kind = '' } = {}) {
    return `<div class="ew-state">${kind ? `<span class="ew-state-kind">${esc(kind)}</span>` : ''}<h2>${esc(title)}</h2>`
      + `${body ? `<p>${esc(body)}</p>` : ''}${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  }
  function errorState({ title = '', body = '', actions = '' } = {}) {
    return `<div class="ew-state ew-state-error" role="alert"><h2>${esc(title)}</h2>`
      + `${body ? `<p>${esc(body)}</p>` : ''}${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  }
  function loadingState({ label = 'Loading…', lines = 3 } = {}) {
    const n = Math.max(1, Math.min(10, Number(lines) || 3));
    return `<div class="ew-loading" role="status" aria-busy="true" aria-label="${esc(label)}"><span class="ew-skeleton ew-skeleton-title"></span>`
      + Array.from({ length: n }, () => '<span class="ew-skeleton"></span>').join('') + '</div>';
  }

  // Plan A (design 8): a retained artifact made before the workspace's
  // plan allowed only excerpts is refused with this server reason.
  function planRefusal(err) {
    return !!err && err.status === 403 && /not available on your plan/i.test(String(err.message || ''));
  }
  /* DEMO2-SOL-R3-04: a saved scope's words in one quote style. Quoted
     spans are exact phrases (as the server reads them); the rest are
     loose words. Plain text: callers escape. */
  function queryWords(q) {
    const text = String(q || '').trim();
    if (!text) return '';
    const phrases = [...text.matchAll(/"([^"]+)"/g)].map(m => m[1].trim()).filter(Boolean);
    const rest = text.replace(/"([^"]+)"/g, ' ').replace(/"/g, ' ').split(/\s+/).filter(Boolean).join(' ');
    const quote = w => `“${w}”`;
    const exact = phrases.length ? `exact ${phrases.length === 1 ? 'phrase' : 'phrases'} ${phrases.map(quote).join(', ')}` : '';
    return [exact, rest ? quote(rest) : ''].filter(Boolean).join(' plus ');
  }
  window.EWUI = { queryWords, categoryLabel, sourceCard, resultRow, resultFacts, playSpan, clipNote, audioState, audioDetail, AUDIO_STATES: { clip: AUDIO_CLIP, none: AUDIO_NONE, changed: AUDIO_CHANGED }, directoryCard, operationState, kindLabel, statusLabel, initials,
    needsReview, underReview, nameUnconfirmed, nameFlag, modelNamed, modelMark, modelTip, modelSummary, shownBrands, brandsHtml, shownNames, offerBrand, modelOnlyMark, nameFlagWords, quoteText, inspectLabel, timeRange, readFacts, brandLabel, selfPromoReason, clock, statusBadge, reportControl,
    tag, tierTag, brandHtml, heldBrand, timeRangeHtml, playButton, ledger, uncertainBlock, citationChip, podBar,
    previewCard, initialsTile, emptyState, errorState, loadingState, planRefusal, BRAND_LOGOS, BRAND_LOGO_ALIASES, brandLogo, brandLogoHtml };
  window.EWUI.REPORT_LABEL = REPORT_LABEL;
})();
