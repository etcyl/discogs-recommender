"""Tests for services/verification.py — catalogue-backed accuracy checking.

No test here touches the network. Catalogue lookups are patched so the tests
exercise the matching logic and the policy behaviour, which is where the
decisions actually live.
"""
from unittest.mock import patch

import pytest

from services import verification
from services.verification import Policy, Result, Status


@pytest.fixture(autouse=True)
def _clear_cache():
    """Verification memoises to the shared cache; isolate each test."""
    from services.cache import cache
    cache.clear()
    yield
    cache.clear()


class TestNormalize:
    def test_folds_accents(self):
        assert verification.normalize("Sigur Rós") == "sigur ros"
        assert verification.normalize("Björk") == "bjork"

    def test_a_name_made_entirely_of_version_words_survives(self):
        """The bands Live and Mono were normalising to nothing, so every one
        of their songs was reported as invented."""
        assert verification.normalize("Live") == "live"
        assert verification.normalize("Mono") == "mono"
        assert verification.normalize("Demo") == "demo"
        assert verification.similarity("Live", "Live") == 1.0

    def test_version_words_are_still_stripped_when_they_are_a_suffix(self):
        assert verification.normalize("Alive - Live") == "alive"
        assert verification.normalize("Dreams - 2004 Remaster") == "dreams 2004"

    def test_drops_punctuation(self):
        # Punctuation becomes a separator rather than vanishing, so "N.W.A"
        # and "N W A" compare equal without "a.b" collapsing into "ab".
        assert verification.normalize("N.W.A!") == "n w a"

    def test_strips_version_noise(self):
        assert verification.normalize("Song (2011 Remaster)") == "song 2011"
        assert verification.normalize("Track - Radio Edit") == "track"

    def test_strips_featured_artists(self):
        assert verification.normalize("Song (feat. Someone)") == "song"

    def test_handles_empty(self):
        assert verification.normalize("") == ""
        assert verification.normalize(None) == ""


class TestSimilarity:
    def test_identical_is_one(self):
        assert verification.similarity("Pixies", "Pixies") == 1.0

    def test_accent_insensitive(self):
        assert verification.similarity("Sigur Ros", "Sigur Rós") == 1.0

    def test_containment_scores_high(self):
        assert verification.similarity("Glósóli", "Glosoli - 2005 Remaster") >= 0.95

    def test_different_names_score_low(self):
        assert verification.similarity("Portishead", "The Knife") < 0.5

    def test_near_miss_is_not_a_match(self):
        """Galaxie 2000 vs Galaxie 500 — close, but a different band."""
        assert verification.similarity("Galaxie 2000", "Galaxie 500") < 1.0

    def test_empty_is_zero(self):
        assert verification.similarity("", "anything") == 0.0


class TestVerifySong:
    def test_blank_input_is_skipped(self):
        assert verification.verify_song("", "Song").status == Status.SKIPPED
        assert verification.verify_song("Artist", "").status == Status.SKIPPED

    @patch("services.verification._deezer")
    def test_exact_match_is_verified(self, mock_deezer):
        mock_deezer.return_value = ("Pixies", "The Thing", 1.0, True)
        res = verification.verify_song("Pixies", "The Thing")
        assert res.status == Status.VERIFIED
        assert res.source == "deezer"

    @patch("services.verification._musicbrainz", return_value=None)
    @patch("services.verification._itunes", return_value=None)
    @patch("services.verification._deezer")
    def test_near_match_is_corrected_not_verified(self, mock_deezer, *_):
        """A different spelling is worth surfacing, not silently accepting."""
        mock_deezer.return_value = ("Tujiko Noriko", "The Promenade", 0.88, False)
        res = verification.verify_song("Tujiko Norne", "The Promenade")
        assert res.status == Status.CORRECTED
        assert res.matched_artist == "Tujiko Noriko"

    @patch("services.verification._musicbrainz", return_value=None)
    @patch("services.verification._itunes", return_value=None)
    @patch("services.verification._deezer", return_value=None)
    def test_no_catalogue_match_is_unverified(self, *_):
        res = verification.verify_song("Made Up Band", "Invented Song")
        assert res.status == Status.UNVERIFIED
        assert res.checked_sources == ["deezer", "itunes", "musicbrainz"]

    @patch("services.verification._musicbrainz")
    @patch("services.verification._itunes", return_value=None)
    @patch("services.verification._deezer", return_value=None)
    def test_falls_through_to_later_catalogues(self, _d, _i, mock_mb):
        mock_mb.return_value = ("Obscure Act", "B-Side", 1.0, True)
        res = verification.verify_song("Obscure Act", "B-Side")
        assert res.status == Status.VERIFIED
        assert res.source == "musicbrainz"

    @patch("services.verification._musicbrainz", return_value=None)
    @patch("services.verification._itunes", return_value=None)
    @patch("services.verification._deezer", side_effect=Exception("network down"))
    def test_a_failing_catalogue_does_not_condemn_the_track(self, *_):
        """An outage must not be reported as 'this song is fake'."""
        res = verification.verify_song("Artist", "Song")
        assert res.status == Status.UNVERIFIED
        assert "deezer" not in res.checked_sources

    @patch("services.verification._deezer")
    def test_result_is_cached(self, mock_deezer):
        mock_deezer.return_value = ("A", "B", 1.0, True)
        verification.verify_song("A", "B")
        verification.verify_song("A", "B")
        assert mock_deezer.call_count == 1


class TestVerifySongs:
    @staticmethod
    def _songs():
        return [{"artist": "Pixies", "title": "The Thing"},
                {"artist": "Fake Band", "title": "Fake Song"}]

    def test_policy_off_skips_everything(self):
        songs, summary = verification.verify_songs(self._songs(), Policy.OFF.value)
        assert summary["skipped"] == 2
        assert all(s["verification"]["status"] == "skipped" for s in songs)

    @patch("services.verification.verify_song")
    def test_flag_annotates_but_keeps_everything(self, mock_verify):
        mock_verify.side_effect = [
            Result(status=Status.VERIFIED, source="deezer", confidence=1.0),
            Result(status=Status.UNVERIFIED),
        ]
        songs, summary = verification.verify_songs(self._songs(), Policy.FLAG.value)
        assert len(songs) == 2
        assert summary["verified"] == 1
        assert summary["unverified"] == 1
        assert summary["dropped"] == 0

    @patch("services.verification.verify_song")
    def test_strict_drops_unverified(self, mock_verify):
        mock_verify.side_effect = [
            Result(status=Status.VERIFIED, source="deezer", confidence=1.0),
            Result(status=Status.UNVERIFIED),
        ]
        songs, summary = verification.verify_songs(self._songs(), Policy.STRICT.value)
        assert len(songs) == 1
        assert songs[0]["artist"] == "Pixies"
        assert summary["dropped"] == 1
        # Dropped songs still appear in the counts — they are not erased.
        assert summary["unverified"] == 1

    @patch("services.verification.verify_song")
    def test_strict_keeps_corrected(self, mock_verify):
        """A name-variant match is still a real recording."""
        mock_verify.side_effect = [
            Result(status=Status.CORRECTED, source="itunes", confidence=0.88),
            Result(status=Status.UNVERIFIED),
        ]
        songs, summary = verification.verify_songs(self._songs(), Policy.STRICT.value)
        assert len(songs) == 1
        assert summary["corrected"] == 1

    @patch("services.verification.verify_song", side_effect=Exception("boom"))
    def test_an_exception_never_drops_a_song(self, _):
        """Failing closed would let an outage silently empty a playlist."""
        songs, summary = verification.verify_songs(self._songs(), Policy.STRICT.value)
        assert len(songs) == 2
        assert summary["skipped"] == 2
        assert summary["dropped"] == 0

    @patch("services.verification.verify_song",
           return_value=Result(status=Status.VERIFIED))
    def test_records_what_the_model_claimed(self, _):
        songs, _s = verification.verify_songs(self._songs(), Policy.FLAG.value)
        assert songs[0]["verification"]["claimed_as"] == "Pixies — The Thing"

    def test_empty_input(self):
        songs, summary = verification.verify_songs([], Policy.STRICT.value)
        assert songs == []
        assert summary["checked"] == 0


class TestReconcile:
    """YouTube resolution rewrites artist/title from the video title."""

    @staticmethod
    def _song(artist, title, status="verified",
              m_artist="Van Dyke Parks", m_title="Vine Street"):
        return {"artist": artist, "title": title,
                "verification": {"status": status, "matched_artist": m_artist,
                                 "matched_title": m_title, "source": "deezer"}}

    def test_restores_identity_after_a_bad_rewrite(self):
        """The real case: a podcast episode title replacing the song."""
        songs = [self._song(
            "", 'Behind the Song: "Vine Street" by Randy Newman — featuring Van Dyke Parks')]
        assert verification.reconcile(songs) == 1
        assert songs[0]["artist"] == "Van Dyke Parks"
        assert songs[0]["title"] == "Vine Street"

    def test_records_what_it_replaced(self):
        songs = [self._song("", "Behind the Song: something entirely different")]
        verification.reconcile(songs)
        assert "displayCorrectedFrom" in songs[0]

    def test_leaves_a_close_rewrite_alone(self):
        """Minor corrections from the video title are an improvement."""
        songs = [self._song("Van Dyke Parks", "Vine Street (Remastered)")]
        assert verification.reconcile(songs) == 0
        assert songs[0]["title"] == "Vine Street (Remastered)"

    def test_ignores_unverified_songs(self):
        """With nothing confirmed, there is no better name to fall back to."""
        songs = [self._song("Whoever", "Whatever", status="unverified",
                            m_artist="", m_title="")]
        assert verification.reconcile(songs) == 0
        assert songs[0]["artist"] == "Whoever"

    def test_ignores_songs_with_no_match_data(self):
        songs = [{"artist": "A", "title": "B",
                  "verification": {"status": "verified"}}]
        assert verification.reconcile(songs) == 0

    def test_handles_songs_without_verification(self):
        songs = [{"artist": "A", "title": "B"}]
        assert verification.reconcile(songs) == 0
