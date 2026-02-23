"""Tests for services/claude_recommender.py — covers CWE-20, CWE-502, CWE-209."""
import json
from unittest.mock import MagicMock, patch
from concurrent.futures import Future

import pytest

from services.claude_recommender import ClaudeRecommender


@pytest.fixture
def recommender():
    """Create a ClaudeRecommender (no external clients needed)."""
    return ClaudeRecommender(api_key="sk-ant-test-key")


class TestGetRecommendations:
    """Tests for get_recommendations()."""

    @patch("services.claude_recommender.call_llm")
    def test_valid_json_response(self, mock_llm, recommender, sample_profile, sample_collection):
        valid_json = json.dumps([
            {"artist": "Boards of Canada", "album": "Music Has the Right to Children",
             "year": 1998, "reason": "Ambient electronic", "genres": ["Electronic"],
             "styles": ["IDM"], "tracks": [{"title": "Roygbiv", "reason": "dreamy synths"}]}
        ])
        mock_llm.return_value = valid_json

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 1
        assert result[0]["artist"] == "Boards of Canada"

    @patch("services.claude_recommender.call_llm")
    def test_json_with_surrounding_text(self, mock_llm, recommender, sample_profile, sample_collection):
        """CWE-502: Handle LLM returning JSON embedded in prose."""
        text = 'Here are my recommendations:\n[{"artist": "Test", "album": "A", "year": 2020, "reason": "x", "genres": [], "styles": [], "tracks": []}]\nHope you enjoy!'
        mock_llm.return_value = text

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 1

    @patch("services.claude_recommender.call_llm")
    def test_invalid_json_returns_empty(self, mock_llm, recommender, sample_profile, sample_collection):
        mock_llm.return_value = "not json at all"
        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert result == []

    @patch("services.claude_recommender.call_llm")
    def test_empty_response_returns_empty(self, mock_llm, recommender, sample_profile, sample_collection):
        mock_llm.return_value = ""
        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert result == []

    @patch("services.claude_recommender.call_llm")
    def test_multiple_recommendations(self, mock_llm, recommender, sample_profile, sample_collection):
        recs = [
            {"artist": f"Artist{i}", "album": f"Album{i}", "year": 2000 + i,
             "reason": "test", "genres": ["Rock"], "styles": ["Indie"],
             "tracks": [{"title": "Song", "reason": "good"}]}
            for i in range(15)
        ]
        mock_llm.return_value = json.dumps(recs)

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 15

    @patch("services.claude_recommender.call_llm")
    def test_preferences_included_in_prompt(self, mock_llm, recommender, sample_profile, sample_collection):
        mock_llm.return_value = "[]"

        recommender.get_recommendations(sample_profile, sample_collection, preferences="I love jazz")
        call_kwargs = mock_llm.call_args[1]
        assert "I love jazz" in call_kwargs["user_prompt"]

    @patch("services.claude_recommender.call_llm")
    def test_ai_model_passed_through(self, mock_llm, recommender, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        recommender.get_recommendations(sample_profile, sample_collection, ai_model="ollama")

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "ollama"

    @patch("services.claude_recommender.call_llm")
    def test_default_ai_model_is_claude_sonnet(self, mock_llm, recommender, sample_profile, sample_collection):
        mock_llm.return_value = "[]"
        recommender.get_recommendations(sample_profile, sample_collection)

        call_kwargs = mock_llm.call_args[1]
        assert call_kwargs["provider"] == "claude-sonnet"


class TestEnrichWithDiscogs:
    """Tests for enrich_with_discogs()."""

    def test_enriches_found_results(self, recommender):
        recs = [
            {"artist": "Radiohead", "album": "OK Computer"},
            {"artist": "Unknown", "album": "Nothing"},
        ]
        mock_discogs = MagicMock()
        mock_discogs.search.side_effect = [
            [{"id": 1, "title": "OK Computer"}],  # master search
            [],  # no master for Unknown
            [],  # no release for Unknown
        ]

        result = recommender.enrich_with_discogs(recs, mock_discogs)
        assert result[0]["discogs_match"] is not None
        assert result[0]["discogs_match"]["id"] == 1

    def test_enrichment_handles_search_exception(self, recommender):
        recs = [{"artist": "Test", "album": "Album"}]
        mock_discogs = MagicMock()
        mock_discogs.search.side_effect = Exception("API error")

        result = recommender.enrich_with_discogs(recs, mock_discogs)
        assert result[0]["discogs_match"] is None

    def test_empty_recommendations_list(self, recommender):
        mock_discogs = MagicMock()
        result = recommender.enrich_with_discogs([], mock_discogs)
        assert result == []

    def test_fallback_to_release_type(self, recommender):
        recs = [{"artist": "Test", "album": "Album"}]
        mock_discogs = MagicMock()
        mock_discogs.search.side_effect = [
            [],  # no master
            [{"id": 42, "title": "Album"}],  # found as release
        ]

        result = recommender.enrich_with_discogs(recs, mock_discogs)
        assert result[0]["discogs_match"]["id"] == 42


class TestBuildSummary:
    """Tests for _build_summary()."""

    def test_includes_all_profile_sections(self, recommender, sample_profile, sample_collection):
        summary = recommender._build_summary(sample_profile, sample_collection)
        assert "Total releases" in summary
        assert "Top genres" in summary
        assert "Top styles" in summary
        assert "Top artists" in summary
        assert "Top labels" in summary

    def test_includes_sample_releases(self, recommender, sample_profile, sample_collection):
        summary = recommender._build_summary(sample_profile, sample_collection)
        assert "Radiohead" in summary
        assert "OK Computer" in summary

    def test_limits_to_30_releases(self, recommender, sample_profile):
        big_collection = [
            {"id": i, "artists": [f"Artist{i}"], "title": f"Album{i}", "year": 2000}
            for i in range(100)
        ]
        summary = recommender._build_summary(sample_profile, big_collection)
        # Should only include first 30
        assert "Artist29" in summary
        assert "Artist30" not in summary

    def test_empty_collection(self, recommender, sample_profile):
        summary = recommender._build_summary(sample_profile, [])
        assert "Sample releases" in summary
