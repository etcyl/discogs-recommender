import json
import logging
from concurrent.futures import ThreadPoolExecutor
import anthropic
from youtubesearchpython import VideosSearch

from services.cache import cache

logger = logging.getLogger(__name__)


class RadioService:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def _discovery_guidance(self, discovery: int) -> str:
        """Return prompt guidance based on the discovery slider level (0-100)."""
        if discovery <= 20:
            return (
                "- STICK CLOSELY to the listener's collection — familiar artists, labels, and styles.\n"
                "- At least 80% of songs should be by artists IN or very closely related to their collection.\n"
                "- Prioritize comfort and recognition over surprise.\n"
                "- Deep cuts from artists they already own are perfect."
            )
        elif discovery <= 45:
            return (
                "- Balance familiar territory with moderate discoveries.\n"
                "- About 60% familiar artists/styles, 40% adjacent discoveries.\n"
                "- Dig deep within their preferred genres but introduce nearby scenes."
            )
        elif discovery <= 70:
            return (
                "- Push beyond their comfort zone while keeping a thread of connection.\n"
                "- About 40% familiar, 60% new territory.\n"
                "- Cross genres, eras, and scenes — find unexpected connections.\n"
                "- Introduce artists from different countries and movements."
            )
        else:
            return (
                "- PUSH BOUNDARIES — deep cuts, unexpected genres, artists they've NEVER heard of.\n"
                "- At least 70% should be artists NOT in their collection or obvious sphere.\n"
                "- Cross-cultural, cross-era, cross-genre — surprise them completely.\n"
                "- Think: 'You had no idea you'd love this.' Obscure is good.\n"
                "- Only keep a thin thread connecting back to their taste."
            )

    def _era_guidance(self, era_from: int | None, era_to: int | None) -> str:
        """Return prompt block constraining songs to a year range."""
        if not era_from and not era_to:
            return ""
        if era_from and era_to:
            return f"\nERA CONSTRAINT: ONLY recommend songs released between {era_from} and {era_to}. Every song MUST have a year in this range.\n"
        if era_from:
            return f"\nERA CONSTRAINT: ONLY recommend songs released from {era_from} onward. Every song MUST be from {era_from} or later.\n"
        return f"\nERA CONSTRAINT: ONLY recommend songs released up to {era_to}. Every song MUST be from {era_to} or earlier.\n"

    def generate_playlist(self, profile: dict, collection: list[dict],
                          thumbs_summary: str = "",
                          dislikes_summary: str = "",
                          play_history_summary: str = "",
                          discovery: int = 30,
                          era_from: int | None = None,
                          era_to: int | None = None) -> list[dict]:
        """Ask Claude to generate a 40-song radio playlist (big batch to minimize API calls)."""
        summary = self._build_profile_summary(profile, collection)
        discovery_guide = self._discovery_guidance(discovery)
        era_guide = self._era_guidance(era_from, era_to)

        dislikes_block = ""
        if dislikes_summary:
            dislikes_block = f"""
PREVIOUSLY DISLIKED SONGS (listener skipped/disliked these — AVOID these and similar):
{dislikes_summary}
"""

        history_block = ""
        if play_history_summary:
            history_block = f"""
RECENTLY PLAYED SONGS (the listener has heard these recently — DO NOT repeat any of these):
{play_history_summary}
"""

        system_text = """You are an expert music curator with encyclopedic knowledge — deeper than
Spotify, Last.fm, or Pandora. Your recommendations should surprise and delight,
not just serve safe, obvious picks. Go beyond surface-level genre matching.

CURATION PHILOSOPHY:
- Dig deep: obscure B-sides, overlooked album tracks, international gems, reissued rarities
- Map musical DNA: if they like Artist A, find artists who share producers, session musicians,
  label mates, or scene connections — not just "similar sounding" acts
- Create flow: sequence songs so each transition feels intentional (tempo, mood, key)
- Pull from any era or country — a 1972 Japanese psych track can follow a 2023 post-punk single
- If they have thumbs-up history, lean INTO those preferences but still push boundaries
- NEVER repeat a song from the thumbs-up history or the disliked list
- Avoid overly obvious hits — dig for the deeper cuts

For EACH song, include a "similar_to" field: an array of 1-3 specific
artist+album combos FROM THE LISTENER'S COLLECTION that this song connects to,
and briefly why.

Return a JSON array of exactly 40 objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "reason" should be 1 sentence explaining the specific connection to their taste.
The "similar_to" should be an array like: [{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich"}]

Return ONLY the JSON array, no other text."""

        user_text = f"""Create a radio playlist of 40 SONGS based on this listener's Discogs collection.

COLLECTION PROFILE:
{summary}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet — this is their first session."}
{dislikes_block}
{history_block}
DISCOVERY LEVEL: {discovery}/100 (0 = stick to what I know, 100 = surprise me completely)
{discovery_guide}
{era_guide}"""

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
        )

        u = message.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        logger.info("Radio playlist — in:%d out:%d cached:%d", u.input_tokens, u.output_tokens, cached)

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

    def generate_playlist_from_tracks(self, tracks: list[dict],
                                      mode: str = "similar_songs",
                                      thumbs_summary: str = "",
                                      dislikes_summary: str = "",
                                      play_history_summary: str = "",
                                      discovery: int = 30,
                                      era_from: int | None = None,
                                      era_to: int | None = None) -> list[dict]:
        """Generate a 40-song playlist based on Spotify playlist tracks."""
        track_listing = self._build_track_listing(tracks)
        discovery_guide = self._discovery_guidance(discovery)
        era_guide = self._era_guidance(era_from, era_to)

        if mode == "new_discoveries":
            philosophy = """CURATION PHILOSOPHY — NEW DISCOVERIES MODE:
- The listener already knows and loves the playlist songs. Your job is to EXPAND their horizons.
- Recommend songs from DIFFERENT genres, eras, and scenes that share deeper musical DNA.
- No songs by artists already in the playlist.
- Prioritize: unexpected connections, cross-cultural links, genre-bridging artists.
- At least 50% of songs should be from genres NOT represented in the playlist.
- Think: "If you like this playlist, you have NO IDEA you'd also love..."
"""
        else:
            philosophy = """CURATION PHILOSOPHY — SIMILAR SONGS MODE:
- Find songs that would fit seamlessly INTO this playlist.
- Match the mood, energy, tempo range, and sonic palette.
- Include artists from the same scenes, labels, and eras.
- Balance: 60% similar vibes, 40% slightly adjacent discoveries.
- Dig deep: obscure B-sides, overlooked album tracks, international gems.
"""

        dislikes_block = ""
        if dislikes_summary:
            dislikes_block = f"""
PREVIOUSLY DISLIKED SONGS (AVOID these and similar):
{dislikes_summary}
"""

        history_block = ""
        if play_history_summary:
            history_block = f"""
RECENTLY PLAYED SONGS (the listener has heard these recently — DO NOT repeat any of these):
{play_history_summary}
"""

        system_text = f"""You are an expert music curator with encyclopedic knowledge.

{philosophy}
RULES:
- Do NOT repeat any song from the input playlist.
- NEVER repeat a song from the disliked list.
- For EACH song, include a "similar_to" field referencing 1-3 songs from the INPUT playlist
  that this recommendation connects to, and briefly why.

Return a JSON array of exactly 40 objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "similar_to" should be an array like: [{{"artist": "Tame Impala", "album": "Currents", "why": "same dreamy psychedelic production"}}]

Return ONLY the JSON array, no other text."""

        user_text = f"""Create a radio playlist of 40 SONGS based on this Spotify playlist.

PLAYLIST TRACKS:
{track_listing}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet."}
{dislikes_block}
{history_block}
DISCOVERY LEVEL: {discovery}/100 (0 = stick to what I know, 100 = surprise me completely)
{discovery_guide}
{era_guide}"""

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
        )

        u = message.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        logger.info("Radio from-tracks — in:%d out:%d cached:%d", u.input_tokens, u.output_tokens, cached)

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

    def generate_themed_playlist(self, profile: dict, collection: list[dict],
                                    theme: str,
                                    thumbs_summary: str = "",
                                    dislikes_summary: str = "",
                                    play_history_summary: str = "",
                                    discovery: int = 30,
                                    era_from: int | None = None,
                                    era_to: int | None = None) -> list[dict]:
        """Generate a 40-song playlist themed around a user-defined mood/genre/vibe."""
        summary = self._build_profile_summary(profile, collection)
        discovery_guide = self._discovery_guidance(discovery)
        era_guide = self._era_guidance(era_from, era_to)

        dislikes_block = ""
        if dislikes_summary:
            dislikes_block = f"""
PREVIOUSLY DISLIKED SONGS (AVOID these and similar):
{dislikes_summary}
"""

        history_block = ""
        if play_history_summary:
            history_block = f"""
RECENTLY PLAYED SONGS (the listener has heard these recently — DO NOT repeat any of these):
{play_history_summary}
"""

        system_text = f"""You are an expert music curator with encyclopedic knowledge.

Create a themed radio playlist of 40 SONGS focused on the theme: "{theme}"
Interpret the theme broadly — it could be a genre, mood, era, activity, scenario, or vibe.

CURATION PHILOSOPHY:
- Every song should fit the theme "{theme}"
- Still connect to the listener's taste — use their collection as a taste anchor
- Dig deep: obscure B-sides, overlooked album tracks, international gems
- Create flow: sequence songs so each transition feels intentional
- NEVER repeat a song from the disliked list
- Avoid overly obvious hits — dig for the deeper cuts

For EACH song, include a "similar_to" field: an array of 1-3 specific
artist+album combos FROM THE LISTENER'S COLLECTION that this song connects to.

Return a JSON array of exactly 40 objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "similar_to" should be an array like: [{{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich"}}]

Return ONLY the JSON array, no other text."""

        user_text = f"""Create a themed radio playlist based on this listener's collection.

COLLECTION PROFILE:
{summary}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet."}
{dislikes_block}
{history_block}
DISCOVERY LEVEL: {discovery}/100
{discovery_guide}
{era_guide}"""

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_text}],
        )

        u = message.usage
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        logger.info("Radio themed — in:%d out:%d cached:%d", u.input_tokens, u.output_tokens, cached)

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

    def _build_track_listing(self, tracks: list[dict], max_tracks: int = 80) -> str:
        """Format Spotify tracks for the Claude prompt."""
        lines = []
        for t in tracks[:max_tracks]:
            year = f" ({t['year']})" if t.get("year") else ""
            lines.append(f"  - {t['artist']} - {t['title']} [{t.get('album', '')}]{year}")
        return "\n".join(lines)

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
