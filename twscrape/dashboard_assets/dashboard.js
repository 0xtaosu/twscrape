const tokenMeta = document.querySelector('meta[name="twscrape-token"]');
const token = tokenMeta ? tokenMeta.content : "";

const POLL_MS = 10000;
const CHECK_POLL_MS = 1500;
const STATUS_LABELS = {
  ready: "可用",
  cooling: "冷却中",
  attention: "需处理",
  disabled: "已停用",
};

const state = {
  accounts: [],
  summary: null,
  check: null,
  expanded: null,
  editing: null,
  editingInitialActive: false,
  pendingDelete: null,
  openDeleteOnEditClose: false,
  loading: true,
  loadError: "",
};

const els = {
  loadError: document.querySelector("#loadError"),
  viewAttention: document.querySelector("#viewAttention"),
  statReady: document.querySelector("#statReady"),
  statCooling: document.querySelector("#statCooling"),
  statAttention: document.querySelector("#statAttention"),
  statDisabled: document.querySelector("#statDisabled"),
  rows: document.querySelector("#accountRows"),
  empty: document.querySelector("#emptyState"),
  emptyTitle: document.querySelector("#emptyTitle"),
  emptyHint: document.querySelector("#emptyHint"),
  search: document.querySelector("#searchInput"),
  filter: document.querySelector("#statusFilter"),
  updated: document.querySelector("#updatedAt"),
  checkAll: document.querySelector("#checkAllButton"),
  checkPanel: document.querySelector("#checkPanel"),
  checkStatus: document.querySelector("#checkStatus"),
  checkFill: document.querySelector("#checkFill"),
  cancelCheck: document.querySelector("#cancelCheckButton"),
  sessionUser: document.querySelector("#sessionUser"),
  logout: document.querySelector("#logoutButton"),
  addButton: document.querySelector("#addButton"),
  dialog: document.querySelector("#addDialog"),
  form: document.querySelector("#addForm"),
  username: document.querySelector("#cookieUsername"),
  cookies: document.querySelector("#cookieValue"),
  formError: document.querySelector("#formError"),
  submit: document.querySelector("#submitAccount"),
  closeDialog: document.querySelector("#closeDialog"),
  cancelDialog: document.querySelector("#cancelDialog"),
  editDialog: document.querySelector("#editDialog"),
  editForm: document.querySelector("#editForm"),
  editUsernameDisplay: document.querySelector("#editUsernameDisplay"),
  editActive: document.querySelector("#editActive"),
  editCookies: document.querySelector("#editCookies"),
  editProxyStatus: document.querySelector("#editProxyStatus"),
  editProxySource: document.querySelector("#editProxySource"),
  editProxyKeep: document.querySelector("#editProxyKeep"),
  editProxySet: document.querySelector("#editProxySet"),
  editProxyClear: document.querySelector("#editProxyClear"),
  editProxyLabel: document.querySelector("#editProxyLabel"),
  editProxy: document.querySelector("#editProxy"),
  editFormError: document.querySelector("#editFormError"),
  editSubmit: document.querySelector("#submitEdit"),
  closeEditDialog: document.querySelector("#closeEditDialog"),
  cancelEditDialog: document.querySelector("#cancelEditDialog"),
  deleteAccountButton: document.querySelector("#deleteAccountButton"),
  deleteDialog: document.querySelector("#deleteDialog"),
  deleteForm: document.querySelector("#deleteForm"),
  deleteSummary: document.querySelector("#deleteSummary"),
  deleteConfirm: document.querySelector("#deleteConfirmUsername"),
  deleteFormError: document.querySelector("#deleteFormError"),
  deleteSubmit: document.querySelector("#submitDelete"),
  closeDeleteDialog: document.querySelector("#closeDeleteDialog"),
  cancelDeleteDialog: document.querySelector("#cancelDeleteDialog"),
  accountsHeading: document.querySelector("#accountsHeading"),
  toast: document.querySelector("#toast"),
};

for (const [name, node] of Object.entries(els)) {
  if (!node) throw new Error(`DOM 节点缺失: ${name}`);
}

function apiHeaders(withJson = false) {
  const headers = { "X-Twscrape-Token": token };
  if (withJson) headers["Content-Type"] = "application/json";
  return headers;
}

async function api(path, options = {}) {
  const withJson = options.body !== undefined;
  const response = await fetch(path, {
    ...options,
    headers: {
      ...apiHeaders(withJson),
      ...(options.headers || {}),
    },
  });
  const raw = await response.text();
  let body = {};
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = { error: raw };
    }
  }
  if (response.status === 401) {
    location.replace("/login");
  }
  if (!response.ok) {
    throw new Error(body.error || `请求失败 (${response.status})`);
  }
  return body;
}

function showToast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", Boolean(isError));
  els.toast.classList.toggle("ok", !isError);
  els.toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.remove("show"), 2600);
}

function loginLabel(method) {
  if (method === "cookies") return "Cookie";
  if (method === "password") return "密码";
  return method || "—";
}

function relativeTime(value) {
  if (!value) return "从未";
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return "—";
  const seconds = Math.round((parsed - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "刚刚";
  if (abs < 3600) return `${Math.round(abs / 60)} 分钟前`;
  if (abs < 86400) return `${Math.round(abs / 3600)} 小时前`;
  return `${Math.round(abs / 86400)} 天前`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 60) return `${value} 秒后`;
  if (value < 3600) return `${Math.ceil(value / 60)} 分钟后`;
  return `${Math.ceil(value / 3600)} 小时后`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function isAdminDisabled(account) {
  return Boolean(account.manual_disabled) || account.status === "disabled";
}

function needsCookieRepair(account) {
  return (
    !account.has_session ||
    account.attention_reason === "session_missing" ||
    account.attention_reason === "auth_error"
  );
}

function proxyHostPort(display) {
  if (!display) return "";
  const idx = String(display).indexOf("://");
  return idx >= 0 ? String(display).slice(idx + 3) : String(display);
}

function lockRecovery(account) {
  const count = Number(account.lock_count) || 0;
  if (!count) return { text: "当前没有本地锁", title: "" };
  const wait = formatDuration(account.next_unlock_in_seconds);
  const queues = Array.isArray(account.locked_queues)
    ? account.locked_queues.join(", ")
    : "";
  const when = account.next_unlock_at ? formatDateTime(account.next_unlock_at) : "";
  const text = wait ? `${count} 个队列 · ${wait}` : `${count} 个队列`;
  const title = [queues && `队列: ${queues}`, when && `最早恢复: ${when}`]
    .filter(Boolean)
    .join(" · ");
  return { text, title };
}

function filteredAccounts() {
  const query = els.search.value.trim().toLowerCase();
  const filter = els.filter.value;
  return state.accounts.filter((account) => {
    const username = String(account.username || "").toLowerCase();
    const matchQuery = !query || username.includes(query);
    const matchFilter =
      filter === "all" ||
      account.status === filter ||
      (filter === "attention" && account.needs_attention);
    return matchQuery && matchFilter;
  });
}

function appendText(parent, tag, text, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  parent.appendChild(node);
  return node;
}

function labeledCell(label, className) {
  const td = document.createElement("td");
  td.dataset.label = label;
  if (className) td.className = className;
  return td;
}

function renderDetailCard(title, items, emptyText) {
  const card = document.createElement("div");
  card.className = "detail-card";
  appendText(card, "h3", title);
  if (!items.length) {
    appendText(card, "p", emptyText, "detail-empty");
    return card;
  }
  const list = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    appendText(li, "span", item.label);
    appendText(li, "span", item.value);
    list.appendChild(li);
  }
  card.appendChild(list);
  return card;
}

function renderDetailRow(account) {
  const tr = document.createElement("tr");
  tr.className = "detail-row";
  const td = document.createElement("td");
  td.colSpan = 6;
  const grid = document.createElement("div");
  grid.className = "detail-grid";

  const queues = Array.isArray(account.requests_by_queue)
    ? account.requests_by_queue.map((item) => ({
        label: String(item.queue || "—"),
        value: Number(item.count || 0).toLocaleString("zh-CN"),
      }))
    : [];
  const locks = Array.isArray(account.active_locks)
    ? account.active_locks.map((item) => ({
        label: String(item.queue || "—"),
        value: formatDateTime(item.unlock_at),
      }))
    : [];

  const lockCard = renderDetailCard("本地锁", locks, "当前没有本地锁");
  const recovery = lockRecovery(account);
  if (Number(account.lock_count) > 0) {
    if (recovery.text) {
      const note = appendText(lockCard, "p", recovery.text, "detail-empty");
      if (recovery.title) note.title = recovery.title;
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "link-action";
    reset.dataset.action = "reset";
    reset.dataset.user = String(account.username || "");
    reset.textContent = "清除本地锁";
    lockCard.appendChild(reset);
  }

  const errorCard = document.createElement("div");
  errorCard.className = "detail-card";
  appendText(errorCard, "h3", "错误信息");
  appendText(
    errorCard,
    "p",
    account.error_message || "没有错误信息",
    account.error_message ? "detail-error" : "detail-empty"
  );

  grid.append(
    renderCheckCard(account),
    renderDetailCard("各队列请求", queues, "还没有请求记录"),
    lockCard,
    errorCard
  );
  td.appendChild(grid);
  tr.appendChild(td);
  return tr;
}

function checkProbeValue(probe) {
  const latency = Number(probe.latency_ms);
  const text =
    probe.status === "ok"
      ? probe.detail || "正常"
      : probe.reason_label || probe.reason || "失败";
  return Number.isFinite(latency) && latency > 0 ? `${text} · ${latency}ms` : text;
}

function probeValueClass(probe) {
  if (probe.status === "ok") return "";
  // 跳过是"没轮到它"，不是失败，别跟真正的红色错误混在一起
  return probe.status === "skipped" ? "detail-empty" : "detail-error";
}

function renderCheckCard(account) {
  const card = document.createElement("div");
  card.className = "detail-card";
  appendText(card, "h3", "检测结果");

  if (account.checking) {
    appendText(card, "p", "正在检测…", "detail-empty");
    return card;
  }

  const check = account.last_check;
  if (!check) {
    appendText(card, "p", "还没有检测过", "detail-empty");
    return card;
  }

  const probes = Array.isArray(check.probes) ? check.probes : [];
  if (probes.length) {
    const list = document.createElement("ul");
    for (const probe of probes) {
      const li = document.createElement("li");
      appendText(li, "span", probe.probe_label || probe.probe);
      const value = appendText(li, "span", checkProbeValue(probe), probeValueClass(probe));
      if (probe.detail) value.title = probe.detail;
      list.appendChild(li);
    }
    card.appendChild(list);
  }

  if (!check.ok && check.detail) {
    appendText(card, "p", check.detail, "detail-error");
  }
  if (check.applied) {
    appendText(card, "p", "已按检测结果自动停用", "detail-empty");
  }
  if (check.finished_at) {
    const stamp = appendText(card, "p", `检测于 ${relativeTime(check.finished_at)}`, "detail-empty");
    stamp.title = formatDateTime(check.finished_at);
  }
  return card;
}

function renderProxyCell(account) {
  const td = labeledCell("代理", "cell-proxy");
  const source = account.proxy_source;
  const accountDisplay = account.proxy_display;
  const effective = account.effective_proxy_display;

  if (source === "env") {
    if (effective) {
      const code = appendText(td, "code", proxyHostPort(effective), "proxy-addr");
      code.title = effective;
    } else {
      appendText(td, "span", "—", "muted");
    }
    appendText(td, "span", "全局 TWS_PROXY", "proxy-source");
    return td;
  }

  if (accountDisplay) {
    const code = appendText(td, "code", proxyHostPort(accountDisplay), "proxy-addr");
    code.title = accountDisplay;
    return td;
  }

  appendText(td, "span", "—", "muted");
  if (account.has_proxy) td.title = "代理地址无法安全显示";
  return td;
}

function sessionMeta(account) {
  const method = loginLabel(account.login_method);
  if (account.attention_reason === "auth_error" || account.error_message) {
    return `${method} · 会话已失效`;
  }
  if (account.has_session) return `${method} · Cookie 已添加`;
  return `${method} · 缺少 Cookie`;
}

function syncAttentionFilter() {
  els.viewAttention.setAttribute(
    "aria-pressed",
    els.filter.value === "attention" ? "true" : "false"
  );
}

function renderAccounts() {
  const accounts = filteredAccounts();
  els.rows.replaceChildren();
  syncAttentionFilter();

  if (!accounts.length) {
    els.empty.hidden = false;
    if (state.loading && !state.accounts.length) {
      els.emptyTitle.textContent = "正在加载账号";
      els.emptyHint.textContent = "正在读取本机账号池";
      return;
    }
    if (state.loadError && !state.accounts.length) {
      els.emptyTitle.textContent = "无法加载账号";
      els.emptyHint.textContent = state.loadError;
      return;
    }
    const hasAny = state.accounts.length > 0;
    els.emptyTitle.textContent = hasAny ? "没有匹配的账号" : "还没有账号";
    els.emptyHint.textContent = hasAny
      ? "试试其他筛选或搜索"
      : "添加 Cookie 后才会出现在列表中";
    return;
  }

  els.empty.hidden = true;
  const fragment = document.createDocumentFragment();

  for (const account of accounts) {
    const username = String(account.username || "");
    const tr = document.createElement("tr");
    tr.className = "account-row";
    tr.dataset.username = username;
    tr.tabIndex = 0;
    tr.setAttribute("aria-expanded", account.username === state.expanded ? "true" : "false");
    if (account.status === "attention" || account.needs_attention) {
      tr.classList.add("row-attention");
    } else if (account.status === "cooling") {
      tr.classList.add("row-cooling");
    }
    if (state.expanded === username) tr.classList.add("is-expanded");

    const nameTd = labeledCell("账号", "cell-account");
    const cell = document.createElement("div");
    cell.className = "account-cell";
    appendText(cell, "span", state.expanded === username ? "▾" : "▸", "chevron");
    const identity = document.createElement("div");
    identity.className = "account-identity";
    appendText(identity, "span", `@${username}`, "account-name");
    const meta = appendText(identity, "span", sessionMeta(account), "account-meta");
    if (needsCookieRepair(account)) {
      const repair = document.createElement("button");
      repair.type = "button";
      repair.className = "link-action";
      repair.dataset.action = "add_cookie";
      repair.dataset.user = username;
      repair.textContent = "修复 Cookie";
      meta.append(" · ");
      meta.appendChild(repair);
    }
    cell.appendChild(identity);
    nameTd.appendChild(cell);

    const statusTd = labeledCell("状态", "cell-status");
    const statusKey = STATUS_LABELS[account.status] ? account.status : "disabled";
    appendText(
      statusTd,
      "span",
      account.status_label || STATUS_LABELS[statusKey] || account.status,
      `status status-${statusKey}`
    );

    if (account.checking) {
      appendText(statusTd, "span", "检测中…", "check-badge is-running");
    } else if (account.last_check) {
      const check = account.last_check;
      const badge = appendText(
        statusTd,
        "span",
        check.ok ? "检测正常" : check.reason_label || "检测失败",
        `check-badge ${check.ok ? "is-ok" : "is-bad"}`
      );
      if (check.detail) badge.title = check.detail;
    }

    const proxyTd = renderProxyCell(account);

    const reqTd = labeledCell("总请求", "cell-requests");
    reqTd.textContent = Number(account.total_requests || 0).toLocaleString("zh-CN");

    const usedTd = labeledCell("最后使用", "cell-meta");
    usedTd.textContent = relativeTime(account.last_used);
    if (account.last_used) usedTd.title = formatDateTime(account.last_used);

    const actionTd = labeledCell("操作", "cell-actions");
    const actions = document.createElement("div");
    actions.className = "row-actions";

    const toggle = document.createElement("button");
    toggle.type = "button";
    if (isAdminDisabled(account)) {
      toggle.dataset.action = "enable";
      toggle.className = "action-enable";
      toggle.textContent = "启用";
    } else {
      toggle.dataset.action = "disable";
      toggle.className = "action-disable";
      toggle.textContent = "停用";
    }
    toggle.dataset.user = username;
    actions.appendChild(toggle);

    const check = document.createElement("button");
    check.type = "button";
    check.dataset.action = "check";
    check.dataset.user = username;
    check.className = "action-check";
    check.textContent = account.checking ? "检测中" : "检测";
    check.disabled = Boolean(account.checking);
    check.setAttribute("aria-label", `检测 @${username}`);
    actions.appendChild(check);

    const manage = document.createElement("button");
    manage.type = "button";
    manage.dataset.action = "manage";
    manage.dataset.user = username;
    manage.className = "action-manage";
    manage.textContent = "管理";
    manage.setAttribute("aria-haspopup", "dialog");
    manage.setAttribute("aria-label", `管理 @${username}`);
    actions.appendChild(manage);
    actionTd.appendChild(actions);

    tr.append(nameTd, statusTd, proxyTd, reqTd, usedTd, actionTd);
    fragment.appendChild(tr);
    if (state.expanded === username) {
      fragment.appendChild(renderDetailRow(account));
    }
  }

  els.rows.appendChild(fragment);
}

function renderSummary() {
  const summary = state.summary;
  const ready = Number(summary?.ready || 0);
  const attention = Number(summary?.attention || 0);
  const cooling = Number(summary?.cooling || 0);
  const disabled = Number(summary?.disabled || 0);

  els.statReady.textContent = summary ? String(ready) : "—";
  els.statAttention.textContent = summary ? String(attention) : "—";
  els.statCooling.textContent = summary ? String(cooling) : "—";
  els.statDisabled.textContent = summary ? String(disabled) : "—";

  els.statReady.classList.toggle("is-ok", ready > 0);
  els.statAttention.classList.toggle("is-bad", attention > 0);
  els.statCooling.classList.toggle("is-warn", cooling > 0);

  els.loadError.hidden = !state.loadError;
  els.loadError.textContent = state.loadError || "";
  syncAttentionFilter();
}

function clearCookieForm() {
  els.form.reset();
  els.cookies.value = "";
  els.username.value = "";
  els.formError.hidden = true;
  els.formError.textContent = "";
}

function openCookieDialog(username = "") {
  clearCookieForm();
  if (username) els.username.value = username;
  if (typeof els.dialog.showModal === "function") {
    els.dialog.showModal();
  }
  (username ? els.cookies : els.username).focus();
}

function closeCookieDialog() {
  clearCookieForm();
  if (els.dialog.open) els.dialog.close();
}

function selectedProxyMode() {
  if (els.editProxySet.checked) return "set";
  if (els.editProxyClear.checked) return "clear";
  return "keep";
}

function syncProxyInputVisibility() {
  const isSet = selectedProxyMode() === "set";
  els.editProxyLabel.hidden = !isSet;
  els.editProxy.disabled = !isSet;
  if (!isSet) els.editProxy.value = "";
}

function setEditFormError(message) {
  els.editFormError.textContent = message || "";
  els.editFormError.hidden = !message;
}

function setDeleteFormError(message) {
  els.deleteFormError.textContent = message || "";
  els.deleteFormError.hidden = !message;
}

function clearEditSecrets() {
  els.editCookies.value = "";
  els.editProxy.value = "";
}

function resetEditForm() {
  els.editForm.reset();
  els.editUsernameDisplay.textContent = "";
  els.editProxyStatus.textContent = "未配置";
  els.editProxyStatus.classList.remove("is-configured");
  els.editProxySource.hidden = true;
  els.editProxySource.textContent = "";
  els.editProxyKeep.checked = true;
  els.editSubmit.disabled = false;
  els.editSubmit.textContent = "保存更改";
  els.deleteAccountButton.disabled = false;
  clearEditSecrets();
  setEditFormError("");
  syncProxyInputVisibility();
}

function onEditDialogClosed() {
  const shouldOpenDelete = state.openDeleteOnEditClose;
  const openingDelete = state.pendingDelete;
  state.openDeleteOnEditClose = false;
  resetEditForm();
  state.editing = null;
  if (shouldOpenDelete && openingDelete && openingDelete.username) {
    window.setTimeout(() => {
      if (!els.deleteDialog.open) openDeleteDialog(openingDelete);
    }, 0);
  }
}

function closeEditDialog() {
  if (els.editDialog.open) {
    els.editDialog.close();
  } else {
    onEditDialogClosed();
  }
}

function openEditDialog(username) {
  const account = state.accounts.find((item) => item.username === username);
  if (!account) return;
  resetEditForm();
  state.editing = String(account.username || "");
  const adminEnabled = !isAdminDisabled(account);
  state.editingInitialActive = adminEnabled;
  els.editUsernameDisplay.textContent = `@${state.editing}`;
  els.editActive.checked = adminEnabled;
  if (account.proxy_display) {
    els.editProxyStatus.textContent = account.proxy_display;
    els.editProxyStatus.classList.add("is-configured");
  } else if (account.has_proxy) {
    els.editProxyStatus.textContent = "已配置，但无法安全显示";
    els.editProxyStatus.classList.remove("is-configured");
  } else {
    els.editProxyStatus.textContent = "未配置";
    els.editProxyStatus.classList.remove("is-configured");
  }
  if (account.proxy_source === "env") {
    els.editProxySource.hidden = false;
    els.editProxySource.textContent = account.effective_proxy_display
      ? `全局 TWS_PROXY 覆盖账号代理，当前代理地址为 ${account.effective_proxy_display}`
      : "全局 TWS_PROXY 覆盖账号代理，地址无法显示";
  } else {
    els.editProxySource.hidden = true;
    els.editProxySource.textContent = "";
  }
  els.editProxyKeep.checked = true;
  syncProxyInputVisibility();
  if (typeof els.editDialog.showModal === "function") {
    els.editDialog.showModal();
  }
  els.editActive.focus();
}

function updateDeleteSubmitState() {
  const expected = state.pendingDelete ? state.pendingDelete.username : "";
  const typed = String(els.deleteConfirm.value || "");
  els.deleteSubmit.disabled = !expected || typed !== expected;
}

function resetDeleteForm() {
  els.deleteForm.reset();
  els.deleteSummary.textContent = "";
  els.deleteConfirm.value = "";
  els.deleteSubmit.textContent = "确认删除";
  setDeleteFormError("");
  updateDeleteSubmitState();
}

function onDeleteDialogClosed() {
  resetDeleteForm();
  state.pendingDelete = null;
}

function closeDeleteDialog() {
  if (els.deleteDialog.open) {
    els.deleteDialog.close();
  } else {
    onDeleteDialogClosed();
  }
}

function openDeleteDialog(account) {
  const username = String(account.username || "");
  if (!username) return;
  resetDeleteForm();
  state.pendingDelete = {
    username,
    total_requests: Number(account.total_requests || 0),
  };
  const count = state.pendingDelete.total_requests.toLocaleString("zh-CN");
  els.deleteSummary.textContent = `将永久删除 @${username}，该账号累计 ${count} 次请求。此操作无法撤销。`;
  updateDeleteSubmitState();
  if (typeof els.deleteDialog.showModal === "function") {
    els.deleteDialog.showModal();
  }
  els.deleteConfirm.focus();
}

function startDeleteFromEdit() {
  const username = state.editing;
  if (!username) return;
  const account = state.accounts.find((item) => item.username === username);
  state.pendingDelete = {
    username: String((account && account.username) || username),
    total_requests: Number((account && account.total_requests) || 0),
  };
  if (els.editDialog.open) {
    state.openDeleteOnEditClose = true;
    els.editDialog.close();
  } else {
    state.openDeleteOnEditClose = false;
    openDeleteDialog(state.pendingDelete);
  }
}

function clearSensitiveInputs() {
  els.cookies.value = "";
  clearEditSecrets();
}

function setSync(text, isError = false) {
  els.updated.textContent = text;
  els.updated.classList.toggle("is-error", Boolean(isError));
}

// 检测中轮询到 1.5s，慢响应会盖住新响应 —— 每轮领一个号，回来发现号过期就丢掉
let pollSeq = 0;

async function loadAccounts(seq = ++pollSeq) {
  if (!state.accounts.length) state.loading = true;
  setSync("正在刷新");
  try {
    const data = await api("/admin/accounts");
    if (seq !== pollSeq) return;
    state.accounts = Array.isArray(data.accounts) ? data.accounts : [];
    state.summary = data.summary || null;
    state.loadError = "";
    state.loading = false;
    if (state.expanded && !state.accounts.some((item) => item.username === state.expanded)) {
      state.expanded = null;
    }
    renderSummary();
    renderAccounts();
    const stamp = data.updated_at ? new Date(data.updated_at) : new Date();
    setSync(
      `已更新 ${stamp.toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })}`
    );
  } catch (error) {
    if (seq !== pollSeq) return;
    state.loadError = error.message || "无法读取账号池";
    state.loading = false;
    renderSummary();
    renderAccounts();
    setSync("刷新失败", true);
    showToast(state.loadError, true);
  }
}

function checkIsActive() {
  const run = state.check;
  return Boolean(run && (run.state === "queued" || run.state === "running"));
}

function checkHeadline(run, done, total) {
  if (run.state === "cancelled") return `已取消 ${done}/${total}`;
  if (run.state === "done") return `检测完成 ${done}/${total}`;
  return `检测中 ${done}/${total}`;
}

function renderCheckPanel() {
  const run = state.check;
  if (!run) {
    els.checkPanel.hidden = true;
    return;
  }

  const summary = run.summary || {};
  const total = Number(summary.total || 0);
  const done = Number(summary.done || 0);
  const active = checkIsActive();

  els.checkPanel.hidden = false;
  els.checkPanel.classList.toggle("is-active", active);
  els.checkFill.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
  els.cancelCheck.hidden = !active;
  els.checkStatus.textContent = `${checkHeadline(run, done, total)} · 正常 ${Number(
    summary.ok || 0
  )} · 异常 ${Number(summary.failed || 0)}`;
}

async function loadChecks(seq = ++pollSeq) {
  try {
    const data = await api("/admin/checks");
    if (seq !== pollSeq) return;
    state.check = data.run || null;
  } catch {
    // 检测只是辅助信息，读不到就沿用上一次的结果，不打断账号列表
  }
  if (seq !== pollSeq) return;
  renderCheckPanel();
}

let pollTimer = null;

function scheduleTick() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(tick, checkIsActive() ? CHECK_POLL_MS : POLL_MS);
}

async function tick() {
  clearTimeout(pollTimer);
  const seq = ++pollSeq;
  try {
    await Promise.all([loadAccounts(seq), loadChecks(seq)]);
  } finally {
    scheduleTick();
  }
}

async function startCheck(usernames, button) {
  if (checkIsActive()) {
    showToast("已有检测在进行中", true);
    return;
  }
  if (button) button.disabled = true;
  try {
    const data = await api("/admin/checks", {
      method: "POST",
      body: JSON.stringify({ usernames }),
    });
    pollSeq += 1; // POST 的结果最新，作废所有在途轮询响应
    state.check = data.run || null;
    renderCheckPanel();
    showToast(`已开始检测 ${Number(state.check?.summary?.total || 0)} 个账号`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button) button.disabled = false;
    await tick();
  }
}

async function cancelCheck() {
  const run = state.check;
  if (!run) return;
  els.cancelCheck.disabled = true;
  try {
    await api(`/admin/checks/${encodeURIComponent(run.id)}/cancel`, {
      method: "POST",
      body: "{}",
    });
    showToast("已取消，正在进行的检测会先跑完");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    els.cancelCheck.disabled = false;
    await tick();
  }
}

async function runAccountAction(action, username, button) {
  if (!username) return;
  if (action === "manage") {
    openEditDialog(username);
    return;
  }
  if (action === "add_cookie") {
    openCookieDialog(username);
    return;
  }
  if (action === "check") {
    await startCheck([username], button);
    return;
  }
  if (action === "reset") {
    const ok = window.confirm(
      "只会清除这个账号在本机的限流锁，不能绕过 X 侧的真实限流。确定继续？"
    );
    if (!ok) return;
  }
  if (button) button.disabled = true;
  try {
    if (action === "enable" || action === "disable") {
      await api(`/admin/accounts/${encodeURIComponent(username)}`, {
        method: "PATCH",
        body: JSON.stringify({ active: action === "enable" }),
      });
      showToast(action === "enable" ? "账号已启用" : "账号已停用");
    } else if (action === "reset") {
      await api(`/admin/accounts/${encodeURIComponent(username)}/reset-locks`, {
        method: "POST",
        body: "{}",
      });
      showToast("本地锁已清除");
    }
    await loadAccounts();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}

function toggleExpanded(username) {
  state.expanded = state.expanded === username ? null : username;
  renderAccounts();
  for (const row of els.rows.querySelectorAll("tr.account-row")) {
    if (row.dataset.username === username) {
      row.focus();
      break;
    }
  }
}

els.search.addEventListener("input", renderAccounts);
els.filter.addEventListener("change", () => {
  renderSummary();
  renderAccounts();
});
els.addButton.addEventListener("click", () => openCookieDialog());
els.checkAll.addEventListener("click", () => startCheck("all", els.checkAll));
els.cancelCheck.addEventListener("click", cancelCheck);
els.closeDialog.addEventListener("click", closeCookieDialog);
els.cancelDialog.addEventListener("click", closeCookieDialog);
els.dialog.addEventListener("close", clearCookieForm);
els.dialog.addEventListener("cancel", clearCookieForm);

els.closeEditDialog.addEventListener("click", closeEditDialog);
els.cancelEditDialog.addEventListener("click", closeEditDialog);
els.editDialog.addEventListener("close", onEditDialogClosed);
els.editDialog.addEventListener("cancel", resetEditForm);
els.deleteAccountButton.addEventListener("click", startDeleteFromEdit);

els.editForm.addEventListener("change", (event) => {
  if (event.target && event.target.name === "editProxyMode") {
    const wasSet = !els.editProxyLabel.hidden;
    syncProxyInputVisibility();
    if (!wasSet && selectedProxyMode() === "set") {
      els.editProxy.focus();
    }
  }
});

els.editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = state.editing;
  if (!username) return;
  setEditFormError("");
  const proxyMode = selectedProxyMode();
  const cookies = String(els.editCookies.value || "").trim();
  const proxy = String(els.editProxy.value || "").trim();
  if (proxyMode === "set" && !proxy) {
    setEditFormError("请输入代理地址");
    els.editProxy.focus();
    return;
  }
  const payload = {
    proxy_mode: proxyMode,
  };
  const nextActive = Boolean(els.editActive.checked);
  if (nextActive !== Boolean(state.editingInitialActive)) {
    payload.active = nextActive;
  }
  if (cookies) payload.cookies = cookies;
  if (proxyMode === "set") payload.proxy = proxy;
  if (payload.active === undefined && !cookies && proxyMode === "keep") {
    setEditFormError("没有需要更新的字段");
    return;
  }
  els.editSubmit.disabled = true;
  els.deleteAccountButton.disabled = true;
  const original = els.editSubmit.textContent;
  els.editSubmit.textContent = "保存中…";
  try {
    await api(`/admin/accounts/${encodeURIComponent(username)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    closeEditDialog();
    showToast("账号已更新");
    await loadAccounts();
  } catch (error) {
    setEditFormError(error.message || "保存失败");
  } finally {
    clearEditSecrets();
    els.editSubmit.disabled = false;
    els.deleteAccountButton.disabled = false;
    els.editSubmit.textContent = original;
  }
});

els.closeDeleteDialog.addEventListener("click", closeDeleteDialog);
els.cancelDeleteDialog.addEventListener("click", closeDeleteDialog);
els.deleteDialog.addEventListener("close", onDeleteDialogClosed);
els.deleteDialog.addEventListener("cancel", resetDeleteForm);
els.deleteConfirm.addEventListener("input", updateDeleteSubmitState);

els.deleteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const username = state.pendingDelete ? state.pendingDelete.username : "";
  const confirmUsername = String(els.deleteConfirm.value || "");
  if (!username) return;
  if (confirmUsername !== username) {
    setDeleteFormError("请输入完整账号名称确认删除");
    updateDeleteSubmitState();
    els.deleteConfirm.focus();
    return;
  }
  setDeleteFormError("");
  els.deleteSubmit.disabled = true;
  const original = els.deleteSubmit.textContent;
  els.deleteSubmit.textContent = "删除中…";
  try {
    await api(`/admin/accounts/${encodeURIComponent(username)}`, {
      method: "DELETE",
      body: JSON.stringify({ confirm_username: confirmUsername }),
    });
    if (state.expanded === username) state.expanded = null;
    if (state.editing === username) state.editing = null;
    closeDeleteDialog();
    showToast("账号已删除");
    await loadAccounts();
  } catch (error) {
    setDeleteFormError(error.message || "删除失败");
    updateDeleteSubmitState();
  } finally {
    els.deleteSubmit.textContent = original;
    updateDeleteSubmitState();
  }
});

els.viewAttention.addEventListener("click", () => {
  els.filter.value = "attention";
  renderAccounts();
  els.accountsHeading.scrollIntoView({ behavior: "smooth", block: "start" });
});

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.formError.hidden = true;
  els.formError.textContent = "";
  els.submit.disabled = true;
  const original = els.submit.textContent;
  els.submit.textContent = "提交中…";
  const username = String(els.username.value || "").trim();
  const cookies = String(els.cookies.value || "").trim();
  try {
    await api("/admin/accounts", {
      method: "POST",
      body: JSON.stringify({ username, cookies }),
    });
    clearCookieForm();
    if (els.dialog.open) els.dialog.close();
    showToast("账号已添加");
    await loadAccounts();
  } catch (error) {
    els.formError.textContent = error.message;
    els.formError.hidden = false;
  } finally {
    els.cookies.value = "";
    els.submit.disabled = false;
    els.submit.textContent = original;
  }
});

els.rows.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (button) {
    event.preventDefault();
    event.stopPropagation();
    runAccountAction(button.dataset.action, button.dataset.user, button);
    return;
  }
  if (event.target.closest(".row-actions")) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  const row = event.target.closest("tr.account-row");
  if (row) toggleExpanded(row.dataset.username);
});

els.rows.addEventListener("keydown", (event) => {
  if (event.target.closest("button")) return;
  const row = event.target.closest("tr.account-row");
  if (!row) return;
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    toggleExpanded(row.dataset.username);
  }
});

async function loadSession() {
  try {
    const data = await api("/auth/session");
    const username = String(data.username || "");
    if (!username) {
      els.sessionUser.hidden = true;
      els.sessionUser.textContent = "";
      return;
    }
    els.sessionUser.textContent = username;
    els.sessionUser.hidden = false;
  } catch {
    els.sessionUser.hidden = true;
    els.sessionUser.textContent = "";
  }
}

els.logout.addEventListener("click", async () => {
  els.logout.disabled = true;
  const original = els.logout.textContent;
  els.logout.textContent = "退出中…";
  try {
    await api("/auth/logout", {
      method: "POST",
      body: "{}",
    });
    location.replace("/login");
  } catch (error) {
    if (error.message === "Authentication required") {
      location.replace("/login");
      return;
    }
    showToast(error.message || "退出失败", true);
    els.logout.disabled = false;
    els.logout.textContent = original;
  }
});

window.addEventListener("pagehide", clearSensitiveInputs);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) clearSensitiveInputs();
});

loadSession();
renderAccounts();
tick();
