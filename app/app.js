/* EarWitness connected customer application. Server-owned state only.
   The browser keeps navigation, scroll/focus maps, and unsaved form input.
   Every durable write waits for the server commit before reporting Saved. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clone = v => JSON.parse(JSON.stringify(v));
  const unique = a => [...new Set(a)];
  const date = v => {
    if (!v) return 'Date unknown';
    if (!/^\d{4}-\d{2}-\d{2}/.test(v)) return 'Date unknown';
    const d = new Date(v.slice(0, 10) + 'T12:00:00Z');
    if (!Number.isFinite(d.getTime())) return 'Invalid date';
    return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(d);
  };
  const plural = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
  // Timestamps follow the Settings time zone and name it (formatDate).
  // stamp() is HTML: a <time> with the UTC time on hover (timeHtml).
  // stampText() is the same words as plain text, for comparisons.
  const stamp = v => v ? timeHtml(v) : 'No successful update';
  const stampText = v => v ? formatDate(v) : 'No successful update';
  // Counts shown to people carry thousands separators (1,198, not 1198).
  const num = n => Number(n).toLocaleString('en-US');
  // Human-readable timestamps: zero-padded minutes/seconds with one
  // fractional digit, rounded once so binary floats never leak noise.
  const time = n => {
    const v = Number(n);
    if (!Number.isFinite(v) || v < 0) return '0:00.0';
    const totalTenths = Math.round(v * 10);
    const tenths = totalTenths % 10;
    const totalSeconds = Math.floor(totalTenths / 10);
    const seconds = totalSeconds % 60;
    const minutes = Math.floor(totalSeconds / 60) % 60;
    const hours = Math.floor(totalSeconds / 3600);
    const mm = hours ? String(minutes).padStart(2, '0') : String(minutes);
    return `${hours ? hours + ':' : ''}${mm}:${String(seconds).padStart(2, '0')}.${tenths}`;
  };
  const link = (route, text, cls = '') => `<a class="${cls}" href="${esc(route)}">${text}</a>`;
  // A stored file's Download (READ-QUEUE-FIX2 RQ-R2-04): a plain click
  // reads it through the request queue; the link is the fallback.
  const downloadLink = (path, text, cls = '') => `<a class="${cls}" href="${esc('/api/v1' + path)}" download data-action="download-file" data-path="${esc(path)}">${text}</a>`;
  const button = (act, text, cls = '', attrs = '') => `<button type="button" class="button ${cls}" data-action="${act}" ${attrs}>${text}</button>`;
  const tag = (text, cls = '') => `<span class="tag ${cls}">${esc(text)}</span>`;
  const normalize = v => String(v ?? '').toLocaleLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '');
  const tokensOf = (q = '') => (String(q ?? '').match(/"[^"]+"|\S+/g) || []).map(x => normalize(x.replace(/^"|"$/g, '')));

  const current = {
    route: location.hash || '#/overview', scroll: new Map(), focus: new Map(),
    back: new Map(), filters: {}, directory: {}, dialogOpener: null,
    dialogRoute: null, toastTimer: null, searchTimer: null, fetchToken: 0,
    authorityEpoch: 0, framePath: null,
    // DEMO2 pass 14: the epoch the page on screen was published in (null:
    // none, or a refusal answered its render); the render a refusal
    // boundary started to check access again.
    pageEpoch: 0, recheckPending: false, recheckToken: null, refusedToken: null,
    csrf: '', aborters: new Map(), dataset: null,
    // UX-09: entries of dialogs closed unsaved, kept for this page
    // session only (never storage), keyed by dialog and record.
    dialogDrafts: new Map(),
    // Displayed mutation basis: the canonical epoch the currently
    // published view was captured under. Assigned ONLY at successful
    // ordinary render publication; cleared on canonical change,
    // sign-in, refusal, logout/login and workspace clear. Ordinary
    // hosted writes submit this basis — never a newer global read —
    // so a stale page cannot relabel old values with a new epoch.
    displayedWorkspaceSwitchRevision: undefined,
  };
  // Workspace generation: counts every change of the active workspace,
  // whichever path assigns it (boot, switch reply, refusal, sign-out), so
  // a draft stashed under an earlier generation is never restored after
  // the tab left that workspace, even if it came back (A -> B -> A).
  let activeWs;
  current.workspaceGeneration = 0;
  Object.defineProperty(current, 'activeWorkspace', {
    enumerable: true,
    get: () => activeWs,
    // DEMO2 pass 23 (owner decision): audio rights are per workspace, so
    // any change of it, whichever path adopts it, stops every player.
    set: (v) => {
      const moved = (v || '') !== (activeWs || '');
      if (moved) current.workspaceGeneration += 1;
      activeWs = v;
      if (moved) stopAudio();
    },
  });

  /* User-facing workspace notices: what happened, that nothing was
     saved, and the one next step. Shared by dispatch and the views. */
  const notices = {
    // Held forms only: the draft stays on screen beside a Reload action.
    changedElsewhere: 'This workspace changed in another tab or window. Nothing was saved; your entries are kept here. Reload the workspace to continue.',
    // Button actions hold no entries. Dispatch reports the conflict at
    // once; the reconcile then reports its actual outcome.
    conflictAction: 'This workspace changed in another tab or window. Nothing was changed.',
    conflictRendered: 'This workspace changed in another tab or window. Nothing was changed; the page now shows the current workspace.',
    conflictHeld: 'This workspace changed in another tab or window. Nothing was changed. Reload the workspace to continue.',
    conflictFailed: 'This workspace changed in another tab or window. Nothing was changed. The current workspace could not be loaded; try again.',
    changed: 'The workspace changed. Nothing was saved; your entries are kept here. Reload the workspace to continue.',
    signedOut: 'You are signed out. Nothing was saved; your entries stay here until you choose Sign in, which clears them.',
    signedOutAction: 'You are signed out. Nothing was changed.',
    fencePending: 'This change is held while the workspace switch is confirmed. Nothing was saved.',
    // One plain copy for every lost or unconfirmed switch; the way to
    // the chooser is a real control beside it, never a direction.
    fenceUncertain: 'The workspace switch could not be confirmed. Choose the workspace again to confirm it. Nothing was saved.',
    switchLost: 'The workspace switch could not be confirmed. Choose the workspace again to confirm it. Nothing was saved.',
  };
  // While the switch is still settling the change is merely held; after
  // an uncertain outcome the chooser is the way forward.
  const switching = () => !!(current.switchPending || current.switchSettling);
  const fenceNotice = () => (switching() ? notices.fencePending : notices.fenceUncertain);
  // A conflict that overlaps this tab's own switch may be that switch:
  // never claim it came from another tab or window.
  const neutralCopy = (message) => String(message).replace(' in another tab or window', '');

  // Server error text may lead with an internal code
  // ("legacy_review_required: …"). Users get plain copy for the codes
  // listed here; any other text (a field name such as "date_to: …")
  // stays exactly as the server sent it.
  const LEGACY_CAMPAIGN = "This campaign was created before campaign types existed. It's read-only until the workspace owner reviews it.";
  const SERVER_CODE_COPY = { legacy_review_required: LEGACY_CAMPAIGN };
  function plainServerText(message, code = '') {
    const text = String(message ?? '');
    const m = /^([a-z]+(?:_[a-z]+)+):/.exec(text);
    const key = m && Object.hasOwn(SERVER_CODE_COPY, m[1]) ? m[1] : String(code || '').toLowerCase();
    return Object.hasOwn(SERVER_CODE_COPY, key) ? SERVER_CODE_COPY[key] : text;
  }

  // A failure with no server message: plain words for its status.
  // Server messages (field validation text) are always kept as sent.
  function statusCopy(status) {
    if (status === 400 || status === 422) return "That didn't work. Check the entries and try again.";
    if (status === 401) return 'You are signed out. Sign in, then try again.';
    if (status === 403) return "You don't have permission to do that.";
    if (status === 404) return 'That item no longer exists.';
    if (status === 409 || status === 412) return 'This changed since you opened it. Reload, then try again.';
    if (status === 429) return 'Too many requests right now. Wait a moment, then try again.';
    if (status >= 500) return 'Something went wrong on our side. Try again.';
    return "That didn't work. Try again.";
  }

  const writeWaiters = [];
  // Resolves once no ordinary write is in flight.
  function whenWritesSettled() {
    if (!current.writesInFlight) return Promise.resolve();
    return new Promise(res => writeWaiters.push(res));
  }
  // DEMO2 pass 23: session reads in flight. Resolves once none is.
  // DEMO2 pass 24 (R20-03): and once the last one's answer was adopted:
  // its reader adopts right after the request settles, so the waiters
  // run on a later task, if no read opened meanwhile.
  let sessionReadsOpen = 0;
  const sessionIdleWaiters = [];
  // DEMO2 pass 25 (SEC-R14-05): with none open too, on a later task, so
  // a reader that just got its answer has adopted it.
  function whenSessionReadsSettled() {
    return new Promise(res => { sessionIdleWaiters.push(res); if (!sessionReadsOpen) sessionReadsIdle(); });
  }
  // DEMO2 pass 24: the tab-return gate open now, if any (views own it).
  function returnGate() {
    const views = window.EWViews;
    return views && typeof views.tabReturnGate === 'function' ? views.tabReturnGate() : null;
  }
  function sessionReadsIdle() {
    const run = () => {
      if (sessionReadsOpen) return;
      const w = sessionIdleWaiters.splice(0);
      for (const res of w) res();
    };
    if (typeof setTimeout === 'function') setTimeout(run, 0);
    else Promise.resolve().then(() => Promise.resolve()).then(run);
  }

  /* Request timeouts. A reply that never comes must not hold renders,
     saves in flight or a workspace switch forever. The page's own
     timers are captured once at load; tests replace set/clear with
     fake timers. A write that times out has an UNKNOWN outcome (the
     server may still commit it): it is reported as unconfirmed with
     status undefined, like a transport failure, and never retried. A
     read that times out is an ordinary failure with Retry. */
  const nativeSetTimeout = typeof setTimeout === 'function' ? setTimeout : null;
  const nativeClearTimeout = typeof clearTimeout === 'function' ? clearTimeout : null;
  const requestTimeout = {
    readMs: 20000,
    writeMs: 30000,
    // A complete export renders every result (about 1 s each on the
    // sample corpus; 150 at most): it waits longer than other writes.
    exportMs: 150000,
    set: nativeSetTimeout ? (fn, ms) => nativeSetTimeout(fn, ms) : null,
    clear: (id) => { if (nativeClearTimeout) nativeClearTimeout(id); },
  };
  // Fetch plus body read, settled within the timeout. The caller's
  // abort (navigation) still aborts; the timeout aborts the request too
  // and rejects even when the transport ignores the abort.
  function timedFetch(url, init, ms, onTimeout) {
    const work = async (signal) => {
      const resp = await fetch(url, { ...init, signal });
      return { resp, text: await resp.text() };
    };
    if (!requestTimeout.set || !(ms > 0)) return work(init.signal);
    const controller = new AbortController();
    const outer = init.signal;
    const follow = () => controller.abort(outer.reason);
    if (outer && outer.aborted) follow();
    else if (outer && outer.addEventListener) outer.addEventListener('abort', follow, { once: true });
    let timer = null;
    const run = work(controller.signal);
    const expired = new Promise((_, reject) => {
      timer = requestTimeout.set(() => { reject(onTimeout()); controller.abort(); }, ms);
    });
    // A late settlement of the abandoned request is ignored.
    run.catch(() => {});
    return Promise.race([run, expired]).finally(() => {
      requestTimeout.clear(timer);
      if (outer && outer.removeEventListener) outer.removeEventListener('abort', follow);
    });
  }

  /* -- API client -------------------------------------------------------- */
  /* DEMO2-SOL-R3-01: the session's filter values and brand/show names are
     read once and reused by every route, instead of on every route. One
     read per workspace (context and active workspace, like preferences)
     and view revision: a research or filter read that reports another
     revision, a switch, or any settled write (a rename, a review, a
     track) reads them again. Only the exact unfiltered reads are shared;
     a failed read is never kept. Each caller gets its own copy, and its
     own navigation abort stops only its wait, never the shared read.
     DEMO2-SEC-R1-01: no route publishes cached data without a fresh
     request that checks access on that navigation. A kept answer is
     reused only as auxiliary data beside the route's own fresh read
     (options.authorizedBy) once that read succeeded; without one, the
     read goes to the server (and refreshes the kept answer). A refusal
     that starts the boundary (401, or 403 marked lost) on any read
     clears every kept answer.
     DEMO2-SEC-R2-02 / R10-01: a kept answer is bound to the cache
     generation it was taken from (map, workspace key, revision, entry).
     If any of them changed between capture and publication (a write, a
     refusal, another revision, a switch), it reads the server instead;
     this holds for a kept promise still in flight too.
     DEMO2-R10-N3: a missing or null authorizedBy, or one that resolves to
     null, is no proof: the read goes to the server. */
  const SHARED_READ = /^\/customer\/(?:brands|shows|filter-values\?field=(?:brands|shows|categories)&limit=50)$/;
  const sharedReads = { key: null, revision: null, entries: new Map() };
  const sharedKey = () => `${current.workspaceContext || ''}|${current.activeWorkspace || ''}`;
  function invalidateSharedReads() { sharedReads.key = null; sharedReads.revision = null; sharedReads.entries = new Map(); }
  /* DEMO2 pass 13: the authority epoch. One counter for what this tab
     may still show without asking. It advances at every authority
     boundary: a navigation away from the page on screen (render), a
     restore (pageshow persisted, a same-hash popstate with stored
     state), a 401 or a 403 marked lost, a workspace change. Everything published
     later from an earlier answer carries the epoch it was validated
     under and publishes only while that epoch still holds: retained
     views (retain/retained), kept shared reads (sharedRead), async
     dialog openers (owner.epoch, checked by openDialog and editStale)
     and in-dialog fills (dialogHolds). */
  const RETAINED_VIEWS = ['lastSearch', 'lastSearchTotal', 'searchItems'];
  /* DEMO2 pass 14: one registry of retained stores. Every store that
     keeps server-derived data past the request that read it registers
     here (current fields cleared to null, or its own clear); a refusal or
     restore clears them all. tests/frontend_demo2_pass14 fails on any
     module-level cache or current field that is neither registered nor
     classified as not holding server data. */
  const retainedStores = new Map();
  function registerRetained(name, spec) { retainedStores.set(name, { fields: spec.fields || [], clear: spec.clear || null }); }
  function clearRetainedStores() {
    for (const store of retainedStores.values()) {
      for (const field of store.fields) current[field] = null;
      if (store.clear) store.clear();
    }
  }
  const retainedStoreList = () => [...retainedStores].map(([name, store]) => ({ name, fields: store.fields.slice() }));
  registerRetained('sharedReads', { clear: invalidateSharedReads });
  registerRetained('retainedViews', { fields: RETAINED_VIEWS });
  registerRetained('datasetStrip', { fields: ['dataset'] });
  /* M9-BACK: kept pages. The last Overview, Search and Alerts pages this
     tab rendered, by route, in memory only (never storage), each with the
     person, workspace, role and switch revision it was rendered for
     (views.js keptPage checks them). Back, a Back link or the nav paints
     one at once while its route reads again behind it. A retained store:
     a refusal, a restore, a role change, a workspace change and a change
     of person clear it; so does any write (api) and an unowned history
     entry (forgetRouteCaches). */
  const keptPages = new Map();
  registerRetained('keptPages', { clear: () => keptPages.clear() });
  registerRetained('workspaceViews', {
    fields: ['pendingSearch', 'displayedAlert', 'displayedDraft', 'savedAlert', 'savedExcerpt', 'savedNote',
      'createdInvite', 'createdShare', 'invitePreflight', 'accessReads', 'keepAccessReads'],
    clear: () => { current.directory = {}; },
  });
  /* DEMO2 pass 14: the network gate (decision 1). Every workspace request
     goes through apiCall, the only caller of fetch. A read is stamped
     with the authority epoch when it is sent; a boundary aborts every
     read in flight, and a read that answers under another epoch is
     dropped: its caller gets a droppedContext error, never the data.
     Writes are never aborted or dropped, so a committed write still
     reports as committed. */
  const inflightReads = new Set();
  // READ-QUEUE: the network side of counted reads already sent (apiCall).
  // Leaving a page (leavePage) rejects their callers through
  // inflightReads but lets the requests finish, so their places stay
  // counted while the server runs them; any other boundary aborts them.
  const inflightSends = new Set();
  // SPEED-R1 (R1-16): settles when this tab's last research query has its
  // answer, or was never sent (apiCall).
  let researchLane = Promise.resolve(), researchOpen = 0;
  function authorityEpoch() { return current.authorityEpoch; }
  function authorityHolds(epoch) { return epoch === current.authorityEpoch; }
  function authorityBoundary({ leavePage = false } = {}) {
    current.authorityEpoch += 1;
    const reads = [...inflightReads];
    inflightReads.clear();
    if (!leavePage) { reads.push(...inflightSends); inflightSends.clear(); }
    for (const c of reads) { try { c.abort(); } catch { /* already settled */ } }
  }
  function authorityDropped() {
    const err = new Error('Access changed while this read was in flight; its response was dropped.');
    err.status = 0;
    err.droppedContext = true;
    err.droppedAuthority = true;
    return err;
  }
  // A retained view is stored with the epoch its fresh read succeeded
  // in (never one that has passed) and read back only within it.
  function retain(name, value, epoch = current.authorityEpoch) {
    if (!RETAINED_VIEWS.includes(name) || !authorityHolds(epoch)) return false;
    current[name] = { ...value, epoch };
    return true;
  }
  function retained(name) {
    const value = RETAINED_VIEWS.includes(name) ? current[name] : null;
    return value && authorityHolds(value.epoch) ? value : null;
  }
  // The dialog a fill captured is still open as that dialog, and no
  // authority boundary passed since the fill started (epoch; default:
  // since the dialog opened).
  function dialogHolds(own, epoch) { return own === current.dialogEpoch && authorityHolds(epoch !== undefined ? epoch : own ? own.authority : current.authorityEpoch); }
  // DEMO2-SEC-R2-01: everything a view keeps from an earlier read: the
  // shared reads, the retained Search views and the dataset strip. A
  // refusal or restore is an authority boundary.
  function forgetRetainedViews() {
    clearRetainedStores();
    authorityBoundary();
  }
  /* DEMO2 pass 15: which refusals start an authority boundary. A 401
     (signed out), or a 403 the server marks as lost access to the
     workspace (error.access === 'lost'; status and code FORBIDDEN are
     unchanged). Every other 403 (a role or feature refusal) and every 428
     (a missing revision) stays local to its action, which shows the
     server's sentence. */
  function startsBoundary(status, err) {
    if (status === 401) return true;
    const error = err && err.body && err.body.error;
    return status === 403 && !!error && typeof error === 'object' && error.access === 'lost';
  }
  /* DEMO2 pass 16/17: the server's own refusal when the caller's workspace
     role changed after admission (access.RoleChanged). Status, code and
     message are those of any role refusal; the body is marked
     error.reason "role_changed" (DEMO2-R13-02). The client keys on that
     marker, never on the message text. Never lost access. */
  function roleChanged(status, err) {
    const error = err && err.body && err.body.error;
    return status === 403 && !!error && typeof error === 'object' && error.access !== 'lost'
      && error.reason === 'role_changed';
  }
  // DEMO2-SEC-R6-01 (owner decision): a role change is a soft boundary
  // (EWViews.roleBoundary); without the views, the stores and epoch go.
  function roleBoundary(opts) {
    const views = window.EWViews;
    if (views && typeof views.roleBoundary === 'function') views.roleBoundary(opts);
    else forgetRetainedViews();
  }
  // DEMO2-SEC-R7-01: an adopted signed-in identity wipes what another
  // person, or the same person in another workspace, typed
  // (EWViews.settleIdentity).
  function settleIdentity() {
    const views = window.EWViews;
    if (views && typeof views.settleIdentity === 'function') views.settleIdentity();
  }
  // A refusal that started the boundary (marked by apiCall below).
  const accessRefused = err => !!err && err.accessRefused === true;
  function noteViewRevision(revision) {
    if (revision === undefined || revision === null || revision === '') return;
    if (sharedReads.revision !== null && sharedReads.revision !== revision) sharedReads.entries = new Map();
    sharedReads.revision = revision;
  }
  function abortError() {
    try { return new DOMException('The operation was aborted.', 'AbortError'); }
    catch { const err = new Error('The operation was aborted.'); err.name = 'AbortError'; return err; }
  }
  // Callers waiting on one shared read: its retry signal aborts when every
  // caller that can leave (one with a signal) has left. A caller without
  // a signal waits for good.
  function sharedWaiters() {
    const ctl = new AbortController();
    let open = 0, held = false;
    return { signal: ctl.signal, join(signal) {
      if (!signal) { held = true; return; }
      open += 1;
      const leave = () => { open -= 1; if (!open && !held) ctl.abort(); };
      if (signal.aborted) leave(); else signal.addEventListener('abort', leave, { once: true });
    } };
  }
  function sharedRead(path, options = {}) {
    const { signal = null, authorizedBy, ...rest } = options;
    if (!SHARED_READ.test(path) || (rest.method && rest.method !== 'GET')) return window.EW.api(path, { ...rest, signal });
    if (sharedReads.key !== sharedKey()) { invalidateSharedReads(); sharedReads.key = sharedKey(); }
    const entries = sharedReads.entries, key = sharedReads.key, revision = sharedReads.revision;
    const epoch = current.authorityEpoch;
    // PLAIN-ADS-FIX3 (code PA-R3-02): an entry still in flight whose
    // callers all left (its retry signal aborted) cannot retry a 429: it
    // counts as absent. A settled answer is reused as before.
    let kept = authorizedBy === undefined || authorizedBy === null ? null : entries.get(path);
    if (kept && kept.waiting && kept.waiting.signal.aborted && !kept.settled) kept = null;
    let entry;
    if (kept) {
      // The route's own fresh read decides: its success publishes the
      // kept answer while its generation still holds; an access refusal
      // clears every kept answer and refuses; any other failure reads
      // the server instead.
      const holds = () => authorityHolds(epoch) && sharedReads.entries === entries && sharedReads.key === key
        && sharedReads.revision === revision && entries.get(path) === kept;
      const fresh = () => sharedRead(path, { ...rest, signal });
      entry = Promise.resolve(authorizedBy).then(proof => {
        if (proof === null || proof === undefined || !holds()) return fresh();
        return kept.then(value => (holds() ? value : fresh()));
      }, err => {
        if (accessRefused(err)) { invalidateSharedReads(); throw err; }
        if (err && (err.name === 'AbortError' || err.droppedContext)) throw err;
        return sharedRead(path, { ...rest });
      });
    } else {
      // PLAIN-ADS-FIX2 (code R2-04): the shared answer is never aborted,
      // but its 429 retry stops once no caller waits for it.
      const waiting = sharedWaiters();
      entry = Promise.resolve().then(() => window.EW.api(path, { ...rest, signal: null, retrySignal: waiting.signal })).then(out => {
        if (out && typeof out === 'object' && 'revision' in out && entries === sharedReads.entries) {
          noteViewRevision(out.revision);
          if (sharedReads.entries !== entries) sharedReads.entries.set(path, entry);
        }
        return out;
      });
      entry.catch(err => {
        if (accessRefused(err)) invalidateSharedReads();
        for (const map of [entries, sharedReads.entries]) if (map.get(path) === entry) map.delete(path);
      });
      entry.waiting = waiting;
      entry.then(() => { entry.settled = true; }, () => {});
      entries.set(path, entry);
    }
    if (entry.waiting) entry.waiting.join(signal);
    else if (kept && kept.waiting) kept.waiting.join(signal);
    const copy = entry.then(clone);
    if (!signal) return copy;
    if (signal.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const stop = () => reject(abortError());
      signal.addEventListener('abort', stop, { once: true });
      copy.then(v => { signal.removeEventListener('abort', stop); resolve(v); },
        e => { signal.removeEventListener('abort', stop); reject(e); });
    });
  }
  // A settled write (or one whose outcome is unknown) may have changed
  // names or values: the shared reads start over. The read-only research
  // query is not a write.
  async function api(path, options = {}) {
    const method = options.method || 'GET';
    if (method === 'GET' || method === 'HEAD') return readWithRetry(path, options);
    if (path === '/customer/research-query') return apiCall(path, options);
    // M9-BACK: a write may change any kept page; they load fresh next time.
    try { return await apiCall(path, options); } finally { invalidateSharedReads(); keptPages.clear(); }
  }
  /* OVERVIEW-429: the hosted server admits 2 requests at once per browser
     session, so a page that starts 3 reads gets a 429 on one of them. A
     rate-limited GET waits the server's Retry-After and tries again: at
     most 2 retries and 3 s of waiting in all, never after the caller
     aborted. Writes (and the POST research query) never retry here. The
     page shows its error only when the last try fails. */
  const READ_RETRY = { max: 2, budgetMs: 3000 };
  /* READ-QUEUE (QA-full G2-02, lead decision 2026-10-24): the client keeps
     the server's count. At most 2 customer requests are sent at once per
     tab (asgi.py RateLimiter counts per browser session: reads, writes
     and media alike; the queue is this page's own, so other tabs of the
     same session fall back to the read retry); the rest wait here, in
     the order asked, unsent. A person's
     action (a write, a clip press: options.press) takes the next free
     place ahead of every queued read and still counts toward the 2. A
     queued request whose caller aborts (the page left, a teardown, an
     authority boundary) leaves the queue unsent. Two holds keep the count
     honest: a read that got 429 keeps its place through its Retry-After
     wait (readWithRetry), and a read whose caller leaves after it was
     sent (the page left, a teardown, a second press) keeps its place
     until the server answers it, because the server finishes it anyway:
     the caller gets its AbortError at once and the answer is dropped,
     but the request is not aborted. Any other authority boundary (a
     refusal, a sign-in change, a switch) and the request timeout still
     abort it (its place then frees at once). Session operations (the
     session read, sign-in, sign-out, the workspace switch) are not
     counted by the server and pass. A clip's first media load and a
     stored file are counted too (READ-QUEUE-FIX1: takePlace,
     holdPlace, options.download). */
  const REQUEST_SLOTS = { limit: 2 };
  const slots = { busy: 0, queue: [] };
  function grantSlot() {
    slots.busy += 1;
    let open = true;
    return { release() { if (!open) return; open = false; slots.busy -= 1; pumpSlots(); } };
  }
  // The caller's view of a sent request: its abort rejects at once with
  // an AbortError; the request runs on (see above).
  function untilAbort(sent, signal) {
    if (!signal) return sent;
    return new Promise((resolve, reject) => {
      const stop = () => reject(abortError());
      if (signal.aborted) return stop();
      signal.addEventListener('abort', stop, { once: true });
      sent.then(resolve, reject).finally(() => signal.removeEventListener('abort', stop));
    });
  }
  function pumpSlots() {
    while (slots.busy < REQUEST_SLOTS.limit && slots.queue.length) {
      const entry = slots.queue.shift();
      for (const sig of entry.signals) sig.removeEventListener('abort', entry.stop);
      entry.resolve(grantSlot());
    }
  }
  // A slot now (returned), or a promise of one. Urgent entries go ahead of
  // every queued read, behind earlier urgent ones. Any of `signals`
  // aborting (the caller's, or a shared read's "every caller left")
  // takes the entry out unsent.
  function takeSlot(urgent, signals) {
    signals = signals.filter(sig => sig && sig.addEventListener);
    if (signals.some(sig => sig.aborted)) return Promise.reject(abortError());
    if (slots.busy < REQUEST_SLOTS.limit && (urgent || !slots.queue.length)) return grantSlot();
    return new Promise((resolve, reject) => {
      const entry = { urgent, signals, resolve, stop: null };
      entry.stop = () => {
        for (const sig of signals) sig.removeEventListener('abort', entry.stop);
        const i = slots.queue.indexOf(entry);
        if (i >= 0) slots.queue.splice(i, 1);
        reject(abortError());
      };
      for (const sig of signals) sig.addEventListener('abort', entry.stop, { once: true });
      const at = urgent ? slots.queue.findIndex(e => !e.urgent) : -1;
      if (at >= 0) slots.queue.splice(at, 0, entry); else slots.queue.push(entry);
    });
  }
  const requestQueue = () => ({ limit: REQUEST_SLOTS.limit, busy: slots.busy, queued: slots.queue.length });
  /* READ-QUEUE-FIX1 (RQ-R1-01): the media request of a clip press is the
     browser's own, but the server counts it. A press takes a place (ahead
     of queued reads: a slot now, or a promise of one) and holds it until
     the audio first loads or fails, at most readMs; release() gives it
     back sooner (the press stopped before its media was asked for).
     READ-QUEUE-FIX2 (RQ-R2-01): the hold is keyed to the source it was
     taken for. A reused element given a new source fires abort and
     emptied for the previous one after the press ran: those end this
     hold only once the element moved off its source. */
  const takePlace = () => takeSlot(true, []);
  function holdPlace(media, slot) {
    const ends = ['loadeddata', 'canplay', 'error', 'abort', 'emptied'];
    const src = media.src;
    let timer = null;
    const release = () => {
      if (media.removeEventListener) for (const type of ends) media.removeEventListener(type, end);
      if (timer !== null) { requestTimeout.clear(timer); timer = null; }
      slot.release();
    };
    const end = ev => { if ((ev.type === 'abort' || ev.type === 'emptied') && media.src === src) return; release(); };
    if (media.addEventListener) for (const type of ends) media.addEventListener(type, end);
    if (requestTimeout.set) timer = requestTimeout.set(release, requestTimeout.readMs);
    return release;
  }
  async function readWithRetry(path, options) {
    const signal = options.retrySignal || options.signal || null;
    // PLAIN-ADS-FIX2 (code R2-02): a retry belongs to the access and the
    // workspace of the first try. An access change during the wait drops
    // the read (nothing is sent); a moved display drops its answer.
    const session = path === '/session';
    const epoch = current.authorityEpoch;
    if (!session) options = { ...options, workspaceContext: options.workspaceContext || current.workspaceContext || '' };
    // PLAIN-ADS-FIX3 (code PA-R3-01): a caller that reads one thing more
    // than once (the clip's retry of a dropped read) passes one budget to
    // every read, so all of them share 2 retries and 3 s of waiting.
    const budget = options.retryBudget || { retries: 0, waited: 0 };
    // READ-QUEUE: a read that got 429 keeps its place through the wait
    // (apiCall leaves it here), so no queued read meets the full server.
    const lease = { slot: null };
    options = { ...options, slotLease: lease };
    let mark = null;
    try {
      for (;;) {
        // The page-level note ("Trying again automatically") belongs to
        // the last try only.
        if (mark && current.rateLimitedRead === mark) current.rateLimitedRead = null;
        try {
          return await apiCall(path, options);
        } catch (err) {
          const ms = err && err.status === 429 && err.retryAfter > 0 ? err.retryAfter * 1000 : 0;
          if (!ms || budget.retries >= READ_RETRY.max || budget.waited + ms > READ_RETRY.budgetMs
            || (signal && signal.aborted) || !requestTimeout.set) throw err;
          mark = current.rateLimitedRead;
          budget.retries += 1;
          budget.waited += ms;
          await retryWait(ms, signal);
          // RQ-R3-01: a download is not stopped by a page leave, also
          // while it waits (apiCall drops its answer on a workspace, person
          // or session change).
          if (!session && !options.download && !authorityHolds(epoch)) throw authorityDropped();
        }
      }
    } finally {
      if (lease.slot) { lease.slot.release(); lease.slot = null; }
    }
  }
  function retryWait(ms, signal) {
    return new Promise((resolve, reject) => {
      const stop = () => { requestTimeout.clear(timer); reject(abortError()); };
      const timer = requestTimeout.set(() => { if (signal) signal.removeEventListener('abort', stop); resolve(); }, ms);
      if (signal) signal.addEventListener('abort', stop, { once: true });
    });
  }
  async function apiCall(path, options = {}) {
    const { method = 'GET', body = null, signal: callerSignal = null, workspaceContext = null } = options;
    // The caller may pin the expected context; otherwise the displayed
    // context is captured at dispatch. After the await, the DISPLAYED
    // context must still match the captured one before ANY non-adopt
    // return: a stale server echo that equals the dispatch value is
    // still late when the display has moved on. Adopt paths learn the
    // context only when the display has not moved meanwhile, so
    // superseded session/switch responses are rejected too. Abort and
    // transport failures throw before any of this and are never
    // treated as empty data.
    const dispatchContext = workspaceContext || current.workspaceContext || '';
    // Dispatch stamps: the route render current now, the canonical
    // reconcile sequence (a reconcile started later reflects server
    // state at least as new as this request), and the workspace a write
    // is sent from.
    const dispatchToken = current.fetchToken;
    const dispatchSeq = current.reconcileSeq || 0;
    const dispatchWorkspace = current.activeWorkspace || '';
    // DEMO2 pass 20: the person this request was sent for. DEMO2 pass 21:
    // an operation passes the stamp it took when it started, so its
    // answers are judged against that moment, not this dispatch.
    const dispatchViews = window.EWViews;
    const opIdentity = options.identity
      || (dispatchViews && typeof dispatchViews.operationStamp === 'function' ? dispatchViews.operationStamp() : null)
      || null;
    const dispatchIdentity = opIdentity
      || (dispatchViews && typeof dispatchViews.identityStamp === 'function' ? dispatchViews.identityStamp() : null);
    const dropLate = (out) => {
      const err = new Error('The workspace changed while this read was in flight; its response was dropped.');
      err.status = 0;
      err.droppedContext = true;
      // DEMO2 pass 22: a newer session operation was issued; its own
      // flow adopts and renders.
      if (sessionSuperseded()) err.supersededSession = true;
      err.body = out;
      throw err;
    };
    const isWrite = method !== 'GET' && method !== 'HEAD';
    const sessionOp = path === '/session'
      || (path === '/session/workspace' && method === 'POST')
      || (path === '/session/login' && method === 'POST')
      || (path === '/session/password' && method === 'POST')
      || (path === '/session/password/change' && method === 'POST')
      || (path === '/session/logout' && method === 'POST');
    const exactGuestOp = guestEntryRoute()
      && (path.startsWith('/shared/') || /\/invites\/[^/]+\/(accept|preflight)$/.test(path));
    const researchQuery = path === '/customer/research-query';
    // READ-QUEUE-FIX1 (RQ-R1-01): a stored export's file, read as a
    // person's action (options.press) and returned as its bytes and name.
    const download = options.download === true;
    // DEMO2 pass 21b (owner decision): an operation whose stamp no longer
    // holds (another person was adopted since it started, even if the
    // first one came back) sends nothing. It is dropped here, before any
    // state changes or fetch, as a superseded request: every caller paints
    // nothing for those, and the new person's view renders fresh. Only
    // the canonical session read and sign-in/sign-out still go out; the
    // workspace switch is the operation's own request and does not.
    const sendExempt = (path === '/session' && method === 'GET')
      || ((path === '/session/login' || path === '/session/logout') && method === 'POST');
    // DEMO2 pass 22 (owner decision): session reads, sign-in and sign-out
    // are numbered in the order they are issued. Only the newest issued
    // one is adopted; an older answer (success, refusal or failure) is
    // discarded, whatever the workspace token says. Two people with no
    // active workspace share the empty token, so only this order tells
    // an old person's session from the new one's.
    // DEMO2 pass 22b (owner decision): sign-in and sign-out also advance
    // a session-change sequence of their own. Only a newer sign-in or
    // sign-out supersedes them; a session read never does. When one
    // answers, every session read issued before that answer is discarded,
    // so an older read never overwrites its result.
    const sessionTurn = sendExempt ? (current.sessionSeq = (current.sessionSeq || 0) + 1) : 0;
    const sessionChange = sendExempt && method === 'POST';
    const changeTurn = sessionChange ? (current.sessionChangeSeq = (current.sessionChangeSeq || 0) + 1) : 0;
    const sessionSuperseded = () => (sessionChange ? current.sessionChangeSeq !== changeTurn
      : sessionTurn > 0 && current.sessionSeq !== sessionTurn);
    if (sessionTurn && !sessionChange) sessionReadsOpen += 1;
    // DEMO2 pass 24: the reader learns which session read it adopts.
    if (sessionTurn && options.sessionTicket) options.sessionTicket.turn = sessionTurn;
    if (opIdentity && !sendExempt && dispatchViews && typeof dispatchViews.identityHolds === 'function'
        && !dispatchViews.identityHolds(opIdentity)) {
      const err = new Error('This action started before another person signed in. Nothing was sent.');
      err.status = 0;
      err.droppedContext = true;
      err.staleOperation = true;
      throw err;
    }
    // Wrong-workspace write fence: while a switch outcome is uncertain,
    // workspace-bound mutations refuse BEFORE dispatch — never after a
    // committed-but-unacknowledged result. Reads (including the
    // read-only research query POST), the canonical session read, the
    // authentication/switch endpoints and exact guest-entry operations
    // pass — the same set the mutation epoch exempts; everything else
    // waits for reconcile. Old drafts never replay: they belong to the
    // origin workspace and re-render from the reconciled one.
    if (current.writeFence && isWrite && !sessionOp && !exactGuestOp && !researchQuery) {
      const err = new Error(fenceNotice());
      err.status = 0;
      err.writeFenced = true;
      throw err;
    }
    // Positive current compatible admission at ordinary dispatch:
    // reads AND writes refuse on a refused marker. Canonical session
    // reads, workspace/auth switches, and exact-entry named operations
    // pass; everything else waits for a compatible admission. A
    // refusal never gains an old-success bypass.
    if (current.bootRefused && !current.bootAdmitted && !sessionOp && !exactGuestOp) {
      const err = new Error('Workspace session is unconfirmed: reload or switch workspace to restore access.');
      err.status = 0;
      err.refusedAdmission = true;
      throw err;
    }
    // Common server mutation epoch: an ordinary hosted browser-session
    // mutation carries the DISPLAYED view/action epoch, captured here
    // at dispatch before any await — never the newest canonical read
    // alone, which could relabel old displayed values with a new epoch
    // the server would accept into the wrong workspace. A missing or
    // malformed displayed basis refuses the mutation before fetch,
    // never an invented 0. The header orders; it never authenticates.
    // The switch POST keeps its own body CAS, auth endpoints are
    // unchanged, and adopt reads, exact-entry named operations and the
    // read-only research query pass untouched. A handler that awaits
    // before its write pins the click-time basis as displayedRevision;
    // it validates exactly like the dispatch-time read.
    const epochExempt = sessionOp || exactGuestOp || researchQuery;
    let epochRev = null;
    if (isWrite && current.hosted && !epochExempt) {
      const rev = 'displayedRevision' in options ? options.displayedRevision : current.displayedWorkspaceSwitchRevision;
      if (typeof rev !== 'number' || !Number.isInteger(rev) || rev < 0) {
        const err = new Error('Nothing was saved. Reload the workspace to continue.');
        err.status = 0;
        err.epochRefused = true;
        // Wherever it surfaces, the way forward is on the page.
        const views = window.EWViews;
        if (views && typeof views.reloadNotice === 'function') views.reloadNotice(err.message);
        throw err;
      }
      epochRev = rev;
    }
    const headers = {};
    let payload = null;
    if (body !== null && body !== undefined) {
      payload = JSON.stringify(body);
      headers['Content-Type'] = 'application/json';
    }
    // A refused contract adopts no token; its session-scoped token stays
    // usable for session ops only (switch, sign-out) so the user can leave.
    if (isWrite) headers['X-CSRF-Token'] = current.csrf || (sessionOp && current.sessionCsrf) || '';
    if (epochRev !== null) headers['X-EarWitness-Workspace-Switch-Revision'] = String(epochRev);
    // Workspace-changing single-flight: no second workspace POST
    // dispatches while one is outstanding — an older outstanding
    // request may still commit server-side after a newer one, and a
    // dropped response never cancels the server mutation. The flag
    // covers the network window only; uncertainty after settlement
    // stays on the write fence for reconcile/recovery, so a settled
    // switch never deadlocks its retry.
    const isSwitchPost = path === '/session/workspace' && method === 'POST';
    if (isSwitchPost) {
      if (current.switchPending) {
        const busy = new Error('A workspace switch is already in progress. Wait for it to settle, then try again.');
        busy.status = 0;
        busy.switchPending = true;
        throw busy;
      }
      current.switchPending = true;
    }
    // A write's outcome that arrives after the display moved belongs to
    // the workspace it was sent from. It is never returned to a caller
    // that could render or act on it in the current view: it throws a
    // superseded outcome (every caller paints nothing for those) and is
    // announced once, here, as information — never as a failure.
    const lateWriteCandidate = isWrite && !sessionOp && !researchQuery && !exactGuestOp;
    // DEMO2-R13-N1: 'roleSaved' is a save committed in the workspace on
    // screen whose answer carried a token that changed only in role.
    const lateWrite = (kind) => {
      const where = dispatchWorkspace ? workspaceName(dispatchWorkspace) : 'the previous workspace';
      // Local mode has no workspaces to name.
      const message = kind === 'roleSaved' ? 'Saved. Your role in this workspace changed.'
        : !current.hosted
          ? (kind === 'saved' ? 'Saved.' : kind === 'rejected' ? 'Your change was not saved.' : 'Your change could not be confirmed.')
          : kind === 'saved' ? `Saved in ${where}.`
            : kind === 'rejected' ? `Your change in ${where} was not saved.`
              : `Your change in ${where} could not be confirmed.`;
      const err = new Error(message);
      err.status = 0;
      err.droppedContext = true;
      err.lateWrite = kind === 'roleSaved' ? 'saved' : kind;
      err.workspace = dispatchWorkspace || 'the previous workspace';
      if (kind === 'saved' || kind === 'roleSaved') err.committedElsewhere = true;
      if (kind === 'roleSaved') err.roleChanged = true;
      // DEMO2-SEC-R9-01 / R16-01: another person was adopted since this
      // write was sent. Its server effect stands; nothing is announced
      // or recorded for the person now signed in.
      const views = window.EWViews;
      if (views && typeof views.identityHolds === 'function' && !views.identityHolds(dispatchIdentity)) return err;
      const announce = (window.EW && window.EW.toast) || toast;
      announce(message, { low: true });
      // The toast passes; the page keeps a durable record of where it went.
      if (views && typeof views.noteOutcome === 'function') {
        if (kind === 'roleSaved') views.noteOutcome({ roleSaved: true });
        else if (kind === 'saved' && current.hosted && dispatchWorkspace) views.noteOutcome({ saved: dispatchWorkspace });
      }
      return err;
    };
    const displayMoved = () => (current.workspaceContext || '') !== dispatchContext;
    // DEMO2-R17-02 (owner decision, pass 21): another person was adopted
    // since dispatch. The context token is a hash with no per-session
    // part, so after A -> B -> A it can be the same again: a moved
    // identity makes the answer late exactly like a moved token. Session
    // operations order themselves (switch revision, reconcile) and are
    // judged by the token alone.
    const identityMoved = () => {
      const views = window.EWViews;
      return !sessionOp && !!dispatchIdentity && !!views && typeof views.identityHolds === 'function'
        && !views.identityHolds(dispatchIdentity);
    };
    // A workspace switch is waiting to set its fence: a new ordinary
    // write would race it, so it refuses before dispatch.
    if (lateWriteCandidate && current.switchWaiting) {
      const err = new Error('A workspace switch is in progress. Nothing was saved.');
      err.status = 0;
      err.switchWaiting = true;
      throw err;
    }
    // In-flight ordinary writes: workspace switching waits for them to
    // settle (whenWritesSettled), so no switch races a commit.
    if (lateWriteCandidate) current.writesInFlight = (current.writesInFlight || 0) + 1;
    // DEMO2 pass 14 network gate: every workspace read (the research
    // query is a read sent as POST) is stamped and abortable at a
    // boundary. Session operations order themselves (switch revision,
    // reconcile) and stay outside it. A download is not a page's read:
    // leaving the page keeps it; a person or workspace change drops it.
    const gated = !sessionOp && !download && (!isWrite || researchQuery);
    const sentEpoch = current.authorityEpoch;
    let signal = callerSignal, gate = null;
    if (gated) {
      gate = new AbortController();
      if (callerSignal && callerSignal.aborted) gate.abort();
      else if (callerSignal && callerSignal.addEventListener) callerSignal.addEventListener('abort', () => gate.abort(), { once: true });
      inflightReads.add(gate);
      signal = gate.signal;
    }
    // Whether this request belongs to a render that a refusal boundary
    // started (its own fresh check): see EWViews.refusalBoundary.
    const recheck = current.recheckToken !== null && current.recheckToken === current.fetchToken
      && current.publishedToken !== current.fetchToken;
    let resp, text;
    // A write that got no answer (timeout or transport failure) may
    // still have committed. Status stays undefined: every write path
    // reads it as "could not be confirmed", never failed-and-retryable.
    // Reads show a timeout as a failure with Retry.
    const unknownOutcome = (cause) => {
      const err = new Error('Your change could not be confirmed.');
      err.outcomeUnknown = true;
      if (cause) err.cause = cause;
      return err;
    };
    const timedOut = () => {
      // The research query is a read sent as POST: a timeout is a slow
      // read with Retry, never an unconfirmed change.
      const err = isWrite && !researchQuery ? unknownOutcome() : new Error('The server took too long to respond.');
      err.timedOut = true;
      return err;
    };
    let failed = null;
    // READ-QUEUE: the server counts every customer request, session
    // operations aside. A write (not the read-only research query) or a
    // clip press jumps the queue. Dispatch values above were taken at
    // the call; the request timeout starts when it is sent.
    const lease = options.slotLease || null;
    let slot = null, sent = null, netGate = null, refused = null;
    const sendTimeRefusal = () => {
      const views = window.EWViews;
      let err = null;
      if (opIdentity && views && typeof views.identityHolds === 'function' && !views.identityHolds(opIdentity)) {
        err = new Error('This action started before another person signed in. Nothing was sent.');
        err.droppedContext = true;
        err.staleOperation = true;
      } else if (download) {
        return null;
      } else if (current.writeFence) {
        err = new Error(fenceNotice());
        err.writeFenced = true;
      } else if (current.switchWaiting) {
        err = new Error('A workspace switch is in progress. Nothing was saved.');
        err.switchWaiting = true;
      }
      if (err) err.status = 0;
      return err;
    };
    // SPEED-R1 (R1-16): one research query at a time per tab. A newer one
    // waits here, unsent, until the one before it has its answer; a view
    // left (or an access boundary) meanwhile takes it out unsent. Quick
    // view changes (pages, sort, Cards and List, Back) send the first and
    // the last query, not one each, and the last no longer queues on the
    // server behind the ones nobody waits for.
    let laneAfter = null, laneFree = null;
    if (researchQuery) {
      // A free lane is not waited for: the query keeps its place in the
      // page's order (READ-QUEUE).
      const before = researchLane;
      if (researchOpen) laneAfter = before;
      researchOpen += 1;
      let open = true;
      const mine = new Promise(res => { laneFree = () => { if (open) { open = false; researchOpen -= 1; res(); } }; });
      researchLane = before.then(() => mine);
    }
    try {
      if (laneAfter) await untilAbort(laneAfter, signal);
      if (!sessionOp) {
        if (lease && lease.slot) { slot = lease.slot; lease.slot = null; }
        else {
          const got = takeSlot((isWrite && !researchQuery) || options.press === true, [signal, options.retrySignal]);
          slot = typeof got.then === 'function' ? await got : got;
          // RQ-R1-07: aborted in the tick its place was granted: not sent.
          if ([signal, options.retrySignal].some(sig => sig && sig.aborted)) throw abortError();
        }
      }
      // READ-QUEUE-FIX1 (RQ-R1-03): a write (or a download) that waited
      // for its place is checked again before it is sent, as it was when
      // asked for: the person, the write fence, a waiting switch.
      const late = (lateWriteCandidate || download) && sendTimeRefusal();
      if (late) { refused = late; throw late; }
      // A counted read is not aborted by its caller once sent, only by an
      // authority boundary or its timeout: its place frees when the
      // server has answered it.
      if (slot && gate) { netGate = new AbortController(); inflightSends.add(netGate); }
      sent = timedFetch('/api/v1' + path, { method, headers, body: payload, signal: netGate ? netGate.signal : signal },
        // RQ-SCR-R3-03: a stored file has the export's limit too.
        path === '/customer/research-export' || download ? requestTimeout.exportMs
          : isWrite ? requestTimeout.writeMs : requestTimeout.readMs, timedOut);
      if (netGate) { const g = netGate; sent.then(() => inflightSends.delete(g), () => inflightSends.delete(g)); }
      ({ resp, text } = await (netGate ? untilAbort(sent, signal) : sent));
    } catch (terr) {
      failed = { terr };
    } finally {
      if (laneFree) { if (sent) sent.then(laneFree, laneFree); else laneFree(); }
      if (slot) {
        if (!failed && resp && resp.status === 429 && lease) lease.slot = slot;
        // READ-QUEUE-FIX1 (RQ-R1-01): a clip press's record read hands its
        // place to the clip's audio; the caller releases it.
        else if (!failed && resp && resp.ok && options.keepPlace) options.keepPlace.slot = slot;
        // Its caller left while the server still works on it: free the
        // place when the answer (or the timeout, or a failure) comes.
        else if (failed && sent) { const s = slot; sent.then(() => s.release(), () => s.release()); }
        else slot.release();
      }
      if (gate) inflightReads.delete(gate);
      if (isSwitchPost) current.switchPending = false;
      // DEMO2 pass 23 (owner decision): every sign-in or sign-out answer,
      // adopted or superseded, discards every session read issued before
      // it. A superseded one may still have changed the server session.
      if (sessionChange) current.sessionSeq = (current.sessionSeq || 0) + 1;
      if (sessionTurn && !sessionChange && !--sessionReadsOpen) sessionReadsIdle();
      if (lateWriteCandidate) {
        current.writesInFlight = Math.max(0, (current.writesInFlight || 1) - 1);
        if (!current.writesInFlight) { const w = writeWaiters.splice(0); for (const res of w) res(); }
      }
    }
    /* DEMO2 pass 24 (owner decision): while a tab return's session read
       is pending, every non-session answer (a read, a write's answer, a
       player load, a failure) waits here, unpainted. The same person:
       they go on in order. Another person or signed out: dropped. */
    if (!sessionOp) {
      for (let gate = returnGate(); gate; gate = returnGate()) {
        if (await gate.verdict === 'drop') {
          const err = new Error('Another person or no one is signed in now; this answer was dropped.');
          err.status = 0;
          err.droppedContext = true;
          err.returnDropped = true;
          throw err;
        }
      }
    }
    if (refused) throw refused;
    if (failed) {
      const { terr } = failed;
      // A read aborted by a boundary, or failing after one: dropped.
      if (gated && !authorityHolds(sentEpoch)) throw authorityDropped();
      if (sessionSuperseded() && !(terr && terr.name === 'AbortError')) dropLate(null);
      // Transport failure after the display moved: unknown outcome there.
      if (lateWriteCandidate && (displayMoved() || identityMoved()) && !(terr && terr.name === 'AbortError')) throw lateWrite('unknown');
      if (lateWriteCandidate && !(terr && (terr.name === 'AbortError' || terr.timedOut))) throw unknownOutcome(terr);
      throw terr;
    }
    // A read that answers under another authority epoch is never
    // delivered: a boundary passed after it was sent.
    if (gated && !authorityHolds(sentEpoch)) throw authorityDropped();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (!resp.ok || (data && data.ok === false)) {
      // A failure that arrives after the display moved on belongs to a
      // superseded request: drop it instead of toasting stale errors.
      // The comparison is exact: a cleared display ('') no longer
      // matches a dispatched context ('A'), so its late failure drops
      // instead of surfacing as a live error.
      const liveErr = current.workspaceContext || '';
      if (liveErr !== dispatchContext || identityMoved() || sessionSuperseded()) {
        // A definite rejection (4xx) was not saved; a server error leaves
        // the outcome unknown.
        if (lateWriteCandidate) throw lateWrite(resp.status >= 500 ? 'unknown' : 'rejected');
        dropLate(data || {});
      }
      // Server errors arrive as objects ({code, message, ...}): never
      // interpolate the object itself (that renders "[object Object]").
      const bodyErr = data && data.error;
      const serverText = typeof bodyErr === 'string' ? bodyErr : (bodyErr && bodyErr.message) || '';
      // A rate limit always reads as plain guidance: the server's own
      // words ("slow down and retry") are not customer copy.
      const rateLimited = resp.status === 429;
      let message = rateLimited ? statusCopy(429) : (serverText || statusCopy(resp.status));
      const code = (bodyErr && typeof bodyErr === 'object' && bodyErr.code) || (data && data.code) || '';
      message = plainServerText(message, code);
      const ordinaryWrite = isWrite && !epochExempt;
      // A stale ordinary write never retries or rebases: it reports in
      // product language and the views re-read the canonical session
      // once (single-flight) so the displayed basis is invalidated.
      const workspaceConflict = ordinaryWrite && resp.status === 409 && code === 'WORKSPACE_SWITCH_CONFLICT';
      // RENO-R1-01: the server says no one is signed in (any request).
      const signinRequired = resp.status === 401 && (code === 'SIGNIN_REQUIRED' || !!(data && data.signin));
      const signedOut = ordinaryWrite && signinRequired;
      if (workspaceConflict) message = switching() ? neutralCopy(notices.conflictAction) : notices.conflictAction;
      if (signedOut) message = notices.signedOutAction;
      const err = new Error(message);
      err.status = resp.status;
      err.body = data;
      // The message is the server's own words, or plain guidance mapped
      // from its code — not the bare status fallback.
      err.serverMessage = (!!serverText && !rateLimited) || message !== statusCopy(resp.status);
      if (rateLimited) {
        err.rateLimited = true;
        // Retry-After in whole seconds (an HTTP date is ignored). Only a
        // page read records it, for one bounded automatic retry of the
        // render it belongs to; a write never retries on its own.
        const header = resp.headers && typeof resp.headers.get === 'function' ? resp.headers.get('Retry-After') : null;
        const secs = header !== null && /^\s*\d+\s*$/.test(String(header)) ? Number(header) : null;
        if (secs !== null) err.retryAfter = secs;
        if (!isWrite) current.rateLimitedRead = { token: dispatchToken, retryAfter: secs };
      }
      // M9-BACK: a read of this render was refused (401, 403, 428): a kept
      // page is never left up beside its failure. The research query is a
      // read (a POST only for its body).
      const views = window.EWViews;
      const pageRead = !isWrite || path === '/customer/research-query';
      if (pageRead && (resp.status === 401 || resp.status === 403 || resp.status === 428)) {
        current.refusedRead = dispatchToken;
        if (views && typeof views.dropKeptPaint === 'function') views.dropKeptPaint(dispatchToken);
      }
      if (workspaceConflict) {
        err.workspaceConflict = true;
        err.duringSwitch = switching();
        if (views && typeof views.reconcileCanonical === 'function') views.reconcileCanonical({ conflict: err });
      }
      // DEMO2-SEC-R1-01 / pass 14: an access refusal is an authority
      // boundary: nothing kept from before is published again without
      // asking the server, and the page and dialogs are checked again.
      // DEMO2-R12-01: a signed-out write (Save after the session expired)
      // takes the same boundary: typed entries are kept, the page and
      // dialogs lose every server-derived value.
      // A wrong password is the credential form's own answer, not a
      // sign-out: the sign-in call's SIGNIN_FAILED and Change password's
      // PASSWORD_INCORRECT stay in their form, which shows the refusal.
      // Every other 401 still starts the boundary.
      const credentialRefusal = method === 'POST' && resp.status === 401
        && ((path === '/session/password' && code === 'SIGNIN_FAILED')
          || (path === '/session/password/change' && code === 'PASSWORD_INCORRECT'));
      // RENO-R1-01: the session read's own "sign in" is the signed-out
      // state, which boot adopts and the page renders (the sign-in page).
      // It runs the 401 boundary's teardown (RR2-01: views.signedOutBoundary)
      // but refuses no page. A tab return on the sign-in page reads it.
      const sessionState = signinRequired && method === 'GET' && path === '/session';
      if (!credentialRefusal && startsBoundary(resp.status, err)) {
        err.accessRefused = true;
        if (sessionState && views && typeof views.signedOutBoundary === 'function') views.signedOutBoundary();
        else if (!views || typeof views.refusalBoundary !== 'function' || sessionState) forgetRetainedViews();
        // A page read's "sign in": the boundary's check reads the session
        // first (a write's own follow-up reads it: followSignedOut).
        else views.refusalBoundary({ recheck, message, signin: signinRequired && !signedOut });
      } else if (roleChanged(resp.status, err)) {
        // DEMO2-SEC-R6-01 (owner decision): the role changed. Not lost
        // access, but a soft boundary: the epoch advances (reads in flight
        // abort and drop), every retained store and page-captured cache is
        // cleared, and the current route is read again, without the
        // "Checking access…" page.
        err.roleChanged = true;
        roleBoundary({ refused: true });
      }
      if (signedOut) {
        err.signedOut = true;
        if (views && typeof views.followSignedOut === 'function') views.followSignedOut(err);
      }
      throw err;
    }
    // A successful ordinary write committed (late or not): the switch
    // copy never claims "Nothing was saved" across such a commit.
    if (lateWriteCandidate) current.writeCommits = (current.writeCommits || 0) + 1;
    const out = data || {};
    const ctx = out.workspace_context;
    // Exact captured/displayed comparison: after the display clears,
    // live ('') differs from the dispatched context and the stale
    // response drops. Equal empties (initial boot, no context yet)
    // still proceed so session adoption works.
    const adopt = path === '/session' || path === '/session/workspace';
    // A successful ordinary write was admitted under its own epoch, so it
    // committed in the workspace it was sent from. It is never dropped as
    // late: it returns marked, and callers report it there without
    // rendering its payload into whatever view is current now.
    const live = current.workspaceContext || '';
    if (live !== dispatchContext || identityMoved() || sessionSuperseded()) {
      if (lateWriteCandidate) throw lateWrite('saved');
      dropLate(out);
    }
    if (download) {
      const header = k => (resp.headers && typeof resp.headers.get === 'function' ? resp.headers.get(k) : null) || '';
      const name = /filename="([^"]+)"/.exec(header('Content-Disposition'));
      return { text: text || '', type: header('Content-Type'), filename: name ? name[1] : '' };
    }
    // Session/switch metadata validates before anything is adopted: an
    // incompatible marker throws here, so no caller can adopt a context
    // the contract refuses. Reads/writes stay closed until a compatible
    // admission, never on a settled flag alone. Refusal of any canonical
    // session/switch response invalidates ordinary admission BEFORE the
    // failure escapes — including the switch POST itself and access
    // refreshes, not only the reconcile read.
    const adoptCanonical = () => {
      const prevRev = current.workspaceSwitchRevision;
      const prevCtx = current.workspaceContext || '';
      // Ordered switch sequencing: the canonical session carries its
      // workspace_switch_revision (integer >= 0, also unchosen). A
      // missing or malformed field INVALIDATES any retained basis —
      // it never survives as fresh beyond-revision proof, and the next
      // selection refuses instead of ordering on stale authority.
      const adoptRevision = () => {
        if (typeof out.workspace_switch_revision === 'number'
            && Number.isInteger(out.workspace_switch_revision)
            && out.workspace_switch_revision >= 0) {
          current.workspaceSwitchRevision = out.workspace_switch_revision;
        } else {
          current.workspaceSwitchRevision = undefined;
        }
      };
      // Any adopted canonical revision beyond an uncertain switch's
      // initiating revision orders it: that request can no longer
      // commit, so its fence releases here on every adopt path (switch
      // reply, reconcile read, plain refresh). The release records its
      // operation so that switch's own reconcile still completes.
      const releaseFence = () => {
        const fence = current.writeFence;
        if (fence && typeof fence.rev === 'number' && current.workspaceSwitchRevision > fence.rev) {
          current.writeFence = null;
          current.fenceReleasedOp = fence.op;
        }
      };
      try { requireContract(out); }
      catch (cerr) {
        if (cerr && cerr.contractMismatch) {
          current.bootAdmitted = false;
          current.bootRefused = cerr.message || 'Starting the workspace failed.';
          // A refusal is not a failed read: Retry would only refuse again.
          current.bootRetry = false;
          current.displayedWorkspaceSwitchRevision = undefined;
          // The workspace the refusal is about: its chooser preselects
          // another one when any exists.
          current.refusedWorkspace = (cerr.body && cerr.body.active_workspace) || '';
          // The session-level revision is still canonical (a 200 switch
          // committed it): it orders the next explicit switch out of the
          // refused workspace. No context, marker or ordinary basis is
          // adopted from a refused response.
          adoptRevision();
          releaseFence();
        }
        throw cerr;
      }
      current.refusedWorkspace = '';
      adoptRevision();
      releaseFence();
      // DEMO2-SEC-R6-01: an adopted signed-in session whose token moved
      // while the person and the workspace stayed and the role changed is a
      // role change (soft boundary), never a workspace change.
      const roleMoved = out.authenticated === true && typeof ctx === 'string' && !!ctx && !!prevCtx && ctx !== prevCtx
        && typeof current.sessionRole === 'string' && typeof out.role === 'string' && out.role !== current.sessionRole
        && (out.active_workspace || '') === (current.activeWorkspace || '') && out.principal === current.principal;
      if (out.authenticated === true) current.sessionRole = typeof out.role === 'string' ? out.role : null;
      if (typeof ctx === 'string' && ctx) current.workspaceContext = ctx;
      if (current.workspaceSwitchRevision !== prevRev || (current.workspaceContext || '') !== prevCtx) {
        // The canonical epoch/context moved: any displayed form/action
        // basis is stale — invalidate it rather than relabeling old
        // values with the new epoch. A fresh render publication
        // re-establishes it; until then ordinary writes hold.
        current.displayedWorkspaceSwitchRevision = undefined;
      }
      if (roleMoved) {
        current.roleChange = { from: prevCtx, to: ctx };
        // The boot that adopted this session keeps workspace state (no
        // clearWorkspaceState): the soft boundary below owns it.
        Object.defineProperty(out, 'roleChangedOnly', { value: true });
        // TR-R1-01: the boundary's render is the first to draw the new
        // role, so it adopts what this session shows first.
        roleBoundary({ session: out });
      }
    };
    if (adopt) {
      // DEMO2 pass 26 (owner decision 1): a session read's reader (boot,
      // Settings) adopts it itself (sessionTicket.adopt), once it has
      // checked that no newer session read was issued meanwhile. Nothing
      // is set here before that check.
      const ticket = path === '/session' && method === 'GET' ? options.sessionTicket : null;
      if (ticket) { ticket.adopt = adoptCanonical; return out; }
      adoptCanonical();
      return out;
    }
    if (typeof ctx === 'string' && ctx) {
      if (!dispatchContext) current.workspaceContext = ctx;
      else if (ctx !== dispatchContext) {
        // The display has not moved but the server's session names
        // another workspace (e.g. a switch committed while its reply was
        // lost): the response is still dropped, and the views reconcile
        // once through the canonical session read instead of dropping
        // every later read against the stale display.
        // Exact guest-entry operations carry their own grant context and
        // never reconcile the member session.
        const views = window.EWViews;
        let reconciling = null;
        if (!exactGuestOp && views && typeof views.reconcileServerContext === 'function') {
          reconciling = views.reconcileServerContext(dispatchContext, ctx, { token: dispatchToken, seq: dispatchSeq });
        }
        if (lateWriteCandidate) {
          // DEMO2-R13-N1: the display has not moved, so this write
          // committed in the workspace on screen. The canonical re-read
          // says whether only the role moved; the outcome then says so.
          if (reconciling) { try { await reconciling; } catch { /* the reconcile reports itself */ } }
          const rc = current.roleChange;
          throw lateWrite(rc && rc.from === dispatchContext && rc.to === ctx ? 'roleSaved' : 'saved');
        }
        dropLate(out);
      }
    }
    if ((path === '/session/login' || path === '/session/password' || path === '/session/logout') && method === 'POST') {
      // Identity changed: the previous display belongs to another
      // principal, so its mutation basis cannot carry over, and the
      // refused-session exit token belongs to the old identity too.
      current.displayedWorkspaceSwitchRevision = undefined;
      current.sessionCsrf = '';
    }
    return out;
  }

  // DEMO2-SEC-R11-05 / pass 22b: on a change of person or workspace every
  // player (the search player outside the page content and the clip
  // players) stops, lets go of its media and leaves the page.
  function stopAudio() {
    // DEMO2 pass 23: a play started before this teardown never speaks.
    current.audioEpoch = (current.audioEpoch || 0) + 1;
    const players = document.querySelectorAll ? document.querySelectorAll('audio') : [];
    for (const audio of players) {
      try { audio.pause(); } catch { /* not playing */ }
      audio.removeAttribute('src');
      try { audio.load(); } catch { /* nothing loaded */ }
      audio.remove();
    }
  }

  /* Workspace switch: clear workspace-bound navigation, read caches and
     drafts so the new workspace never inherits the old one's origins. */
  // `boundary: false` (a change of person, DEMO2M-R4-01): the state goes
  // but no authority boundary is crossed here; the previous person's late
  // answers are already dropped by the identity checks (identityMoved,
  // op.live), and a render in progress for the adopted person publishes.
  function clearWorkspaceState({ keepDialog = false, boundary = true } = {}) {
    invalidateSharedReads();
    current.back.clear();
    current.scroll.clear();
    current.focus.clear();
    current.dataset = null;
    current.directory = {};
    current.filters = {};
    current.staleDraft = null;
    current.savedExcerpt = null;
    current.savedNote = null;
    clearRetainedStores();
    if (boundary) authorityBoundary();
    current.pendingSearch = null;
    current.displayedAlert = null;
    current.displayedDraft = null;
    current.savedAlert = null;
    // Entry links and invite IDs belong to the workspace they were made in.
    current.createdInvite = null;
    current.createdShare = null;
    current.dirtyForm = null;
    current.dirtyWorkspace = null;
    // DEMO2M-R4-02: the forms an unsaved-changes prompt listed.
    current.promptDrafts = null;
    current.dialogDrafts.clear();
    current.displayedWorkspaceSwitchRevision = undefined;
    // Plan A excerpt view: the plan level belongs to the workspace.
    current.excerptOnly = false;
    current.excerptOnlyContext = '';
    for (const [, c] of current.aborters) { try { c.abort(); } catch { /* replaced */ } }
    current.aborters.clear();
    // DEMO2 pass 22b (owner decision): audio rights are per workspace.
    stopAudio();
    // A held draft inside a dialog stays visible (submit disabled) for
    // the explicit Reload workspace step instead of vanishing here.
    if (!keepDialog) closeDialog();
  }

  // DEMO2M-R3-01 (owner: close the class): every per-person field in one
  // place, run on sign-out (boot) and when another person is adopted
  // (settleIdentity). Retained workspace reads go with the context
  // (clearWorkspaceState) and on a 401 (the refusal boundary).
  // tests/frontend_query's "DEMO2M-R3 guard" classifies every current field.
  function clearPersonState() {
    current.inviteSends = null;
    current.inviteAlert = null;
    current.inviteAccepted = null;
    current.inviteError = null;
    current.createdInvite = null;
    current.createdShare = null;
    current.searchInspect = null;
    current.searchPrefNote = null;
    current.outcomeNotice = null;
    // The next person never inherits the previous person's plan level.
    current.excerptOnly = false;
    current.excerptOnlyContext = '';
  }

  var FRONTEND_CONTRACT = 'earwitness/customer-ui/2';

  /* Ordinary active customer reads require the exact contract marker.
     Anonymous, unconfigured, signed-no-workspace chooser, invitation
     and named-share entries keep their special admission and gain no
     general reads from this gate. Markers are never fabricated. */
  /* Exact guest-entry routes carry their own marker discovery: an
     authenticated session naming a workspace whose contract marker
     and context are both still null may proceed only on an explicit
     #/invite or #/shared entry with all required locators. Ordinary
     member and local routes stay strict, and a real marker mismatch
     still throws everywhere including entries. */
  function guestEntryRoute() {
    const parts = String(current.route || '').split('?');
    const params = new URLSearchParams(parts[1] || '');
    if (parts[0] === '#/invite') return Boolean(params.get('workspace') && params.get('id'));
    if (parts[0] === '#/shared') {
      return Boolean(params.get('workspace') && params.get('share_id') && params.get('type') && params.get('id'));
    }
    return false;
  }
  function requireContract(session) {
    if (!session) return;
    const activeCustomer = (session.authenticated === true && session.active_workspace)
      || (!session.authenticated && session.workspace_context);
    if (activeCustomer && session.frontend_contract !== FRONTEND_CONTRACT) {
      if (!session.frontend_contract && !session.workspace_context && guestEntryRoute()) return;
      const err = new Error(session.authenticated === true
        ? 'This workspace needs a different version of the app. Choose another workspace or sign out.'
        : 'This page needs a different version of the app. Reload after updating.');
      err.status = 0;
      err.contractMismatch = true;
      err.body = session;
      throw err;
    }
  }

  // Who is signed in, in words a person reads: the email when the
  // session has one, else the subject — never the raw provider URI.
  function identityOf(session) {
    return { email: typeof session.email === 'string' ? session.email : '',
      subject: typeof session.subject === 'string' ? session.subject : '' };
  }
  // The session's workspaces: IDs drive every request and comparison;
  // names are only for people. A session without names keeps IDs.
  // Always assign new objects: workspaceLabels caches on identity.
  function adoptWorkspaces(session) {
    const names = session && session.workspace_names;
    current.workspaces = (session && session.workspaces) || [];
    current.workspaceNames = names && typeof names === 'object' && !Array.isArray(names) ? { ...names } : {};
  }
  // A workspace in words a person reads: its display name, else the ID
  // (or the phrase passed in when there is no workspace to name). A
  // name another workspace also shows carries its ID ("Marketing
  // (ws-a)") so choosers and notices never name two alike; `bare`
  // skips that for a field that edits the name itself.
  function workspaceName(id, bare = false) {
    const label = shownName(current.workspaceNames || {}, id);
    if (bare || !id || label === id) return label;
    const labels = workspaceLabels();
    if (labels.has(id)) return labels.get(id);
    const key = labelKey(label);
    return [...labels.values()].some(l => labelKey(l) === key) ? `${label} (${id})` : label;
  }
  function labelKey(s) { return String(s).replace(/\s+/g, ' ').toLowerCase(); }
  function shownName(names, w) {
    const name = w && Object.prototype.hasOwnProperty.call(names, w) ? names[w] : '';
    return typeof name === 'string' && name.trim() ? name.trim() : w;
  }
  // Every workspace's label, built once per session name set. Labels
  // compare as a page shows them: case and runs of spaces aside. A
  // label another workspace also shows gains its ID; repeat until none
  // collide, since a name can read like another's label with its ID
  // ("Marketing (ws-a)"). Each workspace gains its ID at most once.
  let labelCache = { names: null, list: null, labels: new Map() };
  function workspaceLabels() {
    const names = current.workspaceNames || {}, list = current.workspaces || [];
    if (labelCache.names === names && labelCache.list === list) return labelCache.labels;
    const ids = [...new Set([...Object.keys(names), ...list])];
    const labels = new Map(ids.map(w => [w, shownName(names, w)]));
    const keys = new Map(ids.map(w => [w, labelKey(labels.get(w))]));
    for (let changed = true; changed;) {
      changed = false;
      const counts = new Map();
      for (const k of keys.values()) counts.set(k, (counts.get(k) || 0) + 1);
      for (const w of ids) {
        const l = labels.get(w);
        if (l === w || l !== shownName(names, w) || counts.get(keys.get(w)) < 2) continue;
        labels.set(w, `${l} (${w})`); keys.set(w, `${keys.get(w)} (${w})`); changed = true;
      }
    }
    labelCache = { names, list, labels };
    return labels;
  }
  // A server token ("past_due") as plain words ("Past due"), for
  // values with no mapped copy.
  function plainWords(v) {
    const t = String(v ?? '').replace(/[_-]+/g, ' ').trim();
    return t ? t[0].toUpperCase() + t.slice(1) : '';
  }
  // Requirement outcomes as customers read them; the raw token stays
  // in Technical details where a page shows it.
  const REQUIREMENT_OUTCOMES = {
    supported: 'Supported by the stored evidence',
    pending: 'Pending — the placement window is still open',
    ambiguous: "Unclear — the evidence can't settle it",
    contradicted: 'Contradicted by verified evidence',
    incomplete_coverage: "Incomplete — some sources in this window couldn't be checked",
    no_observation_in_covered_sources: 'No match found in the sources we checked',
    unreviewed_detections_in_scope: "Ads in scope are waiting for review, so a match can't be ruled out",
  };
  function requirementOutcomeText(o) { return Object.hasOwn(REQUIREMENT_OUTCOMES, o) ? REQUIREMENT_OUTCOMES[o] : plainWords(o); }
  // What changed since a report was frozen, from the server's stale
  // reason tokens ("attachments:<refs>" keeps only its kind).
  const STALE_PARTS = {
    brief: 'the brief', brief_versions: 'the brief', attachments: 'attached sources', pool_refs: 'attached sources',
    'mistake-notes': 'notes', notes: 'notes', placements: 'placements', targets: 'research targets',
    coverage: 'coverage', coverage_sources: 'coverage', source_scopes: 'captured sources', source_scope_state: 'captured sources',
    allocation: 'which sources count where', research_scope: 'the research scope',
    'campaign changed since freeze': 'the campaign', campaign: 'the campaign', 'research view changed since capture': 'the research view',
  };
  // Reasons that are whole statements, never followed by "changed".
  const STALE_SAYS = {
    'campaign unavailable': 'the campaign is no longer available', 'pins unavailable': 'the capture record is unavailable',
    'campaign removed after capture': 'the campaign was removed',
    'rights withheld': 'we can’t compare it with the campaign now, because the rights for its shows don’t allow it',
  };
  function staleText(reasons) {
    const parts = (reasons || []).map(r => { const k = String(r).split(':')[0]; return Object.hasOwn(STALE_PARTS, k) ? STALE_PARTS[k] : plainWords(k).toLowerCase(); });
    return unique(parts.filter(Boolean)).join(', ');
  }
  // "the brief, coverage changed" plus any whole statements, as one line.
  function staleSummary(reasons, changed = 'changed') {
    const keys = (reasons || []).map(r => String(r).split(':')[0]);
    const says = unique(keys.filter(k => Object.hasOwn(STALE_SAYS, k)).map(k => STALE_SAYS[k]));
    const nouns = staleText(keys.filter(k => !Object.hasOwn(STALE_SAYS, k)));
    return [...says, nouns ? nouns + ' ' + changed : ''].filter(Boolean).join('; ');
  }
  // ADS-R1-02 (lead decision 10-29, the approved rights pattern): a report
  // the rights for its shows withhold keeps its row, its own number only.
  const reportWithheldLine = n => `Report ${Number(n)} can’t be shown here, because the rights for its shows don’t allow it.`;

  // A person as customers read them: the email when the server gives
  // one, otherwise the subject after the principal's last ':' — never
  // the raw "oidc:<issuer>:<subject>" principal.
  function personName(principal, email = '') {
    if (typeof email === 'string' && email) return email;
    const raw = String(principal ?? '');
    // Only a principal-shaped value ("scheme:…" with no spaces) is cut;
    // a typed name such as "Pat: marketing" stays as written.
    return /^[a-z][a-z0-9_-]*:\S+$/i.test(raw) ? raw.slice(raw.lastIndexOf(':') + 1) : raw;
  }

  function signedInName(principal = current.principal, identity = current.identity) {
    const id = identity || {};
    if (id.email) return id.email;
    if (id.subject) return id.subject;
    const raw = String(principal || '');
    return raw.startsWith('oidc:') ? raw.slice(raw.lastIndexOf(':') + 1) : raw;
  }

  async function boot({ keepDialog = false, signal = null } = {}) {
    // Loopback owner boot: session carries the CSRF token. Hosted boot:
    // the session status carries the authenticated principal, CSRF token,
    // and workspace choice; unauthenticated boot records the sign-in
    // state so the UI shows sign-in instead of failing reads.
    // A refused contract rolls back any adopted marker: ordinary dispatch
    // requires a compatible admission, never a settled flag alone.
    // The adopted session is canonical truth: when it names a different
    // workspace than the settled display, stale origins, caches, drafts
    // and dialogs belong to the old workspace and are reconciled away
    // instead of lingering under the new one. Same or initial contexts
    // reconcile to nothing.
    // DEMO2-SEC-R6-01: a token that changed only in role took the soft
    // boundary when it was adopted (apiCall): no workspace change here.
    const reconcileAdoptedContext = (prev, session) => {
      const now = current.workspaceContext || '';
      if (session && session.roleChangedOnly) return;
      // keepDialog may be a callback: decided when the context actually
      // moves, after the session round trip, not when the read started.
      if (prev && now !== prev) clearWorkspaceState({ keepDialog: typeof keepDialog === 'function' ? keepDialog() : keepDialog });
    };
    // The next render may show this adopted session instead of asking
    // again; a failed or refused boot hands nothing on.
    current.adoptedSession = null;
    // DEMO2 pass 24: which session read this boot adopts, for the
    // tab-return gate, and the first identity of this page load.
    const ticket = {};
    // DEMO2 pass 26 (owner decision 1): only the newest issued session
    // read is adopted. A newer one can be issued after the request layer
    // returned this answer and before this await resumes: then this one
    // adopts nothing, releases nothing and drops nothing.
    const superseded = () => !!ticket.turn && (current.sessionSeq || 0) !== ticket.turn;
    const supersededRead = () => {
      const err = new Error('A newer session read was issued; this answer was dropped.');
      err.status = 0;
      err.droppedContext = true;
      err.supersededSession = true;
      return err;
    };
    const adopted = () => {
      noteLoadIdentity();
      const views = window.EWViews;
      if (views && typeof views.sessionAdopted === 'function') views.sessionAdopted(ticket.turn);
    };
    try {
      const prevContext = current.workspaceContext || '';
      const session = await api('/session', { sessionTicket: ticket, signal });
      if (superseded()) throw supersededRead();
      if (ticket.adopt) ticket.adopt();
      try {
        requireContract(session);
      } catch (cerr) {
        if (cerr && cerr.contractMismatch) {
          current.workspaceContext = '';
          current.csrf = '';
          current.workspaceSwitchRevision = undefined;
          current.displayedWorkspaceSwitchRevision = undefined;
        }
        throw cerr;
      }
      // A compatible session supplies the token: no exit token remains.
      current.sessionCsrf = '';
      if (session.authenticated === true) {
        current.csrf = session.csrf_token;
        current.hosted = true;
        const prevPrincipal = current.principal || '';
        current.principal = session.principal;
        current.identity = identityOf(session);
        current.activeWorkspace = session.active_workspace || '';
        adoptWorkspaces(session);
        if (typeof session.workspace_context === 'string' && session.workspace_context) {
          current.workspaceContext = session.workspace_context;
        } else if (prevPrincipal && prevPrincipal !== session.principal) {
          // DEMO2M-R3-03: another person with no workspace: the previous
          // person's context is not theirs, so the workspace cleanup runs.
          current.workspaceContext = '';
        }
        reconcileAdoptedContext(prevContext, session);
        settleIdentity();
        current.adoptedSession = session;
        adopted();
        return { admitted: true, context: current.workspaceContext || '', fresh: !!session.workspace_context };
      }
      current.csrf = session.csrf_token;
      current.hosted = false;
      if (typeof session.workspace_context === 'string' && session.workspace_context) {
        current.workspaceContext = session.workspace_context;
      }
      reconcileAdoptedContext(prevContext);
      return { admitted: true, context: current.workspaceContext || '', fresh: !!session.workspace_context };
    } catch (err) {
      // A dropped late response keeps its body for diagnosis, but that
      // body is obsolete: never let its signin change boot state.
      if (err && err.droppedContext) throw err;
      if (superseded()) throw supersededRead();
      if (err && err.contractMismatch && err.body && err.body.authenticated === true) {
        // Refused contract on a signed-in session: no context, token or
        // ordinary basis is adopted, but the refusal keeps its exits —
        // the chooser over the session's workspaces and Sign out are
        // session ops the refusal still admits.
        current.hosted = true;
        current.principal = err.body.principal;
        current.identity = identityOf(err.body);
        current.activeWorkspace = err.body.active_workspace || '';
        adoptWorkspaces(err.body);
        current.sessionCsrf = err.body.csrf_token || '';
        settleIdentity();
        adopted();
      }
      if (err && err.body && err.body.signin) {
        // Signed-out/expired: the previous identity and workspace are
        // obsolete — clear them so a retained context can never pass
        // as a fresh admission. The sign-in state itself is kept.
        current.principal = null;
        // DEMO2M-R2-01 / R3-01: every per-person field goes.
        clearPersonState();
        current.identity = null;
        current.activeWorkspace = '';
        adoptWorkspaces(null);
        current.workspaceContext = '';
        current.workspaceSwitchRevision = undefined;
        current.displayedWorkspaceSwitchRevision = undefined;
        current.sessionCsrf = '';
        current.hosted = true;
        current.signin = err.body.signin;
        // DEMO2 pass 23 (owner decision): signed out is a change of
        // principal and of workspace: every player stops and unloads.
        stopAudio();
        adopted();
        return { admitted: false, reason: 'signin' };
      }
      throw err;
    }
  }

  // Toasts: one message at a time, as before, but no outcome is lost.
  // A newer ordinary message replaces the one showing (one immediate
  // message per action). Late-write notices are low priority: they wait
  // in a short queue (oldest dropped past five) and are never allowed to
  // hide an outcome — an ordinary message interrupting one shows at once
  // and the notice follows it. Ordinary messages are never dropped.
  // Only an immediate repeat of the last submitted message is ignored.
  const toastQueue = [];
  // G2-01: an ordinary toast may carry one action button (Undo). Its data
  // become data-* attributes, so the click runs through the same action
  // dispatch as any other button. It stays up a little longer.
  function toast(message, { low = false, action = null } = {}) {
    // DEMO2 pass 26 (owner decision 2): while a tab return holds answers,
    // a toast (a queued one included) waits with them: shown in order once
    // the same person is confirmed, dropped otherwise.
    const views = window.EWViews;
    if (views && typeof views.holdEffect === 'function' && views.holdEffect(() => toast(message, { low, action }))) return;
    const repeat = !action && current.lastToast === message;
    current.lastToast = message;
    if (current.toastTimer) {
      if (repeat) return;
      if (low) {
        toastQueue.push(message);
        while (toastQueue.length > 5) toastQueue.shift();
        return;
      }
      if (current.toastLow) toastQueue.unshift($('#toast').textContent);
      clearTimeout(current.toastTimer);
      current.toastTimer = null;
    }
    if (action) {
      const data = Object.entries(action.data || {}).map(([k, v]) => ` data-${k.replace(/[A-Z]/g, c => '-' + c.toLowerCase())}="${esc(v)}"`).join('');
      $('#toast').innerHTML = `${esc(message)} <button type="button" class="toast-action"${data}>${esc(action.label)}</button>`;
    } else {
      $('#toast').textContent = message;
    }
    reserveToastSpace();
    current.toastLow = low;
    const expire = () => {
      // CR-R1-02: a toast with an action (Undo) waits while it has focus
      // or the pointer, so it is never taken from under the person.
      const el = $('#toast');
      if (action && el && ((document.activeElement && typeof el.contains === 'function' && el.contains(document.activeElement))
          || (typeof el.matches === 'function' && el.matches(':hover')))) {
        current.toastTimer = setTimeout(expire, 3000);
        return;
      }
      current.toastTimer = null;
      current.toastLow = false;
      $('#toast').textContent = '';
      reserveToastSpace();
      if (toastQueue.length) toast(toastQueue.shift(), { low: true });
    };
    current.toastTimer = setTimeout(expire, toastQueue.length ? 4000 : action ? 10000 : 7000);
  }

  // A message about an action must not outlive the page it was about:
  // navigating away or discarding the draft it mentions clears it.
  function clearToast() {
    if (current.toastTimer) clearTimeout(current.toastTimer);
    current.toastTimer = null;
    current.toastLow = false;
    current.lastToast = null;
    toastQueue.length = 0;
    const t = $('#toast');
    if (t) t.textContent = '';
    reserveToastSpace();
  }

  // While an announcement shows, the page keeps its height plus 16 px free
  // at the bottom, so the fixed toast never covers the last content
  // (Sol ui-acceptance-r1 POLISH-2). Its live-region role is unchanged.
  function reserveToastSpace() {
    const el = $('#toast');
    const body = document.body;
    if (!el || !body || !body.style) return;
    const height = el.textContent && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect().height : 0;
    body.style.paddingBottom = height ? `${Math.ceil(height) + 16}px` : '';
  }

  function saveNotice(message, isError) {
    const n = $('#save-notice');
    if (!n) return;
    if (!message) { n.hidden = true; n.textContent = ''; return; }
    n.hidden = false;
    n.textContent = message;
    n.classList.toggle('notice', !!isError);
  }

  /* -- Navigation state (tab-local; no shared last-search record) -------- */
  function remember() {
    if (!current.route) return;
    current.scroll.set(current.route, window.scrollY);
    const a = document.activeElement;
    if (a) current.focus.set(current.route, { id: a.id, href: a.getAttribute('href'), text: a.textContent });
  }

  /* DEMO2 pass 23 (owner decision): every history entry the app writes
     carries an owner tag. Private search terms and filters live only in
     the entry's state; the URL shows the route without them. A missing
     or mismatched tag restores nothing: the route renders fresh.
     DEMO2 pass 24 (owner decision): the tag is this tab's id, its
     identity epoch, the principal and the workspace. The tab id and the
     epoch (a counter that only grows, on every identity change) are kept
     in sessionStorage, so they survive a reload or a sign-in return in
     this tab: the same person's Back restores again, A -> B -> A does
     not. Nothing else is kept there (but views.js' one-time signed-out
     note flag, G1-16). Without sessionStorage the id and
     the counter live for this page load only. */
  const TAB_EPOCH_KEY = 'earWitnessTabEpoch';
  const newTabId = () => Date.now().toString(36) + '.' + Math.random().toString(36).slice(2);
  const tabEpoch = (() => {
    let store = null;
    try { store = window.sessionStorage || null; if (store) store.getItem(TAB_EPOCH_KEY); } catch { store = null; }
    if (store && (typeof store.getItem !== 'function' || typeof store.setItem !== 'function')) store = null;
    if (store) {
      try {
        const kept = JSON.parse(store.getItem(TAB_EPOCH_KEY) || 'null');
        if (kept && typeof kept.tab === 'string' && kept.tab && Number.isInteger(kept.n) && kept.n >= 0) return { tab: kept.tab, n: kept.n, store };
      } catch { /* unreadable: start over */ }
    }
    const made = { tab: newTabId(), n: 0, store };
    if (store) try { store.setItem(TAB_EPOCH_KEY, JSON.stringify({ tab: made.tab, n: made.n })); } catch { made.store = null; }
    return made;
  })();
  // Accepted gap (owner, pass 24b): with no principal kept here, A ->
  // B -> A where both arrive through a sign-in redirect after an in-tab
  // session end is not seen; it can only return A's own entries to A.
  // Another person (or, at a page load, anyone other than the person on
  // the entry the page loaded on) was adopted: entries tagged before do
  // not restore. A write that fails falls back to this page load alone.
  function identityChanged() {
    tabEpoch.n += 1;
    if (!tabEpoch.store) return;
    try { tabEpoch.store.setItem(TAB_EPOCH_KEY, JSON.stringify({ tab: tabEpoch.tab, n: tabEpoch.n })); }
    catch {
      try { tabEpoch.store.removeItem(TAB_EPOCH_KEY); } catch { /* nothing kept */ }
      tabEpoch.store = null;
      tabEpoch.tab = newTabId();
    }
  }
  // The owner tag of the entry this page loaded on (a reload keeps it).
  const loadTag = (() => {
    try {
      const tag = history.state && typeof history.state === 'object' ? history.state.earWitnessOwner : null;
      return tag && typeof tag === 'object' ? { tab: tag.tab, epoch: tag.epoch, principal: tag.principal } : null;
    } catch { return null; }
  })();
  let loadIdentityNoted = false;
  // The first identity this page load adopts (signed out included) is
  // compared with that entry's tag: another principal there than now
  // (another person signed in elsewhere, or the person signed out, then
  // reloaded) is an identity change.
  function noteLoadIdentity() {
    if (loadIdentityNoted) return;
    loadIdentityNoted = true;
    if (!loadTag || loadTag.tab !== tabEpoch.tab || loadTag.epoch !== tabEpoch.n) return;
    if (typeof loadTag.principal === 'string' && loadTag.principal && loadTag.principal !== (current.principal || '')) identityChanged();
  }
  // History writes fail in embedded previews: the URL is then the route.
  let historyLocked = false;
  // Routes whose query holds a person's search terms or filters, and the
  // keys that do (null: every key).
  const PRIVATE_QUERY = { '#/search': null, '#/compare': null, '#/saved': ['q', 'cursor'] };
  function publicRoute(route) {
    const r = String(route || '');
    const at = r.indexOf('?');
    if (at < 0) return r;
    const path = r.slice(0, at);
    if (!Object.prototype.hasOwnProperty.call(PRIVATE_QUERY, path)) return r;
    const keys = PRIVATE_QUERY[path];
    if (!keys) return path;
    const params = new URLSearchParams(r.slice(at + 1));
    for (const k of keys) params.delete(k);
    const rest = params.toString();
    return path + (rest ? '?' + rest : '');
  }
  function historyOwner() {
    return { tab: tabEpoch.tab, epoch: tabEpoch.n, principal: current.principal || '', workspace: current.activeWorkspace || '' };
  }
  function historyOwnerHolds(tag) {
    const now = historyOwner();
    return !!tag && typeof tag === 'object' && tag.tab === now.tab && tag.epoch === now.epoch
      && tag.principal === now.principal && tag.workspace === now.workspace;
  }
  function historyState() {
    try { return history.state && typeof history.state === 'object' ? history.state : null; } catch { return null; }
  }
  // This entry's state when its owner tag holds, else nothing.
  function ownedHistoryState() {
    const st = historyState();
    return st && historyOwnerHolds(st.earWitnessOwner) ? st : null;
  }
  function writeHistory(mode, entry, route, keepUrl = false) {
    const state = { ...entry, earWitnessOwner: historyOwner(), earWitnessRoute: route };
    try { history[mode](state, '', keepUrl ? undefined : publicRoute(route)); } catch { historyLocked = true; /* embedded previews may prohibit URL changes */ }
  }
  /* The route a history traversal or restore lands on: the entry's own
     route when its tag holds and it matches the URL, else the URL's
     route without private terms. The URL is rewritten without them.
     DEMO2 pass 24 (owner decision): an entry whose tag does not hold
     (missing or mismatched, the same URL as the page on screen
     included) inherits no Back cache: its route's Back origin, scroll
     and focus and the kept Search frames are forgotten. */
  // DEMO2 pass 25: `info.fresh` is set when the entry is unowned (the
  // route renders fresh, the same route included).
  function historyRoute(hash, info = null) {
    const at = hash || '#/overview';
    const st = ownedHistoryState();
    // History writes prohibited or not applied (the URL still carries
    // the private terms the app never writes there): the URL showing
    // exactly the route on screen is that route. Unowned, it still
    // inherits no Back cache, and the URL loses the terms where it can.
    if (at === current.route && (historyLocked || publicRoute(at) !== at)) {
      // Not marked fresh: with writes not applied no entry is ever
      // owned, and the twin event of one move would render twice.
      if (!st) { forgetRouteCaches(at); if (!historyLocked) writeHistory('replaceState', {}, at); }
      return at;
    }
    const own = st && typeof st.earWitnessRoute === 'string' && publicRoute(st.earWitnessRoute) === publicRoute(at) ? st.earWitnessRoute : null;
    // BL-033: an entry a fragment navigation just made (info.explicit: the
    // browser fired a Navigation API push/replace for it) opens with its
    // terms, as a page load does (normalizeRoute); the URL still loses them
    // below. Chromium fires it for a typed, pasted or bookmarked address and
    // for location.hash; WebKit only for location.hash, so there a typed,
    // pasted or bookmarked address in the open tab opens fresh, as before
    // (a new tab is a page load and keeps them; real Safari unverified).
    const route = own || (info && info.explicit ? at : publicRoute(at));
    if (!own) { forgetRouteCaches(route); if (info) info.fresh = true; }
    if (!own || (location.hash || '') !== publicRoute(route)) writeHistory('replaceState', own ? st : {}, route);
    return route;
  }
  function forgetRouteCaches(route) {
    current.back.delete(route);
    current.scroll.delete(route);
    current.focus.delete(route);
    keptPages.delete(route);
    for (const field of RETAINED_VIEWS) current[field] = null;
  }
  // The current entry, retagged for whoever is current now. Anything it
  // held for an earlier owner (a search origin) is dropped.
  function syncHistory() {
    const st = ownedHistoryState();
    const shown = publicRoute(current.route);
    const hash = location.hash || '';
    // A first page with no hash shows Overview: its URL stays as it is.
    const urlShows = hash === shown || (!hash && shown === '#/overview');
    if (st && st.earWitnessRoute === current.route && urlShows) return;
    writeHistory('replaceState', st || {}, current.route, urlShows);
  }
  // Another person was adopted: the tab's route and navigation memory
  // lose the previous person's terms, and so does the current entry.
  function forgetPrivateRoute() {
    current.back.clear();
    current.scroll.clear();
    current.focus.clear();
    keptPages.clear();
    current.route = publicRoute(current.route);
    writeHistory('replaceState', {}, current.route);
  }

  function go(route, replace = false) {
    remember();
    clearToast();
    const dest = route.startsWith('#') ? route : '#/' + route;
    const entry = {};
    // The origin belongs to this evidence history entry, never a shared value.
    if (/^#\/evidence\//.test(dest) && /^#\/search(?:\?|$)/.test(current.route)) {
      entry.earWitnessSearchOrigin = {
        evidence: dest, route: current.route,
        scroll: current.scroll.get(current.route) || 0,
        focus: current.focus.get(current.route) || null,
      };
    }
    if (/#\/(evidence|episode|creative|brand|show|alert|campaign)\//.test(dest) && dest !== current.route) {
      current.back.set(dest, current.route || '#/overview');
    }
    // Brand/Show detail drill-downs into Search remember their origin so
    // the results page can offer a path back; returning reuses the stored
    // route, query, and scroll position through the normal render path.
    if (/^#\/search(?:\?|$)/.test(dest) && /^#\/(brand|show)\//.test(current.route || '') && dest !== current.route) {
      current.back.set(dest, current.route);
    }
    current.route = dest;
    writeHistory(replace ? 'replaceState' : 'pushState', entry, dest);
    // Rendered by the views closure (assigned at views.js load); never a
    // bare global — a bare render() call throws and leaves stale results.
    window.EWViews.render();
  }

  function restoreEvidenceSearchOrigin() {
    if (!/^#\/evidence\//.test(current.route) || current.back.has(current.route)) return;
    try {
      // DEMO2 pass 23: only an entry this owner wrote restores its origin.
      const st = ownedHistoryState();
      const origin = st && st.earWitnessSearchOrigin;
      if (!origin || origin.evidence !== current.route || typeof origin.route !== 'string' || !/^#\/search(?:\?|$)/.test(origin.route)) return;
      current.back.set(current.route, origin.route);
      if (Number.isFinite(origin.scroll)) current.scroll.set(origin.route, Math.max(0, origin.scroll));
      if (origin.focus && typeof origin.focus === 'object') current.focus.set(origin.route, origin.focus);
    } catch { /* direct-entry fallback below */ }
  }

  function back(defaultRoute = '#/search') { return current.back.get(current.route) || defaultRoute; }

  /* Canonical CustomerQuery2 URL wire. Set fields use repeated
     plural keys (brands=a&brands=b); a truly absent key is null (Any)
     while one __none__ value is the explicit exact-empty set. Legacy
     singular keys (brand/show/status/audio/category) parse the same
     way but are never emitted. The counted-cell selector travels in a
     separate percent-encoded JSON `selector` parameter, never as a
     Query2 field. */
  const SET_KEYS = ['brands', 'shows', 'categories', 'statuses', 'audio'];
  // Legacy singular aliases only: audio is already the canonical key
  // and must never pass through alias removal.
  const LEGACY_SET = { brand: 'brands', show: 'shows', category: 'categories', status: 'statuses' };

  function parseSet(params, key) {
    const plural = params.getAll(key);
    const singularName = { brands: 'brand', shows: 'show', categories: 'category', statuses: 'status', audio: 'audio' }[key];
    const singular = singularName ? params.getAll(singularName) : [];
    const raw = plural.length ? plural : singular;
    if (!raw.length) return null;
    if (raw.length === 1 && (raw[0] === '__none__' || raw[0] === '')) return [];
    return [...new Set(raw.filter(v => v !== ''))];
  }

  function searchCriteria() {
    const query = (current.route.split('?')[1] || '');
    const params = new URLSearchParams(query);
    const mode = params.get('mode') || 'ads';
    const c = { version: 2, mode, q: params.get('q') || '' };
    for (const k of SET_KEYS) c[k] = parseSet(params, k);
    // PLAIN-ADS: customers filter no review state; an old link's status
    // filter (statuses=confirmed would show 0) is dropped on parse.
    c.statuses = null;
    c.date_status = params.get('date_status') || 'all';
    c.from = params.get('from') || null;
    c.to = params.get('to') || null;
    if (!c.from) c.from = null;
    if (!c.to) c.to = null;
    c.sort = params.get('sort') || 'newest';
    c.page = Math.max(1, Number(params.get('page')) || 1);
    // REDESIGN S9: only ads with a clip, Ads mode only; no key without it.
    if (mode === 'ads' && params.get('has_clip') === '1') c.has_clip = true;
    const ids = params.getAll('ids');
    if (!ids.length) c.ids = null;
    else if (ids.length === 1 && (ids[0] === '__none__' || ids[0] === '')) c.ids = [];
    else c.ids = [...new Set(ids.filter(v => v !== ''))];
    return c;
  }

  /* The counted-cell selector is route state beside the query, never a
     Query2 field. Malformed selector JSON is a visible error, not a
     silent widening to the unscoped query. */
  function searchSelector() {
    const query = (current.route.split('?')[1] || '');
    const params = new URLSearchParams(query);
    const raw = params.get('selector');
    if (raw === null || raw === '') return null;
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new Error('This chart selection is unreadable. Clear it to return to the full scope.');
    }
    if (!parsed || typeof parsed !== 'object' || parsed.version !== 1 || !parsed.base_query || !parsed.clause) {
      throw new Error('This chart selection is unreadable. Clear it to return to the full scope.');
    }
    return parsed;
  }

  /* Complete research scope for every result/summary/filter/compare/
     drilldown/save/tracker/export consumer: query plus selector. */
  function searchScope() {
    return { query: searchCriteria(), selector: searchSelector() };
  }

  function emitSet(p, key, value) {
    if (value === null || value === undefined) return;
    if (Array.isArray(value)) {
      if (!value.length) p.append(key, '__none__');
      else for (const v of value) p.append(key, String(v));
    } else if (value !== '') {
      p.append(key, String(value));
    }
  }

  function normalizeQueryInput(input = {}) {
    const q = { ...input };
    for (const [singular, plural] of Object.entries(LEGACY_SET)) {
      if (q[plural] === undefined && q[singular] !== undefined) q[plural] = q[singular];
      delete q[singular];
    }
    if (q.status !== undefined && q.statuses === undefined) { q.statuses = q.status; }
    delete q.status;
    delete q.selector;
    delete q.view;
    return q;
  }

  /* SAVED-FILTER (G3-01): the brands a saved search put on Search ride
     in the route as saved_brands, beside the query and never a Query2
     field or a server parameter. A new typed query drops them (views.js);
     every other change keeps the ones still applied. */
  function savedBrands(route = current.route) {
    if (!/^#\/search(?:\?|$)/.test(route || '')) return [];
    return [...new Set(new URLSearchParams(String(route).split('?')[1] || '').getAll('saved_brands').filter(Boolean))];
  }
  // The saved search's own words ride beside its marked brands (SF-R1-01);
  // null when the route carries none.
  function savedWords(route = current.route) {
    if (!/^#\/search(?:\?|$)/.test(route || '')) return null;
    return new URLSearchParams(String(route).split('?')[1] || '').get('saved_q');
  }

  function searchURL(input = {}, { selector = null, savedBrands: marked, savedQ } = {}) {
    const q = normalizeQueryInput(input);
    const p = new URLSearchParams();
    if (q.mode) p.set('mode', String(q.mode));
    if (q.q) p.set('q', String(q.q));
    for (const k of SET_KEYS) emitSet(p, k, q[k] === undefined ? null : q[k]);
    if (q.date_status && q.date_status !== 'all') p.set('date_status', String(q.date_status));
    if (q.from) p.set('from', String(q.from));
    if (q.to) p.set('to', String(q.to));
    if (q.sort) p.set('sort', String(q.sort));
    if (q.page && Number(q.page) !== 1) p.set('page', String(Number(q.page)));
    if (q.has_clip === true && (q.mode || 'ads') === 'ads') p.set('has_clip', '1');
    if (Array.isArray(q.ids)) {
      if (!q.ids.length) p.append('ids', '__none__');
      else for (const v of q.ids) p.append('ids', String(v));
    } else if (typeof q.ids === 'string' && q.ids) {
      p.append('ids', q.ids);
    }
    /* dv3-shared: G1 — the Ads results view (Cards/List) rides in the
       URL beside the query, never as a Query2 field. An explicit view
       wins; otherwise a search route keeps the view it already shows. */
    const view = input.view !== undefined ? input.view
      : (/^#\/search(?:\?|$)/.test(current.route || '') ? new URLSearchParams(String(current.route).split('?')[1] || '').get('view') : null);
    if ((q.mode || 'ads') === 'ads' && (view === 'list' || view === 'cards')) p.set('view', view);
    const applied = (Array.isArray(q.brands) ? q.brands : typeof q.brands === 'string' ? [q.brands] : []).map(String);
    for (const b of (marked === undefined ? savedBrands() : marked || [])) {
      if (applied.includes(String(b)) && !p.getAll('saved_brands').includes(String(b))) p.append('saved_brands', String(b));
    }
    const words = savedQ === undefined ? savedWords() : savedQ;
    if (p.has('saved_brands') && words != null) p.set('saved_q', String(words));
    if (selector) p.set('selector', JSON.stringify(selector));
    const qs = p.toString();
    return '#/search' + (qs ? '?' + qs : '');
  }

  /* Backend GET wire for the retained V1-compatible reads: one Query2
     JSON `query` parameter. POST research-query carries the same object
     as a body. */
  function backendQueryParams(query, extra = {}) {
    const p = new URLSearchParams();
    p.set('query', JSON.stringify(query));
    for (const [k, v] of Object.entries(extra)) {
      if (v !== '' && v !== undefined && v !== null) p.set(k, String(v));
    }
    return p.toString();
  }

  /* -- Shared presentation helpers --------------------------------------- */
  /* Design v3 shell: serif wordmark, seven primary items, Settings and
     Connections in their own group; a Menu button below 1024 px. */
  const WORDMARK = 'EarWitness';
  // The Menu button says what a press will do: open, or "Close menu" while open.
  const MENU_OPEN_LABEL = '<span class="ew-menu-icon" aria-hidden="true"></span>Menu';
  const MENU_CLOSE_LABEL = '<span class="ew-menu-close-icon" aria-hidden="true">\u00d7</span>Close menu';
  function wordmark() { return link('#/overview', WORDMARK, 'wordmark'); }
  // A hosted pilot whose session says agent access is off has no
  // Connections item; its route shows the off state. Local mode and a
  // session that names no features keep it.
  function connectionsOff() {
    const f = current.hosted && current.pilotFeatures;
    return !!f && f.agent_access === false;
  }
  function header(section) {
    // REDESIGN (b2): a redesigned page gets the s-nav header (app/b2.js),
    // with today's items, order and rules; other pages keep this one.
    if (window.EWB2 && window.EWB2.paintHeader(section, $('#header'))) { renderDatasetStrip(); return; }
    const primary = ['Overview', 'Search', ...(current.askAvailable === true || current.askPaused === true ? ['Ask'] : []), 'Alerts', 'Brands', 'Shows', 'Campaigns', 'Saved'];
    const secondary = connectionsOff() ? ['Settings'] : ['Settings', 'Connections'];
    const anchor = n => `<a href="#/${n.toLowerCase()}" ${section === n ? 'aria-current="page"' : ''}>${n}</a>`;
    // JUDGE-SMALL G1-03: a signed-in person's Sign out sits with Settings
    // (the phone menu shows this nav too); it runs the existing sign-out.
    const signout = current.hosted && current.principal ? '<button type="button" class="nav-signout" data-action="signout">Sign out</button>' : '';
    // JUDGE-SMALL-FIX1 (JS-R1-07): the group's label names what is in it.
    const parts = [...secondary, ...(signout ? ['Sign out'] : [])].map((n, i) => (i ? n.toLowerCase() : n));
    const groupLabel = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
    // The header rebuilds on every route render, so the mobile menu
    // always closes on route change with expanded=false restored.
    $('#header').innerHTML = `${wordmark()}`
      + `<span class="mobile-section">${esc(section)}</span>`
      + `<nav class="primary-nav" id="primary-nav" aria-label="Primary">${primary.map(anchor).join('')}`
      + `<span class="nav-secondary" role="group" aria-label="${esc(groupLabel)}">${secondary.map(anchor).join('')}${signout}</span></nav>`
      // D10: a phone Search link beside Menu, except on Search itself.
      + (section === 'Search' ? '' : '<a class="button quiet mobile-search" href="#/search">Search</a>')
      + `${button('menu', MENU_OPEN_LABEL, 'mobile-menu', 'aria-expanded="false" aria-controls="primary-nav"')}`
      + '<div class="ew-menu-scrim" data-action="menu-close" aria-hidden="true" hidden></div>';
    renderDatasetStrip();
  }

  /* Phone menu state: nav, button label, aria-expanded and the dimmed scrim
     move together. Escape, a scrim tap and the Close menu button all go
     through closeMenu(true) and return focus to the button. Route choice
     rebuilds the header (menu starts closed); modified or middle clicks on
     real anchors keep native behavior. */
  function setMenu(open, focusButton) {
    const nav = document.querySelector('.primary-nav');
    const btn = document.querySelector('.mobile-menu');
    const scrim = document.querySelector('.ew-menu-scrim');
    if (nav) nav.classList.toggle('menu-open', open);
    if (scrim) scrim.hidden = !open;
    if (btn) {
      btn.setAttribute('aria-expanded', String(open));
      btn.innerHTML = open ? MENU_CLOSE_LABEL : MENU_OPEN_LABEL;
      if (focusButton && btn.focus) { try { btn.focus(); } catch { /* keep focus */ } }
    }
  }
  function closeMenu(focusButton) { setMenu(false, focusButton); }

  async function renderDatasetStrip() {
    const el = $('#dataset');
    if (!el) return;
    if (current.hosted) {
      // Hosted: no protected customer read and no local-workspace
      // banner, before or after a workspace is chosen (P3-4).
      el.innerHTML = '';
      return;
    }
    // Route/context ownership: a warm navigation or workspace switch
    // supersedes this read, so a late completion must not replay old
    // counts into the new chrome.
    const owner = current.fetchToken, route = current.route;
    if (!current.dataset) {
      try {
        const out = await api('/customer/overview');
        if (current.fetchToken !== owner || current.route !== route) return;
        const cov = out.coverage || {};
        current.dataset = `${plural(cov.episodes || 0, 'episode')} · ${plural(cov.shows || 0, 'show')} · stored records only`;
      } catch (err) {
        // A dropped late response applies nothing: no fallback text
        // and no DOM write — the owning render already moved on. A
        // refused-admission failure applies nothing either: the chrome
        // must not present fallback counts for an unadmitted session.
        // Genuine errors keep the named unavailable feedback.
        if (err && (err.droppedContext || err.refusedAdmission)) return;
        if (current.fetchToken !== owner || current.route !== route) return;
        current.dataset = 'Stored records only';
      }
    }
    if (current.fetchToken !== owner || current.route !== route) return;
    el.innerHTML = `<span>Local workspace</span><span>·</span><span>${esc(current.dataset)} · no live services</span>`;
  }

  function pageHead(title, description = '', actions = '') {
    return `<div class="page-head"><div><h1>${esc(title)}</h1>${description ? `<p>${esc(description)}</p>` : ''}</div>${actions ? `<div class="actions">${actions}</div>` : ''}</div>`;
  }

  function empty(title, desc, action = '', heading = 'h2') {
    return `<div class="empty"><${heading}>${esc(title)}</${heading}><p>${esc(desc)}</p>${action ? `<div class="actions">${action}</div>` : ''}</div>`;
  }

  // The record type a not-found page names, and the list it points back to.
  const NOT_FOUND_KINDS = {
    'ad record': ['#/search', 'Search'], alert: ['#/alerts', 'Alerts'], campaign: ['#/campaigns', 'Campaigns'],
    brand: ['#/brands', 'Brands'], show: ['#/shows', 'Shows'], episode: ['#/shows', 'Shows'],
  };

  // A not-found page is the whole page, so its heading is the page's h1
  // (R1-15); .empty h1 keeps the empty-state size.
  const notFoundPage = (title, desc, action) => empty(title, desc, action, 'h1');

  function notFound(opts = {}) {
    // An address that matches no route at all: a page, not a record.
    if (opts.route) return notFoundPage('Page not found', 'There is no page at this address.', link('#/overview', 'Go to Overview', 'button primary'));
    const kind = NOT_FOUND_KINDS[opts.kind];
    // The route was last shown in a workspace this tab has since left:
    // the record belongs there, not to a different dataset revision.
    if (current.hosted && current.recordLeftWs) {
      return notFoundPage('This record is not available', `This record belongs to another workspace. This tab now shows ${workspaceName(current.activeWorkspace) || 'another workspace'}.`, link('#/overview', 'Go to Overview', 'button primary'));
    }
    // A record page that knows its type says so and points back to its list.
    if (kind) {
      return notFoundPage(`We can't find this ${opts.kind}`, 'It may have been removed, or the link may be wrong.',
        link(kind[0], `Back to ${kind[1]}`, 'button primary') + link('#/overview', 'Go to Overview', 'button'));
    }
    // A hosted workspace has no local dataset revisions to blame.
    if (current.hosted) {
      return notFoundPage('This record is not available', "This record isn't available in this workspace. It may have been removed or changed.", link('#/overview', 'Go to Overview', 'button primary'));
    }
    // Local single-dataset mode: a link from another dataset revision.
    return notFoundPage('This record is not available', 'The link may refer to a different local dataset revision. Your saved searches and campaigns remain unchanged.', link('#/overview', 'Go to Overview', 'button primary'));
  }

  function loadingBlock(label) {
    return `<div class="loading" role="status" aria-label="${esc(label)}"><p>${esc(label)}</p><div class="skeleton"></div><div class="skeleton"></div></div>`;
  }

  function errorBlock(message, retryAction) {
    return `<div class="notice error" role="alert"><p>${esc(message)}</p>${retryAction ? `<div class="actions">${retryAction}</div>` : ''}</div>`;
  }

  function audioBox(state) {
    if (state === 'available') return '';
    return `<div class="audio-box"><strong>${state === 'mismatch' ? 'Audio changed since transcription' : 'No clip (transcript only)'}</strong>`
      + `<p class="small muted">${state === 'mismatch' ? 'Playback is withheld because the recording version cannot support this passage.' : 'The transcript remains readable. No recording or playback is implied.'}</p></div>`;
  }

  // Local-only audio element. The src is always a local /media/ URL
  // chosen by the caller from the record's trusted player descriptor;
  // remote streams are never passed here. start_s seeks into the local
  // recording via a media fragment instead of defaulting to zero.
  function audioPlayer(audioUrl, start_s = null) {
    if (!audioUrl) return '';
    const start = Number(start_s);
    const frag = Number.isFinite(start) && start > 0 ? `#t=${start.toFixed(1)}` : '';
    return `<audio controls preload="none" src="${esc(audioUrl)}${frag}"></audio>`;
  }

  /* Whole-word matching, as the server does (customer.word_in): a word
     edge that is a letter, digit, mark or underscore may not run on into
     one, so "meta" never marks "metal" or "ameta". No regex lookbehind
     (older Safari): the trailing edge is a lookahead, the leading edge is
     checked against the character before each match. Text is NFC first,
     so a decomposed accent never ends a word. */
  const WORDCH = '[\\p{L}\\p{N}\\p{M}_]';
  // One whole character (code point): a class before '$' misses a
  // trailing astral letter on V8, and text[0] is half of one.
  const isWordCh = c => !!c && new RegExp('^' + WORDCH, 'u').test(c);
  const firstCh = s => [...s][0];
  const lastCh = s => [...s].pop();
  const escRe = v => String(v).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function wordSource(w) {
    const s = String(w).normalize('NFC');
    return escRe(s) + (isWordCh(lastCh(s)) ? `(?!${WORDCH})` : '');
  }
  /* Non-overlapping matches of needles ({ text, whole, cls }) in text,
     longest needle first at a position: [{ start, end, needle }] over
     the NFC text (returned as .text). */
  function matchRanges(text, needles) {
    const t = String(text ?? '').normalize('NFC');
    const list = needles.filter(n => String(n.text).trim())
      .map(n => ({ ...n, text: String(n.text).normalize('NFC') }))
      .sort((a, b) => b.text.length - a.text.length);
    const out = [];
    if (!list.length) return { text: t, ranges: out };
    const source = n => (n.whole ? wordSource(n.text) : escRe(n.text));
    const re = new RegExp(list.map(n => '(' + source(n) + ')').join('|'), 'giu');
    // A whole word may not start right after a word character.
    const edgeOk = (n, at) => !(n.whole && isWordCh(firstCh(n.text)) && isWordCh(lastCh(t.slice(Math.max(0, at - 2), at))));
    let m;
    while ((m = re.exec(t))) {
      if (!m[0]) { re.lastIndex += 1; continue; }
      let needle = list[m.slice(1).findIndex(g => g !== undefined)], end = m.index + m[0].length;
      if (!edgeOk(needle, m.index)) {
        // Try the other needles here, longest first (a code inside a rejected word).
        needle = null;
        for (const n of list) {
          if (!edgeOk(n, m.index)) continue;
          const one = new RegExp(source(n), 'iuy');
          one.lastIndex = m.index;
          const hit = one.exec(t);
          if (hit && hit[0]) { needle = n; end = m.index + hit[0].length; break; }
        }
        if (!needle) { re.lastIndex = m.index + (t.codePointAt(m.index) > 0xffff ? 2 : 1); continue; }
        re.lastIndex = end;
      }
      out.push({ start: m.index, end, needle });
    }
    return { text: t, ranges: out };
  }
  // Escaped text with each range wrapped by wrap(part, needle).
  function markRanges({ text, ranges }, wrap) {
    let html = '', at = 0;
    for (const r of ranges) { html += esc(text.slice(at, r.start)) + wrap(text.slice(r.start, r.end), r.needle); at = r.end; }
    return html + esc(text.slice(at));
  }
  // Does text say w as a whole word (case-insensitive)?
  const saysWord = (text, w) => matchRanges(text, [{ text: w, whole: true }]).ranges.length > 0;

  function highlight(text, q) {
    const words = unique(tokensOf(q)).filter(Boolean);
    if (!words.length) return esc(text);
    return markRanges(matchRanges(text, words.map(w => ({ text: w, whole: true }))), part => `<mark>${esc(part)}</mark>`);
  }

  /* A short plain-text excerpt around the earliest matched term (about
     80 characters of lead-in, cut at spaces, "…" where text was cut).
     No match or no terms: the start of the text. The caller escapes it
     (through highlight). */
  function snippet(text, terms, max = 240) {
    const t = String(text ?? '').normalize('NFC').replace(/\s+/g, ' ').trim();
    if (t.length <= max) return t;
    const first = matchRanges(t, unique(tokensOf(terms)).filter(Boolean).map(w => ({ text: w, whole: true }))).ranges[0];
    const at = first ? first.start : -1;
    let start = at < 0 ? 0 : Math.max(0, at - 80);
    if (start > 0) {
      const sp = t.lastIndexOf(' ', start);
      start = sp > 0 ? sp + 1 : 0;
    }
    let end = Math.min(t.length, start + max);
    if (end < t.length) {
      const sp = t.lastIndexOf(' ', end);
      if (sp > start) end = sp;
    }
    return (start > 0 ? '…' : '') + t.slice(start, end).trim() + (end < t.length ? '…' : '');
  }

  function basisLine(item) {
    const basis = item.match_basis;
    if (!basis) return '';
    const label = basis === 'title' ? 'Matched title' : basis === 'mixed' ? 'Matched title and passage text' : 'Matched passage text';
    return `<span class="match-reason">${esc(label)}</span>`;
  }

  function outcomeLabel(s) {
    return { supported: 'Supported', pending: 'Pending', incomplete_coverage: 'Incomplete coverage', no_observation_in_covered_sources: 'No qualifying evidence', unreviewed_detections_in_scope: 'Waiting for review', ambiguous: 'Ambiguous', contradicted: 'Contradicted' }[s] || s || 'Unknown';
  }
  function outcomeClass(s) { return s === 'supported' ? 'green' : s === 'contradicted' ? 'red' : s === 'pending' ? '' : 'amber'; }

  function eventTitle(kind) {
    return { offer_difference: 'Different offer or code observed', new_finding: 'New advertising passage', new_episode: 'New matching episode', new_text: 'New matching words' }[kind] || 'New matching words';
  }

  /* -- Dialogs (native, focus-restoring, unshadowable dispatch) ----------- */
  function openDialog(title, bodyHtml, footerHtml = '', formId = '', owner = null) {
    // DEMO2 pass 13: an async opener's owner carries the authority epoch
    // it started in; after any boundary it never opens (returns false).
    if (owner && owner.epoch !== undefined && !authorityHolds(owner.epoch)) return false;
    // DEMO2 pass 14 (R4-01): a synchronous opener carries the authority
    // of the page it came from: only a page published in this epoch, with
    // no refusal in its render. Fixed-copy prompts carry no server data.
    if (!(owner && (owner.epoch !== undefined || owner.fixedCopy))
        && (current.pageEpoch === null || !authorityHolds(current.pageEpoch))) return false;
    remember();
    // Origin owner for every dialog-owned form: the captured initiating
    // workspace/route when an async editor supplies one, else current.
    // Submissions refuse when the workspace moved underneath the dialog,
    // and route transitions close a dialog opened for another route.
    current.dialogWorkspace = (owner && typeof owner.ctx === 'string') ? owner.ctx : (current.workspaceContext || '');
    // The displayed epoch this dialog's captured values belong to: a
    // submit under any other displayed epoch refuses instead of sending
    // old captures with a newer header.
    current.dialogEpoch = { rev: current.displayedWorkspaceSwitchRevision, authority: current.authorityEpoch };
    // Every open counts: a delayed opener captured before it is stale.
    current.dialogOpenSeq = (current.dialogOpenSeq || 0) + 1;
    // A dialog replacing a "Leave this page?" prompt voids its destination.
    if (current.leaveTarget) { current.leaveTarget = null; current.leaveDrafts = null; current.focusChooser = false; }
    // The workspace entries in this dialog are typed in (for accurate
    // discard copy after a later change).
    current.dialogWorkspaceName = current.activeWorkspace || '';
    const dlg = $('#modal');
    current.dialogRoute = (owner && typeof owner.route === 'string') ? owner.route : current.route;
    current.dialogOpener = document.activeElement;
    dlg.innerHTML = `${formId ? `<form id="${formId}" novalidate>` : ''}`
      + `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2>${button('close-dialog', 'Close', 'quiet')}</div>`
      + `<div class="dialog-body">${bodyHtml}</div>`
      + `${footerHtml ? `<div class="dialog-footer">${footerHtml}</div>` : ''}`
      + `${formId ? '</form>' : ''}`;
    dlg.setAttribute('aria-labelledby', 'dialog-title');
    // A bottom sheet is set by its caller after this; a dialog opened
    // from inside a sheet is an ordinary dialog (F-21).
    if (dlg.classList) dlg.classList.remove('ew-sheet');
    const focusFirst = () => {
      const input = dlg.querySelector('[autofocus],input:not([type=hidden]),select,textarea');
      (input || dlg.querySelector('button')).focus();
    };
    // DEMO2 pass 26 (owner decision 2): while a tab return holds answers,
    // the dialog is built but not shown. The same person confirmed: it
    // opens then, unless a newer dialog replaced it. Otherwise it goes.
    const views = window.EWViews;
    const seq = current.dialogOpenSeq;
    const opened = () => {
      if (current.dialogOpenSeq !== seq || dlg.open) return;
      dlg.showModal();
      setTimeout(focusFirst, 0);
    };
    const dropped = () => { if (current.dialogOpenSeq === seq && !dlg.open) { dlg.innerHTML = ''; current.dialogRoute = null; } };
    if (views && typeof views.holdEffect === 'function' && views.holdEffect(opened, dropped)) {
      if (dlg.open) dlg.close();
      return true;
    }
    dlg.showModal();
    setTimeout(focusFirst, 0);
    return true;
  }

  // SPEED-FIX2 (SPC-C-05): a closed dialog's "Preview matches" query is
  // dropped, unsent if it still waits in the research lane, so the page's
  // next query does not wait behind it.
  function dropDialogReads() {
    const dlg = $('#modal');
    const box = dlg && dlg.querySelector ? dlg.querySelector('#alert-preview') : null;
    if (box && box.previewAbort) box.previewAbort.abort();
  }

  function closeDialog() {
    const dlg = $('#modal');
    dropDialogReads();
    if (dlg && dlg.open) dlg.close();
    // Any close ends a "Leave this page?" prompt: its destination is void.
    if (current.leaveTarget) { current.leaveTarget = null; current.leaveDrafts = null; current.focusChooser = false; }
    // A closed dialog's forms are no longer drafts anywhere.
    if (dlg && dlg.querySelectorAll) {
      for (const f of dlg.querySelectorAll('form[data-dirty], form[data-held]')) { delete f.dataset.dirty; delete f.dataset.held; }
    }
  }

  // A form's message box: id="form-errors" in dialogs; pages with
  // several forms (Settings) give each box its own id and mark it
  // data-form-errors, so ids stay unique on the page.
  function errorBox(form) {
    return (form && form.querySelector && (form.querySelector('#form-errors') || form.querySelector('[data-form-errors]'))) || null;
  }

  function formError(message, field, form) {
    const el = errorBox(form) || $('#modal[open] #form-errors') || $('#form-errors');
    if (el) {
      el.innerHTML = `<p class="field-error">${esc(message)}</p>`;
      el.setAttribute('role', 'alert');
      el.tabIndex = -1;
      if (el.dataset) delete el.dataset.errorKey;
    }
    // Forms validate in order and show one message, so a field marked by
    // an earlier message is valid by now: clear its mark (UX-07).
    const scope = form && form.querySelectorAll ? form : null;
    if (scope) for (const f of scope.querySelectorAll('[aria-invalid="true"]')) f.removeAttribute('aria-invalid');
    if (scope) clearFieldErrors(scope);
    if (field) {
      for (let p = field.parentElement; p; p = p.parentElement) if (p.tagName === 'DETAILS') p.open = true;
      field.setAttribute('aria-invalid', 'true');
      const inline = fieldErrorBeside(field, message);
      field.focus();
      // In a long dialog the box at the top is out of view by now (UAT-091).
      if (inline && inline.scrollIntoView) inline.scrollIntoView({ block: 'nearest' });
    } else if (el) el.focus();
    return false;
  }

  // The message again beside its field: the top box stays the one
  // announcement; the field is described by this copy (UAT-091).
  let fieldErrorSeq = 0;
  function fieldErrorBeside(field, message) {
    const block = typeof field.closest === 'function' ? field.closest('.od-field, fieldset') : null;
    const anchor = block || field;
    if (typeof anchor.insertAdjacentElement !== 'function') return null;
    const p = document.createElement('p');
    p.className = 'field-error';
    p.dataset.inlineError = '';
    p.id = 'field-error-' + (++fieldErrorSeq);
    p.textContent = message;
    anchor.insertAdjacentElement(block ? 'beforeend' : 'afterend', p);
    const described = field.getAttribute('aria-describedby');
    field.setAttribute('aria-describedby', described ? `${described} ${p.id}` : p.id);
    return p;
  }
  function clearFieldErrors(scope) {
    for (const p of scope.querySelectorAll('[data-inline-error]')) {
      for (const f of scope.querySelectorAll(`[aria-describedby~="${p.id}"]`)) {
        const rest = f.getAttribute('aria-describedby').split(/\s+/).filter(x => x && x !== p.id).join(' ');
        if (rest) f.setAttribute('aria-describedby', rest); else f.removeAttribute('aria-describedby');
      }
      p.remove();
    }
  }

  function conflictBlock(currentRevision) {
    return `<div class="conflict-box" role="alert"><p><strong>This record changed while you were editing.</strong> `
      + `Your entries are preserved below; nothing was saved. `
      + `Review the current values, then save again to apply your edit.</p>`
      + `<details class="tech-details"><summary>Technical details</summary><p class="small muted">Current server revision is ${esc(String(currentRevision))}.</p></details></div>`;
  }

  window.EW = {
    $, esc, clone, unique, date, stamp, stampText, time, link, downloadLink, button, tag, normalize, tokensOf, plural, num,
    FRONTEND_CONTRACT, requireContract, notices, LEGACY_CAMPAIGN, plainServerText, fenceNotice, neutralCopy,
    current, api, requestQueue, takePlace, holdPlace, sessionSeq: () => current.sessionSeq || 0, sessionChangeSeq: () => current.sessionChangeSeq || 0, sharedRead, invalidateSharedReads, forgetRetainedViews, noteViewRevision,
    authorityEpoch, authorityHolds, authorityBoundary, retain, retained, dialogHolds,
    registerRetained, clearRetainedStores, clearPersonState, retainedStoreList, keptPages, requestTimeout, boot, signedInName, personName, plainWords, requirementOutcomeText, staleText, staleSummary, reportWithheldLine, adoptWorkspaces, workspaceName, toast, clearToast, whenWritesSettled, saveNotice, remember, go, back, restoreEvidenceSearchOrigin,
    searchCriteria, searchSelector, searchScope, searchURL, savedBrands, savedWords, backendQueryParams,
    clearWorkspaceState, stopAudio, whenSessionReadsSettled, publicRoute, historyRoute, syncHistory, forgetPrivateRoute, identityChanged, header, connectionsOff, wordmark, setMenu, closeMenu, pageHead, empty, notFound,
    loadingBlock, errorBlock, audioBox, audioPlayer, highlight, snippet, wordSource, matchRanges, markRanges, saysWord,
    basisLine, outcomeLabel, outcomeClass, eventTitle, openDialog, closeDialog, dropDialogReads,
    formError, clearFieldErrors, errorBox, statusCopy, conflictBlock,
  };

  /* -- Settings preferences (design v3) ----------------------------------
     The caller's own preferences (PREFS-SHARED: the server answers each
     member with theirs), read once per workspace context, never at boot.
     A failed read resolves {} so no render waits on it; the sync helpers
     read the last answer for the displayed workspace (or the prefs the
     caller passes) and fall back to the plain defaults. */
  const PREF_RANGE_DAYS = { '7d': 7, '30d': 30, '90d': 90 };
  const prefCache = { key: null, value: null, pending: null };
  const prefKey = () => `${current.workspaceContext || ''}|${current.activeWorkspace || ''}`;
  function invalidatePreferences() { prefCache.key = null; prefCache.value = null; prefCache.pending = null; }
  registerRetained('preferences', { clear: invalidatePreferences });
  function preferences() {
    const key = prefKey();
    if (prefCache.key !== key) invalidatePreferences();
    if (prefCache.value) return Promise.resolve(prefCache.value);
    if (prefCache.pending) return prefCache.pending;
    prefCache.key = key;
    const pending = api('/customer/preferences')
      .then(out => {
        const prefs = (out && out.preferences && typeof out.preferences === 'object') ? out.preferences : {};
        // The response carries the plan signal; a cold Search open needs it.
        if (out && typeof out.excerpt_only === 'boolean' && typeof window.EW.notePlan === 'function') window.EW.notePlan(out, { result: true });
        if (prefCache.pending === pending && prefKey() === key) { prefCache.value = prefs; prefCache.pending = null; }
        return prefs;
      })
      .catch(() => { if (prefCache.pending === pending) prefCache.pending = null; return {}; });
    prefCache.pending = pending;
    return pending;
  }
  // PUT the changed fields with the displayed revision; on success the
  // cache holds the server's answer. Errors (conflict, refusal) throw.
  async function savePreferences(patch, expectedRevision) {
    const key = prefKey();
    const out = await api('/customer/preferences', { method: 'PUT', body: { ...patch, expected_revision: expectedRevision } });
    invalidatePreferences();
    if (out && out.preferences && prefKey() === key) {
      prefCache.key = key;
      prefCache.value = { ...out.preferences, revision: out.revision ?? out.preferences.revision };
    }
    return out;
  }
  const shownPrefs = () => (prefCache.key === prefKey() && prefCache.value) || {};
  function prefSearchTab(prefs = shownPrefs()) {
    return ['ads', 'mentions', 'episodes'].includes(prefs.search_tab) ? prefs.search_tab : 'ads';
  }
  function prefResultsView(prefs = shownPrefs()) { return prefs.results_view === 'list' ? 'list' : 'cards'; }
  function zoneOr(prefs) { return typeof prefs.time_zone === 'string' && prefs.time_zone ? prefs.time_zone : undefined; }
  // Search criteria patch for the default date range: the last N days up
  // to today in the chosen zone (or the browser's), as Query2 from/to.
  function prefDateRange(prefs = shownPrefs(), now = new Date()) {
    if (prefs.default_range === 'all') return { from: null, to: null };
    const days = PREF_RANGE_DAYS[prefs.default_range];
    if (!days) return {};
    let today;
    try { today = new Intl.DateTimeFormat('en-CA', { timeZone: zoneOr(prefs), year: 'numeric', month: '2-digit', day: '2-digit' }).format(now); }
    catch { today = now.toISOString().slice(0, 10); }
    const start = new Date(today + 'T12:00:00Z');
    start.setUTCDate(start.getUTCDate() - (days - 1));
    return { from: start.toISOString().slice(0, 10), to: null };
  }
  // Calendar dates stay calendar dates (date()); timestamps show in the
  // chosen zone, a zoneless server time is UTC, an unknown zone falls
  // back to UTC. Unknown stays "Date unknown".
  function formatDate(value, prefs = shownPrefs()) {
    const s = String(value ?? '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return date(s);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return 'Date unknown';
    const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : s + 'Z');
    if (!Number.isFinite(d.getTime())) return 'Date unknown';
    const opts = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' };
    try { return new Intl.DateTimeFormat('en', { ...opts, timeZone: zoneOr(prefs) }).format(d); }
    catch { return new Intl.DateTimeFormat('en', { ...opts, timeZone: 'UTC' }).format(d); }
  }
  // Day-only form of formatDate (dv3-shared: G5): "Oct 14" for timestamps in
  // the chosen zone (no clock time or zone name); calendar dates and
  // unknown values behave exactly as formatDate.
  function formatDay(value, prefs = shownPrefs()) {
    const s = String(value ?? '');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return formatDate(value, prefs);
    const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : s + 'Z');
    if (!Number.isFinite(d.getTime())) return 'Date unknown';
    const opts = { month: 'short', day: 'numeric' };
    try { return new Intl.DateTimeFormat('en', { ...opts, timeZone: zoneOr(prefs) }).format(d); }
    catch { return new Intl.DateTimeFormat('en', { ...opts, timeZone: 'UTC' }).format(d); }
  }
  // Year-bearing form of formatDate ("Oct 21, 2026 at 7:29 PM PDT") for
  // freshness evidence that can outlive a year end (FRESHUI-SOL-R1-01);
  // calendar dates and unknown values behave exactly as formatDate.
  function formatYearStamp(value, prefs = shownPrefs()) {
    const s = String(value ?? '');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return formatDate(value, prefs);
    const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : s + 'Z');
    if (!Number.isFinite(d.getTime())) return 'Date unknown';
    const words = timeZone => new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone }).format(d)
      + ' at ' + new Intl.DateTimeFormat('en', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone }).format(d);
    try { return words(zoneOr(prefs)); } catch { return words('UTC'); }
  }
  // Clock time ("9:05 AM") in the chosen zone, for "Saved hh:mm".
  function formatClock(value, prefs = shownPrefs()) {
    const opts = { hour: 'numeric', minute: '2-digit' };
    try { return new Intl.DateTimeFormat('en', { ...opts, timeZone: zoneOr(prefs) }).format(value); }
    catch { return new Intl.DateTimeFormat('en', { ...opts, timeZone: 'UTC' }).format(value); }
  }
  /* A timestamp as a <time> element (decision 14): the chosen zone's
     local words, the exact instant in datetime, and the UTC time on hover.
     A value that is not a timestamp (a calendar date, unknown) stays plain
     escaped text. day: the "Oct 14" form; year: the formatYearStamp form. Returns HTML: never pass it to
     esc() or put it inside an attribute. */
  const UTC_HOVER = { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short' };
  function timeElement(d, words) {
    const iso = d.toISOString().replace(/\.000Z$/, 'Z');
    return `<time datetime="${esc(iso)}" title="${esc(new Intl.DateTimeFormat('en', UTC_HOVER).format(d))}">${esc(words)}</time>`;
  }
  function timeHtml(value, { day = false, year = false, prefs = shownPrefs() } = {}) {
    const s = String(value ?? '');
    const words = day ? formatDay(value, prefs) : year ? formatYearStamp(value, prefs) : formatDate(value, prefs);
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return esc(words);
    const d = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : s + 'Z');
    return Number.isFinite(d.getTime()) ? timeElement(d, words) : esc(words);
  }
  // "9:05 AM" for a moment (a Date), with the same hover.
  function clockHtml(value, prefs = shownPrefs()) {
    const d = new Date(value);
    return Number.isFinite(d.getTime()) ? timeElement(d, formatClock(d, prefs)) : '';
  }
  // Clip start: always 3 s before the read, never before the file start.
  // The stored clip_lead_in preference is ignored (design 6.1).
  function clipStart(start_s) {
    if (start_s === null || start_s === undefined || start_s === '') return null;
    const n = Number(start_s);
    if (!Number.isFinite(n)) return null;
    return Math.max(0, n - 3);
  }
  Object.assign(window.EW, {
    preferences, invalidatePreferences, savePreferences,
    prefSearchTab, prefResultsView, prefDateRange, formatDate, formatDay, formatYearStamp, formatClock, timeHtml, clockHtml, clipStart,
  });
})();
