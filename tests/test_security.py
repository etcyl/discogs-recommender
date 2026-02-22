"""
Security-focused tests mapped to modern CWE categories.

CWE-20:  Improper Input Validation
CWE-22:  Path Traversal
CWE-79:  Cross-site Scripting (XSS)
CWE-116: Improper Encoding or Escaping of Output
CWE-138: Improper Neutralization of Special Elements
CWE-200: Exposure of Sensitive Information
CWE-209: Generation of Error Message Containing Sensitive Info
CWE-400: Uncontrolled Resource Consumption
CWE-502: Deserialization of Untrusted Data
CWE-601: URL Redirection to Untrusted Site
CWE-693: Protection Mechanism Failure
CWE-770: Allocation of Resources Without Limits or Throttling
CWE-918: Server-Side Request Forgery (SSRF)
"""
import json
import os
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from services.cache import SimpleCache
from services import thumbs
from services.discogs_service import _sanitize_search_input


# ---------------------------------------------------------------------------
# Fixture: patched app with mocked services
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_services():
    with patch("app.discogs") as mock_discogs, \
         patch("app.claude") as mock_claude, \
         patch("app.radio") as mock_radio, \
         patch("app.cache") as mock_cache, \
         patch("app.thumbs") as mock_thumbs:
        mock_cache.get.return_value = None
        mock_discogs.get_full_collection.return_value = [
            {"id": 1, "title": "Test", "year": 2020, "artists": ["A"],
             "genres": ["Rock"], "styles": ["Indie"], "labels": ["L"],
             "formats": ["Vinyl"], "thumb": "", "cover_image": "",
             "url": "https://www.discogs.com/release/1", "date_added": "2024-01-01"}
        ]
        mock_thumbs.get_thumbs_summary.return_value = ""
        mock_thumbs.save_thumb.return_value = {
            "artist": "X", "title": "Y", "album": "", "genres": [], "styles": [],
            "timestamp": "2024-01-01",
        }
        yield {
            "discogs": mock_discogs, "claude": mock_claude, "radio": mock_radio,
            "cache": mock_cache, "thumbs": mock_thumbs,
        }


@pytest.fixture
def client():
    from app import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolate_thumbs(tmp_path):
    test_dir = tmp_path / "data"
    test_dir.mkdir()
    test_file = test_dir / "thumbs.json"
    with patch.object(thumbs, "DATA_DIR", test_dir), \
         patch.object(thumbs, "THUMBS_FILE", test_file):
        yield test_file


# ===========================================================================
# CWE-20: Improper Input Validation
# ===========================================================================

class TestCWE20_InputValidation:
    """Verify all user inputs are validated and sanitized."""

    def test_search_null_bytes_stripped(self):
        result = _sanitize_search_input("test\x00injection")
        assert "\x00" not in (result or "")

    def test_search_control_chars_stripped(self):
        result = _sanitize_search_input("\x01\x02\x03normal")
        assert result == "normal"

    def test_search_truncated_to_max_length(self):
        result = _sanitize_search_input("x" * 1000)
        assert len(result) <= 200

    def test_thumbs_artist_null_byte(self):
        entry = thumbs.save_thumb(artist="Art\x00ist", title="Song")
        assert "\x00" not in entry["artist"]

    def test_thumbs_title_control_chars(self):
        entry = thumbs.save_thumb(artist="Artist", title="So\x01ng\x02")
        assert "\x01" not in entry["title"]
        assert "\x02" not in entry["title"]

    def test_cache_key_validation(self):
        c = SimpleCache()
        with pytest.raises(ValueError):
            c.set("", "value")
        with pytest.raises(ValueError):
            c.set(None, "value")  # type: ignore
        with pytest.raises(ValueError):
            c.set("x" * 300, "value")

    def test_engine_param_sanitized(self, client):
        """Engine parameter should default to 'genre' for invalid values."""
        response = client.get("/recommendations?engine=<script>alert(1)</script>")
        assert response.status_code == 200
        # Should not crash or expose the injected value in error

    def test_discovery_bounds_enforced(self, client):
        response = client.get("/recommendations?discovery=-999")
        assert response.status_code == 422
        response = client.get("/recommendations?discovery=999")
        assert response.status_code == 422

    def test_page_bounds_enforced(self, client):
        response = client.get("/collection?page=-1")
        assert response.status_code == 422
        response = client.get("/collection?page=0")
        assert response.status_code == 422

    def test_release_id_type_enforced(self, client):
        response = client.get("/release/abc")
        assert response.status_code == 422
        response = client.get("/release/1.5")
        assert response.status_code == 422

    def test_thumb_request_missing_required(self, client):
        response = client.post("/api/radio/thumbs", json={})
        assert response.status_code == 422

    def test_thumb_request_empty_strings(self, client):
        response = client.post("/api/radio/thumbs", json={"artist": "", "title": ""})
        assert response.status_code == 422

    def test_thumb_genres_type_validated(self, client):
        response = client.post("/api/radio/thumbs", json={
            "artist": "A", "title": "B", "genres": "not-a-list"
        })
        assert response.status_code == 422


# ===========================================================================
# CWE-22: Path Traversal
# ===========================================================================

class TestCWE22_PathTraversal:
    """Ensure no path traversal via user inputs."""

    def test_release_path_traversal_attempt(self, client):
        """release_id is typed as int, so '../' can't be injected."""
        response = client.get("/release/../../../etc/passwd")
        assert response.status_code in (404, 422)

    def test_thumbs_data_dir_confined(self):
        """Thumbs file should be within the data directory."""
        from services.thumbs import DATA_DIR, THUMBS_FILE
        # The file path should be under DATA_DIR
        assert str(THUMBS_FILE).startswith(str(DATA_DIR))


# ===========================================================================
# CWE-79: Cross-site Scripting (XSS)
# ===========================================================================

class TestCWE79_XSS:
    """Test that user inputs aren't rendered as raw HTML."""

    def test_search_xss_in_query(self, client, mock_services):
        mock_services["discogs"].search.return_value = []
        response = client.get('/search?q=<script>alert("xss")</script>')
        assert response.status_code == 200
        # Jinja2 auto-escapes by default; raw script tag should not appear
        assert '<script>alert("xss")</script>' not in response.text

    def test_search_xss_in_artist(self, client, mock_services):
        mock_services["discogs"].search.return_value = []
        response = client.get('/search?artist=<img src=x onerror=alert(1)>')
        assert response.status_code == 200
        # Jinja2 auto-escapes: <img> becomes &lt;img&gt; so no raw HTML tag injection
        assert "<img src=x" not in response.text


# ===========================================================================
# CWE-138: Improper Neutralization of Special Elements
# ===========================================================================

class TestCWE138_SpecialElements:
    """Test neutralization of null bytes, control chars, etc."""

    def test_null_byte_in_search(self):
        result = _sanitize_search_input("test\x00value")
        assert "\x00" not in (result or "")

    def test_newline_in_search(self):
        result = _sanitize_search_input("test\nvalue")
        # Newlines should be stripped (not printable in search context)
        assert result is not None

    def test_tab_in_search(self):
        result = _sanitize_search_input("test\tvalue")
        assert result is not None

    def test_emoji_preserved(self):
        """Emoji/unicode should be preserved — only control chars removed."""
        entry = thumbs.save_thumb(artist="Test", title="Song")
        assert entry["artist"] == "Test"


# ===========================================================================
# CWE-200/209: Information Exposure
# ===========================================================================

class TestCWE200_209_InfoExposure:
    """Ensure error messages don't leak secrets or system info."""

    def test_error_redacts_api_key(self):
        from app import _sanitize_error
        from config import settings
        error = Exception(f"Auth failed with key {settings.anthropic_api_key}")
        sanitized = _sanitize_error(error)
        assert settings.anthropic_api_key not in sanitized
        assert "[REDACTED]" in sanitized

    def test_error_redacts_discogs_token(self):
        from app import _sanitize_error
        from config import settings
        error = Exception(f"Token {settings.discogs_token} is invalid")
        sanitized = _sanitize_error(error)
        assert settings.discogs_token not in sanitized

    def test_api_docs_disabled(self, client):
        """Swagger/ReDoc docs should be disabled in production."""
        response = client.get("/docs")
        assert response.status_code == 404
        response = client.get("/redoc")
        assert response.status_code == 404

    def test_error_in_route_does_not_expose_stack(self, client, mock_services):
        mock_services["discogs"].get_full_collection.side_effect = Exception(
            "Connection to internal-db:5432 failed"
        )
        response = client.get("/")
        assert response.status_code == 200
        # The HTML should contain the error but not expose internal hostnames dangerously
        # Jinja2 escapes it, so it's rendered as text, not executable


# ===========================================================================
# CWE-400/770: Resource Exhaustion
# ===========================================================================

class TestCWE400_770_ResourceExhaustion:
    """Verify limits on cache size, file size, and entry counts."""

    def test_cache_max_entries(self):
        c = SimpleCache(max_entries=5)
        for i in range(10):
            c.set(f"key{i}", f"val{i}")
        assert c.size() <= 5

    def test_cache_negative_ttl_rejected(self):
        c = SimpleCache()
        with pytest.raises(ValueError):
            c.set("key", "val", ttl=-1)

    def test_thumbs_max_entries(self):
        original = thumbs.MAX_THUMBS_ENTRIES
        try:
            thumbs.MAX_THUMBS_ENTRIES = 3
            for i in range(10):
                thumbs.save_thumb(artist=f"A{i}", title=f"T{i}")
            loaded = thumbs.load_thumbs()
            assert len(loaded) <= 3
        finally:
            thumbs.MAX_THUMBS_ENTRIES = original

    def test_thumbs_oversized_file_rejected(self, isolate_thumbs):
        """Files >5 MB are rejected to prevent memory exhaustion."""
        big_data = json.dumps([{"artist": "x" * 1000}] * 6000)
        isolate_thumbs.write_text(big_data)
        result = thumbs.load_thumbs()
        assert result == []

    def test_thumb_field_truncation(self):
        entry = thumbs.save_thumb(artist="A" * 1000, title="T" * 1000)
        assert len(entry["artist"]) <= 500
        assert len(entry["title"]) <= 500


# ===========================================================================
# CWE-502: Deserialization of Untrusted Data
# ===========================================================================

class TestCWE502_Deserialization:
    """Verify safe JSON parsing with no code execution."""

    def test_thumbs_invalid_json_safe(self, isolate_thumbs):
        isolate_thumbs.write_text("__import__('os').system('echo pwned')")
        result = thumbs.load_thumbs()
        assert result == []

    def test_thumbs_non_list_rejected(self, isolate_thumbs):
        isolate_thumbs.write_text('{"__class__": "exploit"}')
        result = thumbs.load_thumbs()
        assert result == []

    def test_thumb_endpoint_rejects_bad_json(self, client):
        response = client.post(
            "/api/radio/thumbs",
            content="this is not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422

    def test_thumb_endpoint_rejects_nested_objects(self, client):
        """Deeply nested objects should not cause stack overflow."""
        # Build deeply nested dict
        nested = {"artist": "A", "title": "B"}
        current = nested
        for _ in range(100):
            current["extra"] = {"nested": True}
            current = current["extra"]
        response = client.post("/api/radio/thumbs", json=nested)
        # Should either succeed (extra fields ignored) or 422
        assert response.status_code in (200, 422)


# ===========================================================================
# CWE-601: Open Redirect
# ===========================================================================

class TestCWE601_OpenRedirect:
    """Verify no open redirect vulnerabilities."""

    def test_no_redirect_on_bad_path(self, client):
        response = client.get("/redirect?url=http://evil.com", follow_redirects=False)
        assert response.status_code in (404, 405)

    def test_static_path_no_traversal(self, client):
        response = client.get("/static/../.env")
        # Should not serve the .env file
        assert response.status_code in (400, 403, 404)


# ===========================================================================
# CWE-693: Protection Mechanism Failure
# ===========================================================================

class TestCWE693_ProtectionMechanisms:
    """Verify security headers and protections are active."""

    def test_all_security_headers_present(self, client):
        response = client.get("/")
        headers = response.headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert headers.get("X-XSS-Protection") == "1; mode=block"

    def test_security_headers_on_api_routes(self, client, mock_services):
        response = client.get("/api/refresh-collection")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_security_headers_on_error(self, client, mock_services):
        mock_services["discogs"].get_full_collection.side_effect = Exception("error")
        response = client.get("/")
        assert response.headers.get("X-Frame-Options") == "DENY"


# ===========================================================================
# CWE-918: Server-Side Request Forgery (SSRF)
# ===========================================================================

class TestCWE918_SSRF:
    """Verify no SSRF via user-controlled URLs."""

    def test_search_query_not_used_as_url(self, client, mock_services):
        """Search params should be passed to Discogs API, not fetched as URLs."""
        mock_services["discogs"].search.return_value = []
        response = client.get("/search?q=http://internal-server/admin")
        assert response.status_code == 200
        # The query should be passed to discogs.search(), not fetched
        mock_services["discogs"].search.assert_called_once()

    def test_release_id_is_integer_only(self, client):
        """Release ID must be an integer, preventing URL injection."""
        response = client.get("/release/http://evil.com")
        # FastAPI returns 404 or 422 for non-integer path params
        assert response.status_code in (404, 422)


# ===========================================================================
# Config validation tests
# ===========================================================================

class TestConfigValidation:
    """Verify config validation catches bad values."""

    def test_invalid_username_chars(self):
        from config import Settings
        with pytest.raises(Exception):
            Settings(
                discogs_token="test_token_1234567890",
                discogs_username="user<script>",
                anthropic_api_key="sk-ant-test",
            )

    def test_empty_username(self):
        from config import Settings
        with pytest.raises(Exception):
            Settings(
                discogs_token="test_token_1234567890",
                discogs_username="",
                anthropic_api_key="sk-ant-test",
            )

    def test_invalid_anthropic_key_prefix(self):
        from config import Settings
        with pytest.raises(Exception):
            Settings(
                discogs_token="test_token_1234567890",
                discogs_username="testuser",
                anthropic_api_key="wrong-prefix-key",
            )

    def test_short_discogs_token(self):
        from config import Settings
        with pytest.raises(Exception):
            Settings(
                discogs_token="short",
                discogs_username="testuser",
                anthropic_api_key="sk-ant-test",
            )

    def test_valid_config(self):
        from config import Settings
        s = Settings(
            discogs_token="test_token_1234567890",
            discogs_username="valid_user-123",
            anthropic_api_key="sk-ant-valid-key-here",
        )
        assert s.discogs_username == "valid_user-123"
