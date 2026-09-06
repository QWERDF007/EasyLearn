import { PdfReader } from "./pdf-viewer.js";

const state = {
  documents: [],
  document: null,
  parse: null,
  translations: new Map(),
  selectedBlocks: new Set(),
  hoverBlock: null,
  view: "zh",
  searchQuery: "",
  tasks: new Map(),
  taskWatchers: new Set(),
  health: null,
  markdownCache: new Map(),
  qaRecords: [],
  selectedQaId: null,
  answerTaskId: null,
  answerAbortController: null,
  evidenceBlock: null,
  questionContext: null,
  retryingTasks: new Set(),
  busyButtons: new Set(),
  documentLoadGeneration: 0,
  parseLoadGeneration: 0,
};

const $ = (selector) => document.querySelector(selector);
const terminalStatuses = new Set(["succeeded", "failed", "cancelled"]);
const pdfReader = new PdfReader($("#pdf-viewer"), {
  onPageChange(pageIndex, pageCount) {
    $("#page-count").textContent = `${pageIndex + 1} / ${pageCount}`;
    $("#prev-page").disabled = pageIndex <= 0;
    $("#next-page").disabled = pageIndex >= pageCount - 1;
  },
  onBlockClick(blockId) {
    selectBlock(blockId, false);
    scrollToResultBlock(blockId);
  },
  onBlockHover(blockId) {
    setHover(blockId);
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

function selectedParseId() {
  return state.document?.active_parse_id || state.document?.parse_results?.[0]?.parse_id || null;
}

async function refreshDocuments() {
  state.documents = await api("/api/documents");
  const list = $("#document-list");
  list.replaceChildren();
  for (const item of state.documents) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "document-item" + (
      state.document?.document_id === item.document_id ? " is-active" : ""
    );
    button.textContent = item.name;
    const detail = document.createElement("small");
    detail.textContent = item.active_parse_id ? "已解析" : "待解析";
    button.append(detail);
    button.addEventListener("click", () => void openDocument(item.document_id));
    list.append(button);
  }
}

async function openDocument(documentId) {
  const generation = ++state.documentLoadGeneration;
  try {
    const loaded = await api(`/api/documents/${documentId}`);
    if (generation !== state.documentLoadGeneration) return;
    state.document = loaded;
    state.parse = null;
    state.translations = new Map();
    state.markdownCache.clear();
    state.selectedBlocks.clear();
    state.searchQuery = "";
    $("#result-search-input").value = "";
    state.questionContext = null;
    state.qaRecords = [];
    state.selectedQaId = null;
    state.answerTaskId = null;
    state.answerAbortController?.abort();
    state.answerAbortController = null;
    $("#stop-answer-button").hidden = true;
    $("#stop-answer-button").disabled = false;
    $("#document-name").textContent = loaded.name;
    $("#favorite-button").textContent = loaded.favorite ? "取消收藏" : "收藏";
    for (const task of loaded.tasks || []) {
      state.tasks.set(task.task_id, task);
      watchTask(task);
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
    await refreshDocuments();
  } catch (error) {
    if (generation === state.documentLoadGeneration) notify(error.message);
  }
}

async function openParse(parseId, documentGeneration = state.documentLoadGeneration) {
  if (!state.document) return;
  const generation = ++state.parseLoadGeneration;
  const documentId = state.document.document_id;
  try {
    const [parse, translations] = await Promise.all([
      api(`/api/documents/${documentId}/parses/${parseId}`),
      api(`/api/documents/${documentId}/translations?parse_id=${encodeURIComponent(parseId)}`),
    ]);
    if (generation !== state.parseLoadGeneration || documentGeneration !== state.documentLoadGeneration) return;
    state.parse = parse;
    state.translations = new Map(translations.map((item) => [item.unit_id, item]));
    state.selectedBlocks = new Set([...state.selectedBlocks].filter((id) =>
      parse.blocks.some((block) => block.block_id === id)
    ));
    renderVersionSelect(parseId);
    syncToolbar();
    $("#document-status").textContent = "解析结果已加载";
    await pdfReader.load(
      `/api/documents/${documentId}/files/preview:${parseId}`,
      parse.pages,
      parse.blocks,
    );
    if (generation !== state.parseLoadGeneration || documentGeneration !== state.documentLoadGeneration) return;
    pdfReader.setSelected(state.selectedBlocks);
    renderResult();
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

function syncToolbar() {
  const hasDocument = Boolean(state.document);
  const hasParse = Boolean(state.parse);
  const llmConfigured = state.health?.llm?.configured === true;
  const qaEnabled = state.health?.extensions?.qa_enabled === true;
  const officeEnabled = state.health?.extensions?.office_enabled === true;
  $("#parse-button").disabled = !hasDocument || state.busyButtons.has("parse-button");
  $("#translate-button").disabled = !hasParse || !llmConfigured || state.busyButtons.has("translate-button");
  $("#qa-button").disabled = !hasParse || !qaEnabled || state.busyButtons.has("ask-button");
  $("#export-button").disabled = !hasParse || state.busyButtons.has("export-button");
  $("#favorite-button").disabled = !hasDocument;
  $("#delete-button").disabled = !hasDocument;
  const isXlsx = Boolean(state.document?.name?.toLowerCase().endsWith(".xlsx"));
  $("#office-options").hidden = !isXlsx || !officeEnabled;
  $("#office-sheet").disabled = !isXlsx || !officeEnabled;
  $("#office-print-range").disabled = !isXlsx || !officeEnabled;
  $("#auto-translate").disabled = !hasDocument || !llmConfigured;
}

async function withButtonBusy(buttonId, busyLabel, work) {
  if (state.busyButtons.has(buttonId)) return undefined;
  const button = $(`#${buttonId}`);
  if (!button) return undefined;
  const idleLabel = button.textContent;
  state.busyButtons.add(buttonId);
  button.textContent = busyLabel;
  syncToolbar();
  try {
    return await work();
  } finally {
    state.busyButtons.delete(buttonId);
    button.textContent = idleLabel;
    syncToolbar();
  }
}

function parseRequestBody(scope = null) {
  const source = scope || {};
  const body = {};
  if (source.options && typeof source.options === "object") {
    body.options = source.options;
  }

  const office = source.office && typeof source.office === "object"
    ? { ...source.office }
    : {};
  if (!scope && state.document?.name?.toLowerCase().endsWith(".xlsx")) {
    const sheet = $("#office-sheet").value.trim();
    const printRange = $("#office-print-range").value.trim();
    if (sheet) office.sheet = sheet;
    if (printRange) office.print_range = printRange;
  }
  if (Object.keys(office).length) body.office = office;

  body.auto_translate = typeof source.auto_translate === "boolean"
    ? source.auto_translate
    : $("#auto-translate").checked;
  return body;
}

function parseBlockIds(value) {
  return [...new Set(value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean))];
}

function qaRequestBody(scope = null) {
  const source = scope || {};
  const context = {
    parse_id: source.parse_id || state.parse.parse_run_id,
    question: typeof source.question === "string" ? source.question : $("#question-input").value,
    block_ids: Array.isArray(source.block_ids) ? [...source.block_ids] : [...state.selectedBlocks],
    related_block_ids: Array.isArray(source.related_block_ids)
      ? [...source.related_block_ids]
      : parseBlockIds($("#qa-related-blocks").value),
    exclude_block_ids: Array.isArray(source.exclude_block_ids)
      ? [...source.exclude_block_ids]
      : parseBlockIds($("#qa-exclude-blocks").value),
    auto_related: typeof source.auto_related === "boolean"
      ? source.auto_related
      : $("#qa-auto-related").checked,
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
  select.replaceChildren();
  if (!state.qaRecords.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无历史提问";
    select.append(option);
    select.disabled = true;
    $("#delete-qa-button").hidden = true;
    return;
  }
  for (const record of state.qaRecords) {
    const option = document.createElement("option");
    option.value = record.qa_id;
    option.textContent = `${record.question.slice(0, 48)} · ${new Date(record.created_at).toLocaleString()}`;
    option.selected = record.qa_id === state.selectedQaId;
    select.append(option);
  }
  select.disabled = false;
  $("#delete-qa-button").hidden = !state.selectedQaId;
}

function renderQaRecord(record) {
  if (!record) {
    $("#answer-content").textContent = "";
    $("#citation-list").replaceChildren();
    $("#delete-qa-button").hidden = true;
    return;
  }
  state.selectedQaId = record.qa_id;
  $("#question-input").value = record.question;
  $("#answer-content").textContent = record.answer;
  const list = $("#citation-list");
  list.replaceChildren();
  for (const citation of record.citations || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "citation";
    button.textContent = `[${citation.citation}] ${citation.text}`;
    button.addEventListener("click", () => void focusCitation(citation));
    list.append(button);
  }
  $("#delete-qa-button").hidden = false;
}

async function focusCitation(citation) {
  const parseId = citation.block_ref?.parse_run_id;
  const blockId = citation.block_id;
  if (typeof parseId !== "string" || typeof blockId !== "string") {
    notify("引用缺少可定位信息");
    return;
  }
  try {
    if (state.parse?.parse_run_id !== parseId) {
      await openParse(parseId);
    }
    if (state.parse?.parse_run_id !== parseId) {
      notify("引用对应的解析版本已不可用");
      return;
    }
    state.evidenceBlock = blockId;
    setResultView("zh");
    selectBlock(blockId, false);
    await pdfReader.focusBlock(blockId);
    $("#qa-dialog").close();
  } catch (error) {
    notify(error.message);
  }
}

function message(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "empty-state";
  paragraph.textContent = text;
  return paragraph;
}

function blockSourceText(block) {
  return block.source_text || (block.source_nodes || []).map(nodeText).join("");
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

function syncResultTabs() {
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
  syncResultTabs();
  $("#selection-count").textContent = state.selectedBlocks.size
    ? `已选 ${state.selectedBlocks.size} 块`
    : "";
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
    const wrapper = document.createElement("article");
    wrapper.className = "result-block" + (
      state.selectedBlocks.has(block.block_id) ? " is-selected" : ""
    );
    wrapper.dataset.blockId = block.block_id;

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
    bindResultBlock(wrapper, block.block_id);
    content.append(wrapper);
  }
  pdfReader.setSelected(state.selectedBlocks);
}

function setResultView(view) {
  state.view = view;
  syncResultTabs();
  renderResult();
}

function nodeText(node) {
  return node.text || node.label || node.alt || node.latex || node.code || "";
}

function appendInlineNodes(container, block, language) {
  for (const node of block.source_nodes || []) {
    const unit = state.translations.get(`${block.block_id}:${node.node_id}`);
    const translated = language === "chinese" ? unit?.effective_text : null;
    if (node.type === "text") {
      const text = document.createElement("span");
      text.textContent = translated ?? node.text ?? "";
      container.append(text);
    } else if (node.type === "math") {
      const math = document.createElement("span");
      math.className = "math-node";
      math.setAttribute("aria-label", "数学公式");
      math.textContent = `\\(${node.latex || ""}\\)`;
      container.append(math);
    } else if (node.type === "code") {
      const code = document.createElement("code");
      code.className = "inline-code";
      code.textContent = node.code || "";
      container.append(code);
    } else if (node.type === "link") {
      const link = document.createElement("a");
      link.textContent = translated ?? node.label ?? "";
      const href = safeHref(node.target);
      if (href) link.href = href;
      else link.title = "已隐藏不安全链接";
      container.append(link);
    } else if (node.type === "reference") {
      const reference = document.createElement("button");
      reference.type = "button";
      reference.className = "inline-reference";
      reference.textContent = translated ?? node.label ?? "";
      reference.addEventListener("click", (event) => {
        event.stopPropagation();
        const target = node.target?.block_id;
        if (!target) return;
        selectBlock(target, false);
        void pdfReader.focusBlock(target);
      });
      container.append(reference);
    } else if (node.type === "image") {
      const image = document.createElement("img");
      image.className = "inline-image";
      image.loading = "lazy";
      image.alt = translated ?? node.alt ?? "";
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
  if (block.block_type === "code") {
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
    selectBlock(blockId, event.ctrlKey || event.metaKey);
    void pdfReader.focusBlock(blockId);
  });
}

function blockUnits(block) {
  return (block.source_nodes || [])
    .map((node) => state.translations.get(`${block.block_id}:${node.node_id}`))
    .filter(Boolean);
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
  pdfReader.setSelected(state.selectedBlocks);
  renderResult();
}

function scrollToResultBlock(blockId) {
  document.querySelector(`[data-block-id="${CSS.escape(blockId)}"]`)
    ?.scrollIntoView({ block: "center", behavior: "auto" });
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
  if (finished.status !== "succeeded") {
    throw new Error(finished.failure?.message || finished.message || "任务失败");
  }
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
    void waitTask(followUpId).then((followUp) => {
      if (followUp.status === "succeeded" && state.document?.document_id === finished.document_id) {
        void openParse(finished.result_ref.parse_id);
      }
    }).catch((error) => notify(error.message));
  }
  if (successMessage) notify(successMessage);
  return finished;
}

async function waitTask(taskId) {
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
    state.tasks.set(taskId, task);
    renderTasks();
    if (terminalStatuses.has(task.status)) return task;
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
}

function watchTask(task) {
  if (terminalStatuses.has(task.status) || state.taskWatchers.has(task.task_id)) return;
  state.taskWatchers.add(task.task_id);
  void waitTask(task.task_id).then(async (finished) => {
    if (finished.status !== "succeeded" || state.document?.document_id !== finished.document_id) return;
    if (finished.kind === "parse") {
      await openDocument(finished.document_id);
    } else if (finished.kind === "translate") {
      const parseId = finished.scope?.parse_id;
      if (typeof parseId === "string" && state.parse?.parse_run_id === parseId) {
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
      retry.textContent = "重试";
      retry.disabled = state.retryingTasks.has(task.task_id);
      retry.addEventListener("click", () => void retryTask(task));
      item.append(retry);
    }
    if (task.status === "queued" || task.status === "running") {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "取消";
      cancel.addEventListener("click", async () => {
        try {
          const updated = await api(`/api/tasks/${task.task_id}/cancel`, { method: "POST" });
          state.tasks.set(task.task_id, updated);
          renderTasks();
        } catch (error) {
          notify(error.message);
        }
      });
      item.append(cancel);
    }
    list.append(item);
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

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#file-input").files[0];
  if (!file) return;
  try {
    const form = new FormData();
    form.append("file", file);
    const item = await api("/api/documents", { method: "POST", body: form });
    await refreshDocuments();
    await openDocument(item.document_id);
    await runTask(
      `/api/documents/${item.document_id}/parse`,
      parseRequestBody(),
      "解析完成",
    );
    await openDocument(item.document_id);
  } catch (error) {
    notify(error.message);
  }
});

$("#file-input").addEventListener("change", () => {
  if ($("#file-input").files.length) $("#upload-form").requestSubmit();
});

$("#version-select").addEventListener("change", (event) => {
  void openParse(event.target.value);
});

$("#prev-page").addEventListener("click", () => {
  const current = Number($("#page-count").textContent.split("/")[0]) - 1;
  if (Number.isInteger(current)) void pdfReader.goToPage(current - 1);
});

$("#next-page").addEventListener("click", () => {
  const current = Number($("#page-count").textContent.split("/")[0]) - 1;
  if (Number.isInteger(current)) void pdfReader.goToPage(current + 1);
});

$("#zoom-select").addEventListener("change", (event) => pdfReader.setScale(event.target.value));
$("#fit-width").addEventListener("click", () => pdfReader.fitWidth());
$("#rotate-page").addEventListener("click", () => pdfReader.rotate());

$("#parse-button").addEventListener("click", async () => {
  if (!state.document || $("#parse-button").disabled) return;
  state.busyButtons.add("parse-button");
  $("#parse-button").disabled = true;
  try {
    await runTask(
      `/api/documents/${state.document.document_id}/parse`,
      parseRequestBody(),
      "解析完成",
    );
    await openDocument(state.document.document_id);
  } catch (error) {
    notify(error.message);
  } finally {
    state.busyButtons.delete("parse-button");
    syncToolbar();
  }
});

$("#favorite-button").addEventListener("click", async () => {
  if (!state.document || $("#favorite-button").disabled) return;
  $("#favorite-button").disabled = true;
  try {
    const updated = await api(`/api/documents/${state.document.document_id}/favorite`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favorite: !state.document.favorite }),
    });
    state.document = updated;
    $("#favorite-button").textContent = updated.favorite ? "取消收藏" : "收藏";
    await refreshDocuments();
  } catch (error) {
    notify(error.message);
  } finally {
    syncToolbar();
  }
});

$("#delete-button").addEventListener("click", async () => {
  if (!state.document || !window.confirm("确定删除“" + state.document.name + "”吗？")) return;
  $("#delete-button").disabled = true;
  const documentId = state.document.document_id;
  try {
    state.answerAbortController?.abort();
    await api(`/api/documents/${documentId}`, { method: "DELETE" });
    state.document = null;
    state.parse = null;
    state.translations = new Map();
    state.selectedBlocks.clear();
    state.evidenceBlock = null;
    state.questionContext = null;
    state.answerTaskId = null;
    state.answerAbortController?.abort();
    state.answerAbortController = null;
    $("#stop-answer-button").hidden = true;
    $("#stop-answer-button").disabled = false;
    pdfReader.destroy();
    $("#document-name").textContent = "选择一个文档";
    $("#document-status").textContent = "上传后开始解析";
    $("#page-count").textContent = "0 / 0";
    renderVersionSelect();
    renderResult();
    syncToolbar();
    await refreshDocuments();
    notify("文档已删除");
  } catch (error) {
    notify(error.message);
  } finally {
    syncToolbar();
  }
});

$("#translate-button").addEventListener("click", async () => {
  if (!state.document || !state.parse || $("#translate-button").disabled) return;
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

$("#export-button").addEventListener("click", async () => {
  if (!state.document || !state.parse || $("#export-button").disabled) return;
  const documentId = state.document.document_id;
  const parseId = state.parse.parse_run_id;
  await withButtonBusy("export-button", "导出中…", async () => {
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

$("#qa-button").addEventListener("click", () => {
  if (!state.document || !state.parse || $("#qa-button").disabled) return;
  $("#qa-anchor").textContent = state.selectedBlocks.size
    ? `已固定 ${state.selectedBlocks.size} 个块作为问题锚点。`
    : "当前未选择块，将根据问题查找相关内容。";
  $("#qa-dialog").showModal();
  void loadQaRecords().catch((error) => notify(error.message));
});

$("#close-qa-button").addEventListener("click", () => $("#qa-dialog").close());

$("#qa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.document || !state.parse || state.busyButtons.has("ask-button")) return;
  const documentId = state.document.document_id;
  const answer = $("#answer-content");
  const body = qaRequestBody();
  state.selectedQaId = null;
  await withButtonBusy("ask-button", "回答中…", async () => {
    answer.textContent = "";
    $("#citation-list").replaceChildren();
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
        return;
      }
      state.selectedQaId = finished.result_ref?.qa_id || null;
      await loadQaRecords();
    } catch (error) {
      if (state.answerTaskId === taskId || taskId === null) answer.textContent = error.message;
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

$("#search-toggle").addEventListener("click", () => {
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
  state.selectedBlocks.clear();
  pdfReader.setSelected(state.selectedBlocks);
  renderResult();
});

for (const tab of document.querySelectorAll(".result-tab")) {
  tab.addEventListener("click", () => setResultView(tab.dataset.view));
}

void Promise.all([refreshHealth(), refreshDocuments()]).catch((error) => notify(error.message));
