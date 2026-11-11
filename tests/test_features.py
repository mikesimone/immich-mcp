"""People, archive toggle, batch upload, share links, tags."""
import base64

import pytest

from immich_mcp.service import ToolFailure

from .conftest import make_service
from .fakes import png


async def run(svc, coro):
    return await svc._wrap(coro)


def b64(seed):
    return base64.b64encode(png(seed)).decode()


# ---------------------------------------------------------------- people
async def test_list_people_only_from_visible_assets(svc, fake):
    alice = fake.add_person("Alice", fake.visible_asset)
    secret = fake.add_person("Secret Person", fake.hidden_asset)
    unnamed = fake.add_person("", fake.visible_asset)
    r = await run(svc, svc.list_people(None, True, 1, 50))
    assert [p["name"] for p in r["people"]] == ["Alice"] and r["people"][0]["photo_count"] == 1
    r = await run(svc, svc.list_people(None, False, 1, 50))
    assert {p["id"] for p in r["people"]} == {alice, unnamed}
    assert secret not in {p["id"] for p in r["people"]}


async def test_search_by_person(svc, fake):
    other = fake.add_asset(fake.memes, "other.png")
    bob = fake.add_person("Bob", other)
    fake.assets[fake.hidden_asset]["people"].append(fake.people[bob])     # Bob is also in a hidden photo
    r = await run(svc, svc.search_assets(query=None, filename=None, description=None, asset_type=None, taken_after=None,
                                         taken_before=None, album_id=None, is_favorite=None, page=1, page_size=25,
                                         person_ids=[bob]))
    assert [a["id"] for a in r["assets"]] == [other]


async def test_name_person_visible_only(svc, fake):
    p = fake.add_person("", fake.visible_asset)
    hidden = fake.add_person("Hidden", fake.hidden_asset)
    r = await run(svc, svc.name_person(p, "Carol"))
    assert r["name"] == "Carol" and fake.people[p]["name"] == "Carol"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.name_person(hidden, "Renamed"))
    assert e.value.kind == "not_found" and fake.people[hidden]["name"] == "Hidden"


async def test_get_asset_people_and_tags(svc, fake):
    fake.add_person("Dana", fake.visible_asset)
    fake.assets[fake.visible_asset]["tags"] = [{"value": "funny"}]
    r = await run(svc, svc.get_asset(fake.visible_asset))
    assert r["people"][0]["name"] == "Dana" and r["tags"] == ["funny"] and r["is_archived"] is True


# ---------------------------------------------------------------- archive
async def test_set_archived_toggles_and_skips_locked(svc, fake):
    a, locked = fake.visible_asset, fake.add_asset(fake.memes, "locked.png")
    fake.assets[locked]["visibility"] = "locked"
    r = await run(svc, svc.set_archived([a, locked], False))
    assert r["changed"] == [a] and r["skipped"][0]["asset_id"] == locked
    assert fake.assets[a]["visibility"] == "timeline" and fake.assets[locked]["visibility"] == "locked"
    r = await run(svc, svc.set_archived([a], False))
    assert r["unchanged"] == [a]
    with pytest.raises(ToolFailure):
        await run(svc, svc.set_archived([fake.hidden_asset], False))


# ---------------------------------------------------------------- batch upload
async def test_batch_upload_partial_failure(svc, fake):
    bad = base64.b64encode(b"not an image at all").decode()
    r = await run(svc, svc.upload_assets(album_id=fake.memes, album_name=None, files=None, urls=None,
                                         images=[{"data_base64": b64(31)}, {"data_base64": bad}, {"data_base64": b64(32)}],
                                         taken_at=None))
    assert r["counts"] == {"created": 2, "duplicate": 0, "already_uploaded": 0, "failed": 1}
    assert r["results"][1]["status"] == "failed" and r["results"][1]["error"].startswith("[unsupported_media]")
    again = await run(svc, svc.upload_assets(album_id=fake.memes, album_name=None, files=None, urls=None,
                                             images=[{"data_base64": b64(31)}], taken_at=None))
    assert again["counts"]["already_uploaded"] == 1


async def test_batch_upload_limits(svc, fake):
    svc.cfg.max_batch_items = 2
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_assets(album_id=fake.memes, album_name=None, files=None, urls=None,
                                         images=[{"data_base64": b64(i)} for i in range(3)], taken_at=None))
    assert e.value.kind == "too_large"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.upload_assets(album_id=fake.memes, album_name=None, files=None, urls=None, images=None,
                                         taken_at=None))
    assert e.value.kind == "bad_request"


# ---------------------------------------------------------------- share links
async def test_share_links_off_by_default(svc, fake):
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.create_share_link([fake.visible_asset], 24, True, None, True))
    assert e.value.kind == "not_configured" and not fake.links


async def test_share_link_flow(svc, fake):
    svc.cfg.share_links_enabled, svc.cfg.public_immich_url = True, "https://photos.example.com"
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.create_share_link([fake.visible_asset], 24, True, None, False))
    assert e.value.kind == "confirmation_required"
    with pytest.raises(ToolFailure):
        await run(svc, svc.create_share_link([fake.hidden_asset], 24, True, None, True))
    r = await run(svc, svc.create_share_link([fake.visible_asset], 99999, False, "for a friend", True))
    link = fake.links[r["link_id"]]
    assert r["url"].startswith("https://photos.example.com/share/") and link["showMetadata"] is False
    assert link["type"] == "INDIVIDUAL" and link["allowDownload"] is False
    assert (await run(svc, svc.revoke_share_link(r["link_id"])))["revoked"] and r["link_id"] not in fake.links
    with pytest.raises(ToolFailure) as e:                                    # not created by this server
        await run(svc, svc.revoke_share_link("someone-elses-link"))
    assert e.value.kind == "not_found"


# ---------------------------------------------------------------- tags
async def test_list_tags_needs_text_and_hides_hidden_only_tags(svc, fake):
    fake.add_tag("funny cat", fake.visible_asset)
    fake.add_tag("funny secret", fake.hidden_asset)
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.list_tags("f", 1, 50))
    assert e.value.kind == "bad_request"
    r = await run(svc, svc.list_tags("funny", 1, 50))
    assert [t["name"] for t in r["tags"]] == ["funny cat"]


async def test_tag_and_untag(svc, fake):
    r = await run(svc, svc.tag_assets([fake.visible_asset], ["favorites"]))
    tid = r["results"][0]["tag_id"]
    assert r["results"][0]["added"] == 1 and fake.visible_asset in fake.asset_tags[tid]
    r = await run(svc, svc.tag_assets([fake.visible_asset], ["favorites"]))
    assert r["results"][0]["already"] == 1 and r["results"][0]["tag_id"] == tid
    r = await run(svc, svc.untag_assets([fake.visible_asset], ["favorites", "never existed"]))
    assert r["results"][0]["removed"] == 1 and r["results"][1]["tag_id"] is None
    with pytest.raises(ToolFailure):
        await run(svc, svc.tag_assets([fake.hidden_asset], ["x"]))
