const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  csrf: "", busy: false, approvalId: null, config: {}, activities: [],
  queue: [], discard: false,
  servers: [], active: "", defaultServer: "", serversError: "", refreshing: false,
  conversations: [], abortController: null,
  auth: { logged_in: false, email: "" },
  token: localStorage.getItem("vaspilot.token") || "",
  editingServer: null,
  editingSnapshot: null,
  projects: [], activeProject: null,
  terminals: new Map(), activeTerminal: "", terminalDockOpen: false,
  // Non-stream action awaiting approval (server-to-server transfer); when set,
  // the approval dialog answers /api/action/approve instead of /api/approve.
  pendingAction: null,
};

async function request(path, options = {}) {
  const init = { method: options.method || "GET", headers: {} };
  if (state.token) init.headers["X-Auth-Token"] = state.token;
  if (options.body) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify({ ...options.body, csrf: state.csrf });
  }
  const response = await fetch(path, init);
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(data.error || "请求失败");
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
}

// Theme: three-state cycle dark -> light -> auto, persisted for the
// pre-paint script in index.html (it only reads localStorage, it never
// writes). "auto" resolves through matchMedia and follows system changes.
const THEME_ORDER = ["dark", "light", "auto"];

function resolveTheme(theme) {
  return (theme === "auto" && window.matchMedia)
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme;
}

function applyTheme(theme) {
  localStorage.setItem("vaspilot.theme", theme);
  document.documentElement.dataset.theme = resolveTheme(theme);
  updateThemeIcon(theme);
  $("#themeBtn").title = theme === "auto"
    ? "主题：跟随系统（点击切换）"
    : `主题：${theme === "dark" ? "深色" : "浅色"}（点击切换）`;
}

function updateThemeIcon(theme) {
  const icon = $("#themeIcon");
  if (theme === "light") {
    icon.innerHTML = '<circle cx="12" cy="12" r="4.5"/><path d="M12 2.5v2m0 15v2M4.3 4.3l1.4 1.4m12.6 12.6 1.4 1.4M2.5 12h2m15 0h2M4.3 19.7l1.4-1.4M18.3 5.7l1.4-1.4"/>';
  } else if (theme === "auto") {
    icon.innerHTML = '<path d="M12 3a9 9 0 1 0 8.5 12.2A7 7 0 0 1 12 3Z"/>';
  } else {
    icon.innerHTML = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>';
  }
}

if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", event => {
    if (localStorage.getItem("vaspilot.theme") === "auto") {
      document.documentElement.dataset.theme = event.matches ? "dark" : "light";
    }
  });
}

// Wallpaper: static image or looping video background, persisted in
// localStorage (data URLs survive restarts; remote URLs are used as-is).
const WP_KEY = "vaspilot.wallpaper";
const WP_DIM_KEY = "vaspilot.wallpaperDim";

function readWallpaper() {
  try {
    const raw = localStorage.getItem(WP_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function currentWallpaperDim() {
  return parseFloat(localStorage.getItem(WP_DIM_KEY) || "0.35");
}

function saveWallpaper(wp, dim) {
  localStorage.setItem(WP_KEY, JSON.stringify(wp));
  localStorage.setItem(WP_DIM_KEY, String(dim));
  renderWallpaper(wp, dim);
}

function renderWallpaper(wp, dim) {
  const has = wp && wp.mode !== "none" && wp.src;
  document.body.style.setProperty("--wp-dim", String(dim ?? 0.35));
  let layer = $("#wallpaperLayer");
  if (!has) {
    if (layer) layer.remove();
    delete document.body.dataset.wallpaper;
    return;
  }
  if (!layer) {
    layer = document.createElement("div");
    layer.id = "wallpaperLayer";
    layer.className = "wallpaper-layer";
    layer.setAttribute("aria-hidden", "true");
    document.body.prepend(layer);
  }
  layer.innerHTML = "";
  const el = document.createElement(wp.mode === "video" ? "video" : "img");
  if (wp.mode === "video") {
    el.autoplay = true; el.muted = true; el.loop = true; el.playsInline = true;
  } else {
    el.alt = "";
  }
  el.src = wp.src;
  el.addEventListener("error", () => {
    toast("壁纸加载失败，已恢复为无壁纸");
    saveWallpaper({ mode: "none", src: "" }, currentWallpaperDim());
    const sel = $("#wallpaperMode");
    if (sel) sel.value = "none";
  });
  layer.appendChild(el);
  document.body.dataset.wallpaper = wp.mode;
}

function addActivity(title, detail = "") {
  state.activities.unshift({ title, detail, time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) });
  state.activities = state.activities.slice(0, 5);
  $("#activityList").innerHTML = state.activities.map(item => `
    <div class="activity-item"><i></i><div><strong>${escapeHtml(item.title)}</strong>${escapeHtml(item.detail)} · ${item.time}</div></div>
  `).join("");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function setBusy(value) {
  state.busy = value;
  // The composer stays enabled while the model works so the user can keep
  // typing and send more messages; they are queued and drained after the
  // current turn finishes.
}

function showMessages() {
  $("#welcome").hidden = true;
}

function appendMessage(role, content, rich = false) {
  showMessages();
  const node = document.createElement("div");
  node.className = `message ${role}`;
  if (role === "user") {
    node.innerHTML = `<div class="message-body">${escapeHtml(content)}</div>`;
  } else {
    node.innerHTML = `<div class="avatar">V</div><div><div class="message-meta">VASPILOT</div><div class="message-body"></div></div>`;
    const body = node.querySelector(".message-body");
    if (rich) renderRichText(body, content);
    else body.textContent = content ?? "";
  }
  $("#messages").appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
  return node;
}

function addTyping() {
  showMessages();
  const node = document.createElement("div");
  node.className = "message";
  node.id = "typing";
  node.innerHTML = `<div class="avatar">V</div><div><div class="message-meta">正在获取服务器证据</div><div class="typing"><i></i><i></i><i></i></div></div>`;
  $("#messages").appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
}

function removeTyping() { $("#typing")?.remove(); }

async function streamRequest(path, body, handlers) {
  // POST + streaming SSE reader. The csrf token goes in the body like every
  // other POST; EventSource cannot POST, so we read the fetch body manually.
  const controller = new AbortController();
  state.abortController = controller;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(state.token ? { "X-Auth-Token": state.token } : {}) },
      body: JSON.stringify({ ...body, csrf: state.csrf }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = "请求失败";
      try { detail = (await response.json()).error || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        let event = "message", dataText = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataText += line.slice(5).trim();
        }
        if (!dataText) continue;
        let parsed;
        try { parsed = JSON.parse(dataText); } catch { continue; }
        if (handlers[event]) await handlers[event](parsed);
      }
    }
  } finally {
    if (state.abortController === controller) state.abortController = null;
  }
}

function createAssistantNode() {
  // One mutable node per streamed turn: reasoning box + tool status lines
  // + the streaming answer text.
  showMessages();
  const node = document.createElement("div");
  node.className = "message assistant";
  node.innerHTML = `<div class="avatar">V</div><div><div class="message-meta">VASPILOT</div><div class="stream-content"></div></div>`;
  const content = node.querySelector(".stream-content");
  const reasoning = document.createElement("div");
  reasoning.className = "reasoning-box";
  reasoning.hidden = true;
  reasoning.innerHTML = `<div class="reasoning-label">思考过程</div><div class="reasoning-text"></div>`;
  const tools = document.createElement("div");
  tools.className = "tool-list";
  const text = document.createElement("div");
  text.className = "message-body stream-text";
  content.append(reasoning, tools, text);
  $("#messages").appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
  return { node, reasoningText: reasoning.querySelector(".reasoning-text"), tools, text };
}

function sendRequest(message) {
  setBusy(true);
  const view = createAssistantNode();
  let assistant = "";
  streamRequest("/api/chat", { message }, {
    reasoning(data) {
      view.reasoningText.parentElement.hidden = false;
      view.reasoningText.textContent += data.delta || "";
      view.node.scrollIntoView({ behavior: "smooth", block: "end" });
    },
    delta(data) {
      assistant += data.delta || "";
      view.text.textContent = assistant;
      view.node.scrollIntoView({ behavior: "smooth", block: "end" });
    },
    tool_start(data) {
      const line = document.createElement("div");
      line.className = "tool-line running";
      line.dataset.name = data.name || "";
      line.textContent = `⚙ 正在调用 ${data.name || "工具"}…`;
      view.tools.appendChild(line);
      addActivity("调用工具", data.name || "");
    },
    tool_end(data) {
      const name = data.name || "";
      const line = [...view.tools.querySelectorAll(".tool-line.running")].find(l => l.dataset.name === name);
      const detail = data.ok
        ? (data.summary ? ` · ${data.summary}` : "")
        : ` · ${data.error || "失败"}`;
      if (line) {
        line.classList.remove("running");
        line.classList.add(data.ok ? "ok" : "fail");
        line.textContent = `${data.ok ? "✓" : "✗"} ${name}${detail}`;
      }
      addActivity(data.ok ? "工具完成" : "工具失败", `${name}${data.ok && data.summary ? " · " + data.summary : ""}`);
      // A model-initiated transfer was approved and runs in the background:
      // poll until it finishes and announce the outcome.
      if (data.action_id) pollTransfer(data.action_id);
    },
    approval(data) {
      // The stream pauses; the reader stays open and continues once the
      // user answers through /api/approve.
      state.approvalId = data.id;
      $("#approvalTitle").textContent = data.title;
      $("#approvalDescription").textContent = data.description;
      $("#approveBtn").textContent = "确认执行";
      $("#approvalDialog").showModal();
      setBusy(false);
    },
    error(data) {
      showStreamError(view, assistant, data.message || "未知错误");
    },
    done() {
      // Stream finished: re-render the final answer so code blocks become
      // copyable and remote paths become clickable file chips.
      if (assistant.trim()) {
        view.text.textContent = "";
        renderRichText(view.text, assistant);
      }
    },
  })
    .then(finishTurn)
    .catch(error => {
      // AbortError means the chat was reset or a history conversation was
      // loaded mid-turn; drop the partial reply silently.
      if (error.name !== "AbortError") {
        showStreamError(view, assistant, error.message || "未知错误");
      }
      finishTurn();
    });
}

function showStreamError(view, assistant, message) {
  if (!assistant) {
    view.text.textContent = `暂时无法完成：${message}`;
    return;
  }
  const note = document.createElement("div");
  note.className = "stream-error";
  note.textContent = message;
  view.node.appendChild(note);
}

function finishTurn() {
  setBusy(false);
  if (state.queue.length) sendRequest(state.queue.shift());
}

function sendMessage(text) {
  const message = String(text ?? "").trim();
  if (!message) return;
  $("#prompt").value = "";
  resizePrompt();
  appendMessage("user", message);
  if (state.busy) {
    state.queue.push(message);
    toast("已排队，回复结束后自动发送");
    return;
  }
  sendRequest(message);
}

async function decideApproval(approved) {
  $("#approvalDialog").close();
  setBusy(true);
  try {
    if (state.pendingAction) {
      // Non-stream action (server-to-server transfer): resolve it through
      // /api/action/approve. A transfer can run for a long time, so the UI
      // is released immediately and pollTransfer announces the outcome.
      const action = state.pendingAction;
      state.pendingAction = null;
      setBusy(false);
      try {
        await request("/api/action/approve", { method: "POST", body: { action_id: action.actionId, approve: approved } });
        if (approved) pollTransfer(action.actionId);
        else toast("已取消传输");
      } catch (error) {
        toast(`确认操作失败：${error.message}`);
      }
      return;
    }
    // The server resolves the pending approval and the SAME streaming
    // connection continues emitting events (tool_end, delta, done).
    await request("/api/approve", { method: "POST", body: { id: state.approvalId, approved } });
    state.approvalId = null;
  } catch (error) {
    toast(`确认操作失败：${error.message}`);
  }
}

async function pollTransfer(actionId) {
  for (;;) {
    const data = await request("/api/action/poll", { method: "POST", body: { action_id: actionId } });
    if (!data.done) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      continue;
    }
    const result = data.result || {};
    if (result.ok) {
      toast("传输完成 ✓");
    } else {
      toast(`传输失败：${result.error || result.output || "未知错误"}`);
    }
    return;
  }
}

function activeServerEntry() {
  return state.servers.find(server => server.name === state.active) || null;
}

function updateRoute() {
  const active = activeServerEntry();
  const name = active ? active.name : (state.active || "—");
  $("#routeServer").textContent = name;
  $("#routeState").textContent = active
    ? (active.connected ? "链路正常 · 安全复用中" : "未连接 · 点击「连接」完成 SSH 认证")
    : (state.serversError ? `无法读取服务器列表：${state.serversError}` : "等待服务器列表");
  $("#serverDot").className = "server-dot" + (active ? (active.connected ? " connected" : " disconnected") : "");
  $("#welcomeKicker").textContent = `VASP · SLURM · ${name.toUpperCase()}`;
  if (active && active.root && !$("#calcPath").value) $("#calcPath").value = active.root + "/";
}

function renderServers() {
  const list = $("#serverList");
  if (state.serversError && !state.servers.length) {
    list.innerHTML = `<div class="activity-empty">无法读取服务器列表：${escapeHtml(state.serversError)}</div>`;
    updateRoute();
    return;
  }
  if (!state.servers.length) {
    list.innerHTML = `<div class="activity-empty">还没有服务器，点 ＋ 添加</div>`;
    updateRoute();
    return;
  }
  list.innerHTML = state.servers.map(server => {
    const dot = server.connected ? "connected" : "disconnected";
    const active = server.name === state.active ? " active" : "";
    const badge = server.name === state.defaultServer ? `<span class="default-badge">默认</span>` : "";
    return `
      <div class="server-row${active}" data-name="${escapeHtml(server.name)}">
        <span class="status-dot ${dot}"></span>
        <div class="server-main">
          <div class="server-name">${escapeHtml(server.name)} ${badge}</div>
          <div class="server-target">${escapeHtml(server.target)} · ${escapeHtml(server.root || "主目录")}</div>
        </div>
        <button class="row-edit" type="button" title="编辑属性" data-edit="${escapeHtml(server.name)}">✎</button>
        <button class="row-terminal" type="button" title="在底部打开人工终端（不进入智能体上下文）" data-terminal="${escapeHtml(server.name)}">&gt;_</button>
        <button class="server-del" type="button" title="从目录中删除" data-remove="${escapeHtml(server.name)}">×</button>
      </div>`;
  }).join("");
  list.querySelectorAll(".server-row").forEach(row => row.addEventListener("click", () => selectServer(row.dataset.name)));
  list.querySelectorAll(".row-edit").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    const entry = state.servers.find(server => server.name === button.dataset.edit);
    if (entry) openServerDialog(entry);
  }));
  list.querySelectorAll(".server-del").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    removeServer(button.dataset.remove);
  }));
  list.querySelectorAll(".row-terminal").forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    openManualTerminal(button.dataset.terminal);
  }));
  updateRoute();
}

async function refreshServers(showToast = false) {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const data = await request("/api/servers");
    state.servers = data.servers || [];
    state.active = data.active || "";
    state.serversError = data.error || "";
    if (showToast && data.error) toast(data.error);
    renderServers();
  } catch (error) {
    state.serversError = error.message;
    renderServers();
    if (showToast) toast(error.message);
  } finally {
    state.refreshing = false;
  }
}

async function selectServer(name) {
  try {
    const data = await request("/api/servers/select", { method: "POST", body: { name } });
    state.active = data.active;
    const entry = state.servers.find(server => server.name === data.active);
    if (entry && entry.root) $("#calcPath").value = entry.root + "/";
    renderServers();
    toast(`已切换到服务器 ${data.active}，对话已保留`);
    refreshServers();
  } catch (error) {
    toast(error.message);
  }
}

const TERMINAL_HEIGHT_KEY = "vaspilot.terminalHeight";

function renderTerminalDock() {
  const dock = $("#terminalDock");
  const tabs = $("#terminalTabs");
  const hasSessions = state.terminals.size > 0;
  const visible = hasSessions && state.terminalDockOpen;
  dock.hidden = !visible;
  $("#terminalToggleBtn").classList.toggle("active", hasSessions);
  $("#terminalToggleBtn").setAttribute("aria-expanded", String(visible));
  $("#terminalToggleBtn").title = hasSessions
    ? (visible ? "收起人工终端（Ctrl+`）" : "展开人工终端（Ctrl+`）")
    : "打开人工终端（Ctrl+`）";

  tabs.replaceChildren();
  for (const [name, iframe] of state.terminals) {
    const tab = document.createElement("div");
    tab.className = "terminal-tab" + (name === state.activeTerminal ? " active" : "");

    const select = document.createElement("button");
    select.type = "button";
    select.className = "terminal-tab-select";
    select.role = "tab";
    select.setAttribute("aria-selected", String(name === state.activeTerminal));
    select.textContent = name;
    select.title = `切换到 ${name} 人工终端`;
    select.addEventListener("click", () => activateManualTerminal(name));

    const close = document.createElement("button");
    close.type = "button";
    close.className = "terminal-tab-close";
    close.textContent = "×";
    close.title = `关闭 ${name} 终端并断开会话`;
    close.setAttribute("aria-label", close.title);
    close.addEventListener("click", () => closeManualTerminal(name));

    tab.append(select, close);
    tabs.appendChild(tab);
    iframe.hidden = name !== state.activeTerminal;
  }
}

function activateManualTerminal(name) {
  if (!state.terminals.has(name)) return;
  state.activeTerminal = name;
  state.terminalDockOpen = true;
  renderTerminalDock();
}

function openManualTerminal(name) {
  if (!name) {
    toast("请先选择服务器");
    return;
  }
  let iframe = state.terminals.get(name);
  if (!iframe) {
    iframe = document.createElement("iframe");
    iframe.className = "terminal-frame";
    iframe.title = `${name} 人工终端`;
    iframe.src = "/terminal.html?embedded=1&server=" + encodeURIComponent(name);
    iframe.referrerPolicy = "no-referrer";
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
    $("#terminalFrames").appendChild(iframe);
    state.terminals.set(name, iframe);
    addActivity("打开人工终端", name);
  }
  activateManualTerminal(name);
}

function closeManualTerminal(name) {
  const iframe = state.terminals.get(name);
  if (!iframe) return;
  iframe.remove();
  state.terminals.delete(name);
  if (state.activeTerminal === name) {
    state.activeTerminal = state.terminals.keys().next().value || "";
  }
  if (!state.terminals.size) state.terminalDockOpen = false;
  renderTerminalDock();
  addActivity("关闭人工终端", name);
}

function toggleTerminalDock() {
  if (!state.terminals.size) {
    openManualTerminal(state.active);
    return;
  }
  state.terminalDockOpen = !state.terminalDockOpen;
  renderTerminalDock();
}

function initTerminalDock() {
  const dock = $("#terminalDock");
  const handle = $("#terminalResizeHandle");
  const savedHeight = parseInt(localStorage.getItem(TERMINAL_HEIGHT_KEY) || "300", 10);

  function setHeight(height, persist = false) {
    const maximum = Math.max(220, Math.floor(window.innerHeight * 0.7));
    const value = Math.max(170, Math.min(maximum, height));
    dock.style.height = `${value}px`;
    if (persist) localStorage.setItem(TERMINAL_HEIGHT_KEY, String(value));
  }

  if (Number.isFinite(savedHeight)) setHeight(savedHeight);
  $("#terminalToggleBtn").addEventListener("click", toggleTerminalDock);
  $("#terminalMinimizeBtn").addEventListener("click", () => {
    state.terminalDockOpen = false;
    renderTerminalDock();
  });

  let startY = 0;
  let startHeight = 0;
  handle.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    startY = event.clientY;
    startHeight = dock.getBoundingClientRect().height;
    handle.setPointerCapture(event.pointerId);
    dock.classList.add("resizing");
    document.body.classList.add("terminal-resizing");
  });
  handle.addEventListener("pointermove", event => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    setHeight(startHeight + startY - event.clientY);
  });
  function finishResize(event) {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    handle.releasePointerCapture(event.pointerId);
    dock.classList.remove("resizing");
    document.body.classList.remove("terminal-resizing");
    localStorage.setItem(TERMINAL_HEIGHT_KEY, String(Math.round(dock.getBoundingClientRect().height)));
  }
  handle.addEventListener("pointerup", finishResize);
  handle.addEventListener("pointercancel", finishResize);
  handle.addEventListener("keydown", event => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const delta = event.key === "ArrowUp" ? 20 : -20;
    setHeight(dock.getBoundingClientRect().height + delta, true);
  });
  window.addEventListener("resize", () => {
    if (!dock.hidden) setHeight(dock.getBoundingClientRect().height);
  });
  renderTerminalDock();
}

async function addServer(payload) {
  const data = await request("/api/servers/add", { method: "POST", body: payload });
  state.servers = data.servers || state.servers;
  state.active = data.active || state.active;
  renderServers();
  refreshServers();
  return data;
}

async function removeServer(name) {
  if (!confirm(`确定从目录中删除服务器 ${name}？远端文件不受影响。`)) return;
  try {
    const data = await request("/api/servers/remove", { method: "POST", body: { name } });
    state.servers = data.servers || [];
    state.active = data.active || "";
    renderServers();
    toast(`已删除服务器 ${name}`);
    refreshServers();
  } catch (error) {
    toast(error.message);
  }
}

async function disconnectServer() {
  if (!state.active) { toast("没有活动服务器"); return; }
  try {
    await request("/api/servers/disconnect", { method: "POST", body: { name: state.active } });
    toast(`已断开 ${state.active}`);
    refreshServers();
  } catch (error) {
    toast(error.message);
  }
}

async function quickAction(operation, args = []) {
  if (state.busy) return;
  setBusy(true);
  addActivity("执行快捷操作", operation);
  try {
    const data = await request("/api/action", { method: "POST", body: { operation, arguments: args } });
    const result = data.result;
    if (operation === "jobs") {
      const lines = (result.output || "").trim().split(/\r?\n/).filter(Boolean);
      $("#jobCount").textContent = String(Math.max(0, lines.length - 1));
    }
    appendMessage("assistant", result.data ? JSON.stringify(result.data, null, 2) : (result.output || result.error || "操作完成"));
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
    refreshServers();
  }
}

function resizePrompt() {
  const prompt = $("#prompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 150)}px`;
}

async function saveSettings(event) {
  event.preventDefault();
  try {
    // Providers carry name/url/model plus an optional api_key; an empty key
    // keeps the saved one (the placeholder says so), "__CLEAR__" forgets it.
    // Keys are encrypted server-side and restored on login; the active
    // provider is switched from the top-bar dropdown, not from this panel.
    const providers = pendingProviders.map(p => ({
      id: p.id || undefined,
      name: p.name.trim(),
      base_url: p.base_url.trim(),
      model: p.model.trim(),
      api_key: p.key.trim() || undefined,
    }));
    const data = await request("/api/config", { method: "POST", body: {
      identity_file: $("#identityFile").value,
      providers,
    }});
    state.config = data.config;
    $("#modelBadge").textContent = data.config.model;
    renderModelSelect();
    $("#settingsDialog").close();
    toast("设置已保存到当前会话");
    if (data.config.identity_exists) refreshServers();
    else {
      $("#routeState").textContent = "等待完成本机设置";
    }
  } catch (error) {
    // Toasts are hidden behind an open <dialog> (top layer); show here instead.
    const box = $("#settingsError");
    if (box) { box.textContent = error.message; box.hidden = false; }
    else toast(error.message);
  }
}

async function connectServer() {
  if (!state.active) { toast("请先选择服务器"); return; }
  try {
    await request("/api/connect", { method: "POST", body: { name: state.active } });
    toast(`已为 ${state.active} 打开 SSH 窗口，请输入密码和六位验证码`);
    addActivity("建立服务器连接", `${state.active} 等待 SSH 认证`);
    setTimeout(() => refreshServers(true), 7000);
  } catch (error) { toast(error.message); }
}

function openServerDialog(server = null) {
  $("#serverForm").reset();
  const err = $("#serverError");
  err.hidden = true;
  err.textContent = "";
  if (server) {
    state.editingServer = server.name;
    // Snapshot the original values so submit can send only what changed;
    // sending the pre-filled target/port back to a connected server would
    // trip the gateway's "disconnect first" guard even though nothing moved.
    state.editingSnapshot = { target: server.target, port: server.port, root: server.root, persist: server.persist || "" };
    $("#serverDialogTitle").textContent = "编辑服务器";
    $("#serverName").value = server.name;
    $("#serverName").disabled = false;
    $("#serverTarget").value = server.target;
    $("#serverPort").value = server.port;
    $("#serverRoot").value = server.root;
    $("#serverPersist").value = server.persist || "";
    $("#saveServer").textContent = "保存修改";
    $("#serverNote").textContent = "改名称或地址端口需要先断开该服务器；根路径与保持时长可随时修改。";
  } else {
    state.editingServer = null;
    state.editingSnapshot = null;
    $("#serverDialogTitle").textContent = "添加服务器";
    $("#serverName").disabled = false;
    $("#saveServer").textContent = "添加";
    $("#serverNote").textContent = "密码与六位验证码只在连接时于独立 SSH 窗口输入，任何地方都不会保存。删除会移除目录条目，不影响远端文件。";
  }
  $("#serverDialog").showModal();
  $("#serverName").focus();
}

function updateSafetyNote() {
  const enabled = $("#autoApprove").checked;
  const note = $("#safetyNote");
  note.innerHTML = enabled
    ? "<span>◇</span> 全程免确认已开启 · 共享/提交/取消将直接执行"
    : "<span>◇</span> 操作前确认 · 凭据不交给模型";
  note.classList.toggle("warn", enabled);
}

async function toggleAutoApprove(event) {
  const enabled = event.target.checked;
  try {
    const data = await request("/api/autoapprove", { method: "POST", body: { enabled } });
    event.target.checked = data.auto_approve;
    state.config.auto_approve = data.auto_approve;
    updateSafetyNote();
    toast(data.auto_approve ? "全程免确认已开启（本会话有效，重启后失效）" : "已恢复逐项确认");
  } catch (error) {
    event.target.checked = !enabled;
    updateSafetyNote();
    toast(error.message);
  }
}

function resetChat() {
  // Clear the view immediately; the server saves the old conversation and
  // starts a fresh one in the background, so the button is instant even while
  // a previous turn is still running.
  state.queue = [];
  state.discard = state.busy;
  state.approvalId = null;
  state.pendingAction = null;
  if (state.abortController) state.abortController.abort();
  removeSessionBar();
  $("#messages").innerHTML = "";
  $("#welcome").hidden = false;
  if ($("#approvalDialog").open) $("#approvalDialog").close();
  request("/api/conversations/new", { method: "POST", body: {} })
    .then(() => { toast("已新建对话"); refreshHistory(); })
    .catch(error => {
      // Without a login the session is still reset locally; only surface
      // real failures.
      if (!error.message.includes("登录")) toast(`新建对话失败：${error.message}`);
    });
}

function relativeTime(iso) {
  if (!iso) return "";
  const time = new Date(iso).getTime();
  if (!Number.isFinite(time)) return "";
  const diff = Date.now() - time;
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

async function refreshHistory() {
  try {
    const query = state.activeProject ? `?project=${encodeURIComponent(state.activeProject)}` : "";
    const data = await request(`/api/conversations${query}`);
    state.conversations = data.conversations || [];
  } catch (error) {
    if (error.message.includes("登录")) {
      // Session lost (server restarted): drop the stale token.
      state.token = "";
      localStorage.removeItem("vaspilot.token");
      state.auth.logged_in = false;
      renderAuthPanel();
    }
    // Keep the stale list visible while the server is unreachable.
  }
  renderHistory();
}

function historyGroupLabel(iso) {
  const time = new Date(iso).getTime();
  if (!Number.isFinite(time)) return "更早";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  if (time >= today) return "今天";
  if (time >= today - day) return "昨天";
  if (time >= today - 7 * day) return "7 天内";
  return "更早";
}

function renderHistory() {
  const list = $("#historyList");
  if (!state.auth.logged_in) {
    list.innerHTML = `
      <div class="activity-empty">登录后查看历史对话</div>
      <button id="historyLoginBtn" class="ghost-button" type="button">登录 / 注册</button>`;
    list.querySelector("#historyLoginBtn")?.addEventListener("click", () => openAuthDialog("login"));
    return;
  }
  if (!state.conversations.length) {
    list.innerHTML = `<div class="activity-empty">还没有历史对话</div>`;
    return;
  }
  const groups = {};
  for (const item of state.conversations) {
    const label = historyGroupLabel(item.updated);
    (groups[label] = groups[label] || []).push(item);
  }
  const order = ["今天", "昨天", "7 天内", "更早"];
  list.innerHTML = order.filter(label => groups[label]).map(label => `
    <div class="history-group">
      <div class="history-group-label">${label}</div>
      ${groups[label].map(item => `
        <div class="history-row" data-id="${escapeHtml(item.id)}">
          <div class="history-main">
            <span class="history-title">${escapeHtml(item.title)}</span>
            <span class="history-time">${escapeHtml(relativeTime(item.updated))}</span>
          </div>
          <button class="history-del" data-id="${escapeHtml(item.id)}" type="button" title="删除">×</button>
        </div>`).join("")}
    </div>`).join("");
}

async function loadConversation(id) {
  const entry = state.conversations.find(item => item.id === id);
  const title = entry ? entry.title : "该对话";
  if (state.busy || $("#messages").children.length) {
    if (!confirm(`加载历史对话「${title}」将替换当前对话。继续？`)) return;
  }
  state.queue = [];
  state.discard = state.busy;
  if (state.abortController) state.abortController.abort();
  $("#messages").innerHTML = "";
  $("#welcome").hidden = false;
  if ($("#approvalDialog").open) $("#approvalDialog").close();
  try {
    const data = await request("/api/conversations/load", { method: "POST", body: { id, project: state.activeProject || "" } });
    const messages = data.messages || [];
    if (messages.length) {
      $("#welcome").hidden = true;
      showSessionBar(title, messages.length, entry ? entry.updated : "");
      renderStoredMessages(messages);
    }
    toast(`已加载「${title}」`);
  } catch (error) {
    toast(error.message);
  }
  refreshHistory();
}

async function deleteConversation(id) {
  if (!confirm("删除这条历史对话？此操作不可恢复。")) return;
  try {
    await request("/api/conversations/delete", { method: "POST", body: { id, project: state.activeProject || "" } });
    toast("已删除历史对话");
    refreshHistory();
  } catch (error) {
    toast(error.message);
  }
}

function renderStoredMessages(messages) {
  // Replay a saved conversation: user bubbles, assistant answers, and compact
  // "⚙ tool" chips for tool calls (tool result messages are skipped).
  for (const message of messages) {
    if (message.role === "user") {
      appendMessage("user", message.content || "");
    } else if (message.role === "assistant") {
      if (message.content) appendMessage("assistant", message.content, true);
      const calls = message.tool_calls || [];
      if (calls.length) appendToolChips(calls);
    }
  }
}

function appendToolChips(calls) {
  const node = document.createElement("div");
  node.className = "tool-chips";
  node.innerHTML = calls.map(call => {
    const name = (call.function && call.function.name) || "工具";
    return `<span class="tool-chip">⚙ ${escapeHtml(name)}</span>`;
  }).join("");
  $("#messages").appendChild(node);
  node.scrollIntoView({ behavior: "smooth", block: "end" });
}

function renderModelSelect() {
  // The dropdown mirrors the configured providers; each provider carries its
  // own base URL and (in-memory) API key, so switching never rewrites
  // settings. The current provider is always shown even when missing.
  const select = $("#modelSelect");
  const providers = (state.config.providers && state.config.providers.length)
    ? state.config.providers : [];
  if (!providers.length) {
    select.innerHTML = `<option value="">未配置服务商</option>`;
    return;
  }
  const current = state.config.current_provider || providers[0].id;
  select.innerHTML = providers.map(provider => `
    <option value="${escapeHtml(provider.id)}"${provider.id === current ? " selected" : ""}>${escapeHtml(provider.name)} · ${escapeHtml(provider.model)}</option>`).join("");
}

// Providers pending in the settings panel; committed on 保存设置.
// {id, name, base_url, model, key} — key is the edit buffer only; an empty
// key keeps the server's in-memory key for that provider.
let pendingProviders = [];

function renderProviderList() {
  const list = $("#providerList");
  if (!list) return;
  if (!pendingProviders.length) {
    list.innerHTML = `<div class="activity-empty">还没有模型服务商，点下方按钮添加</div>`;
    return;
  }
  list.innerHTML = pendingProviders.map((p, index) => `
    <div class="provider-card" data-index="${index}">
      <div class="provider-head">
        <span class="provider-title">${escapeHtml(p.name || "未命名")}</span>
        <span class="provider-chip${p.id === (state.config.current_provider || "") ? " active" : ""}">${escapeHtml(p.model || "未填模型")}</span>
        <button class="provider-del" data-index="${index}" type="button" title="删除服务商">×</button>
      </div>
      <div class="provider-grid">
        <label>显示名<input class="p-name" data-index="${index}" value="${escapeHtml(p.name)}" maxlength="40" spellcheck="false"></label>
        <label>模型名<input class="p-model" data-index="${index}" value="${escapeHtml(p.model)}" maxlength="64" spellcheck="false"></label>
        <label class="p-url-label">API 地址（可含端口）<input class="p-url" data-index="${index}" value="${escapeHtml(p.base_url)}" placeholder="https://api.deepseek.com 或 http://127.0.0.1:3000/v1" spellcheck="false"></label>
        <label class="p-key-label">API Key<input class="p-key" data-index="${index}" type="password" value="${escapeHtml(p.key)}" placeholder="${p.has_key ? "已保存（登录后自动恢复），留空则保持不变" : "未配置，请粘贴密钥"}" autocomplete="off"></label>
        <div class="p-key-foot">${p.has_key ? `<button class="key-clear" data-index="${index}" type="button" title="删除已保存的密钥">清除已保存</button>` : ""}</div>
      </div>
    </div>`).join("");
}

function renderAuthPanel() {
  const area = $("#authArea");
  if (state.auth.logged_in) {
    area.innerHTML = `
      <div class="auth-user"><span class="auth-email" title="${escapeHtml(state.auth.email)}">${escapeHtml(state.auth.email)}</span>
      <button id="logoutBtn" class="mini-button" type="button" title="退出登录">退出</button></div>`;
  } else {
    area.innerHTML = `<button id="authLoginBtn" class="ghost-button" type="button">登录 / 注册</button>`;
  }
}

let authMode = "login";

function openAuthDialog(mode = "login") {
  authMode = mode;
  $("#authTitle").textContent = mode === "login" ? "登录" : "注册";
  $("#authSwitch").textContent = mode === "login" ? "切换为注册" : "切换为登录";
  $("#authSubmit").textContent = mode === "login" ? "登录" : "注册";
  $("#authForm").reset();
  $("#authDialog").showModal();
  $("#authEmail").focus();
}

async function logout() {
  try {
    await request("/api/auth/logout", { method: "POST", body: {} });
  } catch { /* the token may already be dead; local cleanup below still applies */ }
  state.token = "";
  localStorage.removeItem("vaspilot.token");
  state.auth = { logged_in: false, email: "" };
  state.projects = [];
  state.activeProject = null;
  renderAuthPanel();
  renderHistory();
  renderProjects();
  toast("已退出登录，历史对话已锁定");
}

async function refreshProjects() {
  try {
    const data = await request("/api/projects");
    state.projects = data.projects || [];
    if (state.activeProject && !state.projects.some(p => p.id === state.activeProject)) {
      state.activeProject = null; // the project was removed elsewhere
    }
  } catch (error) {
    if (error.message.includes("登录")) {
      state.token = "";
      localStorage.removeItem("vaspilot.token");
      state.auth.logged_in = false;
      renderAuthPanel();
    }
    state.projects = [];
  }
  renderProjects();
}

function renderProjects() {
  const list = $("#projectList");
  if (!state.auth.logged_in) {
    list.innerHTML = `<div class="activity-empty">登录后使用项目</div>`;
    return;
  }
  const items = [
    { id: "", name: "全部对话", path: "所有项目与未归类的对话" },
    ...state.projects.map(p => ({ id: p.id, name: p.name, path: p.path })),
  ];
  list.innerHTML = items.map(item => `
    <div class="project-row ${item.id === state.activeProject ? "active" : ""}" data-project="${escapeHtml(item.id)}">
      <div class="project-main">
        <span class="project-name">${escapeHtml(item.name)}</span>
        <span class="project-path">${escapeHtml(item.path)}</span>
      </div>
      ${item.id ? `<button class="project-del" data-project="${escapeHtml(item.id)}" type="button" title="删除项目">×</button>` : ""}
    </div>`).join("");
}

async function switchProject(id) {
  id = id || null;
  if (id === state.activeProject) return;
  if (state.busy || $("#messages").children.length) {
    if (!confirm("切换项目将替换当前对话。继续？")) { renderProjects(); return; }
  }
  state.queue = [];
  state.discard = state.busy;
  if (state.abortController) state.abortController.abort();
  removeSessionBar();
  $("#messages").innerHTML = "";
  $("#welcome").hidden = false;
  if ($("#approvalDialog").open) $("#approvalDialog").close();
  state.activeProject = id;
  try {
    const data = await request("/api/projects/switch", { method: "POST", body: { project: id } });
    state.activeProject = data.project || null;
    renderProjects();
    refreshHistory();
    toast(id ? "已切换项目" : "已切换到全部对话");
  } catch (error) {
    toast(error.message);
    state.activeProject = null;
    renderProjects();
  }
}

async function deleteProject(id) {
  const project = state.projects.find(p => p.id === id);
  if (!project) return;
  if (!confirm(`删除项目「${project.name}」？该项目的历史对话将一并删除，不可恢复。`)) return;
  try {
    const data = await request("/api/projects/remove", { method: "POST", body: { id } });
    state.projects = data.projects || [];
    if (state.activeProject === id) state.activeProject = null;
    renderProjects();
    refreshHistory();
    toast("已删除项目");
  } catch (error) { toast(error.message); }
}

function openProjectDialog() {
  $("#projectForm").reset();
  $("#projectDialogTitle").textContent = "新建项目";
  $("#saveProject").textContent = "创建项目";
  $("#projectDialog").showModal();
  $("#projectName").focus();
}

function showSessionBar(title, count, iso) {
  removeSessionBar();
  const bar = document.createElement("div");
  bar.id = "sessionBar";
  bar.className = "session-bar";
  bar.innerHTML = `<span class="session-title">${escapeHtml(title)}</span><span class="session-meta">${count} 条消息 · ${escapeHtml(relativeTime(iso))}</span>`;
  $("#messages").prepend(bar);
}

function removeSessionBar() { $("#sessionBar")?.remove(); }

function renderRichText(container, text) {
  // Split on ``` fences: odd parts are code blocks (first line may be a
  // language tag), even parts are prose where absolute remote paths become
  // clickable file chips.
  container.textContent = "";
  String(text ?? "").split(/```/).forEach((part, index) => {
    if (index % 2 === 1) {
      // Strip a markdown language tag (```bash) when present; a short
      // letter-only first line that is not the fence (```INCAR) is ambiguous
      // and kept as content.
      const code = part.replace(/^(?:bash|sh|zsh|python|py|js|json|yaml|yml|toml|xml|html|css|c|h|cpp|fortran|f90|text|diff|ini)\n/i, "");
      const pre = document.createElement("pre");
      pre.className = "code-block";
      const codeEl = document.createElement("code");
      codeEl.textContent = code;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-btn";
      copy.textContent = "复制";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(code);
          copy.textContent = "已复制";
          setTimeout(() => { copy.textContent = "复制"; }, 1500);
        } catch { copy.textContent = "复制失败"; }
      });
      pre.append(codeEl, copy);
      container.appendChild(pre);
      return;
    }
    appendTextWithChips(container, part);
  });
}

function isOpenablePath(token, root) {
  if (!root || !token.startsWith(root + "/") || token === root) return false;
  if (token.includes("..") || !/^\/[A-Za-z0-9._+\-@=\/]+$/.test(token)) return false;
  return true;
}

function appendTextWithChips(container, text) {
  const entry = activeServerEntry();
  const root = entry ? entry.root : "";
  for (const token of String(text).split(/(\s+)/)) {
    if (/^\s*$/.test(token)) {
      container.appendChild(document.createTextNode(token));
      continue;
    }
    if (isOpenablePath(token, root)) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "file-chip";
      chip.textContent = "📄 " + token;
      chip.title = "下载并打开 " + token;
      chip.addEventListener("click", () => openRemoteFile(token));
      container.appendChild(chip);
    } else {
      container.appendChild(document.createTextNode(token));
    }
  }
}

async function openRemoteFile(path) {
  try {
    const data = await request("/api/file/open", { method: "POST", body: { path } });
    toast(`已下载并打开：${data.local_path}`);
  } catch (error) {
    toast(`打开文件失败：${error.message}`);
  }
}

async function init() {
  try {
    const data = await request("/api/health");
    state.csrf = data.csrf;
    state.config = data.config;
    $("#identityFile").value = data.config.identity_file || "";
    $("#modelBadge").textContent = data.config.model || "DeepSeek";
    $("#autoApprove").checked = !!data.config.auto_approve;
    updateSafetyNote();
    renderModelSelect();
    applyTheme(localStorage.getItem("vaspilot.theme") || "dark");
    renderWallpaper(readWallpaper(), currentWallpaperDim());
    try {
      const status = await request("/api/auth/status");
      state.auth = { logged_in: !!status.logged_in, email: status.email || "" };
      if (!status.logged_in) {
        state.token = "";
        localStorage.removeItem("vaspilot.token");
      }
    } catch { /* stay logged out */ }
    renderAuthPanel();
    refreshProjects();
    if (!data.config.identity_exists || !data.config.api_configured) {
      setTimeout(() => $("#settingsDialog").showModal(), 450);
    }
    if (data.config.identity_exists) refreshServers();
    else {
      $("#routeState").textContent = "等待完成本机设置";
    }
    refreshHistory();
  } catch (error) { toast(`界面初始化失败：${error.message}`); }
}

$("#historyList").addEventListener("click", event => {
  const del = event.target.closest(".history-del");
  if (del) {
    event.stopPropagation();
    deleteConversation(del.dataset.id);
    return;
  }
  const row = event.target.closest(".history-row");
  if (row) loadConversation(row.dataset.id);
});
$("#refreshHistoryBtn").addEventListener("click", () => refreshHistory());

const COMMANDS = [
  { label: "新建对话", hint: "Ctrl+N", run: () => resetChat() },
  { label: "检查任务队列", hint: "", run: () => quickAction("jobs") },
  { label: "最近计算", hint: "", run: () => quickAction("recent") },
  { label: "连接当前服务器", hint: "", run: () => connectServer() },
  { label: "断开当前服务器", hint: "", run: () => disconnectServer() },
  { label: "打开人工终端", hint: "Ctrl+`", run: () => openManualTerminal(state.active) },
  { label: "添加服务器", hint: "", run: () => openServerDialog() },
  { label: "刷新服务器状态", hint: "", run: () => refreshServers(true) },
  { label: "预检查计算目录", hint: "计算目录", run: () => quickAction("vasp-validate", ["-RemotePath", $("#calcPath").value]) },
  { label: "查看计算进度", hint: "计算目录", run: () => quickAction("vasp-progress", ["-RemotePath", $("#calcPath").value]) },
  { label: "打开设置", hint: "Ctrl+,", run: () => $("#settingsDialog").showModal() },
];

function openCommands() {
  const dialog = $("#commandDialog");
  if (dialog.open) return;
  dialog.innerHTML = "";
  COMMANDS.forEach((command, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "command-item";
    button.innerHTML = `<span>${escapeHtml(command.label)}</span><small>${escapeHtml(command.hint)}</small>`;
    button.addEventListener("click", () => {
      dialog.close();
      $("#prompt").value = ""; // drop the "/" that opened the palette
      resizePrompt();
      command.run();
    });
    dialog.appendChild(button);
  });
  dialog.showModal();
  dialog.querySelector(".command-item")?.focus();
}

$("#commandDialog").addEventListener("keydown", event => {
  const items = [...$("#commandDialog").querySelectorAll(".command-item")];
  if (!items.length) return;
  let index = items.indexOf(document.activeElement);
  if (event.key === "ArrowDown") {
    event.preventDefault();
    items[(index + 1) % items.length].focus();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    items[(index - 1 + items.length) % items.length].focus();
  } else if (event.key === "Enter") {
    event.preventDefault();
    items[Math.max(index, 0)].click();
  }
});

$("#chatForm").addEventListener("submit", event => { event.preventDefault(); sendMessage($("#prompt").value); });
$("#prompt").addEventListener("input", resizePrompt);
$("#prompt").addEventListener("keydown", event => {
  if (event.isComposing) return; // never hijack keys while the IME is composing
  if (event.key === "/" && !$("#prompt").value.trim()) { openCommands(); return; }
  if (event.key === "Enter" && (event.ctrlKey || !event.shiftKey)) {
    event.preventDefault();
    $("#chatForm").requestSubmit();
  }
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "n") {
    event.preventDefault();
    resetChat();
  }
  if ((event.ctrlKey || event.metaKey) && event.key === ",") {
    event.preventDefault();
    $("#settingsDialog").showModal();
  }
  if ((event.ctrlKey || event.metaKey) && event.code === "Backquote") {
    event.preventDefault();
    toggleTerminalDock();
  }
});
$$('[data-prompt]').forEach(button => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
$$('[data-action]').forEach(button => button.addEventListener("click", () => quickAction(button.dataset.action)));
$("#validateBtn").addEventListener("click", () => quickAction("vasp-validate", ["-RemotePath", $("#calcPath").value]));
$("#progressBtn").addEventListener("click", () => quickAction("vasp-progress", ["-RemotePath", $("#calcPath").value]));

// ---- server-to-server transfer -------------------------------------------

function renderTransferSelects() {
  const servers = state.servers.map(server => server.name);
  for (const id of ["transferFromServer", "transferToServer"]) {
    const select = $(`#${id}`);
    select.innerHTML = servers
      .map(name => `<option value="${name}">${escapeHtml(name)}</option>`)
      .join("");
  }
  const active = state.active;
  if (servers.includes(active) && servers.length > 1) {
    $("#transferFromServer").value = active;
    const other = servers.find(name => name !== active);
    $("#transferToServer").value = other;
  } else if (servers.length > 1) {
    $("#transferToServer").value = servers[1];
  }
}

$("#transferBtn").addEventListener("click", () => {
  if (state.servers.length < 2) {
    toast("传输需要至少两台服务器，请先在左侧添加并连接");
    return;
  }
  renderTransferSelects();
  $("#transferError").hidden = true;
  $("#transferDialog").showModal();
});

$("#closeTransfer").addEventListener("click", () => $("#transferDialog").close());
$("#cancelTransfer").addEventListener("click", () => $("#transferDialog").close());

$("#transferForm").addEventListener("submit", async event => {
  event.preventDefault();
  const fromServer = $("#transferFromServer").value;
  const toServer = $("#transferToServer").value;
  const fromPath = $("#transferFromPath").value.trim();
  const toPath = $("#transferToPath").value.trim();
  const error = $("#transferError");
  error.hidden = true;
  if (!fromPath || !toPath) {
    error.textContent = "请填写源路径和目标路径";
    error.hidden = false;
    return;
  }
  const submit = $("#startTransfer");
  try {
    submit.disabled = true;
    const data = await request("/api/action/transfer", {
      method: "POST",
      body: { from_server: fromServer, from_path: fromPath, to_server: toServer, to_path: toPath },
    });
    if (data.needs_approval) {
      $("#transferDialog").close();
      state.pendingAction = { actionId: data.action_id, summary: data.summary };
      $("#approvalTitle").textContent = "确认传输";
      $("#approvalDescription").textContent = `确认把 ${data.summary}？文件经 Vlab 中转节点暂存后写入目标服务器。`;
      $("#approveBtn").textContent = "开始传输";
      $("#approvalDialog").showModal();
    } else {
      toast("操作已提交");
    }
  } catch (exc) {
    error.textContent = exc.message || "传输请求失败";
    error.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
$("#refreshBtn").addEventListener("click", () => refreshServers(true));
$("#themeBtn").addEventListener("click", () => {
  const current = localStorage.getItem("vaspilot.theme") || "dark";
  const next = THEME_ORDER[(THEME_ORDER.indexOf(current) + 1) % THEME_ORDER.length];
  applyTheme(next);
  toast(next === "auto" ? "主题已切换为跟随系统" : `主题已切换为${next === "dark" ? "深色" : "浅色"}`);
});
$("#connectBtn").addEventListener("click", connectServer);
$("#disconnectBtn").addEventListener("click", disconnectServer);
$("#addServerBtn").addEventListener("click", openServerDialog);
$("#settingsBtn").addEventListener("click", () => $("#settingsDialog").showModal());
$("#settingsForm").addEventListener("submit", saveSettings);
$("#closeSettings").addEventListener("click", () => $("#settingsDialog").close());
$("#newChat").addEventListener("click", resetChat);
$("#approveBtn").addEventListener("click", () => decideApproval(true));
$("#autoApprove").addEventListener("change", toggleAutoApprove);
$("#denyBtn").addEventListener("click", () => decideApproval(false));
$("#authArea").addEventListener("click", event => {
  if (event.target.closest("#logoutBtn")) logout();
  else if (event.target.closest("#authLoginBtn")) openAuthDialog("login");
});
$("#authForm").addEventListener("submit", async event => {
  event.preventDefault();
  const email = $("#authEmail").value.trim();
  const password = $("#authPassword").value;
  const endpoint = authMode === "login" ? "/api/auth/login" : "/api/auth/register";
  try {
    const data = await request(endpoint, { method: "POST", body: { email, password } });
    state.token = data.token;
    state.auth = { logged_in: true, email: data.email };
    localStorage.setItem("vaspilot.token", data.token);
    renderAuthPanel();
    $("#authDialog").close();
    toast(authMode === "login" ? "登录成功" : "注册成功，已自动登录");
    refreshHistory();
  } catch (error) {
    toast(error.message);
  }
});
$("#authSwitch").addEventListener("click", () => openAuthDialog(authMode === "login" ? "register" : "login"));
$("#closeAuth").addEventListener("click", () => $("#authDialog").close());
$("#modelSelect").addEventListener("change", async event => {
  const providerId = event.target.value;
  if (!providerId) return;
  try {
    const data = await request("/api/config", { method: "POST", body: { current_provider: providerId } });
    state.config = data.config;
    $("#modelBadge").textContent = data.config.model;
    renderModelSelect();
    const provider = (data.config.providers || []).find(p => p.id === providerId);
    toast(provider ? `已切换到 ${provider.name}（${provider.model}）` : "已切换服务商");
  } catch (error) {
    toast(error.message);
    renderModelSelect();
  }
});
$("#providerList").addEventListener("input", event => {
  const provider = pendingProviders[Number(event.target.dataset.index)];
  if (!provider) return;
  if (event.target.classList.contains("p-name")) provider.name = event.target.value;
  else if (event.target.classList.contains("p-model")) provider.model = event.target.value;
  else if (event.target.classList.contains("p-url")) provider.base_url = event.target.value;
  else if (event.target.classList.contains("p-key")) provider.key = event.target.value;
});
$("#providerList").addEventListener("click", event => {
  const del = event.target.closest(".provider-del");
  if (del) {
    pendingProviders.splice(Number(del.dataset.index), 1);
    renderProviderList();
    return;
  }
  const clear = event.target.closest(".key-clear");
  if (clear) {
    const provider = pendingProviders[Number(clear.dataset.index)];
    if (!provider) return;
    provider.key = "__CLEAR__";
    provider.has_key = false;
    renderProviderList();
  }
});
$("#addProviderBtn").addEventListener("click", () => {
  if (pendingProviders.length >= 20) { toast("模型服务商最多 20 个"); return; }
  pendingProviders.push({ id: "", name: "", base_url: "", model: "", key: "", has_key: false });
  renderProviderList();
  const last = $("#providerList").querySelector(".provider-card:last-child .p-name");
  last?.focus();
});
$("#settingsDialog").addEventListener("show", () => {
  pendingProviders = (state.config.providers || []).map(p => ({ ...p, key: "" }));
  renderProviderList();
  const err = $("#settingsError");
  err.hidden = true;
  err.textContent = "";
  const themeSelect = $("#settingsTheme");
  if (themeSelect) themeSelect.value = localStorage.getItem("vaspilot.theme") || "dark";
  syncWallpaperControls();
});
$("#settingsDialog").addEventListener("click", event => {
  const item = event.target.closest(".settings-nav-item");
  if (!item) return;
  $$(".settings-nav-item").forEach(button => button.classList.toggle("active", button === item));
  $$(".settings-page").forEach(page => { page.hidden = page.dataset.page !== item.dataset.settingsPage; });
});
$("#settingsTheme").addEventListener("change", event => {
  applyTheme(event.target.value);
  toast(event.target.value === "auto" ? "已切换为跟随系统" : `已切换为${event.target.value === "dark" ? "深色" : "浅色"}主题`);
});

// ---- wallpaper controls ---------------------------------------------------
function syncWallpaperControls() {
  const wp = readWallpaper();
  const active = wp && wp.mode !== "none" && wp.src;
  const isData = active && wp.src.startsWith("data:");
  $("#wallpaperMode").value = active ? wp.mode : "none";
  $("#wallpaperUrl").value = (active && !isData) ? wp.src : "";
  $("#wallpaperFileName").textContent = isData ? "已使用本地文件" : "";
  $("#wallpaperDim").value = currentWallpaperDim();
}

$("#wallpaperPick").addEventListener("click", () => $("#wallpaperFile").click());
$("#wallpaperFile").addEventListener("change", async event => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  const isVideo = file.type.startsWith("video/");
  const maxBytes = isVideo ? 3 * 1024 * 1024 : 3.5 * 1024 * 1024;
  if (file.size > maxBytes) {
    toast(isVideo ? "视频文件过大（>3MB），请使用网络视频地址" : "图片文件过大（>3.5MB），请使用网络图片地址");
    return;
  }
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file);
    });
    const mode = isVideo ? "video" : "image";
    saveWallpaper({ mode, src: dataUrl }, parseFloat($("#wallpaperDim").value));
    $("#wallpaperMode").value = mode;
    $("#wallpaperUrl").value = "";
    toast(`壁纸已更新：${file.name}`);
  } catch (error) {
    toast(error.message);
  }
});
$("#wallpaperApply").addEventListener("click", () => {
  const mode = $("#wallpaperMode").value;
  const dim = parseFloat($("#wallpaperDim").value);
  if (mode === "none") {
    saveWallpaper({ mode: "none", src: "" }, dim);
    toast("已移除壁纸");
    return;
  }
  const url = $("#wallpaperUrl").value.trim();
  if (!url) { toast("请先粘贴图片/视频地址，或点击「选择文件」"); return; }
  saveWallpaper({ mode, src: url }, dim);
  toast("壁纸已应用");
});
$("#wallpaperClear").addEventListener("click", () => {
  $("#wallpaperMode").value = "none";
  $("#wallpaperUrl").value = "";
  $("#wallpaperFileName").textContent = "";
  saveWallpaper({ mode: "none", src: "" }, parseFloat($("#wallpaperDim").value));
  toast("已移除壁纸");
});
$("#wallpaperDim").addEventListener("input", event => {
  document.body.style.setProperty("--wp-dim", event.target.value);
});
$("#wallpaperDim").addEventListener("change", event => {
  localStorage.setItem(WP_DIM_KEY, event.target.value);
});
$("#projectList").addEventListener("click", event => {
  const del = event.target.closest(".project-del");
  if (del) {
    event.stopPropagation();
    deleteProject(del.dataset.project);
    return;
  }
  const row = event.target.closest(".project-row");
  if (row) switchProject(row.dataset.project);
});
$("#addProjectBtn").addEventListener("click", openProjectDialog);
$("#projectForm").addEventListener("submit", async event => {
  event.preventDefault();
  const name = $("#projectName").value.trim();
  const path = $("#projectPath").value.trim();
  try {
    const data = await request("/api/projects/add", { method: "POST", body: { name, path } });
    state.projects = data.projects || [];
    renderProjects();
    $("#projectDialog").close();
    toast(`已创建项目「${name}」`);
  } catch (error) { toast(error.message); }
});
$("#closeProject").addEventListener("click", () => $("#projectDialog").close());
$("#cancelProject").addEventListener("click", () => $("#projectDialog").close());
$("#serverForm").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const payload = {
      name: $("#serverName").value.trim(),
      target: $("#serverTarget").value.trim(),
      port: parseInt($("#serverPort").value, 10),
      root: $("#serverRoot").value.trim(),
      persist: $("#serverPersist").value.trim(),
    };
    if (state.editingServer) {
      // Edit mode: send only what the user actually changed. The snapshot is
      // the pre-filled original; sending unchanged target/port to a server
      // that is currently connected would be rejected as "disconnect first".
      const snapshot = state.editingSnapshot || {};
      const edit = { name: state.editingServer };
      const newName = payload.name;
      if (newName && newName !== state.editingServer) edit.new_name = newName;
      if (payload.target && payload.target !== snapshot.target) edit.target = payload.target;
      if (Number.isInteger(payload.port) && payload.port !== snapshot.port) edit.port = payload.port;
      if (payload.root !== snapshot.root) edit.root = payload.root;
      if (payload.persist !== snapshot.persist) edit.persist = payload.persist;
      if (Object.keys(edit).length === 1) {
        $("#serverDialog").close();
        toast("未做任何修改");
        return;
      }
      const data = await request("/api/servers/edit", { method: "POST", body: edit });
      state.servers = data.servers || state.servers;
      state.active = data.active || state.active;
      renderServers();
      refreshServers();
      toast(edit.new_name
        ? `已更新服务器 ${state.editingServer} → ${edit.new_name}`
        : `已更新服务器 ${state.editingServer}`);
    } else {
      if (!payload.persist) delete payload.persist;
      await addServer(payload);
      toast(`已添加服务器 ${payload.name}，可点击行切换`);
    }
    $("#serverDialog").close();
  } catch (error) {
    // Errors inside a <dialog> are invisible as toasts (the dialog sits in the
    // top layer above everything); show them in the dialog itself instead.
    const box = $("#serverError");
    if (box) { box.textContent = error.message; box.hidden = false; }
    else toast(error.message);
  }
});
$("#closeServer").addEventListener("click", () => $("#serverDialog").close());
$("#cancelServer").addEventListener("click", () => $("#serverDialog").close());

// Poll connection states every 10s; refreshServers guards concurrent runs.
setInterval(() => refreshServers(), 10000);

initTerminalDock();
init();
