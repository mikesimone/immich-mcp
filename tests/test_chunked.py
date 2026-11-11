"""Chunked uploads for files bigger than one request."""
import base64
import hashlib

import pytest

from immich_mcp.auth import principal
from immich_mcp.service import ToolFailure

from .fakes import png
from .test_service import run


def pieces(data, n):
    step = -(-len(data) // n)
    return [data[i:i + step] for i in range(0, len(data), step)]


async def begin(svc, fake, data, **kw):
    return await run(svc, svc.begin_upload(filename="clip", total_bytes=len(data), mime_type=None,
                                           sha256=kw.get("sha256"), album_id=fake.memes, album_name=None,
                                           taken_at=None, description=None))


async def test_chunked_happy_path_with_retry(svc, fake):
    data = png(901) * 3
    s = await begin(svc, fake, data, sha256=hashlib.sha256(data).hexdigest())
    parts = pieces(data, 3)
    for i, p in enumerate(parts):
        await run(svc, svc.upload_chunk(s["upload_session"], i, base64.b64encode(p).decode()))
    r = await run(svc, svc.upload_chunk(s["upload_session"], 1, base64.b64encode(parts[1]).decode()))   # resend: harmless
    assert r["received_bytes"] == len(data) and r["chunks_received"] == [0, 1, 2]
    out = await run(svc, svc.finish_upload(s["upload_session"]))
    assert out["status"] == "created" and out["bytes"] == len(data) and out["album"]["status"] == "added"
    assert not svc._chunk_dir(s["upload_session"]).exists()
    with pytest.raises(ToolFailure) as e:                    # session is gone after finishing
        await run(svc, svc.finish_upload(s["upload_session"]))
    assert e.value.kind == "not_found"


async def test_missing_piece_and_wrong_checksum(svc, fake):
    data = png(902) * 2
    s = await begin(svc, fake, data, sha256="0" * 64)
    a, b = pieces(data, 2)
    await run(svc, svc.upload_chunk(s["upload_session"], 0, base64.b64encode(a).decode()))
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.finish_upload(s["upload_session"]))
    assert "missing" in e.value.message
    await run(svc, svc.upload_chunk(s["upload_session"], 1, base64.b64encode(b).decode()))
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.finish_upload(s["upload_session"]))
    assert "sha256" in e.value.message


async def test_overflow_and_limits(svc, fake):
    data = png(903)
    s = await begin(svc, fake, data)
    with pytest.raises(ToolFailure):
        await run(svc, svc.upload_chunk(s["upload_session"], 0, base64.b64encode(data + b"extra").decode()))
    svc.cfg.max_upload_bytes = 10
    with pytest.raises(ToolFailure) as e:
        await begin(svc, fake, data)
    assert e.value.kind == "too_large"


async def test_other_client_cannot_use_session(svc, fake):
    data = png(904)
    s = await begin(svc, fake, data)
    tok = principal.set("someone-else")
    try:
        with pytest.raises(ToolFailure) as e:
            await run(svc, svc.upload_chunk(s["upload_session"], 0, base64.b64encode(data).decode()))
        assert e.value.kind == "not_found"
    finally:
        principal.reset(tok)


async def test_expired_sessions_are_cleaned(svc, fake, monkeypatch):
    data = png(905)
    s = await begin(svc, fake, data)
    svc.CHUNK_TTL = -1
    await begin(svc, fake, data)                             # begin() sweeps expired sessions
    assert not svc._chunk_dir(s["upload_session"]).exists()
    with pytest.raises(ToolFailure):
        await run(svc, svc.finish_upload(s["upload_session"]))


async def test_cannot_target_hidden_album(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.begin_upload(filename="x", total_bytes=10, mime_type=None, sha256=None,
                                        album_id=fake.private, album_name=None, taken_at=None, description=None))
    assert e.value.kind == "not_found"
