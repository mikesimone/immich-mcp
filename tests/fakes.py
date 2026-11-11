"""In-memory fakes for Immich, served through httpx.MockTransport."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

import httpx

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def png(seed: int) -> bytes:
    return PNG + seed.to_bytes(4, "big") * 1024   # >2 KB, distinct per seed


class FakeImmich:
    def __init__(self):
        self.albums: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}
        self.members: dict[str, set[str]] = {}
        self.by_sha1: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[tuple[str, str], list[int]] = {}   # (method, path-regex) -> status codes to return first
        self.trashed: set[str] = set()
        self.people: dict[str, dict] = {}
        self.tags: dict[str, dict] = {}
        self.asset_tags: dict[str, set] = {}      # tag id -> asset ids
        self.links: dict[str, dict] = {}

    # helpers for tests
    def add_album(self, name: str) -> str:
        i = str(uuid.uuid4())
        self.albums[i] = {"id": i, "albumName": name, "description": "", "assetCount": 0}
        self.members[i] = set()
        return i

    def add_asset(self, album_id: str | None = None, name="a.png") -> str:
        i = str(uuid.uuid4())
        self.assets[i] = {"id": i, "type": "IMAGE", "originalFileName": name, "originalMimeType": "image/png",
                          "fileCreatedAt": "2026-01-01T00:00:00Z", "isFavorite": False, "exifInfo": {}, "people": [],
                          "visibility": "archive", "tags": []}
        if album_id:
            self.members[album_id].add(i)
        return i

    def add_person(self, name: str, *asset_ids: str) -> str:
        i = str(uuid.uuid4())
        self.people[i] = {"id": i, "name": name}
        for a in asset_ids:
            self.assets[a]["people"].append(self.people[i])
        return i

    def add_tag(self, value: str, *asset_ids: str) -> str:
        i = str(uuid.uuid4())
        self.tags[i] = {"id": i, "name": value, "value": value}
        self.asset_tags[i] = set(asset_ids)
        return i

    def _album_out(self, a):
        return {**a, "assetCount": len(self.members[a["id"]])}

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api")
        self.calls.append((req.method, path))
        for (m, rx), codes in self.fail.items():
            if m == req.method and re.fullmatch(rx, path) and codes:
                return httpx.Response(codes.pop(0), json={"message": "injected"})
        if req.headers.get("x-api-key") != "test-key":
            return httpx.Response(401, json={"message": "Invalid API key"})
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else None
        m = req.method
        if path == "/server/version":
            return httpx.Response(200, json={"major": 3, "minor": 3, "patch": 6})
        if path == "/albums" and m == "GET":
            aid = req.url.params.get("assetId")
            out = [self._album_out(a) for a in self.albums.values() if not aid or aid in self.members[a["id"]]]
            return httpx.Response(200, json=out)
        if path == "/albums" and m == "POST":
            i = self.add_album(body["albumName"])
            return httpx.Response(201, json=self._album_out(self.albums[i]))
        mm = re.fullmatch(r"/albums/([^/]+)", path)
        if mm and m == "PATCH":
            a = self.albums[mm.group(1)]
            a.update({k: body[k] for k in ("albumName", "description") if k in body})
            return httpx.Response(200, json=self._album_out(a))
        if mm and m == "GET":
            a = self.albums.get(mm.group(1))
            return httpx.Response(200, json=self._album_out(a)) if a else httpx.Response(400, json={"message": "Not found or no album.read access"})
        mm = re.fullmatch(r"/albums/([^/]+)/assets", path)
        if mm:
            aid = mm.group(1)
            res = []
            for i in body["ids"]:
                if m == "PUT":
                    if i in self.members[aid]:
                        res.append({"id": i, "success": False, "error": "duplicate"})
                    else:
                        self.members[aid].add(i); res.append({"id": i, "success": True})
                else:
                    ok = i in self.members[aid]; self.members[aid].discard(i)
                    res.append({"id": i, "success": ok, **({} if ok else {"error": "not_found"})})
            return httpx.Response(200, json=res)
        if path in ("/search/metadata", "/search/smart"):
            items = [a for i, a in self.assets.items() if i not in self.trashed]
            for aid in body.get("albumIds", []):
                items = [a for a in items if a["id"] in self.members[aid]]
            if body.get("originalFileName"):
                items = [a for a in items if body["originalFileName"].lower() in a["originalFileName"].lower()]
            for pid in body.get("personIds", []):
                items = [a for a in items if any(p["id"] == pid for p in a["people"])]
            for tid in body.get("tagIds", []):
                items = [a for a in items if a["id"] in self.asset_tags.get(tid, set())]
            if not body.get("withPeople"):
                items = [{**a, "people": []} for a in items]
            size, page = body.get("size", 100), body.get("page", 1)
            chunk = items[(page - 1) * size: page * size]
            return httpx.Response(200, json={"assets": {"items": chunk, "total": len(items), "count": len(chunk),
                                                        "nextPage": str(page + 1) if page * size < len(items) else None}})
        mm = re.fullmatch(r"/assets/([^/]+)", path)
        if mm and m == "GET":
            a = self.assets.get(mm.group(1))
            return httpx.Response(200, json=a) if a else httpx.Response(400, json={"message": "no access"})
        mm = re.fullmatch(r"/assets/([^/]+)/thumbnail", path)
        if mm:
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"j" * 100, headers={"content-type": "image/jpeg"})
        if path == "/assets" and m == "POST":
            sha1 = req.headers.get("x-immich-checksum")
            if sha1 in self.by_sha1:
                return httpx.Response(200, json={"id": self.by_sha1[sha1], "status": "duplicate"})
            i = self.add_asset(name="upload")
            self.by_sha1[sha1] = i
            return httpx.Response(201, json={"id": i, "status": "created"})
        if path == "/assets" and m == "PUT":
            for i in body["ids"]:
                if "description" in body:
                    self.assets[i]["exifInfo"]["description"] = body["description"]
                if "isFavorite" in body:
                    self.assets[i]["isFavorite"] = body["isFavorite"]
                if "visibility" in body:
                    assert self.assets[i]["visibility"] in ("archive", "timeline"), "must never touch locked/hidden"
                    self.assets[i]["visibility"] = body["visibility"]
            return httpx.Response(204)
        mm = re.fullmatch(r"/people/([^/]+)", path)
        if mm and m == "PUT":
            self.people[mm.group(1)]["name"] = body["name"]
            return httpx.Response(200, json=self.people[mm.group(1)])
        if path == "/tags" and m == "GET":
            return httpx.Response(200, json=list(self.tags.values()))
        if path == "/tags" and m == "POST":
            i = self.add_tag(body["name"])
            return httpx.Response(201, json=self.tags[i])
        mm = re.fullmatch(r"/tags/([^/]+)/assets", path)
        if mm:
            cur = self.asset_tags.setdefault(mm.group(1), set()); res = []
            for a in body["ids"]:
                if m == "PUT":
                    res.append({"id": a, "success": a not in cur, **({} if a not in cur else {"error": "duplicate"})}); cur.add(a)
                else:
                    res.append({"id": a, "success": a in cur}); cur.discard(a)
            return httpx.Response(200, json=res)
        if path == "/shared-links" and m == "POST":
            i = str(uuid.uuid4()); self.links[i] = {**body, "id": i, "key": "k" + i[:8]}
            return httpx.Response(201, json=self.links[i])
        mm = re.fullmatch(r"/shared-links/([^/]+)", path)
        if mm and m == "DELETE":
            self.links.pop(mm.group(1), None)
            return httpx.Response(204)
        if path == "/assets" and m == "DELETE":
            assert body["force"] is False, "server must never force-delete"
            self.trashed.update(body["ids"])
            return httpx.Response(204)
        if path == "/duplicates":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": f"fake: no route {m} {path}"})

    def transport(self):
        return httpx.MockTransport(self.handler)

