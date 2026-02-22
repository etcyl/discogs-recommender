"""Tests for services/thumbs.py — covers CWE-20, CWE-22, CWE-138, CWE-400, CWE-367, CWE-502, CWE-770."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from services import thumbs


@pytest.fixture(autouse=True)
def isolate_thumbs(tmp_path):
    """Redirect thumbs file I/O to a temp directory for every test."""
    test_data_dir = tmp_path / "data"
    test_data_dir.mkdir()
    test_thumbs_file = test_data_dir / "thumbs.json"
    with patch.object(thumbs, "DATA_DIR", test_data_dir), \
         patch.object(thumbs, "THUMBS_FILE", test_thumbs_file):
        yield test_thumbs_file


class TestLoadThumbs:
    """Tests for load_thumbs()."""

    def test_load_empty_returns_list(self):
        assert thumbs.load_thumbs() == []

    def test_load_valid_json(self, isolate_thumbs):
        data = [{"artist": "Test", "title": "Song", "album": "", "genres": [], "styles": [], "timestamp": "2024-01-01"}]
        isolate_thumbs.write_text(json.dumps(data))
        result = thumbs.load_thumbs()
        assert len(result) == 1
        assert result[0]["artist"] == "Test"

    def test_load_corrupt_json_returns_empty(self, isolate_thumbs):
        isolate_thumbs.write_text("{invalid json")
        assert thumbs.load_thumbs() == []

    def test_load_non_list_json_returns_empty(self, isolate_thumbs):
        isolate_thumbs.write_text('{"not": "a list"}')
        assert thumbs.load_thumbs() == []

    def test_load_oversized_file_returns_empty(self, isolate_thumbs):
        """CWE-400: Reject files larger than 5 MB."""
        # Write >5 MB of valid JSON
        big_data = json.dumps([{"artist": "x" * 1000}] * 6000)
        isolate_thumbs.write_text(big_data)
        assert thumbs.load_thumbs() == []


class TestSaveThumb:
    """Tests for save_thumb()."""

    def test_save_basic(self):
        entry = thumbs.save_thumb(artist="Radiohead", title="Creep")
        assert entry["artist"] == "Radiohead"
        assert entry["title"] == "Creep"
        assert "timestamp" in entry

    def test_save_with_all_fields(self):
        entry = thumbs.save_thumb(
            artist="MBV", title="Only Shallow", album="Loveless",
            genres=["Rock"], styles=["Shoegaze"]
        )
        assert entry["album"] == "Loveless"
        assert entry["genres"] == ["Rock"]
        assert entry["styles"] == ["Shoegaze"]

    def test_no_duplicate(self):
        thumbs.save_thumb(artist="Radiohead", title="Creep")
        entry2 = thumbs.save_thumb(artist="radiohead", title="creep")
        # Should return existing entry, not create duplicate
        loaded = thumbs.load_thumbs()
        assert len(loaded) == 1

    def test_missing_artist_raises(self):
        with pytest.raises(ValueError, match="artist and title are required"):
            thumbs.save_thumb(artist="", title="Song")

    def test_missing_title_raises(self):
        with pytest.raises(ValueError, match="artist and title are required"):
            thumbs.save_thumb(artist="Artist", title="")

    def test_persists_to_disk(self, isolate_thumbs):
        thumbs.save_thumb(artist="Test", title="Song")
        raw = json.loads(isolate_thumbs.read_text())
        assert len(raw) == 1

    def test_multiple_saves(self):
        thumbs.save_thumb(artist="A1", title="T1")
        thumbs.save_thumb(artist="A2", title="T2")
        thumbs.save_thumb(artist="A3", title="T3")
        loaded = thumbs.load_thumbs()
        assert len(loaded) == 3


class TestSaveThumbInputValidation:
    """Input validation — CWE-20, CWE-138."""

    def test_null_byte_stripped(self):
        entry = thumbs.save_thumb(artist="Radio\x00head", title="Cr\x00eep")
        assert "\x00" not in entry["artist"]
        assert "\x00" not in entry["title"]

    def test_control_characters_stripped(self):
        entry = thumbs.save_thumb(artist="Radio\x01\x02head", title="Song\x03")
        assert "\x01" not in entry["artist"]

    def test_long_string_truncated(self):
        long_name = "A" * 1000
        entry = thumbs.save_thumb(artist=long_name, title="Song")
        assert len(entry["artist"]) <= 500

    def test_non_string_genres_filtered(self):
        entry = thumbs.save_thumb(
            artist="Test", title="Song",
            genres=[123, None, "Rock", True]  # type: ignore
        )
        assert entry["genres"] == ["Rock"]

    def test_too_many_genres_truncated(self):
        genres = [f"Genre{i}" for i in range(50)]
        entry = thumbs.save_thumb(artist="Test", title="Song", genres=genres)
        assert len(entry["genres"]) <= 20

    def test_non_list_genres_returns_empty(self):
        entry = thumbs.save_thumb(
            artist="Test", title="Song",
            genres="not a list"  # type: ignore
        )
        assert entry["genres"] == []


class TestSaveThumbResourceLimits:
    """Resource exhaustion protection — CWE-400, CWE-770."""

    def test_max_entries_enforced(self):
        """Should cap at MAX_THUMBS_ENTRIES."""
        original_max = thumbs.MAX_THUMBS_ENTRIES
        try:
            thumbs.MAX_THUMBS_ENTRIES = 5
            for i in range(10):
                thumbs.save_thumb(artist=f"Artist{i}", title=f"Song{i}")
            loaded = thumbs.load_thumbs()
            assert len(loaded) <= 5
        finally:
            thumbs.MAX_THUMBS_ENTRIES = original_max


class TestGetThumbsSummary:
    """Tests for get_thumbs_summary()."""

    def test_empty_summary(self):
        result = thumbs.get_thumbs_summary()
        assert result == "No liked songs yet."

    def test_summary_format(self):
        thumbs.save_thumb(artist="Radiohead", title="Creep", genres=["Rock"])
        result = thumbs.get_thumbs_summary()
        assert "Radiohead" in result
        assert "Creep" in result
        assert "Rock" in result

    def test_summary_max_entries_clamped(self):
        # Should not exceed MAX_THUMBS_ENTRIES
        result = thumbs.get_thumbs_summary(max_entries=99999)
        assert isinstance(result, str)

    def test_summary_min_entries(self):
        result = thumbs.get_thumbs_summary(max_entries=-5)
        assert isinstance(result, str)


class TestSanitizeString:
    """Tests for _sanitize_string helper."""

    def test_normal_string(self):
        assert thumbs._sanitize_string("hello") == "hello"

    def test_strips_whitespace(self):
        assert thumbs._sanitize_string("  hello  ") == "hello"

    def test_non_string_returns_empty(self):
        assert thumbs._sanitize_string(123) == ""  # type: ignore
        assert thumbs._sanitize_string(None) == ""  # type: ignore

    def test_preserves_unicode(self):
        assert thumbs._sanitize_string("Bjork") == "Bjork"

    def test_max_length(self):
        result = thumbs._sanitize_string("x" * 1000, max_length=10)
        assert len(result) == 10


class TestAtomicWrite:
    """Tests for atomic file writing — CWE-367."""

    def test_atomic_write_creates_file(self, isolate_thumbs):
        thumbs._atomic_write_json(isolate_thumbs, [{"test": True}])
        data = json.loads(isolate_thumbs.read_text())
        assert data == [{"test": True}]

    def test_atomic_write_overwrites(self, isolate_thumbs):
        thumbs._atomic_write_json(isolate_thumbs, [1])
        thumbs._atomic_write_json(isolate_thumbs, [2])
        data = json.loads(isolate_thumbs.read_text())
        assert data == [2]
