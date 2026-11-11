"""Thin async client for Immich's internal API, with bounded retries and classified errors."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

log = logging.getLogger("immich_mcp.immich")

RETRY_STATUS = {429, 500, 502, 503, 504}


class ImmichError(Exception):
    """kind: auth | permission | not_found | bad_request | unsupported_media | network | server"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(f"[{kind}] {message}")
        self.kind, self.message, self.status = kind, message, status


def _classify(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 403:
        return "permission"
    if status == 404:
        return "not_found"
    if status in (400, 422):
        return "bad_request"
    if status == 415:
        return "unsupported_media"
    return "server"


class Immich:
    def __init__(self, base_url: str, api_key: str, *, retries: int = 3, backoff: float = 0.5,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 120.0):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api",
                                         headers={"x-api-key": api_key, "Accept": "application/json"},
                                         timeout=timeout, transport=transport)
        self.retries, self.backoff = retries, backoff

    async def aclose(self):
        await self._client.aclose()

    async def request(self, method: str, path: str, *, raw: bool = False, **kw) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = await self._client.request(method, path, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = ImmichError("network", f"{method} {path}: {type(e).__name__}")
            else:
                if r.status_code < 400:
                    if raw:
                        return r
                    return r.json() if r.content else None
                if r.status_code not in RETRY_STATUS:
                    try:
                        detail = r.json().get("message")
                    except Exception:
                        detail = r.text[:200]
                    if isinstance(detail, list):
                        detail = "; ".join(map(str, detail))
                    raise ImmichError(_classify(r.status_code), f"{method} {path}: {detail}", r.status_code)
                last = ImmichError("server", f"{method} {path}: HTTP {r.status_code}", r.status_code)
            if attempt < self.retries:
                await asyncio.sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    # ---- reads
    async def version(self) -> str:
        v = await self.request("GET", "/server/version")
        return f"{v['major']}.{v['minor']}.{v['patch']}"

    async def albums(self, asset_id: str | None = None) -> list[dict]:
        return await self.request("GET", "/albums", params={"assetId": asset_id} if asset_id else None)

    async def album(self, album_id: str) -> dict:
        return await self.request("GET", f"/albums/{album_id}")

    async def asset(self, asset_id: str) -> dict:
        return await self.request("GET", f"/assets/{asset_id}")

    async def search_metadata(self, body: dict) -> dict:
        return (await self.request("POST", "/search/metadata", json=body))["assets"]

    async def search_smart(self, body: dict) -> dict:
        return (await self.request("POST", "/search/smart", json=body))["assets"]

    async def thumbnail(self, asset_id: str, size: str) -> tuple[bytes, str]:
        r = await self.request("GET", f"/assets/{asset_id}/thumbnail", params={"size": size}, raw=True)
        return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]

    async def duplicates(self) -> list[dict]:
        return await self.request("GET", "/duplicates")

    # ---- writes
    async def upload(self, data: bytes, filename: str, mime: str, *, device_asset_id: str,
                     created_at: str, sha1_hex: str) -> dict:
        files = {"assetData": (filename, data, mime)}
        form = {"deviceAssetId": device_asset_id, "deviceId": "immich-mcp",
                "fileCreatedAt": created_at, "fileModifiedAt": created_at, "filename": filename}
        # x-immich-checksum lets Immich answer "duplicate" without storing the bytes again,
        # which also makes a retried upload safe.
        return await self.request("POST", "/assets", data=form, files=files,
                                  headers={"x-immich-checksum": sha1_hex})

    async def create_album(self, name: str, description: str = "") -> dict:
        return await self.request("POST", "/albums", json={"albumName": name, "description": description})

    async def update_album(self, album_id: str, fields: dict) -> dict:
        return await self.request("PATCH", f"/albums/{album_id}", json=fields)

    async def add_to_album(self, album_id: str, ids: list[str]) -> list[dict]:
        return await self.request("PUT", f"/albums/{album_id}/assets", json={"ids": ids})

    async def remove_from_album(self, album_id: str, ids: list[str]) -> list[dict]:
        return await self.request("DELETE", f"/albums/{album_id}/assets", json={"ids": ids})

    async def update_assets(self, ids: list[str], fields: dict) -> None:
        await self.request("PUT", "/assets", json={"ids": ids, **fields})

    async def update_person(self, person_id: str, fields: dict) -> dict:
        return await self.request("PUT", f"/people/{person_id}", json=fields)

    async def tags(self) -> list[dict]:
        return await self.request("GET", "/tags")

    async def create_tag(self, name: str) -> dict:
        return await self.request("POST", "/tags", json={"name": name})

    async def tag_assets(self, tag_id: str, ids: list[str]) -> list[dict]:
        return await self.request("PUT", f"/tags/{tag_id}/assets", json={"ids": ids})

    async def untag_assets(self, tag_id: str, ids: list[str]) -> list[dict]:
        return await self.request("DELETE", f"/tags/{tag_id}/assets", json={"ids": ids})

    async def create_shared_link(self, body: dict) -> dict:
        return await self.request("POST", "/shared-links", json=body)

    async def delete_shared_link(self, link_id: str) -> None:
        await self.request("DELETE", f"/shared-links/{link_id}")

    async def trash(self, ids: list[str]) -> None:
        # force=False: Immich moves them to its trash (restorable for 30 days). Never permanent.
        await self.request("DELETE", "/assets", json={"ids": ids, "force": False})


def new_id() -> str:
    return str(uuid.uuid4())
