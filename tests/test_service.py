import base64

import pytest

from immich_mcp.service import ToolFailure

from .conftest import make_service
from .fakes import png


def b64(seed):
    return base64.b64encode(png(seed)).decode()


async def run(svc, coro):
    return await svc._wrap(coro)


# ---------------------------------------------------------------- uploads
async def test_upload_creates_and_adds_to_album(svc, fake):
    r = await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(1), url=None, file=None,
                                        filename="x", mime_type=None, taken_at=None, description="hi"))
    assert r["status"] == "created" and r["album"]["status"] == "added"
    assert r["asset_id"] in fake.members[fake.memes]
    assert r["upload_id"].startswith("up_") and len(r["sha256"]) == 64
    assert r["filename"] == "x.png" and r["mime_type"] == "image/png"
    assert fake.assets[r["asset_id"]]["exifInfo"]["description"] == "hi"


async def test_retry_is_idempotent_and_does_not_reupload(svc, fake):
    args = dict(album_id=fake.memes, album_name=None, data_base64=b64(2), url=None, file=None, filename="x",
                mime_type=None, taken_at=None, description=None)
    first = await run(svc, svc.upload_asset(**args))
    posts = sum(1 for c in fake.calls if c == ("POST", "/assets"))
    second = await run(svc, svc.upload_asset(**args))
    assert second["status"] == "already_uploaded" and second["asset_id"] == first["asset_id"]
    assert second["upload_id"] == first["upload_id"]
    assert second["album"]["status"] == "already_in_album"
    assert sum(1 for c in fake.calls if c == ("POST", "/assets")) == posts   # no second upload


async def test_duplicate_reported_when_immich_already_has_it(cfg, fake, tmp_path):
    a = make_service(cfg, fake)
    r1 = await a.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(3), url=None, file=None,
                              filename=None, mime_type=None, taken_at=None, description=None)
    cfg2 = type(cfg)(**{**cfg.__dict__, "state_dir": str(tmp_path / "other")})   # empty ledger
    b = make_service(cfg2, fake)
    r2 = await b.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(3), url=None, file=None,
                              filename=None, mime_type=None, taken_at=None, description=None)
    assert r2["status"] == "duplicate" and r2["asset_id"] == r1["asset_id"]


async def test_restart_recovery_uses_persisted_ledger(cfg, fake):
    a = make_service(cfg, fake)
    r1 = await a.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(4), url=None, file=None,
                              filename=None, mime_type=None, taken_at=None, description=None)
    b = make_service(cfg, fake)   # "restarted": new process objects, same state dir
    st = await b.get_upload_status(r1["upload_id"], None)
    assert st["status"] == "created" and st["asset_id"] == r1["asset_id"]
    assert st["albums"][0]["status"] == "added"
    r2 = await b.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(4), url=None, file=None,
                              filename=None, mime_type=None, taken_at=None, description=None)
    assert r2["status"] == "already_uploaded"


async def test_album_failure_is_independent_of_upload(svc, fake):
    fake.fail[("PUT", r"/albums/.+/assets")] = [500, 500, 500, 500]   # exhaust retries
    r = await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(5), url=None, file=None,
                                        filename=None, mime_type=None, taken_at=None, description=None))
    assert r["status"] == "created" and r["album"]["status"] == "failed"
    st = await svc.get_upload_status(r["upload_id"], None)
    assert st["status"] == "created" and st["albums"][0]["status"] == "failed"
    r2 = await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(5), url=None, file=None,
                                         filename=None, mime_type=None, taken_at=None, description=None))
    assert r2["status"] == "already_uploaded" and r2["album"]["status"] == "added"


async def test_transient_errors_are_retried(svc, fake):
    fake.fail[("POST", "/assets")] = [503, 502]
    r = await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(6), url=None, file=None,
                                        filename=None, mime_type=None, taken_at=None, description=None))
    assert r["status"] == "created"


async def test_upload_failure_is_not_reported_as_success(svc, fake):
    fake.fail[("POST", "/assets")] = [500] * 10
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(7), url=None, file=None,
                                        filename=None, mime_type=None, taken_at=None, description=None))
    assert e.value.kind == "server"
    rows = svc.ledger._db.execute("SELECT status FROM uploads").fetchall()
    assert [r[0] for r in rows] == ["failed"]


async def test_auth_error_from_immich_is_classified(cfg, fake):
    from immich_mcp.immich import Immich
    from immich_mcp.ledger import Ledger
    from immich_mcp.service import Service
    bad = Service(cfg, Immich("http://immich.test", "wrong", transport=fake.transport()), Ledger(f"{cfg.state_dir}/x.db"))
    with pytest.raises(ToolFailure) as e:
        await bad._wrap(bad.server_info())
    assert e.value.kind == "auth"


async def test_unsupported_media_and_bad_base64(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None,
                                        data_base64=base64.b64encode(b"%PDF-1.7 not an image").decode(), url=None,
                                        file=None, filename=None, mime_type=None, taken_at=None, description=None))
    assert e.value.kind == "unsupported_media"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64="@@not base64@@", url=None,
                                        file=None, filename=None, mime_type=None, taken_at=None, description=None))
    assert e.value.kind == "bad_request"


async def test_base64_size_limit(svc, fake):
    svc.cfg.max_base64_bytes = 1000
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(8), url=None, file=None,
                                        filename=None, mime_type=None, taken_at=None, description=None))
    assert e.value.kind == "too_large"


async def test_exactly_one_source(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_asset(album_id=fake.memes, album_name=None, data_base64=b64(9), url="https://x.example/a.png",
                                        file=None, filename=None, mime_type=None, taken_at=None, description=None))
    assert e.value.kind == "bad_request"


# ---------------------------------------------------------------- albums
async def test_create_album_prefix_enforced_and_idempotent(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.create_album("Random album", ""))
    assert e.value.kind == "permission"
    r1 = await run(svc, svc.create_album("Family - Test 99 New", ""))
    r2 = await run(svc, svc.create_album("Family - Test 99 New", ""))
    assert r1["created"] and not r2["created"] and r1["album"]["id"] == r2["album"]["id"]


async def test_upload_into_new_album_by_name(svc, fake):
    r = await run(svc, svc.upload_asset(album_id=None, album_name="Family - Fresh", data_base64=b64(10), url=None,
                                        file=None, filename=None, mime_type=None, taken_at=None, description=None))
    assert r["album"]["name"] == "Family - Fresh" and r["album"]["status"] == "added"


async def test_add_and_remove_assets(svc, fake):
    other = await run(svc, svc.create_album("Family - Other", ""))
    aid = other["album"]["id"]
    r = await run(svc, svc.add_assets_to_album(aid, [fake.visible_asset]))
    assert r["results"][0]["status"] == "added"
    r = await run(svc, svc.add_assets_to_album(aid, [fake.visible_asset]))
    assert r["results"][0]["status"] == "already_in_album"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.remove_assets_from_album(aid, [fake.visible_asset], confirm=False))
    assert e.value.kind == "confirmation_required"
    r = await run(svc, svc.remove_assets_from_album(aid, [fake.visible_asset], confirm=True))
    assert r["results"][0]["status"] == "removed" and fake.visible_asset in fake.assets


# ---------------------------------------------------------------- visibility / permissions
async def test_hidden_album_and_asset_are_invisible(svc, fake):
    albums = await run(svc, svc.list_albums(None, 1, 100))
    assert [a["name"] for a in albums["albums"]] == ["Family - Memes"]
    for coro in (svc.get_asset(fake.hidden_asset), svc.get_album(fake.private, 1, 10),
                 svc.get_asset_image(fake.hidden_asset, "preview")):
        with pytest.raises(ToolFailure) as e:
            await run(svc, coro)
        assert e.value.kind == "not_found"


async def test_search_excludes_hidden_assets(svc, fake):
    r = await run(svc, svc.search_assets(query=None, filename=".png", description=None, asset_type=None, taken_after=None,
                                         taken_before=None, album_id=None, is_favorite=None, page=1, page_size=25))
    ids = {a["id"] for a in r["assets"]}
    assert fake.visible_asset in ids and fake.hidden_asset not in ids
    with pytest.raises(ToolFailure):
        await run(svc, svc.search_assets(query="x", filename=None, description=None, asset_type=None, taken_after=None,
                                         taken_before=None, album_id=fake.private, is_favorite=None, page=1, page_size=5))


async def test_cannot_pull_hidden_asset_into_visible_album(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.add_assets_to_album(fake.memes, [fake.hidden_asset]))
    assert e.value.kind == "not_found"
    assert fake.hidden_asset not in fake.members[fake.memes]


async def test_delete_is_trash_only_with_confirmation_and_visible_only(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.delete_assets([fake.visible_asset], confirm=False))
    assert e.value.kind == "confirmation_required"
    with pytest.raises(ToolFailure):
        await run(svc, svc.delete_assets([fake.hidden_asset], confirm=True))
    assert fake.hidden_asset not in fake.trashed
    r = await run(svc, svc.delete_assets([fake.visible_asset], confirm=True))
    assert r["trashed"] == [fake.visible_asset] and fake.visible_asset in fake.trashed   # fake asserts force=False


async def test_update_metadata_visible_only(svc, fake):
    await run(svc, svc.update_asset_metadata([fake.visible_asset], None, True, None, None))
    assert fake.assets[fake.visible_asset]["isFavorite"] is True
    with pytest.raises(ToolFailure):
        await run(svc, svc.update_asset_metadata([fake.hidden_asset], "x", None, None, None))


async def test_image_retrieval(svc, fake):
    data, mime, meta = await run(svc, svc.get_asset_image(fake.visible_asset, "preview"))
    assert mime == "image/jpeg" and data.startswith(b"\xff\xd8") and meta["bytes"] == len(data)


# ---------------------------------------------------------------- rename
async def test_rename_album(svc, fake):
    r = await run(svc, svc.update_album(fake.memes, "Family - Test 50 Renamed", "new desc"))
    assert r["old_name"] == "Family - Memes" and r["album"]["name"] == "Family - Test 50 Renamed"
    assert fake.albums[fake.memes]["albumName"] == "Family - Test 50 Renamed"
    assert fake.albums[fake.memes]["description"] == "new desc"
    r = await run(svc, svc.update_album(fake.memes, "Family - Test 50 Renamed", None))   # idempotent, keeps description
    assert fake.albums[fake.memes]["description"] == "new desc"


async def test_rename_album_rules(svc, fake):
    with pytest.raises(ToolFailure) as e:                    # would vanish from the allow-list
        await run(svc, svc.update_album(fake.memes, "Not allowed", None))
    assert e.value.kind == "permission"
    with pytest.raises(ToolFailure) as e:                    # hidden album can't be renamed (or revealed)
        await run(svc, svc.update_album(fake.private, "Family - Test 51 Revealed", None))
    assert e.value.kind == "not_found"
    assert fake.albums[fake.private]["albumName"].startswith("Private")
    other = await run(svc, svc.create_album("Family - Test 52 Taken", ""))
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.update_album(fake.memes, "Family - Test 52 Taken", None))
    assert e.value.kind == "bad_request"


async def test_update_album_description_only(svc, fake):
    r = await run(svc, svc.update_album(fake.memes, None, "just the description"))
    assert r["album"]["name"] == "Family - Memes"
    assert fake.albums[fake.memes]["description"] == "just the description"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.update_album(fake.memes, None, None))
    assert e.value.kind == "bad_request"


# ---------------------------------------------------------------- captions
SEP = "\n\n--- generation info ---\n"


async def test_set_caption_replace_append_and_keep_gen_info(svc, fake):
    a = fake.visible_asset
    fake.assets[a]["exifInfo"]["description"] = "A raccoon holding a sign." + SEP + '{"seed": 42}'
    r = await run(svc, svc.set_caption([a], "Favorite", "append"))
    assert r["results"][0]["kept_generation_info"] is True
    assert fake.assets[a]["exifInfo"]["description"] == "A raccoon holding a sign.\n\nFavorite" + SEP + '{"seed": 42}'
    await run(svc, svc.set_caption([a], "Replaced", "replace"))
    assert fake.assets[a]["exifInfo"]["description"] == "Replaced" + SEP + '{"seed": 42}'
    await run(svc, svc.set_caption([a], "", "replace"))
    assert fake.assets[a]["exifInfo"]["description"] == SEP + '{"seed": 42}'


async def test_set_caption_plain_and_hidden(svc, fake):
    a = fake.visible_asset
    r = await run(svc, svc.set_caption([a], "first", "append"))          # append to nothing = set
    assert fake.assets[a]["exifInfo"]["description"] == "first" and r["results"][0]["changed"]
    r = await run(svc, svc.set_caption([a], "first", "replace"))
    assert r["results"][0]["changed"] is False
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.set_caption([fake.hidden_asset], "nope", "replace"))
    assert e.value.kind == "not_found"


async def test_update_metadata_description_keeps_gen_info(svc, fake):
    a = fake.visible_asset
    fake.assets[a]["exifInfo"]["description"] = "old" + SEP + "{}"
    await run(svc, svc.update_asset_metadata([a], "new", True, None, None))
    assert fake.assets[a]["exifInfo"]["description"] == "new" + SEP + "{}"
    assert fake.assets[a]["isFavorite"] is True
