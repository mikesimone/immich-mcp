"""Entry point.

  immich-mcp                 Streamable HTTP at http://HOST:PORT/mcp (default 127.0.0.1:8099), AUTH_MODE applies
  immich-mcp --stdio         stdio transport (auth is the process boundary; for Claude Code / tunnel-client --mcp-command)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import Config


def make_service(cfg: Config):
    from .immich import Immich
    from .ledger import Ledger
    from .service import Service
    immich = Immich(cfg.immich_url, cfg.api_key())
    return Service(cfg, immich, Ledger(os.path.join(cfg.state_dir, "uploads.db")))


def make_app(cfg: Config, service=None):
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse
    from .auth import AuthMiddleware
    from .server import build
    service = service or make_service(cfg)
    mcp = build(service)
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        max_request_body_size=cfg.max_base64_bytes * 4 // 3 + 1024 * 1024,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                     allowed_hosts=list(cfg.allowed_hosts), allowed_origins=[]),
        host=cfg.host)

    async def healthz(request):
        return JSONResponse({"ok": True})
    app.add_route("/healthz", healthz)
    return AuthMiddleware(app, cfg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="immich-mcp")
    ap.add_argument("--stdio", action="store_true", help="serve MCP over stdio instead of HTTP")
    a = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    cfg = Config.from_env()
    if a.stdio and cfg.auth_mode == "token":
        cfg.auth_mode = "none"          # stdio: the process boundary is the auth
    bad = cfg.problems()
    if bad:
        sys.exit("immich-mcp: " + "; ".join(bad))
    if a.stdio:
        from .server import build
        build(make_service(cfg)).run("stdio")
        return
    import uvicorn
    uvicorn.run(make_app(cfg), host=cfg.host, port=cfg.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
