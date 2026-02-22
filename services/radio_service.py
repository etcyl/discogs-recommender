import json
from concurrent.futures import ThreadPoolExecutor
import anthropic
from youtubesearchpython import VideosSearch

from services.cache import cache


class RadioService:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate_playlist(self, profile: dict, collection: list[dict],
                          thumbs_summary: str = "",
                          dislikes_summary: str = "") -> list[dict]:
        """Ask Claude to generate a 40-song radio playlist (big batch to minimize API calls)."""
        summary = self._build_profile_summary(profile, collection)

        dislikes_block = ""
        if dislikes_summary:
            dislikes_block = f"""
PREVIOUSLY DISLIKED SONGS (listener skipped/disliked these — AVOID these and similar):
{dislikes_summary}
"""

        prompt = f"""You are an expert music curator with encyclopedic knowledge — deeper than
Spotify, Last.fm, or Pandora. Your recommendations should surprise and delight,
not just serve safe, obvious picks.

Based on this listener's Discogs collection, create a radio playlist of 40 SONGS
they would love. Go beyond surface-level genre matching:

COLLECTION PROFILE:
{summary}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet — this is their first session."}
{dislikes_block}
CURATION PHILOSOPHY:
- Dig deep: obscure B-sides, overlooked album tracks, international gems, reissued rarities
- Map musical DNA: if they like Artist A, find artists who share producers, session musicians,
  label mates, or scene connections — not just "similar sounding" acts
- Create flow: sequence songs so each transition feels intentional (tempo, mood, key)
- Balance 60% familiar territory with 40% genuine discoveries they've never heard
- Pull from any era or country — a 1972 Japanese psych track can follow a 2023 post-punk single
- If they have thumbs-up history, lean INTO those preferences but still push boundaries
- NEVER repeat a song from the thumbs-up history or the disliked list
- Avoid overly obvious hits — dig for the deeper cuts

For EACH song, also include a "similar_to" field: an array of 1-3 specific
artist+album combos FROM THE LISTENER'S COLLECTION that this song connects to,
and briefly why. This helps the listener understand the recommendation.

Return a JSON array of exactly 40 objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "reason" should be 1 sentence explaining the specific connection to their taste.
The "similar_to" should be an array like: [{{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich"}}]

Return ONLY the JSON array, no other text."""

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = message.content[0].text
        try:
            playlist = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                playlist = json.loads(text[start:end])
            else:
                playlist = []

        return playlist

    def resolve_youtube_ids(self, playlist: list[dict]) -> list[dict]:
        """Find YouTube video IDs for each song in the playlist (parallel)."""
        def _resolve_one(song):
            artist = song.get("artist", "")
            title = song.get("title", "")
            video_info = self._find_youtube_video(artist, title)
            if video_info:
                song["videoId"] = video_info["videoId"]
                song["thumbnail"] = video_info["thumbnail"]
                song["duration"] = video_info["duration"]
                song["altVideoIds"] = video_info.get("altVideoIds", [])
                return song
            return None

        resolved = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_resolve_one, song): i for i, song in enumerate(playlist)}
            results = [None] * len(playlist)
            for future in futures:
                idx = futures[future]
                results[idx] = future.result()
            resolved = [r for r in results if r is not None]
        return resolved

    def _find_youtube_video(self, artist: str, title: str) -> dict | None:
        """Search YouTube for a song, return video info with backup IDs. Cached 24hr."""
        cache_key = f"yt:{artist.lower()}:{title.lower()}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        all_results = []
        try:
            search = VideosSearch(f"{artist} {title} official audio", limit=5)
            all_results = search.result().get("result", [])
            if not all_results:
                search = VideosSearch(f"{artist} {title}", limit=5)
                all_results = search.result().get("result", [])
        except Exception:
            pass

        if not all_results:
            return None

        best = all_results[0]
        info = {
            "videoId": best["id"],
            "thumbnail": (best.get("thumbnails", [{}])[-1].get("url", "")
                          if best.get("thumbnails") else ""),
            "duration": best.get("duration", ""),
            "ytTitle": best.get("title", ""),
            "altVideoIds": [r["id"] for r in all_results[1:]],
        }
        cache.set(cache_key, info, ttl=86400)
        return info

    def _build_profile_summary(self, profile: dict, collection: list[dict]) -> str:
        lines = [
            f"Total releases: {profile['total_releases']}",
            f"Top genres: {', '.join(f'{g} ({c})' for g, c in profile['top_genres'])}",
            f"Top styles: {', '.join(f'{s} ({c})' for s, c in profile['top_styles'])}",
            f"Top artists: {', '.join(f'{a} ({c})' for a, c in profile['top_artists'])}",
            f"Top labels: {', '.join(f'{la} ({c})' for la, c in profile['top_labels'])}",
            "",
            "Sample releases:",
        ]
        for r in collection[:30]:
            artists = ", ".join(r.get("artists", ["Unknown"]))
            year = r.get("year", "n/a")
            lines.append(f"  - {artists} - {r.get('title', '')} ({year})")
        return "\n".join(lines)
