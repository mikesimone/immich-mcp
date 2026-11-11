"""Exclude list overrides the allow list (firewall-style), and optional visibility of assets in no album."""
import pytest

from immich_mcp.service import ToolFailure

from .test_service import run


def search(svc, **kw):
    base = dict(query=None, filename=None, description=None, asset_type=None, taken_after=None, taken_before=None,
                album_id=None, is_favorite=None, page=1, page_size=50)
    return svc.search_assets(**{**base, **kw})


async def test_excluded_album_hides_its_assets_even_inside_allowed_albums(svc, fake):
    nsfw = fake.add_album("Family - NSFW")                  # allowed prefix, but excluded below
    both = fake.add_asset(fake.memes, "both.png")
    fake.members[nsfw].add(both)                            # in an allowed album AND an excluded one
    svc.cfg.excluded_album_prefixes = ("Family - NSFW",)
    svc.policy.invalidate()
    names = [a["name"] for a in (await run(svc, svc.list_albums(None, 1, 50)))["albums"]]
    assert "Family - NSFW" not in names and "Family - Memes" in names
    ids = {a["id"] for a in (await run(svc, search(svc, filename=".png")))["assets"]}
    assert both not in ids and fake.visible_asset in ids
    in_album = {a["id"] for a in (await run(svc, svc.get_album(fake.memes, 1, 50)))["assets"]}
    assert both not in in_album
    album_search = {a["id"] for a in (await run(svc, search(svc, album_id=fake.memes, filename=".png")))["assets"]}
    assert both not in album_search
    for coro in (svc.get_asset(both), svc.get_album(nsfw, 1, 10)):
        with pytest.raises(ToolFailure) as e:
            await run(svc, coro)
        assert e.value.kind == "not_found"


async def test_exclude_wins_over_wildcard(svc, fake):
    svc.cfg.allowed_album_prefixes = ("*",)
    svc.cfg.excluded_album_prefixes = ("Private",)
    svc.policy.invalidate()
    names = [a["name"] for a in (await run(svc, svc.list_albums(None, 1, 50)))["albums"]]
    assert names == ["Family - Memes"]
    with pytest.raises(ToolFailure):
        await run(svc, svc.get_asset(fake.hidden_asset))


async def test_cannot_create_or_rename_into_excluded(svc, fake):
    svc.cfg.excluded_album_prefixes = ("Family - Secret",)
    with pytest.raises(ToolFailure) as e:
        await run(svc, svc.create_album("Family - Secret stash", ""))
    assert e.value.kind == "permission"
    with pytest.raises(ToolFailure):
        await run(svc, svc.update_album(fake.memes, "Family - Secret now", None))


async def test_assets_without_album_optional(svc, fake):
    loose = fake.add_asset(None, "loose.png")
    assert loose not in await svc.policy.visible_asset_ids()
    with pytest.raises(ToolFailure):
        await run(svc, svc.get_asset(loose))
    svc.cfg.include_assets_without_album = True
    svc.policy.invalidate()
    assert loose in await svc.policy.visible_asset_ids()
    assert (await run(svc, svc.get_asset(loose)))["id"] == loose
    assert fake.hidden_asset not in await svc.policy.visible_asset_ids()   # still in a non-allowed album


async def test_people_respect_excludes(svc, fake):
    nsfw = fake.add_album("Family - NSFW")
    pic = fake.add_asset(nsfw, "x.png")
    p = fake.add_person("Only In Excluded", pic)
    svc.cfg.excluded_album_prefixes = ("Family - NSFW",)
    svc.policy.invalidate()
    assert p not in {x["id"] for x in (await run(svc, svc.list_people(None, True, 1, 50)))["people"]}
