const state = {
  token: localStorage.getItem("supra_token") || "",
  user: JSON.parse(localStorage.getItem("supra_user") || "null"),
  permissions: [],
  products: [],
  salesmen: [],
  settings: {},
  session: null,
  summary: null,
  selectedSalesmanId: null,
  keypadProduct: null,
  keypadValue: "",
  queue: JSON.parse(localStorage.getItem("supra_offline_queue") || "[]"),
};

const fmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });

function has(permission) {
  return state.permissions.includes(permission);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let err = { message: "Something went wrong." };
    try { err = await res.json(); } catch (_) {}
    throw new Error(err.message || err.error || "Request failed.");
  }
  if ((res.headers.get("Content-Type") || "").includes("text/csv")) return res.text();
  return res.json();
}

function saveSession() {
  if (state.token) localStorage.setItem("supra_token", state.token);
  if (state.user) localStorage.setItem("supra_user", JSON.stringify(state.user));
  localStorage.setItem("supra_offline_queue", JSON.stringify(state.queue));
}

function logout() {
  localStorage.removeItem("supra_token");
  localStorage.removeItem("supra_user");
  state.token = "";
  state.user = null;
  renderLogin();
}

async function boot() {
  if (!state.token || !state.user) return renderLogin();
  try {
    const me = await api("/api/me");
    state.permissions = me.permissions;
    await loadBootstrap();
    renderApp();
  } catch (_) {
    logout();
  }
}

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  state.products = data.products;
  state.salesmen = data.salesmen;
  state.settings = data.settings || {};
  state.session = data.session;
  if (state.session && (has("view_reports") || has("enter_tally"))) {
    await loadSummary();
  }
}

async function loadSummary() {
  if (!state.session) return;
  state.summary = await api(`/api/sessions/${state.session.id}/summary`);
  state.session = state.summary.session;
  state.salesmen = state.summary.salesmen;
  state.products = state.summary.products;
  if (!state.selectedSalesmanId && state.salesmen[0] && state.user?.role !== "salesman") {
    state.selectedSalesmanId = state.salesmen[0].id;
  }
}

function renderLogin() {
  document.querySelector("#app").innerHTML = `
    <main class="login-shell">
      <section class="login-panel">
        <div>
          <p class="eyebrow">Supra Network DMS</p>
          <h1>Dispatch operations</h1>
          <p class="muted">Sign in to open a session, enter warehouse tallies, generate invoices, and export the ITC CSV.</p>
        </div>
        <form id="loginForm" class="stack">
          <label>Username <input name="username" autocomplete="username" /></label>
          <label>Password <input name="password" type="password" autocomplete="current-password" /></label>
          <label>User type
            <select name="role">
              <option value="salesman">Salesman</option>
              <option value="supervisor">Supervisor</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <button class="primary" type="submit">Sign in</button>
          <p id="loginError" class="error"></p>
        </form>
      </section>
    </main>`;
  document.querySelector("#loginForm").addEventListener("submit", login);
}

async function login(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
        role: form.get("role"),
      }),
    });
    state.token = data.token;
    state.user = data.user;
    saveSession();
    await boot();
  } catch (err) {
    document.querySelector("#loginError").textContent = err.message;
  }
}

function renderApp() {
  const status = state.session ? state.session.status : "no session";
  document.querySelector("#app").innerHTML = `
    <div class="app-shell">
      <header class="topbar">
        <div class="brand">
          <div class="brand-mark">S</div>
          <div>
            <strong>Supra Network DMS</strong>
            <span>${state.session ? `Dispatch session ${state.session.session_date}` : "No dispatch session open"}</span>
          </div>
        </div>
        <div class="top-actions">
          <span class="role-pill">${state.user.username} / ${state.user.role.replace("_", " ")}</span>
          <span class="status-pill ${status}">${status.replace("_", " ")}</span>
          <button class="ghost" id="logoutBtn">Sign out</button>
        </div>
      </header>
      <div id="offline" class="offline">No connection - entries saved locally. Will sync when connected.</div>
      <main class="main-grid">
        <aside class="side">
          ${renderSessionPanel()}
          ${renderSalesmen()}
        </aside>
        <section class="workspace">
          ${renderDashboard()}
          ${renderTallyPad()}
          ${renderInvoices()}
          ${renderAudit()}
        </section>
      </main>
      ${renderKeypad()}
    </div>`;
  bindApp();
  updateOfflineBanner();
}

function renderSessionPanel() {
  const totals = state.summary?.totals || { sticks: 0, value: 0, entries: 0 };
  return `
    <section class="panel stack">
      <h2>Session</h2>
      <div class="stats">
        <div class="stat"><span>Sticks</span><strong>${fmt.format(totals.sticks)}</strong></div>
        <div class="stat"><span>Value</span><strong>${money.format(totals.value)}</strong></div>
        <div class="stat"><span>Lines</span><strong>${fmt.format(totals.entries)}</strong></div>
      </div>
      <div class="toolbar-group">
        ${has("open_session") && !state.session ? `<button class="primary" id="openSessionBtn">Open session</button>` : ""}
        ${has("end_session") && state.session && state.session.status !== "uploaded" ? `<button class="danger" id="endSessionBtn">Close day</button>` : ""}
        ${has("end_session") && state.session && state.session.status !== "uploaded" ? `<button class="primary" id="endAndOpenSessionBtn">Close & start next</button>` : ""}
      </div>
      ${has("set_inventory") && state.session?.status !== "uploaded" ? `<button class="secondary" id="seedInventoryBtn">Set opening stock</button>` : ""}
    </section>`;
}

function renderSalesmen() {
  if (!state.session) {
    return `
      <section class="panel stack">
        <h2>Salesmen</h2>
        <p class="muted">Start a dispatch session to choose a salesman and enter bills.</p>
        ${has("open_session") ? `<button class="primary" id="openSessionFromSalesmenBtn">Start session</button>` : ""}
      </section>`;
  }
  return `
    <section class="panel stack">
      <h2>Salesmen</h2>
      <div class="salesman-grid">
        ${state.salesmen.map((s) => `
          <button class="salesman-card ${s.id === state.selectedSalesmanId ? "active" : ""}" data-salesman="${s.id}">
            <span class="code">${s.billing_code}</span>
            <span><strong>${s.name}</strong><br><small>${fmt.format(s.sticks || 0)} sticks / ${s.slots_used || 0} / ${s.slots_total || 0} slots</small></span>
            <span class="tick">${s.complete ? "OK" : ""}</span>
          </button>`).join("")}
      </div>
    </section>`;
}

function renderDashboard() {
  if (!state.session) return "";
  return `
    <section class="panel">
      <div class="toolbar">
        <div>
          <p class="eyebrow">Live supervisor dashboard</p>
          <h2>Dispatch progress</h2>
        </div>
        <button class="ghost" id="refreshBtn">Refresh</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Salesman</th><th>Status</th><th>Sticks</th><th>Value</th><th>Slots</th></tr></thead>
          <tbody>
            ${state.salesmen.map((s) => `
              <tr>
                <td><strong>${s.billing_code}</strong> ${s.name}</td>
                <td>${s.complete ? "Complete" : "Pending"}</td>
                <td>${fmt.format(s.sticks || 0)}</td>
                <td>${money.format(s.value || 0)}</td>
                <td>${s.slots_used || 0} / ${s.slots_total || 0}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </section>`;
}

function renderTallyPad() {
  if (!state.session || !has("enter_tally")) return "";
  const salesman = state.salesmen.find((s) => s.id === state.selectedSalesmanId);
  if (!salesman) {
    return `
      <section class="panel">
        <div class="toolbar">
          <div>
            <p class="eyebrow">Warehouse tally pad</p>
            <h2>Choose a salesman</h2>
          </div>
        </div>
        <p class="muted">Select a salesman from the list to open their product billing pad.</p>
      </section>`;
  }
  const entries = state.summary?.entries || [];
  const byProduct = new Map(entries.filter((e) => e.salesman_id === salesman?.id).map((e) => [e.product_id, e]));
  const totals = [...byProduct.values()].reduce((acc, e) => {
    acc.sticks += Number(e.sticks);
    acc.value += Number(e.line_value);
    acc.boxes += Number(e.sticks) / 10;
    return acc;
  }, { sticks: 0, boxes: 0, value: 0 });
  const readOnly = state.session.status === "uploaded";
  return `
    <section class="panel">
      <div class="toolbar">
        <div>
          <p class="eyebrow">Warehouse tally pad</p>
          <h2>${salesman ? `${salesman.billing_code} - ${salesman.name}` : "Select salesman"}</h2>
        </div>
        <div class="toolbar-group">
          ${has("generate_bill") && salesman && !readOnly ? `<button class="primary" id="generateSalesmanBillBtn">Generate bill</button>` : ""}
          ${readOnly ? `<span class="status-pill uploaded">uploaded</span>` : ""}
        </div>
      </div>
      <div class="product-list">
        ${state.products.map((p) => {
          const entry = byProduct.get(p.id);
          return `
            <button class="product-row" data-product="${p.id}" ${readOnly ? "disabled" : ""}>
              <span class="code">${p.tally_sort_order}</span>
              <span><strong>${p.item_description}</strong><br><small>${p.item_code} / Rs ${Number(p.price_per_stick).toFixed(2)} per stick</small></span>
              <span class="qty">${entry ? `${Number(entry.quantity_m).toFixed(3)}M` : ""}</span>
              <span class="value">${entry ? money.format(entry.line_value) : ""}</span>
            </button>`;
        }).join("")}
      </div>
      <div class="running-total">
        <div class="stat"><span>Sticks</span><strong>${fmt.format(totals.sticks)}</strong></div>
        <div class="stat"><span>Boxes</span><strong>${fmt.format(totals.boxes)}</strong></div>
        <div class="stat"><span>Value</span><strong>${money.format(totals.value)}</strong></div>
      </div>
    </section>`;
}

function renderInvoices() {
  if (!state.session || !(has("generate_invoices") || has("download_csv") || has("generate_bill"))) return "";
  return `
    <section class="panel stack">
      <div class="toolbar">
        <div>
          <p class="eyebrow">Bills and exports</p>
          <h2>Generated invoices and ITC export</h2>
        </div>
        <div class="toolbar-group">
          ${has("generate_invoices") ? `<button class="secondary" id="previewBtn">Preview splits</button>` : ""}
          ${has("generate_invoices") ? `<button class="primary" id="generateBtn">Generate invoices</button>` : ""}
          ${has("download_csv") ? `<button class="ghost" id="csvBtn">Download ITC CSV</button>` : ""}
          ${has("download_csv") ? `<button class="ghost" id="salesmanCsvBtn">Download selected salesman</button>` : ""}
        </div>
      </div>
      <div id="invoiceOutput" class="table-wrap">${renderAllocations()}</div>
    </section>`;
}

function renderAllocations() {
  const allocations = state.summary?.allocations || [];
  if (!allocations.length) return `<p class="muted">No invoices allocated yet.</p>`;
  return `
    <table>
      <thead><tr><th>Salesman</th><th>Invoice</th><th>Total</th><th>Created</th>${has("download_csv") ? "<th>ITC CSV</th>" : ""}</tr></thead>
      <tbody>${allocations.map((a) => `
        <tr>
          <td>${a.billing_code || a.salesman_id}</td>
          <td>${a.invoice_no}</td>
          <td>${money.format(a.total_value)}</td>
          <td>${a.created_at}</td>
          ${has("download_csv") ? `<td><button class="ghost compact" data-order-csv="${a.id}" data-invoice="${a.invoice_no}" data-billing-code="${a.billing_code || a.salesman_id}">Download</button></td>` : ""}
        </tr>`).join("")}</tbody>
    </table>`;
}

function renderAudit() {
  if (!has("view_audit")) return "";
  return `
    <section class="panel stack">
      <div class="toolbar">
        <div>
          <p class="eyebrow">Compliance</p>
          <h2>Audit trail</h2>
        </div>
        <button class="ghost" id="auditBtn">Load audit log</button>
      </div>
      <div id="auditOutput" class="table-wrap"><p class="muted">Load the latest immutable events.</p></div>
    </section>`;
}

function renderKeypad() {
  return `
    <div class="modal" id="keypadModal">
      <section class="keypad">
        <div>
          <p class="eyebrow">Enter M quantity</p>
          <h2 id="keypadTitle">Product</h2>
        </div>
        <div class="display" id="keypadDisplay">0</div>
        <div class="keys">
          ${["1","2","3","4","5","6","7","8","9",".","0","back"].map((k) => `<button data-key="${k}">${k === "back" ? "Del" : k}</button>`).join("")}
          <button class="ghost" data-key="clear">Clear</button>
          <button class="primary wide" data-key="ok">OK</button>
        </div>
        <button class="danger" data-key="delete">Delete entry</button>
        <button class="ghost" data-key="cancel">Cancel</button>
      </section>
    </div>`;
}

function bindApp() {
  document.querySelector("#logoutBtn")?.addEventListener("click", logout);
  document.querySelector("#openSessionBtn")?.addEventListener("click", openSession);
  document.querySelector("#openSessionFromSalesmenBtn")?.addEventListener("click", openSession);
  document.querySelector("#endSessionBtn")?.addEventListener("click", () => endSession(false));
  document.querySelector("#endAndOpenSessionBtn")?.addEventListener("click", () => endSession(true));
  document.querySelector("#seedInventoryBtn")?.addEventListener("click", seedInventory);
  document.querySelector("#refreshBtn")?.addEventListener("click", refresh);
  document.querySelector("#previewBtn")?.addEventListener("click", previewInvoices);
  document.querySelector("#generateBtn")?.addEventListener("click", generateInvoices);
  document.querySelector("#generateSalesmanBillBtn")?.addEventListener("click", generateSalesmanBill);
  document.querySelector("#csvBtn")?.addEventListener("click", downloadCsv);
  document.querySelector("#salesmanCsvBtn")?.addEventListener("click", downloadSalesmanCsv);
  document.querySelector("#auditBtn")?.addEventListener("click", loadAudit);
  document.querySelectorAll("[data-order-csv]").forEach((btn) => btn.addEventListener("click", () => {
    downloadOrderCsv(Number(btn.dataset.orderCsv), btn.dataset.invoice, btn.dataset.billingCode);
  }));
  document.querySelectorAll("[data-salesman]").forEach((btn) => btn.addEventListener("click", () => {
    state.selectedSalesmanId = Number(btn.dataset.salesman);
    renderApp();
  }));
  document.querySelectorAll("[data-product]").forEach((btn) => btn.addEventListener("click", () => openKeypad(Number(btn.dataset.product))));
  document.querySelectorAll("[data-key]").forEach((btn) => btn.addEventListener("click", () => keypad(btn.dataset.key)));
  window.removeEventListener("keydown", handleKeyboardEntry);
  window.addEventListener("keydown", handleKeyboardEntry);
}

async function openSession() {
  await api("/api/sessions/open", { method: "POST", body: JSON.stringify({}) });
  await loadBootstrap();
  renderApp();
}

async function endSession(openNext = false) {
  const message = openNext
    ? "Close this day and start the next session? Current tally entries will become read-only."
    : "Close this day? Current tally entries will become read-only.";
  if (!confirm(message)) return;
  state.summary = await api(`/api/sessions/${state.session.id}/end`, {
    method: "POST",
    body: JSON.stringify({ open_next: openNext }),
  });
  if (openNext) {
    state.session = state.summary.session;
    state.salesmen = state.summary.salesmen;
    state.products = state.summary.products;
  } else {
    state.summary = null;
    state.session = null;
    await loadBootstrap();
  }
  renderApp();
}

async function seedInventory() {
  const openingSticks = Number(state.settings.default_opening_sticks || 0);
  const balances = state.products.map((p) => ({ product_id: p.id, opening_sticks: openingSticks }));
  await api(`/api/sessions/${state.session.id}/inventory`, { method: "POST", body: JSON.stringify({ balances }) });
  alert(`Opening inventory set to ${fmt.format(openingSticks)} sticks for each active product.`);
}

async function refresh() {
  await flushQueue();
  await loadSummary();
  renderApp();
}

function openKeypad(productId) {
  state.keypadProduct = state.products.find((p) => p.id === productId);
  state.keypadValue = "";
  document.querySelector("#keypadTitle").textContent = state.keypadProduct.item_description;
  document.querySelector("#keypadDisplay").textContent = "0";
  document.querySelector("#keypadModal").classList.add("show");
}

function keypad(key) {
  if (key === "cancel") return closeKeypad();
  if (key === "delete") return deleteKeypadEntry();
  if (key === "clear") state.keypadValue = "";
  else if (key === "back") state.keypadValue = state.keypadValue.slice(0, -1);
  else if (key === "ok") return submitKeypad();
  else if (key === "." && state.keypadValue.includes(".")) return;
  else state.keypadValue += key;
  document.querySelector("#keypadDisplay").textContent = state.keypadValue || "0";
}

function handleKeyboardEntry(event) {
  const modal = document.querySelector("#keypadModal");
  if (!modal?.classList.contains("show")) return;

  const keyMap = {
    Enter: "ok",
    Escape: "cancel",
    Backspace: "back",
    Delete: "delete",
  };
  const key = keyMap[event.key] || event.key;
  if (/^[0-9.]$/.test(key) || ["ok", "cancel", "back", "delete"].includes(key)) {
    event.preventDefault();
    keypad(key);
  }
}

function closeKeypad() {
  document.querySelector("#keypadModal").classList.remove("show");
  state.keypadProduct = null;
  state.keypadValue = "";
}

async function submitKeypad() {
  const quantity = state.keypadValue.startsWith(".") ? `0${state.keypadValue}` : state.keypadValue;
  if (!quantity || Number(quantity) <= 0) return alert("Enter a number like 0.2 or 1.5");
  const payload = {
    session_id: state.session.id,
    salesman_id: state.selectedSalesmanId,
    product_id: state.keypadProduct.id,
    quantity_m: quantity,
  };
  closeKeypad();
  if (!navigator.onLine) {
    state.queue.push(payload);
    saveSession();
    updateOfflineBanner();
    return;
  }
  try {
    const data = await api("/api/tally", { method: "POST", body: JSON.stringify(payload) });
    state.summary = data.summary;
    state.session = data.summary.session;
    state.salesmen = data.summary.salesmen;
    state.products = data.summary.products;
    renderApp();
  } catch (err) {
    alert(err.message);
  }
}

async function deleteKeypadEntry() {
  if (!state.keypadProduct) return;
  const payload = {
    session_id: state.session.id,
    salesman_id: state.selectedSalesmanId,
    product_id: state.keypadProduct.id,
  };
  closeKeypad();
  try {
    const data = await api("/api/tally", { method: "DELETE", body: JSON.stringify(payload) });
    state.summary = data.summary;
    state.session = data.summary.session;
    state.salesmen = data.summary.salesmen;
    state.products = data.summary.products;
    renderApp();
  } catch (err) {
    alert(err.message);
  }
}

async function flushQueue() {
  if (!navigator.onLine || !state.queue.length) return;
  const pending = [...state.queue];
  state.queue = [];
  for (const item of pending) {
    try {
      const data = await api("/api/tally", { method: "POST", body: JSON.stringify(item) });
      state.summary = data.summary;
    } catch (err) {
      alert(`Offline entry failed: ${err.message}`);
    }
  }
  saveSession();
}

function updateOfflineBanner() {
  const banner = document.querySelector("#offline");
  if (!banner) return;
  banner.classList.toggle("show", !navigator.onLine || state.queue.length > 0);
  banner.textContent = !navigator.onLine
    ? "No connection - entries saved locally. Will sync when connected."
    : `${state.queue.length} offline entr${state.queue.length === 1 ? "y" : "ies"} waiting to sync.`;
}

async function previewInvoices() {
  try {
    await loadSummary();
    const output = document.querySelector("#invoiceOutput");
    const allocations = state.summary?.allocations || [];
    if (allocations.length) {
      const bySalesman = new Map();
      for (const allocation of allocations) {
        const key = allocation.salesman_id;
        if (!bySalesman.has(key)) {
          bySalesman.set(key, {
            label: `${allocation.billing_code || allocation.salesman_id} - ${allocation.salesman_name || ""}`.trim(),
            allocations: [],
            total: 0,
          });
        }
        const item = bySalesman.get(key);
        item.allocations.push(allocation);
        item.total += Number(allocation.total_value || 0);
      }
      output.innerHTML = `
        <table>
          <thead><tr><th>Salesman</th><th>Generated slots</th><th>Totals</th><th>Session total</th></tr></thead>
          <tbody>${[...bySalesman.values()].map((item) => `
            <tr>
              <td>${item.label}</td>
              <td>${item.allocations.length}</td>
              <td>${item.allocations.map((a) => `${a.invoice_no}: ${money.format(a.total_value)}`).join("<br>")}</td>
              <td>${money.format(item.total)}</td>
            </tr>`).join("")}</tbody>
        </table>`;
      return;
    }

    const data = await api(`/api/sessions/${state.session.id}/invoice-preview`);
    if (!data.preview.length) {
      output.innerHTML = `<p class="muted">No generated bills or pending splits for this session yet.</p>`;
      return;
    }
    output.innerHTML = `
      <table>
        <thead><tr><th>Salesman</th><th>Groups</th><th>Totals</th></tr></thead>
        <tbody>${data.preview.map((item) => `
          <tr>
            <td>${item.salesman.billing_code} - ${item.salesman.name}</td>
            <td>${item.groups.length}</td>
            <td>${item.groups.map((g) => money.format(g.total_value)).join("<br>")}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  } catch (err) {
    alert(err.message);
  }
}

async function generateInvoices() {
  try {
    await api(`/api/sessions/${state.session.id}/generate-invoices`, { method: "POST", body: "{}" });
    await loadSummary();
    renderApp();
  } catch (err) {
    alert(err.message);
  }
}

async function generateSalesmanBill() {
  const salesman = state.salesmen.find((s) => s.id === state.selectedSalesmanId);
  if (!salesman) return;
  try {
    state.summary = await api(`/api/sessions/${state.session.id}/salesmen-bills/${salesman.id}`, {
      method: "POST",
      body: "{}",
    });
    state.session = state.summary.session;
    state.salesmen = state.summary.salesmen;
    state.products = state.summary.products;
    renderApp();
  } catch (err) {
    alert(err.message);
  }
}

async function downloadEntriesCsv() {
  try {
    const csv = await api(`/api/sessions/${state.session.id}/entries-csv`, { headers: { Accept: "text/csv" } });
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `ENTERED_DATA_${state.session.session_date}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(err.message);
  }
}

function safeFilenamePart(value) {
  return String(value || "export").replace(/[^a-z0-9_-]+/gi, "_").replace(/^_+|_+$/g, "") || "export";
}

async function downloadTextFile(path, filename) {
  const csv = await api(path, { headers: { Accept: "text/csv" } });
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function downloadCsv() {
  try {
    await downloadTextFile(
      `/api/sessions/${state.session.id}/csv`,
      `ORDER_IMPORT_DATA_${state.session.session_date}.csv`,
    );
  } catch (err) {
    alert(err.message);
  }
}

async function downloadSalesmanCsv() {
  const salesman = state.salesmen.find((s) => s.id === state.selectedSalesmanId);
  if (!salesman) return alert("Select a salesman first.");
  try {
    await downloadTextFile(
      `/api/sessions/${state.session.id}/csv?salesman_id=${salesman.id}`,
      `ORDER_IMPORT_DATA_${state.session.session_date}_${safeFilenamePart(salesman.billing_code)}.csv`,
    );
  } catch (err) {
    alert(err.message);
  }
}

async function downloadOrderCsv(allocationId, invoiceNo, billingCode) {
  if (!allocationId) return;
  try {
    await downloadTextFile(
      `/api/sessions/${state.session.id}/csv?allocation_id=${allocationId}`,
      `ORDER_IMPORT_DATA_${state.session.session_date}_${safeFilenamePart(billingCode)}_${safeFilenamePart(invoiceNo)}.csv`,
    );
  } catch (err) {
    alert(err.message);
  }
}

async function loadAudit() {
  try {
    const data = await api("/api/audit");
    document.querySelector("#auditOutput").innerHTML = `
      <table>
        <thead><tr><th>Time</th><th>User</th><th>Event</th><th>Entity</th></tr></thead>
        <tbody>${data.logs.map((l) => `<tr><td>${l.created_at}</td><td>${l.username || ""}</td><td>${l.event_type}</td><td>${l.entity_type} #${l.entity_id}</td></tr>`).join("")}</tbody>
      </table>`;
  } catch (err) {
    alert(err.message);
  }
}

window.addEventListener("online", async () => {
  await flushQueue();
  if (state.session) await loadSummary();
  renderApp();
});
window.addEventListener("offline", updateOfflineBanner);

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
boot();
