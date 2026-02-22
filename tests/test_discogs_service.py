"""Tests for services/discogs_service.py — covers CWE-20, CWE-400, CWE-209."""
from unittest.mock import MagicMock, patch, PropertyMock
import time

import pytest

from services.discogs_service import DiscogsService, _sanitize_search_input, MAX_PER_PAGE


class TestSanitizeSearchInput:
    """Input sanitization — CWE-20."""

    def test_none_returns_none(self):
        assert _sanitize_search_input(None) is None

    def test_normal_string(self):
        assert _sanitize_search_input("Radiohead") == "Radiohead"

    def test_strips_whitespace(self):
        assert _sanitize_search_input("  hello  ") == "hello"

    def test_removes_control_characters(self):
        result = _sanitize_search_input("test\x00\x01\x02value")
        assert "\x00" not in result
        assert "\x01" not in result

    def test_truncates_long_input(self):
        result = _sanitize_search_input("x" * 500)
        assert len(result) == 200

    def test_empty_string_returns_none(self):
        assert _sanitize_search_input("") is None

    def test_whitespace_only_returns_none(self):
        assert _sanitize_search_input("   ") is None

    def test_non_string_returns_none(self):
        assert _sanitize_search_input(123) is None  # type: ignore


class TestDiscogsServiceInit:
    """Constructor tests."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_init_creates_client(self, mock_client_cls):
        svc = DiscogsService("TestApp/1.0", "token123", "testuser")
        mock_client_cls.assert_called_once_with("TestApp/1.0", user_token="token123")
        assert svc.username == "testuser"


class TestGetReleaseDetails:
    """Tests for get_release_details() — CWE-20."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_negative_id_raises(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        with pytest.raises(ValueError, match="positive integer"):
            svc.get_release_details(-1)

    @patch("services.discogs_service.discogs_client.Client")
    def test_zero_id_raises(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        with pytest.raises(ValueError, match="positive integer"):
            svc.get_release_details(0)

    @patch("services.discogs_service.discogs_client.Client")
    def test_valid_id(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_release = MagicMock()
        mock_release.id = 12345
        mock_release.title = "Test Album"
        mock_release.artists = []
        mock_release.labels = []
        mock_release.tracklist = []
        mock_release.images = None
        mock_release.year = 2020
        mock_release.genres = ["Rock"]
        mock_release.styles = ["Indie"]
        mock_release.formats = []
        mock_release.thumb = ""
        mock_release.country = "US"
        mock_release.notes = ""
        mock_release.num_for_sale = 5
        mock_release.lowest_price = 10.99
        svc.client.release.return_value = mock_release

        result = svc.get_release_details(12345)
        assert result["id"] == 12345
        assert result["title"] == "Test Album"
        assert result["url"] == "https://www.discogs.com/release/12345"


class TestSearch:
    """Tests for search() — CWE-20, CWE-400."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_sanitizes_inputs(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.return_value = []
        svc.client.search.return_value = mock_results

        svc.search(query="test\x00value", artist="\x01bad")
        call_kwargs = svc.client.search.call_args[1]
        assert "\x00" not in call_kwargs.get("q", "")

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_invalid_type_defaults_to_release(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.return_value = []
        svc.client.search.return_value = mock_results

        svc.search(query="test", type="invalid_type")
        call_kwargs = svc.client.search.call_args[1]
        assert call_kwargs["type"] == "release"

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_per_page_clamped(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.return_value = []
        svc.client.search.return_value = mock_results

        svc.search(query="test", per_page=500)
        assert mock_results.per_page == MAX_PER_PAGE

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_page_clamped_to_min_1(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.return_value = []
        svc.client.search.return_value = mock_results

        svc.search(query="test", page=-5)
        mock_results.page.assert_called_with(1)

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_exception_returns_empty(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.side_effect = Exception("API error")
        svc.client.search.return_value = mock_results

        result = svc.search(query="test")
        assert result == []

    @patch("services.discogs_service.discogs_client.Client")
    def test_search_only_non_none_params(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_results = MagicMock()
        mock_results.page.return_value = []
        svc.client.search.return_value = mock_results

        svc.search(artist="Radiohead")
        call_kwargs = svc.client.search.call_args[1]
        assert "q" not in call_kwargs
        assert call_kwargs["artist"] == "Radiohead"


class TestSerializeSearchResult:
    """Tests for _serialize_search_result()."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_parse_artist_title_format(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_item = MagicMock()
        mock_item.data = {
            "id": 999,
            "title": "Radiohead - OK Computer",
            "year": "1997",
            "genre": ["Rock"],
            "style": ["Alternative Rock"],
            "label": ["Parlophone"],
            "format": ["Vinyl"],
            "thumb": "thumb.jpg",
            "cover_image": "cover.jpg",
            "uri": "/release/999",
            "type": "release",
        }

        result = svc._serialize_search_result(mock_item)
        assert result["artists"] == ["Radiohead"]
        assert result["title"] == "OK Computer"
        assert result["url"] == "https://www.discogs.com/release/999"

    @patch("services.discogs_service.discogs_client.Client")
    def test_parse_no_dash_in_title(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_item = MagicMock()
        mock_item.data = {"title": "Just A Title", "id": 1, "uri": ""}

        result = svc._serialize_search_result(mock_item)
        assert result["artists"] == []
        assert result["title"] == "Just A Title"


class TestSerializeCollectionItem:
    """Tests for _serialize_collection_item()."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_serialize_basic_info(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_item = MagicMock()
        mock_item.data = {
            "basic_information": {
                "id": 1001,
                "title": "OK Computer",
                "year": 1997,
                "artists": [{"name": "Radiohead"}],
                "genres": ["Rock"],
                "styles": ["Alternative Rock"],
                "labels": [{"name": "Parlophone"}],
                "formats": [{"name": "Vinyl"}],
                "thumb": "thumb.jpg",
                "cover_image": "cover.jpg",
            },
            "date_added": "2024-01-01T00:00:00",
        }

        result = svc._serialize_collection_item(mock_item)
        assert result["id"] == 1001
        assert result["title"] == "OK Computer"
        assert result["artists"] == ["Radiohead"]
        assert result["genres"] == ["Rock"]
        assert result["url"] == "https://www.discogs.com/release/1001"

    @patch("services.discogs_service.discogs_client.Client")
    def test_serialize_missing_fields(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_item = MagicMock()
        mock_item.data = {"basic_information": {}}

        result = svc._serialize_collection_item(mock_item)
        assert result["title"] == ""
        assert result["artists"] == []
        assert result["genres"] == []


class TestRateLimitedCall:
    """Tests for _rate_limited_call() — CWE-400."""

    @patch("services.discogs_service.discogs_client.Client")
    @patch("services.discogs_service.time.sleep")
    def test_retries_on_429(self, mock_sleep, mock_client_cls):
        from discogs_client.exceptions import HTTPError

        svc = DiscogsService("TestApp", "token", "user")
        mock_func = MagicMock()
        error = HTTPError("Rate limited", 429)
        mock_func.side_effect = [error, "success"]

        result = svc._rate_limited_call(mock_func)
        assert result == "success"
        assert mock_func.call_count == 2
        mock_sleep.assert_called_once()

    @patch("services.discogs_service.discogs_client.Client")
    @patch("services.discogs_service.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep, mock_client_cls):
        from discogs_client.exceptions import HTTPError

        svc = DiscogsService("TestApp", "token", "user")
        mock_func = MagicMock()
        error = HTTPError("Rate limited", 429)
        mock_func.side_effect = [error, error, error]

        with pytest.raises(HTTPError):
            svc._rate_limited_call(mock_func)

    @patch("services.discogs_service.discogs_client.Client")
    def test_raises_non_429_immediately(self, mock_client_cls):
        from discogs_client.exceptions import HTTPError

        svc = DiscogsService("TestApp", "token", "user")
        mock_func = MagicMock()
        error = HTTPError("Not Found", 404)
        mock_func.side_effect = error

        with pytest.raises(HTTPError):
            svc._rate_limited_call(mock_func)
        assert mock_func.call_count == 1

    @patch("services.discogs_service.discogs_client.Client")
    @patch("services.discogs_service.time.sleep")
    def test_exponential_backoff_timing(self, mock_sleep, mock_client_cls):
        from discogs_client.exceptions import HTTPError

        svc = DiscogsService("TestApp", "token", "user")
        mock_func = MagicMock()
        error = HTTPError("Rate limited", 429)
        mock_func.side_effect = [error, error, "success"]

        svc._rate_limited_call(mock_func)
        # First retry waits 2^1=2, second waits 2^2=4
        assert mock_sleep.call_args_list[0][0][0] == 2
        assert mock_sleep.call_args_list[1][0][0] == 4


class TestSerializeRelease:
    """Tests for _serialize_release() error handling."""

    @patch("services.discogs_service.discogs_client.Client")
    def test_handles_artists_exception(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_release = MagicMock()
        mock_release.id = 1
        mock_release.title = "Test"
        type(mock_release).artists = PropertyMock(side_effect=Exception("API error"))
        mock_release.labels = []
        mock_release.tracklist = []
        mock_release.images = None

        result = svc._serialize_release(mock_release)
        assert result["artists"] == []

    @patch("services.discogs_service.discogs_client.Client")
    def test_handles_tracklist_exception(self, mock_client_cls):
        svc = DiscogsService("TestApp", "token", "user")
        mock_release = MagicMock()
        mock_release.id = 1
        mock_release.title = "Test"
        mock_release.artists = []
        mock_release.labels = []
        type(mock_release).tracklist = PropertyMock(side_effect=Exception("API error"))
        mock_release.images = None

        result = svc._serialize_release(mock_release)
        assert result["tracklist"] == []
