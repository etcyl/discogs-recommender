"""Tests for app.py routes — covers CWE-20, CWE-209, CWE-693, CWE-346."""
import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# Patch config before importing app
@pytest.fixture(autouse=True)
def mock_services():
    """Mock all external services to prevent real API calls."""
    with patch("app.discogs") as mock_discogs, \
         patch("app.claude") as mock_claude, \
         patch("app.radio") as mock_radio, \
         patch("app.cache") as mock_cache, \
         patch("app.thumbs") as mock_thumbs:
        # Default returns
        mock_cache.get.return_value = None
        mock_discogs.get_full_collection.return_value = [
            {
                "id": 1001, "title": "OK Computer", "year": 1997,
                "artists": ["Radiohead"], "genres": ["Rock"],
                "styles": ["Alternative Rock"], "labels": ["Parlophone"],
                "formats": ["Vinyl"], "thumb": "", "cover_image": "",
                "url": "https://www.discogs.com/release/1001",
                "date_added": "2024-01-01",
            }
        ]
        mock_thumbs.get_thumbs_summary.return_value = "No liked songs yet."
        mock_thumbs.save_thumb.return_value = {
            "artist": "Test", "title": "Song", "album": "",
            "genres": [], "styles": [], "timestamp": "2024-01-01",
        }
        yield {
            "discogs": mock_discogs,
            "claude": mock_claude,
            "radio": mock_radio,
            "cache": mock_cache,
            "thumbs": mock_thumbs,
        }


@pytest.fixture
def client():
    from app import app
    return TestClient(app)


class TestSecurityHeaders:
    """CWE-693: Security header verification."""

    def test_x_content_type_options(self, client):
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, client):
        response = client.get("/")
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_referrer_policy(self, client):
        response = client.get("/")
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_xss_protection(self, client):
        response = client.get("/")
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"


class TestHomeRoute:
    """Tests for GET /."""

    def test_home_success(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_home_renders_html(self, client):
        response = client.get("/")
        assert "text/html" in response.headers.get("content-type", "")

    def test_home_error_handling(self, client, mock_services):
        mock_services["discogs"].get_full_collection.side_effect = Exception("API down")
        response = client.get("/")
        assert response.status_code == 200  # Should render error in template, not crash


class TestCollectionRoute:
    """Tests for GET /collection."""

    def test_collection_page_1(self, client):
        response = client.get("/collection?page=1")
        assert response.status_code == 200

    def test_collection_invalid_page(self, client):
        # FastAPI validates ge=1, so page=0 should return 422
        response = client.get("/collection?page=0")
        assert response.status_code == 422

    def test_collection_negative_page(self, client):
        response = client.get("/collection?page=-1")
        assert response.status_code == 422


class TestRecommendationsRoute:
    """Tests for GET /recommendations — CWE-20."""

    def test_genre_engine(self, client, mock_services):
        mock_services["cache"].get.return_value = [{"id": 1, "title": "Rec", "score": 5}]
        response = client.get("/recommendations?engine=genre&discovery=50")
        assert response.status_code == 200

    def test_claude_engine(self, client, mock_services):
        mock_services["cache"].get.return_value = [{"artist": "Test", "album": "A"}]
        response = client.get("/recommendations?engine=claude")
        assert response.status_code == 200

    def test_invalid_engine_defaults_to_genre(self, client):
        response = client.get("/recommendations?engine=malicious_value")
        assert response.status_code == 200

    def test_discovery_bounds(self, client):
        response = client.get("/recommendations?discovery=50")
        assert response.status_code == 200

    def test_discovery_below_min(self, client):
        response = client.get("/recommendations?discovery=-10")
        assert response.status_code == 422

    def test_discovery_above_max(self, client):
        response = client.get("/recommendations?discovery=200")
        assert response.status_code == 422


class TestSearchRoute:
    """Tests for GET /search."""

    def test_search_no_params(self, client):
        response = client.get("/search")
        assert response.status_code == 200

    def test_search_with_query(self, client, mock_services):
        mock_services["discogs"].search.return_value = []
        response = client.get("/search?q=radiohead")
        assert response.status_code == 200

    def test_search_with_multiple_params(self, client, mock_services):
        mock_services["discogs"].search.return_value = []
        response = client.get("/search?artist=radiohead&genre=rock")
        assert response.status_code == 200


class TestReleaseRoute:
    """Tests for GET /release/{release_id}."""

    def test_valid_release(self, client, mock_services):
        mock_services["discogs"].get_release_details.return_value = {
            "id": 1001, "title": "OK Computer", "artists": ["Radiohead"],
        }
        response = client.get("/release/1001")
        assert response.status_code == 200

    def test_invalid_release_id_type(self, client):
        response = client.get("/release/notanumber")
        assert response.status_code == 422

    def test_release_error(self, client, mock_services):
        mock_services["discogs"].get_release_details.side_effect = Exception("Not found")
        response = client.get("/release/99999")
        assert response.status_code == 200  # Renders error in template


class TestRefreshEndpoints:
    """Tests for cache refresh endpoints."""

    def test_refresh_recommendations(self, client, mock_services):
        response = client.get("/api/refresh-recommendations")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_refresh_collection(self, client, mock_services):
        response = client.get("/api/refresh-collection")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_refresh_radio(self, client, mock_services):
        response = client.get("/api/radio/refresh-playlist")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRadioPlaylist:
    """Tests for GET /api/radio/playlist."""

    def test_cached_playlist_returned(self, client, mock_services):
        mock_services["cache"].get.side_effect = lambda key: (
            [{"artist": "A", "title": "B"}] if "radio_playlist" in key else None
        )
        response = client.get("/api/radio/playlist")
        assert response.status_code == 200
        data = response.json()
        assert data["cached"] is True

    def test_empty_collection_returns_400(self, client, mock_services):
        mock_services["discogs"].get_full_collection.return_value = []
        response = client.get("/api/radio/playlist")
        assert response.status_code == 400


class TestRadioThumbs:
    """Tests for POST /api/radio/thumbs — CWE-20, CWE-502."""

    def test_valid_thumb(self, client, mock_services):
        response = client.post("/api/radio/thumbs", json={
            "artist": "Radiohead", "title": "Creep", "album": "Pablo Honey",
            "genres": ["Rock"], "styles": ["Alternative Rock"],
        })
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_missing_artist(self, client):
        response = client.post("/api/radio/thumbs", json={
            "title": "Creep"
        })
        assert response.status_code == 422

    def test_missing_title(self, client):
        response = client.post("/api/radio/thumbs", json={
            "artist": "Radiohead"
        })
        assert response.status_code == 422

    def test_empty_artist(self, client):
        response = client.post("/api/radio/thumbs", json={
            "artist": "", "title": "Creep"
        })
        assert response.status_code == 422

    def test_invalid_json_body(self, client):
        response = client.post(
            "/api/radio/thumbs",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422

    def test_oversized_artist_truncated(self, client, mock_services):
        response = client.post("/api/radio/thumbs", json={
            "artist": "A" * 501, "title": "Song",
        })
        # Pydantic max_length=500 should reject
        assert response.status_code == 422

    def test_non_list_genres(self, client):
        response = client.post("/api/radio/thumbs", json={
            "artist": "Test", "title": "Song", "genres": "not a list",
        })
        assert response.status_code == 422


class TestRadioPage:
    """Tests for GET /radio."""

    def test_radio_page_renders(self, client):
        response = client.get("/radio")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
