#!/bin/sh
# Claude Code "headersHelper": prints the immich-mcp client header from a token file, so the token never sits in
# ~/.claude.json. The file holds one "name:token" line (or just the token).
#   claude mcp add-json -s user immich '{"type":"http","url":"http://127.0.0.1:8099/mcp","headersHelper":"/path/to/claude-code-headers.sh"}'
f="${IMMICH_MCP_TOKEN_FILE:-$HOME/.config/immich-mcp/token}"
tok=$(sed -n '1s/^[^:]*://p' "$f" 2>/dev/null); [ -n "$tok" ] || tok=$(head -n1 "$f" 2>/dev/null)
[ -n "$tok" ] || exit 1
printf '{"X-Immich-MCP-Token": "%s"}\n' "$tok"
