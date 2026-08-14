"""Tests for services/audit.py — the AI generation audit log.

The properties that matter: a run is recorded completely, songs the accuracy
check removed are still on the record, one user cannot read another's runs,
and a logging failure never propagates into the request that triggered it.
"""
from unittest.mock import patch

import pytest

from services import audit


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    """Point the audit tables at a throwaway database."""
    from services import database
    monkeypatch.setattr(database, "DB_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    audit.init_audit_db()
    yield


def _record(**overrides):
    kwargs = dict(
        user_id="user-1", channel_id="ch-1", channel_name="My Collection",
        source_type="discogs", mode="similar_songs", ai_model="ollama",
        prompt_tier="rich", prompt_sha256="abc123", discovery=40,
        num_requested=25,
        songs_kept=[{"artist": "Pixies", "title": "The Thing", "year": "2016",
                     "reason": "shares the jangle", "match_score": 80,
                     "obscurity_score": 40,
                     "verification": {"status": "verified", "source": "deezer",
                                      "confidence": 1.0, "matched_artist": "Pixies",
                                      "matched_title": "The Thing"}}],
        songs_dropped=[{"artist": "Fake", "title": "Invented", "year": "1999",
                        "verification": {"status": "unverified", "source": "",
                                         "confidence": None}}],
        verification_summary={"policy": "strict", "verified": 1, "corrected": 0,
                              "unverified": 1, "dropped": 1},
        duration_ms=1234, app_version="1.13.0",
    )
    kwargs.update(overrides)
    return audit.record_run(**kwargs)


class TestPromptFingerprint:
    def test_is_stable(self):
        a = audit.prompt_fingerprint("sys", "user")
        assert a == audit.prompt_fingerprint("sys", "user")

    def test_differs_on_change(self):
        assert (audit.prompt_fingerprint("sys", "user")
                != audit.prompt_fingerprint("sys", "user2"))

    def test_boundary_is_unambiguous(self):
        """('ab','c') and ('a','bc') must not collide."""
        assert audit.prompt_fingerprint("ab", "c") != audit.prompt_fingerprint("a", "bc")

    def test_handles_none(self):
        assert len(audit.prompt_fingerprint(None, None)) == 64


class TestRecordRun:
    def test_returns_a_run_id(self):
        assert _record() is not None

    def test_stores_run_metadata(self):
        run_id = _record()
        run = audit.get_run(run_id)
        assert run["ai_model"] == "ollama"
        assert run["prompt_tier"] == "rich"
        assert run["discovery"] == 40
        assert run["duration_ms"] == 1234
        assert run["verification_policy"] == "strict"

    def test_counts_generated_as_kept_plus_dropped(self):
        run = audit.get_run(_record())
        assert run["num_generated"] == 2
        assert run["num_dropped"] == 1

    def test_records_songs_with_no_playable_match(self):
        """A song that survived the check but found no video still counts."""
        run_id = _record(songs_unresolved=[{"artist": "Real", "title": "But Unfindable"}])
        run = audit.get_run(run_id)
        assert run["num_generated"] == 3        # kept + dropped + unresolved
        assert run["num_unresolved"] == 1
        item = next(i for i in run["items"] if i["title"] == "But Unfindable")
        assert item["kept"] == 0
        assert item["drop_reason"] == "no-playable-match"

    def test_drop_reasons_distinguish_the_two_causes(self):
        run = audit.get_run(
            _record(songs_unresolved=[{"artist": "R", "title": "NoVideo"}]))
        reasons = {i["title"]: i["drop_reason"] for i in run["items"]}
        assert reasons["The Thing"] == ""
        assert reasons["Invented"] == "unverified"
        assert reasons["NoVideo"] == "no-playable-match"

    def test_dropped_songs_stay_on_the_record(self):
        """A filtered-out recommendation must not simply disappear."""
        run = audit.get_run(_record())
        titles = {(i["title"], i["kept"]) for i in run["items"]}
        assert ("The Thing", 1) in titles
        assert ("Invented", 0) in titles

    def test_stores_verification_detail_per_song(self):
        run = audit.get_run(_record())
        kept = next(i for i in run["items"] if i["kept"])
        assert kept["verify_status"] == "verified"
        assert kept["verify_source"] == "deezer"
        assert "Pixies" in kept["matched_as"]

    def test_stores_the_model_claim(self):
        run = audit.get_run(_record())
        kept = next(i for i in run["items"] if i["kept"])
        assert kept["reason"] == "shares the jangle"

    def test_tolerates_missing_fields(self):
        run_id = _record(songs_kept=[{"artist": "A"}], songs_dropped=[])
        assert audit.get_run(run_id)["items"][0]["title"] == ""

    def test_non_numeric_scores_do_not_raise(self):
        run_id = _record(songs_kept=[{"artist": "A", "title": "B",
                                      "match_score": "high",
                                      "obscurity_score": None}],
                         songs_dropped=[])
        assert audit.get_run(run_id)["items"][0]["match_score"] is None

    def test_a_database_failure_never_propagates(self):
        """Bookkeeping must not be able to break generation."""
        import sqlite3
        with patch("services.audit.get_db", side_effect=sqlite3.Error("disk full")):
            assert _record() is None


class TestListAndGet:
    def test_scopes_to_the_owner(self):
        _record(user_id="user-1")
        _record(user_id="user-2")
        assert len(audit.list_runs("user-1")) == 1
        assert len(audit.list_runs("user-2")) == 1
        assert len(audit.list_runs(None)) == 2

    def test_newest_first(self):
        first = _record(channel_name="older")
        second = _record(channel_name="newer")
        runs = audit.list_runs("user-1")
        assert runs[0]["id"] == second
        assert runs[1]["id"] == first

    def test_get_run_refuses_another_users_run(self):
        run_id = _record(user_id="user-1")
        assert audit.get_run(run_id, "user-2") is None
        assert audit.get_run(run_id, "user-1") is not None

    def test_get_missing_run(self):
        assert audit.get_run(99999) is None

    def test_limit_is_clamped(self):
        for _ in range(3):
            _record()
        assert len(audit.list_runs("user-1", limit=10_000)) == 3


class TestStats:
    def test_reports_verified_percentage_per_model(self):
        _record(ai_model="ollama",
                verification_summary={"policy": "flag", "verified": 8,
                                      "corrected": 1, "unverified": 1, "dropped": 0})
        stats = audit.stats("user-1")
        row = next(r for r in stats["by_model"] if r["ai_model"] == "ollama")
        # verified + corrected out of everything checked
        assert row["verified_pct"] == 90.0

    def test_groups_by_model(self):
        _record(ai_model="ollama")
        _record(ai_model="claude-sonnet")
        assert len({r["ai_model"] for r in audit.stats("user-1")["by_model"]}) == 2

    def test_no_runs_gives_empty(self):
        assert audit.stats("nobody")["by_model"] == []


class TestPruneAndExport:
    def test_prune_keeps_recent_runs(self):
        _record()
        assert audit.prune(retention_days=90) == 0
        assert len(audit.list_runs("user-1")) == 1

    def test_prune_removes_old_runs_and_their_items(self):
        run_id = _record()
        # Backdate it well past the window.
        from services.database import get_db
        conn = get_db()
        conn.execute("UPDATE generation_runs SET created_at = ? WHERE id = ?",
                     ("2000-01-01T00:00:00+00:00", run_id))
        conn.commit()
        conn.close()

        assert audit.prune(retention_days=1) == 1
        assert audit.get_run(run_id) is None

    def test_export_is_json(self):
        import json
        payload = json.loads(audit.export_run(_record()))
        assert payload["ai_model"] == "ollama"
        assert len(payload["items"]) == 2

    def test_export_missing_run_is_empty_object(self):
        assert audit.export_run(99999) == "{}"
