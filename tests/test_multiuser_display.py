"""Regressions from setting up a second account on a shared install.

Both of these were found by a real person signing in and finding the app
behaved as though she were someone else.
"""
import pytest


class TestGreeting:
    """A second account was greeted by the owner's name."""

    def _name_for(self, user):
        import app
        return app._get_user_username(user)

    def test_admin_falls_back_to_the_configured_discogs_name(self):
        assert self._name_for({"is_admin": 1, "discogs_username": "",
                               "display_name": "Owner"}) == "testuser"

    def test_admin_prefers_their_own_discogs_name(self):
        assert self._name_for({"is_admin": 1, "discogs_username": "etcyl",
                               "display_name": "Owner"}) == "etcyl"

    def test_guest_is_greeted_by_their_own_name(self):
        """The bug: a guest with no Discogs was greeted as the owner."""
        assert self._name_for({"is_admin": 0, "discogs_username": None,
                               "display_name": "Bee",
                               "login_name": "bee"}) == "Bee"

    def test_guest_with_their_own_discogs_uses_it(self):
        assert self._name_for({"is_admin": 0, "discogs_username": "beelette",
                               "display_name": "Bee"}) == "beelette"

    def test_falls_back_to_login_name(self):
        assert self._name_for({"is_admin": 0, "discogs_username": None,
                               "display_name": "", "login_name": "bee"}) == "bee"

    def test_placeholder_local_username_is_not_used(self):
        assert self._name_for({"is_admin": 1, "discogs_username": "local",
                               "display_name": "Local User"}) == "Local User"

    def test_never_returns_empty(self):
        assert self._name_for({"is_admin": 0}) == "there"


class TestFixedPlaylistNeedsNoModel:
    """Every playlist channel was unplayable for a model-restricted account."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        from services import database
        monkeypatch.setattr(database, "DB_DIR", tmp_path)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "m.db")
        database.init_db()
        yield

    def test_guest_is_limited_to_local_models(self):
        from services import auth_service
        user = auth_service.create_account(
            display_name="Bee", login_name="bee",
            password="correct horse battery", allowed_models="ollama")
        assert auth_service.get_allowed_models(user) == {"ollama"}

    def test_a_play_playlist_channel_stores_a_model_it_will_never_use(self,
                                                                     tmp_path):
        """The setup that caused the bug: default ai_model on a fixed list."""
        from services import channel_service
        ch = channel_service.create_channel(
            name="Hers", source_type="upload",
            source_data={"tracks": [{"artist": "A", "title": "B"}]},
            mode="play_playlist", data_dir=tmp_path)
        assert ch["mode"] == "play_playlist"
        assert ch["ai_model"] == "claude-sonnet"   # not one a guest may use

    def test_channels_can_be_created_with_an_allowed_model(self, tmp_path):
        from services import channel_service
        ch = channel_service.create_channel(
            name="Theirs", source_type="discogs", source_data={"theme": "calm"},
            mode="themed", ai_model="ollama", data_dir=tmp_path)
        assert ch["ai_model"] == "ollama"
