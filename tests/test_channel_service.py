"""Tests for services/channel_service.py."""
import json
import pytest
from unittest.mock import patch
from pathlib import Path

from services import channel_service


@pytest.fixture(autouse=True)
def tmp_channels_dir(tmp_path):
    """Redirect channel_service to use a temp directory."""
    with patch.object(channel_service, "DATA_DIR", tmp_path), \
         patch.object(channel_service, "CHANNELS_FILE", tmp_path / "channels.json"):
        yield tmp_path


class TestLoadChannels:
    def test_creates_default_channel_if_missing(self):
        channels = channel_service.load_channels()
        assert len(channels) >= 1
        assert channels[0]["id"] == "my-collection"
        assert channels[0]["is_default"] is True

    def test_preserves_existing_channels(self, tmp_channels_dir):
        data = [
            channel_service.DEFAULT_CHANNEL_DISCOGS.copy(),
            channel_service.DEFAULT_CHANNEL_LIKED.copy(),
            {"id": "abc", "name": "Test", "source_type": "spotify",
             "source_data": {}, "mode": "similar_songs",
             "created_at": "2026-01-01T00:00:00", "is_default": False},
        ]
        (tmp_channels_dir / "channels.json").write_text(json.dumps(data))

        channels = channel_service.load_channels()
        assert len(channels) == 3
        assert channels[2]["id"] == "abc"


class TestGetChannel:
    def test_finds_existing_channel(self):
        channel = channel_service.get_channel("my-collection")
        assert channel is not None
        assert channel["name"] == "My Collection"

    def test_returns_none_for_missing(self):
        assert channel_service.get_channel("nonexistent") is None


class TestCreateChannel:
    def test_creates_spotify_channel(self):
        ch = channel_service.create_channel(
            name="Test Channel",
            source_type="spotify",
            source_data={"playlist_id": "abc"},
            mode="similar_songs",
        )
        assert ch["name"] == "Test Channel"
        assert ch["source_type"] == "spotify"
        assert ch["is_default"] is False
        assert len(ch["id"]) == 8

        # Verify persisted
        channels = channel_service.load_channels()
        assert any(c["id"] == ch["id"] for c in channels)

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="name is required"):
            channel_service.create_channel("", "spotify", {}, "similar_songs")

    def test_rejects_invalid_source_type(self):
        with pytest.raises(ValueError, match="Invalid source_type"):
            channel_service.create_channel("Test", "invalid", {}, "similar_songs")

    def test_rejects_invalid_mode(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            channel_service.create_channel("Test", "spotify", {}, "bad_mode")

    def test_enforces_max_channels(self, tmp_channels_dir):
        data = [channel_service.DEFAULT_CHANNEL_DISCOGS.copy()]
        for i in range(channel_service.MAX_CHANNELS - 1):
            data.append({
                "id": f"ch{i}", "name": f"Ch{i}", "source_type": "spotify",
                "source_data": {}, "mode": "similar_songs",
                "created_at": "2026-01-01T00:00:00", "is_default": False,
            })
        (tmp_channels_dir / "channels.json").write_text(json.dumps(data))

        with pytest.raises(ValueError, match="Maximum"):
            channel_service.create_channel("One More", "spotify", {}, "similar_songs")

    def test_sanitizes_name(self):
        ch = channel_service.create_channel(
            name="  Test\x00Channel  ",
            source_type="spotify",
            source_data={},
            mode="play_playlist",
        )
        assert ch["name"] == "TestChannel"


class TestRenameChannel:
    def test_renames_existing(self):
        ch = channel_service.create_channel("Old", "spotify", {}, "similar_songs")
        updated = channel_service.rename_channel(ch["id"], "New Name")
        assert updated["name"] == "New Name"

    def test_rejects_empty_name(self):
        ch = channel_service.create_channel("Test", "spotify", {}, "similar_songs")
        with pytest.raises(ValueError, match="name is required"):
            channel_service.rename_channel(ch["id"], "")

    def test_rejects_missing_channel(self):
        with pytest.raises(ValueError, match="not found"):
            channel_service.rename_channel("nonexistent", "Name")


class TestUpdateChannelAiModel:
    def test_updates_ai_model(self):
        ch = channel_service.create_channel("Test", "spotify", {}, "similar_songs")
        updated = channel_service.update_channel_ai_model(ch["id"], "ollama")
        assert updated["ai_model"] == "ollama"

        # Verify persisted
        reloaded = channel_service.get_channel(ch["id"])
        assert reloaded["ai_model"] == "ollama"

    def test_rejects_invalid_model(self):
        ch = channel_service.create_channel("Test", "spotify", {}, "similar_songs")
        with pytest.raises(ValueError, match="Invalid ai_model"):
            channel_service.update_channel_ai_model(ch["id"], "gpt-4")

    def test_rejects_missing_channel(self):
        with pytest.raises(ValueError, match="not found"):
            channel_service.update_channel_ai_model("nonexistent", "ollama")

    def test_default_ai_model_on_create(self):
        ch = channel_service.create_channel("Test", "spotify", {}, "similar_songs")
        assert ch["ai_model"] == "claude-sonnet"

    def test_ai_model_migration(self, tmp_channels_dir):
        """Channels without ai_model field get migrated."""
        data = [{
            "id": "my-collection", "name": "My Collection",
            "source_type": "discogs", "source_data": {},
            "mode": "similar_songs", "discovery": 30,
            "era_from": None, "era_to": None,
            "created_at": "2026-01-01T00:00:00", "is_default": True,
        }]
        (tmp_channels_dir / "channels.json").write_text(json.dumps(data))

        channels = channel_service.load_channels()
        assert channels[0]["ai_model"] == "claude-sonnet"


class TestDeleteChannel:
    def test_deletes_channel(self):
        ch = channel_service.create_channel("ToDelete", "spotify", {}, "similar_songs")
        result = channel_service.delete_channel(ch["id"])
        assert result is True
        assert channel_service.get_channel(ch["id"]) is None

    def test_cannot_delete_default(self):
        with pytest.raises(ValueError, match="Cannot delete"):
            channel_service.delete_channel("my-collection")

    def test_rejects_missing_channel(self):
        with pytest.raises(ValueError, match="not found"):
            channel_service.delete_channel("nonexistent")
