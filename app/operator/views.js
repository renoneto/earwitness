"use strict";
const Views = {};
const Lib = { filter: null, items: [], total: 0, selected: new Set(), timer: null, request: 0 };
const badge = (value) => `<span class="badge ${esc(value || "unreviewed")}">${esc((value || "unreviewed").replaceAll("_", " "))}</span>`;
const evLink = (id) => `<a class="code" title="${esc(id)}" href="#/review/${encodeURIComponent(id)}">${esc(id.length > 30 ? id.slice(0, 20) + "…" : id)}</a>`;
const field = (id, label, value = "", type = "text") => `<label class="field" for="${id}"><span>${label}</span><input id="${id}" type="${type}" value="${esc(value)}" ${type === "number" ? 'step="any" min="0"' : ""}></label>`;
const area = (id, label, value = "") => `<label class="field" for="${id}"><span>${label}</span><textarea id="${id}" rows="3">${esc(value)}</textarea></label>`;
const select = (id, label, values, current = "") => `<label class="field" for="${id}"><span>${label}</span><select id="${id}">${values.map((v) => { const pair = Array.isArray(v) ? v : [v, v.replaceAll("_", " ")]; return `<option value="${esc(pair[0])}" ${String(pair[0]) === String(current) ? "selected" : ""}>${esc(pair[1])}</option>`; }).join("")}</select></label>`;
const submit = (label = "Save") => `<button type="submit" class="primary">${label}</button> <span role="status" class="meta"></span>`;
const panel = (title, body, id = "") => `<section class="panel"${id ? ` id="${id}"` : ""}><h2>${title}</h2>${body}</section>`;
const form = (id, revision, body, draft = true) => `<form id="${id}" data-revision="${revision ?? 0}" ${draft ? "" : 'data-draft="off"'}>${body}</form>`;
const queryString = (filter) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(filter)) { if (v === "" || v === null || v === undefined) continue; if (Array.isArray(v)) v.forEach((x) => p.append(k, x)); else p.set(k, String(v)); } return p.toString(); };
const post = (path, body, revision, method = "POST") => api(path, { method, headers: revision === undefined ? {} : { "If-Match": String(revision) }, body: JSON.stringify(body) });
function downloadPayload(payload, format, id) {
  const url = URL.createObjectURL(new Blob([payload], { type: { csv: "text/csv", json: "application/json", html: "text/html" }[format] + ";charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = `earwitness-${id}.${format}`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function exportRecords(body) {
  try { const out = await post("/api/v1/exports", body); downloadPayload(out.payload, body.format, out.export_id); say(`Exported ${out.item_count} evidence records.`); }
  catch (error) { modal("Export failed", `<p>${esc(error.message)}</p>`); }
}
function evidenceTable(items) {
  return `<div class="table-wrap"><table class="grid"><thead><tr><th>Evidence</th><th>Episode</th><th>Source interval</th><th>Review</th></tr></thead><tbody>${items.map((e) => `<tr><td>${evLink(e.id)}<br>${badge(e.source)} ${badge(e.record_kind || "candidate")}</td><td>${esc(e.show_title || e.episode_id)}<br><a href="#/episode/${encodeURIComponent(e.episode_id)}">${esc(e.ep_title || e.episode_id)}</a></td><td class="num">${fmtT(e.orig_start_s)}–${fmtT(e.orig_end_s)}</td><td>${badge(e.judgment)}</td></tr>`).join("") || '<tr><td colspan="4">No evidence in this selection. This does not establish the absence of advertising.</td></tr>'}</tbody></table></div>`;
}
const recordKindBadge = (recordKind) => `<span class="badge ${esc(recordKind || "candidate")}" title="Record kind: candidate (raw window) or observation (versioned proposal)">${esc((recordKind || "candidate").replaceAll("_", " "))}</span>`;
function wireHideShow(root, refresh) {
  if (!root) return;
  root.querySelectorAll("[data-hide-show]").forEach((el) => {
    el.onclick = async () => {
      const showTitle = el.dataset.hideShow, hide = !el.dataset.showHidden;
      el.disabled = true;
      try {
        await post("/api/v1/shows/hide", { show_title: showTitle, hidden: hide }, Number(el.dataset.showRevision || 0), "PUT");
        say(hide ? `Show hidden: ${showTitle}. Its evidence left the Library queue; raw rows are kept.` : `Show visible again: ${showTitle}.`);
        await refresh();
      } catch (error) {
        el.disabled = false;
        if (error.status === 412) modal("Stale show revision", `<p>Someone changed this show's visibility. Reload the page and try again. ${esc(error.message)}</p>`);
        else modal("Show visibility failed", `<p>${esc(error.message)}</p>`);
      }
    };
  });
}
const sourceLegend = () => `<details class="legend"><summary>Source and record legend</summary><ul><li><strong>detector</strong> — built-in pattern scan</li><li><strong>jev</strong> — model screen</li><li><strong>manual</strong> — human-created</li><li><strong>pipeline</strong> — processing runs, incl. <span class="code">pipe:obs:*</span> machine proposals awaiting judgment</li><li><strong>candidate</strong> — unreviewed proposal</li><li><strong>observation</strong> — reviewed outcome</li></ul></details>`;

Views.overview = async () => {
  const data = await api("/api/v1/overview");
  const rows = [["Supplied metadata records", data.metadata_records, "#/explore?tab=shows"], ["Supplied transcript files", data.transcript_files, "#/explore?tab=shows"], ["Canonical transcript episodes", data.canonical_transcript_episodes, "#/explore?tab=shows"], ["Transcript hours", data.transcript_hours, "#/explore?tab=shows"], ["Jev listening windows", data.evidence_by_source.jev || 0, "#/library?source=jev"], ["Experimental detector candidates", data.evidence_by_source.detector || 0, "#/library?source=detector"], ["Manual evidence", data.evidence_by_source.manual || 0, "#/library?source=manual"]];
  $("view").innerHTML = `<h1>Research workspace</h1><p class="meta">Find evidence, inspect the transcript, listen, and record what you can support. Local corpus only.</p>${data.hidden_excluded > 0 ? `<p class="notice" role="note">${data.hidden_excluded} evidence from ${data.hidden_shows.length} hidden show(s) excluded from every count below — <a href="#/explore?tab=shows">unhide in Explore</a> to restore.</p>` : ""}${panel("Imported coverage", `<div class="table-wrap"><table class="grid"><thead><tr><th>Coverage</th><th>Count</th><th></th></tr></thead><tbody>${rows.map(([label, count, link]) => `<tr><td>${label}</td><td class="num">${count}</td><td><a href="${link}">Explore</a></td></tr>`).join("")}</tbody></table></div><p class="meta">Windows and candidates are not unique ads. Spend, reach, market coverage, and current accuracy are not established.</p>`)}${panel("Assisted review", Object.entries(data.review_states).map(([state, count]) => `<p><a href="#/library?review=${state}">${badge(state)} ${count} records</a></p>`).join("") || '<p>No imported evidence yet. <a href="#/data">Import the supplied corpus</a>.</p>')}`;
};

function libraryValues() {
  const values = formValues($("filters"));
  return { q: values["f-q"], source: values["f-source"] ? [values["f-source"]] : [], review: values["f-review"] ? [values["f-review"]] : [], brand: values["f-brand"], brand_basis: values["f-basis"] ? [values["f-basis"]] : [], show: values["f-show"], category: values["f-category"], episode: values["f-episode"], pub_from: values["f-from"], pub_to: values["f-to"], include_unknown_dates: values["f-unknown"], history: values["f-history"], source_version: values["f-version"], sort: values["f-sort"], per_page: Number(values["f-per"]), page: 1 };
}
Views.library = async () => {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  let initial = Lib.filter || { source: ["jev"], review: [], brand_basis: [], per_page: 50, include_unknown_dates: true };
  if (p.size) { initial = Object.fromEntries(p); for (const k of ["source", "review", "brand_basis"]) initial[k] = p.getAll(k); initial.include_unknown_dates = !["0", "false"].includes(p.get("include_unknown_dates")); }
  const data = await api("/api/v1/overview");
  const versions = (data.import_state || []).filter((v) => ["jev-run", "detector-output"].includes(v.source_key));
  setToolbar(`<label>Export scope <select id="export-scope"><option value="all">All matching evidence</option><option value="selected">Selected evidence</option></select></label><button id="export-csv" data-export disabled>CSV</button><button id="export-json" data-export disabled>JSON</button><button id="export-html" data-export disabled>Printable HTML</button><button id="save-search">Save search</button>`);
  $("view").innerHTML = `<h1>Library</h1>${sourceLegend()}${form("filters", 0, field("f-q", "Literal text", initial.q) + select("f-source", "Source", [["", "All sources"], ["jev", "Jev leads"], ["detector", "Experimental detector"], ["pipeline", "Machine proposals"], ["manual", "Manual evidence"]], initial.source?.[0]) + select("f-review", "Review", [["", "Any review"], "unreviewed", "ad", "not_ad", "unsure"], initial.review?.[0]) + field("f-brand", "Brand", initial.brand) + select("f-basis", "Attribution basis", [["", "Any basis"], "confirmed", "incidental", "unknown", "suggested"], initial.brand_basis?.[0]) + field("f-show", "Show contains", initial.show) + field("f-category", "Category", initial.category) + field("f-episode", "Episode ID", initial.episode) + field("f-from", "Published from", initial.pub_from, "date") + field("f-to", "Published to", initial.pub_to, "date") + `<label><input id="f-unknown" type="checkbox" ${initial.include_unknown_dates !== false ? "checked" : ""}> Include unknown dates</label>` + select("f-history", "Source history", [["active", "Active versions"], ["all", "All versions"]], initial.history) + select("f-version", "Specific source version", [["", "Any version"], ...versions.map((v) => [v.id, `${v.source_key} v${v.version_no}`])], initial.source_version) + select("f-sort", "Sort", ["episode", "start", "brand"], initial.sort) + select("f-per", "Per page", [[25, "25"], [50, "50"], [100, "100"], [200, "200"]], initial.per_page || 50) + submit("Search"), false)}<p id="lib-status" role="status"></p><div id="lib-results"></div><div class="pager"><button id="previous">Previous</button><span id="page-info"></span><button id="next">Next</button></div>`;
  $("filters").classList.add("panel", "filters");
  $("filters").oninput = () => { Lib.ready = false; Lib.request++; document.querySelectorAll("[data-export]").forEach((b) => b.disabled = true); };
  $("filters").onsubmit = (event) => { event.preventDefault(); searchLibrary(1); };
  $("filters").onchange = () => searchLibrary(1);
  $("f-q").oninput = () => { clearTimeout(Lib.timer); Lib.timer = setTimeout(() => searchLibrary(1), 250); };
  $("previous").onclick = () => searchLibrary(Lib.filter.page - 1);
  $("next").onclick = () => searchLibrary(Lib.filter.page + 1);
  for (const format of ["csv", "json", "html"]) $("export-" + format).onclick = () => {
    if (!Lib.ready) return say("Wait for the current search.");
    if ($("export-scope").value === "selected" && !Lib.selected.size) return say("Select evidence first.");
    exportRecords({ format, ...($("export-scope").value === "selected" ? { evidence_ids: [...Lib.selected] } : { filter: Lib.filter }) });
  };
  $("save-search").onclick = () => { App.watchEdit = { ...(App.editWatchFromLibrary || { name: "" }), query: libraryValues() }; App.editWatchFromLibrary = null; navigate("#/watchlists/" + (App.watchEdit.id || "new")); };
  await searchLibrary(Number(initial.page) || 1);
};
async function searchLibrary(page) {
  if (!$("filters")) return;
  const request = ++Lib.request, generation = App.generation;
  const filter = { ...libraryValues(), page: Math.max(1, page) };
  Lib.ready = false; document.querySelectorAll("[data-export]").forEach((b) => b.disabled = true);
  $("lib-status").textContent = "Searching…";
  try {
    const result = await api("/api/v1/evidence?" + queryString(filter));
    if (generation !== App.generation || request !== Lib.request) return;
    Lib.filter = result.filter; Lib.items = result.items; Lib.total = result.total;
    Lib.ready = true; document.querySelectorAll("[data-export]").forEach((b) => b.disabled = false);
    App.routeHash = "#/library?" + queryString(result.filter); history.replaceState(null, "", App.routeHash);
    const start = result.total ? (result.page - 1) * result.per_page + 1 : 0;
    $("lib-status").textContent = `Showing ${start}–${result.total ? start + result.items.length - 1 : 0} of ${result.total}.`
      + (result.hidden_excluded > 0 ? ` ${result.hidden_excluded} evidence from ${result.hidden_shows.length} hidden show(s) excluded — unhide in Explore to restore.` : "");
    $("page-info").textContent = `Page ${result.page}`; $("previous").disabled = result.page <= 1; $("next").disabled = result.page * result.per_page >= result.total;
    $("lib-results").innerHTML = `<div class="table-wrap"><table class="grid"><thead><tr><th>Select</th><th>Evidence</th><th>Episode</th><th>Interval</th><th>Transcript excerpt</th><th>Review</th></tr></thead><tbody>${result.items.map((e) => `<tr><td><input type="checkbox" data-select="${esc(e.id)}" aria-label="Select ${esc(e.id)}" ${Lib.selected.has(e.id) ? "checked" : ""}></td><td>${evLink(e.id)}<br>${badge(e.source)} ${recordKindBadge(e.record_kind)}</td><td>${esc(e.show_title || e.episode_id)}<br><a href="#/episode/${encodeURIComponent(e.episode_id)}">${esc(e.ep_title || e.episode_id)}</a></td><td>${fmtT(e.orig_start_s)}–${fmtT(e.orig_end_s)}</td><td>${esc(e.excerpt.slice(0, 240))}</td><td>${badge(e.judgment)}</td></tr>`).join("") || '<tr><td colspan="6">No matches. Try a wider filter. Evidence-free transcripts are searchable in Explore.</td></tr>'}</tbody></table></div>`;
    document.querySelectorAll("[data-select]").forEach((el) => el.onchange = () => { if (el.checked) Lib.selected.add(el.dataset.select); else Lib.selected.delete(el.dataset.select); say(`${Lib.selected.size} evidence selected.`); });
  } catch (error) { if (generation === App.generation && request === Lib.request && error.name !== "AbortError") $("lib-status").textContent = `Search failed: ${error.message}. Change the search or retry.`; }
}

function transcriptMarkup() { return `<p class="meta">Select a segment, then Shift-select to extend. Enter and Space work on each segment.</p><p id="selection-status" role="status">No selection.</p><ol id="segments" class="segment-list" aria-label="Transcript segments"></ol>`; }
const REVIEW_SHORTCUTS = [["Space", "Play or pause the interval"], ["Left / Right", "Seek 5 seconds"], ["A", "Draft judgment: ad"], ["N", "Draft judgment: not an ad"], ["U", "Draft judgment: unsure"], ["Ctrl/⌘+Enter", "Save review"]];
function reviewKeysMarkup() {
  return `<details><summary>Keyboard shortcuts (review workspace only)</summary><ul>`
    + REVIEW_SHORTCUTS.map(([key, what]) => `<li><strong>${key}</strong> — ${what}</li>`).join("")
    + `</ul><p class="meta">Shortcuts run only with focus in the review/transcript workspace, never inside fields, buttons, links, dialogs, or while composing text. Judgment keys edit a local draft; nothing is saved until Save. <label><input id="review-keys-off" type="checkbox"> Disable shortcuts on this browser</label></p></details>`;
}
function reviewKeysOff() { try { return localStorage.getItem("earwitness-keys-off") === "1"; } catch (_) { return false; } }
function wireReviewKeys(generation, saveNext) {
  if (App._reviewKeys) document.removeEventListener("keydown", App._reviewKeys);
  const handler = (event) => {
    if (generation !== App.generation || App.route !== "review" || reviewKeysOff()) return;
    if (event.isComposing || event.key === "Unidentified") return;
    const target = event.target;
    if (target?.closest?.("input, textarea, select, [contenteditable='true'], dialog")) return;
    if (target?.closest?.("button, a, summary, [role='button'], [role='link'], [role='dialog'], [role='option']")) return;
    if (!$("review-form")?.isConnected) return;
    const player = $("audio");
    const inAudio = target === player || target?.id === "audio";
    const modified = event.ctrlKey || event.metaKey || event.altKey;
    if ((event.ctrlKey || event.metaKey) && !event.altKey && (event.key === "Enter" || event.key === "Return")) {
      event.preventDefault(); saveNext(false); return;
    }
    if (modified) return;
    if (event.key === " " && !inAudio) {
      event.preventDefault();
      if ($("play-evidence") && !$("play-evidence").disabled) $("play-evidence").click();
    } else if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && !inAudio) {
      event.preventDefault();
      const button = $(event.key === "ArrowLeft" ? "seek-back" : "seek-fwd");
      if (button && !button.disabled) button.click();
    } else if (event.key === "a" || event.key === "A") {
      setJudgmentDraft("ad");
    } else if (event.key === "n" || event.key === "N") {
      setJudgmentDraft("not_ad");
    } else if (event.key === "u" || event.key === "U") {
      setJudgmentDraft("unsure");
    }
  };
  App._reviewKeys = handler;
  document.addEventListener("keydown", handler);
}
function setJudgmentDraft(value) {
  const selectEl = $("review-judgment");
  if (!selectEl || selectEl.disabled) return;
  selectEl.value = value;
  rememberForm($("review-form"));
  say(`Draft judgment: ${value.replace("_", " ")}. Not saved yet.`);
}
function manualMarkup(episode, version) { return form("manual-form", 0, field("manual-start", "Manual start (seconds)", "", "number") + field("manual-end", "Manual end (seconds)", "", "number") + field("manual-origin", "Origin note (required)") + submit("Create manual evidence")); }
// span: the passage under review [lo, hi]; rows that overlap it are marked
// (a left bar plus words for screen readers). An unknown bound marks none.
function wireTranscript(segments, episode, version, span = []) {
  let anchor = null, selected = [], firstInSpan = null;
  const list = $("segments");
  const [spanLo, spanHi] = span || [];
  const marked = Number.isFinite(spanLo) && Number.isFinite(spanHi);
  segments.forEach((segment, index) => {
    const li = document.createElement("li"), button = document.createElement("button");
    button.type = "button"; button.className = "segment-button"; button.setAttribute("aria-pressed", "false");
    const inSpan = marked && segment.end > spanLo && segment.start < spanHi;
    button.innerHTML = `<time>${fmtT(segment.start)}–${fmtT(segment.end)}</time><span>${esc(segment.text)}</span>${inSpan ? '<span class="visually-hidden"> (in the passage under review)</span>' : ""}`;
    if (inSpan) { li.classList.add("in-span"); firstInSpan = firstInSpan || li; }
    button.onclick = (event) => {
      const first = event.shiftKey && anchor !== null ? Math.min(anchor, index) : index;
      const last = event.shiftKey && anchor !== null ? Math.max(anchor, index) : index;
      if (!event.shiftKey || anchor === null) anchor = index;
      selected = [first, last];
      [...list.children].forEach((row, i) => { const on = i >= first && i <= last; row.classList.toggle("sel", on); row.firstElementChild.setAttribute("aria-pressed", String(on)); });
      $("manual-start").value = segments[first].start; $("manual-end").value = segments[last].end;
      rememberForm($("manual-form"));
      $("selection-status").textContent = `Selected ${fmtT(segments[first].start)}–${fmtT(segments[last].end)}.`;
    };
    li.append(button); list.append(li);
  });
  if (firstInSpan && Number.isFinite(firstInSpan.offsetTop)) list.scrollTop = firstInSpan.offsetTop - list.offsetTop;
  if (!segments.length) list.innerHTML = '<li>No transcript is available for this episode.</li>';
  wireForm("manual-form", async (values) => {
    if (!values["manual-start"] || !values["manual-end"]) { if (values["manual-start"] !== "0") throw new Error("Select transcript bounds first."); }
    const out = await post("/api/v1/evidence/manual", { episode_id: episode, transcript_version_id: version, start_s: Number(values["manual-start"]), end_s: Number(values["manual-end"]), origin_note: values["manual-origin"] });
    const link = document.createElement("p"); link.innerHTML = `Created ${evLink(out.evidence_id)}.`; $("manual-form").append(link); return out;
  });
  return () => selected.length ? segments[selected[0]].start : null;
}
function audioMarkup() {
  return `<div class="audio-row" role="group" aria-label="Listening transport"><button id="play-evidence">Play interval</button>`
    + `<button id="seek-back" type="button" aria-label="Back 5 seconds">−5s</button><button id="seek-fwd" type="button" aria-label="Forward 5 seconds">+5s</button>`
    + `<label for="audio-rate">Speed</label><select id="audio-rate">${[0.5, 0.75, 1, 1.25, 1.5, 2].map((n) => `<option value="${n}">${n}×</option>`).join("")}</select>`
    + `<label><input id="loop-interval" type="checkbox"> Loop selection</label>`
    + `<button id="expand-context" type="button">Expand context +5s</button></div>`
    + `<audio id="audio" controls preload="none" hidden></audio><p id="interval-status" class="meta" role="status"></p><p id="audio-status" class="meta" role="status"></p>`
    + `<p class="meta">Playback stops at the interval end (browser timing tolerance, not sample accuracy). Context widening is listening-only and never alters reviewed bounds.</p>`;
}
function wireAudio(media, start, end, selection, alignment = "unknown") {
  const player = $("audio"), status = $("audio-status"), interval = $("interval-status");
  const play = $("play-evidence"), back = $("seek-back"), fwd = $("seek-fwd"), loop = $("loop-interval"), expand = $("expand-context");
  const binding = String(Number(player.dataset.binding || 0) + 1); player.dataset.binding = binding;
  const state = { request: 0, contextS: 0, pending: null };
  player.onloadedmetadata = null; player.onerror = null; player.ontimeupdate = null;
  player.pause(); player.removeAttribute("src"); player.load();
  player.hidden = true; // no source yet: no dead 0:00 control (the status says why)
  for (const el of [play, back, fwd, expand]) { el.onclick = null; el.disabled = false; }
  loop.onchange = null; loop.checked = false;
  const offset = media?.source_offset_s || 0;
  const trust = media?.trust || "unknown";
  const preroll = trust === "rotated" && Number.isFinite(start) && start < 120;
  const liveNote = trust === "exact" ? ""
    : preroll ? " This ad window's audio has rotated away — judge this ad on the text; episode body audio still aligns approximately."
    : media?.kind === "remote_full" ? " Live stream: ads may have rotated since transcription, so a heard ad can differ from the excerpt."
    : "";
  const clipEnd = media?.kind === "local_clip" ? offset + 90 : null;
  const explicitEnd = Number.isFinite(end) ? end : null;
  const extentHi = () => {
    if (clipEnd !== null) return clipEnd;
    const duration = player.duration;
    return Number.isFinite(duration) ? offset + duration : null;
  };
  const bounds = () => {
    const base = selection?.() ?? start ?? 0;
    const lo = Math.max(offset, base - state.contextS);
    const rawHi = explicitEnd !== null ? explicitEnd + state.contextS : extentHi();
    const cap = extentHi();
    return { lo, hi: rawHi === null ? null : (cap === null ? rawHi : Math.min(rawHi, cap)) };
  };
  const refreshInterval = () => {
    const { lo, hi } = bounds();
    if (hi === null || !(hi > lo)) {
      interval.textContent = explicitEnd === null
        ? "Interval end unknown: stop and loop are unavailable until bounds are known."
        : "Interval unavailable: end is outside the known media extent.";
      loop.disabled = true;
    } else {
      interval.textContent = `Listening interval ${fmtT(lo)}–${fmtT(hi)}${state.contextS ? ` (includes ${state.contextS}s listening context; reviewed bounds unchanged)` : ""}. Stops at the end; loop is opt-in.`;
      loop.disabled = explicitEnd === null;
      if (explicitEnd === null && interval.textContent) loop.title = "Loop needs a reviewed interval end.";
    }
  };
  if (!media) {
    for (const el of [play, back, fwd, expand]) el.disabled = true;
    loop.disabled = true;
    status.textContent = "Audio unavailable. Transcript work remains available.";
    interval.textContent = "No media: interval playback is unavailable with this episode.";
    return { refreshInterval, bounds: () => ({ lo: 0, hi: null }) };
  }
  status.textContent = `${media.label}. Alignment: ${alignment}.${liveNote} Nothing is fetched until Play interval.`;
  const speed = Number(App.settings.playback_speed || 1);
  if (![...$("audio-rate").options].some((o) => Number(o.value) === speed)) $("audio-rate").add(new Option(`${speed}×`, speed));
  $("audio-rate").value = String(speed); player.playbackRate = speed;
  $("audio-rate").onchange = () => { player.playbackRate = Number($("audio-rate").value); };
  player.onloadedmetadata = () => {
    if (!player.isConnected || player.dataset.binding !== binding) return;
    if (state.pending !== null) { try { player.currentTime = state.pending; } catch (_) {} state.pending = null; }
    refreshInterval();
  };
  player.onerror = () => { player.hidden = true; status.textContent = "Audio failed to load. The transcript remains available."; };
  player.ontimeupdate = () => {
    if (!player.isConnected || player.dataset.binding !== binding) return;
    const { lo, hi } = bounds();
    if (hi === null || player.paused) return;
    if (player.currentTime + offset >= hi) {
      const token = state.request;
      if (loop.checked && !loop.disabled && explicitEnd !== null) {
        try { player.currentTime = Math.max(0, lo - offset); } catch (_) {}
        player.play().catch(() => {});
        if (token === state.request && player.isConnected) status.textContent = `Looping ${fmtT(lo)}–${fmtT(hi)}.`;
      } else {
        player.pause();
        if (token === state.request && player.isConnected) status.textContent = `Stopped at the interval end ${fmtT(hi)} (browser timing tolerance applies).`;
      }
    }
  };
  const seekBy = (delta) => {
    if (!player.getAttribute("src")) return;
    const { lo, hi } = bounds();
    const loM = Math.max(0, lo - offset);
    const topM = hi !== null ? Math.max(loM, hi - offset) : Number.POSITIVE_INFINITY;
    try { player.currentTime = Math.min(Math.max(player.currentTime + delta, Math.max(0, loM - 60)), topM); }
    catch (_) {}
  };
  back.onclick = () => seekBy(-5); fwd.onclick = () => seekBy(5);
  expand.onclick = () => {
    state.contextS += 5; state.request++;
    refreshInterval();
    say(`Listening context widened by 5 seconds each side. Reviewed bounds unchanged.`);
  };
  play.onclick = async () => {
    const { lo, hi } = bounds();
    const top = hi !== null ? hi : extentHi();
    if (top !== null && (lo < offset - 0.001 || (clipEnd !== null && lo >= clipEnd))) {
      status.textContent = "This interval is outside the bundled clip. Associate matching full-episode audio to listen here.";
      return;
    }
    const current = ++state.request;
    const target = Math.max(0, lo - offset);
    state.pending = target;
    if (!player.getAttribute("src")) player.src = media.src;
    player.hidden = false;
    if (player.readyState >= 1) { try { player.currentTime = target; } catch (_) {} state.pending = null; }
    status.textContent = `Loading ${fmtT(lo)}…`;
    try {
      await player.play();
      if (current === state.request && player.isConnected && player.dataset.binding === binding)
        status.textContent = hi !== null ? `Playing ${fmtT(lo)}–${fmtT(hi)}. Alignment: ${alignment}.${liveNote}` : `Playing from ${fmtT(lo)}; stop-at-end unavailable (bounds unknown). Alignment: ${alignment}.${liveNote}`;
    } catch (error) {
      if (current === state.request && player.isConnected && player.dataset.binding === binding)
        status.textContent = player.error ? "Audio failed to load." : `Playback could not start: ${error.message}`;
    }
  };
  refreshInterval();
  return { refreshInterval, bounds };
}
// Brand panel of the review page (validation plan A6): the read's brand,
// needs-review and confidence, and the namer's brand candidates, each one
// click from becoming the confirmed brand (an operator brand choice, kept
// in the read's association history).
function brandChoiceMarkup(read, revision) {
  if (!read || read.historical) return "";
  const brand = read.brand || {};
  const pick = String(brand.suggested || "").toLowerCase();
  const candidates = (read.candidates || []).filter((c) => c && typeof c.brand === "string" && c.brand.trim());
  const needs = read.needs_review && !brand.confirmed;
  const current = String(brand.key || "").toLowerCase();
  const rows = candidates.map((c) => {
    const isCurrent = c.brand.toLowerCase() === current;
    return `<label class="brand-cand"><input type="radio" name="brand-candidate" value="${esc(c.brand)}" ${isCurrent ? "checked" : ""}> <span>${esc(c.brand)}</span>${c.score != null ? ` <span class="meta">score ${esc(c.score)}</span>` : ""}${c.brand.toLowerCase() === pick ? ' <span class="meta">(namer\'s pick)</span>' : ""}${isCurrent ? ' <span class="meta">(on this read now)</span>' : ""}</label>`;
  }).join("");
  return `<section class="panel" id="brand-choice" data-revision="${esc(revision ?? 0)}" aria-label="Brand"><h2>Brand</h2>`
    + `<p>${esc(brand.display ? brandLabel(brand) : "Not named")} ${badge(brand.confirmed ? "confirmed" : "suggested")}${needs ? ' <span class="badge unsure">needs review</span>' : ""}${read.confidence ? ` <span class="meta">confidence ${esc(read.confidence)}${read.confidence_score != null ? ` (${esc(read.confidence_score)})` : ""}</span>` : ""} <span class="meta">· ${esc(read.source_label || "")}</span></p>`
    + (rows ? `<fieldset class="brand-candidates"><legend class="meta">The namer's brand candidates for this read. Using one makes it the confirmed brand; the change is kept in this read's association history.</legend>${rows}</fieldset><button type="button" id="use-brand" disabled>Use selected brand</button>`
      : '<p class="meta">The namer listed no brand candidates for this read.</p>')
    + '<p id="brand-choice-status" class="meta" role="status"></p></section>';
}
const chooseBrand = (encodedId, brand, revision) => post(`/api/v1/evidence/${encodedId}/brand-choice`, { brand }, revision);
// One brand choice: "Use selected brand" turns on only when the picked
// candidate is not the brand already on the read.
function wireBrandChoice(encoded, currentKey) {
  const use = $("use-brand");
  if (!use) return;
  const radios = [...document.querySelectorAll('input[name="brand-candidate"]')];
  const picked = () => radios.find((r) => r.checked)?.value || null;
  const sync = () => { const value = picked(); use.disabled = !value || value.toLowerCase() === String(currentKey || "").toLowerCase(); };
  radios.forEach((r) => { r.onchange = sync; });
  use.onclick = async () => {
    const value = picked();
    if (!value) return;
    const status = $("brand-choice-status");
    use.disabled = true; status.textContent = "Saving…";
    try {
      await chooseBrand(encoded, value, $("brand-choice").dataset.revision);
      say(`Brand set to ${value}. The change is kept in the association history.`);
      await render();
    } catch (error) {
      use.disabled = false;
      status.textContent = error.status === 412
        ? "This read's brands changed meanwhile, so nothing was saved. Reload the page to see the current brands."
        : `Could not use this brand: ${error.message}`;
    }
  };
}
// The label over the excerpt: the interval the stored text was cut at (the
// original one), the reviewed correction when it differs, and the spoken
// brand when the record has one. Corrected times never head the original text.
function reviewExcerptHead(evidence, lo, hi) {
  const spoken = (evidence.attrs || {}).brand_spoken;
  const from = evidence.orig_start_s, to = evidence.orig_end_s;
  const corrected = (lo ?? null) !== (from ?? null) || (hi ?? null) !== (to ?? null);
  const interval = corrected
    ? `original interval ${fmtT(from)}–${fmtT(to)} (corrected bounds ${fmtT(lo)}–${fmtT(hi)}; the text is from the original interval)`
    : `${fmtT(from)}–${fmtT(to)}`;
  return `<p class="meta review-excerpt-head">Passage under review · ${interval}${spoken ? ` · spoken brand “${esc(spoken)}”` : ""}</p>`;
}
Views.review = async ([rawId]) => {
  const id = decodeURIComponent(rawId || ""), encoded = encodeURIComponent(id);
  const [response, transcript] = await Promise.all([api(`/api/v1/evidence/${encoded}`), api(`/api/v1/evidence/${encoded}/transcript`)]);
  const evidence = response.evidence, review = evidence.review || {}, episode = (await api(`/api/v1/episodes/${encodeURIComponent(evidence.episode_id)}`)).episode;
  setToolbar('<button id="review-prev">Previous</button><button id="review-next">Next</button><a class="btn" href="#/library">Back to Library</a>');
  const effLo = review.eff_start_s ?? evidence.orig_start_s, effHi = review.eff_end_s ?? evidence.orig_end_s;
  const txLabel = evidence.transcript_provenance ? `v${evidence.transcript_provenance.version_no} · id ${evidence.transcript_provenance.id}` : `id ${evidence.transcript_version_id ?? "unknown"}`;
  const refinedNotice = (episode.transcript_version_id && evidence.transcript_version_id && episode.transcript_version_id !== evidence.transcript_version_id)
    ? `<p class="notice" role="note">Refined transcript alternative pinned: evidence uses transcript version ${esc(evidence.transcript_version_id)}; episode current is ${esc(episode.transcript_version_id)}. Original text is preserved; the effective version is shown.</p>`
    : "";
  $("view").innerHTML = `<h1>Review evidence</h1><p>${esc(evidence.show_title || evidence.episode_id)} · ${esc(evidence.ep_title || "")}</p><p class="meta code">${esc(id)} · ${recordKindBadge(evidence.record_kind)} ${badge(evidence.source)}</p>`
    + `<section class="panel review-primary" aria-label="Judgment and save">${form("review-form", review.revision || 0, `<p id="queue-position" class="meta" role="status"></p>${reviewExcerptHead(evidence, effLo, effHi)}<blockquote class="excerpt" id="review-excerpt">${esc(evidence.excerpt || "No excerpt stored for this evidence.")}</blockquote>` + select("review-judgment", "Judgment", [["", "No judgment"], ["ad", "Ad"], ["not_ad", "Not an ad"], ["unsure", "Unsure"]], review.judgment) + `<div class="primary-actions">` + submit("Save review") + `<button type="button" id="review-save-next">Save and next</button></div><div class="primary-actions"><label><input id="save-next" type="checkbox"> Advance to next after a successful save (opt-in; failed saves never advance)</label><button type="button" id="clear-judgment">Clear judgment only</button></div><p class="meta" id="review-keys-hint">Draft with A (ad), N (not an ad), or U (unsure); save with Ctrl/⌘+Enter. Judgment keys edit a local draft; nothing is saved until Save.</p>` + reviewKeysMarkup())}</section>`
    + `<div class="workbench-grid"><div class="transcript-col">`
    + `<details class="panel" open><summary>Listen &amp; audio</summary>${audioMarkup()}</details>`
    + `<details class="panel" open><summary>Transcript</summary>${transcriptMarkup()}</details>`
    + `<details class="panel"><summary>Bounds correction</summary>` + field("review-start", "Corrected start (seconds)", review.eff_start_s ?? "", "number") + field("review-end", "Corrected end (seconds)", review.eff_end_s ?? "", "number") + `<label><input id="bounds-verified" type="checkbox" ${review.bounds_verified ? "checked" : ""}> I explicitly verified these boundaries</label><p class="meta">Judgment, attribution, boundaries, and audio alignment are separate. These are assisted reviews, not independent labels.</p></details>`
    + `<details class="panel"><summary>Attribution channels</summary><p class="meta">Edit or remove each brand and attribution channel. Blank brand rows are omitted.</p>` + form("assoc-form", evidence.assoc_revision, '<div id="association-rows"></div><input id="association-count" type="hidden"><button id="add-association" type="button">Add brand</button>' + submit("Save associations")) + `</details>`
    + `<details class="panel"><summary>Confirm copy fields</summary><p class="meta">Human verification of read type, message, offer, and code on the linked observation. Machine proposals never count: each confirmation binds the exact value, observation revision, source pins, and segment/character or audio evidence to you. Customer comparisons read only these records; unverified copy stays unknown.</p><div class="od-field"><label for="confirm-obs">Observation ID</label><input id="confirm-obs" value="${esc((evidence.attrs || {}).observation_id || "")}" placeholder="obs:…"></div><button type="button" id="confirm-load">Load fields</button><div id="confirm-rows"></div><p role="status" class="meta" id="confirm-status"></p></details>`
    + `<details class="panel"><summary>Notes</summary>` + area("review-notes", "Notes", review.notes) + `</details>`
    + `<details class="panel"><summary>Provenance &amp; history</summary><dl class="defs"><dt>Source</dt><dd>${badge(evidence.source)}</dd><dt>Record kind</dt><dd>${recordKindBadge(evidence.record_kind)}</dd><dt>Original interval</dt><dd>${fmtT(evidence.orig_start_s)}–${fmtT(evidence.orig_end_s)}</dd><dt>Effective bounds</dt><dd>${fmtT(effLo)}–${fmtT(effHi)}${(review.eff_start_s ?? review.eff_end_s ?? null) !== null && (review.eff_start_s !== evidence.orig_start_s || review.eff_end_s !== evidence.orig_end_s) ? " (reviewed correction; original preserved above)" : " (matches original)"}</dd><dt>Effective transcript version</dt><dd class="code">${esc(txLabel)}</dd><dt>Original attribution</dt><dd>Spoken: ${esc(evidence.attrs.brand_spoken || "unknown")}<br>URL: ${esc(evidence.attrs.brand_url || "unknown")}<br>Code: ${esc(evidence.attrs.brand_code || "unknown")}</dd><dt>Model signal</dt><dd>${esc(evidence.attrs.noul ?? evidence.attrs.confidence ?? "none")}</dd></dl>${refinedNotice}<details><summary>Source provenance</summary><pre>${esc(JSON.stringify({ source: evidence.provenance, transcript: evidence.transcript_provenance, attrs: evidence.attrs }, null, 2))}</pre></details><h3>Related intervals</h3>${evidence.overlaps.map((e) => `<p>${evLink(e.id)} · ${esc(e.source)} ${fmtT(e.orig_start_s)}–${fmtT(e.orig_end_s)}</p>`).join("") || "No overlapping source intervals."}</details>`
    + `<details class="panel"><summary>Create manual evidence</summary>${manualMarkup(evidence.episode_id, evidence.transcript_version_id)}</details>`
    + `<details class="panel"><summary>Propose split or merge</summary><p class="meta">Entry points to the observation workflow. Previews are non-mutating and show affected relations: split/merge preserve parent history — reviews, clip verification, creative membership, and campaign support are NOT transferred and require deliberate reassessment.</p>`
    + form("split-form", 0, field("split-observation", "Observation ID to split", "") + field("split-out-0-start", "Output 1 start (seconds)", "", "number") + field("split-out-0-end", "Output 1 end (seconds)", "", "number") + field("split-out-1-start", "Output 2 start (seconds)", "", "number") + field("split-out-1-end", "Output 2 end (seconds)", "", "number") + area("split-rationale", "Rationale (required to commit)", "") + `<button type="button" id="split-preview">Preview split</button> <span role="status" class="meta"></span>`)
    + form("merge-form", 0, field("merge-observations", "Observation IDs to merge (comma-separated)", "") + field("merge-out-start", "Merged output start (seconds)", "", "number") + field("merge-out-end", "Merged output end (seconds)", "", "number") + area("merge-rationale", "Rationale (required to commit)", "") + `<button type="button" id="merge-preview">Preview merge</button> <span role="status" class="meta"></span>`)
    + `<div id="split-merge-preview"></div></details>`
    + `</div><div class="review-side">${brandChoiceMarkup(evidence.read, evidence.assoc_revision)}${sourceLegend()}</div></div>`;
  for (const boundId of ["review-start", "review-end", "bounds-verified", "review-notes"]) $(boundId)?.setAttribute("form", "review-form");
  const selection = wireTranscript(transcript.segments, evidence.episode_id, evidence.transcript_version_id, [effLo, effHi]);
  wireAudio(episode.media, evidence.orig_start_s, evidence.orig_end_s, selection, evidence.transcript_version_id === episode.transcript_version_id ? episode.alignment : "unknown");
  wireBrandChoice(encoded, evidence.read?.brand?.key);
  const reviewFormEl = $("review-form");
  const saveReview = async (advance) => {
    const form = $("review-form");
    if (!form?.isConnected) return;
    const button = form.querySelector('[type="submit"]');
    if (button?.disabled) return;
    const submitted = formValues(form), hash = App.routeHash;
    const status = form.querySelector('[role="status"]');
    if (button) button.disabled = true;
    if (status) status.textContent = "Saving…";
    say("Saving review.");
    try {
      const out = await post(`/api/v1/evidence/${encoded}/review`, {
        judgment: submitted["review-judgment"] || null,
        eff_start_s: submitted["review-start"] === "" ? null : Number(submitted["review-start"]),
        eff_end_s: submitted["review-end"] === "" ? null : Number(submitted["review-end"]),
        bounds_verified: submitted["bounds-verified"], notes: submitted["review-notes"],
      }, form.dataset.revision);
      if (!form.isConnected || App.routeHash !== hash) return;
      advanceRevision(form, out?.revision);
      savedForm(form, submitted);
      if (status) status.textContent = `Saved · revision ${out?.revision}.`;
      say("Saved.");
      if (advance || $("save-next")?.checked) await step(1);
    } catch (error) {
      if (error.name === "AbortError") return;
      rememberForm(form);
      const text = error.status === 412
        ? `Conflict: ${error.message}. Your draft is retained and navigation stopped. Discard the draft to reload the server copy.`
        : error.message;
      if (status) status.textContent = text; else modal("Could not save", `<p>${esc(text)}</p>`);
      say("Save failed. Draft retained; did not advance.");
    } finally { if (button?.isConnected) button.disabled = false; }
  };
  reviewFormEl.addEventListener("submit", (event) => { event.preventDefault(); saveReview(false); });
  $("review-save-next").onclick = () => saveReview(true);
  $("review-keys-off").checked = reviewKeysOff();
  $("review-keys-off").onchange = () => {
    try { localStorage.setItem("earwitness-keys-off", $("review-keys-off").checked ? "1" : "0"); }
    catch (_) { say("Shortcut preference cannot be stored in this browser."); }
    say($("review-keys-off").checked ? "Review shortcuts disabled." : "Review shortcuts enabled.");
  };
  wireReviewKeys(App.generation, saveReview);
  $("clear-judgment").onclick = async () => {
    try { const out = await api(`/api/v1/evidence/${encoded}/review`, { method: "DELETE", headers: { "If-Match": $("review-form").dataset.revision } }); advanceRevision($("review-form"), out.revision); $("review-judgment").value = ""; rememberForm($("review-form")); say("Judgment cleared. Notes and bounds were retained."); }
    catch (error) { modal("Clear failed", `<p>${esc(error.message)}</p>`); }
  };
  let count = 0;
  const add = (a = {}) => { const index = count++; const row = document.createElement("fieldset"); row.innerHTML = `<legend>Brand ${index + 1}</legend>${field(`brand-${index}`, "Brand", a.brand)}${select(`basis-${index}`, "Review basis", ["unknown", "suggested", "confirmed", "incidental"], a.basis || "unknown")}${select(`channel-${index}`, "Attribution channel", ["unspecified", "spoken", "url", "code"], a.channel)}<button type="button" data-remove="${index}">Remove brand</button>`; row.querySelector("button").onclick = () => { $(`brand-${index}`).value = ""; rememberForm($("assoc-form")); row.hidden = true; }; $("association-rows").append(row); $("association-count").value = count; };
  evidence.assocs.forEach(add);
  const previousCount = Number(App.drafts[App.routeHash]?.["assoc-form"]?.values?.["association-count"] || 0);
  while (count < Math.max(previousCount, 1)) add();
  $("add-association").onclick = () => { if (count < 50) { add(); rememberForm($("assoc-form")); } };
  wireForm("assoc-form", (v, element) => { const assocs = []; for (let i = 0; i < count; i++) if (v[`brand-${i}`]?.trim()) assocs.push({ brand: v[`brand-${i}`].trim(), basis: v[`basis-${i}`], channel: v[`channel-${i}`] }); return post(`/api/v1/evidence/${encoded}/assocs`, { assocs }, element.dataset.revision, "PUT"); });
  const confirmState = () => $("confirm-status");
  const renderConfirmRows = (detail) => {
    const box = $("confirm-rows");
    const rev = detail.revision;
    box.innerHTML = ["read_type", "message", "offer", "code"].map((f) => {
      const proposed = (detail.fields || {})[f];
      const conf = (detail.confirmations || {})[f];
      const state = conf ? `Confirmed “${conf.value}” by ${conf.actor} at ${conf.reviewed_at} (obs rev ${conf.observation_revision})` : "Unconfirmed — unknown in campaign comparisons.";
      return `<fieldset><legend>${esc(f)}</legend><p class="meta">Proposed: ${esc(proposed ?? "—")}</p><p class="meta">${esc(state)}</p>`
        + `<div class="od-field"><label>Value<input id="cf-value-${f}" value="${esc(conf ? conf.value : (proposed ?? ""))}"></label></div>`
        + `<div class="od-field"><label>Segment ref (or audio ref below)<input id="cf-seg-${f}" inputmode="numeric" placeholder="0"></label></div>`
        + `<div class="od-field"><label>Audio ref (when no segment)<input id="cf-audio-${f}" placeholder=""></label></div>`
        + `<button type="button" data-cf-confirm="${esc(f)}">Confirm</button> <button type="button" data-cf-clear="${esc(f)}">Clear</button></fieldset>`;
    }).join("");
    box.dataset.revision = rev;
    box.dataset.obs = detail.observation.id;
    box.querySelectorAll("[data-cf-confirm]").forEach((b) => { b.onclick = async () => {
      const f = b.dataset.cfConfirm;
      const segRaw = ($(`cf-seg-${f}`)?.value || "").trim();
      const evidence = segRaw ? { segment_ref: Number(segRaw) } : { audio_ref: ($(`cf-audio-${f}`)?.value || "").trim() };
      try {
        const out = await post(`/api/v1/observations/${encodeURIComponent(box.dataset.obs)}/fields/${encodeURIComponent(f)}`, { value: $(`cf-value-${f}`).value, evidence }, Number(box.dataset.revision), "PUT");
        say(`Confirmed ${f} at revision ${out.revision}.`);
        await loadConfirmations();
      } catch (error) { const text = `Confirm failed: ${error.message}`; say(text); if (confirmState()) confirmState().textContent = text; }
    }; });
    box.querySelectorAll("[data-cf-clear]").forEach((b) => { b.onclick = async () => {
      const f = b.dataset.cfClear;
      try {
        const out = await post(`/api/v1/observations/${encodeURIComponent(box.dataset.obs)}/fields/${encodeURIComponent(f)}`, {}, Number(box.dataset.revision), "DELETE");
        say(`Cleared ${f} at revision ${out.revision}. History kept; authority ended.`);
        await loadConfirmations();
      } catch (error) { const text = `Clear failed: ${error.message}`; say(text); if (confirmState()) confirmState().textContent = text; }
    }; });
  };
  const loadConfirmations = async () => {
    const oid = ($("confirm-obs")?.value || "").trim();
    if (!oid) { if (confirmState()) confirmState().textContent = "Enter the linked observation ID first."; return; }
    try {
      const detail = await api(`/api/v1/observations/${encodeURIComponent(oid)}`);
      renderConfirmRows(detail);
      if (confirmState()) confirmState().textContent = `Loaded observation revision ${detail.revision}.`;
    } catch (error) { if (confirmState()) confirmState().textContent = `Load failed: ${error.message}`; }
  };
  if ($("confirm-load")) $("confirm-load").onclick = loadConfirmations;
  const drawPosition = () => {
    const el = $("queue-position");
    if (!el) return;
    const index = Lib.items.findIndex((e) => e.id === id);
    if (index < 0 || !Lib.total) { el.textContent = "Outside the Library queue."; return; }
    const absolute = (Lib.filter ? Lib.filter.page - 1 : 0) * (Lib.filter?.per_page || Lib.items.length) + index + 1;
    el.textContent = `Result ${absolute} of ${Lib.total}.`;
  };
  const step = async (direction) => {
    let index = Lib.items.findIndex((e) => e.id === id), next = Lib.items[index + direction];
    if (!next && Lib.filter && index >= 0) {
      const page = Lib.filter.page + direction;
      if (page < 1) { say("First result."); drawPosition(); return; }
      const result = await api("/api/v1/evidence?" + queryString({ ...Lib.filter, page }));
      if (result.items.length) { Lib.items = result.items; Lib.filter = result.filter; next = result.items[direction > 0 ? 0 : result.items.length - 1]; }
    }
    if (next) navigate("#/review/" + encodeURIComponent(next.id));
    else {
      const atEnd = direction > 0;
      const msg = atEnd ? "End of queue. No further result in this queue." : "First result in this queue.";
      say(msg);
      drawPosition();
      const pos = $("queue-position");
      if (pos && !pos.textContent.includes(msg)) pos.textContent = `${pos.textContent ? pos.textContent + " " : ""}${msg}`;
    }
  };
  $("review-prev").onclick = () => step(-1); $("review-next").onclick = () => step(1);
  drawPosition();
  const previewInto = $("split-merge-preview");
  $("split-preview").onclick = async () => {
    const status = $("split-form").querySelector('[role="status"]');
    const outputs = [];
    for (const n of [0, 1]) {
      const s = $("split-out-" + n + "-start").value, e = $("split-out-" + n + "-end").value;
      if (s !== "" && e !== "") outputs.push({ start_s: Number(s), end_s: Number(e) });
    }
    if (!outputs.length) { status.textContent = "Enter at least one output interval first. Draft retained."; return; }
    status.textContent = "Previewing…";
    try {
      const out = await post("/api/v1/observations/split-preview", { observation_id: $("split-observation").value.trim(), outputs });
      rememberForm($("split-form"));
      status.textContent = "Preview ready (non-mutating).";
      previewInto.innerHTML = `<h3>Split preview</h3><pre>${esc(JSON.stringify(out.preview, null, 2))}</pre>`;
      say("Split preview ready.");
    } catch (error) { status.textContent = `Preview unavailable: ${error.message}. Draft retained.`; }
  };
  $("merge-preview").onclick = async () => {
    const status = $("merge-form").querySelector('[role="status"]');
    const ids = $("merge-observations").value.split(",").map((s) => s.trim()).filter(Boolean);
    if (!ids.length) { status.textContent = "Enter at least one observation ID first. Draft retained."; return; }
    status.textContent = "Previewing…";
    try {
      const out = await post("/api/v1/observations/merge-preview", { observation_ids: ids, output: { start_s: Number($("merge-out-start").value), end_s: Number($("merge-out-end").value) } });
      rememberForm($("merge-form"));
      status.textContent = "Preview ready (non-mutating).";
      previewInto.innerHTML = `<h3>Merge preview</h3><pre>${esc(JSON.stringify(out.preview, null, 2))}</pre>`;
      say("Merge preview ready.");
    } catch (error) { status.textContent = `Preview unavailable: ${error.message}. Draft retained.`; }
  };
};

Views.explore = async () => {
  const tab = new URLSearchParams(location.hash.split("?")[1]).get("tab") || "brands";
  setToolbar('<a class="btn" href="#/explore?tab=brands">Brands</a><a class="btn" href="#/explore?tab=shows">Shows and episodes</a>');
  $("view").innerHTML = `<h1>${tab === "brands" ? "Brands" : "Shows and episodes"}</h1><p class="meta">Search the imported corpus. A missing detection does not mean an ad is absent.</p>${field("explore-q", tab === "brands" ? "Brand search" : "Show, episode, or full transcript text")}<p id="explore-status" role="status"></p><div id="explore-results"></div><div class="pager"><button id="explore-prev">Previous</button><span id="explore-page"></span><button id="explore-next">Next</button></div>`;
  let request = 0, timer, records = [], page = 1;
  const generation = App.generation, perPage = 25;
  App.controller.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
  const countCache = new Map();
  const draw = () => {
    const start = (page - 1) * perPage, rows = records.slice(start, start + perPage);
    $("explore-status").textContent = `Showing ${records.length ? start + 1 : 0}–${start + rows.length} of ${records.length}.`;
    $("explore-prev").disabled = page === 1; $("explore-next").disabled = start + perPage >= records.length;
    $("explore-page").textContent = `Page ${page}`;
    $("explore-results").innerHTML = tab === "brands"
      ? `<div class="table-wrap"><table class="grid"><thead><tr><th>Brand</th><th>Associations</th><th>Reviewed ads</th><th>Creatives (confirmed creative IDs)</th><th>Occurrences (resolved occurrence IDs)</th></tr></thead><tbody>${rows.map((b) => `<tr><td><a href="#/brand/${encodeURIComponent(b.brand)}">${esc(b.brand)}</a></td><td class="num">${b.c}</td><td class="num">${b.ads}</td><td class="num" data-creative-count="${esc(b.brand)}">…</td><td class="num" data-occurrence-count="${esc(b.brand)}">…</td></tr>`).join("")}</tbody></table></div><p class="meta">Units are distinct and never summed: associations, confirmed creative IDs, and resolved occurrence IDs are separate counts. Proposals stay separated on the brand page.</p>`
      : `<div class="table-wrap"><table class="grid"><thead><tr><th>Show / episode</th><th>Category</th><th>Transcript</th><th>Evidence</th><th>Visibility</th></tr></thead><tbody>${rows.map((e) => `<tr><td>${esc(e.show_title || "Unmapped show")}${e.hidden ? " " + badge("hidden") : ""}<br><a href="#/episode/${encodeURIComponent(e.id)}">${esc(e.ep_title || e.id)}</a><br><span class="meta">${esc(e.id)}</span></td><td>${esc(e.category || "unknown")}</td><td>${e.has_transcript ? "Available" : "Metadata only"}</td><td>${e.evidence}</td><td>${e.show_title ? `<button type="button" data-hide-show="${esc(e.show_title)}" data-show-revision="${e.show_revision ?? 0}" data-show-hidden="${e.hidden ? "1" : ""}">${e.hidden ? "Unhide show" : "Hide show"}</button>` : ""}</td></tr>`).join("")}</tbody></table></div><p class="meta">Hidden shows are excluded from the Library queue and all counts. Raw rows are kept and restored on unhide.</p>`;
    wireHideShow($("explore-results"), () => search());
    if (tab === "brands") {
      const myGen = generation, myReq = request;
      document.querySelectorAll("[data-creative-count]").forEach(async (el) => {
        const brand = el.dataset.creativeCount;
        try {
          let counts = countCache.get(brand);
          if (!counts) { counts = await api(`/api/v1/brands/${encodeURIComponent(brand)}/counts?review=reviewed`); countCache.set(brand, counts); }
          if (myGen !== App.generation || myReq !== request || !el.isConnected) return;
          el.textContent = String(counts.creatives.value);
          el.title = counts.creatives.unit;
          const occ = document.querySelector(`[data-occurrence-count="${CSS.escape(brand)}"]`);
          if (occ) { occ.textContent = String(counts.occurrences.value); occ.title = counts.occurrences.unit; }
        } catch (_) {
          if (!el.isConnected) return;
          el.textContent = "n/a";
          const occ = document.querySelector(`[data-occurrence-count="${CSS.escape(brand)}"]`);
          if (occ) occ.textContent = "n/a";
        }
      });
    }
  };
  const search = async () => {
    if (generation !== App.generation) return;
    const current = ++request;
    try {
      const result = await api(`/api/v1/${tab === "brands" ? "brands" : "episodes"}?q=${encodeURIComponent($("explore-q").value)}`);
      if (generation !== App.generation || current !== request) return;
      records = tab === "brands" ? result.brands : result.episodes; page = 1; draw();
    } catch (error) { if (generation === App.generation && current === request && error.name !== "AbortError") $("explore-status").textContent = error.message; }
  };
  $("explore-prev").onclick = () => { if (page > 1) { page--; draw(); } };
  $("explore-next").onclick = () => { if (page * perPage < records.length) { page++; draw(); } };
  $("explore-q").oninput = () => { clearTimeout(timer); request++; timer = setTimeout(search, 250); };
  await search();
};
Views.brand = async ([raw]) => {
  const name = decodeURIComponent(raw), result = await api(`/api/v1/brands/${encodeURIComponent(name)}`);
  let counts = null, countsError = "";
  try { counts = await api(`/api/v1/brands/${encodeURIComponent(name)}/counts?review=reviewed`); }
  catch (error) { countsError = error.message; }
  setToolbar('<button id="watch-brand">Watch this brand</button><a class="btn" href="#/creatives">Open creatives</a>');
  $("view").innerHTML = `<h1>${esc(name)}</h1><p class="meta">Suggested, incidental, unknown, and confirmed associations are distinct.</p>`
    + (counts ? `<section class="panel"><h2>Counts by unit (reviewed-only by default)</h2><dl class="defs"><dt>Brand association rows</dt><dd class="num">${counts.brand_associations}</dd><dt>Reviewed ad associations</dt><dd class="num">${counts.reviewed_associations}</dd><dt>Creatives</dt><dd class="num">${counts.creatives.value} <span class="meta">${esc(counts.creatives.unit)}</span></dd><dt>Occurrences</dt><dd class="num">${counts.occurrences.value} <span class="meta">${esc(counts.occurrences.unit)}</span></dd><dt>Proposals (separated)</dt><dd class="num">${counts.proposals.value} <span class="meta">${esc(counts.proposals.unit)}</span></dd></dl><p class="meta">${esc(counts.note)}</p><p><a href="#/brand/${encodeURIComponent(name)}">Reviewed-only</a> · <button id="brand-counts-all" type="button">Include proposals</button></p><div id="brand-counts-alt"></div></section>` : `<p role="alert">Counts unavailable: ${esc(countsError)}</p>`)
    + `<div class="table-wrap"><table class="grid"><thead><tr><th>Evidence</th><th>Basis</th><th>Provenance</th><th>Episode</th></tr></thead><tbody>${result.assocs.map((a) => `<tr><td>${evLink(a.evidence_id)}</td><td>${badge(a.basis)}</td><td>${esc(a.origin)} · ${esc(a.channel || "unspecified")}</td><td><a href="#/episode/${encodeURIComponent(a.episode_id)}">${esc(a.episode_id)}</a></td></tr>`).join("")}</tbody></table></div>`;
  $("watch-brand").onclick = () => { App.watchEdit = { name: name, query: { brand: name } }; navigate("#/watchlists/new"); };
  const allBtn = $("brand-counts-all");
  if (allBtn) allBtn.onclick = async () => {
    try {
      const alt = await api(`/api/v1/brands/${encodeURIComponent(name)}/counts?review=all`);
      $("brand-counts-alt").innerHTML = `<dl class="defs"><dt>All associations (incl. proposals)</dt><dd class="num">${alt.brand_associations}</dd><dt>Reviewed ad associations</dt><dd class="num">${alt.reviewed_associations}</dd></dl><p class="meta">Proposals are shown separately above and are never summed with confirmed creative or resolved occurrence counts.</p>`;
      say("Brand counts with proposals shown separately.");
    } catch (error) { $("brand-counts-alt").innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
};
Views.episode = async ([raw]) => {
  const id = decodeURIComponent(raw), encoded = encodeURIComponent(id);
  const [response, transcript, evidence] = await Promise.all([api(`/api/v1/episodes/${encoded}`), api(`/api/v1/episodes/${encoded}/transcript`), api("/api/v1/evidence?" + queryString({ episode: id, per_page: 200, history: "all" }))]);
  const ep = response.episode;
  setToolbar('<button id="watch-show">Watch this show</button>');
  if (ep.hidden) {
    $("view").innerHTML = `<h1>${esc(ep.ep_title || id)}</h1><p>${esc(ep.show_title || "Show unknown")} · ${esc(ep.category || "Category unknown")} · published ${esc(ep.pub_date || "unknown")}</p><section class="panel"><h2>Show hidden</h2><p class="notice" id="hidden-show-notice" role="note">This show is hidden, so this episode's review workbench is unavailable. Its evidence is excluded from the Library queue and all counts. Raw rows are kept. Unhide the show to restore the workbench.</p><p><button type="button" id="unhide-show" data-hide-show="${esc(ep.show_title || "")}" data-show-revision="${ep.show_revision ?? 0}" data-show-hidden="1">Unhide show</button></p></section>`;
    wireHideShow($("view"), () => render());
    $("watch-show").onclick = () => { App.watchEdit = { name: ep.show_title || id, query: ep.show_title ? { show: ep.show_title } : { episode: id } }; navigate("#/watchlists/new"); };
    return;
  }
  $("view").innerHTML = `<h1>${esc(ep.ep_title || id)}</h1><p>${esc(ep.show_title || "Show unknown")} · ${esc(ep.category || "Category unknown")} · published ${esc(ep.pub_date || "unknown")}</p>${ep.alias_of ? `<p class="notice">Exact supplied transcript alias of ${esc(ep.alias_of)}. Both source files are retained.</p>` : ""}${panel("Transcript and audio", audioMarkup() + transcriptMarkup())}${panel("Create first or additional manual evidence", manualMarkup(id, transcript.version_id))}${panel("Media association", form("media-form", ep.revision, field("media-url", "HTTPS audio URL (blank clears)", ep.audio_url) + select("media-id", "Registered media", [["", "No local clip"], ["practice-clip-s002e1", "Bundled 90s clip at 1276s"]], ep.media_id) + select("media-alignment", "Audio alignment with current transcript", [["unknown", "Not verified"], ["verified", "I verified alignment"], ["mismatch", "Known mismatch"]], ep.alignment) + submit("Save media association")))}${panel(`Evidence · ${evidence.total} across all versions`, evidenceTable(evidence.items))}`;
  const selection = wireTranscript(transcript.segments, id, transcript.version_id); wireAudio(ep.media, 0, null, selection, ep.alignment);
  wireForm("media-form", async (v, element) => {
    const out = await post(`/api/v1/episodes/${encoded}/media`, { audio_url: v["media-url"].trim() || null, media_id: v["media-id"] || null, alignment: v["media-alignment"] }, element.dataset.revision, "PUT");
    const saved = (await api(`/api/v1/episodes/${encoded}`)).episode;
    wireAudio(saved.media, 0, null, selection, saved.alignment); return out;
  });
  $("watch-show").onclick = () => { App.watchEdit = { name: ep.show_title || id, query: ep.show_title ? { show: ep.show_title } : { episode: id } }; navigate("#/watchlists/new"); };
};

Views.watchlists = async ([rawId]) => {
  setToolbar('<button id="new-watch">New saved search</button><button id="refresh-watch">Refresh matches</button>');
  const result = await api("/api/v1/watchlists");
  const saved = result.watchlists.find((w) => w.id === Number(rawId));
  const edit = saved ? (App.watchEdit?.id === saved.id ? App.watchEdit : saved) : (App.watchEdit && !App.watchEdit.id ? App.watchEdit : { name: "", query: {} });
  const rows = await Promise.all(result.watchlists.map(async (w) => {
    try { return { ...w, evaluation: await api(`/api/v1/watchlists/${w.id}/evaluate`) }; }
    catch (error) { return { ...w, evaluation: null, evaluationError: error.message }; }
  }));
  const query = typeof edit.query === "string" ? JSON.parse(edit.query) : edit.query;
  $("view").innerHTML = `<h1>Watchlists</h1><p class="meta">Matches imported corpus, including later corrections and imports. No live alerts.</p>${panel(edit.id ? "Edit saved search" : "Create saved search", form("watch-form", edit.revision || 0, field("watch-name", "Name", edit.name) + field("watch-q", "Literal text", query.q) + field("watch-brand", "Brand", query.brand) + field("watch-show", "Show", query.show) + select("watch-source", "Source", [["", "All sources"], "jev", "detector", "pipeline", "manual"], query.source?.[0]) + select("watch-review", "Review", [["", "Any review"], "ad", "not_ad", "unsure", "unreviewed"], query.review?.[0]) + `<p class="meta">Other saved Library filters are preserved. Open the search in Library to adjust every filter.</p>` + submit(edit.id ? "Update watchlist" : "Create watchlist")))}<div class="table-wrap"><table class="grid"><thead><tr><th>Name</th><th>Matches</th><th>Evaluated locally</th><th>Actions</th></tr></thead><tbody>${rows.map((w) => `<tr><td>${esc(w.name)}</td><td>${w.evaluation ? w.evaluation.match_count : "Needs correction"}</td><td>${w.evaluation ? esc(w.evaluation.evaluated_at) : esc(w.evaluationError || "Saved search cannot be evaluated until repaired.")}</td><td><button data-open-watch="${w.id}">Open matches</button><button data-edit-watch="${w.id}">Edit / rename</button><button data-delete-watch="${w.id}">Delete</button></td></tr>`).join("")}</tbody></table></div>`;
  $("new-watch").onclick = () => { App.watchEdit = { name: "", query: {} }; navigate("#/watchlists/new"); };
  $("refresh-watch").onclick = render;
  wireForm("watch-form", async (v, element) => {
    const body = { name: v["watch-name"], query: { ...query, q: v["watch-q"], brand: v["watch-brand"], show: v["watch-show"], source: v["watch-source"] ? [v["watch-source"]] : [], review: v["watch-review"] ? [v["watch-review"]] : [] } };
    const created = !edit.id;
    const out = await post(edit.id ? `/api/v1/watchlists/${edit.id}` : "/api/v1/watchlists", body, edit.id ? element.dataset.revision : undefined, edit.id ? "PUT" : "POST");
    edit.id = out.id; edit.revision = out.revision; App.watchEdit = { ...edit, ...body };
    return { ...out, ...(created ? { route: "#/watchlists/" + out.id } : {}) };
  });
  document.querySelectorAll("[data-open-watch]").forEach((button) => button.onclick = () => { const w = rows.find((r) => r.id === Number(button.dataset.openWatch)); App.editWatchFromLibrary = w; navigate("#/library?" + queryString(JSON.parse(w.query))); });
  document.querySelectorAll("[data-edit-watch]").forEach((button) => button.onclick = () => { App.watchEdit = null; navigate("#/watchlists/" + button.dataset.editWatch); });
  document.querySelectorAll("[data-delete-watch]").forEach((button) => button.onclick = async () => { const w = rows.find((r) => r.id === Number(button.dataset.deleteWatch)); if (!confirm(`Delete ${w.name}?`)) return; try { await api(`/api/v1/watchlists/${w.id}`, { method: "DELETE", headers: { "If-Match": String(w.revision) } }); if (App.watchEdit?.id === w.id) App.watchEdit = null; render(); } catch (error) { modal("Delete failed", `<p>${esc(error.message)}</p>`); } });
};
Views.campaigns = async () => {
  const result = await api("/api/v1/campaigns");
  $("view").innerHTML = `<h1>Campaign briefs</h1>${panel("New brief", form("new-campaign", 0, field("new-campaign-name", "Campaign name") + submit("Create brief")))}<div class="table-wrap"><table class="grid"><thead><tr><th>Campaign</th><th>Status</th><th>Brand</th><th>Manual assessment</th></tr></thead><tbody>${result.campaigns.map((c) => `<tr><td><a href="#/campaign/${c.id}">${esc(c.name)}</a></td><td>${esc(c.status)}</td><td>${esc(c.brand || "unknown")}</td><td>${esc(c.manual_conclusion || "none")}</td></tr>`).join("")}</tbody></table></div>`;
  wireForm("new-campaign", async (v) => { const out = await post("/api/v1/campaigns", { name: v["new-campaign-name"] }); const link = document.createElement("p"); link.innerHTML = `<a href="#/campaign/${out.id}">Open ${esc(v["new-campaign-name"])}</a>`; $("new-campaign").append(link); return out; });
};
Views.campaign = async ([id]) => {
  const campaign = (await api(`/api/v1/campaigns/${id}`)).campaign;
  setToolbar(["json", "csv", "html"].map((format) => `<button data-package="${format}">Campaign ${format === "html" ? "printable HTML" : format.toUpperCase()}</button>`).join(""));
  $("view").innerHTML = `<h1>${esc(campaign.name)}</h1>${panel("Guided campaign flow", '<ol><li><strong>Brief.</strong> Save the brief below. Synthetic briefs stay labeled and are never contractual verification.</li><li><strong>Evidence.</strong> Find and attach judged evidence.</li><li><strong>Checks.</strong> Add checks with separate manual assessments; missing evidence stays unresolved.</li><li><strong>Placement.</strong> Add the expected placement under Expected placements, then evaluate and freeze.</li><li><strong>Share.</strong> Use Campaign printable HTML above for the standalone share pack: readable brief, evidence, checks, placements and frozen evaluations — no app links, no audio payload.</li></ol><p><button id="build-occs" type="button">Build occurrences from attached evidence</button> <button id="eval-now" type="button">Evaluate now (coverage complete)</button></p>')}${panel("Campaign brief", form("campaign-form", campaign.revision, field("campaign-name", "Name", campaign.name) + select("campaign-status", "Status", ["active", "archived"], campaign.status) + field("campaign-brand", "Brand", campaign.brand) + field("campaign-from", "Starts", campaign.date_from, "date") + field("campaign-to", "Ends", campaign.date_to, "date") + area("campaign-shows", "Target shows (one per line)", campaign.target_shows.join("\n")) + area("campaign-message", "Expected message", campaign.message) + field("campaign-offer", "Expected offer", campaign.offer) + area("campaign-notes", "Notes", campaign.notes) + select("campaign-conclusion", "Separate manual conclusion", [["", "No manual conclusion"], "supported", "not_supported", "inconclusive"], campaign.manual_conclusion) + area("campaign-reason", "Manual reason", campaign.manual_reason) + submit("Save brief")))}${panel("Attached evidence", '<div id="campaign-members"></div>' + form("attach-form", campaign.revision, field("evidence-search", "Find evidence by literal text") + '<button id="find-evidence" type="button">Find evidence</button>' + select("attach-id", "Choose evidence", [["", "Search first"]]) + submit("Attach evidence"), false))}${panel("Checks and assessments", '<div id="campaign-checks"></div>' + form("check-form", 0, '<input id="check-id" type="hidden">' + select("check-type", "Check", [["confirmed_brand", "Confirmed brand"], ["literal_phrase", "Literal transcript phrase"], ["duration_range", "Duration range"], ["position_range", "Position range"]]) + select("check-evidence", "Attached evidence", [["", "No evidence — unresolved"]]) + field("check-text", "Brand or literal phrase") + field("check-min", "Minimum seconds (timing checks)", "", "number") + field("check-max", "Maximum seconds (timing checks)", "", "number") + select("check-manual", "Separate manual assessment", [["", "No manual assessment"], "pass", "fail", "unknown"]) + area("check-reason", "Manual assessment reason") + submit("Save check")) + '<p class="meta">Timing requires explicitly verified boundaries and matching audio alignment. Suggestions and missing evidence stay unresolved.</p>')}`;
  const updateRevision = (value) => { const previous = campaign.revision; campaign.revision = value; if (Number($("campaign-form").dataset.revision) === previous) advanceRevision($("campaign-form"), value); advanceRevision($("attach-form"), value); };
  const renderMembers = () => {
    $("campaign-members").innerHTML = campaign.evidence.map((e) => `<p>${evLink(e.id)} · ${esc(e.episode_id)} <button data-detach="${esc(e.id)}">Remove</button></p>`).join("") || '<p>No evidence linked. This is not evidence that the campaign is absent.</p>';
    const selected = $("check-evidence").value;
    $("check-evidence").innerHTML = '<option value="">No evidence — unresolved</option>' + campaign.evidence.map((e) => `<option value="${esc(e.id)}">${esc(e.episode_id)} · ${fmtT(e.orig_start_s)} · ${esc(e.id.slice(0, 18))}</option>`).join(""); $("check-evidence").value = selected;
    document.querySelectorAll("[data-detach]").forEach((button) => button.onclick = async () => { try { const out = await api(`/api/v1/campaigns/${id}/evidence/${encodeURIComponent(button.dataset.detach)}`, { method: "DELETE", headers: { "If-Match": String(campaign.revision) } }); updateRevision(out.campaign_revision); await refreshRelations(); } catch (error) { modal("Remove failed", `<p>${esc(error.message)}</p>`); } });
  };
  const renderChecks = () => {
    $("campaign-checks").innerHTML = campaign.checks.map((check) => `<div class="check-row"><strong>${esc(check.check_type.replaceAll("_", " "))}</strong> · ${check.evidence_id ? evLink(check.evidence_id) : "No evidence"}<p>Computed: ${badge(check.result.outcome)} ${esc(check.result.reason)}</p><p class="meta">Manual: ${esc(check.manual_verdict || "none")} ${esc(check.manual_reason)}</p><button data-edit-check="${check.id}">Edit definition / assessment</button></div>`).join("") || '<p>No checks yet.</p>';
    document.querySelectorAll("[data-edit-check]").forEach((button) => button.onclick = () => {
      if ($("check-form").querySelector('[type="submit"]').disabled) return say("Wait for the current save.");
      if (App.drafts[App.routeHash]?.["check-form"] && !confirm("Discard the current check draft?")) return;
      const check = campaign.checks.find((c) => c.id === Number(button.dataset.editCheck));
      $("check-id").value = check.id; $("check-form").dataset.revision = check.revision; $("check-type").value = check.check_type; $("check-evidence").value = check.evidence_id || ""; $("check-text").value = check.params.brand || check.params.phrase || ""; $("check-min").value = check.params.min_s ?? ""; $("check-max").value = check.params.max_s ?? ""; $("check-manual").value = check.manual_verdict || ""; $("check-reason").value = check.manual_reason; rememberForm($("check-form")); $("check-type").focus();
    });
  };
  const refreshRelations = async () => { const current = (await api(`/api/v1/campaigns/${id}`)).campaign; campaign.evidence = current.evidence; campaign.checks = current.checks; renderMembers(); renderChecks(); };
  renderMembers(); renderChecks();
  const newCheck = document.createElement("button"); newCheck.type = "button"; newCheck.textContent = "New check";
  newCheck.onclick = () => {
    if ($("check-form").querySelector('[type="submit"]').disabled) return say("Wait for the current save.");
    if (App.drafts[App.routeHash]?.["check-form"] && !confirm("Discard the current check draft and start a new check?")) return;
    $("check-form").reset(); $("check-id").value = ""; $("check-form").dataset.revision = "0";
    delete App.drafts[App.routeHash]?.["check-form"]; persistDrafts();
    $("check-form").querySelector('[role="status"]').textContent = ""; $("check-type").focus();
  };
  $("check-form").append(newCheck);
  wireForm("campaign-form", async (v, element) => { const out = await post(`/api/v1/campaigns/${id}`, { name: v["campaign-name"], status: v["campaign-status"], brand: v["campaign-brand"] || null, date_from: v["campaign-from"] || null, date_to: v["campaign-to"] || null, target_shows: v["campaign-shows"].split("\n").map((s) => s.trim()).filter(Boolean), message: v["campaign-message"], offer: v["campaign-offer"], notes: v["campaign-notes"], manual_conclusion: v["campaign-conclusion"] || null, manual_reason: v["campaign-reason"] }, element.dataset.revision, "PUT"); updateRevision(out.revision); return out; });
  $("find-evidence").onclick = async () => { try { const result = await api("/api/v1/evidence?" + queryString({ q: $("evidence-search").value, per_page: 200 })); $("attach-id").innerHTML = result.items.map((e) => `<option value="${esc(e.id)}">${esc(e.show_title || e.episode_id)} · ${fmtT(e.orig_start_s)} · ${esc(e.excerpt.slice(0, 90))}</option>`).join("") || '<option value="">No matching evidence</option>'; say(`${result.total} matches. Refine the search if more than 200.`); } catch (error) { modal("Search failed", `<p>${esc(error.message)}</p>`); } };
  wireForm("attach-form", async (v) => { const out = await post(`/api/v1/campaigns/${id}/evidence`, { evidence_id: v["attach-id"] }, campaign.revision); updateRevision(out.campaign_revision); await refreshRelations(); return out; });
  wireForm("check-form", async (v, element) => {
    const type = v["check-type"], params = type === "confirmed_brand" ? { brand: v["check-text"] } : type === "literal_phrase" ? { phrase: v["check-text"] } : { min_s: v["check-min"] === "" ? null : Number(v["check-min"]), max_s: v["check-max"] === "" ? null : Number(v["check-max"]) };
    const body = { campaign_id: Number(id), campaign_revision: campaign.revision, check_type: type, evidence_id: v["check-evidence"] || null, params, manual_verdict: v["check-manual"] || null, manual_reason: v["check-reason"] };
    const out = await post(v["check-id"] ? `/api/v1/checks/${v["check-id"]}` : "/api/v1/checks", body, v["check-id"] ? element.dataset.revision : undefined, v["check-id"] ? "PUT" : "POST");
    updateRevision(out.campaign_revision); $("check-id").value = out.id; await refreshRelations(); return out;
  });
  document.querySelectorAll("[data-package]").forEach((button) => button.onclick = () => exportRecords({ format: button.dataset.package, campaign_id: Number(id), title: `Campaign: ${campaign.name}` }));
  $("build-occs").onclick = async () => {
    if (!campaign.evidence.length) { say("No attached evidence — attach judged evidence first."); return; }
    if (!$("eval-occs")) { say("Expected placements section is not loaded — reload the campaign page."); return; }
    const occs = [];
    try {
      for (const e of campaign.evidence) {
        const d = (await api(`/api/v1/evidence/${encodeURIComponent(e.id)}`)).evidence;
        const review = d.review || {}, judgment = review.judgment;
        const lo = review.eff_start_s ?? d.orig_start_s, hi = review.eff_end_s ?? d.orig_end_s;
        const occ = {
          id: `built:${d.id}`, episode_id: d.episode_id,
          review_status: judgment === "ad" ? "reviewed_ad" : judgment === "not_ad" ? "reviewed_not_ad" : judgment === "unsure" ? "reviewed_unsure" : "unreviewed",
          correspondence: "confirmed",
          advertiser_confirmed: (d.assocs || []).some((a) => a.basis === "confirmed"),
          bounds_verified: !!review.bounds_verified,
        };
        if (Number.isFinite(lo)) occ.verified_start_s = lo;
        if (Number.isFinite(lo) && Number.isFinite(hi)) { occ.position_s = lo; occ.duration_s = hi - lo; }
        occs.push(occ);
      }
    } catch (error) { modal("Occurrence build failed", `<p>${esc(error.message)}</p>`); return; }
    $("eval-occs").value = JSON.stringify(occs, null, 2);
    say(`${occs.length} occurrences ready — the supply behind this verdict.`);
  };
  $("eval-now").onclick = () => {
    if (!$("placement-eval") || !$("eval-coverage")) { say("Expected placements section is not loaded — reload the campaign page."); return; }
    $("eval-coverage").value = '{"state": "complete"}';
    $("placement-eval").requestSubmit();
  };
};

const Proc = { selected: new Map(), estimate: null, estimateKey: "", detail: null, runs: [] };
const procProgress = (done, total) => Number.isFinite(done) && Number.isFinite(total) && total > 0
  ? `<progress value="${done}" max="${total}">${done} of ${total} stages</progress>`
  : '<span class="meta">Progress unknown until the run reports stage counts.</span>';
const procStageCounts = (jobs) => {
  const done = jobs.filter((j) => ["succeeded", "failed", "cancelled"].includes(j.state)).length;
  return { done, total: jobs.length };
};
async function procEstimate(selected, profile, configText, statusEl) {
  let config = null;
  if ((configText || "").trim()) {
    try { config = JSON.parse(configText); }
    catch (_) { statusEl.textContent = "Config must be valid JSON or blank. Nothing was sent."; return null; }
  }
  try {
    const out = await post("/api/v1/processing/estimate",
      { episode_ids: [...selected.keys()], profile, config });
    Proc.estimate = out; Proc.estimateKey = [...selected.keys()].join(",") + "|" + profile + "|" + (configText || "").trim();
    return out;
  } catch (error) { statusEl.textContent = `Estimate rejected: ${error.message}`; return null; }
}
function procEstimateMarkup(out) {
  return `<dl class="defs"><dt>Episodes</dt><dd>${out.episodes.length}</dd>`
    + `<dt>Stages</dt><dd>${esc(out.stages.join(" → "))}</dd>`
    + `<dt>Cache reuse</dt><dd>${out.reusable} of ${out.stage_count} stage outputs already succeeded; ${out.new_stages} need work</dd>`
    + `<dt>Reserved cost</dt><dd>$${Number(out.reserved_cost).toFixed(2)} — ${esc(out.cost_note)}</dd>`
    + `<dt>Retention</dt><dd>${esc(out.retention_note)}</dd>`
    + `<dt>Config digest</dt><dd class="code">${esc(out.config_digest.slice(0, 24))}… (a new model or config is a new run)</dd></dl>`
    + `<div class="table-wrap"><table class="grid"><thead><tr><th>Episode</th><th>Stage</th><th>Cached</th></tr></thead><tbody>`
    + out.rows.map((r) => `<tr><td>${esc(r.episode_id)}</td><td>${esc(r.stage)}</td><td>${r.reused ? "Reused" : "New work"}</td></tr>`).join("")
    + `</tbody></table></div>`;
}
Views.processing = async () => {
  let sources = null, runs = { runs: [] }, loadError = "";
  try {
    [sources, runs] = await Promise.all([
      api("/api/v1/processing/sources"), api("/api/v1/processing/runs?limit=20")]);
  } catch (error) { loadError = error.message; }
  Proc.runs = runs.runs || [];
  if (Proc.detail) {
    const stillThere = Proc.runs.some((r) => r.run_id === Proc.detail.run.run_id);
    if (!stillThere) Proc.detail = null;
  }
  setToolbar('<button id="proc-refresh">Refresh</button><a class="btn" href="#/library">Back to Library</a>');
  const caps = sources?.uploads?.audio;
  $("view").innerHTML = `<h1>Processing center</h1>`
    + `<p class="meta">Approved feeds and files in, explicit starts only. Search and import review never start runs. The server never launches workers.</p>`
    + (loadError ? `<p role="alert">Processing state unavailable: ${esc(loadError)}</p>` : "")
    + panel("New run",
      field("proc-episode-q", "Find episodes (show, episode, or transcript text)", "")
      + `<div id="proc-episode-results"></div>`
      + `<h3>Selected episodes (<span id="proc-sel-count">0</span>)</h3><div id="proc-selected"></div>`
      + form("estimate-form", 0, field("proc-profile", "Backend profile", "local")
        + area("proc-config", "Optional config JSON (a change starts a new run, never mutates an old one)", "")
        + submit("Inspect estimate"), false)
      + `<div id="proc-estimate"></div>`
      + `<button id="proc-start" type="button" class="primary" disabled>Start run</button> <span id="proc-start-status" class="meta" role="status"></span>`)
    + `<div id="proc-runs"></div>`
    + panel("Add sources",
      `<h3>Registered RSS feeds</h3>`
      + `<div class="table-wrap"><table class="grid"><thead><tr><th>Feed host</th><th>Registered URL</th></tr></thead><tbody>`
      + ((sources?.feed_sources || []).map((s) => `<tr><td>${esc(s.host)}</td><td class="code">${esc(s.url)}</td></tr>`).join("")
        || '<tr><td colspan="2">No RSS feeds registered. Shows already imported are listed under Registered inputs.</td></tr>')
      + `</tbody></table></div>`
      + form("rss-form", 0, field("rss-url", "RSS feed URL (https, public host only)", "") + submit("Validate and register"), false)
      + `<h3>Direct audio upload</h3><p class="meta">${caps ? `Permitted: ${esc(caps.mime_types.join(", "))}. Bounded to ${(caps.max_bytes / 1048576).toFixed(0)} MiB and ${(caps.max_duration_s / 3600).toFixed(0)} h. Filenames are labels only; the stored artifact is content-hash named and private to this workspace.` : "Upload limits unavailable."}</p>`
      + `<label class="field" for="audio-file"><span>Audio file</span><input id="audio-file" type="file" accept="audio/*,.mp3,.m4a,.wav,.ogg,.flac,.aac"></label>`
      + `<button id="audio-upload" type="button">Upload audio</button> <span id="upload-status" class="meta" role="status"></span>`
      + `<p class="meta">Transcript JSON uses the existing compatible upload in <a href="#/data">Data &amp; Settings</a>.</p>`)
    + panel(`Registered inputs · ${(sources?.registered_inputs || []).length} shows`,
      `<p class="meta">Shows with episodes in this workspace. To start a run, find episodes in New run above.</p>`
      + `<div class="table-wrap"><table class="grid"><thead><tr><th>Show</th><th>Episodes</th></tr></thead><tbody>`
      + ((sources?.registered_inputs || []).map((f) => `<tr><td>${esc(f.show_title || "Unmapped show")}</td><td class="num">${f.episodes}</td></tr>`).join("")
        || '<tr><td colspan="2">No episodes in this workspace yet.</td></tr>')
      + `</tbody></table></div>`);
  $("proc-refresh").onclick = render;
  const drawSelected = () => {
    $("proc-sel-count").textContent = String(Proc.selected.size);
    $("proc-selected").innerHTML = [...Proc.selected.entries()].map(([id, title]) =>
      `<p>${esc(title)} <span class="code">${esc(id)}</span> <button type="button" data-unselect="${esc(id)}">Remove</button></p>`).join("")
      || '<p class="meta">No episodes selected.</p>';
    document.querySelectorAll("[data-unselect]").forEach((b) => b.onclick = () => {
      Proc.selected.delete(b.dataset.unselect); Proc.estimate = null; drawSelected(); $("proc-estimate").innerHTML = ""; $("proc-start").disabled = true;
    });
  };
  drawSelected();
  let searchTimer = null;
  $("proc-episode-q").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = $("proc-episode-q").value.trim();
      if (!q) { $("proc-episode-results").innerHTML = ""; return; }
      try {
        const result = await api(`/api/v1/episodes?q=${encodeURIComponent(q)}`);
        $("proc-episode-results").innerHTML = `<div class="table-wrap"><table class="grid"><thead><tr><th></th><th>Show / episode</th></tr></thead><tbody>`
          + (result.episodes.map((e) => `<tr><td><input type="checkbox" data-pick="${esc(e.id)}" aria-label="Select ${esc(e.ep_title || e.id)}" ${Proc.selected.has(e.id) ? "checked" : ""}></td><td>${esc(e.show_title || "Unmapped show")}<br><span class="meta">${esc(e.ep_title || e.id)}</span></td></tr>`).join("")
            || '<tr><td colspan="2">No matching episodes.</td></tr>') + `</tbody></table></div>`;
        document.querySelectorAll("[data-pick]").forEach((el) => el.onchange = async () => {
          const row = result.episodes.find((e) => e.id === el.dataset.pick);
          if (el.checked) Proc.selected.set(el.dataset.pick, `${row.show_title || ""} — ${row.ep_title || row.id}`);
          else Proc.selected.delete(el.dataset.pick);
          Proc.estimate = null; drawSelected(); $("proc-estimate").innerHTML = ""; $("proc-start").disabled = true;
        });
      } catch (error) { $("proc-episode-results").innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
    }, 250);
  };
  $("rss-form").onsubmit = async (event) => {
    event.preventDefault();
    const status = $("rss-form").querySelector('[role="status"]');
    status.textContent = "Validating…";
    try {
      const out = await post("/api/v1/processing/sources", { url: $("rss-url").value.trim() });
      status.textContent = `Registered ${out.host}.`;
      $("rss-url").value = ""; render();
    } catch (error) { status.textContent = `Rejected: ${error.message}. Nothing reached the worker.`; }
  };
  $("audio-upload").onclick = async () => {
    const file = $("audio-file").files[0], status = $("upload-status");
    if (!file) { status.textContent = "Choose an audio file first."; return; }
    $("audio-upload").disabled = true; status.textContent = "Uploading…";
    try {
      const response = await fetch("/api/v1/processing/uploads", {
        method: "POST", headers: { "X-CSRF-Token": App.csrf, "X-Filename": file.name, "Content-Type": file.type || "audio/mpeg" },
        body: file });
      const body = await response.json();
      if (!response.ok || body.ok === false) throw new Error(body.error || `Upload failed (${response.status})`);
      status.textContent = `Staged ${body.bytes} bytes${body.duration_s ? `, ${body.duration_s.toFixed(1)}s audio` : " (duration unknown)"}. Artifact ${body.artifact_id}.`;
      say("Audio staged as a private artifact.");
    } catch (error) { status.textContent = `Rejected: ${error.message}`; }
    finally { $("audio-upload").disabled = false; }
  };
  $("estimate-form").onsubmit = async (event) => {
    event.preventDefault();
    const status = $("estimate-form").querySelector('[role="status"]');
    if (!Proc.selected.size) { status.textContent = "Select at least one episode first."; return; }
    status.textContent = "Estimating…";
    const out = await procEstimate(Proc.selected, $("proc-profile").value.trim() || "local", $("proc-config").value, status);
    if (!out) { $("proc-start").disabled = true; return; }
    status.textContent = "Estimate ready. Review bounds, cost, and retention before starting.";
    $("proc-estimate").innerHTML = procEstimateMarkup(out);
    $("proc-start").disabled = false;
    $("proc-start-status").textContent = "";
  };
  $("proc-start").onclick = async () => {
    if (!$("proc-estimate").innerHTML || !Proc.estimate) { $("proc-start-status").textContent = "Inspect an estimate first."; return; }
    const currentKey = [...Proc.selected.keys()].join(",") + "|" + ($("proc-profile").value.trim() || "local") + "|" + $("proc-config").value.trim();
    if (currentKey !== Proc.estimateKey) { $("proc-start-status").textContent = "Selection changed since the estimate. Re-inspect before starting."; $("proc-start").disabled = true; return; }
    $("proc-start").disabled = true;
    $("proc-start-status").textContent = "Starting… (repeat clicks replay the same run, never a new one)";
    try {
      const out = await post("/api/v1/processing/runs", {
        idempotency_key: [...Proc.selected.keys()].join(",") + "|" + Proc.estimate.config_digest,
        episode_ids: [...Proc.selected.keys()],
        profile: $("proc-profile").value.trim() || "local",
        config: $("proc-config").value.trim() ? JSON.parse($("proc-config").value) : null });
      $("proc-start-status").textContent = `Started ${out.run.run_id}.`;
      Proc.detail = out; await drawRuns();
      say(`Run ${out.run.run_id} started.`);
    } catch (error) { $("proc-start-status").textContent = `Start rejected: ${error.message}`; }
    finally { $("proc-start").disabled = false; }
  };
  const drawRuns = async () => {
    try {
      const fresh = await api("/api/v1/processing/runs?limit=20");
      Proc.runs = fresh.runs || [];
    } catch (error) { say(`Run list refresh failed: ${error.message}`); return; }
    // One Runs panel, redrawn in place (B-12).
    const host = $("proc-runs");
    if (!host) return;
    host.innerHTML = `<section class="panel"><h2>Runs</h2><div class="table-wrap"><table class="grid"><thead><tr><th>Run</th><th>Status</th><th>Stages</th><th>Cost</th><th></th></tr></thead><tbody>`
      + (Proc.runs.map((r) => {
        return `<tr><td class="code">${esc(r.run_id)}</td><td>${badge(r.status)}<br><span class="meta">${esc(r.financial_status || "")}</span></td>`
          + `<td class="num">${esc(Object.entries(r.stage_counts || {}).map(([k, v]) => `${k.split(":")[1] || k} ${v}`).join(", "))}</td>`
          + `<td class="num">$${Number(r.reserved_cost || 0).toFixed(2)}${r.unknown_cost ? ` + ${Number(r.unknown_cost).toFixed(2)} unresolved` : ""}</td>`
          + `<td><button type="button" data-open-run="${esc(r.run_id)}">Inspect</button></td></tr>`;
      }).join("") || '<tr><td colspan="5">No runs yet. Estimates never create runs.</td></tr>') + `</tbody></table></div><div id="proc-detail"></div></section>`;
    host.querySelectorAll("[data-open-run]").forEach((b) => b.onclick = async () => {
      try {
        Proc.detail = await api(`/api/v1/processing/runs/${encodeURIComponent(b.dataset.openRun)}`);
        drawDetail();
      } catch (error) { say(`Run detail failed: ${error.message}`); }
    });
    drawDetail();
  };
  const drawDetail = () => {
    const host = $("proc-detail");
    if (!host) return;
    const d = Proc.detail;
    if (!d) { host.innerHTML = '<p class="meta">Select a run to inspect stages, failures, and recovery.</p>'; return; }
    const { done, total } = procStageCounts(d.jobs || []);
    const failed = (d.jobs || []).filter((j) => j.state === "failed");
    const workerBlock = d.worker?.worker_live
      ? `<p class="meta">Worker live (heartbeat ${esc(d.worker.last_heartbeat || "recent")}).</p>`
      : `<p class="notice" role="alert">Worker offline. Required profile: ${esc(d.required_profile)}. Run the operator command: <code>${esc(d.operator_command)}</code></p>`
        + (d.worker?.queued_without_worker ? `<p class="meta">Queued work is waiting, not processing.</p>` : "");
    host.innerHTML = `<h3>Run <span class="code">${esc(d.run.run_id)}</span> ${badge(d.run.status)}</h3>`
      + `<p class="meta">Revision ${d.run.revision} · mode ${esc(d.run.mode)} · financial ${esc(d.run.financial_status)}</p>`
      + procProgress(done, total) + `<p class="meta">${done} of ${total} stages settled (succeeded, failed, or cancelled).</p>`
      + `<dl class="defs"><dt>Reserved cost</dt><dd>$${Number(d.run.reserved_cost || 0).toFixed(2)}</dd>`
      + `<dt>Unresolved exposure</dt><dd>${d.run.unknown_cost ? `$${Number(d.run.unknown_cost).toFixed(2)} — pending reconciliation, never auto-cleared` : "None"}</dd>`
      + `<dt>Failures</dt><dd>${failed.length ? failed.map((j) => `${esc(j.stage)} (${esc(j.failure_class || "unknown")})`).join(", ") : "None"}</dd>`
      + `<dt>Skipped</dt><dd>${(d.jobs || []).filter((j) => j.state === "cancelled").length} cancelled stages</dd></dl>`
      + `<div class="table-wrap"><table class="grid"><thead><tr><th>Episode</th><th>Stage</th><th>State</th><th>Failure</th></tr></thead><tbody>`
      + Object.entries(d.per_episode || {}).map(([ep, js]) => js.map((j) =>
        `<tr><td>${esc(ep)}</td><td>${esc(j.stage)}</td><td>${badge(j.state)}</td><td>${esc(j.failure_class || "—")}</td></tr>`).join("")).join("")
      + `</tbody></table></div>`
      + `<details><summary>Stage attempts and events (${(d.attempts || []).length})</summary><div class="table-wrap"><table class="grid"><thead><tr><th>Job</th><th>Attempt</th><th>State</th><th>Error</th></tr></thead><tbody>`
      + ((d.attempts || []).map((a) => `<tr><td class="code">${esc(a.job_id)}</td><td class="num">${a.attempt_no}</td><td>${esc(a.state)}</td><td>${esc(a.error || "—")}</td></tr>`).join("")
        || '<tr><td colspan="4">No attempts recorded yet.</td></tr>') + `</tbody></table></div></details>`
      + workerBlock
      + `<h4>Results</h4>`
      + (d.episodes || []).map((ep) => `<p><a href="#/library?episode=${encodeURIComponent(ep)}">${esc(ep)}</a>`
        + ((d.coverage_gaps || []).includes(ep) ? ' <span class="badge uncertain">coverage gap — no completed run</span>' : ' <span class="badge ad">completed</span>') + `</p>`).join("")
      + ((d.coverage_gaps || []).length ? `<p class="meta">Gaps sit beside successes: a gap means no completed processing here, not proof that no ad exists.</p>` : "")
      + (failed.length ? `<h4>Retry failed work only</h4><form id="retry-form" data-draft="off">`
        + failed.map((j) => `<label><input type="checkbox" name="retry-job" value="${esc(j.id)}" checked> ${esc(j.stage)} <span class="code">${esc(j.id)}</span> (${esc(j.failure_class || "unknown")})</label>`).join("")
        + `<br><button type="submit">Retry selected failures</button> <span role="status" class="meta"></span></form>
        <p class="meta">Retry keeps the existing input and config identity. A new model or config starts a new run from a fresh estimate.</p>` : "")
      + `<h4>Cancel</h4><form id="cancel-form" data-draft="off"><button type="submit">Request cancellation (revision ${d.run.revision})</button> <span role="status" class="meta"></span></form>`
      + ((d.reconcile || []).length ? `<h4>Pending reconciliation</h4>` + d.reconcile.map((r) =>
        `<p class="notice">Operation <span class="code">${esc(r.operation_id)}</span> (${esc(r.disposition)}): ${esc(r.note)}<br>Operator action: <code>${esc(r.operator_command)}</code></p>`).join("") : "");
    const retryForm = $("retry-form");
    if (retryForm) retryForm.onsubmit = async (event) => {
      event.preventDefault();
      const status = retryForm.querySelector('[role="status"]');
      const picked = [...retryForm.querySelectorAll('[name="retry-job"]:checked')].map((el) => el.value);
      if (!picked.length) { status.textContent = "Select at least one failed job."; return; }
      status.textContent = "Retrying…";
      try {
        Proc.detail = await post(`/api/v1/processing/runs/${encodeURIComponent(d.run.run_id)}/retry`,
          { failed_job_ids: picked, idempotency_key: `${d.run.run_id}-r${d.run.revision}`, expected_revision: d.run.revision });
        drawDetail(); await drawRunsRefreshOnly(); say("Retry accepted for selected failures.");
      } catch (error) { status.textContent = `Retry rejected: ${error.message}`; }
    };
    $("cancel-form").onsubmit = async (event) => {
      event.preventDefault();
      const status = $("cancel-form").querySelector('[role="status"]');
      status.textContent = "Requesting cancellation…";
      try {
        Proc.detail = await post(`/api/v1/processing/runs/${encodeURIComponent(d.run.run_id)}/cancel`,
          { expected_revision: d.run.revision });
        drawDetail(); await drawRunsRefreshOnly(); say("Cancellation recorded; running work observes the fence.");
      } catch (error) { status.textContent = `Cancel rejected: ${error.message}`; }
    };
  };
  const drawRunsRefreshOnly = async () => {
    try {
      const fresh = await api("/api/v1/processing/runs?limit=20");
      Proc.runs = fresh.runs || [];
    } catch (error) { say(`Run list refresh failed: ${error.message}`); }
  };
  await drawRuns();
};
function agentConnState() { try { return JSON.parse(localStorage.getItem("earwitness-agent-conn") || "{}"); } catch (_) { return {}; } }
function agentConnSave(patch) { try { localStorage.setItem("earwitness-agent-conn", JSON.stringify({ ...agentConnState(), ...patch })); return true; } catch (_) { return false; } }
function agentConnRender() {
  const box = $("agent-conn-state"); if (!box) return;
  const s = agentConnState();
  if (s.status === "Connected") box.innerHTML = `<p class="success-box">Connected · authorized coverage verified${s.verifiedAt ? ` at ${esc(s.verifiedAt)}` : ""}${s.coverage ? ` · ${esc(s.coverage)}` : ""}.</p>`;
  else if (s.status === "Awaiting verification") box.innerHTML = `<p class="notice">Awaiting verification — configuration ready, no authorized request yet. Copying config never connects.</p>`;
  else if (s.status === "Verification failed") box.innerHTML = `<p class="error-box">Verification failed${s.detail ? `: ${esc(s.detail)}` : "."} Fix the cause and verify again.</p>`;
  else box.innerHTML = `<p class="meta">Not connected — no local agent configuration recorded here.</p>`;
}
function agentSnippet(client, command, script, workspace) {
  const quote = (s) => `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
  const args = [script, "--workspace", workspace];
  if (client === "codex") return `[mcp_servers.earwitness]\ncommand = ${quote(command)}\nargs = [${args.map(quote).join(", ")}]`;
  return JSON.stringify({ mcpServers: { earwitness: { command, args } } }, null, 2);
}
function wireAgentConnections() {
  if (!$("agent-verify")) return;
  agentConnRender();
  $("agent-command").placeholder = "/path/to/.venv-agent/bin/python";
  $("agent-script").placeholder = "/path/to/repo/scripts/app_mcp.py";
  $("agent-workspace").placeholder = "/absolute/path/to/workspace";
  $("agent-verify").onclick = async () => {
    try {
      const overview = await api("/api/v1/overview");
      const coverage = `${overview.canonical_transcript_episodes ?? "?"} episodes · ${overview.transcript_hours ?? "?"} transcript hours · ${Object.values(overview.evidence_by_source || {}).reduce((a, b) => a + b, 0)} evidence records`;
      agentConnSave({ status: "Connected", verifiedAt: new Date().toISOString(), coverage });
      say("Agent connection verified through an authorized coverage request.");
    } catch (error) {
      agentConnSave({ status: "Verification failed", detail: error.message });
      say("Agent connection verification failed.");
    }
    agentConnRender();
  };
  $("agent-forget").onclick = () => { agentConnSave({ status: "Not connected", verifiedAt: null, coverage: null, detail: null }); agentConnRender(); say("Local agent connection state forgotten. Client configurations elsewhere are unaffected."); };
  $("agent-snippet").onclick = () => {
    const status = $("agent-snippet-status"), out = $("agent-snippet-out");
    const command = $("agent-command").value.trim(), script = $("agent-script").value.trim(), workspace = $("agent-workspace").value.trim();
    if (!command || !script || !workspace) { status.textContent = "Enter all three paths first."; return; }
    if (!workspace.startsWith("/")) { status.textContent = "Workspace must be an absolute path."; return; }
    out.textContent = agentSnippet($("agent-client").value, command, script, workspace);
    status.textContent = "Generated locally — not connected until verified.";
    if ((agentConnState().status || "Not connected") === "Not connected") agentConnSave({ status: "Awaiting verification" });
    agentConnRender(); say("Snippet generated. Status is Awaiting verification, not Connected.");
  };
  $("agent-copy").onclick = async () => {
    const status = $("agent-snippet-status"), text = $("agent-snippet-out").textContent;
    if (!text || text === "Generate to preview.") { status.textContent = "Generate a snippet first."; return; }
    try { await navigator.clipboard.writeText(text); status.textContent = "Copied. Copying never connects."; }
    catch (_) {
      const area = document.createElement("textarea"); area.value = text; document.body.append(area); area.select();
      try { document.execCommand("copy"); status.textContent = "Copied. Copying never connects."; }
      catch (_) { status.textContent = "Copy unavailable — select the snippet manually."; }
      area.remove();
    }
  };
  $("agent-open").onclick = () => {
    const status = $("agent-cite-status"), raw = $("agent-citation").value.trim();
    const m = raw.match(/^ew-local:\/\/\/w\/[^/]+\/evidence\/([^/?#]+)$/);
    const id = m ? decodeURIComponent(m[1]) : raw;
    if (!id || /\s/.test(id)) { status.textContent = "Enter an ew-local://…/evidence/<id> citation or a bare evidence id."; return; }
    navigate("#/review/" + encodeURIComponent(id));
  };
}
// Hosted connections live in the hosted service, which this local app is not
// part of: the local server has no connections route. Say so instead of asking for
// a route that can only answer 404.
function wireHostedConnections() {
  const box = $("hosted-conn-state"); if (!box) return;
  box.innerHTML = `<p class="meta">Status: <strong>Not part of this local app</strong></p><p class="meta">Hosted connections are created, shown and revoked in the hosted service. This local workspace has no hosted connection, so there is nothing to show or revoke here. Your local agent setup is under Agent connections (local).</p>`;
}
Views.data = async () => {
  const [overview, settings, audit] = await Promise.all([api("/api/v1/overview"), api("/api/v1/settings"), api("/api/v1/audit?limit=20")]);
  $("view").innerHTML = `<h1>Data and settings</h1><nav class="op-index" aria-label="Sections on this page"><span class="meta">Jump to</span> <button type="button" data-jump="ds-settings">Settings</button> <button type="button" data-jump="ds-corpus">Supplied corpus</button> <button type="button" data-jump="ds-upload">Transcript upload</button> <button type="button" data-jump="ds-agent-local">Agent connections (local)</button> <button type="button" data-jump="ds-backup">Backup</button> <button type="button" data-jump="ds-agent-hosted">Agent connections (hosted)</button> <button type="button" data-jump="ds-changes">Recent changes</button> <button type="button" data-jump="ds-proc-sources">Processing</button> <button type="button" data-jump="ds-source-versions">Source versions</button></nav>${panel("Supplied corpus", '<p class="meta">Imports stay local. No download, transcription, or model call runs here.</p><button id="seed">Import / refresh supplied corpus</button> <button id="reindex">Reindex search</button><p id="import-status" role="status"></p><pre id="import-report"></pre>', "ds-corpus")}${panel("Compatible transcript upload", form("upload-form", 0, area("upload-json", "Transcript JSON") + field("upload-origin", "Origin note (required)") + submit("Import transcript")) + '<p class="meta">Upload IDs are separate from supplied episodes. Identical imports are reused; changed transcripts create versions. Open an episode to create its first manual evidence.</p>', "ds-upload")}${panel("Settings", form("settings-form", 0, field("settings-speed", "Playback speed (0.5–2.0)", settings.settings.playback_speed, "number") + select("settings-density", "Density", ["comfortable", "compact"], settings.settings.page_density) + submit("Save settings")), "ds-settings")}${panel("Agent connections (local)", '<p class="meta">Local stdio reader only — no tokens, no hosted services. Copying or generating a configuration never connects anything: status turns Connected only after an authorized request succeeds. Retrieved content may reach your agent provider\u2019s remote models. Full instructions: <span class="code">docs/agent-connect.md</span>.</p><div id="agent-conn-state"><p class="meta">Status: loading…</p></div><p><button id="agent-verify" type="button">Verify connection</button> <button id="agent-forget" type="button">Forget local state</button></p><p class="meta">Verify runs an authorized coverage request through this browser session. It does not observe your external client\u2019s own requests — verify those with the probe command in the guide.</p><p><strong>Fixed reader profile</strong></p><div class="table-wrap"><table class="grid"><thead><tr><th>Scope</th><th>Permits</th></tr></thead><tbody><tr><td class="code">research:read</td><td>Coverage, evidence excerpts, provenance, review status, entities, aggregates</td></tr><tr><td class="code">transcripts:read</td><td>Transcript search, windows, chunks, interval text</td></tr><tr><td class="code">campaigns:read *</td><td>Briefs and checks — upgrade opt-in only</td></tr><tr><td class="code">watchlists:read *</td><td>Saved searches and evaluation — upgrade opt-in only</td></tr><tr><td class="code">notes:read *</td><td>Research notes — upgrade opt-in only</td></tr></tbody></table></div><p><strong>Setup commands</strong></p><pre>python3 scripts/app_workspace.py upgrade --workspace &lt;existing-workspace&gt;\npython3 -m venv .venv-agent && . .venv-agent/bin/activate && pip install -r requirements-agent.txt\npython3 scripts/app_workspace.py agent-config --workspace &lt;existing-workspace&gt; --client codex --profile reader</pre><p><strong>Client snippet (non-secret)</strong></p><p class="meta">Enter the three real paths from your <span class="code">agent-config</span> output; the snippet is generated locally in this page and never leaves it.</p>' + select("agent-client", "Client", ["codex", "cursor", "claude-desktop"], "codex") + field("agent-command", "Interpreter (command)", "") + field("agent-script", "Reader script", "") + field("agent-workspace", "Workspace directory (absolute)", "") + '<p><button id="agent-snippet" type="button">Generate snippet</button> <button id="agent-copy" type="button">Copy snippet</button> <span id="agent-snippet-status" class="meta" role="status"></span></p><pre id="agent-snippet-out">Generate to preview.</pre><p><strong>Open a citation</strong></p><p class="meta">Paste an <span class="code">ew-local://…/evidence/&lt;id&gt;</span> citation or a bare evidence id to open it in the authenticated evidence view. Citations pin revisions; the browser shows current state.</p>' + field("agent-citation", "Citation or evidence id", "") + '<p><button id="agent-open" type="button">Open evidence</button> <span id="agent-cite-status" class="meta" role="status"></span></p>', "ds-agent-local")}${panel("Backup and offline restore", '<p>Download a consistent snapshot of the product workspace. Audio is not included.</p><a class="btn" href="/api/v1/backup">Download workspace backup</a><pre>python3 scripts/app_workspace.py backup --out .earwitness-backup.sqlite3\npython3 scripts/app_workspace.py restore --backup .earwitness-backup.sqlite3 --workspace .earwitness-restored</pre><p class="meta">Restore requires an unused workspace. Existing state is never replaced without --force; a recovery copy is retained. Stop the server before replacement.</p>', "ds-backup")}${panel("Agent connections (hosted)", '<p class="meta">Hosted OAuth connections only — no tokens are shown or stored here. Initial consent offers research and transcript access; watchlists, campaigns, and notes are separate choices. Retrieved content may reach your agent provider\u2019s remote models.</p><div id="hosted-conn-state"><p class="meta">Status: loading…</p></div>', "ds-agent-hosted")}${panel("Recent changes", `<ul>${audit.audit.map((a) => `<li>${esc(a.at)} · ${esc(a.action)} · ${esc(a.target_type)} ${esc(a.target_id)} · rev ${a.aggregate_rev}</li>`).join("")}</ul>`, "ds-changes")}${panel("Processing sources", '<div id="data-sources"><p class="meta">Loading…</p></div><p><a href="#/processing">Open the Processing center</a></p>', "ds-proc-sources")}${panel("Recent runs", '<div id="data-runs"><p class="meta">Loading…</p></div><p><a href="#/processing">Open the Processing center</a></p>', "ds-runs")}${panel(`Source versions and fingerprints · ${overview.import_state.length}`, `<div class="table-wrap"><table class="grid"><thead><tr><th>Source</th><th>Version</th><th>State</th><th>SHA-256</th><th></th></tr></thead><tbody>${overview.import_state.map((s) => `<tr><td>${esc(s.source_key)}</td><td>${s.version_no}</td><td>${s.is_active ? "Active" : "Historical"}</td><td class="code">${esc(s.sha256 || "unknown")}</td><td><button data-source="${s.id}">Inspect original</button></td></tr>`).join("")}</tbody></table></div><p class="meta">Jev supplied run: 431,559 input tokens; $0.01813 at its documented rate. Imported evidence does not establish accuracy. Missing show, date, and audio mappings remain unknown.</p>`, "ds-source-versions")}`;
  document.querySelectorAll("[data-jump]").forEach((b) => b.onclick = () => {
    const s = $(b.dataset.jump), h = s?.querySelector("h2"); if (!s || !h) return;
    h.tabIndex = -1;
    const still = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    s.scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" });
    h.focus({ preventScroll: true });
  });
  $("settings-form").dataset.revision = settings.settings.revision;
  $("seed").onclick = async () => { $("import-status").textContent = "Importing…"; try { const result = await post("/api/v1/imports/seed", {}); $("import-status").textContent = "Import complete. Reload Data to inspect updated versions."; $("import-report").textContent = JSON.stringify(result.import, null, 2); } catch (error) { $("import-status").textContent = `Rejected: ${error.message}`; } };
  $("reindex").onclick = async () => { try { await post("/api/v1/imports/reindex", {}); $("import-status").textContent = "Index rebuilt; reviews retained."; } catch (error) { $("import-status").textContent = error.message; } };
  wireForm("upload-form", async (values) => { const payload = JSON.parse(values["upload-json"]); if (!payload || Array.isArray(payload) || typeof payload !== "object") throw new Error("Transcript JSON must be an object."); const result = await post("/api/v1/imports/upload", { ...payload, origin_note: values["upload-origin"] }); const link = document.createElement("p"); link.innerHTML = `<a href="#/episode/${encodeURIComponent(result.import.episode_id)}">Open imported transcript ${esc(result.import.episode_id)}</a>`; $("upload-form").append(link); return result; });
  wireForm("settings-form", async (values, element) => { const out = await post("/api/v1/settings", { playback_speed: Number(values["settings-speed"]), page_density: values["settings-density"] }, element.dataset.revision); App.settings = out.settings; document.body.dataset.density = out.settings.page_density; return out; });
  document.querySelectorAll("[data-source]").forEach((button) => button.onclick = async () => { try { const result = await api(`/api/v1/sources/${button.dataset.source}`); modal("Original source version", `<pre>${esc(JSON.stringify(result.source, null, 2))}</pre>`); } catch (error) { modal("Source unavailable", `<p>${esc(error.message)}</p>`); } });
  try {
    const [sources, runs] = await Promise.all([
      api("/api/v1/processing/sources"), api("/api/v1/processing/runs?limit=5")]);
    $("data-sources").innerHTML = `<p>${sources.feed_sources.length} registered RSS feeds · ${sources.registered_inputs.length} shows with episodes.</p>`
      + (sources.feed_sources.slice(0, 5).map((s) => `<p class="code">${esc(s.url)}</p>`).join("") || '<p class="meta">No RSS feeds registered.</p>');
    $("data-runs").innerHTML = (runs.runs.map((r) =>
      `<p><span class="code">${esc(r.run_id)}</span> ${badge(r.status)} <span class="meta">${esc(Object.entries(r.stage_counts || {}).map(([k, v]) => `${k} ${v}`).join(", "))}</span></p>`).join("")
      || '<p class="meta">No runs yet. Estimates never create runs.</p>');
  } catch (error) {
    if ($("data-sources")) $("data-sources").innerHTML = `<p class="meta">Processing state unavailable: ${esc(error.message)}</p>`;
    if ($("data-runs")) $("data-runs").innerHTML = "";
  }
  wireAgentConnections();
  wireHostedConnections();
};

/* -- PSURF additive surfaces (407/540/604) : native views only, no service logic -- */
Views.creatives = async () => {
  setToolbar('<a class="btn" href="#/explore?tab=brands">Back to Explore</a>');
  // A failed load is not an empty collection: render() shows the error and Retry.
  const list = (await api("/api/v1/creatives?limit=50")).creatives;
  $("view").innerHTML = `<h1>Creatives</h1><p class="meta">Suggested memberships stay suggestions until confirmed. Confirming membership never copies ad, boundary, alignment, or campaign judgments.</p>`
    + panel("Open creative", form("creative-open", 0, field("creative-open-id", "Creative ID") + submit("Open creative"), false))
    + panel("Suggest memberships", form("creative-suggest", 0, field("creative-suggest-obs", "Observation ID") + submit("Suggest"), false) + `<div id="suggest-out"></div>`)
    + panel("Create creative", form("creative-new", 0, field("creative-new-obs", "Observation IDs (comma-separated)") + submit("Create creative")))
    + panel("Confirmed creatives (IDs, reviewed-only work)", `<div class="table-wrap"><table class="grid"><thead><tr><th>Creative</th><th>Status</th><th>Revision</th><th></th></tr></thead><tbody>${list.map((c) => `<tr><td class="code">${esc(c.id)}</td><td>${badge(c.status)}</td><td class="num">${c.revision}</td><td><a href="#/creative/${encodeURIComponent(c.id)}">Review</a></td></tr>`).join("") || '<tr><td colspan="4">No creatives yet.</td></tr>'}</tbody></table></div>`);
  $("creative-open").onsubmit = (e) => { e.preventDefault(); const id = $("creative-open-id").value.trim(); if (id) navigate("#/creative/" + encodeURIComponent(id)); };
  $("creative-suggest").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("suggest-out");
    out.innerHTML = '<p role="status">Suggesting…</p>';
    try {
      const res = await api(`/api/v1/creatives/suggest?observation_id=${encodeURIComponent($("creative-suggest-obs").value.trim())}`);
      out.innerHTML = `<p role="status">${res.suggestions.length} suggestion(s), config ${esc(res.config_digest)}.</p>` + (res.suggestions.map((s) => `<details><summary><span class="code">${esc(s.observation_id)}</span> · score ${s.score}</summary><pre>${esc(JSON.stringify({ score: s.score, shingle_overlap: s.shingle_overlap, commercial_diff: s.commercial_diff, observation_revision: s.observation_revision }, null, 2))}</pre></details>`).join("") || "<p>No suggestions.</p>");
      say("Suggestions ready; all are unconfirmed.");
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  wireForm("creative-new", async (v) => {
    const ids = v["creative-new-obs"].split(",").map((s) => s.trim()).filter(Boolean);
    const out = await post("/api/v1/creatives", { observation_ids: ids });
    const p = document.createElement("p"); p.innerHTML = `<a href="#/creative/${encodeURIComponent(out.creative_id)}">Open ${esc(out.creative_id)}</a>`;
    $("creative-new").append(p); return out;
  });
};

Views.creative = async ([raw]) => {
  const id = decodeURIComponent(raw || ""), encoded = encodeURIComponent(id), generation = App.generation;
  let detail;
  try { detail = await api(`/api/v1/creatives/${encoded}`); }
  catch (error) { $("view").innerHTML = `<h1>Creative</h1><p role="alert">${esc(error.message)}</p>`; return; }
  if (generation !== App.generation) return;
  const creative = detail.creative, members = detail.members || [], history = detail.history || [];
  setToolbar('<a class="btn" href="#/creatives">All creatives</a>');
  $("view").innerHTML = `<h1>Creative <span class="code">${esc(id)}</span></h1><p>${badge(creative.status)} <span class="meta">revision ${creative.revision}</span></p><p class="meta">${esc(detail.display?.confirm_note || "")}</p>`
    + panel("Memberships (suggested until confirmed)", `<div class="table-wrap"><table class="grid"><thead><tr><th>Observation</th><th>Method</th><th>Confirmed</th><th></th></tr></thead><tbody>${members.map((m) => `<tr><td class="code">${esc(m.observation_id)}</td><td>${esc(m.method)}</td><td>${m.confirmed ? "Confirmed" : "Suggested"}</td><td>${m.confirmed ? "" : `<button data-confirm="${esc(m.observation_id)}">Confirm</button>`}</td></tr>`).join("") || '<tr><td colspan="4">No members.</td></tr>'}</tbody></table></div><div id="creative-status" role="status" class="meta"></div>`)
    + panel("Reject pair (version-keyed)", form("creative-reject", creative.revision, field("reject-a", "Observation A", members[0]?.observation_id || "") + field("reject-b", "Observation B", members[1]?.observation_id || members[0]?.observation_id || "") + submit("Reject pair")))
    + panel("Suggest with offer/code diffs", form("creative-suggest-one", 0, field("suggest-obs", "Observation ID", members[0]?.observation_id || "") + submit("Suggest"), false) + `<div id="creative-suggest-out"></div>`)
    + panel("Split creative (reversible lineage)", form("creative-split", creative.revision, area("split-groups", "Groups, one observation ID per line; blank line separates groups") + submit("Split")))
    + panel("Merge creatives (reversible lineage)", form("creative-merge", 0, field("merge-ids", "Creative IDs (comma-separated)", id) + field("merge-revs", "Expected revisions JSON, e.g. {\"id\": rev}") + submit("Merge")))
    + panel("Lineage (reversible via audit)", `<ul>${history.map((h) => `<li>${esc(h.at)} · ${esc(h.actor)} · ${esc(h.action)} · rev ${h.aggregate_rev}<br><span class="meta code">${esc(JSON.stringify(h.detail).slice(0, 300))}</span></li>`).join("") || "<li>No lineage yet.</li>"}</ul>`);
  const status = $("creative-status");
  document.querySelectorAll("[data-confirm]").forEach((b) => b.onclick = async () => {
    status.textContent = "Confirming…";
    try {
      const out = await post(`/api/v1/creatives/${encoded}/confirm`, { observation_id: b.dataset.confirm }, $("creative-reject").dataset.revision);
      status.textContent = `Confirmed · revision ${out.revision}. No judgments copied.`;
      say("Membership confirmed.");
      render();
    } catch (error) {
      status.textContent = error.status === 412 ? `Conflict: ${error.message}. Draft retained; membership unchanged.` : error.message;
      say("Confirm failed; membership unchanged.");
    }
  });
  wireForm("creative-reject", async (v, el) => {
    const out = await post(`/api/v1/creatives/${encoded}/reject`, { observation_id_a: v["reject-a"].trim(), observation_id_b: v["reject-b"].trim() }, el.dataset.revision);
    status.textContent = `Rejected · revision ${out.revision}.`; render(); return out;
  });
  $("creative-suggest-one").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("creative-suggest-out"); out.innerHTML = '<p role="status">Suggesting…</p>';
    try {
      const res = await api(`/api/v1/creatives/suggest?observation_id=${encodeURIComponent($("suggest-obs").value.trim())}`);
      out.innerHTML = res.suggestions.map((s) => `<details><summary><span class="code">${esc(s.observation_id)}</span> · score ${s.score}</summary><pre>${esc(JSON.stringify({ commercial_diff: s.commercial_diff, shingle_overlap: s.shingle_overlap, observation_revision: s.observation_revision, confirmed: s.confirmed }, null, 2))}</pre></details>`).join("") || "<p>No suggestions.</p>";
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  wireForm("creative-split", async (v, el) => {
    const groups = v["split-groups"].split(/\n\s*\n/).map((g) => g.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean)).filter((g) => g.length);
    const out = await post("/api/v1/creatives/split", { creative_id: id, groups }, el.dataset.revision);
    status.textContent = `Split into ${out.children.length} groups. Lineage preserved.`;
    say("Creative split; lineage preserved."); return out;
  });
  wireForm("creative-merge", async (v) => {
    const ids = v["merge-ids"].split(",").map((s) => s.trim()).filter(Boolean);
    const out = await post("/api/v1/creatives/merge", { creative_ids: ids, expected_revisions: JSON.parse(v["merge-revs"] || "{}") });
    const p = document.createElement("p"); p.innerHTML = `<a href="#/creative/${encodeURIComponent(out.creative_id)}">Open merged ${esc(out.creative_id)}</a>`;
    $("creative-merge").append(p); say("Creatives merged; lineage preserved."); return out;
  });
};

const _psurfWatchBase = Views.watchlists;
Views.watchlists = async ([rawId]) => {
  await _psurfWatchBase([rawId]);
  const host = document.createElement("section");
  host.className = "panel";
  host.innerHTML = `<h2>Monitoring (no live schedules activated, no sends)</h2><p class="meta">Enable establishes a baseline of current matches; the historical corpus never floods. Backfill is a separate explicit action.</p>`
    + form("monitor-enable", 0, field("monitor-watch-id", "Watchlist ID", rawId === "new" ? "" : (rawId || "")) + field("monitor-rev", "Definition revision", "1", "number") + submit("Enable monitor"), false)
    + `<p><button id="monitor-disable" type="button">Disable monitor</button> <span id="monitor-status" class="meta" role="status"></span></p>`
    + form("monitor-backfill", 0, field("backfill-watch-id", "Watchlist ID", rawId === "new" ? "" : (rawId || "")) + field("backfill-kind", "Event kind", "new_observation") + field("backfill-identity", "Match identity") + `<label><input id="backfill-confirm" type="checkbox"> I understand backfill is explicit and separate</label>` + submit("Backfill"), false)
    + panel("Schedule freshness (last success vs last attempt, paused state)", `<label class="field" for="freshness-id"><span>Schedule ID (watchlist-N)</span><input id="freshness-id" value="watchlist-${esc(rawId === "new" ? "" : (rawId || ""))}"></label><button id="freshness-check" type="button">Check freshness</button><div id="freshness-out"></div>`)
    + panel("Digest preferences (per user)", form("monitor-prefs", 0, field("prefs-member", "Member", "owner") + select("prefs-digest", "Digest", ["daily", "immediate", "none"], "daily") + `<label><input id="prefs-immediate" type="checkbox"> Immediate delivery opt-in</label>` + submit("Save preferences"), false) + `<div id="prefs-out"></div>` + form("monitor-unsub", 0, field("unsub-member", "Member", "owner") + submit("Unsubscribe"), false));
  $("view").append(host);
  $("monitor-enable").onsubmit = async (e) => {
    e.preventDefault();
    const st = $("monitor-status"); st.textContent = "Enabling…";
    try {
      const out = await post(`/api/v1/monitoring/watchlists/${encodeURIComponent($("monitor-watch-id").value.trim())}/enable`, { current_matches: [], definition_revision: Number($("monitor-rev").value || 1) });
      st.textContent = `Enabled ${out.schedule_id}; baseline of ${out.baseline_count} identities. No flood.`;
      say("Monitor enabled with baseline.");
    } catch (error) { st.textContent = error.message; }
  };
  $("monitor-disable").onclick = async () => {
    const st = $("monitor-status"); st.textContent = "Disabling…";
    try {
      const out = await post(`/api/v1/monitoring/watchlists/${encodeURIComponent($("monitor-watch-id").value.trim())}/disable`, {});
      st.textContent = `Disabled ${out.schedule_id}.`; say("Monitor disabled.");
    } catch (error) { st.textContent = error.message; }
  };
  $("monitor-backfill").onsubmit = async (e) => {
    e.preventDefault();
    const st = $("monitor-backfill").querySelector('[role="status"]'); st.textContent = "Backfilling…";
    try {
      const out = await post(`/api/v1/monitoring/watchlists/${encodeURIComponent($("backfill-watch-id").value.trim())}/backfill`, { confirm: $("backfill-confirm").checked, current_matches: [{ kind: $("backfill-kind").value.trim() || "new_observation", identity: $("backfill-identity").value.trim() }], definition_revision: 1 });
      st.textContent = `Backfilled: ${out.created} created, ${out.duplicates} duplicates.`; say("Backfill complete.");
    } catch (error) { st.textContent = error.message; }
  };
  $("freshness-check").onclick = async () => {
    const out = $("freshness-out"); out.innerHTML = '<p role="status">Checking…</p>';
    try {
      const res = await api(`/api/v1/monitoring/schedules/${encodeURIComponent($("freshness-id").value.trim())}/status`);
      const s = res.schedule;
      out.innerHTML = `<dl class="defs"><dt>Next due</dt><dd class="code">${esc(s.next_due_at || "none")}</dd><dt>Last attempt</dt><dd class="code">${esc(s.last_attempt_at || "never")}</dd><dt>Last success</dt><dd class="code">${esc(s.last_success_at || "never")}</dd><dt>Paused</dt><dd>${esc(s.paused_reason || "no")}</dd><dt>Resampling</dt><dd>${esc(s.resampling || "disabled")}</dd></dl><details><summary>Last successful coverage</summary><pre>${esc(JSON.stringify(s.last_coverage || {}, null, 2))}</pre></details>`;
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  $("monitor-prefs").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("prefs-out"); out.innerHTML = '<p role="status">Saving…</p>';
    try {
      const res = await post("/api/v1/monitoring/preferences", { member: $("prefs-member").value.trim(), digest: $("prefs-digest").value, immediate: $("prefs-immediate").checked });
      out.innerHTML = `<p role="status">Saved: ${esc(res.digest)}${res.immediate ? " + immediate" : ""}.</p>`;
      const cur = await api(`/api/v1/monitoring/preferences?member=${encodeURIComponent($("prefs-member").value.trim())}`);
      out.innerHTML += `<pre>${esc(JSON.stringify(cur.preferences, null, 2))}</pre>`;
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  $("monitor-unsub").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await post("/api/v1/monitoring/preferences/unsubscribe", { member: $("unsub-member").value.trim(), unsubscribed: true });
      $("prefs-out").innerHTML = "<p role='status'>Unsubscribed; future delivery stops.</p>";
      say("Unsubscribed.");
    } catch (error) { $("prefs-out").innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
};

const _psurfCampaignBase = Views.campaign;
Views.campaign = async ([cid]) => {
  await _psurfCampaignBase([cid]);
  const host = document.createElement("section");
  host.className = "panel";
  host.innerHTML = `<h2>Expected placements (scope, count, requirements, provenance)</h2><div id="placement-list"><p class="meta">Loading…</p></div>`
    + form("placement-new", 0, select("placement-scope", "Scope", ["show_window", "episode"], "show_window") + field("placement-show", "Show ID", "") + field("placement-episode", "Exact episode ID (episode scope only)", "") + field("placement-from", "Date from (YYYY-MM-DD)", "", "text") + field("placement-to", "Date to (YYYY-MM-DD)", "", "text") + field("placement-tz", "IANA timezone", "UTC") + field("placement-count", "Expected count", "1", "number") + area("placement-reqs", "Requirements JSON (duration_s, position_s, read_type, message, offer, code, priority)", "{}") + area("placement-prov", "Provenance JSON (brief_ref or {\"synthetic\": true})", "{\"synthetic\": true}") + submit("Save expected placement"))
    + `<h3>Requirement-vs-observation evaluation (six-state vocabulary)</h3>`
    + form("placement-eval", 0, area("eval-occs", "Occurrences JSON (list of {id, correspondence, review_status, advertiser_confirmed, ...})", "[]") + area("eval-coverage", "Coverage JSON ({\"state\": \"complete\"})", "{\"state\": \"complete\"}") + submit("Evaluate"), false)
    + `<div id="eval-out"></div>`
    + `<h3>Manual assessment (separate from computed)</h3>`
    + form("placement-assess", 0, field("assess-placement", "Placement ID", "") + field("assess-author", "Author", "owner") + select("assess-verdict", "Verdict", ["supported", "not_supported", "inconclusive"], "inconclusive") + area("assess-reason", "Reason (required)", "") + submit("Save manual assessment"))
    + `<h3>Frozen evaluation history</h3><label class="field" for="history-placement"><span>Placement ID</span><input id="history-placement"></label><button id="history-check" type="button">Load history</button><div id="history-out"></div>`;
  $("view").append(host);
  const seenOutcome = {};
  const refresh = async () => {
    try {
      const res = await api(`/api/v1/campaigns/${encodeURIComponent(cid)}/placements`);
      $("placement-list").innerHTML = res.placements.map((p) => `<p class="meta">${esc(p.scope)} · count ${p.expected_count} · window ${esc(p.date_from || "open")} → ${esc(p.date_to || "open")} · latest outcome ${esc(seenOutcome[p.id] || "no frozen evaluation yet")}</p><details><summary><span class="code">${esc(p.id)}</span> · ${esc(p.scope)} · count ${p.expected_count} · ${p.requirements?.priority ? "priority " + p.requirements.priority : "default priority"}${p.provenance?.synthetic ? " · synthetic (not contractual)" : ""}</summary><pre>${esc(JSON.stringify({ scope: p.scope, show_id: p.show_id, episode_id: p.episode_id, date_from: p.date_from, date_to: p.date_to, timezone: p.timezone, expected_count: p.expected_count, requirements: p.requirements, provenance: p.provenance }, null, 2))}</pre></details>`).join("") || "<p>No expected placements yet.</p>";
    } catch (error) { $("placement-list").innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  await refresh();
  wireForm("placement-new", async (v) => {
    const out = await post(`/api/v1/campaigns/${encodeURIComponent(cid)}/placements`, { scope: v["placement-scope"], show_id: v["placement-show"] || "", episode_id: v["placement-episode"] || null, date_from: v["placement-from"] || null, date_to: v["placement-to"] || null, timezone: v["placement-tz"] || "UTC", expected_count: Number(v["placement-count"] || 1), requirements: JSON.parse(v["placement-reqs"] || "{}"), provenance: JSON.parse(v["placement-prov"] || "{}") });
    await refresh(); say("Expected placement saved."); return out;
  });
  $("placement-eval").onsubmit = async (e) => {
    e.preventDefault();
    const out = $("eval-out"); out.innerHTML = '<p role="status">Evaluating…</p>';
    try {
      const res = await post(`/api/v1/campaigns/${encodeURIComponent(cid)}/evaluate`, { occurrences: JSON.parse($("eval-occs").value || "[]"), coverage: JSON.parse($("eval-coverage").value || "{}"), now_iso: new Date().toISOString() });
      out.innerHTML = Object.values(res.evaluations).map((ev) => `<details open><summary>${badge(ev.outcome)} <span class="code">${esc(ev.placement_id)}</span> · eligible ${ev.eligible_ids.length}</summary><pre>${esc(JSON.stringify({ outcome: ev.outcome, reasons: ev.reasons, eligible_ids: ev.eligible_ids, allocated_elsewhere: ev.allocated_elsewhere }, null, 2))}</pre></details>`).join("");
      say("Evaluation ready with six-state outcomes.");
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
  wireForm("placement-assess", async (v) => {
    const out = await post(`/api/v1/campaigns/${encodeURIComponent(cid)}/placements/${encodeURIComponent(v["assess-placement"].trim())}/assess`, { author: v["assess-author"].trim(), verdict: v["assess-verdict"], reason: v["assess-reason"], evidence: [] });
    say("Manual assessment saved separately."); return out;
  });
  $("history-check").onclick = async () => {
    const out = $("history-out"); out.innerHTML = '<p role="status">Loading…</p>';
    try {
      const pid = $("history-placement").value.trim();
      const res = await api(`/api/v1/campaigns/${encodeURIComponent(cid)}/placements/${encodeURIComponent(pid)}/history`);
      out.innerHTML = `<ul>${res.history.map((h) => `<li>${esc(h.created_at)} · ${badge(h.outcome)} · campaign rev ${h.campaign_revision}<br><span class="meta code">${esc(h.reasons)}</span></li>`).join("") || "<li>No frozen evaluations.</li>"}</ul>`;
      seenOutcome[pid] = res.history.length ? res.history[res.history.length - 1].outcome : "no frozen evaluation yet";
      await refresh();
    } catch (error) { out.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  };
};
// Ad review (admin review, 2026-10-11): one read at a time, most impact
// first. Each action is one key and saves at once, then moves on. Actions
// that change other reads (an alias for every read heard one way: Shift+A,
// or Shift+Enter on a typed brand; a generic word) show the count first
// and need the key again; plain Enter on a typed brand saves this read
// only (decision 14); on a known brand the page says
// so. Confirm and pick send the brand on screen; type, alias, self-promo
// and generic send the heard-as spelling on screen. A read that changed
// meanwhile is refused (READ_CHANGED) and shown again. After a rule the
// queue and every prefetched read reload. Rules can be removed, count
// first (decision 12); actions wait while a removal runs.
const AdReview = { items: [], index: 0, done: new Set(), filter: { show: "", brand: "", reason: "" }, cache: new Map(), total: 0, facets: null, keyGeneration: 0, pending: null, lastMessage: "", lastSaved: null, changed: null, reloadRules: null, removing: false, acting: false, front: null };
// Moves past the read. A save (not a skip) keeps the read's id so the result
// line can offer "Correct this read"; a save the server labelled (saved =
// { label, labelId, listed }) can also be undone from there.
function adReviewAdvance(id, message, saved) {
  AdReview.lastMessage = message;
  AdReview.lastSaved = saved ? { id, message, ...(typeof saved === "object" ? saved : {}) } : null;
  AdReview.changed = null;
  AdReview.done.add(id);
  AdReview.index += 1;
}
// Turns off the given buttons while a save runs. The returned release()
// turns back on only the ones that were on: a button off for a reason (no
// brand to confirm, no spoken name) stays off after a failed save.
function adReviewHold(nodes) {
  const held = [...nodes].filter((b) => !b.disabled);
  held.forEach((b) => { b.disabled = true; });
  return () => held.forEach((b) => { b.disabled = false; });
}
// One read-changing action at a time on Ad review (AU-02, AU-03): the
// last-save Undo, a suggestion's Confirm / Not this / Undo, a rule Remove
// and the review save. While one runs the others are disabled and refuse;
// each one's own outcome draws what it changed. Returns the end function
// (call it once the answer is in), or null when another action runs.
// Play controls are not actions: dead audio keeps them off (R6-02).
const AR_ACTIONS = ".ar-actions button, .ar-candidates button, .ar-remove, .ar-undo, #ar-suggest button[data-sg], #ar-filters button, .ar-rules-reload, .ar-promo-actions button";
const adReviewPlayControl = (b) => b.dataset?.ar === "play" || b.dataset?.ar === "replay" || !!b.classList?.contains?.("ar-play");
function adReviewBegin() {
  if (AdReview.acting) return null;
  AdReview.acting = true;
  const unhold = adReviewHold([...document.querySelectorAll(AR_ACTIONS)].filter((b) => !adReviewPlayControl(b)));
  let ended = false;
  return () => {
    if (ended) return;
    ended = true; AdReview.acting = false; unhold();
    // Panels drawn while it ran were drawn disabled: enable them (R6-01).
    // A suggestion action draws its own panel (its row is still busy here).
    // A list load still running draws it itself (R8-01).
    if (AR_SG.busy === null && !AR_SG.loading && $("ar-suggest")?.querySelector?.("[data-acting]")) drawAdReviewSuggest();
    document.querySelectorAll(".ar-remove[data-acting]").forEach((b) => { b.disabled = false; delete b.dataset.acting; });
  };
}
// A read that changed while the operator looked (READ_CHANGED): what the
// page showed and what the server has now, from the two loaded reads only.
function adReviewSnapshot(item) {
  const brand = item?.read?.brand || {};
  return { brand: brand.display || brand.key || null, confirmed: !!brand.confirmed, heard: item?.raw_brand || null,
    candidates: (item?.read?.candidates || []).map((c) => c && c.brand).filter(Boolean) };
}
function adReviewDiffRows(before, after) {
  const brandText = (s) => `${s.brand || "Not named"}${s.confirmed ? " (confirmed)" : ""}`;
  const list = (c) => c.length ? c.join(", ") : "none";
  return [
    ["Brand", brandText(before), brandText(after)],
    ["Spoken name", before.heard ? `“${before.heard}”` : "none", after.heard ? `“${after.heard}”` : "none"],
    ["Candidates", list(before.candidates), list(after.candidates)],
  ].filter(([, a, b]) => a !== b);
}
// While a changed read waits for "Review changed read", these keys save nothing.
const AR_SAVE_KEYS = new Set(["confirm", "type", "alias", "not_ad", "self_promo", "generic"]);
const AR_CHANGED_WORDS = "This read changed — press Review changed read first.";
function adReviewRuleErrorMarkup(message, ruleId, { answered = true } = {}) {
  if (!answered) return `<li class="ar-rule-error" role="alert"><span>No answer from the server. This rule may or may not be removed, and this list may be out of date.</span><button type="button" class="ar-rules-reload">Reload rules</button></li>`;
  return `<li class="ar-rule-error" role="alert"><span>Not removed: ${esc(message)}. The rule still applies and no read changed.</span><button type="button" class="ar-remove ar-retry" data-rule="${esc(ruleId)}">Retry</button></li>`;
}
// The count-first prompt of a rule removal (decision 12, F-13).
function adReviewRemoveAskMarkup(reads, changed) {
  return `<li class="ar-rule-ask notice" role="alert"><span>${changed ? "The count changed. " : ""}This changes ${reads} read${reads === 1 ? "" : "s"}. Press Remove again to remove the rule; Esc cancels.</span></li>`;
}
// Actions about the read's heard-as spelling carry the spelling the page
// shows, so a stale tab never acts on one nobody saw (server: READ_CHANGED).
const HEARD_ACTIONS = new Set(["type", "alias", "self_promo", "generic"]);
function adReviewBody(body, item) {
  return HEARD_ACTIONS.has(body.action) ? { ...body, heard: item.raw_brand || "" } : body;
}
// The count-first prompt: which key saves, and what it changes.
function adReviewAskWords(name, details, brand) {
  const reads = `${details.reads} read${details.reads === 1 ? "" : "s"}`;
  const known = details.known_brand ? ` “${details.word || details.raw}” is a known brand.` : "";
  if (name === "self_promo") return `This changes ${reads}. Press Enter again to save “${details.brand}” as self-promotion on ${details.exact ? "this show" : `shows with “${details.network}”`}; Esc cancels.`;
  if (name === "generic") return `This changes ${reads}.${known} Press Shift+G again to make “${details.word}” a generic word (never a brand); Esc cancels.`;
  const what = `show every read heard as “${details.raw}” as ${brand}`;
  return `This changes ${reads}.${known} Press Shift+A again (or Shift+Enter in the brand field) to ${what}; Esc cancels.`;
}
const clockT = (s) => {
  const v = Math.round(Number(s));
  if (!Number.isFinite(v) || v < 0) return "unknown";
  const h = Math.floor(v / 3600), m = Math.floor(v / 60) % 60, sec = String(v % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
};
const REASON_WORDS = { needs_review: "Needs review", "report:wrong_brand": "Reported: wrong brand", "report:not_an_ad": "Reported: not an ad", "report:wrong_time": "Reported: wrong time", "report:other": "Reported: other" };
const REPORT_WORDS = { wrong_brand: "Wrong brand", not_an_ad: "Not an ad", wrong_time: "Wrong time", other: "Other" };
const AR_REASONS = [["", "All reasons"], ["needs_review", "Needs review"], ["report", "Member reports"], ["wrong_brand", "Reported: wrong brand"], ["not_an_ad", "Reported: not an ad"], ["wrong_time", "Reported: wrong time"], ["other", "Reported: other"]];
const AR_PLAY_ICON = '<svg class="ar-icon-play" width="16" height="16" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.5v9l7.5-4.5z" fill="currentColor"/></svg><svg class="ar-icon-pause" width="16" height="16" viewBox="0 0 12 12" aria-hidden="true"><rect x="2.5" y="1.5" width="2.5" height="9" fill="currentColor"/><rect x="7" y="1.5" width="2.5" height="9" fill="currentColor"/></svg>';
// Which review action a key press asks for; null inside editable controls
// (DESIGN.md: shortcuts never fire while typing) or with Ctrl, Alt or Cmd.
// The rule keys need Shift (Shift+A, Shift+G): plain A means "ad" on Review
// evidence, so here it does nothing.
function adReviewKey(event) {
  if (!event || event.metaKey || event.ctrlKey || event.altKey) return null;
  const target = event.target;
  if (target && typeof target.closest === "function" && target.closest("input, textarea, select, [contenteditable]")) return null;
  const key = String(event.key || "").toLowerCase();
  if (/^[1-5]$/.test(key)) return `pick:${key}`;
  const rule = { a: "alias", g: "generic" }[key];
  if (rule) return event.shiftKey ? rule : null;
  // "Review changed read" needs its button or Shift+Enter: a stray Enter (the
  // habit the count prompts teach) must not dismiss the old -> new block.
  if (key === "enter") return event.shiftKey ? "seen" : null;
  return { " ": "play", r: "replay", c: "confirm", t: "type", n: "not_ad", p: "self_promo", s: "skip", arrowright: "skip", escape: "cancel" }[key] || null;
}
// After a save: a rule that changed other reads makes every prefetched
// read and the queue stale, so both reload. Returns whether it did.
function adReviewAfterSave(out) {
  if (!out || !out.corpus_changed) return false;
  AdReview.cache.clear(); AdReview.items = []; AdReview.index = 0;
  return true;
}
// What the page says about the audio before playing from the read start.
function adReviewAudioNote(duration, startAt) {
  if (Number.isFinite(duration) && startAt >= duration)
    return `This stream ends at ${clockT(duration)}, before the read starts: its ads changed since transcription. Judge the read on the transcript.`;
  if (!Number.isFinite(duration))
    return "This stream's length is unknown, so it may not reach the read. If it does not play from the read start, judge on the transcript.";
  return null;
}
async function adReviewLoad() {
  const out = await api(`/api/v1/admin/review-queue?${queryString({ ...AdReview.filter, limit: 500 })}`);
  AdReview.items = out.items.filter((i) => !AdReview.done.has(i.evidence_id));
  // An undone read comes back first (its fresh queue entry when it has one).
  const front = AdReview.front;
  if (front) AdReview.items = [AdReview.items.find((i) => i.evidence_id === front.evidence_id) || front, ...AdReview.items.filter((i) => i.evidence_id !== front.evidence_id)];
  AdReview.front = null;
  AdReview.total = out.total; AdReview.facets = out.facets; AdReview.index = 0; AdReview.cache.clear();
}
function adReviewDetail(id) {
  if (!AdReview.cache.has(id)) AdReview.cache.set(id, api(`/api/v1/admin/review-queue/${encodeURIComponent(id)}`).then((r) => r.item).catch((e) => { AdReview.cache.delete(id); throw e; }));
  return AdReview.cache.get(id);
}
function adReviewFilters() {
  const f = AdReview.filter, facets = AdReview.facets || { shows: [], reasons: [] };
  const shows = [["", "All shows"], ...facets.shows.map((s) => [s.value, `${s.value || "Unknown show"} (${s.count})`])];
  return `<form id="ar-filters" class="filters ar-filters" data-draft="off">${select("ar-show", "Show", shows, f.show)}<label class="field" for="ar-brand-filter"><span>Brand</span><input id="ar-brand-filter" type="text" value="${esc(f.brand)}" placeholder="any brand"></label>${select("ar-reason", "Reason", AR_REASONS, f.reason)}<button type="submit">Filter</button><p class="ar-order">Order: member reports first, then the namer's unchecked reads by how many reads carry the brand and the show's chart rank.</p></form>`;
}
// The head's queue line, from the queue the server returned (nothing else).
function adReviewQueueLine(position) {
  const n = AdReview.items.length;
  if (!n) return "Queue empty";
  const reports = AdReview.items.filter((i) => i.reports).length;
  return `Read ${position} of ${n}${AdReview.total > n ? ` · ${AdReview.total} waiting in all` : " waiting"}${reports ? ` · ${reports} member report${reports === 1 ? "" : "s"}` : ""}`;
}
const adReviewHead = (line) => `<div class="ar-head"><h1>Ad review</h1><p class="ar-queue" id="ar-position" role="status">${esc(line)}</p></div>`;
// The way back after a save: Review evidence can clear the judgment and
// change the brand. That link is not an undo (the labelled example stays),
// so it is never labelled Undo. Beside it, Undo (decision 11) reverses the
// save itself, for the last save of this session only and only while the
// server has not refused it. Shown only beside the message of that save.
function adReviewFixLink() {
  const saved = AdReview.lastSaved && AdReview.lastSaved.message === AdReview.lastMessage ? AdReview.lastSaved : null;
  const undo = saved && saved.labelId && saved.undo !== false ? '<button type="button" class="ar-undo">Undo</button>' : "";
  return saved ? `<span class="ar-last-fix">${undo}<a href="#/review/${encodeURIComponent(saved.id)}">Correct this read</a></span>` : "";
}
// After the server undid a save: say what was undone and bring that read
// back as the next one to review (the queue reloads when rules changed
// other reads).
function adReviewUndone(saved, out, lead = "Undone") {
  AdReview.lastMessage = `${lead}: ${saved.label || "the last save"}. ${out.message || ""}`.trim();
  AdReview.lastSaved = null; AdReview.changed = null; AdReview.pending = null;
  AdReview.done.delete(saved.id); AdReview.cache.delete(saved.id);
  const entry = saved.listed || { evidence_id: saved.id, reasons: [] };
  if (out.corpus_changed) { AdReview.cache.clear(); AdReview.items = []; AdReview.index = 0; AdReview.front = entry; return; }
  AdReview.items = AdReview.items.filter((i) => i.evidence_id !== saved.id);
  AdReview.index = Math.min(AdReview.index, AdReview.items.length);
  AdReview.items.splice(AdReview.index, 0, entry);
}
// Undo the last save. Saves wait while it runs (busy); a read changed since
// (READ_CHANGED) keeps only "Correct this read". "Already undone" means an
// earlier Undo committed but its answer was lost (D-12): it is done, and
// the queue reloads with that read first (what its rules changed is not
// known here).
async function adReviewUndo(button, busy) {
  const saved = AdReview.lastSaved;
  if (!saved || !saved.labelId || busy.on || AdReview.removing) return;
  const end = adReviewBegin();
  if (!end) return;
  busy.on = true; button.disabled = true;
  const status = (text) => { if ($("ar-status")) $("ar-status").textContent = text; };
  status("Undoing the last save…");
  try {
    const out = await post(`/api/v1/admin/review-queue/${encodeURIComponent(saved.id)}/undo`, { label_id: saved.labelId });
    end();
    adReviewUndone(saved, out);
  } catch (error) {
    end();
    busy.on = false;
    if (error.status === 400 && /already undone/.test(error.message || "")) adReviewUndone(saved, { corpus_changed: true }, "Already undone");
    else if (error.code !== "READ_CHANGED") {
      button.disabled = false;
      // The empty-queue page has no status line: the reason goes in a dialog (R5-03).
      const text = `Not undone: ${error.message}`;
      if ($("ar-status")) status(text); else modal("Not undone", `<p>${esc(text)}</p>`);
      return;
    }
    else {
      AdReview.lastMessage = `Not undone: ${error.message} Use Correct this read to change it on Review evidence.`;
      AdReview.lastSaved = { ...saved, message: AdReview.lastMessage, undo: false };
    }
  }
  busy.on = false;
  await render();
}
function wireAdReviewUndo(busy = { on: false }) {
  document.querySelectorAll(".ar-undo").forEach((b) => { b.onclick = () => adReviewUndo(b, busy); });
}
function adReviewEmptyMarkup() {
  const f = AdReview.filter;
  const reason = AR_REASONS.find(([value]) => value && value === f.reason);
  const scope = [f.show || "all shows", f.brand ? `brand “${f.brand}”` : "all brands", reason ? reason[1] : "all reasons"].join(", ");
  return `<section class="ar-box ar-empty"><h2 class="ar-empty-title">Nothing waits for review${Object.values(f).some(Boolean) ? " with these filters" : ""}.</h2>`
    + `<p>New member reports and unchecked names appear here. Filters: ${esc(scope)}.</p><p class="meta" role="status">${esc(AdReview.lastMessage || "")}</p>${adReviewFixLink()}</section>`;
}
function adReviewItemMarkup(item) {
  const read = item.read || {}, brand = read.brand || {};
  const candidates = (read.candidates || []).filter((c) => c && typeof c.brand === "string" && c.brand.trim()).slice(0, 5);
  const media = item.media;
  const listed = AdReview.items[AdReview.index]?.evidence_id === item.evidence_id ? AdReview.items[AdReview.index] : null;
  const reasons = (listed?.reasons || []).map((r) => `<span class="badge ${r === "needs_review" ? "unsure" : "report"}">${esc(REASON_WORDS[r] || r)}</span>`).join(" ");
  const lines = (item.transcript || []).map((l) => `<li class="${l.in_read ? "in-read" : ""}"><time>${clockT(l.start)}</time><span>${esc(l.text)}</span></li>`).join("") || '<li><span class="meta">No transcript lines for this read.</span></li>';
  const raw = item.raw_brand || "";
  const [from, to] = item.span || [];
  const length = Number(to) - Number(from);
  const reports = (item.reports || []).map((r) => `<div class="ar-report"><b>Member report · ${esc(REPORT_WORDS[r.reason] || r.reason)}</b><span>${r.note ? `<q>${esc(r.note)}</q> · ` : ""}${esc(String(r.created_at || "").replace("T", " ").replace(/:\d\dZ$/, " UTC"))}</span></div>`).join("");
  const player = media
    ? `<div class="ar-player"><button type="button" class="ar-play" data-ar="play" aria-label="Play, key Space">${AR_PLAY_ICON}</button><span class="ar-clock" id="ar-clock">${clockT(from)}</span><span class="ar-range">${clockT(from)} → ${clockT(to)}</span><span class="ar-spacer"></span><button type="button" data-ar="replay">Replay <kbd>R</kbd></button></div>`
      + (length > 0 ? `<progress id="ar-progress" class="ar-progress" max="${Number(length.toFixed(2))}" value="0" aria-label="Position in the read"></progress>` : "")
      + `<p class="meta ar-audio-status" id="ar-audio-status">${esc(media.label || "")}${media.kind === "remote_full" ? " · live stream: the ad heard may differ from the transcript" : ""} · Space play/pause · R replay from the read start</p>`
      + `<details class="ar-native"><summary>Audio controls</summary><audio id="ar-audio" controls preload="none"></audio></details>`
    : `<div class="ar-player"><span class="ar-clock">${clockT(from)}</span><span class="ar-range">${clockT(from)} → ${clockT(to)}</span></div><p class="meta">No audio for this episode. Judge the read on the transcript.</p>`;
  // Facts the operator API gives; a fact it does not give is not shown.
  const facts = [
    ["Guessed name (operators only)", brand.display ? esc(brandLabel(brand)) : "Not named", "", brand.confirmed ? badge("confirmed") : ""],
    raw ? ["Spoken name", `“${esc(raw)}”`, "ar-mono"] : null,
    listed?.brand && listed.brand_frequency ? ["Reads carrying this brand", String(listed.brand_frequency)] : null,
    read.confidence ? ["Namer's confidence", `${esc(read.confidence)}${Number.isFinite(read.confidence_score) ? ` (${read.confidence_score.toFixed(2)})` : ""}`] : null,
  ].filter(Boolean).map(([label, value, cls, extra]) => `<div class="ar-fact"><span class="ar-fact-label">${label}</span><strong${cls ? ` class="${cls}"` : ""}>${value}</strong>${extra || ""}</div>`).join("");
  const changed = AdReview.changed && AdReview.changed.id === item.evidence_id && AdReview.changed.after ? AdReview.changed : null;
  const off = changed ? " disabled" : "";
  const diff = changed ? adReviewDiffRows(changed.before, changed.after) : [];
  const changedBlock = changed ? `<div class="notice ar-changed" role="alert"><p><strong>This read changed while you were looking. Nothing was saved.</strong></p>`
    + (diff.length ? `<dl class="ar-diff">${diff.map(([what, a, b]) => `<dt>${what}</dt><dd>${esc(a)} → ${esc(b)}</dd>`).join("")}</dl>` : '<p class="meta">The brand, spoken name and candidates look the same; something else on the read changed. Check it again.</p>')
    + `<p class="meta">Press Review changed read (Shift+Enter) to act on it; until then the save keys do nothing.</p></div>` : "";
  const why = (c) => [c.brand === brand.key ? "current guess" : "", c.score != null ? `score ${Number(c.score).toFixed(2)}` : "", ...(Array.isArray(c.evidence) ? c.evidence.slice(0, 2) : [])].filter(Boolean).join(" · ");
  return `<div class="ar-grid"><section class="ar-box ar-item ar-read" aria-label="Read under review" data-brand="${esc(brand.key || "")}" tabindex="-1">${reports}`
    + `<div class="ar-body">${changedBlock}<p class="ar-where"><span>${esc(item.show_title || "Unknown show")} · ${esc(item.ep_title || item.episode_id)} · ${esc(item.pub_date || "date unknown")}</span> ${reasons}</p>`
    + player
    + `<ol class="ar-lines">${lines}</ol>`
    + `<div class="ar-facts">${facts}</div></div></section>`
    + `<section class="ar-decide" aria-label="Decide"><div class="ar-box ar-brand"><h2>Suggested names</h2>`
    + (candidates.length ? `<ol class="ar-candidates">${candidates.map((c, i) => `<li><button type="button" class="ar-cand${i === 0 ? " is-top" : ""}" data-ar-pick="${i + 1}" data-brand="${esc(c.brand)}"${off}><kbd>${i + 1}</kbd><span class="ar-cand-text"><b>${esc(c.brand)}</b>${why(c) ? `<span class="ar-cand-why">${esc(why(c))}</span>` : ""}</span></button></li>`).join("")}</ol>` : '<p class="meta">The namer listed no candidates.</p>')
    + `<form id="ar-type" class="ar-type" data-draft="off"><label class="field" for="ar-brand"><span>Type a brand <kbd>T</kbd> — keys are off while typing</span><input id="ar-brand" type="text" list="ar-brands" autocomplete="off" placeholder="Known brand or a new one"></label><datalist id="ar-brands"></datalist><p class="meta">Enter: this read only${raw ? ` · Shift+Enter: every read heard as “${esc(raw)}” (shows the count first)` : ""} · Esc: back to keys</p></form></div>`
    + `<form id="ar-promo" class="ar-box ar-promo" data-draft="off" hidden aria-labelledby="ar-promo-title"><h2 id="ar-promo-title">Self-promotion <kbd>P</kbd></h2><label class="field" for="ar-network"><span>Self-promotion on this show. To cover a network, type its name: every show whose title has those words.</span><input id="ar-network" type="text" value="${esc(item.show_title || "")}"></label><div class="ar-promo-actions"><button type="submit" class="primary">Save as self-promotion</button><button type="button" id="ar-promo-cancel">Cancel <kbd>Esc</kbd></button></div></form>`
    + `<div id="ar-suggest"></div><div id="ar-rules"></div></section></div>`
    + `<div class="ar-dock"><p class="ar-last" id="ar-status" role="status" aria-live="polite">${esc(AdReview.lastMessage || "")}</p>`
    + adReviewFixLink()
    + `<div id="ar-confirm" class="notice ar-confirm" role="alert" hidden></div>`
    + `<div class="ar-actions" role="group" aria-label="Review actions">`
    // This read: one key saves this read and moves on. Rules: change other
    // reads too, and always show the count first.
    + `<div class="ar-act-group" role="group" aria-labelledby="ar-act-read"><span class="ar-act-label" id="ar-act-read">This read</span>`
    + (changed ? `<button type="button" class="primary" data-ar="seen">Review changed read <kbd>Shift+Enter</kbd></button>` : "")
    + `<button type="button"${changed ? "" : ' class="primary"'} data-ar="confirm" ${brand.key && !changed ? "" : "disabled"}>Confirm ${brand.display ? esc(brand.display) : "shown brand"} <kbd>C</kbd></button>`
    + `<button type="button" data-ar="type"${off}>Type a brand <kbd>T</kbd></button>`
    + `<button type="button" data-ar="not_ad"${off}>Not an ad <kbd>N</kbd></button>`
    + `<button type="button" data-ar="skip">Skip <kbd>S / →</kbd></button>`
    + (media ? `<button type="button" data-ar="play">Play / pause <kbd>Space</kbd></button>` : "")
    + `</div><div class="ar-act-group ar-act-rules" role="group" aria-labelledby="ar-act-rules"><span class="ar-act-label" id="ar-act-rules">Rules · change other reads, count shown first</span>`
    + `<button type="button" data-ar="alias" ${raw && !changed ? "" : "disabled"}>${raw ? `Rename every “${esc(raw)}” read` : "Rename every read"} <kbd>Shift+A</kbd></button>`
    + `<button type="button" data-ar="generic" ${raw && !changed ? "" : "disabled"}>Generic word${raw ? ` “${esc(raw)}”` : ""} <kbd>Shift+G</kbd></button>`
    + `<button type="button" data-ar="self_promo" ${(brand.key || raw) && !changed ? "" : "disabled"}>Self-promotion <kbd>P</kbd></button>`
    + `<button type="button" data-ar="cancel">Cancel <kbd>Esc</kbd></button></div></div>`
    + `<p class="meta ar-foot">Keys do nothing while a text field has focus. This read: saves at once. Rules: Shift+A (Shift+Enter in the brand field), Shift+G and P show the count first; press the same key again (Enter for P) to save. Remove shows the count first; press it again to remove. A rule applies to every read now and on every load.</p></div>`;
}
function adReviewRuleWords(out) {
  const parts = out.rules.filter((r) => !r.replaced).map((r) => r.kind === "alias" ? `every “${r.raw}” read is now ${r.brand}`
    : r.kind === "stoplist" ? `“${r.word}” is never a brand`
    : r.kind === "self_promo" ? `${r.brand} is self-promotion on ${r.exact ? "this show" : `shows with “${r.network}”`}`
    : r.kind === "brand" ? `${r.brand} added to the brand list`
    : r.kind === "not_ad" ? "kept as not an ad" : r.kind);
  return parts.length ? ` Rule: ${parts.join("; ")}${out.reads ? ` (${out.reads} read${out.reads === 1 ? "" : "s"} changed)` : ""}.` : "";
}
function adReviewErrorPanel(message, skippable) {
  return `<section class="ar-box ar-item ar-error"><p role="alert">${esc(message)}</p>${skippable ? '<div class="ar-actions"><button type="button" data-ar="skip">Skip this read <kbd>S</kbd></button><button type="button" data-ar="retry">Retry</button></div>' : ""}<p class="meta" id="ar-status" role="status"></p></section>`;
}
Views["ad-review"] = async () => {
  const generation = App.generation;
  const view = $("view");
  try {
    if (!AdReview.items.length || AdReview.index >= AdReview.items.length) await adReviewLoad();
  } catch (error) {
    view.innerHTML = adReviewHead("Queue not loaded") + adReviewErrorPanel(error.code === "UPGRADE_REQUIRED" ? error.message : `The review queue could not load: ${error.message}`, false);
    return;
  }
  const current = AdReview.items[AdReview.index];
  const head = adReviewHead(adReviewQueueLine(AdReview.index + 1));
  const rulesSlot = `<div id="ar-suggest" class="ar-after"></div><div id="ar-rules" class="ar-after"></div>`;
  // Resolves true when the rules list was drawn, false when it could not load.
  const loadRules = (opts) => api("/api/v1/admin/rules").then((r) => { if (generation === App.generation && $("ar-rules")) { $("ar-rules").innerHTML = adReviewRulesMarkup(r, opts); wireAdReviewRules(); } return true; }).catch((error) => {
    if (generation === App.generation && $("ar-rules")) {
      $("ar-rules").innerHTML = adReviewRulesErrorMarkup(error.message);
      $("ar-rules-retry").onclick = () => { $("ar-rules-retry").disabled = true; loadRules(opts); };
    }
    return false;
  });
  AdReview.reloadRules = loadRules;
  if (!current) {
    view.innerHTML = head + adReviewFilters() + adReviewEmptyMarkup() + rulesSlot;
    wireAdReviewFilters(); wireAdReviewUndo(); loadRules(); loadAdReviewSuggest();
    return;
  }
  const keyGeneration = ++AdReview.keyGeneration;
  const advance = (message, saved = false) => { adReviewAdvance(current.evidence_id, message, saved); render(); };
  let item;
  try { item = await adReviewDetail(current.evidence_id); }
  catch (error) {
    if (generation !== App.generation) return;
    view.innerHTML = head + adReviewFilters() + adReviewErrorPanel(`This read could not load: ${error.message}`, true) + rulesSlot;
    wireAdReviewFilters(); loadRules(); loadAdReviewSuggest();
    view.querySelector('[data-ar="skip"]').onclick = () => advance("Skipped a read that could not load.");
    view.querySelector('[data-ar="retry"]').onclick = () => render();
    const onKey = (event) => {
      if (keyGeneration !== AdReview.keyGeneration || !$("ar-status")) { document.removeEventListener("keydown", onKey); return; }
      if (adReviewKey(event) === "skip" && !AdReview.acting) { event.preventDefault(); document.removeEventListener("keydown", onKey); advance("Skipped a read that could not load."); }
    };
    document.addEventListener("keydown", onKey);
    return;
  }
  if (generation !== App.generation) return;
  if (AdReview.changed && AdReview.changed.id !== current.evidence_id) AdReview.changed = null;
  if (AdReview.changed) AdReview.changed.after = adReviewSnapshot(item);
  const next = AdReview.items[AdReview.index + 1];
  if (next) adReviewDetail(next.evidence_id).catch(() => {});
  view.innerHTML = head + adReviewFilters() + adReviewItemMarkup(item);
  wireAdReviewFilters(); loadRules(); loadAdReviewSuggest();
  const transcript = document.querySelector?.(".ar-lines"), firstRead = transcript?.querySelector(".in-read");
  if (transcript && firstRead) transcript.scrollTop += firstRead.getBoundingClientRect().top - transcript.getBoundingClientRect().top - 24;
  const player = $("ar-audio");
  const AR_AUDIO_DEAD = "Audio failed to load. Judge the read on the transcript.";
  const disablePlayButtons = () => view.querySelectorAll('.ar-play, [data-ar="replay"], [data-ar="play"]').forEach((b) => { b.disabled = true; b.title = AR_AUDIO_DEAD; });
  const startAt = Math.max(0, (item.span[0] ?? 0) - ((item.media || {}).source_offset_s || 0));
  const audioStatus = (text) => { if ($("ar-audio-status")) $("ar-audio-status").textContent = text; };
  // The remote stream is requested only once the operator presses play: the
  // first press (button, Space, R or the native control) sets the source, and
  // loadedmetadata then starts playback at the read.
  let sourced = false;
  const ensureSource = () => { if (sourced) return false; sourced = true; player.src = item.media.src; return true; };
  const replay = () => {
    if (!player || ensureSource()) return;
    const note = adReviewAudioNote(player.duration, startAt);
    if (note && Number.isFinite(player.duration)) { audioStatus(note); return; }
    try { player.currentTime = startAt; } catch (_) {}
    player.play().then(() => {
      setTimeout(() => {
        if (!player.isConnected) return;
        const reached = Math.abs(player.currentTime - startAt) < 15;
        audioStatus(reached ? `Playing from ${clockT(item.span[0])}.` : (note || "This stream did not reach the read start. Judge the read on the transcript."));
      }, 1200);
    }).catch(() => { audioStatus("Press Space or play to listen (the browser held autoplay)."); });
  };
  if (player) {
    const offset = (item.media || {}).source_offset_s || 0, clock = $("ar-clock"), progress = $("ar-progress");
    const syncPlaying = () => {
      const playing = !player.paused && !player.ended;
      view.querySelectorAll(".ar-play").forEach((b) => { b.classList.toggle("is-playing", playing); b.setAttribute("aria-label", `${playing ? "Pause" : "Play"}, key Space`); });
    };
    ["play", "pause", "ended"].forEach((type) => player.addEventListener(type, syncPlaying));
    player.addEventListener("timeupdate", () => {
      if (clock) clock.textContent = clockT(player.currentTime + offset);
      if (progress) progress.value = Math.min(Number(progress.max), Math.max(0, player.currentTime - startAt));
    });
    player.playbackRate = Number(App.settings.playback_speed || 1);
    player.addEventListener("loadedmetadata", replay, { once: true });
    player.addEventListener("play", ensureSource);
    player.addEventListener("error", () => { audioStatus(AR_AUDIO_DEAD); disablePlayButtons(); }, { once: true });
  }
  const status = (text) => { if ($("ar-status")) $("ar-status").textContent = text; };
  const confirmBox = $("ar-confirm");
  const askFor = (name, details, body, label) => {
    AdReview.pending = { name, body, label };
    confirmBox.innerHTML = `<p><strong>${esc(adReviewAskWords(name, details, body.brand))}</strong></p>`;
    confirmBox.hidden = false;
    AdReview.pending.body = { ...body, expected_reads: details.reads, known_brand: !!details.known_brand };
  };
  const cancelPending = () => { AdReview.pending = null; confirmBox.hidden = true; confirmBox.innerHTML = ""; };
  const busy = { on: false };
  const send = async (body, label, name) => {
    if (busy.on) return;
    if (AdReview.removing) { status("A rule is being removed; try again when it finishes."); return; }
    if (AdReview.changed) { status(AR_CHANGED_WORDS); return; }
    const end = adReviewBegin();
    if (!end) { status("Wait for the current change to finish, then save."); return; }  // R8-02
    busy.on = true;
    body = adReviewBody(body, item);
    const unhold = adReviewHold(document.querySelectorAll(".ar-actions button, .ar-candidates button"));
    status("Saving…");
    // A play button that went dead during the save stays off.
    const release = () => { busy.on = false; unhold(); end(); if (player?.error) disablePlayButtons(); };
    try {
      const out = await post(`/api/v1/admin/review-queue/${encodeURIComponent(current.evidence_id)}/action`, body);
      end();
      player?.pause();
      cancelPending();
      adReviewAfterSave(out);
      advance(`Saved: ${label}.${adReviewRuleWords(out)}`, { label, labelId: out.label_id, listed: current });
    } catch (error) {
      release();
      if (error.code === "CONFIRM_REQUIRED") { askFor(name, error.details, body, label); status(""); return; }
      if (error.code === "READ_CHANGED") {
        AdReview.cache.delete(current.evidence_id);
        AdReview.changed = { id: current.evidence_id, before: adReviewSnapshot(item), after: null };
        AdReview.lastMessage = `Not saved: ${error.message}`;
        render();
        return;
      }
      status(`Not saved: ${error.message}`);
    }
  };
  const typeForm = $("ar-type"), promoForm = $("ar-promo"), input = $("ar-brand");
  let lookup = null;
  input.addEventListener("input", () => {
    clearTimeout(lookup);
    lookup = setTimeout(async () => {
      try {
        const out = await api(`/api/v1/admin/brands?q=${encodeURIComponent(input.value)}`);
        $("ar-brands").innerHTML = out.brands.map((b) => `<option value="${esc(b.display)}">${esc(b.key)} · ${b.reads} reads</option>`).join("");
      } catch (_) {}
    }, 150);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { cancelPending(); input.blur(); return; }
    if (event.key !== "Enter") return;
    event.preventDefault();
    const typed = input.value.trim();
    if (!typed) { status("Type a brand first."); return; }
    // Decision 14: Enter saves this read only; Shift+Enter is the
    // every-read rename (Shift+A), count first.
    if (event.shiftKey) {
      if (!item.raw_brand) { status("This read has no spoken name, so there is no every-read rule. Enter saves this read."); return; }
      const pending = AdReview.pending;
      if (pending?.name === "alias" && pending.body.brand === typed) return send(pending.body, pending.label, "alias");
      cancelPending();
      return send({ action: "alias", brand: typed }, `every “${item.raw_brand}” read is ${typed}`, "alias");
    }
    cancelPending();
    send({ action: "type", brand: typed, every_read: false }, `${typed} (this read only)`, "type");
  });
  typeForm.addEventListener("submit", (event) => event.preventDefault());
  const network = $("ar-network");
  const savePromo = () => {
    const text = network.value.trim();
    // The count came back for this exact text: the second Enter saves it.
    const pending = AdReview.pending;
    if (pending?.name === "self_promo" && pending.body.network === text) return send(pending.body, pending.label, "self_promo");
    cancelPending();
    send({ action: "self_promo", network: text }, `self-promotion on ${text === (item.show_title || "").trim() ? "this show" : `shows with “${text}”`}`, "self_promo");
  };
  network.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { cancelPending(); promoForm.hidden = true; network.blur(); return; }
    if (event.key !== "Enter") return;
    event.preventDefault();
    savePromo();
  });
  promoForm.addEventListener("submit", (event) => { event.preventDefault(); savePromo(); });
  $("ar-promo-cancel").onclick = () => { cancelPending(); promoForm.hidden = true; };
  const shownBrand = (item.read?.brand || {});
  const actions = {
    confirm: () => send({ action: "confirm", brand: shownBrand.key }, `confirmed ${shownBrand.display || shownBrand.key}`, "confirm"),
    type: () => { input.focus(); input.select(); },
    alias: () => {
      const typed = input.value.trim();
      if (AdReview.pending?.name === "alias" && AdReview.pending.body.brand === typed) return send(AdReview.pending.body, AdReview.pending.label, "alias");
      cancelPending();
      if (!typed) { status(`Type the brand every “${item.raw_brand}” read should show, then press Shift+A.`); input.focus(); return; }
      send({ action: "alias", brand: typed }, `every “${item.raw_brand}” read is ${typed}`, "alias");
    },
    not_ad: () => send({ action: "not_ad" }, "not an ad", "not_ad"),
    self_promo: () => { promoForm.hidden = false; network.focus(); network.select(); },
    generic: () => (AdReview.pending?.name === "generic" ? send(AdReview.pending.body, AdReview.pending.label, "generic")
      : send({ action: "generic" }, `“${item.raw_brand}” is a generic word`, "generic")),
    skip: () => advance("Skipped. It stays in the queue for later."),
    cancel: () => { cancelPending(); adReviewDisarmRules(); },
    play: () => { if (player && !ensureSource()) { if (player.paused) player.play().catch(() => {}); else player.pause(); } },
    replay: () => replay(),
    seen: () => {
      AdReview.changed = null;
      AdReview.lastMessage = "";
      Promise.resolve(render()).then(() => document.querySelector?.(".ar-read")?.focus?.());
    },
  };
  view.querySelectorAll("[data-ar]").forEach((b) => { b.onclick = () => actions[b.dataset.ar](); });
  view.querySelectorAll("[data-ar-pick]").forEach((b) => { b.onclick = () => send({ action: "pick", rank: Number(b.dataset.arPick), brand: b.dataset.brand }, `candidate ${b.dataset.arPick}, ${b.dataset.brand}`, "pick"); });
  wireAdReviewUndo(busy);
  cancelPending();
  const onKey = (event) => {
    if (keyGeneration !== AdReview.keyGeneration || App.route !== "ad-review" || !$("ar-status")) { document.removeEventListener("keydown", onKey); return; }
    const name = adReviewKey(event);
    if (!name) return;
    if ((name === "play" || name === "replay") && player?.error) { event.preventDefault(); return; }
    if (name === "play") { if (player) { event.preventDefault(); if (!ensureSource()) { if (player.paused) player.play().catch(() => {}); else player.pause(); } } return; }
    if (name === "replay") { if (player) { event.preventDefault(); replay(); } return; }
    if (name === "seen") { if (AdReview.changed && !AdReview.acting) { event.preventDefault(); actions.seen(); } return; }
    if (AdReview.changed && (AR_SAVE_KEYS.has(name) || name.startsWith("pick:"))) { event.preventDefault(); status(AR_CHANGED_WORDS); return; }
    if (name.startsWith("pick:")) { const b = view.querySelector(`[data-ar-pick="${name.slice(5)}"]`); if (b && !b.disabled) { event.preventDefault(); b.click(); } return; }
    if (name !== "cancel" && AdReview.pending && AdReview.pending.name !== name) cancelPending();
    const b = view.querySelector(`[data-ar="${name}"]`);
    if (name === "cancel" || (b && !b.disabled)) { event.preventDefault(); actions[name](); }
  };
  document.addEventListener("keydown", onKey);
};
// -- Suggested brands (brand_suggest). One stored machine suggestion per off-list
// heard name; it changes nothing until Confirm. Confirm and Undo that change
// shown reads (or a known brand) show the count inline first, as the Rules
// panel Remove does, then send it back (expected_reads, known_brand).
const AR_SG = { items: [], ask: null, status: {}, busy: null, loading: 0 };
const AR_SG_MADE = {
  alias: (s, key) => `Every read heard as “${esc(key)}” shows as ${esc(s.company || key)}`,
  brand: (s) => `${esc(s.company || "It")} is on the brand list`,
  stoplist: (s, key) => `“${esc(key)}” is a generic word`,
};
function adReviewSuggestLine(s) {
  if (s.state === "resolved") return "Already a known brand now";
  if (s.state === "confirmed") {
    const made = (s.rules || []).map(([kind, key]) => (AR_SG_MADE[kind] ? AR_SG_MADE[kind](s, key) : "")).filter(Boolean);
    return `Confirmed by ${esc(s.decided_by || "Reno")}${made.length ? ` · ${made.join(" · ")}` : ""}`;
  }
  if (s.state === "rule_removed") return "Its rule was removed in Rules";
  if (s.state === "dismissed") return "Not this: left unconfirmed";
  if (s.verdict === "not_brand") return "Suggested: not a brand";
  if (s.verdict === "unsure") {
    return "No clear suggestion" + (s.company ? ` · maybe ${esc(s.company)}` : "")
      + (s.self_promo ? " · looks like the show's own promotion" : "");
  }
  return `Suggested: <b>${esc(s.company)}</b>${s.official_domain ? ` (${esc(s.official_domain)})` : ""}${(s.confirm_writes ? s.confirm_writes === "alias" : s.heard_correct === false) ? " · misheard" : ""}`;
}
function adReviewSuggestRow(s, opts) {
  const id = String(s.id), busy = opts.busy === id || !!opts.acting;
  // Disabled only because another action runs: end() draws it again (R6-01).
  const held = !!opts.acting && opts.busy !== id;
  const ask = opts.ask && opts.ask.id === id ? opts.ask : null;
  // What the page shows, sent back so a later answer for the name is not confirmed by mistake.
  const data = `data-id="${esc(id)}" data-verdict="${esc(s.verdict)}"${s.company ? ` data-company="${esc(s.company)}"` : ""}`
    + `${s.heard_correct === null || s.heard_correct === undefined ? "" : ` data-heard-correct="${s.heard_correct ? "true" : "false"}"`}`
    + `${s.fetch_date ? ` data-fetch-date="${esc(s.fetch_date)}"` : ""} data-status="${esc(s.status)}"`;
  const button = (action, label) => {
    const again = ask && ask.action === action ? `${label}: press again` : label;
    return `<button type="button" class="ar-sg-btn" data-sg="${action}" ${data}${held ? " data-acting" : ""}${busy ? " disabled" : ""}>${again}</button>`;
  };
  const actions = s.state === "pending"
    ? (s.verdict === "unsure" ? "" : button("confirm", "Confirm")) + button("dismiss", "Not this")
    : s.state === "confirmed" || s.state === "dismissed" || s.state === "rule_removed" ? button("undo", "Undo") : "";
  const n = Number(s.reads_not_saying) || 0;
  const meta = `heard in ${Number(s.ads) || 0} ad${Number(s.ads) === 1 ? "" : "s"} when asked · ${(s.shows || []).map(esc).join(", ")}`
    + (n ? ` · ${n} more read${n === 1 ? "" : "s"} ${n === 1 ? "does" : "do"} not say it` : "");
  const reads = ask ? `${ask.reads} read${ask.reads === 1 ? "" : "s"}` : "";
  const label = ask ? ask.action[0].toUpperCase() + ask.action.slice(1) : "";
  const askLine = ask ? `<p class="ar-sg-ask notice" role="alert">This changes ${reads}.${ask.known ? ` “${esc(ask.word)}” is a known brand.` : ""} Press ${label} again to save; Esc cancels.</p>` : "";
  return `<li class="ar-sg-row" data-state="${esc(s.state)}"><div class="ar-sg-main">`
    + `<p class="ar-sg-name"><strong class="ar-mono">“${esc(s.key)}”</strong> <span class="badge report">Name not confirmed</span></p>`
    + `<p class="meta">${meta}</p>`
    + (s.excerpt ? `<p class="meta ar-sg-excerpt">${esc(s.excerpt)}</p>` : "")
    + `<p class="ar-sg-says">${adReviewSuggestLine(s)}</p>`
    + (s.evidence && s.state !== "resolved" ? `<p class="meta ar-sg-evidence">${esc(s.evidence)}</p>` : "")
    + askLine
    + `<p class="meta ar-sg-status" role="status" aria-live="polite">${esc((opts.status || {})[id] || "")}</p></div>`
    + `<div class="ar-sg-actions">${actions}</div></li>`;
}
function adReviewSuggestMarkup(res, opts = {}) {
  const items = res.suggestions || [];
  const waiting = items.filter((s) => s.state === "pending").length;
  return `<details class="ar-rules ar-suggest" open><summary id="ar-suggest-h">Suggested brands · ${waiting} waiting</summary><div class="ar-rules-body">`
    + `<p class="meta">A suggestion changes nothing until you confirm it. Confirm makes the same rule as the buttons on a read; Undo takes it back.</p>`
    + `<ul class="ar-sg-list">${items.map((s) => adReviewSuggestRow(s, opts)).join("")}</ul></div></details>`;
}
function adReviewSuggestError(message) {
  return `<section class="ar-rules ar-rules-failed"><div class="ar-rules-body"><p class="error-box" role="alert">Suggested brands could not load: ${esc(message)}</p>`
    + `<button type="button" data-sg="reload">Retry</button></div></section>`;
}
function drawAdReviewSuggest() {
  const host = $("ar-suggest");
  if (!host) return;
  host.innerHTML = adReviewSuggestMarkup({ suggestions: AR_SG.items }, { ask: AR_SG.ask, status: AR_SG.status, busy: AR_SG.busy, acting: AdReview.acting });
  host.onkeydown = (event) => { if (event.key === "Escape" && AR_SG.ask) { AR_SG.ask = null; drawAdReviewSuggest(); } };
  wireAdReviewSuggest(host);
}
async function loadAdReviewSuggest(keep = false) {
  const host = $("ar-suggest");
  if (!host) return;
  if (!keep) { AR_SG.items = []; AR_SG.ask = null; AR_SG.status = {}; AR_SG.busy = null; }
  AR_SG.loading++;
  try {
    const res = await api("/api/v1/admin/brand-suggestions");
    AR_SG.items = res.suggestions || [];
    drawAdReviewSuggest();
  } catch (error) {
    host.innerHTML = adReviewSuggestError(error.message);
    wireAdReviewSuggest(host);
  } finally { AR_SG.loading--; }
}
function wireAdReviewSuggest(host) {
  host.querySelectorAll("button[data-sg]").forEach((b) => { b.onclick = () => adReviewSuggestPress(b); });
}
async function adReviewSuggestPress(button) {
  const action = button.dataset.sg;
  if (action === "reload") return loadAdReviewSuggest();
  const end = adReviewBegin();
  if (!end) return;  // another read-changing action is running
  const id = button.dataset.id;
  const ask = AR_SG.ask && AR_SG.ask.id === id && AR_SG.ask.action === action ? AR_SG.ask : null;
  if (!ask) AR_SG.ask = null;
  const row = AR_SG.items.find((s) => String(s.id) === id);
  const d = button.dataset;
  const body = action === "undo" ? { status: d.status, fetch_date: d.fetchDate === undefined ? null : d.fetchDate } : {
    verdict: d.verdict, company: d.company === undefined ? null : d.company,
    heard_correct: d.heardCorrect === undefined ? null : d.heardCorrect === "true",
    fetch_date: d.fetchDate === undefined ? null : d.fetchDate };
  if (ask) { body.expected_reads = ask.reads; if (ask.known) body.known_brand = true; }
  AR_SG.busy = id; AR_SG.status[id] = action === "undo" ? "Undoing…" : "Saving…";
  drawAdReviewSuggest();
  try {
    const out = await api(`/api/v1/admin/brand-suggestions/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify(body) });
    end();
    AR_SG.busy = null; AR_SG.ask = null; AR_SG.status[id] = "Saved";
    const rules = action === "confirm" || (action === "undo" && row && row.state === "confirmed");
    if (rules && Number(out.reads) > 0) {
      // Reads changed: draw the page again, as the Rules panel Remove does.
      AdReview.cache.clear(); AdReview.items = []; AdReview.lastMessage = out.message || "Saved";
      await render();
      return;
    }
    await loadAdReviewSuggest(true);
    if (rules) await AdReview.reloadRules?.({ open: true });
  } catch (error) {
    end();
    AR_SG.busy = null;
    if (error.code === "CONFIRM_REQUIRED") {
      const d = error.details || {};
      AR_SG.ask = { id, action, reads: Number(d.reads) || 0, known: !!d.known_brand, word: d.word || d.raw || (row && row.key) || "" };
      AR_SG.status[id] = "";
    } else {
      AR_SG.ask = null;
      AR_SG.status[id] = error.code === "READ_CHANGED" ? error.message : `Not saved: ${error.message}`;
      if (error.code === "READ_CHANGED") { await loadAdReviewSuggest(true); return; }
    }
    drawAdReviewSuggest();
  }
}
// opts.open keeps the panel open after a reload; opts.note puts an alert on top.
function adReviewRulesMarkup(res, opts = {}) {
  const r = res.rules, ids = r.rule_ids || {};
  const remove = (kind, key) => ids[`${kind}\t${key}`] ? `<button type="button" class="ar-remove" data-rule="${ids[`${kind}\t${key}`]}"${AdReview.acting ? " data-acting disabled" : ""}>Remove</button>` : "";
  const row = (text, button) => `<li><span>${text}</span>${button}</li>`;
  const group = (title, items, fmt) => items.length ? `<h3>${title}</h3><ul>${items.map(fmt).join("")}</ul>` : "";
  const count = r.brand_aliases.length + r.stoplist.length + r.self_promo.length + r.not_ads.length + r.new_brands.length;
  return `<details class="ar-rules"${opts.open ? " open" : ""}><summary>Rules so far · ${count} <span class="meta">· ${r.labels} labeled example${r.labels === 1 ? "" : "s"}</span></summary><div class="ar-rules-body">`
    + (opts.note ? `<p class="ar-rule-error" role="alert">${esc(opts.note)}</p>` : "")
    + `<p class="meta">Removing a rule puts every read it changed that nobody reviewed back as the namer gave it, now and on every load. ${esc("What stays: the brand or judgment you gave the read you acted on, and any alerts already sent. The examples the rule came from are marked superseded.")}</p>`
    + (count ? "" : '<p class="meta">No rules yet.</p>')
    + group("Brand aliases", r.brand_aliases, (a) => row(`“${esc(a.raw)}” → ${esc(a.display || a.canonical)}`, remove("alias", a.raw)))
    + group("Generic words (never a brand)", r.stoplist, (w) => row(esc(w.word), remove("stoplist", w.word)))
    + group("Self-promotion", r.self_promo, (p) => row(`${esc(p.brand)} on ${p.exact_show ? `the show “${esc(p.network)}”` : `shows with “${esc(p.network)}”`}`, remove("self_promo", `${p.brand}\t${p.network}\t${p.exact_show ? 1 : 0}`)))
    + group("Not ads", r.not_ads, (n) => row(`${esc(n.show_title || "")} <span class="code">${esc(clockT(n.start_s))}–${esc(clockT(n.end_s))}</span>`, remove("not_ad", n.record_key)))
    + group("New brands for the namer", r.new_brands, (b) => row(esc(b.display || b.brand) + (b.actor === "brand-curate" ? '<span class="meta"> · added automatically</span>' : ""), remove("brand", b.brand)))
    + `<p class="meta">Export for the pipeline: <span class="code">scripts/app_workspace.py --workspace &lt;ws&gt; corrections export --out corrections.json</span></p></div></details>`;
}
// The Rules panel stays on the page when the list cannot load.
function adReviewRulesErrorMarkup(message) {
  return `<section class="ar-rules ar-rules-failed" aria-labelledby="ar-rules-failed-h"><div class="ar-rules-body"><h2 id="ar-rules-failed-h" class="ar-rules-failed-title">Rules</h2>`
    + `<p class="error-box" role="alert">Rules could not load, so you can't see or remove them here. Rules already saved still apply. This read and its keys still work.</p>`
    + `<p class="meta">${esc(message)}</p><button type="button" id="ar-rules-retry">Retry</button></div></section>`;
}
// Drops the removal prompt: the next Remove press asks for a fresh count.
function adReviewDisarmRules() {
  document.querySelectorAll(".ar-rule-ask").forEach((row) => row.remove());
  document.querySelectorAll(".ar-remove").forEach((x) => {
    if (x.dataset.reads === undefined) return;
    delete x.dataset.reads;
    x.textContent = x.classList?.contains("ar-retry") ? "Retry" : "Remove";
  });
}
function wireAdReviewRules() {
  if ($("ar-rules")) $("ar-rules").onkeydown = (event) => { if (event.key === "Escape") adReviewDisarmRules(); };
  document.querySelectorAll(".ar-rules-reload").forEach((b) => {
    b.onclick = async () => {
      if (AdReview.acting) return;  // a read-changing action is running (R6-03)
      b.disabled = true;
      AdReview.cache.clear(); AdReview.items = [];
      await render();
      await AdReview.reloadRules?.({ open: true });
    };
  });
  document.querySelectorAll(".ar-remove").forEach((b) => {
    b.onclick = async () => {
      if (AdReview.removing) return;
      const end = adReviewBegin();
      if (!end) return;  // another read-changing action is running
      // Count first (decision 12): a press without the count shown is a dry
      // run the server answers with the reads the removal changes; the
      // press after it sends that count. One rule asks at a time.
      const shown = b.dataset.reads;
      if (shown === undefined) adReviewDisarmRules();
      // Nothing else is saved while the rule's reads are counted or re-applied.
      const unhold = adReviewHold(document.querySelectorAll(".ar-actions button, .ar-candidates button, .ar-remove, #ar-brand, #ar-network"));
      AdReview.removing = true;
      b.textContent = shown === undefined ? "Counting…" : "Removing…";
      if ($("ar-status")) $("ar-status").textContent = shown === undefined ? "Counting the reads this removal changes. Actions wait until it finishes." : "Removing the rule. Actions wait until it finishes.";
      try {
        const out = await api(`/api/v1/admin/rules/${encodeURIComponent(b.dataset.rule)}`,
          shown === undefined ? { method: "DELETE" } : { method: "DELETE", body: JSON.stringify({ expected_reads: Number(shown) }) });
        end();
        AdReview.removing = false;
        AdReview.cache.clear(); AdReview.items = []; AdReview.lastMessage = out.message;
        render();
      } catch (error) {
        end();
        AdReview.removing = false;
        unhold();
        const id = b.dataset.rule;
        if (error.code === "CONFIRM_REQUIRED") {
          adReviewDisarmRules();
          const reads = Number(error.details?.reads) || 0;
          b.dataset.reads = String(reads);
          b.textContent = "Remove: press again";
          if ($("ar-status")) $("ar-status").textContent = "";
          b.closest("li")?.insertAdjacentHTML("afterend", adReviewRemoveAskMarkup(reads, shown !== undefined && Number(shown) !== reads));
          return;
        }
        adReviewDisarmRules();
        b.textContent = b.classList?.contains("ar-retry") ? "Retry" : "Remove";
        // Shown where the operator looks (the Rules panel and the result
        // line), not only to screen readers.
        if (/unknown rule/i.test(error.message)) {
          // The rule's reads changed too: drop the prefetched reads and the
          // queue, draw the read again, then reload the list. "Reloaded" is
          // said only once that worked.
          AdReview.cache.clear(); AdReview.items = [];
          try {
            await render();
            const ok = await AdReview.reloadRules?.({ open: true, note: "That rule was already removed, maybe in another tab. The list now shows the current rules." });
            if ($("ar-status")) $("ar-status").textContent = ok ? "That rule was already removed. The read and the rules list are reloaded." : "That rule was already removed, but the rules list could not be reloaded. Use Retry in the Rules panel.";
          } catch (reloadError) {
            if ($("ar-status")) $("ar-status").textContent = `That rule was already removed, but the page could not be reloaded: ${reloadError.message}.`;
          }
          return;
        }
        // No status means the server never answered: the delete may have run.
        const answered = error.status !== undefined;
        if ($("ar-status")) $("ar-status").textContent = answered ? `Not removed: ${error.message}. The rule still applies.` : "No answer from the server, so this rule may or may not be removed. Reload the rules to see the current list.";
        document.querySelectorAll(".ar-rule-error").forEach((row) => row.remove());
        const row = [...document.querySelectorAll(".ar-remove")].find((x) => x.dataset.rule === id)?.closest("li");
        row?.insertAdjacentHTML("afterend", adReviewRuleErrorMarkup(error.message, id, { answered }));
        wireAdReviewRules();
      }
    };
  });
}
function wireAdReviewFilters() {
  $("ar-filters").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (AdReview.acting) {  // a read-changing action is running (R6-03)
      if ($("ar-status")) $("ar-status").textContent = "Wait for the current change to finish, then filter.";
      return;
    }
    AdReview.filter = { show: $("ar-show").value, brand: $("ar-brand-filter").value.trim(), reason: $("ar-reason").value };
    AdReview.items = []; AdReview.lastMessage = "";
    render();
  });
}
