// Sign-in, registration, email confirmation, the terms, the account page and the administration console.
import { api, type AdminUser, type AuditRow, type MaintenanceStatus, type MaintenanceTask, type Me } from "./api";
import { bar, crumbs } from "./components";
import { h, toast } from "./dom";
import type { View } from "./main";
import { go, session, setSession } from "./session";

/** Terms and privacy text: "#" headings, "- " items, paragraphs. Only text is ever inserted. */
function termsBlocks(text: string): HTMLElement[] {
  const blocks: HTMLElement[] = [];
  let list: HTMLElement | null = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("- ")) {
      if (!list) {
        list = h("ul", {});
        blocks.push(list);
      }
      list.append(h("li", {}, line.slice(2).replace(/\*\*/g, "")));
      continue;
    }
    list = null;
    if (!line) continue;
    if (line.startsWith("# ")) blocks.push(h("h2", {}, line.slice(2)));
    else if (line.startsWith("## ")) blocks.push(h("h3", {}, line.slice(3)));
    else if (line.startsWith(">")) blocks.push(h("p", { class: "hint" }, line.replace(/^>\s*/, "").replace(/\*\*/g, "")));
    else blocks.push(h("p", {}, line.replace(/\*\*/g, "")));
  }
  return blocks;
}

function errorLine(): HTMLElement {
  return h("p", { class: "form-error", role: "alert" });
}

function fail(el: HTMLElement, err: unknown): void {
  el.textContent = (err as Error).message;
}

export function loginView(): View {
  const error = errorLine();
  const email = h("input", { type: "email", name: "email", required: true, autocomplete: "username" }) as HTMLInputElement;
  const password = h("input", { type: "password", name: "password", required: true, autocomplete: "current-password" }) as HTMLInputElement;
  const form = h(
    "form",
    {
      class: "auth-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        error.textContent = "";
        try {
          setSession(await api.login({ email: email.value, password: password.value }));
          go("/", true);
        } catch (err) {
          fail(error, err);
        }
      },
    },
    h("label", { class: "field" }, h("span", {}, "Email"), email),
    h("label", { class: "field" }, h("span", {}, "Password"), password),
    error,
    h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn primary" }, "Sign in")),
  );
  return {
    title: "Sign in",
    el: h("section", { class: "auth-card" },
      h("h1", {}, "Sign in to Listen Vault"),
      form,
      h("p", { class: "hint" }, "No account yet? ", h("a", { href: "/register", "data-link": true }, "Create one"), "."),
    ),
  };
}

export function registerView(): View {
  const error = errorLine();
  const email = h("input", { type: "email", name: "email", required: true, autocomplete: "email" }) as HTMLInputElement;
  const password = h("input", {
    type: "password", name: "password", required: true, minlength: 12, autocomplete: "new-password",
  }) as HTMLInputElement;
  const accept = h("input", { type: "checkbox", name: "accept" }) as HTMLInputElement;
  const terms = h("div", { class: "terms-text" }, h("p", { class: "pane-message" }, "Loading the terms…"));
  const sent = h("section", { class: "auth-card", hidden: true });
  const form = h(
    "form",
    {
      class: "auth-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        error.textContent = "";
        try {
          const res = await api.register({ email: email.value, password: password.value, accept_terms: accept.checked });
          if (res.status === "verify_email") {
            form.hidden = true;
            sent.hidden = false;
            sent.replaceChildren(h("h1", {}, "Check your email"),
              h("p", {}, `We sent a link to ${res.email}. Open it to confirm the address; then sign in.`),
              h("p", { class: "hint" }, "The link works for 48 hours."));
            return;
          }
          setSession(res);
          go("/", true);
        } catch (err) {
          fail(error, err);
        }
      },
    },
    h("label", { class: "field" }, h("span", {}, "Email"), email),
    h("label", { class: "field" }, h("span", {}, "Password (at least 12 characters)"), password),
    h("label", { class: "check" }, accept, h("span", {}, "I have read and accept the terms of service and the privacy statement below.")),
    h("p", { class: "hint" }, "The operator of this service can read the PDFs you upload. Read section 2 before you accept."),
    error,
    h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn primary" }, "Create account")),
  );
  api.terms().then(
    (t) => terms.replaceChildren(...(t.text ? termsBlocks(t.text) : [h("p", {}, "No terms are published yet.")])),
    (err) => fail(terms, err),
  );
  return {
    title: "Create account",
    el: h("div", { class: "auth-page-stack" },
      h("section", { class: "auth-card" }, h("h1", {}, "Create an account"), form, sent,
        h("p", { class: "hint" }, "Already registered? ", h("a", { href: "/login", "data-link": true }, "Sign in"), ".")),
      h("section", { class: "auth-card terms" }, h("h2", {}, "Terms of service and privacy"), terms),
    ),
  };
}

export function verifyView(): View {
  const body = h("p", {}, "Confirming your address…");
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  api.verify(token).then(
    () => body.replaceWith(h("p", {}, "Your address is confirmed. ", h("a", { href: "/login", "data-link": true }, "Sign in"), ".")),
    (err) => fail(body, err),
  );
  return { title: "Confirm your address", el: h("section", { class: "auth-card" }, h("h1", {}, "Confirm your address"), body) };
}

export function termsView(): View {
  const version = session?.terms.current ?? null;
  const box = h("div", { class: "terms-text" }, h("p", { class: "pane-message" }, "Loading…"));
  const extra: HTMLElement[] = [];
  if (session && version && !session.terms.accepted) {
    extra.push(h("button", {
      type: "button", class: "btn primary",
      onclick: async () => {
        try {
          await api.acceptTerms(version);
          if (session) session.terms.accepted = true;
          toast("Thank you. You can add papers now.");
          go("/", true);
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    }, "I accept these terms"));
  }
  api.terms().then(
    (t) => box.replaceChildren(...(t.text ? termsBlocks(t.text) : [h("p", {}, "No terms are published yet.")])),
    (err) => fail(box, err),
  );
  return {
    title: "Terms of service",
    el: h("section", { class: session ? "" : "auth-card" },
      session ? crumbs(["Library", "/"], ["Terms"]) : null,
      h("h1", {}, "Terms of service and privacy"),
      box,
      h("div", { class: "form-actions" }, ...extra),
    ),
  };
}

export function accountView(me: Me): View {
  const quota = me.quota;
  const password = (name: string, label: string, auto: string) =>
    h("label", { class: "field" }, h("span", {}, label),
      h("input", { type: "password", name, required: true, autocomplete: auto, minlength: 12 }));
  const changePassword = h(
    "form",
    {
      class: "settings-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        const data = new FormData(e.target as HTMLFormElement);
        try {
          await api.changePassword(String(data.get("current")), String(data.get("new")));
          (e.target as HTMLFormElement).reset();
          toast("Password changed. Your other sessions have been signed out.");
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    },
    h("fieldset", {},
      h("legend", {}, "Password"),
      password("current", "Current password", "current-password"),
      password("new", "New password (at least 12 characters)", "new-password"),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn" }, "Change password")),
    ),
  );
  const del = h(
    "form",
    {
      class: "settings-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        if (!confirm("Delete your account? Your library, listening progress and questions are removed. Papers nobody else holds are deleted too.")) return;
        try {
          await api.deleteAccount(String((new FormData(e.target as HTMLFormElement)).get("password")));
          setSession(null);
          toast("Your account is deleted.");
          go("/login", true);
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    },
    h("fieldset", {},
      h("legend", {}, "Delete account"),
      h("p", { class: "hint" }, "Confirm with your password. This cannot be undone."),
      password("password", "Password", "current-password"),
      h("div", { class: "form-actions" }, h("button", { type: "submit", class: "btn danger" }, "Delete my account")),
    ),
  );
  return {
    title: "Account",
    el: h("section", { class: "account-page" },
      crumbs(["Library", "/"], ["Account"]),
      h("h1", {}, "Account"),
      h("p", {}, me.email),
      h("div", { class: "quota" }, bar(quota.quota ? quota.used / quota.quota : 0, `${quota.used} of ${quota.quota} pages used`)),
      h("p", { class: "hint" }, "Each page of a paper in your library counts. Papers that other people also have count for you too; they are stored once."),
      h("div", { class: "form-actions wrap" },
        h("a", { class: "btn", href: api.exportUrl, download: "listen-vault-export.zip" }, "Download my data"),
        h("a", { class: "btn", href: "/terms", "data-link": true }, "Terms and privacy"),
        h("button", {
          type: "button", class: "btn",
          onclick: async () => {
            await api.logout().catch(() => undefined);
            setSession(null);
            go("/login", true);
          },
        }, "Sign out"),
      ),
      changePassword,
      del,
    ),
  };
}

export function adminView(): View {
  const root = h("section", { class: "admin-page" }, h("p", { class: "pane-message" }, "Loading…"));

  async function load(): Promise<void> {
    try {
      const [users, reg, stats, audit, maintenance] = await Promise.all([
        api.adminUsers(), api.adminRegistration(), api.adminStats(), api.adminAudit(), api.maintenance(),
      ]);
      root.replaceChildren(
        crumbs(["Library", "/"], ["Administration"]),
        h("h1", {}, "Administration"),
        statsLine(stats),
        registrationBox(reg.open, reg.email_verification),
        maintenanceBox(maintenance),
        h("h2", {}, "Accounts"),
        h("p", { class: "hint" }, "Counts only: this page does not show the titles of anyone's papers."),
        usersTable(users),
        h("h2", {}, "Recent changes"),
        auditTable(audit),
      );
    } catch (err) {
      root.replaceChildren(h("p", { class: "form-error" }, (err as Error).message));
    }
  }

  function statsLine(s: Record<string, number>): HTMLElement {
    return h("p", { class: "hint" },
      `${s.users} accounts, ${s.documents} stored papers, ${s.signed_in} signed in now, ${s.open_jobs} jobs waiting.`);
  }

  function registrationBox(open: boolean, verification: boolean): HTMLElement {
    const box = h("input", { type: "checkbox", checked: open }) as HTMLInputElement;
    return h("fieldset", { class: "settings-form" },
      h("legend", {}, "Sign-up"),
      h("label", { class: "check" }, box, h("span", {}, "Anyone can create an account")),
      h("p", { class: "hint" }, verification
        ? "New accounts confirm their address by email."
        : "Email confirmation is off (SMTP is not set): new accounts are active at once."),
      h("button", {
        type: "button", class: "btn",
        onclick: async () => {
          try {
            await api.adminSetRegistration(box.checked);
            toast(box.checked ? "Sign-up is open" : "Sign-up is closed");
            void load();
          } catch (err) {
            toast((err as Error).message, "error");
          }
        },
      }, "Save"),
    );
  }

  function maintenanceBox(m: MaintenanceStatus): HTMLElement {
    const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
    const time = h("input", { type: "time", value: m.rebuild_at, "aria-label": "Daily topic rebuild time" }) as HTMLInputElement;
    const run = async (task: MaintenanceTask) => {
      try {
        const res = await api.runMaintenance(task);
        const queued = res.queued === true || (typeof res.queued === "number" && res.queued > 0);
        toast(queued ? "Started." : "Already waiting or nothing to do.");
        void load();
      } catch (err) {
        toast((err as Error).message, "error");
      }
    };
    return h("fieldset", { class: "settings-form" },
      h("legend", {}, "Maintenance"),
      h("p", { class: "hint" },
        `Topics are rebuilt every day at ${m.rebuild_at} (${m.timezone}) when a paper has finished since the last rebuild. `,
        `Last rebuild: ${when(m.last_run)}. Next check: ${new Date(m.next_run).toLocaleString()}.`,
        m.papers_waiting ? " New papers are waiting." : ""),
      h("label", { class: "field" }, h("span", {}, "Daily rebuild time"),
        h("span", { class: "range" }, time, h("button", {
          type: "button", class: "btn small",
          onclick: async () => {
            try {
              await api.setRebuildTime(time.value);
              toast(`Topics will be rebuilt at ${time.value}`);
              void load();
            } catch (err) {
              toast((err as Error).message, "error");
            }
          },
        }, "Save"))),
      h("div", { class: "table-wrap" },
        h("table", { class: "admin-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Task"), h("th", {}, "Last run"), h("th", {}, ""))),
          h("tbody", {}, ...m.tasks.map((t) => h("tr", {},
            h("td", {}, t.label),
            h("td", {}, when(t.last_run)),
            h("td", {}, h("button", { type: "button", class: "btn small", onclick: () => run(t.id) }, "Run now")))))),
      ),
    );
  }

  function usersTable(users: AdminUser[]): HTMLElement {
    const rows = users.map((u) => {
      const quotaInput = h("input", { type: "number", min: 0, max: 10000000, value: u.page_quota, class: "quota-input", "aria-label": `Page quota for ${u.email}` }) as HTMLInputElement;
      const act = async (changes: Parameters<typeof api.adminUpdateUser>[1], done: string) => {
        try {
          await api.adminUpdateUser(u.id, changes);
          toast(done);
          void load();
        } catch (err) {
          toast((err as Error).message, "error");
        }
      };
      const status = [u.is_admin && "administrator", u.is_disabled && "disabled", !u.verified && "not confirmed"].filter(Boolean).join(", ");
      return h("tr", {},
        h("td", {}, u.email, status ? h("span", { class: "hint" }, ` (${status})`) : null),
        h("td", {}, `${u.pages_used} / ${u.page_quota}`),
        h("td", {}, String(u.documents)),
        h("td", {}, h("span", { class: "range" }, quotaInput, h("button", {
          type: "button", class: "btn small",
          onclick: () => act({ page_quota: Number(quotaInput.value) }, "Quota saved"),
        }, "Save"))),
        h("td", { class: "actions" },
          h("button", { type: "button", class: "btn small", onclick: () => act({ is_disabled: !u.is_disabled }, u.is_disabled ? "Account enabled" : "Account disabled") },
            u.is_disabled ? "Enable" : "Disable"),
          h("button", { type: "button", class: "btn small", onclick: () => act({ is_admin: !u.is_admin }, u.is_admin ? "No longer an administrator" : "Now an administrator") },
            u.is_admin ? "Remove admin" : "Make admin"),
          h("button", { type: "button", class: "btn small", onclick: async () => {
            try {
              const { revoked } = await api.adminRevokeSessions(u.id);
              toast(`${revoked} sessions signed out`);
            } catch (err) {
              toast((err as Error).message, "error");
            }
          } }, "Sign out everywhere"),
        ),
      );
    });
    return h("div", { class: "table-wrap" },
      h("table", { class: "admin-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Account"), h("th", {}, "Pages"), h("th", {}, "Papers"), h("th", {}, "Quota"), h("th", {}, "Actions"))),
        h("tbody", {}, ...rows)));
  }

  function auditTable(rows: AuditRow[]): HTMLElement {
    if (!rows.length) return h("p", { class: "hint" }, "No changes yet.");
    return h("div", { class: "table-wrap" },
      h("table", { class: "admin-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Who"), h("th", {}, "What"), h("th", {}, "Detail"))),
        h("tbody", {}, ...rows.map((r) => h("tr", {},
          h("td", {}, new Date(r.created_at).toLocaleString()),
          h("td", {}, r.actor ?? "command line"),
          h("td", {}, r.action),
          h("td", {}, Object.keys(r.detail).length ? JSON.stringify(r.detail) : "")))),
      ));
  }

  void load();
  return { title: "Administration", el: root };
}
