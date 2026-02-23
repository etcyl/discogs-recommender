"""Tests for services/radio_service.py — covers CWE-20, CWE-502, CWE-400."""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.radio_service import RadioService


@pytest.fixture
def radio_svc():
    """Create a RadioService (no external clients needed)."""
    return RadioService(anthropic_api_key="sk-ant-test-key")


class TestGeneratePlaylist:
    """Tests for generate_playlist()."""

    @patch("services.radio_service.call_llm")
    def test_valid_playlist(self, mock_llm, radio_svc, sample_profile, sample_collection):
        playlist = [
            {"artist": f"Artist{i}", "title": f"Song{i}", "album": f"Album{i}",
             "year": 2000, "reason": "great", "similar_to": []}
            for i in range(40)
        ]
        mock_llm.return_value = json.dumps(playlist)

        result = radio_svc.generate_playlist(sample_profile, sample_collection)
        assert len(result) == 40

    @patch("services.radio_service.call_llm")
    def test_embedded_json(self, mock_llm, radio_svc, sample_profile, sample_collection):
        text = 'Sure, here is your playlist:\n[{"artist": "A", "title": "B", "album": "C", "year": 2020, "reason": "x", "similar_to": []}]'
        mock_llm.return_value = text

        result = radio_svc.generate_playlist(sample_profile, sample_collection)
        assert len(result) == 1

    @patch("services.radio_service.call_llm")
    def test_invalid_json_returns_empty(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "no json here"
        result = radio_svc.generate_playlist(sample_profile, sample_collection)
        assert result == []

    @patch("services.radio_service.call_llm")
    def test_thumbs_summary_included(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        radio_svc.generate_playlist(sample_profile, sample_collection, thumbs_summary="Radiohead - Creep")

        call_kwargs = mock_llm.call_args[1]
        assert "Radiohead - Creep" in call_kwargs["user_prompt"]

    @patch("services.radio_service.call_llm")
    def test_empty_thumbs(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        radio_svc.generate_playlist(sample_profile, sample_collection, thumbs_summary="")

        call_kwargs = mock_llm.call_args[1]
        assert "first session" in call_kwargs["user_prompt"]

    @patch("services.radio_service.call_llm")
    def test_ai_model_passed_through(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        radio_svc.generate_playlist(sample_profile, sample_collection, ai_model="ollama")

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "ollama"

    @patch("services.radio_service.call_llm")
    def test_default_ai_model_is_claude_sonnet(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        radio_svc.generate_playlist(sample_profile, sample_collection)

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "claude-sonnet"


class TestGeneratePlaylistFromTracks:
    """Tests for generate_playlist_from_tracks()."""

    @patch("services.radio_service.call_llm")
    def test_ai_model_passed_through(self, mock_llm, radio_svc):
        mock_llm.return_value = "[]"
        tracks = [{"artist": "A", "title": "B", "album": "C", "year": 2020}]
        radio_svc.generate_playlist_from_tracks(tracks, ai_model="claude-haiku")

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "claude-haiku"


class TestGenerateThemedPlaylist:
    """Tests for generate_themed_playlist()."""

    @patch("services.radio_service.call_llm")
    def test_ai_model_passed_through(self, mock_llm, radio_svc, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        radio_svc.generate_themed_playlist(sample_profile, sample_collection,
                                           "chill vibes", ai_model="ollama")

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "ollama"


class TestResolveYoutubeIds:
    """Tests for resolve_youtube_ids()."""

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_resolves_successfully(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        mock_search_instance = MagicMock()
        mock_search_instance.result.return_value = {
            "result": [
                {"id": "abc123", "thumbnails": [{"url": "thumb.jpg"}],
                 "duration": "3:45", "title": "Artist - Song"}
            ]
        }
        mock_search_cls.return_value = mock_search_instance

        playlist = [{"artist": "Test", "title": "Song", "album": "Album"}]
        result = radio_svc.resolve_youtube_ids(playlist)
        assert len(result) == 1
        assert result[0]["videoId"] == "abc123"

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_missing_video_filtered_out(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        mock_search_instance = MagicMock()
        mock_search_instance.result.return_value = {"result": []}
        mock_search_cls.return_value = mock_search_instance

        playlist = [{"artist": "Test", "title": "Song"}]
        result = radio_svc.resolve_youtube_ids(playlist)
        assert len(result) == 0

    @patch("services.radio_service.cache")
    def test_uses_cache(self, mock_cache, radio_svc):
        mock_cache.get.return_value = {
            "videoId": "cached123",
            "thumbnail": "cached_thumb.jpg",
            "duration": "4:00",
            "ytTitle": "Cached Song",
        }

        playlist = [{"artist": "Cached", "title": "Song"}]
        result = radio_svc.resolve_youtube_ids(playlist)
        assert len(result) == 1
        assert result[0]["videoId"] == "cached123"

    def test_empty_playlist(self, radio_svc):
        result = radio_svc.resolve_youtube_ids([])
        assert result == []

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_preserves_order(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        mock_search_instance = MagicMock()
        mock_search_instance.result.return_value = {
            "result": [
                {"id": "vid", "thumbnails": [{"url": "t.jpg"}],
                 "duration": "3:00", "title": "T"}
            ]
        }
        mock_search_cls.return_value = mock_search_instance

        playlist = [
            {"artist": f"Artist{i}", "title": f"Song{i}"} for i in range(5)
        ]
        result = radio_svc.resolve_youtube_ids(playlist)
        artists = [r["artist"] for r in result]
        assert artists == [f"Artist{i}" for i in range(5)]


class TestFindYoutubeVideo:
    """Tests for _find_youtube_video()."""

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_returns_video_info(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        mock_instance = MagicMock()
        mock_instance.result.return_value = {
            "result": [
                {"id": "xyz", "thumbnails": [{"url": "small.jpg"}, {"url": "large.jpg"}],
                 "duration": "5:30", "title": "Official Audio"}
            ]
        }
        mock_search_cls.return_value = mock_instance

        result = radio_svc._find_youtube_video("Artist", "Song")
        assert result["videoId"] == "xyz"
        assert result["thumbnail"] == "large.jpg"  # last thumbnail

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_fallback_query(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        # First search returns empty, fallback search returns result
        mock_empty = MagicMock()
        mock_empty.result.return_value = {"result": []}
        mock_found = MagicMock()
        mock_found.result.return_value = {
            "result": [{"id": "fallback", "thumbnails": [], "duration": "3:00", "title": "F"}]
        }
        mock_search_cls.side_effect = [mock_empty, mock_found]

        result = radio_svc._find_youtube_video("Artist", "Song")
        assert result["videoId"] == "fallback"

    @patch("services.radio_service.VideosSearch")
    @patch("services.radio_service.cache")
    def test_exception_returns_none(self, mock_cache, mock_search_cls, radio_svc):
        mock_cache.get.return_value = None
        mock_search_cls.side_effect = Exception("Network error")

        result = radio_svc._find_youtube_video("Artist", "Song")
        assert result is None

    @patch("services.radio_service.cache")
    def test_cached_result_returned(self, mock_cache, radio_svc):
        mock_cache.get.return_value = {"videoId": "cached", "thumbnail": "", "duration": "", "ytTitle": ""}
        result = radio_svc._find_youtube_video("Artist", "Song")
        assert result["videoId"] == "cached"


class TestBuildProfileSummary:
    """Tests for _build_profile_summary()."""

    def test_includes_profile_data(self, radio_svc, sample_profile, sample_collection):
        summary = radio_svc._build_profile_summary(sample_profile, sample_collection)
        assert "Total releases: 5" in summary
        assert "Radiohead" in summary

    def test_limits_sample_releases(self, radio_svc, sample_profile):
        big_collection = [
            {"id": i, "artists": [f"A{i}"], "title": f"T{i}", "year": 2000}
            for i in range(100)
        ]
        summary = radio_svc._build_profile_summary(sample_profile, big_collection)
        assert "A29" in summary
        assert "A30" not in summary
