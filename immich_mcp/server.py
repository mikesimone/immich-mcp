"""MCP adapter: registers the Service's operations as MCP tools. Transport-agnostic (stdio or Streamable HTTP)."""
from __future__ import annotations

import base64
import json
import functools
from typing import Annotated, Any, Literal, Optional, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from .service import Service, ToolFailure

INSTRUCTIONS = """A private Immich photo library. You only see what the owner exposes: albums whose names match an allow-list
(minus an exclude list that always wins) and the assets inside them, plus optionally assets in no album. Everything
else behaves as if it doesn't exist.

Editing: update_album renames albums or edits their description; set_caption writes or appends photo captions;
set_archived moves photos off/onto the timeline; tag_assets/untag_assets manage tags.

People: list_people (visible people only), then search_assets(person_ids=[...]); name_person names a face.

Sharing: create_share_link makes an expiring link to send someone (if enabled; needs confirm=true).\n\nFinding things: search_assets (semantic `query` like "dog on a beach", or filename/description/date filters),
list_albums / get_album to browse, get_asset for details, get_asset_image to actually look at a picture.

Uploading: upload_assets uploads many items into one album in one call (best for an email's worth of images);
upload_asset takes ONE source: data_base64 (small images, up to the server's base64 limit),
url (a public https URL the server can fetch), or file (a ChatGPT file object with download_url). Every upload
must name a target album (album_id, or album_name which is created if missing and must use an allowed prefix),
so the upload stays visible. Uploads are idempotent: the same bytes return the same upload_id and are never stored
twice; status is created, duplicate (Immich already had it), or already_uploaded. Album assignment is reported
separately (album.status). Use get_upload_status to check an upload_id later.

This server does not care where media comes from (chat uploads, email, the web): hand it the bytes, a fetchable
URL, or a file object and it stores them.

Destructive tools (remove_assets_from_album, delete_assets) need confirm=true; deletes go to Immich's trash
(restorable for 30 days), never permanent. Errors start with a [kind] tag: auth, permission, not_found,
bad_request, unsupported_media, too_large, blocked_url, network, server, confirmation_required, not_configured."""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False, idempotent_hint=True)
WRITE_OPEN = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True, idempotent_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False, idempotent_hint=True)


# ---------------------------------------------------------------- output shapes
class AssetOut(TypedDict):
    id: str
    type: Optional[str]
    filename: Optional[str]
    mime_type: Optional[str]
    taken_at: Optional[str]
    width: Optional[int]
    height: Optional[int]
    is_favorite: bool
    duplicate_id: Optional[str]
    description: Optional[str]


class AlbumOut(TypedDict):
    id: str
    name: str
    asset_count: int
    description: str
    start_date: Optional[str]
    end_date: Optional[str]


class AlbumRef(TypedDict):
    id: str
    name: str


class PersonRef(TypedDict):
    id: str
    name: str


class AssetDetail(AssetOut):
    rating: Optional[int]
    camera: Optional[str]
    location: Optional[str]
    file_size: Optional[int]
    people: list[PersonRef]
    tags: list[str]
    albums: list[AlbumRef]
    is_archived: bool
    is_trashed: bool


class AssetPage(TypedDict):
    assets: list[AssetOut]
    page: int
    next_page: Optional[int]
    truncated_scan: bool


class AlbumPage(TypedDict):
    albums: list[AlbumOut]
    total: int
    page: int
    next_page: Optional[int]


class AlbumDetail(TypedDict):
    album: AlbumOut
    assets: list[AssetOut]
    page: int
    next_page: Optional[int]


class Limits(TypedDict):
    max_upload_bytes: int
    max_base64_bytes: int
    max_page_size: int


class ServerInfo(TypedDict):
    server: str
    immich_version: str
    visible_album_prefixes: list[str]
    excluded_album_prefixes: list[str]
    includes_assets_without_album: bool
    visible_album_count: int
    limits: Limits
    upload_sources: list[str]
    delete_behavior: str
    share_links_enabled: bool


class AlbumAssign(TypedDict):
    id: str
    name: str
    status: str
    error: Optional[str]


class UploadOut(TypedDict):
    upload_id: str
    status: Literal["created", "duplicate", "already_uploaded"]
    asset_id: str
    sha256: str
    filename: str
    mime_type: str
    bytes: int
    album: AlbumAssign


class AlbumAddRow(TypedDict):
    album_id: str
    status: str
    error: Optional[str]


class UploadStatus(TypedDict):
    upload_id: str
    status: str
    asset_id: Optional[str]
    sha256: str
    filename: Optional[str]
    mime_type: Optional[str]
    bytes: Optional[int]
    error: Optional[str]
    albums: list[AlbumAddRow]


class CreateAlbumOut(TypedDict):
    album: AlbumOut
    created: bool


class RenameAlbumOut(TypedDict):
    album: AlbumOut
    old_name: str


class CaptionRow(TypedDict):
    asset_id: str
    caption: str
    kept_generation_info: bool
    changed: bool


class CaptionOut(TypedDict):
    results: list[CaptionRow]


class MembershipRow(TypedDict):
    asset_id: str
    status: str
    error: Optional[str]


class MembershipOut(TypedDict):
    album_id: str
    album_name: str
    results: list[MembershipRow]


class UpdateOut(TypedDict):
    updated: list[str]
    fields: list[str]


class TrashOut(TypedDict):
    trashed: list[str]
    restorable_days: int


class DupGroup(TypedDict):
    duplicate_id: str
    assets: list[AssetOut]
    suggested_keep: list[str]


class DupPage(TypedDict):
    groups: list[DupGroup]
    total: int
    page: int
    next_page: Optional[int]



class PersonOut(TypedDict):
    id: str
    name: str
    photo_count: int


class PeoplePage(TypedDict):
    people: list[PersonOut]
    total: int
    page: int
    next_page: Optional[int]


class NamePersonOut(TypedDict):
    id: str
    name: str
    old_name: str


class SkippedAsset(TypedDict):
    asset_id: str
    visibility: Optional[str]


class ArchiveOut(TypedDict):
    archived: bool
    changed: list[str]
    unchanged: list[str]
    skipped: list[SkippedAsset]


class BatchRow(TypedDict):
    index: int
    status: str
    upload_id: Optional[str]
    asset_id: Optional[str]
    filename: Optional[str]
    album_status: Optional[str]
    error: Optional[str]


class BatchOut(TypedDict):
    album: AlbumRef
    counts: dict[str, int]
    results: list[BatchRow]


class ShareLinkOut(TypedDict):
    link_id: str
    url: str
    expires_at: str
    asset_count: int
    allow_download: bool
    shows_metadata: bool


class RevokeOut(TypedDict):
    link_id: str
    revoked: bool


class TagOut(TypedDict):
    id: str
    name: str
    visible_asset_count: int


class TagPage(TypedDict):
    tags: list[TagOut]
    total: int
    page: int
    next_page: Optional[int]
    scanned_candidates: int


class TagAddRow(TypedDict):
    tag: str
    tag_id: str
    added: int
    already: int


class TagAddOut(TypedDict):
    results: list[TagAddRow]


class TagRemoveRow(TypedDict):
    tag: str
    tag_id: Optional[str]
    removed: int


class TagRemoveOut(TypedDict):
    results: list[TagRemoveRow]


class Base64Item(TypedDict, total=False):
    data_base64: str
    filename: str
    mime_type: str

# ---------------------------------------------------------------- input field docs
AssetId = Annotated[str, Field(description="Immich asset ID (UUID)")]
AssetIds = Annotated[list[str], Field(description="Immich asset IDs (UUIDs)", min_length=1, max_length=500)]
AlbumId = Annotated[str, Field(description="Immich album ID (UUID)")]
Page = Annotated[int, Field(description="1-based page number", ge=1)]
PageSize = Annotated[int, Field(description="Results per page (max 100)", ge=1, le=100)]
Confirm = Annotated[bool, Field(description="Must be true. Confirms this change was explicitly requested.")]

FILE_SCHEMA_NOTE = ("ChatGPT file object ({download_url, file_id, mime_type?, file_name?}). The server downloads "
                    "download_url itself.")


class FileParam(TypedDict, total=False):
    download_url: str
    file_id: str
    mime_type: str
    file_name: str


class _Fail(Exception):
    def __init__(self, result: CallToolResult):
        self.result = result


def build(service: Service) -> MCPServer:
    instructions = INSTRUCTIONS + ("\n\nNotes from this server's owner:\n" + service.cfg.instructions_extra
                                   if service.cfg.instructions_extra else "")
    mcp = MCPServer(name="immich", title="Immich", instructions=instructions,
                    version="1.0.0")

    async def run(coro):
        try:
            return await service._wrap(coro)
        except ToolFailure as e:
            # Machine-readable error result: is_error + structured {error, message}, text tagged "[kind] ..."
            raise _Fail(CallToolResult(is_error=True, content=[TextContent(type="text", text=f"[{e.kind}] {e.message}")],
                                       structured_content={"error": e.kind, "message": e.message}))

    def tool(**kw):
        """mcp.tool, but a _Fail raised inside the tool is returned as its error result."""
        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **k):
                try:
                    return await fn(*a, **k)
                except _Fail as f:
                    return f.result
            return mcp.tool(**kw)(wrapper)
        return deco

    @tool(title="Server info", annotations=RO,
              description="Use first to learn what this server can do: Immich version, which album prefixes are "
                          "visible, upload limits and sources.")
    async def get_server_info() -> ServerInfo:
        return await run(service.server_info())

    @tool(title="Search photos and videos", annotations=RO,
              description="Find assets. `query` does semantic (CLIP) search, e.g. 'raccoon holding a sign'. Without "
                          "query, filters by filename/description substring, type, taken date range, album, favorite. "
                          "Results are limited to visible albums and paginated.")
    async def search_assets(
            query: Annotated[Optional[str], Field(description="Natural-language description of the image content")] = None,
            filename: Annotated[Optional[str], Field(description="Substring of the original filename")] = None,
            description: Annotated[Optional[str], Field(description="Substring of the asset description")] = None,
            asset_type: Annotated[Optional[Literal["IMAGE", "VIDEO"]], Field(description="Restrict to images or videos")] = None,
            taken_after: Annotated[Optional[str], Field(description="ISO 8601 date/time, inclusive")] = None,
            taken_before: Annotated[Optional[str], Field(description="ISO 8601 date/time, exclusive")] = None,
            album_id: Annotated[Optional[str], Field(description="Only search inside this album")] = None,
            is_favorite: Annotated[Optional[bool], Field(description="Only favorites (true) or non-favorites (false)")] = None,
            person_ids: Annotated[Optional[list[str]], Field(description="Only assets showing ALL of these people (IDs from list_people)", max_length=10)] = None,
            tag_ids: Annotated[Optional[list[str]], Field(description="Only assets with these tags (IDs from list_tags)", max_length=10)] = None,
            page: Page = 1, page_size: PageSize = 25) -> AssetPage:
        return await run(service.search_assets(query=query, filename=filename, description=description,
                                               asset_type=asset_type, taken_after=taken_after, taken_before=taken_before,
                                               album_id=album_id, is_favorite=is_favorite, page=page, page_size=page_size,
                                               person_ids=person_ids, tag_ids=tag_ids))

    @tool(title="Get asset details", annotations=RO,
              description="Metadata for one asset: filename, type, dates, size, camera, location, people, description, "
                          "favorite/rating, and which visible albums contain it.")
    async def get_asset(asset_id: AssetId) -> AssetDetail:
        return await run(service.get_asset(asset_id))

    @tool(title="View an image", annotations=RO,
              description="Returns the picture itself (JPEG/WebP rendition) so you can look at it. 'preview' is large "
                          "(~1440px), 'thumbnail' is small (~250px). Works for videos too (poster frame).")
    async def get_asset_image(asset_id: AssetId,
                              size: Annotated[Literal["preview", "thumbnail"], Field(description="Rendition size")] = "preview"
                              ) -> CallToolResult:
        data, mime, meta = await run(service.get_asset_image(asset_id, size))
        return CallToolResult(content=[ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime),
                                       TextContent(type="text", text=json.dumps(meta))],
                              structured_content=meta)

    @tool(title="List albums", annotations=RO,
              description="List visible albums (name, asset count, dates), optionally filtered by a name substring.")
    async def list_albums(name_contains: Annotated[Optional[str], Field(description="Case-insensitive substring of the album name")] = None,
                          page: Page = 1, page_size: PageSize = 50) -> AlbumPage:
        return await run(service.list_albums(name_contains, page, page_size))

    @tool(title="Get album", annotations=RO,
              description="One album's details and a page of its assets (oldest first).")
    async def get_album(album_id: AlbumId, page: Page = 1, page_size: PageSize = 50) -> AlbumDetail:
        return await run(service.get_album(album_id, page, page_size))

    @tool(title="Upload a photo or video", annotations=WRITE_OPEN,
              meta={"openai/fileParams": ["file"]},
              description="Upload one image or video into an album. Give exactly one source: data_base64, url, or file. "
                          "Idempotent by content hash (safe to retry). Returns upload_id, Immich asset_id, and the album "
                          "assignment result.")
    async def upload_asset(
            album_id: Annotated[Optional[str], Field(description="Target album ID")] = None,
            album_name: Annotated[Optional[str], Field(description="Target album name; created if missing (must use an allowed prefix)")] = None,
            data_base64: Annotated[Optional[str], Field(description="File bytes, base64 (a data: URL prefix is accepted). For small files.")] = None,
            url: Annotated[Optional[str], Field(description="Public https URL of the file (no private/internal addresses)")] = None,
            file: Annotated[Optional[FileParam], Field(description=FILE_SCHEMA_NOTE)] = None,
            filename: Annotated[Optional[str], Field(description="Filename to store (extension inferred if missing)")] = None,
            mime_type: Annotated[Optional[str], Field(description="Declared MIME type (content is sniffed regardless)")] = None,
            taken_at: Annotated[Optional[str], Field(description="ISO 8601 capture date to record; defaults to now")] = None,
            description: Annotated[Optional[str], Field(description="Description to set on a newly created asset")] = None,
    ) -> UploadOut:
        return await run(service.upload_asset(album_id=album_id, album_name=album_name, data_base64=data_base64, url=url,
                                              file=file, filename=filename, mime_type=mime_type, taken_at=taken_at,
                                              description=description))

    @tool(title="Upload status", annotations=RO,
              description="Outcome of an earlier upload, by upload_id (or the file's SHA-256): status, asset_id, album assignments.")
    async def get_upload_status(upload_id: Annotated[Optional[str], Field(description="upload_id from upload_asset")] = None,
                                sha256: Annotated[Optional[str], Field(description="SHA-256 hex of the file")] = None) -> UploadStatus:
        return await run(service.get_upload_status(upload_id, sha256))

    @tool(title="Create album", annotations=WRITE,
              description="Create an album, or return the existing one with that exact name. Name must start with an "
                          "allowed prefix (see get_server_info).")
    async def create_album(name: Annotated[str, Field(description="Album name", min_length=1, max_length=200)],
                           description: Annotated[str, Field(description="Album description")] = "") -> CreateAlbumOut:
        r = await run(service.create_album(name, description))
        return {"album": r["album"], "created": r["created"]}

    @tool(title="Rename album / edit album description", annotations=WRITE,
              description="Rename a visible album, change its description, or both. A new name must also start with an "
                          "allowed prefix and must not match another visible album. Photos are untouched.")
    async def update_album(album_id: AlbumId,
                           new_name: Annotated[Optional[str], Field(description="New album name, omit to keep", min_length=1, max_length=200)] = None,
                           description: Annotated[Optional[str], Field(description="New album description ('' clears it); omit to keep")] = None
                           ) -> RenameAlbumOut:
        return await run(service.update_album(album_id, new_name, description))

    @tool(title="Add assets to album", annotations=WRITE,
              description="Add visible assets to a visible album. Already-present assets are reported as already_in_album.")
    async def add_assets_to_album(album_id: AlbumId, asset_ids: AssetIds) -> MembershipOut:
        return await run(service.add_assets_to_album(album_id, asset_ids))

    @tool(title="Remove assets from album", annotations=DESTRUCTIVE,
              description="Remove assets from an album. The photos themselves are kept. Requires confirm=true.")
    async def remove_assets_from_album(album_id: AlbumId, asset_ids: AssetIds, confirm: Confirm = False) -> MembershipOut:
        return await run(service.remove_assets_from_album(album_id, asset_ids, confirm))

    @tool(title="Set photo caption", annotations=WRITE,
              description="Write or extend the caption (Immich's description, which semantic search also reads) on one or "
                          "more visible photos/videos. mode=replace overwrites the caption ('' clears it); mode=append adds "
                          "a new paragraph after the existing caption, e.g. to keep an auto-generated scene description. "
                          "Any generation-info section on AI images is always preserved.")
    async def set_caption(asset_ids: Annotated[list[str], Field(description="Immich asset IDs (UUIDs)", min_length=1, max_length=100)],
                          caption: Annotated[str, Field(description="Caption text", max_length=5000)],
                          mode: Annotated[Literal["replace", "append"], Field(description="replace or append")] = "replace"
                          ) -> CaptionOut:
        return await run(service.set_caption(asset_ids, caption, mode))

    @tool(title="Update asset metadata", annotations=WRITE,
              description="Set favorite, star rating, capture date, or (same as set_caption replace) the caption on one or "
                          "more visible assets.")
    async def update_asset_metadata(
            asset_ids: AssetIds,
            description: Annotated[Optional[str], Field(description="New caption/description (replaces the caption; generation info kept)")] = None,
            is_favorite: Annotated[Optional[bool], Field(description="Favorite on/off")] = None,
            rating: Annotated[Optional[int], Field(description="Star rating 0-5", ge=0, le=5)] = None,
            taken_at: Annotated[Optional[str], Field(description="New capture date/time, ISO 8601")] = None) -> UpdateOut:
        return await run(service.update_asset_metadata(asset_ids, description, is_favorite, rating, taken_at))

    @tool(title="List people", annotations=RO,
          description="People recognised in the visible photos, most-photographed first, with how many visible photos "
                      "each appears in. Use the IDs with search_assets(person_ids=...) to find photos of someone.")
    async def list_people(name_contains: Annotated[Optional[str], Field(description="Case-insensitive substring of the name")] = None,
                          named_only: Annotated[bool, Field(description="Skip unnamed faces")] = True,
                          page: Page = 1, page_size: PageSize = 50) -> PeoplePage:
        return await run(service.list_people(name_contains, named_only, page, page_size))

    @tool(title="Name a person", annotations=WRITE,
          description="Set or change the name of a recognised person (e.g. an unnamed face found via list_people with "
                      "named_only=false). Affects every photo of them.")
    async def name_person(person_id: Annotated[str, Field(description="Person ID from list_people or get_asset")],
                          name: Annotated[str, Field(description="Name to set", min_length=1, max_length=100)]) -> NamePersonOut:
        return await run(service.name_person(person_id, name))

    @tool(title="Archive / unarchive", annotations=WRITE,
          description="archived=true takes photos off the main timeline (they stay in albums and search); archived=false "
                      "puts them back on the timeline. Locked or hidden assets are never touched.")
    async def set_archived(asset_ids: AssetIds,
                           archived: Annotated[bool, Field(description="true = archive, false = back to timeline")]) -> ArchiveOut:
        return await run(service.set_archived(asset_ids, archived))

    @tool(title="Upload many photos", annotations=WRITE_OPEN, meta={"openai/fileParams": ["files"]},
          description="Upload several images/videos into one album in a single call, e.g. every image from an email. "
                      "Give files (ChatGPT file objects), urls (public https), and/or images (base64). Each item is "
                      "independent and idempotent; failures are reported per item without stopping the rest.")
    async def upload_assets(
            album_id: Annotated[Optional[str], Field(description="Target album ID")] = None,
            album_name: Annotated[Optional[str], Field(description="Target album name; created if missing (allowed prefix)")] = None,
            files: Annotated[Optional[list[FileParam]], Field(description="ChatGPT file objects", max_length=50)] = None,
            urls: Annotated[Optional[list[str]], Field(description="Public https URLs", max_length=50)] = None,
            images: Annotated[Optional[list[Base64Item]], Field(description="Base64 items {data_base64, filename?, mime_type?}; keep small", max_length=50)] = None,
            taken_at: Annotated[Optional[str], Field(description="ISO 8601 capture date for all items; defaults to now")] = None,
    ) -> BatchOut:
        return await run(service.upload_assets(album_id=album_id, album_name=album_name, files=files, urls=urls,
                                               images=images, taken_at=taken_at))

    @tool(title="Create share link", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                                 open_world_hint=True, idempotent_hint=False),
          description="Make an expiring public link to one or more visible photos, to send to someone. Anyone with the "
                      "link can view them until it expires. Metadata (GPS, camera) is hidden. Requires confirm=true; only "
                      "available if the server enables share links.")
    async def create_share_link(asset_ids: Annotated[list[str], Field(description="Asset IDs to share", min_length=1, max_length=100)],
                                expires_in_hours: Annotated[int, Field(description="Hours until the link stops working", ge=1, le=8760)] = 72,
                                allow_download: Annotated[bool, Field(description="Let viewers download originals")] = True,
                                description: Annotated[Optional[str], Field(description="Optional note shown on the share page", max_length=500)] = None,
                                confirm: Confirm = False) -> ShareLinkOut:
        return await run(service.create_share_link(asset_ids, expires_in_hours, allow_download, description, confirm))

    @tool(title="Revoke share link", annotations=DESTRUCTIVE,
          description="Kill a share link created through this server, before it expires.")
    async def revoke_share_link(link_id: Annotated[str, Field(description="link_id from create_share_link")]) -> RevokeOut:
        return await run(service.revoke_share_link(link_id))

    @tool(title="Find tags", annotations=RO,
          description="Tags whose name contains the text (at least 2 characters), limited to tags on visible photos, with "
                      "how many visible photos carry each. Use the IDs with search_assets(tag_ids=...).")
    async def list_tags(name_contains: Annotated[str, Field(description="Substring of the tag name (min 2 chars)", min_length=2)],
                        page: Page = 1, page_size: PageSize = 50) -> TagPage:
        return await run(service.list_tags(name_contains, page, page_size))

    @tool(title="Tag photos", annotations=WRITE,
          description="Add one or more tags (by name; created if they don't exist) to visible photos.")
    async def tag_assets(asset_ids: AssetIds,
                         tags: Annotated[list[str], Field(description="Tag names", min_length=1, max_length=20)]) -> TagAddOut:
        return await run(service.tag_assets(asset_ids, tags))

    @tool(title="Untag photos", annotations=WRITE,
          description="Remove one or more tags (by name) from visible photos. The tags themselves are kept.")
    async def untag_assets(asset_ids: AssetIds,
                           tags: Annotated[list[str], Field(description="Tag names", min_length=1, max_length=20)]) -> TagRemoveOut:
        return await run(service.untag_assets(asset_ids, tags))

    @tool(title="Find duplicates", annotations=RO,
              description="Groups of visually duplicate assets that Immich detected, limited to visible assets.")
    async def get_duplicates(page: Page = 1, page_size: PageSize = 25) -> DupPage:
        return await run(service.get_duplicates(page, page_size))

    @tool(title="Delete assets (to trash)", annotations=DESTRUCTIVE,
              description="Move visible assets to Immich's trash (restorable for 30 days; never permanent). Requires confirm=true.")
    async def delete_assets(asset_ids: AssetIds, confirm: Confirm = False) -> TrashOut:
        return await run(service.delete_assets(asset_ids, confirm))

    return mcp
