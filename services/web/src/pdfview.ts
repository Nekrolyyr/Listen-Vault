// PDF.js viewer that can follow playback: scroll to a page unless the user is browsing on their own.
import * as pdfjs from "pdfjs-dist";
import { EventBus, PDFLinkService, PDFViewer } from "pdfjs-dist/web/pdf_viewer.mjs";
import "pdfjs-dist/web/pdf_viewer.css";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { h } from "./dom";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

export class PdfView {
  readonly el: HTMLElement;
  private container: HTMLDivElement;
  private viewer: PDFViewer;
  private bus = new EventBus();
  private ready = false;
  private pendingPage: number | null = null;
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
    // Any wheel/touch/key scroll by the user pauses "follow playback".
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

  showPage(page: number): void {
    if (!this.ready) {
      this.pendingPage = page;
      return;
    }
    if (page >= 1 && page <= this.viewer.pagesCount && this.viewer.currentPageNumber !== page) {
      this.viewer.currentPageNumber = page;
    }
  }

  destroy(): void {
    this.resize.disconnect();
    this.loading?.destroy();
  }
}
