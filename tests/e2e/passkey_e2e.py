"""End-to-end login test against native Traefik (labels from compose.yaml) + Authelia (repo config) + api.

1. password, then enroll a passkey (one-time code from the filesystem notifier), finish 2FA with it
2. the app loads and the api sees the user; sign out ends the session
3. fresh browser: "Sign in with a passkey", then the password Authelia asks for under two_factor
4. three wrong passwords trigger the brute-force ban
A CDP virtual authenticator stands in for a phone or security key.
"""
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).parent
spki = (HERE / "spki.txt").read_text().strip()
NOTIFY = HERE / "state/notification.txt"
ARGS = ["--host-resolver-rules=MAP listen.vault.akiba-z.ch:443 127.0.0.1:8443", f"--ignore-certificate-errors-spki-list={spki}"]
APP = "https://listen.vault.akiba-z.ch/"
PASSWORD = "correct-horse-battery-staple"
results: list[tuple[str, bool]] = []
API_JSON = """async (path) => { const r = await fetch(path, {redirect: 'manual'});
  const json = (r.headers.get('content-type') || '').includes('json');
  return {status: r.status, json, body: json ? await r.json() : null}; }"""


def check(name: str, ok: bool, url: str = "") -> None:
    results.append((name, ok))
    if url and not ok:
        print("     url:", url)
    print(("PASS " if ok else "FAIL ") + name)


def authenticator(page, credential=None):
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    auth_id = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
    }})["authenticatorId"]
    if credential:
        cdp.send("WebAuthn.addCredential", {"authenticatorId": auth_id, "credential": credential})
    return cdp, auth_id


def wait_code(page) -> str:
    for _ in range(40):
        if NOTIFY.exists() and (codes := re.findall(r"\b([A-Z0-9]{8,10})\b", NOTIFY.read_text().split("Date:")[-1])):
            return codes[-1]
        page.wait_for_timeout(250)
    raise RuntimeError("no one-time code written")


def in_app(page) -> bool:
    try:
        page.wait_for_url(re.compile(r"^https://listen\.vault\.akiba-z\.ch/(?!auth)"), timeout=15000)
        page.wait_for_selector(".topbar .brand", timeout=15000)
        return True
    except Exception:  # noqa: BLE001
        return False


with sync_playwright() as p:
    browser = p.chromium.launch(args=ARGS)

    # ---- 1. password + passkey enrollment
    ctx = browser.new_context()
    page = ctx.new_page()
    cdp, auth_id = authenticator(page)
    page.goto(APP)
    page.wait_for_selector("#username-textfield")
    check("unauthenticated visit lands on the Authelia portal", urlparse(page.url).path.startswith("/auth"), page.url)
    page.fill("#username-textfield", "doctor")
    page.fill("#password-textfield", PASSWORD)
    page.click("#sign-in-button")
    page.wait_for_selector("#register-link")
    check("password alone is not enough (second factor required)", "/auth/2fa" in page.url)
    NOTIFY.unlink(missing_ok=True)
    page.click("#register-link")
    page.click("#webauthn-credential-add")
    page.fill("#one-time-code", wait_code(page))
    page.click("#dialog-verify")
    page.fill("#webauthn-credential-description", "Test passkey")
    page.click("#dialog-next")
    expect(page.locator("#webauthn-credential-0-description")).to_have_text("Test passkey", timeout=15000)
    enrolled = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"][0]
    check("passkey enrolled as a discoverable credential", enrolled["isResidentCredential"])

    # finish the second factor with the new passkey
    page.goto(APP)
    check("second factor with passkey opens the app", in_app(page))
    me = page.evaluate(API_JSON, "/api/me")
    check("api sees the authenticated user", me.get("body", {}).get("user") == "doctor")
    # export after the last use, so the copy's signature counter is current (Authelia rejects clones)
    credential = cdp.send("WebAuthn.getCredentials", {"authenticatorId": auth_id})["credentials"][0]
    page.screenshot(path=HERE / "e2e-in-app.png")

    # ---- 2. sign out
    page.goto(APP + "auth/logout")
    page.wait_for_timeout(1500)
    page.goto(APP)
    page.wait_for_selector("#username-textfield", timeout=15000)
    check("after sign-out the app requires login again", urlparse(page.url).path.startswith("/auth"), page.url)
    ctx.close()

    # ---- 3. fresh browser, passkey sign-in
    ctx2 = browser.new_context()
    page2 = ctx2.new_page()
    authenticator(page2, credential)
    page2.goto(APP)
    page2.wait_for_selector("#passkey-sign-in-button")
    page2.click("#passkey-sign-in-button")
    page2.wait_for_timeout(2500)
    page2.screenshot(path=HERE / "e2e-after-passkey.png")
    error = page2.get_by_text(re.compile("failed", re.I)).count() > 0
    passkey_ok = not error and not page2.locator("#username-textfield").is_visible()
    check("passkey sign-in succeeds without typing the username", passkey_ok)
    asks_password = passkey_ok and page2.locator("#password-textfield").is_visible()
    check("then asks for the password (two_factor policy)", asks_password)
    if asks_password:
        page2.fill("#password-textfield", PASSWORD)
        page2.keyboard.press("Enter")
    ok = in_app(page2)
    check("passkey + password opens the app in a fresh browser", ok, page2.url)
    page2.screenshot(path=HERE / "e2e-fresh-after-password.png")
    res = page2.evaluate(API_JSON, "/api/documents")
    check("library api answers with JSON for the session", res.get("status") == 200 and res.get("json"))
    ctx2.close()

    # ---- 4. brute-force protection
    ctx3 = browser.new_context()
    page3 = ctx3.new_page()
    page3.goto(APP)
    for _ in range(3):
        page3.wait_for_selector("#username-textfield")
        page3.fill("#username-textfield", "doctor")
        page3.fill("#password-textfield", "wrong-password")
        page3.click("#sign-in-button")
        page3.wait_for_timeout(1200)
    page3.fill("#password-textfield", PASSWORD)
    page3.click("#sign-in-button")
    page3.wait_for_timeout(2000)
    check("correct password is refused while banned after 3 failures", "/auth/2fa" not in page3.url)
    ctx3.close()
    browser.close()

print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed")
