"use strict";
// Small dependency-free SPA. The server enforces every permission; the UI only hides
// actions the current user cannot take (it asks the API via `allowed_transitions`).

const TOKEN_KEY = "desk.token";
const state = { user: null, stream: null };
const $view = document.getElementById("view");

// ---------- helpers ----------

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const isOps = () => state.user && (state.user.role === "operator" || state.user.role === "admin");
const isClient = () => state.user && state.user.role === "client";
const fmtDate = (iso) => (iso ? new Date(iso + (iso.endsWith("Z") ? "" : "Z")).toLocaleString() : "");
const badge = (value) => `<span class="badge ${esc(value)}">${esc(String(value).replace("_", " "))}</span>`;

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 3500);
}

class ApiError extends Error {
  constructor(status, body) {
    const detail = body && body.detail;
    let message = typeof detail === "string" ? detail : `Request failed (${status})`;
    if (Array.isArray(detail)) message = detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    if (body && body.episode_ids) message += `: ${body.episode_ids.join(", ")}`;
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: form ?? (body !== undefined ? JSON.stringify(body) : undefined) });
  if (res.status === 401 && path !== "/api/auth/login") {
    logout();
    throw new ApiError(401, { detail: "Your session has expired, please log in again" });
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

// Wraps a handler so any API error is shown next to the form instead of being lost.
const guarded = (errorEl, fn) => async (event) => {
  if (event) event.preventDefault();
  errorEl.textContent = "";
  try {
    await fn(event);
  } catch (err) {
    errorEl.textContent = err.message;
  }
};

// ---------- auth & layout ----------

function logout() {
  sessionStorage.removeItem(TOKEN_KEY);
  state.user = null;
  stopStream();
  document.getElementById("topbar").hidden = true;
  if (location.hash === "#/login") viewLogin();
  else location.hash = "#/login";
}

function renderNav() {
  const links = [["#/requests", "Requests"]];
  if (isClient()) links.push(["#/new", "New request"]);
  const current = location.hash.split("/").slice(0, 2).join("/");
  document.getElementById("nav").innerHTML = links
    .map(([href, label]) => `<a href="${href}" class="${current === href ? "active" : ""}">${label}</a>`)
    .join("");
  document.getElementById("whoami").textContent = `${state.user.name} · ${state.user.role}`;
  document.getElementById("topbar").hidden = false;
}

function viewLogin() {
  document.getElementById("topbar").hidden = true;
  $view.innerHTML = `
    <form class="panel login" id="login-form">
      <h2>Sign in</h2>
      <label>Email <input name="email" type="email" autocomplete="username" required autofocus></label>
      <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
      <div class="error" id="login-error"></div>
      <button type="submit">Sign in</button>
    </form>`;
  const form = document.getElementById("login-form");
  form.addEventListener("submit", guarded(document.getElementById("login-error"), async () => {
    const data = await api("/api/auth/login", { method: "POST", body: Object.fromEntries(new FormData(form)) });
    sessionStorage.setItem(TOKEN_KEY, data.access_token);
    state.user = data.user;
    startStream();
    location.hash = "#/requests";
  }));
}

// ---------- requests ----------

async function viewRequests() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const status = params.get("status") || "";
  const offset = Number(params.get("offset") || 0);
  const query = new URLSearchParams({ limit: 50, offset });
  if (status) query.set("status", status);
  const page = await api(`/api/requests?${query}`);
  const statuses = ["", "submitted", "in_progress", "delivered", "accepted", "rejected"];
  const pageLink = (o) => `#/requests?${new URLSearchParams({ status, offset: o })}`;
  $view.innerHTML = `
    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h2>${isClient() ? "My requests" : "All requests"}</h2>
        <label style="max-width:220px">Status
          <select id="status-filter">${statuses.map((s) => `<option value="${s}" ${s === status ? "selected" : ""}>${s ? s.replace("_", " ") : "All"}</option>`).join("")}</select>
        </label>
      </div>
      ${page.items.length === 0 ? `<p class="muted">No requests yet.${isClient() ? ' <a href="#/new">Create one</a>.' : ""}</p>` : `
      <table>
        <thead><tr><th>#</th>${isOps() ? "<th>Client</th>" : ""}<th>Task</th><th>Progress</th><th>Deadline</th><th>Status</th><th>Created</th></tr></thead>
        <tbody>${page.items.map((r) => `
          <tr class="clickable" data-id="${r.id}">
            <td>${r.id}</td>${isOps() ? `<td>${esc(r.client_name)}</td>` : ""}
            <td>${esc(r.task_name)}</td>
            <td>${r.assigned_count} / ${r.episodes_requested}</td>
            <td>${esc(r.deadline)}</td>
            <td>${badge(r.status)}</td>
            <td class="muted">${fmtDate(r.created_at)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
      <div class="actions">
        ${offset > 0 ? `<a href="${pageLink(Math.max(0, offset - 50))}">← Previous</a>` : ""}
        <span class="muted">${offset + 1}–${offset + page.items.length} of ${page.total}</span>
        ${offset + page.items.length < page.total ? `<a href="${pageLink(offset + 50)}">Next →</a>` : ""}
      </div>`}
    </div>`;
  document.getElementById("status-filter").addEventListener("change", (e) => {
    location.hash = `#/requests?${new URLSearchParams({ status: e.target.value })}`;
  });
  $view.querySelectorAll("tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", () => (location.hash = `#/requests/${tr.dataset.id}`)));
}

function viewNewRequest() {
  const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  $view.innerHTML = `
    <form class="panel" id="new-form" style="max-width:560px">
      <h2>New dataset request</h2>
      <label>Task name <input name="task_name" required maxlength="255" placeholder="e.g. pick cup"></label>
      <div class="row">
        <label>Episodes requested <input name="episodes_requested" type="number" min="1" required></label>
        <label>Deadline <input name="deadline" type="date" min="${new Date().toISOString().slice(0, 10)}" value="${tomorrow}" required></label>
      </div>
      <label>Notes <textarea name="notes" rows="4" maxlength="5000"></textarea></label>
      <div class="error" id="new-error"></div>
      <button type="submit">Submit request</button>
    </form>`;
  const form = document.getElementById("new-form");
  form.addEventListener("submit", guarded(document.getElementById("new-error"), async () => {
    const data = Object.fromEntries(new FormData(form));
    data.episodes_requested = Number(data.episodes_requested);
    if (!data.notes) delete data.notes;
    const created = await api("/api/requests", { method: "POST", body: data });
    toast(`Request #${created.id} submitted`);
    location.hash = `#/requests/${created.id}`;
  }));
}

const ACTION_LABELS = {
  in_progress: "Start work",
  delivered: "Mark as delivered",
  accepted: "Accept delivery",
  rejected: "Reject delivery",
};

async function viewRequestDetail(id) {
  const [req, assigned] = await Promise.all([api(`/api/requests/${id}`), api(`/api/requests/${id}/assignments`)]);
  const canEditAssignments = isOps() && req.status === "in_progress";
  const pct = Math.min(100, Math.round((req.assigned_count / req.episodes_requested) * 100));
  $view.innerHTML = `
    <p><a href="#/requests">← Back to requests</a></p>
    <div class="grid2">
      <div class="panel">
        <h2>Request #${req.id} ${badge(req.status)}</h2>
        <dl>
          <dt>Client</dt><dd>${esc(req.client_name)}</dd>
          <dt>Task</dt><dd>${esc(req.task_name)}</dd>
          <dt>Episodes</dt><dd>${req.assigned_count} of ${req.episodes_requested} assigned
            <div class="progress"><span style="width:${pct}%"></span></div></dd>
          <dt>Deadline</dt><dd>${esc(req.deadline)}</dd>
          <dt>Notes</dt><dd>${esc(req.notes) || '<span class="muted">–</span>'}</dd>
        </dl>
        <div class="actions">${req.allowed_transitions.map((to) =>
          `<button data-to="${to}" class="${to === "rejected" ? "danger" : ""}">${ACTION_LABELS[to] || to}</button>`).join("")}
        </div>
        <div id="reject-box" hidden>
          <label>Reason for rejection <textarea id="reject-note" rows="3" maxlength="2000"></textarea></label>
          <button id="reject-confirm" class="danger">Confirm rejection</button>
        </div>
        <div class="error" id="action-error"></div>
      </div>
      <div class="panel">
        <h3>History</h3>
        <table><tbody>${req.events.map((e) => `
          <tr><td>${e.from_status ? `${badge(e.from_status)} → ` : ""}${badge(e.to_status)}</td>
          <td>${esc(e.actor_name)}<br><span class="muted">${fmtDate(e.created_at)}</span>
          ${e.note ? `<br><em>${esc(e.note)}</em>` : ""}</td></tr>`).join("")}
        </tbody></table>
      </div>
    </div>
    <div class="panel">
      <h3>Assigned episodes (${assigned.length})</h3>
      ${assigned.length === 0 ? '<p class="muted">None yet.</p>' : `
      <div class="scroll"><table>
        <thead><tr><th>Episode</th><th>Robot</th><th>Task</th><th>Recorded</th><th>Duration</th><th>Quality</th>${canEditAssignments ? "<th></th>" : ""}</tr></thead>
        <tbody>${assigned.map((e) => `<tr>
          <td>${esc(e.episode_id)}</td><td>${esc(e.robot_id)}</td><td>${esc(e.task_name)}</td>
          <td>${fmtDate(e.recorded_at)}</td><td>${e.duration_seconds}s</td><td>${badge(e.quality)}</td>
          ${canEditAssignments ? `<td><button class="link" data-unassign="${esc(e.episode_id)}">Remove</button></td>` : ""}
        </tr>`).join("")}</tbody>
      </table></div>`}
      <div class="error" id="unassign-error"></div>
    </div>
    ${canEditAssignments ? '<div class="panel" id="picker"></div>' : ""}
    ${isOps() && req.status === "submitted" ? '<p class="muted">Start work on this request to assign episodes.</p>' : ""}`;

  const actionError = document.getElementById("action-error");
  const doTransition = async (to, note) => {
    await api(`/api/requests/${id}/transitions`, { method: "POST", body: { to_status: to, note: note || null } });
    toast(`Request #${id}: ${to.replace("_", " ")}`);
    await viewRequestDetail(id);
  };
  $view.querySelectorAll("button[data-to]").forEach((btn) =>
    btn.addEventListener("click", guarded(actionError, async () => {
      if (btn.dataset.to === "rejected") {
        document.getElementById("reject-box").hidden = false;
        document.getElementById("reject-note").focus();
        return;
      }
      await doTransition(btn.dataset.to);
    })));
  const confirmReject = document.getElementById("reject-confirm");
  if (confirmReject) confirmReject.addEventListener("click", guarded(actionError, async () =>
    doTransition("rejected", document.getElementById("reject-note").value.trim())));

  const unassignError = document.getElementById("unassign-error");
  $view.querySelectorAll("button[data-unassign]").forEach((btn) =>
    btn.addEventListener("click", guarded(unassignError, async () => {
      await api(`/api/requests/${id}/assignments/${encodeURIComponent(btn.dataset.unassign)}`, { method: "DELETE" });
      await viewRequestDetail(id);
    })));

  if (canEditAssignments) await renderPicker(req);
}

async function renderPicker(req) {
  const box = document.getElementById("picker");
  const taskNames = await api("/api/episodes/task-names");
  const filters = { task_name: taskNames.includes(req.task_name) ? req.task_name : "", quality: "", offset: 0 };

  box.innerHTML = `
    <h3>Assign episodes</h3>
    <div class="row">
      <label>Task name <select id="f-task"><option value="">Any task</option>
        ${taskNames.map((t) => `<option ${t === filters.task_name ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></label>
      <label>Quality <select id="f-quality"><option value="">good or usable</option><option>good</option><option>usable</option></select></label>
    </div>
    <div id="picker-list"></div>
    <div class="error" id="assign-error"></div>
    <div class="actions"><button id="assign-btn" disabled>Assign selected</button><span class="muted" id="picked"></span></div>`;

  const list = document.getElementById("picker-list");
  const assignBtn = document.getElementById("assign-btn");
  const selected = new Set();
  const updateSelected = () => {
    assignBtn.disabled = selected.size === 0;
    document.getElementById("picked").textContent = selected.size ? `${selected.size} selected` : "";
  };

  async function load() {
    const q = new URLSearchParams({ unassigned: "true", limit: 100, offset: filters.offset });
    if (filters.task_name) q.set("task_name", filters.task_name);
    // Only good/usable episodes are assignable, so 'bad' is never offered.
    const qualities = filters.quality ? [filters.quality] : ["good", "usable"];
    const pages = await Promise.all(qualities.map((quality) => api(`/api/episodes?${q}&quality=${quality}`)));
    const items = pages.flatMap((p) => p.items).sort((a, b) => b.recorded_at.localeCompare(a.recorded_at));
    const total = pages.reduce((n, p) => n + p.total, 0);
    list.innerHTML = items.length === 0 ? '<p class="muted">No unassigned episodes match these filters.</p>' : `
      <p class="muted">${total} unassigned episode(s) match${total > items.length ? `, showing ${items.length}` : ""}.</p>
      <div class="scroll"><table>
        <thead><tr><th><input type="checkbox" id="pick-all" aria-label="Select all"></th><th>Episode</th><th>Robot</th><th>Task</th><th>Recorded</th><th>Duration</th><th>Quality</th></tr></thead>
        <tbody>${items.map((e) => `<tr>
          <td><input type="checkbox" data-ep="${esc(e.episode_id)}" ${selected.has(e.episode_id) ? "checked" : ""}></td>
          <td>${esc(e.episode_id)}</td><td>${esc(e.robot_id)}</td><td>${esc(e.task_name)}</td>
          <td>${fmtDate(e.recorded_at)}</td><td>${e.duration_seconds}s</td><td>${badge(e.quality)}</td></tr>`).join("")}
        </tbody></table></div>`;
    list.querySelectorAll("input[data-ep]").forEach((cb) => cb.addEventListener("change", () => {
      cb.checked ? selected.add(cb.dataset.ep) : selected.delete(cb.dataset.ep);
      updateSelected();
    }));
    const all = document.getElementById("pick-all");
    if (all) all.addEventListener("change", () => {
      list.querySelectorAll("input[data-ep]").forEach((cb) => {
        cb.checked = all.checked;
        all.checked ? selected.add(cb.dataset.ep) : selected.delete(cb.dataset.ep);
      });
      updateSelected();
    });
  }

  document.getElementById("f-task").addEventListener("change", (e) => { filters.task_name = e.target.value; load(); });
  document.getElementById("f-quality").addEventListener("change", (e) => { filters.quality = e.target.value; load(); });
  assignBtn.addEventListener("click", guarded(document.getElementById("assign-error"), async () => {
    const res = await api(`/api/requests/${req.id}/assignments`, { method: "POST", body: { episode_ids: [...selected] } });
    toast(`Assigned ${res.assigned.length} episode(s)`);
    await viewRequestDetail(req.id);
  }));
  await load();
}

// ---------- live updates (operators) ----------
// fetch() streaming instead of EventSource so the token travels in a header, not the URL.

function stopStream() {
  if (state.stream) state.stream.abort();
  state.stream = null;
  document.getElementById("live").hidden = true;
}

async function startStream() {
  if (!isOps() || state.stream) return;
  const controller = new AbortController();
  state.stream = controller;
  const live = document.getElementById("live");
  while (state.stream === controller) {
    try {
      const res = await fetch("/api/events", {
        headers: { Authorization: `Bearer ${sessionStorage.getItem(TOKEN_KEY)}` },
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`stream ${res.status}`);
      live.hidden = false;
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("\n");
          if (data) onLiveEvent(JSON.parse(data));
        }
      }
    } catch (err) {
      if (controller.signal.aborted) return;
    }
    live.hidden = true;
    await new Promise((r) => setTimeout(r, 3000)); // reconnect with a small delay
  }
}

let refreshTimer = null;
function onLiveEvent(event) {
  if (event.type === "request_created") toast(`New request #${event.request_id}`);
  else if (event.type === "request_status_changed") toast(`Request #${event.request_id} is now ${event.status.replace("_", " ")}`);
  const [, page, id] = location.hash.split("?")[0].split("/");
  const affectsView = page === "requests" && (!id || Number(id) === event.request_id);
  // Don't re-render a detail page while the operator is mid-selection in the picker.
  const busy = document.querySelector("#picker input[data-ep]:checked, #reject-box:not([hidden])");
  if (affectsView && !busy) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(route, 300);
  }
}

// ---------- router ----------

async function route() {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const [path] = location.hash.slice(1).split("?");
  if (!token) return viewLogin();
  try {
    if (!state.user) {
      state.user = await api("/api/auth/me");
      startStream();
    }
    if (path === "/login" || path === "" || path === "/") {
      location.hash = "#/requests";
      return;
    }
    renderNav();
    const detail = path.match(/^\/requests\/(\d+)$/);
    if (detail) return await viewRequestDetail(Number(detail[1]));
    if (path === "/requests") return await viewRequests();
    if (path === "/new" && isClient()) return viewNewRequest();
    location.hash = "#/requests";
  } catch (err) {
    if (err.status === 401) return;
    $view.innerHTML = `<div class="panel"><p class="error">${esc(err.message)}</p><a href="#/requests">Back to requests</a></div>`;
  }
}

document.getElementById("logout").addEventListener("click", logout);
window.addEventListener("hashchange", route);
route();
