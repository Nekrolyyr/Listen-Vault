// The first-time walk-through: welcome, the terms of use (must be accepted), a picture tutorial, and done.
// The server keeps whether it is finished; the step is kept in this browser so a refresh resumes where it was.
import { api } from "./api";
import { termsBlocks } from "./accounts";
import { h, toast } from "./dom";
import type { View } from "./main";
import { go, session, setSession } from "./session";

const STEP_KEY = "listen-onboarding-step";
const TUTORIAL = [
  {
    image: "step-1-add.svg",
    title: "Add a paper",
    text: "Drop a PDF on the library page, or paste the link of an article. Each paper gets its own card.",
  },
  {
    image: "step-2-wait.svg",
    title: "Wait a moment",
    text: "The text is prepared first, then your audio. A paper is ready when its progress bar is full, and the first track is the overview.",
  },
  {
    image: "step-3-listen.svg",
    title: "Listen while you read",
    text: "Press play. The PDF follows the voice and highlights the sentence being read. Scroll freely and press Follow to come back.",
  },
  {
    image: "step-4-find.svg",
    title: "Find your way",
    text: "The library map groups papers by area and topic. On a paper, “Ask about this paper” answers from its text, with the page it came from.",
  },
  {
    image: "step-5-settings.svg",
    title: "Make it yours",
    text: "Your voice and speed are in Settings, and the audio of each paper is made with them. The storage bar on your account page shows how much room is left.",
  },
];
const STEPS = ["welcome", "terms", ...TUTORIAL.map((_, i) => `tutorial-${i}`), "done"];

function savedStep(): number {
  try {
    const n = Number(localStorage.getItem(STEP_KEY));
    return Number.isInteger(n) && n >= 0 && n < STEPS.length ? n : 0;
  } catch {
    return 0; // storage can be blocked: start again from the beginning
  }
}

function keepStep(n: number): void {
  try {
    localStorage.setItem(STEP_KEY, String(n));
  } catch {
    /* not kept */
  }
}

function forgetStep(): void {
  try {
    localStorage.removeItem(STEP_KEY);
  } catch {
    /* nothing to forget */
  }
}

export function onboardingView(): View {
  let step = savedStep();
  const root = h("section", { class: "onboarding" });
  const acceptedHere = { read: false, ticked: false };

  function dots(): HTMLElement {
    return h("ol", { class: "onboarding-dots", "aria-label": `Step ${step + 1} of ${STEPS.length}` },
      ...STEPS.map((_, i) => h("li", { class: i === step ? "current" : i < step ? "done" : "", "aria-current": i === step ? "step" : null })));
  }

  function go_(to: number): void {
    step = Math.max(0, Math.min(STEPS.length - 1, to));
    keepStep(step);
    render();
  }

  async function finish(): Promise<void> {
    try {
      await api.finishOnboarding();
      if (session) session.onboarded = true;
      forgetStep();
      go("/", true);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function footer(opts: { back?: boolean; next?: { label: string; disabled?: boolean; run: () => void | Promise<void> }; skip?: boolean }): HTMLElement {
    return h("div", { class: "onboarding-actions" },
      opts.back ? h("button", { type: "button", class: "btn", onclick: () => go_(step - 1) }, "Back") : null,
      opts.skip ? h("button", { type: "button", class: "btn quiet", onclick: () => void finish() }, "Skip the tutorial") : null,
      opts.next ? h("button", {
        type: "button", class: "btn primary", disabled: opts.next.disabled ?? false,
        onclick: () => void opts.next!.run(),
      }, opts.next.label) : null,
    );
  }

  function welcome(): HTMLElement {
    return h("div", { class: "onboarding-step" },
      h("h1", {}, "Welcome to Listen Vault"),
      h("p", {}, "Listen Vault turns scientific papers and web articles into audio. It reads each paper aloud, chapter by chapter, and shows the PDF next to the voice."),
      h("p", {}, "Before you start: a short tour, and the terms of use. You need to accept the terms to add papers."),
      footer({ next: { label: "Continue", run: () => go_(step + 1) } }),
    );
  }

  function terms(): HTMLElement {
    const box = h("div", { class: "terms-text onboarding-terms", tabindex: 0 }, h("p", { class: "pane-message" }, "Loading the terms…"));
    const tick = h("input", { type: "checkbox", name: "accept" }) as HTMLInputElement;
    const next = h("button", { type: "button", class: "btn primary", disabled: true }, "I accept and continue") as HTMLButtonElement;
    const hint = h("p", { class: "hint" }, "Read the whole text to enable the checkbox.");
    const refresh = () => {
      const ready = acceptedHere.read || session?.terms.accepted === true;
      tick.disabled = !ready;
      hint.hidden = ready;
      next.disabled = !(ready && tick.checked);
    };
    box.addEventListener("scroll", () => {
      if (box.scrollTop + box.clientHeight >= box.scrollHeight - 8) acceptedHere.read = true;
      refresh();
    });
    tick.addEventListener("change", () => {
      acceptedHere.ticked = tick.checked;
      refresh();
    });
    next.addEventListener("click", async () => {
      try {
        const version = session?.terms.current;
        if (version && session && !session.terms.accepted) {
          await api.acceptTerms(version);
          session.terms.accepted = true;
        }
        go_(step + 1);
      } catch (err) {
        toast((err as Error).message, "error");
      }
    });
    api.terms().then(
      (t) => {
        box.replaceChildren(...(t.text ? termsBlocks(t.text) : [h("p", {}, "No terms are published yet.")]));
        if (box.scrollHeight <= box.clientHeight + 8) acceptedHere.read = true; // short text: nothing to scroll
        refresh();
      },
      (err) => box.replaceChildren(h("p", { class: "form-error" }, (err as Error).message)),
    );
    refresh();
    return h("div", { class: "onboarding-step" },
      h("h1", {}, "Terms of use"),
      h("p", { class: "onboarding-warning" }, h("strong", {}, "The operator can read the PDF files you upload."), " Please read the terms to the end before you accept them."),
      box,
      h("label", { class: "check" }, tick, h("span", {}, "I have read and accept the terms of service and the privacy statement.")),
      hint,
      h("div", { class: "onboarding-actions" },
        h("button", {
          type: "button", class: "btn quiet",
          onclick: async () => {
            await api.logout().catch(() => undefined);
            setSession(null);
            forgetStep();
            go("/login", true);
          },
        }, "I do not accept"),
        h("button", { type: "button", class: "btn", onclick: () => go_(step - 1) }, "Back"),
        next,
      ),
    );
  }

  function tutorial(i: number): HTMLElement {
    const item = TUTORIAL[i];
    return h("div", { class: "onboarding-step tutorial" },
      h("p", { class: "hint" }, `Tutorial ${i + 1} of ${TUTORIAL.length}`),
      h("img", { src: `/onboarding/${item.image}`, alt: item.title, width: 480, height: 270, class: "tutorial-image" }),
      h("h2", {}, item.title),
      h("p", {}, item.text),
      footer({
        back: true,
        skip: true,
        next: { label: "Next", run: () => go_(step + 1) },
      }),
    );
  }

  function done(): HTMLElement {
    return h("div", { class: "onboarding-step" },
      h("h1", {}, "You are ready"),
      h("p", {}, "Add your first paper from the library. You can show this tutorial again from your account page."),
      footer({ back: true, next: { label: "Open the library", run: () => finish() } }),
    );
  }

  function render(): void {
    const name = STEPS[step];
    const content = name === "welcome" ? welcome() : name === "terms" ? terms()
      : name === "done" ? done() : tutorial(step - 2);
    root.replaceChildren(dots(), content);
  }

  render();
  return { title: "Welcome", el: root };
}
