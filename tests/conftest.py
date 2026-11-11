import pytest

from immich_mcp.config import Config
from immich_mcp.immich import Immich
from immich_mcp.ledger import Ledger
from immich_mcp.service import Service

from .fakes import FakeImmich


@pytest.fixture
def fake():
    f = FakeImmich()
    f.memes = f.add_album("Family - Memes")
    f.private = f.add_album("Private - Stuff")
    f.visible_asset = f.add_asset(f.memes, "meme.png")
    f.hidden_asset = f.add_asset(f.private, "secret.png")
    return f


@pytest.fixture
def cfg(tmp_path):
    c = Config(immich_api_key="test-key", state_dir=str(tmp_path), auth_mode="none",
               allowed_album_prefixes=("Family - ",))
    return c


def make_service(cfg, fake):
    imm = Immich("http://immich.test", "test-key", transport=fake.transport(), backoff=0.001)
    return Service(cfg, imm, Ledger(f"{cfg.state_dir}/uploads.db"))


@pytest.fixture
def svc(cfg, fake):
    return make_service(cfg, fake)
