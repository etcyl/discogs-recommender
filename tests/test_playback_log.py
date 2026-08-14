"""Playback failures become a record instead of a shrug.

The report that prompted this was "some songs aren't playing for her" — true,
unactionable, and impossible to confirm fixed. These tests cover the part that
makes it actionable: which track, whose account, and what the player said.
"""
import pytest

from services import playback_log


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    from services import database
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "playback.db")
    database.init_db()
    playback_log.init_playback_db()
    yield


def _fail(user="u1", artist="Magazine", title="Shot by Both Sides", code=150,
          event="error", **kw):
    playback_log.record(user_id=user, event=event, artist=artist, title=title,
                        error_code=code, **kw)


class TestRecording:

    def test_a_failure_is_kept(self):
        _fail()
        rows = playback_log.recent("u1")
        assert len(rows) == 1
        assert rows[0]["artist"] == "Magazine"
        assert rows[0]["error_code"] == 150

    def test_the_code_is_translated_for_a_reader(self):
        _fail(code=150)
        assert "outside YouTube" in playback_log.recent("u1")[0]["meaning"]

    def test_an_unknown_code_still_reads_as_a_sentence(self):
        _fail(code=999)
        assert playback_log.recent("u1")[0]["meaning"]

    def test_newest_first(self):
        _fail(title="First")
        _fail(title="Second")
        assert [r["title"] for r in playback_log.recent("u1")] == ["Second",
                                                                  "First"]

    def test_one_account_cannot_see_another_s_failures(self):
        _fail(user="u1", artist="Hers")
        _fail(user="u2", artist="His")
        assert [r["artist"] for r in playback_log.recent("u1")] == ["Hers"]
        assert [r["artist"] for r in playback_log.recent("u2")] == ["His"]

    def test_no_user_scope_returns_everyone(self):
        _fail(user="u1")
        _fail(user="u2")
        assert len(playback_log.recent(None)) == 2


class TestItNeverBreaksPlayback:
    """Logging is bookkeeping. It must not raise into the request path."""

    def test_an_unknown_event_is_ignored(self):
        playback_log.record("u1", "exploded", artist="A", title="B")
        assert playback_log.recent("u1") == []

    def test_a_missing_user_is_ignored(self):
        playback_log.record("", "error", artist="A", title="B")
        assert playback_log.recent(None) == []

    def test_a_missing_error_code_is_allowed(self):
        playback_log.record("u1", "error", artist="A", title="B")
        assert playback_log.recent("u1")[0]["error_code"] is None

    def test_overlong_fields_are_truncated_not_rejected(self):
        _fail(artist="x" * 5000, title="y" * 5000)
        row = playback_log.recent("u1")[0]
        assert len(row["artist"]) == 300 and len(row["title"]) == 300

    def test_a_broken_database_does_not_raise(self, monkeypatch):
        import sqlite3
        from services import database
        monkeypatch.setattr(database, "get_db",
                            lambda: (_ for _ in ()).throw(sqlite3.Error("no")))
        monkeypatch.setattr(playback_log, "get_db", database.get_db)
        playback_log.record("u1", "error", artist="A", title="B")
        assert playback_log.recent("u1") == []
        assert playback_log.summary("u1")["total"] == 0


class TestSummary:

    def test_counts_errors_and_recoveries_separately(self):
        _fail()
        _fail()
        _fail(event="recovered")
        s = playback_log.summary("u1")
        assert s["total"] == 2
        assert s["recovered"] == 1

    def test_a_recovery_is_not_counted_as_a_failure(self):
        _fail(event="recovered")
        s = playback_log.summary("u1")
        assert s["total"] == 0
        assert s["worst"] == []

    def test_the_same_track_failing_twice_is_one_row(self):
        _fail()
        _fail()
        worst = playback_log.summary("u1")["worst"]
        assert len(worst) == 1 and worst[0]["n"] == 2

    def test_case_differences_do_not_split_a_track(self):
        _fail(artist="Magazine", title="Shot by Both Sides")
        _fail(artist="MAGAZINE", title="SHOT BY BOTH SIDES")
        assert len(playback_log.summary("u1")["worst"]) == 1

    def test_worst_offenders_come_first(self):
        _fail(title="Rare")
        for _ in range(3):
            _fail(title="Always broken")
        assert playback_log.summary("u1")["worst"][0]["title"] == "Always broken"

    def test_reasons_are_grouped_by_code(self):
        _fail(code=150)
        _fail(code=150)
        _fail(code=100)
        reasons = {r["error_code"]: r["n"]
                   for r in playback_log.summary("u1")["by_reason"]}
        assert reasons == {150: 2, 100: 1}

    def test_summary_is_scoped_to_one_account(self):
        _fail(user="u1")
        _fail(user="u2")
        _fail(user="u2")
        assert playback_log.summary("u1")["total"] == 1
        assert playback_log.summary("u2")["total"] == 2
        assert playback_log.summary(None)["total"] == 3

    def test_empty_summary_has_the_same_shape(self):
        s = playback_log.summary("nobody")
        assert s == {"by_reason": [], "worst": [], "total": 0, "recovered": 0}


class TestRepairingAPlaylist:

    def test_failing_tracks_are_returned_for_matching(self):
        _fail(artist="Magazine", title="Shot by Both Sides")
        assert ("magazine", "shot by both sides") in \
            playback_log.failing_tracks("u1")

    def test_a_one_off_can_be_excluded(self):
        _fail(title="Blipped once")
        assert playback_log.failing_tracks("u1", min_failures=2) == set()


class TestRetention:

    def test_old_events_are_pruned(self):
        from datetime import datetime, timedelta, timezone
        from services.database import get_db
        _fail()
        old = (datetime.now(timezone.utc)
               - timedelta(days=200)).isoformat(timespec="seconds")
        conn = get_db()
        conn.execute("UPDATE playback_events SET created_at = ?", (old,))
        conn.commit()
        conn.close()
        assert playback_log.prune(90) == 1
        assert playback_log.recent("u1") == []

    def test_recent_events_survive(self):
        _fail()
        assert playback_log.prune(90) == 0
        assert len(playback_log.recent("u1")) == 1
