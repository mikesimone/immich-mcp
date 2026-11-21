"""file_event_pictures: owner's camera shots during an event -> next numbered album, add only."""
import json

import pytest

from immich_mcp.config import Config
from immich_mcp.events import EventError, parse_rules, pick_album, window
from immich_mcp.server import build
from immich_mcp.service import ToolFailure

from .conftest import make_service

RULES = json.dumps([
    {"device": "phone", "exif_make": "samsung", "exif_model_prefix": "SM-F966U"},
    {"device": "glasses", "exif_make": "Meta"},
    {"device": "spycam", "filename_contains": "SpyPhoto"},
    {"device": "spycam", "filename_prefix": "SpyVideo"},
])
START, END = "2026-11-13T19:30:00-08:00", "2026-11-13T20:30:00-08:00"


async def run(svc, coro):
    return await svc._wrap(coro)


def shot(fake, album, name, local, make=None, model=None, vis="timeline", utc=None):
    i = fake.add_asset(album, name)
    fake.assets[i].update(localDateTime=local, fileCreatedAt=utc or local, visibility=vis,
                          exifInfo={"make": make, "model": model})
    return i


@pytest.fixture
def ecfg(tmp_path):
    return Config(immich_api_key="test-key", state_dir=str(tmp_path), auth_mode="none",
                  allowed_album_prefixes=("010.", "Family - "), excluded_album_prefixes=("999.",),
                  event_camera_rules_json=RULES, event_album_prefix="010.", event_timezone="America/Los_Angeles",
                  event_number_min=100, event_number_max=199)


@pytest.fixture
def scene(fake):
    camspy = fake.add_album("400.001 - Camspy")            # hidden album
    fake.add_album("010.021 - Old event block")
    fake.add_album("010.119 - Kill Tony November 2026")
    fake.add_album("010.200 - Anchorage")
    fake.add_album("010.301 - Airport")
    fake.add_album("010.500.001 - nested, ignored")
    secret = fake.add_album("999.000 - Never")
    s = {
        # Pacific shots stored with zone: wall clock 19:45 local, 03:45Z
        "phone": shot(fake, None, "20261113_194500.jpg", "2026-11-13T19:45:00.000Z", "samsung", "SM-F966U1",
                      utc="2026-11-14T03:45:00.000Z"),
        # glasses with no zone: Immich stores wall clock as UTC; must still match
        "glasses": shot(fake, camspy, "photo.jpg", "2026-11-13T19:50:00.000Z", "Meta", "Ray-Ban Meta Smart Glasses 2",
                        vis="archive"),
        "spy": shot(fake, camspy, "SpyVideo_1.mp4", "2026-11-13T20:59:00.000Z", vis="archive"),
        "late": shot(fake, None, "x.jpg", "2026-11-13T21:01:00.000Z", "samsung", "SM-F966U"),   # past end+pad
        "other_cam": shot(fake, None, "y.jpg", "2026-11-13T20:00:00.000Z", "Apple", "iPhone"),
        "tumblr": shot(fake, None, "tumblr_abc.jpg", "2026-11-13T20:00:00.000Z"),
        "denied": shot(fake, secret, "SpyPhoto_9.jpg", "2026-11-13T20:00:00.000Z"),
        "locked": shot(fake, None, "SpyPhoto_l.jpg", "2026-11-13T20:00:00.000Z", vis="locked"),
    }
    s["camspy"] = camspy
    return s


async def test_dry_run_changes_nothing(ecfg, fake, scene):
    svc = make_service(ecfg, fake)
    before = {k: set(v) for k, v in fake.members.items()}
    r = await run(svc, svc.file_event_pictures(title="Gary Numan", start=START, end=END, apply=False,
                                               timezone_name=None, pad_minutes=None))
    assert r["album_name"] == "010.120 - Gary Numan" and not r["album_existed"] and not r["applied"]
    assert r["existing_candidates"] == []
    assert r["matched"] == 3 and r["by_device"] == {"phone": 1, "glasses": 1, "spycam": 1}
    assert r["skipped_excluded"] == 1 and r["asset_ids"] == []
    assert {k: set(v) for k, v in fake.members.items()} == before


async def test_apply_adds_only_and_rerun_reuses(ecfg, fake, scene):
    svc = make_service(ecfg, fake)
    r = await run(svc, svc.file_event_pictures(title="Gary Numan", start=START, end=END, apply=True,
                                               timezone_name=None, pad_minutes=None))
    assert r["applied"] and r["added"] == 3 and r["verified_in_album"] == 3 and r["failed"] == 0
    assert set(r["asset_ids"]) == {scene["phone"], scene["glasses"], scene["spy"]}
    assert fake.members[r["album_id"]] == set(r["asset_ids"])
    assert {scene["glasses"], scene["spy"]} <= fake.members[scene["camspy"]]       # still in Camspy
    assert fake.assets[scene["glasses"]]["visibility"] == "archive"                 # archive untouched
    # now visible through the normal tools
    got = await run(svc, svc.get_album(r["album_id"], 1, 50))
    assert len(got["assets"]) == 3
    r2 = await run(svc, svc.file_event_pictures(title="gary numan", start=START, end=END, apply=True,
                                                timezone_name=None, pad_minutes=None))
    assert r2["album_existed"] and r2["album_id"] == r["album_id"] and r2["added"] == 0 and r2["already_in_album"] == 3


async def test_all_day_and_pad(ecfg, fake, scene):
    svc = make_service(ecfg, fake)
    r = await run(svc, svc.file_event_pictures(title="Day", start="2026-11-13", end="2026-11-14", apply=False,
                                               timezone_name=None, pad_minutes=None))
    assert r["all_day"] and r["matched"] == 4                                       # 'late' included
    r = await run(svc, svc.file_event_pictures(title="Tight", start=START, end=END, apply=False,
                                               timezone_name=None, pad_minutes=0))
    assert r["matched"] == 2                                                        # spy at 20:59 drops out


async def test_disabled_and_bad_input(cfg, ecfg, fake, scene):
    svc = make_service(cfg, fake)
    with pytest.raises(ToolFailure, match="not_configured"):
        await run(svc, svc.file_event_pictures(title="x", start=START, end=END, apply=False, timezone_name=None, pad_minutes=None))
    svc = make_service(ecfg, fake)
    with pytest.raises(ToolFailure, match="bad_request"):
        await run(svc, svc.file_event_pictures(title="x", start="2026-11-13", end=END, apply=False, timezone_name=None, pad_minutes=None))
    with pytest.raises(ToolFailure, match="bad_request"):
        await run(svc, svc.file_event_pictures(title="x", start=START, end=END, apply=False, timezone_name="Mars/Base", pad_minutes=None))


async def test_tool_registered_only_when_enabled(cfg, ecfg, fake):
    on = {t.name for t in await build(make_service(ecfg, fake)).list_tools()}
    off = {t.name for t in await build(make_service(cfg, fake)).list_tools()}
    assert "file_event_pictures" in on and "file_event_pictures" not in off


def test_pure_helpers():
    with pytest.raises(EventError):
        parse_rules('[{"device": "x"}]')
    with pytest.raises(EventError):
        parse_rules('[{"device": "x", "exif_maker": "y"}]')
    a, name = pick_album([{"albumName": "010.009 - Gary Numan", "id": "1"}], "010.", "Gary Numan")
    assert a["id"] == "1" and name == "010.009 - Gary Numan"
    assert pick_album([], "010.", "New")[1] == "010.001 - New"
    from zoneinfo import ZoneInfo
    s, e, day = window("2026-11-13T19:30:00-08:00", "2026-11-13T20:30:00-08:00", ZoneInfo("America/Los_Angeles"), 30)
    assert (s.hour, s.minute, e.hour, e.minute, day) == (19, 0, 21, 0, False)


def test_config_problems(ecfg):
    assert ecfg.problems() == []
    ecfg.event_album_prefix = "400."
    assert any("EVENT_ALBUM_PREFIX" in p for p in ecfg.problems())


async def test_existing_album_for_event_is_found_not_duplicated(ecfg, fake, scene):
    old = fake.add_album("010.118 - Gary Numan - November 2026")
    fake.members[old].add(scene["phone"])
    svc = make_service(ecfg, fake)
    r = await run(svc, svc.file_event_pictures(title="Gary Numan North American Fall Tour 2026", start=START, end=END,
                                               apply=False, timezone_name=None, pad_minutes=None))
    assert r["album_name"] == "010.120 - Gary Numan North American Fall Tour 2026"
    assert r["existing_candidates"] == [{"album_id": old, "album_name": "010.118 - Gary Numan - November 2026",
                                         "assets_in_window": 1, "matched_already_in": 1}]
    n_albums = len(fake.albums)
    with pytest.raises(ToolFailure, match="possible_duplicate"):
        await run(svc, svc.file_event_pictures(title="Gary Numan North American Fall Tour 2026", start=START, end=END,
                                               apply=True, timezone_name=None, pad_minutes=None))
    assert len(fake.albums) == n_albums
    r = await run(svc, svc.file_event_pictures(title="Gary Numan North American Fall Tour 2026", start=START, end=END,
                                               apply=True, timezone_name=None, pad_minutes=None, album_id=old))
    assert r["album_id"] == old and r["added"] == 2 and r["already_in_album"] == 1 and r["verified_in_album"] == 3
    assert len(fake.albums) == n_albums
    r = await run(svc, svc.file_event_pictures(title="Gary Numan North American Fall Tour 2026", start=START, end=END,
                                               apply=True, timezone_name=None, pad_minutes=None, new_album=True))
    assert r["album_name"].startswith("010.120 ") and r["album_existed"] is False
    with pytest.raises(ToolFailure, match="bad_request"):
        await run(svc, svc.file_event_pictures(title="x", start=START, end=END, apply=False, timezone_name=None,
                                               pad_minutes=None, album_id=scene["camspy"]))


def test_numbering_block():
    albums = [{"albumName": n, "id": n} for n in ("010.021 - a", "010.119 - b", "010.200 - c", "010.203.001 - d", "010.301 - e")]
    assert pick_album(albums, "010.", "New", 100, 199)[1] == "010.120 - New"
    assert pick_album([{"albumName": "010.021 - a", "id": "x"}], "010.", "New", 100, 199)[1] == "010.100 - New"
    with pytest.raises(EventError):
        pick_album([{"albumName": "010.199 - z", "id": "z"}], "010.", "New", 100, 199)
