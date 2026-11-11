"""Configuration: environment variables only (non-secret), secrets by file path.

Settings with no safe universal default (album allow-list, credentials) must be set explicitly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(x.strip() for x in os.environ.get(name, default).split(",") if x.strip())


def read_secret(path: str | os.PathLike | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    return p.read_text().strip() if p.is_file() else None


@dataclass
class Config:
    # Immich (internal API, never exposed by this server)
    immich_url: str = "http://127.0.0.1:2283"
    immich_api_key_file: str = ""
    immich_api_key: str | None = None            # tests only; production reads the file

    # What a connected client may see: albums whose names start with one of these prefixes ("*" = every album).
    # Required: there is no safe default for someone else's library.
    allowed_album_prefixes: tuple[str, ...] = ()
    # Deny list that overrides the allow list (firewall-style): an asset in ANY album matching one of these is
    # hidden, even if it is also in an allowed album. Albums matching it are hidden too.
    excluded_album_prefixes: tuple[str, ...] = ()
    # Also show assets that are in no album at all (for libraries that don't use albums much). With
    # ALLOWED_ALBUM_PREFIXES=* this means "the whole library minus excluded albums".
    include_assets_without_album: bool = False

    # Auth at the MCP boundary: "token" (loopback HTTP: tunnel-client, Claude Code),
    # "cloudflare" (Cloudflare Access JWT, for a later public endpoint), "none" (stdio / tests only).
    auth_mode: str = "token"
    token_file: str = ""                     # lines of "client-name:token"
    token_header: str = "x-immich-mcp-token"
    cf_team_domain: str = ""                 # e.g. yourteam.cloudflareaccess.com
    cf_audience: tuple[str, ...] = ()
    allowed_emails: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8099", "localhost:8099")

    # HTTP listener (loopback only by default)
    host: str = "127.0.0.1"
    port: int = 8099

    # State + limits
    state_dir: str = "/var/lib/immich-mcp"
    max_upload_bytes: int = 200 * 1024 * 1024     # one asset
    max_base64_bytes: int = 7 * 1024 * 1024       # decoded; keeps requests under the 10 MiB tunnel envelope
    max_image_return_bytes: int = 2 * 1024 * 1024
    url_fetch_allowed_hosts: tuple[str, ...] = ()  # empty = any public host (SSRF rules still apply)
    url_fetch_timeout: float = 60.0
    search_scan_limit: int = 500                  # how many raw Immich results a filtered search may scan


    # Share links (outward-facing: off unless enabled). public_immich_url is what recipients open.
    share_links_enabled: bool = False
    public_immich_url: str = ""
    share_link_max_hours: int = 720
    max_batch_items: int = 50

    # Extra server instructions appended for clients (e.g. "new memes go to album X").
    instructions_extra: str = ""

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        c = cls()
        c.immich_url = e("IMMICH_URL", c.immich_url).rstrip("/")
        c.immich_api_key_file = e("IMMICH_API_KEY_FILE", c.immich_api_key_file)
        c.allowed_album_prefixes = _list("ALLOWED_ALBUM_PREFIXES", ",".join(c.allowed_album_prefixes))
        c.excluded_album_prefixes = _list("EXCLUDED_ALBUM_PREFIXES")
        c.include_assets_without_album = e("INCLUDE_ASSETS_WITHOUT_ALBUM", "false").lower() in ("1", "true", "yes")
        c.auth_mode = e("AUTH_MODE", c.auth_mode)
        c.token_file = e("TOKEN_FILE", c.token_file)
        c.cf_team_domain = e("CF_ACCESS_TEAM_DOMAIN", c.cf_team_domain)
        c.cf_audience = _list("CF_ACCESS_AUD")
        c.allowed_emails = _list("ALLOWED_EMAILS", ",".join(c.allowed_emails))
        c.allowed_hosts = _list("ALLOWED_HOSTS", ",".join(c.allowed_hosts))
        c.host = e("HOST", c.host)
        c.port = int(e("PORT", c.port))
        c.state_dir = e("STATE_DIR", e("STATE_DIRECTORY", c.state_dir))
        c.max_upload_bytes = int(e("MAX_UPLOAD_BYTES", c.max_upload_bytes))
        c.max_base64_bytes = int(e("MAX_BASE64_BYTES", c.max_base64_bytes))
        c.url_fetch_allowed_hosts = _list("URL_FETCH_ALLOWED_HOSTS")
        c.instructions_extra = e("INSTRUCTIONS_EXTRA", "").replace("\\n", "\n")
        c.share_links_enabled = e("ENABLE_SHARE_LINKS", "false").lower() in ("1", "true", "yes")
        c.public_immich_url = e("PUBLIC_IMMICH_URL", c.public_immich_url).rstrip("/")
        c.share_link_max_hours = int(e("SHARE_LINK_MAX_HOURS", c.share_link_max_hours))
        c.max_batch_items = int(e("MAX_BATCH_ITEMS", c.max_batch_items))
        return c

    def api_key(self) -> str:
        key = self.immich_api_key or read_secret(self.immich_api_key_file) or os.environ.get("IMMICH_API_KEY")
        if not key:
            raise RuntimeError(f"no Immich API key ({self.immich_api_key_file} missing)")
        return key

    def album_excluded(self, name: str) -> bool:
        return any(name.startswith(p) for p in self.excluded_album_prefixes)

    def album_allowed(self, name: str) -> bool:
        """Visible album: matches the allow list and NOT the exclude list (exclude wins)."""
        if self.album_excluded(name):
            return False
        if "*" in self.allowed_album_prefixes:
            return True
        return any(name.startswith(p) for p in self.allowed_album_prefixes)

    def problems(self) -> list[str]:
        """Configuration errors that should stop the server from starting."""
        out = []
        if not self.allowed_album_prefixes:
            out.append("ALLOWED_ALBUM_PREFIXES is required (comma-separated album-name prefixes, or * for every album)")
        modes = {m.strip() for m in self.auth_mode.split(",") if m.strip()}
        if "token" in modes and not self.token_file:
            out.append("AUTH_MODE includes token but TOKEN_FILE is not set")
        if "cloudflare" in modes and not (self.cf_team_domain and self.cf_audience and self.allowed_emails):
            out.append("AUTH_MODE includes cloudflare but CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD and ALLOWED_EMAILS are not all set")
        return out
