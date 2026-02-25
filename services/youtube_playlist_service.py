"""YouTube playlist import service using yt-dlp.

Extracts video metadata from YouTube playlists without downloading content.
No API key required — uses yt-dlp's flat extraction mode.
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

MAX_PLAYLIST_TRACKS = 200

# Patterns to strip from video titles when parsing artist/title
_STRIP_PATTERNS = re.compile(
    r'\s*[\(\[\{]'
    r'(?:official\s*(?:music\s*)?video|official\s*audio|official\s*lyric\s*video'
    r'|lyric\s*video|lyrics|audio|visuali[sz]er|music\s*video|mv|hd|hq'
    r'|4k|remaster(?:ed)?|full\s*album|video\s*clip|clip\s*officiel'
    r'|videoclip|explicit|clean|radio\s*edit|single\s*version'
    r'|album\s*version|bonus\s*track|deluxe)'
    r'[\)\]\}]\s*',
    re.IGNORECASE,
)

# "Topic" channel suffix (e.g. "Artist Name - Topic")
_TOPIC_SUFFIX = re.compile(r'\s*-\s*Topic$', re.IGNORECASE)


class YouTubeServiceError(Exception):
    """Raised when YouTube playlist data cannot be fetched."""


class YouTubePlaylistService:
    """Fetch public YouTube playlist data using yt-dlp.

    No API credentials required — uses flat extraction (metadata only, no download).
    """

    @staticmethod
    def parse_playlist_url(url: str) -> Optional[str]:
        """Extract playlist ID from YouTube URL.

        Handles:
          https://www.youtube.com/playlist?list=PLxxx
          https://youtube.com/playlist?list=PLxxx
          https://www.youtube.com/watch?v=xxx&list=PLxxx
          https://music.youtube.com/playlist?list=PLxxx
        """
        match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
        return match.group(1) if match else None

    def get_playlist_info(self, url: str) -> dict:
        """Fetch playlist metadata (name, track count)."""
        import yt_dlp

        playlist_id = self.parse_playlist_url(url)
        if not playlist_id:
            raise YouTubeServiceError("Invalid YouTube playlist URL")

        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                data = ydl.extract_info(playlist_url, download=False)
        except Exception as e:
            raise YouTubeServiceError(f"Could not fetch playlist: {e}")

        if not data:
            raise YouTubeServiceError("Playlist not found or is private")

        entries = data.get("entries") or []

        return {
            "playlist_id": playlist_id,
            "name": data.get("title") or "YouTube Playlist",
            "owner": data.get("uploader") or data.get("channel") or "",
            "track_count": len(entries),
            "image_url": data.get("thumbnails", [{}])[-1].get("url", "") if data.get("thumbnails") else "",
        }

    def get_playlist_tracks(self, url: str) -> list[dict]:
        """Fetch tracks from a YouTube playlist. Returns simplified track dicts.

        Uses yt-dlp's flat extraction — only fetches metadata, no video content.
        Parses video titles to extract artist and song title.
        """
        import yt_dlp

        playlist_id = self.parse_playlist_url(url)
        if not playlist_id:
            raise YouTubeServiceError("Invalid YouTube playlist URL")

        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                data = ydl.extract_info(playlist_url, download=False)
        except Exception as e:
            raise YouTubeServiceError(f"Could not fetch playlist: {e}")

        if not data:
            raise YouTubeServiceError("Playlist not found or is private")

        entries = data.get("entries") or []
        tracks = []

        for entry in entries[:MAX_PLAYLIST_TRACKS]:
            if not entry:
                continue

            title = entry.get("title") or ""
            if not title or title == "[Deleted video]" or title == "[Private video]":
                continue

            video_id = entry.get("id") or entry.get("url") or ""
            uploader = entry.get("uploader") or entry.get("channel") or ""
            duration = entry.get("duration") or 0

            # Parse title into artist + song title
            artist, song_title = self._parse_video_title(title, uploader)

            tracks.append({
                "artist": artist,
                "title": song_title,
                "album": "",
                "year": "",
                "videoId": video_id,
                "duration_ms": int(duration * 1000) if duration else 0,
            })

        return tracks

    @staticmethod
    def _parse_video_title(title: str, uploader: str = "") -> tuple[str, str]:
        """Parse a YouTube video title into (artist, song_title).

        Handles common formats:
          "Artist - Song Title"
          "Artist - Song Title (Official Video)"
          "Artist | Song Title"
          "Song Title" (with uploader as artist)
        """
        # Strip common suffixes like (Official Video), [Audio], etc.
        cleaned = _STRIP_PATTERNS.sub('', title).strip()
        # Remove trailing whitespace/dashes
        cleaned = cleaned.rstrip(' -|')

        # Try "Artist - Title" format (most common for music)
        for sep in [' - ', ' — ', ' – ', ' | ', ' // ']:
            if sep in cleaned:
                parts = cleaned.split(sep, 1)
                artist = parts[0].strip()
                song = parts[1].strip()
                if artist and song:
                    return artist, song

        # Try "Artist: Title" format
        if ': ' in cleaned:
            parts = cleaned.split(': ', 1)
            if len(parts[0].split()) <= 4:  # Artist name is typically short
                return parts[0].strip(), parts[1].strip()

        # Fall back to uploader as artist, cleaned title as song
        if uploader:
            # Clean "Topic" suffix from channel names
            artist = _TOPIC_SUFFIX.sub('', uploader).strip()
            return artist, cleaned

        # Last resort: entire title as song, "Unknown" as artist
        return "Unknown", cleaned
