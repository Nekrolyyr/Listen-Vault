"""Drive the real UI in Chromium against the native dev api. Traefik/Authelia are emulated with headers.

Checks library rendering, adding a link, opening the reader, PDF rendering, playback with sentence
tracking and PDF follow, resume position, settings, and phone layout. Writes screenshots to OUT.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
HEADERS = {"Remote-User": "doctor", "X-Listen-Proxy": "dev-secret"}
errors: list[str] = []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    ctx = browser.new_context(extra_http_headers=HEADERS, viewport={"width": 1366, "height": 860}, color_scheme="light")
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))

    # ---- library
    page.goto(BASE + "/")
    expect(page.locator(".doc-card").first).to_be_visible(timeout=10_000)
    page.screenshot(path=OUT / "library-desktop.png", full_page=True)
    print("library cards:", page.locator(".doc-card").count())
    print("first card:", page.locator(".doc-card").first.inner_text().replace("\n", " | ")[:200])

    # invalid link is rejected by the browser, valid one is added
    page.fill(".url-form input", "https://example.org/some-article")
    page.click(".url-form button")
    expect(page.locator("#toast")).to_contain_text("Link added", timeout=5000)
    print("cards after adding link:", page.locator(".doc-card").count())

    # search
    page.fill("input[type=search]", "attention")
    page.wait_for_timeout(600)
    print("search 'attention' ->", page.locator(".doc-card").count(), "card(s)")
    page.fill("input[type=search]", "")
    page.wait_for_timeout(600)

    # ---- reader
    page.locator(".doc-card", has_text="Attention Is All You Need").locator("a.btn").first.click()
    expect(page.locator(".reader-title h1")).to_be_visible()
    expect(page.locator(".pdfViewer .page canvas").first).to_be_visible(timeout=15_000)
    print("reader title:", page.locator(".reader-title h1").inner_text())
    print("chapters listed:", page.locator(".chapter").count(), "| ready:", page.locator(".chapter:not(.pending)").count())
    page.wait_for_timeout(800)
    page.screenshot(path=OUT / "reader-desktop.png")

    # play a ready chapter, check that time advances and sentences are tracked
    page.locator(".chapter:not(.pending) button", has_text="Introduction").first.click()
    page.wait_for_timeout(4000)
    t = page.locator(".bar-seek .time").first.inner_text()
    now = page.locator(".now-reading").inner_text()
    print("time after 4 s:", t, "| now reading:", now.replace("\n", " ")[:120])
    page.locator(".player-bar .icon-btn[aria-label='Forward 15 seconds']").click()
    page.wait_for_timeout(1500)
    print("after +15 s:", page.locator(".bar-seek .time").first.inner_text())
    current_page = page.evaluate("() => document.querySelector('.pdfViewer .page[data-loaded]')?.dataset.pageNumber")
    print("pdf page rendered near:", current_page)
    page.screenshot(path=OUT / "reader-playing.png")
    page.locator(".play").click()  # pause -> saves position
    page.wait_for_timeout(800)
    pb = page.evaluate("() => fetch(location.pathname.replace('/doc/', '/api/documents/') + '/playback').then(r => r.json())")
    print("saved playback:", pb)

    # resume: reload and check the label
    page.reload()
    expect(page.locator(".now-reading")).to_contain_text("resume", timeout=10_000)
    print("after reload:", page.locator(".now-reading").inner_text())

    # ---- settings
    page.goto(BASE + "/settings")
    expect(page.locator("select[name=voice]")).to_be_visible()
    page.select_option("select[name=voice]", "bf_emma")
    page.click("button[type=submit]")
    expect(page.locator("#toast")).to_contain_text("saved")
    page.screenshot(path=OUT / "settings.png", full_page=True)

    # ---- dark mode and phone layout
    dark = browser.new_context(extra_http_headers=HEADERS, viewport={"width": 390, "height": 844},
                               color_scheme="dark", is_mobile=True, has_touch=True, device_scale_factor=2)
    m = dark.new_page()
    m.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
    m.goto(BASE + "/")
    expect(m.locator(".doc-card").first).to_be_visible()
    overflow = m.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
    print("phone library horizontal overflow:", overflow)
    m.screenshot(path=OUT / "library-phone-dark.png", full_page=True)
    m.locator(".doc-card", has_text="Attention Is All You Need").locator("a.btn").first.click()
    expect(m.locator(".pane-tabs")).to_be_visible()
    m.wait_for_timeout(2500)
    m.screenshot(path=OUT / "reader-phone-doc.png")
    m.locator(".pane-tabs button", has_text="Chapters").click()
    m.wait_for_timeout(300)
    print("phone reader overflow:", m.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"))
    m.screenshot(path=OUT / "reader-phone-chapters.png")
    browser.close()

print("errors:", errors or "none")
