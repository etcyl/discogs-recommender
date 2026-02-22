"""Tests for services/spotify_service.py."""
import pytest
from unittest.mock import MagicMock, patch

from services.spotify_service import SpotifyService, MAX_PLAYLIST_TRACKS


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

    @patch("services.spotify_service.spotipy.Spotify")
    def test_returns_info(self, mock_sp_cls):
        mock_sp = MagicMock()
        mock_sp.playlist.return_value = {
            "name": "Chill Hits",
            "description": "Relax",
            "owner": {"display_name": "Spotify"},
            "images": [{"url": "https://img.spotify.com/cover.jpg"}],
            "tracks": {"total": 80},
        }
        mock_sp_cls.return_value = mock_sp

        with patch("services.spotify_service.SpotifyClientCredentials"):
            svc = SpotifyService("id", "secret")
        svc.sp = mock_sp

        info = svc.get_playlist_info("abc123")
        assert info["name"] == "Chill Hits"
        assert info["track_count"] == 80
        assert info["playlist_id"] == "abc123"

    @patch("services.spotify_service.spotipy.Spotify")
    def test_no_images(self, mock_sp_cls):
        mock_sp = MagicMock()
        mock_sp.playlist.return_value = {
            "name": "Test",
            "owner": {"display_name": "User"},
            "images": [],
            "tracks": {"total": 5},
        }
        mock_sp_cls.return_value = mock_sp

        with patch("services.spotify_service.SpotifyClientCredentials"):
            svc = SpotifyService("id", "secret")
        svc.sp = mock_sp

        info = svc.get_playlist_info("xyz")
        assert info["image_url"] == ""


class TestGetPlaylistTracks:
    """Tests for get_playlist_tracks()."""

    @patch("services.spotify_service.spotipy.Spotify")
    def test_returns_tracks(self, mock_sp_cls):
        mock_sp = MagicMock()
        mock_sp.playlist_tracks.return_value = {
            "items": [
                {
                    "track": {
                        "name": "Song A",
                        "artists": [{"name": "Artist A"}],
                        "album": {"name": "Album A", "release_date": "2020-01-15"},
                        "duration_ms": 240000,
                        "id": "track1",
                    }
                },
                {
                    "track": {
                        "name": "Song B",
                        "artists": [{"name": "Artist B"}, {"name": "Artist C"}],
                        "album": {"name": "Album B", "release_date": "2019"},
                        "duration_ms": 180000,
                        "id": "track2",
                    }
                },
            ],
            "next": None,
        }
        mock_sp_cls.return_value = mock_sp

        with patch("services.spotify_service.SpotifyClientCredentials"):
            svc = SpotifyService("id", "secret")
        svc.sp = mock_sp

        tracks = svc.get_playlist_tracks("abc123")
        assert len(tracks) == 2
        assert tracks[0]["artist"] == "Artist A"
        assert tracks[0]["title"] == "Song A"
        assert tracks[0]["year"] == "2020"
        assert tracks[1]["all_artists"] == ["Artist B", "Artist C"]

    @patch("services.spotify_service.spotipy.Spotify")
    def test_skips_null_tracks(self, mock_sp_cls):
        mock_sp = MagicMock()
        mock_sp.playlist_tracks.return_value = {
            "items": [
                {"track": None},
                {"track": {"name": "", "artists": [], "album": {}, "duration_ms": 0, "id": ""}},
                {"track": {"name": "Real Song", "artists": [{"name": "A"}], "album": {"name": "B"}, "duration_ms": 100, "id": "x"}},
            ],
            "next": None,
        }
        mock_sp_cls.return_value = mock_sp

        with patch("services.spotify_service.SpotifyClientCredentials"):
            svc = SpotifyService("id", "secret")
        svc.sp = mock_sp

        tracks = svc.get_playlist_tracks("abc")
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Real Song"

    @patch("services.spotify_service.spotipy.Spotify")
    def test_respects_max_tracks_limit(self, mock_sp_cls):
        mock_sp = MagicMock()
        items = [
            {"track": {"name": f"Song {i}", "artists": [{"name": "A"}], "album": {"name": "B"}, "duration_ms": 100, "id": f"t{i}"}}
            for i in range(MAX_PLAYLIST_TRACKS + 50)
        ]
        mock_sp.playlist_tracks.return_value = {"items": items, "next": None}
        mock_sp_cls.return_value = mock_sp

        with patch("services.spotify_service.SpotifyClientCredentials"):
            svc = SpotifyService("id", "secret")
        svc.sp = mock_sp

        tracks = svc.get_playlist_tracks("abc")
        assert len(tracks) == MAX_PLAYLIST_TRACKS
