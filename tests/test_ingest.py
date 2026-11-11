import httpx
import pytest

from immich_mcp.ingest import IngestError, fetch_url, safe_filename, sniff

from .fakes import png


async def public(host, port):
    return "93.184.216.34"


def serve(routes):
    def h(req: httpx.Request):
        assert req.extensions.get("sni_hostname"), "TLS must be verified against the real hostname"
        r = routes[req.headers["host"] + req.url.path]
        return r() if callable(r) else r
    return httpx.MockTransport(h)


async def test_fetch_ok_pins_ip_and_sniffs():
    t = serve({"img.example.com/a": httpx.Response(200, content=png(1), headers={"content-type": "image/png"})})
    m = await fetch_url("https://img.example.com/a", max_bytes=10**6, transport=t, resolver=public)
    assert m.mime == "image/png" and m.filename == "a.png"


@pytest.mark.parametrize("url,kind", [
    ("http://img.example.com/a.png", "blocked_url"),
    ("https://user:pw@img.example.com/a.png", "blocked_url"),
    ("https://127.0.0.1/a.png", "blocked_url"),
    ("https://[::1]/a.png", "blocked_url"),
    ("file:///etc/passwd", "blocked_url"),
])
async def test_fetch_rejects_bad_urls(url, kind):
    with pytest.raises(IngestError) as e:
        await fetch_url(url, max_bytes=10**6, resolver=public)
    assert e.value.kind == kind


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.5.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1",
                                "fd00::1", "::ffff:10.0.0.1", "0.0.0.0", "100.64.0.1"])
async def test_fetch_rejects_private_resolution(ip, monkeypatch):
    import immich_mcp.ingest as ing

    async def fake_getaddrinfo(host, port, type=0):
        return [(None, None, None, None, (ip, port))]

    class Loop:
        getaddrinfo = staticmethod(fake_getaddrinfo)
    monkeypatch.setattr(ing.asyncio, "get_running_loop", lambda: Loop())
    with pytest.raises(IngestError) as e:
        await fetch_url("https://evil.example.com/a.png", max_bytes=10**6)
    assert e.value.kind == "blocked_url"


async def test_redirect_to_private_is_revalidated():
    async def resolver(host, port):
        if host == "internal.example.com":
            raise IngestError("blocked_url", "internal resolves to a non-public address")
        return "93.184.216.34"
    t = serve({"img.example.com/a": httpx.Response(302, headers={"location": "https://internal.example.com/x"})})
    with pytest.raises(IngestError) as e:
        await fetch_url("https://img.example.com/a", max_bytes=10**6, transport=t, resolver=resolver)
    assert e.value.kind == "blocked_url"


async def test_redirect_to_http_refused():
    t = serve({"img.example.com/a": httpx.Response(301, headers={"location": "http://img.example.com/b"})})
    with pytest.raises(IngestError) as e:
        await fetch_url("https://img.example.com/a", max_bytes=10**6, transport=t, resolver=public)
    assert e.value.kind == "blocked_url"


async def test_size_cap_streams():
    t = serve({"img.example.com/big": httpx.Response(200, content=png(2) * 50)})
    with pytest.raises(IngestError) as e:
        await fetch_url("https://img.example.com/big", max_bytes=10_000, transport=t, resolver=public)
    assert e.value.kind == "too_large"


async def test_non_media_refused():
    t = serve({"img.example.com/p": httpx.Response(200, content=b"<html>hi</html>", headers={"content-type": "image/png"})})
    with pytest.raises(IngestError) as e:
        await fetch_url("https://img.example.com/p", max_bytes=10**6, transport=t, resolver=public)
    assert e.value.kind == "unsupported_media"


async def test_host_allowlist():
    with pytest.raises(IngestError) as e:
        await fetch_url("https://other.example.org/a.png", max_bytes=10**6, allowed_hosts=("example.com",), resolver=public)
    assert e.value.kind == "blocked_url"


def test_sniff_and_filenames():
    assert sniff(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(b"\x00\x00\x00\x18ftypheic....") == "image/heic"
    assert sniff(b"\x00\x00\x00\x18ftypisom....") == "video/mp4"
    assert safe_filename("../../etc/passwd", "image/png") == "passwd.png"
    assert safe_filename("", "image/jpeg") == "upload.jpg"
