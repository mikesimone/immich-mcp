"""End-to-end over real Streamable HTTP: MCP initialization, tool discovery, auth, tool calls, image content."""
import asyncio
import base64
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from immich_mcp.__main__ import make_app

from .conftest import make_service
from .fakes import png

TOKEN = "t" * 40


@pytest.fixture
def server(cfg, fake, tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    (tmp_path / "tokens").write_text(f"test-client:{TOKEN}\n# comment\nshort:abc\n")
    cfg.auth_mode, cfg.token_file = "token", str(tmp_path / "tokens")
    cfg.allowed_hosts = (f"127.0.0.1:{port}",)
    app = make_app(cfg, make_service(cfg, fake))
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True; t.join(5)


async def session_call(base, headers, fn):
    async with httpx.AsyncClient(headers=headers, timeout=30) as hc:
        async with streamable_http_client(base + "/mcp", http_client=hc) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)


async def test_auth_failures(server):
    async with httpx.AsyncClient() as c:
        assert (await c.post(server + "/mcp", json={})).status_code == 401
        assert (await c.post(server + "/mcp", json={}, headers={"X-Immich-MCP-Token": "wrong" * 10})).status_code == 401
        assert (await c.post(server + "/mcp", json={}, headers={"X-Immich-MCP-Token": "abc"})).status_code == 401  # too-short token ignored
        assert (await c.get(server + "/healthz")).json() == {"ok": True}


async def test_wrong_host_header_rejected(server):
    async with httpx.AsyncClient() as c:
        r = await c.post(server + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         headers={"X-Immich-MCP-Token": TOKEN, "Host": "evil.example.com",
                                  "Accept": "application/json, text/event-stream"})
        assert r.status_code in (400, 403, 421)


async def test_initialize_and_discover_tools(server):
    async def fn(s):
        return (await s.list_tools()).tools
    tools = await session_call(server, {"X-Immich-MCP-Token": TOKEN}, fn)
    by = {t.name: t for t in tools}
    assert set(by) >= {"get_server_info", "search_assets", "get_asset", "get_asset_image", "list_albums", "get_album",
                       "upload_asset", "create_album", "add_assets_to_album", "remove_assets_from_album",
                       "get_upload_status", "update_asset_metadata", "get_duplicates", "delete_assets"}
    assert by["delete_assets"].annotations.destructive_hint is True
    assert by["search_assets"].annotations.read_only_hint is True
    assert by["upload_asset"].meta["openai/fileParams"] == ["file"]
    assert by["upload_assets"].meta["openai/fileParams"] == ["files"]
    assert by["revoke_share_link"].annotations.destructive_hint is True
    assert by["upload_asset"].output_schema and by["search_assets"].input_schema["properties"]["query"]["description"]


async def test_bearer_header_also_accepted_and_upload_roundtrip(server, fake):
    async def fn(s):
        up = await s.call_tool("upload_asset", {"album_id": fake.memes, "data_base64": base64.b64encode(png(77)).decode(),
                                                "filename": "chatgpt"})
        img = await s.call_tool("get_asset_image", {"asset_id": up.structured_content["asset_id"], "size": "thumbnail"})
        bad = await s.call_tool("get_asset", {"asset_id": fake.hidden_asset})
        return up, img, bad
    up, img, bad = await session_call(server, {"Authorization": f"Bearer {TOKEN}"}, fn)
    assert up.structured_content["status"] == "created" and up.structured_content["album"]["status"] == "added"
    assert img.content[0].type == "image" and img.content[0].mime_type == "image/jpeg"
    assert bad.is_error and bad.content[0].text.startswith("[not_found]")


async def test_destructive_requires_confirm_over_mcp(server, fake):
    async def fn(s):
        return await s.call_tool("delete_assets", {"asset_ids": [fake.visible_asset]})
    r = await session_call(server, {"X-Immich-MCP-Token": TOKEN}, fn)
    assert r.is_error and "[confirmation_required]" in r.content[0].text
    assert fake.visible_asset not in fake.trashed
