import httpx
import pytest

from listen.worker import fetch

PUBLIC = "93.184.215.14"  # an IP literal resolves without DNS


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost:8080/", "http://10.1.2.3/x", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "ftp://example.org/file", "file:///etc/passwd",
    "http://user:pw@93.184.215.14/",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(fetch.FetchError):
        fetch.check_url(url)


def _client(routes):
    def handler(request: httpx.Request):
        key = str(request.url)
        if key not in routes:
            return httpx.Response(404)
        status, headers, body = routes[key]
        return httpx.Response(status, headers=headers, content=body)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_redirect_to_private_address_is_blocked():
    client = _client({f"http://{PUBLIC}/a": (302, {"location": "http://10.0.0.5/secret"}, b"")})
    with pytest.raises(fetch.FetchError, match="non-public"):
        fetch.get(f"http://{PUBLIC}/a", client)


def test_follows_citation_pdf_url():
    html = f'<html><head><meta name="citation_pdf_url" content="http://{PUBLIC}/paper.pdf"></head></html>'.encode()
    client = _client({
        f"http://{PUBLIC}/article": (200, {"content-type": "text/html"}, html),
        f"http://{PUBLIC}/paper.pdf": (200, {"content-type": "application/pdf"}, b"%PDF-1.7 ..."),
    })
    got = fetch.fetch_article(f"http://{PUBLIC}/article", client)
    assert got.is_pdf and got.url.endswith("/paper.pdf")


def test_blocked_pdf_falls_back_to_html():
    html = f'<html><head><meta name="citation_pdf_url" content="http://{PUBLIC}/paper.pdf"></head></html>'.encode()
    client = _client({
        f"http://{PUBLIC}/article": (200, {"content-type": "text/html"}, html),
        f"http://{PUBLIC}/paper.pdf": (403, {}, b"forbidden"),
    })
    got = fetch.fetch_article(f"http://{PUBLIC}/article", client)
    assert not got.is_pdf and got.content == html


def test_size_limit(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    client = _client({f"http://{PUBLIC}/big": (200, {}, b"x" * 100)})
    with pytest.raises(fetch.FetchError, match="larger"):
        fetch.get(f"http://{PUBLIC}/big", client)


def test_server_errors_are_retryable():
    client = _client({f"http://{PUBLIC}/down": (503, {}, b"")})
    with pytest.raises(fetch.FetchError) as e:
        fetch.get(f"http://{PUBLIC}/down", client)
    assert e.value.retryable
