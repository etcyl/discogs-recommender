import logging
from concurrent.futures import ThreadPoolExecutor
from youtubesearchpython import VideosSearch

from services.cache import cache
from services.llm_provider import call_llm, parse_llm_json

logger = logging.getLogger(__name__)


class RadioService:
    def __init__(self, anthropic_api_key: str = "",
                 ollama_base_url: str = "http://localhost:11434",
                 ollama_model: str = "llama3.1:8b"):
        self.anthropic_api_key = anthropic_api_key
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model

    BATCH_SIZE = 25
    HAIKU_BATCH_SIZE = 15   # Haiku is smaller — fewer songs per batch for reliability
    OLLAMA_BATCH_SIZE = 10  # Smaller batches for local models to avoid truncation

    def _call_and_parse(self, system_text: str, user_text: str,
                        ai_model: str = "claude-sonnet",
                        max_tokens: int = 6000) -> list[dict]:
        """Call LLM and parse the JSON array response."""
        text = call_llm(
            system_prompt=system_text,
            user_prompt=user_text,
            provider=ai_model,
            max_tokens=max_tokens,
            anthropic_api_key=self.anthropic_api_key,
            ollama_base_url=self.ollama_base_url,
            ollama_model=self.ollama_model,
        )
        result = parse_llm_json(text)
        if not result:
            logger.warning("LLM [%s] returned unparseable response (len=%d): %.500s",
                           ai_model, len(text), text)
        return result

    # Variety hints appended to parallel batches to reduce duplicates
    _VARIETY_HINTS = [
        "\nFocus on deep cuts and lesser-known tracks.",
        "\nFocus on different eras and decades than usual.",
        "\nFocus on international and cross-cultural picks.",
        "\nFocus on artists from independent and small labels.",
        "\nFocus on recently released or contemporary music.",
        "\nFocus on classic and influential tracks.",
    ]

    def _batched_generate(self, build_prompts, num_songs: int,
                          ai_model: str = "claude-sonnet",
                          on_batch=None) -> list[dict]:
        """Generate songs using parallel LLM calls, then dedup.

        Fires multiple requests simultaneously for speed, each with a
        different variety hint to encourage diversity, then deduplicates.
        """
        is_ollama = ai_model == "ollama"
        is_haiku = ai_model == "claude-haiku"
        if is_ollama:
            effective_batch = self.OLLAMA_BATCH_SIZE
        elif is_haiku:
            effective_batch = self.HAIKU_BATCH_SIZE
        else:
            effective_batch = self.BATCH_SIZE
        num_workers = max(1, (num_songs + effective_batch - 1) // effective_batch)
        # Cap workers: Ollama 3 (local GPU), Haiku 3 (cheap+fast), Sonnet 2 (API cost)
        max_workers = 3 if (is_ollama or is_haiku) else 2
        num_workers = min(max_workers, num_workers)

        def _max_tokens_for(batch_size):
            if is_ollama:
                return max(2000, batch_size * 60)
            elif is_haiku:
                return max(4000, batch_size * 150)
            return max(4000, batch_size * 150)

        def _run_one(idx):
            batch_size = min(effective_batch, num_songs)
            hint = self._VARIETY_HINTS[idx % len(self._VARIETY_HINTS)]
            system_text, user_text = build_prompts(batch_size, hint)
            return self._call_and_parse(system_text, user_text,
                                        ai_model=ai_model,
                                        max_tokens=_max_tokens_for(batch_size))

        logger.info("Parallel %s: %d workers x %d songs each",
                    ai_model, num_workers, min(effective_batch, num_songs))

        all_songs: list[dict] = []
        seen: set[tuple[str, str]] = set()

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = [pool.submit(_run_one, i) for i in range(num_workers)]
            for i, future in enumerate(futures):
                try:
                    batch = future.result()
                except Exception as e:
                    logger.warning("Parallel batch %d failed: %s", i, e)
                    continue
                if not batch:
                    continue
                for song in batch:
                    key = (song.get("artist", "").lower().strip(),
                           song.get("title", "").lower().strip())
                    if key not in seen:
                        seen.add(key)
                        all_songs.append(song)
                logger.info("Parallel batch %d: +%d songs, total unique %d/%d",
                            i, len(batch), len(all_songs), num_songs)
                if on_batch:
                    on_batch(min(len(all_songs), num_songs), num_songs)

        # If parallel didn't reach target, do one sequential top-up
        if len(all_songs) < num_songs:
            remaining = num_songs - len(all_songs)
            lines = [f"  - {s.get('artist','')} - {s.get('title','')}" for s in all_songs]
            already_picked = "\n\nALREADY SELECTED (do NOT repeat):\n" + "\n".join(lines)
            system_text, user_text = build_prompts(remaining, already_picked)
            batch = self._call_and_parse(system_text, user_text,
                                         ai_model=ai_model,
                                         max_tokens=_max_tokens_for(remaining))
            if batch:
                for song in batch:
                    key = (song.get("artist", "").lower().strip(),
                           song.get("title", "").lower().strip())
                    if key not in seen:
                        seen.add(key)
                        all_songs.append(song)
                logger.info("Top-up: total unique %d/%d", len(all_songs), num_songs)

        return all_songs[:num_songs]

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
                          era_to: int | None = None,
                          ai_model: str = "claude-sonnet",
                          num_songs: int = 50,
                          on_batch=None) -> list[dict]:
        """Ask LLM to generate a radio playlist (batched for reliability)."""
        is_small_model = ai_model in ("ollama", "claude-haiku")
        summary = self._build_profile_summary(profile, collection, compact=is_small_model)
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

        is_ollama = ai_model == "ollama"
        is_haiku = ai_model == "claude-haiku"

        def build_prompts(batch_size, already_picked):
            if is_ollama or is_haiku:
                system_text = f"""You are a music curator. Recommend {batch_size} songs.
Return a JSON array of objects with keys: artist, title, album, year
Return ONLY the JSON array, no other text."""

                user_text = f"""Recommend {batch_size} songs for a listener with this taste:
{summary}
{f"Liked: {thumbs_summary}" if thumbs_summary else ""}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
{f"Recently played (DO NOT repeat): {play_history_summary}" if play_history_summary else ""}
Discovery: {discovery}/100 (0=familiar, 100=adventurous)
{discovery_guide}
{era_guide}{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge — deeper than
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

Return a JSON array of exactly {batch_size} objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "reason" should be 1 sentence explaining the specific connection to their taste.
The "similar_to" should be an array like: [{{"artist": "Radiohead", "album": "OK Computer", "why": "shared producer Nigel Godrich"}}]

Return ONLY the JSON array, no other text."""

                user_text = f"""Create a radio playlist of {batch_size} SONGS based on this listener's Discogs collection.

COLLECTION PROFILE:
{summary}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet — this is their first session."}
{dislikes_block}
{history_block}
DISCOVERY LEVEL: {discovery}/100 (0 = stick to what I know, 100 = surprise me completely)
{discovery_guide}
{era_guide}{already_picked}"""

            return system_text, user_text

        return self._batched_generate(build_prompts, num_songs, ai_model=ai_model,
                                      on_batch=on_batch)

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
                                      era_to: int | None = None,
                                      ai_model: str = "claude-sonnet",
                                      num_songs: int = 50,
                                      on_batch=None) -> list[dict]:
        """Generate a playlist based on Spotify/upload tracks (batched)."""
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

        is_ollama = ai_model == "ollama"
        is_haiku = ai_model == "claude-haiku"

        def build_prompts(batch_size, already_picked):
            if is_ollama or is_haiku:
                mode_hint = "new discoveries from different genres" if mode == "new_discoveries" else "similar songs"
                system_text = f"""You are a music curator. Recommend {batch_size} {mode_hint}.
Return a JSON array of objects with keys: artist, title, album, year
Do NOT repeat songs from the input playlist. Return ONLY the JSON array."""

                user_text = f"""Recommend {batch_size} songs based on this playlist:
{track_listing}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
Discovery: {discovery}/100
{era_guide}{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge.

{philosophy}
RULES:
- Do NOT repeat any song from the input playlist.
- NEVER repeat a song from the disliked list.
- For EACH song, include a "similar_to" field referencing 1-3 songs from the INPUT playlist
  that this recommendation connects to, and briefly why.

Return a JSON array of exactly {batch_size} objects with these keys:
"artist", "title", "album", "year", "reason", "similar_to"

The "similar_to" should be an array like: [{{"artist": "Tame Impala", "album": "Currents", "why": "same dreamy psychedelic production"}}]

Return ONLY the JSON array, no other text."""

                user_text = f"""Create a radio playlist of {batch_size} SONGS based on this Spotify playlist.

PLAYLIST TRACKS:
{track_listing}

PREVIOUSLY LIKED SONGS (from radio thumbs-up):
{thumbs_summary or "None yet."}
{dislikes_block}
{history_block}
DISCOVERY LEVEL: {discovery}/100 (0 = stick to what I know, 100 = surprise me completely)
{discovery_guide}
{era_guide}{already_picked}"""

            return system_text, user_text

        return self._batched_generate(build_prompts, num_songs, ai_model=ai_model,
                                      on_batch=on_batch)

    def generate_themed_playlist(self, profile: dict, collection: list[dict],
                                    theme: str,
                                    thumbs_summary: str = "",
                                    dislikes_summary: str = "",
                                    play_history_summary: str = "",
                                    discovery: int = 30,
                                    era_from: int | None = None,
                                    era_to: int | None = None,
                                    ai_model: str = "claude-sonnet",
                                    num_songs: int = 50,
                                    on_batch=None) -> list[dict]:
        """Generate a themed playlist around a user-defined mood/genre/vibe (batched)."""
        summary = self._build_profile_summary(profile, collection, compact=(ai_model in ("ollama", "claude-haiku")))
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

        is_ollama = ai_model == "ollama"
        is_haiku = ai_model == "claude-haiku"

        def build_prompts(batch_size, already_picked):
            if is_ollama or is_haiku:
                system_text = f"""You are a music curator. Recommend {batch_size} songs matching the theme: "{theme}"
Return a JSON array of objects with keys: artist, title, album, year
Return ONLY the JSON array, no other text."""

                user_text = f"""Recommend {batch_size} songs matching "{theme}" for a listener with this taste:
{summary}
{f"Liked: {thumbs_summary}" if thumbs_summary else ""}
{f"Disliked (AVOID): {dislikes_summary}" if dislikes_summary else ""}
Discovery: {discovery}/100
{era_guide}{already_picked}"""
            else:
                system_text = f"""You are an expert music curator with encyclopedic knowledge.

Create a themed radio playlist of {batch_size} SONGS focused on the theme: "{theme}"
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

Return a JSON array of exactly {batch_size} objects with these keys:
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
{era_guide}{already_picked}"""

            return system_text, user_text

        return self._batched_generate(build_prompts, num_songs, ai_model=ai_model,
                                      on_batch=on_batch)

    def _build_track_listing(self, tracks: list[dict], max_tracks: int = 80) -> str:
        """Format Spotify tracks for the Claude prompt."""
        lines = []
        for t in tracks[:max_tracks]:
            year = f" ({t['year']})" if t.get("year") else ""
            lines.append(f"  - {t['artist']} - {t['title']} [{t.get('album', '')}]{year}")
        return "\n".join(lines)

    def _build_profile_summary(self, profile: dict, collection: list[dict],
                               compact: bool = False) -> str:
        if compact:
            # Shorter summary for local models — fewer input tokens
            genres = ', '.join(g for g, _ in profile['top_genres'][:8])
            artists = ', '.join(a for a, _ in profile['top_artists'][:10])
            samples = []
            for r in collection[:15]:
                a = ", ".join(r.get("artists", ["Unknown"]))
                samples.append(f"{a} - {r.get('title', '')}")
            return f"Genres: {genres}\nArtists: {artists}\nSample: {'; '.join(samples)}"

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
