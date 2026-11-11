from immich_mcp.config import Config


def test_allow_list_is_required():
    assert any("ALLOWED_ALBUM_PREFIXES" in p for p in Config(token_file="/x").problems())


def test_wildcard_and_prefixes():
    assert Config(allowed_album_prefixes=("*",)).album_allowed("anything at all")
    c = Config(allowed_album_prefixes=("Family - ", "Pets - "))
    assert c.album_allowed("Pets - Rex") and not c.album_allowed("Private - x")


def test_auth_mode_needs_its_settings():
    c = Config(allowed_album_prefixes=("*",), auth_mode="token,cloudflare", token_file="")
    probs = " ".join(c.problems())
    assert "TOKEN_FILE" in probs and "CF_ACCESS" in probs
    assert Config(allowed_album_prefixes=("*",), auth_mode="none").problems() == []


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("ALLOWED_ALBUM_PREFIXES", "A - , B - ")
    monkeypatch.setenv("INSTRUCTIONS_EXTRA", "line one\\nline two")
    monkeypatch.setenv("ENABLE_SHARE_LINKS", "true")
    c = Config.from_env()
    assert c.allowed_album_prefixes == ("A -", "B -") or c.allowed_album_prefixes == ("A - ", "B - ")
    assert c.instructions_extra == "line one\nline two" and c.share_links_enabled
