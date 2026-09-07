import { GlobalWorkerOptions, TextLayer, getDocument } from "./pdfjs/pdf.min.mjs";

GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.min.mjs";

const MIN_SCALE = 0.5;
const MAX_SCALE = 2;

export function blockTypeLabel(type) {
  const map = {
    title: "标题",
    heading: "标题",
    header: "页眉",
    footer: "页脚",
    paragraph: "正文",
    abstract: "摘要",
    table: "表格",
    table_cell: "单元格",
    image: "图片",
    figure: "图表",
    equation: "公式",
    formula: "公式",
    code: "代码",
    reference: "参考文献",
    footnote: "脚注",
  };
  return map[type?.toLowerCase()] || type || "块";
}

export class PdfReader {
  constructor(
    container,
    {
      onPageChange = () => {},
      onError = () => {},
      onBlockClick = () => {},
      onBlankClick = () => {},
      onBlockHover = () => {},
      onScaleChange = () => {},
    } = {},
  ) {
    this.container = container;
    this.pagesContainer = container.querySelector("#pdf-pages");
    this.onPageChange = onPageChange;
    this.onError = onError;
    this.onBlockClick = onBlockClick;
    this.onBlankClick = onBlankClick;
    this.onBlockHover = onBlockHover;
    this.onScaleChange = onScaleChange;
    this.pdf = null;
    this.loadingTask = null;
    this.pageGeometries = [];
    this.blocks = [];
    this.pageStates = [];
    this.hoveredBlock = null;
    this.selectedBlocks = new Set();
    this.scale = 1;
    this.isFitWidth = true;
    this.rotation = 0;
    this.focusGeneration = 0;
    this.pointerStart = null;
    this.currentPage = null;
    this.scrollRafId = null;
    this.pointerMoveRafId = null;
    this.intersectionObserver = new IntersectionObserver(
      (entries) => this.#observePages(entries),
      { root: container, rootMargin: "1200px 0px" },
    );
    this.resizeObserver = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => {
        if (this.pdf && this.isFitWidth) this.fitWidth();
      })
      : null;
    this.resizeObserver?.observe(this.container);
    this.container.addEventListener("scroll", () => {
      if (this.scrollRafId) return;
      this.scrollRafId = requestAnimationFrame(() => {
        this.scrollRafId = null;
        this.#updatePageCounter(null, true);
      });
    }, { passive: true });
    this.container.addEventListener("wheel", (event) => {
      if (
        !event.ctrlKey
        || !(event.target instanceof Element)
        || event.target.closest("#pdf-toolbar")
      ) return;
      event.preventDefault();
      const next = this.scale * Math.exp(-event.deltaY * 0.002);
      this.setScale(next);
    }, { passive: false });
    this.container.addEventListener("pointerdown", (event) => {
      this.pointerStart = { x: event.clientX, y: event.clientY };
    }, { passive: true });
    this.container.addEventListener("pointermove", (event) => this.#handlePointerMove(event), {
      passive: true,
    });
    this.container.addEventListener("pointerleave", () => {
      this.pointerStart = null;
      this.setHover(null);
      this.onBlockHover(null);
    }, { passive: true });
    this.container.addEventListener("click", (event) => this.#handleContainerClick(event));
  }

  async load(url, pageGeometries, blocks) {
    const generation = ++this.focusGeneration;
    this.#disposeDocument();
    this.pageGeometries = pageGeometries || [];
    this.blocks = blocks || [];
    this.pageStates = [];
    this.pagesContainer.replaceChildren();
    if (!url) {
      this.#updateControls(false);
      return;
    }
    this.#updateControls(true);
    this.onScaleChange(this.scale);
    try {
      this.loadingTask = getDocument({ url });
      const pdf = await this.loadingTask.promise;
      if (generation !== this.focusGeneration) {
        await pdf.destroy();
        return;
      }
      this.pdf = pdf;
      this.#createPageShells();
      this.fitWidth();
      await this.#renderNearby(0);
      this.#updatePageCounter(0, false);
    } catch (error) {
      if (generation === this.focusGeneration) this.onError(error);
    } finally {
      this.loadingTask = null;
    }
  }

  destroy() {
    ++this.focusGeneration;
    if (this.scrollRafId) {
      cancelAnimationFrame(this.scrollRafId);
      this.scrollRafId = null;
    }
    if (this.pointerMoveRafId) {
      cancelAnimationFrame(this.pointerMoveRafId);
      this.pointerMoveRafId = null;
    }
    this.#disposeDocument();
    this.intersectionObserver?.disconnect();
    this.resizeObserver?.disconnect();
    this.pagesContainer.replaceChildren();
    this.pageStates = [];
    this.currentPage = null;
    this.#updateControls(false);
  }

  setScale(value, userManual = true) {
    if (value === "") return;
    const scale = Number(value);
    if (!Number.isFinite(scale)) return;
    if (userManual) this.isFitWidth = false;
    const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
    if (Math.abs(nextScale - this.scale) < 0.001) return;
    this.scale = nextScale;
    this.onScaleChange(this.scale);
    this.#resizeShells();
    void this.#rerenderVisible();
  }

  zoomIn(step = 0.1) {
    this.setScale(Math.round((this.scale + step) * 100) / 100);
  }

  zoomOut(step = 0.1) {
    this.setScale(Math.round((this.scale - step) * 100) / 100);
  }

  resetScale() {
    this.fitWidth();
  }

  fitWidth() {
    this.isFitWidth = true;
    const first = this.pageStates[0];
    if (!first) return;
    const geometry = this.pageGeometries[0];
    const sideways = this.#rotationForPage(0) % 180 !== 0;
    const width = geometry
      ? sideways
        ? geometry.crop_box[3] - geometry.crop_box[1]
        : geometry.crop_box[2] - geometry.crop_box[0]
      : sideways ? first.height : first.width;
    const available = Math.max(1, this.container.clientWidth - 44);
    const targetScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, available / width));
    this.setScale(targetScale, false);
  }

  rotate() {
    this.rotation = (this.rotation + 90) % 360;
    this.#resizeShells();
    void this.#rerenderVisible();
  }

  async goToPage(pageIndex) {
    const generation = ++this.focusGeneration;
    const state = this.pageStates[pageIndex];
    if (!state) return;
    state.shell.scrollIntoView({ block: "start", behavior: "auto" });
    await this.#renderPage(state);
    if (generation !== this.focusGeneration) return;
    this.#updatePageCounter(pageIndex, false);
  }

  async focusBlock(blockId) {
    const generation = ++this.focusGeneration;
    const block = this.blocks.find((item) => item.block_id === blockId);
    const region = block?.source_regions?.[0];
    if (!region) {
      const page = block?.source_locator?.page_indices?.[0];
      if (Number.isInteger(page)) await this.goToPage(page);
      return;
    }
    const state = this.pageStates[region.page_index];
    if (!state) return;
    state.shell.scrollIntoView({ block: "start", behavior: "auto" });
    await this.#renderPage(state);
    if (generation !== this.focusGeneration) return;
    const target = state.regions.find((item) => item.dataset.blockId === blockId);
    target?.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
    this.#updatePageCounter(region.page_index, false);
  }

  setHover(blockId) {
    this.hoveredBlock = blockId;
    this.#updateRegionClasses();
  }

  setSelected(blockIds) {
    this.selectedBlocks = new Set(blockIds || []);
    this.pagesContainer.classList.toggle("has-selection", this.selectedBlocks.size > 0);
    this.#updateRegionClasses();
  }

 #createPageShells() {
    this.intersectionObserver = new IntersectionObserver(
      (entries) => this.#observePages(entries),
      { root: this.container, rootMargin: "1200px 0px" },
    );
   for (let index = 0; index < this.pdf.numPages; index += 1) {
      const shell = document.createElement("section");
      shell.className = "pdf-page";
      shell.dataset.pageIndex = String(index);
      shell.setAttribute("aria-label", `第 ${index + 1} 页`);
      const geometry = this.pageGeometries[index];
      const width = geometry ? geometry.crop_box[2] - geometry.crop_box[0] : 612;
      const height = geometry ? geometry.crop_box[3] - geometry.crop_box[1] : 792;
      const state = {
        index,
        shell,
        page: null,
        width,
        height,
        canvas: null,
        renderedScale: 0,
        renderedRotation: null,
        renderTask: null,
        renderPromise: null,
        renderGeneration: 0,
        regions: [],
      };
      this.pageStates.push(state);
      this.pagesContainer.append(shell);
      this.intersectionObserver.observe(shell);
    }
    this.#resizeShells();
  }

  #resizeShells() {
    for (const state of this.pageStates) {
      const sideways = this.#rotationForPage(state.index) % 180 !== 0;
      state.shell.style.width = `${(sideways ? state.height : state.width) * this.scale}px`;
      state.shell.style.height = `${(sideways ? state.width : state.height) * this.scale}px`;
    }
  }

  #rotationForPage(index) {
    const intrinsic = this.pageGeometries[index]?.intrinsic_rotation || 0;
    return (intrinsic + this.rotation) % 360;
  }

  #observePages(entries) {
    for (const entry of entries) {
      if (entry.isIntersecting) void this.#renderPage(this.pageStates[Number(entry.target.dataset.pageIndex)]);
    }
    this.#updatePageCounter();
  }

  async #renderNearby(index) {
    await Promise.all(
      this.pageStates.slice(Math.max(0, index - 1), index + 2).map((state) => this.#renderPage(state)),
    );
  }

  async #rerenderVisible() {
    const visible = this.pageStates.filter((state) => {
      const box = state.shell.getBoundingClientRect();
      const root = this.container.getBoundingClientRect();
      return box.bottom >= root.top - 1200 && box.top <= root.bottom + 1200;
    });
    await Promise.all(visible.map((state) => this.#renderPage(state)));
  }

  async #renderPage(state) {
    if (!state || !this.pdf) return;
    if (
      state.canvas &&
      state.renderedScale === this.scale &&
      state.renderedRotation === this.rotation
    ) {
      return;
    }
    state.renderGeneration += 1;
    state.renderTask?.cancel();
    if (state.renderPromise) return state.renderPromise;
    const promise = this.#renderPageLoop(state);
    state.renderPromise = promise;
    try {
      await promise;
    } finally {
      if (state.renderPromise === promise) state.renderPromise = null;
    }
  }

  async #renderPageLoop(state) {
    while (this.pdf && state.renderGeneration > 0) {
      const generation = state.renderGeneration;
      const pdf = this.pdf;
      const page = state.page || await pdf.getPage(state.index + 1);
      if (pdf !== this.pdf) return;
      state.page = page;
      const baseViewport = page.getViewport({ scale: 1, rotation: 0 });
      const viewport = page.getViewport({
        scale: this.scale,
        rotation: this.#rotationForPage(state.index),
      });
      state.width = baseViewport.width;
      state.height = baseViewport.height;
      if (generation !== state.renderGeneration) continue;
      state.shell.style.width = `${viewport.width}px`;
      state.shell.style.height = `${viewport.height}px`;
      state.shell.replaceChildren();

      const canvas = document.createElement("canvas");
      canvas.className = "pdf-page-canvas";
      const outputScale = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(viewport.width * outputScale));
      canvas.height = Math.max(1, Math.floor(viewport.height * outputScale));
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      state.shell.append(canvas);

      const textLayer = document.createElement("div");
      textLayer.className = "textLayer pdf-text-layer";
      state.shell.append(textLayer);
      const regionLayer = document.createElement("div");
      regionLayer.className = "pdf-region-layer";
      state.shell.append(regionLayer);
      state.canvas = canvas;
      state.regions = [];

      const renderContext = {
        canvasContext: canvas.getContext("2d", { alpha: false }),
        viewport,
        transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null,
      };
      const renderTask = page.render(renderContext);
      state.renderTask = renderTask;
      try {
        await renderTask.promise;
        const text = new TextLayer({
          textContentSource: page.streamTextContent({ includeMarkedContent: true }),
          container: textLayer,
          viewport,
        });
        await text.render();
      } catch (error) {
        if (pdf !== this.pdf) return;
        if (generation !== state.renderGeneration) continue;
        throw error;
      } finally {
        if (state.renderTask === renderTask) state.renderTask = null;
      }
      if (pdf !== this.pdf) return;
      if (generation !== state.renderGeneration) continue;
      state.renderedScale = this.scale;
      state.renderedRotation = this.rotation;
      this.#renderRegions(state, viewport, regionLayer);
      return;
    }
  }

  #renderRegions(state, viewport, layer) {
    for (const block of this.blocks) {
      for (const region of block.source_regions || []) {
        if (region.page_index !== state.index) continue;
        const rectangle = viewport.convertToViewportRectangle(region.bbox_pdf);
        const left = Math.min(rectangle[0], rectangle[2]);
        const top = Math.min(rectangle[1], rectangle[3]);
        const regionElement = document.createElement("button");
        regionElement.type = "button";
        regionElement.className = "pdf-region";
        regionElement.style.background = "transparent";
        regionElement.dataset.blockId = block.block_id;
        regionElement.dataset.blockType = block.block_type || "paragraph";
        regionElement.dataset.label = blockTypeLabel(block.block_type);
        regionElement.title = `${blockTypeLabel(block.block_type)} (${block.block_id})`;
        regionElement.style.left = `${left}px`;
        regionElement.style.top = `${top}px`;
        regionElement.style.width = `${Math.abs(rectangle[2] - rectangle[0])}px`;
        regionElement.style.height = `${Math.abs(rectangle[3] - rectangle[1])}px`;
        regionElement.addEventListener("click", () => this.onBlockClick(block.block_id));
        layer.append(regionElement);
        state.regions.push(regionElement);
      }
    }
    this.#updateRegionClasses();
  }

  #blockAtPoint(clientX, clientY) {
    for (const state of this.pageStates) {
      if (!state.regions.length) continue;
      const pageRect = state.shell.getBoundingClientRect();
      if (
        clientX < pageRect.left ||
        clientX > pageRect.right ||
        clientY < pageRect.top ||
        clientY > pageRect.bottom
      ) {
        continue;
      }
      for (const region of state.regions) {
        const rectangle = region.getBoundingClientRect();
        if (
          clientX >= rectangle.left &&
          clientX <= rectangle.right &&
          clientY >= rectangle.top &&
          clientY <= rectangle.bottom
        ) {
          return region.dataset.blockId;
        }
      }
    }
    return null;
  }

  #handlePointerMove(event) {
    if (this.pointerMoveRafId) return;
    const clientX = event.clientX;
    const clientY = event.clientY;
    this.pointerMoveRafId = requestAnimationFrame(() => {
      this.pointerMoveRafId = null;
      const blockId = this.#blockAtPoint(clientX, clientY);
      if (blockId === this.hoveredBlock) return;
      this.setHover(blockId);
      this.onBlockHover(blockId);
    });
  }

  #handleContainerClick(event) {
    const target = event.target;
    if (target instanceof Element && target.closest("#pdf-toolbar, button, select, input")) return;
    if (target instanceof Element && target.closest(".pdf-region")) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.toString()) return;
    if (this.pointerStart) {
      const distance = Math.hypot(
        event.clientX - this.pointerStart.x,
        event.clientY - this.pointerStart.y,
      );
      this.pointerStart = null;
      if (distance > 5) return;
    }
    const blockId = this.#blockAtPoint(event.clientX, event.clientY);
    if (blockId) {
      this.onBlockClick(blockId);
    } else {
      this.onBlankClick();
    }
  }

  #updateRegionClasses() {
    for (const state of this.pageStates) {
      for (const region of state.regions) {
        const active = region.dataset.blockId === this.hoveredBlock;
        const selected = this.selectedBlocks.has(region.dataset.blockId);
        region.classList.toggle("is-hovered", active);
        region.classList.toggle("is-selected", selected);
      }
    }
  }

  #updatePageCounter(forceIndex = null, isUserScroll = true) {
    if (!this.pageStates.length) return;
    let index = forceIndex;
    if (index === null) {
      const containerTop = this.container.scrollTop;
      const probeY = containerTop + this.container.clientHeight * 0.25;
      let minDistance = Number.POSITIVE_INFINITY;
      for (let i = 0; i < this.pageStates.length; i++) {
        const shell = this.pageStates[i].shell;
        const top = shell.offsetTop;
        const bottom = top + shell.offsetHeight;
        if (probeY >= top && probeY <= bottom) {
          index = i;
          break;
        }
        const dist = Math.abs(top - probeY);
        if (dist < minDistance) {
          minDistance = dist;
          index = i;
        }
      }
    }
    if (Number.isInteger(index) && index !== this.currentPage) {
      this.currentPage = index;
      this.onPageChange(index, this.pageStates.length, isUserScroll);
    }
  }

  #updateControls(enabled) {
    for (const selector of ["#prev-page", "#next-page", "#zoom-out", "#zoom-in", "#reset-zoom"]) {
      const control = document.querySelector(selector);
      if (control) control.disabled = !enabled;
    }
  }

  #disposeDocument() {
    for (const state of this.pageStates) state.renderTask?.cancel();
    this.intersectionObserver.disconnect();
    if (this.pdf) void this.pdf.destroy();
    if (this.loadingTask) void this.loadingTask.destroy();
    this.pdf = null;
    this.loadingTask = null;
  }
}
