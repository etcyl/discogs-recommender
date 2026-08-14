"""Tests for services/household.py.

Sharing decides who can see whose listening. The interesting cases are the
ones where the answer must be "no".
"""
import json

import pytest

from services import auth_service, household


@pytest.fixture(autouse=True)
def _temp_env(tmp_path, monkeypatch):
    from services import database, paths
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "house.db")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(household, "data_dir", lambda: tmp_path)
    database.init_db()
    yield


PW = "correct horse battery"


def person(login, name=None):
    return auth_service.create_account(display_name=name or login.title(),
                                       login_name=login, password=PW)


def give_likes(tmp_path, user, songs):
    d = tmp_path / user["id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "thumbs.json").write_text(json.dumps(songs), encoding="utf-8")


class TestNowPlaying:
    def test_records_and_reports_a_track(self):
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words", channel_name="Mix")
        people = household.household(auth_service.get_user(a["id"]))
        assert len(people) == 1
        assert people[0]["name"] == "Bo"
        assert people[0]["track"]["title"] == "Words"
        assert people[0]["track"]["channel"] == "Mix"
        assert people[0]["track"]["live"] is True

    def test_overwrites_rather_than_accumulating(self):
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        household.set_now_playing(b["id"], "Ride", "Vapour Trail")
        track = household.household(auth_service.get_user(a["id"]))[0]["track"]
        assert track["title"] == "Vapour Trail"

    def test_stale_playback_is_not_live(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        from services.database import get_db
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        conn = get_db()
        conn.execute("UPDATE now_playing SET updated_at = ? WHERE user_id = ?",
                     (old, b["id"]))
        conn.commit()
        conn.close()
        track = household.household(auth_service.get_user(a["id"]))[0]["track"]
        assert track["live"] is False
        assert track["title"] == "Words"   # still shown as last played

    def test_ignores_an_empty_track(self):
        b = person("bo")
        household.set_now_playing(b["id"], "", "")
        a = person("ana")
        assert household.household(auth_service.get_user(a["id"]))[0]["track"] is None

    def test_you_do_not_appear_in_your_own_household(self):
        a = person("ana")
        household.set_now_playing(a["id"], "Low", "Words")
        assert household.household(auth_service.get_user(a["id"])) == []


class TestSharingToggle:
    def test_opting_out_hides_you_from_others(self):
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        household.set_sharing(b["id"], False)
        assert household.household(auth_service.get_user(a["id"])) == []

    def test_opting_out_also_blinds_you(self):
        """Watching without being seen is the asymmetry to avoid."""
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        household.set_sharing(a["id"], False)
        assert household.household(auth_service.get_user(a["id"])) == []

    def test_opting_out_clears_what_you_were_playing(self):
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        household.set_sharing(b["id"], False)
        household.set_sharing(b["id"], True)
        # Turning it back on must not resurrect the old track.
        assert household.household(auth_service.get_user(a["id"]))[0]["track"] is None

    def test_a_hidden_user_is_not_reported_even_if_playing(self):
        """set_now_playing is not itself gated — the route and the read are.

        Recording is cheap and harmless; what matters is that nothing reaches
        another account while sharing is off.
        """
        a, b = person("ana"), person("bo")
        household.set_sharing(b["id"], False)
        household.set_now_playing(b["id"], "Low", "Words")
        assert household.household(auth_service.get_user(a["id"])) == []
        assert not household.can_view(auth_service.get_user(a["id"]), b["id"])

    def test_suspended_accounts_are_hidden(self):
        a, b = person("ana"), person("bo")
        household.set_now_playing(b["id"], "Low", "Words")
        auth_service.suspend_user(b["id"])
        assert household.household(auth_service.get_user(a["id"])) == []


class TestCanView:
    def test_both_sharing_allows_viewing(self):
        a, b = person("ana"), person("bo")
        assert household.can_view(auth_service.get_user(a["id"]), b["id"])

    def test_target_opted_out_blocks_viewing(self):
        a, b = person("ana"), person("bo")
        household.set_sharing(b["id"], False)
        assert not household.can_view(auth_service.get_user(a["id"]), b["id"])

    def test_viewer_opted_out_blocks_viewing(self):
        a, b = person("ana"), person("bo")
        household.set_sharing(a["id"], False)
        assert not household.can_view(auth_service.get_user(a["id"]), b["id"])

    def test_you_can_always_view_yourself(self):
        a = person("ana")
        household.set_sharing(a["id"], False)
        assert household.can_view(auth_service.get_user(a["id"]), a["id"])

    def test_unknown_user_is_refused(self):
        a = person("ana")
        assert not household.can_view(auth_service.get_user(a["id"]), "nosuchid")


class TestLikedSongs:
    def test_reads_the_other_persons_likes_newest_first(self, tmp_path):
        b = person("bo")
        give_likes(tmp_path, b, [
            {"artist": "Low", "title": "Words"},
            {"artist": "Ride", "title": "Vapour Trail"},
        ])
        songs = household.liked_songs(b["id"])
        assert [s["title"] for s in songs] == ["Vapour Trail", "Words"]

    def test_counts_appear_in_the_household_view(self, tmp_path):
        a, b = person("ana"), person("bo")
        give_likes(tmp_path, b, [{"artist": "Low", "title": "Words"}])
        assert household.household(auth_service.get_user(a["id"]))[0]["liked_count"] == 1

    def test_missing_file_is_empty_not_an_error(self):
        b = person("bo")
        assert household.liked_songs(b["id"]) == []

    def test_no_user_id(self):
        assert household.liked_songs("") == []
