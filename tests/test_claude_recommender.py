"""Tests for services/claude_recommender.py — covers CWE-20, CWE-502, CWE-209."""
import json
from unittest.mock import MagicMock, patch
from concurrent.futures import Future

import pytest

from services.claude_recommender import ClaudeRecommender


@pytest.fixture
def recommender():
    """Create a ClaudeRecommender with mocked Anthropic client."""
    with patch("services.claude_recommender.anthropic.Anthropic") as mock_cls:
        rec = ClaudeRecommender(api_key="sk-ant-test-key")
        yield rec


@pytest.fixture
def mock_claude_response():
    """Factory to create mock Claude API responses."""
    def _make(text: str):
        mock_msg = MagicMock()
        mock_content = MagicMock()
        mock_content.text = text
        mock_msg.content = [mock_content]
        return mock_msg
    return _make


class TestGetRecommendations:
    """Tests for get_recommendations()."""

    def test_valid_json_response(self, recommender, mock_claude_response, sample_profile, sample_collection):
        valid_json = json.dumps([
            {"artist": "Boards of Canada", "album": "Music Has the Right to Children",
             "year": 1998, "reason": "Ambient electronic", "genres": ["Electronic"],
             "styles": ["IDM"], "tracks": [{"title": "Roygbiv", "reason": "dreamy synths"}]}
        ])
        recommender.client.messages.create.return_value = mock_claude_response(valid_json)

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 1
        assert result[0]["artist"] == "Boards of Canada"

    def test_json_with_surrounding_text(self, recommender, mock_claude_response, sample_profile, sample_collection):
        """CWE-502: Handle Claude returning JSON embedded in prose."""
        text = 'Here are my recommendations:\n[{"artist": "Test", "album": "A", "year": 2020, "reason": "x", "genres": [], "styles": [], "tracks": []}]\nHope you enjoy!'
        recommender.client.messages.create.return_value = mock_claude_response(text)

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 1

    def test_invalid_json_returns_empty(self, recommender, mock_claude_response, sample_profile, sample_collection):
        recommender.client.messages.create.return_value = mock_claude_response("not json at all")
        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert result == []

    def test_empty_response_returns_empty(self, recommender, mock_claude_response, sample_profile, sample_collection):
        recommender.client.messages.create.return_value = mock_claude_response("")
        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert result == []

    def test_multiple_recommendations(self, recommender, mock_claude_response, sample_profile, sample_collection):
        recs = [
            {"artist": f"Artist{i}", "album": f"Album{i}", "year": 2000 + i,
             "reason": "test", "genres": ["Rock"], "styles": ["Indie"],
             "tracks": [{"title": "Song", "reason": "good"}]}
            for i in range(15)
        ]
        recommender.client.messages.create.return_value = mock_claude_response(json.dumps(recs))

        result = recommender.get_recommendations(sample_profile, sample_collection)
        assert len(result) == 15

    def test_preferences_included_in_prompt(self, recommender, mock_claude_response, sample_profile, sample_collection):
        recommender.client.messages.create.return_value = mock_claude_response("[]")

        recommender.get_recommendations(sample_profile, sample_collection, preferences="I love jazz")
        call_args = recommender.client.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        assert "I love jazz" in prompt_text


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
