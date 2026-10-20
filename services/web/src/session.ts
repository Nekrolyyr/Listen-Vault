// The signed-in account, loaded once when the app starts. Views read it; sign-in and sign-out change it.
import type { Me } from "./api";

export let session: Me | null = null;

export function setSession(me: Me | null): void {
  session = me;
}

/** Asks the api who is signed in. Any failure means nobody is. */
export async function loadSession(): Promise<Me | null> {
  try {
    const res = await fetch("/api/me", { credentials: "same-origin" });
    session = res.ok ? ((await res.json()) as Me) : null;
  } catch {
    session = null;
  }
  return session;
}

/** Changes the address and tells the router, without importing it (the router imports the views). */
export function go(path: string, replace = false): void {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
