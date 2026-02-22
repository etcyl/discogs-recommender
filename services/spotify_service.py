import re
import logging
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

logger = logging.getLogger(__name__)

MAX_PLAYLIST_TRACKS = 200


class SpotifyService:
    def __init__(self, client_id: str, client_secret: str):
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=client_id,
                client_secret=client_secret,
            )
        )

    @staticmethod
    def parse_playlist_url(url: str) -> Optional[str]:
        """Extract playlist ID from Spotify URL or URI.

        Handles:
          https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U
          https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=abc
          spotify:playlist:37i9dQZF1DWXRqgorJj26U
        """
        match = re.search(r"playlist[/:]([a-zA-Z0-9]+)", url)
        return match.group(1) if match else None

    def get_playlist_info(self, playlist_id: str) -> dict:
        """Fetch playlist metadata (name, owner, image, track count)."""
        playlist = self.sp.playlist(
            playlist_id,
            fields="name,description,owner,images,tracks.total",
        )
        return {
            "playlist_id": playlist_id,
            "name": playlist["name"],
            "description": playlist.get("description", ""),
            "owner": playlist["owner"]["display_name"],
            "image_url": (
                playlist["images"][0]["url"] if playlist.get("images") else ""
            ),
            "track_count": playlist["tracks"]["total"],
        }

    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Fetch tracks from a public playlist. Returns simplified track dicts."""
        tracks = []
        results = self.sp.playlist_tracks(
            playlist_id,
            fields="items(track(name,artists,album(name,release_date),duration_ms,id)),next",
            limit=100,
        )

        while results and len(tracks) < MAX_PLAYLIST_TRACKS:
            for item in results.get("items", []):
                track = item.get("track")
                if not track or not track.get("name"):
                    continue
                artists = [a["name"] for a in track.get("artists", [])]
                album = track.get("album", {})
                release_date = album.get("release_date", "")
                year = release_date[:4] if release_date else ""

                tracks.append({
                    "artist": artists[0] if artists else "Unknown",
                    "all_artists": artists,
                    "title": track["name"],
                    "album": album.get("name", ""),
                    "year": year,
                    "duration_ms": track.get("duration_ms", 0),
                    "spotify_id": track.get("id", ""),
                })

            if results.get("next") and len(tracks) < MAX_PLAYLIST_TRACKS:
                results = self.sp.next(results)
            else:
                break

        return tracks[:MAX_PLAYLIST_TRACKS]
