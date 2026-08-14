"""Tests for token-free (public) Discogs collection access.

A Discogs collection its owner has left public is readable over the plain REST
API with only a User-Agent. The app used to refuse all collection features
without a token; public mode removes that requirement for the parts that
genuinely don't need it, and fails clearly for the parts that do.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.discogs_service import DiscogsService, PublicCollectionError


def _page(items, pages=1, total=None):
    return {
        "pagination": {"page": 1, "pages": pages, "items": total or len(items)},
        "releases": items,
    }


def _release(rid=1, title="Kid A", artist="Radiohead", year=2000):
    return {
        "date_added": "2024-01-02T03:04:05-08:00",
        "basic_information": {
            "id": rid, "title": title, "year": year,
            "artists": [{"name": artist}],
            "genres": ["Electronic", "Rock"],
            "styles": ["Experimental"],
            "labels": [{"name": "Parlophone"}],
            "formats": [{"name": "Vinyl"}],
            "thumb": "t.jpg", "cover_image": "c.jpg",
        },
    }


class TestPublicMode:
    def test_no_token_enables_public_mode(self):
        svc = DiscogsService("App/1.0", "", "etcyl")
        assert svc.public_mode is True
        assert svc.client is None

    def test_token_uses_the_authenticated_client(self):
        with patch("services.discogs_service.discogs_client.Client") as mock_client:
            svc = DiscogsService("App/1.0", "sometoken", "etcyl")
        assert svc.public_mode is False
        assert mock_client.called


class TestPublicCollection:
    @patch("httpx.get")
    def test_reads_a_public_collection(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: _page([_release()]))
        svc = DiscogsService("App/1.0", "", "etcyl")
        result = svc.get_collection_page()
        assert result["total"] == 1
        item = result["items"][0]
        assert item["title"] == "Kid A"
        assert item["artists"] == ["Radiohead"]
        assert item["genres"] == ["Electronic", "Rock"]
        assert item["url"].endswith("/release/1")

    @patch("httpx.get")
    def test_sends_a_user_agent(self, mock_get):
        """Discogs rejects requests without one."""
        mock_get.return_value = MagicMock(status_code=200, json=lambda: _page([]))
        DiscogsService("App/1.0", "", "etcyl").get_collection_page()
        assert mock_get.call_args.kwargs["headers"]["User-Agent"] == "App/1.0"

    @patch("time.sleep")
    @patch("httpx.get")
    def test_paginates_the_whole_collection(self, mock_get, _sleep):
        mock_get.side_effect = [
            MagicMock(status_code=200,
                      json=lambda: _page([_release(1), _release(2)], pages=2, total=3)),
            MagicMock(status_code=200,
                      json=lambda: _page([_release(3)], pages=2, total=3)),
        ]
        svc = DiscogsService("App/1.0", "", "etcyl")
        assert len(svc.get_full_collection()) == 3

    @patch("time.sleep")
    @patch("httpx.get")
    def test_paces_requests_between_pages(self, mock_get, mock_sleep):
        """Unauthenticated callers get a smaller rate budget."""
        mock_get.side_effect = [
            MagicMock(status_code=200, json=lambda: _page([_release(1)], pages=2)),
            MagicMock(status_code=200, json=lambda: _page([_release(2)], pages=2)),
        ]
        DiscogsService("App/1.0", "", "etcyl").get_full_collection()
        assert mock_sleep.called

    @patch("httpx.get")
    def test_missing_user_is_explained(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404, json=lambda: {})
        svc = DiscogsService("App/1.0", "", "nosuchuser")
        with pytest.raises(PublicCollectionError, match="no public collection"):
            svc.get_collection_page()

    @patch("httpx.get")
    def test_private_collection_is_explained(self, mock_get):
        mock_get.return_value = MagicMock(status_code=403, json=lambda: {})
        svc = DiscogsService("App/1.0", "", "someone")
        with pytest.raises(PublicCollectionError, match="not public"):
            svc.get_collection_page()

    @patch("time.sleep")
    @patch("httpx.get")
    def test_retries_on_rate_limit(self, mock_get, _sleep):
        mock_get.side_effect = [
            MagicMock(status_code=429, json=lambda: {}),
            MagicMock(status_code=200, json=lambda: _page([_release()])),
        ]
        svc = DiscogsService("App/1.0", "", "etcyl")
        assert svc.get_collection_page()["total"] == 1

    @patch("time.sleep")
    @patch("httpx.get", side_effect=Exception("no network"))
    def test_unreachable_api_raises_a_clear_error(self, _get, _sleep):
        svc = DiscogsService("App/1.0", "", "etcyl")
        with pytest.raises(PublicCollectionError, match="Could not reach"):
            svc.get_collection_page()


class TestPublicRelease:
    @patch("httpx.get")
    def test_serializes_release_detail(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {
            "id": 42, "title": "Aja", "year": 1977,
            "artists": [{"name": "Steely Dan"}],
            "genres": ["Jazz"], "styles": ["Jazz-Rock"],
            "labels": [{"name": "ABC"}], "formats": [{"name": "Vinyl"}],
            "tracklist": [{"position": "A1", "title": "Black Cow", "duration": "5:10"}],
            "images": [{"uri": "big.jpg"}], "country": "US",
        })
        svc = DiscogsService("App/1.0", "", "etcyl")
        rel = svc.get_release_details(42)
        assert rel["title"] == "Aja"
        assert rel["artists"] == ["Steely Dan"]
        assert rel["tracklist"][0]["title"] == "Black Cow"

    def test_still_validates_the_release_id(self):
        svc = DiscogsService("App/1.0", "", "etcyl")
        with pytest.raises(ValueError):
            svc.get_release_details("42; DROP TABLE")


class TestPublicSearchIsRefused:
    def test_search_explains_that_it_needs_a_token(self):
        """Catalogue search is the one thing Discogs requires auth for."""
        svc = DiscogsService("App/1.0", "", "etcyl")
        with pytest.raises(PublicCollectionError, match="DISCOGS_TOKEN"):
            svc.search(artist="Pixies")


class TestSettings:
    def test_username_alone_counts_as_configured(self):
        from config import Settings
        s = Settings(discogs_username="etcyl", discogs_token="")
        assert s.discogs_configured is True
        assert s.discogs_public_mode is True

    def test_token_and_username_is_not_public_mode(self):
        from config import Settings
        s = Settings(discogs_username="etcyl", discogs_token="a-real-token-value")
        assert s.discogs_configured is True
        assert s.discogs_public_mode is False

    def test_no_username_is_unconfigured(self):
        from config import Settings
        s = Settings(discogs_username="", discogs_token="")
        assert s.discogs_configured is False
        assert s.discogs_public_mode is False
