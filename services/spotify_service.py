import json
import re
import logging
from typing import Optional
from urllib.parse import unquote

import httpx

logger = logging.getLogger(__name__)

MAX_PLAYLIST_TRACKS = 200

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# In-memory cache for embed data (avoid re-fetching within same session)
_embed_cache: dict[str, dict] = {}


class SpotifyServiceError(Exception):
    """Raised when Spotify data cannot be fetched."""


class SpotifyService:
    """Fetch public Spotify playlist data by scraping the embed page.

    No API credentials required — works with any public playlist.
    """

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

    def _fetch_embed_data(self, playlist_id: str) -> dict:
        """Fetch and parse the embed page JSON for a playlist."""
        if playlist_id in _embed_cache:
            return _embed_cache[playlist_id]

        url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        resp = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=15.0)

        if resp.status_code == 404:
            raise SpotifyServiceError(f"Playlist not found: {playlist_id}")
        if resp.status_code == 429:
            raise SpotifyServiceError("Spotify rate limit hit — try again in a minute")
        resp.raise_for_status()

        html = resp.text
        data = None

        # Method 1: __NEXT_DATA__ script tag (modern Spotify embed pages)
        m = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                next_data = json.loads(m.group(1))
                entity = next_data["props"]["pageProps"]["state"]["data"]["entity"]
                data = entity
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning("Failed to parse __NEXT_DATA__ for playlist %s: %s", playlist_id, e)

        # Method 2: URL-encoded "resource" field in inline script (legacy fallback)
        if not data:
            m = re.search(r'"resource"\s*:\s*"(.*?)"', html)
            if m:
                try:
                    data = json.loads(unquote(m.group(1)))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("Failed to parse resource field for playlist %s: %s", playlist_id, e)

        if not data:
            raise SpotifyServiceError(
                f"Could not extract playlist data from embed page. "
                f"Spotify may have changed their page format."
            )

        _embed_cache[playlist_id] = data
        return data

    def get_playlist_info(self, playlist_id: str) -> dict:
        """Fetch playlist metadata (name, owner, image, track count)."""
        data = self._fetch_embed_data(playlist_id)

        # Extract cover art URL
        image_url = ""
        cover_art = data.get("coverArt") or data.get("images") or {}
        if isinstance(cover_art, dict):
            sources = cover_art.get("sources") or cover_art.get("items") or []
            if sources:
                image_url = sources[0].get("url", "")
        elif isinstance(cover_art, list) and cover_art:
            image_url = cover_art[0].get("url", "")

        track_list = data.get("trackList") or []

        return {
            "playlist_id": playlist_id,
            "name": data.get("name") or data.get("title") or "Unknown Playlist",
            "description": data.get("description", ""),
            "owner": data.get("subtitle") or data.get("ownerV2", {}).get("data", {}).get("name", ""),
            "image_url": image_url,
            "track_count": len(track_list),
        }

    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """Fetch tracks from a public playlist. Returns simplified track dicts."""
        data = self._fetch_embed_data(playlist_id)
        track_list = data.get("trackList") or []

        tracks = []
        for item in track_list[:MAX_PLAYLIST_TRACKS]:
            title = item.get("title") or ""
            if not title:
                continue

            # Artist is in "subtitle" field
            artist_str = item.get("subtitle") or "Unknown"
            # Split multiple artists (often joined with ", " or " & ")
            all_artists = [a.strip() for a in re.split(r",\s*|\s+&\s+", artist_str)]

            # Extract Spotify ID from URI (spotify:track:ID)
            uri = item.get("uri") or ""
            spotify_id = uri.split(":")[-1] if uri.startswith("spotify:track:") else ""

            tracks.append({
                "artist": all_artists[0] if all_artists else "Unknown",
                "all_artists": all_artists,
                "title": title,
                "album": "",  # Not available from embed page
                "year": "",   # Not available from embed page
                "duration_ms": item.get("duration") or 0,
                "spotify_id": spotify_id,
            })

        return tracks
