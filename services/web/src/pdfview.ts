// PDF.js viewer that follows playback: it scrolls so the sentence being read sits in the upper part of the
// viewport and highlights it. Scrolls caused by the app are told apart from scrolls by the reader.
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

const PROGRAMMATIC_MS = 900; // smooth scrolls by the app last about this long
const VIEWPORT_ANCHOR = 0.3; // fraction of the viewport height above the sentence

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
  private resize = new ResizeObserver(() => {
    if (this.ready) this.viewer.currentScaleValue = "page-width";
  });

  constructor(url: string, private onUserScroll: () => void) {
    this.container = h("div", { class: "pdf-container", tabindex: 0, "aria-label": "PDF document" }, h("div", { class: "pdfViewer" }));
    this.el = h("div", { class: "pdf-pane" }, this.container);
    const links = new PDFLinkService({ eventBus: this.bus });
    this.viewer = new PDFViewer({ container: this.container, eventBus: this.bus, linkService: links, removePageBorders: true });
    links.setViewer(this.viewer);
    this.bus.on("pagesinit", () => {
      this.ready = true;
      this.viewer.currentScaleValue = "page-width";
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

    this.loading = pdfjs.getDocument({ url, withCredentials: true });
    this.loading.promise.then(
      (doc) => {
        this.viewer.setDocument(doc);
        links.setDocument(doc);
      },
      (err) => this.el.replaceChildren(h("p", { class: "pane-message" }, `The PDF could not be shown: ${err.message ?? err}`)),
    );
    this.resize.observe(this.el);
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
    const target = pageTop + y * pv.scale - this.container.clientHeight * VIEWPORT_ANCHOR;
    this.programmaticUntil = performance.now() + PROGRAMMATIC_MS;
    this.container.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
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
      const s = pv.scale;
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
