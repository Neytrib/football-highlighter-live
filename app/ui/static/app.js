const CATALOG_SOURCE_STORAGE_KEY = "footballHighlighterChannelCatalogSources";

function parseCatalogSources(value) {
  return String(value || "")
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function readCatalogSources() {
  try {
    return parseCatalogSources(localStorage.getItem(CATALOG_SOURCE_STORAGE_KEY));
  } catch {
    return [];
  }
}

function writeCatalogSources(sources) {
  try {
    localStorage.setItem(CATALOG_SOURCE_STORAGE_KEY, sources.join(", "));
  } catch {
    // Browser storage can be unavailable in hardened local contexts.
  }
}

const state = {
  status: null,
  clips: [],
  categories: [],
  channels: [],
  activeCategory: "all",
  channelQuery: "",
  channelLanguage: "all",
  channelSources: readCatalogSources(),
};

const els = {
  serverLine: document.getElementById("serverLine"),
  engineState: document.getElementById("engineState"),
  highlighterState: document.getElementById("highlighterState"),
  modeState: document.getElementById("modeState"),
  streamState: document.getElementById("streamState"),
  lastUpdated: document.getElementById("lastUpdated"),
  commandMessage: document.getElementById("commandMessage"),
  streamPreview: document.getElementById("streamPreview"),
  streamHint: document.getElementById("streamHint"),
  streamForm: document.getElementById("streamForm"),
  streamIdInput: document.getElementById("streamIdInput"),
  refreshChannels: document.getElementById("refreshChannels"),
  channelCount: document.getElementById("channelCount"),
  channelSearch: document.getElementById("channelSearch"),
  channelLanguage: document.getElementById("channelLanguage"),
  catalogSourceForm: document.getElementById("catalogSourceForm"),
  catalogSourceInput: document.getElementById("catalogSourceInput"),
  channelForm: document.getElementById("channelForm"),
  channelNameInput: document.getElementById("channelNameInput"),
  channelIdInput: document.getElementById("channelIdInput"),
  channelLangInput: document.getElementById("channelLangInput"),
  channelQualityInput: document.getElementById("channelQualityInput"),
  channelList: document.getElementById("channelList"),
  clipCount: document.getElementById("clipCount"),
  categoryTabs: document.getElementById("categoryTabs"),
  clipList: document.getElementById("clipList"),
  logList: document.getElementById("logList"),
  categoryForm: document.getElementById("categoryForm"),
  categoryName: document.getElementById("categoryName"),
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modalTitle"),
  modalText: document.getElementById("modalText"),
  modalInput: document.getElementById("modalInput"),
  modalCancel: document.getElementById("modalCancel"),
  modalConfirm: document.getElementById("modalConfirm"),
};

function titleCase(value) {
  return String(value || "unknown").replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatSize(bytes) {
  const value = Number(bytes || 0);
  if (value > 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  if (value > 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  if (value > 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

async function post(path, body = {}) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

async function refreshStatus() {
  state.status = await api("/api/status");
  renderStatus();
}

async function refreshLogs() {
  const payload = await api("/api/logs?limit=80");
  renderLogs(payload.logs || []);
}

async function refreshClips() {
  const payload = await api("/api/clips");
  state.clips = payload.clips || [];
  state.categories = payload.categories || [];
  renderTabs();
  renderClips();
}

async function refreshChannels() {
  const payload = await api("/api/channels");
  state.channels = payload.channels || [];
  renderChannels();
}

async function importConfiguredChannelSources() {
  const sources = parseCatalogSources(els.catalogSourceInput.value);
  state.channelSources = sources;
  writeCatalogSources(sources);
  if (!sources.length) {
    await refreshChannels();
    setMessage("No catalog sources configured");
    return;
  }
  const payload = await post("/api/channels/refresh", { sources });
  await refreshChannels();
  setMessage(payload.refresh?.message || "Channels refreshed");
}

async function refreshAll() {
  await Promise.all([refreshStatus(), refreshLogs(), refreshClips(), refreshChannels()]);
}

function renderStatus() {
  const status = state.status || {};
  const engine = status.engine || {};
  const highlighter = status.highlighter || {};
  const mode = status.mode || {};
  const stream = status.stream || {};

  els.engineState.textContent = titleCase(engine.state);
  els.highlighterState.textContent = titleCase(highlighter.state);
  els.modeState.textContent = mode.dryRun ? "Dry Run" : "Live Clips";
  els.streamState.textContent = stream.configured ? "Configured" : "Missing";
  els.serverLine.textContent = `${status.server?.url || "http://127.0.0.1:5174"} · uptime ${status.server?.uptimeSeconds || 0}s`;
  els.lastUpdated.textContent = new Date().toLocaleTimeString();
  if (document.activeElement !== els.streamIdInput) {
    els.streamIdInput.value = stream.id || "";
  }

  if (stream.playbackUrl) {
    if (els.streamPreview.src !== stream.playbackUrl) {
      els.streamPreview.src = stream.playbackUrl;
    }
    els.streamHint.textContent = stream.playbackUrl;
  } else {
    els.streamPreview.removeAttribute("src");
    els.streamHint.textContent = "No stream configured";
  }
}

function renderTabs() {
  const tabs = [{ key: "all", label: "All", count: state.clips.length }, ...state.categories];
  els.categoryTabs.innerHTML = tabs.map((category) => {
    const active = category.key === state.activeCategory ? " active" : "";
    const label = `${category.label || "All"} (${category.count ?? 0})`;
    return `<button class="tab${active}" type="button" data-category="${escapeHtml(category.key)}">${escapeHtml(label)}</button>`;
  }).join("");
}

function renderClips() {
  const filtered = state.activeCategory === "all"
    ? state.clips
    : state.clips.filter((clip) => clip.root === state.activeCategory);

  els.clipCount.textContent = `${filtered.length} clip${filtered.length === 1 ? "" : "s"}`;
  if (filtered.length === 0) {
    els.clipList.innerHTML = '<div class="empty">No clips in this view</div>';
    return;
  }

  els.clipList.innerHTML = filtered.map((clip) => {
    const mtime = new Date((clip.mtime || 0) * 1000).toLocaleString();
    return `
      <article class="clip-row">
        <div>
          <div class="clip-title">
            <span class="clip-name">${escapeHtml(clip.name)}</span>
            <span class="badge">${escapeHtml(clip.category)}</span>
            <span class="badge">${escapeHtml(formatSize(clip.size))}</span>
          </div>
          <div class="clip-meta">${escapeHtml(mtime)} · ${escapeHtml(clip.path)}</div>
        </div>
        <div class="clip-actions">
          <a class="button" href="${escapeAttr(clip.mediaUrl)}" target="_blank" rel="noreferrer">Open</a>
          <button class="button" type="button" data-action="rename" data-root="${escapeAttr(clip.root)}" data-path="${escapeAttr(clip.path)}" data-name="${escapeAttr(clip.name)}">Rename</button>
          <button class="button" type="button" data-action="move" data-root="${escapeAttr(clip.root)}" data-path="${escapeAttr(clip.path)}">Move</button>
          <button class="button danger" type="button" data-action="delete" data-root="${escapeAttr(clip.root)}" data-path="${escapeAttr(clip.path)}" data-name="${escapeAttr(clip.name)}">Delete</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderChannels() {
  const activeId = state.status?.stream?.id || "";
  const query = state.channelQuery.toLowerCase();
  const channels = state.channels.filter((channel) => {
    const languageMatch = state.channelLanguage === "all" || channel.language === state.channelLanguage;
    const text = `${channel.name || ""} ${channel.language || ""} ${channel.quality || ""} ${channel.source || ""}`.toLowerCase();
    return languageMatch && (!query || text.includes(query));
  });

  els.channelCount.textContent = `${channels.length} channel${channels.length === 1 ? "" : "s"}`;
  if (!channels.length) {
    els.channelList.innerHTML = '<div class="empty">No channels in this view</div>';
    return;
  }

  els.channelList.innerHTML = channels.map((channel) => {
    const active = channel.streamId === activeId ? '<span class="badge active-badge">Active</span>' : "";
    const source = channel.source && channel.source !== "manual" ? `<span class="channel-source">${escapeHtml(channel.source)}</span>` : "";
    return `
      <article class="channel-row">
        <div class="channel-main">
          <div class="channel-title">
            <span class="channel-name">${escapeHtml(channel.name)}</span>
            ${active}
            <span class="badge">${escapeHtml((channel.language || "other").toUpperCase())}</span>
            ${channel.quality ? `<span class="badge">${escapeHtml(channel.quality)}</span>` : ""}
          </div>
          <div class="channel-meta">${escapeHtml(channel.streamId)} ${source}</div>
        </div>
        <div class="channel-actions">
          <button class="button primary" type="button" data-channel-action="use" data-stream-id="${escapeAttr(channel.streamId)}">Use</button>
          <button class="button danger" type="button" data-channel-action="delete" data-stream-id="${escapeAttr(channel.streamId)}" data-name="${escapeAttr(channel.name)}">Delete</button>
        </div>
      </article>
    `;
  }).join("");
}

function renderLogs(logs) {
  if (!logs.length) {
    els.logList.innerHTML = '<div class="empty">No logs yet</div>';
    return;
  }
  els.logList.innerHTML = logs.map((log) => {
    const level = String(log.level || "info").toLowerCase();
    const cls = ["warning", "error"].includes(level) ? level : "info";
    const meta = [log.ts, log.logger, log.func].filter(Boolean).join(" · ");
    return `
      <div class="log-row ${cls}">
        <div class="log-message">${escapeHtml(log.message || "")}</div>
        <div class="log-meta">${escapeHtml(level.toUpperCase())} · ${escapeHtml(meta)}</div>
      </div>
    `;
  }).join("");
}

async function runCommand(path) {
  setMessage("Running command...");
  try {
    await post(path);
    setMessage("Command completed");
    await refreshStatus();
  } catch (error) {
    setMessage(error.message);
  }
}

function setMessage(message) {
  els.commandMessage.textContent = message;
}

function askModal({ title, text, value = "", input = true, confirm = "Confirm" }) {
  return new Promise((resolve) => {
    els.modalTitle.textContent = title;
    els.modalText.textContent = text;
    els.modalInput.value = value;
    els.modalInput.hidden = !input;
    els.modalConfirm.textContent = confirm;
    els.modal.hidden = false;
    if (input) {
      requestAnimationFrame(() => els.modalInput.focus());
    }

    const close = (result) => {
      els.modal.hidden = true;
      els.modalConfirm.removeEventListener("click", onConfirm);
      els.modalCancel.removeEventListener("click", onCancel);
      els.modalInput.removeEventListener("keydown", onKey);
      resolve(result);
    };
    const onConfirm = () => close(input ? els.modalInput.value.trim() : true);
    const onCancel = () => close(null);
    const onKey = (event) => {
      if (event.key === "Enter") onConfirm();
      if (event.key === "Escape") onCancel();
    };
    els.modalConfirm.addEventListener("click", onConfirm);
    els.modalCancel.addEventListener("click", onCancel);
    els.modalInput.addEventListener("keydown", onKey);
  });
}

async function handleClipAction(button) {
  const action = button.dataset.action;
  const root = button.dataset.root;
  const path = button.dataset.path;
  try {
    if (action === "rename") {
      const newName = await askModal({
        title: "Rename Clip",
        text: "Enter a new file name.",
        value: button.dataset.name || "",
        confirm: "Rename",
      });
      if (!newName) return;
      await post("/api/clips/rename", { root, path, newName });
    }
    if (action === "move") {
      const category = await askModal({
        title: "Move Clip",
        text: "Enter a custom category name.",
        confirm: "Move",
      });
      if (!category) return;
      await post("/api/clips/move", { root, path, category });
    }
    if (action === "delete") {
      const confirmed = await askModal({
        title: "Delete Clip",
        text: `Delete ${button.dataset.name || "this clip"} from disk?`,
        input: false,
        confirm: "Delete",
      });
      if (!confirmed) return;
      await post("/api/clips/delete", { root, path });
    }
    await refreshClips();
    setMessage("Clip library updated");
  } catch (error) {
    setMessage(error.message);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

document.getElementById("refreshAll").addEventListener("click", () => refreshAll().catch((error) => setMessage(error.message)));
document.getElementById("refreshLogs").addEventListener("click", () => refreshLogs().catch((error) => setMessage(error.message)));
els.refreshChannels.addEventListener("click", async () => {
  try {
    setMessage("Refreshing channels...");
    await importConfiguredChannelSources();
  } catch (error) {
    setMessage(error.message);
  }
});

document.querySelectorAll("[data-command]").forEach((button) => {
  button.addEventListener("click", () => runCommand(button.dataset.command));
});

document.getElementById("openStream").addEventListener("click", () => {
  const url = state.status?.stream?.playbackUrl;
  if (url) window.open(url, "_blank", "noreferrer");
});

document.getElementById("copyStream").addEventListener("click", async () => {
  const url = state.status?.stream?.playbackUrl;
  if (!url) return;
  await navigator.clipboard.writeText(url);
  setMessage("Stream link copied");
});

els.categoryTabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-category]");
  if (!button) return;
  state.activeCategory = button.dataset.category;
  renderTabs();
  renderClips();
});

els.clipList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  handleClipAction(button);
});

els.channelSearch.addEventListener("input", () => {
  state.channelQuery = els.channelSearch.value.trim();
  renderChannels();
});

els.channelLanguage.addEventListener("change", () => {
  state.channelLanguage = els.channelLanguage.value;
  renderChannels();
});

els.catalogSourceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    setMessage("Checking channel catalog...");
    await importConfiguredChannelSources();
  } catch (error) {
    setMessage(error.message);
  }
});

els.channelList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-channel-action]");
  if (!button) return;
  const streamId = button.dataset.streamId;
  const action = button.dataset.channelAction;
  try {
    if (action === "use") {
      setMessage("Setting channel stream...");
      await post("/api/stream", { streamId, restartHighlighter: true });
      await refreshStatus();
      renderChannels();
      setMessage("Channel selected");
    }
    if (action === "delete") {
      const confirmed = await askModal({
        title: "Delete Channel",
        text: `Delete ${button.dataset.name || "this channel"} from the local list?`,
        input: false,
        confirm: "Delete",
      });
      if (!confirmed) return;
      await post("/api/channels/delete", { streamId });
      await refreshChannels();
      setMessage("Channel deleted");
    }
  } catch (error) {
    setMessage(error.message);
  }
});

els.channelForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.channelNameInput.value.trim();
  const streamId = els.channelIdInput.value.trim();
  if (!streamId) return;
  try {
    await post("/api/channels", {
      name,
      streamId,
      language: els.channelLangInput.value,
      quality: els.channelQualityInput.value.trim(),
    });
    els.channelNameInput.value = "";
    els.channelIdInput.value = "";
    els.channelQualityInput.value = "";
    await refreshChannels();
    setMessage("Channel added");
  } catch (error) {
    setMessage(error.message);
  }
});

els.categoryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = els.categoryName.value.trim();
  if (!name) return;
  try {
    await post("/api/categories", { name });
    els.categoryName.value = "";
    await refreshClips();
    setMessage("Category created");
  } catch (error) {
    setMessage(error.message);
  }
});

els.streamForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const streamId = els.streamIdInput.value.trim();
  if (!streamId) return;
  try {
    setMessage("Updating stream...");
    await post("/api/stream", { streamId, restartHighlighter: true });
    await refreshStatus();
    setMessage("Stream updated");
  } catch (error) {
    setMessage(error.message);
  }
});

els.catalogSourceInput.value = state.channelSources.join(", ");

refreshAll().catch((error) => setMessage(error.message));
setInterval(() => {
  refreshStatus().catch(() => {});
  refreshLogs().catch(() => {});
}, 5000);
setInterval(() => {
  refreshClips().catch(() => {});
}, 12000);
setInterval(() => {
  if (!state.channelSources.length) return;
  post("/api/channels/refresh", { sources: state.channelSources })
    .then(() => refreshChannels())
    .catch(() => {});
}, 60000);
