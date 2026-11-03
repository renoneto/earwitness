/* EarWitness Saved Research collections (EWSaved).
   Saved excerpts/queries/bookmarks/reports/notes rendering plus B2
   draft/request construction. No authority of its own: every mutation
   body matches the server contract (no actor, tenant, source text or
   verification fields) and the caller owns dispatch, revisions and
   conflict recovery. No boot, no fetch on module load. */
(() => {
  'use strict';

  const TABS = ['excerpts', 'queries', 'bookmarks', 'reports', 'notes'];
  const TAB_TYPE = { excerpts: 'excerpt', queries: 'query', bookmarks: 'bookmark' };

  /* One canonical route parser for q/cursor/tab: URLSearchParams
     decodes + as space and %2B as a literal plus, so filter text and
     cursors round-trip exactly. Never regex + decodeURIComponent. */
  function routeParams(route) {
    try { return new URLSearchParams(String(route || '').split('?')[1] || ''); }
    catch { return new URLSearchParams(); }
  }
  function tabFromRoute(route) {
    const tab = routeParams(route).get('tab') || 'excerpts';
    return TABS.includes(tab) ? tab : 'excerpts';
  }

  function savedURL(tab, extra = {}) {
    const p = new URLSearchParams();
    p.set('tab', tab);
    for (const [k, v] of Object.entries(extra)) {
      if (v !== '' && v !== undefined && v !== null) p.set(k, String(v));
    }
    const qs = p.toString();
    return '#/saved' + (qs ? '?' + qs : '');
  }

  function qs(params) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (v !== '' && v !== undefined && v !== null) p.set(k, String(v));
    }
    const s = p.toString();
    return s ? '?' + s : '';
  }

  /* -- request construction ---------------------------------------------- */
  function checkDraft(type, draft) {
    const d = draft || {};
    // A tracked brand is named by its brand page: no title to check.
    if (type === 'bookmark' && d.kind === 'brand') {
      if (!d.id || typeof d.id !== 'string') return 'Tracking needs the brand.';
      if (d.note && String(d.note).length > 2000) return 'Keep the note within 2000 characters.';
      return '';
    }
    if (!d.title || !String(d.title).trim()) return 'Name this saved item so you can find it later.';
    if (String(d.title).length > 120) return 'Keep the title within 120 characters.';
    if (d.note && String(d.note).length > 2000) return 'Keep the note within 2000 characters.';
    if (type === 'excerpt') {
      if (!d.ref) return 'An excerpt needs the displayed source reference.';
    } else if (type === 'query') {
      if (!d.query || typeof d.query !== 'object') return 'A saved query needs its complete criteria.';
    } else if (type === 'bookmark') {
      if (!d.kind) return 'A bookmark needs its identity (show, episode or creative).';
      if (d.kind === 'episode' && !d.id) return 'An episode bookmark needs its episode identity.';
      if ((d.kind === 'show' || d.kind === 'creative') && !d.id) return 'This bookmark needs its saved identity.';
    } else {
      return `Unknown saved type: ${type}.`;
    }
    return '';
  }

  /* Allowed B2 create body: typed target only. The server captures
     source text, identity and provenance; the client never sends them,
     and never sends actor/tenant/authority fields. */
  function createRequest(type, draft) {
    const d = draft || {};
    const problem = checkDraft(type, d);
    if (problem) throw new Error(problem);
    if (type === 'bookmark' && d.kind === 'brand') {
      return { type, target: { kind: 'brand', id: d.id }, note: String(d.note || '') };
    }
    const body = { type, title: String(d.title).trim(), note: String(d.note || '') };
    if (type === 'excerpt') {
      body.target = { ref: d.ref };
      if (d.start_s !== undefined && d.start_s !== null && d.start_s !== '') body.target.start_s = Number(d.start_s);
      if (d.end_s !== undefined && d.end_s !== null && d.end_s !== '') body.target.end_s = Number(d.end_s);
    } else if (type === 'query') {
      body.query = d.query;
      if (d.selector) body.selector = d.selector;
    } else {
      body.target = { kind: d.kind, id: d.id };
      if (d.kind === 'episode') {
        if (d.source_ref) body.target.source_ref = d.source_ref;
        if (d.transcript_version_id !== undefined && d.transcript_version_id !== null) {
          body.target.transcript_version_id = d.transcript_version_id;
        }
      }
    }
    return body;
  }

  /* Editable fields plus the displayed revision for PUT. Replacing an
     excerpt source/bounds or a bookmark target is rejected here with
     guidance to create a new item: the server refuses rewrites that
     would change captured history. */
  function editDraft(item) {
    const it = item || {};
    return {
      id: it.id, type: it.type, title: it.title || '', note: it.note || '',
      revision: it.revision, target: it.target || null,
      query: it.query || null, selector: it.selector || null,
    };
  }

  function updateRequest(item, draft) {
    const it = item || {};
    const d = draft || {};
    // Replacing an excerpt's source or a bookmark's target would rewrite
    // captured history: refuse with guidance to create a new item. Only
    // title/note (and query definitions) are editable here.
    if (it.type === 'excerpt' && (d.ref !== undefined && d.ref !== (it.target || {}).ref)) {
      throw new Error('An excerpt keeps its pinned source. Create a new excerpt for a different source.');
    }
    if (it.type === 'bookmark' && ((d.kind !== undefined && d.kind !== (it.target || {}).kind)
        || (d.id !== undefined && d.id !== (it.target || {}).id))) {
      throw new Error('A bookmark keeps its saved identity. Create a new bookmark for a different target.');
    }
    const problem = checkDraft(it.type, { ...d, ref: d.ref || (it.target || {}).ref,
      kind: d.kind || (it.target || {}).kind, id: d.id || (it.target || {}).id,
      query: d.query || it.query });
    if (problem) throw new Error(problem);
    return {
      item: { title: String(d.title).trim(), note: String(d.note || ''),
        query: it.type === 'query' ? (d.query || it.query) : undefined,
        selector: it.type === 'query' && (d.selector || it.selector) ? (d.selector || it.selector) : undefined },
      expected_revision: it.revision,
    };
  }

  /* -- collection rendering ------------------------------------------------ */
  /* Availability comes from the server projection (current /
     earlier_copy / retained_historical / source_changed / unavailable)
     with a full-sentence reason, printed as is. A denied source stays denied: the row shows the denial,
     never a blank quotation presented as success. */
  function availabilityTag(item) {
    const U = window.EWUI;
    // Unknown stays unknown: no availability from the server, no tag.
    const state = item.availability;
    if (!state) return '';
    if (state === 'current') return U.tag('ink', 'Current');
    if (state === 'retained_historical') return U.tag('neutral', 'Historical');
    // Saved from an ad on an earlier transcript: nothing changed after saving.
    if (state === 'earlier_copy') return U.tag('neutral', 'Earlier copy');
    if (state === 'source_changed') return U.tag('info', 'Changed since saved');
    return U.tag('gap', 'Unavailable');
  }

  function availabilityNote(E, item) {
    if (!item.availability || item.availability === 'current') return '';
    return `<p class="saved-why">${E.esc(item.availability_reason || 'The saved source changed. Your saved copy is kept.')}</p>`;
  }

  function excerptQuote(E, item) {
    // Real server capture: item.captured.text with its labels. Aliases
    // (captured_text/item.text) are never read; a missing capture is a
    // visible gap, never a blank-as-success quotation. The passage folds
    // under "Saved passage" so a long capture keeps the row compact.
    const captured = (item.captured && typeof item.captured === 'object') ? item.captured : null;
    const text = captured && typeof captured.text === 'string' ? captured.text : '';
    const quote = text
      ? `<details class="ew-details-quiet saved-passage"><summary>Saved passage</summary><blockquote class="ew-quote">${E.esc(text)}</blockquote></details>`
      : `<p class="notice warn" role="note">Saved quotation unavailable${captured ? '' : ' (no capture stored)'}.</p>`;
    const provenance = captured && captured.source_ref
      ? `<details class="tech-details"><summary>Technical details</summary><p class="small muted">Pin <span class="mono">${E.esc(captured.source_ref)}</span></p></details>`
      : '';
    return quote + provenance;
  }

  // Where an excerpt was heard: its saved bounds (the passage it saved,
  // never the whole record its pin resolves to: R1-09), show, episode, date.
  function excerptMeta(E, item) {
    const captured = (item.captured && typeof item.captured === 'object') ? item.captured : {};
    const labels = captured.labels || {};
    const b = captured.selected_bounds || captured.effective_bounds || {};
    const span = window.EWUI.timeRangeHtml([b.start_s, b.end_s]);
    return [span, E.esc(labels.show_title || ''), E.esc(labels.ep_title || ''), labels.pub_date ? E.esc(E.date(labels.pub_date)) : ''];
  }
  function savedOn(E, item) { return item.saved_at ? 'saved ' + E.timeHtml(item.saved_at, { day: true }) : ''; }
  const MARKS = { excerpt: 'EX', query: 'QY', bookmark: 'BM' };

  /* Item titles never point at the bare hub (which would reset the
     tab): reuse the item's own real destination when it has one, else
     plain text beside the existing explicit Open action. */
  function itemTitleHref(item) {
    const t = item.type;
    if (t === 'query') return queryRoute(item);
    if (t === 'excerpt') {
      const captured = (item.captured && typeof item.captured === 'object') ? item.captured : {};
      const pin = captured.source_ref || (item.saved_target && item.saved_target.ref) || (item.target && item.target.ref) || '';
      return pin ? '#/evidence/' + encodeURIComponent(pin) : '';
    }
    const target = item.target || {};
    if ((target.kind === 'show' || target.kind === 'creative' || target.kind === 'brand') && target.id) return '#/' + target.kind + '/' + encodeURIComponent(target.id);
    if (target.kind === 'episode' && target.source_ref) return '#/evidence/' + encodeURIComponent(target.source_ref);
    if (target.kind === 'episode' && target.id) return '#/episode/' + encodeURIComponent(target.id);
    return '';
  }
  function savedRow(mark, title, meta, tagHtml, detail, actions) {
    const line = meta.filter(Boolean).join(' · ');
    return `<li class="saved-row"><span class="saved-mark" aria-hidden="true">${mark}</span>`
      + `<div class="saved-main"><h3 class="saved-title">${title}</h3>${line ? `<p class="saved-meta">${line}</p>` : ''}${detail}</div>`
      + `<div class="saved-side">${tagHtml}<div class="actions">${actions}</div></div></li>`;
  }
  function itemCard(E, item) {
    const t = item.type;
    const titleHref = itemTitleHref(item);
    const title = titleHref ? E.link(titleHref, E.esc(item.title || 'Saved item'), '') : `<span>${E.esc(item.title || 'Saved item')}</span>`;
    let detail = '', meta = [], open = '';
    if (t === 'excerpt') {
      const captured = (item.captured && typeof item.captured === 'object') ? item.captured : {};
      const pin = captured.source_ref || (item.saved_target && item.saved_target.ref) || (item.target && item.target.ref) || '';
      meta = excerptMeta(E, item);
      detail = excerptQuote(E, item) + availabilityNote(E, item)
        + (item.note ? `<p class="saved-note">Workspace note: ${E.esc(item.note)}</p>` : '');
      // The title already opens the saved source (UX-14c / R2-17).
      open = pin && titleHref !== '#/evidence/' + encodeURIComponent(pin)
        ? E.link('#/evidence/' + encodeURIComponent(pin), 'Open →', 'text-link') : '';
    } else if (t === 'query') {
      const qs = queryState(item);
      meta = [E.esc(describeQuery(item.query))];
      detail = (item.selector ? `<p class="saved-note">Scoped to the saved chart selection.</p>` : '')
        + (qs.ok ? '' : `<p class="notice warn" role="alert">${E.esc(qs.reason)}</p>`)
        + (item.note ? `<p class="saved-note">Workspace note: ${E.esc(item.note)}</p>` : '');
      // The title already opens the current results (F-24): no second link.
    } else {
      const target = item.target || {};
      meta = [E.esc(bookmarkLabel(item))];
      detail = availabilityNote(E, item) + (item.note ? `<p class="saved-note">Workspace note: ${E.esc(item.note)}</p>` : '');
      // The title already opens the target (F-24); without one, say so.
      open = titleHref ? '' : bookmarkLink(E, item, target);
    }
    meta.push(savedOn(E, item));
    // Note creation lives on the evidence reader for the exact saved
    // pin (Add/Edit note), never on a dead route and never invented
    // for queries/bookmarks: their personal text is the item note
    // edited through Edit.
    const noteAction = (t === 'excerpt' && (item.captured || {}).source_ref)
      ? `${E.link('#/evidence/' + encodeURIComponent(item.captured.source_ref), 'Add note on saved source', 'button quiet')}`
      : '';
    // UX-14c: one visible primary action; the rest in one row menu, with
    // Remove last in danger colour (it still asks to confirm). FR-11: Edit
    // and Remove only on an item this member may change (the server's
    // can_change); no menu when nothing is left in it.
    const changes = item.can_change === false ? ''
      : `${E.button('edit-saved', 'Edit', 'quiet', `data-id="${E.esc(String(item.id))}"`)}`
      + `${E.button('delete-saved', 'Remove', 'quiet ew-row-menu-danger', `data-id="${E.esc(String(item.id))}" data-rev="${E.esc(String(item.revision ?? ''))}" data-title="${E.esc(item.title || 'Saved item')}"`)}`;
    const actions = open + (noteAction || changes
      ? `<details class="ew-row-menu"><summary class="button" aria-label="${E.esc('Actions for ' + (item.title || 'Saved item'))}">Actions</summary>`
        + `<div class="ew-row-menu-panel">${noteAction}${changes}</div></details>` : '');
    return savedRow(MARKS[t] || '··', title, meta, availabilityTag(item), detail, actions);
  }

  function describeQuery(query) {
    // An explicit [] is an exact-empty predicate (matches nothing),
    // never omitted as unrestricted scope; null/absent stays the
    // unrestricted label. Retained date/ids/sort predicates render
    // too, so an exact-empty query never reads as bare All mentions.
    const q = query || {};
    const parts = [q.mode === 'ads' ? 'Ad records' : q.mode === 'episodes' ? 'Episodes' : 'All mentions'];
    if (q.q) parts.push(window.EWUI.queryWords(q.q));
    for (const [k, label] of [['brands', 'brands'], ['shows', 'shows'], ['categories', 'categories'], ['statuses', 'status'], ['audio', 'audio']]) {
      // A stored status reads as what it is, never its review state (PLAIN-ADS).
      if (!Array.isArray(q[k])) continue;
      const words = k === 'statuses' ? [...new Set(q[k].map(v => (v === 'mention' ? 'mentions' : 'ads')))] : q[k];
      parts.push(q[k].length ? `${label}: ${words.join(', ')}` : `${label}: exact-empty (matches nothing)`);
    }
    if (q.date_status && q.date_status !== 'all') parts.push(`dates: ${q.date_status}`);
    if (q.from || q.to) parts.push(`dates ${q.from || 'start'}–${q.to || 'latest'}`);
    if (Array.isArray(q.ids)) parts.push(q.ids.length ? `${q.ids.length} exact records` : 'exact-empty ids');
    if (q.sort) parts.push(`sort: ${q.sort}`);
    return parts.join(' · ');
  }

  /* Saved query reopen state. The full stored live-definition scope
     is preserved; an unreadable or unavailable definition shows
     repair, never an unrestricted '#/search' and never a dropped
     selector or faked query. Selector authority is not mutated. */
  function queryState(item, { saved = false } = {}) {
    const E = window.EW;
    const query = (item && item.query) || null;
    if (!query || typeof query !== 'object'
        || !['ads', 'mentions', 'episodes'].includes(query.mode)) {
      return { ok: false, reason: 'This saved query is unreadable. Remove it and save the search again.' };
    }
    const selector = liveSelector(item.selector);
    if (item.selector != null && !selector) {
      return { ok: false, reason: 'This saved chart selection is unreadable. Remove the query and save the search again.' };
    }
    let route = '';
    try {
      // SAVED-FILTER (G3-01): opened from its saved row, the route marks
      // the saved search's brands; a new typed query drops them.
      route = E.searchURL({ ...query, page: 1 }, { selector, ...(saved ? { savedBrands: query.brands || [], savedQ: query.q || '' } : {}) });
    } catch {
      return { ok: false, reason: 'This saved query cannot be reopened. Remove it and save the search again.' };
    }
    return { ok: true, route };
  }

  function queryRoute(item) {
    const state = queryState(item, { saved: true });
    return state.ok ? state.route : '';
  }

  /* Stored live_definition selectors carry no counted revision; they
     evaluate under current authority at open. */
  function liveSelector(selector) {
    if (!selector || typeof selector !== 'object') return null;
    return { ...selector, scope_mode: 'live_definition', view_revision: null, workspace_context: null };
  }

  /* What a bookmark points at, from the server's captured names (the
     stored show display title, the episode and show titles), never the
     user's own title repeated. Without a captured name: the old copy. */
  function bookmarkLabel(item) {
    const target = item.target || {};
    const captured = (item.captured && typeof item.captured === 'object') ? item.captured : {};
    const named = [captured.show_title, captured.label].filter(Boolean).join(' · ');
    if (target.kind === 'show') return `Show bookmark · ${captured.label || item.title || target.id || ''} (opens the current profile)`;
    if (target.kind === 'creative') return `Creative bookmark · ${item.title || target.id || ''} (opens the current profile)`;
    if (target.kind === 'brand') return target.id ? `Tracked brand · ${item.title || ''} (opens the current brand page)` : 'Tracked brand · not available to you now';
    if (target.kind === 'episode') {
      if (target.source_ref) {
        return captured.label ? `Pinned episode · ${named} (opens the transcript version you saved)`
          : 'Pinned episode · opens the transcript version you saved';
      }
      return captured.label ? `Episode bookmark · ${named} (opens the current episode)`
        : 'Episode bookmark · metadata only (opens the current episode)';
    }
    return item.title || 'Bookmark';
  }

  function bookmarkLink(E, item, target) {
    if (target.kind === 'show' && target.id) return E.link('#/show/' + encodeURIComponent(target.id), 'Open current profile →', 'text-link');
    if (target.kind === 'creative' && target.id) return E.link('#/creative/' + encodeURIComponent(target.id), 'Open current profile →', 'text-link');
    if (target.kind === 'brand' && target.id) return E.link('#/brand/' + encodeURIComponent(target.id), 'Open brand →', 'text-link');
    if (target.kind === 'episode') {
      if (target.source_ref) return E.link('#/evidence/' + encodeURIComponent(target.source_ref), 'Open captured pin →', 'text-link');
      if (target.id) return E.link('#/episode/' + encodeURIComponent(target.id), 'Open current episode →', 'text-link');
    }
    return '<span class="section-note">Saved record retained; its target is unavailable.</span>';
  }

  const TAB_LABELS = { excerpts: 'Excerpts', queries: 'Queries', bookmarks: 'Bookmarks', reports: 'Reports', notes: 'Standalone notes' };

  /* Per-tab counts from the server only: the saved-items read carries
     counts per type; reports and notes give their own totals. A tab
     whose count is not known shows none (never a guess). Count reads
     are small (limit 1) and a failed one only drops its number. */
  async function tabCounts(api, tab, filter, main, signal) {
    const counts = {};
    // A count keeps its server relation; without one there is no count.
    const fromItems = out => {
      // The saved-items response carries the plan signal (excerpt_only):
      // a Saved page opened first keys it to this workspace.
      if (out && typeof out.excerpt_only === 'boolean' && typeof window.EW.notePlan === 'function') window.EW.notePlan(out, { result: true });
      const c = (out && out.counts) || {};
      const rel = out && out.total_relation;
      if (!rel) return;
      for (const [t, type] of Object.entries(TAB_TYPE)) if (Number.isFinite(c[type])) counts[t] = { n: c[type], rel };
    };
    const reads = [];
    if (TAB_TYPE[tab]) reads.push(main.then(fromItems));
    else reads.push(api('/customer/saved-items' + qs({ limit: 1 }), { signal }).then(fromItems));
    for (const [t, path] of [['reports', '/customer/archive'], ['notes', '/customer/mistakes']]) {
      const own = t === tab && !filter;
      reads.push((own ? main : api(path + qs({ limit: 1 }), { signal }))
        .then(out => { if (out && Number.isFinite(out.total) && out.total_relation) counts[t] = { n: out.total, rel: out.total_relation }; }));
    }
    await Promise.allSettled(reads);
    return counts;
  }

  // Exact: the number. Lower bound: "N+" read as "at least N". Any
  // other relation: the number with the relation's own words.
  function countText({ n, rel }) {
    const E = window.EW;
    if (rel === 'exact') return String(n);
    if (rel === 'lower_bound') return `<span aria-hidden="true">${n}+</span><span class="visually-hidden">at least ${n}</span>`;
    return `${n} · ${E.esc(String(rel).replace(/_/g, ' '))}`;
  }

  /* Full Saved hub for the route tab. scope carries the list filter
     ({query:{q},selector} is not a research scope here: q filters saved
     titles/text/notes). Reports come from the retained archive;
     notes are personal discrepancy notes with real delivery state. A
     failed list stays a failure, never an empty tab. */
  async function render({ api, esc, link, scope, workspaceContext, signal, isCurrent }) {
    const E = window.EW;
    const route = (E.current && E.current.route) || '';
    const tab = tabFromRoute(route);
    const params = routeParams(route);
    const filter = params.get('q') || '';
    const cursor = params.get('cursor') || '';
    void scope; void workspaceContext; void isCurrent;
    const main = tab === 'reports' ? api('/customer/archive' + qs({ q: filter, cursor, limit: 20 }), { signal })
      : tab === 'notes' ? api('/customer/mistakes' + qs({ q: filter, cursor, limit: 20 }), { signal })
        : api('/customer/saved-items' + qs({ type: TAB_TYPE[tab], q: filter, cursor, limit: 20 }), { signal });
    main.catch(() => {});
    const countsP = tabCounts(api, tab, filter, main, signal);
    let body = '';
    try {
      const out = await main;
      // The counts read sets the plan flag before any empty state renders.
      await countsP;
      if (tab === 'reports') body = reportsTab(E, out, filter);
      else if (tab === 'notes') body = notesTab(E, out, filter);
      else body = itemsTab(E, out, TAB_TYPE[tab], filter);
    } catch (err) {
      if (err && err.droppedContext) throw err;
      body = E.errorBlock(`Saved ${tab} didn't load. Nothing was removed. ` + (err.message || err), E.button('retry', 'Retry'));
    }
    const counts = await countsP;
    const tabs = TABS.map(t => {
      const active = t === tab ? ' aria-current="page"' : '';
      const n = counts[t] ? ` <span class="ew-tab-count">${countText(counts[t])}</span>` : '';
      return `<a href="${savedURL(t, filter ? { q: filter } : {})}"${active}>${TAB_LABELS[t]}${n}</a>`;
    }).join('');
    return `<div class="ew-saved">${E.pageHead('Saved', 'Everything you kept. Each item says whether it still matches what we hold today.')}`
      + `<div class="saved-tabs-wrap"><nav class="saved-tabs" aria-label="Saved collections">${tabs}</nav></div>`
      + `<form id="saved-filter" class="saved-filter" role="search"><label for="saved-q">Filter ${esc(TAB_LABELS[tab].toLowerCase())}</label>`
      + `<div class="saved-filter-row"><input id="saved-q" name="q" type="search" value="${esc(filter)}" autocomplete="off">`
      + `<button class="button" type="submit">Filter</button>`
      + (filter ? `<button class="button quiet" type="button" data-action="saved-clear-filter">Clear</button>` : '')
      + `</div><input type="hidden" name="tab" value="${tab}"></form>`
      + `<section aria-label="Saved ${esc(tab)}">${body}</section></div>`;
  }

  function pagerHtml(E, out) {
    return out.next_cursor ? `<div class="actions saved-more">${E.link(routeWithCursor(out.next_cursor), 'More →', 'button')}</div>` : '';
  }
  // While a filter is applied, the server's own count of what matched:
  // "1 saved item matches", "3 saved items match".
  function matchLine(E, out, filter, [one, many]) {
    if (!filter || out.total === undefined || out.total === null) return '';
    const n = out.total;
    return `<p class="saved-count">${n} ${n === 1 ? one + ' matches' : many + ' match'} “${E.esc(filter)}”${out.total_relation && out.total_relation !== 'exact' ? ` · ${E.esc(out.total_relation)}` : ''}</p>`;
  }

  const PLURAL = { excerpt: 'excerpts', query: 'queries', bookmark: 'bookmarks' };
  // A filter that matches nothing is not an empty collection: say so, keep
  // the items in view, and make Clear filter the action.
  function filteredEmpty(E, noun, filter) {
    return E.empty(`No ${noun} match “${filter}”`,
      'Your saved items are still here. Clear the filter to see them all.',
      `<button class="button primary" type="button" data-action="saved-clear-filter">Clear filter</button>`);
  }

  // Saved items the plan withholds (Plan A old artifacts), and those the
  // rights for their shows withhold (DENY-WORDS FDV-R1-02/-03, UX
  // 5e799bded): one page-level line with the server's counts, each
  // sentence only when its count > 0; nothing about the items themselves.
  const count = v => { const n = Number(v); return Number.isInteger(n) && n > 0 ? n : 0; };
  function withheldLine(out) {
    const p = count(out.plan_withheld), r = count(out.rights_withheld);
    if (!p && !r) return '';
    const plan = p ? `${p === 1 ? '1 saved item is' : `${p} saved items are`} not available on your plan.` : '';
    const rights = !r ? ''
      : p ? `${r} more can’t be shown here, because the rights for ${r === 1 ? 'its' : 'their'} shows don’t allow it.`
      : r === 1 ? '1 saved item can’t be shown here, because the rights for its shows don’t allow it.'
      : `${r} saved items can’t be shown here, because the rights for their shows don’t allow it.`;
    return `<p class="saved-count saved-withheld">${[plan, rights].filter(Boolean).join(' ')}</p>`;
  }

  // DENY-WORDS (ADC-C-06/07) and (4) (DPC-06, UX 34c544385): reports and
  // exports the plan withholds, then those a grant leaves out, then exports
  // a source's own deny withholds; one muted line per reason under the list
  // or its empty state, naming each kind only when its count > 0. The verb
  // and its/their follow the line's total. Rights-withheld reports have
  // their own number-only rows.
  function archiveWithheldLines(out) {
    const x = out.exports_withheld || {};
    const p = count(out.plan_withheld), a = count(out.access_withheld);
    const xp = count(x.plan), xa = count(x.access), xr = count(x.rights);
    const kinds = (r, e) => [r && (r === 1 ? '1 report' : `${r} reports`), e && (e === 1 ? '1 export' : `${e} exports`)]
      .filter(Boolean).join(' and ');
    const line = t => `<p class="saved-foot saved-withheld">${t}</p>`;
    return (p + xp ? line(`${kinds(p, xp)} ${p + xp === 1 ? 'is' : 'are'} not available on your plan.`) : '')
      + (a + xa ? line(`${kinds(a, xa)} ${a + xa === 1 ? 'isn’t' : 'aren’t'} shown, because your access doesn’t include all of ${a + xa === 1 ? 'its' : 'their'} shows.`) : '')
      + (xr ? line(`${kinds(0, xr)} can’t be shown here, because the rights for ${xr === 1 ? 'its' : 'their'} shows don’t allow it.`) : '');
  }

  // Plan A has no transcripts to search: the button says what Search
  // finds there (Muse pa-r1 FIX-3); Plan B keeps its words.
  const searchLink = E => E.link('#/search', typeof E.excerptOnly === 'function' && E.excerptOnly()
    ? 'Search ads' : 'Search transcripts', 'button');

  function itemsTab(E, out, type, filter) {
    const items = out.items || [];
    const withheld = withheldLine(out);
    if (filter && !items.length) return withheld + filteredEmpty(E, PLURAL[type], filter);
    if (!items.length && type === 'excerpt' && withheld) {
      // Withheld, nothing listed: the count sits in the card, never under
      // a heading that says nothing was saved (Muse pa-r1 POLISH-1).
      return matchLine(E, out, filter, ['saved item', 'saved items'])
        + `<div class="empty"><h2>No saved excerpts available</h2>${withheld}<p>To save an excerpt, open an ad and choose Save excerpt.</p>`
        + `<div class="actions">${searchLink(E)}</div></div>`;
    }
    const range = withheld + matchLine(E, out, filter, ['saved item', 'saved items']);
    if (!items.length) {
      return range + E.empty(`No saved ${PLURAL[type]}`,
        type === 'excerpt' ? 'To save an excerpt, open an ad and choose Save excerpt.'
        : type === 'query' ? 'Save a search to reopen its current results at page one. Saving a query never starts tracking.'
        : 'Bookmark a show, episode or creative, or track a brand, to revisit its current page.',
        searchLink(E));
    }
    // One legend line per state on the page, each saying what it means.
    const shown = state => items.some(i => i.availability === state);
    return range + `<ul class="ew-card saved-list">${items.map(i => itemCard(E, i)).join('')}</ul>` + pagerHtml(E, out)
      + (shown('source_changed') ? `<p class="saved-foot">Changed since saved: the source was updated after you saved. Your copy keeps what you saw.</p>` : '')
      + (shown('earlier_copy') ? `<p class="saved-foot">Earlier copy: you saved an ad from an earlier transcript of the episode. Nothing changed after you saved.</p>` : '');
  }

  function routeWithCursor(cursor) {
    const route = (window.EW.current && window.EW.current.route) || '#/saved';
    const [base, query] = route.split('?');
    const p = new URLSearchParams(query || '');
    p.set('cursor', cursor);
    return base + '?' + p.toString();
  }

  /* Global retained archive: reports AND exports in one read
     projection, newest last per the server order. Every row renders
     its own server-returned download_path (same bytes on
     re-download); no null-campaign or regenerated links are built.
     Filter/count/cursor bind the literal kind/campaign/title cohort:
     a changed cohort refuses as a refresh, never mixed pages. */
  function archiveRow(E, r) {
    // ADS-R1-02: a withheld report is its number and the line, nothing else.
    if (r.kind === 'report' && r.rights_withheld) {
      // DPV-01: the empty mark keeps the line in the text columns.
      return `<li class="saved-row"><span class="saved-mark" aria-hidden="true"></span><div class="saved-main"><p class="saved-title">${E.esc(E.reportWithheldLine(r.report_no))}</p></div></li>`;
    }
    const U = window.EWUI;
    const tags = U.tag('neutral', 'Frozen · won’t change')
      + (r.stale ? U.tag('uncertain', 'Stale' + ((r.stale_reasons || []).length ? ': ' + E.staleSummary(r.stale_reasons) : '')) : '');
    const isReport = r.kind === 'report';
    // DEMO2-SCR-R1-01 / -03: an export counts its rows; an empty one says
    // when rows containing transcript text were left out. An export stored
    // before `withheld` was kept has none, so it reads "0 rows" (SCR1C-R1-05).
    const n = Number(r.count);
    const count = r.count == null ? ''
      : !isReport && Number.isFinite(n) ? (n === 0 && Number(r.withheld) > 0 ? 'Empty file · transcript text left out' : `${E.num(n)} ${n === 1 ? 'row' : 'rows'}`)
        : `${r.count} ${E.esc(E.plainWords(r.count_units || 'items').toLowerCase())}`;
    // AA-01: a row without export permission stays listed, with no open or download link.
    const closed = r.exportable === false;
    const title = isReport && !closed
      ? E.link('#/campaign/' + encodeURIComponent(r.campaign_id) + '/report/' + encodeURIComponent(r.report_no), E.esc(r.title || ('Report ' + r.report_no)))
      : `<span>${E.esc(r.title || (isReport ? 'Report ' + r.report_no : 'Export ' + r.id))}</span>`;
    const meta = isReport
      ? ['Campaign report', `frozen ${E.timeHtml(r.captured_at, { day: true })}`, count]
      : [`Export${r.format ? ' · ' + E.esc(String(r.format).toUpperCase()) : ''}`, `captured ${E.timeHtml(r.captured_at, { day: true })}`, count];
    const digest = r.digest ? `<details class="tech-details"><summary>Technical details</summary><p class="small muted mono">sha256:${E.esc(String(r.digest).slice(0, 16))}…</p></details>` : '';
    // The report title opens it (R2-17); every row then offers Download.
    const actions = closed ? '<span class="saved-note">You can see this in history but not open or download it.</span>'
      : (r.download_path
        ? E.downloadLink(r.download_path + (isReport ? '?download=1' : ''), 'Download', 'text-link')
        : `<span class="saved-note">No stored download for this row.</span>`);
    return savedRow('RP', title, meta, tags, digest, actions);
  }

  function reportsTab(E, out, filter) {
    const items = out.items || [];
    const range = matchLine(E, out, filter, ['report or export', 'reports or exports']);
    const withheld = archiveWithheldLines(out);
    if (filter && !items.length) return filteredEmpty(E, 'reports or exports', filter) + withheld;
    if (!items.length) {
      return range + E.empty('No retained reports or exports', 'Save a final campaign report or capture a research export. Re-downloads are byte-identical.', E.link('#/campaigns', 'Open campaigns', 'button'))
        + withheld;
    }
    return range
      + `<ul class="ew-card saved-list">${items.map(r => archiveRow(E, r)).join('')}</ul>`
      + withheld
      + pagerHtml(E, out)
      // DPV-02: only when a row below can be downloaded (UX words 10-30).
      + (items.some(r => !r.rights_withheld && r.exportable !== false && r.download_path)
        ? `<p class="saved-foot">Frozen files never change. Each download is the same file as the first.</p>` : '');
  }

  function notesTab(E, out, filter) {
    const notes = out.notes || out.mistakes || [];
    const range = matchLine(E, out, filter, ['note', 'notes']);
    if (filter && !notes.length) return filteredEmpty(E, 'notes', filter);
    if (!notes.length) {
      return range + E.empty('No standalone notes yet.', 'Notes attached to saved excerpts appear under Excerpts. Add a standalone note from any evidence page.', searchLink(E));
    }
    const local = n => n.delivery_context === 'local_only';
    return range + `<ul class="ew-card saved-list">${notes.map(n => savedRow('NT',
      E.link('#/note/' + encodeURIComponent(n.id), E.esc((n.reason || '').slice(0, 80) || ('Note ' + n.id))),
      [E.esc('Note · ' + (n.status || n.note_status || 'open')), n.created_at ? 'saved ' + E.timeHtml(n.created_at, { day: true }) : ''],
      window.EWUI.tag(local(n) ? 'neutral' : 'info', local(n) ? (E.current && E.current.hosted ? 'Private note' : 'Local only') : 'Received'), '',
      E.link('#/note/' + encodeURIComponent(n.id), 'Open', 'text-link'))).join('')}</ul>` + pagerHtml(E, out)
      + `<p class="saved-foot">Notes are yours.</p>`;
  }

  /* -- Track a brand (a saved bookmark of kind "brand") ------------------ */
  /* The caller passes its api (E.api). Every tracked brand comes from the
     saved bookmarks list, all pages; a failed read rejects, never an
     empty list. Track twice returns the server's existing item. */
  async function trackedBrands(api, { signal = null } = {}) {
    const out = [];
    let cursor = null;
    for (let page = 0; page < 50; page += 1) {
      const res = await api('/customer/saved-items' + qs({ type: 'bookmark', limit: 20, cursor }), { signal });
      for (const it of (res && res.items) || []) if (it && it.target && it.target.kind === 'brand') out.push(it);
      cursor = res && res.next_cursor;
      if (!cursor) break;
    }
    return out;
  }
  function trackedBrandFor(items, brandKey) {
    if (!brandKey) return null;
    return (items || []).find(it => it && it.target && it.target.kind === 'brand' && it.target.id === brandKey) || null;
  }
  async function trackBrand(api, brandKey) {
    if (!brandKey || typeof brandKey !== 'string') throw new Error('Tracking needs the brand.');
    return api('/customer/saved-items', { method: 'POST', body: createRequest('bookmark', { kind: 'brand', id: brandKey }) });
  }
  async function untrackBrand(api, item) {
    const it = item || {};
    if (!it.id || typeof it.revision !== 'number') throw new Error('Reload to see the current revision before you stop tracking.');
    return api('/customer/saved-items/' + encodeURIComponent(it.id), { method: 'DELETE', body: { expected_revision: it.revision } });
  }

  /* Saved's tab strip scrolls sideways on a phone. Bring the selected tab
     into view by moving the strip's own scrollLeft (never scrollIntoView,
     never focus), and cue hidden tabs on either side. */
  function syncCue(nav) {
    const wrap = nav.parentElement;
    if (!wrap || !wrap.classList) return;
    wrap.classList.toggle('is-more-start', nav.scrollLeft > 1);
    wrap.classList.toggle('is-more-end', nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 1);
  }
  function revealCurrentTab(root) {
    const nav = root && root.querySelector ? root.querySelector('.saved-tabs') : null;
    if (!nav || !(nav.scrollWidth > nav.clientWidth)) return;
    const a = nav.querySelector('a[aria-current]');
    if (a) {
      const margin = 16, max = nav.scrollWidth - nav.clientWidth;
      let left = nav.scrollLeft;
      if (a.offsetLeft < left + margin) left = a.offsetLeft - margin;
      else if (a.offsetLeft + a.offsetWidth > left + nav.clientWidth - margin) left = a.offsetLeft + a.offsetWidth - nav.clientWidth + margin;
      nav.scrollLeft = Math.max(0, Math.min(max, left));
    }
    if (!nav.dataset.cue) { nav.dataset.cue = '1'; nav.addEventListener('scroll', () => syncCue(nav), { passive: true }); }
    syncCue(nav);
  }

  window.EWSaved = { revealCurrentTab, trackedBrands, trackedBrandFor, trackBrand, untrackBrand, render, editDraft, createRequest, updateRequest, checkDraft, savedURL, tabFromRoute, liveSelector, describeQuery, queryState, archiveRow, excerptQuote };
})();
