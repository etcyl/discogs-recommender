"""Tests for services/spotify_service.py (embed scraping approach)."""
import json
import pytest
from unittest.mock import MagicMock, patch

from services.spotify_service import SpotifyService, SpotifyServiceError, MAX_PLAYLIST_TRACKS, _embed_cache


def _make_embed_html(entity_data: dict) -> str:
    """Build fake embed page HTML with __NEXT_DATA__ containing the entity."""
    next_data = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": entity_data
                    }
                }
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'


SAMPLE_ENTITY = {
    "name": "Chill Hits",
    "subtitle": "Spotify",
    "description": "Relax and unwind",
    "coverArt": {
        "sources": [{"url": "https://img.spotify.com/cover.jpg", "width": 640, "height": 640}]
    },
    "trackList": [
        {
            "title": "Song A",
            "subtitle": "Artist A",
            "uri": "spotify:track:track1",
            "duration": 240000,
        },
        {
            "title": "Song B",
            "subtitle": "Artist B, Artist C",
            "uri": "spotify:track:track2",
            "duration": 180000,
        },
    ],
}


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the embed cache before each test."""
    _embed_cache.clear()
    yield
    _embed_cache.clear()


class TestParsePlaylistUrl:
    """Tests for the static URL parser."""

    def test_standard_url(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U"
        assert SpotifyService.parse_playlist_url(url) == "37i9dQZF1DWXRqgorJj26U"

    def test_url_with_query_params(self):
        url = "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=abc123"
        assert SpotifyService.parse_playlist_url(url) == "37i9dQZF1DWXRqgorJj26U"

    def test_spotify_uri(self):
        url = "spotify:playlist:37i9dQZF1DWXRqgorJj26U"
        assert SpotifyService.parse_playlist_url(url) == "37i9dQZF1DWXRqgorJj26U"

    def test_invalid_url(self):
        assert SpotifyService.parse_playlist_url("https://example.com") is None

    def test_empty_string(self):
        assert SpotifyService.parse_playlist_url("") is None

    def test_album_url_does_not_match(self):
        url = "https://open.spotify.com/album/abc123"
        assert SpotifyService.parse_playlist_url(url) is None


class TestGetPlaylistInfo:
    """Tests for get_playlist_info()."""

    @patch("services.spotify_service.httpx.get")
    def test_returns_info(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(SAMPLE_ENTITY)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        info = svc.get_playlist_info("abc123")
        assert info["name"] == "Chill Hits"
        assert info["track_count"] == 2
        assert info["playlist_id"] == "abc123"
        assert info["image_url"] == "https://img.spotify.com/cover.jpg"
        assert info["owner"] == "Spotify"

    @patch("services.spotify_service.httpx.get")
    def test_no_cover_art(self, mock_get):
        entity = {**SAMPLE_ENTITY, "coverArt": {}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(entity)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        info = svc.get_playlist_info("xyz")
        assert info["image_url"] == ""

    @patch("services.spotify_service.httpx.get")
    def test_404_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        with pytest.raises(SpotifyServiceError, match="not found"):
            svc.get_playlist_info("bad_id")


class TestGetPlaylistTracks:
    """Tests for get_playlist_tracks()."""

    @patch("services.spotify_service.httpx.get")
    def test_returns_tracks(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(SAMPLE_ENTITY)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        tracks = svc.get_playlist_tracks("abc123")
        assert len(tracks) == 2
        assert tracks[0]["artist"] == "Artist A"
        assert tracks[0]["title"] == "Song A"
        assert tracks[0]["spotify_id"] == "track1"
        assert tracks[0]["duration_ms"] == 240000
        assert tracks[1]["all_artists"] == ["Artist B", "Artist C"]

    @patch("services.spotify_service.httpx.get")
    def test_skips_empty_titles(self, mock_get):
        entity = {
            **SAMPLE_ENTITY,
            "trackList": [
                {"title": "", "subtitle": "A", "uri": "spotify:track:x", "duration": 100},
                {"title": "Real Song", "subtitle": "B", "uri": "spotify:track:y", "duration": 200},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(entity)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        tracks = svc.get_playlist_tracks("abc")
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Real Song"

    @patch("services.spotify_service.httpx.get")
    def test_respects_max_tracks_limit(self, mock_get):
        track_list = [
            {"title": f"Song {i}", "subtitle": "A", "uri": f"spotify:track:t{i}", "duration": 100}
            for i in range(MAX_PLAYLIST_TRACKS + 50)
        ]
        entity = {**SAMPLE_ENTITY, "trackList": track_list}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(entity)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        tracks = svc.get_playlist_tracks("abc")
        assert len(tracks) == MAX_PLAYLIST_TRACKS

    @patch("services.spotify_service.httpx.get")
    def test_caches_embed_data(self, mock_get):
        """Second call should use cached data, not make another HTTP request."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = _make_embed_html(SAMPLE_ENTITY)
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        svc.get_playlist_tracks("cached_id")
        svc.get_playlist_tracks("cached_id")
        assert mock_get.call_count == 1

    @patch("services.spotify_service.httpx.get")
    def test_malformed_html_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>No data here</body></html>"
        mock_get.return_value = mock_resp

        svc = SpotifyService()
        with pytest.raises(SpotifyServiceError, match="Could not extract"):
            svc.get_playlist_tracks("bad")
