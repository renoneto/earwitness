"use strict";
// Owner ad review (BRAND-CONFIRM-ROUTE). The hosted page for the workspace
// owner: the review queue, any read by its record id, and Confirm, Type a
// brand (this read only), Not an ad and Undo. Every write sends the
// session's CSRF token and workspace switch revision; the server decides.
(function () {
  const API = "/api/v1/owner/ad-review";
  const state = { csrf: "", revision: "0", item: null, saved: null };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const clock = (s) => {
    const v = Math.round(Number(s));
    if (!Number.isFinite(v) || v < 0) return "unknown";
    const h = Math.floor(v / 3600), m = Math.floor(v / 60) % 60, sec = String(v % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };
  const say = (text) => { $("ar-status").textContent = text; };

  async function call(path, body) {
    const init = { headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (body !== undefined) {
      init.method = "POST";
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
      init.headers["X-CSRF-Token"] = state.csrf;
      init.headers["X-EarWitness-Workspace-Switch-Revision"] = state.revision;
    }
    const response = await fetch(path, init);
    const out = await response.json().catch(() => ({}));
    if (!response.ok || out.ok === false) {
      const error = out.error && typeof out.error === "object" ? out.error : {};
      const failure = new Error(error.message || `Request failed (${response.status})`);
      failure.status = response.status; failure.code = error.code;
      throw failure;
    }
    return out;
  }

  function readMarkup(item) {
    const read = item.read || {}, brand = read.brand || {};
    const lines = (item.transcript || []).map((l) => `<li class="${l.in_read ? "in-read" : ""}"><time>${clock(l.start)}</time><span>${esc(l.text)}</span></li>`).join("")
      || '<li><span class="meta">No transcript lines for this read.</span></li>';
    const [from, to] = item.span || [];
    const shown = brand.display || brand.key || "";
    return `<section class="ar-box ar-read" aria-label="Read under review">`
      + `<p class="ar-where">${esc(item.show_title || "Unknown show")} · ${esc(item.ep_title || item.episode_id)} · ${esc(item.pub_date || "date unknown")} · ${clock(from)} → ${clock(to)}</p>`
      + `<p class="meta code">${esc(item.evidence_id)}</p>`
      + `<ol class="ar-lines">${lines}</ol>`
      + `<div class="ar-facts"><div class="ar-fact"><span class="ar-fact-label">Brand shown</span><strong>${shown ? esc(shown) : "No brand yet"}</strong>${brand.confirmed ? ' <span class="badge confirmed">confirmed</span>' : ""}</div>`
      + (item.raw_brand ? `<div class="ar-fact"><span class="ar-fact-label">Spoken name</span><strong class="ar-mono">“${esc(item.raw_brand)}”</strong></div>` : "")
      + `</div>`
      + `<div class="ar-actions" role="group" aria-label="Review actions">`
      + `<button type="button" class="primary" id="ar-confirm"${brand.key ? "" : " disabled"}>Confirm ${shown ? esc(shown) : "brand"}</button>`
      + `<button type="button" id="ar-not-ad">Not an ad</button>`
      + `<button type="button" id="ar-undo"${state.saved && state.saved.id === item.evidence_id ? "" : " disabled"}>Undo last save</button>`
      + `</div>`
      + `<form id="ar-type" class="ar-type"><label class="field" for="ar-brand"><span>Type a brand (this read only)</span><input id="ar-brand" type="text" autocomplete="off" placeholder="The right brand name"></label><button type="submit">Save brand</button></form>`
      + `</section>`;
  }

  async function save(body, label) {
    const item = state.item;
    say("Saving…");
    try {
      const out = await call(`${API}/${encodeURIComponent(item.evidence_id)}/action`, body);
      state.saved = { id: item.evidence_id, labelId: out.label_id, label };
      say(`Saved: ${label}.`);
    } catch (error) {
      say(`Not saved: ${error.message}`);
    }
    await open(item.evidence_id, true);
    await loadQueue();
  }

  async function undo() {
    const saved = state.saved;
    if (!saved) return;
    say("Undoing…");
    try {
      const out = await call(`${API}/${encodeURIComponent(saved.id)}/undo`, { label_id: saved.labelId });
      state.saved = null;
      say(`Undone: ${saved.label}. ${out.message || ""}`.trim());
    } catch (error) {
      say(`Not undone: ${error.message}`);
    }
    await open(saved.id, true);
    await loadQueue();
  }

  async function open(id, keepStatus) {
    const target = $("ar-read");
    if (!keepStatus) say("");
    try {
      const out = await call(`${API}/${encodeURIComponent(id)}`);
      state.item = out.item;
    } catch (error) {
      state.item = null;
      target.innerHTML = `<p role="alert">${error.status === 404 ? "No read with that record id in this workspace." : esc(error.message)}</p>`;
      return;
    }
    const item = state.item, brand = (item.read || {}).brand || {};
    target.innerHTML = readMarkup(item);
    $("ar-confirm").onclick = () => save({ action: "confirm", brand: brand.key }, `confirmed ${brand.display || brand.key}`);
    $("ar-not-ad").onclick = () => save({ action: "not_ad" }, "not an ad");
    $("ar-undo").onclick = undo;
    $("ar-type").onsubmit = (event) => {
      event.preventDefault();
      const typed = $("ar-brand").value.trim();
      if (!typed) { say("Type a brand first."); return; }
      save({ action: "type", brand: typed, heard: item.raw_brand || "", every_read: false }, `${typed} (this read only)`);
    };
  }

  async function loadQueue() {
    const box = $("ar-queue");
    try {
      const out = await call(`${API}?limit=50`);
      $("ar-position").textContent = `${out.total} waiting`;
      box.innerHTML = out.items.length
        ? `<div class="table-wrap"><table class="grid"><thead><tr><th>Show</th><th>Date</th><th>Spoken name</th><th></th></tr></thead><tbody>${out.items.map((i) => `<tr><td>${esc(i.show_title || "Unknown show")}</td><td>${esc(i.pub_date || "")}</td><td>${esc(i.raw_brand || "")}</td><td><button type="button" data-open="${esc(i.evidence_id)}">Open</button></td></tr>`).join("")}</tbody></table></div>`
        : '<p class="meta">Nothing waits for review.</p>';
      box.querySelectorAll("[data-open]").forEach((b) => { b.onclick = () => { $("ar-id").value = b.dataset.open; open(b.dataset.open); }; });
    } catch (error) {
      box.innerHTML = `<p role="alert">The queue could not load: ${esc(error.message)}</p>`;
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    $("ar-open").onsubmit = (event) => {
      event.preventDefault();
      const id = $("ar-id").value.trim();
      if (id) open(id);
    };
    try {
      const session = await call("/api/v1/session");
      state.csrf = session.csrf_token || "";
      state.revision = String(session.workspace_switch_revision ?? 0);
    } catch (error) {
      say(`Could not read your session: ${error.message}`);
      return;
    }
    const wanted = new URLSearchParams(location.search).get("id");
    if (wanted) { $("ar-id").value = wanted; open(wanted); }
    loadQueue();
  });
}());
