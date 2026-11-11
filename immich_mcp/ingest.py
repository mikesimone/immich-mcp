"""Getting bytes in: base64 from the client, a restricted HTTPS fetch, and media sniffing.

URL fetches are the SSRF surface, so: https only; every DNS answer must be a public (global) address;
the connection is pinned to the address we checked (no DNS-rebinding window) with TLS still verified
against the real hostname; each redirect hop is re-validated; the body is streamed with a hard size
cap; and the result must sniff as an image or video. Credentials in URLs are refused.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx


class IngestError(Exception):
    """kind: bad_request | too_large | unsupported_media | blocked_url | network"""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind, self.message = kind, message


@dataclass
class Media:
    data: bytes
    mime: str
    filename: str


# magic bytes -> mime. Only media Immich ingests.
_SIGS = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"BM", 0, "image/bmp"),
    (b"II*\x00", 0, "image/tiff"),
    (b"MM\x00*", 0, "image/tiff"),
    (b"\x1aE\xdf\xa3", 0, "video/webm"),
]
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
        "image/heic": ".heic", "image/heif": ".heif", "image/avif": ".avif", "image/bmp": ".bmp",
        "image/tiff": ".tif", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "image/jxl": ".jxl"}


def sniff(data: bytes) -> str | None:
    for sig, off, mime in _SIGS:
        if data[off:off + len(sig)] == sig:
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "image/jxl"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    return None


def safe_filename(name: str | None, mime: str) -> str:
    name = unquote(name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")[:120]
    ext = _EXT.get(mime, "")
    if not name:
        name = "upload"
    if ext and not name.lower().endswith(ext) and "." not in name[-6:]:
        name += ext
    return name


def check_media(data: bytes, filename: str | None, declared_mime: str | None, max_bytes: int) -> Media:
    if not data:
        raise IngestError("bad_request", "empty file")
    if len(data) > max_bytes:
        raise IngestError("too_large", f"{len(data)} bytes exceeds the {max_bytes}-byte limit")
    mime = sniff(data)
    if not mime:
        raise IngestError("unsupported_media", f"not a recognised image or video (declared {declared_mime or 'unknown'})")
    return Media(data, mime, safe_filename(filename, mime))


def from_base64(text: str, filename: str | None, declared_mime: str | None, max_bytes: int) -> Media:
    m = re.match(r"data:([\w/+.-]+);base64,", text)
    if m:
        declared_mime, text = declared_mime or m.group(1), text[m.end():]
    if len(text) > max_bytes * 4 // 3 + 8:
        raise IngestError("too_large", f"base64 payload exceeds the {max_bytes}-byte limit; use a URL instead")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", text), validate=True)
    except (binascii.Error, ValueError):
        raise IngestError("bad_request", "data_base64 is not valid base64")
    return check_media(data, filename, declared_mime, max_bytes)


def _ip_ok(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


async def _resolve(host: str, port: int) -> str:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise IngestError("network", f"cannot resolve {host}")
    addrs = {i[4][0] for i in infos}
    bad = [a for a in addrs if not _ip_ok(a)]
    if not addrs or bad:
        raise IngestError("blocked_url", f"{host} resolves to a non-public address")
    return sorted(addrs)[0]


def _validate(url: str, allowed_hosts: tuple[str, ...]) -> tuple[str, int]:
    u = urlsplit(url)
    if u.scheme != "https":
        raise IngestError("blocked_url", "only https URLs are accepted")
    if u.username or u.password:
        raise IngestError("blocked_url", "URLs with credentials are refused")
    host = (u.hostname or "").lower()
    if not host:
        raise IngestError("blocked_url", "URL has no host")
    try:
        ipaddress.ip_address(host)
        raise IngestError("blocked_url", "literal IP addresses are refused")
    except ValueError:
        pass
    if allowed_hosts and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise IngestError("blocked_url", f"{host} is not on the URL allow-list")
    return host, u.port or 443


async def fetch_url(url: str, *, max_bytes: int, allowed_hosts: tuple[str, ...] = (), timeout: float = 60.0,
                    max_redirects: int = 3, filename: str | None = None,
                    transport: httpx.AsyncBaseTransport | None = None, resolver=None) -> Media:
    resolver = resolver or _resolve
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport) as client:
        for _ in range(max_redirects + 1):
            host, port = _validate(url, allowed_hosts)
            ip = await resolver(host, port)
            u = urlsplit(url)
            target = u._replace(netloc=(f"[{ip}]" if ":" in ip else ip) + (f":{u.port}" if u.port else "")).geturl()
            req = client.build_request("GET", target, headers={"Host": u.netloc, "Accept": "image/*,video/*"},
                                       extensions={"sni_hostname": host})
            try:
                resp = await client.send(req, stream=True)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                raise IngestError("network", f"fetch failed: {type(e).__name__}")
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise IngestError("network", "redirect without a Location")
                    url = urljoin(url, loc)
                    continue
                if resp.status_code != 200:
                    raise IngestError("network", f"fetch returned HTTP {resp.status_code}")
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    raise IngestError("too_large", f"{clen} bytes exceeds the {max_bytes}-byte limit")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise IngestError("too_large", f"download exceeds the {max_bytes}-byte limit")
                name = filename
                cd = resp.headers.get("content-disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
                if not name and m:
                    name = m.group(1)
                if not name:
                    name = urlsplit(url).path.rsplit("/", 1)[-1]
                return check_media(bytes(buf), name, resp.headers.get("content-type"), max_bytes)
            finally:
                await resp.aclose()
        raise IngestError("blocked_url", "too many redirects")
