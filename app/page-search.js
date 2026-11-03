/* Redesigned Search (#/search). REDESIGN-BUILD; build source
   docs/landing/build-spec/search/SPEC.md (63b853fbb); plan ws-app/logs/redesign/PLAN.md.
   Owner: the Search builder (this file, app/search-b2.css, tests/frontend_b2_search.test.cjs).
   Route switch: ON. false keeps today's page; true draws this one.

   - render keeps today's searchPage reads (views.js): the Settings opening,
     one research-query bundle (result + week summary + revision), and the
     brand and show names. It retains the frame ('lastSearch') and the items
     ('searchItems') in today's shape, keeps #search-results, the inspect
     and data-index hooks, selected_ref boxes and #search-sort.
   - The mode toggle: with a brand in play (a one-brand filter, or words
     that name a listed brand), "Ads only" is that brand's own ads and
     "Any mention" every ad that says the words; the counts are the reads'
     own (break_total, brand_break_total). With no brand it is today's Ads /
     All mentions switch. Episodes results keep today's page (Q5).
   - Server fields (REDESIGN S1-S7) with their spec fallbacks: heard_at_s
     (else the ad window start), artwork_url (else the initials tile),
     read.media (else the record's own clip block, read when the row is
     selected), brand_spelled (else no spelling line), cells' breaks /
     brand_breaks (else no chart), ads_with_clips (else the clip line's
     first sentence only). */
(() => {
  'use strict';
  const ON = true;
  const E = () => window.EW;
  const U = () => window.EWUI;
  const B = () => window.EWB2;
  const X = () => window.EWViews._b2;
  const esc = v => E().esc(v);
  const num = n => Number(n).toLocaleString('en-US');
  const isNum = v => v != null && v !== '' && Number.isFinite(Number(v));
  const phone = () => B().layout() === 'phone';
  // SEARCH-7 (RSEARCH-R1-01): today's Plan A signal (views.js notePlan/excerptOnly): an excerpt_only workspace has no
  // Any mention; the last full answer or a workspace switch clears it.
  const planA = () => !!(E().excerptOnly && E().excerptOnly());
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // "Oct 5" from an ISO day (UTC), for chart weeks.
  const dayName = iso => {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
    return m ? `${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : '';
  };
  // NOGAP SR row 42 (SR-SCR-R2-05): a date from another year names its year.
  function resultDate(v) {
    const day = E().date(v);
    const year = /^\d{4}-\d{2}-\d{2}/.test(v || '') ? Number(String(v).slice(0, 4)) : null;
    return year && /^[A-Z][a-z]{2} \d/.test(day) && year !== new Date().getUTCFullYear() ? `${day}, ${year}` : day;
  }
  // Date rule (Landing 8a9a0f289, Reno via Review): a range across two years shows both years; inside the current year
  // none ("Oct 19–21" within one month); inside one past year the year once, at the end. A single date (no start)
  // drops the current year only. Dates never break inside (no-break spaces). year: the current year (injected).
  function rangeText(from, to, year = new Date().getUTCFullYear()) {
    const p = v => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || '')); return m ? { y: Number(m[1]), m: MONTHS[Number(m[2]) - 1], d: Number(m[3]) } : null; };
    const a = p(from), b = p(to);
    if (!b) return '';
    const md = x => `${x.m}\u00a0${x.d}`, ymd = x => `${md(x)},\u00a0${x.y}`;
    if (!a) return b.y === year ? md(b) : ymd(b);
    if (a.y !== b.y) return `${ymd(a)} to ${ymd(b)}`;
    const span = a.m === b.m ? `${md(a)}–${b.d}` : `${md(a)} to ${md(b)}`;
    return b.y === year ? span : `${span},\u00a0${b.y}`;
  }
  // 849c650c4 + 399bed463: the collection's start, "2019"; in the current year "Aug 31" (no break inside).
  function goingBack(iso, year = new Date().getUTCFullYear()) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!m) return '';
    return Number(m[1]) === year ? `${MONTHS[Number(m[2]) - 1]}\u00a0${Number(m[3])}` : m[1];
  }
  // 8a9a0f289: a date never breaks inside ("Oct 5", "Oct 21, 2025"): its spaces are U+00A0.
  const keepDate = d => String(d).replace(/ /g, '\u00a0');
  const fold = v => String(v ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const days = (a, b) => (Date.parse(String(b).slice(0, 10) + 'T00:00:00Z') - Date.parse(String(a).slice(0, 10) + 'T00:00:00Z')) / 86400000;

  /* -- Facts of one result ---------------------------------------------------- */
  function facts(item) {
    const it = item || {};
    const { read, span } = U().resultFacts(it);
    const brand = (read && read.brand) || null;
    const held = !!(brand && brand.under_review);
    const named = !!(brand && brand.display && !held);
    const passage = it.record_kind === 'transcript_passage';
    const heard = isNum(it.heard_at_s) ? Number(it.heard_at_s) : span ? span[0] : null;
    const show = it.show_title || (it.show || {}).display_title || 'Unknown show';
    const ep = it.ep_title || it.episode_id || 'Episode';
    return { it, read, span, brand, held, named, passage, heard, show, ep, ref: it.ref || '', unid: !passage && !named };
  }
  // The quote: the brand word (its spelling in the transcript, its name, the
  // search words) marked once. Data rule 8 (Spec 407513adc): the
  // transcript's own punctuation; "…" only where the text is cut, by the
  // source (#11, cross #20: an excerpt from inside the read, or words that
  // start or end mid-sentence) or by the page. The markup cuts at a word end
  // a little past what its surface shows (max); fitQuotes then cuts it to
  // the words that fit its lines, so the CSS clamp is only the safety net.
  // show: about what the surface shows (rows 118, cards and panel 95, phone
  // 105); a mark later than that moves the start.
  // aaecbd611 (recheck 5 #3): a cut never ends on a separator ("|", "/", "-", "–", "—", ":").
  const TRIM = /[\s,;:.…—–|\/-]+$/;
  const FN = /\s+(?:the|a|an|and|or|of|with|to|in|on|for|at|by|from|as|&)$/i;
  function cutText(s, max) {
    if (s.length > max) {
      const part = s.slice(0, max + 1);
      const sp = part.lastIndexOf(' ');
      return (sp > 0 ? part.slice(0, sp) : s.slice(0, max)).replace(TRIM, '') + '…';
    }
    return /[.!?…]["”’)]?$/.test(s) ? s : s.replace(TRIM, '') + '…';
  }
  function quoteHtml(item, q, max = 240, show = max) {
    const f = facts(item);
    const qt = U().quoteText(item) || { lead: '', text: '' };
    let text = String(qt.text || '').replace(/\s+/g, ' ').trim();
    const ex = item && item.match_excerpt;
    let lead = ex && Number(ex.start) === 0 ? '' : qt.lead || '';
    if (!lead && /^\p{Ll}/u.test(text)) lead = '…';
    const needles = [item && item.brand_spelled, f.named && f.brand.display, f.brand && f.brand.key, ...String(q || '').split(/\s+/)]
      .filter(w => w && String(w).trim().length > 1);
    let range = null;
    for (const w of needles) {
      const r = E().matchRanges(text, [{ text: String(w), whole: true }]).ranges[0];
      if (r) { range = r; break; }
    }
    // Keep the mark inside what shows: start a little before it when it is late.
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
    let html;
    if (range && range.end <= body.length) {
      const leadMark = !lead && range.start === 0;
      html = esc(body.slice(0, range.start)) + `<mark class="s-mark${leadMark ? ' s-mark--lead' : ''}">${esc(body.slice(range.start, range.end))}</mark>` + esc(body.slice(range.end));
    } else html = esc(body);
    return `“${esc(lead)}${html}${tail}”`;
  }
  // lib.cjs cutQuotes: the longest cut of body (the words after the mark) at
  // a word end, with "…" and no space, comma or period before it, that fits;
  // null when even "…" alone does not.
  function fitText(body, fits) {
    const ends = [0];
    for (let i = 1; i < body.length; i++) if (/\s/.test(body[i]) && !/\s/.test(body[i - 1])) ends.push(i);
    // textcut.cjs: a cut one word after the source's own "…" ends at that "…" (no "…Download…").
    // c9ea045ca (textcut.cjs FN rule): a cut drops a short word it would end on ("…DMT and the…" reads "…DMT…").
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
  // The words before the mark, when they alone overflow (real leads run longer
  // than the reference's): the longest run of whole words that fits, behind
  // "…"; the text unchanged when it fits; null when even "…" does not.
  function fitFront(text, fits) {
    if (fits(text)) return text;
    const starts = [];
    for (let i = 1; i < text.length; i++) if (!/\s/.test(text[i]) && /\s/.test(text[i - 1])) starts.push(i);
    starts.push(text.length);
    const at = k => '…' + text.slice(starts[k]);
    let lo = 0, hi = starts.length - 1, best = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (fits(at(mid))) { best = mid; hi = mid - 1; } else lo = mid + 1; }
    return best < 0 ? null : at(best);
  }
  const QUOTES = '.s-page .s-quote, .s-page .s-quote-m, .s-page .s-panel__quote';
  // The text as rendered, kept per element, so a resize cuts again from the whole text.
  const ORIG = new WeakMap();
  function restoreText(el) {
    const saved = ORIG.get(el);
    if (saved) { for (const [t, d] of saved) if (el.contains(t)) t.data = d; return; }
    const keep = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) keep.push([t, t.data]);
    ORIG.set(el, keep);
  }
  // Pure: write a cut text back into its own text nodes in order (a bold show
  // stays bold); "…" goes on the last node that keeps any text.
  function spreadText(nodes, orig, v) {
    const full = orig.join('');
    const cut = v.endsWith('…') && full.startsWith(v.slice(0, -1));
    let left = (cut ? v.slice(0, -1) : v).length;
    nodes.forEach((t, i) => { const take = Math.max(0, Math.min(orig[i].length, left)); t.data = orig[i].slice(0, take); left -= take; });
    const end = [...nodes].reverse().find(t => t.data.trim()) || nodes[0];
    if (end && cut) end.data = end.data.replace(/\s+$/, '') + '…';
  }
  // 4c6e1c3ac (UX recheck 3 #3, textcut.cjs): every other clamped or one-line
  // text (episode titles, show and tile names) is cut in the text at a word
  // end, with "…"; the element keeps its whole text in title.
  const CLIPS = 'p, span, div, b, a, h1, h2, h3, button, strong';
  const isClipped = el => {
    const cs = getComputedStyle(el);
    return /hidden|clip/.test(cs.overflow + cs.overflowX) && ((cs.webkitLineClamp && cs.webkitLineClamp !== 'none') || cs.textOverflow === 'ellipsis');
  };
  // c9ea045ca (recheck 4 #1, textcut.cjs): "{name} · {date}" cuts inside the name only, so the date follows the "…".
  function cutRoot(el) {
    const fe = el.firstElementChild;
    if (!fe || (fe.previousSibling && String(fe.previousSibling.textContent || '').trim())) return el;
    let after = '';
    for (let x = fe.nextSibling; x; x = x.nextSibling) after += x.textContent || '';
    return /^\s*·\s*\S/.test(after) ? fe : el;
  }
  // aaecbd611 (recheck 5 #2): "{name} · {date}" is one line whose name takes the CSS ellipsis; it is over when the
  // name is, so the name is cut once, in the text, and CSS never cuts it again.
  function isOver(el) {
    const root = cutRoot(el);
    return el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1 || (root !== el && root.scrollWidth > root.clientWidth + 1);
  }
  function fitPlain(el) {
    const over = () => isOver(el);
    if (!el.clientWidth || !over()) return;
    const whole = el.textContent;
    const nodes = [];
    const tw = document.createTreeWalker(cutRoot(el), NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) nodes.push(t);
    if (!nodes.length) return;
    const orig = nodes.map(t => t.data);
    const full = orig.join('');
    const best = fitText(full, v => { spreadText(nodes, orig, v); return !over(); });
    if (best === null || best === '…') { nodes.forEach((t, i) => { t.data = orig[i]; }); return; }
    spreadText(nodes, orig, best);
    if (!el.closest('[title]')) el.setAttribute('title', whole.replace(/\s+/g, ' ').trim());
  }
  function fitQuote(el) {
    const over = () => el.scrollHeight > el.clientHeight + 1;
    if (!el.clientHeight || !over()) return;
    const marks = el.querySelectorAll('mark');
    const last = marks[marks.length - 1];
    const nodes = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let t = tw.nextNode(); t; t = tw.nextNode()) {
      if (!last || (!last.contains(t) && (last.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING))) nodes.push(t);
    }
    if (!nodes.length) return;
    const full = nodes.map(t => t.data).join('');
    const pre = !last && full.startsWith('“') ? full.slice(0, /^“…?/.exec(full)[0].length) : '';
    const close = full.endsWith('”') ? '”' : '';
    const body = full.slice(pre.length, full.length - close.length);
    const set = v => { nodes[0].data = pre + v + close; for (const t of nodes.slice(1)) t.data = ''; };
    // The words before the first mark (only with a mark): cut when "…" after the mark still overflows.
    const lead = [];
    if (last) {
      const fw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let t = fw.nextNode(); t; t = fw.nextNode()) if (marks[0].compareDocumentPosition(t) & Node.DOCUMENT_POSITION_PRECEDING) lead.push(t);
    }
    const leadFull = lead.map(t => t.data).join('');
    const open = (/^“/.exec(leadFull) || [''])[0];
    const leadText = leadFull.slice(open.length);
    const setLead = v => { if (!lead.length) return; lead[0].data = open + v; for (const t of lead.slice(1)) t.data = ''; };
    const r = cutQuote(last ? leadText : null, body, (l, t) => { setLead(l); set(t); return !over(); });
    setLead(r ? r.lead : leadText);
    set(r ? r.tail : body);
  }
  // Pure: the quote's cut. The end after the last mark first (lib.cjs cutQuotes);
  // when even "…" there overflows, the words before the first mark too, then the
  // end again. lead is null without a mark. null: nothing fits (the clamp stays).
  function cutQuote(lead, body, fits) {
    const tail = fitText(body, v => fits(lead || '', v));
    if (tail !== null || lead === null) return tail === null ? null : { lead, tail };
    const front = fitFront(lead, v => fits(v, '…'));
    if (front === null) return null;
    const end = fitText(body, v => fits(front, v));
    return { lead: front, tail: end === null ? '…' : end };
  }
  // Fit every quote on screen whenever the page paints (render, the panel,
  // Show 10 more), on resize and once the fonts load.
  let fitQueued = false;
  function queueFit() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => {
      fitQueued = false;
      const quotes = new Set(document.querySelectorAll(QUOTES));
      const plain = [...document.querySelectorAll('.s-page :is(' + CLIPS + ')')].filter(el => !quotes.has(el) && isClipped(el));
      for (const el of [...quotes, ...plain]) restoreText(el);
      for (const el of quotes) fitQuote(el);
      for (const el of plain.reverse()) fitPlain(el);
    });
  }
  function watchQuotes() {
    if (typeof MutationObserver !== 'function' || typeof requestAnimationFrame !== 'function' || typeof document === 'undefined' || !document.createTreeWalker) return;
    const root = document.getElementById('main') || document.body;
    if (!root) return;
    new MutationObserver(() => { refocusDraft(); queueFit(); }).observe(root, { childList: true, subtree: true });
    window.addEventListener('resize', queueFit);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
  }
  // Tags: only what was found (PLAIN-ADS-FIX2), and the spelling line when it differs.
  // Q4 (Landing final): on Any mention, a row that is an ad says whose ad it is.
  const insideTag = f => (f.named && !f.passage
    ? `<span class="s-tag s-tag--inside">Inside ${/^[aeiou]/i.test(allNames(f)) ? 'an' : 'a'} ${esc(allNames(f))} ad</span>` : '');
  // Review 3 (search-60fb65a5): the spelling line only when the shown quote says it.
  const quoteSays = (quote, word) => !!E().matchRanges(String(quote || '').replace(/<[^>]+>/g, ''), [{ text: String(word), whole: true }]).ranges[0];
  function tagsHtml(f, { spelling = true, inside = false, quote = '' } = {}) {
    const said = (f.read && f.read.said) || {};
    const tags = [
      ...(inside && insideTag(f) ? [insideTag(f)] : []),
      ...(said.web_addresses || []).map(x => `<span class="s-tag s-tag--code-sm">Web address read out <b class="s-code">${esc(x)}</b></span>`),
      ...(said.promo_codes || []).map(x => `<span class="s-tag s-tag--code-sm">Promo code read out <b class="s-code">${esc(x)}</b></span>`),
    ];
    const sp = f.it.brand_spelled;
    if (spelling && f.named && sp && fold(sp) !== fold(f.brand.display) && quoteSays(quote, sp)) tags.push(`<span class="s-meta">The transcript spells it “${esc(sp)}”.</span>`);
    return tags.length ? `<div class="s-panel__tags">${tags.join('')}</div>` : '';
  }
  // Play {length} on today's clip engine; a clip whose block is not on the
  // row (S4 missing) selects the row, whose record read brings it.
  function playHtml(f, id, size) {
    const media = f.read && f.read.media;
    if (f.read && f.read.has_clip === true && media === undefined) {
      const cls = { '': 's-play', card: 's-play s-play--card', lg: 's-play s-play--lg', phone: 's-play-m' }[size] || 's-play';
      return `<button type="button" class="${cls}" data-b2-select><span class="s-play__disc"><span class="s-play__tri${size === 'phone' ? ' s-play__tri--phone' : size ? ' s-play__tri--' + size : ''}"></span></span>Play</button>`;
    }
    return B().play({ ref: f.ref, media, span: f.span, id, size, quoteAt: f.it.quote_at_s });
  }
  const showRoute = ref => '#/evidence/' + encodeURIComponent(ref);
  function check(f, ticked) {
    if (!X().canExport()) return '';
    return `<input class="s-check" type="checkbox" name="selected_ref" value="${esc(f.ref)}" aria-label="Select this ad"${ticked ? ' checked' : ''}>`;
  }
  const unidHtml = (f, ph) => (f.unid ? `<span class="s-unid-plain${ph ? ' s-unid-plain--phone' : ''}">Brand not identified</span>` : '');
  /* PODS (PODS-STRINGS P1, PODS-REDESIGN-PORT): a read's every shown name through `one`, each a model-named one
     with "Named by Muse Spark" right after it, the two one unit (EWUI.brandsHtml). Short and screen-reader labels
     name every brand with no label (EWUI.shownNames). A brand-level name (filter, heading, brand list) takes the
     label only when every read of it is model-named (model_only). Campaign screens never name a model-named read. */
  // A model-named read's names are one group (s-names) in the row they sit in, so they wrap whole, never squeezed.
  const namesHtml = (f, one) => (U().shownBrands(f.read).some(U().modelNamed) ? `<span class="s-names">${U().brandsHtml(f.read, one)}</span>` : U().brandsHtml(f.read, one));
  const allNames = f => U().shownNames(f.read);
  const onlyUnit = (b, html) => (U().modelOnlyMark(b) ? `<span class="ew-brand-unit">${html}${U().modelOnlyMark(b)}</span>` : html);
  const campaignName = f => (f.named && !U().modelNamed(f.brand) ? f.brand.display : '');

  /* -- Rows, cards, phone rows ----------------------------------------------- */
  // d25da7c04 recheck 2 #1: the air date never cuts; the title clips, its full text in title.
  const epline = (f, item) => `<span class="s-row__eptitle" title="${esc(f.ep)}">${esc(f.ep)}</span><span class="s-row__epdate">· ${esc(resultDate(item.pub_date))}</span>`;
  function rowHtml({ item, index, q, ticked = false, current = false, inside = false }) {
    const f = facts(item);
    const cls = ['s-row', ticked ? 's-row--ticked' : '', current ? 's-row--current' : ''].filter(Boolean).join(' ');
    const quote = quoteHtml(item, q, 160, 118);
    const when = f.heard == null ? '' : `<div class="s-list__when"><div class="s-row__at">${esc(U().clock(f.heard))}</div><div class="s-row__into">into the episode</div></div>`;
    // RSEARCH-R1-03: Tab reaches every row; Enter or Space previews it (from 1300 px) or opens the ad (keydown).
    return `<div class="${cls}" data-action="inspect" data-index="${esc(index)}" id="row-${esc(f.ref)}" tabindex="0">`
      + (X().canExport() ? `<label class="s-row__tick">${check(f, ticked)}</label>` : '')
      + `<div class="s-row__lead">${B().cover({ title: f.show, artwork_url: item.artwork_url })}</div>`
      + `<div class="s-row__body"><p class="s-quote">${quote}</p>${tagsHtml(f, { inside, quote })}${unidHtml(f)}`
      + `<div class="s-row__meta"><span class="s-row__show"><b class="s-row__name" title="${esc(f.show)}">${esc(f.show)}</b>`
      + `<span class="s-row__epline">${epline(f, item)}</span></span></div></div>`
      + `<div class="s-row__side">${when}${f.passage ? '' : playHtml(f, 'r' + index, '')}</div></div>`;
  }
  function cardHtml({ item, index, q, ticked = false, current = false, inside = false }) {
    const f = facts(item);
    const cls = ['s-card', ticked ? 's-card--ticked' : '', current ? 's-card--current' : ''].filter(Boolean).join(' ');
    const quote = quoteHtml(item, q, 130, 95);
    const mark = f.named ? namesHtml(f, b => B().brandMark(b, { cls: 's-logo--14' })) : f.unid ? unidHtml(f) : '';
    const at = f.heard == null ? '' : `<span class="s-card__at">${esc(U().clock(f.heard))}</span><span class="s-meta--faint">into the episode</span>`;
    return `<article class="${cls}" data-action="inspect" data-index="${esc(index)}" id="row-${esc(f.ref)}">`
      + `<div>${B().cover({ title: f.show, artwork_url: item.artwork_url }, { size: 'xl' })}</div><div class="s-card__body">`
      + `<div class="s-card__top"><div class="s-card__title"><div class="s-showname" title="${esc(f.show)}">${esc(f.show)}</div>`
      + `<div class="s-card__epline">${epline(f, item)}</div></div>`
      + (X().canExport() ? `<label class="s-card__tick">${check(f, ticked)}</label>` : '') + `</div>`
      + `<div class="s-ids">${f.passage ? '' : '<span class="s-adtag">AD</span>'}${mark}<span class="s-grow"></span>${at}</div>`
      + `<p class="s-quote s-quote--card">${quote}</p>${tagsHtml(f, { inside, quote })}`
      + `<div class="s-card__foot">${f.passage ? '' : playHtml(f, 'c' + index, 'card')}<span class="s-grow"></span><a class="s-btn s-btn--wide" href="${esc(showRoute(f.ref))}">Open the ad</a></div>`
      + `</div></article>`;
  }
  function phoneRowHtml({ item, index, q, inside = false }) {
    const f = facts(item);
    const name = f.named ? allNames(f) : 'an ad';
    const play = f.passage ? '' : playHtml(f, 'm' + index, 'phone');
    const quote = quoteHtml(item, q, 140, 105);
    // "{show} · {date}" as the refs (Landing's corrected answer): Search is one brand.
    const where = f.show;
    return `<div class="s-prow" role="link" tabindex="0" aria-label="Open the ad: ${esc(name)} on ${esc(f.show)}" data-href="${esc(showRoute(f.ref))}" data-index="${esc(index)}">`
      + `<div>${B().cover({ title: f.show, artwork_url: item.artwork_url }, { size: 'sm' })}</div>`
      + `<div class="s-row__body"><span class="s-quote-m">${quote}</span>${tagsHtml(f, { inside, quote })}${unidHtml(f, true)}`
      + `<span class="s-meta s-meta--clamp s-prow__show" title="${esc(where)}"><b class="s-chart__title">${esc(where)}</b> · ${esc(resultDate(item.pub_date))}</span>`
      + (f.heard == null ? '' : `<span class="s-meta"><b class="s-prow__at">${esc(U().clock(f.heard))}</b> into the episode</span>`)
      + (/s-noclip/.test(play) ? play : play ? `<span class="s-prow__chips">${play}</span>` : '')
      + `</div><span class="s-prow__go" aria-hidden="true">›</span></div>`;
  }

  /* -- Preview panel ------------------------------------------------------------ */
  // Walkthrough 6 (SEARCH-7 7a): `clip` is the selected row's clip id (r{i} / c{i}) when the row has one on screen: the
  // panel's Play names it and draws no audio, so one player sounds and the engine shows Pause on both buttons.
  function panelHtml(item, { loading = false, q = '', clip = null } = {}) {
    if (!item) {
      return `<div class="s-panel__none"><div class="s-panel__none-title">Pick an ad to see its proof</div>`
        + `<div class="s-panel__none-text">Click any row. You’ll see what the host said, on which show, and how far into the episode.</div></div>`;
    }
    if (loading) {
      return `<div class="s-panel__load" role="status"><div class="s-meta">Loading this ad…</div><div class="s-skel__text"></div><div class="s-skel__text"></div><div class="s-skel__text s-skel__text--75"></div>`
        + `<div class="s-skel__who"><div class="s-skel__avatar"></div><div class="s-skel__who-text"><div class="s-skel__name"></div><div class="s-skel__meta"></div></div></div><div class="s-skel__btn"></div></div>`;
    }
    const f = facts(item);
    const mark = f.named ? namesHtml(f, b => (B().hasLogo(b) ? B().brandMark(b, { cls: 's-logo--15' }) : `<b class="s-chart__title">${esc(b.display)}</b>`)) : unidHtml(f);
    const time = f.heard == null ? '' : `<span class="s-panel__time">${esc(U().clock(f.heard))}</span><span class="s-meta--faint">into the episode</span>`;
    const heldNote = f.held ? '<p class="s-panel__note s-panel__note--muted">We heard an ad here but couldn’t name the brand.</p>' : '';
    // UX D41 (WORDS.md): no "Brand heard" chip on a model-named read; the ids line above names it, labelled.
    const heardChip = f.named && !U().shownBrands(f.read).some(U().modelNamed) ? `<span class="s-tag s-tag--none">Brand heard: <b class="s-chart__title">${esc(f.brand.display)}</b></span>` : '';
    const found = tagsHtml(f, { spelling: false }).replace(/^<div class="s-panel__tags">|<\/div>$/g, '');
    const sp = item.brand_spelled;
    const quote = quoteHtml(item, q, 120, 95);
    const spelling = f.named && sp && fold(sp) !== fold(f.brand.display) && quoteSays(quote, sp) ? `<p class="s-meta">The transcript spells it “${esc(sp)}”.</p>` : '';
    // RSEARCH-R1-07: a mention passage takes it hosted only, as today's inspector (views.js inspectorReadActions).
    const attach = f.passage && !E().current.hosted ? '' : `<button type="button" class="s-panel__add" data-action="add-to-campaign" data-ref="${esc(f.ref)}" data-episode="${esc(item.episode_id || '')}" data-show="${esc((item.show || {}).show_identity_id || '')}" data-day="${esc(item.pub_date || '')}" data-show-name="${esc((item.show || {}).display_title || item.show_title || '')}" data-episode-name="${esc(item.ep_title || '')}"`
      + (f.passage ? ' data-kind="passage"' : '') + (campaignName(f) ? ` data-brand-name="${esc(campaignName(f))}"` : '') + (f.heard == null ? '' : ` data-at="${esc(U().clock(f.heard))}"`)
      + ` data-adding="${esc([f.passage ? 'Mention' : campaignName(f) ? `${campaignName(f)} ad` : 'Ad, brand not identified', f.show, f.heard == null ? '' : B().at(f.heard)].filter(Boolean).join(' · '))}">Add to my campaign</button>`;
    return `<div class="s-panel__top"><span class="s-panel__label">Ad preview</span>`
      + `<div class="s-ids">${f.passage ? '' : '<span class="s-adtag">AD</span>'}${mark}<span class="s-grow"></span>${time}</div>`
      + `<p class="s-panel__quote">${quote}</p>${spelling}${heldNote}`
      + (heardChip || found ? `<div class="s-panel__tags">${heardChip}${found}</div>` : '') + `</div>`
      + `<div class="s-panel__show"><div>${B().cover({ title: f.show, artwork_url: item.artwork_url }, { size: 'panel' })}</div>`
      + `<div class="s-panel__text"><div class="s-panel__name" title="${esc(f.show)}">${esc(f.show)}</div>`
      + `<div class="s-card__epline">${epline(f, item)}</div></div></div>`
      + `<div class="s-panel__actions"><div class="s-panel__buttons">${f.passage ? '' : clip ? playHtml(f, clip, 'lg').replace(/<audio\b[^>]*><\/audio>/, '') : playHtml(f, 'p-' + f.ref, 'lg')}`
      + `${attach}</div>`
      // UX D43 (QUOTE-START): the panel's one filled button, on its own full-width line below Play and Add.
      + `<a class="s-cta s-cta--nowrap s-panel__open" href="${esc(showRoute(f.ref))}">Open the full ad page →</a>`
      + `<div class="s-panel__links"><button type="button" class="s-btn" data-action="copy-read-link" data-ref="${esc(f.ref)}"${f.passage ? ' data-kind="passage"' : ''}>Copy link</button>`
      + `${f.passage ? '' : U().reportControl(f.ref)}</div></div>`;
  }
  const inspectorLive = item => {
    if (!item) return '';
    const f = facts(item);
    return `Selected: ${f.named ? allNames(f) : f.passage ? 'Mention' : 'Brand not identified'} · ${f.show}`;
  };

  /* -- Selection bar (Export only, data rule 6) --------------------------------- */
  function selbar(n) {
    if (!n) return '';
    return `<div class="s-selbar"><span class="s-selbar__count">${num(n)} selected</span><button type="button" class="s-selbar__clear" data-b2-clear>Clear</button>`
      + `<span class="s-selbar__note">A spreadsheet with each ad’s show, episode, date, time and words. No audio.</span>`
      + `<button type="button" class="s-selbar__export" data-action="export-selected">Export ${B().count(n, 'ad')} (CSV)</button></div>`;
  }

  /* -- Week chart (inline SVG; attributes and classes only) ---------------------- */
  const G = {
    wide: { w: 560, h: 150, x0: 28, x1: 556, axis: 124, top: 18, bar: 0.58, lbl: 139, tick: 22 },
    phone: { w: 326, h: 132, x0: 24, x1: 322, axis: 110, top: 18, bar: 0.64, lbl: 123, tick: 18 },
  };
  const trim = n => String(Math.round(n * 1000) / 1000);
  function niceStep(max) {
    for (const s of [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000]) if (s * 2 >= max) return s;
    return Math.ceil(max / 2);
  }
  // The week this route shows (from/to inside one week), or null.
  function chosenWeek(c) {
    if (!c.from || !c.to) return null;
    const d = days(c.from, c.to);
    return d >= 0 && d <= 6 ? c.from : null;
  }
  const weekRoute = (c, from, to) => E().searchURL({ ...c, from, to, page: 1 });
  // "Oct 19–21" ("Oct 30–Nov 1") for a week shorter than seven days, else null.
  // The chosen week's days: its chart cell when the chart is read (the data
  // may end before the route's week does), else the route's own dates.
  let weekCells = [];
  const weekPart = c => partialDays(weekCells.find(x => (x.interval || {}).from === c.from) || { interval: { from: c.from, to: c.to } });
  function partialDays(cell) {
    const { from, to } = cell.interval || {};
    if (!from || !to || days(from, to) >= 6) return null;
    const a = dayName(from), b = dayName(to);
    return a.split(' ')[0] === b.split(' ')[0] ? `${a}–${b.split(' ')[1]}` : `${a}–${b}`;
  }
  // c9ea045ca (recheck 4, Review): the axis labels the first and the last week at every width; middle labels skip
  // (every); a label that would touch the one before it goes, and when the last would touch, the one before it goes
  // (about 7 px a character at 12 px; spec design-patches.cjs). Pure: which of names[] show.
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
  function chartSvg(dated, { geo, value, route, chosen, label, clear = '' }) {
    const n = dated.length;
    const step = niceStep(Math.max(1, ...dated.map(value)));
    const topV = step * 2;
    const span = geo.axis - geo.top;
    const y = v => geo.axis - (v / topV) * span;
    const wk = (geo.x1 - geo.x0) / n;
    const every = geo === G.phone ? 2 : Math.max(1, Math.ceil(n / 9));
    const names = dated.map(cell => partialDays(cell) || dayName((cell.interval || {}).from || cell.key));
    const shown = axisPlan(names, wk, every);
    let out = `<svg class="s-chart${chosen ? ' s-chart--filtered' : ''}" width="100%" height="${geo.h}" viewBox="0 0 ${geo.w} ${geo.h}" role="group" aria-label="${esc(label)} per week. Choose a week to filter the list.">`
      + `<line class="s-chart__axis" x1="${geo.x0}" x2="${geo.x1}" y1="${geo.axis}" y2="${geo.axis}"></line><text class="s-chart__tick" x="${geo.tick}" y="${geo.axis + 4}">0</text>`
      + [1, 2].map(k => `<line class="s-chart__grid" x1="${geo.x0}" x2="${geo.x1}" y1="${trim(y(step * k))}" y2="${trim(y(step * k))}"></line><text class="s-chart__tick" x="${geo.tick}" y="${trim(y(step * k) + 4)}">${num(step * k)}</text>`).join('')
      + `<line class="s-chart__axis" x1="${geo.x0}" x2="${geo.x0}" y1="${geo.top}" y2="${geo.axis}"></line>`;
    dated.forEach((cell, i) => {
      const v = value(cell), from = (cell.interval || {}).from || cell.key;
      const cx = geo.x0 + wk * i + wk / 2, bw = wk * geo.bar, h = (v / topV) * span;
      const on = !!chosen && from === chosen;
      // ae05c2b14 (Brand #3): a week the data cuts short is named by its days, "Oct 19–21".
      const part = partialDays(cell);
      const what = part ? `${part} (partial week)` : `Week of ${dayName(from)}`;
      // The chosen week: its label bold and underlined; choosing it again shows all weeks.
      const name = `${what}: ${B().count(v, 'ad')}. ${on ? 'Selected.' : 'Show only this week.'}`;
      const axis = names[i];
      out += `<g class="s-chart__week"><rect class="s-chart__bar${on ? ' s-chart__bar--on' : ''}" x="${trim(cx - bw / 2)}" y="${trim(geo.axis - h)}" width="${trim(bw)}" height="${trim(h)}" rx="2"></rect>`
        + `<text class="s-chart__val" x="${trim(cx)}" y="${trim(geo.axis - h - 5)}">${num(v)}</text>`
        + `<text class="s-chart__lbl${on ? ' s-chart__lbl--on' : ''}" x="${trim(cx)}" y="${geo.lbl}">${on || shown[i] ? esc(axis) : ''}</text>`
        + (on ? `<rect class="s-chart__sel" x="${trim(cx - bw / 2)}" y="${geo.h - 4}" width="${trim(bw)}" height="3" rx="1.5"></rect>` : '')
        + `<rect class="s-chart__hit" x="${trim(geo.x0 + wk * i)}" y="0" width="${trim(wk)}" height="${geo.h}" tabindex="0" role="button" aria-pressed="${on ? 'true' : 'false'}" aria-label="${esc(name)}" data-search="${esc(on && clear ? clear : route(cell))}"></rect></g>`;
    });
    return out + '</svg>';
  }
  // The cells the chart draws and how it counts them: distinct ads (S5),
  // the brand's own when the head counts the brand's own.
  // The server's week runs from + 6 days; the data's last week ends on the data's last day (Overview's
  // newest_episode.date; SEARCH-5, App: "Oct 19–21", never "Oct 25").
  const clampWeek = (cell, lastDay) => {
    const { from, to } = cell.interval || {};
    return lastDay && from && to && from <= lastDay && lastDay < to ? { ...cell, interval: { from, to: lastDay } } : cell;
  };
  function chartModel(cells, { brandOwn, lastDay = null }) {
    const dated = (cells || []).filter(x => x.key !== 'unknown').map(x => clampWeek(x, lastDay));
    const key = brandOwn && dated.length && dated.every(x => isNum(x.brand_breaks)) ? 'brand_breaks' : 'breaks';
    if (!dated.length || !dated.every(x => isNum(x[key]))) return null;
    return { dated, value: x => Number(x[key]) || 0, brandOwn: key === 'brand_breaks' };
  }
  // 4c6e1c3ac (recheck 3 #5, #7): the last week's end, with a no-break space; no range. The end is the data's
  // last day (the cells are clamped to it); without it the caption claims no end. c9ea045ca (recheck 4 #3): a chart
  // of the brand's own ads under Any mention says so.
  const caption = (dated, { lastDay = null, only = '' } = {}) => {
    const b = lastDay && (dated[dated.length - 1].interval || {}).to;
    const counts = only ? `Counts ${esc(only)} ads only.` : 'Counts of ads heard in this collection.';
    return `<div class="s-chart__cap">Week starting${b ? `; the last week runs to ${esc(dayName(b).replace(' ', '\u00a0'))}` : ''}. ${counts}</div>`;
  };
  const chartTitle = brand => (brand ? `${brand.display} ads heard, by week` : 'Ads heard, by week');
  function wideChart({ model, brand, c, clear, lastDay = null, only = '' }) {
    if (!model) return '';
    const chosen = chosenWeek(c);
    const route = cell => weekRoute(c, (cell.interval || {}).from, (cell.interval || {}).to);
    return `<div class="s-chart__col"><div class="s-chart__head"><span class="s-chart__title">${esc(chartTitle(brand))}</span>`
      + (chosen ? `<button type="button" class="s-chart__clear" data-search="${esc(clear)}">Show all weeks</button>` : '') + `</div>`
      + chartSvg(model.dated, { geo: G.wide, value: model.value, route, chosen, clear, label: chartTitle(brand).replace(', by week', '') })
      + `<div class="s-chart__notes">${chosen ? '' : '<div class="s-meta">Click a week to see only its ads.</div>'}${caption(model.dated, { lastDay, only })}</div></div>`;
  }
  // Phone: the chart folded to one line; "Show chart" opens it (X18, R22).
  function phoneChart({ cells, brand, route, open = false, c = null, brandOwn = false, lastDay = null, only = '' }) {
    const model = chartModel(cells, { brandOwn, lastDay });
    if (!model) return '';
    const crit = c || E().searchCriteria();
    const chosen = chosenWeek(crit);
    const busiest = model.dated.reduce((a, x) => (model.value(x) > model.value(a) ? x : a), model.dated[0]);
    const part = chosen ? partialDays(model.dated.find(x => (x.interval || {}).from === crit.from) || { interval: { from: crit.from, to: crit.to } }) : null;
    const text = chosen ? (part ? `Showing ${part}` : `Showing the week of ${keepDate(dayName(chosen))}`) : `Busiest: week of ${keepDate(dayName((busiest.interval || {}).from))}, ${B().count(model.value(busiest), 'ad')}`;
    const bname = brand && typeof brand === 'object' ? brand : brand ? { display: brand } : null;
    const r = cell => weekRoute(crit, (cell.interval || {}).from, (cell.interval || {}).to);
    return `<div class="s-fold"><span class="s-fold__text">${esc(text)}</span><button type="button" class="s-btn-m" aria-expanded="${open}" data-b2-chart>${open ? 'Hide chart' : 'Show chart'}</button></div>`
      + `<div class="s-fold__chart"${open ? '' : ' hidden'}><span class="s-fold__title">${esc(chartTitle(bname))}</span>`
      + chartSvg(model.dated, { geo: G.phone, value: model.value, route: r, chosen, clear: E().searchURL({ ...crit, from: null, to: null, page: 1 }), label: chartTitle(bname).replace(', by week', '') })
      + `<div class="s-meta">Tap a week to see only its ads.</div>${caption(model.dated, { lastDay, only })}</div>`;
  }

  /* -- Head parts --------------------------------------------------------------- */
  // Walkthrough (SEARCH-7 7b): words typed before the page draws its box are kept on that route (the draft), and the
  // box takes focus back; a run search, or another route, drops them.
  let draft = null;
  const draftFor = () => (draft && draft.route === E().current.route ? draft : (draft = null));
  function refocusDraft() {
    const d = draftFor();
    const box = d && document.getElementById && document.getElementById('search-query');
    if (!box || box === d.el) return;
    d.el = box;
    if (box.value !== d.value) box.value = d.value;
    if (typeof box.focus === 'function') box.focus();
    if (typeof box.setSelectionRange === 'function') box.setSelectionRange(d.value.length, d.value.length);
  }
  const searchBox = c => {
    const ph = phone();
    const d = draftFor();
    return `<form id="search" class="s-search${ph ? ' s-search--phone' : ''}" role="search"><label class="visually-hidden" for="search-query">Search what podcasts say</label>`
      + `<input id="search-query" class="s-search__q${ph ? ' s-search__q--phone' : ''}" name="q" type="search" value="${esc(d ? d.value : c.q)}" placeholder="Search a brand, show or phrase" autocomplete="off">`
      + `<button class="${ph ? 's-search__go-m' : 's-search__go'}" type="submit">Search</button></form>`;
  };
  // Chips: the brand(s) with ×, the week, then "+ Show" and "+ Date"
  // (today's scope dialogs). "+ Has a clip" waits for a clip filter (S9).
  // Q3 (Landing final): "+ Show" and "+ Date" open a popover under the chip
  // holding only that filter (Clear / Apply); an applied filter's chip reads
  // "Show: {name}", "Shows: {n}" or "Date: Sep 1 – Sep 30" and opens it
  // again. Every applied chip's × is "Remove filter: {chip text}". On the
  // phone a chip opens today's Filters sheet at that filter.
  function chipsHtml(c, brands, shows) {
    const ph = phone();
    const named = key => (brands || []).find(b => fold(b.key) === fold(key)) || { key, display: key };
    const x = (label, attrs) => `<button type="button" class="${ph ? 's-chip__x-m' : 's-chip__x'}" aria-label="Remove filter: ${esc(label)}" ${attrs}>×</button>`;
    const all = patch => `data-search="${esc(E().searchURL({ ...c, ...patch, page: 1 }))}"`;
    const out = [];
    for (const key of (c.brands || [])) {
      const b = named(key);
      const mark = onlyUnit(b, B().hasLogo(b) ? B().brandMark(b, { cls: ph ? 's-logo--12' : 's-logo--13' }) : esc(b.display || key));
      const rm = x(`Brand: ${b.display || key}`, `data-action="remove-filter" data-key="brands" data-value="${esc(key)}"`);
      out.push(ph ? `<span class="s-chip-m">${mark}${rm}</span>` : `<span class="s-chip"><span class="s-chip__label">Brand</span>${mark}${rm}</span>`);
    }
    const scope = X().scopeChips({ ...c, brands: null, shows: null, from: null, to: null }, brands, shows, null);
    for (const [label, key, value] of scope) {
      out.push(`<span class="${ph ? 's-chip-m' : 's-chip'}">${esc(label)}${x(label, `data-action="remove-filter" data-key="${esc(key)}" data-value="${esc(value)}"`)}</span>`);
    }
    // An applied chip: its words open the filter again (desktop: the popover; phone: the sheet).
    const applied = (kind, group, label, patch) => {
      const open = ph ? `<button type="button" class="s-chip__open" data-action="filters" data-b2-group="${group}">${esc(label)}</button>`
        : `<button type="button" class="s-chip__open" data-b2-pop="${kind}" aria-haspopup="dialog" aria-expanded="false">${esc(label)}</button>`;
      const chip = `<span class="${ph ? 's-chip-m' : 's-chip'}">${open}${x(label, all(patch))}</span>`;
      return ph ? chip : `<span class="s-chipwrap">${chip}</span>`;
    };
    const plus = (kind, group, label) => (ph ? `<button type="button" class="s-pill-m" data-action="filters" data-b2-group="${group}">${label}</button>`
      : `<span class="s-chipwrap"><button type="button" class="s-pill" data-b2-pop="${kind}" aria-haspopup="dialog" aria-expanded="false">${label}</button></span>`);
    const chosen = Array.isArray(c.shows) ? c.shows.filter(Boolean) : [];
    const showChip = chosen.length ? applied('show', 'shows', showChipText(chosen, shows), { shows: null }) : plus('show', 'shows', '+ Show');
    const week = chosenWeek(c);
    let dateChip;
    if (week) {
      const label = weekPart(c) || `Week of ${dayName(week)}`;
      dateChip = `<span class="${ph ? 's-chip-m' : 's-chip'} s-chip--week">${esc(label)}${x(label, all({ from: null, to: null }))}</span>`;
    } else dateChip = c.from || c.to ? applied('date', 'dates', dateChipText(c.from, c.to), { from: null, to: null }) : plus('date', 'dates', '+ Date');
    out.push(showChip, dateChip);
    // S9: "+ Has a clip" is a plain toggle (Ads only), no popover; on, its chip reads "Has a clip". W2 (80ae14dfc): it and
    // "Show only ads with a clip" are one filter, both aria-pressed; pressing the applied chip turns it off.
    if ((c.mode || 'ads') === 'ads') {
      out.push(c.has_clip ? `<span class="${ph ? 's-chip-m' : 's-chip'}"><button type="button" class="s-chip__open" ${all({ has_clip: null })} aria-pressed="true">Has a clip</button>${x('Has a clip', all({ has_clip: null }))}</span>`
        : `<button type="button" class="${ph ? 's-pill-m' : 's-pill'}" ${all({ has_clip: true })} aria-pressed="false">+ Has a clip</button>`);
    }
    return ph ? `<div class="s-chips-m">${out.join('')}</div>` : `<div class="s-chips">${out.join('')}</div>`;
  }
  const showName = (id, shows) => {
    const hit = (shows || []).find(x => String(x.id) === String(id));
    return (hit && hit.display_title) || String(id);
  };
  const showChipText = (ids, shows) => (ids.length === 1 ? `Show: ${showName(ids[0], shows)}` : `Shows: ${num(ids.length)}`);
  const dateChipText = (from, to) => (from && to ? `Date: ${keepDate(resultDate(from))} – ${keepDate(resultDate(to))}` : from ? `Date: from ${keepDate(resultDate(from))}` : `Date: to ${keepDate(resultDate(to))}`);
  const isoDay = d => new Date(d).toISOString().slice(0, 10);
  const minusDays = (day, n) => isoDay(Date.parse(day + 'T00:00:00Z') - n * 86400000);
  // The popovers: markup, and the search Apply runs (the count updates on Apply).
  const POP = {
    items(shows, chosen, find) {
      const picked = new Set((chosen || []).map(String));
      const want = fold(find || '');
      const list = (shows || []).filter(x => x && x.id != null);
      const first = list.filter(x => picked.has(String(x.id)));
      const rest = list.filter(x => !picked.has(String(x.id)) && (!want || fold(x.display_title || x.id).includes(want))).slice(0, 50);
      return [...first, ...rest].map(x => `<label class="s-pop__item"><input class="s-check" type="checkbox" name="b2-show" value="${esc(x.id)}"${picked.has(String(x.id)) ? ' checked' : ''}>${esc(x.display_title || x.id)}</label>`).join('');
    },
    html(kind, c, shows, find = '') {
      const actions = '<div class="s-pop__actions"><button type="button" class="s-btn" data-b2-pop-clear>Clear</button><button type="button" class="s-cta" data-b2-pop-apply>Apply</button></div></div>';
      if (kind === 'show') {
        return '<div class="s-pop" role="dialog" aria-label="Show" data-b2-pop-panel="show">'
          + '<label class="s-pop__find">Find a show<input type="search" class="s-pop__input" data-b2-pop-find autocomplete="off"></label>'
          + `<div class="s-pop__list" data-b2-pop-list>${POP.items(shows, c.shows, find)}</div>` + actions;
      }
      const range = (v, words) => `<label class="s-pop__item"><input type="radio" name="b2-range" value="${v}">${words}</label>`;
      return '<div class="s-pop" role="dialog" aria-label="Date" data-b2-pop-panel="date">'
        + `<div class="s-pop__ranges">${range('7', 'Last 7 days')}${range('30', 'Last 30 days')}${range('all', 'All dates in this collection')}</div>`
        + `<div class="s-pop__dates"><label class="s-pop__field">From<input type="date" name="b2-from" value="${esc(c.from || '')}"></label>`
        + `<label class="s-pop__field">To<input type="date" name="b2-to" value="${esc(c.to || '')}"></label></div>` + actions;
    },
    dates(range, today) {
      if (range === 'all') return { from: null, to: null };
      const n = range === '7' ? 6 : 29;
      return { from: minusDays(today, n), to: today };
    },
    apply(kind, c, v = {}) {
      if (kind === 'show') return E().searchURL({ ...c, shows: v.shows && v.shows.length ? v.shows : null, page: 1 });
      const d = v.range ? POP.dates(v.range, v.today || isoDay(Date.now())) : { from: v.from || null, to: v.to || null };
      return E().searchURL({ ...c, ...d, page: 1 });
    },
  };
  let pop = null;
  function openPop(chip) {
    closePop(false);
    const kind = chip.dataset.b2Pop;
    chip.insertAdjacentHTML('afterend', POP.html(kind, E().searchCriteria(), lastShows, ''));
    const el = chip.nextElementSibling;
    chip.setAttribute('aria-expanded', 'true');
    pop = { kind, chip, el };
    const first = el && el.querySelector && el.querySelector('input');
    if (first && first.focus) first.focus();
  }
  function closePop(focus) {
    if (!pop) return;
    const { chip, el } = pop;
    pop = null;
    if (el && el.remove) el.remove();
    chip.setAttribute('aria-expanded', 'false');
    if (focus && chip.focus) chip.focus();
  }
  function readPop(p) {
    if (p.kind === 'show') return { shows: [...p.el.querySelectorAll('input[name="b2-show"]:checked')].map(b => b.value) };
    const field = n => { const f = p.el.querySelector(`input[name="${n}"]`); return f ? f.value : ''; };
    return { from: field('b2-from'), to: field('b2-to') };
  }
  function clearPop(el) {
    for (const b of el.querySelectorAll('input')) {
      if (b.type === 'checkbox' || b.type === 'radio') b.checked = false; else b.value = '';
    }
    const list = el.querySelector('[data-b2-pop-list]');
    if (list) list.innerHTML = POP.items(lastShows, [], '');
  }
  // Review 2 (search-60fb65a5): words that name a listed brand open on Ads
  // only, the brand's own ads (its route, so Save search, alerts and Sort
  // keep it); Any mention is that route without the brand, once chosen for
  // these words (Sort, pages and weeks keep it).
  const anyChosen = new Set();
  const wordsOf = route => fold(new URLSearchParams(String(route || '').split('?')[1] || '').get('q') || '');
  function opening(c, brands, route) {
    if ((c.mode || 'ads') !== 'ads' || Array.isArray(c.brands) || (anyChosen.has(wordsOf(route)) && !planA())) return null;
    const ctx = brandInPlay(c, brands);
    return ctx && !ctx.own ? E().searchURL(ownQuery(c, ctx.brand)) : null;
  }
  // Plan A (SEARCH-7): words that name a brand are plain Ads (the ads that say them), never the brand's Any mention.
  function brandContext(c, brands) {
    const ctx = brandInPlay(c, brands);
    return ctx && !ctx.own && planA() ? null : ctx;
  }
  // The brand in play: a one-brand filter (own), or words that name a listed brand.
  function brandInPlay(c, brands) {
    if ((c.mode || 'ads') !== 'ads') return null;
    if (Array.isArray(c.brands) && c.brands.length === 1) {
      const b = (brands || []).find(x => fold(x.key) === fold(c.brands[0]));
      return { brand: b || { key: c.brands[0], display: c.brands[0] }, own: true };
    }
    const q = String(c.q || '').trim();
    if (!q || q.includes('"') || (Array.isArray(c.brands) && c.brands.length)) return null;
    const b = X().namedBrand(q, brands);
    return b ? { brand: { ...b, display: b.display || b.brand || b.key }, own: false } : null;
  }
  const ownQuery = (c, b) => ({ ...c, mode: 'ads', brands: [b.key], page: 1 });
  const anyQuery = c => ({ ...c, mode: 'ads', q: String(c.q || '').trim(), brands: null, page: 1 });
  // K1 interim (lead 2026-10-31): the brand route with no words (Brand's "See all") has no Any mention. Its number was
  // the display name's typed words (Mobil 1: 0 next to Ads only 11); no read counts mentions by the brand filter.
  // S3 (RKS2-01): any brand filter, one or several, with no words.
  const wordless = c => Array.isArray(c.brands) && c.brands.length > 0 && !String(c.q || '').trim();
  // S3 (RKS2-01): Any mention with a brand filter and no words (an old Brand link, a saved search) counted the whole
  // collection (Mobil 1: 19,472). It opens as that route on Ads only, its other filters kept.
  const mentionsToAds = c => (c.mode === 'mentions' && wordless(c) ? { ...c, mode: 'ads', page: 1 } : null);
  // { own, any, on: 'own'|'any'|'ads'|'mentions', ctx } or null.
  function toggleModel(c, ctx, result, anyTotal) {
    const total = isNum(result.break_total) ? Number(result.break_total) : null;
    if (ctx) {
      if (ctx.own) return { on: 'own', own: total, any: isNum(anyTotal) ? Number(anyTotal) : null, ctx };
      return { on: 'any', own: isNum(result.brand_break_total) ? Number(result.brand_break_total) : null, any: total, ctx };
    }
    const mode = c.mode === 'mentions' ? 'mentions' : 'ads';
    return { on: mode, own: mode === 'ads' ? total : null, any: mode === 'mentions' && isNum(result.total) ? Number(result.total) : null, ctx: null };
  }
  function toggleHtml(t, c, { skeleton = false } = {}) {
    const ph = phone();
    const n = v => (skeleton ? ' · <span class="s-skel-num"></span>' : v == null ? '' : ` · ${num(v)}`);
    const btn = (label, v, on, attrs) => `<button type="button" class="${ph ? 's-seg__btn-m' : 's-seg__btn'}${on ? ' s-seg__btn--on' : ''}" aria-pressed="${on}" ${attrs}>${label}${n(v)}</button>`;
    const ctx = t.ctx;
    const ownOn = t.on === 'own' || t.on === 'ads';
    const ownAttr = ctx ? `data-search="${esc(E().searchURL(ownQuery(c, ctx.brand)))}"` : 'data-mode="ads"';
    const anyAttr = ctx ? `data-search="${esc(E().searchURL(anyQuery(c)))}" data-b2-any` : 'data-mode="mentions"';
    const extra = ctx && t.own != null && t.any != null && t.any > t.own ? t.any - t.own : 0;
    const name = ctx ? ctx.brand.display : '';
    const line = skeleton ? `<span class="${ph ? 's-skel-m__seg-note' : 's-skel__seg-note'}"></span>`
      : !extra ? ''
        : t.on === 'own' ? `<span class="s-meta${ph ? ' s-meta--center' : ''}">${extra === 1 ? `1 more is another ad or talk that names ${esc(name)}.` : `${num(extra)} more are other ads or talk that name ${esc(name)}.`}</span>`
          : `<span class="s-meta${ph ? ' s-meta--center' : ''}">Includes ${num(extra)} other ${extra === 1 ? 'ad or talk that names' : 'ads or talk that name'} ${esc(name)}.</span>`;
    // Plan A: Ads only alone, no Any mention and no line about other mentions (today's page hides All mentions).
    // K1: the wordless brand route too.
    if (planA() || wordless(c)) {
      const one = btn('Ads only', t.own, ownOn, ownAttr);
      return ph ? `<div class="s-segwrap-m"><div class="s-seg s-seg--grid s-seg--one">${one}</div></div>` : `<div class="s-segwrap"><span class="s-seg">${one}</span></div>`;
    }
    const seg = btn('Ads only', t.own, ownOn, ownAttr) + btn('Any mention', t.any, t.on === 'any' || t.on === 'mentions', anyAttr);
    return ph ? `<div class="s-segwrap-m"><div class="s-seg s-seg--grid">${seg}</div>${line}</div>`
      : `<div class="s-segwrap"><span class="s-seg">${seg}</span>${line}</div>`;
  }
  function headTitle(t, c, result) {
    const q = String(c.q || '').trim();
    const total = t.on === 'mentions' ? Number(result.total || 0) : Number(result.break_total || 0);
    const relation = result.total_relation === 'lower_bound' ? 'At least ' : '';
    // S10: the result's distinct episodes and shows, under an ads count.
    const reach = isNum(result.episode_total) && isNum(result.show_total)
      ? `in ${B().count(result.episode_total, 'episode')} across ${B().count(result.show_total, 'show')}` : '';
    if (t.on === 'own') return { title: `${relation}${B().count(total, t.ctx.brand.display + ' ad')}`, sub: reach };
    if (t.on === 'any') {
      const own = t.own, extra = own != null ? total - own : null;
      const name = t.ctx.brand.display;
      const sub = own == null || relation ? '' : !extra ? `All are ${name}’s own.`
        : `${num(own)} ${own === 1 ? 'is' : 'are'} ${name}’s own, plus ${num(extra)} other ${extra === 1 ? 'ad or talk that names' : 'ads or talk that name'} ${name}.`;
      // Talk is not an ad (Review call 2): Any mention counts results.
      return { title: `${relation}${num(total)} ${total === 1 ? 'result mentions' : 'results mention'} “${q}”`, sub };
    }
    if (t.on === 'mentions') return { title: `${relation}${B().count(total, 'mention')}${q ? ` of “${q}”` : ''}`, sub: '' };
    return { title: q ? `${relation}${num(total)} ${total === 1 ? 'ad mentions' : 'ads mention'} “${q}”` : `${relation}${B().count(total, 'ad')}`, sub: reach };
  }
  // JE-COVERAGE: each show row's Screened episodes (server) sum to the checked counts; a row without
  // them (an older server) leaves them out, so no "checked" claim shows (B().checked).
  const coverageCounts = shows => {
    if (!(Array.isArray(shows) && shows.length)) return null;
    const out = { shows: shows.length, episodes: shows.reduce((a, s) => a + (Number(s.episodes) || 0), 0) };
    if (shows.every(s => typeof s.screened_episodes === 'number')) {
      out.screened_episodes = shows.reduce((a, s) => a + s.screened_episodes, 0);
      out.screened_shows = shows.filter(s => s.screened_episodes > 0).length;
    }
    return out;
  };
  let lastCoverage = null;
  function coveragePhone(cov) {
    const k = B().checked(cov);
    if (!k) return '';
    return `<a class="s-coverage s-coverage--phone" href="/coverage/"><span class="s-checked-m"><span><b class="s-strong">${B().CHECK_MARK}Ads checked</b> in ${esc(B().checkedIn(k))}.</span></span></a>`;
  }
  // The phone head's brand page link, under the coverage line (recheck #10).
  // d25da7c04 (Review option b): on the phone, See the brand page, Save search and Create alert are one More menu.
  const moreMenu = ctx => '<details class="s-menu-m"><summary class="s-menu-m__btn">More</summary><div class="s-menu-m__list" role="menu">'
    + (ctx ? `<a class="s-menu-m__item" role="menuitem" href="#/brand/${esc(encodeURIComponent(ctx.brand.key))}">See the ${esc(ctx.brand.display)} page →</a>` : '')
    + '<button type="button" class="s-menu-m__item" role="menuitem" data-action="save-search">Save search</button>'
    + (alertOff(ctx) ? '' : '<button type="button" class="s-menu-m__item" role="menuitem" data-action="create-alert">Create alert</button>') + '</div></details>';
  // PODS (UX 2026-10-28): no brand alert on a model_only brand's own ads (it would never fire).
  const alertOff = ctx => !!(ctx && ctx.own && ctx.brand.model_only === true);
  // PODS: the heading's brand name with its label, only for a model_only brand (its own ads' count names it).
  const titleHtml = (head, t) => {
    const html = esc(head.title);
    if (t.on !== 'own' || !U().modelOnlyMark(t.ctx.brand)) return html;
    const name = esc(t.ctx.brand.display);
    const at = html.indexOf(name);
    return at < 0 ? html : html.slice(0, at) + onlyUnit(t.ctx.brand, name) + html.slice(at + name.length);
  };

  /* -- The page frame ------------------------------------------------------------- */
  // Rows by ad: one row per ad the server listed (its brand-naming read,
  // today's adAttachItem pick), loose reads each their own row.
  function rowsOf(items, adBreaks) {
    if (!Array.isArray(adBreaks)) return items.map((item, index) => ({ item, index }));
    const used = new Set(), at = new Map(items.map((x, i) => [x.ref, i]));
    const out = [];
    for (const b of adBreaks) {
      let idx = items.map((x, i) => (x.ad_break_id === b.id ? i : -1)).filter(i => i >= 0);
      if (!idx.length) idx = (b.refs || []).map(r => at.get(r)).filter(i => i !== undefined);
      idx = idx.filter(i => !used.has(i));
      if (!idx.length) continue;
      idx.forEach(i => used.add(i));
      const pick = X().adAttachItem(b, idx.map(i => items[i]));
      out.push({ item: pick, index: items.indexOf(pick) });
    }
    items.forEach((item, index) => { if (!used.has(index)) out.push({ item, index }); });
    return out;
  }
  function frame(d) {
    const { c, result, brands, shows, view, pick, panelLoading, selection } = d;
    const ph = phone();
    const q = String((d.query || c).q || '');
    const items = result.items || [];
    const cov = coverageCounts(shows) || lastCoverage;
    const ctx = brandContext(c, brands);
    // A chosen week filters the list; the head, toggle and chart stay the whole scope's.
    const scope = (d.scope && d.scope.result) || result;
    const t = toggleModel(c, ctx, scope, d.anyTotal);
    const head = headTitle(t, c, scope);
    const perPage = result.per_page || 10;
    const ads = Array.isArray(result.ad_breaks) && isNum(result.break_total);
    const shownTotal = t.on === 'mentions' || !ads ? Number(result.total || 0) : Number(result.break_total);
    const rows = rowsOf(items, ads ? result.ad_breaks : null);
    const page = c.page || 1;
    const pages = Math.max(1, Math.ceil(shownTotal / perPage));
    const first = shownTotal ? (page - 1) * perPage + 1 : 0;
    const last = Math.min(shownTotal, (page - 1) * perPage + rows.length);
    const week = chosenWeek(c);
    const brandOwn = !!(ctx && !ctx.own && isNum(scope.brand_break_total));
    const lastDay = d.lastDay || null;
    const model = t.on === 'mentions' ? null : chartModel(((d.scope || d.bundle || {}).summary || {}).week, { brandOwn, lastDay });
    const only = brandOwn && model && model.brandOwn ? ctx.brand.display : '';
    if (model) weekCells = model.dated;
    const brandName = ctx ? ctx.brand : null;
    const ticked = new Set(selection || []);
    const inside = t.on === 'any' || t.on === 'mentions';
    const clipN = ads && isNum(result.ads_with_clips) ? Number(result.ads_with_clips) : 0;
    // 4c6e1c3ac (recheck 3 #8): on Any mention the line speaks of results.
    const clip = t.on === 'any' ? B().clipLine(clipN, shownTotal).replace(/^Every ad shows/, 'Every result shows') : B().clipLine(clipN, shownTotal);
    const sortSel = `<select class="s-sort__sel${ph ? ' s-sort__sel--phone' : ''}" id="search-sort"><option value="newest"${c.sort !== 'oldest' ? ' selected' : ''}>Newest first</option><option value="oldest"${c.sort === 'oldest' ? ' selected' : ''}>Oldest first</option></select>`;
    const quiet = '<button type="button" class="s-btn-quiet" data-action="save-search">Save search</button>' + (alertOff(ctx) ? '' : '<button type="button" class="s-btn-quiet" data-action="create-alert">Create alert</button>');
    // PODS P4: the model-named ads among the rows shown (Ads; Any mention counts its ads), under the count.
    const p4 = t.on === 'mentions' ? '' : U().modelSummary(rows.map(r => U().shownBrands((facts(r.item).read) || {})));
    const brandLink = ctx ? `<a class="s-btn" href="#/brand/${esc(encodeURIComponent(ctx.brand.key))}">See the ${esc(ctx.brand.display)} page →</a>` : '';
    const logo = ctx && B().hasLogo(ctx.brand) ? B().brandMark(ctx.brand, { cls: 's-logo--head' }) : '';
    // W2 (Landing 80ae14dfc): the clip line and its button sit under the chips, beside the toggle, above the chart.
    const clipRow = t.on === 'mentions' ? '' : ph ? `<div class="s-hint-m">${esc(clip)}</div>`
      : `<div class="s-hint"><span class="s-hint__text">${esc(clip)}</span>${clipButton(c, t.on === 'any' ? 'results' : 'ads')}</div>`;
    const top = searchBox(c) + (ph ? chipsHtml(c, brands, shows) + clipRow + toggleHtml(t, c)
      : `<div class="s-filters"><div class="s-filters__main">${chipsHtml(c, brands, shows)}${clipRow}</div>${toggleHtml(t, c)}</div>`);
    if (ph) {
      const list = rows.map(r => phoneRowHtml({ ...r, q, inside })).join('');
      return `<div class="s-main s-main--phone">${top}<div class="s-stack s-stack--tight">`
        + `<div class="s-head__main-m"><h1 class="s-head__title s-head__title--phone">${titleHtml(head, t)}</h1>${head.sub ? `<div class="s-muted">${esc(head.sub)}</div>` : ''}${cov ? coveragePhone(cov) : ''}</div>`
        + (model ? phoneChart({ cells: model.dated, brand: brandName, c, brandOwn, lastDay, only }) : '')
        + `<div class="s-stack s-stack--tight"><div class="s-bar-m"><span class="s-bar__count-m">Showing ${num(first)}–${num(last)} of ${num(shownTotal)}</span>`
        + `<label class="s-sort s-sort--phone">Sort${sortSel}</label>${moreMenu(ctx)}</div>${p4}`
        + `<div id="search-results"><div class="s-list">${list}</div></div>`
        + (page < pages ? `<button type="button" class="s-more-m" data-action="page-next" data-b2-more="${page + 1}">Show 10 more</button>` : '')
        + `</div></div></div>`;
    }
    const current = pick == null || pick < 0 ? -1 : pick;
    const one = view === 'cards' ? cardHtml : rowHtml;
    const listBody = rows.map(r => one({ ...r, q, inside, ticked: ticked.has(r.item.ref), current: r.index === current })).join('');
    const tickHint = X().canExport() ? (view === 'cards' ? '<div class="s-cards__hint">Tick to export.</div>' : '<div class="s-list__hint">Tick to export.</div>') : '';
    const listHead = view === 'cards' ? '' : `<div class="s-list__head"><span></span><span></span><span>What the host said</span><span class="s-list__when">When</span></div>`;
    // c9ea045ca (recheck 4 #4): the Cards pager has no fill.
    const foot = `<div class="${view === 'cards' ? 's-cards__foot' : 's-list__foot'}">${page > 1 ? '<button type="button" class="s-btn" data-action="page-prev">← Previous</button>' : ''}<span class="s-grow"></span>`
      + `<span class="s-muted">Page ${num(page)} of ${num(pages)}</span>${page < pages ? `<button type="button" class="s-btn" data-action="page-next">Next ${num(Math.min(perPage, shownTotal - page * perPage))} →</button>` : ''}</div>`;
    const results = view === 'cards'
      ? `${tickHint}<div class="s-cards">${listBody}</div>${foot}`
      : `<div class="s-list">${tickHint}${listHead}${listBody}${foot}</div>`;
    const selected = current >= 0 ? items[current] : null;
    return `<div class="s-main">${top}<div class="s-stack">`
      + `<section class="s-head"><div class="s-head__main">${logo}<h1 class="s-head__title">${titleHtml(head, t)}${head.sub ? `<span class="s-head__sub">${esc(head.sub)}</span>` : ''}</h1>`
      + `<div class="s-head__facts">${cov ? B().coverageLink(cov) : ''}${brandLink}</div></div>`
      + wideChart({ model, brand: brandName, c, lastDay, only, clear: E().searchURL({ ...c, from: null, to: null, page: 1 }) }) + `</section>`
      + `<div class="s-results"><div class="s-results__list" aria-label="Search results">`
      // 4c6e1c3ac (recheck 3 #5): each unit ends in a comma, so no line starts with "·".
      + `<div class="s-bar"><span class="s-muted"><span class="s-nowrap"><b class="s-showing__n">Showing ${num(first)}–${num(last)} of ${num(shownTotal)}</b>,</span>`
      + (week ? ` <span class="s-nowrap">${esc(weekPart(c) || `week of ${dayName(week)}`)},</span>` : '')
      + ` <span class="s-nowrap">${c.sort === 'oldest' ? 'oldest' : 'newest'} first</span></span><span class="s-grow"></span>`
      + (t.on === 'mentions' ? '' : `<span class="s-view"><button type="button" class="s-view__btn" aria-pressed="${view !== 'cards'}" data-b2-view="list">List</button><button type="button" class="s-view__btn" aria-pressed="${view === 'cards'}" data-b2-view="cards">Cards</button></span>`)
      + `<label class="s-sort">Sort${sortSel}</label><span class="s-toolbar__sep"></span>${quiet}</div>${p4}`
      + `<div class="s-selbar-slot" data-b2-selbar>${selbar(ticked.size)}</div>`
      + `<div id="search-results">${results}</div></div>`
      + `<aside class="ew-inspector s-panel" aria-label="Selected result"><p class="visually-hidden" role="status" aria-live="polite" data-inspector-live>${esc(inspectorLive(selected))}</p>`
      + `<div class="s-panel__body" data-b2-panel>${panelHtml(selected, { loading: panelLoading, q, clip: current >= 0 ? (view === 'cards' ? 'c' : 'r') + current : null })}</div></aside>`
      + `</div></div></div>`;
  }

  // W2 (80ae14dfc): "Show only ads with a clip" switches the same filter as the "+ Has a clip" chip; never wraps.
  const clipButton = (c, what, { disabled = false } = {}) => `<button type="button" class="s-btn s-btn--nowrap" aria-pressed="${c.has_clip ? 'true' : 'false'}"`
    + (disabled ? ' disabled' : ` data-search="${esc(E().searchURL({ ...c, has_clip: c.has_clip ? null : true, page: 1 }))}"`) + `>Show only ${what} with a clip</button>`;

  /* -- Empty, failed, loading ------------------------------------------------------- */
  function emptyHtml(c, { shows, brands, earliest = null }) {
    const ph = phone();
    const q = String(c.q || '').trim();
    const cov = coverageCounts(shows) || lastCoverage;
    // Collection words (Reno ~17:45Z; Landing 849c650c4 + 399bed463): "going back to {year}" from S8
    // coverage.earliest_episode, never a range; a current-year start reads "going back to {Mon D}". Without S8 no start.
    const back = goingBack(earliest);
    // JE-COVERAGE: "all" only when every episode was checked (B().collectionSentence, Overview's words).
    const k = cov && B().checked(cov);
    const checked = cov ? B().collectionSentence(k, `<a class="s-coverage-inline" href="/coverage/">${k && k.all ? 'all ' : ''}${esc(B().count(cov.episodes, 'episode'))} from ${esc(B().count(cov.shows, 'show'))}</a>`, back ? `, going back to ${esc(back)}` : '') : '';
    const top = [...(brands || [])].filter(b => b && b.key && !b.under_review && !b.hidden).sort((a, b) => (Number(b.ad_breaks) || 0) - (Number(a.ad_breaks) || 0)).slice(0, 3);
    const mention = E().searchURL({ mode: 'mentions', q, page: 1 });
    // Review calls 6, 7: the brand in play, else the words in title case (the sample's "Ferncastle").
    const ctx = brandContext(c, brands);
    const named = ctx ? ctx.brand.display : q.replace(/(^|\s)(\p{Ll})/gu, (m, a, b) => a + b.toUpperCase());
    const alertName = named ? named + ' ' : '';
    return `<div class="s-main${ph ? ' s-main--phone' : ''}">${searchBox(c)}${ph ? chipsHtml(c, brands, shows) : `<div class="s-filters">${chipsHtml(c, brands, shows)}</div>`}`
      + `<section class="s-empty"><h1 class="s-empty__title">${q ? `No ads found for “${esc(q)}”` : 'No ads found'}</h1>`
      + `<p class="s-empty__text">${checked}We found no ad that names it. Check the spelling${planA() ? '.' : ', or look for any mention of the word.'}</p>`
      + `<div class="s-empty__actions">${q && !planA() ? `<a class="s-cta" href="${esc(mention)}">Search any mention of “${esc(q)}”</a>` : ''}`
      + `<button type="button" class="s-btn s-btn--xl" data-action="create-alert">Alert me to new ${esc(alertName)}ads</button></div>`
      + (top.length ? `<div class="s-empty__more"><span class="s-empty__more-title">Or try a brand we hear often</span><div class="s-empty__brands">`
        + top.map((b, i) => `<a class="s-empty__brand" href="${esc(E().searchURL({ mode: 'ads', brands: [b.key], page: 1 }))}">${onlyUnit(b, B().hasLogo(b) ? B().brandMark(b, { cls: 's-logo--' + (14 + i) }) : esc(b.display || b.key))}</a>`).join('')
        + `</div></div>` : '')
      + `</section></div>`;
  }
  // Plan A: a plan-limited answer is not an empty search (today's page: PLAN_COPY.mentionsNotIncluded, views.js:1225).
  function planLimitedHtml(c, { brands, shows } = {}) {
    const ph = phone();
    const ads = E().searchURL({ ...c, mode: 'ads', page: 1 });
    return `<div class="s-main${ph ? ' s-main--phone' : ''}">${searchBox(c)}${ph ? chipsHtml(c, brands, shows) : `<div class="s-filters">${chipsHtml(c, brands, shows)}</div>`}`
      + `<section class="s-empty"><h1 class="s-empty__title" id="plan-limited-why">${esc(X().PLAN_COPY.mentionsNotIncluded)}</h1>`
      + `<div class="s-empty__actions"><a class="s-cta" href="${esc(ads)}">${esc(X().PLAN_COPY.searchAds)}</a></div></section></div>`;
  }
  function failedHtml(c, message, { invalid = false, brands, shows } = {}) {
    return `<div class="s-main${phone() ? ' s-main--phone' : ''}">${searchBox(c)}${phone() ? chipsHtml(c, brands, shows) : `<div class="s-filters">${chipsHtml(c, brands, shows)}</div>`}`
      + `<div class="ew-state ew-state-error ew-search-error" role="alert"><span class="ew-state-kind">! Search didn’t finish</span>`
      + `<h2>${invalid ? 'This scope was not applied' : 'We couldn’t complete this search'}</h2>`
      + `<p>${esc((invalid ? 'This scope is invalid and was not applied: ' : 'Search is unavailable: ') + message)}</p>`
      + `<p>This is not “no results”. Your search and filters are kept.</p>`
      + `<div class="actions">${E().button('retry', 'Retry', 'primary')}${E().button('reset-filters', 'Reset scope', 'quiet')}</div></div></div>`;
  }
  // Page loading (R8): the controls and the coverage line stay; every number is a skeleton.
  function loadingHtml(route) {
    const prev = E().current.route;
    E().current.route = route;
    let c;
    try { c = E().searchCriteria(); } finally { E().current.route = prev; }
    if (c.mode === 'episodes') return null;
    c = mentionsToAds(c) || c;
    // A refresh of the frame on screen keeps today's retained-frame path.
    const kept = E().retained('lastSearch');
    if (kept && kept.route === route && (kept.ws || '') === (E().current.workspaceContext || '')) return null;
    const ph = phone();
    // Walkthrough (SEARCH-7 7c): the bare open (the nav link) picks its route first (the last search, the Settings, a
    // brand's own); it draws the box and chips only, so no placeholder rows show before the spec's loading skeleton.
    if (route === '#/search') return `<div class="s-main${ph ? ' s-main--phone' : ''}" aria-busy="true">${searchBox(c)}${ph ? chipsHtml(c, lastBrands, null) : `<div class="s-filters">${chipsHtml(c, lastBrands, null)}</div>`}</div>`;
    const q = String(c.q || '').trim();
    const brands = lastBrands;
    const ctx = brandContext(c, brands);
    const cov = lastCoverage;
    const t = { on: ctx ? (ctx.own ? 'own' : 'any') : c.mode === 'mentions' ? 'mentions' : 'ads', ctx };
    // 7c: no words, no quotes ("Searching {n} episodes…").
    const note = q ? `Searching ${cov ? B().count(cov.episodes, 'episode') + ' ' : ''}for “${q}”…` : `Searching${cov ? ' ' + B().count(cov.episodes, 'episode') : ''}…`;
    // W2: the clip line's skeleton (and its button, still) under the chips.
    const top = searchBox(c) + (ph ? chipsHtml(c, brands, null) + '<div class="s-hint-m"><span class="s-skel-m__line"></span></div>' + toggleHtml(t, c, { skeleton: true })
      : `<div class="s-filters"><div class="s-filters__main">${chipsHtml(c, brands, null)}<div class="s-hint"><span><span class="s-skel__clipline"></span></span>${clipButton(c, t.on === 'any' ? 'results' : 'ads', { disabled: true })}</div></div>${toggleHtml(t, c, { skeleton: true })}</div>`);
    const brandLink = ctx ? `<a class="s-btn" href="#/brand/${esc(encodeURIComponent(ctx.brand.key))}">See the ${esc(ctx.brand.display)} page →</a>` : '';
    if (ph) {
      const row = '<div class="s-skel-m__row"><div class="s-skel-m__cover"></div><div class="s-skel-m__lines"><div class="s-skel__line"></div><div class="s-skel-m__l1"></div><div class="s-skel-m__l2"></div></div></div>';
      return `<div class="s-main s-main--phone" aria-busy="true">${top}<div class="s-stack s-stack--tight"><div class="s-head__main-m"><span class="s-skel-m__h1"></span><span class="s-skel-m__line"></span>${cov ? coveragePhone(cov) : ''}</div>`
        + `<div class="s-fold"><span class="s-grow"><span class="s-skel-m__line"></span></span><button type="button" class="s-btn-m" aria-expanded="false" disabled>Show chart</button></div>`
        + `<div class="s-stack s-stack--tight"><div class="s-bar-m"><span class="s-grow"><span class="s-skel-m__showing"></span></span><label class="s-sort s-sort--phone">Sort<select class="s-sort__sel s-sort__sel--phone" disabled><option>${c.sort === 'oldest' ? 'Oldest first' : 'Newest first'}</option></select></label>${moreMenu(ctx)}</div>`
        + `<div class="s-skel-m" role="status"><div class="s-skel-m__note">${esc(note)}</div>${row.repeat(4)}</div></div></div></div>`;
    }
    const view = new URLSearchParams(String(route).split('?')[1] || '').get('view') === 'cards' ? 'cards' : 'list';
    const row = '<div class="s-skel__row"><div class="s-skel__tick"></div><div class="s-skel__cover"></div><div class="s-skel__lines"><div class="s-skel__line"></div><div class="s-skel__sub"></div></div><div class="s-skel__when"></div></div>';
    const logo = ctx && B().hasLogo(ctx.brand) ? onlyUnit(ctx.brand, B().brandMark(ctx.brand, { cls: 's-logo--head' })) : '';
    return `<div class="s-main" aria-busy="true">${top}<div class="s-stack"><section class="s-head"><div class="s-head__main">${logo}<div class="s-skel__head"><span class="s-skel__h1"></span><span class="s-skel__h1-sub"></span></div>`
      + `<div class="s-head__facts">${cov ? B().coverageLink(cov) : ''}${brandLink}</div></div>`
      + `<div class="s-chart__col"><div class="s-chart__head"><span class="s-chart__title">${esc(chartTitle(ctx && ctx.brand))}</span></div><div class="s-skel__chart"></div></div></section>`
      + `<div class="s-results"><div class="s-results__list"><div class="s-bar"><span class="s-skel__showing"></span><span class="s-grow"></span>`
      + `<span class="s-view"><button type="button" class="s-view__btn" aria-pressed="${view !== 'cards'}" disabled>List</button><button type="button" class="s-view__btn" aria-pressed="${view === 'cards'}" disabled>Cards</button></span>`
      + `<label class="s-sort">Sort<select class="s-sort__sel" disabled><option>${c.sort === 'oldest' ? 'Oldest first' : 'Newest first'}</option></select></label><span class="s-toolbar__sep"></span>`
      + `<button type="button" class="s-btn-quiet" data-action="save-search">Save search</button><button type="button" class="s-btn-quiet" data-action="create-alert">Create alert</button></div>`
      + `<div role="status"><div class="s-list"><div class="s-skel__note">${esc(note)}</div>${row.repeat(6)}</div></div></div>`
      + `<div class="s-skel__panel" aria-hidden="true"><div class="s-skel__text"></div><div class="s-skel__text"></div><div class="s-skel__text s-skel__text--70"></div></div></div></div></div>`;
  }

  /* -- render ---------------------------------------------------------------------- */
  // The Overview read holds the collection's range: S8 coverage.earliest_episode and newest_episode.date (the data's
  // last day; views.js's rule). Kept per workspace and data revision; a failed read is no range (null).
  // RSEARCH-R1-04: a read still pending when its render's signal aborted is never reused (its rejection may land
  // after a newer search asks); a landed read is kept whatever its signal.
  const realDay = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && (t => Number.isFinite(t.getTime()) && t.toISOString().slice(0, 10) === v)(new Date(v + 'T12:00:00Z'));
  let keptOverview = null;
  function overviewRead(signal) {
    const key = (E().current.workspaceContext || '') + '|' + (E().current.displayedRevision || '');
    const k = keptOverview;
    if (k && k.key === key && (k.landed || !(k.signal && k.signal.aborted))) return k.read;
    const read = E().api('/customer/overview', { signal }).catch(() => null);
    const entry = { key, read, signal, landed: false };
    keptOverview = entry;
    read.then(v => { entry.landed = v !== null; if (v === null && keptOverview === entry) keptOverview = null; });
    return read;
  }
  let lastBrands = null;
  let redirectedFrom = null;
  let lastShows = null;
  // The page's own state: the current read's items and the panel's pick.
  const state = { route: '', items: [], rows: [], q: '', token: null };
  async function viewFor(c) {
    const v = new URLSearchParams(String(E().current.route || '').split('?')[1] || '').get('view');
    if (v === 'list' || v === 'cards') return v;
    // List is the default; a saved "cards" preference keeps Cards (SPEC 5).
    const wait = new Promise(r => setTimeout(() => r(null), 250));
    const prefs = await Promise.race([E().preferences().catch(() => null), wait]);
    return prefs && prefs.results_view === 'cards' && c.mode === 'ads' ? 'cards' : 'list';
  }
  // UX task test r2 #3: the address bar never holds a search's words (app.js PRIVATE_QUERY), so coming back (the
  // Search tab, Back) arrives at a bare #/search. It reopens this person's last search in this workspace, words and
  // mode. Kept in this page's memory only, never stored.
  let lastSearch = null;
  const restoreRoute = (route, last, who) => (route === '#/search' && last && last.who === who ? last.route : null);
  const whoKey = () => `${E().current.principal || ''}|${E().current.workspaceContext || ''}`;
  async function render(token, old) {
    const back = restoreRoute(E().current.route, lastSearch, whoKey());
    if (back) { E().go(back, true); return ''; }
    const c0 = E().searchCriteria();
    if (c0.mode === 'episodes') return episodesView(token, old);
    const prefRoute = await X().searchPrefsRoute();
    if (prefRoute && E().current.route === '#/search') { E().current.searchPrefNote = prefRoute; E().go(prefRoute, true); return ''; }
    const read = E().searchCriteria();
    if (read.mode === 'episodes') return episodesView(token, old);
    let selector = null;
    try { selector = E().searchSelector(); } catch { return old(token); }
    // S3 (RKS2-01): replace the route once; a route that cannot change still draws as Ads only.
    const asAds = !selector && read.ids === null && mentionsToAds(read);
    if (asAds && redirectedFrom !== E().current.route) { redirectedFrom = E().current.route; E().go(E().searchURL(asAds), true); return ''; }
    const c = asAds || read;
    // No words and no brand on Any mention: today's prompt (D-6).
    const needsWords = c.mode === 'mentions' && !String(c.q || '').trim() && !Array.isArray(c.brands) && !selector && c.ids === null;
    if (needsWords) return old(token);
    // Ads only first: once per route, so a route that cannot change (a test, an embed) still draws.
    if (!selector && c.ids === null) {
      const route = E().current.route;
      const known = lastBrands || await E().sharedRead('/customer/brands', {}).then(r => r.brands || [], () => null);
      if (E().current.route !== route) return '';
      if (Array.isArray(known)) lastBrands = known;
      const own = opening(c, known, route);
      if (own && redirectedFrom !== route) { redirectedFrom = route; E().go(own, true); return ''; }
    }
    E().current.filters = c;
    const epoch = E().authorityEpoch();
    const { signal, live } = X().guardedFetch(token);
    const refresh = E().current.cohortRefresh === true;
    E().current.cohortRefresh = false;
    const resultRead = X().apiBundle({ query: c, selector }, ['week'], { signal, refresh });
    const bundleRead = resultRead.then(out => ({ out }), err => ({ err }));
    const namesRead = Promise.all([
      E().sharedRead('/customer/brands', { signal, authorizedBy: resultRead }).then(r => r.brands || []).catch(() => null),
      E().sharedRead('/customer/shows', { signal, authorizedBy: resultRead }).then(r => r.shows || []).catch(() => null),
    ]);
    const viewRead = viewFor(c);
    const ovRead = overviewRead(signal);
    let [brands, shows] = await namesRead;
    if (!live()) return;
    if (Array.isArray(brands)) lastBrands = brands;
    if (Array.isArray(shows)) lastShows = shows;
    if (coverageCounts(shows)) lastCoverage = coverageCounts(shows);
    const ctx = brandContext(c, brands);
    // The other count of the toggle (Ads only shows the brand's own; the
    // Any mention count is its own read) and, with a week chosen, the
    // whole chart: same scope, no week.
    const week = !!chosenWeek(c) && !selector;
    const whole = week ? { ...c, from: null, to: null, page: 1 } : c;
    const anyRead = ctx && ctx.own && !selector && !wordless(c)
      ? X().apiBundle({ query: anyQuery(whole), selector: null }, [], { signal }).then(o => (o.result || {}).break_total, () => null) : Promise.resolve(null);
    const scopeRead = week
      ? X().apiBundle({ query: whole, selector: null }, ['week'], { signal }).then(o => o, () => null) : Promise.resolve(null);
    const { out: bundle, err } = await bundleRead;
    if (!live()) return;
    if (err) {
      if (err.droppedContext) return '';
      if (err.status === 409 || selector) return old(token);
      const conflict = X().scopeConflictText(err.message || '');
      if (conflict) return old(token);
      const invalid = /invalid|unknown query|unsupported|must be|requires/i.test(err.message || '');
      return shell(failedHtml(c, err.message || String(err), { invalid, brands, shows }));
    }
    const result = bundle.result || {};
    const query = bundle.query || c;
    const perPage = result.per_page || 10;
    const ads = Array.isArray(result.ad_breaks) && isNum(result.break_total);
    const shownTotal = c.mode === 'mentions' || !ads ? Number(result.total || 0) : Number(result.break_total);
    const pages = Math.max(1, Math.ceil(shownTotal / perPage));
    if (c.page > pages && shownTotal) { E().go(E().searchURL({ ...c, page: pages }, { selector })); return E().loadingBlock('Opening page…'); }
    E().current.displayedRevision = bundle.revision || '';
    E().notePlan(result, { result: true });
    // Plan A: a plan-limited answer is not an empty search.
    if (result.plan_limited === true) {
      lastSearch = { who: whoKey(), route: E().current.route };
      return shell(planLimitedHtml(c, { brands, shows }));
    }
    const items = result.items || [];
    const [anyTotal, scopeBundle, view] = await Promise.all([anyRead, scopeRead, viewRead]);
    if (!live()) return;
    E().retain('searchItems', { route: E().current.route, items, q: query.q || '', mode: query.mode || 'ads' }, epoch);
    E().retain('lastSearchTotal', { route: E().current.route, total: Number(result.total || 0), relation: result.total_relation === 'lower_bound' ? 'at least ' : '',
      ads: ads ? Number(result.break_total) : null, excerptsOnly: result.export_excerpts_only === true }, epoch);
    const ov = await ovRead;
    if (!live()) return;
    const earliest = ov && ov.coverage && realDay(ov.coverage.earliest_episode) ? ov.coverage.earliest_episode : null;
    const lastDay = ov && ov.newest_episode && realDay(ov.newest_episode.date) ? ov.newest_episode.date : null;
    if (!items.length) {
      lastSearch = { who: whoKey(), route: E().current.route };
      const html = shell(emptyHtml(c, { shows, brands, earliest }));
      E().retain('lastSearch', { route: E().current.route, html, mode: query.mode, q: query.q || '', revision: bundle.revision || '', ws: E().current.workspaceContext || '' }, epoch);
      return html;
    }
    const inspect = E().current.searchInspect;
    const pick = inspect && inspect.route === E().current.route && inspect.index < items.length ? inspect.index : rowsOf(items, ads ? result.ad_breaks : null)[0].index;
    E().current.searchInspect = { route: E().current.route, index: pick };
    const selected = pick >= 0 ? items[pick] : null;
    const panelLoading = !phone() && needsRecord(selected);
    Object.assign(state, { route: E().current.route, items, q: query.q || '', token, inside: !!ctx ? !ctx.own : c.mode === 'mentions' });
    lastSearch = { who: whoKey(), route: E().current.route };
    const html = shell(frame({ c, query, result, bundle, scope: scopeBundle, brands, shows, view, pick, panelLoading, anyTotal, lastDay,
      selection: X().selectionFor(E().current.route).ids }));
    E().retain('lastSearch', { route: E().current.route, html, mode: query.mode, q: query.q || '', revision: bundle.revision || '', ws: E().current.workspaceContext || '' }, epoch);
    if (panelLoading) loadRecord(pick, token);
    const note = X().prefNote();
    return note ? note + html : html;
  }

  // Q5 (Landing final): today's Episodes view, no new words, inside the new
  // head (search box, chips, h1 area, coverage line). Today's page draws it;
  // its own head is left out. Anything else today's page draws stays whole.
  // RSEARCH-R1-06: today's Episodes week strip ("By week · Aug 31 to Oct 25") ends on the server's last week start + 6.
  // With the data's last day (Overview's newest_episode.date) inside those six days, the caption ends on it. Pure.
  function weekEnd(html, lastDay) {
    if (!lastDay) return html;
    const ends = new Set([1, 2, 3, 4, 5, 6].map(n => E().date(new Date(Date.parse(lastDay + 'T00:00:00Z') + n * 86400000).toISOString().slice(0, 10))));
    return String(html).replace(/(By week · [^"<·]*? to )([^"<·]+?)(?= ·|"|<)/g, (m, a, b) => (ends.has(b) ? a + E().date(lastDay) : m));
  }
  async function episodesView(token, old) {
    const ovRead = overviewRead();
    const raw = await old(token);
    const at = typeof raw === 'string' ? raw.indexOf('<div class="ew-search-body') : -1;
    if (at < 0) return raw;
    const ov = await ovRead;
    const html = weekEnd(raw, ov && ov.newest_episode && realDay(ov.newest_episode.date) ? ov.newest_episode.date : null);
    const read = path => E().sharedRead(path, {}).catch(() => null);
    const [b, sh] = await Promise.all([read('/customer/brands'), read('/customer/shows')]);
    const brands = (b && b.brands) || lastBrands;
    const shows = (sh && sh.shows) || lastShows;
    if (Array.isArray(shows)) lastShows = shows;
    if (coverageCounts(shows)) lastCoverage = coverageCounts(shows);
    const c = E().searchCriteria();
    const ph = phone();
    const cov = coverageCounts(shows) || lastCoverage;
    const t = { on: 'episodes', own: null, any: null, ctx: null };
    const top = searchBox(c) + (ph ? chipsHtml(c, brands, shows) + toggleHtml(t, c) : `<div class="s-filters">${chipsHtml(c, brands, shows)}${toggleHtml(t, c)}</div>`);
    const head = ph
      ? `<div class="s-head__main-m"><h1 class="s-head__title s-head__title--phone">Episodes</h1>${cov ? coveragePhone(cov) : ''}</div>`
      : `<section class="s-head s-head--episodes"><div class="s-head__main"><h1 class="s-head__title">Episodes</h1><div class="s-head__facts">${cov ? B().coverageLink(cov) : ''}</div></div></section>`;
    return shell(`<div class="s-main${ph ? ' s-main--phone' : ''}">${top}${head}<div class="s-episodes">${html.slice(at)}</div></div>`);
  }

  /* -- Selecting a row into the panel ---------------------------------------------- */
  // A row with a clip whose clip block is not on the item (S4 missing).
  const needsRecord = item => !!(item && item.read && item.read.has_clip === true && item.read.media === undefined && item.record_kind !== 'transcript_passage');
  const records = new Map();
  async function loadRecord(index, token) {
    const item = state.items[index];
    if (!item) return;
    try {
      const out = records.get(item.ref) || await E().api('/customer/records/' + encodeURIComponent(item.ref));
      records.set(item.ref, out);
      const media = ((out && out.record) || out || {}).media;
      item.read = { ...item.read, media: media && media.playback === 'local' ? media : null };
    } catch { item.read = { ...item.read, media: null }; }
    if (E().current.fetchToken !== token && token != null) return;
    const cur = E().current.searchInspect;
    if (cur && cur.route === state.route && cur.index === index) paintPanel(index);
  }
  function paintPanel(index) {
    if (typeof document === 'undefined' || !document.querySelector) return;
    const body = document.querySelector('[data-b2-panel]');
    const item = index >= 0 ? state.items[index] : null;
    const loading = needsRecord(item);
    const clip = ['r', 'c'].map(k => k + index).find(id => document.getElementById('clip-' + id)) || null;
    if (body) { body.innerHTML = panelHtml(item, { loading, q: state.q, clip }); syncPanelPlay(body, clip); }
    const live = document.querySelector('[data-inspector-live]');
    if (live) live.textContent = inspectorLive(item);
    for (const el of document.querySelectorAll('.s-row[data-index], .s-card[data-index]')) {
      const on = Number(el.dataset.index) === index;
      el.classList.toggle(el.classList.contains('s-card') ? 's-card--current' : 's-row--current', on);
    }
    if (loading) loadRecord(index, E().current.fetchToken);
  }
  /* Walkthrough 1 (SEARCH-7 7d): opening the Ad page stops the Search clip (one player). The point it reached is left in
     EW.current.clipHandoff = { ref, at: seconds into the episode, t: when }, kept fresh while it plays, so the Ad page
     can start its own player there (the Ad page's side is App's). A clip the member paused hands nothing over. */
  function clipPoint(audio) {
    const m = /\/customer\/records\/([^/?#]+)\/media/.exec(String((audio && audio.getAttribute && audio.getAttribute('src')) || ''));
    if (!m) return null;
    const box = audio.closest ? audio.closest('[data-clip-id]') : null;
    const offset = Number(box && box.dataset && box.dataset.offset) || 0;
    return { ref: decodeURIComponent(m[1]), at: Math.round((offset + (Number(audio.currentTime) || 0)) * 10) / 10 };
  }
  function noteClip(event) {
    const a = event && event.target;
    // Search's clips only: the Ad page's own player never writes it.
    if (!a || !E() || !E().current || !/^#\/search\b/.test(String(E().current.route || ''))) return;
    if (event.type === 'pause') { if (a.isConnected && E().current.clipHandoff) E().current.clipHandoff = null; return; }
    if (a.paused) return;
    const p = clipPoint(a);
    if (p) E().current.clipHandoff = { ...p, t: Date.now() };
  }
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('timeupdate', noteClip, true);
    document.addEventListener('pause', noteClip, true);
  }
  // A panel painted while its row's clip plays starts as Pause (the engine changes buttons only on its own events).
  function syncPanelPlay(body, clip) {
    const audio = clip && document.getElementById('clip-' + clip);
    if (!audio || audio.paused || !body.querySelectorAll) return;
    for (const b of body.querySelectorAll('[data-b2-play]')) {
      b.classList.add('is-playing');
      b.setAttribute('aria-pressed', 'true');
      b.innerHTML = B().playInner(b, true);
    }
  }
  function select(index) {
    E().current.searchInspect = { route: E().current.route, index };
    paintPanel(index);
  }
  // 4c6e1c3ac (UX recheck 3 #1): the Preview panel shows from 1300 px.
  const wideEnough = () => !(window.matchMedia && window.matchMedia('(max-width: 1299px)').matches);

  /* -- Add to my campaign: today's dialog (campaignAttachDialog: its model,
        checks and submit) in the reference's s-dialog look and AD-DECISIONS
        item 4's words, with Review calls 1 and 5 (2026-10-30): with no
        campaign the dialog has one exit, "Start a campaign", the D3 sentence
        and no note. Pure: today's dialog markup in, the page's out. ------- */
  // UX item 6 (REDESIGN-AD-WORDS round 2): FR-11's greyed rows (another member's campaign: disabled, no value; a
  // browser reads the attribute back as disabled="").
  const THEIRS_RE = /<label><input type="radio" name="choice" disabled(="")?><span>([\s\S]*?)<\/span><\/label>/g;
  const THEIRS_LINE = 'Only the member who made a campaign can add to it. Start a campaign of your own, then add this ad to it.';
  // UX D35: item 6's heading is the dialog's own (EWB2.addWhy.WORDS.theirsTitle); its line stays above the rows.
  const THEIRS_HEAD = `<div class="s-dialog__none" data-theirs><span class="s-meta--faint">${THEIRS_LINE}</span></div>`;
  const KEEP_NOTE = 'Adding an ad keeps it with the campaign. It doesn’t mark it as checked.';
  const D3 = 'Your campaigns hold the ad buys you’re checking: did each booked read run, with the right code?';
  const NONE_RE = /<p class="section-note">No (?:campaign placement|research target) covers this episode\.<\/p>/;
  // The research case (UX m4, final) is the shared EWB2.addWhy, in this page's look.
  const WHY_LOOK = { title: 's-dialog__title', none: 's-dialog__none', meta: 's-meta--faint', foot: 's-dialog__actions', cancel: 's-dialog__cancel', second: 's-dialog__second', cta: 's-cta' };
  const START = '<a class="s-cta" href="#/campaigns">Start a campaign</a>';
  function dialogHtml(html, { adding = '', research = [], cfa = false } = {}) {
    const s = String(html);
    const bodyAt = s.indexOf('<div class="dialog-body">');
    const footAt = s.indexOf('<div class="dialog-footer">');
    const formEnd = s.lastIndexOf('</form>');
    if (bodyAt < 0 || formEnd < 0) return s;
    const none = NONE_RE.test(s);
    const submit = footAt > 0 && /type="submit"/.test(s.slice(footAt, formEnd));
    const head = s.slice(0, bodyAt).replace(/<form id="attach-to-campaign" novalidate(="")?>/, '<form id="attach-to-campaign" novalidate$1 class="s-dialog">');
    const addingHtml = adding ? `<div class="s-dialog__adding"><span class="s-chip__label">Adding:</span> <span class="s-strong">${esc(adding)}</span></div>` : '';
    // UX m4 (cases 2 and 3): the member's own open research campaigns, none covering the episode: the shared words
    // and buttons, no campaign rows.
    // SEARCH-7 item 2: a row of the member's own (already attached too) is never the research case (shared rule).
    const why = submit ? [] : B().addWhy.list(s, research);
    if (why.length) return head.replace(/<div class="dialog-head">[\s\S]*$/, '') + B().addWhy.html(why, WHY_LOOK, { adding: addingHtml, cfa }) + s.slice(formEnd);
    // UX item 6: another member's campaign (FR-11's greyed rows: disabled, no value) says who can add to it.
    const theirsOnly = !submit && (s.match(THEIRS_RE) || []).length > 0 && !/name="choice" value=/.test(s);
    // UX D35: item 6 has its own heading; otherwise "Add to my campaign".
    const title = theirsOnly ? B().addWhy.WORDS.theirsTitle : B().addWhy.WORDS.noneTitle;
    const top = head.replace(/<div class="dialog-head">[\s\S]*$/, `<h2 class="s-dialog__title" id="dialog-title">${esc(title)}</h2>` + addingHtml);
    let body = s.slice(bodyAt + '<div class="dialog-body">'.length, (footAt > 0 ? footAt : formEnd) - '</div>'.length)
      // m4 #5: a row is the campaign's name, its status, and for another member's the note (EWB2.addWhy.row).
      .replace(THEIRS_RE, (_, d, t) => `<label><input type="radio" name="choice" disabled${d || ''}><span>${B().addWhy.row(t, { theirs: true })}</span></label>`)
      .replace(/(<label><input type="radio" name="choice" value=[^>]*>)<span>([\s\S]*?)<\/span><\/label>/g, (_, a, t) => `${a}<span>${B().addWhy.row(t)}</span></label>`)
      .replace(/<fieldset class="radio-group"><legend>[^<]*<\/legend>/g, '<div class="s-skel__lines" role="radiogroup" aria-label="Where to keep it"><span class="s-empty__more-title">Where to keep it</span>')
      .replace(/<label><input type="radio" name="choice"([^>]*)>/g, (_, a) => `<label class="s-dialog__camp${/\schecked\b/.test(a) ? ' s-dialog__camp--on' : ''}"><input class="s-radio" type="radio" name="choice"${a}>`)
      .replace(/<\/fieldset>/g, '<a class="s-dialog__new" href="#/campaigns">Start a campaign</a></div>')
      .replace(NONE_RE, `<div class="s-dialog__none"><span class="s-dialog__none-title">None of your campaigns follows this episode yet.</span><span class="s-meta--faint">${esc(D3)}</span></div>`)
      .replace(/<p><a class="text-link" href="#\/campaigns">Go to Campaigns<\/a><\/p>/g, '')
      .replace(/<p class="section-note">A source reference only points to a passage;[^<]*<\/p>/, none ? '' : `<span class="s-meta--faint">${esc(KEEP_NOTE)}</span>`)
      .replace(/<p class="section-note">/g, '<p class="s-meta">');
    // Only other members' greyed rows with nothing to choose: UX item 6's words above them, one exit.
    if (theirsOnly) body = body.replace('<a class="s-dialog__new" href="#/campaigns">Start a campaign</a></div>', '</div>').replace('<div class="s-skel__lines" role="radiogroup"', THEIRS_HEAD + '<div class="s-skel__lines" role="radiogroup"');
    const out = top + body + '<div class="s-dialog__actions"><button class="s-dialog__cancel" type="button" data-action="close-dialog">Cancel</button>'
      + (submit ? '<button class="s-cta" type="submit">Add to my campaign</button>' : theirsOnly ? '<a class="s-cta" href="#/campaigns">Start your own campaign</a>' : START)
      + '</div>' + s.slice(formEnd);
    // Part B (lead 10-30, as the Ad page): for a caller who may make campaigns, every "Start a campaign" exit and item
    // 6's "Start your own campaign" become the shared "Start a campaign with this ad" (EWB2.cfa); item 6's line carries the ad.
    if (!cfa) return out;
    const btn = cls => `<button class="${cls}" type="button" data-b2-cfa>${esc(B().cfa.WORDS.start)}</button>`;
    return out
      .replace(START, btn('s-cta'))
      .replace('<a class="s-cta" href="#/campaigns">Start your own campaign</a>', btn('s-cta'))
      .replace('<a class="s-dialog__new" href="#/campaigns">Start a campaign</a>', btn('s-dialog__new'))
      .replace(THEIRS_LINE, esc(B().cfa.WORDS.theirsLine));
  }
  // Pure: the campaigns read landed after the restyle: the same dialog as if it had come first; null when there is
  // nothing to add (no research, already painted).
  function whyRepaint(raw, current, { adding = '', research = [], cfa = false } = {}) {
    if (!research.length || /data-why/.test(String(current))) return null;
    const next = dialogHtml(raw, { adding, research, cfa });
    return /data-why/.test(next) ? next : null;
  }
  // Read with the press, hosted only (campaignAttachDialog's researchOn), as the Ad page.
  let research = [], names = [];
  let rawDialog = null;
  // Pure (App Search-5b, as the Ad page's 8403f99a): the member's own open research campaigns (can_change); another
  // member's belong to item 6 (EWB2.addWhy.WORDS.theirsTitle). A viewer changes none, so a viewer's are every open one.
  function ownResearch(campaigns, role) {
    const open = c => c && c.mode === 'research' && c.availability === 'active' && (role === 'viewer' || c.can_change !== false);
    return (campaigns || []).filter(open).map(c => ({ id: c.id, name: c.name || `Campaign ${c.id}`, viewer: c.can_change === false }));
  }
  function readResearch() {
    research = []; names = [];
    if (!E().current.hosted) return;
    const asked = adding;
    Promise.resolve().then(() => E().api('/customer/campaigns')).then(r => {
      if (adding === null || adding !== asked) return;
      research = ownResearch((r && r.campaigns) || [], E().current.workspaceRole);
      // Every name in the workspace, for "Start a campaign with this ad"'s "(2)".
      names = ((r && r.campaigns) || []).map(c => c && c.name).filter(Boolean);
      paintWhy();
    }, () => { /* the dialog says what failed */ });
  }
  function paintWhy() {
    const dlg = typeof document !== 'undefined' && document.getElementById && document.getElementById('modal');
    // Once "Start a campaign with this ad" is open, a late read never draws over it.
    if (!dlg || rawDialog === null || adding === null || made) return;
    const next = whyRepaint(rawDialog, dlg.innerHTML, { adding, research, cfa: B().cfa.may(E().current) });
    if (next === null) return;
    dlg.innerHTML = next;
    const styled = dlg.querySelector('form#attach-to-campaign');
    if (styled) styled.classList.add('s-dialog');
    const first = dlg.querySelector('.s-dialog__actions .s-cta');
    if (first && first.focus) { try { first.focus(); } catch { /* keep focus */ } }
  }
  // UX task test r2 #4: today's save disables the submit and writes "Saving…" (views.js saveIn); while the add runs it
  // reads "Adding…". Today's restore after an error (its own label) stands.
  function busyStep(btn) {
    if (btn && btn.disabled && btn.textContent === 'Saving…') btn.textContent = 'Adding…';
  }
  function watchBusy(form) {
    const btn = form.querySelector && form.querySelector('button[type="submit"]');
    if (!btn || typeof MutationObserver !== 'function' || (btn.dataset && btn.dataset.b2Busy)) return;
    if (btn.dataset) btn.dataset.b2Busy = '1';
    new MutationObserver(() => busyStep(btn)).observe(btn, { childList: true, characterData: true, subtree: true, attributes: true });
  }
  // Today's opener draws the dialog; the page restyles it once it is open.
  let adding = null;
  let watchingDialog = false;
  function restyleDialog() {
    const dlg = document.getElementById('modal');
    const form = dlg && dlg.querySelector('form#attach-to-campaign');
    if (!form || adding === null || /\bs-dialog\b/.test(form.className || '')) return;
    rawDialog = dlg.innerHTML;
    dlg.innerHTML = dialogHtml(rawDialog, { adding, research, cfa: B().cfa.may(E().current) });
    // The mark that stops the observer, whatever the markup read back as.
    const styled = dlg.querySelector('form#attach-to-campaign');
    if (styled) styled.classList.add('s-dialog');
    dlg.classList.add('s-modal');
    dlg.addEventListener('close', () => { dlg.classList.remove('s-modal'); adding = null; rawDialog = null; made = null; }, { once: true });
    const first = dlg.querySelector('input[name="choice"]:checked') || dlg.querySelector('input[name="choice"]:not([disabled])') || dlg.querySelector('button, a[href]');
    if (first && first.focus) { try { first.focus(); } catch { /* keep focus */ } }
  }
  /* Part B: "Start a campaign with this ad" in the same dialog (EWB2.cfa, shared with the Ad page). The ad is the
     Add button's own data; the dialog keeps the steps done, so "Try again" resumes. */
  let addAd = null, made = null;
  const CFA_LOOK = { form: 's-dialog', title: 's-dialog__title', meta: 's-meta--faint', field: 's-dialog__field', label: 's-chip__label', input: 's-dialog__input',
    foot: 's-dialog__actions', cancel: 's-dialog__cancel', cta: 's-cta', error: 's-dialog__error', link: 's-dialog__new' };
  function paintMade(focusSel) {
    const dlg = typeof document !== 'undefined' && document.getElementById && document.getElementById('modal');
    if (!dlg || adding === null || !made) return;
    dlg.innerHTML = B().cfa.html(addAd, made, CFA_LOOK);
    const el = focusSel && dlg.querySelector(focusSel);
    if (el && el.focus) { try { el.focus(); } catch { /* keep focus */ } }
  }
  function startMade() {
    if (!addAd || adding === null) return;
    made = B().cfa.state(addAd, names);
    paintMade('input[name="name"]');
  }
  // AD-FIX2 RA-R1-02 (as the Ad page's d0c332ee/2fc870eb): today's dialog guard before each write: the workspace or the
  // displayed switch revision moved, or the session changed (authority epoch) since the dialog opened. The form is the
  // one the dialog holds now (each paint draws a new one).
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
    // Refused before anything was saved: the dialog closes with the words. No answer (RA-R1-01): held with today's
    // UNCONFIRMED line and Reload workspace (EWB2.cfa draws it), never resent.
    if (st.refused) { E().closeDialog(); E().toast(st.refused); return; }
    paintMade(st.done ? '[role="status"]' : st.held ? '[role="alert"]' : '[type="submit"]');
  }
  function watchDialog() {
    const dlg = typeof document !== 'undefined' && document.getElementById && document.getElementById('modal');
    if (watchingDialog || typeof MutationObserver !== 'function' || !dlg) return;
    watchingDialog = true;
    new MutationObserver(restyleDialog).observe(dlg, { childList: true });
  }

  /* -- Paging (NOGAP SR row 60: a page turn opens at the top) ----------------------- */
  function turnPage(delta) {
    const c = E().searchCriteria();
    let selector = null;
    try { selector = E().searchSelector(); } catch { return false; }
    const dest = E().searchURL({ ...c, page: Math.max(1, c.page + delta) }, { selector });
    E().current.scroll.delete(dest);
    E().current.focus.delete(dest);
    E().go(dest);
    return true;
  }
  // Phone "Show 10 more": the next page's rows join the list.
  async function showMore(btn) {
    const c = E().searchCriteria();
    const next = Number(btn.dataset.b2More) || c.page + 1;
    let selector = null;
    try { selector = E().searchSelector(); } catch { return; }
    btn.disabled = true;
    try {
      const out = await X().apiBundle({ query: { ...c, page: next }, selector }, []);
      const result = out.result || {};
      const items = result.items || [];
      const ads = Array.isArray(result.ad_breaks) && isNum(result.break_total);
      const base = state.items.length;
      state.items = state.items.concat(items);
      const rows = rowsOf(items, ads ? result.ad_breaks : null).map(r => ({ ...r, index: r.index + base }));
      const list = document.querySelector('#search-results .s-list');
      if (list) list.insertAdjacentHTML('beforeend', rows.map(r => phoneRowHtml({ ...r, q: state.q, inside: state.inside })).join(''));
      const total = ads ? Number(result.break_total) : Number(result.total || 0);
      const shown = document.querySelectorAll('#search-results .s-prow').length;
      const line = document.querySelector('.s-bar__count-m');
      if (line) line.textContent = `Showing 1–${num(shown)} of ${num(total)}`;
      if (shown >= total) btn.remove();
      else { btn.dataset.b2More = String(next + 1); btn.disabled = false; }
    } catch (err) {
      btn.disabled = false;
      E().toast('More results didn’t load: ' + (err.message || err));
    }
  }

  /* -- Events (capture; true stops today's handler) -------------------------------- */
  const CONTROL = 'a, input, label, select, textarea, summary, details, [data-action]:not([data-action="inspect"])';
  function click(event) {
    const t = event.target;
    if (!t || !t.closest) return false;
    // A More item closes its menu; today's handler runs the action.
    const moreItem = t.closest('.s-menu-m__item');
    if (moreItem) { const menu = moreItem.closest('details.s-menu-m'); if (menu) menu.removeAttribute('open'); return false; }
    // Any mention, once chosen, stays for its route (today's handler navigates).
    const anyBtn = t.closest('[data-b2-any]');
    if (anyBtn) { anyChosen.add(wordsOf(anyBtn.getAttribute('data-search'))); return false; }
    // Q3: the phone chips open today's Filters sheet at their filter.
    const group = t.closest('[data-b2-group]');
    if (group) { E().current.filtersLastGroup = group.dataset.b2Group; return false; }
    const opener = t.closest('[data-b2-pop]');
    if (opener) {
      if (pop && pop.chip === opener) closePop(true); else openPop(opener);
      return true;
    }
    if (pop) {
      if (t.closest('.s-pop')) {
        if (t.closest('[data-b2-pop-apply]')) { const url = POP.apply(pop.kind, E().searchCriteria(), readPop(pop)); closePop(false); E().go(url); return true; }
        if (t.closest('[data-b2-pop-clear]')) { clearPop(pop.el); return true; }
        return false;
      }
      // A click outside closes it, nothing applied; the click goes on.
      closePop(!t.closest('a, button, input, select, textarea, label, summary'));
    }
    if (t.closest('[data-b2-clear]')) { setTicks([]); return true; }
    // Add to my campaign: today's handler opens the dialog; the page restyles it.
    const add = t.closest('[data-action="add-to-campaign"][data-adding]');
    if (add) {
      adding = add.getAttribute('data-adding') || ''; rawDialog = null; made = null;
      const at = k => add.getAttribute(k) || '';
      addAd = { ref: at('data-ref'), episode: at('data-episode'), episodeName: at('data-episode-name'), showName: at('data-show-name'), brand: at('data-brand-name'), at: at('data-at') };
      readResearch(); watchDialog(); return false;
    }
    if (t.closest('[data-b2-cfa]')) { startMade(); return true; }
    const viewBtn = t.closest('[data-b2-view]');
    if (viewBtn) {
      let selector = null;
      try { selector = E().searchSelector(); } catch { return false; }
      const c = E().searchCriteria();
      E().go(E().searchURL({ ...c, view: viewBtn.dataset.b2View }, { selector }));
      return true;
    }
    const chartBtn = t.closest('[data-b2-chart]');
    if (chartBtn) {
      const open = chartBtn.getAttribute('aria-expanded') !== 'true';
      chartBtn.setAttribute('aria-expanded', String(open));
      chartBtn.textContent = open ? 'Hide chart' : 'Show chart';
      const chart = document.querySelector('.s-fold__chart');
      if (chart) chart.hidden = !open;
      return true;
    }
    const more = t.closest('[data-b2-more]');
    if (more && phone()) { showMore(more); return true; }
    const turn = t.closest('[data-action="page-next"], [data-action="page-prev"]');
    if (turn) return turnPage(turn.dataset.action === 'page-next' ? 1 : -1);
    // Phone rows open the ad; Play inside plays.
    const prow = t.closest('.s-prow[data-href]');
    if (prow && !t.closest('button, a, summary, details, input, label')) { E().go(prow.dataset.href); return true; }
    const row = t.closest('.s-row[data-index], .s-card[data-index]');
    if (!row) return false;
    const index = Number(row.dataset.index);
    // Below 1300 px there is no panel: the row opens the ad page.
    if (!wideEnough()) {
      if (t.closest(CONTROL) || t.closest('button')) return false;
      const item = state.items[index];
      if (item) E().go('#/evidence/' + encodeURIComponent(item.ref));
      return true;
    }
    // Play selects the row too, then plays (today's clip-toggle runs).
    const play = t.closest('[data-action="clip-toggle"], [data-b2-select]');
    if (play) { select(index); return !!play.hasAttribute('data-b2-select'); }
    // The tick box and other controls keep their own behaviour; today's
    // inspector must not take the click (it would paint the old panel).
    if (t.closest(CONTROL)) return !!t.closest('label.s-row__tick, label.s-card__tick, input[name="selected_ref"]');
    select(index);
    return true;
  }
  function setTicks(ids) {
    const route = E().current.route;
    X().setSelection(route, ids);
    const set = new Set(ids);
    if (typeof document === 'undefined' || !document.querySelectorAll) return;
    for (const box of document.querySelectorAll('input[name="selected_ref"]')) {
      box.checked = set.has(box.value);
      const row = box.closest && box.closest('.s-row, .s-card');
      if (row) row.classList.toggle(row.classList.contains('s-card') ? 's-card--ticked' : 's-row--ticked', box.checked);
    }
    const slot = document.querySelector('[data-b2-selbar]');
    if (slot) slot.innerHTML = selbar(set.size);
  }
  function change(event) {
    const t = event.target;
    // A range fills From and To (Apply runs them).
    if (pop && t && t.name === 'b2-range') {
      const d = POP.dates(t.value, isoDay(Date.now()));
      for (const [n, v] of [['b2-from', d.from], ['b2-to', d.to]]) { const f = pop.el.querySelector(`input[name="${n}"]`); if (f) f.value = v || ''; }
      return true;
    }
    if (pop && t && (t.name === 'b2-from' || t.name === 'b2-to')) {
      for (const r of pop.el.querySelectorAll('input[name="b2-range"]')) r.checked = false;
      return true;
    }
    if (!t || t.name !== 'selected_ref') return false;
    const route = E().current.route;
    const boxes = [...document.querySelectorAll('input[name="selected_ref"]')];
    const pageIds = new Set(boxes.map(b => b.value));
    const kept = X().selectionFor(route).ids.filter(id => !pageIds.has(id));
    setTicks([...kept, ...boxes.filter(b => b.checked).map(b => b.value)]);
    return true;
  }
  function keydown(event) {
    const t = event.target;
    if (pop && event.key === 'Escape') { if (event.preventDefault) event.preventDefault(); closePop(true); return true; }
    if (!t || !t.closest || (event.key !== 'Enter' && event.key !== ' ')) return false;
    const bar = t.closest('.s-chart__hit[data-search]');
    if (bar) { event.preventDefault(); E().go(bar.getAttribute('data-search')); return true; }
    const prow = t.closest('.s-prow[data-href]');
    if (prow && t === prow) { event.preventDefault(); E().go(prow.dataset.href); return true; }
    // RSEARCH-R1-03: a focused list row (not a control inside it) does what its click does.
    const row = t.closest('.s-row[data-index], .s-card[data-index]');
    if (!row || t !== row) return false;
    event.preventDefault();
    const index = Number(row.dataset.index);
    if (wideEnough()) { select(index); return true; }
    const item = state.items[index];
    if (item) E().go('#/evidence/' + encodeURIComponent(item.ref));
    return true;
  }

  // A typed search starts fresh (UX task test r2 #3): words that name a brand open on Ads only again. The Add
  // dialog's submit shows its busy words.
  function submit(event) {
    const f = event.target;
    if (!f || !f.getAttribute) return false;
    const id = f.getAttribute('id');
    if (id === 'search') {
      const box = f.querySelector && f.querySelector('input[name="q"]');
      draft = null;
      if (box) anyChosen.delete(fold(box.value));
      // 7b: Enter on an empty box with no brand would list every ad: nothing runs.
      const c = E().searchCriteria();
      if (box && !String(box.value || '').trim() && !(c.brands || []).length) { if (event.preventDefault) event.preventDefault(); return true; }
    }
    if (id === 'attach-to-campaign' && f.closest && f.closest('#modal')) watchBusy(f);
    if (id === 'campaign-from-ad') { if (event.preventDefault) event.preventDefault(); submitMade(f); return true; }
    return false;
  }
  // "Find a show" narrows the list; ticked shows stay on top.
  function input(event) {
    const t = event.target;
    if (t && t.id === 'search-query') { draft = { route: E().current.route, value: String(t.value || ''), el: t }; return false; }
    if (!pop || !t || !t.hasAttribute || !t.hasAttribute('data-b2-pop-find')) return false;
    const list = pop.el.querySelector('[data-b2-pop-list]');
    const ticked = [...pop.el.querySelectorAll('input[name="b2-show"]:checked')].map(b => b.value);
    if (list) list.innerHTML = POP.items(lastShows, ticked, t.value);
    return true;
  }

  // The reference's page shell: phone rules hang off .s-page--phone.
  const shell = html => (html ? `<div class="s-page${phone() ? ' s-page--phone' : ''}">${html}</div>` : html);
  function loading(route) { try { return shell(loadingHtml(route)); } catch { return null; } }

  // Pure parts, for the render tests.
  window.EWSearchB2 = Object.freeze({ axisPlan, cutRoot, isOver, frame, rowHtml, cardHtml, phoneRowHtml, panelHtml, selbar, phoneChart, quoteHtml, fitText, fitFront, cutQuote, spreadText, dialog: dialogHtml, whyRepaint, weekEnd, clipPoint, noteClip, busyStep, restoreRoute, ownResearch, rangeText, popover: { html: POP.html, apply: POP.apply }, opening, emptyHtml, loadingHtml, turnPage, rowsOf });
  watchQuotes();
  window.EWB2.page('search', {
    on: ON,
    render: (token, old) => render(token, old),
    loading,
    handlers: { click, change, keydown, input, submit },
  });
})();
