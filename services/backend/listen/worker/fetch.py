"""Fetch article URLs safely.

Guards: only http(s), only public IP addresses (checked for the first request and every redirect),
size limit, timeout. Scientific shortcuts: arXiv abstract pages map to their PDF, and HTML pages
that advertise a PDF via <meta name="citation_pdf_url"> are fetched as PDF when possible.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) listen-vault/0.1 (personal paper reader)"

ARXIV_ABS = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/([^?#]+)", re.I)
CITATION_PDF = re.compile(
    r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""",
    re.I,
)


class FetchError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Fetched:
    url: str
    content: bytes
    content_type: str

    @property
    def is_pdf(self) -> bool:
        return self.content.startswith(b"%PDF-") or "application/pdf" in self.content_type


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("Only http and https URLs are supported")
    if parts.username or parts.password:
        raise FetchError("URLs with credentials are not allowed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise FetchError(f"Cannot resolve {parts.hostname}", retryable=True) from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(f"{parts.hostname} resolves to a non-public address; refusing to fetch it")


def get(url: str, client: httpx.Client | None = None) -> Fetched:
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            try:
                with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(
                            f"{url} returned HTTP {resp.status_code}",
                            retryable=resp.status_code in (429, 500, 502, 503, 504),
                        )
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > MAX_BYTES:
                        raise FetchError("Document is larger than 50 MB")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise FetchError("Document is larger than 50 MB")
                    return Fetched(str(resp.url), bytes(body), resp.headers.get("content-type", ""))
            except httpx.TransportError as e:
                raise FetchError(f"Could not fetch {url}: {e}", retryable=True) from e
        raise FetchError("Too many redirects")
    finally:
        if own:
            client.close()


def fetch_article(url: str, client: httpx.Client | None = None) -> Fetched:
    """Fetch a URL, preferring the PDF version of scientific articles when one is available."""
    if m := ARXIV_ABS.match(url):
        try:
            pdf = get(f"https://arxiv.org/pdf/{m.group(1)}", client)
            if pdf.is_pdf:
                return pdf
        except FetchError:
            pass
    page = get(url, client)
    if page.is_pdf:
        return page
    head = page.content[:200_000].decode("utf-8", "replace")
    if m := CITATION_PDF.search(head):
        pdf_url = urljoin(page.url, m.group(1) or m.group(2))
        try:
            pdf = get(pdf_url, client)
            if pdf.is_pdf:
                return pdf
        except FetchError:
            pass  # publisher blocks direct PDF access: fall back to the HTML article
    return page
