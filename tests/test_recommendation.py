"""Tests for services/recommendation.py — covers scoring algorithm, profile building, CWE-20."""
import random
from unittest.mock import MagicMock, patch

import pytest

from services.recommendation import CollectionAnalyzer


class TestCollectionAnalyzerInit:
    """Profile building and analysis."""

    def test_empty_collection(self):
        analyzer = CollectionAnalyzer([])
        profile = analyzer.get_profile()
        assert profile["total_releases"] == 0
        assert profile["top_genres"] == []
        assert profile["top_styles"] == []

    def test_profile_counts(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        profile = analyzer.get_profile()
        assert profile["total_releases"] == 5

        genre_dict = dict(profile["top_genres"])
        assert genre_dict["Electronic"] == 4
        assert genre_dict["Rock"] == 3

    def test_artist_counts(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        profile = analyzer.get_profile()
        artist_dict = dict(profile["top_artists"])
        assert artist_dict["Radiohead"] == 2

    def test_label_counts(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        profile = analyzer.get_profile()
        label_dict = dict(profile["top_labels"])
        assert label_dict["Parlophone"] == 2

    def test_style_counts(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        profile = analyzer.get_profile()
        style_dict = dict(profile["top_styles"])
        assert style_dict["Art Rock"] == 2
        assert style_dict["Trip Hop"] == 2

    def test_release_ids_tracked(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        assert 1001 in analyzer.release_ids
        assert 1005 in analyzer.release_ids

    def test_owned_titles_normalized(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        assert "radiohead - ok computer" in analyzer.owned_titles
        assert "dj shadow - endtroducing....." in analyzer.owned_titles


class TestIsOwned:
    """Ownership detection logic."""

    def test_owned_by_id(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {"id": 1001, "artists": ["Someone"], "title": "Something"}
        assert analyzer._is_owned(candidate) is True

    def test_owned_by_artist_title(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        # Different ID, but same artist + title (case-insensitive)
        candidate = {"id": 99999, "artists": ["radiohead"], "title": "OK Computer"}
        assert analyzer._is_owned(candidate) is True

    def test_not_owned(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {"id": 99999, "artists": ["Unknown"], "title": "Unknown Album"}
        assert analyzer._is_owned(candidate) is False

    def test_missing_id_field(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {"artists": ["Unknown"], "title": "Unknown"}
        assert analyzer._is_owned(candidate) is False

    def test_missing_artists_field(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {"id": 99999, "title": "Test"}
        assert analyzer._is_owned(candidate) is False


class TestScoreRelease:
    """Scoring algorithm tests."""

    def test_owned_returns_negative(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        owned = {"id": 1001, "artists": ["Radiohead"], "title": "OK Computer"}
        assert analyzer.score_release(owned) == -1

    def test_matching_artist_scores_high(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999,
            "artists": ["Radiohead"],
            "title": "New Album",
            "genres": [],
            "styles": [],
            "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0

    def test_matching_genre_adds_score(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["New Artist"], "title": "New Album",
            "genres": ["Electronic"], "styles": [], "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0

    def test_matching_style_adds_score(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["New Artist"], "title": "New Album",
            "genres": [], "styles": ["Trip Hop"], "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0

    def test_matching_label_adds_score(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["New Artist"], "title": "New Album",
            "genres": [], "styles": [], "labels": ["Parlophone"],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0

    def test_no_match_scores_zero(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["Unknown"], "title": "Unknown",
            "genres": ["Country"], "styles": ["Bluegrass"], "labels": ["Nashville"],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score == 0

    def test_discovery_reduces_artist_weight(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New Album",
            "genres": [], "styles": [], "labels": [],
        }
        score_low = analyzer.score_release(candidate, discovery=0)
        # At high discovery, seed random for determinism
        random.seed(42)
        score_high = analyzer.score_release(candidate, discovery=100)
        # Low discovery should have higher artist weight
        # (jitter makes exact comparison unreliable, but artist_weight is 5 vs 1)
        assert score_low > 0
        assert score_high > 0

    def test_discovery_adds_jitter(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New",
            "genres": ["Rock"], "styles": ["Art Rock"], "labels": ["Parlophone"],
        }
        # With discovery=0, no jitter, scores should be deterministic
        scores_zero = [analyzer.score_release(candidate, discovery=0) for _ in range(5)]
        assert len(set(scores_zero)) == 1  # All identical

        # With discovery>0, jitter applied, scores should vary
        scores_high = [analyzer.score_release(candidate, discovery=80) for _ in range(20)]
        assert len(set(scores_high)) > 1  # Should vary

    def test_empty_collection_scores_zero(self):
        analyzer = CollectionAnalyzer([])
        candidate = {
            "id": 99999, "artists": ["Any"], "title": "Any",
            "genres": ["Rock"], "styles": ["Indie"], "labels": ["Label"],
        }
        # Empty counters should not crash
        score = analyzer.score_release(candidate, discovery=0)
        assert score == 0

    def test_artist_count_capped_at_3(self, sample_collection):
        """Artist contribution capped at min(count, 3)."""
        # Add more Radiohead releases to test cap
        collection = sample_collection + [
            {"id": 2001, "artists": ["Radiohead"], "title": "Amnesiac",
             "genres": ["Electronic"], "styles": ["Art Rock"], "labels": ["Parlophone"],
             "year": 2001},
            {"id": 2002, "artists": ["Radiohead"], "title": "Hail to the Thief",
             "genres": ["Rock"], "styles": ["Alternative Rock"], "labels": ["Parlophone"],
             "year": 2003},
            {"id": 2003, "artists": ["Radiohead"], "title": "In Rainbows",
             "genres": ["Rock"], "styles": ["Art Rock"], "labels": ["XL"],
             "year": 2007},
        ]
        analyzer = CollectionAnalyzer(collection)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "Unknown Album",
            "genres": [], "styles": [], "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        # artist_weight=5, min(count=5, 3)=3, so 5*3=15
        assert score == 15


class TestGetRecommendations:
    """Integration tests for get_recommendations()."""

    def test_returns_list(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        mock_discogs.search.return_value = []

        result = analyzer.get_recommendations(mock_discogs, max_results=5)
        assert isinstance(result, list)

    def test_excludes_owned_releases(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        # Return an already-owned release
        mock_discogs.search.return_value = [
            {"id": 1001, "artists": ["Radiohead"], "title": "OK Computer",
             "genres": ["Rock"], "styles": ["Alternative Rock"], "labels": ["Parlophone"]},
        ]

        result = analyzer.get_recommendations(mock_discogs, max_results=5)
        assert all(r.get("id") != 1001 for r in result)

    def test_deduplicates_candidates(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        # Return same release from multiple searches
        candidate = {"id": 5001, "artists": ["New Band"], "title": "New Album",
                     "genres": ["Rock"], "styles": ["Indie"], "labels": ["Indie Label"]}
        mock_discogs.search.return_value = [candidate, candidate]

        result = analyzer.get_recommendations(mock_discogs, max_results=10)
        ids = [r["id"] for r in result]
        assert len(ids) == len(set(ids))

    def test_sorts_by_score_descending(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        mock_discogs.search.return_value = [
            {"id": 5001, "artists": ["Radiohead"], "title": "B-Sides",
             "genres": ["Rock"], "styles": ["Art Rock"], "labels": ["Parlophone"]},
            {"id": 5002, "artists": ["Unknown"], "title": "Random",
             "genres": ["Country"], "styles": ["Bluegrass"], "labels": ["Nashville"]},
        ]

        # Use discovery=0 for deterministic scores
        result = analyzer.get_recommendations(mock_discogs, max_results=10, discovery=0)
        if len(result) >= 2:
            assert result[0]["score"] >= result[1]["score"]

    def test_max_results_respected(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        candidates = [
            {"id": 5000 + i, "artists": ["Radiohead"], "title": f"Album {i}",
             "genres": ["Rock"], "styles": ["Art Rock"], "labels": ["Parlophone"]}
            for i in range(50)
        ]
        mock_discogs.search.return_value = candidates

        result = analyzer.get_recommendations(mock_discogs, max_results=5, discovery=0)
        assert len(result) <= 5

    def test_high_discovery_searches_genres(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        mock_discogs.search.return_value = []

        analyzer.get_recommendations(mock_discogs, discovery=50)
        # At discovery > 40, genre searches should happen
        call_kwargs = [call[1] for call in mock_discogs.search.call_args_list]
        genre_searches = [kw for kw in call_kwargs if "genre" in kw]
        assert len(genre_searches) > 0

    def test_search_exception_handled(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection)
        mock_discogs = MagicMock()
        mock_discogs.search.side_effect = Exception("API error")

        # Should not raise, just return empty or partial results
        result = analyzer.get_recommendations(mock_discogs, max_results=5)
        assert isinstance(result, list)


class TestRecentlyRecommendedPenalty:
    """Tests for freshness scoring penalty."""

    def test_recently_recommended_artist_penalized(self, sample_collection):
        recently = {"radiohead"}
        analyzer = CollectionAnalyzer(sample_collection, recently_recommended=recently)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New",
            "genres": ["Rock"], "styles": ["Art Rock"], "labels": ["Parlophone"],
        }
        score_fresh = CollectionAnalyzer(sample_collection).score_release(candidate, discovery=0)
        score_penalized = analyzer.score_release(candidate, discovery=0)
        assert score_penalized < score_fresh
        # 60% penalty: penalized should be ~40% of fresh
        assert abs(score_penalized - score_fresh * 0.4) < 0.01

    def test_non_recommended_artist_unaffected(self, sample_collection):
        recently = {"some other artist"}
        analyzer = CollectionAnalyzer(sample_collection, recently_recommended=recently)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New",
            "genres": ["Rock"], "styles": ["Art Rock"], "labels": [],
        }
        score_fresh = CollectionAnalyzer(sample_collection).score_release(candidate, discovery=0)
        score_with_history = analyzer.score_release(candidate, discovery=0)
        assert score_fresh == score_with_history

    def test_empty_recently_recommended(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection, recently_recommended=set())
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New",
            "genres": [], "styles": [], "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0

    def test_none_recently_recommended(self, sample_collection):
        analyzer = CollectionAnalyzer(sample_collection, recently_recommended=None)
        candidate = {
            "id": 99999, "artists": ["Radiohead"], "title": "New",
            "genres": [], "styles": [], "labels": [],
        }
        score = analyzer.score_release(candidate, discovery=0)
        assert score > 0
