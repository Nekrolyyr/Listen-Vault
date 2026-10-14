// PDF.js viewer that follows playback: it scrolls so the sentence being read sits in the upper part of the
// viewport and highlights it. Scrolls caused by the app are told apart from scrolls by the reader.
// Zoom: "fit page" and "fit width" stay in effect while the window is resized; fixed levels do not.
import * as pdfjs from "pdfjs-dist";
import { EventBus, PDFLinkService, PDFViewer } from "pdfjs-dist/web/pdf_viewer.mjs";
import "pdfjs-dist/web/pdf_viewer.css";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { h } from "./dom";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/** [page, x, y, width, height] in PDF points, origin top-left. */
export type Rect = [number, number, number, number, number];

/** The part of a PDF.js page view that we use. */
interface PageView {
  div: HTMLElement;
  scale: number;
}

export type ZoomMode = "page-fit" | "page-width" | number;

const PROGRAMMATIC_MS = 900; // smooth scrolls by the app last about this long
const CSS_PER_PT = 96 / 72; // PDF points to CSS pixels; a page view's scale is the zoom, before this factor
const VIEWPORT_ANCHOR = 0.3; // fraction of the viewport height above the sentence
const ZOOM_LEVELS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const ZOOM_KEY = "lv.zoom";
const PHONE = "(max-width: 900px)";

function loadZoom(): ZoomMode {
  try {
    const v = localStorage.getItem(ZOOM_KEY);
    if (v === "page-fit" || v === "page-width") return v;
    if (v && Number(v) > 0) return Number(v);
  } catch {
    /* storage unavailable: use the default */
  }
  return window.matchMedia(PHONE).matches ? "page-width" : "page-fit";
}

function saveZoom(zoom: ZoomMode): void {
  try {
    localStorage.setItem(ZOOM_KEY, String(zoom));
  } catch {
    /* ignore */
  }
}

export class PdfView {
  readonly el: HTMLElement;
  private container: HTMLDivElement;
  private viewer: PDFViewer;
  private bus = new EventBus();
  private ready = false;
  private pendingPage: number | null = null;
  private highlight: Rect[] = [];
  private programmaticUntil = 0;
  private loading: ReturnType<typeof pdfjs.getDocument> | null = null;
  private zoom: ZoomMode;
  private zoomSelect: HTMLSelectElement;
  private zoomLabel: HTMLElement;
  private resizePending = false;
  private resize = new ResizeObserver(() => {
    // fit modes follow the window; coalesce the many resize notifications of one drag into one redraw
    if (!this.ready || typeof this.zoom === "number" || this.resizePending) return;
    this.resizePending = true;
    requestAnimationFrame(() => {
      this.resizePending = false;
      this.applyZoom();
    });
  });

  constructor(url: string, private onUserScroll: () => void) {
    this.zoom = loadZoom();
    this.container = h("div", { class: "pdf-container", tabindex: 0, "aria-label": "PDF document" }, h("div", { class: "pdfViewer" }));

    this.zoomSelect = h(
      "select",
      { class: "zoom-select", "aria-label": "Zoom", onchange: (e: Event) => this.pickZoom((e.target as HTMLSelectElement).value) },
      h("option", { value: "page-fit" }, "Fit page"),
      h("option", { value: "page-width" }, "Fit width"),
      ...ZOOM_LEVELS.map((z) => h("option", { value: String(z) }, `${Math.round(z * 100)}%`)),
    ) as HTMLSelectElement;
    this.zoomLabel = h("span", { class: "zoom-label", "aria-live": "polite" });
    const toolbar = h(
      "div",
      { class: "pdf-toolbar", role: "toolbar", "aria-label": "PDF zoom" },
      h("button", { type: "button", "aria-label": "Zoom out", title: "Zoom out", onclick: () => this.step(-1) }, "−"),
      this.zoomSelect,
      h("button", { type: "button", "aria-label": "Zoom in", title: "Zoom in", onclick: () => this.step(1) }, "+"),
      this.zoomLabel,
    );
    this.el = h("div", { class: "pdf-pane" }, toolbar, this.container);

    const links = new PDFLinkService({ eventBus: this.bus });
    this.viewer = new PDFViewer({ container: this.container, eventBus: this.bus, linkService: links, removePageBorders: true });
    links.setViewer(this.viewer);
    this.bus.on("pagesinit", () => {
      this.ready = true;
      this.applyZoom();
      if (this.pendingPage) this.showPage(this.pendingPage);
    });
    // A page is re-rendered on zoom or when scrolled into view: paint the highlight onto it again.
    this.bus.on("pagerendered", (e: { pageNumber: number }) => this.paintPage(e.pageNumber));

    // Scrolls that are not ours pause "follow playback". Wheel, touch and keys are caught as well, so a
    // scroll is recognised even when the browser delivers it late.
    this.container.addEventListener("scroll", () => {
      if (performance.now() > this.programmaticUntil) this.onUserScroll();
    }, { passive: true });
    for (const ev of ["wheel", "touchmove", "keydown"]) {
      this.container.addEventListener(ev, () => this.onUserScroll(), { passive: true });
    }
    // Ctrl (or ⌘) + wheel zooms the document instead of the page.
    this.container.addEventListener("wheel", (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      this.step(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });

    this.loading = pdfjs.getDocument({ url, withCredentials: true });
    this.loading.promise.then(
      (doc) => {
        this.viewer.setDocument(doc);
        links.setDocument(doc);
      },
      (err) => this.el.replaceChildren(h("p", { class: "pane-message" }, `The PDF could not be shown: ${err.message ?? err}`)),
    );
    this.resize.observe(this.container);
    this.updateZoomControls();
  }

  /** Scroll to a page's top, for chapter starts and citations. */
  showPage(page: number): void {
    if (!this.ready) {
      this.pendingPage = page;
      return;
    }
    if (page >= 1 && page <= this.viewer.pagesCount && this.viewer.currentPageNumber !== page) {
      this.programmaticUntil = performance.now() + PROGRAMMATIC_MS;
      this.viewer.currentPageNumber = page;
    }
  }

  /** Highlight a sentence and bring its first line into view. Without rectangles, only the page is shown. */
  showSentence(page: number | null, rects: Rect[]): void {
    this.highlight = rects;
    if (!rects.length) {
      if (page) this.showPage(page);
      return;
    }
    if (!this.ready) return;
    this.paintAll();
    const [pageNo, , y] = rects[0];
    const pv = this.pageView(pageNo);
    if (!pv) {
      this.showPage(pageNo);
      return;
    }
    const pageTop = pv.div.getBoundingClientRect().top - this.container.getBoundingClientRect().top + this.container.scrollTop;
    const target = pageTop + y * pv.scale * CSS_PER_PT - this.container.clientHeight * VIEWPORT_ANCHOR;
    this.programmaticUntil = performance.now() + PROGRAMMATIC_MS;
    this.container.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
  }

  /** Set the zoom: "page-fit", "page-width" or a scale (1 = 100%). */
  setZoom(zoom: ZoomMode): void {
    this.zoom = zoom;
    saveZoom(zoom);
    this.applyZoom();
  }

  private pickZoom(value: string): void {
    this.setZoom(value === "page-fit" || value === "page-width" ? value : Number(value));
  }

  /** One zoom level in (dir > 0) or out (dir < 0). */
  private step(dir: number): void {
    const current = typeof this.zoom === "number" ? this.zoom : this.viewer.currentScale || 1;
    const next = dir > 0
      ? ZOOM_LEVELS.find((z) => z > current + 0.01)
      : [...ZOOM_LEVELS].reverse().find((z) => z < current - 0.01);
    if (next !== undefined) this.setZoom(next);
  }

  private applyZoom(): void {
    if (!this.ready) return;
    if (typeof this.zoom === "number") this.viewer.currentScale = this.zoom;
    else this.viewer.currentScaleValue = this.zoom;
    this.updateZoomControls();
  }

  private updateZoomControls(): void {
    const value = typeof this.zoom === "number" ? String(this.zoom) : this.zoom;
    if (![...this.zoomSelect.options].some((o) => o.value === value)) {
      this.zoomSelect.append(h("option", { value }, "Custom"));
    }
    this.zoomSelect.value = value;
    const percent = this.ready && this.viewer.currentScale ? Math.round(this.viewer.currentScale * 100) : null;
    this.zoomLabel.textContent = percent === null ? "" : `${percent}%`;
  }

  private pageView(pageNo: number): PageView | undefined {
    if (pageNo < 1 || pageNo > this.viewer.pagesCount) return undefined;
    return (this.viewer as unknown as { getPageView(i: number): PageView | undefined }).getPageView(pageNo - 1);
  }

  private paintAll(): void {
    for (const pageNo of new Set(this.highlight.map((r) => r[0]))) this.paintPage(pageNo);
  }

  private paintPage(pageNo: number): void {
    const pv = this.pageView(pageNo);
    if (!pv) return;
    pv.div.querySelectorAll(".sentence-hl").forEach((el) => el.remove());
    for (const [p, x, y, w, ht] of this.highlight) {
      if (p !== pageNo) continue;
      const s = pv.scale * CSS_PER_PT;
      pv.div.append(
        h("div", {
          class: "sentence-hl",
          style: `left:${x * s}px;top:${y * s}px;width:${w * s}px;height:${ht * s}px`,
        }),
      );
    }
  }

  destroy(): void {
    this.resize.disconnect();
    this.loading?.destroy();
  }
}
