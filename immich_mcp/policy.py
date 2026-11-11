"""Visibility policy: a client sees only albums matching the allow-list, and only assets inside them.

Immich's own album filter is AND (an asset must be in *every* listed album), so "in any allowed album"
can't be one query. We keep a cached set of visible asset IDs, refreshed every few minutes and after
every write, and confirm misses with a direct per-asset album lookup.
"""
from __future__ import annotations

import asyncio
import time

from .config import Config
from .immich import Immich, ImmichError


class Denied(Exception):
    """The asset or album exists (or may exist) but is outside the allow-list. Reported as not_found."""


class Policy:
    def __init__(self, cfg: Config, immich: Immich, *, album_ttl: float = 60, asset_ttl: float = 300):
        self.cfg, self.immich = cfg, immich
        self.album_ttl, self.asset_ttl = album_ttl, asset_ttl
        self._albums: list[dict] = []
        self._albums_at = float("-inf")     # never loaded (monotonic() starts near 0 on a fresh boot)
        self._assets: set[str] = set()
        self._people: dict[str, dict] = {}
        self._assets_at = float("-inf")
        self._lock = asyncio.Lock()

    def invalidate(self):
        self._albums_at = self._assets_at = float("-inf")

    async def visible_albums(self) -> list[dict]:
        if time.monotonic() - self._albums_at > self.album_ttl:
            allb = await self.immich.albums()
            self._albums = sorted((a for a in allb if self.cfg.album_allowed(a["albumName"])),
                                  key=lambda a: a["albumName"])
            self._albums_at = time.monotonic()
        return self._albums

    async def visible_asset_ids(self) -> set[str]:
        async with self._lock:
            if time.monotonic() - self._assets_at > self.asset_ttl:
                ids: set[str] = set()
                rows: dict[str, list] = {}          # asset id -> its people
                excluded: set[str] = set()

                async def scan(body: dict, into_ids: bool):
                    page = 1
                    while page:
                        r = await self.immich.search_metadata({**body, "size": 1000, "page": page, "withPeople": True})
                        for x in r["items"]:
                            if into_ids:
                                ids.add(x["id"])
                                rows[x["id"]] = x.get("people") or []
                            else:
                                excluded.add(x["id"])
                        page = int(r["nextPage"]) if r.get("nextPage") else 0

                for a in await self.visible_albums():
                    await scan({"albumIds": [a["id"]]}, True)
                if self.cfg.include_assets_without_album:
                    await scan({"isNotInAlbum": True}, True)
                if self.cfg.excluded_album_prefixes:      # deny wins: drop anything that is also in an excluded album
                    for a in await self.immich.albums():
                        if self.cfg.album_excluded(a["albumName"]):
                            await scan({"albumIds": [a["id"]]}, False)
                ids -= excluded
                people: dict[str, dict] = {}
                for aid in ids:
                    for p in rows.get(aid, []):
                        e = people.setdefault(p["id"], {"id": p["id"], "name": p.get("name") or "", "photo_count": 0})
                        e["photo_count"] += 1
                self._assets, self._people, self._assets_at = ids, people, time.monotonic()
            return self._assets

    async def visible_people(self) -> dict[str, dict]:
        """People who appear in at least one visible asset -> {id, name, photo_count (visible photos)}."""
        await self.visible_asset_ids()
        return self._people

    async def require_person(self, person_id: str) -> dict:
        p = (await self.visible_people()).get(person_id)
        if not p:
            raise Denied(person_id)
        return p

    async def album(self, album_id: str) -> dict:
        try:
            a = await self.immich.album(album_id)
        except ImmichError as e:
            if e.kind in ("not_found", "bad_request", "permission"):
                raise Denied(album_id)
            raise
        if not self.cfg.album_allowed(a["albumName"]):
            raise Denied(album_id)
        return a

    async def is_visible(self, asset_id: str) -> bool:
        if asset_id in await self.visible_asset_ids():
            return True
        try:   # cache may be stale (just uploaded/added): ask Immich directly. Exclude wins.
            names = [a["albumName"] for a in await self.immich.albums(asset_id)]
        except ImmichError:
            return False
        if any(self.cfg.album_excluded(n) for n in names):
            return False
        if any(self.cfg.album_allowed(n) for n in names):
            return True
        return not names and self.cfg.include_assets_without_album

    async def require_assets(self, ids: list[str]) -> None:
        hidden = [i for i in ids if not await self.is_visible(i)]
        if hidden:
            raise Denied(", ".join(hidden))

    async def visible_album_names_for(self, asset_id: str) -> list[dict]:
        return [{"id": a["id"], "name": a["albumName"]} for a in await self.immich.albums(asset_id)
                if self.cfg.album_allowed(a["albumName"])]
