import { PdfReader, blockTypeLabel } from "./pdf-viewer.js";
import katex from "./katex/katex.mjs";

const state = {
  documents: [],
  document: null,
  docFilter: {
    tab: "recent",
    searchKeyword: "",
    sortOrder: "desc",
    fileType: "all",
    parseStatus: "all",
  },
  parse: null,
  models: [],
  selectedModelId: null,
  translations: new Map(),
  sourceEdits: new Map(),
  selectedBlocks: new Set(),
  hoverBlock: null,
  view: "source",
  userPreferredSourceView: false,
  searchQuery: "",
  tasks: new Map(),
  taskWatchers: new Set(),
  taskPollers: new Map(),
  health: null,
  markdownCache: new Map(),
  qaRecords: [],
  selectedQaId: null,
  activeQaAnchorBlockId: null,
  answerTaskId: null,
  answerAbortController: null,
  evidenceBlock: null,
  questionContext: null,
  retryingTasks: new Set(),
  busyButtons: new Set(),
  editingSourceBlockId: null,
  pendingDeleteDocumentId: null,
  uploading: false,
  uploadingDoc: null,
  parseProgressTimer: null,
  documentLoadGeneration: 0,
  parseLoadGeneration: 0,
  readerHidden: false,
};

if ("scrollRestoration" in history) {
  history.scrollRestoration = "manual";
}
window.scrollTo(0, 0);

const $ = (selector) => document.querySelector(selector);
const terminalStatuses = new Set(["succeeded", "failed", "cancelled"]);
const pdfReader = new PdfReader($("#pdf-viewer"), {
  onPageChange(pageIndex, pageCount, isUserScroll = false) {
    const currentElem = $("#page-current");
    const totalElem = $("#page-total");
    if (currentElem && totalElem) {
      currentElem.textContent = String(pageIndex + 1);
      totalElem.textContent = String(pageCount);
    } else {
      $("#page-count").textContent = `${pageIndex + 1} / ${pageCount}`;
    }
    $("#prev-page").disabled = pageIndex <= 0;
    $("#next-page").disabled = pageIndex >= pageCount - 1;
    if (!isUserScroll) {
      const firstBlock = document.querySelector(`[data-block-id^="p${pageIndex}."]`);
      if (firstBlock) {
        firstBlock.scrollIntoView({ block: "start", behavior: "auto" });
        window.scrollTo(0, 0);
      }
    }
  },
  onBlockClick(blockId) {
    selectBlock(blockId, false);
    scrollToResultBlock(blockId);
    void pdfReader.focusBlock(blockId);
  },
  onBlankClick() {
    clearBlockSelection();
  },
  onBlockHover(blockId) {
    setHover(blockId);
  },
  onScaleChange(scale) {
    const zoomValue = $("#zoom-value");
    if (zoomValue) zoomValue.textContent = `${Math.round(scale * 100)}%`;
    $("#pdf-viewer").dataset.scale = String(scale);
    const zoomOut = $("#zoom-out");
    if (zoomOut) zoomOut.disabled = scale <= 0.5;
    const zoomIn = $("#zoom-in");
    if (zoomIn) zoomIn.disabled = scale >= 2.0;
  },
  onError(error) {
    notify(`PDF 加载失败：${error?.message || "未知错误"}`);
  },
});

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text().catch(() => null);
  if (!response.ok) {
    const error = new Error(payload?.message || `请求失败（${response.status}）`);
    error.code = payload?.code;
    error.status = response.status;
    throw error;
  }
  return payload;
}

function notify(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => toast.classList.remove("is-visible"), 3200);
}

async function refreshHealth() {
  try {
    state.health = await api("/api/health");
    syncToolbar();
  } catch (error) {
    notify(error.message);
  }
}

function formatFileSize(bytes) {
  if (typeof bytes !== "number" || isNaN(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function refreshModels() {
  try {
    const models = await api("/api/mineru/models");
    state.models = Array.isArray(models) ? models : [];
    if (!state.selectedModelId) {
      const defaultModel = state.models.find((m) => m.selected) || state.models[0];
      state.selectedModelId = defaultModel?.model_id || null;
    }
    renderModelSelect();
    syncToolbar();
  } catch (error) {
    console.warn("加载解析模型失败", error);
  }
}

function renderModelSelect() {
  const select = $("#model-select");
  if (!select) return;
  select.replaceChildren();
  if (!state.models.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "默认解析器";
    select.append(option);
    select.disabled = true;
    return;
  }
  for (const model of state.models) {
    const option = document.createElement("option");
    option.value = model.model_id;
    option.textContent = model.name || model.model_id;
    if (model.model_id === state.selectedModelId || (!state.selectedModelId && model.selected)) {
      option.selected = true;
      state.selectedModelId = model.model_id;
    }
    select.append(option);
  }
  select.disabled = false;
}

function selectedParseId() {
  return state.document?.active_parse_id || state.document?.parse_results?.[0]?.parse_id || null;
}

function getDocumentTask(documentId, item) {
  const activeInState = [...state.tasks.values()]
    .filter((t) => t.document_id === documentId && (t.status === "queued" || t.status === "running"))
    .sort((a, b) => {
      if (a.status === "running" && b.status !== "running") return -1;
      if (b.status === "running" && a.status !== "running") return 1;
      return new Date(b.created_at || 0) - new Date(a.created_at || 0);
    })[0];
  if (activeInState) return activeInState;

  if (Array.isArray(item?.tasks)) {
    const activeInItem = item.tasks
      .filter((t) => t.status === "queued" || t.status === "running")
      .sort((a, b) => {
        if (a.status === "running" && b.status !== "running") return -1;
        if (b.status === "running" && a.status !== "running") return 1;
        return new Date(b.created_at || 0) - new Date(a.created_at || 0);
      })[0];
    if (activeInItem) return activeInItem;
  }

  const recentTerminal = [...state.tasks.values()]
    .filter((t) => t.document_id === documentId && (t.status === "failed" || t.status === "cancelled"))
    .sort((a, b) => new Date(b.finished_at || b.created_at || 0) - new Date(a.finished_at || a.created_at || 0))[0];
  if (recentTerminal) return recentTerminal;

  if (Array.isArray(item?.tasks)) {
    const terminalInItem = item.tasks
      .filter((t) => t.status === "failed" || t.status === "cancelled")
      .sort((a, b) => new Date(b.finished_at || b.created_at || 0) - new Date(a.finished_at || a.created_at || 0))[0];
    if (terminalInItem) return terminalInItem;
  }

  return null;
}

function getFileBadgeInfo(filename = "") {
  const ext = (filename.split(".").pop() || "").toLowerCase();
  if (ext === "pdf") {
    return { className: "badge-pdf", html: "PDF" };
  }
  if (["ppt", "pptx"].includes(ext)) {
    return { className: "badge-pptx", html: "P" };
  }
  if (["doc", "docx", "word"].includes(ext)) {
    return { className: "badge-docx", html: "W" };
  }
  if (["xls", "xlsx", "csv"].includes(ext)) {
    return { className: "badge-xlsx", html: "X" };
  }
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "jp2"].includes(ext)) {
    return {
      className: "badge-img",
      html: `<svg width="14" height="14" viewBox="0 0 24 24" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3" ry="3"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`,
    };
  }
  return {
    className: "badge-file",
    html: `<svg width="14" height="14" viewBox="0 0 24 24" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>`,
  };
}

function createDocumentItemElement(item) {
  const isUploading = Boolean(item.isUploading);
  let statusText = "待解析";
  let statusClass = "is-muted";
  let statusIcon = null;
  let progress = null;
  let isSpinning = false;
  let hasActiveTask = false;
  let ringVariant = "is-parsing";
  let showRing = false;
  let failedTask = null;

  if (isUploading) {
    hasActiveTask = true;
    showRing = true;
    statusClass = "is-running";
    ringVariant = "is-uploading";
    const pct = Math.round((item.uploadProgress || 0) * 100);
    statusText = `↑ 上传中 ${pct}%`;
    progress = item.uploadProgress || 0;
  } else {
    const task = getDocumentTask(item.document_id, item);
    if (task && (task.status === "queued" || task.status === "running")) {
      hasActiveTask = true;
      showRing = true;
      if (task.status === "queued") {
        statusClass = "is-muted";
        statusText = task.kind === "translate" ? "⌛ 排队翻译" : "⌛ 排队中";
        isSpinning = true;
        ringVariant = "is-queued";
      } else {
        statusClass = "is-running";
        ringVariant = "is-parsing";
        const known = typeof task.progress === "number";
        const pct = known ? Math.round(task.progress * 100) : null;
        if (task.kind === "parse") {
          statusText = pct !== null ? `⚡ 解析中 ${pct}%` : (task.message || "⚡ 解析中");
        } else if (task.kind === "translate") {
          statusText = pct !== null ? `⚡ 翻译中 ${pct}%` : (task.message || "⚡ 翻译中");
        } else {
          statusText = pct !== null ? `⚡ 处理中 ${pct}%` : (task.message || "⚡ 处理中");
        }
        if (pct !== null) {
          progress = pct / 100;
        } else {
          isSpinning = true;
        }
      }
    } else if (task && task.status === "failed") {
      failedTask = task;
      statusText = task.kind === "translate" ? "✕ 翻译失败" : "✕ 解析失败";
      statusClass = "is-failed";
    } else if (task && task.status === "cancelled") {
      statusText = "已取消";
      statusClass = "is-muted";
    } else if (item.active_parse_id || (item.parse_results && item.parse_results.length > 0)) {
      const activeRes = item.parse_results?.find((p) => p.parse_id === item.active_parse_id) || item.parse_results?.[0];
      const pages = activeRes?.pages;
      const isTranslated = Boolean(
        activeRes?.has_translation ||
        item.has_translation ||
        (state.document?.document_id === item.document_id && hasDocumentTranslation())
      );
      const actionName = isTranslated ? "翻译完成" : "解析完成";
      statusText = pages ? `✓ ${actionName} · ${pages}页` : `✓ ${actionName}`;
      statusClass = "is-success";
      showRing = true;
      ringVariant = "is-success";
      progress = 1.0;
    } else {
      statusText = "待解析";
      statusClass = "is-muted";
    }
  }

  const entry = document.createElement("div");
  entry.className = "document-item" + (
    state.document?.document_id === item.document_id ? " is-active" : ""
  ) + (hasActiveTask ? " is-busy" : "");
  if (item.document_id && !isUploading) {
    entry.dataset.documentId = item.document_id;
  }

  const open = document.createElement("button");
  open.type = "button";
  open.className = "document-open";
  if (isUploading) {
    open.disabled = true;
  } else {
    open.addEventListener("click", () => void openDocument(item.document_id));
  }

  const iconContainer = document.createElement("div");
  iconContainer.className = "doc-icon-container";

  if (showRing) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", `doc-ring-svg ${isSpinning ? "is-spinning" : ""}`);
    svg.setAttribute("viewBox", "0 0 36 36");

    const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    track.setAttribute("class", "ring-track");
    track.setAttribute("fill", "none");
    track.setAttribute("cx", "18");
    track.setAttribute("cy", "18");
    track.setAttribute("r", "15.5");

    const progCircle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    progCircle.setAttribute("class", `ring-progress ${ringVariant}`);
    progCircle.setAttribute("fill", "none");
    progCircle.setAttribute("cx", "18");
    progCircle.setAttribute("cy", "18");
    progCircle.setAttribute("r", "15.5");

    const circumference = 97.39;
    progCircle.setAttribute("stroke-dasharray", isSpinning ? "24.35 73.04" : String(circumference));
    const offset = isSpinning
      ? 0
      : (progress !== null ? circumference * (1 - progress) : circumference);
    progCircle.setAttribute("stroke-dashoffset", String(offset));

    svg.append(track, progCircle);
    iconContainer.append(svg);
  }

  const badgeInfo = getFileBadgeInfo(item.name);
  const badge = document.createElement("div");
  badge.className = `doc-badge ${badgeInfo.className}`;
  badge.innerHTML = badgeInfo.html;
  iconContainer.append(badge);

  const info = document.createElement("div");
  info.className = "doc-info";

  const name = document.createElement("span");
  name.className = "document-item-name";
  name.textContent = item.name;
  name.title = item.name;

  const statusElem = document.createElement("small");
  statusElem.className = `document-item-status ${statusClass}`;
  if (statusIcon) {
    const iconSpan = document.createElement("span");
    iconSpan.className = "status-icon";
    iconSpan.innerHTML = statusIcon;
    const textSpan = document.createElement("span");
    textSpan.className = "status-text";
    textSpan.textContent = statusText;
    statusElem.append(iconSpan, textSpan);
  } else {
    const textSpan = document.createElement("span");
    textSpan.className = "status-text";
    textSpan.textContent = statusText;
    statusElem.append(textSpan);
  }

  info.append(name, statusElem);
  open.append(iconContainer, info);
  entry.append(open);

  if (!isUploading) {
    const actions = document.createElement("div");
    actions.className = "document-actions";


    if (failedTask) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "icon-button action-icon-btn document-action retry-action";
      retry.setAttribute("aria-label", "重试任务");
      retry.dataset.tooltip = "重试任务";
      retry.title = "重试任务";
      retry.disabled = state.retryingTasks.has(failedTask.task_id);
      retry.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`;
      retry.addEventListener("click", (e) => {
        e.stopPropagation();
        void retryTask(failedTask);
      });
      actions.append(retry);
    }

    const favorite = document.createElement("button");
    favorite.type = "button";
    favorite.className = "icon-button action-icon-btn document-action favorite-action" + (item.favorite ? " is-active" : "");
    favorite.setAttribute("aria-label", item.favorite ? "取消收藏" : "收藏");
    favorite.dataset.tooltip = item.favorite ? "取消收藏" : "收藏";
    favorite.title = item.favorite ? "取消收藏" : "收藏";
    favorite.innerHTML = item.favorite
      ? `<svg width="14" height="14" viewBox="0 0 16 16" fill="#f59e0b" stroke="#f59e0b" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><polygon points="8 1.5 10 5.8 14.5 6.4 11.2 9.6 12 14.2 8 12 4 14.2 4.8 9.6 1.5 6.4 6 5.8 8 1.5"/></svg>`
      : `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><polygon points="8 1.5 10 5.8 14.5 6.4 11.2 9.6 12 14.2 8 12 4 14.2 4.8 9.6 1.5 6.4 6 5.8 8 1.5"/></svg>`;
    favorite.addEventListener("click", (event) => {
      event.stopPropagation();
      void setDocumentFavorite(item.document_id, !item.favorite);
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-button action-icon-btn document-action delete-action";
    remove.setAttribute("aria-label", "删除文档");
    remove.dataset.tooltip = "删除文档";
    remove.title = "删除文档";
    remove.innerHTML = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4H13M5.5 4V2.5C5.5 2.22386 5.72386 2 6 2H10C10.2761 2 10.5 2.22386 10.5 2.5V4M6.5 7V11.5M9.5 7V11.5M4 4L4.8 13.2C4.85 13.65 5.2 14 5.65 14H10.35C10.8 14 11.15 13.65 11.2 13.2L12 4"/></svg>`;
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      openDeleteDialog(item.document_id, item.name);
    });

    actions.append(favorite, remove);
    entry.append(actions);
  }

  return entry;
}

function getFilteredAndSortedDocuments() {
  const { tab, searchKeyword, sortOrder, fileType, parseStatus } = state.docFilter;

  const filtered = state.documents.filter((doc) => {
    if (tab === "favorite" && !doc.favorite) {
      return false;
    }

    if (searchKeyword) {
      const name = (doc.name || "").toLowerCase();
      if (!name.includes(searchKeyword.toLowerCase())) {
        return false;
      }
    }

    if (fileType !== "all") {
      const ext = ((doc.name || "").split(".").pop() || "").toLowerCase();
      const isImg = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "jp2"].includes(ext);
      const isDoc = ["pdf", "ppt", "pptx", "doc", "docx", "word", "xls", "xlsx", "csv"].includes(ext);
      if (fileType === "doc" && !isDoc) return false;
      if (fileType === "image" && !isImg) return false;
    }

    if (parseStatus !== "all") {
      const task = getDocumentTask(doc.document_id, doc);
      const isRunningOrQueued = Boolean(task && (task.status === "queued" || task.status === "running"));
      const isFailed = Boolean(task && task.status === "failed");
      const isCompleted = Boolean(doc.active_parse_id || (doc.parse_results && doc.parse_results.length > 0));

      if (parseStatus === "parsing" && !isRunningOrQueued) return false;
      if (parseStatus === "failed" && !isFailed) return false;
      if (parseStatus === "completed" && (!isCompleted || isRunningOrQueued || isFailed)) return false;
    }

    return true;
  });

  filtered.sort((a, b) => {
    const timeA = new Date(a.created_at || 0).getTime();
    const timeB = new Date(b.created_at || 0).getTime();
    if (sortOrder === "asc") {
      return (timeA - timeB) || (a.name || "").localeCompare(b.name || "");
    }
    return (timeB - timeA) || (b.name || "").localeCompare(a.name || "");
  });

  return filtered;
}

function renderDocumentList() {
  const list = $("#document-list");
  if (!list) return;
  list.replaceChildren();

  const isRecentTab = state.docFilter.tab === "recent";
  if (state.uploadingDoc && isRecentTab) {
    const uploadingEntry = createDocumentItemElement({
      document_id: "uploading",
      name: state.uploadingDoc.name,
      isUploading: true,
      uploadProgress: state.uploadingDoc.progress,
    });
    list.append(uploadingEntry);
  }

  const items = getFilteredAndSortedDocuments();
  for (const item of items) {
    const entry = createDocumentItemElement(item);
    list.append(entry);
  }

  const footer = document.createElement("div");
  footer.className = "document-list-footer";
  if (items.length > 0 || (state.uploadingDoc && isRecentTab)) {
    footer.textContent = "没有更多啦";
  } else {
    footer.textContent = state.docFilter.tab === "favorite" ? "暂无收藏文档" : "暂无相关文档";
  }
  list.append(footer);
}

async function refreshDocuments() {
  state.documents = await api("/api/documents");
  for (const doc of state.documents) {
    if (Array.isArray(doc.tasks)) {
      for (const t of doc.tasks) {
        if (t.status === "queued" || t.status === "running") {
          if (!state.tasks.has(t.task_id)) {
            state.tasks.set(t.task_id, t);
            watchTask(t);
          }
        }
      }
    }
  }
  renderDocumentList();
}

async function setDocumentFavorite(documentId, favorite) {
  const target = state.documents.find((d) => d.document_id === documentId);
  const previousDocFavorite = target?.favorite;
  const isCurrentDoc = state.document?.document_id === documentId;
  const previousCurrentFavorite = state.document?.favorite;

  if (target) {
    target.favorite = favorite;
  }
  if (isCurrentDoc && state.document) {
    state.document.favorite = favorite;
    syncFavoriteButton();
  }
  renderDocumentList();

  try {
    const updated = await api(`/api/documents/${documentId}/favorite`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favorite }),
    });
    if (target) {
      Object.assign(target, updated);
    }
    if (isCurrentDoc && state.document) {
      state.document = updated;
      syncFavoriteButton();
    }
  } catch (error) {
    if (target && previousDocFavorite !== undefined) {
      target.favorite = previousDocFavorite;
    }
    if (isCurrentDoc && state.document && previousCurrentFavorite !== undefined) {
      state.document.favorite = previousCurrentFavorite;
      syncFavoriteButton();
    }
    renderDocumentList();
    notify(error.message);
  }
}

async function openDocument(documentId) {
  const generation = ++state.documentLoadGeneration;
  for (const el of document.querySelectorAll("#document-list .document-item")) {
    if (el.dataset.documentId === documentId) {
      el.classList.add("is-active");
    } else {
      el.classList.remove("is-active");
    }
  }
  const cachedDoc = state.documents.find((d) => d.document_id === documentId);
  if (cachedDoc) {
    $("#document-name").textContent = cachedDoc.name;
    const sizeElem = $("#document-size");
    if (sizeElem && cachedDoc.size_bytes) sizeElem.textContent = formatFileSize(cachedDoc.size_bytes);
    if (cachedDoc.favorite !== undefined) {
      const favBtn = $("#favorite-button");
      if (favBtn) {
        favBtn.classList.toggle("is-active", Boolean(cachedDoc.favorite));
        const icon = favBtn.querySelector("span");
        if (icon) icon.textContent = cachedDoc.favorite ? "★" : "☆";
      }
    }
  }

  try {
    const loaded = await api(`/api/documents/${documentId}`);
    if (generation !== state.documentLoadGeneration) return;
    state.document = loaded;
    state.parse = null;
    state.translations = new Map();
    state.sourceEdits = new Map();
    state.markdownCache.clear();
    state.selectedBlocks.clear();
    state.searchQuery = "";
    state.userPreferredSourceView = false;
    $("#result-search-input").value = "";
    state.questionContext = null;
    state.qaRecords = [];
    state.selectedQaId = null;
    state.activeQaAnchorBlockId = null;
    state.editingSourceBlockId = null;
    state.answerTaskId = null;
    state.answerAbortController?.abort();
    state.answerAbortController = null;
    $("#stop-answer-button").hidden = true;
    $("#stop-answer-button").disabled = false;
    $("#document-name").textContent = loaded.name;
    const sizeElem = $("#document-size");
    if (sizeElem) sizeElem.textContent = formatFileSize(loaded.size_bytes);
    syncFavoriteButton();
    for (const task of loaded.tasks || []) {
      if (task.status !== "succeeded") {
        state.tasks.set(task.task_id, task);
        watchTask(task);
      }
    }
    renderTasks();
    const parseId = selectedParseId();
    renderVersionSelect();
    syncToolbar();
    if (parseId) await openParse(parseId, generation);
    else {
      pdfReader.destroy();
      $("#page-count").textContent = "0 / 0";
      $("#document-status").textContent = "等待解析";
      renderResult();
    }
  } catch (error) {
    if (generation === state.documentLoadGeneration) notify(error.message);
  }
}

async function openParse(parseId, documentGeneration = state.documentLoadGeneration) {
  if (!state.document) return;
  const generation = ++state.parseLoadGeneration;
  const documentId = state.document.document_id;
  try {
    const [parse, translations, sourceEdits] = await Promise.all([
      api(`/api/documents/${documentId}/parses/${parseId}`),
      api(`/api/documents/${documentId}/translations?parse_id=${encodeURIComponent(parseId)}`),
      api(`/api/documents/${documentId}/source-edits?parse_id=${encodeURIComponent(parseId)}`),
    ]);
    if (generation !== state.parseLoadGeneration || documentGeneration !== state.documentLoadGeneration) return;
    state.parse = parse;
    state.translations = new Map(translations.map((item) => [item.unit_id, item]));
    state.sourceEdits = new Map(
      sourceEdits.map((item) => [`${item.block_id}:${item.node_id}`, item]),
    );
    const hasTranslation = hasDocumentTranslation();
    if (!hasTranslation) {
      state.view = "source";
    } else if (state.view === "source" && !state.userPreferredSourceView) {
      state.view = "zh";
    }
    state.selectedBlocks = new Set([...state.selectedBlocks].filter((id) =>
      parse.blocks.some((block) => block.block_id === id)
    ));
    renderVersionSelect(parseId);
    syncToolbar();
    $("#document-status").textContent = "解析结果已加载";
    renderResult();
    await pdfReader.load(
      `/api/documents/${documentId}/files/preview:${parseId}`,
      parse.pages,
      parse.blocks,
    );
    if (generation !== state.parseLoadGeneration || documentGeneration !== state.documentLoadGeneration) return;
    pdfReader.setSelected(state.selectedBlocks);
  } catch (error) {
    if (generation === state.parseLoadGeneration) notify(error.message);
  }
}

function renderVersionSelect(activeId = selectedParseId()) {
  const select = $("#version-select");
  select.replaceChildren();
  for (const result of state.document?.parse_results || []) {
    const option = document.createElement("option");
    option.value = result.parse_id;
    option.textContent = `${result.parse_id.slice(0, 8)} · ${result.pages} 页`;
    option.selected = result.parse_id === activeId;
    select.append(option);
  }
  select.disabled = !state.document?.parse_results?.length;
}

function syncFavoriteButton() {
  const button = $("#favorite-button");
  if (!button) return;
  const favorite = state.document?.favorite === true;
  button.setAttribute("aria-label", favorite ? "取消收藏" : "收藏");
  button.title = favorite ? "取消收藏" : "收藏";
  button.classList.toggle("is-active", favorite);
  const icon = button.querySelector("span");
  if (icon) icon.textContent = favorite ? "★" : "☆";
}

function syncToolbar() {
  const hasDocument = Boolean(state.document);
  const hasParse = Boolean(state.parse);
  const llmConfigured = state.health?.llm?.configured === true;
  const qaEnabled = state.health?.extensions?.qa_enabled === true;
  const officeEnabled = state.health?.extensions?.office_enabled === true;

  const settingsBtn = $("#settings-button");
  if (settingsBtn) settingsBtn.disabled = false;
  const toolbarSettingsBtn = $("#toolbar-settings-button");
  if (toolbarSettingsBtn) toolbarSettingsBtn.disabled = false;

  const reparseBtn = $("#reparse-button");
  if (reparseBtn) reparseBtn.disabled = !hasDocument || state.busyButtons.has("reparse-button");

  const copyBtn = $("#copy-button");
  if (copyBtn) copyBtn.disabled = !hasParse;

  const downloadBtn = $("#download-button");
  if (downloadBtn) downloadBtn.disabled = !hasParse || state.busyButtons.has("download-button");

  const modelSelect = $("#model-select");
  if (modelSelect) modelSelect.disabled = !state.models.length || state.busyButtons.has("reparse-button");

  const translateBtn = $("#translate-button");
  if (translateBtn) translateBtn.disabled = !hasParse || state.busyButtons.has("translate-button");

  const qaBtn = $("#qa-button");
  if (qaBtn) qaBtn.disabled = !hasParse || state.busyButtons.has("ask-button");

  const favoriteBtn = $("#favorite-button");
  if (favoriteBtn) favoriteBtn.disabled = !hasDocument;

  const deleteBtn = $("#delete-button");
  if (deleteBtn) deleteBtn.disabled = !hasDocument;

  const isXlsx = Boolean(state.document?.name?.toLowerCase().endsWith(".xlsx"));
  const officeOptions = $("#office-options");
  if (officeOptions) officeOptions.hidden = !isXlsx || !officeEnabled;
  const officeSheet = $("#office-sheet");
  if (officeSheet) officeSheet.disabled = !isXlsx || !officeEnabled;
  const officePrintRange = $("#office-print-range");
  if (officePrintRange) officePrintRange.disabled = !isXlsx || !officeEnabled;

  const autoTranslate = $("#auto-translate");
  if (autoTranslate) autoTranslate.disabled = !llmConfigured;

  const versionField = $("#version-setting-field");
  if (versionField) versionField.hidden = !hasDocument;

  syncWorkspaceLayout();
}

function syncWorkspaceLayout() {
  const hasDocument = Boolean(state.document);
  const shell = document.querySelector(".app-shell");
  shell.classList.toggle("has-document", hasDocument);
  shell.classList.toggle("no-document", !hasDocument);
  $("#empty-upload-state").hidden = hasDocument;
  $("#pdf-viewer").hidden = !hasDocument;
  const pdfToolbar = $("#pdf-toolbar");
  if (pdfToolbar) pdfToolbar.hidden = !hasDocument;
  updateReaderVisibility();
}

function updateReaderVisibility() {
  const toggleBtn = $("#toggle-reader-button");
  const shell = document.querySelector(".app-shell");
  const hasDocument = Boolean(state.document);
  const isHidden = Boolean(state.readerHidden && hasDocument);

  if (shell) {
    shell.classList.toggle("is-reader-hidden", isHidden);
  }

  if (toggleBtn) {
    toggleBtn.disabled = !hasDocument;
    toggleBtn.classList.toggle("is-off", isHidden);
    toggleBtn.setAttribute("aria-pressed", String(isHidden));
    const titleText = !hasDocument
      ? "打开文档后可切换原文面板"
      : (isHidden ? "显示原文面板" : "隐藏原文面板");
    toggleBtn.title = titleText;
    toggleBtn.setAttribute("aria-label", titleText);
    toggleBtn.dataset.tooltip = titleText;
  }
}

function setReaderHidden(hidden) {
  state.readerHidden = Boolean(hidden);
  updateReaderVisibility();
}

function toggleReaderView(force) {
  if (!state.document) {
    notify("打开文档后可折叠/展开原文面板");
    return;
  }
  state.readerHidden = typeof force === "boolean" ? force : !state.readerHidden;
  updateReaderVisibility();
}

async function withButtonBusy(buttonId, busyLabel, work) {
  if (state.busyButtons.has(buttonId)) return undefined;
  const button = $(`#${buttonId}`);
  if (!button) return undefined;
  const idleLabel = button.getAttribute("aria-label") || button.title;
  state.busyButtons.add(buttonId);
  button.setAttribute("aria-label", busyLabel);
  button.title = busyLabel;
  button.setAttribute("aria-busy", "true");
  syncToolbar();
  try {
    return await work();
  } finally {
    state.busyButtons.delete(buttonId);
    button.setAttribute("aria-label", idleLabel);
    button.title = idleLabel;
    button.removeAttribute("aria-busy");
    syncToolbar();
  }
}

function parseRequestBody(scope = null) {
  const source = scope || {};
  const body = {};
  const modelId = source.model_id || state.selectedModelId || $("#model-select")?.value || null;
  if (modelId) body.model_id = modelId;

  if (source.options && typeof source.options === "object") {
    body.options = source.options;
  }

  const office = source.office && typeof source.office === "object"
    ? { ...source.office }
    : {};
  if (!scope && state.document?.name?.toLowerCase().endsWith(".xlsx")) {
    const sheet = $("#office-sheet")?.value?.trim();
    const printRange = $("#office-print-range")?.value?.trim();
    if (sheet) office.sheet = sheet;
    if (printRange) office.print_range = printRange;
  }
  if (Object.keys(office).length) body.office = office;

  body.auto_translate = typeof source.auto_translate === "boolean"
    ? source.auto_translate
    : Boolean($("#auto-translate")?.checked);
  return body;
}

function parseBlockIds(value) {
  return [...new Set(value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean))];
}

function qaRequestBody(scope = null) {
  const source = scope || {};
  const context = {
    parse_id: source.parse_id || state.parse?.parse_run_id,
    question: typeof source.question === "string" ? source.question : ($("#question-input")?.value || ""),
    block_ids: Array.isArray(source.block_ids) ? [...source.block_ids] : [...state.selectedBlocks],
    related_block_ids: Array.isArray(source.related_block_ids)
      ? [...source.related_block_ids]
      : parseBlockIds($("#qa-related-blocks")?.value || ""),
    exclude_block_ids: Array.isArray(source.exclude_block_ids)
      ? [...source.exclude_block_ids]
      : parseBlockIds($("#qa-exclude-blocks")?.value || ""),
    auto_related: typeof source.auto_related === "boolean"
      ? source.auto_related
      : Boolean($("#qa-auto-related")?.checked ?? true),
  };
  if (!scope) {
    state.questionContext = {
      parse_id: context.parse_id,
      question: context.question,
      block_ids: [...context.block_ids],
      related_block_ids: [...context.related_block_ids],
      exclude_block_ids: [...context.exclude_block_ids],
      auto_related: context.auto_related,
    };
  }
  return context;
}

async function loadQaRecords() {
  const documentId = state.document?.document_id;
  if (!documentId) return;
  const records = await api(`/api/documents/${documentId}/qa`);
  if (state.document?.document_id !== documentId) return;
  state.qaRecords = records;
  const selected = records.find((record) => record.qa_id === state.selectedQaId) || records[0] || null;
  state.selectedQaId = selected?.qa_id || null;
  renderQaHistory();
  renderQaRecord(selected);
}

function renderQaHistory() {
  const select = $("#qa-history-select");
  if (!select) return;
  select.replaceChildren();
  if (!state.qaRecords.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无历史提问";
    select.append(option);
    select.disabled = true;
    const delBtn = $("#delete-qa-button");
    if (delBtn) delBtn.hidden = true;
    return;
  }
  for (const record of state.qaRecords) {
    const option = document.createElement("option");
    option.value = record.qa_id;
    option.textContent = `${record.question.slice(0, 36)} · ${new Date(record.created_at).toLocaleTimeString()}`;
    option.selected = record.qa_id === state.selectedQaId;
    select.append(option);
  }
  select.disabled = false;
  const delBtn = $("#delete-qa-button");
  if (delBtn) delBtn.hidden = !state.selectedQaId;
}

function renderQaRecord(record) {
  const answerContent = $("#answer-content");
  const delBtn = $("#delete-qa-button");
  const answerState = $("#answer-state");
  if (!record) {
    if (answerContent) answerContent.textContent = "选择快捷问题或在下方输入自己的问题，体验深度解读与证据定位。";
    if (delBtn) delBtn.hidden = true;
    if (answerState) answerState.textContent = "等待提问";
    state.activeQaAnchorBlockId = [...state.selectedBlocks][0] || null;
    updateQaAnchorBox();
    return;
  }
  state.selectedQaId = record.qa_id;
  const questionInput = $("#question-input");
  if (questionInput) questionInput.value = record.question;
  if (answerContent) answerContent.textContent = record.answer;
  if (answerState) answerState.textContent = "已完成";
  if (delBtn) delBtn.hidden = false;

  const anchorId = record.citations?.[0]?.block_id || record.context?.[0]?.block_id || null;
  state.activeQaAnchorBlockId = anchorId;
  updateQaAnchorBox(anchorId);
}

async function locateAnchorBlock() {
  const blockId = state.activeQaAnchorBlockId || [...state.selectedBlocks][0];
  if (!blockId) {
    notify("当前为整篇文档模式，暂无特定内容块可定位");
    return;
  }
  selectBlock(blockId, false);
  scrollToResultBlock(blockId);
  await pdfReader.focusBlock(blockId);
  notify(`已在原文与解析区域高亮定位块 ${blockId}`);
}

function updateQaAnchorBox(forcedBlockId = null) {
  const titleEl = $("#qa-anchor-title");
  const snippetEl = $("#qa-anchor-snippet");
  const locateTag = $("#qa-anchor-box .anchor-locate-tag");
  if (!titleEl || !snippetEl) return;

  const targetBlockId = forcedBlockId || state.activeQaAnchorBlockId || [...state.selectedBlocks][0] || null;
  if (!targetBlockId) {
    titleEl.textContent = "整篇文档";
    snippetEl.textContent = "当前未限定具体内容块，将基于整篇文档上下文提问与解读。点击左侧或右侧任意块可直接限定。";
    if (locateTag) locateTag.hidden = true;
    return;
  }
  const block = state.parse?.blocks?.find((b) => b.block_id === targetBlockId);
  titleEl.textContent = `已选块 ${targetBlockId} (${blockTypeLabel(block?.block_type) || "内容块"})`;
  if (block) {
    const text = state.view === "source" ? blockSourceText(block) : blockTranslationText(block);
    snippetEl.textContent = text.slice(0, 160) + (text.length > 160 ? "…" : "");
  } else {
    snippetEl.textContent = "";
  }
  if (locateTag) locateTag.hidden = false;
}

function openQaDrawer() {
  const panel = $("#ai-panel");
  if (!panel) return;
  panel.hidden = false;
  $("#qa-button")?.setAttribute("aria-expanded", "true");
  updateQaAnchorBox();
  void loadQaRecords().catch((err) => notify(err.message));
  $("#question-input")?.focus();
}

function closeQaDrawer() {
  const panel = $("#ai-panel");
  if (panel) panel.hidden = true;
  $("#qa-button")?.setAttribute("aria-expanded", "false");
}

const openQaPopover = openQaDrawer;
const closeQaPopover = closeQaDrawer;

function openQaForBlock(blockId) {
  state.activeQaAnchorBlockId = blockId;
  selectBlock(blockId, false);
  scrollToResultBlock(blockId);
  openQaDrawer();
}

function message(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty-state";
  paragraph.textContent = text;
  return paragraph;
}

function blockSourceText(block) {
  return (block.source_nodes || []).map((node) => sourceNodeText(block, node)).join("");
}

function sourceEditKey(blockId, nodeId) {
  return `${blockId}:${nodeId}`;
}

function sourceNodeText(block, node) {
  return state.sourceEdits.get(sourceEditKey(block.block_id, node.node_id))?.effective_text
    ?? nodeText(node);
}

function isEditableSourceNode(node) {
  return ["text", "math", "code", "link", "reference", "image"].includes(node.type);
}

function editableSourceNodes(block) {
  return (block.source_nodes || []).filter(isEditableSourceNode);
}

function blockTranslationText(block) {
  return (block.source_nodes || []).map((node) => {
    const unit = state.translations.get(`${block.block_id}:${node.node_id}`);
    return unit?.effective_text ?? nodeText(node);
  }).join("");
}

function blockDisplayText(block) {
  return state.view === "source" ? blockSourceText(block) : blockTranslationText(block);
}

function hasDocumentTranslation() {
  if (!state.translations || state.translations.size === 0) return false;
  for (const item of state.translations.values()) {
    if (item.auto_text || item.manual_text) return true;
  }
  return false;
}

function syncResultTabs() {
  const hasTranslation = hasDocumentTranslation();
  const zhTab = document.querySelector('.result-tab[data-view="zh"]');
  if (zhTab) {
    zhTab.disabled = !hasTranslation;
    zhTab.setAttribute("aria-disabled", String(!hasTranslation));
    zhTab.title = hasTranslation ? "" : "尚未生成中文翻译，可点击右上角翻译按钮进行翻译";
  }
  if (!hasTranslation && state.view === "zh") {
    state.view = "source";
  }
  document.querySelectorAll(".result-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.view === state.view);
  });
}

async function renderRawMarkdown(content, parseId) {
  const key = `${parseId}:source`;
  let markdown = state.markdownCache.get(key);
  if (markdown === undefined) {
    try {
      markdown = await api(
        `/api/documents/${state.document.document_id}/parses/${parseId}/markdown?language=raw`,
      );
      state.markdownCache.set(key, markdown);
    } catch (error) {
      if (state.parse?.parse_run_id === parseId && state.view === "raw") {
        content.replaceChildren(message(error.message));
      }
      return;
    }
  }
  if (state.parse?.parse_run_id !== parseId || state.view !== "raw") return;
  const pre = document.createElement("pre");
  pre.className = "markdown-source";
  pre.textContent = markdown;
  content.replaceChildren(pre);
  $("#result-search-count").textContent = state.searchQuery
    ? "源码视图使用浏览器查找"
    : "";
}

function renderResult() {
  const content = $("#result-content");
  content.replaceChildren();
  content.classList.toggle("has-selection", state.selectedBlocks.size > 0);
  syncResultTabs();
  const selectionCount = $("#selection-count");
  if (selectionCount) {
    selectionCount.textContent = state.selectedBlocks.size
      ? `已选 ${state.selectedBlocks.size} 块`
      : "";
  }
  if (!state.parse) {
    content.append(message("解析完成后，结果会显示在这里。"));
    return;
  }
  if (state.view === "raw") {
    content.append(message("正在加载源码…"));
    void renderRawMarkdown(content, state.parse.parse_run_id);
    return;
  }
  if (state.view === "json") {
    const pre = document.createElement("pre");
    pre.className = "json-view";
    pre.textContent = JSON.stringify(state.parse, null, 2);
    content.append(pre);
    return;
  }
  const query = state.searchQuery.trim().toLocaleLowerCase();
  const blockById = new Map(state.parse.blocks.map((block) => [block.block_id, block]));
  const matches = (block) => `${block.block_id} ${blockSourceText(block)} ${blockTranslationText(block)}`
    .toLocaleLowerCase()
    .includes(query);
  const blocks = state.parse.blocks.filter((block) => {
    if (block.block_type === "table_cell") return false;
    if (!query || matches(block)) return true;
    if (block.block_type !== "table" || !block.table) return false;
    return block.table.cells.some((cell) => blockById.has(cell.block_ref.block_id)
      && matches(blockById.get(cell.block_ref.block_id)));
  });
  $("#result-search-count").textContent = query
    ? `${blocks.length} / ${state.parse.blocks.length} 块`
    : `${state.parse.blocks.length} 块`;
  if (!blocks.length) {
    content.append(message("没有匹配的块。"));
    return;
  }
  for (const block of blocks) {
    if (state.editingSourceBlockId === block.block_id) {
      const editCard = renderBlockEditCard(block);
      content.append(editCard);
      continue;
    }

    const wrapper = document.createElement("article");
    wrapper.className = "result-block" + (
      state.selectedBlocks.has(block.block_id) ? " is-selected" : ""
    );
    wrapper.dataset.blockId = block.block_id;
    wrapper.dataset.blockType = block.block_type || "paragraph";
    wrapper.dataset.label = blockTypeLabel(block.block_type);

    const heading = document.createElement("div");
    heading.className = "result-block-heading";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "block-selection";
    checkbox.checked = state.selectedBlocks.has(block.block_id);
    checkbox.setAttribute("aria-label", `选择块 ${block.block_id}`);
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => selectBlock(block.block_id, true));
    const title = document.createElement("h3");
    title.textContent = `${block.block_id} · ${block.block_type || "block"}`;
    heading.append(checkbox, title);
    wrapper.append(heading);
    if (block.block_type === "table" && block.table) {
      appendTable(wrapper, block, blockById);
    } else {
      const rendered = document.createElement("div");
      rendered.className = "rich-text";
      appendBlockContent(rendered, block, state.view);
      wrapper.append(rendered);
    }
    const actions = document.createElement("div");
    actions.className = "result-block-actions";
    const copyBlockBtn = document.createElement("button");
    copyBlockBtn.type = "button";
    copyBlockBtn.className = "result-block-action-btn";
    copyBlockBtn.textContent = "复制";
    copyBlockBtn.addEventListener("click", async (event) => {
      event.stopPropagation();
      const text = state.view === "source" ? blockSourceText(block) : blockTranslationText(block);
      try {
        await navigator.clipboard.writeText(text);
        notify("已复制块内容");
      } catch {
        notify("复制失败");
      }
    });
    actions.append(copyBlockBtn);
    const edit = createSourceEditButton(block);
    if (edit) {
      edit.className = "result-block-action-btn";
      edit.textContent = "纠正";
      actions.append(edit);
    }
    const qaBlockBtn = document.createElement("button");
    qaBlockBtn.type = "button";
    qaBlockBtn.className = "result-block-action-btn";
    qaBlockBtn.textContent = "✦ AI解读";
    qaBlockBtn.title = "针对该块进行 AI 解读";
    qaBlockBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      openQaForBlock(block.block_id);
    });
    actions.append(qaBlockBtn);
    wrapper.append(actions);

    bindResultBlock(wrapper, block.block_id);
    content.append(wrapper);
  }
  pdfReader.setSelected(state.selectedBlocks);
}

function setResultView(view) {
  if (view === "zh" && !hasDocumentTranslation()) {
    notify("当前文档尚未翻译，请点击右上角翻译按钮进行全文翻译");
    return;
  }
  state.view = view;
  if (view === "source") {
    state.userPreferredSourceView = true;
  } else if (view === "zh") {
    state.userPreferredSourceView = false;
  }
  syncResultTabs();
  renderResult();
}

function nodeText(node) {
  return node.text ?? node.label ?? node.alt ?? node.latex ?? node.code ?? "";
}

function renderTextWithMath(text, container) {
  if (!text) return;
  const regex = /(\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$|\\\([\s\S]*?\\\)|\$(?:[^\$\n\\]|\\.)+?\$)/g;
  let lastIndex = 0;
  let match;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      container.append(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    const token = match[0];
    let isDisplay = false;
    let mathContent = "";
    if (token.startsWith("\\[") && token.endsWith("\\]")) {
      isDisplay = true;
      mathContent = token.slice(2, -2).trim();
    } else if (token.startsWith("$$") && token.endsWith("$$")) {
      isDisplay = true;
      mathContent = token.slice(2, -2).trim();
    } else if (token.startsWith("\\(") && token.endsWith("\\)")) {
      isDisplay = false;
      mathContent = token.slice(2, -2).trim();
    } else if (token.startsWith("$") && token.endsWith("$")) {
      isDisplay = false;
      mathContent = token.slice(1, -1).trim();
    }
    if (mathContent) {
      const span = document.createElement("span");
      span.className = "math-node" + (isDisplay ? " math-display" : "");
      span.setAttribute("aria-label", "数学公式");
      try {
        katex.render(mathContent, span, {
          displayMode: isDisplay,
          throwOnError: false,
        });
        container.append(span);
      } catch {
        container.append(document.createTextNode(token));
      }
    } else {
      container.append(document.createTextNode(token));
    }
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < text.length) {
    container.append(document.createTextNode(text.slice(lastIndex)));
  }
}

function appendInlineNodes(container, block, language) {
  const isFormulaBlock = block.block_type === "formula" || block.block_type === "equation";
  for (const node of block.source_nodes || []) {
    const unit = state.translations.get(`${block.block_id}:${node.node_id}`);
    const translated = language === "chinese" ? unit?.effective_text : null;
    const sourceText = sourceNodeText(block, node);
    if (node.type === "text") {
      const text = translated ?? sourceText;
      renderTextWithMath(text, container);
    } else if (node.type === "math") {
      const math = document.createElement("span");
      const isDisplay = isFormulaBlock;
      math.className = "math-node" + (isDisplay ? " math-display" : "");
      math.setAttribute("aria-label", "数学公式");
      const latex = (translated ?? sourceText).trim();
      try {
        katex.render(latex, math, {
          displayMode: isDisplay,
          throwOnError: false,
        });
      } catch {
        math.textContent = isDisplay ? `\\[${latex}\\]` : `\\(${latex}\\)`;
      }
      container.append(math);
    } else if (node.type === "code") {
      const code = document.createElement("code");
      code.className = "inline-code";
      code.textContent = translated ?? sourceText;
      container.append(code);
    } else if (node.type === "link") {
      const link = document.createElement("a");
      link.textContent = translated ?? sourceText;
      const href = safeHref(node.target);
      if (href) link.href = href;
      else link.title = "已隐藏不安全链接";
      container.append(link);
    } else if (node.type === "reference") {
      const reference = document.createElement("button");
      reference.type = "button";
      reference.className = "inline-reference";
      reference.textContent = translated ?? sourceText;
      reference.addEventListener("click", (event) => {
        event.stopPropagation();
        const target = node.target?.block_id;
        if (!target) return;
        selectBlock(target, false);
        scrollToResultBlock(target);
        void pdfReader.focusBlock(target);
      });
      container.append(reference);
    } else if (node.type === "image") {
      const image = document.createElement("img");
      image.className = "inline-image";
      image.loading = "lazy";
      image.alt = translated ?? sourceText;
      if (state.document?.document_id && state.parse?.parse_run_id && node.asset_id) {
        image.src = `/api/documents/${encodeURIComponent(state.document.document_id)}/files/asset:${encodeURIComponent(state.parse.parse_run_id)}:${encodeURIComponent(node.asset_id)}`;
      }
      container.append(image);
    } else {
      const text = document.createElement("span");
      text.textContent = nodeText(node);
      container.append(text);
    }
  }
}

function appendBlockContent(container, block, view) {
  if (view === "bilingual") {
    const source = document.createElement("div");
    source.className = "bilingual-source rich-text";
    appendInlineNodes(source, block, "source");
    const chinese = document.createElement("div");
    chinese.className = "bilingual-chinese rich-text";
    appendInlineNodes(chinese, block, "chinese");
    container.append(source, chinese);
    return;
  }
  const language = view === "zh" ? "chinese" : "source";
  if (block.block_type === "formula" || block.block_type === "equation") {
    const formulaBox = document.createElement("div");
    formulaBox.className = "result-formula-container";
    appendInlineNodes(formulaBox, block, language);
    container.append(formulaBox);
  } else if (block.block_type === "code") {
    const pre = document.createElement("pre");
    pre.className = "code-block";
    appendInlineNodes(pre, block, language);
    container.append(pre);
  } else {
    appendInlineNodes(container, block, language);
  }
}

function appendTable(container, block, blockById) {
  const table = document.createElement("table");
  table.className = "result-table";
  table.setAttribute("aria-label", `表格 ${block.block_id}`);
  const rows = Array.from({ length: block.table.rows }, () => {
    const row = document.createElement("tr");
    table.append(row);
    return row;
  });
  const cells = [...block.table.cells].sort((left, right) =>
    left.row - right.row || left.column - right.column);
  for (const cell of cells) {
    const child = blockById.get(cell.block_ref.block_id);
    const row = rows[cell.row];
    if (!child || !row) continue;
    const element = document.createElement(cell.role === "header" ? "th" : "td");
    element.dataset.blockId = child.block_id;
    element.rowSpan = cell.row_span;
    element.colSpan = cell.column_span;
    appendBlockContent(element, child, state.view);
    if (state.view === "zh") addEditor(element, child);
    const edit = createSourceEditButton(child);
    if (edit) element.prepend(edit);
    if (state.editingSourceBlockId === child.block_id) addSourceEditor(element, child);
    bindResultBlock(element, child.block_id);
    row.append(element);
  }
  container.append(table);
}

function safeHref(value) {
  if (typeof value !== "string") return null;
  const target = value.trim();
  if (/^(?:https?:|mailto:|#|\/)/i.test(target)) return target;
  return null;
}

function bindResultBlock(element, blockId) {
  element.addEventListener("mouseenter", () => setHover(blockId));
  element.addEventListener("mouseleave", () => setHover(null));
  element.addEventListener("click", (event) => {
    if (event.target.closest("textarea,button,input,select,a")) return;
    if (window.getSelection()?.toString()) return;
    event.stopPropagation();
    selectBlock(blockId, false);
    scrollToResultBlock(blockId);
    void pdfReader.focusBlock(blockId);
  });
}

function blockUnits(block) {
  return (block.source_nodes || [])
    .map((node) => state.translations.get(`${block.block_id}:${node.node_id}`))
    .filter(Boolean);
}

function createSourceEditButton(block) {
  if (!editableSourceNodes(block).length) return null;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-button block-edit-button";
  button.setAttribute("aria-label", `纠正 ${block.block_id}`);
  button.title = "纠正";
  button.innerHTML = '<span aria-hidden="true">✎</span>';
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    state.editingSourceBlockId = state.editingSourceBlockId === block.block_id
      ? null
      : block.block_id;
    if (state.editingSourceBlockId) {
      state.selectedBlocks = new Set([block.block_id]);
      pdfReader.setSelected(state.selectedBlocks);
    }
    renderResult();
  });
  return button;
}

function applyTextareaFormat(textarea, type) {
  textarea.focus();
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const val = textarea.value;
  const selectedText = val.substring(start, end);

  if (type === "bold") {
    const wrapped = `**${selectedText || "粗体文字"}**`;
    textarea.setRangeText(wrapped, start, end, "select");
  } else if (type === "italic") {
    const wrapped = `*${selectedText || "斜体文字"}*`;
    textarea.setRangeText(wrapped, start, end, "select");
  } else if (type === "strike") {
    const wrapped = `~~${selectedText || "删除线文字"}~~`;
    textarea.setRangeText(wrapped, start, end, "select");
  } else if (type === "title") {
    const lineStart = val.lastIndexOf("\n", start - 1) + 1;
    const lineEnd = val.indexOf("\n", end);
    const actualLineEnd = lineEnd === -1 ? val.length : lineEnd;
    const line = val.substring(lineStart, actualLineEnd);
    let newLine;
    if (line.startsWith("### ")) {
      newLine = line.slice(4);
    } else if (line.startsWith("## ")) {
      newLine = "### " + line.slice(3);
    } else if (line.startsWith("# ")) {
      newLine = "## " + line.slice(2);
    } else {
      newLine = "# " + line;
    }
    textarea.setRangeText(newLine, lineStart, actualLineEnd, "select");
  }
  textarea.style.height = "auto";
  textarea.style.height = `${Math.max(60, textarea.scrollHeight)}px`;
}

function renderBlockEditCard(block) {
  const card = document.createElement("article");
  card.className = "block-edit-card";
  card.dataset.blockId = block.block_id;

  const toolbar = document.createElement("div");
  toolbar.className = "block-edit-toolbar";

  const formatGroup = document.createElement("div");
  formatGroup.className = "block-edit-format-group";

  const titleBtn = document.createElement("button");
  titleBtn.type = "button";
  titleBtn.className = "block-format-btn";
  titleBtn.title = "标题格式";
  titleBtn.innerHTML = 'T<sub style="font-size: 10px; bottom: 0;">ᴛ</sub>';

  const boldBtn = document.createElement("button");
  boldBtn.type = "button";
  boldBtn.className = "block-format-btn font-bold";
  boldBtn.title = "加粗";
  boldBtn.textContent = "B";

  const italicBtn = document.createElement("button");
  italicBtn.type = "button";
  italicBtn.className = "block-format-btn font-italic";
  italicBtn.title = "斜体";
  italicBtn.textContent = "I";

  const strikeBtn = document.createElement("button");
  strikeBtn.type = "button";
  strikeBtn.className = "block-format-btn font-strike";
  strikeBtn.title = "删除线";
  strikeBtn.textContent = "S";

  formatGroup.append(titleBtn, boldBtn, italicBtn, strikeBtn);

  const actionGroup = document.createElement("div");
  actionGroup.className = "block-edit-action-group";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "block-edit-cancel-btn";
  cancelBtn.textContent = "取消";
  cancelBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    state.editingSourceBlockId = null;
    renderResult();
  });

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "block-edit-save-btn";
  saveBtn.textContent = "保存";

  actionGroup.append(cancelBtn, saveBtn);
  toolbar.append(formatGroup, actionGroup);
  card.append(toolbar);

  const textarea = document.createElement("textarea");
  textarea.className = "block-edit-textarea";
  textarea.value = blockSourceText(block);
  textarea.setAttribute("aria-label", `纠正文本 ${block.block_id}`);

  titleBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); applyTextareaFormat(textarea, "title"); });
  boldBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); applyTextareaFormat(textarea, "bold"); });
  italicBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); applyTextareaFormat(textarea, "italic"); });
  strikeBtn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); applyTextareaFormat(textarea, "strike"); });

  saveBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    void saveBlockEdit(card, block, textarea.value);
  });

  textarea.addEventListener("input", () => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.max(60, textarea.scrollHeight)}px`;
  });

  textarea.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void saveBlockEdit(card, block, textarea.value);
    } else if (e.key === "Escape") {
      e.preventDefault();
      state.editingSourceBlockId = null;
      renderResult();
    }
  });

  card.addEventListener("click", (e) => {
    e.stopPropagation();
  });

  card.append(textarea);

  requestAnimationFrame(() => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.max(60, textarea.scrollHeight)}px`;
    textarea.focus();
  });

  return card;
}

async function saveBlockEdit(card, block, text) {
  if (!state.document || !state.parse) return;
  const parseId = state.parse.parse_run_id;
  const editableNodes = editableSourceNodes(block);
  if (!editableNodes.length) return;

  const saveBtn = card.querySelector(".block-edit-save-btn");
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "保存中…";
  }

  try {
    const firstNode = editableNodes[0];
    const current = state.sourceEdits.get(sourceEditKey(block.block_id, firstNode.node_id));
    const previousText = editableNodes.length === 1
      ? (current?.effective_text ?? ((block.source_nodes || []).find((n) => n.node_id === firstNode.node_id) ? nodeText(firstNode) : ""))
      : blockSourceText(block);

    if (text !== previousText) {
      const updated = await api(
        `/api/documents/${state.document.document_id}/source-edits`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            parse_id: parseId,
            block_id: block.block_id,
            node_id: firstNode.node_id,
            expected_revision: current?.revision ?? 0,
            text: text,
          }),
        },
      );
      state.sourceEdits.set(sourceEditKey(block.block_id, firstNode.node_id), updated);

      for (let i = 1; i < editableNodes.length; i++) {
        const extraNode = editableNodes[i];
        const extraCurrent = state.sourceEdits.get(sourceEditKey(block.block_id, extraNode.node_id));
        const extraUpdated = await api(
          `/api/documents/${state.document.document_id}/source-edits`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              parse_id: parseId,
              block_id: block.block_id,
              node_id: extraNode.node_id,
              expected_revision: extraCurrent?.revision ?? 0,
              text: "",
            }),
          },
        );
        state.sourceEdits.set(sourceEditKey(block.block_id, extraNode.node_id), extraUpdated);
      }
    }
    state.editingSourceBlockId = null;
    await openParse(parseId);
    notify("原文已纠正并保存");
  } catch (error) {
    notify(error.message);
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "保存";
    }
  }
}

function addSourceEditor(wrapper, block) {
  wrapper.append(renderBlockEditCard(block));
}

function addEditor(wrapper, block) {
  const units = blockUnits(block);
  if (!units.length) return;
  const editor = document.createElement("div");
  editor.className = "translation-editor";
  for (const unit of units) addTranslationUnit(editor, unit, block);
  wrapper.append(editor);
}

function addTranslationUnit(editor, unit, block) {
  const section = document.createElement("section");
  section.className = "translation-unit";
  const label = document.createElement("span");
  label.className = "translation-unit-label";
  label.textContent = unit.unit_id;
  const textarea = document.createElement("textarea");
  textarea.value = unit.effective_text;
  textarea.disabled = unit.locked;
  textarea.setAttribute("aria-label", `译文 ${unit.unit_id}`);
  const actions = document.createElement("div");
  actions.className = "translation-actions";

  const save = document.createElement("button");
  save.type = "button";
  save.textContent = "保存";
  save.disabled = unit.locked;
  save.addEventListener("click", async () => {
    try {
      await api(`/api/documents/${state.document.document_id}/translations/${unit.unit_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parse_id: state.parse.parse_run_id,
          block_id: block.block_id,
          expected_revision: unit.revision,
          text: textarea.value,
        }),
      });
      await openParse(state.parse.parse_run_id);
      notify("译文已保存");
    } catch (error) {
      notify(error.message);
    }
  });

  const lock = document.createElement("button");
  lock.type = "button";
  lock.textContent = unit.locked ? "解锁" : "锁定";
  lock.addEventListener("click", async () => {
    try {
      await api(`/api/documents/${state.document.document_id}/translations/${unit.unit_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parse_id: state.parse.parse_run_id,
          block_id: block.block_id,
          expected_revision: unit.revision,
          locked: !unit.locked,
        }),
      });
      await openParse(state.parse.parse_run_id);
    } catch (error) {
      notify(error.message);
    }
  });

  const automatic = document.createElement("button");
  automatic.type = "button";
  automatic.textContent = "使用自动译文";
  automatic.disabled = !unit.auto_text;
  automatic.addEventListener("click", async () => {
    try {
      await api(`/api/documents/${state.document.document_id}/translations/${unit.unit_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parse_id: state.parse.parse_run_id,
          block_id: block.block_id,
          expected_revision: unit.revision,
          use_manual: false,
          locked: false,
        }),
      });
      await openParse(state.parse.parse_run_id);
    } catch (error) {
      notify(error.message);
    }
  });
  actions.append(save, lock, automatic);

  const history = document.createElement("details");
  history.className = "history";
  const summary = document.createElement("summary");
  summary.textContent = "近期修订";
  history.append(summary);
  history.addEventListener("toggle", async () => {
    if (!history.open || history.dataset.loaded) return;
    history.dataset.loaded = "true";
    try {
      const entries = await api(
        `/api/documents/${state.document.document_id}/translations/${unit.unit_id}/history?parse_id=${encodeURIComponent(state.parse.parse_run_id)}`,
      );
      for (const entry of entries) {
        const row = document.createElement("div");
        row.className = "history-entry";
        const value = document.createElement("span");
        value.textContent = entry.text;
        const restore = document.createElement("button");
        restore.type = "button";
        restore.textContent = "恢复";
        restore.addEventListener("click", async () => {
          try {
            await api(`/api/documents/${state.document.document_id}/translations/${unit.unit_id}/history/${entry.id}/restore`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                parse_id: state.parse.parse_run_id,
                block_id: block.block_id,
                expected_revision: unit.revision,
              }),
            });
            await openParse(state.parse.parse_run_id);
          } catch (error) {
            notify(error.message);
          }
        });
        row.append(value, restore);
        history.append(row);
      }
    } catch (error) {
      notify(error.message);
    }
  });
  section.append(label, textarea, actions, history);
  editor.append(section);
}

function setHover(blockId) {
  state.hoverBlock = blockId;
  pdfReader.setHover(blockId);
  for (const element of document.querySelectorAll("[data-block-id]")) {
    element.classList.toggle("is-hovered", element.dataset.blockId === blockId);
  }
}

function selectBlock(blockId, additive) {
  if (additive) {
    if (state.selectedBlocks.has(blockId)) state.selectedBlocks.delete(blockId);
    else state.selectedBlocks.add(blockId);
  } else {
    state.selectedBlocks = new Set([blockId]);
  }
  state.activeQaAnchorBlockId = [...state.selectedBlocks][0] || null;
  pdfReader.setSelected(state.selectedBlocks);
  renderResult();
  const panel = $("#ai-panel");
  if (panel && !panel.hidden) {
    updateQaAnchorBox();
  }
}

function clearBlockSelection() {
  if (!state.selectedBlocks.size && !state.editingSourceBlockId) return;
  state.selectedBlocks.clear();
  state.editingSourceBlockId = null;
  state.activeQaAnchorBlockId = null;
  pdfReader.setSelected(state.selectedBlocks);
  renderResult();
  const panel = $("#ai-panel");
  if (panel && !panel.hidden) {
    updateQaAnchorBox();
  }
}

function scrollToResultBlock(blockId) {
  const container = $("#result-content");
  if (!container) return;
  const target = container.querySelector(`[data-block-id="${CSS.escape(blockId)}"]`);
  if (target) {
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    window.scrollTo(0, 0);
    target.classList.remove("is-target-flash");
    void target.offsetWidth;
    target.classList.add("is-target-flash");
    setTimeout(() => target.classList.remove("is-target-flash"), 1000);
  }
}

async function runTask(path, body, successMessage) {
  const task = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  state.tasks.set(task.task_id, task);
  renderTasks();
  const finished = await waitTask(task.task_id);
  state.tasks.delete(task.task_id);
  renderTasks();
  if (finished.status !== "succeeded") {
    throw new Error(finished.failure?.message || finished.message || "任务失败");
  }
  await refreshDocuments();
  const followUpIds = Array.isArray(finished.result_ref?.follow_up_task_ids)
    ? finished.result_ref.follow_up_task_ids
    : finished.result_ref?.follow_up_task_id
      ? [finished.result_ref.follow_up_task_id]
      : [];
  for (const followUpId of followUpIds) {
    state.tasks.set(followUpId, {
      task_id: followUpId,
      kind: "translate",
      status: "queued",
      message: "等待解析发布",
    });
    renderTasks();
    void waitTask(followUpId).then(async (followUp) => {
      state.tasks.delete(followUpId);
      renderTasks();
      await refreshDocuments();
      if (followUp.status === "succeeded" && state.document?.document_id === finished.document_id) {
        void openParse(finished.result_ref.parse_id);
      }
    }).catch((error) => notify(error.message));
  }
  if (successMessage) notify(successMessage);
  return finished;
}

async function waitTask(taskId) {
  const existing = state.taskPollers.get(taskId);
  if (existing) return existing;
  const polling = pollTask(taskId);
  state.taskPollers.set(taskId, polling);
  polling.then(
    () => state.taskPollers.delete(taskId),
    () => state.taskPollers.delete(taskId),
  );
  return polling;
}

async function pollTask(taskId) {
  let delay = 650;
  while (true) {
    let task;
    try {
      task = await api(`/api/tasks/${taskId}`);
    } catch (error) {
      if (error?.code !== "TASK_EXPIRED") throw error;
      const previous = state.tasks.get(taskId) || { task_id: taskId };
      task = {
        ...previous,
        status: "failed",
        message: "上次处理已结束，请重新执行",
        failure: {
          code: "TASK_EXPIRED",
          message: "上次处理已结束，请重新执行",
          retryable: true,
        },
      };
    }
    if (task.status === "succeeded") {
      state.tasks.delete(taskId);
    } else {
      state.tasks.set(taskId, task);
    }
    renderTasks();
    if (terminalStatuses.has(task.status)) return task;
    await new Promise((resolve) => window.setTimeout(resolve, delay));
    delay = task.status === "running" ? 1100 : 800;
  }
}

function watchTask(task) {
  if (terminalStatuses.has(task.status) || state.taskWatchers.has(task.task_id)) return;
  state.taskWatchers.add(task.task_id);
  void waitTask(task.task_id).then(async (finished) => {
    await refreshDocuments();
    if (finished.status !== "succeeded" || state.document?.document_id !== finished.document_id) return;
    if (finished.kind === "parse") {
      await openDocument(finished.document_id);
    } else if (finished.kind === "translate") {
      const parseId = finished.scope?.parse_id;
      if (typeof parseId === "string" && state.parse?.parse_run_id === parseId) {
        state.view = "zh";
        state.userPreferredSourceView = false;
        await openParse(parseId);
      }
    } else if (finished.kind === "qa") {
      await loadQaRecords();
    }
  }).catch((error) => notify(error.message))
    .finally(() => state.taskWatchers.delete(task.task_id));
}

async function streamAnswer(taskId, element, signal) {
  let response;
  try {
    response = await fetch(`/api/tasks/${taskId}/answer-stream`, { signal });
  } catch (error) {
    if (error?.name !== "AbortError") return waitTask(taskId);
    return waitTask(taskId);
  }
  if (!response.ok || !response.body) return waitTask(taskId);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = null;
  const consume = (packet) => {
    const lines = packet.split(/\r?\n/);
    const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const data = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    try {
      const payload = JSON.parse(data);
      if (event === "done") {
        finished = payload;
      } else if (typeof payload.delta === "string") {
        element.textContent += payload.delta;
      }
    } catch (error) {
      if (event === "done") throw error;
    }
  };

  try {
    while (!finished) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const packets = buffer.split(/\r?\n\r?\n/);
      buffer = packets.pop() || "";
      for (const packet of packets) consume(packet);
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
  } catch (error) {
    if (error?.name !== "AbortError") {
      const latest = await waitTask(taskId);
      if (latest.answer) element.textContent = latest.answer;
      return latest;
    }
  } finally {
    reader.releaseLock();
  }
  if (finished) {
    if (typeof finished.answer === "string") element.textContent = finished.answer;
    return finished;
  }
  const latest = await waitTask(taskId);
  if (latest.answer) element.textContent = latest.answer;
  return latest;
}

function renderTasks() {
  const list = $("#task-list");
  list.replaceChildren();
  for (const task of state.tasks.values()) {
    if (task.status === "succeeded") continue;
    const item = document.createElement("div");
    item.className = "task-item";
    item.textContent = `${task.kind} · ${task.status}`;
    const detail = document.createElement("small");
    detail.textContent = task.progress === null || task.progress === undefined
      ? task.message || ""
      : `${Math.round(task.progress * 100)}% · ${task.message || ""}`;
    item.append(detail);
    if (task.status === "failed" && task.failure?.retryable) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "icon-button action-icon-btn task-action retry-action";
      retry.setAttribute("aria-label", "重试任务");
      retry.dataset.tooltip = "重试任务";
      retry.title = "重试任务";
      retry.disabled = state.retryingTasks.has(task.task_id);
      retry.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M13.5 8C13.5 11.0376 11.0376 13.5 8 13.5C4.96243 13.5 2.5 11.0376 2.5 8C2.5 4.96243 4.96243 2.5 8 2.5C10.15 2.5 12.02 3.73 12.94 5.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M13.5 2.5V5.5H10.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
      retry.addEventListener("click", () => void retryTask(task));
      item.append(retry);
    }
    if (task.status === "queued" || task.status === "running") {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "icon-button action-icon-btn task-action cancel-action";
      cancel.setAttribute("aria-label", "取消任务");
      cancel.dataset.tooltip = "取消任务";
      cancel.title = "取消任务";
      cancel.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="3.5" y1="3.5" x2="12.5" y2="12.5"/><line x1="12.5" y1="3.5" x2="3.5" y2="12.5"/></svg>`;
      cancel.addEventListener("click", () => void cancelTask(task.task_id));
      item.append(cancel);
    }
    list.append(item);
  }
  renderParseProgress();
  renderDocumentList();
}

async function cancelTask(taskId) {
  try {
    const updated = await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    state.tasks.set(taskId, updated);
    renderTasks();
  } catch (error) {
    notify(error.message);
  }
}

function parseTaskForCurrentDocument() {
  const documentId = state.document?.document_id;
  if (!documentId) return null;
  return [...state.tasks.values()]
    .filter((task) => task.document_id === documentId && task.kind === "parse")
    .sort((left, right) => new Date(right.created_at || 0) - new Date(left.created_at || 0))[0] || null;
}

function elapsedText(task) {
  const start = Date.parse(task.created_at || "");
  const end = task.finished_at ? Date.parse(task.finished_at) : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `已用时 ${seconds} 秒`;
  return `已用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function renderParseProgress() {
  const panel = $("#parse-progress");
  const task = parseTaskForCurrentDocument();
  const visible = Boolean(task && (task.status === "queued" || task.status === "running" || task.status === "failed" || task.status === "cancelled"));
  panel.hidden = !visible;
  if (!visible) {
    if (state.parseProgressTimer !== null) {
      window.clearInterval(state.parseProgressTimer);
      state.parseProgressTimer = null;
    }
    return;
  }
  const running = task.status === "queued" || task.status === "running";
  const knownProgress = typeof task.progress === "number";
  const indeterminate = running && (
    !knownProgress || /MinerU/i.test(task.message || "")
  );
  const percent = knownProgress ? Math.round(task.progress * 100) : null;
  $("#parse-progress-title").textContent = task.status === "queued"
    ? "等待解析"
    : task.status === "failed"
      ? "解析失败"
      : task.status === "cancelled"
        ? "解析已取消"
        : "正在解析";
  $("#parse-progress-message").textContent = task.message || "处理中";
  $("#parse-progress-percent").textContent = percent === null ? "处理中" : `${percent}%`;
  const bar = $("#parse-progress-bar");
  bar.style.width = `${percent ?? 6}%`;
  bar.classList.toggle("is-indeterminate", indeterminate);
  $("#parse-progress-elapsed").textContent = elapsedText(task);
  const cancel = $("#parse-progress-cancel");
  cancel.hidden = !running;
  cancel.disabled = task.cancel_requested === true;
  if (running && state.parseProgressTimer === null) {
    state.parseProgressTimer = window.setInterval(renderParseProgress, 1000);
  }
  if (!running && state.parseProgressTimer !== null) {
    window.clearInterval(state.parseProgressTimer);
    state.parseProgressTimer = null;
  }
}

async function retryTask(task) {
  if (state.retryingTasks.has(task.task_id)) return;
  state.retryingTasks.add(task.task_id);
  renderTasks();
  try {
    if (task.kind === "parse") {
      await runTask(
        `/api/documents/${task.document_id}/parse`,
        parseRequestBody(task.scope),
        "解析完成",
      );
    }
    if (task.kind === "translate") await runTask(`/api/documents/${task.document_id}/translate`, {
      parse_id: task.scope.parse_id,
      block_ids: task.scope.block_ids,
    }, "翻译完成");
    if (task.kind === "export") await runTask(`/api/documents/${task.document_id}/exports`, {
      parse_id: task.scope.parse_id,
      format: task.scope.format,
      include_bilingual: task.scope.include_bilingual,
    }, "导出完成");
    if (task.kind === "qa") {
      const scope = task.scope;
      const body = qaRequestBody({
        parse_id: scope.parse_id,
        question: scope.question,
        block_ids: scope.block_ids || scope.context?.filter((item) => item.required).map((item) => item.block_id) || [],
        related_block_ids: scope.related_block_ids || [],
        exclude_block_ids: scope.exclude_block_ids || [],
        auto_related: scope.auto_related ?? true,
      });
      await runTask(`/api/documents/${task.document_id}/qa`, body, "回答完成");
    }
    await openDocument(task.document_id);
  } catch (error) {
    notify(error.message);
  } finally {
    state.retryingTasks.delete(task.task_id);
    renderTasks();
  }
}

async function uploadDocument(file) {
  if (!file || state.uploading) return;
  state.uploading = true;
  state.uploadingDoc = {
    name: file.name,
    progress: 0,
  };
  syncToolbar();
  renderDocumentList();

  try {
    const form = new FormData();
    form.append("file", file);

    const item = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/documents");
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          if (state.uploadingDoc) {
            state.uploadingDoc.progress = event.loaded / event.total;
            renderDocumentList();
          }
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch (err) {
            reject(err);
          }
        } else {
          try {
            const data = JSON.parse(xhr.responseText);
            reject(new Error(data.detail || data.message || "上传失败"));
          } catch {
            reject(new Error(`上传失败 (${xhr.status})`));
          }
        }
      };
      xhr.onerror = () => reject(new Error("网络连接失败，请重试"));
      xhr.send(form);
    });

    state.uploadingDoc = null;
    state.uploading = false;
    $("#file-input").value = "";
    syncToolbar();
    await refreshDocuments();
    await openDocument(item.document_id);
    await runTask(
      `/api/documents/${item.document_id}/parse`,
      parseRequestBody(),
      "解析完成",
    );
    await openDocument(item.document_id);
  } catch (error) {
    state.uploadingDoc = null;
    notify(error.message);
  } finally {
    $("#file-input").value = "";
    state.uploading = false;
    state.uploadingDoc = null;
    syncToolbar();
    renderDocumentList();
  }
}

$("#upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void uploadDocument($("#file-input").files[0]);
});

$("#file-input").addEventListener("change", () => {
  if ($("#file-input").files.length) $("#upload-form").requestSubmit();
});

const emptyUploadState = $("#empty-upload-state");
emptyUploadState.addEventListener("dragover", (event) => {
  event.preventDefault();
  $("#empty-upload-dropzone").classList.add("is-dragover");
});
emptyUploadState.addEventListener("dragleave", (event) => {
  if (event.relatedTarget instanceof Node && emptyUploadState.contains(event.relatedTarget)) return;
  $("#empty-upload-dropzone").classList.remove("is-dragover");
});
emptyUploadState.addEventListener("drop", (event) => {
  event.preventDefault();
  $("#empty-upload-dropzone").classList.remove("is-dragover");
  void uploadDocument(event.dataTransfer?.files?.[0]);
});

$("#version-select").addEventListener("change", (event) => {
  void openParse(event.target.value);
});

$("#prev-page").addEventListener("click", () => {
  const currentElem = $("#page-current");
  const current = currentElem
    ? Number(currentElem.textContent) - 1
    : Number($("#page-count").textContent.split("/")[0]) - 1;
  if (Number.isInteger(current)) void pdfReader.goToPage(current - 1);
});

$("#next-page").addEventListener("click", () => {
  const currentElem = $("#page-current");
  const current = currentElem
    ? Number(currentElem.textContent) - 1
    : Number($("#page-count").textContent.split("/")[0]) - 1;
  if (Number.isInteger(current)) void pdfReader.goToPage(current + 1);
});

$("#zoom-out")?.addEventListener("click", () => pdfReader.zoomOut());
$("#zoom-in")?.addEventListener("click", () => pdfReader.zoomIn());
$("#reset-zoom")?.addEventListener("click", () => pdfReader.resetScale());
$("#parse-progress-cancel").addEventListener("click", () => {
  const task = parseTaskForCurrentDocument();
  if (task) void cancelTask(task.task_id);
});

function openSettingsDrawer(activeTab = "settings") {
  const panel = $("#settings-panel");
  if (!panel) return;
  panel.hidden = false;
  $("#settings-button")?.setAttribute("aria-expanded", "true");
  $("#toolbar-settings-button")?.setAttribute("aria-expanded", "true");
  switchSettingsTab(activeTab);
  syncSettingsApiInfo();
}

function closeSettingsDrawer() {
  const panel = $("#settings-panel");
  if (panel) panel.hidden = true;
  $("#settings-button")?.setAttribute("aria-expanded", "false");
  $("#toolbar-settings-button")?.setAttribute("aria-expanded", "false");
}

function switchSettingsTab(tabName) {
  const isSettings = tabName === "settings";
  $("#tab-btn-settings")?.classList.toggle("is-active", isSettings);
  $("#tab-btn-api")?.classList.toggle("is-active", !isSettings);
  const paneSettings = $("#pane-settings");
  const paneApi = $("#pane-api");
  if (paneSettings) paneSettings.hidden = !isSettings;
  if (paneApi) paneApi.hidden = isSettings;
}

function syncSettingsApiInfo() {
  const llmConfigured = state.health?.llm?.configured === true;
  const badge = $("#api-status-badge");
  if (badge) {
    badge.textContent = llmConfigured ? "已连接并就绪" : "未配置大模型";
    badge.classList.toggle("is-ok", llmConfigured);
  }
  const endpoint = $("#api-endpoint-text");
  if (endpoint) {
    endpoint.textContent = state.health?.llm?.base_url || "http://127.0.0.1:8000/v1";
  }
  const modelText = $("#api-model-text");
  if (modelText) {
    modelText.textContent = state.health?.llm?.model || "（未配置）";
  }
}

$("#settings-button")?.addEventListener("click", () => openSettingsDrawer("settings"));
$("#toolbar-settings-button")?.addEventListener("click", () => openSettingsDrawer("settings"));
$("#close-settings-button")?.addEventListener("click", closeSettingsDrawer);
$("#cancel-settings-button")?.addEventListener("click", closeSettingsDrawer);
$("#settings-backdrop")?.addEventListener("click", closeSettingsDrawer);
$("#tab-btn-settings")?.addEventListener("click", () => switchSettingsTab("settings"));
$("#tab-btn-api")?.addEventListener("click", () => switchSettingsTab("api"));
$("#apply-settings-button")?.addEventListener("click", () => {
  closeSettingsDrawer();
  notify("设置已应用");
});

$("#copy-button")?.addEventListener("click", async () => {
  if (!state.parse || $("#copy-button").disabled) return;
  let text = "";
  if (state.view === "json") {
    text = JSON.stringify(state.parse, null, 2);
  } else if (state.view === "raw") {
    text = state.markdownCache.get(state.parse.parse_run_id) || "";
  } else {
    text = state.parse.blocks
      .filter((b) => b.block_type !== "table_cell")
      .map((b) => (state.view === "source" ? blockSourceText(b) : blockTranslationText(b)))
      .filter(Boolean)
      .join("\n\n");
  }
  try {
    await navigator.clipboard.writeText(text);
    notify("已复制当前视图内容至剪贴板");
  } catch {
    notify("复制失败，请重试");
  }
});

$("#model-select")?.addEventListener("change", (event) => {
  state.selectedModelId = event.target.value;
});

$("#reparse-button").addEventListener("click", async () => {
  if (!state.document || $("#reparse-button").disabled) return;
  const documentId = state.document.document_id;
  await withButtonBusy("reparse-button", "解析中…", async () => {
    try {
      await runTask(
        `/api/documents/${documentId}/parse`,
        parseRequestBody(),
        "解析完成",
      );
      if (state.document?.document_id === documentId) await openDocument(documentId);
    } catch (error) {
      notify(error.message);
    }
  });
});

$("#favorite-button")?.addEventListener("click", () => {
  if (!state.document) return;
  void setDocumentFavorite(state.document.document_id, !state.document.favorite);
});

$("#delete-button")?.addEventListener("click", async () => {
  if (!state.document) return;
  openDeleteDialog(state.document.document_id, state.document.name);
});

function openDeleteDialog(documentId, name) {
  state.pendingDeleteDocumentId = documentId;
  $("#delete-confirm-message").textContent = `确定删除“${name}”吗？此操作会移除该文档的解析版本。`;
  $("#document-delete-dialog").showModal();
}

async function deletePendingDocument() {
  const documentId = state.pendingDeleteDocumentId;
  if (!documentId) return;
  $("#confirm-delete-button").disabled = true;
  try {
    state.answerAbortController?.abort();
    await api(`/api/documents/${documentId}`, { method: "DELETE" });
    if (state.document?.document_id === documentId) clearCurrentDocument();
    $("#document-delete-dialog").close();
    await refreshDocuments();
    notify("文档已删除");
  } catch (error) {
    notify(error.message);
  } finally {
    $("#confirm-delete-button").disabled = false;
    state.pendingDeleteDocumentId = null;
    syncToolbar();
  }
}

function clearCurrentDocument() {
  state.document = null;
  state.parse = null;
  state.translations = new Map();
  state.sourceEdits = new Map();
  state.selectedBlocks.clear();
  state.evidenceBlock = null;
  state.questionContext = null;
  state.editingSourceBlockId = null;
  state.answerTaskId = null;
  state.answerAbortController?.abort();
  state.answerAbortController = null;
  $("#stop-answer-button").hidden = true;
  $("#stop-answer-button").disabled = false;
  pdfReader.destroy();
  $("#document-name").textContent = "选择一个文档";
  const sizeElem = $("#document-size");
  if (sizeElem) sizeElem.textContent = "";
  $("#document-status").textContent = "上传后开始解析";
  const currentElem = $("#page-current");
  const totalElem = $("#page-total");
  if (currentElem && totalElem) {
    currentElem.textContent = "0";
    totalElem.textContent = "0";
  } else {
    $("#page-count").textContent = "0 / 0";
  }
  const zoomValue = $("#zoom-value");
  if (zoomValue) zoomValue.textContent = "100%";
  renderVersionSelect();
  renderResult();
  syncFavoriteButton();
  syncToolbar();
}

$("#document-delete-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void deletePendingDocument();
});
$("#cancel-delete-button").addEventListener("click", () => {
  state.pendingDeleteDocumentId = null;
  $("#document-delete-dialog").close();
});
$("#close-delete-button").addEventListener("click", () => {
  state.pendingDeleteDocumentId = null;
  $("#document-delete-dialog").close();
});

$("#translate-button")?.addEventListener("click", async () => {
  if (!state.document || !state.parse || $("#translate-button")?.disabled) return;
  const llmConfigured = state.health?.llm?.configured === true;
  if (!llmConfigured) {
    notify("未配置大模型服务：请在 config.toml 中配置 [llm] 后即可执行全文翻译");
    openSettingsDrawer("api");
    return;
  }
  const documentId = state.document.document_id;
  const parseId = state.parse.parse_run_id;
  const blockIds = state.selectedBlocks.size ? [...state.selectedBlocks] : null;
  await withButtonBusy("translate-button", "翻译中…", async () => {
    try {
      await runTask(`/api/documents/${documentId}/translate`, {
        parse_id: parseId,
        block_ids: blockIds,
      }, "翻译完成");
      if (state.document?.document_id === documentId && state.parse?.parse_run_id === parseId) {
        await openParse(parseId);
      }
    } catch (error) {
      notify(error.message);
    }
  });
});

$("#download-button").addEventListener("click", async () => {
  if (!state.document || !state.parse || $("#download-button").disabled) return;
  const documentId = state.document.document_id;
  const parseId = state.parse.parse_run_id;
  await withButtonBusy("download-button", "导出中…", async () => {
    try {
      const task = await runTask(`/api/documents/${documentId}/exports`, {
        parse_id: parseId,
        format: "zip",
      }, "导出完成");
      window.open(`/api/documents/${documentId}/files/${task.result_ref.file_id}`, "_blank");
    } catch (error) {
      notify(error.message);
    }
  });
});

$("#qa-button")?.addEventListener("click", () => {
  if (!state.document || !state.parse || $("#qa-button").disabled) return;
  const panel = $("#ai-panel");
  if (panel && !panel.hidden) {
    closeQaDrawer();
  } else {
    openQaDrawer();
  }
});

$("#close-qa-button")?.addEventListener("click", closeQaDrawer);
$("#ai-backdrop")?.addEventListener("click", closeQaDrawer);
$("#qa-anchor-box")?.addEventListener("click", () => void locateAnchorBlock());
$("#qa-anchor-box")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    void locateAnchorBlock();
  }
});
$("#adopt-selection-button")?.addEventListener("click", () => {
  state.selectedQaId = null;
  state.activeQaAnchorBlockId = [...state.selectedBlocks][0] || null;
  updateQaAnchorBox();
  notify("已将当前所选块设为问题锚点");
});

document.querySelectorAll(".quick-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const prompt = btn.dataset.prompt;
    const input = $("#question-input");
    if (input && prompt) {
      input.value = prompt;
      $("#qa-form")?.requestSubmit();
    }
  });
});

$("#question-input")?.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    $("#qa-form")?.requestSubmit();
  }
});

$("#qa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.document || !state.parse || state.busyButtons.has("ask-button")) return;
  const q = $("#question-input")?.value?.trim();
  if (!q) {
    notify("请输入要解读的问题");
    return;
  }
  const llmConfigured = state.health?.llm?.configured === true;
  if (!llmConfigured) {
    const answer = $("#answer-content");
    const answerState = $("#answer-state");
    if (answerState) answerState.textContent = "未配置 LLM";
    if (answer) {
      answer.innerHTML = `⚠️ <strong>大模型服务尚未配置</strong><br>请在项目根目录 <code>config.toml</code> 中添加 <code>[llm]</code> 配置段（如 <code>base_url</code> 和 <code>model</code>），即可启用基于整篇文档或选中段落的深度 AI 解读与问答。`;
    }
    notify("请在 config.toml 中配置 [llm] 后启用大模型解读");
    openSettingsDrawer("api");
    return;
  }
  const documentId = state.document.document_id;
  const answer = $("#answer-content");
  const answerState = $("#answer-state");
  const body = qaRequestBody();
  state.selectedQaId = null;
  if (answerState) answerState.textContent = "生成中…";
  await withButtonBusy("ask-button", "回答中…", async () => {
    answer.textContent = "";
    let taskId = null;
    try {
      const task = await api(`/api/documents/${documentId}/qa`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      taskId = task.task_id;
      state.answerTaskId = taskId;
      state.answerAbortController = new AbortController();
      state.tasks.set(taskId, task);
      renderTasks();
      $("#stop-answer-button").hidden = false;
      $("#stop-answer-button").disabled = false;
      const finished = await streamAnswer(
        taskId,
        answer,
        state.answerAbortController.signal,
      );
      if (state.answerTaskId !== taskId) return;
      if (finished.status !== "succeeded") {
        answer.textContent = finished.answer || finished.failure?.message || finished.message || "回答未完成";
        if (answerState) answerState.textContent = "未完成";
        return;
      }
      state.tasks.delete(taskId);
      renderTasks();
      if (answerState) answerState.textContent = "已完成";
      state.selectedQaId = finished.result_ref?.qa_id || null;
      await loadQaRecords();
    } catch (error) {
      if (state.answerTaskId === taskId || taskId === null) {
        answer.textContent = error.message;
        if (answerState) answerState.textContent = "出错";
      }
    } finally {
      if (state.answerTaskId === taskId) {
        state.answerTaskId = null;
        state.answerAbortController = null;
        $("#stop-answer-button").hidden = true;
        $("#stop-answer-button").disabled = false;
      }
    }
  });
});

$("#stop-answer-button").addEventListener("click", async () => {
  const taskId = state.answerTaskId;
  if (!taskId || $("#stop-answer-button").disabled) return;
  $("#stop-answer-button").disabled = true;
  state.answerAbortController?.abort();
  try {
    const updated = await api(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    state.tasks.set(taskId, updated);
    renderTasks();
  } catch (error) {
    notify(error.message);
    if (state.answerTaskId === taskId) $("#stop-answer-button").disabled = false;
  }
});

$("#qa-history-select").addEventListener("change", (event) => {
  const record = state.qaRecords.find((item) => item.qa_id === event.target.value) || null;
  state.selectedQaId = record?.qa_id || null;
  renderQaRecord(record);
});

$("#delete-qa-button").addEventListener("click", async () => {
  const documentId = state.document?.document_id;
  const qaId = state.selectedQaId;
  if (!documentId || !qaId || !window.confirm("确定删除这条问答记录吗？")) return;
  try {
    await api(`/api/documents/${documentId}/qa/${qaId}`, { method: "DELETE" });
    state.selectedQaId = null;
    await loadQaRecords();
    notify("问答记录已删除");
  } catch (error) {
    notify(error.message);
  }
});

$("#search-toggle")?.addEventListener("click", () => {
  const search = $("#result-search");
  const hidden = !search.hidden;
  search.hidden = hidden;
  $("#search-toggle").setAttribute("aria-expanded", String(!hidden));
  if (!hidden) $("#result-search-input").focus();
});

$("#result-search-input").addEventListener("input", (event) => {
  state.searchQuery = event.target.value;
  renderResult();
});

$("#clear-selection").addEventListener("click", () => {
  clearBlockSelection();
});

$("#result-content")?.addEventListener("click", (event) => {
  if (event.target.closest(".result-block, .block-edit-card, button, input, textarea, select, a")) return;
  if (window.getSelection()?.toString()) return;
  clearBlockSelection();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const sortPopover = $("#doc-sort-popover");
    if (sortPopover && !sortPopover.hidden) {
      sortPopover.hidden = true;
      $("#doc-sort-filter-btn")?.setAttribute("aria-expanded", "false");
      $("#doc-sort-filter-btn")?.classList.remove("is-active");
      return;
    }
    const aiPanel = $("#ai-panel");
    if (aiPanel && !aiPanel.hidden) {
      closeQaDrawer();
      return;
    }
    const settingsPanel = $("#settings-panel");
    if (settingsPanel && !settingsPanel.hidden) {
      closeSettingsDrawer();
      return;
    }
    if (state.selectedBlocks.size || state.editingSourceBlockId) {
      clearBlockSelection();
    }
  }
});

function initDocumentSidebarControls() {
  const recentTab = $("#doc-tab-recent");
  const favoriteTab = $("#doc-tab-favorite");
  const searchToggleBtn = $("#doc-search-toggle-btn");
  const searchBar = $("#doc-search-bar");
  const searchInput = $("#doc-search-input");
  const searchClearBtn = $("#doc-search-clear-btn");
  const sortFilterBtn = $("#doc-sort-filter-btn");
  const sortPopover = $("#doc-sort-popover");
  const sortCancelBtn = $("#doc-sort-cancel-btn");
  const sortConfirmBtn = $("#doc-sort-confirm-btn");

  recentTab?.addEventListener("click", () => {
    if (state.docFilter.tab === "recent") return;
    state.docFilter.tab = "recent";
    recentTab.classList.add("is-active");
    recentTab.setAttribute("aria-selected", "true");
    favoriteTab?.classList.remove("is-active");
    favoriteTab?.setAttribute("aria-selected", "false");
    renderDocumentList();
  });

  favoriteTab?.addEventListener("click", () => {
    if (state.docFilter.tab === "favorite") return;
    state.docFilter.tab = "favorite";
    favoriteTab.classList.add("is-active");
    favoriteTab.setAttribute("aria-selected", "true");
    recentTab?.classList.remove("is-active");
    recentTab?.setAttribute("aria-selected", "false");
    renderDocumentList();
  });

  searchToggleBtn?.addEventListener("click", () => {
    if (!searchBar) return;
    const willShow = searchBar.hidden;
    searchBar.hidden = !willShow;
    searchToggleBtn.classList.toggle("is-active", willShow);
    searchToggleBtn.setAttribute("aria-expanded", String(willShow));
    if (willShow) {
      searchInput?.focus();
    } else if (state.docFilter.searchKeyword) {
      state.docFilter.searchKeyword = "";
      if (searchInput) searchInput.value = "";
      if (searchClearBtn) searchClearBtn.hidden = true;
      renderDocumentList();
    }
  });

  searchInput?.addEventListener("input", (e) => {
    const val = e.target.value.trim();
    state.docFilter.searchKeyword = val;
    if (searchClearBtn) searchClearBtn.hidden = !val;
    renderDocumentList();
  });

  searchClearBtn?.addEventListener("click", () => {
    if (searchInput) {
      searchInput.value = "";
      searchInput.focus();
    }
    state.docFilter.searchKeyword = "";
    searchClearBtn.hidden = true;
    renderDocumentList();
  });

  function openSortPopover() {
    if (!sortPopover || !sortFilterBtn) return;
    const sortRadio = sortPopover.querySelector(`input[name="popover-sort"][value="${state.docFilter.sortOrder}"]`);
    if (sortRadio) sortRadio.checked = true;
    const typeRadio = sortPopover.querySelector(`input[name="popover-filetype"][value="${state.docFilter.fileType}"]`);
    if (typeRadio) typeRadio.checked = true;
    const statusRadio = sortPopover.querySelector(`input[name="popover-status"][value="${state.docFilter.parseStatus}"]`);
    if (statusRadio) statusRadio.checked = true;

    const rect = sortFilterBtn.getBoundingClientRect();
    const arrowOffset = 36;
    const btnCenterX = rect.left + rect.width / 2;
    let targetLeft = btnCenterX - arrowOffset;
    const popoverWidth = 445;
    if (targetLeft + popoverWidth > window.innerWidth - 10) {
      targetLeft = window.innerWidth - popoverWidth - 10;
    }
    if (targetLeft < 10) targetLeft = 10;
    sortPopover.style.top = `${rect.bottom + 8}px`;
    sortPopover.style.left = `${targetLeft}px`;

    sortPopover.hidden = false;
    sortFilterBtn.setAttribute("aria-expanded", "true");
    sortFilterBtn.classList.add("is-active");
  }

  function closeSortPopover() {
    if (!sortPopover || !sortFilterBtn) return;
    sortPopover.hidden = true;
    sortFilterBtn.setAttribute("aria-expanded", "false");
    sortFilterBtn.classList.remove("is-active");
  }

  sortFilterBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (sortPopover && !sortPopover.hidden) {
      closeSortPopover();
    } else {
      openSortPopover();
    }
  });

  sortCancelBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    closeSortPopover();
  });

  sortConfirmBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!sortPopover) return;
    const selectedSort = sortPopover.querySelector('input[name="popover-sort"]:checked')?.value || "desc";
    const selectedType = sortPopover.querySelector('input[name="popover-filetype"]:checked')?.value || "all";
    const selectedStatus = sortPopover.querySelector('input[name="popover-status"]:checked')?.value || "all";

    state.docFilter.sortOrder = selectedSort;
    state.docFilter.fileType = selectedType;
    state.docFilter.parseStatus = selectedStatus;

    closeSortPopover();
    renderDocumentList();
  });

  sortPopover?.addEventListener("click", (e) => {
    e.stopPropagation();
  });

  document.addEventListener("click", (e) => {
    if (sortPopover && !sortPopover.hidden) {
      if (!sortPopover.contains(e.target) && !sortFilterBtn?.contains(e.target)) {
        closeSortPopover();
      }
    }
  });

  window.addEventListener("resize", () => {
    if (sortPopover && !sortPopover.hidden) {
      closeSortPopover();
    }
  });
}

$("#toggle-reader-button")?.addEventListener("click", () => {
  toggleReaderView();
});

for (const tab of document.querySelectorAll(".result-tab")) {
  tab.addEventListener("click", () => {
    if (tab.disabled) return;
    setResultView(tab.dataset.view);
  });
}

initDocumentSidebarControls();
syncWorkspaceLayout();
void Promise.all([refreshHealth(), refreshModels(), refreshDocuments()]).catch((error) => notify(error.message));

window.__easyLearn = {
  state,
  renderDocumentList,
  getFilteredAndSortedDocuments,
  createDocumentItemElement,
  refreshDocuments,
  updateReaderVisibility,
  setReaderHidden,
  toggleReaderView,
};
