// Actions on papers, shared by every library page: the paper menu, adding papers, and the areas and topics dialog.
import { api, type Doc, type DocDetail } from "./api";
import { h, icon, toast } from "./dom";
import { areaList } from "./areas";

/** Re-running processing makes no sense while it runs or before it was queued. */
export function notProcessed(doc: Doc): boolean {
  return doc.status === "queued" || doc.status === "processing" || doc.status === "catalogued";
}

export async function act(doc: Doc, action: string, done: () => void): Promise<void> {
  try {
    if (action === "delete") {
      if (!confirm(`Delete “${doc.title}” with its PDF and audio?`)) return;
      await api.remove(doc.id);
      toast("Deleted");
    } else if (action === "listened") {
      await api.patch(doc.id, { listened: !doc.listened });
    } else if (action === "rerun-audio") {
      await api.rerun(doc.id, "synthesis");
      toast("Audio will be generated again with the current voice settings.");
    } else if (action === "rerun-start") {
      await api.rerun(doc.id, "start");
      toast("Processing starts over.");
    } else if (action === "queue") {
      await api.queue(doc.id);
      toast("Queued for processing.");
    }
  } catch (e) {
    toast((e as Error).message, "error");
  }
  done();
}

/** Add papers: drop or choose PDFs, or paste a link. Progress of the uploads is shown under the form. */
export function addPanel(added: () => void): HTMLElement {
  const uploads = new Map<string, number>();
  const uploadList = h("ul", { class: "upload-list" });
  const renderUploads = () => {
    uploadList.replaceChildren(
      ...[...uploads].map(([name, p]) =>
        h("li", {}, h("span", {}, `Uploading ${name}`), h("progress", { max: 1, value: p, "aria-label": `Uploading ${name}` })),
      ),
    );
  };
  async function addFiles(files: Iterable<File>): Promise<void> {
    for (const file of files) {
      if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
        toast(`${file.name} is not a PDF`, "error");
        continue;
      }
      uploads.set(file.name, 0);
      renderUploads();
      try {
        await api.upload(file, (p) => {
          uploads.set(file.name, p);
          renderUploads();
        });
        toast(`Added ${file.name}. Processing has started.`);
      } catch (e) {
        toast(`${file.name}: ${(e as Error).message}`, "error");
      }
      uploads.delete(file.name);
      renderUploads();
      added();
    }
  }
  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", multiple: true, hidden: true }) as HTMLInputElement;
  fileInput.addEventListener("change", () => {
    if (fileInput.files) void addFiles([...fileInput.files]);
    fileInput.value = "";
  });
  const drop = h(
    "label",
    {
      class: "dropzone",
      ondragover: (e: Event) => {
        e.preventDefault();
        drop.classList.add("over");
      },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e: Event) => {
        e.preventDefault();
        drop.classList.remove("over");
        const files = (e as DragEvent).dataTransfer?.files;
        if (files) void addFiles(files);
      },
    },
    icon("upload"),
    h("span", {}, h("strong", {}, "Drop PDFs here"), " or ", h("u", {}, "choose files")),
    fileInput,
  );
  const urlInput = h("input", {
    type: "url", name: "url", placeholder: "https://arxiv.org/abs/… or any article URL", required: true,
    "aria-label": "Article or paper URL", autocomplete: "off",
  }) as HTMLInputElement;
  const urlForm = h(
    "form",
    {
      class: "url-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        const url = urlInput.value.trim();
        if (!url) return;
        try {
          await api.addUrl(url);
          urlInput.value = "";
          toast("Link added. Processing has started.");
          added();
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    },
    icon("link"),
    urlInput,
    h("button", { type: "submit", class: "btn primary" }, "Add link"),
  );
  return h("div", { class: "add-panel" }, drop, urlForm, uploadList);
}

/** The areas and topics of one paper, edited in a dialog. Saving pins the choices so a new classification keeps them. */
export async function openClassifier(docId: string, saved: () => void): Promise<void> {
  let doc: DocDetail;
  let areas: { id: string; name: string }[];
  let topics: { id: number; name: string; terms: string[] }[];
  try {
    [doc, areas, topics] = await Promise.all([api.document(docId), areaList(), api.topics()]);
  } catch (e) {
    toast((e as Error).message, "error");
    return;
  }
  const current = doc.areas;
  const onTopics = new Set((doc.topics ?? []).map((t) => t.id));
  const initialTopic = new Map(topics.map((t) => [t.id, onTopics.has(t.id)]));

  const primary = areas.map((a) =>
    h("label", { class: "pick" }, h("input", { type: "radio", name: "primary", value: a.id, checked: a.id === (current?.primary ?? "other") }), h("span", {}, a.name)),
  );
  const alsoInputs = areas.filter((a) => a.id !== "other").map((a) => {
    const box = h("input", { type: "checkbox", value: a.id, checked: current?.also.includes(a.id) ?? false }) as HTMLInputElement;
    box.addEventListener("change", () => {
      const picked = alsoInputs.filter((x) => x.box.checked).length;
      alsoInputs.forEach((x) => (x.box.disabled = picked >= 2 && !x.box.checked));
    });
    return { box, label: h("label", { class: "pick" }, box, h("span", {}, a.name)) };
  });
  alsoInputs.forEach((x) => (x.box.disabled = alsoInputs.filter((y) => y.box.checked).length >= 2 && !x.box.checked));
  const topicInputs = topics.map((t) => {
    const box = h("input", { type: "checkbox", checked: onTopics.has(t.id) }) as HTMLInputElement;
    return { id: t.id, name: t.name, box, label: h("label", { class: "pick topic-pick" }, box, h("span", {}, t.name), h("span", { class: "quiet" }, t.terms.slice(0, 2).join(", "))) };
  });

  const dialog = h("dialog", { class: "classifier", "aria-labelledby": "classifier-title" });
  const save = async () => {
    const chosen = primary.map((l) => l.querySelector("input") as HTMLInputElement).find((i) => i.checked)?.value ?? "other";
    const also = alsoInputs.filter((x) => x.box.checked).map((x) => x.box.value);
    const overrides: Record<string, boolean> = {};
    for (const t of topicInputs) {
      if (t.box.checked !== initialTopic.get(t.id)) overrides[t.name] = t.box.checked;
    }
    try {
      await api.classification(doc.id, { primary: chosen, also, topics: overrides });
      dialog.close();
      toast("Saved. These choices are kept when papers are classified again.");
      saved();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  dialog.append(
    h("form", { method: "dialog", class: "classifier-form" },
      h("h2", { id: "classifier-title" }, "Areas and topics"),
      h("p", { class: "quiet classifier-paper" }, doc.title),
      h("fieldset", {}, h("legend", {}, "Main area"), h("div", { class: "pick-grid" }, ...primary)),
      h("fieldset", {}, h("legend", {}, "Also in (up to two)"), h("div", { class: "pick-grid" }, ...alsoInputs.map((x) => x.label))),
      h("fieldset", {}, h("legend", {}, "Topics"), topicInputs.length
        ? h("div", { class: "pick-list" }, ...topicInputs.map((t) => t.label))
        : h("p", { class: "quiet" }, "No topics yet. Rebuild them in Settings.")),
      h("div", { class: "dialog-actions" },
        h("button", { type: "button", class: "btn", onclick: () => dialog.close() }, "Cancel"),
        h("button", { type: "button", class: "btn primary", onclick: save }, "Save")),
    ),
  );
  document.body.append(dialog);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}
