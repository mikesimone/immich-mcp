#!/usr/bin/env python3
"""Smoke test against a running immich-mcp over Streamable HTTP.

  scripts/smoke-test.py [--url http://127.0.0.1:8099/mcp] [--token-file FILE | env IMMICH_MCP_TOKEN] [--write]

Read-only by default: initialize, list tools, server info, list albums, search, view one image, and confirm
an unauthenticated request is refused. --write (needs --album, an album name your allow-list permits) also uploads a generated PNG there,
re-uploads it (must be idempotent), then moves it to the trash.
"""
import argparse, asyncio, base64, json, os, struct, sys, time, zlib

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def tiny_png(seed: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    w = h = 16
    raw = b"".join(b"\x00" + bytes([(seed >> 16) & 255, (seed >> 8) & 255, seed & 255]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def ok(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  ({detail})" if detail else ""))
    if not cond:
        ok.failed = True
ok.failed = False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099/mcp")
    ap.add_argument("--token-file")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--album", help="album name for --write (must match ALLOWED_ALBUM_PREFIXES)")
    a = ap.parse_args()
    if a.write and not a.album:
        ap.error("--write needs --album")
    tok = os.environ.get("IMMICH_MCP_TOKEN") or (open(a.token_file).read().strip().split(":", 1)[-1] if a.token_file else "")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(a.url, json={})
        ok("unauthenticated request refused", r.status_code == 401, f"HTTP {r.status_code}")
    async with httpx.AsyncClient(headers={"X-Immich-MCP-Token": tok}, timeout=180) as hc:
        async with streamable_http_client(a.url, http_client=hc) as (rd, wr, *_):
            async with ClientSession(rd, wr) as s:
                init = await s.initialize()
                ok("initialize", init.server_info.name == "immich", init.server_info.name)
                tools = {t.name for t in (await s.list_tools()).tools}
                ok("tool discovery", len(tools) >= 28, f"{len(tools)} tools")
                info = (await s.call_tool("get_server_info", {})).structured_content
                ok("server info", bool(info and info.get("immich_version")), json.dumps(info)[:160])
                albums = (await s.call_tool("list_albums", {"page_size": 5})).structured_content
                ok("list albums", bool(albums and albums["total"] > 0), f"{albums['total']} visible")
                hits = (await s.call_tool("search_assets", {"query": "dog", "page_size": 3})).structured_content
                ok("semantic search", bool(hits and hits["assets"]), f"{len(hits['assets'])} hits")
                if hits and hits["assets"]:
                    img = await s.call_tool("get_asset_image", {"asset_id": hits["assets"][0]["id"], "size": "thumbnail"})
                    ok("image retrieval", img.content and img.content[0].type == "image", img.content[0].type if img.content else "")
                if a.write:
                    data = base64.b64encode(tiny_png(int(time.time()) & 0xffffff)).decode()
                    args = {"album_name": a.album, "data_base64": data, "filename": "smoke-test"}
                    u1 = (await s.call_tool("upload_asset", args)).structured_content
                    ok("upload", u1 and u1["status"] in ("created", "duplicate") and u1["album"]["status"] == "added", json.dumps(u1)[:200])
                    u2 = (await s.call_tool("upload_asset", args)).structured_content
                    ok("idempotent re-upload", u2 and u2["status"] == "already_uploaded" and u2["asset_id"] == u1["asset_id"])
                    st = (await s.call_tool("get_upload_status", {"upload_id": u1["upload_id"]})).structured_content
                    ok("upload status", st and st["asset_id"] == u1["asset_id"])
                    nc = await s.call_tool("delete_assets", {"asset_ids": [u1["asset_id"]]})
                    ok("delete refused without confirm", nc.is_error)
                    d = (await s.call_tool("delete_assets", {"asset_ids": [u1["asset_id"]], "confirm": True})).structured_content
                    ok("delete to trash", d and d["trashed"] == [u1["asset_id"]])
    sys.exit(1 if ok.failed else 0)

asyncio.run(main())
