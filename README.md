# immich-mcp

An [MCP](https://modelcontextprotocol.io) server for [Immich](https://immich.app). Hook it up to ChatGPT, Claude,
Claude Code or any other MCP client and you can search, look at, organize and upload photos in your Immich library by
asking for it, **limited to the albums you choose to expose**.

> Not affiliated with the Immich project.

What it can do:

- **Find things**: semantic search ("dog on a beach"), filename/description/date/type filters, search by person or tag.
- **Look at them**: returns the actual image so the model can see it.
- **Organize**: create and rename albums, add/remove photos, captions, favorites, ratings, capture dates, tags,
  archive/unarchive, find duplicates, name people.
- **Upload**: one file or a whole batch (e.g. every image in an email) from base64, a public URL, or ChatGPT file
  references. Uploads are idempotent: the same bytes are never stored twice, retries are safe.
- **Share**: optional expiring share links with metadata hidden.
- **Delete safely**: deletes only ever go to Immich's trash, and destructive tools need `confirm=true`.

## What clients can see (read this first)

A photo library is personal, so visibility works like firewall rules:

- `ALLOWED_ALBUM_PREFIXES` (required): albums whose names start with one of these prefixes are visible, along with the
  photos in them. `*` means every album.
- `EXCLUDED_ALBUM_PREFIXES` (optional) **always wins**: albums matching it are hidden, and so is every photo in them,
  even a photo that is also in an allowed album.
- `INCLUDE_ASSETS_WITHOUT_ALBUM=true` (optional) also shows photos that are in no album. If you don't organize with
  albums, `ALLOWED_ALBUM_PREFIXES=*` + `INCLUDE_ASSETS_WITHOUT_ALBUM=true` + an exclude list means "my whole library
  except these albums".

Anything not visible behaves as if it doesn't exist: search results are filtered, direct IDs return `not_found`,
people and tags are only listed if they appear in visible photos, you can't pull a hidden photo into a visible album,
and new or renamed albums must stay inside the rules. Locked-folder assets are never visible. Changes made outside the
server (a photo you just filed into an excluded album) are picked up within 5 minutes or on restart.

If you use ChatGPT, everything a tool returns, images included, goes to OpenAI. Choose the allow-list with that in mind.

## Tools

| Tool | What it does |
|---|---|
| `get_server_info` | Immich version, visible prefixes, limits, whether share links are on |
| `search_assets` | Semantic `query`, or filename/description/type/date/album/favorite/person/tag filters; paginated |
| `get_asset` / `get_asset_image` | Details (people, tags, albums, camera, location) / the picture itself |
| `list_albums` / `get_album` | Browse visible albums and their contents |
| `create_album` / `update_album` | Create, rename, or re-describe albums |
| `add_assets_to_album` / `remove_assets_from_album` | Album membership (removal needs `confirm=true`, originals kept) |
| `upload_asset` / `upload_assets` | One file / a batch, into an album; returns asset IDs and per-item results |
| `begin_upload` / `upload_chunk` / `finish_upload` | Files too big for one request (long videos), sent in pieces |
| `get_upload_status` | Outcome of an earlier upload |
| `set_caption` / `update_asset_metadata` | Captions (replace or append), favorite, rating, capture date |
| `set_archived` | Take photos off the timeline, or put them back |
| `list_people` / `name_person` | People in visible photos, most-photographed first / name a face |
| `list_tags` / `tag_assets` / `untag_assets` | Find tags (substring), add or remove them |
| `create_share_link` / `revoke_share_link` | Expiring public links (opt-in, `confirm=true`) |
| `get_duplicates` | Immich's duplicate groups among visible photos |
| `delete_assets` | Move to Immich's trash (restorable for 30 days), `confirm=true` |
| `file_event_pictures` | Opt-in: everything *your own cameras* shot during an event (e.g. a calendar entry) into the next numbered album; dry run by default |

All tools have typed inputs and outputs. Errors come back as results with `{"error": kind, "message": ...}` where kind
is one of `auth`, `permission`, `not_found`, `bad_request`, `unsupported_media`, `too_large`, `blocked_url`, `network`,
`server`, `confirmation_required`, `not_configured`.

## Setup

### 1. An Immich API key

In Immich: *Account Settings → API Keys → New API Key*, and grant only what the server uses:

`server.about`, `asset.read`, `asset.view`, `asset.upload`, `asset.update`, `asset.delete`, `asset.share`,
`album.read`, `album.create`, `album.update`, `albumAsset.create`, `albumAsset.delete`, `duplicate.read`,
`person.read`, `person.update`, `tag.read`, `tag.create`, `tag.asset`, `sharedLink.create`, `sharedLink.delete`

(Drop the `sharedLink.*`/`asset.share` ones if you won't enable share links, `person.update` if you don't want
naming, and so on. A tool whose permission is missing returns a `permission` error.)

### 2. Run it

**Docker** (next to Immich): copy `deploy/immich-mcp.env.example` to `immich-mcp.env`, fill it in, put the API key
and a client-token file in `./secrets/`, then use `deploy/docker-compose.example.yml`.

**systemd** (no Docker):

```sh
sudo useradd --system --home-dir /var/lib/immich-mcp --no-create-home --shell /usr/sbin/nologin immich-mcp
sudo git clone https://github.com/mikesimone/immich-mcp /opt/immich-mcp
cd /opt/immich-mcp && sudo python3 -m venv venv && sudo venv/bin/pip install .
sudo install -d -m 750 -g immich-mcp /etc/immich-mcp
sudo install -m 640 -g immich-mcp deploy/immich-mcp.env.example /etc/immich-mcp/immich-mcp.env   # then edit it
# secrets, readable only by the service user:
printf '%s' 'YOUR_IMMICH_API_KEY' | sudo install -m 400 -o immich-mcp /dev/stdin /etc/immich-mcp/immich-api-key
echo "claude-code:$(openssl rand -hex 32)" | sudo install -m 400 -o immich-mcp /dev/stdin /etc/immich-mcp/client-tokens
sudo install -m 644 deploy/immich-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now immich-mcp
```

It listens on `127.0.0.1:8099` (`/mcp`, Streamable HTTP). `immich-mcp --stdio` runs it over stdio instead.
`scripts/smoke-test.py` checks a running instance.

### 3. Connect a client

**Claude Code** (same machine):

```sh
mkdir -p ~/.config/immich-mcp && echo "claude-code:<the token from client-tokens>" > ~/.config/immich-mcp/token
chmod 600 ~/.config/immich-mcp/token
claude mcp add-json -s user immich '{"type":"http","url":"http://127.0.0.1:8099/mcp","headersHelper":"/opt/immich-mcp/deploy/claude-code-headers.sh"}'
```

The header helper reads the token at connect time, so it never lands in `~/.claude.json`. Any other client that can
send a header works the same way: `X-Immich-MCP-Token: <token>` (or `Authorization: Bearer <token>`). Give each client
its own line in the token file; the name is what the audit log records.

**ChatGPT / Claude.ai** need a public HTTPS endpoint with OAuth. The tested setup is a **Cloudflare Tunnel** plus a
**Cloudflare Access** self-hosted application with **Managed OAuth** (it handles the OAuth 2.1 / dynamic client
registration these connectors expect, so this server doesn't implement OAuth itself):

1. Tunnel ingress: `mcp.example.com → http://localhost:8099`.
2. Zero Trust → Access → Applications → self-hosted app on `mcp.example.com`, a policy that allows only you, and
   under Advanced settings enable Managed OAuth with dynamic client registration, allowing redirect URIs
   `https://chatgpt.com/*`, `https://claude.ai/*`, `https://claude.com/*` (and localhost if you want CLI clients).
3. In `immich-mcp.env`: `AUTH_MODE=token,cloudflare`, `CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com`,
   `CF_ACCESS_AUD=<the app's AUD tag>`, `ALLOWED_EMAILS=you@example.com`, and add `mcp.example.com` to `ALLOWED_HOSTS`.
   The server verifies Access's signed `Cf-Access-Jwt-Assertion` header on every request.
4. ChatGPT: *Settings → Plugins → + → Add custom MCP server*, URL `https://mcp.example.com/mcp`, authentication
   OAuth. Claude.ai: *Settings → Connectors → Add custom connector*, same URL. Sign in when prompted.

(OpenAI's Secure MCP Tunnel is an alternative for ChatGPT that avoids a public endpoint; point its `tunnel-client` at
`http://127.0.0.1:8099/mcp` and give it a token via `mcp.extra_headers` with a custom header name, since headers the
connector forwards override static ones.)

## Uploads and getting bytes in

- `data_base64`: bytes sent by the client, capped per request at `MAX_BASE64_BYTES` (32 MiB decoded by default; base64
  adds a third on the wire, so behind Cloudflare's 100 MB request cap the practical maximum is about 64 MiB).
- Chunked: `begin_upload` (name, exact size, album, optional SHA-256), then `upload_chunk` for pieces 0..n-1 (each up to
  `MAX_BASE64_BYTES`; re-sending a piece is harmless), then `finish_upload`, which verifies and stores it like any other
  upload, up to `MAX_UPLOAD_BYTES` (200 MiB default). Pieces live under `STATE_DIR/chunks`; sessions expire after 24 h.
- `url`: the server downloads a **public https** URL. SSRF protections: no credentials or literal IPs, every DNS
  answer must be a public address, the connection is pinned to the checked IP (no DNS rebinding) with TLS still
  verified against the hostname, each redirect hop is re-checked, the body is size-capped, and the content must sniff
  as an image or video. `URL_FETCH_ALLOWED_HOSTS` narrows it further.
- `file` / `files`: ChatGPT file references (`openai/fileParams`). In practice ChatGPT can hand over images it got from
  its Gmail connector this way, inline email images included.

Every upload names a target album so it stays visible. `upload_id` is derived from the content's SHA-256, Immich is
asked to dedupe by checksum, the result is recorded in a small SQLite ledger (`STATE_DIR`), and album assignment is
tracked separately, so a retry only redoes what's missing. Nothing is reported as uploaded until Immich returns an
asset ID.

## Event albums (opt-in)

`file_event_pictures(title, start, end, apply)` makes an album of what your own cameras shot during an event: give it
the calendar entry's title and its start/end (ISO date-times with offset, or dates for an all-day event, end
exclusive). It names the album `<EVENT_ALBUM_PREFIX>nnn - <title>` with the next free number, reuses that album if
you run it again for the same title, and only **adds**: assets stay in every album they are in and their archive state
is unchanged. `apply=false` (the default) is a dry run with counts per camera.

Off unless `EVENT_CAMERA_RULES` is set (the tool isn't even listed otherwise):

```
EVENT_CAMERA_RULES=[{"device":"phone","exif_make":"samsung","exif_model_prefix":"SM-F966U"},{"device":"glasses","exif_make":"Meta"},{"device":"spycam","filename_prefix":"SpyVideo"}]
EVENT_ALBUM_PREFIX=010.            # must be an allowed prefix
EVENT_TIMEZONE=America/Los_Angeles # default zone for events; per-call time_zone overrides
EVENT_PAD_MINUTES=30               # added before and after timed events
EVENT_NUMBER_MIN=100               # optional: number new event albums only inside this block
EVENT_NUMBER_MAX=199               #   (e.g. events 010.100-199 beside places in 010.200-299)
```

**No duplicate albums:** the dry run lists `existing_candidates`, event-prefix albums that already hold pictures from
inside the window (the same event filed earlier under another name). If there are any, `apply=true` refuses with
`possible_duplicate` until you pass `album_id` (add to that album) or `new_album=true`.

A rule matches when all its fields match, case-insensitive: `exif_make`, `exif_model_prefix`, `filename_contains`,
`filename_prefix`, `filename_regex` (full match on the base name). Shots are matched on the local wall clock (Immich
`localDateTime`), because glasses and spy cams often write no zone offset. A rule with `"clock":"filename"` takes the
wall clock from a leading `YYYYMMDD_HHMMSS` in the file name instead (falling back to `localDateTime` if the name doesn't
parse). Use it for cameras whose videos carry no camera EXIF or a time Immich places hours off: e.g. Ray-Ban Meta videos
as the Meta app exports them have no EXIF but are named `YYYYMMDD_HHMMSS_<8 hex>.mp4` in local time, so
`{"device":"glasses","filename_regex":"[0-9]{8}_[0-9]{6}_[0-9a-f]{8}[.](jpg|mp4)","clock":"filename"}` picks them up
while chat downloads (`Facebook_creation_*`, `VID-*-WA*`, `signal-*`) match no rule. Avoid backslashes in the regex
when the value goes through a systemd `EnvironmentFile` (`[.]`, `[0-9]` instead of `\.`, `\d`). **Visibility:** this is the one tool that looks past the allow-list. It selects assets in
any album (or none) as long as they match a camera rule inside the event window, and makes them visible by putting
them in an allowed album. The exclude list still wins, and hidden/locked/trashed assets are never touched.

## Security notes

- Listener on loopback by default, DNS-rebinding protection via `ALLOWED_HOSTS`.
- Per-client tokens compared in constant time, or Cloudflare Access JWTs verified against the team's keys.
- A JSON audit line (`immich_mcp.audit` logger) for every write, delete, tag change and share link, with the client.
- `file_event_pictures` (opt-in) is the only tool that selects assets outside the allow-list, by camera rule and time
  window only; see "Event albums". The exclude list still applies.
- Deletes are always `force=false` (trash). Locked-folder assets are never shown and never changed.
- Share links are off unless `ENABLE_SHARE_LINKS=true`, always hide metadata, expire (max `SHARE_LINK_MAX_HOURS`,
  default 720), and can only be revoked if this server created them.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test]' && .venv/bin/pytest -q
```

The tests run against an in-memory fake Immich (`tests/fakes.py`) and real Streamable HTTP; no Immich needed.

## Credits

Written predominantly by Claude (Anthropic's AI model, working in Claude Code), designed, directed, tested and
maintained by Mike Simone. It's listed as Vibe-Coded on [awesome.immich.app](https://awesome.immich.app) for that reason.
Issues and pull requests are welcome.

## License

MIT
