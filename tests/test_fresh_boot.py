"""Regression: caches must not treat "never loaded" as fresh when monotonic() is small (a just-booted host)."""
import time

from .test_service import run


async def test_visible_set_on_fresh_boot(svc, fake, monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 5.0)      # five seconds after boot
    r = await run(svc, svc.list_albums(None, 1, 50))
    assert r["total"] == 1
    assert fake.visible_asset in await svc.policy.visible_asset_ids()
