"""Tool logic, independent of MCP and of the transport. server.py only adapts these to MCP tools."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .auth import principal
from .config import Config
from .immich import Immich, ImmichError
from .ingest import IngestError, Media, check_media, fetch_url, from_base64
from .ledger import Ledger, upload_id_for
from .policy import Denied, Policy

audit_log = logging.getLogger("immich_mcp.audit")
PAGE_MAX = 100


class ToolFailure(Exception):
    """Raised to the client as a tool error. kind is machine-readable."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind, self.message = kind, message


def audit(action: str, **fields):
    audit_log.info(json.dumps({"action": action, "principal": principal.get(), **fields}, default=str))


def _asset_summary(a: dict) -> dict:
    exif = a.get("exifInfo") or {}
    return {
        "id": a["id"], "type": a.get("type"), "filename": a.get("originalFileName"),
        "mime_type": a.get("originalMimeType"),
        "taken_at": a.get("localDateTime") or a.get("fileCreatedAt"),
        "width": a.get("width") or exif.get("exifImageWidth"), "height": a.get("height") or exif.get("exifImageHeight"),
        "is_favorite": a.get("isFavorite", False), "duplicate_id": a.get("duplicateId"),
        "description": exif.get("description") or None,
    }


def _album_summary(a: dict) -> dict:
    return {"id": a["id"], "name": a["albumName"], "asset_count": a.get("assetCount", 0),
            "description": a.get("description") or "", "start_date": a.get("startDate"), "end_date": a.get("endDate")}


# Same convention as the semantic captioner: prose caption first, then (optionally) this separator and the
# image's generation JSON. Caption edits only ever touch the part before it.
GEN_INFO_SEPARATOR = "\n\n--- generation info ---\n"


def split_description(desc: str | None) -> tuple[str, str]:
    """-> (caption, tail) where tail is "" or GEN_INFO_SEPARATOR + generation info, unchanged."""
    desc = desc or ""
    i = desc.find(GEN_INFO_SEPARATOR)
    if i == -1:
        return desc.strip(), ""
    return desc[:i].strip(), desc[i:]


def _page(page: int, page_size: int) -> tuple[int, int]:
    return max(1, int(page or 1)), min(PAGE_MAX, max(1, int(page_size or 25)))


class Service:
    def __init__(self, cfg: Config, immich: Immich, ledger: Ledger):
        self.cfg, self.immich, self.ledger = cfg, immich, ledger
        self.policy = Policy(cfg, immich)

    async def _wrap(self, coro):
        try:
            return await coro
        except Denied as e:
            raise ToolFailure("not_found", f"not found or not visible to this connector: {e}")
        except ImmichError as e:
            raise ToolFailure(e.kind, e.message)
        except IngestError as e:
            raise ToolFailure(e.kind, e.message)

    # ------------------------------------------------------------------ discovery
    async def server_info(self) -> dict:
        return {
            "server": "immich-mcp", "immich_version": await self.immich.version(),
            "visible_album_prefixes": list(self.cfg.allowed_album_prefixes),
            "visible_album_count": len(await self.policy.visible_albums()),
            "limits": {"max_upload_bytes": self.cfg.max_upload_bytes, "max_base64_bytes": self.cfg.max_base64_bytes,
                       "max_page_size": PAGE_MAX},
            "upload_sources": ["data_base64", "url (public https only)", "file (ChatGPT file param)"],
            "delete_behavior": "moves to Immich trash (restorable 30 days); never permanent",
            "share_links_enabled": bool(self.cfg.share_links_enabled and self.cfg.public_immich_url),
        }

    async def list_albums(self, name_contains: str | None, page: int, page_size: int) -> dict:
        page, size = _page(page, page_size)
        albums = await self.policy.visible_albums()
        if name_contains:
            albums = [a for a in albums if name_contains.lower() in a["albumName"].lower()]
        chunk = albums[(page - 1) * size: page * size]
        return {"albums": [_album_summary(a) for a in chunk], "total": len(albums), "page": page,
                "next_page": page + 1 if page * size < len(albums) else None}

    async def get_album(self, album_id: str, page: int, page_size: int) -> dict:
        page, size = _page(page, page_size)
        a = await self.policy.album(album_id)
        r = await self.immich.search_metadata({"albumIds": [album_id], "page": page, "size": size,
                                               "order": "asc", "withExif": True})
        return {"album": _album_summary(a), "assets": [_asset_summary(x) for x in r["items"]], "page": page,
                "next_page": int(r["nextPage"]) if r.get("nextPage") else None}

    async def search_assets(self, *, query, filename, description, asset_type, taken_after, taken_before,
                            album_id, is_favorite, page, page_size, person_ids=None, tag_ids=None) -> dict:
        page, size = _page(page, page_size)
        body: dict = {"withExif": True}
        if asset_type:
            body["type"] = asset_type
        if taken_after:
            body["takenAfter"] = taken_after
        if taken_before:
            body["takenBefore"] = taken_before
        if is_favorite is not None:
            body["isFavorite"] = is_favorite
        if album_id:
            await self.policy.album(album_id)
            body["albumIds"] = [album_id]
        if person_ids:
            body["personIds"] = list(person_ids)       # Immich: the asset must show ALL of these people
        if tag_ids:
            body["tagIds"] = list(tag_ids)
        if not query:
            if filename:
                body["originalFileName"] = filename
            if description:
                body["description"] = description
        search = self.immich.search_smart if query else self.immich.search_metadata
        if query:
            body["query"] = query
        visible = None if album_id else await self.policy.visible_asset_ids()
        want_from, want_to = (page - 1) * size, page * size
        hits, scanned, ipage, more = [], 0, 1, True
        while more and len(hits) < want_to + 1 and scanned < self.cfg.search_scan_limit:
            r = await search({**body, "page": ipage, "size": 100})
            items = r["items"]
            scanned += len(items)
            if query and (filename or description):   # smart search can't combine these server-side
                items = [x for x in items if (not filename or filename.lower() in (x.get("originalFileName") or "").lower())
                         and (not description or description.lower() in ((x.get("exifInfo") or {}).get("description") or "").lower())]
            hits += [x for x in items if visible is None or x["id"] in visible]
            more = bool(r.get("nextPage"))
            ipage += 1
        return {"assets": [_asset_summary(x) for x in hits[want_from:want_to]], "page": page,
                "next_page": page + 1 if len(hits) > want_to else None,
                "truncated_scan": more and scanned >= self.cfg.search_scan_limit}

    async def get_asset(self, asset_id: str) -> dict:
        await self.policy.require_assets([asset_id])
        a = await self.immich.asset(asset_id)
        out = _asset_summary(a)
        exif = a.get("exifInfo") or {}
        out.update({
            "rating": exif.get("rating"), "camera": " ".join(x for x in (exif.get("make"), exif.get("model")) if x) or None,
            "location": ", ".join(x for x in (exif.get("city"), exif.get("state"), exif.get("country")) if x) or None,
            "file_size": exif.get("fileSizeInByte"),
            "people": [{"id": p["id"], "name": p.get("name") or ""} for p in a.get("people", []) or []],
            "tags": [t.get("value") or t.get("name") for t in a.get("tags", []) or []],
            "albums": await self.policy.visible_album_names_for(asset_id),
            "is_archived": a.get("visibility") == "archive",
            "is_trashed": a.get("isTrashed", False),
        })
        return out

    async def get_asset_image(self, asset_id: str, size: str) -> tuple[bytes, str, dict]:
        await self.policy.require_assets([asset_id])
        data, mime = await self.immich.thumbnail(asset_id, size)
        if len(data) > self.cfg.max_image_return_bytes and size == "preview":
            data, mime = await self.immich.thumbnail(asset_id, "thumbnail")
            size = "thumbnail"
        return data, mime, {"asset_id": asset_id, "size": size, "mime_type": mime, "bytes": len(data)}

    async def get_duplicates(self, page: int, page_size: int) -> dict:
        page, size = _page(page, page_size)
        visible = await self.policy.visible_asset_ids()
        groups = []
        for g in await self.immich.duplicates():
            assets = [a for a in g.get("assets", []) if a["id"] in visible]
            if len(assets) >= 2:
                groups.append({"duplicate_id": g["duplicateId"], "assets": [_asset_summary(a) for a in assets],
                               "suggested_keep": [i for i in g.get("suggestedKeepAssetIds", []) if i in visible]})
        return {"groups": groups[(page - 1) * size: page * size], "total": len(groups), "page": page,
                "next_page": page + 1 if page * size < len(groups) else None}

    # ------------------------------------------------------------------ albums
    async def _resolve_album(self, album_id: str | None, album_name: str | None, create: bool) -> dict:
        if album_id:
            return await self.policy.album(album_id)
        if not album_name:
            raise ToolFailure("bad_request", "give album_id or album_name")
        for a in await self.policy.visible_albums():
            if a["albumName"] == album_name:
                return a
        if not create:
            raise ToolFailure("not_found", f"no visible album named {album_name!r}")
        return (await self.create_album(album_name, ""))["album_raw"]

    async def create_album(self, name: str, description: str) -> dict:
        name = name.strip()
        if not self.cfg.album_allowed(name):
            raise ToolFailure("permission", f"album names must start with one of {list(self.cfg.allowed_album_prefixes)}")
        self.policy.invalidate()
        for a in await self.policy.visible_albums():
            if a["albumName"] == name:
                return {"album": _album_summary(a), "created": False, "album_raw": a}
        a = await self.immich.create_album(name, description)
        audit("create_album", album_id=a["id"], name=name)
        self.policy.invalidate()
        return {"album": _album_summary(a), "created": True, "album_raw": a}

    async def update_album(self, album_id: str, new_name: str | None, description: str | None) -> dict:
        if new_name is None and description is None:
            raise ToolFailure("bad_request", "give new_name and/or description")
        a = await self.policy.album(album_id)                 # must be visible now
        fields: dict = {}
        if new_name is not None:
            new_name = new_name.strip()
            if not self.cfg.album_allowed(new_name):           # ...and stay visible afterwards
                raise ToolFailure("permission", f"album names must start with one of {list(self.cfg.allowed_album_prefixes)}")
            if new_name != a["albumName"]:
                self.policy.invalidate()
                clash = [x for x in await self.policy.visible_albums() if x["albumName"] == new_name and x["id"] != album_id]
                if clash:
                    raise ToolFailure("bad_request", f"another album is already named {new_name!r} ({clash[0]['id']})")
            fields["albumName"] = new_name
        if description is not None:
            fields["description"] = description
        updated = await self.immich.update_album(album_id, fields)
        audit("update_album", album_id=album_id, old_name=a["albumName"], new_name=fields.get("albumName"),
              description_changed=description is not None)
        self.policy.invalidate()
        return {"album": _album_summary({**a, **updated}), "old_name": a["albumName"]}

    async def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> dict:
        a = await self.policy.album(album_id)
        await self.policy.require_assets(asset_ids)   # never pull a hidden asset into a visible album
        res = await self.immich.add_to_album(album_id, asset_ids)
        audit("add_assets_to_album", album_id=album_id, asset_ids=asset_ids)
        self.policy.invalidate()
        return {"album_id": album_id, "album_name": a["albumName"], "results": [
            {"asset_id": r["id"], "status": "added" if r.get("success") else ("already_in_album" if r.get("error") == "duplicate" else "failed"),
             "error": None if r.get("success") or r.get("error") == "duplicate" else r.get("error")} for r in res]}

    async def remove_assets_from_album(self, album_id: str, asset_ids: list[str], confirm: bool) -> dict:
        if confirm is not True:
            raise ToolFailure("confirmation_required", "set confirm=true to remove assets from the album (originals are kept)")
        a = await self.policy.album(album_id)
        res = await self.immich.remove_from_album(album_id, asset_ids)
        audit("remove_assets_from_album", album_id=album_id, asset_ids=asset_ids)
        self.policy.invalidate()
        return {"album_id": album_id, "album_name": a["albumName"], "results": [
            {"asset_id": r["id"], "status": "removed" if r.get("success") else "failed",
             "error": None if r.get("success") else r.get("error")} for r in res]}

    # ------------------------------------------------------------------ asset changes
    async def set_caption(self, asset_ids: list[str], caption: str, mode: str) -> dict:
        """Edit only the caption part of each description; a generation-info section after GEN_INFO_SEPARATOR
        (written by the semantic captioner for AI-generated images) is kept byte-for-byte."""
        if mode not in ("replace", "append"):
            raise ToolFailure("bad_request", "mode must be replace or append")
        await self.policy.require_assets(asset_ids)
        caption = caption.strip()
        results = []
        for aid in asset_ids:
            current = ((await self.immich.asset(aid)).get("exifInfo") or {}).get("description") or ""
            prose, tail = split_description(current)
            new_prose = (prose + "\n\n" + caption).strip() if mode == "append" and prose else caption
            new_desc = new_prose + tail
            if new_desc != current:
                await self.immich.update_assets([aid], {"description": new_desc})
            results.append({"asset_id": aid, "caption": new_prose, "kept_generation_info": bool(tail),
                            "changed": new_desc != current})
        audit("set_caption", asset_ids=asset_ids, mode=mode, chars=len(caption))
        return {"results": results}

    async def update_asset_metadata(self, asset_ids: list[str], description, is_favorite, rating, taken_at) -> dict:
        await self.policy.require_assets(asset_ids)
        fields = {k: v for k, v in {"isFavorite": is_favorite, "rating": rating,
                                    "dateTimeOriginal": taken_at}.items() if v is not None}
        if not fields and description is None:
            raise ToolFailure("bad_request", "nothing to update")
        if fields:
            await self.immich.update_assets(asset_ids, fields)
        if description is not None:            # same caption-only rule as set_caption
            await self.set_caption(asset_ids, description, "replace")
        names = sorted(fields) + (["description"] if description is not None else [])
        audit("update_asset_metadata", asset_ids=asset_ids, fields=names)
        return {"updated": asset_ids, "fields": names}

    async def delete_assets(self, asset_ids: list[str], confirm: bool) -> dict:
        if confirm is not True:
            raise ToolFailure("confirmation_required", "set confirm=true to move these assets to the Immich trash")
        await self.policy.require_assets(asset_ids)
        await self.immich.trash(asset_ids)
        audit("delete_assets", asset_ids=asset_ids, mode="trash")
        self.policy.invalidate()
        return {"trashed": asset_ids, "restorable_days": 30}

    # ------------------------------------------------------------------ people
    async def list_people(self, name_contains: str | None, named_only: bool, page: int, page_size: int) -> dict:
        page, size = _page(page, page_size)
        people = list((await self.policy.visible_people()).values())
        if named_only:
            people = [p for p in people if p["name"]]
        if name_contains:
            people = [p for p in people if name_contains.lower() in p["name"].lower()]
        people.sort(key=lambda p: (-p.get("photo_count", 0), p["name"].lower()))
        chunk = people[(page - 1) * size: page * size]
        return {"people": [{"id": p["id"], "name": p["name"], "photo_count": p.get("photo_count", 0)} for p in chunk],
                "total": len(people), "page": page, "next_page": page + 1 if page * size < len(people) else None}

    async def name_person(self, person_id: str, name: str) -> dict:
        before = await self.policy.require_person(person_id)
        name = name.strip()
        if not name:
            raise ToolFailure("bad_request", "name must not be empty")
        await self.immich.update_person(person_id, {"name": name})
        audit("name_person", person_id=person_id, old_name=before["name"], new_name=name)
        self.policy.invalidate()
        return {"id": person_id, "name": name, "old_name": before["name"]}

    # ------------------------------------------------------------------ archive
    async def set_archived(self, asset_ids: list[str], archived: bool) -> dict:
        await self.policy.require_assets(asset_ids)
        changed, unchanged, skipped = [], [], []
        target = "archive" if archived else "timeline"
        for aid in asset_ids:
            vis = (await self.immich.asset(aid)).get("visibility")
            if vis not in ("archive", "timeline"):
                skipped.append({"asset_id": aid, "visibility": vis})   # locked / hidden: never touched
            elif vis == target:
                unchanged.append(aid)
            else:
                changed.append(aid)
        if changed:
            await self.immich.update_assets(changed, {"visibility": target})
            audit("set_archived", asset_ids=changed, archived=archived)
        return {"archived": archived, "changed": changed, "unchanged": unchanged, "skipped": skipped}

    # ------------------------------------------------------------------ batch upload
    async def upload_assets(self, *, album_id, album_name, files, urls, images, taken_at) -> dict:
        items = [("file", f) for f in files or []] + [("url", u) for u in urls or []] + [("base64", i) for i in images or []]
        if not items:
            raise ToolFailure("bad_request", "give at least one of files, urls or images")
        if len(items) > self.cfg.max_batch_items:
            raise ToolFailure("too_large", f"{len(items)} items; the limit is {self.cfg.max_batch_items} per call")
        album = await self._resolve_album(album_id, album_name, create=True)
        results, counts = [], {"created": 0, "duplicate": 0, "already_uploaded": 0, "failed": 0}
        for i, (kind, it) in enumerate(items, 1):
            try:
                if kind == "file":
                    media, source = await self.get_media(data_base64=None, url=None, file=it, filename=None, mime_type=None)
                elif kind == "url":
                    media, source = await self.get_media(data_base64=None, url=it, file=None, filename=None, mime_type=None)
                else:
                    media, source = await self.get_media(data_base64=it.get("data_base64"), url=None, file=None,
                                                         filename=it.get("filename"), mime_type=it.get("mime_type"))
                r = await self.store(media, source, album, taken_at=taken_at)
                counts[r["status"]] += 1
                results.append({"index": i, "status": r["status"], "upload_id": r["upload_id"], "asset_id": r["asset_id"],
                                "filename": r["filename"], "album_status": r["album"]["status"], "error": None})
            except (IngestError, ImmichError, ToolFailure) as e:
                counts["failed"] += 1
                kind_ = getattr(e, "kind", "server")
                results.append({"index": i, "status": "failed", "upload_id": None, "asset_id": None, "filename": None,
                                "album_status": None, "error": f"[{kind_}] {getattr(e, 'message', str(e))}"})
        return {"album": {"id": album["id"], "name": album["albumName"]}, "counts": counts, "results": results}

    # ------------------------------------------------------------------ share links
    async def create_share_link(self, asset_ids: list[str], expires_in_hours: int, allow_download: bool,
                                description: str | None, confirm: bool) -> dict:
        if not self.cfg.share_links_enabled:
            raise ToolFailure("not_configured", "share links are disabled on this server (ENABLE_SHARE_LINKS)")
        if not self.cfg.public_immich_url:
            raise ToolFailure("not_configured", "PUBLIC_IMMICH_URL is not set, so a link can't be built")
        if confirm is not True:
            raise ToolFailure("confirmation_required", "set confirm=true: anyone with the link can view these photos")
        hours = max(1, min(int(expires_in_hours or 72), self.cfg.share_link_max_hours))
        await self.policy.require_assets(asset_ids)
        expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")
        link = await self.immich.create_shared_link({
            "type": "INDIVIDUAL", "assetIds": asset_ids, "expiresAt": expires, "allowDownload": bool(allow_download),
            "allowUpload": False, "showMetadata": False, **({"description": description} if description else {})})
        url = f"{self.cfg.public_immich_url}/share/{link['key']}"
        self.ledger.add_share_link(link["id"], url, asset_ids, expires, principal.get())
        audit("create_share_link", link_id=link["id"], asset_ids=asset_ids, expires_at=expires)
        return {"link_id": link["id"], "url": url, "expires_at": expires, "asset_count": len(asset_ids),
                "allow_download": bool(allow_download), "shows_metadata": False}

    async def revoke_share_link(self, link_id: str) -> dict:
        row = self.ledger.share_link(link_id)
        if not row:
            raise ToolFailure("not_found", "no share link with that id was created through this server")
        if not row["revoked"]:
            try:
                await self.immich.delete_shared_link(link_id)
            except ImmichError as e:
                if e.kind not in ("not_found", "bad_request"):
                    raise
            self.ledger.revoke_share_link(link_id)
            audit("revoke_share_link", link_id=link_id)
        return {"link_id": link_id, "revoked": True}

    # ------------------------------------------------------------------ tags
    async def _all_tags(self) -> list[dict]:
        if not hasattr(self, "_tags_cache") or time.monotonic() - self._tags_cache[0] > 600:   # [0] = -inf: reload
            self._tags_cache = (time.monotonic(), await self.immich.tags())
        return self._tags_cache[1]

    async def list_tags(self, name_contains: str, page: int, page_size: int) -> dict:
        page, size = _page(page, page_size)
        needle = (name_contains or "").strip().lower()
        if len(needle) < 2:
            raise ToolFailure("bad_request", "name_contains needs at least 2 characters (libraries can have tens of thousands of tags)")
        visible = await self.policy.visible_asset_ids()
        candidates = [t for t in await self._all_tags() if needle in (t.get("value") or "").lower()][:200]
        found = []
        for t in candidates:      # only tags that are on at least one visible asset (tag names can be revealing)
            r = await self.immich.search_metadata({"tagIds": [t["id"]], "size": 200})
            n = sum(1 for x in r["items"] if x["id"] in visible)
            if n:
                found.append({"id": t["id"], "name": t.get("value") or t.get("name"), "visible_asset_count": n})
        found.sort(key=lambda t: (-t["visible_asset_count"], t["name"].lower()))
        return {"tags": found[(page - 1) * size: page * size], "total": len(found), "page": page,
                "next_page": page + 1 if page * size < len(found) else None, "scanned_candidates": len(candidates)}

    async def _tag_id(self, name: str, create: bool) -> str | None:
        name = name.strip()
        for t in await self._all_tags():
            if (t.get("value") or "") == name:
                return t["id"]
        if not create:
            return None
        t = await self.immich.create_tag(name)
        self._tags_cache = (float("-inf"), [])
        audit("create_tag", tag_id=t["id"], name=name)
        return t["id"]

    async def tag_assets(self, asset_ids: list[str], tags: list[str]) -> dict:
        await self.policy.require_assets(asset_ids)
        out = []
        for name in tags:
            tid = await self._tag_id(name, create=True)
            res = await self.immich.tag_assets(tid, asset_ids)
            out.append({"tag": name.strip(), "tag_id": tid,
                        "added": sum(1 for r in res if r.get("success")), "already": sum(1 for r in res if r.get("error") == "duplicate")})
        audit("tag_assets", asset_ids=asset_ids, tags=tags)
        return {"results": out}

    async def untag_assets(self, asset_ids: list[str], tags: list[str]) -> dict:
        await self.policy.require_assets(asset_ids)
        out = []
        for name in tags:
            tid = await self._tag_id(name, create=False)
            if not tid:
                out.append({"tag": name.strip(), "tag_id": None, "removed": 0})
                continue
            res = await self.immich.untag_assets(tid, asset_ids)
            out.append({"tag": name.strip(), "tag_id": tid, "removed": sum(1 for r in res if r.get("success"))})
        audit("untag_assets", asset_ids=asset_ids, tags=tags)
        return {"results": out}

    # ------------------------------------------------------------------ uploads
    async def get_media(self, *, data_base64, url, file, filename, mime_type) -> tuple[Media, str]:
        given = [x for x in (data_base64, url, file) if x]
        if len(given) != 1:
            raise ToolFailure("bad_request", "give exactly one of data_base64, url, or file")
        if data_base64:
            return from_base64(data_base64, filename, mime_type, self.cfg.max_base64_bytes), "base64"
        if file:
            dl = file.get("download_url") if isinstance(file, dict) else None
            if not dl:
                raise ToolFailure("bad_request", "file must carry a download_url")
            return await fetch_url(dl, max_bytes=self.cfg.max_upload_bytes, timeout=self.cfg.url_fetch_timeout,
                                   filename=filename or file.get("file_name")), "file"
        return await fetch_url(url, max_bytes=self.cfg.max_upload_bytes, allowed_hosts=self.cfg.url_fetch_allowed_hosts,
                               timeout=self.cfg.url_fetch_timeout, filename=filename), "url"

    async def store(self, media: Media, source: str, album: dict, *, taken_at: str | None = None,
                    description: str | None = None) -> dict:
        """Idempotent: upload once per content hash, then (independently) ensure album membership."""
        sha256 = hashlib.sha256(media.data).hexdigest()
        sha1 = hashlib.sha1(media.data).hexdigest()
        uid = upload_id_for(sha256)
        prior = self.ledger.get(uid)
        when = taken_at or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if prior and prior["status"] in ("created", "duplicate") and prior["asset_id"]:
            status, asset_id = "already_uploaded", prior["asset_id"]
        else:
            self.ledger.begin(uid, sha256, sha1, media.filename, media.mime, len(media.data), source)
            try:
                r = await self.immich.upload(media.data, media.filename, media.mime, device_asset_id=uid,
                                             created_at=when, sha1_hex=sha1)
            except ImmichError as e:
                self.ledger.finish(uid, "failed", error=e.message)
                raise
            status, asset_id = r["status"], r["id"]          # "created" | "duplicate"
            self.ledger.finish(uid, status, asset_id)
            audit("upload_asset", upload_id=uid, asset_id=asset_id, status=status, source=source,
                  size=len(media.data), mime=media.mime)
        album_status, album_error = "pending", None
        try:
            res = await self.immich.add_to_album(album["id"], [asset_id])
            r0 = res[0] if res else {}
            album_status = "added" if r0.get("success") else ("already_in_album" if r0.get("error") == "duplicate" else "failed")
            album_error = None if album_status != "failed" else r0.get("error")
        except ImmichError as e:
            album_status, album_error = "failed", e.message
        self.ledger.album(uid, album["id"], album_status, album_error)
        if album_status == "added":
            audit("add_assets_to_album", album_id=album["id"], asset_ids=[asset_id], via=uid)
        if description and status == "created":
            try:
                await self.immich.update_assets([asset_id], {"description": description})
            except ImmichError:
                pass
        self.policy.invalidate()
        return {"upload_id": uid, "status": status, "asset_id": asset_id, "sha256": sha256,
                "filename": media.filename, "mime_type": media.mime, "bytes": len(media.data),
                "album": {"id": album["id"], "name": album["albumName"], "status": album_status, "error": album_error}}

    async def upload_asset(self, *, album_id, album_name, data_base64, url, file, filename, mime_type,
                           taken_at, description) -> dict:
        album = await self._resolve_album(album_id, album_name, create=True)
        media, source = await self.get_media(data_base64=data_base64, url=url, file=file, filename=filename,
                                             mime_type=mime_type)
        return await self.store(media, source, album, taken_at=taken_at, description=description)

    async def get_upload_status(self, upload_id: str | None, sha256: str | None) -> dict:
        if not upload_id and not sha256:
            raise ToolFailure("bad_request", "give upload_id or sha256")
        uid = upload_id or upload_id_for(sha256.lower())
        row = self.ledger.get(uid)
        if not row:
            raise ToolFailure("not_found", f"no upload {uid} recorded")
        return {"upload_id": uid, "status": row["status"], "asset_id": row["asset_id"], "sha256": row["sha256"],
                "filename": row["filename"], "mime_type": row["mime"], "bytes": row["size"], "error": row["error"],
                "albums": row["albums"]}
