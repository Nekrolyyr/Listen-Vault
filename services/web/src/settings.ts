import { api, type Settings } from "./api";
import { h, toast } from "./dom";
import type { View } from "./main";

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
      ),
      h(
        "fieldset",
        {},
        h("legend", {}, "Library"),
        h("p", { class: "hint" }, "Papers added before keywords existed have none. This finds their task, methods, datasets, topics and keywords in the background."),
        h("button", { type: "button", class: "btn", onclick: backfill }, "Extract keywords for the library"),
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
      });
      render(saved);
      toast("Settings saved");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  });

  api.settings().then(render, (e) => toast((e as Error).message, "error"));
  return { el: h("section", { class: "settings" }, h("h1", {}, "Settings"), form), title: "Settings" };
}
