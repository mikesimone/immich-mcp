"""Authentication at the MCP boundary (pure ASGI middleware), independent of the transport in front.

Modes:
  token       Each client (tunnel-client, Claude Code, ...) has its own token, sent in the
              X-Immich-MCP-Token header (or Authorization: Bearer). Compared in constant time. The
              client's name becomes the audit identity. For the loopback listener.
  cloudflare  Cloudflare Access in front (e.g. Managed OAuth for ChatGPT/Claude.ai connectors): verify
              the Cf-Access-Jwt-Assertion header (RS256, team JWKS, audience, issuer) and that the email
              is allowed. Kept so a public Cloudflare endpoint is a config change, not a rewrite.
  none        No check. Only for stdio (the process boundary is the auth) and tests.
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
from pathlib import Path

import jwt

from .config import Config

principal: contextvars.ContextVar[str] = contextvars.ContextVar("principal", default="local")
log = logging.getLogger("immich_mcp.auth")


def load_tokens(path: str) -> dict[str, str]:
    """file of 'client-name:token' lines -> {token: name}"""
    out = {}
    p = Path(path)
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                name, tok = line.split(":", 1)
                if len(tok.strip()) >= 32:
                    out[tok.strip()] = name.strip()
    return out


class AuthMiddleware:
    def __init__(self, app, cfg: Config, *, public_paths=("/healthz",)):
        self.app, self.cfg, self.public = app, cfg, set(public_paths)
        # AUTH_MODE may list several modes ("token,cloudflare"): a request passes if ANY of them accepts it.
        self.modes = {m.strip() for m in cfg.auth_mode.split(",") if m.strip()}
        if not self.modes or self.modes - {"token", "cloudflare", "none"}:
            raise ValueError(f"unknown AUTH_MODE {cfg.auth_mode!r}")
        if "none" in self.modes and len(self.modes) > 1:
            raise ValueError("AUTH_MODE=none cannot be combined with other modes")
        if "cloudflare" in self.modes and not cfg.cf_audience:
            raise ValueError("AUTH_MODE=cloudflare needs CF_ACCESS_AUD")
        self._jwks = jwt.PyJWKClient(f"https://{cfg.cf_team_domain}/cdn-cgi/access/certs",
                                     cache_keys=True, lifespan=3600) if "cloudflare" in self.modes else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.public or self.modes == {"none"}:
            return await self.app(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        who = None
        if "cloudflare" in self.modes and hdrs.get("cf-access-jwt-assertion"):
            who = self._cloudflare(hdrs)
        if not who and "token" in self.modes:
            who = self._token(hdrs)
        if not who:
            log.warning("auth failed: %s %s from %s", scope.get("method"), scope["path"],
                        (scope.get("client") or ("?",))[0])
            body = json.dumps({"error": "auth", "message": "missing or invalid credentials"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        tok = principal.set(who)
        try:
            await self.app(scope, receive, send)
        finally:
            principal.reset(tok)

    def _token(self, hdrs: dict) -> str | None:
        presented = hdrs.get(self.cfg.token_header, "")
        if not presented and hdrs.get("authorization", "").lower().startswith("bearer "):
            presented = hdrs["authorization"][7:]
        presented = presented.strip()
        if not presented:
            return None
        match = None
        for tok, name in load_tokens(self.cfg.token_file).items():   # re-read: rotation needs no restart
            if hmac.compare_digest(tok.encode(), presented.encode()):
                match = name
        return match

    def _cloudflare(self, hdrs: dict) -> str | None:
        token = hdrs.get("cf-access-jwt-assertion")
        if not token:
            return None
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=list(self.cfg.cf_audience),
                                issuer=f"https://{self.cfg.cf_team_domain}")
        except jwt.PyJWTError as e:
            log.warning("Access JWT rejected: %s", type(e).__name__)
            return None
        email = (claims.get("email") or "").lower()
        return email if email in {e.lower() for e in self.cfg.allowed_emails} else None
