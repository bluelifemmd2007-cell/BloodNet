const state = { hospitals: [], requests: [], selectedId: null, filter: "all", snapshot: null };
const activeStates = new Set(["open", "mobilizing", "partially_fulfilled"]);
const terminalStates = new Set(["fulfilled", "cancelled", "expired"]);
const productNames = { red_cells: "گلبول قرمز", platelets: "پلاکت", plasma: "پلاسما" };
const statusNames = {
  pending_review: "در انتظار بررسی",
  open: "باز",
  mobilizing: "در حال تأمین",
  partially_fulfilled: "تأمین نسبی",
  fulfilled: "تکمیل‌شده",
  cancelled: "لغوشده",
  expired: "منقضی",
};
const progressNames = {
  invited: "دعوت‌شده",
  accepted: "پذیرفته‌شده",
  en_route: "در مسیر مرکز",
  arrived: "رسیده به مرکز",
  screen_pass: "غربالگری موفق",
  screen_fail: "غربالگری ناموفق",
  donated: "اهدا انجام شد",
  no_show: "عدم مراجعه",
};

const el = (id) => document.getElementById(id);
const hospitalToken = () => el("hospital-token").value.trim() || "hospital-token";
const coordinatorToken = () => el("coordinator-token").value.trim() || "coordinator-token";

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  if (options.auth !== false) headers.set("Authorization", `Bearer ${options.token || hospitalToken()}`);
  const response = await fetch(path, { ...options, headers });
  let body = {};
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const detail = body?.error?.message || body?.detail || `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return body;
}

function announce(message, tone = "normal") {
  const status = el("status-message");
  status.textContent = message;
  status.dataset.tone = tone;
}

function stateLabel(status) {
  return statusNames[status] || (status || "نامشخص").replaceAll("_", " ");
}

function matchesFilter(request) {
  if (state.filter === "all") return true;
  if (state.filter === "active") return activeStates.has(request.status);
  return request.status === state.filter;
}

function renderMetrics() {
  const rows = state.requests;
  el("metric-open").textContent = rows.filter((item) => activeStates.has(item.status)).length;
  el("metric-review").textContent = rows.filter((item) => item.status === "pending_review").length;
  el("metric-fulfilled").textContent = rows.filter((item) => item.status === "fulfilled").length;
  el("metric-critical").textContent = rows.filter((item) => item.urgency === "critical" && activeStates.has(item.status)).length;
  el("metric-partial").textContent = rows.filter((item) => item.status === "partially_fulfilled").length;
  el("metric-attention").textContent = rows.filter((item) => ["expired", "pending_review"].includes(item.status)).length;
}

function renderRows() {
  const body = el("request-rows");
  const rows = state.requests.filter(matchesFilter);
  el("result-count").textContent = `${rows.length} درخواست`;
  if (!rows.length) {
    body.innerHTML = '<tr><td class="table-empty" colspan="5">درخواستی با این وضعیت پیدا نشد.</td></tr>';
    return;
  }
  body.replaceChildren(...rows.map((request) => {
    const row = document.createElement("tr");
    row.tabIndex = 0;
    row.setAttribute("aria-pressed", String(request.request_id === state.selectedId));
    if (request.request_id === state.selectedId) row.classList.add("selected");
    const supplied = Number(request.fulfilled || 0) + Number(request.stock_fulfilled || 0);
    const progress = Math.min(100, Math.round((supplied / Math.max(1, Number(request.needed))) * 100));
    row.innerHTML = `
      <td><div class="request-code"></div><div class="request-hospital"></div></td>
      <td></td><td></td>
      <td class="progress-cell"><div class="progress-count"></div><div class="progress-track"><div class="progress-fill"></div></div></td>
      <td><span class="state-pill"></span></td>`;
    row.querySelector(".request-code").textContent = request.request_id;
    row.querySelector(".request-hospital").textContent = request.hospital_id;
    row.children[1].textContent = `${productNames[request.product] || request.product} · ${request.blood_group}`;
    row.children[2].textContent = request.urgency;
    row.querySelector(".progress-count").textContent = `${supplied} / ${request.needed} تحویل‌شده`;
    row.querySelector(".progress-fill").style.width = `${progress}%`;
    const pill = row.querySelector(".state-pill");
    pill.classList.add(request.status);
    pill.textContent = stateLabel(request.status);
    const select = () => { state.selectedId = request.request_id; renderRows(); renderDetail(); };
    row.addEventListener("click", select);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
    return row;
  }));
}

function fillRows(id, rows, columns, emptyMessage) {
  const body = el(id);
  if (!rows.length) {
    body.innerHTML = `<tr><td class="table-empty" colspan="${columns.length}">${emptyMessage}</td></tr>`;
    return;
  }
  body.replaceChildren(...rows.map((item) => {
    const row = document.createElement("tr");
    for (const column of columns) {
      const cell = document.createElement("td");
      const value = column.value(item);
      cell.textContent = value == null || value === "" ? "—" : String(value);
      row.appendChild(cell);
    }
    return row;
  }));
}

function renderOperations(snapshot) {
  fillRows("inventory-rows", snapshot.inventory || [], [
    { value: (item) => item.center_id },
    { value: (item) => productNames[item.product] || item.product },
    { value: (item) => item.blood_group },
    { value: (item) => item.units },
    { value: (item) => item.expires_sim_min },
  ], "موجودی زنده‌ای گزارش نشده است.");
  fillRows("dispatch-rows", snapshot.dispatches || [], [
    { value: (item) => item.request_id },
    { value: (item) => item.center_id },
    { value: (item) => `${item.blood_group} · ${productNames[item.product] || item.product}` },
    { value: (item) => item.units },
    { value: (item) => `${stateLabel(item.status)} · ETA ${item.eta_sim_min ?? "—"}` },
  ], "هنوز ارسالی ثبت نشده است.");
  fillRows("donor-rows", snapshot.invitations || [], [
    { value: (item) => item.request_id },
    { value: (item) => item.donor_id },
    { value: (item) => item.response === "accepted" ? "پذیرفته‌شده" : item.response === "declined" ? "ردشده" : "بی‌پاسخ" },
    { value: (item) => progressNames[item.progress_state] || item.progress_state },
    { value: (item) => item.supply_delivered_sim != null ? `تحویل در ${Number(item.supply_delivered_sim).toFixed(1)}` : item.supply_eta_sim != null ? `ETA ${Number(item.supply_eta_sim).toFixed(1)}` : "—" },
    { value: (item) => item.created_sim },
  ], "هنوز دعوتی ثبت نشده است.");
  fillRows("event-rows", snapshot.events || [], [
    { value: (item) => item.kind },
    { value: (item) => item.request_id },
    { value: (item) => item.created_sim },
  ], "رویدادی برای نمایش نیست.");

  const health = snapshot.services || {};
  const online = Boolean(health.api && health.gateway && health.inventory);
  el("service-dot").className = `service-dot ${online ? "online" : "offline"}`;
  el("service-label").textContent = online ? "سرویس‌ها متصل‌اند" : "اختلال در سرویس";
  el("sim-time").textContent = Number(snapshot.sim_time || 0).toFixed(1);
}

function renderDetail() {
  const request = state.requests.find((item) => item.request_id === state.selectedId);
  el("detail-empty").hidden = Boolean(request);
  el("request-detail").hidden = !request;
  if (!request) return;
  el("detail-title").textContent = `${productNames[request.product] || request.product} · ${request.blood_group}`;
  el("detail-id").textContent = request.request_id;
  const status = el("detail-status");
  status.className = `state-pill ${request.status}`;
  status.textContent = stateLabel(request.status);
  el("detail-needed").textContent = request.needed;
  el("detail-confirmed").textContent = request.confirmed || 0;
  el("detail-enroute").textContent = request.en_route || 0;
  el("detail-arrived").textContent = request.arrived || 0;
  el("detail-donated").textContent = request.donated || 0;
  el("detail-donation-inflight").textContent = request.donations_inflight || 0;
  el("detail-fulfilled").textContent = request.fulfilled || 0;
  el("detail-stock").textContent = request.stock_inflight || 0;
  const due = request.deadline_sim_min == null ? "not set" : `${Math.round(request.deadline_sim_min)} sim min`;
  el("detail-meta").textContent = `فوریت: ${request.urgency} · مهلت شبیه‌سازی: ${due} · موجودی تحویل‌شده: ${request.stock_fulfilled || 0}`;
  el("amend-urgency").value = request.urgency;
  const active = activeStates.has(request.status);
  el("amend-urgency").disabled = !active;
  el("amend-button").disabled = !active;
  el("cancel-button").disabled = terminalStates.has(request.status);
  el("review-actions").hidden = request.status !== "pending_review";
}

async function refreshRequests(quiet = false) {
  const hospitalId = el("hospital-select").value;
  if (!hospitalId) return;
  try {
    const snapshot = await api(`/operations/snapshot?hospital_id=${encodeURIComponent(hospitalId)}`);
    state.snapshot = snapshot;
    state.requests = snapshot.requests || [];
    renderOperations(snapshot);
    if (!state.requests.some((item) => item.request_id === state.selectedId)) state.selectedId = state.requests[0]?.request_id || null;
    renderMetrics();
    renderRows();
    renderDetail();
    if (!quiet) announce(`اطلاعات در دقیقه شبیه‌سازی ${Number(snapshot.sim_time || 0).toFixed(1)} به‌روز شد.`);
  } catch (error) {
    el("service-dot").className = "service-dot offline";
    el("service-label").textContent = "سرویس در دسترس نیست · داده‌ها قدیمی‌اند";
    announce(`به‌روزرسانی انجام نشد: ${error.message}`, "error");
  }
}

async function loadHospitals() {
  try {
    const result = await api("/hospitals", { auth: false });
    state.hospitals = result.hospitals || [];
    const select = el("hospital-select");
    select.replaceChildren(...state.hospitals.map((hospital) => {
      const option = document.createElement("option");
      option.value = hospital.hospital_id;
      option.textContent = `${hospital.hospital_id} · ${hospital.name}`;
      return option;
    }));
    if (state.hospitals.length) select.value = state.hospitals[0].hospital_id;
    await refreshRequests(true);
  } catch (error) {
    announce(error.message, "error");
  }
}

function idempotencyKey() {
  return globalThis.crypto?.randomUUID ? crypto.randomUUID() : `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

el("request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = {
    hospital_id: el("hospital-select").value,
    blood_group: String(form.get("blood_group")),
    product: String(form.get("product")),
    units: Number(form.get("units")),
    urgency: String(form.get("urgency")),
  };
  const note = String(form.get("note") || "").trim();
  if (note) payload.note = note;
  try {
    const result = await api("/requests", { method: "POST", body: JSON.stringify(payload), headers: { "Idempotency-Key": idempotencyKey() } });
    state.selectedId = result.request_id;
    announce(`درخواست ${result.request_id} ثبت شد · ${stateLabel(result.status)}`);
    await refreshRequests(true);
  } catch (error) { announce(error.message, "error"); }
});

el("refresh-button").addEventListener("click", () => refreshRequests());
el("hospital-select").addEventListener("change", () => { state.selectedId = null; refreshRequests(true); });
el("status-filter").addEventListener("change", (event) => { state.filter = event.target.value; renderRows(); });

el("amend-button").addEventListener("click", async () => {
  if (!state.selectedId) return;
  try {
    const result = await api(`/requests/${encodeURIComponent(state.selectedId)}/amend`, { method: "POST", body: JSON.stringify({ urgency: el("amend-urgency").value }) });
    announce(`فوریت به ${result.urgency} تغییر کرد.`);
    await refreshRequests(true);
  } catch (error) { announce(error.message, "error"); }
});

el("cancel-button").addEventListener("click", async () => {
  if (!state.selectedId || !window.confirm("درخواست لغو شود؟")) return;
  try {
    const result = await api(`/requests/${encodeURIComponent(state.selectedId)}/cancel`, { method: "POST", body: "{}" });
    announce(`وضعیت درخواست: ${stateLabel(result.status)}`);
    await refreshRequests(true);
  } catch (error) { announce(error.message, "error"); }
});

async function reviewRequest(approved) {
  if (!state.selectedId) return;
  try {
    const result = await api(`/requests/${encodeURIComponent(state.selectedId)}/approve`, {
      method: "POST",
      body: JSON.stringify({ approved }),
      token: coordinatorToken(),
    });
    announce(approved ? "درخواست تأیید شد." : "درخواست رد شد.");
    await refreshRequests(true);
    state.selectedId = result.request_id;
    renderDetail();
  } catch (error) { announce(error.message, "error"); }
}

el("approve-button").addEventListener("click", () => reviewRequest(true));
el("reject-button").addEventListener("click", () => reviewRequest(false));

loadHospitals();
window.setInterval(() => refreshRequests(true), 10000);
