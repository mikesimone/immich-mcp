"""AuthMiddleware: token mode, Cloudflare Access JWT mode, and both combined."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from immich_mcp.auth import AuthMiddleware, principal
from immich_mcp.config import Config

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-123"
TOKEN = "k" * 48


class FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        class K:
            key = KEY.public_key()
        return K()


def access_jwt(email="owner@example.com", aud=AUD, iss=f"https://{TEAM}", exp_in=300, key=KEY):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in}, key, algorithm="RS256")


async def call(mw, headers):
    seen = {}

    async def app(scope, receive, send):
        seen["who"] = principal.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw.app = app
    out = {}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]

    scope = {"type": "http", "path": "/mcp", "method": "POST", "client": ("127.0.0.1", 1),
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    await mw(scope, None, send)
    return out["status"], seen.get("who")


def mw(tmp_path, mode):
    (tmp_path / "tokens").write_text(f"claude-code:{TOKEN}\n")
    cfg = Config(auth_mode=mode, token_file=str(tmp_path / "tokens"), cf_team_domain=TEAM, cf_audience=(AUD,),
                 allowed_emails=("owner@example.com",))
    m = AuthMiddleware(None, cfg)
    if m._jwks:
        m._jwks = FakeJWKS()
    return m


async def test_cloudflare_valid(tmp_path):
    assert await call(mw(tmp_path, "cloudflare"), {"Cf-Access-Jwt-Assertion": access_jwt()}) == (200, "owner@example.com")


@pytest.mark.parametrize("bad", [
    dict(aud="someone-else"), dict(email="intruder@example.com"), dict(iss="https://evil.cloudflareaccess.com"),
    dict(exp_in=-60), dict(key=OTHER)])
async def test_cloudflare_rejects(tmp_path, bad):
    status, _ = await call(mw(tmp_path, "cloudflare"), {"Cf-Access-Jwt-Assertion": access_jwt(**bad)})
    assert status == 401


async def test_cloudflare_mode_ignores_tokens(tmp_path):
    status, _ = await call(mw(tmp_path, "cloudflare"), {"X-Immich-MCP-Token": TOKEN})
    assert status == 401


async def test_combined_accepts_either(tmp_path):
    m = mw(tmp_path, "token,cloudflare")
    assert await call(m, {"X-Immich-MCP-Token": TOKEN}) == (200, "claude-code")
    assert await call(m, {"Cf-Access-Jwt-Assertion": access_jwt()}) == (200, "owner@example.com")
    assert (await call(m, {}))[0] == 401
    assert (await call(m, {"Cf-Access-Jwt-Assertion": access_jwt(email="x@y.z")}))[0] == 401


def test_bad_mode_config():
    with pytest.raises(ValueError):
        AuthMiddleware(None, Config(auth_mode="none,token"))
    with pytest.raises(ValueError):
        AuthMiddleware(None, Config(auth_mode="cloudflare", cf_audience=()))
