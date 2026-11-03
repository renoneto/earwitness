"use strict";
const App = { csrf: null, aggregateRev: 0, route: "overview", routeHash: "", lastLibrary: null,
  controller: null, generation: 0, mutations: 0, drafts: {}, settings: {}, scrollPositions: {} };
try { App.drafts = JSON.parse(sessionStorage.getItem("earwitness-drafts") || "{}"); } catch (_) {}
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
// A read's brand as text (escape it to render): "Brand · Product · Network"
// when the namer gave its product or network (Option A), else the brand alone.
const brandLabel = (brand) => [brand?.display, brand?.product, brand?.network].filter(Boolean).join(" · ");
const fmtT = (s) => typeof s === "number" && Number.isFinite(s) ? `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}` : "unknown";
function say(text) { $("live").textContent = text; }
function setToolbar(markup) { $("toolbar").innerHTML = markup || ""; }
function modal(title, body) {
  const opener = document.activeElement;
  $("modal-title").textContent = title; $("modal-body").innerHTML = body;
  $("modal").showModal(); $("modal-ok").focus();
  $("modal").addEventListener("close", () => { if (opener?.isConnected) opener.focus(); }, { once: true });
}
async function api(path, options = {}) {
  const write = ["POST", "PUT", "DELETE"].includes(options.method);
  const init = { ...options, headers: { Accept: "application/json", ...options.headers } };
  if (init.body !== undefined) init.headers["Content-Type"] = "application/json";
  if (write) { init.headers["X-CSRF-Token"] = App.csrf; App.mutations++; }
  else if (!init.signal) init.signal = App.controller?.signal;
  try {
    const response = await fetch(path, init).catch(() => {
      throw new Error("EarWitness server is unreachable. It refuses to start on a missing,"
        + " unupgraded, or quarantined workspace; check the server terminal, then upgrade"
        + " or reconcile the workspace and reload.");
    });
    const body = await response.json();
    if (!response.ok || body.ok === false) {
      const detail = body.error && typeof body.error === "object" ? body.error : null;
      const error = new Error((detail ? detail.message : body.error) || `Request failed (${response.status})`);
      error.status = response.status; error.code = detail?.code; error.details = detail?.details || {}; throw error;
    }
    if (body.aggregate_rev !== undefined) {
      App.aggregateRev = body.aggregate_rev;
      $("agg-rev").textContent = `rev ${App.aggregateRev}`;
    }
    return body;
  } finally { if (write) App.mutations--; }
}
function formValues(form) {
  const values = {};
  for (const el of form.elements) {
    if (!el.id || !/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) continue;
    values[el.id] = el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
  }
  return values;
}
function persistDrafts() {
  try { sessionStorage.setItem("earwitness-drafts", JSON.stringify(App.drafts)); }
  catch (_) { say("Draft storage unavailable. Keep this tab open until you save."); }
}
function rememberForm(form) {
  if (!form?.id || form.dataset.draft === "off") return;
  const page = App.drafts[App.routeHash] ||= {};
  page[form.id] = { values: formValues(form), revision: form.dataset.revision };
  persistDrafts();
}
function restoreDrafts() {
  const drafts = App.drafts[App.routeHash] || {};
  for (const [id, draft] of Object.entries(drafts)) {
    const form = $(id); if (!form) continue;
    for (const [key, value] of Object.entries(draft.values)) {
      const el = $(key); if (!el || (el.form !== form && !form.contains(el))) continue;
      if (el.type === "checkbox" || el.type === "radio") el.checked = value;
      else el.value = value;
    }
    if (draft.revision !== undefined) form.dataset.revision = draft.revision;
  }
  if (Object.keys(drafts).length) say("Unsaved local draft restored. Saving still checks its original revision.");
}
function advanceRevision(form, revision) {
  if (!form || revision === undefined) return;
  form.dataset.revision = revision;
  const draft = App.drafts[App.routeHash]?.[form.id];
  if (draft) { draft.revision = String(revision); persistDrafts(); }
}
function savedForm(form, submitted) {
  if (JSON.stringify(formValues(form)) === JSON.stringify(submitted)) {
    delete App.drafts[App.routeHash]?.[form.id]; persistDrafts();
  } else rememberForm(form);
}
function wireForm(id, action) {
  const form = $(id);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitted = formValues(form), hash = App.routeHash;
    const button = form.querySelector('[type="submit"]');
    if (button?.disabled) return;
    if (button) button.disabled = true;
    const status = form.querySelector('[role="status"]');
    if (status) status.textContent = "Saving…";
    try {
      const out = await action(submitted, form);
      if (!form.isConnected || App.routeHash !== hash) return;
      advanceRevision(form, out?.revision);
      savedForm(form, submitted);
      if (out?.route) {
        const drafts = App.drafts[hash];
        delete App.drafts[hash];
        if (drafts) App.drafts[out.route] = drafts;
        persistDrafts(); history.replaceState(null, "", out.route); App.routeHash = out.route;
        await render();
        if (App.routeHash !== out.route) return;
      }
      // A route change re-renders the page: report on the form now shown.
      const shown = form.isConnected ? status : $(form.id)?.querySelector('[role="status"]');
      if (shown) shown.textContent = `Saved${out?.revision !== undefined ? ` · revision ${out.revision}` : ""}.`;
      say("Saved.");
    } catch (error) {
      if (error.name === "AbortError") return;
      rememberForm(form);
      const text = error.status === 412 ? `Conflict: ${error.message}. Your draft is retained. Discard it to reload the server copy.` : error.message;
      if (status) status.textContent = text; else modal("Could not save", `<p>${esc(text)}</p>`);
    } finally { if (button?.isConnected) button.disabled = false; }
  });
}
function navigate(hash) {
  if (App.mutations) { say("Wait for the current save before navigating."); return false; }
  if (location.hash === hash) render(); else location.hash = hash;
  return true;
}
async function render() {
  if (App.mutations) { history.replaceState(null, "", App.routeHash); say("Wait for the current save."); return; }
  App.controller?.abort(); App.controller = new AbortController();
  const generation = ++App.generation;
  const nextHash = location.hash || "#/overview";
  if (App.routeHash && App.routeHash !== nextHash) App.scrollPositions[App.routeHash] = window.scrollY;
  if (App.routeHash.startsWith("#/library") && !nextHash.startsWith("#/library")) App.editWatchFromLibrary = null;
  clearTimeout(Lib.timer);
  App.routeHash = location.hash || "#/overview";
  const [path] = App.routeHash.replace(/^#\//, "").split("?");
  const [route, ...params] = path.split("/"); App.route = Views[route] ? route : "overview";
  $("sidebar").classList.remove("open"); $("nav-toggle").setAttribute("aria-expanded", "false");
  document.querySelectorAll(".sidebar a").forEach((a) => a.dataset.route === App.route ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  $("view").innerHTML = '<p role="status">Loading…</p>'; setToolbar("");
  try {
    if (!App.csrf) {
      const session = await api("/api/v1/session"); App.csrf = session.csrf_token;
      App.settings = (await api("/api/v1/settings")).settings;
      document.body.dataset.density = App.settings.page_density;
    }
    await Views[App.route](params);
    if (generation !== App.generation) return;
    restoreDrafts();
    const drafts = App.drafts[App.routeHash] || {};
    if (Object.keys(drafts).length) {
      const discard = document.createElement("button"); discard.textContent = "Discard local draft";
      discard.onclick = () => { if (confirm("Discard this page's unsaved draft and reload saved values?")) { delete App.drafts[App.routeHash]; persistDrafts(); render(); } };
      $("toolbar").append(discard);
    }
    $("main").focus({ preventScroll: true });
    window.scrollTo(0, App.scrollPositions[App.routeHash] || 0);
  } catch (error) {
    if (generation !== App.generation || error.name === "AbortError") return;
    $("view").innerHTML = `<h1>Could not load</h1><p role="alert">${esc(error.message)}</p><button id="retry">Retry</button>`;
    $("retry").onclick = render;
  }
}
document.addEventListener("DOMContentLoaded", () => {
  $("nav-toggle").onclick = () => { const open = $("sidebar").classList.toggle("open"); $("nav-toggle").setAttribute("aria-expanded", String(open)); };
  document.addEventListener("input", (event) => rememberForm(event.target.form || event.target.closest("form")));
  document.addEventListener("change", (event) => rememberForm(event.target.form || event.target.closest("form")));
  document.addEventListener("click", (event) => { const a = event.target.closest('a[href^="#/"]'); if (a && !event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(a.getAttribute("href")); } });
  window.addEventListener("hashchange", render);
  window.addEventListener("beforeunload", (event) => { if (App.mutations) { event.preventDefault(); event.returnValue = ""; } });
  render();
});
