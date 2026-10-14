import { api, type Settings } from "./api";
import { h, toast } from "./dom";
import type { View } from "./main";
import { WPM_MAX, WPM_MIN } from "./speedread";

const ACCENTS: Record<string, string> = {
  af: "American English · female",
  am: "American English · male",
  bf: "British English · female",
  bm: "British English · male",
};

function voiceName(id: string): string {
  const name = id.split("_")[1] ?? id;
  return name.charAt(0).toUpperCase() + name.slice(1);
}

export function settingsView(): View {
  const form = h("form", { class: "settings-form" }, h("p", { class: "pane-message" }, "Loading…"));

  async function backfill(): Promise<void> {
    try {
      const { queued } = await api.backfillKeywords();
      toast(queued ? `${queued} papers queued for keyword extraction` : "Every paper already has keywords");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function classify(force: boolean): Promise<void> {
    if (force && !confirm("Place every paper in its area again? Choices you made by hand are kept.")) return;
    try {
      const { queued } = await api.classify(force);
      toast(queued ? "Placing the papers in areas. The map updates as they are done." : "Already being placed.");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function rebuildTopics(): Promise<void> {
    try {
      const { queued } = await api.rebuildTopics();
      toast(queued ? "Topics are being rebuilt. Reload in a minute or two." : "Topics are already being rebuilt.");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function render(s: Settings): void {
    const v = s.values;
    const groups = Object.entries(ACCENTS).map(([prefix, label]) =>
      h(
        "optgroup",
        { label },
        ...s.voices.filter((id) => id.startsWith(prefix)).map((id) => h("option", { value: id, selected: id === v.voice }, voiceName(id))),
      ),
    );
    const speed = h("input", { type: "range", name: "speech_speed", min: 0.8, max: 1.3, step: 0.05, value: v.speech_speed }) as HTMLInputElement;
    const speedOut = h("output", {}, `${v.speech_speed.toFixed(2)}×`);
    speed.addEventListener("input", () => (speedOut.textContent = `${Number(speed.value).toFixed(2)}×`));
    const wpm = h("input", { type: "range", name: "reading_wpm", min: WPM_MIN, max: WPM_MAX, step: 10, value: v.reading_wpm }) as HTMLInputElement;
    const wpmOut = h("output", {}, `${v.reading_wpm} wpm`);
    wpm.addEventListener("input", () => (wpmOut.textContent = `${wpm.value} wpm`));

    form.replaceChildren(
      h(
        "fieldset",
        {},
        h("legend", {}, "Voice"),
        h(
          "div",
          { class: "radio-row" },
          h("label", {}, h("input", { type: "radio", name: "engine", value: "kokoro", checked: v.engine === "kokoro" }), h("span", {}, h("strong", {}, "Kokoro"), " natural voices, about 4× faster than real time on this server")),
          h("label", {}, h("input", { type: "radio", name: "engine", value: "piper", checked: v.engine === "piper" }), h("span", {}, h("strong", {}, "Piper"), " one voice, sounds more synthetic, about 35× faster than real time")),
        ),
        h("label", { class: "field" }, h("span", {}, "Kokoro voice"), h("select", { name: "voice" }, ...groups)),
        h("label", { class: "field" }, h("span", {}, "Reading speed"), h("span", { class: "range" }, speed, speedOut)),
        h("p", { class: "hint" }, "The player has its own speed control too. This setting changes the generated audio itself."),
      ),
      h(
        "fieldset",
        {},
        h("legend", {}, "Reading"),
        h(
          "label",
          { class: "field" },
          h("span", {}, "Citations like “(Smith et al., 2020)” and “[12]”"),
          h(
            "select",
            { name: "citations" },
            h("option", { value: "remove", selected: v.citations === "remove" }, "Leave them out"),
            h("option", { value: "speak", selected: v.citations === "speak" }, "Read them aloud"),
          ),
        ),
        h("label", { class: "field" }, h("span", {}, "Speed reading, words per minute"), h("span", { class: "range" }, wpm, wpmOut)),
        h("p", { class: "hint" }, "Speed reading shows one word at a time. It starts slower and reaches this speed after about 15 seconds."),
      ),
      h(
        "fieldset",
        {},
        h("legend", {}, "Library"),
        h("p", { class: "hint" }, "Papers added before keywords existed have none. This finds their task, methods, datasets, topics and keywords in the background."),
        h("button", { type: "button", class: "btn", onclick: backfill }, "Extract keywords for the library"),
        h("p", { class: "hint" }, "Areas place each paper in a main area and up to two others, from its abstract and content. New papers are placed automatically. Choices made by hand are kept."),
        h("div", { class: "form-actions wrap" },
          h("button", { type: "button", class: "btn", onclick: () => classify(false) }, "Place papers in areas"),
          h("button", { type: "button", class: "btn", onclick: () => classify(true) }, "Place all papers again"),
          h("button", { type: "button", class: "btn", onclick: rebuildTopics }, "Rebuild topics"),
        ),
      ),
      h("p", { class: "hint" }, "Changes apply to newly added documents. For existing ones, choose “Regenerate audio” in the library."),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn primary" }, "Save settings")),
    );
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    try {
      const saved = await api.saveSettings({
        engine: data.get("engine") as Settings["values"]["engine"],
        voice: String(data.get("voice")),
        speech_speed: Number(data.get("speech_speed")),
        citations: data.get("citations") as Settings["values"]["citations"],
        reading_wpm: Number(data.get("reading_wpm")),
      });
      render(saved);
      toast("Settings saved");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  });

  const zoteroBox = h("fieldset", { class: "zotero" }, h("legend", {}, "Zotero"), h("p", { class: "pane-message" }, "Loading…"));

  async function loadZotero(): Promise<void> {
    try {
      const z = await api.zotero();
      const sync = async () => {
        try {
          await api.zoteroSync();
          toast("Sync started");
        } catch (err) {
          toast((err as Error).message, "error");
        }
        loadZotero();
      };
      zoteroBox.replaceChildren(
        h("legend", {}, "Zotero"),
        h(
          "p",
          { class: "hint" },
          z.configured
            ? `Every item of the library is imported and listed. Nothing is processed automatically. ${z.catalogued} listed, not processed.`
            : "Not connected. Set ZOTERO_API_KEY in .env (create it at zotero.org/settings/keys), then restart the api and worker.",
        ),
        ...(z.last_synced_at ? [h("p", { class: "hint" }, `Last sync ${new Date(z.last_synced_at).toLocaleString()}`)] : []),
        ...(z.last_error ? [h("p", { class: "hint" }, `Last sync failed: ${z.last_error}`)] : []),
        h("button", { type: "button", class: "btn", disabled: !z.configured || z.syncing, onclick: sync },
          z.syncing ? "Syncing…" : "Sync now"),
        h("button", {
          type: "button",
          class: "btn",
          disabled: !z.configured || z.catalogued === 0,
          onclick: async () => {
            if (!confirm(`Process ${z.catalogued} listed papers now? This downloads PDFs and runs the analysis on each.`)) return;
            try {
              const { queued } = await api.zoteroQueueAll();
              toast(`${queued} papers queued for processing`);
            } catch (err) {
              toast((err as Error).message, "error");
            }
            loadZotero();
          },
        }, "Process all listed papers"),
      );
    } catch (e) {
      zoteroBox.replaceChildren(h("legend", {}, "Zotero"), h("p", { class: "hint" }, (e as Error).message));
    }
  }

  api.settings().then(render, (e) => toast((e as Error).message, "error"));
  loadZotero();
  return {
    el: h(
      "section",
      { class: "settings" },
      h("h1", {}, "Settings"),
      form,
      zoteroBox,
    ),
    title: "Settings",
  };
}
